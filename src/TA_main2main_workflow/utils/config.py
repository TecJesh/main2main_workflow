"""Configuration for TA_main2main_workflow.

Only user-configurable parameters.  Fixed paths inside triton-ascend
repo are defined where they're used, not here.

Priority:  CLI args  >  env vars  >  defaults
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal


AIBackendChoice = Literal["opencode", "claude", "auto"]


@dataclass
class TAConfig:
    """User-configurable parameters for a workflow run."""

    # ── Repository ────────────────────────────────────────────────────────
    triton_ascend_path: str = ""  # local path (skip clone if set)
    triton_ascend_url: str = "https://github.com/triton-lang/triton-ascend.git"
    triton_path: str = ""  # local triton checkout (for offline/separate-history)
    triton_upstream_url: str = "https://github.com/triton-lang/triton.git"
    target_commit: str = ""

    # ── AI Backend ────────────────────────────────────────────────────────
    ai_backend: AIBackendChoice = "auto"
    ai_timeout_minutes: int = 30
    ai_stale_seconds: int = 1200
    ai_max_stale_retries: int = 3

    # ── Retry / Budget ────────────────────────────────────────────────────
    max_retries: int = 10
    line_budget: int = 1000

    # ── Build / Test parallelism ──────────────────────────────────────────
    llvm_install_prefix: str = ""
    llvm_repo_url: str = "https://github.com/llvm/llvm-project.git"
    build_procs: int = 32
    test_procs: int = 8

    # ── Skip flags ────────────────────────────────────────────────────────
    resume: bool = False  # skip steps whose output already exists
    skip_ai_analysis: bool = False
    skip_build: bool = False
    skip_e2e_test: bool = False
    skip_llvm_rebuild: bool = False  # skip LLVM rebuild when version changes (IR patch)
    skip_baseline_llvm: bool = False  # skip initial baseline LLVM build at workflow start

    # ── Git / Branch ──────────────────────────────────────────────────────
    base_branch: str = "upstream_sync"
    work_branch_base: str = "upstream-ascend"
    progressive_merge: bool = True

    # ── PR / Push ─────────────────────────────────────────────────────────
    push_to_github: bool = False
    github_repo: str = "triton-lang/triton-ascend"

    # ── LLVM workspace ────────────────────────────────────────────────────
    llvm_project_path: str = ""
    llvm_install_prefix_sync: str = ""

    # ── Conda / Python ────────────────────────────────────────────────────
    conda_env: str = "ta-upgrade"
    test_dir: str = "third_party/ascend/unittest/pytest_ut"
    test_dirs: list[str] = field(default_factory=list)  # resolved from env
    test_command: str = ""  # full shell command override (TA_TEST_COMMAND)
    python_exe: str = ""

    # ── Single-step mode (always enabled) ─────────────────────────────────
    single_step_mode: bool = True

    # ── IR patch ──────────────────────────────────────────────────────────
    ir_max_iterations: int = 3

    # ═══════════════════════════════════════════════════════════════════════
    @classmethod
    def from_env(cls) -> TAConfig:
        return cls(
            triton_ascend_path=os.getenv("TRITON_ASCEND_PATH", ""),
            triton_ascend_url=os.getenv(
                "TRITON_ASCEND_URL", "https://github.com/triton-lang/triton-ascend.git"
            ),
            triton_path=os.getenv("TRITON_PATH", ""),
            triton_upstream_url=os.getenv(
                "TRITON_UPSTREAM_URL", "https://github.com/triton-lang/triton.git"
            ),
            target_commit=os.getenv("TRITON_TARGET_COMMIT", ""),
            ai_backend=_env_choice(
                "AI_BACKEND", ["opencode", "claude", "auto"], "auto"
            ),
            ai_timeout_minutes=_env_int("TA_AI_TIMEOUT_MINUTES", 30),
            ai_stale_seconds=_env_int("TA_AI_STALE_SECONDS", 1200),
            ai_max_stale_retries=_env_int("TA_AI_MAX_STALE_RETRIES", 3),
            max_retries=_env_int("TA_MAX_RETRIES", 10),
            line_budget=_env_int("TA_LINE_BUDGET", 1000),
            llvm_install_prefix=os.getenv("LLVM_INSTALL_PREFIX", ""),
            llvm_repo_url=os.getenv(
                "LLVM_REPO_URL", "https://github.com/llvm/llvm-project.git"
            ),
            build_procs=_env_int_fallback("BUILD_PROCS", "MAX_JOBS", 32),
            test_procs=_env_int_fallback("TEST_PROCS", "NUM_PROCS", 8),
            resume=_env_bool("TA_RESUME", False),
            skip_ai_analysis=_env_bool("SKIP_AI_ANALYSIS", False),
            skip_build=_env_bool("SKIP_BUILD", False),
            skip_e2e_test=_env_bool("SKIP_E2E_TEST", False),
            skip_llvm_rebuild=_env_bool("SKIP_LLVM_REBUILD", False),
            skip_baseline_llvm=_env_bool("SKIP_BASELINE_LLVM", False),
            base_branch=os.getenv("TA_BASE_BRANCH", "upstream_sync"),
            work_branch_base=os.getenv("TA_WORK_BRANCH_BASE", "upstream-ascend"),
            progressive_merge=_env_bool("TA_PROGRESSIVE_MERGE", True),
            push_to_github=_env_bool("PUSH_TO_GITHUB", False),
            github_repo=os.getenv("GITHUB_REPO", "triton-lang/triton-ascend"),
            llvm_project_path=os.getenv("LLVM_PROJECT_PATH", ""),
            llvm_install_prefix_sync=os.getenv("LLVM_INSTALL_PREFIX_SYNC", ""),
            conda_env=os.getenv("CONDA_ENV", "ta-upgrade"),
            test_dir=os.getenv("TA_TEST_DIR", "third_party/ascend/unittest/pytest_ut"),
            test_dirs=_resolve_test_dirs(),
            test_command=os.getenv("TA_TEST_COMMAND", ""),
            python_exe=os.getenv("PYTHON", ""),
            single_step_mode=_env_bool("TA_SINGLE_STEP_MODE", True),
            ir_max_iterations=_env_int("TA_IR_MAX_ITERATIONS", 3),
        )

    @property
    def llvm_project(self) -> Path:
        if self.llvm_project_path:
            return Path(os.path.expanduser(self.llvm_project_path))
        return Path(os.path.expanduser(os.getenv("LLVM_PROJECT_PATH", "~/llvm-project")))

    @property
    def llvm_install(self) -> Path:
        if self.llvm_install_prefix_sync:
            return Path(os.path.expanduser(self.llvm_install_prefix_sync))
        return Path(os.path.expanduser(
            os.getenv("LLVM_INSTALL_PREFIX_SYNC", "~/llvm-install-sync")))


def _env_bool(name: str, default: bool) -> bool:
    val = os.getenv(name, "").lower()
    if val in ("true", "1", "yes"):
        return True
    if val in ("false", "0", "no"):
        return False
    return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _env_int_fallback(name: str, legacy_name: str, default: int) -> int:
    """Read *name* first; if unset, fall back to *legacy_name*."""
    val = os.getenv(name, "")
    if val:
        try:
            return int(val)
        except (TypeError, ValueError):
            pass
    val = os.getenv(legacy_name, "")
    if val:
        try:
            return int(val)
        except (TypeError, ValueError):
            pass
    return default


def _resolve_test_dirs(primary: str = "", extra: str = "") -> list[str]:
    """Build the ordered list of test directories to run.

    ``TA_TEST_DIR`` provides the primary directory (default
    ``third_party/ascend/unittest/pytest_ut``).  ``TA_EXTRA_TEST_DIRS``
    adds additional directories, comma- or space-separated.

    When called from main.py with CLI args, *primary* and *extra* override
    the env vars.
    """
    _primary = primary or os.getenv("TA_TEST_DIR", "third_party/ascend/unittest/pytest_ut")
    dirs = [_primary] if _primary else []

    extra_raw = extra or os.getenv("TA_EXTRA_TEST_DIRS", "")
    if extra_raw:
        for part in extra_raw.replace(",", " ").split():
            part = part.strip()
            if part and part not in dirs:
                dirs.append(part)

    return dirs


def _env_choice(name: str, choices: list[str], default: str) -> str:
    val = os.getenv(name, default).lower()
    return val if val in choices else default
