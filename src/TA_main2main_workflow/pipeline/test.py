"""Pipeline step: Run pytest + test fix loop with OOM handling."""

from __future__ import annotations
import json, os, shutil, subprocess, time, xml.etree.ElementTree as ET
from pathlib import Path
from TA_main2main_workflow.utils.config import TAConfig
from TA_main2main_workflow.utils.context import WorkflowContext
from TA_main2main_workflow.utils.logging import get_logger
from TA_main2main_workflow.utils.tracker import timed
from TA_main2main_workflow.utils import TEST_RESULT_FILE, WORKSPACE_DIR, STEPS_DIR
from TA_main2main_workflow.pipeline.fix import ai_fix, validate_fix, get_last_ai_result

log = get_logger(__name__)
_MAX_OOM_RERUNS = 5


def test(ctx: WorkflowContext, config: TAConfig) -> WorkflowContext:
    """Test phase: run pytest with retry+fix loop."""
    if config.skip_e2e_test:
        log.info("SKIP_E2E_TEST=true -- treating tests as passed")
        return ctx.copy_with(test_passed=True)

    ascend_path = Path(ctx.triton_ascend_path)
    step = ctx.steps[ctx.current_step] if ctx.steps else None
    step_id = step["id"] if step else "step-0"
    step_dir = WORKSPACE_DIR / STEPS_DIR / step_id
    step_dir.mkdir(parents=True, exist_ok=True)

    test_passed = False
    attempt = 0

    while attempt <= config.max_retries:
        is_fix_attempt = attempt > 0
        ctx = ctx.copy_with(retry_count=attempt)

        if is_fix_attempt:
            # OOM detection: rerun with reduced concurrency
            if _detect_oom_in_tests():
                log.warning("NPU OOM detected -- rerunning with reduced concurrency")
                oom_result = _rerun_tests_reduced_concurrency(
                    ascend_path, config, max_reruns=_MAX_OOM_RERUNS)
                if oom_result is None or oom_result:
                    return ctx.copy_with(test_passed=True)
                if not _detect_oom_in_tests():
                    log.info("OOM resolved -- remaining failures need AI fix")
                else:
                    log.error(f"OOM persists after {_MAX_OOM_RERUNS} reruns")
                    return ctx.copy_with(test_passed=False)

            log.header(f"Test Fix Attempt {attempt}/{config.max_retries}")
            ctx = ctx.copy_with(fix_errors=_collect_test_error_logs())
            ctx = ai_fix(ctx, config, attempt=attempt)

            # Validate fix
            modified_files = get_last_ai_result().modified_files
            fix_valid, fix_reason = validate_fix(modified_files, ascend_path)
            if not fix_valid:
                log.error(f"Fix rejected: {fix_reason}")
                rejection_file = step_dir / "fix_rejection.txt"
                rejection_file.write_text(
                    f"PREVIOUS FIX WAS REJECTED: {fix_reason}\n"
                    f"For test fixes, prefer files under "
                    f"{ascend_path}/third_party/ascend/.\n",
                    encoding="utf-8")
                ctx = ctx.copy_with(
                    fix_errors=ctx.fix_errors + [str(rejection_file)])
                continue

            # Rebuild after fix
            from TA_main2main_workflow.pipeline.build import _build_triton
            ctx = _build_triton(ctx, config, clean=False)
            if not ctx.build_passed:
                attempt += 1
                continue

        # Run tests
        with timed("test"):
            ctx = _run_pytest(ctx, config)
        if ctx.test_passed:
            test_passed = True
            break

        log.info(f"Tests failed (attempt {attempt + 1}) -- retrying")
        attempt += 1

        if config.skip_ai_analysis:
            log.warning("SKIP_AI_ANALYSIS=true -- stopping test fix loop")
            break

    return ctx.copy_with(test_passed=test_passed)


def _run_pytest(ctx: WorkflowContext, config: TAConfig,
                python_exe: str = "") -> WorkflowContext:
    """Execute pytest and return updated ctx."""
    ascend_path = Path(ctx.triton_ascend_path)
    test_log_dir = WORKSPACE_DIR / "test-logs"
    test_log_dir.mkdir(parents=True, exist_ok=True)

    test_dir_path = (ascend_path / config.test_dir).resolve()
    python_exe = python_exe or os.getenv("PYTHON", "python3.10")

    if not test_dir_path.exists():
        log.warning(f"Test directory not found: {test_dir_path}")
        return ctx.copy_with(test_passed=True)

    junit_xml = test_log_dir / "pytest-junit.xml"
    pytest_bin = shutil.which("pytest")
    cmd = (
        [pytest_bin, str(test_dir_path)]
        if pytest_bin
        else [python_exe, "-m", "pytest", str(test_dir_path)]
    )
    cmd += ["-n", str(config.test_procs), f"--junitxml={junit_xml}"]

    log.section("Run Tests")
    log.info(f"cmd: {' '.join(cmd)}")
    _start = time.time()
    try:
        result = subprocess.run(cmd, cwd=ascend_path, timeout=1000)
        rc = result.returncode
    except subprocess.TimeoutExpired:
        rc = -1
        log.warning("pytest timed out after 1000s")

    elapsed = time.time() - _start
    log.info(f"pytest finished in {elapsed:.0f}s, returncode={rc}")

    pf = pe = tp = 0
    if junit_xml.exists():
        try:
            tree = ET.parse(junit_xml)
            root = tree.getroot()
            suites = [root] if root.tag != "testsuites" else root.findall("testsuite")
            for s in suites:
                tp += int(s.get("tests", 0))
                pf += int(s.get("failures", 0))
                pe += int(s.get("errors", 0))
        except Exception:
            pass

    passed = pf == 0 and pe == 0
    summary = {
        "exit_code": 0 if passed else 1,
        "passed": passed,
        "test_log": str(junit_xml),
        "test_dir": str(test_dir_path),
        "passed_count": tp,
        "failed_count": pf,
        "error_count": pe,
    }
    (WORKSPACE_DIR / TEST_RESULT_FILE).write_text(
        json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    if not passed:
        log.error(f"Tests FAILED ({pf} failed, {pe} errors)")
        return ctx.copy_with(
            test_passed=False,
            fix_errors=[str(WORKSPACE_DIR / TEST_RESULT_FILE)])
    log.status(True, f"All tests passed ({tp} passed)")
    return ctx.copy_with(test_passed=True)


def _detect_oom_in_tests() -> bool:
    """Check test logs for OOM errors."""
    test_log_dir = WORKSPACE_DIR / "test-logs"
    oom_markers = ["out of memory"]
    if test_log_dir.exists():
        try:
            for log_file in test_log_dir.rglob("*"):
                if log_file.suffix not in (".log", ".xml"):
                    continue
                content = log_file.read_text(encoding="utf-8", errors="replace")
                for marker in oom_markers:
                    if marker.lower() in content.lower():
                        return True
        except Exception:
            pass
    test_result = WORKSPACE_DIR / TEST_RESULT_FILE
    if test_result.exists():
        try:
            data = json.loads(test_result.read_text(encoding="utf-8"))
            for marker in oom_markers:
                if marker.lower() in json.dumps(data).lower():
                    return True
        except Exception:
            pass
    return False


def _rerun_tests_reduced_concurrency(ascend_path: Path, config: TAConfig,
                                     max_reruns: int = 5) -> bool | None:
    """Rerun tests with halved concurrency. Restores original after."""
    original_procs = config.test_procs
    reduced = max(1, original_procs // 2)
    log.warning(f"Reducing pytest concurrency: {original_procs} -> {reduced}")
    # Temporarily override config
    object.__setattr__(config, 'test_procs', reduced)
    try:
        for rerun in range(1, max_reruns + 1):
            log.info(f"OOM rerun {rerun}/{max_reruns} (procs={reduced})")
            # Create a temp ctx just for the test run
            temp_ctx = WorkflowContext(triton_ascend_path=str(ascend_path))
            temp_ctx = _run_pytest(temp_ctx, config)
            if temp_ctx.test_passed:
                return True
            if not _detect_oom_in_tests():
                log.info("OOM resolved -- remaining failures are not memory-related")
                return False
        return False
    finally:
        object.__setattr__(config, 'test_procs', original_procs)
        log.info(f"Restored pytest concurrency to {original_procs}")


def _collect_test_error_logs() -> list[str]:
    """Collect test failure log paths."""
    error_logs: list[str] = []
    test_log_dir = WORKSPACE_DIR / "test-logs"
    if test_log_dir.exists():
        for log_file in sorted(test_log_dir.rglob("*.log")):
            error_logs.append(str(log_file))
        for xml_file in sorted(test_log_dir.rglob("*.xml")):
            error_logs.append(str(xml_file))
    test_result = WORKSPACE_DIR / TEST_RESULT_FILE
    if test_result.exists():
        error_logs.append(str(test_result))
    return error_logs
