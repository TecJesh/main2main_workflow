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
            ctx = run_tests(ctx, config)

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


def run_tests(ctx: WorkflowContext, config: TAConfig,
              python_exe: str = "", test_procs: int = 0) -> WorkflowContext:
    """Execute tests: always runs default pytest ut first, then optionally
    appends a custom test command if ``TA_TEST_COMMAND`` is set.

    Tests pass only if ALL test suites pass.
    """
    # 1. Always run the default pytest ut
    ctx = _run_pytest(ctx, config, python_exe=python_exe, test_procs=test_procs)
    if not ctx.test_passed and not config.test_command:
        return ctx  # only pytest, already failed — no need to continue

    # 2. Optionally run additional custom test command
    if config.test_command:
        log.section("Additional Tests (TA_TEST_COMMAND)")
        custom_ctx = _run_custom_test(ctx, config)
        # Merge results: pass only if both suites pass
        all_passed = ctx.test_passed and custom_ctx.test_passed
        merged_errors = ctx.fix_errors + custom_ctx.fix_errors
        ctx = ctx.copy_with(
            test_passed=all_passed,
            fix_errors=merged_errors,
            test_log_dir=str(WORKSPACE_DIR / "test-logs"),
        )

    return ctx


def _run_custom_test(ctx: WorkflowContext, config: TAConfig) -> WorkflowContext:
    """Execute a user-supplied test command and capture results.

    The command is run via ``bash -c`` in the ascend repo root.  stdout
    and stderr are captured to ``test-logs/test-output.log``.  If the
    command produces a JUnit XML file (``--junitxml=...``), it is parsed
    for detailed pass/fail/counts.  Otherwise only the exit code is used.
    """
    ascend_path = Path(ctx.triton_ascend_path)
    test_log_dir = WORKSPACE_DIR / "test-logs"
    test_log_dir.mkdir(parents=True, exist_ok=True)

    cmd = config.test_command
    output_log = test_log_dir / "test-output.log"

    log.section("Run Tests (custom command)")
    log.key_value("command", cmd)
    log.key_value("output log", str(output_log))

    _start = time.time()
    with open(output_log, "w", encoding="utf-8") as fh:
        fh.write(f"=== TA_TEST_COMMAND ===\n{cmd}\n\n")
        fh.flush()
        proc = subprocess.Popen(
            ["bash", "-c", cmd],
            cwd=str(ascend_path),
            stdout=fh, stderr=subprocess.STDOUT,
        )
        try:
            rc = proc.wait(timeout=7200)
        except subprocess.TimeoutExpired:
            proc.kill()
            rc = -1
            log.warning("Test command timed out after 7200s")

    elapsed = time.time() - _start
    log.info(f"Test command finished in {elapsed:.0f}s, exit={rc}")

    # Try to parse JUnit XML if present (extract --junitxml=... from command)
    pf = pe = tp = 0
    junit_xml: Path | None = None
    import re as _re
    m = _re.search(r"--junitxml[= ](\S+)", cmd)
    if m:
        junit_xml = Path(m.group(1))
        if not junit_xml.is_absolute():
            junit_xml = ascend_path / junit_xml
        if junit_xml.exists():
            try:
                tree = ET.parse(str(junit_xml))
                root = tree.getroot()
                suites = [root] if root.tag != "testsuites" else root.findall("testsuite")
                for s in suites:
                    tp += int(s.get("tests", 0))
                    pf += int(s.get("failures", 0))
                    pe += int(s.get("errors", 0))
            except Exception:
                log.warning(f"Could not parse JUnit XML: {junit_xml}")

    passed = rc == 0 and pf == 0 and pe == 0
    summary = {
        "exit_code": rc,
        "passed": passed,
        "test_log": str(junit_xml or output_log),
        "test_command": cmd,
        "passed_count": tp,
        "failed_count": pf,
        "error_count": pe,
    }
    (WORKSPACE_DIR / TEST_RESULT_FILE).write_text(
        json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )

    if not passed:
        log.error(f"Tests FAILED (exit={rc}, {pf} failed, {pe} errors)")
        return ctx.copy_with(
            test_passed=False,
            fix_errors=[str(output_log), str(WORKSPACE_DIR / TEST_RESULT_FILE)],
            test_log_dir=str(test_log_dir),
        )
    log.status(True, f"All tests passed ({tp} passed)" if tp else f"Tests passed (exit 0)")
    return ctx.copy_with(test_passed=True, test_log_dir=str(test_log_dir))


# ---------------------------------------------------------------------------
# Default pytest runner (used when TA_TEST_COMMAND is not set)
# ---------------------------------------------------------------------------


def _run_pytest(ctx: WorkflowContext, config: TAConfig,
                python_exe: str = "", test_procs: int = 0) -> WorkflowContext:
    """Execute pytest across all configured test directories.

    Test directories are resolved from ``config.test_dirs`` (built from
    ``TA_TEST_DIR`` + ``TA_EXTRA_TEST_DIRS`` env vars).  All directories
    are passed to a single pytest invocation.
    """
    ascend_path = Path(ctx.triton_ascend_path)
    test_log_dir = WORKSPACE_DIR / "test-logs"
    test_log_dir.mkdir(parents=True, exist_ok=True)

    python_exe = python_exe or config.python_exe or os.getenv("PYTHON", "python3.10")
    procs = test_procs or config.test_procs

    # Resolve test directories, skipping missing ones
    test_paths: list[Path] = []
    for d in config.test_dirs:
        p = (ascend_path / d).resolve()
        if p.exists():
            test_paths.append(p)
        else:
            log.warning(f"Test directory not found, skipping: {p}")

    if not test_paths:
        log.warning("No test directories found — treating tests as passed")
        return ctx.copy_with(test_passed=True)

    junit_xml = test_log_dir / "pytest-junit.xml"
    pytest_bin = shutil.which("pytest")
    cmd = (
        [pytest_bin] if pytest_bin
        else [python_exe, "-m", "pytest"]
    )
    cmd += [str(p) for p in test_paths]
    cmd += ["-n", str(procs), f"--junitxml={junit_xml}"]

    log.section("Run Tests (pytest)")
    log.key_value("test dirs", ", ".join(str(p.relative_to(ascend_path)) for p in test_paths))
    log.info(f"cmd: {' '.join(cmd)}")
    _start = time.time()
    try:
        result = subprocess.run(cmd, cwd=ascend_path, timeout=3000)
        rc = result.returncode
    except subprocess.TimeoutExpired:
        rc = -1
        log.warning("pytest timed out after 3000s")

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
        "test_dirs": [str(p) for p in test_paths],
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
    # Scan JUnit XML
    junit_xml = test_log_dir / "pytest-junit.xml"
    if junit_xml.exists():
        try:
            content = junit_xml.read_text(encoding="utf-8", errors="replace").lower()
            for kw in oom_keywords:
                if kw.lower() in content:
                    log.warning(f"OOM indicator found in junitxml: '{kw}'")
                    return True
        except Exception:
            pass
    # Also scan custom test output log
    output_log = test_log_dir / "test-output.log"
    if output_log.exists():
        try:
            content = output_log.read_text(encoding="utf-8", errors="replace").lower()
            for kw in oom_keywords:
                if kw.lower() in content:
                    log.warning(f"OOM indicator found in test output: '{kw}'")
                    return True
        except Exception:
            pass
    return False


def rerun_tests_reduced_concurrency(
    ascend_path: Path, config: TAConfig, max_reruns: int = 5
) -> WorkflowContext | None:
    """Rerun pytest with progressively halved concurrency.

    Returns a new WorkflowContext with test results, or None if all fail.

    Stops early when OOM indicators disappear from test output — remaining
    failures are real code issues, not memory-related (matches pre-refactor).
    """
    original_procs = config.test_procs
    for r in range(max_reruns):
        procs = max(1, original_procs // (2 ** (r + 1)))
        if procs >= original_procs:
            break
        log.info(f"Rerunning tests with {procs} workers (attempt {r + 1}/{max_reruns})...")

        ascend_path_str = str(ascend_path)
        ctx = WorkflowContext(triton_ascend_path=ascend_path_str)
        ctx = run_tests(ctx, config, test_procs=procs)
        if ctx.test_passed:
            log.status(True, f"Tests passed with {procs} workers")
            return ctx
        # Stop if OOM is gone — remaining failures are code issues
        if not detect_oom_in_tests(ctx):
            log.info("OOM resolved — remaining failures are not memory-related, stopping rerun")
            return ctx

    log.error(f"Tests still failing after {max_reruns} concurrency reductions")
    return None
