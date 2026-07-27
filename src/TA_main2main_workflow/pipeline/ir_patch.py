"""Pipeline step: IR compatibility patch — apply existing, supplement on test failure.

New flow:
  1. Switch to target LLVM commit, clean workspace
  2. Directly apply existing llvm_patch_f6ded0b.patch (AI fix if needed)
  3. Build LLVM with patch
  4. Build TA + fix compile errors
  5. Run tests:
     - Pass → done
     - IR issues → AI supplements the existing patch → rebuild LLVM → retest
     - Code issues → AI fixes → retest
"""

from __future__ import annotations
import json, os, subprocess
from pathlib import Path
from TA_main2main_workflow.agent.opencode_adapter import run_opencode_adapter
from TA_main2main_workflow.utils.config import TAConfig
from TA_main2main_workflow.utils.context import WorkflowContext
from TA_main2main_workflow.utils.logging import get_logger
from TA_main2main_workflow.utils.tracker import timed
from TA_main2main_workflow.utils.git import run_git, run_git_no_check
from TA_main2main_workflow.utils import (
    WORKSPACE_DIR, BUILD_RESULT_FILE, BUILD_LOG_FILE,
    IR_ANALYSIS_DIR, IR_OPS_REPORT_FILE, IR_CHANGES_REPORT_FILE,
    IR_DIAGNOSIS_FILE, IR_MAX_ITERATIONS, STEPS_DIR,
)
from TA_main2main_workflow.pipeline.fix import ai_fix, validate_fix, get_last_ai_result

log = get_logger(__name__)
_REF = str(Path(__file__).parent.parent / "reference")
_ASCEND_BASELINE_LLVM_HASH = "b5cc222d7429fe6f18c787f633d5262fac2e676f"


def _llvm_project_path() -> Path:
    return Path(os.path.expanduser(
        os.getenv("LLVM_PROJECT_PATH", "~/llvm-project")))


def per_step_ir_patch(ctx: WorkflowContext, config: TAConfig,
                      step: dict) -> WorkflowContext:
    """IR patch pipeline when LLVM hash changed.

    1. Switch to target LLVM, clean workspace
    2. Apply existing llvm_patch_f6ded0b.patch (AI fix if failed)
    3. Build LLVM → Build TA + fix compile errors
    4. Test + supplement loop (IR issues → add to patch → rebuild → retest)
    """
    step_id = step["id"]
    ascend_path = Path(ctx.triton_ascend_path)

    if not _llvm_hash_did_change(ascend_path):
        log.info(f"[{step_id}] LLVM hash unchanged -- skipping IR patch")
        return ctx

    llvm_hash_file = ascend_path / "cmake" / "llvm-hash.txt"
    target_llvm_hash = llvm_hash_file.read_text(encoding="utf-8").strip()
    llvm_project = _llvm_project_path()
    llvm_install = Path(os.path.expanduser(
        config.llvm_install_prefix_sync or "~/llvm-install-sync"))
    ascend_patch = ascend_path / "third_party" / "ascend" / "patch" / "llvm_patch_f6ded0b.patch"

    from TA_main2main_workflow.scripts.build_test import build_llvm, apply_llvm_patches
    from TA_main2main_workflow.pipeline.build import _build_triton, _detect_ascend_npu_ir_errors

    log.header(f"IR Patch Pipeline -- {step_id}")
    log.key_value("Target LLVM", target_llvm_hash[:12])
    log.key_value("Patch file", str(ascend_patch))

    # ═══════════════════════════════════════════════════════════════════
    # 1. Switch to target LLVM, clean workspace
    # ═══════════════════════════════════════════════════════════════════
    if not _ensure_llvm_workspace_clean():
        log.error("Cannot clean llvm-project workspace")
        return ctx.copy_with(build_passed=False)
    try:
        subprocess.run(
            ["git", "checkout", target_llvm_hash],
            cwd=str(llvm_project), capture_output=True, text=True, timeout=120)
        log.info(f"Checked out target LLVM: {target_llvm_hash[:12]}")
    except Exception as e:
        log.error(f"Failed to checkout target LLVM: {e}")
        return ctx.copy_with(build_passed=False)

    # ═══════════════════════════════════════════════════════════════════
    # 2. Apply existing patch directly (AI fix if needed)
    # ═══════════════════════════════════════════════════════════════════
    log.info(f"Applying existing patch: {ascend_patch.name}")
    patch_ok = _apply_patch_with_retry(
        ctx, config, ascend_path, ascend_patch, llvm_project,
        target_llvm_hash, step_id, patch_error_type="apply")
    if not patch_ok:
        log.error("Patch apply failed after all retries")
        return ctx.copy_with(build_passed=False)

    # ═══════════════════════════════════════════════════════════════════
    # 3. Build LLVM with patch
    # ═══════════════════════════════════════════════════════════════════
    log.info("Building LLVM with IR patch...")
    try:
        llvm_prefix = build_llvm(llvm_project, llvm_install,
                                 required_hash=target_llvm_hash)
        ctx = ctx.copy_with(llvm_prefix=str(llvm_prefix))
        log.status(True, "LLVM build with patch complete")
    except Exception as e:
        log.error(f"LLVM build failed: {e}")
        # AI fixes the patch based on build error, then retry
        build_log = WORKSPACE_DIR / "llvm_build.log"
        build_error = str(e)[:500]
        if build_log.exists():
            try:
                log_tail = build_log.read_text(
                    encoding="utf-8", errors="replace")[-3000:]
                build_error = f"Build exception: {e}\n\nBuild log tail:\n{log_tail}"
            except Exception:
                pass
        log.warning("LLVM build failed — AI will fix the patch")
        _fix_patch_and_retry(ctx, config, ascend_path, ascend_patch,
                             target_llvm_hash, "build", build_error, 1)
        # Retry once after fix
        if not _ensure_llvm_workspace_clean():
            return ctx.copy_with(build_passed=False)
        patch_ok = _apply_patch_with_retry(
            ctx, config, ascend_path, ascend_patch, llvm_project,
            target_llvm_hash, step_id, patch_error_type="build")
        if not patch_ok:
            return ctx.copy_with(build_passed=False)
        try:
            llvm_prefix = build_llvm(llvm_project, llvm_install,
                                     required_hash=target_llvm_hash)
            ctx = ctx.copy_with(llvm_prefix=str(llvm_prefix))
            log.status(True, "LLVM rebuild after patch fix succeeded")
        except Exception as e2:
            log.error(f"LLVM build still failing after patch fix: {e2}")
            return ctx.copy_with(build_passed=False)

    # ═══════════════════════════════════════════════════════════════════
    # 4. Build TA + fix compile errors
    # ═══════════════════════════════════════════════════════════════════
    log.info("Building TA with patched LLVM...")
    ctx = _build_triton(ctx, config, clean=True)
    if not ctx.build_passed:
        if config.skip_ai_analysis:
            return ctx.copy_with(build_passed=False)
        log.warning("Build failed — entering compile-error fix loop")
        for fix_attempt in range(1, config.max_retries + 1):
            is_npu_ir = _detect_ascend_npu_ir_errors()
            ctx = ctx.copy_with(fix_errors=[str(WORKSPACE_DIR / BUILD_RESULT_FILE)])
            ctx = ai_fix(ctx, config, attempt=fix_attempt, ascend_npu_ir_fix=is_npu_ir)
            modified_files = get_last_ai_result().modified_files
            fix_valid, fix_reason = validate_fix(modified_files, ascend_path)
            if not fix_valid:
                log.error(f"Fix rejected: {fix_reason}")
                continue
            ctx = _build_triton(ctx, config, clean=False)
            if ctx.build_passed:
                break
        if not ctx.build_passed:
            log.error(f"TA build still failing after {config.max_retries} fixes")
            return ctx.copy_with(build_passed=False, test_passed=False)
    log.status(True, "TA builds successfully")

    # ═══════════════════════════════════════════════════════════════════
    # 5. Test + supplement patch loop
    # ═══════════════════════════════════════════════════════════════════
    return _test_and_supplement_loop(ctx, config, ascend_path, step, step_id,
                                     target_llvm_hash, ascend_patch,
                                     llvm_project, llvm_install)


def _apply_patch_with_retry(ctx: WorkflowContext, config: TAConfig,
                             ascend_path: Path, ascend_patch: Path,
                             llvm_project: Path, target_llvm_hash: str,
                             step_id: str,
                             patch_error_type: str = "apply") -> bool:
    """Try to apply the patch. If it fails, AI fixes it and retry.

    Returns True if patch applied successfully.
    """
    from TA_main2main_workflow.scripts.build_test import apply_llvm_patches

    for attempt in range(IR_MAX_ITERATIONS + 1):
        if attempt > 0:
            log.header(f"Patch Apply Retry {attempt}/{IR_MAX_ITERATIONS}")

        # Clean + checkout before each attempt
        if not _ensure_llvm_workspace_clean():
            return False
        subprocess.run(
            ["git", "checkout", target_llvm_hash],
            cwd=str(llvm_project), capture_output=True, text=True, timeout=120)

        patch_result = apply_llvm_patches(
            ascend_patch.parent, llvm_project,
            target_hash=target_llvm_hash, patch_file=ascend_patch)

        if patch_result["all_ok"]:
            log.status(True, f"Patch applied: {ascend_patch.name}")
            return True

        failed = patch_result.get("failed", [])
        error_msg = failed[0]['error'][:500] if failed else "unknown"
        log.error(f"Patch apply failed: {error_msg}")

        if attempt < IR_MAX_ITERATIONS:
            log.warning("AI will analyze and fix the patch")
            _fix_patch_for_apply(ctx, config, ascend_path, ascend_patch,
                                 target_llvm_hash, error_msg, attempt + 1,
                                 ascend_path / "third_party" / "ascend" / "patch")

    log.error(f"Patch apply failed after {IR_MAX_ITERATIONS} retries")
    return False


def _fix_patch_for_apply(ctx: WorkflowContext, config: TAConfig,
                          ascend_path: Path, ascend_patch: Path,
                          target_llvm_hash: str, error_msg: str,
                          retry: int, step_dir: Path) -> None:
    """AI analyzes why patch failed and adjusts it for the current LLVM commit."""
    log.info(f"AI analyzing patch apply failure (retry {retry})...")
    try:
        run_opencode_adapter({
            "step_id": f"ir-fix-patch-apply-{retry}",
            "previous_step_id": "",
            "previous_step_summary_path": "",
            "is_last_step": "false",
            "step_index": "ir",
            "step_dir": str(step_dir),
            "fix_dir": str(step_dir),
            "conflict_dir": "",
            "ascend_path": str(ascend_path),
            "triton_path": ctx.triton_path,
            "reference_dir": _REF,
            "mode": "ir_fix_patch_apply",
            "error_logs": json.dumps([], ensure_ascii=False),
            "target_commit": ctx.target_commit,
            "llvm_project_path": str(_llvm_project_path()),
            "baseline_llvm_hash": _ASCEND_BASELINE_LLVM_HASH,
            "target_llvm_hash": target_llvm_hash,
            "ascend_patch_file": str(ascend_patch),
            "patch_error_msg": error_msg,
        })
    except Exception as e:
        log.error(f"AI patch fix failed: {e}")


def _test_and_supplement_loop(ctx: WorkflowContext, config: TAConfig,
                               ascend_path: Path, step: dict, step_id: str,
                               target_llvm_hash: str, ascend_patch: Path,
                               llvm_project: Path,
                               llvm_install: Path) -> WorkflowContext:
    """Run tests. On IR issues, supplement the existing patch and retry.

    Does NOT regenerate from scratch — AI adds missing OP adaptations
    to the existing patch file.
    """
    from TA_main2main_workflow.pipeline.build import _build_triton
    from TA_main2main_workflow.pipeline.test import (
        _run_pytest, _detect_oom_in_tests, _rerun_tests_reduced_concurrency,
        _collect_test_error_logs)
    from TA_main2main_workflow.scripts.build_test import build_llvm

    for iteration in range(config.ir_max_iterations):
        ctx = ctx.copy_with(ir_patch_iteration=iteration)
        log.header(f"IR Test Loop — Iteration {iteration + 1}/{config.ir_max_iterations}")

        # Run tests
        ctx = _run_pytest(ctx, config)
        if ctx.test_passed:
            log.status(True, f"All tests pass for {step_id}")
            return ctx.copy_with(test_passed=True)

        # OOM handling
        if _detect_oom_in_tests():
            log.warning("NPU OOM detected — rerunning with reduced concurrency")
            oom_result = _rerun_tests_reduced_concurrency(ascend_path, config)
            if oom_result is None or oom_result:
                return ctx.copy_with(test_passed=True)
            if not _detect_oom_in_tests():
                log.info("OOM resolved — classifying remaining failures")
            else:
                return ctx.copy_with(test_passed=False)

        # Classify failures: IR vs code
        log.warning("Tests failed — classifying failures (IR vs code)...")
        has_ir_issues = _do_ir_diagnose_failures(ctx, ascend_path)

        if has_ir_issues:
            log.warning(
                f"IR compatibility issues detected — "
                f"AI will supplement the existing patch with missing OP adaptations "
                f"(iteration {iteration + 1}/{config.ir_max_iterations})")

            # AI supplements the existing patch (NOT from scratch)
            ir_dir = WORKSPACE_DIR / IR_ANALYSIS_DIR
            ir_dir.mkdir(parents=True, exist_ok=True)
            _supplement_patch(ctx, config, ascend_path, ascend_patch,
                              target_llvm_hash, ir_dir, iteration + 1)

            # Rebuild LLVM with supplemented patch
            if not _ensure_llvm_workspace_clean():
                return ctx.copy_with(test_passed=False)
            subprocess.run(
                ["git", "checkout", target_llvm_hash],
                cwd=str(llvm_project), capture_output=True, text=True, timeout=120)
            if not _apply_patch_with_retry(
                    ctx, config, ascend_path, ascend_patch, llvm_project,
                    target_llvm_hash, step_id, patch_error_type="apply"):
                continue
            try:
                llvm_prefix = build_llvm(llvm_project, llvm_install,
                                         required_hash=target_llvm_hash)
                ctx = ctx.copy_with(llvm_prefix=str(llvm_prefix))
                log.status(True, "LLVM rebuild with supplemented patch complete")
            except Exception as e:
                log.error(f"LLVM rebuild failed after patch supplement: {e}")
                continue

            # Rebuild TA
            ctx = _build_triton(ctx, config, clean=False)
            if not ctx.build_passed:
                log.warning("TA build failed after patch supplement — will retry")
                continue
            continue  # loop back to test

        # Code issues → AI fix
        log.warning("Code issues detected — AI fix")
        ctx = ctx.copy_with(fix_errors=_collect_test_error_logs())
        ctx = ai_fix(ctx, config, attempt=1)
        ctx = _build_triton(ctx, config, clean=False)

    log.error(f"IR test loop exhausted {config.ir_max_iterations} iterations")
    return ctx.copy_with(test_passed=False)


def _supplement_patch(ctx: WorkflowContext, config: TAConfig,
                       ascend_path: Path, ascend_patch: Path,
                       target_llvm_hash: str, ir_dir: Path,
                       iteration: int) -> None:
    """AI supplements the existing patch with missing OP adaptations.

    Key: the AI adds to the EXISTING patch, not generating from scratch.
    It should:
    1. Read the current patch to understand what's already covered
    2. Diagnose which OPs still have IR compatibility issues
    3. Add missing OP adaptations to the patch
    """
    log.info(f"AI supplementing existing patch (iteration {iteration})...")
    try:
        run_opencode_adapter({
            "step_id": f"ir-supplement-patch-{iteration}",
            "previous_step_id": "ir-diagnose",
            "previous_step_summary_path": str(ir_dir / IR_DIAGNOSIS_FILE),
            "is_last_step": "false",
            "step_index": "ir",
            "step_dir": str(ascend_patch.parent),
            "fix_dir": str(ascend_patch.parent),
            "conflict_dir": "",
            "ascend_path": str(ascend_path),
            "triton_path": ctx.triton_path,
            "reference_dir": _REF,
            "mode": "ir_supplement_patch",
            "error_logs": json.dumps(
                [str(ir_dir / IR_DIAGNOSIS_FILE)], ensure_ascii=False),
            "target_commit": ctx.target_commit,
            "llvm_project_path": str(_llvm_project_path()),
            "baseline_llvm_hash": _ASCEND_BASELINE_LLVM_HASH,
            "target_llvm_hash": target_llvm_hash,
            "ascend_patch_file": str(ascend_patch),
        })
    except Exception as e:
        log.error(f"AI patch supplement failed: {e}")


def _fix_patch_and_retry(ctx: WorkflowContext, config: TAConfig,
                          ascend_path: Path, ascend_patch: Path,
                          target_llvm_hash: str, error_type: str,
                          error_msg: str, retry: int) -> None:
    """AI fixes a broken patch based on build error (kept for backward compat)."""
    log.info(f"AI fixing patch ({error_type} failure, retry {retry})...")
    try:
        run_opencode_adapter({
            "step_id": f"ir-fix-patch-{retry}",
            "previous_step_id": "",
            "previous_step_summary_path": "",
            "is_last_step": "false",
            "step_index": "ir",
            "step_dir": str(ascend_patch.parent),
            "fix_dir": str(ascend_patch.parent),
            "conflict_dir": "",
            "ascend_path": str(ascend_path),
            "triton_path": ctx.triton_path,
            "reference_dir": _REF,
            "mode": "ir_fix_patch_apply",
            "error_logs": json.dumps([], ensure_ascii=False),
            "target_commit": ctx.target_commit,
            "llvm_project_path": str(_llvm_project_path()),
            "baseline_llvm_hash": _ASCEND_BASELINE_LLVM_HASH,
            "target_llvm_hash": target_llvm_hash,
            "ascend_patch_file": str(ascend_patch),
            "patch_error_type": error_type,
            "patch_error_msg": error_msg,
        })
    except Exception as e:
        log.error(f"AI patch fix failed: {e}")


def _do_ir_diagnose_failures(ctx: WorkflowContext, ascend_path: Path) -> bool:
    """AI classifies test failures as IR vs code."""
    ir_dir = WORKSPACE_DIR / IR_ANALYSIS_DIR
    ir_dir.mkdir(parents=True, exist_ok=True)
    from TA_main2main_workflow.pipeline.test import _collect_test_error_logs
    error_log_paths = _collect_test_error_logs()

    if not error_log_paths:
        return False

    try:
        run_opencode_adapter({
            "step_id": "ir-diagnose",
            "previous_step_id": "",
            "previous_step_summary_path": "",
            "is_last_step": "true",
            "step_index": "ir",
            "step_dir": str(ir_dir),
            "fix_dir": str(ir_dir),
            "conflict_dir": "",
            "ascend_path": str(ascend_path),
            "triton_path": ctx.triton_path,
            "reference_dir": _REF,
            "mode": "ir_diagnose",
            "error_logs": json.dumps(error_log_paths, ensure_ascii=False),
        })
    except Exception:
        return False

    diagnosis_file = ir_dir / IR_DIAGNOSIS_FILE
    if diagnosis_file.exists():
        try:
            data = json.loads(diagnosis_file.read_text(encoding="utf-8"))
            return data.get("summary", {}).get("has_ir_issues", False)
        except Exception:
            pass
    return False


def _llvm_hash_did_change(ascend_path: Path) -> bool:
    llvm_hash_file = ascend_path / "cmake" / "llvm-hash.txt"
    if not llvm_hash_file.exists():
        return False
    current_hash = llvm_hash_file.read_text(encoding="utf-8").strip()
    return current_hash != _ASCEND_BASELINE_LLVM_HASH


def _ensure_llvm_workspace_clean() -> bool:
    """Ensure the llvm-project working tree is clean."""
    llvm_project = _llvm_project_path()
    if not llvm_project.exists():
        return True
    try:
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=str(llvm_project), capture_output=True, text=True, timeout=15,
        ).stdout.strip()
    except Exception:
        return True
    if not status:
        return True

    log.warning("llvm-project has uncommitted changes -- cleaning...")
    try:
        subprocess.run(
            ["git", "stash", "push", "-u", "-m", "ta-auto-clean"],
            cwd=str(llvm_project), capture_output=True, text=True, timeout=30)
        subprocess.run(
            ["git", "stash", "drop", "stash@{0}"],
            cwd=str(llvm_project), capture_output=True, text=True, timeout=30)
        log.status(True, "Workspace cleaned")
        return True
    except Exception:
        try:
            subprocess.run(
                ["git", "checkout", "--", "."],
                cwd=str(llvm_project), capture_output=True, text=True, timeout=30)
            subprocess.run(
                ["git", "clean", "-fd"],
                cwd=str(llvm_project), capture_output=True, text=True, timeout=30)
            return True
        except Exception:
            return False
