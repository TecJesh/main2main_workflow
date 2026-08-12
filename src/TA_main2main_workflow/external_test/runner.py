"""External test runner — clone, install, test, fix loop per operator repo.

Executes configured external operator repository test suites sequentially,
one repo at a time.  Each repo follows::

    clone → install_deps → pytest → [fail? → AI fix → retest] → next repo

Results are written to ``workspace/test-logs/`` as JUnit XML + per-repo JSON
summaries, matching the conventions used by the main pytest_ut pipeline.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import xml.etree.ElementTree as ET
from pathlib import Path

from TA_main2main_workflow.external_test.config_loader import (
    ExternalTestConfig,
    ExternalTestRepoConfig,
)
from TA_main2main_workflow.agent.opencode_adapter import run_opencode_adapter
from TA_main2main_workflow.utils.config import TAConfig
from TA_main2main_workflow.utils.context import WorkflowContext
from TA_main2main_workflow.utils.git import run_git
from TA_main2main_workflow.utils.logging import get_logger
from TA_main2main_workflow.utils.tracker import timed
from TA_main2main_workflow.utils import WORKSPACE_DIR

log = get_logger(__name__)

# Top-level directory under WORKSPACE_DIR where external repos are cloned
_EXTERNAL_REPOS_DIR = "external_repos"


# ═══════════════════════════════════════════════════════════════════════════
# Public API
# ═══════════════════════════════════════════════════════════════════════════


def run_external_tests(
    ctx: WorkflowContext, config: TAConfig, external_cfg: ExternalTestConfig
) -> WorkflowContext:
    """Execute external operator repo tests sequentially.

    1. Ensure ``workspace/external_repos/`` exists
    2. For each repo in *external_cfg.repos* (in config order):
        a. ``clone_or_update_repo``
        b. ``install_dependencies``
        c. ``run_repo_tests`` — pytest with configured test cases
        d. On failure: ``_external_test_fix_loop`` — AI fix + retest
        e. Record result, continue to next repo
    3. Return updated WorkflowContext

    When *external_cfg.mode* is ``"standalone"``, failures are recorded but
    never propagated as workflow-fatal (caller decides).
    """
    repos_dir = WORKSPACE_DIR / _EXTERNAL_REPOS_DIR
    repos_dir.mkdir(parents=True, exist_ok=True)

    test_log_dir = WORKSPACE_DIR / "test-logs"
    test_log_dir.mkdir(parents=True, exist_ok=True)

    all_passed = True
    results: list[dict] = []

    for i, repo_cfg in enumerate(external_cfg.repos):
        log.section(f"External Repo {i + 1}/{len(external_cfg.repos)}: {repo_cfg.name}")
        repo_result = _process_one_repo(
            repo_cfg, repos_dir, test_log_dir, ctx, config, external_cfg
        )
        results.append(repo_result)
        if not repo_result.get("passed", False):
            all_passed = False

    # ── Write aggregate summary ───────────────────────────────────────────
    summary_file = test_log_dir / "external-test-summary.json"
    summary = {
        "all_passed": all_passed,
        "total_repos": len(external_cfg.repos),
        "passed_repos": sum(1 for r in results if r.get("passed")),
        "failed_repos": sum(1 for r in results if not r.get("passed")),
        "results": results,
    }
    summary_file.write_text(
        json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    log.info(f"External test summary: {summary_file}")

    return ctx.copy_with(
        external_test_passed=all_passed,
        external_test_results=results,
    )


# ═══════════════════════════════════════════════════════════════════════════
# Per-repo processing
# ═══════════════════════════════════════════════════════════════════════════


def _process_one_repo(
    repo_cfg: ExternalTestRepoConfig,
    repos_dir: Path,
    test_log_dir: Path,
    ctx: WorkflowContext,
    config: TAConfig,
    external_cfg: ExternalTestConfig,
) -> dict:
    """Run the full pipeline for a single external repo. Returns a result dict."""
    log.key_value("repo", repo_cfg.name)
    log.key_value("url", repo_cfg.url)
    log.key_value("branch", repo_cfg.branch)

    repo_path = repos_dir / repo_cfg.name
    result: dict = {
        "repo": repo_cfg.name,
        "passed": False,
        "failed_cases": [],
        "fix_count": 0,
    }

    # ── Step 1: Clone or update ───────────────────────────────────────
    with timed(f"clone-{repo_cfg.name}"):
        if not clone_or_update_repo(repo_cfg, repo_path):
            result["error"] = "clone/update failed"
            log.error(f"Failed to clone/update repo: {repo_cfg.name}")
            return result

    # ── Step 2: Install dependencies (skip if install_cmd is empty) ──
    if repo_cfg.install_cmd:
        with timed(f"install-{repo_cfg.name}"):
            if not install_dependencies(repo_path, repo_cfg):
                result["error"] = "dependency installation failed"
                log.error(f"Failed to install dependencies for: {repo_cfg.name}")
                return result
    else:
        log.info(f"No install_cmd configured for {repo_cfg.name} — skipping")

    # ── Step 3: Run tests + fix loop ──────────────────────────────────
    passed, fix_count, failed_cases = _run_repo_tests_with_fix(
        repo_path, repo_cfg, test_log_dir, ctx, config, external_cfg
    )

    result["passed"] = passed
    result["fix_count"] = fix_count
    result["failed_cases"] = failed_cases

    if passed:
        log.status(True, f"External repo {repo_cfg.name}: PASSED")
    else:
        log.status(False, f"External repo {repo_cfg.name}: FAILED")

    return result


# ═══════════════════════════════════════════════════════════════════════════
# Git operations
# ═══════════════════════════════════════════════════════════════════════════


def clone_or_update_repo(cfg: ExternalTestRepoConfig, repo_path: Path) -> bool:
    """Clone *cfg.url* into *repo_path*, or ``git pull`` if it already exists."""
    if repo_path.exists():
        log.info(f"Repo exists, pulling latest: {repo_path}")
        try:
            run_git(repo_path, "fetch", "origin")
            run_git(repo_path, "checkout", cfg.branch)
            run_git(repo_path, "pull", "origin", cfg.branch)
            log.info(f"Updated repo: {cfg.name}")
            return True
        except Exception as exc:
            log.warning(f"git pull failed for {cfg.name}: {exc}")
            return False
    else:
        log.info(f"Cloning {cfg.url} → {repo_path}")
        try:
            repo_path.parent.mkdir(parents=True, exist_ok=True)
            result = subprocess.run(
                ["git", "clone", "--branch", cfg.branch, cfg.url, str(repo_path)],
                capture_output=True,
                text=True,
                timeout=600,
            )
            if result.returncode != 0:
                log.error(f"Clone failed: {result.stderr.strip()}")
                return False
            log.info(f"Cloned repo: {cfg.name}")
            return True
        except subprocess.TimeoutExpired:
            log.error(f"Clone timed out: {cfg.url}")
            return False
        except Exception as exc:
            log.error(f"Clone failed for {cfg.name}: {exc}")
            return False


# ═══════════════════════════════════════════════════════════════════════════
# Dependency installation
# ═══════════════════════════════════════════════════════════════════════════


def install_dependencies(repo_path: Path, cfg: ExternalTestRepoConfig) -> bool:
    """Run *cfg.install_cmd* inside *repo_path* via ``bash -c``."""
    cmd = cfg.install_cmd
    log.info(f"Installing dependencies: {cmd}")

    try:
        result = subprocess.run(
            ["bash", "-c", cmd],
            cwd=str(repo_path),
            capture_output=True,
            text=True,
            timeout=1800,
        )
        if result.returncode != 0:
            log.error(
                f"Dependency install failed for {cfg.name}:\n{result.stderr[-2000:]}"
            )
            return False
        log.info(f"Dependencies installed: {cfg.name}")
        return True
    except subprocess.TimeoutExpired:
        log.error(f"Dependency install timed out: {cfg.name}")
        return False
    except Exception as exc:
        log.error(f"Dependency install error for {cfg.name}: {exc}")
        return False


# ═══════════════════════════════════════════════════════════════════════════
# Test execution + fix loop
# ═══════════════════════════════════════════════════════════════════════════


def _run_repo_tests_with_fix(
    repo_path: Path,
    repo_cfg: ExternalTestRepoConfig,
    test_log_dir: Path,
    ctx: WorkflowContext,
    config: TAConfig,
    external_cfg: ExternalTestConfig,
) -> tuple[bool, int, list[str]]:
    """Run tests for a single repo with optional AI fix loop on failure.

    Returns ``(passed, fix_count, failed_cases)``.
    """
    procs = external_cfg.test_procs
    max_retries = external_cfg.max_retries
    timeout = external_cfg.timeout

    for attempt in range(max_retries + 1):
        if attempt > 0:
            if config.skip_ai_analysis:
                log.info("SKIP_AI_ANALYSIS=true — cannot fix, aborting retries")
                return False, 0, _list_test_cases(repo_cfg)

            log.header(
                f"External Fix Attempt {attempt}/{max_retries} — {repo_cfg.name}"
            )
            _external_test_ai_fix(
                repo_path, repo_cfg, ctx, config, attempt, test_log_dir
            )

        # Run pytest
        passed, failed_cases = run_repo_tests(
            repo_path, repo_cfg, test_log_dir, procs, timeout
        )

        if passed:
            return True, attempt, []
        elif attempt == max_retries:
            log.error(
                f"External repo {repo_cfg.name} failed after {max_retries} fix attempts"
            )
            return False, attempt, failed_cases

        log.info(
            f"External test failed (attempt {attempt + 1}) — will retry with AI fix"
        )

    return False, max_retries, _list_test_cases(repo_cfg)


def run_repo_tests(
    repo_path: Path,
    repo_cfg: ExternalTestRepoConfig,
    test_log_dir: Path,
    procs: int = 8,
    timeout: int = 7200,
) -> tuple[bool, list[str]]:
    """Run pytest for the configured test files in *repo_cfg*.

    Returns ``(passed, failed_case_paths)``.
    """
    if not repo_cfg.test_cases:
        log.info(f"No test cases configured for {repo_cfg.name} — treating as passed")
        return True, []

    test_paths: list[Path] = []
    for tc in repo_cfg.test_cases:
        p = repo_path / tc
        if p.exists():
            test_paths.append(p)
        else:
            log.warning(f"Test file not found in {repo_cfg.name}: {tc}")

    if not test_paths:
        log.warning(f"No test files found for {repo_cfg.name} — treating as passed")
        return True, []

    junit_xml = test_log_dir / f"pytest-junit-external-{repo_cfg.name}.xml"
    output_log = test_log_dir / f"test-output-external-{repo_cfg.name}.log"
    pytest_bin = shutil.which("pytest")
    python_exe = os.getenv("PYTHON", "python3.10")
    cmd = [pytest_bin] if pytest_bin else [python_exe, "-m", "pytest"]
    cmd += [str(p.relative_to(repo_path)) for p in test_paths]
    cmd += ["-n", str(procs), f"--junitxml={junit_xml}"]

    log.key_value(
        f"[{repo_cfg.name}] test files",
        ", ".join(tc for tc in repo_cfg.test_cases),
    )
    log.info(f"[{repo_cfg.name}] cmd: {' '.join(cmd)}")
    log.info(f"[{repo_cfg.name}] output: {output_log}")

    _start = time.time()
    with open(output_log, "w", encoding="utf-8") as fh:
        fh.write(f"=== External Test: {repo_cfg.name} ===\n")
        fh.write(f"cmd: {' '.join(cmd)}\n\n")
        fh.flush()
        proc = subprocess.Popen(
            cmd,
            cwd=str(repo_path),
            stdout=fh,
            stderr=subprocess.STDOUT,
        )
        try:
            rc = proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            rc = -1
            log.warning(f"[{repo_cfg.name}] pytest timed out after {timeout}s")

    elapsed = time.time() - _start
    log.info(f"[{repo_cfg.name}] pytest finished in {elapsed:.0f}s, returncode={rc}")

    # Parse JUnit XML
    pf = pe = tp = 0
    failed_cases: list[str] = []
    if junit_xml.exists():
        try:
            tree = ET.parse(junit_xml)
            root_elem = tree.getroot()
            suites = (
                [root_elem]
                if root_elem.tag != "testsuites"
                else root_elem.findall("testsuite")
            )
            for s in suites:
                tp += int(s.get("tests", 0))
                pf += int(s.get("failures", 0))
                pe += int(s.get("errors", 0))
                for tc_elem in s.findall("testcase"):
                    failure = tc_elem.find("failure")
                    error = tc_elem.find("error")
                    if failure is not None or error is not None:
                        failed_cases.append(
                            f"{tc_elem.get('classname', '')}.{tc_elem.get('name', '')}"
                        )
        except Exception:
            log.warning(f"Could not parse JUnit XML: {junit_xml}")

    passed = pf == 0 and pe == 0

    # Write per-repo result file
    result_file = test_log_dir / f"test-result-external-{repo_cfg.name}.json"
    result_summary = {
        "repo": repo_cfg.name,
        "label": f"external-{repo_cfg.name}",
        "exit_code": 0 if passed else 1,
        "passed": passed,
        "test_log": str(junit_xml),
        "output_log": str(output_log),
        "test_cases": repo_cfg.test_cases,
        "passed_count": tp,
        "failed_count": pf,
        "error_count": pe,
        "failed_case_details": failed_cases,
    }
    result_file.write_text(
        json.dumps(result_summary, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    if not passed:
        log.error(f"[{repo_cfg.name}] Tests FAILED ({pf} failed, {pe} errors)")
    else:
        log.status(True, f"[{repo_cfg.name}] All tests passed ({tp} passed)")

    return passed, failed_cases


# ═══════════════════════════════════════════════════════════════════════════
# AI fix for external tests
# ═══════════════════════════════════════════════════════════════════════════


def _external_test_ai_fix(
    repo_path: Path,
    repo_cfg: ExternalTestRepoConfig,
    ctx: WorkflowContext,
    config: TAConfig,
    attempt: int,
    test_log_dir: Path,
) -> None:
    """Invoke AI to fix test failures in an external operator repository.

    Unlike the main pipeline's ``ai_fix``, this function does NOT gate on
    ``_ALLOWED_FIX_PREFIXES`` — the entire external repo is fair game for
    modifications.  Validation only checks that files exist within the repo.
    """
    if config.skip_ai_analysis:
        return

    # Gather error logs for AI context
    error_log_paths: list[str] = []
    for pattern in [
        f"test-output-external-{repo_cfg.name}.log",
        f"pytest-junit-external-{repo_cfg.name}.xml",
        f"test-result-external-{repo_cfg.name}.json",
    ]:
        p = test_log_dir / pattern
        if p.exists():
            error_log_paths.append(str(p))

    fix_dir = WORKSPACE_DIR / "fixes" / f"external-{repo_cfg.name}-fix-{attempt}"
    fix_dir.mkdir(parents=True, exist_ok=True)

    log.info(f"Invoking AI fix for external repo: {repo_cfg.name}")
    try:
        _ = _list_tracked_files(repo_path)

        result = run_opencode_adapter(
            {
                "step_id": f"external-{repo_cfg.name}-fix-{attempt}",
                "previous_step_id": "",
                "previous_step_summary_path": "",
                "is_last_step": "true",
                "step_dir": str(test_log_dir),
                "fix_dir": str(fix_dir),
                "conflict_dir": "",
                "ascend_path": str(repo_path),
                "triton_path": str(repo_path),
                "reference_dir": "",
                "mode": "external_test_fix",
                "error_logs": json.dumps(error_log_paths, ensure_ascii=False),
                "target_commit": "",
                "step_index": f"external/{repo_cfg.name}",
                "ascend_npu_ir_fix": "false",
                "ascend_npu_ir_compat_ref": "",
            }
        )

        # ── Validate: changes must be inside the external repo ─────────
        if result.modified_files:
            repo_path_str = str(repo_path)
            illegal = [
                f for f in result.modified_files if not _is_under_path(f, repo_path_str)
            ]
            if illegal:
                log.warning(f"AI fix touched files outside {repo_cfg.name}: {illegal}")
                rejection_file = fix_dir / "fix_rejection.txt"
                rejection_file.write_text(
                    f"VALIDATION REJECTED: files outside repo {repo_cfg.name}\n"
                    f"Illegal files: {illegal}\n",
                    encoding="utf-8",
                )
                _revert_changes(repo_path)

        log.ai_result(
            bool(result.modified_files),
            result.modified_files,
            (result.step_summary or "")[:500],
        )
    except Exception as exc:
        log.error(f"AI fix failed for external repo {repo_cfg.name}: {exc}")


def _is_under_path(file_path: str, parent: str) -> bool:
    """Check whether *file_path* resides under *parent* directory."""
    try:
        Path(file_path).resolve().relative_to(Path(parent).resolve())
        return True
    except ValueError:
        return False


def _list_tracked_files(repo: Path) -> set[str]:
    """Return the set of all tracked files in *repo*."""
    try:
        output = run_git(repo, "ls-files")
        return set(output.strip().splitlines())
    except Exception:
        return set()


def _revert_changes(repo: Path) -> None:
    """Revert all uncommitted changes and remove untracked files in *repo*."""
    try:
        run_git(repo, "checkout", "--", ".")
        run_git(repo, "clean", "-fd")
    except Exception as exc:
        log.error(f"Failed to revert changes in {repo}: {exc}")


def _list_test_cases(repo_cfg: ExternalTestRepoConfig) -> list[str]:
    """Return a copy of the configured test case paths."""
    return list(repo_cfg.test_cases)
