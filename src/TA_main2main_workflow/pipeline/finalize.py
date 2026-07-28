"""Pipeline step: Finalize — generate cumulative patch, summary, and sync report."""

from __future__ import annotations

import json
import time
from pathlib import Path

from TA_main2main_workflow.utils.context import WorkflowContext
from TA_main2main_workflow.utils.logging import get_logger
from TA_main2main_workflow.utils.git import run_git
from TA_main2main_workflow.utils.tracker import total_elapsed
from TA_main2main_workflow.utils import (
    FINAL_SUMMARY_FILE,
    FINAL_TARGET_PATCH_FILE,
    WORKSPACE_DIR,
)

log = get_logger(__name__)


def finalize(ctx: WorkflowContext) -> WorkflowContext:
    """Generate final summary, cumulative patch, and sync report."""
    log.header("Finalize & Summary")
    ascend_path = Path(ctx.triton_ascend_path)

    # ── Cumulative patch ──────────────────────────────────────────────
    try:
        patch = run_git(ascend_path, "diff", ctx.ascend_head, "HEAD")
        patch_path = WORKSPACE_DIR / FINAL_TARGET_PATCH_FILE
        patch_path.write_text(patch, encoding="utf-8")
        log.info(f"Cumulative patch: {len(patch)} bytes → {patch_path}")
    except Exception as e:
        log.warning(f"Could not generate patch: {e}")

    # ── Summary ───────────────────────────────────────────────────────
    summary_parts = [
        f"# Triton-Ascend Upstream Sync\n",
        f"- **Target**: `{ctx.target_commit[:12]}`",
        f"- **Steps**: {ctx.total_steps}",
        f"- **Upstream commits**: {ctx.upstream_commits_count}",
        f"- **Status**: Success",
        f"- **Date**: {time.strftime('%Y-%m-%d %H:%M:%S')}",
        f"- **Work branch**: `{ctx.work_branch}`",
    ]
    if ctx.step_details:
        summary_parts.append(f"\n## Per-Step Details\n")
        for d in ctx.step_details:
            summary_parts.append(
                f"- **{d['step_id']}**: {d['commits']} commits, "
                f"end=`{d.get('end_commit', '?')[:12]}`, "
                f"build_fixes={d.get('build_fixes', 0)}, "
                f"test_fixes={d.get('test_fixes', 0)}"
            )
    if ctx.step_pr_descriptions:
        summary_parts.append(f"\n## Step Results\n")
        for desc in ctx.step_pr_descriptions:
            summary_parts.append(f"- {desc}")

    summary_path = WORKSPACE_DIR / FINAL_SUMMARY_FILE
    summary_path.write_text("\n".join(summary_parts) + "\n", encoding="utf-8")
    log.info(f"Final summary: {summary_path}")

    # ── Sync Report (AI-generated, Chinese) ───────────────────────────
    _write_sync_report(ctx)

    # ── Print final table ─────────────────────────────────────────────
    elapsed = total_elapsed()
    log.elapsed(elapsed)
    rows = ctx.summary_rows or []
    rows.append(("Finalize", "PASS", f"{ctx.total_steps} step(s)"))
    rows.append(("OVERALL", "PASS", f"{ctx.total_steps} step(s)"))
    log.table(rows)

    return ctx


def _write_sync_report(ctx: WorkflowContext) -> None:
    """Generate a human-readable sync report (fallback, no AI)."""
    report_path = WORKSPACE_DIR / "SYNC_REPORT.md"
    try:
        report_parts = [
            "# Triton-Ascend 上游同步报告\n",
            f"## 基本信息\n",
            f"- 目标提交: `{ctx.target_commit[:12]}`",
            f"- 步骤数: {ctx.total_steps}",
            f"- 上游提交数: {ctx.upstream_commits_count}",
            f"- 工作分支: `{ctx.work_branch}`",
            f"- 状态: 成功",
        ]
        if ctx.step_details:
            report_parts.append(f"\n## 步骤详情\n")
            for d in ctx.step_details:
                report_parts.append(
                    f"### {d['step_id']}\n"
                    f"- 提交数: {d['commits']}\n"
                    f"- 构建修复: {d.get('build_fixes', 0)}\n"
                    f"- 测试修复: {d.get('test_fixes', 0)}\n"
                )
        report_path.write_text("\n".join(report_parts), encoding="utf-8")
        log.info(f"Sync report: {report_path}")
    except Exception as e:
        log.warning(f"Could not write sync report: {e}")
