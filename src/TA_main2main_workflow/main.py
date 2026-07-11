#!/usr/bin/env python3
"""CLI entrypoint for TA_main2main_workflow — Triton-Ascend upstream sync.

Commands:
  ta-kickoff    Run the main2main sync flow (all output printed locally)
  ta-plot       Generate a flow diagram (HTML)

Environment variables:
  TRITON_ASCEND_PATH   — path to triton-ascend repo (default: cwd)
  TRITON_PATH          — path to upstream triton repo (default: uses remote)
  TRITON_TARGET_COMMIT — specific upstream commit to sync to (default: HEAD)
  AI_BACKEND           — "opencode" or "claude" (default: auto-detect)
  SKIP_AI_ANALYSIS     — set to "true" to skip AI (NOT recommended)
  SKIP_BUILD           — set to "true" to skip build step
  SKIP_E2E_TEST        — set to "true" to skip test step
  PUSH_TO_GITHUB       — set to "true" to auto-create PR after success
  GITHUB_REPO          — "owner/repo" for PR creation
  LLVM_INSTALL_PREFIX  — path to LLVM for building
  CONDA_ENV            — conda env name (default: ta-upgrade)
  NUM_PROCS            — number of parallel pytest workers (default: 16)

  TA_MODE              — Execution mode:
    full (default)       Complete flow: merge → build → test → fix → PR
    merge                Merge + AI resolve only, then push work branch & exit.
                         Used by CI: runs on ubuntu-latest, then triggers NPU tests.
    fix                  AI fix only on an existing work branch. Requires:
                           TA_WORK_BRANCH  — work branch name
                           TA_ERROR_LOGS_PATH — path to test failure logs (optional)
                           TA_FIX_ATTEMPT — retry attempt number (optional)
"""

import argparse
import os
import sys
from pathlib import Path

from TA_main2main_workflow.flow import TA_Main2MainFlow
from TA_main2main_workflow.utils import UpgradeFailed


def _print_startup_banner() -> None:
    skip_ai = os.getenv("SKIP_AI_ANALYSIS", "false").lower() == "true"
    skip_build = os.getenv("SKIP_BUILD", "false").lower() == "true"
    skip_test = os.getenv("SKIP_E2E_TEST", "false").lower() == "true"
    ai_backend = os.getenv("AI_BACKEND", "auto-detect")
    mode = os.getenv("TA_MODE", "full")

    print(f"╔{'═' * 60}╗")
    print(f"║  TA_main2main_workflow — Triton-Ascend Upstream Sync       ║")
    print(f"╠{'═' * 60}╣")
    print(f"║  Mode:          {mode:<44}║")
    print(f"║  AI Backend:    {ai_backend:<44}║")
    print(f"║  AI Enabled:    {'YES' if not skip_ai else 'NO (SKIP_AI_ANALYSIS=true)':<44}║")
    print(f"║  Skip Build:    {str(skip_build):<44}║")
    print(f"║  Skip Test:     {str(skip_test):<44}║")
    print(f"╚{'═' * 60}╝")

    if skip_ai:
        print()
        print("  ⚠  WARNING: SKIP_AI_ANALYSIS=true")
        print("  ⚠  AI will NOT be called to resolve conflicts or fix failures!")
        print("  ⚠  You must resolve conflicts and fix test failures manually.")
        print()


def _is_failed(result) -> bool:
    """Check whether a kickoff result indicates workflow failure.

    Handles both plain string returns (merge/fix modes) and CrewAI
    CrewOutput objects (full mode).
    """
    if result is None:
        return False
    if isinstance(result, str):
        return result == UpgradeFailed
    # CrewAI CrewOutput / object with raw attribute
    if hasattr(result, 'raw'):
        return str(result.raw) == UpgradeFailed
    # Last resort: string representation
    return str(result) == UpgradeFailed


def kickoff():
    parser = argparse.ArgumentParser(
        description="Triton-Ascend Main2Main Upstream Sync Flow"
    )
    parser.add_argument(
        "--mode", default=None,
        choices=["full", "merge", "fix"],
        help="Execution mode: full (default), merge (merge+resolve only), "
             "fix (AI fix on existing work branch). "
             "Can also be set via TA_MODE env var."
    )
    parser.add_argument(
        "--work-branch", default=None,
        help="Work branch name (required for --mode=fix). "
             "Can also be set via TA_WORK_BRANCH env var."
    )
    parser.add_argument(
        "--error-logs-path", default=None,
        help="Path to test failure logs for AI fix (--mode=fix). "
             "Can also be set via TA_ERROR_LOGS_PATH env var."
    )
    parser.add_argument(
        "--fix-attempt", type=int, default=None,
        help="Retry attempt number (--mode=fix). "
             "Can also be set via TA_FIX_ATTEMPT env var."
    )
    parser.add_argument(
        "--triton-ascend-path", default=None,
        help="Local path to the triton-ascend repository (default: current directory)"
    )
    parser.add_argument(
        "--triton-path", default=None,
        help="Local path to the upstream triton repository (default: uses remote)"
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
        "--num-procs", type=int, default=None,
        help="Number of parallel pytest workers (default: 16)"
    )
    args = parser.parse_args()

    # ── Mode: CLI arg takes precedence over env var ──
    if args.mode:
        os.environ["TA_MODE"] = args.mode
    if args.work_branch:
        os.environ["TA_WORK_BRANCH"] = args.work_branch
    if args.error_logs_path:
        os.environ["TA_ERROR_LOGS_PATH"] = args.error_logs_path
    if args.fix_attempt is not None:
        os.environ["TA_FIX_ATTEMPT"] = str(args.fix_attempt)

    _print_startup_banner()

    inputs = {}
    if args.triton_ascend_path:
        inputs["triton_ascend_path"] = args.triton_ascend_path
    if args.triton_path:
        inputs["triton_path"] = args.triton_path
    if args.target_commit:
        inputs["target_commit"] = args.target_commit
    if args.llvm_prefix:
        inputs["llvm_prefix"] = args.llvm_prefix
    if args.conda_env:
        inputs["conda_env"] = args.conda_env
    if args.num_procs:
        inputs["num_procs"] = args.num_procs

    flow = TA_Main2MainFlow()
    try:
        result = flow.kickoff(inputs=inputs if inputs else None)
    except Exception as exc:
        print(f"\n{'=' * 60}")
        print(f"  WORKFLOW CRASHED: {exc}")
        print(f"{'=' * 60}")
        sys.exit(1)

    if _is_failed(result):
        print(f"\n{'=' * 60}")
        print(f"  WORKFLOW FAILED — exiting with code 1")
        print(f"{'=' * 60}")
        sys.exit(1)

    print(f"\n{'=' * 60}")
    print(f"  WORKFLOW COMPLETED SUCCESSFULLY")
    print(f"{'=' * 60}")


def plot():
    import shutil

    output_dir = Path(__file__).resolve().parent / "output"
    output_dir.mkdir(parents=True, exist_ok=True)
    flow = TA_Main2MainFlow()
    tmp_html = Path(flow.plot(filename="flow.html", show=False))
    for f in tmp_html.parent.iterdir():
        shutil.copy2(f, output_dir / f.name)
    print(f"Flow plot saved to: {output_dir / tmp_html.name}")


if __name__ == "__main__":
    kickoff()
