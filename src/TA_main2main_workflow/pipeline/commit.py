"""Pipeline step: Commit step progress."""

from __future__ import annotations
from pathlib import Path
from TA_main2main_workflow.utils.config import TAConfig
from TA_main2main_workflow.utils.context import WorkflowContext
from TA_main2main_workflow.utils.logging import get_logger
from TA_main2main_workflow.utils.git import run_git, submodule_has_changes, commit_submodule

log = get_logger(__name__)


def commit_step(ctx: WorkflowContext, config: TAConfig) -> WorkflowContext:
    ascend_path = Path(ctx.triton_ascend_path)
    step = ctx.steps[ctx.current_step]
    step_id = step["id"]

    if submodule_has_changes(ascend_path):
        commit_submodule(
            ascend_path,
            f"[Sync](fix) AI fix for {ctx.target_commit[:12]}\n")

    if not run_git(ascend_path, "status", "--porcelain").strip():
        log.info(f"[{step_id}] Nothing to commit")
        return ctx

    end_short = step["end_commit"][:12]
    msg = (
        f"sync: merge upstream commits for step {step_id}\n\n"
        f"Upstream range: {step.get('start_commit', '?')[:12]}..{end_short}\n"
        f"Step: {ctx.current_step + 1}/{ctx.total_steps}\n"
        f"Commits: {step['commit_count']}\n")
    try:
        run_git(ascend_path, "add", "-A")
        run_git(ascend_path, "commit", "-s", "-m", msg)
        log.status(True, f"Committed step {step_id}")
    except Exception as e:
        if "nothing to commit" not in str(getattr(e, "stderr", "")):
            log.warning(f"Commit failed: {e}")

    return ctx
