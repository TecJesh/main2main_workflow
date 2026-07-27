"""TA_main2main_workflow utilities — config, context, git, logging, tracking."""

from __future__ import annotations

import os
from pathlib import Path

# ── Workspace path ─────────────────────────────────────────────────────────
WORKSPACE_DIR = Path(os.getenv("TA_MAIN2MAIN_WORKSPACE", str(Path.cwd() / "workspace")))

# ── Flow routing signals ───────────────────────────────────────────────────
UpgradeCompleted = "UpgradeCompleted"
UpgradeFailed = "UpgradeFailed"

# ── Constants ──────────────────────────────────────────────────────────────
LLVM_HASH_FILE = "cmake/llvm-hash.txt"
ENV_BASE_BRANCH = "TA_BASE_BRANCH"
ENV_SINGLE_STEP_MODE = "ENV_SINGLE_STEP_MODE"
IR_MAX_ITERATIONS = 10  # max retries for patch apply/rebuild
LLVM_CHANGE_ANALYSIS_DIR = "llvm_change_analysis"

# ── Output file names ──────────────────────────────────────────────────────
DETECT_FILE = "detect.json"
STEPS_FILE = "steps.json"
BUILD_RESULT_FILE = "build_result.json"
BUILD_LOG_FILE = "build.log"
TEST_RESULT_FILE = "test_result.json"
FIX_LOG_DIR = "fixes"
STEPS_DIR = "steps"
FINAL_SUMMARY_FILE = "final_summary.md"
FINAL_TARGET_PATCH_FILE = "final_target.patch"
PRE_CI_CHECK_FILE = "pre_ci_check.json"
EACH_STEP_SUMMARY_FILE = "step_summary.md"

# IR analysis constants
IR_ANALYSIS_DIR = "ir-analysis"
IR_OPS_REPORT_FILE = "ops_report.json"
IR_CHANGES_REPORT_FILE = "changes_report.json"
IR_DIAGNOSIS_FILE = "ir_diagnosis.json"

# ── Helpers ────────────────────────────────────────────────────────────────


def get_base_branch_ref(remote: str = "origin") -> str:
    branch = os.getenv(ENV_BASE_BRANCH, "upstream_sync")
    return f"{remote}/{branch}"


# ── Re-exports ─────────────────────────────────────────────────────────────
from TA_main2main_workflow.utils.config import TAConfig
from TA_main2main_workflow.utils.context import WorkflowContext
from TA_main2main_workflow.utils.git import (
    run_git, run_git_no_check, submodule_has_changes, commit_submodule)
from TA_main2main_workflow.utils.logging import get_logger, TALogger
from TA_main2main_workflow.utils.submodule import push_submodule
