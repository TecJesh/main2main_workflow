"""Pipeline step: Build Triton-Ascend with AI fix loops.

Main entry point for single-step mode:

  ``build_and_fix_loop(ctx, config)`` — Build TA with AI fix compile
      errors loop.  Used when LLVM hash has NOT changed (LLVM was
      already built by the baseline step).

LLVM build helpers (``build_llvm``, ``llvm_setup``) are public so the
IR patch pipeline in ``ir_patch.py`` can reuse them for LLVM version
changes.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from TA_main2main_workflow.utils.config import TAConfig
from TA_main2main_workflow.utils.context import WorkflowContext
from TA_main2main_workflow.utils.logging import get_logger
from TA_main2main_workflow.utils.tracker import timed
from TA_main2main_workflow.utils.git import run_git, stream_cmd
from TA_main2main_workflow.utils import (
    BUILD_RESULT_FILE, STEPS_DIR, WORKSPACE_DIR,
)
from TA_main2main_workflow.pipeline.fix import ai_fix

log = get_logger(__name__)


def build_and_fix_loop(ctx: WorkflowContext, config: TAConfig) -> WorkflowContext:
    """Build TA with AI fix loop for compile errors.

    Used in single-step mode when LLVM hash has NOT changed.
    Does NOT rebuild LLVM — assumes baseline LLVM is already built.
    """
    if config.skip_build:
        log.info("SKIP_BUILD=true — skipping build")
        return ctx.copy_with(build_passed=True)

    ascend_path = Path(ctx.triton_ascend_path)

    attempt = 0
    while attempt <= config.max_retries:
        ctx = ctx.copy_with(retry_count=attempt)

        if attempt > 0:
            log.header(f"Build Fix Attempt {attempt}/{config.max_retries}")
            ctx = ai_fix(ctx, config, attempt=attempt, mode="fix")

        with timed("build-triton"):
            ctx = build_triton(ctx, config, clean=(attempt == 0))

        if ctx.build_passed:
            # Commit fixes if any
            if attempt > 0:
                step = ctx.steps[ctx.current_step] if ctx.steps else {"id": "step-0"}
                step_dir = WORKSPACE_DIR / STEPS_DIR / step["id"]
                commit_fixes(ascend_path, step_dir)
            return ctx.copy_with(
                build_passed=True,
                build_fix_count=ctx.build_fix_count + (1 if attempt > 0 else 0),
            )

        log.info(f"Triton build failed (attempt {attempt + 1}) — retrying")
        attempt += 1

    return ctx.copy_with(build_passed=False)


# ═══════════════════════════════════════════════════════════════════════════
# LLVM
# ═══════════════════════════════════════════════════════════════════════════


def llvm_setup(ctx: WorkflowContext, config: TAConfig) -> WorkflowContext:
    """Clone LLVM, checkout hash, apply patch. Idempotent."""
    ascend_path = Path(ctx.triton_ascend_path)
    llvm_project = config.llvm_project
    llvm_hash_file = ascend_path / "cmake" / "llvm-hash.txt"

    if not llvm_hash_file.exists():
        log.info("No llvm-hash.txt — skipping LLVM rebuild")
        return ctx
    required_hash = llvm_hash_file.read_text(encoding="utf-8").strip()
    if not required_hash:
        return ctx

    if not llvm_project.exists():
        run_git(WORKSPACE_DIR, "clone", config.llvm_repo_url, str(llvm_project))

    log.section(f"LLVM setup (hash: {required_hash[:12]})")
    run_git(llvm_project, "fetch", "origin", required_hash)
    run_git(llvm_project, "reset", "--hard", "HEAD")
    run_git(llvm_project, "clean", "-fd")
    run_git(llvm_project, "checkout", "-f", required_hash)

    patch_dir = ascend_path / "third_party/ascend/patch"
    patch_files = sorted(patch_dir.glob("*.patch")) if patch_dir.exists() else []
    if patch_files:
        if required_hash[:7] in patch_files[0].name:
            log.info(f"Applying patch: {patch_files[0].name}")
            run_git(llvm_project, "apply", str(patch_files[0]))
        else:
            log.info(
                f"LLVM hash changed ({required_hash[:12]}), "
                f"patch {patch_files[0].name} is for old version"
            )
    return ctx


def build_llvm(ctx: WorkflowContext, num_procs: int = 32) -> WorkflowContext:
    """cmake + ninja for LLVM. Pure build — no retry logic."""
    llvm_project = Path(os.path.expanduser(
        os.getenv("LLVM_PROJECT_PATH", "~/llvm-project")))
    llvm_install = Path(os.path.expanduser(
        os.getenv("LLVM_INSTALL_PREFIX_SYNC", "~/llvm-install-sync")))
    ascend_path = Path(ctx.triton_ascend_path)
    required_hash = (
        (ascend_path / "cmake" / "llvm-hash.txt").read_text(encoding="utf-8").strip()
    )

    step_id = ctx.steps[ctx.current_step]["id"] if ctx.steps else "step-0"
    step_dir = WORKSPACE_DIR / STEPS_DIR / step_id
    step_dir.mkdir(parents=True, exist_ok=True)

    build_dir = WORKSPACE_DIR / "llvm-build"
    if build_dir.exists():
        import shutil
        shutil.rmtree(build_dir)
    build_dir.mkdir(parents=True, exist_ok=True)

    log_dir = step_dir
    log_dir.mkdir(parents=True, exist_ok=True)
    llvm_build_log = log_dir / "llvm-build.log"

    # ── cmake configure ──
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
    with open(llvm_build_log, "w", encoding="utf-8") as fh:
        fh.write(f"=== cmake ===\n{' '.join(cmake_cmd)}\n\n")
        fh.flush()
        rc = stream_cmd(cmake_cmd, build_dir, fh, timeout=300,
                        label="Configuring LLVM with cmake")
    if rc != 0:
        log.error(f"LLVM cmake FAILED — see {llvm_build_log}")
        return ctx.copy_with(
            build_passed=False, fix_errors=[str(llvm_build_log)]
        )
    log.status(True, "cmake configure OK")

    # ── ninja build + install ──
    log.info(f"ninja -j{num_procs} install (this may take a while)...")
    with open(llvm_build_log, "a", encoding="utf-8") as fh:
        fh.write(f"\n=== ninja install ===\n")
        fh.flush()
        rc = stream_cmd(
            ["ninja", "-j", str(num_procs), "install"],
            build_dir, fh, timeout=7200, label="ninja install",
        )
    if rc != 0:
        log.error(f"LLVM ninja FAILED — see {llvm_build_log}")
        return ctx.copy_with(
            build_passed=False, fix_errors=[str(llvm_build_log)]
        )
    log.status(True, "LLVM ninja install OK")

    # Copy FileCheck
    import shutil
    fc = build_dir / "bin" / "FileCheck"
    if fc.exists():
        shutil.copy2(fc, llvm_install / "bin" / "FileCheck")

    # Regenerate patch if AI fix modified the source
    if ctx.retry_count > 0:
        patch_dir = ascend_path / "third_party/ascend/patch"
        patch_dir.mkdir(parents=True, exist_ok=True)
        new_patch_file = patch_dir / f"llvm_patch_{required_hash[:7]}.patch"
        new_patch = run_git(llvm_project, "diff", "HEAD")
        new_patch_file.write_text(new_patch, encoding="utf-8")
        for old in patch_dir.glob("*.patch"):
            if old.name != new_patch_file.name:
                old.unlink()
        log.info(f"Updated patch: {new_patch_file.name} ({len(new_patch)} bytes)")

    log.status(True, "LLVM build passed")
    return ctx.copy_with(build_passed=True)


# ═══════════════════════════════════════════════════════════════════════════
# Triton-Ascend
# ═══════════════════════════════════════════════════════════════════════════


def build_triton(
    ctx: WorkflowContext, config: TAConfig,
    clean: bool = False, python_exe: str = "",
) -> WorkflowContext:
    """Build triton-ascend. Pure build — no retry logic."""
    ascend_path = Path(ctx.triton_ascend_path)
    llvm_install = config.llvm_install
    llvm_prefix = config.llvm_install_prefix or (
        str(llvm_install) if llvm_install.exists() else ""
    )
    python_exe = python_exe or config.python_exe or os.getenv("PYTHON", "python3")

    if clean:
        build_dir_path = ascend_path / "build"
        if build_dir_path.exists():
            subprocess.run(["rm", "-rf", str(build_dir_path)], check=False)

    build_env = {
        "LLVM_SYSPATH": llvm_prefix,
        "LLVM_INSTALL_PREFIX": llvm_prefix,
        "TRITON_BUILD_WITH_CCACHE": "true",
        "TRITON_BUILD_WITH_CLANG_LLD": "true",
        "TRITON_BUILD_PROTON": "OFF",
        "DEBUG": "1",
        "TRITON_WHEEL_NAME": "triton-ascend",
        "TRITON_APPEND_CMAKE_ARGS": "-DTRITON_BUILD_UT=OFF",
        "MAX_JOBS": str(config.build_procs),
        "CMAKE_BUILD_PARALLEL_LEVEL": str(config.build_procs),
    }

    log.key_value("LLVM prefix", llvm_prefix if llvm_prefix else "(empty)")

    step_id = ctx.steps[ctx.current_step]["id"] if ctx.steps else "step-0"
    step_dir = WORKSPACE_DIR / STEPS_DIR / step_id
    step_dir.mkdir(parents=True, exist_ok=True)
    build_log = step_dir / "build.log"

    log.section("Build Triton-Ascend")
    log.info(f"Running: {python_exe} setup.py install")
    with open(build_log, "w", encoding="utf-8") as fh:
        fh.write(f"=== setup.py install ===\n{' '.join(build_env.keys())}\n\n")
        fh.flush()
        rc = stream_cmd(
            [python_exe, "setup.py", "install"],
            cwd=ascend_path,
            log_fh=fh,
            timeout=1800,
            label="Building Triton-Ascend",
        )
    passed = rc == 0

    result = {
        "all_passed": passed,
        "steps": [
            {"step": "setup_py_install", "passed": passed, "exit_code": rc}
        ],
    }
    (step_dir / BUILD_RESULT_FILE).write_text(
        json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )

    if not passed:
        log.error(f"Build FAILED — see {build_log}")
        return ctx.copy_with(
            build_passed=False, fix_errors=[str(build_log)]
        )
    log.status(True, "Build passed")
    return ctx.copy_with(build_passed=True)


def commit_fixes(ascend_path: Path, step_dir: Path) -> None:
    """Commit AI build fixes."""
    try:
        if run_git(ascend_path, "status", "--porcelain").strip():
            run_git(ascend_path, "add", "-A")
            run_git(ascend_path, "commit", "-s", "-m",
                    "[Sync](fix) AI build fix\n")
            log.status(True, "Build fixes committed")
    except Exception as e:
        log.warning(f"Failed to commit build fixes: {e}")
