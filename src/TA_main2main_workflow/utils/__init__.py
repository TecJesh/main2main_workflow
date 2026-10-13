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
# Newer upstream pin format: cmake/llvm-info.json (llvm_hash field).
# See utils/llvm_hash.py for format-agnostic helpers.
LLVM_INFO_FILE = "cmake/llvm-info.json"
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
LLVM20COMPAT_LOG_FILE = "llvm20compat.log"
LLVM20COMPAT_RESULT_FILE = "llvm20compat_result.json"
LLVM_CHANGE_ANALYSIS_DIR = "llvm_change_analysis"

# ═══════════════════════════════════════════════════════════════════════════
# Pipeline test (ta-pipeline-test) file names
# ═══════════════════════════════════════════════════════════════════════════

PIPELINE_TEST_STEP_ID = "pipeline-test"
PIPELINE_TEST_ANALYSIS_FILE = "analysis.json"
PIPELINE_TEST_OVERVIEW_XLSX = "错误信息总览表.xlsx"
PIPELINE_TEST_DETAIL_XLSX = "错误信息汇总表.xlsx"
PIPELINE_TEST_RUN_LOG = "pipeline_test_run.log"
PIPELINE_TEST_FIX_SUMMARIES = "fix_summaries.md"
PIPELINE_TEST_LOG_DIR_NAME = "test_log"
PIPELINE_TEST_ANALYSIS_DIR_NAME = "analysis"
PIPELINE_TEST_RERUN_PREFIX = "rerun_"

# Pipeline test problem categories (问题分类 column in the detail Excel).
PIPELINE_TEST_CATEGORY_OPERATOR = "operator"
PIPELINE_TEST_CATEGORY_TRITON_ASCEND = "triton_ascend"
PIPELINE_TEST_CATEGORY_NPU_IR = "npu_ir"

# Chinese labels used in the Excel reports (per spec).
PIPELINE_TEST_CATEGORY_LABELS = {
    PIPELINE_TEST_CATEGORY_OPERATOR: "算子问题",
    PIPELINE_TEST_CATEGORY_TRITON_ASCEND: "Triton—Ascend问题",
    PIPELINE_TEST_CATEGORY_NPU_IR: "NPU-IR问题",
}

# ═══════════════════════════════════════════════════════════════════════════
# Re-exports
# ═══════════════════════════════════════════════════════════════════════════

from TA_main2main_workflow.utils.config import TAConfig  # noqa: F401, E402
from TA_main2main_workflow.utils.context import WorkflowContext  # noqa: F401, E402
from TA_main2main_workflow.utils.git import run_git, run_git_no_check  # noqa: F401, E402
from TA_main2main_workflow.utils.llvm_hash import (  # noqa: F401, E402
    read_llvm_hash,
    llvm_hash_at_rev,
    LLVM_HASH_PATHS,
)
from TA_main2main_workflow.utils.logging import get_logger, TALogger  # noqa: F401, E402
