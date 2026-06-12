#!/usr/bin/env python3
"""Deterministic step planner for the TA main2main upstream sync pipeline.

Splits a range of upstream Triton commits into ordered steps based on changed
lines in key source directories. Commits that do not touch python/triton/,
lib/, or include/ are skipped (they don't affect the Ascend adaption).

Algorithm:
  1. git log --reverse base..target → ordered commit list
  2. For each commit, git diff-tree --numstat → source dir changed lines
  3. Keep only commits that touch source directories; skip others
  4. Commits accumulate into a step until source_changed_lines > LINE_BUDGET
     or the step reaches the commit-count budget
  5. A single commit with source_changed_lines > LINE_BUDGET becomes its own step

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
    ENV_COMMIT_BUDGET, BASE_COMMIT_COUNT_BUDGET,
    commit_count_budget, run_git,
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


def _make_step(
    index: int,
    commits: list[dict[str, str]],
    start_commit: str,
    total_lines: int,
    line_budget: int,
) -> dict[str, Any]:
    """Build a step dict from accumulated commits.

    The 'commits' field stores objects with 'sha' and 'subject' keys,
    matching the vllm-ascend main2main_flow format.
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
        "commit_count_budget": commit_count_budget(line_budget),
    }


def _plan_steps(
    commits: list[dict[str, str]],
    lines_per_commit: dict[str, int],
    base_commit: str,
    line_budget: int = LINE_BUDGET,
) -> list[dict[str, Any]]:
    """Group commits into steps respecting the line and count budgets.

    Algorithm:
      - Skip commits that touch zero source lines (no impact on adaption).
      - A commit whose source lines exceed LINE_BUDGET becomes its own step.
      - Otherwise accumulate until budget exceeded, then flush current step.
    """
    eligible = [c for c in commits if lines_per_commit.get(c["sha"], 0) > 0]

    steps: list[dict[str, Any]] = []
    step_commits: list[dict[str, str]] = []
    step_lines = 0
    start = base_commit
    ccb = commit_count_budget(line_budget)

    for commit in eligible:
        lines = lines_per_commit[commit["sha"]]

        # ── Oversized single commit: flush pending, emit solo step ──
        if lines > line_budget:
            if step_commits:
                steps.append(_make_step(len(steps) + 1, step_commits, start, step_lines, line_budget))
                start = steps[-1]["end_commit"]
                step_commits = []
                step_lines = 0
            steps.append(_make_step(len(steps) + 1, [commit], start, lines, line_budget))
            start = steps[-1]["end_commit"]
            continue

        # ── Would exceed budget → flush current step first ──
        if step_lines + lines > line_budget or len(step_commits) >= ccb:
            steps.append(_make_step(len(steps) + 1, step_commits, start, step_lines, line_budget))
            start = steps[-1]["end_commit"]
            step_commits = []
            step_lines = 0

        step_commits.append(commit)
        step_lines += lines

    # ── Flush remaining ──
    if step_commits:
        steps.append(_make_step(len(steps) + 1, step_commits, start, step_lines, line_budget))

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

    Commit-count budget is derived from line_budget via commit_count_budget(),
    which can be tuned with TA_COMMIT_BUDGET env var (default base: 5).

    Returns:
        Plan dict with keys: base_commit, target_commit, total_commits, steps.
    """
    if line_budget is None:
        line_budget = int(os.getenv("TA_LINE_BUDGET", str(LINE_BUDGET)))

    commits = _list_commits(triton_path, base_commit, target_commit)
    ccb = commit_count_budget(line_budget)

    print(f"[plan] Scanning {len(commits)} upstream commits "
          f"({base_commit[:8]}..{target_commit[:8]})")
    print(f"[plan] Line budget: {line_budget}, commit-count budget: {ccb} "
          f"(base={os.getenv('TA_COMMIT_BUDGET', str(BASE_COMMIT_COUNT_BUDGET))})")

    # Count changed source lines per commit
    lines_per_commit: dict[str, int] = {}
    eligible_count = 0
    for i, c in enumerate(commits):
        lines = _source_lines_for_commit(triton_path, c["sha"])
        lines_per_commit[c["sha"]] = lines
        if lines > 0:
            eligible_count += 1
        if (i + 1) % 50 == 0:
            print(f"[plan]   ... scanned {i + 1}/{len(commits)} commits")

    skipped = len(commits) - eligible_count
    if skipped:
        print(f"[plan] Skipped {skipped} commits that don't touch source dirs "
              f"({', '.join(SOURCE_DIRS)})")

    steps = _plan_steps(commits, lines_per_commit, base_commit, line_budget)
    _enrich_steps_with_diff(triton_path, steps)

    plan = {
        "base_commit": base_commit,
        "target_commit": target_commit,
        "line_budget": line_budget,
        "commit_count_budget": commit_count_budget(line_budget),
        "total_source_commits": eligible_count,
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
        print(f"        {s['id']}: {s['commit_count']} commits, "
              f"{s['source_changed_lines']} lines "
              f"({'OVERSIZED' if s['source_changed_lines'] > line_budget else 'OK'})")

    return plan
