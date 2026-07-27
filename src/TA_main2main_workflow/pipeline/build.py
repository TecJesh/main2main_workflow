"""Pipeline step: Build LLVM + Triton-Ascend with retry/fix loop."""

from __future__ import annotations
import os, subprocess
from pathlib import Path
from TA_main2main_workflow.utils.config import TAConfig
from TA_main2main_workflow.utils.context import WorkflowContext
from TA_main2main_workflow.utils.logging import get_logger
from TA_main2main_workflow.utils.tracker import timed
from TA_main2main_workflow.utils.git import run_git, run_git_no_check
from TA_main2main_workflow.utils import (
    BUILD_RESULT_FILE, BUILD_LOG_FILE, WORKSPACE_DIR, STEPS_DIR,
)
from TA_main2main_workflow.pipeline.fix import ai_fix, validate_fix, get_last_ai_result

log = get_logger(__name__)


def build(ctx: WorkflowContext, config: TAConfig,
          do_ir_patch: bool = False) -> WorkflowContext:
    """Build phase: LLVM setup -> TA build -> fix loop.

    When do_ir_patch is True, IR patch generation + LLVM rebuild
    happens inside the loop (per-step IR patch mode).
    """
    if config.skip_build:
        log.info("SKIP_BUILD=true -- skipping build")
        return ctx.copy_with(build_passed=True)

    ascend_path = Path(ctx.triton_ascend_path)
    step = ctx.steps[ctx.current_step] if ctx.steps else None
    step_id = step["id"] if step else "step-0"
    step_dir = WORKSPACE_DIR / STEPS_DIR / step_id
    step_dir.mkdir(parents=True, exist_ok=True)

    build_passed = False
    attempt = 0

    while attempt <= config.max_retries:
        is_fix_attempt = attempt > 0
        ctx = ctx.copy_with(retry_count=attempt)

        if is_fix_attempt:
            log.header(f"Build Fix Attempt {attempt}/{config.max_retries}")
            is_npu_ir = _detect_ascend_npu_ir_errors()
            ctx = ai_fix(ctx, config, attempt=attempt, ascend_npu_ir_fix=is_npu_ir)

            # Validate fix
            modified_files = get_last_ai_result().modified_files
            fix_valid, fix_reason = validate_fix(modified_files, ascend_path)
            if not fix_valid:
                log.error(f"Fix rejected: {fix_reason}")
                rejection_file = step_dir / "fix_rejection.txt"
                rejection_file.write_text(
                    f"PREVIOUS FIX WAS REJECTED: {fix_reason}\n"
                    f"Only files under {ascend_path}/third_party/ascend/ "
                    f"may be modified for compile-error fixes.\n",
                    encoding="utf-8")
                ctx = ctx.copy_with(
                    fix_errors=ctx.fix_errors + [str(rejection_file)])
                continue  # don't count this attempt

        # Build triton-ascend
        with timed("build-triton"):
            ctx = _build_triton(ctx, config, clean=(attempt == 0))
        if ctx.build_passed:
            build_passed = True
            break

        log.info(f"Build failed (attempt {attempt + 1}) -- retrying")
        ctx = ctx.copy_with(
            fix_errors=[str(WORKSPACE_DIR / BUILD_RESULT_FILE)])
        attempt += 1

    return ctx.copy_with(build_passed=build_passed)


def _build_triton(ctx: WorkflowContext, config: TAConfig,
                  clean: bool = False, python_exe: str = "python3") -> WorkflowContext:
    """Build triton-ascend."""
    from TA_main2main_workflow.scripts.build_test import build_triton_ascend

    ascend_path = Path(ctx.triton_ascend_path)
    llvm_prefix = config.llvm_install_prefix_sync or ctx.llvm_prefix
    if not llvm_prefix:
        llvm_prefix = os.path.expanduser(
            os.getenv("LLVM_INSTALL_PREFIX_SYNC", "~/llvm-install-sync"))

    python_exe = python_exe or os.getenv("PYTHON", "python3.10")

    log.section("Build Triton-Ascend")
    try:
        build_result = build_triton_ascend(
            ascend_path,
            llvm_prefix=str(llvm_prefix),
            conda_env=config.conda_env,
            clean_build=clean,
            python_exe=python_exe,
        )
        passed = build_result.get("all_passed", False)
        if passed:
            log.status(True, "Build passed")
            return ctx.copy_with(build_passed=True)
        log.error("Build FAILED")
        return ctx.copy_with(
            build_passed=False,
            fix_errors=[str(WORKSPACE_DIR / BUILD_RESULT_FILE)])
    except Exception as e:
        log.error(f"Build FAILED: {e}")
        return ctx.copy_with(
            build_passed=False,
            fix_errors=[str(WORKSPACE_DIR / BUILD_RESULT_FILE)])


def _detect_ascend_npu_ir_errors() -> bool:
    """Check whether the build log contains AscendNPU-IR compile errors."""
    build_log = WORKSPACE_DIR / BUILD_LOG_FILE
    if not build_log.exists():
        return False
    try:
        content = build_log.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return False
    markers = [
        "AscendNPU-IR", "bishengir", "bishengir-", "NPUIR",
        "HACC/IR", "HFusion/IR", "HIVM/IR",
        "third_party/ascend/", "AscendNPU",
    ]
    for marker in markers:
        if marker in content:
            return True
    return False
