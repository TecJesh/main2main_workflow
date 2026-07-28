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
HasNewCommits = "HasNewCommits"
HasNoNewCommits = "HasNoNewCommits"

# ═══════════════════════════════════════════════════════════════════════════
# Step-planning constants
# ═══════════════════════════════════════════════════════════════════════════

LLVM_HASH_FILE = "cmake/llvm-hash.txt"
LINE_BUDGET = 1000
SOURCE_DIRS = ["python/triton/", "lib/", "include/"]
ENV_SINGLE_STEP_MODE = "TA_SINGLE_STEP_MODE"
ENV_BASE_BRANCH = "TA_BASE_BRANCH"

# Baseline LLVM version that Ascend backend OP usage is built against
_ASCEND_BASELINE_LLVM_HASH = "b5cc222d7429fe6f18c787f633d5262fac2e676f"


def get_base_branch_ref(remote: str = "origin") -> str:
    branch = os.getenv(ENV_BASE_BRANCH, "upstream_sync")
    return f"{remote}/{branch}"


# ═══════════════════════════════════════════════════════════════════════════
# Output file names
# ═══════════════════════════════════════════════════════════════════════════

DETECT_FILE = "detect.json"
STEPS_FILE = "steps.json"
BUILD_LOG_FILE = "build.log"
BUILD_RESULT_FILE = "build_result.json"
TEST_RESULT_FILE = "test_result.json"
MERGE_LOG_FILE = "merge.log"
MERGE_RESULT_FILE = "merge_result.json"
FIX_LOG_DIR = "fixes"
STEPS_DIR = "steps"
CONFLICT_LOG_DIR = "conflicts"
FINAL_SUMMARY_FILE = "final_summary.md"
FINAL_TARGET_PATCH_FILE = "final_target.patch"
EACH_STEP_SUMMARY_FILE = "step_summary.md"
EACH_STEP_TARGET_PATCH_FILE = "step_target.patch"
PRE_CI_CHECK_FILE = "pre_ci_check.json"
CODE_STRUCTURE_GUIDE_FILE = "code_structure.md"

# ═══════════════════════════════════════════════════════════════════════════
# IR analysis / patch file names
# ═══════════════════════════════════════════════════════════════════════════

IR_ANALYSIS_DIR = "ir_analysis"
IR_PATCHES_DIR = "ir_patches"
IR_OPS_REPORT_FILE = "ops_report.json"
IR_CHANGES_REPORT_FILE = "changes_report.json"
IR_DIAGNOSIS_FILE = "diagnosis.json"
IR_MAX_ITERATIONS = 3
LLVM_CHANGE_ANALYSIS_DIR = "llvm_change_analysis"

# ═══════════════════════════════════════════════════════════════════════════
# Re-exports
# ═══════════════════════════════════════════════════════════════════════════

from TA_main2main_workflow.utils.config import TAConfig  # noqa: F401, E402
from TA_main2main_workflow.utils.context import WorkflowContext  # noqa: F401, E402
from TA_main2main_workflow.utils.git import run_git, run_git_no_check  # noqa: F401, E402
from TA_main2main_workflow.utils.logging import get_logger, TALogger  # noqa: F401, E402
