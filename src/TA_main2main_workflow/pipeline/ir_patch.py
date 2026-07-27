"""Pipeline step: IR compatibility patch analysis, generation, apply, and retry.

Handles the full IR patch pipeline when LLVM hash changes:
  Phase 1: Build new LLVM (no patches) + fix TA compile errors
  Phase 2: OP analysis -> IR change analysis -> generate patches ->
           apply patches + rebuild LLVM -> build TA -> test + fix loop
           with embedded IR retry on test failures
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
    """Full per-step IR patch pipeline when LLVM hash changed.

    Phase 1: Build new LLVM -> fix TA compile errors.
    Phase 2: IR analysis -> generate patches -> apply -> test with IR retry.
    Returns ctx with build_passed and test_passed updated.
    """
    step_id = step["id"]
    ascend_path = Path(ctx.triton_ascend_path)
    step_dir = WORKSPACE_DIR / STEPS_DIR / step_id

    # Guard: check LLVM hash actually changed
    if not _llvm_hash_did_change(ascend_path):
        log.info(f"[{step_id}] LLVM hash unchanged -- skipping IR patch")
        return ctx

    llvm_hash_file = ascend_path / "cmake" / "llvm-hash.txt"
    target_llvm_hash = llvm_hash_file.read_text(encoding="utf-8").strip()

    # Phase 1: Build new LLVM + fix compile errors
    log.header(f"Phase 1: Build new LLVM + Fix Compile Errors -- {step_id}")
    if not _build_clean_llvm_and_fix_ta(ctx, config, ascend_path, step_id,
                                         target_llvm_hash, step_dir):
        return ctx.copy_with(build_passed=False)

    # Phase 2: IR patch generation + test loop
    log.header(f"Phase 2: IR Patch Generation & Test -- {step_id}")
    return _ir_patch_loop(ctx, config, ascend_path, step, step_id,
                          step_dir, target_llvm_hash)


def _build_clean_llvm_and_fix_ta(ctx: WorkflowContext, config: TAConfig,
                                  ascend_path: Path, step_id: str,
                                  target_llvm_hash: str,
                                  step_dir: Path) -> bool:
    """Phase 1: Build clean LLVM + fix TA compile errors."""
    from TA_main2main_workflow.scripts.build_test import build_llvm
    from TA_main2main_workflow.pipeline.build import _build_triton, _detect_ascend_npu_ir_errors

    llvm_install = Path(os.path.expanduser(
        config.llvm_install_prefix_sync or "~/llvm-install-sync"))

    # Clean + checkout target LLVM
    if not _ensure_llvm_workspace_clean():
        log.error("Cannot clean llvm-project workspace")
        return False
    try:
        subprocess.run(
            ["git", "checkout", target_llvm_hash],
            cwd=str(_llvm_project_path()),
            capture_output=True, text=True, timeout=120)
    except Exception as e:
        log.error(f"Failed to checkout target LLVM: {e}")
        return False

    # Build LLVM (no patches)
    log.info("Building LLVM at target commit (no IR patches)...")
    try:
        llvm_prefix = build_llvm(
            _llvm_project_path(), llvm_install,
            required_hash=target_llvm_hash)
        ctx = ctx.copy_with(llvm_prefix=str(llvm_prefix))
        log.status(True, "Baseline LLVM build complete")
    except Exception as e:
        log.error(f"Baseline LLVM build failed: {e}")
        return False

    # Build TA and fix compile errors
    log.info("Building TA with new LLVM (no IR patches)...")
    ctx = _build_triton(ctx, config, clean=True)
    if ctx.build_passed:
        log.status(True, "TA builds successfully -- compile errors resolved")
        return True

    if config.skip_ai_analysis:
        return False

    log.warning("Build failed -- entering compile-error fix loop")
    for fix_attempt in range(1, config.max_retries + 1):
        is_npu_ir = _detect_ascend_npu_ir_errors()
        ctx = ctx.copy_with(fix_errors=[str(WORKSPACE_DIR / BUILD_RESULT_FILE)])
        ctx = ai_fix(ctx, config, attempt=fix_attempt, ascend_npu_ir_fix=is_npu_ir)
        modified_files = get_last_ai_result().modified_files
        fix_valid, fix_reason = validate_fix(modified_files, ascend_path)
        if not fix_valid:
            rejection_file = step_dir / "fix_rejection.txt"
            rejection_file.write_text(
                f"PREVIOUS FIX WAS REJECTED: {fix_reason}\n", encoding="utf-8")
            ctx = ctx.copy_with(
                fix_errors=ctx.fix_errors + [str(rejection_file)])
            continue
        ctx = _build_triton(ctx, config, clean=False)
        if ctx.build_passed:
            log.status(True, "TA builds successfully -- compile errors resolved")
            return True

    log.error(f"TA build still failing after {config.max_retries} fixes")
    return False


def _ir_patch_loop(ctx: WorkflowContext, config: TAConfig,
                   ascend_path: Path, step: dict, step_id: str,
                   step_dir: Path,
                   target_llvm_hash: str) -> WorkflowContext:
    """Phase 2: IR analysis + patch + test loop with IR retry."""
    from TA_main2main_workflow.pipeline.build import _build_triton

    for iteration in range(config.ir_max_iterations):
        ctx = ctx.copy_with(ir_patch_iteration=iteration)
        log.header(f"IR Patch Loop -- {step_id} (iter {iteration + 1}/{config.ir_max_iterations})")

        # OP analysis (first iteration only)
        ir_dir = WORKSPACE_DIR / IR_ANALYSIS_DIR
        ir_dir.mkdir(parents=True, exist_ok=True)
        if iteration == 0:
            if not _do_ir_op_analysis(ctx, config, ascend_path, ir_dir):
                return ctx.copy_with(test_passed=False)
            if not _do_ir_change_analysis(ctx, config, ascend_path, ir_dir,
                                          target_llvm_hash):
                return ctx.copy_with(test_passed=False)
        else:
            if not _do_ir_change_analysis(ctx, config, ascend_path, ir_dir,
                                          target_llvm_hash):
                return ctx.copy_with(test_passed=False)

        # Generate patches
        if not _do_ir_generate_patches(ctx, config, ascend_path, ir_dir,
                                        target_llvm_hash):
            return ctx.copy_with(test_passed=False)

        # Apply patches + rebuild LLVM
        if not _do_ir_apply_patches_and_rebuild(ctx, config, ascend_path,
                                                 target_llvm_hash):
            log.error("LLVM patch apply/rebuild failed after all retries")
            return ctx.copy_with(test_passed=False)

        # Build TA
        ctx = _build_triton(ctx, config, clean=(iteration == 0))
        if not ctx.build_passed:
            for fix_attempt in range(1, config.max_retries + 1):
                ctx = ctx.copy_with(
                    fix_errors=[str(WORKSPACE_DIR / BUILD_RESULT_FILE)])
                ctx = ai_fix(ctx, config, attempt=fix_attempt)
                ctx = _build_triton(ctx, config, clean=False)
                if ctx.build_passed:
                    break
            if not ctx.build_passed:
                continue

        # Test + fix with IR retry
        test_ok = _test_with_ir_retry(ctx, config, ascend_path, step,
                                       step_id, iteration)
        if test_ok:
            return ctx.copy_with(test_passed=True)

        log.warning(f"IR patch iteration {iteration + 1} -- IR issues remain")

    log.error(f"IR patch loop exhausted {config.ir_max_iterations} iterations")
    return ctx.copy_with(test_passed=False)


# ═══════════════════════════════════════════════════════════════════════════
# IR Analysis helpers
# ═══════════════════════════════════════════════════════════════════════════

def _llvm_hash_did_change(ascend_path: Path) -> bool:
    llvm_hash_file = ascend_path / "cmake" / "llvm-hash.txt"
    if not llvm_hash_file.exists():
        return False
    current_hash = llvm_hash_file.read_text(encoding="utf-8").strip()
    return current_hash != _ASCEND_BASELINE_LLVM_HASH


def _do_ir_op_analysis(ctx: WorkflowContext, config: TAConfig,
                       ascend_path: Path, ir_dir: Path) -> bool:
    """AI analyzes which MLIR OPs the Ascend backend uses."""
    log.header("IR OP Analysis")
    candidate_files: list[str] = []
    ascend_root = ascend_path / "third_party" / "ascend"
    scan_dirs = [ascend_root, ascend_path / "lib" / "Target" / "Ascend"]
    op_patterns = [
        r'::create\b', r'::get\b', r'\.match\b', r'\.walk\b',
        r'isa<', r'cast<', r'dyn_cast<',
    ]
    for sd in scan_dirs:
        if not sd.exists():
            continue
        for pattern in op_patterns:
            try:
                result = subprocess.run(
                    ["grep", "-rl", "--exclude-dir=patch", "--exclude-dir=cmake",
                     pattern, str(sd)],
                    capture_output=True, text=True, timeout=30)
                for f in result.stdout.splitlines():
                    if f not in candidate_files:
                        candidate_files.append(f)
            except Exception:
                pass
    candidate_files.sort()
    log.info(f"Found {len(candidate_files)} candidate files with MLIR OP patterns")

    hint_path = ir_dir / "candidate_files.txt"
    hint_path.write_text("\n".join(candidate_files), encoding="utf-8")

    try:
        run_opencode_adapter({
            "step_id": "ir-analyze-ops",
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
            "mode": "ir_analyze_ops",
            "error_logs": "[]",
            "target_commit": ctx.target_commit,
            "llvm_project_path": str(_llvm_project_path()),
        })
    except Exception as e:
        log.error(f"IR OP analysis failed: {e}")
        return False

    ops_report = ir_dir / IR_OPS_REPORT_FILE
    if ops_report.exists():
        try:
            data = json.loads(ops_report.read_text(encoding="utf-8"))
            ops_list = data.get("ops", data.get("changes", []))
            log.status(True, f"OP analysis: {len(ops_list)} OPs identified")
            return True
        except Exception:
            pass
    return False


def _do_ir_change_analysis(ctx: WorkflowContext, config: TAConfig,
                            ascend_path: Path, ir_dir: Path,
                            target_llvm_hash: str) -> bool:
    """AI analyzes OP definition changes between LLVM versions."""
    log.header("IR Change Analysis")
    baseline_hash = _ASCEND_BASELINE_LLVM_HASH
    llvm_project = _llvm_project_path()

    # Pre-flight: ensure commits exist
    for label, h in [("Baseline", baseline_hash), ("Target", target_llvm_hash)]:
        cat_proc = subprocess.run(
            ["git", "cat-file", "-t", h],
            cwd=str(llvm_project), capture_output=True, text=True, timeout=30)
        if cat_proc.returncode != 0:
            log.warning(f"{label} commit {h[:12]} not found locally -- fetching...")
            fetched = False
            for attempt in range(1, 7):
                fetch_proc = subprocess.run(
                    ["git", "fetch", "origin", h, "--no-tags"],
                    cwd=str(llvm_project), capture_output=True, text=True, timeout=300)
                if fetch_proc.returncode == 0:
                    fetched = True
                    log.status(True, f"{label} commit {h[:12]} fetched (attempt {attempt})")
                    break
            if not fetched:
                log.error(f"{label} commit {h[:12]} not found after 6 fetch attempts")
                return False

    try:
        run_opencode_adapter({
            "step_id": "ir-analyze-changes",
            "previous_step_id": "ir-analyze-ops",
            "previous_step_summary_path": str(ir_dir / IR_OPS_REPORT_FILE),
            "is_last_step": "true",
            "step_index": "ir",
            "step_dir": str(ir_dir),
            "fix_dir": str(ir_dir),
            "conflict_dir": "",
            "ascend_path": str(ascend_path),
            "triton_path": ctx.triton_path,
            "reference_dir": _REF,
            "mode": "ir_analyze_changes",
            "error_logs": json.dumps([str(ir_dir / IR_OPS_REPORT_FILE)], ensure_ascii=False),
            "target_commit": ctx.target_commit,
            "llvm_project_path": str(llvm_project),
            "baseline_llvm_hash": baseline_hash,
            "target_llvm_hash": target_llvm_hash,
        })
    except Exception as e:
        log.error(f"IR change analysis failed: {e}")
        return False

    changes_report = ir_dir / IR_CHANGES_REPORT_FILE
    if changes_report.exists():
        try:
            data = json.loads(changes_report.read_text(encoding="utf-8"))
            summary = data.get("summary", {})
            log.status(True,
                f"Change analysis: {summary.get('total_ops_analyzed', '?')} OPs, "
                f"{summary.get('ops_needing_patch', '?')} need patch")
            return True
        except Exception:
            pass
    return False


def _do_ir_generate_patches(ctx: WorkflowContext, config: TAConfig,
                             ascend_path: Path, ir_dir: Path,
                             target_llvm_hash: str) -> bool:
    """AI modifies the Ascend LLVM patch for IR compatibility."""
    log.header("IR Patch Generation")
    ascend_patch = ascend_path / "third_party" / "ascend" / "patch" / "llvm_patch_f6ded0b.patch"

    try:
        run_opencode_adapter({
            "step_id": "ir-generate-patch",
            "previous_step_id": "ir-analyze-changes",
            "previous_step_summary_path": str(ir_dir / IR_CHANGES_REPORT_FILE),
            "is_last_step": "true",
            "step_index": "ir",
            "step_dir": str(ascend_patch.parent),
            "fix_dir": str(ascend_patch.parent),
            "conflict_dir": "",
            "ascend_path": str(ascend_path),
            "triton_path": ctx.triton_path,
            "reference_dir": _REF,
            "mode": "ir_generate_patch",
            "error_logs": json.dumps([str(ir_dir / IR_CHANGES_REPORT_FILE)], ensure_ascii=False),
            "target_commit": ctx.target_commit,
            "llvm_project_path": str(_llvm_project_path()),
            "baseline_llvm_hash": _ASCEND_BASELINE_LLVM_HASH,
            "target_llvm_hash": target_llvm_hash,
            "ascend_patch_file": str(ascend_patch),
        })
    except Exception as e:
        log.error(f"IR patch generation failed: {e}")
        return False

    if ascend_patch.exists():
        log.status(True, f"Modified {ascend_patch.name}")
        return True
    log.info("No changes needed")
    return True


def _do_ir_apply_patches_and_rebuild(ctx: WorkflowContext, config: TAConfig,
                                      ascend_path: Path,
                                      target_llvm_hash: str) -> bool:
    """Apply patches + rebuild LLVM with retry loop (max IR_MAX_ITERATIONS)."""
    from TA_main2main_workflow.scripts.build_test import apply_llvm_patches, build_llvm

    llvm_project = _llvm_project_path()
    ascend_patch = ascend_path / "third_party" / "ascend" / "patch" / "llvm_patch_f6ded0b.patch"
    llvm_install = Path(os.path.expanduser(
        config.llvm_install_prefix_sync or "~/llvm-install-sync"))

    for retry in range(IR_MAX_ITERATIONS + 1):
        is_retry = retry > 0
        if is_retry:
            log.header(f"Patch Apply/Rebuild Retry {retry}/{IR_MAX_ITERATIONS}")

        if not _ensure_llvm_workspace_clean():
            log.error("Cannot clean llvm-project workspace")
            return False

        patch_result = apply_llvm_patches(
            ascend_patch.parent, llvm_project,
            target_hash=target_llvm_hash, patch_file=ascend_patch)
        if not patch_result["all_ok"]:
            failed = patch_result.get("failed", [])
            error_msg = failed[0]['error'][:500] if failed else "unknown"
            log.error(f"LLVM patch apply failed: {error_msg}")
            if retry < IR_MAX_ITERATIONS:
                log.warning("AI will fix the patch")
                _fix_patch_and_retry(ctx, config, ascend_path, ascend_patch,
                                     target_llvm_hash, "apply", error_msg, retry + 1)
                continue
            return False

        log.status(True, f"{ascend_patch.name} applied")

        # Build LLVM
        try:
            log.info("Rebuilding LLVM...")
            llvm_prefix = build_llvm(llvm_project, llvm_install,
                                     required_hash=target_llvm_hash)
            ctx = ctx.copy_with(llvm_prefix=str(llvm_prefix))
            log.status(True, "LLVM rebuild complete")
            return True
        except Exception as e:
            build_error = str(e)[:500]
            build_log = WORKSPACE_DIR / "llvm_build.log"
            if build_log.exists():
                try:
                    log_tail = build_log.read_text(
                        encoding="utf-8", errors="replace")[-3000:]
                    build_error = f"Build exception: {e}\n\nBuild log tail:\n{log_tail}"
                except Exception:
                    pass
            log.error(f"LLVM rebuild failed: {e}")
            if retry < IR_MAX_ITERATIONS:
                log.warning("AI will fix the patch based on build error")
                _fix_patch_and_retry(ctx, config, ascend_path, ascend_patch,
                                     target_llvm_hash, "build", build_error, retry + 1)
                continue
            return False
    return False


def _fix_patch_and_retry(ctx: WorkflowContext, config: TAConfig,
                          ascend_path: Path, ascend_patch: Path,
                          target_llvm_hash: str, error_type: str,
                          error_msg: str, retry: int) -> None:
    """Invoke AI to fix a broken IR compatibility patch."""
    log.info(f"Invoking AI to fix patch ({error_type} failure, retry {retry})...")
    try:
        run_opencode_adapter({
            "step_id": f"ir-fix-patch-{retry}",
            "previous_step_id": "ir-generate-patch",
            "previous_step_summary_path": "",
            "is_last_step": "false",
            "step_index": "ir",
            "step_dir": str(ascend_patch.parent),
            "fix_dir": str(ascend_patch.parent),
            "conflict_dir": "",
            "ascend_path": str(ascend_path),
            "triton_path": ctx.triton_path,
            "reference_dir": _REF,
            "mode": "ir_generate_patch",
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


def _test_with_ir_retry(ctx: WorkflowContext, config: TAConfig,
                         ascend_path: Path, step: dict,
                         step_id: str, ir_iteration: int) -> bool:
    """Test + AI fix loop with embedded IR patch retry."""
    from TA_main2main_workflow.pipeline.build import _build_triton
    from TA_main2main_workflow.pipeline.test import _run_pytest, _detect_oom_in_tests
    from TA_main2main_workflow.pipeline.test import _rerun_tests_reduced_concurrency

    _MAX_IR_RETRIES = 3
    ir_retries = 0
    code_fix_attempt = 0

    while ir_retries <= _MAX_IR_RETRIES and code_fix_attempt <= config.max_retries:
        ctx = _run_pytest(ctx, config)
        if ctx.test_passed:
            log.status(True, f"All tests pass for {step_id}")
            return True

        if _detect_oom_in_tests():
            log.warning("NPU OOM detected -- rerunning with reduced concurrency")
            oom_result = _rerun_tests_reduced_concurrency(ascend_path, config)
            if oom_result is None or oom_result:
                return True
            if not _detect_oom_in_tests():
                log.info("OOM resolved -- classifying remaining failures")
            else:
                return False

        # Classify failures: IR vs code
        log.warning("Tests failed -- classifying failures (IR vs code)...")
        has_ir_issues = _do_ir_diagnose_failures(ctx, ascend_path)
        if has_ir_issues:
            ir_retries += 1
            log.warning(f"IR issues detected (IR retry {ir_retries}/{_MAX_IR_RETRIES})")
            if not _do_ir_generate_patches(ctx, config, ascend_path,
                                            WORKSPACE_DIR / IR_ANALYSIS_DIR,
                                            ""):
                return False
            for patch_attempt in range(IR_MAX_ITERATIONS):
                if _do_ir_apply_patches_and_rebuild(
                        ctx, config, ascend_path, ""):
                    break
            else:
                continue
            ctx = _build_triton(ctx, config, clean=False)
            continue

        # Code issues -> AI fix
        code_fix_attempt += 1
        log.warning(f"Code issues -- AI fix {code_fix_attempt}/{config.max_retries}")
        from TA_main2main_workflow.pipeline.test import _collect_test_error_logs
        ctx = ctx.copy_with(fix_errors=_collect_test_error_logs())
        ctx = ai_fix(ctx, config, attempt=code_fix_attempt)
        ctx = _build_triton(ctx, config, clean=False)

    return False


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
            "previous_step_id": "ir-generate-patch",
            "previous_step_summary_path": str(ir_dir / IR_CHANGES_REPORT_FILE),
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

    log.warning(f"llvm-project has uncommitted changes -- cleaning...")
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
