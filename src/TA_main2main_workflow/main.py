#!/usr/bin/env python3
"""CLI entrypoint for TA_main2main_workflow — Triton-Ascend upstream sync.

Single-step mode is the only supported mode. Each step runs the full
pipeline: merge → resolve conflicts → build → fix → test → fix → commit.

Environment variables:
  TRITON_ASCEND_PATH   — path to triton-ascend repo (default: cwd)
  TRITON_PATH          — path to upstream triton repo (default: uses remote)
  TRITON_TARGET_COMMIT — specific upstream commit to sync to (default: HEAD)
  AI_BACKEND           — "opencode" or "claude" (default: auto-detect)
  SKIP_AI_ANALYSIS     — set to "true" to skip AI calls
  SKIP_BUILD           — set to "true" to skip build step
  SKIP_E2E_TEST        — set to "true" to skip test step
  PUSH_TO_GITHUB       — set to "true" to auto-create PR after success
  GITHUB_REPO          — "owner/repo" for PR creation
  LLVM_INSTALL_PREFIX  — path to LLVM for building
  LLVM_PROJECT_PATH    — path to llvm-project repo (default: ~/llvm-project)
  LLVM_INSTALL_PREFIX_SYNC — path to LLVM install (default: ~/llvm-install-sync)
  CONDA_ENV            — conda env name (default: ta-upgrade)
  BUILD_PROCS          — number of parallel build workers (default: 32)
  TEST_PROCS           — number of parallel pytest workers (default: 8)
  TA_LINE_BUDGET       — max source lines per merge step (default: 1000)
  TA_MAX_RETRIES       — max AI fix retries (default: 10)
  TA_BASE_BRANCH       — base branch name (default: upstream_sync)
"""

import argparse
import os
import sys
from pathlib import Path

from TA_main2main_workflow.flow import TA_Main2MainFlow
from TA_main2main_workflow.utils import UpgradeFailed
from TA_main2main_workflow.utils.config import TAConfig
from TA_main2main_workflow.utils.logging import get_logger

log = get_logger(__name__)


def kickoff():
    parser = argparse.ArgumentParser(
        description="Triton-Ascend Main2Main Upstream Sync (Single-Step Mode)"
    )
    parser.add_argument(
        "--triton-ascend-path", default=None,
        help="Local path to the triton-ascend repository (default: TRITON_ASCEND_PATH env)"
    )
    parser.add_argument(
        "--triton-path", default=None,
        help="Local path to the upstream triton repository (default: TRITON_PATH env)"
    )
    parser.add_argument(
        "--target-commit", default=None,
        help="Upstream triton commit SHA to merge (default: upstream HEAD)"
    )
    parser.add_argument(
        "--llvm-prefix", default=None,
        help="LLVM install prefix path for building"
    )
    parser.add_argument(
        "--conda-env", default=None,
        help="Conda environment name (default: ta-upgrade)"
    )
    parser.add_argument(
        "--build-procs", type=int, default=None,
        help="Parallel build workers (default: 32)"
    )
    parser.add_argument(
        "--test-procs", type=int, default=None,
        help="Parallel pytest workers (default: 8)"
    )
    args = parser.parse_args()

    config = TAConfig.from_env()
    if args.triton_ascend_path:
        config.triton_ascend_path = args.triton_ascend_path
    if args.triton_path:
        config.triton_path = args.triton_path
    if args.target_commit:
        config.target_commit = args.target_commit
    if args.llvm_prefix:
        config.llvm_install_prefix = args.llvm_prefix
    if args.conda_env:
        config.conda_env = args.conda_env
    if args.build_procs is not None:
        config.build_procs = args.build_procs
    if args.test_procs is not None:
        config.test_procs = args.test_procs

    _print_banner(config)

    flow = TA_Main2MainFlow(config=config)
    try:
        result = flow.run()
    except Exception as exc:
        log.error(f"WORKFLOW CRASHED: {exc}")
        import traceback
        traceback.print_exc()
        sys.exit(1)

    if result == UpgradeFailed:
        log.error("WORKFLOW FAILED")
        sys.exit(1)

    log.info("WORKFLOW COMPLETED SUCCESSFULLY")


def _print_banner(config: TAConfig) -> None:
    ai = "NO (SKIP_AI_ANALYSIS=true)" if config.skip_ai_analysis else "YES"
    log.header("TA_main2main_workflow — Triton-Ascend Upstream Sync")
    log.key_value("AI Backend", config.ai_backend)
    log.key_value("AI Enabled", ai)
    log.key_value("Skip Build", str(config.skip_build))
    log.key_value("Skip Test", str(config.skip_e2e_test))
    if config.skip_ai_analysis:
        log.warning("SKIP_AI_ANALYSIS=true — AI will not be called!")


if __name__ == "__main__":
    kickoff()
