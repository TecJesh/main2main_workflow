"""Shared AI fix invocation — used by both build and test loops."""

from __future__ import annotations
import json, os, subprocess
from pathlib import Path
from TA_main2main_workflow.agent.opencode_adapter import run_opencode_adapter
from TA_main2main_workflow.utils.config import TAConfig
from TA_main2main_workflow.utils.context import WorkflowContext
from TA_main2main_workflow.utils.logging import get_logger
from TA_main2main_workflow.utils import (
    FIX_LOG_DIR, STEPS_DIR, WORKSPACE_DIR,
)

log = get_logger(__name__)
_REF = str(Path(__file__).parent.parent / "reference")


class AIFixResult:
    """Transient per-call result, stored on the flow orchestrator."""
    def __init__(self):
        self.modified_files: list[str] = []
        self.step_summary: str = ""
        self.is_noop: bool = False
        self.elapsed_seconds: float = 0.0


_ai_fix_result = AIFixResult()


def get_last_ai_result() -> AIFixResult:
    return _ai_fix_result


def ai_fix(ctx: WorkflowContext, config: TAConfig, attempt: int = 1,
           ascend_npu_ir_fix: bool = False) -> WorkflowContext:
    """Invoke AI to fix build or test failures."""
    global _ai_fix_result
    _ai_fix_result = AIFixResult()

    if config.skip_ai_analysis:
        log.info("SKIP_AI_ANALYSIS=true -- skipping AI fix")
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
        result = run_opencode_adapter({
            "step_id": f"{step_id}-fix-{attempt}",
            "previous_step_id": step_id,
            "previous_step_summary_path": str(step_dir / "step_summary.md"),
            "is_last_step": "false",
            "step_index": f"{ctx.current_step + 1}/{ctx.total_steps}",
            "step_dir": str(step_dir),
            "fix_dir": str(fix_dir),
            "conflict_dir": "",
            "ascend_path": str(ascend_path),
            "triton_path": ctx.triton_path,
            "reference_dir": _REF,
            "mode": "fix",
            "error_logs": json.dumps(ctx.fix_errors, ensure_ascii=False),
            "target_commit": ctx.target_commit,
            "ascend_npu_ir_fix": str(ascend_npu_ir_fix).lower(),
            "ascend_npu_ir_compat_ref": str(Path(_REF) / "AscendNPU-IR_LLVM_VERSION_COMPAT.md"),
        })
        _ai_fix_result.modified_files = list(result.modified_files)
        _ai_fix_result.step_summary = result.step_summary or ""
        _ai_fix_result.is_noop = result.is_noop
        _ai_fix_result.elapsed_seconds = result.elapsed_seconds
        log.ai_result(
            bool(result.modified_files),
            result.modified_files,
            (result.step_summary or "")[:500],
        )
    except Exception as e:
        log.error(f"AI fix failed: {e}")

    return ctx


def validate_fix(modified_files: list[str], ascend_path: Path) -> tuple:
    """Validate that an AI fix only touches allowed files.

    When validation fails, illegal changes are reverted via git checkout.
    Returns (passed: bool, reason: str).
    """
    if not modified_files:
        return False, "No files were modified"

    illegal_files: list[str] = []
    ascend_root = str(ascend_path / "third_party" / "ascend")
    for f in modified_files:
        f_abs = str(Path(f).resolve()) if not Path(f).is_absolute() else f
        if ascend_root not in f_abs:
            illegal_files.append(f)

    if illegal_files:
        log.warning(f"Reverting invalid fix changes in {ascend_path}...")
        try:
            subprocess.run(
                ["git", "checkout", "--", "."],
                cwd=str(ascend_path), capture_output=True, text=True, timeout=30)
            subprocess.run(
                ["git", "clean", "-fd"],
                cwd=str(ascend_path), capture_output=True, text=True, timeout=30)
            log.status(True, "Reverted -- working tree is clean")
        except Exception as e:
            log.error(f"Failed to revert changes: {e}")
        return False, (
            f"Fix modified files OUTSIDE third_party/ascend/: "
            + ", ".join(illegal_files)
            + ". Changes have been reverted. "
            + f"Next fix MUST only modify files under "
            + f"{ascend_path}/third_party/ascend/")

    log.status(True,
        f"Fix validation: {len(modified_files)} file(s) all within third_party/ascend/")
    return True, "All modified files are within third_party/ascend/"
