#!/usr/bin/env python3
"""Build Triton-Ascend and run tests.

Build steps:
  1. Build C++ extensions (CMake / setup.py build)
  2. Install Python package in development mode
  3. Run pre-commit checks (optional)
  4. Run pytest unit tests

Output:
  - workspace/build_result.json
  - workspace/test_result.json
  - workspace/build.log
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from TA_main2main_workflow.utils import (
    WORKSPACE_DIR, BUILD_RESULT_FILE, BUILD_LOG_FILE, TEST_RESULT_FILE,
)


def _run_to_log(cmd: list[str], cwd: Path, log_path: Path,
                env: dict | None = None, timeout: int = 3600) -> subprocess.CompletedProcess:
    """Run a command, tee output to log file, return CompletedProcess."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    proc_env = os.environ.copy()
    if env:
        proc_env.update(env)
    with log_path.open("w", encoding="utf-8") as fh:
        proc = subprocess.Popen(
            cmd, cwd=cwd, env=proc_env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
        assert proc.stdout is not None
        for line in proc.stdout:
            fh.write(line)
            print(line, end="", flush=True)
        proc.wait(timeout=timeout)
    return subprocess.CompletedProcess(
        cmd, proc.returncode,
        stdout="", stderr=f"See {log_path}"
    )


def build_triton_ascend(
    repo_path: Path,
    llvm_prefix: str = "",
    conda_env: str = "",
    build_dir: str = "build",
    clean_build: bool = False,
) -> dict:
    """Build the Triton-Ascend C++ extensions and Python package."""
    print("\n=== Building Triton-Ascend ===")
    build_log = WORKSPACE_DIR / BUILD_LOG_FILE

    env = {}
    if llvm_prefix:
        env["LLVM_BUILD_DIR"] = llvm_prefix
        env["LLVM_INSTALL_PREFIX"] = llvm_prefix

    steps: list[dict] = []
    all_passed = True

    if clean_build:
        build_dir_path = repo_path / build_dir
        if build_dir_path.exists():
            print(f"  Cleaning build directory: {build_dir_path}")
            subprocess.run(["rm", "-rf", str(build_dir_path)], check=False)
        steps.append({"step": "clean", "passed": True})

    print("  Building C++ extensions...")

    # --- Build via setup.py (retained for reference) ---
    # build_cmd = [
    #     sys.executable, "-m", "pip", "install", "-e", ".",
    #     "--no-build-isolation",
    # ]

    build_env = env.copy()
    build_env.update({
        "LLVM_SYSPATH": llvm_prefix,
        "TRITON_BUILD_WITH_CCACHE": "true",
        "TRITON_BUILD_WITH_CLANG_LLD": "true",
        "TRITON_BUILD_PROTON": "OFF",
        "DEBUG": "1",
        "TRITON_WHEEL_NAME": "triton-ascend",
        "TRITON_APPEND_CMAKE_ARGS": "-DTRITON_BUILD_UT=OFF",
    })
    build_cmd = ["python3", "setup.py", "install"]
    build_proc = _run_to_log(build_cmd, repo_path, build_log, env=build_env, timeout=1800)
    build_passed = build_proc.returncode == 0
    steps.append({
        "step": "setup_py_install",
        "passed": build_passed,
        "exit_code": build_proc.returncode,
        "log": str(build_log),
    })
    if not build_passed:
        all_passed = False
        print("  Build FAILED!")
    else:
        # Clear triton cache after a successful build
        cache_dir = Path.home() / ".triton" / "cache"
        if cache_dir.exists():
            print(f"  Clearing triton cache: {cache_dir}")
            subprocess.run(["rm", "-rf", str(cache_dir)], check=False)
            steps.append({"step": "clear_cache", "passed": True})

    result = {
        "all_passed": all_passed,
        "steps": steps,
        "build_log": str(build_log),
    }
    (WORKSPACE_DIR / BUILD_RESULT_FILE).write_text(
        json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return result


def run_tests(
    repo_path: Path,
    test_dir: str = "third_party/ascend/unittest/pytest_ut",
    num_procs: int = 16,
    conda_env: str = "",
    timeout: int = 3600,
) -> dict:
    """Run pytest unit tests and return structured results."""
    print("\n=== Running Tests ===")
    test_log_dir = WORKSPACE_DIR / "test-logs"
    test_log_dir.mkdir(parents=True, exist_ok=True)

    test_log = test_log_dir / "pytest.log"
    test_dir_path = repo_path / test_dir

    env = {}
    if conda_env:
        env["CONDA_DEFAULT_ENV"] = conda_env

    pytest_cmd = [
        sys.executable, "-m", "pytest",
        str(test_dir_path),
        "-n", str(num_procs),
        "--tb=short",
        "-q",
        #"--timeout=600",
    ]

    print(f"  Running: {' '.join(pytest_cmd)}")
    proc = _run_to_log(pytest_cmd, repo_path, test_log, env=env, timeout=timeout)

    passed = proc.returncode == 0
    summary = {
        "exit_code": proc.returncode,
        "passed": passed,
        "test_log": str(test_log),
        "test_dir": str(test_dir_path),
    }

    if test_log.exists():
        log_text = test_log.read_text(encoding="utf-8", errors="replace")
        import re
        match = re.search(r'(\d+)\s+passed', log_text)
        if match:
            summary["passed_count"] = int(match.group(1))
        match = re.search(r'(\d+)\s+failed', log_text)
        if match:
            summary["failed_count"] = int(match.group(1))
        match = re.search(r'(\d+)\s+error', log_text)
        if match:
            summary["error_count"] = int(match.group(1))

    precommit_config = repo_path / ".pre-commit-config.yaml"
    if precommit_config.exists():
        print("\n  Running pre-commit checks...")
        precommit_log = test_log_dir / "precommit.log"
        try:
            subprocess.run(
                ["pre-commit", "run", "--from-ref", "origin/main", "--to-ref", "HEAD"],
                cwd=repo_path,
                stdout=precommit_log.open("w"),
                stderr=subprocess.STDOUT,
                timeout=300,
            )
        except (subprocess.TimeoutExpired, FileNotFoundError):
            pass

    result_path = WORKSPACE_DIR / TEST_RESULT_FILE
    result_path.write_text(
        json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )

    return summary
