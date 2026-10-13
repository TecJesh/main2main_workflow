#!/usr/bin/env python3
"""CLI entrypoint for the full pipeline test (ta-pipeline-test).

Independent of the main2main sync flow.  Runs a human-configured test
script (TA_PIPELINE_TEST_SCRIPT) that clones external test repos into
~/test_workspace and runs pytest; the workflow then analyzes the logs
with AI, produces two Excel reports, fixes failing cases one by one,
and opens a PR when code changes were required.

Environment variables:
  TRITON_ASCEND_PATH           — path to triton-ascend repo (required)
  TA_PIPELINE_TEST_SCRIPT      — path to the test script (required)
  TA_TEST_WORKSPACE            — test workspace dir (default: ~/test_workspace)
  TA_PIPELINE_TEST_MAX_RETRIES — per-case fix attempts (default: 5)
  TA_PIPELINE_TEST_ROUNDS      — full-test rerun rounds (default: 3)
  TA_PIPELINE_TEST_TIMEOUT     — script timeout, seconds (default: 28800)
  TA_PIPELINE_TEST_PR_TITLE    — override PR title
  AI_BACKEND / SKIP_AI_ANALYSIS / PUSH_TO_GITHUB / GITHUB_REPO —
      shared with the main flow
"""

import argparse
import sys

from TA_main2main_workflow.pipeline_test.flow import PipelineTestFlow
from TA_main2main_workflow.utils.config import TAConfig
from TA_main2main_workflow.utils.logging import get_logger

log = get_logger(__name__)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Triton-Ascend Full Pipeline Test (ta-pipeline-test)"
    )
    parser.add_argument(
        "--triton-ascend-path",
        default=None,
        help=(
            "Local path to the triton-ascend repository "
            "(default: TRITON_ASCEND_PATH env)"
        ),
    )
    parser.add_argument(
        "--test-script",
        default=None,
        help="Path to the test script (default: TA_PIPELINE_TEST_SCRIPT env)",
    )
    parser.add_argument(
        "--test-workspace",
        default=None,
        help=(
            "Test workspace directory (default: TA_TEST_WORKSPACE env, "
            "~/test_workspace)"
        ),
    )
    parser.add_argument(
        "--max-retries",
        type=int,
        default=None,
        help="Per-case fix attempts (default: 5)",
    )
    parser.add_argument(
        "--rounds",
        type=int,
        default=None,
        help="Full-test rerun rounds (default: 3)",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=None,
        help="Test script timeout in seconds (default: 28800)",
    )
    args = parser.parse_args()

    config = TAConfig.from_env()
    if args.triton_ascend_path:
        config.triton_ascend_path = args.triton_ascend_path
    if args.test_script:
        config.pipeline_test_script = args.test_script
    if args.test_workspace:
        config.test_workspace = args.test_workspace
    if args.max_retries is not None:
        config.pipeline_test_max_retries = args.max_retries
    if args.rounds is not None:
        config.pipeline_test_rounds = args.rounds
    if args.timeout is not None:
        config.pipeline_test_timeout = args.timeout

    _print_banner(config)

    flow = PipelineTestFlow(config=config)
    try:
        exit_code = flow.run()
    except Exception as exc:
        log.error(f"PIPELINE TEST CRASHED: {exc}")
        import traceback

        traceback.print_exc()
        sys.exit(1)

    if exit_code != 0:
        log.error("PIPELINE TEST FINISHED WITH FAILURES")
        sys.exit(exit_code)

    log.info("PIPELINE TEST COMPLETED SUCCESSFULLY")


def _print_banner(config: TAConfig) -> None:
    ai = "NO (SKIP_AI_ANALYSIS=true)" if config.skip_ai_analysis else "YES"
    log.header("TA_main2main_workflow — Full Pipeline Test")
    log.key_value("triton-ascend", config.triton_ascend_path)
    log.key_value("test script", config.pipeline_test_script)
    log.key_value("test workspace", str(config.test_workspace_dir))
    log.key_value("AI Backend", config.ai_backend)
    log.key_value("AI Enabled", ai)
    log.key_value("Per-case retries", str(config.pipeline_test_max_retries))
    log.key_value("Rounds", str(config.pipeline_test_rounds))
    log.key_value("Push PR", str(config.push_to_github))
    if config.skip_ai_analysis:
        log.warning("SKIP_AI_ANALYSIS=true — AI will not be called!")


if __name__ == "__main__":
    main()
