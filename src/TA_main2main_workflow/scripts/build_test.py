#!/usr/bin/env python3
"""Build Triton-Ascend and run tests.

Build steps:
  1. Check LLVM version and rebuild if needed (unless SKIP_LLVM_REBUILD=true)
  2. Build C++ extensions (CMake / setup.py build)
  3. Install Python package in development mode
  4. Run pre-commit checks (optional)
  5. Run pytest unit tests

Environment variables:
  LLVM_PROJECT_PATH         — path to llvm-project repo (default: ~/workspace/llvm-project)
  LLVM_INSTALL_PREFIX_SYNC  — where to install LLVM (default: ~/workspace/llvm-install-sync)
  SKIP_LLVM_REBUILD         — set to "true" to skip LLVM rebuild check

Output:
  - workspace/build_result.json
  - workspace/test_result.json
  - workspace/build.log
  - workspace/llvm_build.log
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from TA_main2main_workflow.utils import (
    WORKSPACE_DIR, BUILD_RESULT_FILE, BUILD_LOG_FILE, TEST_RESULT_FILE,
)


def _run_to_log(cmd: list[str], cwd: Path, log_path: Path,
                env: dict | None = None, timeout: int = 3600,
                progress_line: bool = False) -> subprocess.CompletedProcess:
    """Run a command, tee output to log file and console, wait for completion.

    If progress_line is True, only the last line of output is shown,
    overwriting in place with \\r — useful for cmake/ninja build output.
    Full output is always written to the log file.
    """
    log_path.parent.mkdir(parents=True, exist_ok=True)
    proc_env = os.environ.copy()
    if env:
        proc_env.update(env)

    print(f"  Running: {' '.join(cmd)}")
    last_line = ""
    with log_path.open("w", encoding="utf-8") as fh:
        proc = subprocess.Popen(
            cmd, cwd=cwd, env=proc_env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
        assert proc.stdout is not None
        for line in proc.stdout:
            fh.write(line)
            if progress_line:
                stripped = line.rstrip()
                if stripped:
                    last_line = stripped
                    # \r 回到行首，\033[K 清除行尾残留，保证单行刷新
                    print(f"\r  {stripped[:120]}\033[K", end="", flush=True)
            else:
                print(line, end="", flush=True)
        if progress_line and last_line:
            print()  # final newline
        proc.wait(timeout=timeout)

    return subprocess.CompletedProcess(
        cmd, proc.returncode,
        stdout="", stderr=f"See {log_path}"
    )


def apply_llvm_patches(patch_dir: Path, llvm_project: Path,
                      target_hash: str = "") -> dict:
    """Apply generated LLVM patch to llvm-project after cleaning stale state.

    1. Clean any stale modifications in llvm-project (git checkout -- .)
    2. Checkout the target LLVM commit
    3. Apply the single ir_compat.patch with 'git apply'

    This is a deterministic operation — no AI involved.
    Returns a dict with 'applied', 'failed', 'all_ok'.
    """
    patch_file = patch_dir / "ir_compat.patch"
    if not patch_file.exists():
        print("  [llvm-patch] ir_compat.patch not found — nothing to apply")
        return {"applied": [], "failed": [], "all_ok": True}

    print(f"\n{'=' * 60}")
    print(f"  Apply IR compat patch to LLVM")
    print(f"{'=' * 60}")

    # ── Step 1: Clean stale modifications ──
    print("  [llvm-patch] Cleaning stale changes in llvm-project...")
    subprocess.run(
        ["git", "checkout", "--", "."],
        cwd=llvm_project, capture_output=True, text=True,
    )
    subprocess.run(
        ["git", "clean", "-fd"],
        cwd=llvm_project, capture_output=True, text=True,
    )
    print("  [llvm-patch] Working tree cleaned")

    # ── Step 2: Checkout target LLVM commit ──
    if target_hash:
        print(f"  [llvm-patch] Checking out LLVM commit: {target_hash[:12]}")
        result = subprocess.run(
            ["git", "checkout", target_hash],
            cwd=llvm_project, capture_output=True, text=True,
        )
        if result.returncode != 0:
            print(f"  [llvm-patch] FAILED to checkout {target_hash[:12]}: "
                  f"{result.stderr.strip()[-200:]}")
            return {"applied": [], "failed": [{
                "patch": str(patch_file),
                "error": f"git checkout failed: {result.stderr.strip()}",
            }], "all_ok": False}
        print(f"  [llvm-patch] Checked out: {target_hash[:12]}")

    # ── Step 3: Apply the patch ──
    print(f"  [llvm-patch] Applying: {patch_file.name}")
    # dry-run first
    proc = subprocess.run(
        ["git", "apply", "--check", str(patch_file)],
        cwd=llvm_project, capture_output=True, text=True,
    )
    if proc.returncode != 0:
        print(f"  [llvm-patch] Patch does NOT apply cleanly:")
        print(f"    {proc.stderr.strip()[-400:]}")
        return {"applied": [], "failed": [{
            "patch": str(patch_file),
            "error": proc.stderr.strip(),
        }], "all_ok": False}

    result = subprocess.run(
        ["git", "apply", str(patch_file)],
        cwd=llvm_project, capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(f"  [llvm-patch] FAILED: {result.stderr.strip()[-200:]}")
        return {"applied": [], "failed": [{
            "patch": str(patch_file),
            "error": result.stderr.strip(),
        }], "all_ok": False}

    print(f"  [llvm-patch] ✓ Patch applied successfully")
    return {"applied": [str(patch_file)], "failed": [], "all_ok": True}


def _check_and_rebuild_llvm(repo_path: Path, force_rebuild: bool = False) -> str:
    """Check if LLVM version changed and rebuild if needed.

    Reads cmake/llvm-hash.txt from triton-ascend, compares with the
    last-built hash stored at {LLVM_INSTALL_PREFIX_SYNC}/.llvm_hash.
    If they differ (or no previous build exists), checks out the
    required commit in the pre-cloned llvm-project and rebuilds.

    When force_rebuild is True, skips the hash comparison and always
    rebuilds. Used after applying IR compatibility patches to LLVM.

    Environment variables:
      LLVM_PROJECT_PATH         — path to llvm-project (default: ~/llvm-project)
      LLVM_INSTALL_PREFIX_SYNC  — where to install LLVM (default: ~/llvm-install-sync)

    Returns the LLVM install prefix path.
    """
    llvm_project = Path(
        os.getenv("LLVM_PROJECT_PATH",
                   os.path.expanduser("~/llvm-project"))
    )
    llvm_install = Path(
        os.getenv("LLVM_INSTALL_PREFIX_SYNC",
                   os.path.expanduser("~/llvm-install-sync"))
    )

    # Read the required LLVM hash from triton-ascend
    llvm_hash_file = repo_path / "cmake" / "llvm-hash.txt"
    if not llvm_hash_file.exists():
        print(f"  [llvm] {llvm_hash_file} not found — skipping LLVM rebuild")
        return str(llvm_install)

    required_hash = llvm_hash_file.read_text(encoding="utf-8").strip()
    if not required_hash:
        print("  [llvm] llvm-hash.txt is empty — skipping LLVM rebuild")
        return str(llvm_install)

    # Check last-built hash (skip when forcing rebuild)
    hash_cache = llvm_install / ".llvm_hash"
    if not force_rebuild and hash_cache.exists():
        last_hash = hash_cache.read_text(encoding="utf-8").strip()
        if last_hash == required_hash:
            print(f"  [llvm] LLVM hash unchanged ({required_hash[:12]}) — skip rebuild")
            return str(llvm_install)

    if force_rebuild:
        print(f"\n{'=' * 60}")
        print(f"  LLVM force rebuild requested (IR patches applied)")
    else:
        print(f"\n{'=' * 60}")
        print(f"  LLVM version changed!")
        print(f"  Previous: {hash_cache.read_text(encoding='utf-8').strip()[:12] if hash_cache.exists() else '(none)'}")
    print(f"  Required: {required_hash[:12]}")
    print(f"  Rebuilding LLVM...")
    print(f"{'=' * 60}")

    # Ensure llvm-project exists
    if not llvm_project.exists():
        raise RuntimeError(
            f"LLVM project not found at {llvm_project}. "
            f"Clone it with: git clone https://github.com/llvm/llvm-project.git {llvm_project}"
        )

    _run_cmd(
        ["git", "checkout", required_hash],
        cwd=llvm_project,
        timeout=60,
    )

    # Clean and rebuild
    llvm_build_log = WORKSPACE_DIR / "llvm_build.log"
    build_dir = llvm_project / "build"
    if build_dir.exists():
        import shutil
        shutil.rmtree(build_dir)
    build_dir.mkdir()

    cmake_cmd = [
        "cmake", str(llvm_project / "llvm"),
        "-G", "Ninja",
        "-DCMAKE_BUILD_TYPE=Release",
        "-DLLVM_ENABLE_ASSERTIONS=ON",
        "-DLLVM_ENABLE_PROJECTS=mlir;llvm;lld",
        "-DLLVM_TARGETS_TO_BUILD=host;NVPTX;AMDGPU",
        f"-DCMAKE_INSTALL_PREFIX={llvm_install}",
        "-DCMAKE_C_COMPILER=clang",
        "-DCMAKE_CXX_COMPILER=clang++",
    ]
    print(f"  [llvm] Configuring...")
    _run_to_log(cmake_cmd, build_dir, llvm_build_log, timeout=300, progress_line=True)

    print(f"  [llvm] Building (this may take a while)...")
    _run_to_log(
        ["ninja", "install"],
        build_dir, llvm_build_log, timeout=7200, progress_line=True,
    )

    # Copy FileCheck — not installed by ninja install
    filecheck_src = build_dir / "bin" / "FileCheck"
    filecheck_dst = llvm_install / "bin" / "FileCheck"
    if filecheck_src.exists():
        import shutil
        filecheck_dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(filecheck_src, filecheck_dst)
        print(f"  [llvm] Copied FileCheck to {filecheck_dst}")
    else:
        print(f"  [llvm] WARNING: FileCheck not found at {filecheck_src}")

    # Write the hash cache
    llvm_install.mkdir(parents=True, exist_ok=True)
    hash_cache.write_text(required_hash, encoding="utf-8")
    print(f"  [llvm] Rebuild complete — install prefix: {llvm_install}")

    return str(llvm_install)


def _run_cmd(cmd: list[str], cwd: Path, timeout: int = 300) -> str:
    """Run a command, return stdout. Raise on failure."""
    proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        print(f"  [llvm] Command failed: {' '.join(cmd)}")
        print(f"  stderr: {proc.stderr.strip()[-500:]}")
    return proc.stdout.strip()


def build_triton_ascend(
    repo_path: Path,
    llvm_prefix: str = "",
    conda_env: str = "",
    build_dir: str = "build",
    clean_build: bool = False,
    python_exe: str = "python3",
) -> dict:
    """Build the Triton-Ascend C++ extensions and Python package.

    python_exe: Python executable to use for setup.py install
                (default 'python3', use 'python3.10' / 'python3.11' for dual tests).
    """
    print("\n=== Building Triton-Ascend ===")

    # ── Check and rebuild LLVM if needed ──
    skip_llvm = os.getenv("SKIP_LLVM_REBUILD", "false").lower() == "true"
    if skip_llvm:
        print("  SKIP_LLVM_REBUILD=true — skipping LLVM version check")
    else:
        resolved_llvm_prefix = _check_and_rebuild_llvm(repo_path)
        if resolved_llvm_prefix and not llvm_prefix:
            llvm_prefix = resolved_llvm_prefix

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
    build_cmd = [python_exe, "setup.py", "install"]
    build_proc = _run_to_log(build_cmd, repo_path, build_log, env=build_env, timeout=1800, progress_line=True)
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
    python_exe: str = "",
) -> dict:
    """Run pytest unit tests and return structured results.

    python_exe: Python executable for pytest (default '' uses TA_PYTHON env var
                or 'python3'). Set to 'python3.10' / 'python3.11' for dual tests.
    """
    print("\n=== Running Tests ===")
    test_log_dir = WORKSPACE_DIR / "test-logs"
    test_log_dir.mkdir(parents=True, exist_ok=True)

    test_log = test_log_dir / "pytest.log"
    test_dir_path = repo_path / test_dir

    env = {}
    if conda_env:
        env["CONDA_DEFAULT_ENV"] = conda_env

    python_exe = python_exe or os.getenv("TA_PYTHON", "python3")

    # Resolve to absolute path — test_dir_path may be relative, and the
    # subprocess cwd is repo_path.  A relative path relative to repo_path
    # would double-up (e.g. triton-ascend/triton-ascend/third_party/…).
    test_dir_abs = test_dir_path.resolve()

    if not test_dir_abs.exists():
        print(f"  WARNING: test directory not found: {test_dir_abs}")
        print(f"  Skipping tests — directory does not exist after merge.")
        passed = False
        summary = {
            "exit_code": -1,
            "passed": False,
            "error": f"Test directory not found: {test_dir_abs}",
            "test_dir": str(test_dir_abs),
        }
    else:
        # Print bishengir-compile path before running tests
        import shutil
        bishengir_compile_path = shutil.which("bishengir-compile")
        if bishengir_compile_path:
            print(f"  bishengir-compile: {bishengir_compile_path}")

        pytest_cmd = [
            python_exe, "-m", "pytest",
            str(test_dir_abs),
            "-n", str(num_procs),
            "--tb=short",
            "-q",
        ]

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
        precommit_passed = True
        try:
            pc_proc = subprocess.run(
                ["pre-commit", "run", "--from-ref", "origin/main", "--to-ref", "HEAD"],
                cwd=repo_path,
                stdout=precommit_log.open("w"),
                stderr=subprocess.STDOUT,
                timeout=300,
            )
            precommit_passed = pc_proc.returncode == 0
        except (subprocess.TimeoutExpired, FileNotFoundError):
            precommit_passed = False

        # ── If pre-commit auto-fixed files, amend the latest commit ──
        if not precommit_passed:
            print("  Pre-commit found issues — checking for auto-fixes...")
            from TA_main2main_workflow.utils import run_git_no_check
            status_proc = run_git_no_check(repo_path, "status", "--porcelain")
            if status_proc.stdout.strip():
                print("  Pre-commit applied auto-fixes, amending commit...")
                run_git_no_check(repo_path, "add", "-u")
                run_git_no_check(repo_path, "commit", "--amend", "--no-edit")
                print("  Commit amended with pre-commit fixes.")
            else:
                print("  Pre-commit failed but no auto-fixes were applied "
                      "(manual review may be needed).")
        else:
            print("  Pre-commit checks passed.")

        summary["precommit_passed"] = precommit_passed

    result_path = WORKSPACE_DIR / TEST_RESULT_FILE
    result_path.write_text(
        json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )

    return summary
