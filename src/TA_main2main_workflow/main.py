#!/usr/bin/env python3
"""CLI entrypoint for TA_main2main_workflow — Triton-Ascend upstream sync."""

import argparse
import sys

from TA_main2main_workflow.flow import TA_Main2MainFlow
from TA_main2main_workflow.utils.config import TAConfig
from TA_main2main_workflow.utils.logging import get_logger
from TA_main2main_workflow.utils import UpgradeFailed

log = get_logger(__name__)


def kickoff():
    parser = argparse.ArgumentParser(description="Triton-Ascend Main2Main Upstream Sync")
    parser.add_argument("--triton-ascend-path", default=None, help="Path to triton-ascend repo")
    parser.add_argument("--triton-path", default=None, help="Path to local triton repo (offline mode)")
    parser.add_argument("--target-commit", default=None, help="Upstream commit SHA to merge")
    parser.add_argument("--llvm-prefix", default=None, help="LLVM install prefix path")
    parser.add_argument("--build-procs", type=int, default=None, help="Parallel build workers")
    parser.add_argument("--test-procs", type=int, default=None, help="Parallel pytest workers")
    args = parser.parse_args()

    config = TAConfig.from_env()
    if args.triton_ascend_path: config.triton_ascend_path = args.triton_ascend_path
    if args.triton_path: config.triton_path = args.triton_path
    if args.target_commit: config.target_commit = args.target_commit
    if args.llvm_prefix: config.llvm_install_prefix = args.llvm_prefix
    if args.build_procs is not None: config.build_procs = args.build_procs
    if args.test_procs is not None: config.test_procs = args.test_procs

    _print_banner(config)

    flow = TA_Main2MainFlow(config=config)
    try:
        result = flow.run()
    except Exception as exc:
        log.error(f"WORKFLOW CRASHED: {exc}")
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
