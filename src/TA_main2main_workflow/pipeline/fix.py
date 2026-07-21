"""Pipeline step 6: AI fix build/test failures."""

from __future__ import annotations
import json
from pathlib import Path
from TA_main2main_workflow.agent.opencode_adapter import run_opencode_adapter
from TA_main2main_workflow.utils.config import TAConfig
from TA_main2main_workflow.utils.context import WorkflowContext
from TA_main2main_workflow.utils.logging import get_logger
from TA_main2main_workflow.utils import FIX_LOG_DIR, STEPS_DIR, WORKSPACE_DIR

log = get_logger(__name__)
_REF = str(Path(__file__).parent.parent / "reference")


def ai_fix(ctx: WorkflowContext, config: TAConfig, attempt: int = 1) -> WorkflowContext:
    if config.skip_ai_analysis:
        log.info("SKIP_AI_ANALYSIS=true — skipping AI fix")
        return ctx
    ascend_path = Path(ctx.triton_ascend_path)
    step = ctx.steps[ctx.current_step] if ctx.current_step < len(ctx.steps) else None
    step_id = step["id"] if step else "step-0"
    step_dir = WORKSPACE_DIR / STEPS_DIR / step_id
    step_dir.mkdir(parents=True, exist_ok=True)
    fix_dir = WORKSPACE_DIR / FIX_LOG_DIR / f"{step_id}-fix-{attempt}"
    fix_dir.mkdir(parents=True, exist_ok=True)

    log.step(attempt, config.max_retries, "AI fix")
    try:
        result = run_opencode_adapter(
            {
                "step_id": f"{step_id}-fix-{attempt}",
                "step_dir": str(step_dir),
                "fix_dir": str(fix_dir),
                "ascend_path": str(ascend_path),
                "triton_path": ctx.triton_ascend_path,
                "reference_dir": _REF,
                "mode": "fix",
                "error_logs": json.dumps(ctx.fix_errors, ensure_ascii=False),
                "target_commit": ctx.target_commit,
                "step_index": f"{ctx.current_step + 1}/{ctx.total_steps}",
            }
        )
        log.ai_result(
            bool(result.modified_files),
            result.modified_files,
            (result.step_summary or "")[:500],
        )
        return ctx
    except Exception as e:
        log.error(f"AI fix failed: {e}")
        return ctx
