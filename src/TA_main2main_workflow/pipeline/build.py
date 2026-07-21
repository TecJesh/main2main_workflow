"""Pipeline step: Build LLVM then Triton-Ascend, each with retry/fix loops."""

from __future__ import annotations
import json, os, subprocess
from pathlib import Path
from TA_main2main_workflow.utils.config import TAConfig
from TA_main2main_workflow.utils.context import WorkflowContext
from TA_main2main_workflow.utils.logging import get_logger
from TA_main2main_workflow.utils.tracker import timed
from TA_main2main_workflow.utils.git import run_git
from TA_main2main_workflow.utils import BUILD_RESULT_FILE, STEPS_DIR, WORKSPACE_DIR
from TA_main2main_workflow.pipeline.fix import ai_fix

log = get_logger(__name__)

LLVM_BUILD_LOG = "llvm-build.log"


def build(ctx: WorkflowContext, config: TAConfig) -> WorkflowContext:
    """Build phase: LLVM → triton-ascend, with unified retry+fix loop.

    Each retry attempt rebuilds LLVM first, then triton-ascend.
    This ensures LLVM fixes are re-validated when triton fails.
    """
    if config.skip_build:
        log.info("SKIP_BUILD=true — skipping build")
        return ctx.copy_with(build_passed=True)

    if not config.skip_llvm_rebuild:
        ctx = _llvm_setup(ctx, config)

    for attempt in range(config.max_retries + 1):
        ctx = ctx.copy_with(retry_count=attempt)
        if attempt > 0:
            log.header(f"Build Fix Attempt {attempt}/{config.max_retries}")
            ctx = ai_fix(ctx, config, attempt=attempt)

        # ── Rebuild LLVM every attempt ──────────────────────────────
        if not config.skip_llvm_rebuild:
            with timed("build-llvm"):
                ctx = _build_llvm(ctx, config.build_procs)
            if not ctx.build_passed:
                log.info(f"LLVM build failed (attempt {attempt + 1}) — retrying")
                continue

        # ── Build triton-ascend ─────────────────────────────────────
        with timed("build-triton"):
            ctx = _build_triton(ctx, config, clean=(attempt == 0))
        if ctx.build_passed:
            return ctx
        log.info(f"Triton build failed (attempt {attempt + 1}) — retrying")

    return ctx.copy_with(build_passed=False)


# ═══════════════════════════════════════════════════════════════════════════
# LLVM
# ═══════════════════════════════════════════════════════════════════════════

def _llvm_setup(ctx: WorkflowContext, config: TAConfig) -> WorkflowContext:
    """Clone LLVM, checkout hash, apply patch. Idempotent — only runs if hash changed."""
    ascend_path = Path(ctx.triton_ascend_path)
    llvm_project = WORKSPACE_DIR / "llvm-project"
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
        # Patch naming: llvm_patch_<short-hash>.patch
        if required_hash[:7] in patch_files[0].name:
            log.info(f"Applying patch: {patch_files[0].name}")
            run_git(llvm_project, "apply", str(patch_files[0]))
        else:
            log.info(f"LLVM hash changed ({required_hash[:12]}), patch {patch_files[0].name} "
                     f"is for old version — will let AI generate new patch")
    return ctx


def _build_llvm(ctx: WorkflowContext, num_procs: int = 32) -> WorkflowContext:
    """cmake + ninja for LLVM. Pure build — no retry logic.

    On success, regenerates patch if AI fix modified the source.
    """
    llvm_project = WORKSPACE_DIR / "llvm-project"
    llvm_install = WORKSPACE_DIR / "llvm-install"
    ascend_path = Path(ctx.triton_ascend_path)
    required_hash = (ascend_path / "cmake" / "llvm-hash.txt").read_text(encoding="utf-8").strip()

    step_id = ctx.steps[ctx.current_step]["id"] if ctx.steps else "step-0"
    step_dir = WORKSPACE_DIR / STEPS_DIR / step_id
    step_dir.mkdir(parents=True, exist_ok=True)

    build_dir = WORKSPACE_DIR / "llvm-build"
    build_dir.mkdir(parents=True, exist_ok=True)

    log.info(f"cmake configure (parallel: {num_procs})...")
    cmake_log = step_dir / "llvm-cmake.log"
    cmake_err = step_dir / "llvm-cmake.err"
    try:
        with open(cmake_log, "w") as o, open(cmake_err, "w") as e:
            subprocess.run(
                ["cmake", str(llvm_project / "llvm"), "-G", "Ninja",
                 "-DCMAKE_BUILD_TYPE=Release", "-DLLVM_ENABLE_ASSERTIONS=ON",
                 "-DLLVM_ENABLE_PROJECTS=mlir;llvm;lld",
                 "-DLLVM_TARGETS_TO_BUILD=host;NVPTX;AMDGPU",
                 f"-DCMAKE_INSTALL_PREFIX={llvm_install}",
                 "-DCMAKE_C_COMPILER=clang", "-DCMAKE_CXX_COMPILER=clang++"],
                cwd=build_dir, check=True, stdout=o, stderr=e)
    except subprocess.CalledProcessError:
        log.error("LLVM cmake FAILED")
        return ctx.copy_with(build_passed=False,
                             fix_errors=[str(cmake_log), str(cmake_err)])

    log.info(f"ninja -j{num_procs} install (this may take a while)...")
    ninja_log = step_dir / "llvm-ninja.log"
    ninja_err = step_dir / "llvm-ninja.err"
    try:
        with open(ninja_log, "w") as o, open(ninja_err, "w") as e:
            subprocess.run(["ninja", "-j", str(num_procs), "install"], cwd=build_dir,
                           check=True, stdout=o, stderr=e)
    except subprocess.CalledProcessError:
        log.error("LLVM ninja FAILED")
        return ctx.copy_with(build_passed=False,
                             fix_errors=[str(ninja_log), str(ninja_err)])

    fc = build_dir / "bin" / "FileCheck"
    if fc.exists():
        import shutil; shutil.copy2(fc, llvm_install / "bin" / "FileCheck")

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

def _build_triton(ctx: WorkflowContext, config: TAConfig, clean: bool = False,
                  python_exe: str = "python3") -> WorkflowContext:
    """Build triton-ascend. Pure build — no retry logic."""
    ascend_path = Path(ctx.triton_ascend_path)
    llvm_install = WORKSPACE_DIR / "llvm-install"
    llvm_prefix = config.llvm_install_prefix or (str(llvm_install) if llvm_install.exists() else "")

    if clean:
        build_dir_path = ascend_path / "build"
        if build_dir_path.exists():
            subprocess.run(["rm", "-rf", str(build_dir_path)], check=False)

    build_env = {
        "LLVM_SYSPATH": llvm_prefix,
        "TRITON_BUILD_WITH_CCACHE": "true",
        "TRITON_BUILD_WITH_CLANG_LLD": "true",
        "TRITON_BUILD_PROTON": "OFF",
        "DEBUG": "1",
        "TRITON_WHEEL_NAME": "triton-ascend",
        "TRITON_APPEND_CMAKE_ARGS": "-DTRITON_BUILD_UT=OFF",
        "MAX_JOBS": str(config.build_procs),
        "CMAKE_BUILD_PARALLEL_LEVEL": str(config.build_procs),
    }

    step_id = ctx.steps[ctx.current_step]["id"] if ctx.steps else "step-0"
    step_dir = WORKSPACE_DIR / STEPS_DIR / step_id
    step_dir.mkdir(parents=True, exist_ok=True)
    build_log = step_dir / "build.log"
    build_err = step_dir / "build.err"

    log.section("Build Triton-Ascend")
    log.info(f"Running: {python_exe} setup.py install")
    with open(build_log, "w") as o, open(build_err, "w") as e:
        proc = subprocess.run([python_exe, "setup.py", "install"], cwd=ascend_path,
                              env={**os.environ, **build_env}, stdout=o, stderr=e)
    passed = proc.returncode == 0

    result = {"all_passed": passed, "steps": [{"step": "setup_py_install",
                "passed": passed, "exit_code": proc.returncode}]}
    (step_dir / BUILD_RESULT_FILE).write_text(
        json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    if not passed:
        log.error("Build FAILED")
        return ctx.copy_with(build_passed=False,
                             fix_errors=[str(build_log), str(build_err)])
    log.status(True, "Build passed")
    return ctx.copy_with(build_passed=True)
