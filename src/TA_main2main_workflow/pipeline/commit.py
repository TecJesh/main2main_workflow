"""Pipeline step: Commit step progress.

Handles submodule commit first (AscendNPU-IR), then parent repo commit.
"""

from __future__ import annotations

from pathlib import Path

from TA_main2main_workflow.utils.config import TAConfig
from TA_main2main_workflow.utils.context import WorkflowContext
from TA_main2main_workflow.utils.logging import get_logger
from TA_main2main_workflow.utils.git import run_git
from TA_main2main_workflow.utils.submodule import (
    commit_submodule,
    submodule_has_changes,
)
from TA_main2main_workflow.pipeline.pre_ci import cleanup_temp_files

log = get_logger(__name__)


def commit_step(ctx: WorkflowContext, config: TAConfig) -> WorkflowContext:
    """Commit all changes for the current step.

    Order:
    1. Commit AscendNPU-IR submodule if it has changes
    2. Clean temp files
    3. Stage and commit parent repo
    """
    ascend_path = Path(ctx.triton_ascend_path)
    step = ctx.steps[ctx.current_step]
    step_id = step["id"]

    # ── 1. Submodule first ────────────────────────────────────────────
    if submodule_has_changes(ascend_path):
        target_short = ctx.target_commit[:12] if ctx.target_commit else "HEAD"
        commit_submodule(ascend_path, f"[Sync](fix) AI fix for {target_short}\n")

    # ── 2. Clean temp files ───────────────────────────────────────────
    cleanup_temp_files(ascend_path)

    # ── 3. Stage and commit parent repo ───────────────────────────────
    staged = run_git(ascend_path, "status", "--porcelain").strip()
    if not staged:
        log.info(f"[{step_id}] Nothing to commit")
        return ctx

    # Print staged files for visibility
    staged_files = [line[3:] for line in staged.splitlines() if line.strip()]
    log.info(f"Files staged ({len(staged_files)}):")
    for f in staged_files[:30]:
        log.info(f"  {f}")
    if len(staged_files) > 30:
        log.info(f"  ... and {len(staged_files) - 30} more")

    start_short = step.get("start_commit", "?")[:12]
    end_short = step["end_commit"][:12]
    msg = (
        f"[Sync](feat) Merge upstream commits for step {step_id}"
        f"({start_short}..{end_short}, {step['commit_count']} commits)\n\n"
        f"Upstream range: {start_short}..{end_short}\n"
        f"Step: {ctx.current_step + 1}/{ctx.total_steps}\n"
        f"Commits: {step['commit_count']}\n"
    )
    try:
        run_git(ascend_path, "add", "-A")
        run_git(ascend_path, "commit", "-s", "-m", msg)
        log.status(True, f"Committed step {step_id}")
    except Exception as e:
        if "nothing to commit" not in str(getattr(e, "stderr", "")):
            log.warning(f"Commit failed: {e}")

    return ctx
