"""Pipeline step: IR patch generation for LLVM version changes.

Handles the full IR patch pipeline when the LLVM hash changes:
  1. Build baseline LLVM (pre-merge, from base branch's llvm-hash)
  2. Per-step: apply existing patch → build LLVM → build TA → test
  3. On failure: AI adjust patch → rebuild LLVM → rebuild TA → retest
  4. AI supplement missing IR patches → retest loop
  5. Fallback: full OP analysis pipeline

Key entry points:
  - ``build_baseline_llvm(ctx, config)`` — Build baseline LLVM once
  - ``per_step_ir_patch(ctx, config, step)`` — Per-step IR patch flow
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path

from TA_main2main_workflow.agent.opencode_adapter import run_opencode_adapter
from TA_main2main_workflow.utils.config import TAConfig
from TA_main2main_workflow.utils.context import WorkflowContext
from TA_main2main_workflow.utils.logging import get_logger
from TA_main2main_workflow.utils.git import run_git, run_git_no_check, stream_cmd
from TA_main2main_workflow.pipeline.build import build_and_fix_loop
from TA_main2main_workflow.pipeline.test import (
    run_tests,
    detect_oom_in_tests,
    rerun_tests_reduced_concurrency,
    test_and_fix_loop,
    _run_pretest_and_fix,
)
from TA_main2main_workflow.utils import (
    WORKSPACE_DIR,
    STEPS_DIR,
    TEST_RESULT_FILE,
    IR_ANALYSIS_DIR,
    IR_OPS_REPORT_FILE,
    IR_CHANGES_REPORT_FILE,
    IR_DIAGNOSIS_FILE,
    _ASCEND_BASELINE_LLVM_HASH,
)

log = get_logger(__name__)
_REF = str(Path(__file__).parent.parent / "reference")

# Maximum retries for LLVM patch apply/rebuild loop
_MAX_LLVM_RETRIES = 10
# Maximum retries for applying existing patch
_MAX_PATCH_APPLY_RETRIES = 3


# ═══════════════════════════════════════════════════════════════════════════
# Baseline LLVM build (pre-merge, called once)
# ═══════════════════════════════════════════════════════════════════════════


def build_baseline_llvm(ctx: WorkflowContext, config: TAConfig) -> WorkflowContext:
    """Build baseline LLVM from base branch's llvm-hash + existing patch.

    Called once at the start of single-step mode.  This ensures LLVM is
    built and ready before any merge steps begin.  After building, stashes
    the patch changes so the LLVM tree is clean for subsequent steps.
    """
    ascend_path = Path(ctx.triton_ascend_path)
    llvm_project = config.llvm_project
    llvm_install = config.llvm_install

    hash_file = ascend_path / "cmake" / "llvm-hash.txt"
    if not hash_file.exists():
        log.info("No llvm-hash.txt — skipping baseline LLVM build")
        return ctx

    required_hash = hash_file.read_text(encoding="utf-8").strip()
    if not required_hash:
        return ctx

    log.header("Build Baseline LLVM (pre-merge)")

    # ── Print LLVM workspace info ──
    log.key_value("LLVM project", str(llvm_project))
    log.key_value("LLVM install prefix", str(llvm_install))
    log.key_value("Target LLVM hash", required_hash[:12])

    # Allow skipping baseline LLVM build (LLVM already built for current TA)
    if config.skip_baseline_llvm:
        log.status(
            True, "SKIP_BASELINE_LLVM set — assuming baseline LLVM already built"
        )
        return ctx.copy_with(build_passed=True)

    # Ensure llvm-project exists
    if not llvm_project.exists():
        run_git(WORKSPACE_DIR, "clone", config.llvm_repo_url, str(llvm_project))

    # Ensure clean workspace
    _ensure_llvm_workspace_clean(llvm_project, "baseline LLVM build")

    # Checkout the required hash
    log.info(f"Checking out LLVM hash: {required_hash[:12]}")
    _ensure_commit_available(llvm_project, required_hash)
    run_git(llvm_project, "checkout", "-f", required_hash)
    run_git(llvm_project, "clean", "-fd")

    # Apply existing Ascend patch
    patch_dir = ascend_path / "third_party/ascend/patch"
    patch_files = sorted(patch_dir.glob("*.patch")) if patch_dir.exists() else []
    if patch_files:
        patch_file = patch_files[0]
        log.info(f"Applying existing patch: {patch_file.name}")
        try:
            run_git(llvm_project, "apply", str(patch_file))
        except Exception as e:
            log.warning(f"Patch apply failed: {e} — will build without patch")
    else:
        log.info("No existing Ascend patch found — building clean LLVM")

    # Build LLVM
    try:
        prefix = _do_llvm_build(llvm_project, llvm_install, required_hash)
        log.status(True, f"Baseline LLVM built at {prefix}")
    except Exception as e:
        log.error(f"Baseline LLVM build failed: {e}")
        return ctx.copy_with(build_passed=False)

    # Stash/drop patch changes to leave clean tree
    _ensure_llvm_workspace_clean(llvm_project, "post-baseline build")

    # Set SKIP_LLVM_REBUILD so downstream builds don't wipe LLVM
    os.environ["SKIP_LLVM_REBUILD"] = "true"

    return ctx.copy_with(build_passed=True)


# ═══════════════════════════════════════════════════════════════════════════
# Per-step IR patch pipeline
# ═══════════════════════════════════════════════════════════════════════════


def per_step_ir_patch(
    ctx: WorkflowContext, config: TAConfig, step: dict
) -> WorkflowContext:
    """Full per-step IR patch pipeline for LLVM version changes.

    Strategy: apply existing patch first → build → test → AI supplement.
    Falls back to full OP analysis if the existing-patch-first approach
    exhausts retries.
    """
    ascend_path = Path(ctx.triton_ascend_path)
    step_id = step["id"]

    if config.skip_ir_patch:
        log.header(f"IR Patch Pipeline — {step_id} — SKIPPED (SKIP_IR_PATCH=true)")
        return ctx.copy_with(build_passed=True, test_passed=True)

    step_dir = WORKSPACE_DIR / STEPS_DIR / step_id
    step_dir.mkdir(parents=True, exist_ok=True)

    target_llvm_hash = _get_current_llvm_hash(ascend_path)
    log.header(f"IR Patch Pipeline — {step_id}")
    log.key_value("target LLVM hash", target_llvm_hash[:12])

    # ── Phase 1: Apply existing patch + build LLVM ──────────────────
    if not _do_apply_existing_patch(ctx, config, step, target_llvm_hash):
        log.warning("Existing patch apply failed after retries — falling back")
        return _per_step_ir_patch_fallback(ctx, config, step, target_llvm_hash)

    # ── Phase 2: Build Triton-Ascend with AI fix loop ──────────────
    log.section(f"Build TA with AI Fix — {step_id}")
    build_ctx = _do_ta_build_with_fix(ctx, config, step)
    if not build_ctx.build_passed:
        log.error(f"TA build failed for {step_id}")
        return build_ctx

    # ── Phase 3: Test + IR supplement loop ─────────────────────────
    log.section(f"Test + IR Supplement — {step_id}")
    ctx = _do_test_and_fix_with_ir_retry(build_ctx, config, step, target_llvm_hash)

    return ctx


# ═══════════════════════════════════════════════════════════════════════════
# Phase 1: Apply existing patch
# ═══════════════════════════════════════════════════════════════════════════


def _do_apply_existing_patch(
    ctx: WorkflowContext,
    config: TAConfig,
    step: dict,
    target_llvm_hash: str,
) -> bool:
    """Apply existing Ascend LLVM patch to llvm-project, with AI fix retry.

    Returns True if patch applies and LLVM builds successfully.
    """
    ascend_path = Path(ctx.triton_ascend_path)
    llvm_project = config.llvm_project
    llvm_install = config.llvm_install
    _ = step["id"]

    patch_dir = ascend_path / "third_party/ascend/patch"
    patch_files = sorted(patch_dir.glob("*.patch")) if patch_dir.exists() else []

    if not patch_files:
        log.info("No existing patch — building LLVM directly")
        if config.skip_llvm_rebuild:
            log.status(True, "SKIP_LLVM_REBUILD set — assuming LLVM already built")
            return True
        try:
            _do_llvm_build(llvm_project, llvm_install, target_llvm_hash)
            return True
        except Exception:
            return False

    for attempt in range(1, _MAX_PATCH_APPLY_RETRIES + 1):
        log.step(attempt, _MAX_PATCH_APPLY_RETRIES, "Apply existing patch")

        # ── Clean LLVM workspace ──
        _ensure_llvm_workspace_clean(llvm_project, f"patch apply attempt {attempt}")
        _ensure_commit_available(llvm_project, target_llvm_hash)
        run_git(llvm_project, "checkout", "-f", target_llvm_hash)
        run_git(llvm_project, "clean", "-fd")

        # ── Apply patch ──
        patch_file = patch_files[0]
        log.info(f"Applying: {patch_file.name}")
        try:
            run_git(llvm_project, "apply", str(patch_file))
        except Exception as e:
            log.error(f"Patch apply failed: {e}")
            if attempt > 1:
                continue
            # First failure: try AI fix
            log.info("Attempting AI patch adjustment...")
            try:
                _ai_adjust_patch_for_failure(ctx, config, step, str(e))
            except Exception:
                pass
            continue
        log.info(f"Applying existing patch Successfully (attempt {attempt})")
        # ── Build LLVM ──
        if config.skip_llvm_rebuild:
            log.status(
                True,
                f"SKIP_LLVM_REBUILD set — assuming LLVM already built (attempt {attempt})",
            )
            return True
        try:
            _do_llvm_build(llvm_project, llvm_install, target_llvm_hash)
            log.status(True, f"LLVM build with existing patch OK (attempt {attempt})")
            return True
        except Exception as e:
            log.error(f"LLVM build failed: {e}")
            if attempt < _MAX_PATCH_APPLY_RETRIES:
                log.info("Attempting AI patch fix...")
                try:
                    _ai_adjust_patch_for_failure(ctx, config, step, str(e))
                except Exception:
                    pass

    return False


# ═══════════════════════════════════════════════════════════════════════════
# Phase 2: Build TA with AI fix
# ═══════════════════════════════════════════════════════════════════════════


def _do_ta_build_with_fix(
    ctx: WorkflowContext,
    config: TAConfig,
    step: dict,
) -> WorkflowContext:
    """Build Triton-Ascend with AI fix loop for compile errors."""
    return build_and_fix_loop(ctx, config)


# ═══════════════════════════════════════════════════════════════════════════
# Phase 3: Test + IR supplement loop
# ═══════════════════════════════════════════════════════════════════════════


def _do_test_and_fix_with_ir_retry(
    ctx: WorkflowContext,
    config: TAConfig,
    step: dict,
    target_llvm_hash: str,
) -> WorkflowContext:
    """Test with IR supplement loop.

    Runs pytest, classifies failures.  IR issues → supplement patch →
    rebuild LLVM → rebuild TA → retest.  Code issues → AI fix loop.
    Max 3 IR supplement iterations.
    """
    _ = step["id"]
    ascend_path = Path(ctx.triton_ascend_path)
    llvm_project = config.llvm_project
    llvm_install = config.llvm_install
    ir_max = config.ir_max_iterations

    for ir_iter in range(ir_max + 1):
        if ir_iter > 0:
            log.header(f"IR Supplement Iteration {ir_iter}/{ir_max}")

        # ── Pre-test: smoke check before every test run ──────────────
        if not config.skip_e2e_test:
            ctx = _run_pretest_and_fix(ctx, config, ascend_path)
            if not ctx.test_passed:
                log.error("Pre-test failed after all retries — aborting")
                return ctx.copy_with(test_passed=False, pytest_passed=False)

        # ── Run tests ──
        ctx = run_tests(ctx, config)
        if ctx.test_passed:
            log.status(True, f"All tests passed (IR iter {ir_iter})")
            return ctx.copy_with(test_passed=True, pytest_passed=True)

        # ── OOM detection ──
        if detect_oom_in_tests(ctx):
            log.warning("OOM detected — reducing concurrency")
            oom_ctx = rerun_tests_reduced_concurrency(ascend_path, config)
            if oom_ctx is not None and oom_ctx.test_passed:
                return ctx.copy_with(test_passed=True, pytest_passed=True)

        if ir_iter >= ir_max:
            log.error(f"IR supplement loop exhausted ({ir_max} iterations)")
            break

        # ── Classify failures: IR vs code ──
        is_ir_issue = _classify_test_failures(ctx, config, step)
        if is_ir_issue:
            log.info(
                f"IR issues detected — supplementing patch (iter {ir_iter + 1}/{ir_max})"
            )
            _ir_supplement_patch(
                ctx, config, step, target_llvm_hash, supplement_iter=ir_iter + 1
            )
            # Rebuild LLVM with updated patch
            if config.skip_llvm_rebuild:
                log.status(
                    True,
                    "SKIP_LLVM_REBUILD set — skipping LLVM rebuild after supplement",
                )
            else:
                try:
                    # Re-apply updated patch to clean workspace before building
                    _clean_checkout_apply_patch(
                        llvm_project,
                        ascend_path,
                        target_llvm_hash,
                        reason=f"IR supplement iter {ir_iter + 1}",
                    )
                    _do_llvm_build(llvm_project, llvm_install, target_llvm_hash)
                except Exception as e:
                    log.error(f"LLVM rebuild after supplement failed: {e}")
                    continue
            # Rebuild TA
            ctx = _do_ta_build_with_fix(ctx, config, step)
            if not ctx.build_passed:
                return ctx
        else:
            log.info("Code issues detected — entering AI fix loop")
            ctx = _do_ai_fix_loop(ctx, config, step)
            if ctx.test_passed:
                return ctx

    return ctx.copy_with(test_passed=False, pytest_passed=False)


# ═══════════════════════════════════════════════════════════════════════════
# Fallback: Full OP analysis pipeline
# ═══════════════════════════════════════════════════════════════════════════


def _per_step_ir_patch_fallback(
    ctx: WorkflowContext,
    config: TAConfig,
    step: dict,
    target_llvm_hash: str,
) -> WorkflowContext:
    """Fallback: Full OP analysis pipeline when existing-patch-first fails.

    Does: IR OP analysis → IR change analysis → IR generate patches →
    apply patches + rebuild LLVM → build TA → test.
    """
    ascend_path = Path(ctx.triton_ascend_path)
    llvm_project = config.llvm_project
    llvm_install = config.llvm_install
    step_id = step["id"]

    log.header(f"IR Patch Fallback — Full OP Analysis — {step_id}")

    # 1. IR OP analysis (AI scans Ascend code for MLIR OP usage)
    log.section("IR OP Analysis")
    try:
        ops_report = _run_ir_op_analysis(ctx, config)
        ctx = ctx.copy_with(ir_ops_report=ops_report, ir_analysis_done=True)
    except Exception as e:
        log.warning(f"OP analysis failed: {e}")

    # 2. IR change analysis (AI compares OP definitions between LLVM versions)
    log.section("IR Change Analysis")
    try:
        changes_report = _run_ir_change_analysis(ctx, config, target_llvm_hash)
        ctx = ctx.copy_with(ir_changes_report=changes_report)
    except Exception as e:
        log.warning(f"Change analysis failed: {e}")

    # 3. Generate IR patches
    log.section("IR Patch Generation")
    try:
        _run_ir_generate_patches(ctx, config, step, target_llvm_hash)
    except Exception as e:
        log.warning(f"Patch generation failed: {e}")

    # 4. Apply generated patch to clean workspace, then build LLVM
    if config.skip_llvm_rebuild:
        log.status(True, "SKIP_LLVM_REBUILD set — skipping LLVM build in fallback")
    else:
        try:
            _clean_checkout_apply_patch(
                llvm_project,
                ascend_path,
                target_llvm_hash,
                reason="fallback IR pipeline",
            )
            _do_llvm_build(llvm_project, llvm_install, target_llvm_hash)
        except Exception as e:
            log.error(f"LLVM build failed: {e}")
            return ctx.copy_with(build_passed=False)

    # 5. Build TA
    ctx = _do_ta_build_with_fix(ctx, config, step)
    if not ctx.build_passed:
        return ctx

    # 6. Test
    ctx = run_tests(ctx, config)
    return ctx


# ═══════════════════════════════════════════════════════════════════════════
# IR analysis sub-steps
# ═══════════════════════════════════════════════════════════════════════════


def _ir_ai_base(
    ctx: WorkflowContext,
    config: TAConfig,
    step_id: str,
    ir_dir: Path,
    mode: str,
    error_logs: str = "[]",
    extra: dict | None = None,
) -> dict:
    """Build the common AI context dict for IR patch calls."""
    ascend_path = Path(ctx.triton_ascend_path)
    base = {
        "step_id": f"{step_id}-{mode}",
        "previous_step_id": "",
        "previous_step_summary_path": "",
        "is_last_step": "true",
        "step_dir": str(WORKSPACE_DIR / STEPS_DIR / step_id),
        "fix_dir": str(ir_dir),
        "conflict_dir": str(WORKSPACE_DIR / "conflicts"),
        "ascend_path": str(ascend_path),
        "triton_path": ctx.triton_ascend_path,
        "reference_dir": _REF,
        "mode": mode,
        "error_logs": error_logs,
        "target_commit": ctx.target_commit,
        "step_index": f"{ctx.current_step + 1}/{ctx.total_steps}",
        "llvm_project_path": str(config.llvm_project),
    }
    if extra:
        base.update(extra)
    return base


def _run_ir_op_analysis(ctx: WorkflowContext, config: TAConfig) -> dict:
    """AI scans Ascend backend code for MLIR OP usage. Returns ops_report."""
    step = ctx.steps[ctx.current_step] if ctx.steps else {"id": "step-0"}
    step_id = step["id"]
    ir_dir = WORKSPACE_DIR / STEPS_DIR / step_id / IR_ANALYSIS_DIR
    ir_dir.mkdir(parents=True, exist_ok=True)

    result = run_opencode_adapter(
        _ir_ai_base(ctx, config, step_id, ir_dir, "ir_op_analysis")
    )

    ops_file = ir_dir / IR_OPS_REPORT_FILE
    if hasattr(result, "step_summary") and result.step_summary:
        try:
            ops_data = json.loads(result.step_summary)
            ops_file.write_text(
                json.dumps(ops_data, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            return ops_data
        except json.JSONDecodeError:
            pass
    return {}


def _run_ir_change_analysis(
    ctx: WorkflowContext, config: TAConfig, target_llvm_hash: str
) -> dict:
    """AI compares OP .td definitions between baseline and target LLVM."""
    step = ctx.steps[ctx.current_step] if ctx.steps else {"id": "step-0"}
    step_id = step["id"]
    ir_dir = WORKSPACE_DIR / STEPS_DIR / step_id / IR_ANALYSIS_DIR
    ir_dir.mkdir(parents=True, exist_ok=True)

    result = run_opencode_adapter(
        _ir_ai_base(
            ctx,
            config,
            step_id,
            ir_dir,
            "ir_change_analysis",
            error_logs=json.dumps(
                {
                    "baseline_llvm_hash": _ASCEND_BASELINE_LLVM_HASH,
                    "target_llvm_hash": target_llvm_hash,
                },
                ensure_ascii=False,
            ),
            extra={
                "baseline_llvm_hash": _ASCEND_BASELINE_LLVM_HASH,
                "target_llvm_hash": target_llvm_hash,
            },
        )
    )

    changes_file = ir_dir / IR_CHANGES_REPORT_FILE
    if hasattr(result, "step_summary") and result.step_summary:
        try:
            changes_data = json.loads(result.step_summary)
            changes_file.write_text(
                json.dumps(changes_data, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            return changes_data
        except json.JSONDecodeError:
            pass
    return {}


def _run_ir_generate_patches(
    ctx: WorkflowContext, config: TAConfig, step: dict, target_llvm_hash: str = ""
) -> None:
    """AI modifies the existing patch file in-place for new LLVM version."""
    step_id = step["id"]
    ir_dir = WORKSPACE_DIR / STEPS_DIR / step_id / IR_ANALYSIS_DIR
    ir_dir.mkdir(parents=True, exist_ok=True)

    ascend_path = Path(ctx.triton_ascend_path)
    patch_dir = ascend_path / "third_party/ascend/patch"
    patch_files = sorted(patch_dir.glob("*.patch")) if patch_dir.exists() else []
    ascend_patch_file = str(patch_files[0]) if patch_files else ""

    run_opencode_adapter(
        _ir_ai_base(
            ctx,
            config,
            step_id,
            ir_dir,
            "ir_patch_gen",
            error_logs=json.dumps(
                {
                    "ops_report": ctx.ir_ops_report,
                    "changes_report": ctx.ir_changes_report,
                },
                ensure_ascii=False,
                default=str,
            ),
            extra={
                "baseline_llvm_hash": _ASCEND_BASELINE_LLVM_HASH,
                "target_llvm_hash": target_llvm_hash,
                "ascend_patch_file": ascend_patch_file,
            },
        )
    )


# ═══════════════════════════════════════════════════════════════════════════
# AI fix helpers for IR pipeline
# ═══════════════════════════════════════════════════════════════════════════


def _ai_adjust_patch_for_failure(
    ctx: WorkflowContext,
    config: TAConfig,
    step: dict,
    error_info: str,
) -> None:
    """AI adjusts the LLVM patch after build failure."""
    step_id = step["id"]
    ir_dir = WORKSPACE_DIR / STEPS_DIR / step_id / IR_ANALYSIS_DIR
    ir_dir.mkdir(parents=True, exist_ok=True)

    ascend_path = Path(ctx.triton_ascend_path)
    patch_dir = ascend_path / "third_party/ascend/patch"
    patch_files = sorted(patch_dir.glob("*.patch")) if patch_dir.exists() else []
    ascend_patch_file = str(patch_files[0]) if patch_files else ""
    target_llvm_hash = _get_current_llvm_hash(ascend_path)

    run_opencode_adapter(
        _ir_ai_base(
            ctx,
            config,
            step_id,
            ir_dir,
            "ir_patch_fix",
            error_logs=json.dumps({"error": error_info[:5000]}, ensure_ascii=False),
            extra={
                "baseline_llvm_hash": _ASCEND_BASELINE_LLVM_HASH,
                "target_llvm_hash": target_llvm_hash,
                "ascend_patch_file": ascend_patch_file,
                "adjust_mode": "patch_apply_failure",
                "patch_error_type": "apply_or_build",
                "patch_error_msg": error_info[:2000],
            },
        )
    )


def _build_focused_change_report(
    ctx: WorkflowContext,
    config: TAConfig,
    step: dict,
    target_llvm_hash: str,
) -> Path | None:
    """Analyze OP definition diffs for affected OPs identified by ir_diagnose.

    Uses ``git grep`` to find the .td files defining each affected OP,
    then runs ``git diff baseline..target`` to collect the precise changes.

    Writes the focused report to ``focused_changes.json`` in the IR
    analysis directory so the supplement AI can read it.

    Returns the path to the focused changes report, or None if no
    affected OPs could be analyzed.
    """
    step_id = step["id"]
    ir_dir = WORKSPACE_DIR / STEPS_DIR / step_id / IR_ANALYSIS_DIR
    ir_dir.mkdir(parents=True, exist_ok=True)

    baseline_hash = _ASCEND_BASELINE_LLVM_HASH
    llvm_project = config.llvm_project

    # ── Read affected OPs from diagnosis ───────────────────────────
    diagnosis = _read_diagnosis(step_id, ir_dir)
    affected_ops: list[dict] = []
    if isinstance(diagnosis, dict):
        failures = diagnosis.get("failures", [])
        for f in failures:
            if f.get("classification") == "ir_compatibility":
                op_name = f.get("affected_op", "").strip()
                if op_name and op_name not in {o["name"] for o in affected_ops}:
                    affected_ops.append(
                        {
                            "name": op_name,
                            "error_summary": f.get("error_summary", ""),
                            "rationale": f.get("rationale", ""),
                        }
                    )
    if not affected_ops:
        log.info("No ir_compatibility OPs in diagnosis — nothing to analyze")
        return None

    log.info(f"Analyzing changes for {len(affected_ops)} affected OP(s)...")

    # ── For each affected OP, find .td definition and diff ──────────
    analyzed: list[dict] = []
    seen_td_files: set[str] = set()

    for op in affected_ops:
        op_name = op["name"]
        td_relative = _find_td_file(llvm_project, target_llvm_hash, op_name)
        if not td_relative:
            log.info(f"  {op_name}: .td file not found — skipping")
            continue

        entry: dict = {
            "op_name": op_name,
            "td_file": td_relative,
            "error_summary": op["error_summary"],
            "rationale": op["rationale"],
        }

        # Only diff each .td file once (multiple OPs in same file)
        if td_relative not in seen_td_files:
            seen_td_files.add(td_relative)
            diff = _diff_td_file(
                llvm_project, baseline_hash, target_llvm_hash, td_relative
            )
            entry["td_diff"] = diff[:8000] if diff else "(no diff)"
            if diff and len(diff) > 8000:
                entry["td_diff_truncated"] = True
        else:
            entry["td_diff"] = "(see above — already included)"

        analyzed.append(entry)
        log.info(f"  {op_name}: {td_relative}")

    if not analyzed:
        return None

    # ── Write focused report ───────────────────────────────────────
    report = {
        "source_llvm_hash": baseline_hash,
        "target_llvm_hash": target_llvm_hash,
        "diagnosis_source": "ir_diagnose",
        "affected_ops": analyzed,
        "summary": {
            "total_affected_ops": len(analyzed),
            "unique_td_files": len(seen_td_files),
        },
    }
    report_path = ir_dir / "focused_changes.json"
    report_path.write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    log.status(True, f"Focused change analysis: {report_path}")
    return report_path


def _read_diagnosis(step_id: str, ir_dir: Path) -> dict:
    """Read IR diagnosis JSON, trying both possible locations."""
    for p in _diagnosis_candidates(step_id, ir_dir):
        if p.exists():
            try:
                return json.loads(p.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                pass
    return {}


def _find_diagnosis_file(step_id: str, ir_dir: Path) -> Path | None:
    """Find the IR diagnosis file, trying both possible locations."""
    for p in _diagnosis_candidates(step_id, ir_dir):
        if p.exists():
            return p
    return None


def _diagnosis_candidates(step_id: str, ir_dir: Path) -> list[Path]:
    """Candidate paths for IR diagnosis, in priority order."""
    step_dir = WORKSPACE_DIR / STEPS_DIR / step_id
    return [
        step_dir / "ir_diagnosis.json",  # where AI writes per prompt
        ir_dir / IR_DIAGNOSIS_FILE,  # where classify writes parsed
    ]


def _find_td_file(llvm_project: Path, target_hash: str, op_name: str) -> str | None:
    """Find the .td file that defines *op_name* at *target_hash*.

    Uses ``git grep`` to search for ``def OpName`` in .td files.
    """
    # Strip dialect prefix for the def search (e.g., "triton::LoadOp" → "LoadOp")
    short_name = op_name.split("::")[-1]
    try:
        result = subprocess.run(
            ["git", "grep", "-l", f"def {short_name}", target_hash, "--", "*.td"],
            cwd=str(llvm_project),
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if result.returncode != 0 or not result.stdout.strip():
        return None
    # Return first match (most OPs are defined once)
    return result.stdout.strip().split("\n")[0]


def _diff_td_file(
    llvm_project: Path, baseline_hash: str, target_hash: str, td_relative: str
) -> str:
    """Get the diff of a .td file between baseline and target LLVM."""
    try:
        result = subprocess.run(
            ["git", "diff", f"{baseline_hash}..{target_hash}", "--", td_relative],
            cwd=str(llvm_project),
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (subprocess.TimeoutExpired, OSError):
        return ""
    return result.stdout if result.returncode == 0 else ""


def _ir_supplement_patch(
    ctx: WorkflowContext,
    config: TAConfig,
    step: dict,
    target_llvm_hash: str,
    supplement_iter: int = 1,
) -> None:
    """AI supplements the existing IR patch with missing OP IR changes.

    AI is given:
    - Test failure logs showing IR errors (collected from test-logs/)
    - The existing patch file content (first 5000 bytes as context)
    - The target LLVM commit and llvm-project path for OP definition lookup
    - IR diagnosis from previous step (chained via previous_step_summary_path)
    """
    step_id = step["id"]
    ir_dir = WORKSPACE_DIR / STEPS_DIR / step_id / IR_ANALYSIS_DIR
    ir_dir.mkdir(parents=True, exist_ok=True)

    ascend_path = Path(ctx.triton_ascend_path)
    patch_dir = ascend_path / "third_party/ascend/patch"
    patch_files = sorted(patch_dir.glob("*.patch")) if patch_dir.exists() else []
    ascend_patch_file = str(patch_files[0]) if patch_files else ""

    # Collect actual test failure logs
    error_log_paths = _collect_test_error_logs()
    if not error_log_paths:
        log.warning("No test failure logs found — cannot diagnose IR issues")

    # Include IR diagnosis if available (chain from classify step)
    # Try both possible locations: where AI writes per prompt, where classify writes
    diagnosis_path = _find_diagnosis_file(step_id, ir_dir)
    if diagnosis_path:
        error_log_paths.append(str(diagnosis_path))
        log.info(f"Including IR diagnosis: {diagnosis_path}")
    else:
        diagnosis_path = None

    # ── Focused OP change analysis ──
    # Extract affected OPs from diagnosis, diff their .td definitions
    # between baseline and target LLVM, write to focused_changes.json
    focused_report_path = _build_focused_change_report(
        ctx,
        config,
        step,
        target_llvm_hash,
    )
    if focused_report_path:
        error_log_paths.append(str(focused_report_path))
        log.info(f"Including focused change analysis: {focused_report_path}")

    # Build patch content snippet for AI context
    patch_content_snippet = ""
    if ascend_patch_file:
        try:
            full = Path(ascend_patch_file).read_text(encoding="utf-8", errors="replace")
            patch_content_snippet = full[:5000]
            if len(full) > 5000:
                patch_content_snippet += f"\n\n... ({len(full) - 5000} more bytes)"
        except Exception:
            pass

    log.key_value(
        "Existing patch", str(ascend_patch_file) if ascend_patch_file else "(none)"
    )
    log.key_value("Target LLVM", target_llvm_hash[:12])
    log.key_value("Test error logs", str(len(error_log_paths)))
    if focused_report_path:
        log.key_value("Focused changes", str(focused_report_path))
    if diagnosis_path:
        log.key_value("Diagnosis", str(diagnosis_path))

    run_opencode_adapter(
        _ir_ai_base(
            ctx,
            config,
            step_id,
            ir_dir,
            "ir_generate_patch",
            error_logs=json.dumps(error_log_paths, ensure_ascii=False),
            extra={
                "previous_step_id": "ir-diagnose",
                "previous_step_summary_path": str(diagnosis_path or ""),
                "focused_changes_path": str(focused_report_path or ""),
                "target_llvm_hash": target_llvm_hash,
                "baseline_llvm_hash": _ASCEND_BASELINE_LLVM_HASH,
                "ascend_patch_file": ascend_patch_file,
                "patch_content_snippet": patch_content_snippet,
                "adjust_mode": "supplement",
                "supplement_iteration": str(supplement_iter),
                "ascend_npu_ir_compat_ref": str(
                    Path(__file__).parent.parent
                    / "reference"
                    / "AscendNPU-IR_LLVM_VERSION_COMPAT.md"
                ),
            },
        )
    )


def _collect_test_error_logs() -> list[str]:
    """Collect actual test failure log files for AI analysis.

    Gathers all per-suite JUnit XMLs, result JSONs, and custom test
    output logs so the AI has the full failure picture across all
    sequentially-run test suites.
    """
    error_log_paths: list[str] = []
    test_log_dir = WORKSPACE_DIR / "test-logs"
    if test_log_dir.exists():
        # JUnit XML per suite (pytest-junit-primary.xml, pytest-junit-extra-*.xml)
        for f in sorted(test_log_dir.glob("pytest-junit-*.xml")):
            error_log_paths.append(str(f))
        # Result JSON per suite (test-result-primary.json, etc.)
        for f in sorted(test_log_dir.glob("test-result-*.json")):
            error_log_paths.append(str(f))
        # Custom test command output
        for f in sorted(test_log_dir.glob("*.log")):
            error_log_paths.append(str(f))
    # Legacy single-result file (backward compat)
    test_result = WORKSPACE_DIR / TEST_RESULT_FILE
    if test_result.exists():
        error_log_paths.append(str(test_result))
    return error_log_paths


def _classify_test_failures(
    ctx: WorkflowContext,
    config: TAConfig,
    step: dict,
) -> bool:
    """AI classifies test failures: IR compatibility vs code issues.

    Collects actual test log files, invokes AI diagnosis, and writes
    the result to ``IR_DIAGNOSIS_FILE`` so downstream supplement
    steps can chain from it.

    Returns True if IR issues are present (needs supplement),
    False if purely code issues (needs AI fix).
    """
    step_id = step["id"]
    ir_dir = WORKSPACE_DIR / STEPS_DIR / step_id / IR_ANALYSIS_DIR
    ir_dir.mkdir(parents=True, exist_ok=True)

    # Collect actual test failure logs (not just fix_errors paths)
    error_log_paths = _collect_test_error_logs()
    if not error_log_paths:
        log.warning("No test failure logs found — assuming IR issues")
        return True

    log.info(f"Collected {len(error_log_paths)} log file(s) for AI diagnosis")
    for p in error_log_paths[:5]:
        log.info(f"  - {p}")
    if len(error_log_paths) > 5:
        log.info(f"  ... and {len(error_log_paths) - 5} more")

    try:
        result = run_opencode_adapter(
            _ir_ai_base(
                ctx,
                config,
                step_id,
                ir_dir,
                "ir_diagnose",
                error_logs=json.dumps(error_log_paths, ensure_ascii=False),
            )
        )
    except Exception as e:
        log.error(f"IR diagnosis failed: {e}")
        return True  # Default to IR issue on failure

    # Write diagnosis result for chaining
    diagnosis_path = ir_dir / IR_DIAGNOSIS_FILE
    summary = result.step_summary or ""
    try:
        diagnosis_data = json.loads(summary)
    except json.JSONDecodeError:
        diagnosis_data = {
            "summary": summary,
            "has_ir_issues": "ir_issue" in summary.lower(),
        }
    diagnosis_path.write_text(
        json.dumps(diagnosis_data, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    log.info(f"IR diagnosis written: {diagnosis_path}")

    has_ir = diagnosis_data.get("has_ir_issues", False) or "ir_issue" in summary.lower()
    return bool(has_ir)


def _do_ai_fix_loop(
    ctx: WorkflowContext,
    config: TAConfig,
    step: dict,
) -> WorkflowContext:
    """Standard AI fix loop for code issues (not IR-related)."""
    return test_and_fix_loop(ctx, config)


# ═══════════════════════════════════════════════════════════════════════════
# LLVM workspace management
# ═══════════════════════════════════════════════════════════════════════════


def _ensure_llvm_workspace_clean(llvm_project: Path, reason: str = "") -> None:
    """Clean the LLVM working tree: stash changes, checkout HEAD, clean.

    Idempotent — safe to call multiple times.
    """
    if not llvm_project.exists():
        return

    log.info(f"Cleaning LLVM workspace{f' ({reason})' if reason else ''}...")
    try:
        # Abort any in-progress merge
        if (llvm_project / ".git" / "MERGE_HEAD").exists():
            run_git(llvm_project, "merge", "--abort")
    except Exception:
        pass

    try:
        # Stash any local changes
        run_git(llvm_project, "stash", "--include-untracked")
        run_git(llvm_project, "stash", "drop")
    except Exception:
        # If stash fails (no changes), just reset
        try:
            run_git(llvm_project, "checkout", "--", ".")
            run_git(llvm_project, "clean", "-fd")
        except Exception:
            pass


def _ensure_commit_available(llvm_project: Path, commit_hash: str) -> None:
    """Ensure *commit_hash* is available in the local LLVM repo.

    Fetches from origin with retries if the commit is missing.
    """
    max_attempts = 6
    for attempt in range(1, max_attempts + 1):
        result = run_git_no_check(llvm_project, "cat-file", "-t", commit_hash)
        if result.returncode == 0:
            return
        if attempt < max_attempts:
            log.info(
                f"Commit {commit_hash[:12]} not found locally — fetching "
                f"(attempt {attempt}/{max_attempts})..."
            )
            try:
                run_git(llvm_project, "fetch", "origin", commit_hash)
            except Exception:
                time.sleep(2)
    raise RuntimeError(
        f"Failed to fetch LLVM commit {commit_hash[:12]} after {max_attempts} attempts"
    )


def _clean_checkout_apply_patch(
    llvm_project: Path,
    ascend_path: Path,
    target_llvm_hash: str,
    reason: str = "",
) -> bool:
    """Clean workspace, checkout target hash, apply the current Ascend patch.

    Returns True if a patch file was found and applied, False if no patch
    exists (clean LLVM, no Ascend modifications).
    """
    _ensure_llvm_workspace_clean(llvm_project, reason or "prepare for patch apply")
    _ensure_commit_available(llvm_project, target_llvm_hash)
    run_git(llvm_project, "checkout", "-f", target_llvm_hash)
    run_git(llvm_project, "clean", "-fd")

    patch_dir = ascend_path / "third_party/ascend/patch"
    patch_files = sorted(patch_dir.glob("*.patch")) if patch_dir.exists() else []
    if not patch_files:
        log.info("No Ascend patch found — building clean LLVM")
        return False

    patch_file = patch_files[0]
    log.info(f"Applying patch: {patch_file.name}")
    run_git(llvm_project, "apply", str(patch_file))
    log.status(True, f"Patch applied: {patch_file.name}")
    return True


def _get_current_llvm_hash(ascend_path: Path) -> str:
    """Read the current LLVM hash from triton-ascend's cmake/llvm-hash.txt."""
    hash_file = ascend_path / "cmake" / "llvm-hash.txt"
    if hash_file.exists():
        return hash_file.read_text(encoding="utf-8").strip()
    return ""


def _do_llvm_build(llvm_project: Path, llvm_install: Path, required_hash: str) -> str:
    """Build and install LLVM from current working tree state.

    Cleans build directory, runs cmake + ninja install, copies FileCheck.

    Returns the LLVM install prefix path.

    Raises RuntimeError on build failure.
    """
    build_dir = WORKSPACE_DIR / "llvm-build"
    if build_dir.exists():
        shutil.rmtree(build_dir)
    build_dir.mkdir(parents=True, exist_ok=True)

    log_dir = WORKSPACE_DIR / "llvm-logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    log.info(f"Building LLVM (hash: {required_hash[:12]})...")
    log.key_value("LLVM project", str(llvm_project))
    log.key_value("LLVM install prefix", str(llvm_install))
    log.key_value("Build log", str(log_dir / "llvm-build.log"))

    # ── cmake configure ──────────────────────────────────────────────
    cmake_cmd = [
        "cmake",
        str(llvm_project / "llvm"),
        "-G",
        "Ninja",
        "-DCMAKE_BUILD_TYPE=Release",
        "-DLLVM_ENABLE_ASSERTIONS=ON",
        "-DLLVM_ENABLE_PROJECTS=mlir;llvm;lld",
        "-DLLVM_TARGETS_TO_BUILD=host;NVPTX;AMDGPU",
        f"-DCMAKE_INSTALL_PREFIX={llvm_install}",
        "-DCMAKE_C_COMPILER=clang",
        "-DCMAKE_CXX_COMPILER=clang++",
    ]
    llvm_build_log = log_dir / "llvm-build.log"
    with open(llvm_build_log, "w", encoding="utf-8") as fh:
        fh.write(f"=== cmake ===\n{' '.join(cmake_cmd)}\n\n")
        fh.flush()
        rc = stream_cmd(
            cmake_cmd, build_dir, fh, timeout=300, label="Configuring LLVM with cmake"
        )
    if rc != 0:
        raise RuntimeError(
            f"LLVM cmake configure failed (exit {rc}). See: {llvm_build_log}"
        )
    log.status(True, "cmake configure OK")

    # ── ninja build + install ───────────────────────────────────────
    log.info("ninja install (this may take a while)...")
    with open(llvm_build_log, "a", encoding="utf-8") as fh:
        fh.write("\n=== ninja install ===\n")
        fh.flush()
        rc = stream_cmd(
            ["ninja", "install"], build_dir, fh, timeout=7200, label="ninja install"
        )
    if rc != 0:
        raise RuntimeError(
            f"LLVM ninja build failed (exit {rc}). See: {llvm_build_log}"
        )
    log.status(True, "ninja install OK")

    # Copy FileCheck
    fc_src = build_dir / "bin" / "FileCheck"
    fc_dst = llvm_install / "bin" / "FileCheck"
    if fc_src.exists():
        fc_dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(fc_src, fc_dst)

    # Write hash cache
    llvm_install.mkdir(parents=True, exist_ok=True)
    (llvm_install / ".llvm_hash").write_text(required_hash, encoding="utf-8")

    log.status(True, f"LLVM build complete — {llvm_install}")
    return str(llvm_install)


def _detect_ascend_npu_ir_errors(ctx: WorkflowContext) -> bool:
    """Check if build errors are from AscendNPU-IR compilation failures."""
    step = ctx.steps[ctx.current_step] if ctx.steps else {"id": "step-0"}
    step_dir = WORKSPACE_DIR / STEPS_DIR / step["id"]
    build_log = step_dir / "build.log"
    if not build_log.exists():
        return False
    try:
        content = build_log.read_text(encoding="utf-8", errors="replace").lower()
        indicators = [
            "AscendNPU-IR".lower(),
            "ascendnpu-ir",
            "llvm::",
            "mlir::",
            "fatal error",
            "undefined reference",
        ]
        return any(ind in content for ind in indicators)
    except Exception:
        return False
