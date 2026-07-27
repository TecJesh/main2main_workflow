#!/usr/bin/env python3
"""CLI entrypoint for TA_main2main_workflow -- Triton-Ascend upstream sync.

Commands:
  ta-kickoff    Run the main2main sync flow

Configuration is via environment variables (see TAConfig.from_env() for
the full list).  CLI args override env vars for common options.

Key environment variables:
  TRITON_ASCEND_PATH     -- path to triton-ascend repo
  TRITON_PATH            -- path to upstream triton repo
  TRITON_TARGET_COMMIT   -- specific upstream commit to sync to
  TA_MAX_RETRIES         -- max AI fix retries (default: 10)
  SKIP_AI_ANALYSIS       -- set to "true" to skip AI
  SKIP_BUILD             -- set to "true" to skip build
  SKIP_E2E_TEST          -- set to "true" to skip tests
  PUSH_TO_GITHUB         -- set to "true" to auto-create PR
  NUM_PROCS              -- pytest parallel workers (default: 16)
  LLVM_PROJECT_PATH      -- path to llvm-project repo
  LLVM_INSTALL_PREFIX_SYNC -- LLVM install prefix
  TA_BASE_BRANCH         -- base branch for sync (default: upstream_sync)
"""

import argparse
import sys

from TA_main2main_workflow.flow import TA_Main2MainFlow
from TA_main2main_workflow.utils.config import TAConfig
from TA_main2main_workflow.utils import UpgradeFailed


def _print_banner(config: TAConfig) -> None:
    ai = "NO (SKIP_AI_ANALYSIS=true)" if config.skip_ai_analysis else "YES"
    print(f"╔{'═' * 60}╗")
    print(f"║  TA_main2main_workflow -- Triton-Ascend Upstream Sync       ║")
    print(f"╠{'═' * 60}╣")
    print(f"║  AI Backend:    {config.ai_backend:<44}║")
    print(f"║  AI Enabled:    {ai:<44}║")
    print(f"║  Skip Build:    {str(config.skip_build):<44}║")
    print(f"║  Skip Test:     {str(config.skip_e2e_test):<44}║")
    print(f"║  Max Retries:   {config.max_retries:<44}║")
    print(f"╚{'═' * 60}╝")
    if config.skip_ai_analysis:
        print()
        print("  ⚠  WARNING: SKIP_AI_ANALYSIS=true -- AI will NOT be called!")


def kickoff():
    parser = argparse.ArgumentParser(
        description="Triton-Ascend Main2Main Upstream Sync Flow")
    parser.add_argument(
        "--triton-ascend-path", default=None,
        help="Local path to the triton-ascend repository")
    parser.add_argument(
        "--target-commit", default=None,
        help="Upstream triton commit SHA to merge (default: upstream HEAD)")
    parser.add_argument(
        "--llvm-prefix", default=None,
        help="LLVM install prefix path for building (LLVM_INSTALL_PREFIX_SYNC)")
    parser.add_argument(
        "--max-retries", type=int, default=None,
        help="Max AI fix retries (default: 10)")
    parser.add_argument(
        "--num-procs", type=int, default=None,
        help="Number of parallel pytest workers (default: 16)")
    args = parser.parse_args()

    # Build config from env, then overlay CLI args
    config = TAConfig.from_env()
    if args.triton_ascend_path:
        config.triton_ascend_path = args.triton_ascend_path
    if args.target_commit:
        config.target_commit = args.target_commit
    if args.llvm_prefix:
        config.llvm_install_prefix_sync = args.llvm_prefix
    if args.max_retries is not None:
        config.max_retries = args.max_retries
    if args.num_procs is not None:
        config.test_procs = args.num_procs

    _print_banner(config)

    flow = TA_Main2MainFlow(config=config)
    try:
        result = flow.run()
    except Exception as exc:
        print(f"\n{'=' * 60}")
        print(f"  WORKFLOW CRASHED: {exc}")
        print(f"{'=' * 60}")
        import traceback
        traceback.print_exc()
        sys.exit(1)

    if result == UpgradeFailed:
        print(f"\n{'=' * 60}")
        print(f"  WORKFLOW FAILED -- exiting with code 1")
        print(f"{'=' * 60}")
        sys.exit(1)

    print(f"\n{'=' * 60}")
    print(f"  WORKFLOW COMPLETED SUCCESSFULLY")
    print(f"{'=' * 60}")


if __name__ == "__main__":
    kickoff()
