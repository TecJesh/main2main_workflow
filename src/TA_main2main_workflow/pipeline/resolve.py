"""Pipeline step: AI resolve merge conflicts."""

from __future__ import annotations

import json
from pathlib import Path

from TA_main2main_workflow.agent.opencode_adapter import run_opencode_adapter
from TA_main2main_workflow.utils.config import TAConfig
from TA_main2main_workflow.utils.context import WorkflowContext
from TA_main2main_workflow.utils.logging import get_logger
from TA_main2main_workflow.utils.git import run_git
from TA_main2main_workflow.pipeline.pre_ci import cleanup_temp_files, run_pre_ci_check
from TA_main2main_workflow.utils import STEPS_DIR, WORKSPACE_DIR

log = get_logger(__name__)
_REF = str(Path(__file__).parent.parent / "reference")


def resolve_conflicts(ctx: WorkflowContext, config: TAConfig) -> WorkflowContext:
    """AI-driven merge conflict resolution with retry loop."""
    if config.skip_ai_analysis:
        log.warning("SKIP_AI_ANALYSIS=true — cannot resolve conflicts")
        return ctx

    ascend_path = Path(ctx.triton_ascend_path)
    step = ctx.steps[ctx.current_step] if ctx.current_step < len(ctx.steps) else None
    step_id = step["id"] if step else "step-0"
    step_dir = WORKSPACE_DIR / STEPS_DIR / step_id
    step_dir.mkdir(parents=True, exist_ok=True)

    # Compute context for AI: previous step info for continuity
    prev_step_id = ""
    prev_summary_path = ""
    if ctx.current_step > 0 and ctx.current_step <= len(ctx.steps):
        prev = ctx.steps[ctx.current_step - 1]
        prev_step_id = prev["id"]
        prev_summary_path = str(
            WORKSPACE_DIR / STEPS_DIR / prev_step_id / "step_summary.md"
        )
    is_last_step = ctx.current_step >= ctx.total_steps - 1

    log.header("AI Conflict Resolution")
    for attempt in range(1, config.max_retries + 1):
        log.step(attempt, config.max_retries, "AI conflict resolution")
        cf = [
            f
            for f in run_git(ascend_path, "diff", "--name-only", "--diff-filter=U")
            .strip()
            .splitlines()
            if f
        ]
        if not cf:
            log.status(True, "Already resolved!")
            break
        try:
            run_opencode_adapter(
                {
                    "step_id": f"{step_id}-conflict-{attempt}",
                    "previous_step_id": prev_step_id,
                    "previous_step_summary_path": prev_summary_path,
                    "is_last_step": str(is_last_step).lower(),
                    "step_dir": str(step_dir),
                    "conflict_dir": str(step_dir),
                    "ascend_path": str(ascend_path),
                    "triton_path": ctx.triton_ascend_path,
                    "reference_dir": _REF,
                    "mode": "conflict",
                    "merge_mode": ctx.merge_mode,
                    "error_logs": json.dumps(cf, ensure_ascii=False),
                    "target_commit": ctx.target_commit,
                    "step_index": f"{ctx.current_step + 1}/{ctx.total_steps}",
                }
            )
        except Exception as e:
            log.error(f"AI call failed: {e}")
            if attempt < config.max_retries:
                continue
            break
        if not run_git(ascend_path, "diff", "--name-only", "--diff-filter=U").strip():
            log.status(True, f"Resolved (attempt {attempt})")
            break
        cf_remain = [
            f
            for f in run_git(ascend_path, "diff", "--name-only", "--diff-filter=U")
            .strip()
            .splitlines()
            if f
        ]
        log.status(False, f"{len(cf_remain)} conflict(s) remain")
    else:
        log.error(f"Failed after {config.max_retries} attempts")
        return ctx

    cleanup_temp_files(ascend_path)
    try:
        run_git(ascend_path, "add", "-A")
        run_git(ascend_path, "commit", "--no-edit", "-s")
        log.status(True, "Committed resolution")
    except Exception:
        pass
    pre_ci_ok = run_pre_ci_check(str(ascend_path), step_id="conflict-resolution")
    if pre_ci_ok:
        log.status(True, "Pre-CI check passed after conflict resolution")
    else:
        log.warning("Pre-CI check found issues after conflict resolution")
    return ctx.copy_with(
        merge_has_conflicts=False,
        conflict_files_resolved=ctx.conflict_files_resolved + len(ctx.conflict_files),
    )
