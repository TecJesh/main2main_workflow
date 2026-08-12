"""Pipeline step: Finalize — generate cumulative patch, PR description, and sync report.

PR description is AI-generated via the ``report`` mode and saved as
``final_summary.md``.  Falls back to a basic template if AI is
unavailable or skipped.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from TA_main2main_workflow.agent.opencode_adapter import run_opencode_adapter
from TA_main2main_workflow.utils.config import TAConfig
from TA_main2main_workflow.utils.context import WorkflowContext
from TA_main2main_workflow.utils.logging import get_logger
from TA_main2main_workflow.utils.git import run_git
from TA_main2main_workflow.utils.tracker import total_elapsed
from TA_main2main_workflow.utils import (
    FINAL_SUMMARY_FILE,
    FINAL_TARGET_PATCH_FILE,
    STEPS_DIR,
    WORKSPACE_DIR,
)

log = get_logger(__name__)
_REF = str(Path(__file__).parent.parent / "reference")


def finalize(ctx: WorkflowContext, config: TAConfig | None = None) -> WorkflowContext:
    """Generate final summary, cumulative patch, and sync report.

    Uses AI (report mode) for the PR description when available.
    """
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

    # ── PR description (AI-generated) ─────────────────────────────────
    summary_path = WORKSPACE_DIR / FINAL_SUMMARY_FILE
    if config and not config.skip_ai_analysis:
        try:
            _generate_pr_description(ctx, config, summary_path)
        except Exception as e:
            log.warning(f"AI PR description failed: {e} — using fallback")
            _write_summary_fallback(ctx, summary_path)
    else:
        _write_summary_fallback(ctx, summary_path)

    # ── Sync Report ───────────────────────────────────────────────────
    _write_sync_report(ctx)

    # ── Print final table ─────────────────────────────────────────────
    elapsed = total_elapsed()
    log.elapsed(elapsed)
    rows = list(ctx.summary_rows or [])
    rows.append(("Finalize", "PASS", f"{ctx.total_steps} step(s)"))
    rows.append(("OVERALL", "PASS", f"{ctx.total_steps} step(s)"))
    log.table(rows)

    return ctx


# ═══════════════════════════════════════════════════════════════════════════
# Internal
# ═══════════════════════════════════════════════════════════════════════════


def _generate_pr_description(
    ctx: WorkflowContext,
    config: TAConfig,
    summary_path: Path,
) -> None:
    """Invoke AI (report mode) to write the PR description."""
    step = ctx.steps[-1] if ctx.steps else {"id": "step-0"}
    step_dir = WORKSPACE_DIR / STEPS_DIR / step["id"]
    step_dir.mkdir(parents=True, exist_ok=True)

    # Build context for the AI: collect per-step data + fix records
    context = _build_report_context(ctx)

    log.section("AI PR Description (report mode)")
    result = run_opencode_adapter(
        {
            "step_id": "finalize-report",
            "previous_step_id": "",
            "previous_step_summary_path": "",
            "is_last_step": "true",
            "step_dir": str(step_dir),
            "fix_dir": str(step_dir),
            "conflict_dir": str(WORKSPACE_DIR / "conflicts"),
            "ascend_path": str(Path(ctx.triton_ascend_path)),
            "triton_path": ctx.triton_ascend_path,
            "reference_dir": _REF,
            "mode": "report",
            "error_logs": json.dumps(context, ensure_ascii=False, default=str),
            "target_commit": ctx.target_commit,
            "step_index": f"{ctx.total_steps}/{ctx.total_steps}",
            "upstream_commits_count": str(ctx.upstream_commits_count),
            "total_steps": str(ctx.total_steps),
            "conflict_files_resolved": str(ctx.conflict_files_resolved),
            "build_fix_count": str(ctx.build_fix_count),
            "test_fix_count": str(ctx.test_fix_count),
            "final_status": ctx.final_status or "Success",
            "ascend_npu_ir_fix": "false",
            "ascend_npu_ir_compat_ref": "",
        }
    )

    # AI writes to step_dir/step_summary.md; copy to final location
    ai_summary = step_dir / "step_summary.md"
    if ai_summary.exists():
        content = ai_summary.read_text(encoding="utf-8")
        summary_path.write_text(content, encoding="utf-8")
        log.status(True, f"AI PR description: {summary_path} ({len(content)} bytes)")
    elif result.step_summary:
        summary_path.write_text(result.step_summary, encoding="utf-8")
        log.status(True, f"AI PR description (from output): {summary_path}")
    else:
        log.warning("AI produced no summary — using fallback")
        _write_summary_fallback(ctx, summary_path)


def _build_report_context(ctx: WorkflowContext) -> dict:
    """Collect all sync data for the AI report."""
    steps_dir = WORKSPACE_DIR / STEPS_DIR

    # Collect per-step summaries and fix details
    step_data: list[dict] = []
    for s in ctx.steps:
        sd: dict = {
            "id": s["id"],
            "commits": s["commit_count"],
            "start_commit": s.get("start_commit", "")[:12],
            "end_commit": s.get("end_commit", "")[:12],
            "source_lines": s.get("source_changed_lines", 0),
            "reason": s.get("reason", "line_budget"),
        }
        # Include step summary if AI wrote one
        step_dir = steps_dir / s["id"]
        summary_file = step_dir / "step_summary.md"
        if summary_file.exists():
            try:
                sd["summary"] = summary_file.read_text(encoding="utf-8")[:4000]
            except Exception:
                pass
        # Include commit list
        commits_file = step_dir / "commits.txt"
        if commits_file.exists():
            try:
                sd["commit_list"] = commits_file.read_text(encoding="utf-8")[:2000]
            except Exception:
                pass
        step_data.append(sd)

    return {
        "target_commit": ctx.target_commit[:12],
        "upstream_commits_count": ctx.upstream_commits_count,
        "total_steps": ctx.total_steps,
        "work_branch": ctx.work_branch,
        "conflict_files_resolved": ctx.conflict_files_resolved,
        "build_fix_count": ctx.build_fix_count,
        "test_fix_count": ctx.test_fix_count,
        "final_status": ctx.final_status or "Success",
        "steps": step_data,
        "step_pr_descriptions": ctx.step_pr_descriptions,
        "step_details": ctx.step_details,
        "ir_analysis_done": ctx.ir_analysis_done,
    }


def _write_summary_fallback(ctx: WorkflowContext, summary_path: Path) -> None:
    """Write a basic PR description when AI is unavailable."""
    parts = [
        "## Summary",
        f"- **Target**: `{ctx.target_commit[:12]}`",
        f"- **Steps**: {ctx.total_steps}",
        f"- **Upstream commits**: {ctx.upstream_commits_count}",
        "- **Status**: Success",
        f"- **Date**: {time.strftime('%Y-%m-%d %H:%M:%S')}",
        f"- **Work branch**: `{ctx.work_branch}`",
    ]
    if ctx.step_details:
        parts.append("\n## Changes\n")
        for d in ctx.step_details:
            parts.append(
                f"- **{d['step_id']}**: {d['commits']} commits, "
                f"end=`{d.get('end_commit', '?')[:12]}`, "
                f"build_fixes={d.get('build_fixes', 0)}, "
                f"test_fixes={d.get('test_fixes', 0)}"
            )
    if ctx.step_pr_descriptions:
        parts.append("\n## Details\n")
        for desc in ctx.step_pr_descriptions:
            parts.append(f"- {desc}")

    summary_path.write_text("\n".join(parts) + "\n", encoding="utf-8")
    log.info(f"PR description (fallback): {summary_path}")


def _write_sync_report(ctx: WorkflowContext) -> None:
    """Generate a human-readable sync report (fallback, no AI)."""
    report_path = WORKSPACE_DIR / "SYNC_REPORT.md"
    try:
        parts = [
            "# Triton-Ascend 上游同步报告\n",
            "## 基本信息\n",
            f"- 目标提交: `{ctx.target_commit[:12]}`",
            f"- 步骤数: {ctx.total_steps}",
            f"- 上游提交数: {ctx.upstream_commits_count}",
            f"- 工作分支: `{ctx.work_branch}`",
            "- 状态: 成功",
        ]
        if ctx.step_details:
            parts.append("\n## 步骤详情\n")
            for d in ctx.step_details:
                parts.append(
                    f"### {d['step_id']}\n"
                    f"- 提交数: {d['commits']}\n"
                    f"- 构建修复: {d.get('build_fixes', 0)}\n"
                    f"- 测试修复: {d.get('test_fixes', 0)}\n"
                )
        report_path.write_text("\n".join(parts), encoding="utf-8")
        log.info(f"Sync report: {report_path}")
    except Exception as e:
        log.warning(f"Could not write sync report: {e}")
