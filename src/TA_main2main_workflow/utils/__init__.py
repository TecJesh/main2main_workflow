"""Utility package for TA_main2main_workflow.

All constants, helpers, and configuration live here.
Backward-compatible with the old flat ``utils.py`` module.
"""

from __future__ import annotations

import os
from pathlib import Path

# ═══════════════════════════════════════════════════════════════════════════
# Workspace paths (computed once at import time)
# ═══════════════════════════════════════════════════════════════════════════

WORKSPACE_DIR = Path(os.getenv("TA_MAIN2MAIN_WORKSPACE", str(Path.cwd() / "workspace")))

# ═══════════════════════════════════════════════════════════════════════════
# Flow routing signals
# ═══════════════════════════════════════════════════════════════════════════

UpgradeCompleted = "UpgradeCompleted"
UpgradeFailed = "UpgradeFailed"
HasNewCommits = "HasNewCommits"
HasNoNewCommits = "HasNoNewCommits"

# ═══════════════════════════════════════════════════════════════════════════
# Step-planning constants
# ═══════════════════════════════════════════════════════════════════════════

LINE_BUDGET = 1000
LLVM_HASH_FILE = "cmake/llvm-hash.txt"
ENV_BASE_BRANCH = "TA_BASE_BRANCH"


def get_base_branch_ref(remote: str = "origin") -> str:
    branch = os.getenv(ENV_BASE_BRANCH, "upstream_sync")
    return f"{remote}/{branch}"


# ═══════════════════════════════════════════════════════════════════════════
# Output file names
# ═══════════════════════════════════════════════════════════════════════════

DETECT_FILE = "detect.json"
STEPS_FILE = "steps.json"
BUILD_RESULT_FILE = "build_result.json"
TEST_RESULT_FILE = "test_result.json"
FIX_LOG_DIR = "fixes"
STEPS_DIR = "steps"
FINAL_SUMMARY_FILE = "final_summary.md"
FINAL_TARGET_PATCH_FILE = "final_target.patch"
EACH_STEP_SUMMARY_FILE = "step_summary.md"
EACH_STEP_TARGET_PATCH_FILE = "step_target.patch"
PRE_CI_CHECK_FILE = "pre_ci_check.json"

IR_ANALYSIS_DIR = "ir-analysis"
IR_OPS_REPORT_FILE = "ops_report.json"
IR_CHANGES_REPORT_FILE = "changes_report.json"
IR_DIAGNOSIS_FILE = "ir_diagnosis.json"
IR_MAX_ITERATIONS = 3
LLVM_CHANGE_ANALYSIS_DIR = "llvm_change_analysis"

# ═══════════════════════════════════════════════════════════════════════════
# Sub-package re-exports (backward compatibility)
# ═══════════════════════════════════════════════════════════════════════════

# errors
from TA_main2main_workflow.utils.errors import (  # noqa: F401, E402
    TAWorkflowError, StepError, RetryableError,
    ConfigError, GitError, MergeConflictError,
    BuildError, LLVMBuildError, TestFailureError,
    AIBackendError, AIStaleTimeoutError,
    PushError, IRPatchError,
)

# config
from TA_main2main_workflow.utils.config import TAConfig  # noqa: F401, E402

# context
from TA_main2main_workflow.utils.context import WorkflowContext  # noqa: F401, E402

# logging (replaces console.py)
from TA_main2main_workflow.utils.logging import (  # noqa: F401, E402
    get_logger, TALogger, default_logger,
)

# Backward-compat: module-level wrappers for old print_* calls
def print_header(title: str) -> None: default_logger.header(title)
def print_section(title: str) -> None: default_logger.section(title)
def print_step(n: int, t: int, name: str) -> None: default_logger.step(n, t, name)
def print_status(ok: bool, msg: str) -> None: default_logger.status(ok, msg)
def print_info(msg: str) -> None: default_logger.info(msg)
def print_warn(msg: str) -> None: default_logger.warning(msg)
def print_error(msg: str) -> None: default_logger.error(msg)
def print_key_value(k: str, v) -> None: default_logger.key_value(k, v)
def print_flow_progress(p: str, d: str = "") -> None: default_logger.flow_progress(p, d)
def print_summary_table(rows) -> None: default_logger.table(rows)
def print_conflict_list(files) -> None: default_logger.conflict_list(files)
def print_ai_call_info(b, m, a, ma) -> None: default_logger.ai_call(b, m, a, ma)
def print_ai_result(ok, files=(), summary="") -> None: default_logger.ai_result(ok, list(files), summary)
def print_elapsed_total(secs: float) -> None: default_logger.elapsed(secs)
def print_separator() -> None: pass
def _ts() -> str: from datetime import datetime; return datetime.now().strftime("%H:%M:%S")

# git (backward compat)
from TA_main2main_workflow.utils.git import (  # noqa: F401, E402
    run_git, run_git_no_check,
    get_repo_head,
    get_modified_files,
)

# submodule (backward compat)
from TA_main2main_workflow.utils.submodule import (  # noqa: F401, E402
    submodule_has_changes, commit_submodule, push_submodule,
)

# tracker (backward compat)
from TA_main2main_workflow.utils.tracker import (  # noqa: F401, E402
    start_timer, stop_timer,
)

# logging
from TA_main2main_workflow.utils.logging import (  # noqa: F401, E402
    get_logger, TALogger,
)
