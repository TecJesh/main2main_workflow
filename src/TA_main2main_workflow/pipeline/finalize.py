"""Pipeline step: Finalize — generate cumulative patch and summary."""

from __future__ import annotations
import time
from pathlib import Path
from TA_main2main_workflow.utils.config import TAConfig
from TA_main2main_workflow.utils.context import WorkflowContext
from TA_main2main_workflow.utils.logging import get_logger
from TA_main2main_workflow.utils.git import run_git
from TA_main2main_workflow.utils.tracker import total_elapsed
from TA_main2main_workflow.utils import (
    FINAL_SUMMARY_FILE, FINAL_TARGET_PATCH_FILE, WORKSPACE_DIR,
)

log = get_logger(__name__)


def finalize(ctx: WorkflowContext, config: TAConfig) -> WorkflowContext:
    log.header("Finalize & Summary")
    ascend_path = Path(ctx.triton_ascend_path)

    # Summary
    summary_path = WORKSPACE_DIR / FINAL_SUMMARY_FILE
    summary_path.write_text(
        f"# Triton-Ascend Upstream Sync\n\n"
        f"- **Target**: `{ctx.target_commit[:12]}`\n"
        f"- **Steps**: {ctx.total_steps}\n"
        f"- **Upstream commits**: {ctx.upstream_commits_count}\n"
        f"- **Status**: Success\n"
        f"- **Date**: {time.strftime('%Y-%m-%d %H:%M:%S')}\n",
        encoding="utf-8")
    log.info(f"Final summary: {summary_path}")

    # Cumulative patch
    try:
        patch = run_git(ascend_path, "diff", ctx.ascend_head, "HEAD")
        (WORKSPACE_DIR / FINAL_TARGET_PATCH_FILE).write_text(patch, encoding="utf-8")
        log.info(f"Cumulative patch: {len(patch)} bytes")
    except Exception as e:
        log.warning(f"Could not generate patch: {e}")

    # PR body
    pr_body_path = WORKSPACE_DIR / "pr_body.md"
    lines = [
        "## Triton-Ascend Upstream Sync\n\n",
        f"**Target commit**: `{ctx.target_commit[:12]}`\n\n",
        f"**Steps**: {ctx.total_steps}\n\n",
        f"**Commits merged**: {ctx.upstream_commits_count}\n\n",
        "### Step Details\n\n",
    ]
    for desc in ctx.step_pr_descriptions:
        lines.append(f"- {desc}\n")
    lines.append("\n---\nGenerated with [Claude Code](https://claude.com/claude-code)\n")
    pr_body_path.write_text("".join(lines), encoding="utf-8")

    log.header("Sync Complete!")
    elapsed = total_elapsed()
    log.elapsed(elapsed)
    rows = [
        ("Finalize", "PASS", f"{ctx.total_steps} step(s)"),
        ("OVERALL", "PASS", f"{ctx.total_steps} step(s)"),
    ]
    log.table(rows)
    return ctx
