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
"""

import argparse
import os
from pathlib import Path

from TA_main2main_workflow.flow import TA_Main2MainFlow


def _print_startup_banner() -> None:
    skip_ai = os.getenv("SKIP_AI_ANALYSIS", "false").lower() == "true"
    skip_build = os.getenv("SKIP_BUILD", "false").lower() == "true"
    skip_test = os.getenv("SKIP_E2E_TEST", "false").lower() == "true"
    ai_backend = os.getenv("AI_BACKEND", "auto-detect")

    print(f"╔{'═' * 60}╗")
    print(f"║  TA_main2main_workflow — Triton-Ascend Upstream Sync       ║")
    print(f"╠{'═' * 60}╣")
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


def kickoff():
    parser = argparse.ArgumentParser(
        description="Triton-Ascend Main2Main Upstream Sync Flow"
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
    flow.kickoff(inputs=inputs if inputs else None)


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
