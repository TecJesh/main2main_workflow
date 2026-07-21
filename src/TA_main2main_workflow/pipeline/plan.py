"""Pipeline step: Plan steps — split upstream commits by line budget.

Groups upstream commits into ordered steps based on changed lines in
key source directories.  LLVM-hash-changing commits get solo steps.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from TA_main2main_workflow.utils.config import TAConfig
from TA_main2main_workflow.utils.context import WorkflowContext
from TA_main2main_workflow.utils import (
    WORKSPACE_DIR,
    STEPS_FILE,
    STEPS_DIR,
    LLVM_HASH_FILE,
    run_git,
)
from TA_main2main_workflow.utils.logging import get_logger

log = get_logger(__name__)


def run_plan(ctx: WorkflowContext, config: TAConfig) -> WorkflowContext:
    """Plan merge steps (with resume support).

    If ``config.resume`` is set and ``steps.json`` exists, loads cached
    results.  Otherwise runs full planning via :func:`plan_steps`.
    """
    steps_file = WORKSPACE_DIR / STEPS_FILE
    if config.resume and steps_file.exists():
        log.info("Resume: steps.json exists, skipping plan")
        plan = json.loads(steps_file.read_text(encoding="utf-8"))
        return ctx.copy_with(steps=plan["steps"], total_steps=len(plan["steps"]))

    return plan_steps(ctx, config)


def plan_steps(ctx: WorkflowContext, config: TAConfig) -> WorkflowContext:
    """Split upstream commits into steps and populate ctx.steps.

    If progressive_merge is disabled or there is only 1 commit,
    creates a single step covering all commits.
    """
    triton_path = Path(ctx.triton_ascend_path)
    base = ctx.merge_base
    target = ctx.target_commit
    line_budget = config.line_budget

    commits = ctx.upstream_commits
    log.info(
        f"[plan] Scanning {len(commits)} upstream commits ({base[:8]}..{target[:8]})"
    )
    log.info(f"[plan] Line budget: {line_budget}")

    if config.progressive_merge and len(commits) > 1:
        lines_per_commit, llvm_commits = _scan_commits(triton_path, commits)
        steps = _plan_steps_inner(
            commits, lines_per_commit, base, line_budget, llvm_commits
        )
        _enrich_steps(triton_path, steps)

        plan = {
            "base_commit": base,
            "target_commit": target,
            "line_budget": line_budget,
            "total_steps": len(steps),
            "steps": steps,
        }
        _write_plan(plan)

        log.info(f"[plan] Generated {len(steps)} step(s)")
        return ctx.copy_with(steps=steps, total_steps=len(steps))
    else:
        return ctx.copy_with(
            total_steps=1,
            steps=[
                {
                    "index": 1,
                    "id": "step-1",
                    "commit_count": len(commits),
                    "start_commit": base,
                    "end_commit": target,
                    "source_changed_lines": ctx.changed_lines_total,
                }
            ],
        )


# ═══════════════════════════════════════════════════════════════════════════
# Internal helpers (formerly in scripts/plan_steps.py)
# ═══════════════════════════════════════════════════════════════════════════


def _source_lines_for_commit(repo: Path, sha: str) -> int:
    """Return total lines changed in a single commit."""
    try:
        output = run_git(
            repo, "diff-tree", "--no-commit-id", "--shortstat", sha, quiet=True
        )
    except Exception:
        return 0
    # " 5 files changed, 123 insertions(+), 45 deletions(-)"
    return _parse_shortstat(output)


def _parse_shortstat(output: str) -> int:
    """Parse ``git diff --shortstat`` output and return total lines changed."""
    total = 0
    for part in output.split(","):
        part = part.strip()
        if "insertion" in part or "deletion" in part:
            try:
                total += int(part.split()[0])
            except ValueError:
                pass
    return total


def _commit_changed_llvm_hash(repo: Path, sha: str) -> bool:
    try:
        output = run_git(
            repo, "diff-tree", "--no-commit-id", "--name-only", "-r", sha, quiet=True
        )
        return LLVM_HASH_FILE in output
    except Exception:
        return False


def _scan_commits(
    repo: Path, commits: list[dict[str, str]]
) -> tuple[dict[str, int], set[str]]:
    lines_per_commit: dict[str, int] = {}
    llvm_commits: set[str] = set()
    for i, c in enumerate(commits):
        lines = _source_lines_for_commit(repo, c["sha"])
        lines_per_commit[c["sha"]] = lines
        if _commit_changed_llvm_hash(repo, c["sha"]):
            llvm_commits.add(c["sha"])
            log.info(f"[plan] LLVM version change: {c['sha'][:8]} {c['subject'][:80]}")
        if (i + 1) % 50 == 0:
            log.info(f"[plan] ... scanned {i + 1}/{len(commits)} commits")
    return lines_per_commit, llvm_commits


def _make_step(
    index: int,
    commits: list[dict[str, str]],
    start: str,
    lines: int,
    budget: int,
    reason: str = "line_budget",
) -> dict[str, Any]:
    return {
        "index": index,
        "id": f"step-{index}",
        "commits": commits,
        "commit_count": len(commits),
        "start_commit": start,
        "end_commit": commits[-1]["sha"],
        "source_changed_lines": lines,
        "line_budget": budget,
        "reason": reason,
    }


def _plan_steps_inner(
    commits: list[dict[str, str]],
    lines_per_commit: dict[str, int],
    base: str,
    budget: int,
    llvm_commits: set[str],
) -> list[dict[str, Any]]:
    steps: list[dict[str, Any]] = []
    step_commits: list[dict[str, str]] = []
    step_lines = 0
    start = base

    for commit in commits:
        sha = commit["sha"]
        lines = lines_per_commit.get(sha, 0)

        # LLVM change → solo step
        if sha in llvm_commits:
            if step_commits:
                steps.append(
                    _make_step(len(steps) + 1, step_commits, start, step_lines, budget)
                )
                start = steps[-1]["end_commit"]
                step_commits, step_lines = [], 0
            steps.append(
                _make_step(
                    len(steps) + 1,
                    [commit],
                    start,
                    lines,
                    budget,
                    reason="llvm_version",
                )
            )
            start = steps[-1]["end_commit"]
            continue

        # Oversized → solo step
        if lines > budget:
            if step_commits:
                steps.append(
                    _make_step(len(steps) + 1, step_commits, start, step_lines, budget)
                )
                start = steps[-1]["end_commit"]
                step_commits, step_lines = [], 0
            steps.append(
                _make_step(
                    len(steps) + 1, [commit], start, lines, budget, reason="oversized"
                )
            )
            start = steps[-1]["end_commit"]
            continue

        # Would exceed budget → flush
        if step_lines + lines > budget:
            steps.append(
                _make_step(len(steps) + 1, step_commits, start, step_lines, budget)
            )
            start = steps[-1]["end_commit"]
            step_commits, step_lines = [], 0

        step_commits.append(commit)
        step_lines += lines

    if step_commits:
        steps.append(
            _make_step(len(steps) + 1, step_commits, start, step_lines, budget)
        )

    return steps


def _enrich_steps(repo: Path, steps: list[dict[str, Any]]) -> None:
    for step in steps:
        step["upstream_patch"] = run_git(
            repo, "diff", f"{step['start_commit']}..{step['end_commit']}", quiet=True
        )
        step["changed_files"] = run_git(
            repo,
            "diff",
            "--name-only",
            f"{step['start_commit']}..{step['end_commit']}",
            quiet=True,
        )


def _write_plan(plan: dict[str, Any]) -> None:
    steps_dir = WORKSPACE_DIR / STEPS_DIR
    steps_dir.mkdir(parents=True, exist_ok=True)
    (WORKSPACE_DIR / STEPS_FILE).write_text(
        json.dumps(plan, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    for step in plan["steps"]:
        step_dir = steps_dir / step["id"]
        step_dir.mkdir(parents=True, exist_ok=True)
        (step_dir / "upstream.patch").write_text(
            step["upstream_patch"], encoding="utf-8"
        )
        (step_dir / "changed_files.txt").write_text(
            step["changed_files"], encoding="utf-8"
        )
        lines = [f"{c['sha'][:8]}  {c['subject']}" for c in step["commits"]]
        (step_dir / "commits.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
