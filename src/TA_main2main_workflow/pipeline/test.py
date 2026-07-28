"""Pipeline step: Run pytest unit tests with retry/fix loop.

Handles OOM detection and automatic concurrency reduction on retries.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import xml.etree.ElementTree as ET
from pathlib import Path

from TA_main2main_workflow.utils.config import TAConfig
from TA_main2main_workflow.utils.context import WorkflowContext
from TA_main2main_workflow.utils.logging import get_logger
from TA_main2main_workflow.utils.tracker import timed
from TA_main2main_workflow.utils import TEST_RESULT_FILE, WORKSPACE_DIR, STEPS_DIR
from TA_main2main_workflow.pipeline.build import build_triton
from TA_main2main_workflow.pipeline.fix import ai_fix

log = get_logger(__name__)


def test_and_fix_loop(ctx: WorkflowContext, config: TAConfig) -> WorkflowContext:
    """Test + AI fix loop with OOM detection and reduced concurrency retry.

    Used in single-step mode.  After each test failure:
    1. Check for OOM → rerun with halved concurrency (up to 5 retries)
    2. If still failing: AI fix → rebuild TA → retest
    """
    if config.skip_e2e_test:
        log.info("SKIP_E2E_TEST=true — treating tests as passed")
        return ctx.copy_with(test_passed=True, pytest_passed=True)

    ascend_path = Path(ctx.triton_ascend_path)

    attempt = 0
    while attempt <= config.max_retries:
        ctx = ctx.copy_with(retry_count=attempt)

        if attempt > 0:
            # ── OOM detection: check BEFORE AI fix ──────────────────
            if detect_oom_in_tests(ctx):
                log.warning("OOM detected in test output — reducing concurrency")
                oom_ctx = rerun_tests_reduced_concurrency(ascend_path, config)
                if oom_ctx is not None and oom_ctx.test_passed:
                    return ctx.copy_with(
                        test_passed=True, pytest_passed=True,
                        test_fix_count=ctx.test_fix_count,
                    )

            log.header(f"Test Fix Attempt {attempt}/{config.max_retries}")
            ctx = ai_fix(ctx, config, attempt=attempt, mode="fix")

            # Rebuild TA after AI fix (old behavior: rebuild before retest)
            with timed("test-fix-rebuild"):
                ctx = build_triton(ctx, config, clean=False)
            if not ctx.build_passed:
                log.error("Rebuild after AI test fix failed")
                continue

        # Run tests
        with timed("test"):
            ctx = run_pytest(ctx, config)

        if ctx.test_passed:
            return ctx.copy_with(
                test_passed=True, pytest_passed=True,
                test_fix_count=ctx.test_fix_count + (1 if attempt > 0 else 0),
            )

        log.info(f"Tests failed (attempt {attempt + 1}) — retrying")
        attempt += 1

    return ctx.copy_with(test_passed=False, pytest_passed=False)


# ═══════════════════════════════════════════════════════════════════════════
# Internal
# ═══════════════════════════════════════════════════════════════════════════


def run_pytest(ctx: WorkflowContext, config: TAConfig,
                python_exe: str = "", test_procs: int = 0) -> WorkflowContext:
    """Execute pytest and return updated ctx with test_passed + fix_errors."""
    ascend_path = Path(ctx.triton_ascend_path)
    test_log_dir = WORKSPACE_DIR / "test-logs"
    test_log_dir.mkdir(parents=True, exist_ok=True)

    test_dir_path = (ascend_path / config.test_dir).resolve()
    python_exe = python_exe or config.python_exe or os.getenv("PYTHON", "python3.10")
    procs = test_procs or config.test_procs

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
    cmd += ["-n", str(procs), f"--junitxml={junit_xml}"]

    log.section("Run Tests")
    log.info(f"cmd: {' '.join(cmd)}")
    _start = time.time()
    try:
        result = subprocess.run(cmd, cwd=ascend_path, timeout=3000)
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
        json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )

    if not passed:
        log.error(f"Tests FAILED ({pf} failed, {pe} errors)")
        return ctx.copy_with(
            test_passed=False,
            fix_errors=[str(WORKSPACE_DIR / TEST_RESULT_FILE)],
            test_log_dir=str(test_log_dir),
        )
    log.status(True, f"All tests passed ({tp} passed)")
    return ctx.copy_with(test_passed=True, test_log_dir=str(test_log_dir))


def detect_oom_in_tests(ctx: WorkflowContext) -> bool:
    """Check test output for Out-Of-Memory indicators."""
    test_log_dir = Path(ctx.test_log_dir) if ctx.test_log_dir else None
    if not test_log_dir or not test_log_dir.exists():
        return False

    oom_keywords = [
        "OutOfMemoryError", "out of memory", "MemoryError",
        "Cannot allocate memory", "OOM", "Killed",
        "Exit code 137", "exit code 137",
        "CUDA error", "cuMemAlloc", "NPU error",
    ]
    junit_xml = test_log_dir / "pytest-junit.xml"
    if junit_xml.exists():
        try:
            content = junit_xml.read_text(encoding="utf-8", errors="replace").lower()
            for kw in oom_keywords:
                if kw.lower() in content:
                    log.warning(f"OOM indicator found: '{kw}'")
                    return True
        except Exception:
            pass
    return False


def rerun_tests_reduced_concurrency(
    ascend_path: Path, config: TAConfig, max_reruns: int = 5
) -> WorkflowContext | None:
    """Rerun pytest with progressively halved concurrency.

    Returns a new WorkflowContext with test results, or None if all fail.
    """
    original_procs = config.test_procs
    for r in range(max_reruns):
        procs = max(1, original_procs // (2 ** (r + 1)))
        if procs >= original_procs:
            break
        log.info(f"Rerunning tests with {procs} workers (attempt {r + 1}/{max_reruns})...")

        ascend_path_str = str(ascend_path)
        ctx = WorkflowContext(triton_ascend_path=ascend_path_str)
        ctx = run_pytest(ctx, config, test_procs=procs)
        if ctx.test_passed:
            log.status(True, f"Tests passed with {procs} workers")
            return ctx

    log.error(f"Tests still failing after {max_reruns} concurrency reductions")
    return None
