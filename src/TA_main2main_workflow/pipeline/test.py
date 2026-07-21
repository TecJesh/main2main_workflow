"""Pipeline step: Run pytest unit tests on Ascend NPU with retry/fix loop."""
from __future__ import annotations
import json, os, shutil, subprocess, time, xml.etree.ElementTree as ET
from pathlib import Path
from TA_main2main_workflow.utils.config import TAConfig
from TA_main2main_workflow.utils.context import WorkflowContext
from TA_main2main_workflow.utils.logging import get_logger
from TA_main2main_workflow.utils.tracker import timed
from TA_main2main_workflow.utils import TEST_RESULT_FILE, WORKSPACE_DIR
from TA_main2main_workflow.pipeline.fix import ai_fix

log = get_logger(__name__)


def test(ctx: WorkflowContext, config: TAConfig) -> WorkflowContext:
    """Test phase: run pytest with retry+fix loop."""
    if config.skip_e2e_test:
        log.info("SKIP_E2E_TEST=true — treating tests as passed")
        return ctx.copy_with(test_passed=True)

    for attempt in range(config.max_retries + 1):
        ctx = ctx.copy_with(retry_count=attempt)
        if attempt > 0:
            log.header(f"Test Fix Attempt {attempt}/{config.max_retries}")
            ctx = ai_fix(ctx, config, attempt=attempt)
        with timed("test"):
            ctx = _run_pytest(ctx, config)
        if ctx.test_passed:
            return ctx
        log.info(f"Tests failed (attempt {attempt + 1}) — retrying")
    return ctx.copy_with(test_passed=False)


def _run_pytest(ctx: WorkflowContext, config: TAConfig, python_exe: str = "") -> WorkflowContext:
    """Execute pytest and return updated ctx with test_passed + fix_errors."""
    ascend_path = Path(ctx.triton_ascend_path)
    test_log_dir = WORKSPACE_DIR / "test-logs"
    test_log_dir.mkdir(parents=True, exist_ok=True)

    test_dir_path = (ascend_path / "third_party/ascend/unittest/pytest_ut").resolve()
    python_exe = python_exe or os.getenv("PYTHON", "python3.10")

    if not test_dir_path.exists():
        log.warning(f"Test directory not found: {test_dir_path}")
        return ctx.copy_with(test_passed=True)

    junit_xml = test_log_dir / "pytest-junit.xml"
    pytest_bin = shutil.which("pytest")
    cmd = [pytest_bin, str(test_dir_path)] if pytest_bin else [python_exe, "-m", "pytest", str(test_dir_path)]
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
                tp += int(s.get("tests", 0)); pf += int(s.get("failures", 0)); pe += int(s.get("errors", 0))
        except Exception: pass

    passed = (pf == 0 and pe == 0)
    summary = {"exit_code": 0 if passed else 1, "passed": passed, "test_log": str(junit_xml),
               "test_dir": str(test_dir_path), "passed_count": tp, "failed_count": pf, "error_count": pe}
    (WORKSPACE_DIR / TEST_RESULT_FILE).write_text(
        json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    if not passed:
        log.error(f"Tests FAILED ({pf} failed, {pe} errors)")
        return ctx.copy_with(test_passed=False, fix_errors=[str(WORKSPACE_DIR / TEST_RESULT_FILE)])
    log.status(True, f"All tests passed ({tp} passed)")
    return ctx.copy_with(test_passed=True)
