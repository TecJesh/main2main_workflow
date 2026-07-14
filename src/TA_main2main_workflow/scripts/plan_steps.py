#!/usr/bin/env python3
"""Deterministic step planner for the TA main2main upstream sync pipeline.

Splits a range of upstream Triton commits into ordered steps based on changed
lines in key source directories. Every commit between base and target is
included — no commits are skipped, including those that touch zero source
lines (they are still tracked but contribute 0 to the line budget).

Algorithm (in priority order):
  1. LLVM version change → solo step:
     If a commit modifies cmake/llvm-hash.txt it MUST be merged alone,
     regardless of its source-line count. Pending commits are flushed first.
  2. Oversized single commit:
     A commit whose source lines exceed LINE_BUDGET becomes its own step.
  3. Line-budget grouping:
     Commits accumulate into a step until source_changed_lines > LINE_BUDGET
     (no commit-count limit — as many commits as fit within the line budget).

The LINE_BUDGET can be controlled via TA_LINE_BUDGET env var (default: 1000).

Output:
  - <workspace>/steps.json  — machine-readable plan
  - <workspace>/steps/<step-id>/upstream.patch  — per-step upstream diff
  - <workspace>/steps/<step-id>/changed_files.txt — per-step changed files
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from TA_main2main_workflow.utils import (
    WORKSPACE_DIR, STEPS_FILE, STEPS_DIR, LINE_BUDGET, SOURCE_DIRS,
    LLVM_HASH_FILE, run_git,
)


def _list_commits(repo: Path, base: str, target: str) -> list[dict[str, str]]:
    """List all commits between base and target, ordered chronologically."""
    log_output = run_git(
        repo, "log", "--reverse", "--format=%H%x1f%s", f"{base}..{target}"
    )
    commits: list[dict[str, str]] = []
    for line in log_output.strip().splitlines():
        if not line.strip():
            continue
        parts = line.split("\x1f", 1)
        commits.append({
            "sha": parts[0].strip(),
            "subject": parts[1].strip() if len(parts) > 1 else "",
        })
    return commits


def _source_lines_for_commit(repo: Path, sha: str) -> int:
    """Count changed lines in SOURCE_DIRS for a single commit using diff-tree."""
    total = 0
    for src_dir in SOURCE_DIRS:
        try:
            output = run_git(
                repo, "diff-tree", "--no-commit-id", "-r", "--numstat",
                sha, "--", f":(top){src_dir}",
            )
        except Exception:
            continue
        for line in output.strip().splitlines():
            if not line.strip():
                continue
            parts = line.split("\t")
            if len(parts) >= 3:
                added = int(parts[0]) if parts[0] != "-" else 0
                deleted = int(parts[1]) if parts[1] != "-" else 0
                total += added + deleted
    return total


def _commit_changed_llvm_hash(repo: Path, sha: str) -> bool:
    """Check if a single commit modified cmake/llvm-hash.txt.

    Uses git diff-tree to list files changed by *sha*, then checks whether
    LLVM_HASH_FILE appears in the output.  A commit that touches this file
    must become a solo step regardless of its source-line count.
    """
    try:
        output = run_git(repo, "diff-tree", "--no-commit-id", "--name-only", "-r", sha)
        return LLVM_HASH_FILE in output
    except Exception:
        return False


def _make_step(
    index: int,
    commits: list[dict[str, str]],
    start_commit: str,
    total_lines: int,
    line_budget: int,
    reason: str = "line_budget",
) -> dict[str, Any]:
    """Build a step dict from accumulated commits.

    The 'commits' field stores objects with 'sha' and 'subject' keys,
    matching the vllm-ascend main2main_flow format.

    *reason* explains why this step was formed:
      - ``"line_budget"`` — normal grouping by line budget
      - ``"llvm_version"`` — solo step because commit changed llvm-hash.txt
      - ``"oversized"``    — solo step because a single commit exceeds budget
    """
    return {
        "index": index,
        "id": f"step-{index}",
        "commits": commits,  # list of {"sha": ..., "subject": ...}
        "commit_count": len(commits),
        "start_commit": start_commit,
        "end_commit": commits[-1]["sha"],
        "source_changed_lines": total_lines,
        "line_budget": line_budget,
        "reason": reason,
    }


def _plan_steps(
    commits: list[dict[str, str]],
    lines_per_commit: dict[str, int],
    base_commit: str,
    line_budget: int = LINE_BUDGET,
    llvm_commits: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Group commits into steps with LLVM-aware planning.

    Every commit in the range is included — even those that touch zero
    source lines (they contribute 0 to the line budget and don't cause
    step splits on their own).

    Algorithm (in priority order):
      1. **LLVM version change → solo step**: If a commit modifies
         ``cmake/llvm-hash.txt`` it MUST be merged alone, regardless of
         its source-line count.  Pending commits are flushed first.
      2. **Oversized single commit**: A commit whose source lines exceed
         LINE_BUDGET becomes its own step.
      3. **Line-budget grouping**: Otherwise accumulate commits until
         ``step_lines + commit_lines > line_budget``, then flush.
         No commit-count cap — as many commits as fit within the budget.
    """
    if llvm_commits is None:
        llvm_commits = set()

    steps: list[dict[str, Any]] = []
    step_commits: list[dict[str, str]] = []
    step_lines = 0
    start = base_commit

    for commit in commits:
        sha = commit["sha"]
        lines = lines_per_commit.get(sha, 0)
        is_llvm_change = sha in llvm_commits

        # ── Rule 1.1: LLVM version change → solo step ──
        if is_llvm_change:
            if step_commits:
                steps.append(_make_step(
                    len(steps) + 1, step_commits, start, step_lines,
                    line_budget, reason="line_budget",
                ))
                start = steps[-1]["end_commit"]
                step_commits = []
                step_lines = 0
            steps.append(_make_step(
                len(steps) + 1, [commit], start, lines, line_budget,
                reason="llvm_version",
            ))
            start = steps[-1]["end_commit"]
            continue

        # ── Rule 2.1: Oversized single commit → solo step ──
        if lines > line_budget:
            if step_commits:
                steps.append(_make_step(
                    len(steps) + 1, step_commits, start, step_lines,
                    line_budget, reason="line_budget",
                ))
                start = steps[-1]["end_commit"]
                step_commits = []
                step_lines = 0
            steps.append(_make_step(
                len(steps) + 1, [commit], start, lines, line_budget,
                reason="oversized",
            ))
            start = steps[-1]["end_commit"]
            continue

        # ── Would exceed line budget → flush current step first ──
        if step_lines + lines > line_budget:
            steps.append(_make_step(
                len(steps) + 1, step_commits, start, step_lines,
                line_budget, reason="line_budget",
            ))
            start = steps[-1]["end_commit"]
            step_commits = []
            step_lines = 0

        step_commits.append(commit)
        step_lines += lines

    # ── Flush remaining ──
    if step_commits:
        steps.append(_make_step(
            len(steps) + 1, step_commits, start, step_lines,
            line_budget, reason="line_budget",
        ))

    return steps


def _enrich_steps_with_diff(triton_path: Path, steps: list[dict[str, Any]]) -> None:
    """Add upstream diff and changed file list to each step.

    Filters to SOURCE_DIRS only so each step's patch is scoped to the
    code that actually needs adaptation (python/triton/, lib/, include/).
    Matches vllm-ascend's approach of filtering to vllm/.
    """
    # Build pathspec arg for git diff filtering: :(top)python/triton/ :(top)lib/ :(top)include/
    pathspec_args: list[str] = []
    for d in SOURCE_DIRS:
        pathspec_args.extend(["--", f":(top){d}"])

    for step in steps:
        step["upstream_patch"] = run_git(
            triton_path, "diff",
            f"{step['start_commit']}..{step['end_commit']}",
            *pathspec_args,
        )
        changed_files = run_git(
            triton_path, "diff", "--name-only",
            f"{step['start_commit']}..{step['end_commit']}",
            *pathspec_args,
        )
        step["changed_files"] = changed_files
        step["files_changed"] = sorted(
            f for f in changed_files.strip().splitlines() if f
        )


def run_plan(
    triton_path: Path,
    base_commit: str,
    target_commit: str,
    line_budget: int | None = None,
) -> dict[str, Any]:
    """Main entry point: plan steps and write steps.json + per-step artifacts.

    Args:
        triton_path: Path to the upstream Triton git repository.
        base_commit: Merge-base commit (start of the range).
        target_commit: Target upstream commit (end of the range).
        line_budget: Max source lines per step. Reads TA_LINE_BUDGET env var
                     if omitted, falls back to LINE_BUDGET (1000).

    Steps are determined solely by the line budget — there is no
    commit-count limit. All commits between base and target are included.

    Returns:
        Plan dict with keys: base_commit, target_commit, total_commits, steps.
    """
    if line_budget is None:
        line_budget = int(os.getenv("TA_LINE_BUDGET", str(LINE_BUDGET)))

    commits = _list_commits(triton_path, base_commit, target_commit)

    print(f"[plan] Scanning {len(commits)} upstream commits "
          f"({base_commit[:8]}..{target_commit[:8]})")
    print(f"[plan] Line budget: {line_budget} (no commit-count limit)")

    # Count changed source lines per commit + detect LLVM version changes
    lines_per_commit: dict[str, int] = {}
    llvm_commits: set[str] = set()
    source_touching_count = 0
    for i, c in enumerate(commits):
        lines = _source_lines_for_commit(triton_path, c["sha"])
        lines_per_commit[c["sha"]] = lines
        if lines > 0:
            source_touching_count += 1
        # Rule 1.1: check if this commit changed cmake/llvm-hash.txt
        if _commit_changed_llvm_hash(triton_path, c["sha"]):
            llvm_commits.add(c["sha"])
            print(f"[plan]   LLVM version change detected: {c['sha'][:8]} {c['subject'][:80]}")
        if (i + 1) % 50 == 0:
            print(f"[plan]   ... scanned {i + 1}/{len(commits)} commits")

    if source_touching_count < len(commits):
        print(f"[plan] {len(commits) - source_touching_count} commits touch zero "
              f"source lines — included in steps with 0 line contribution")

    if llvm_commits:
        print(f"[plan] {len(llvm_commits)} commit(s) changed LLVM hash "
              f"— each will be a solo merge step")

    steps = _plan_steps(commits, lines_per_commit, base_commit, line_budget,
                        llvm_commits=llvm_commits)
    _enrich_steps_with_diff(triton_path, steps)

    plan = {
        "base_commit": base_commit,
        "target_commit": target_commit,
        "line_budget": line_budget,
        "total_source_commits": source_touching_count,
        "total_commits": sum(s["commit_count"] for s in steps),
        "total_steps": len(steps),
        "steps": steps,
    }

    # ── Write steps.json ──
    steps_dir = WORKSPACE_DIR / STEPS_DIR
    steps_dir.mkdir(parents=True, exist_ok=True)
    (WORKSPACE_DIR / STEPS_FILE).write_text(
        json.dumps(plan, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )

    # ── Write per-step artifacts ──
    for step in steps:
        step_dir = steps_dir / step["id"]
        step_dir.mkdir(parents=True, exist_ok=True)
        (step_dir / "upstream.patch").write_text(
            step["upstream_patch"], encoding="utf-8"
        )
        (step_dir / "changed_files.txt").write_text(
            step["changed_files"], encoding="utf-8"
        )
        # Write a human-readable commit list for this step
        commit_list_lines = []
        for c in step["commits"]:
            commit_list_lines.append(f"{c['sha'][:8]}  {c['subject']}")
        (step_dir / "commits.txt").write_text(
            "\n".join(commit_list_lines) + "\n", encoding="utf-8"
        )

    print(f"[plan] Generated {len(steps)} step(s) totaling "
          f"{plan['total_commits']} source-touching commits")
    for s in steps:
        reason_tag = ""
        if s.get("reason") == "llvm_version":
            reason_tag = " [LLVM VERSION]"
        elif s.get("reason") == "oversized":
            reason_tag = " [OVERSIZED]"
        print(f"        {s['id']}: {s['commit_count']} commits, "
              f"{s['source_changed_lines']} lines "
              f"({'OVERSIZED' if s['source_changed_lines'] > line_budget else 'OK'})"
              f"{reason_tag}")

    return plan


def plan_steps(
    triton_path: Path,
    base_commit: str,
    target_commit: str,
    line_budget: int | None = None,
) -> list[dict[str, Any]]:
    """Public wrapper: plan steps and return the step list (for testing).

    Same as run_plan() but returns just the steps list instead of the full
    plan dict.  Does NOT write files to disk — call run_plan() for that.
    """
    if line_budget is None:
        line_budget = int(os.getenv("TA_LINE_BUDGET", str(LINE_BUDGET)))

    commits = _list_commits(triton_path, base_commit, target_commit)

    lines_per_commit: dict[str, int] = {}
    llvm_commits: set[str] = set()
    for c in commits:
        lines = _source_lines_for_commit(triton_path, c["sha"])
        lines_per_commit[c["sha"]] = lines
        if _commit_changed_llvm_hash(triton_path, c["sha"]):
            llvm_commits.add(c["sha"])

    return _plan_steps(commits, lines_per_commit, base_commit, line_budget,
                       llvm_commits=llvm_commits)
