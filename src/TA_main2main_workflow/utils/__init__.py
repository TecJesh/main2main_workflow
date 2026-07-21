"""Utility package for TA_main2main_workflow."""

from __future__ import annotations

import os
from pathlib import Path

# ═══════════════════════════════════════════════════════════════════════════
# Workspace paths
# ═══════════════════════════════════════════════════════════════════════════

WORKSPACE_DIR = Path(os.getenv("TA_MAIN2MAIN_WORKSPACE", str(Path.cwd() / "workspace")))

# ═══════════════════════════════════════════════════════════════════════════
# Flow routing signals
# ═══════════════════════════════════════════════════════════════════════════

UpgradeCompleted = "UpgradeCompleted"
UpgradeFailed = "UpgradeFailed"

# ═══════════════════════════════════════════════════════════════════════════
# Step-planning constants
# ═══════════════════════════════════════════════════════════════════════════

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
PRE_CI_CHECK_FILE = "pre_ci_check.json"

# ═══════════════════════════════════════════════════════════════════════════
# Re-exports
# ═══════════════════════════════════════════════════════════════════════════

from TA_main2main_workflow.utils.config import TAConfig  # noqa: F401, E402
from TA_main2main_workflow.utils.context import WorkflowContext  # noqa: F401, E402
from TA_main2main_workflow.utils.git import run_git, run_git_no_check  # noqa: F401, E402
from TA_main2main_workflow.utils.logging import get_logger, TALogger  # noqa: F401, E402
