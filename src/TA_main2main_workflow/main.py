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
  TEST_PROCS           — number of parallel pytest workers (default: 16)
  TA_LINE_BUDGET       — max source lines per merge step (default: 1000)
  TA_MAX_RETRIES       — max AI fix retries (default: 10)
  TA_BASE_BRANCH       — base branch name (default: upstream_sync)
  TA_TEST_DIR          — primary test directory (default: third_party/ascend/unittest/pytest_ut)
  TA_EXTRA_TEST_DIRS   — extra test directories, comma/space separated (default: none)
  TA_TEST_COMMAND      — additional custom test command (default: none; runs after pytest ut)
  TA_SINGLE_STEP_MODE  — set to "true" for single-step mode (default: true)
  TA_WORK_BRANCH_BASE  — remote name for work branch base (default: upstream-ascend)
  TA_RESUME            — set to "true" to resume from cached step outputs
  TA_MERGE_MODE        — "upstream" (default) merges triton-upstream/main;
                         "ta_main" merges TA origin/main evolution back into
                         the work branch (conflicts favor incoming TA code,
                         third_party/ascend/patch/triton-ascend-*.patch files
                         are adjusted and applied for build/test)
  TA_MAIN_BRANCH       — branch on origin merged in ta_main mode (default: main)
  TA_SOURCE_PATCHES    — comma/space separated patch files under
                         third_party/ascend/patch/ managed by the workflow
                         (default: triton-ascend-3.7.0.patch,
                          triton-ascend-dev-3.7.0.patch,
                          npuir_adapter_to_llvm_23.patch)
  TA_SKIP_TEST_KEYWORDS — comma/space separated pytest -k exclusion
                         keywords (default: topk — skips test_topk.py)
"""

import argparse
import sys

from TA_main2main_workflow.flow import TA_Main2MainFlow
from TA_main2main_workflow.utils import UpgradeFailed
from TA_main2main_workflow.utils.config import TAConfig, _resolve_test_dirs
from TA_main2main_workflow.utils.logging import get_logger

log = get_logger(__name__)


def kickoff():
    parser = argparse.ArgumentParser(
        description="Triton-Ascend Main2Main Upstream Sync (Single-Step Mode)"
    )
    parser.add_argument(
        "--triton-ascend-path",
        default=None,
        help="Local path to the triton-ascend repository (default: TRITON_ASCEND_PATH env)",
    )
    parser.add_argument(
        "--triton-path",
        default=None,
        help="Local path to the upstream triton repository (default: TRITON_PATH env)",
    )
    parser.add_argument(
        "--target-commit",
        default=None,
        help="Upstream triton commit SHA to merge (default: upstream HEAD)",
    )
    parser.add_argument(
        "--llvm-prefix", default=None, help="LLVM install prefix path for building"
    )
    parser.add_argument(
        "--conda-env", default=None, help="Conda environment name (default: ta-upgrade)"
    )
    parser.add_argument(
        "--build-procs",
        type=int,
        default=None,
        help="Parallel build workers (default: 32)",
    )
    parser.add_argument(
        "--test-procs",
        type=int,
        default=None,
        help="Parallel pytest workers (default: 8)",
    )
    parser.add_argument(
        "--extra-test-dirs",
        default=None,
        help="Extra test directories (comma/space separated, appended to default pytest ut)",
    )
    parser.add_argument(
        "--merge-mode",
        choices=["upstream", "ta_main"],
        default=None,
        help="Merge mode: upstream (default) or ta_main (merge TA main evolution)",
    )
    parser.add_argument(
        "--test-command",
        default=None,
        help="Additional custom test command (runs after pytest ut)",
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
    if args.extra_test_dirs is not None:
        config.test_dirs = _resolve_test_dirs(
            primary=config.test_dir,
            extra=args.extra_test_dirs,
        )
    if args.test_command is not None:
        config.test_command = args.test_command
    if args.merge_mode:
        config.merge_mode = args.merge_mode

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
