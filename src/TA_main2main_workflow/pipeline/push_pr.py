"""Pipeline step: Push to GitHub and create PR."""

from __future__ import annotations
import os, subprocess
from pathlib import Path
from TA_main2main_workflow.utils.config import TAConfig
from TA_main2main_workflow.utils.context import WorkflowContext
from TA_main2main_workflow.utils.logging import get_logger
from TA_main2main_workflow.utils.git import run_git
from TA_main2main_workflow.utils import WORKSPACE_DIR

log = get_logger(__name__)


def push_and_create_pr(ctx: WorkflowContext, config: TAConfig) -> str:
    """Push work branch and create GitHub PR. Returns PR URL."""
    ascend_path = Path(ctx.triton_ascend_path)
    branch = ctx.work_branch
    repo = config.github_repo
    base = config.pr_base_branch
    target_short = ctx.target_commit[:12]

    if config.gh_token:
        os.environ["GH_TOKEN"] = config.gh_token
    if "GH_HOST" not in os.environ:
        os.environ["GH_HOST"] = "github.com"

    log.info(f"Pushing {branch} ...")
    run_git(ascend_path, "push", "-u", "origin", branch)

    title = f"[Sync] Merge upstream {target_short}"
    pr_body_path = WORKSPACE_DIR / "pr_body.md"
    body = pr_body_path.read_text(encoding="utf-8") if pr_body_path.exists() else ""

    log.info(f"Creating PR: {title}")
    for attempt in range(1, 4):
        try:
            result = subprocess.run(
                ["gh", "pr", "create",
                 "--base", base, "--head", branch,
                 "--title", title, "--body", body,
                 "--repo", repo],
                capture_output=True, text=True, timeout=60)
            if result.returncode == 0:
                pr_url = result.stdout.strip()
                log.status(True, f"PR created: {pr_url}")
                return pr_url
            log.warning(f"gh pr create failed (attempt {attempt}/3): {result.stderr.strip()[-200:]}")
        except Exception as e:
            log.warning(f"gh pr create failed (attempt {attempt}/3): {e}")
    raise RuntimeError("Failed to create PR after 3 attempts")
