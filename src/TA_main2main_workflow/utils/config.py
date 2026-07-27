"""Configuration for TA_main2main_workflow.

Only user-configurable parameters.  Fixed paths inside triton-ascend
repo are defined where they're used, not here.

Priority:  CLI args  >  env vars  >  defaults
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Literal


AIBackendChoice = Literal["opencode", "claude", "auto"]


@dataclass
class TAConfig:
    """User-configurable parameters for a workflow run."""

    # ── Repository ────────────────────────────────────────────────────────
    triton_ascend_path: str = ""
    triton_ascend_url: str = "https://github.com/triton-lang/triton-ascend.git"
    triton_path: str = ""
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

    # ── IR Patch ──────────────────────────────────────────────────────────
    ir_max_iterations: int = 3
    ascend_baseline_llvm_hash: str = (
        "b5cc222d7429fe6f18c787f633d5262fac2e676f")

    # ── Build / Test parallelism ──────────────────────────────────────────
    llvm_install_prefix_sync: str = ""       # LLVM_INSTALL_PREFIX_SYNC
    llvm_project_path: str = ""             # LLVM_PROJECT_PATH
    llvm_repo_url: str = "https://github.com/llvm/llvm-project.git"
    build_procs: int = 32
    test_procs: int = 16                    # pytest -n
    test_dir: str = "third_party/ascend/unittest/pytest_ut"
    conda_env: str = ""

    # ── Skip flags ────────────────────────────────────────────────────────
    resume: bool = False
    skip_ai_analysis: bool = False
    skip_build: bool = False
    skip_e2e_test: bool = False
    skip_llvm_rebuild: bool = False
    skip_ir_patch: bool = False

    # ── Git / Branch ──────────────────────────────────────────────────────
    base_branch: str = "main"
    progressive_merge: bool = True
    single_step_mode: bool = True

    # ── PR / Push ─────────────────────────────────────────────────────────
    push_to_github: bool = False
    github_repo: str = "triton-lang/triton-ascend"
    gh_token: str = ""
    pr_base_branch: str = "upstream-sync"

    # ── Workspace ─────────────────────────────────────────────────────────
    workspace_dir: str = ""

    # ═══════════════════════════════════════════════════════════════════════
    @classmethod
    def from_env(cls) -> TAConfig:
        return cls(
            triton_ascend_path=os.getenv("TRITON_ASCEND_PATH", ""),
            triton_ascend_url=os.getenv(
                "TRITON_ASCEND_URL",
                "https://github.com/triton-lang/triton-ascend.git"),
            triton_path=os.getenv("TRITON_PATH", ""),
            triton_upstream_url=os.getenv(
                "TRITON_UPSTREAM_URL",
                "https://github.com/triton-lang/triton.git"),
            target_commit=os.getenv("TRITON_TARGET_COMMIT", ""),
            ai_backend=_env_choice(
                "AI_BACKEND", ["opencode", "claude", "auto"], "auto"),
            ai_timeout_minutes=_env_int("TA_AI_TIMEOUT_MINUTES", 30),
            ai_stale_seconds=_env_int("TA_AI_STALE_SECONDS", 1200),
            ai_max_stale_retries=_env_int("TA_AI_MAX_STALE_RETRIES", 3),
            max_retries=_env_int("TA_MAX_RETRIES", 10),
            line_budget=_env_int("TA_LINE_BUDGET", 1000),
            ir_max_iterations=_env_int("IR_MAX_ITERATIONS", 3),
            ascend_baseline_llvm_hash=os.getenv(
                "ASCEND_BASELINE_LLVM_HASH",
                "b5cc222d7429fe6f18c787f633d5262fac2e676f"),
            llvm_install_prefix_sync=os.getenv(
                "LLVM_INSTALL_PREFIX_SYNC", ""),
            llvm_project_path=os.getenv("LLVM_PROJECT_PATH", ""),
            llvm_repo_url=os.getenv(
                "LLVM_REPO_URL",
                "https://github.com/llvm/llvm-project.git"),
            build_procs=_env_int("BUILD_PROCS", 32),
            test_procs=_env_int("NUM_PROCS", 16),
            test_dir=os.getenv(
                "TEST_DIR", "third_party/ascend/unittest/pytest_ut"),
            conda_env=os.getenv("CONDA_DEFAULT_ENV", ""),
            resume=_env_bool("TA_RESUME", False),
            skip_ai_analysis=_env_bool("SKIP_AI_ANALYSIS", False),
            skip_build=_env_bool("SKIP_BUILD", False),
            skip_e2e_test=_env_bool("SKIP_E2E_TEST", False),
            skip_llvm_rebuild=_env_bool("SKIP_LLVM_REBUILD", False),
            skip_ir_patch=_env_bool("SKIP_IR_PATCH", False),
            base_branch=os.getenv("TA_BASE_BRANCH", "main"),
            progressive_merge=_env_bool("TA_PROGRESSIVE_MERGE", True),
            single_step_mode=_env_bool("ENV_SINGLE_STEP_MODE", True),
            push_to_github=_env_bool("PUSH_TO_GITHUB", False),
            github_repo=os.getenv("GITHUB_REPO", "triton-lang/triton-ascend"),
            gh_token=os.getenv("GH_TOKEN", ""),
            pr_base_branch=os.getenv("TA_PR_BASE_BRANCH", "upstream-sync"),
            workspace_dir=os.getenv("TA_MAIN2MAIN_WORKSPACE", ""),
        )


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


def _env_choice(name: str, choices: list[str], default: str) -> str:
    val = os.getenv(name, default).lower()
    return val if val in choices else default
