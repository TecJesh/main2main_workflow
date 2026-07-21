"""Pipeline step 8: Finalize — generate cumulative patch and summary."""
from __future__ import annotations
import time
from pathlib import Path
from TA_main2main_workflow.utils.config import TAConfig
from TA_main2main_workflow.utils.context import WorkflowContext
from TA_main2main_workflow.utils.logging import get_logger
from TA_main2main_workflow.utils.git import run_git
from TA_main2main_workflow.utils.tracker import total_elapsed
from TA_main2main_workflow.utils import FINAL_SUMMARY_FILE, FINAL_TARGET_PATCH_FILE, STEPS_DIR, WORKSPACE_DIR

log = get_logger(__name__)

def finalize(ctx: WorkflowContext, config: TAConfig) -> WorkflowContext:
    log.header("Finalize & Summary")
    ascend_path = Path(ctx.triton_ascend_path)

    # Summary
    summary_path = WORKSPACE_DIR / FINAL_SUMMARY_FILE
    summary_path.write_text(f"# Triton-Ascend Upstream Sync\n\n- **Target**: `{ctx.target_commit[:12]}`\n- **Steps**: {ctx.total_steps}\n- **Upstream commits**: {ctx.upstream_commits_count}\n- **Status**: Success\n- **Date**: {time.strftime('%Y-%m-%d %H:%M:%S')}\n", encoding="utf-8")
    log.info(f"Final summary: {summary_path}")

    # Patch
    try:
        patch = run_git(ascend_path, "diff", ctx.ascend_head, "HEAD")
        (WORKSPACE_DIR / FINAL_TARGET_PATCH_FILE).write_text(patch, encoding="utf-8")
        log.info(f"Cumulative patch: {len(patch)} bytes")
    except Exception as e:
        log.warning(f"Could not generate patch: {e}")

    log.header("Sync Complete!")
    elapsed = total_elapsed()
    log.elapsed(elapsed)
    rows = list(ctx.summary_rows) + [("Finalize", "PASS", f"{ctx.total_steps} step(s)"), ("OVERALL", "PASS", f"{ctx.total_steps} step(s)")]
    log.table(rows)
    return ctx.copy_with(summary_rows=rows, final_status="UpgradeCompleted")
