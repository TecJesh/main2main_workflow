#!/usr/bin/env python3
"""Push the sync branch and create a GitHub Pull Request for triton-ascend.

Steps:
  1. Ensure gh CLI is authenticated.
  2. Generate cumulative patch (git diff from original base).
  3. Commit changes if there are uncommitted modifications.
  4. Push the work branch to origin.
  5. Open a PR via gh pr create.

Environment variables:
  PUSH_TO_GITHUB  — must be "true" to proceed
  GITHUB_REPO     — target repo "owner/name" (default: triton-lang/triton-ascend)
  GH_TOKEN        — GitHub Personal Access Token (CI fallback)
"""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from TA_main2main_workflow.utils import (
    WORKSPACE_DIR, FINAL_TARGET_PATCH_FILE, FINAL_SUMMARY_FILE,
    run_git,
)


def _detect_default_branch(repo: Path, remote: str = "origin") -> str:
    """Detect the default branch of the remote."""
    try:
        ref = run_git(repo, "symbolic-ref", f"refs/remotes/{remote}/HEAD").strip()
        return ref.rsplit("/", 1)[-1]
    except subprocess.CalledProcessError:
        return "main"


def _ensure_gh_auth(repo: Path) -> None:
    """Ensure GitHub CLI is authenticated."""
    try:
        subprocess.run(
            ["gh", "auth", "status"],
            check=True, capture_output=True, text=True,
        )
        print("[push] gh CLI already authenticated.")
    except subprocess.CalledProcessError:
        gh_token = os.getenv("GH_TOKEN", "")
        if not gh_token:
            print(
                "[push] gh not authenticated and GH_TOKEN not set. "
                "Run 'gh auth login' locally or set GH_TOKEN in CI.",
                file=sys.stderr,
            )
            sys.exit(1)
        print("[push] Authenticating gh CLI with GH_TOKEN...")
        subprocess.run(
            ["gh", "auth", "login", "--with-token"],
            input=gh_token, check=True, capture_output=True, text=True,
        )
        print("[push] gh CLI authenticated via GH_TOKEN.")

    run_git(repo, "config", "credential.helper", "!gh auth git-credential")
    print("[push] Git credential helper configured.")


def push_and_create_pr(
    ascend_path: Path,
    github_repo: str = "triton-lang/triton-ascend",
    work_branch: str = "",
    summary_path: Path | None = None,
) -> str:
    """Push the current work branch and create a GitHub PR.

    Returns the PR URL, or "" on skip/failure.
    """
    repo = Path(ascend_path)

    if not work_branch:
        work_branch = run_git(repo, "branch", "--show-current").strip()

    try:
        base_ref = run_git(repo, "merge-base", "origin/main", "HEAD").strip()
    except subprocess.CalledProcessError:
        base_ref = "HEAD~1"

    patch_content = run_git(repo, "diff", base_ref, "HEAD")
    patch_path = WORKSPACE_DIR / FINAL_TARGET_PATCH_FILE
    patch_path.write_text(patch_content, encoding="utf-8")
    print(f"[push] Cumulative patch written to {patch_path}")

    summary_file = summary_path or (WORKSPACE_DIR / FINAL_SUMMARY_FILE)
    if not summary_file.exists():
        summary_file.write_text(
            f"# Triton-Ascend Upstream Sync\n\n"
            f"Branch: `{work_branch}`\n"
            f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n",
            encoding="utf-8",
        )

    _ensure_gh_auth(repo)

    status = run_git(repo, "status", "--porcelain").strip()
    if status:
        print("[push] Staging uncommitted changes...")
        # Use "git add -u" (tracked-only) to avoid staging test artifacts,
        # cache files, or other transient files created during the flow.
        run_git(repo, "add", "-u")
        commit_msg = f"sync: upstream triton merge ({datetime.now().strftime('%Y%m%d-%H%M%S')})"
        try:
            run_git(repo, "commit", "-s", "-m", commit_msg)
            print(f"[push] Committed: {commit_msg}")
        except subprocess.CalledProcessError:
            print("[push] Nothing to commit (already clean)")

    print(f"[push] Pushing branch '{work_branch}' to origin...")
    run_git(repo, "push", "-u", "origin", work_branch)

    base_branch = _detect_default_branch(repo)
    pr_description = summary_file.read_text(encoding="utf-8") if summary_file.exists() else ""

    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    pr_title = f"sync: upstream triton merge ({ts})"

    print(f"[push] Creating PR: {pr_title}")
    gh_cmd = [
        "gh", "pr", "create",
        "--title", pr_title,
        "--body", pr_description,
        "--head", work_branch,
        "--base", base_branch,
        "--repo", github_repo,
    ]

    result = subprocess.run(
        gh_cmd, check=True, capture_output=True, text=True, cwd=str(repo)
    )
    pr_url = result.stdout.strip()
    print(f"[push] PR created: {pr_url}")
    return pr_url
