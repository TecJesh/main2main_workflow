"""Pipeline step 7: Commit step progress."""
from __future__ import annotations
from pathlib import Path
from TA_main2main_workflow.utils.config import TAConfig
from TA_main2main_workflow.utils.context import WorkflowContext
from TA_main2main_workflow.utils.logging import get_logger
from TA_main2main_workflow.utils.git import run_git
from TA_main2main_workflow.utils.submodule import commit_submodule, submodule_has_changes
from TA_main2main_workflow.pipeline.pre_ci import cleanup_temp_files

log = get_logger(__name__)

def commit_step(ctx: WorkflowContext, config: TAConfig) -> WorkflowContext:
    ascend_path = Path(ctx.triton_ascend_path)
    step = ctx.steps[ctx.current_step]
    step_id = step["id"]

    if submodule_has_changes(ascend_path):
        commit_submodule(ascend_path, f"[Sync](fix) AI fix for {ctx.target_commit[:12]}\n")

    cleanup_temp_files(ascend_path)
    if not run_git(ascend_path, "status", "--porcelain").strip():
        log.info(f"[{step_id}] Nothing to commit")
        return ctx

    end_short = step["end_commit"][:12]
    msg = f"sync: merge upstream commits for step {step_id}\n\nUpstream range: {step.get('start_commit','?')[:12]}..{end_short}\nStep: {ctx.current_step+1}/{ctx.total_steps}\nCommits: {step['commit_count']}\n"
    try:
        run_git(ascend_path, "add", "-A")
        run_git(ascend_path, "commit", "-s", "-m", msg)
        log.status(True, f"Committed step {step_id}")
    except Exception as e:
        if "nothing to commit" not in str(getattr(e, 'stderr', '')):
            log.warning(f"Commit failed: {e}")

    desc = f"✅ **{step_id}**: {step['commit_count']} commits, end_commit=`{end_short}`"
    return ctx.copy_with(
        step_pr_descriptions=ctx.step_pr_descriptions + [desc],
        step_details=ctx.step_details + [{"step_id": step_id, "step_index": ctx.current_step+1, "commits": step["commit_count"], "end_commit": end_short}],
    )
