"""Pipeline step: Plan merge steps — split upstream commits by line budget."""

from __future__ import annotations
import json
from pathlib import Path
from TA_main2main_workflow.utils.config import TAConfig
from TA_main2main_workflow.utils.context import WorkflowContext
from TA_main2main_workflow.utils.git import run_git
from TA_main2main_workflow.utils.logging import get_logger
from TA_main2main_workflow.utils import (
    WORKSPACE_DIR, STEPS_FILE, STEPS_DIR, LLVM_HASH_FILE,
)

log = get_logger(__name__)


def run_plan(ctx: WorkflowContext, config: TAConfig) -> WorkflowContext:
    steps_file = WORKSPACE_DIR / STEPS_FILE
    if config.resume and steps_file.exists():
        log.info("Resume: steps.json exists, skipping plan")
        plan = json.loads(steps_file.read_text(encoding="utf-8"))
        return ctx.copy_with(steps=plan["steps"], total_steps=len(plan["steps"]))
    return _plan_steps(ctx, config)


def _plan_steps(ctx: WorkflowContext, config: TAConfig) -> WorkflowContext:
    ascend_path = Path(ctx.triton_ascend_path)
    base = ctx.merge_base
    target = ctx.target_commit
    commits = ctx.upstream_commits

    log.info(f"Scanning {len(commits)} upstream commits ({base[:8]}..{target[:8]})")

    if config.progressive_merge and len(commits) > 1:
        lines_per_commit, llvm_commits = _scan_commits(ascend_path, commits)
        steps = _build_steps(commits, lines_per_commit, base,
                             config.line_budget, llvm_commits)
        _enrich_steps(ascend_path, steps)
        plan = {
            "base_commit": base, "target_commit": target,
            "line_budget": config.line_budget,
            "total_steps": len(steps), "steps": steps,
        }
        _write_plan(plan)
        log.info(f"Generated {len(steps)} step(s)")
        return ctx.copy_with(steps=steps, total_steps=len(steps))

    return ctx.copy_with(
        total_steps=1,
        steps=[{
            "index": 1, "id": "step-1",
            "commit_count": len(commits),
            "start_commit": base, "end_commit": target,
            "source_changed_lines": ctx.changed_lines_total,
        }],
    )


# ── Internal helpers ────────────────────────────────────────────────────────

def _source_lines_for_commit(repo: Path, sha: str) -> int:
    try:
        output = run_git(repo, "diff-tree", "--no-commit-id", "--shortstat", sha, quiet=True)
    except Exception:
        return 0
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
        output = run_git(repo, "diff-tree", "--no-commit-id", "--name-only", "-r", sha, quiet=True)
        return LLVM_HASH_FILE in output
    except Exception:
        return False


def _scan_commits(repo: Path, commits: list[dict]) -> tuple[dict, set]:
    lines_per_commit: dict[str, int] = {}
    llvm_commits: set[str] = set()
    for i, c in enumerate(commits):
        lines = _source_lines_for_commit(repo, c["sha"])
        lines_per_commit[c["sha"]] = lines
        if _commit_changed_llvm_hash(repo, c["sha"]):
            llvm_commits.add(c["sha"])
        if (i + 1) % 50 == 0:
            log.info(f"  ... scanned {i + 1}/{len(commits)} commits")
    return lines_per_commit, llvm_commits


def _build_steps(commits: list[dict], lines_per_commit: dict, base: str,
                 budget: int, llvm_commits: set) -> list[dict]:
    steps: list[dict] = []
    step_commits: list[dict] = []
    step_lines = 0
    start = base

    for commit in commits:
        sha = commit["sha"]
        lines = lines_per_commit.get(sha, 0)

        if sha in llvm_commits:
            if step_commits:
                steps.append(_make_step(len(steps) + 1, step_commits, start, step_lines, budget))
                start = steps[-1]["end_commit"]
                step_commits, step_lines = [], 0
            steps.append(_make_step(len(steps) + 1, [commit], start, lines, budget, reason="llvm_version"))
            start = steps[-1]["end_commit"]
            continue
        if lines > budget:
            if step_commits:
                steps.append(_make_step(len(steps) + 1, step_commits, start, step_lines, budget))
                start = steps[-1]["end_commit"]
                step_commits, step_lines = [], 0
            steps.append(_make_step(len(steps) + 1, [commit], start, lines, budget, reason="oversized"))
            start = steps[-1]["end_commit"]
            continue
        if step_lines + lines > budget:
            steps.append(_make_step(len(steps) + 1, step_commits, start, step_lines, budget))
            start = steps[-1]["end_commit"]
            step_commits, step_lines = [], 0
        step_commits.append(commit)
        step_lines += lines

    if step_commits:
        steps.append(_make_step(len(steps) + 1, step_commits, start, step_lines, budget))
    return steps


def _make_step(index: int, commits: list[dict], start: str, lines: int,
               budget: int, reason: str = "line_budget") -> dict:
    return {
        "index": index, "id": f"step-{index}",
        "commits": commits, "commit_count": len(commits),
        "start_commit": start, "end_commit": commits[-1]["sha"],
        "source_changed_lines": lines, "line_budget": budget,
        "reason": reason,
    }


def _enrich_steps(repo: Path, steps: list[dict]) -> None:
    for step in steps:
        step["upstream_patch"] = run_git(
            repo, "diff", f"{step['start_commit']}..{step['end_commit']}", quiet=True)
        step["changed_files"] = run_git(
            repo, "diff", "--name-only", f"{step['start_commit']}..{step['end_commit']}", quiet=True)


def _write_plan(plan: dict) -> None:
    steps_dir = WORKSPACE_DIR / STEPS_DIR
    steps_dir.mkdir(parents=True, exist_ok=True)
    (WORKSPACE_DIR / STEPS_FILE).write_text(
        json.dumps(plan, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    for step in plan["steps"]:
        step_dir = steps_dir / step["id"]
        step_dir.mkdir(parents=True, exist_ok=True)
        (step_dir / "upstream.patch").write_text(step["upstream_patch"], encoding="utf-8")
        (step_dir / "changed_files.txt").write_text(step["changed_files"], encoding="utf-8")
        lines = [f"{c['sha'][:8]}  {c['subject']}" for c in step["commits"]]
        (step_dir / "commits.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
