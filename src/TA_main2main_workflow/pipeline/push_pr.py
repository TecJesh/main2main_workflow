"""Pipeline step: Push work branch and create GitHub PR.

Uses ``gh`` CLI for PR creation with fallback to GitHub REST API.
Includes retry logic for both push and PR creation.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

from TA_main2main_workflow.utils.logging import get_logger
from TA_main2main_workflow.utils.git import run_git, run_git_no_check
from TA_main2main_workflow.utils.submodule import push_submodule

log = get_logger(__name__)

_MAX_PUSH_RETRIES = 5
_MAX_PR_RETRIES = 5
_RETRY_DELAY = 3


def push_and_create_pr(
    ascend_path: str | Path,
    github_repo: str,
    summary_path: str | Path | None = None,
    target_commit: str = "",
    work_branch: str = "",
) -> str:
    """Push work branch and create/update a GitHub PR.

    Returns the PR URL on success.

    Raises RuntimeError if push or PR creation fails after all retries.
    """
    ascend_path = Path(ascend_path)
    summary_path = Path(summary_path) if summary_path else None

    # ── 0. Push submodule first ────────────────────────────────────
    push_submodule(ascend_path)

    # ── 1. Determine branch ────────────────────────────────────────
    branch = work_branch or run_git(ascend_path, "branch", "--show-current").strip()
    log.info(f"Pushing branch: {branch}")

    # ── 2. Ensure gh auth ──────────────────────────────────────────
    _ensure_gh_auth(ascend_path)

    # ── 3. Push with retries ───────────────────────────────────────
    _push_with_retry(ascend_path, branch)

    # ── 4. Create PR with retries ──────────────────────────────────
    pr_url = _create_pr_with_retry(
        ascend_path, github_repo, branch, summary_path, target_commit
    )

    return pr_url


# ═══════════════════════════════════════════════════════════════════════════
# Internal
# ═══════════════════════════════════════════════════════════════════════════


def _ensure_gh_auth(repo: Path) -> None:
    """Ensure gh CLI is authenticated.

    Tries ``gh auth login --with-token`` using GH_TOKEN, and also embeds
    the token in the origin URL as a fallback.
    """
    token = os.getenv("GH_TOKEN", "") or os.getenv("GITHUB_TOKEN", "")
    if not token:
        log.warning("No GH_TOKEN set — push/PR may fail")

    # Try gh auth login explicitly against github.com
    if token:
        try:
            subprocess.run(
                ["gh", "auth", "login", "--with-token", "--hostname", "github.com"],
                input=token.encode(), capture_output=True, timeout=30,
            )
            log.info("gh auth login OK")
        except Exception:
            pass

        # Configure git credential helper for github.com
        try:
            subprocess.run(
                ["gh", "auth", "setup-git", "--hostname", "github.com"],
                capture_output=True, text=True, timeout=30,
            )
            log.info("gh auth setup-git OK")
        except Exception:
            pass

    # Embed token in origin URL as fallback (for push through proxy)
    if token:
        try:
            origin_url = run_git(repo, "remote", "get-url", "origin").strip()
            if origin_url.startswith("https://"):
                clean_url = origin_url.replace("https://", "", 1)
                if "@" in clean_url:
                    clean_url = clean_url.split("@", 1)[1]
                new_url = f"https://x-access-token:{token}@{clean_url}"
                run_git(repo, "remote", "set-url", "origin", new_url)
                safe = f"https://x-access-token:***@{clean_url}"
                log.info(f"origin URL rewritten with token: {safe}")
        except Exception as e:
            log.warning(f"Could not rewrite origin URL: {e}")


def _push_with_retry(repo: Path, branch: str) -> None:
    """Push branch with retry logic."""
    for attempt in range(1, _MAX_PUSH_RETRIES + 1):
        log.info(f"Push attempt {attempt}/{_MAX_PUSH_RETRIES}...")
        try:
            run_git(repo, "push", "--force-with-lease", "origin", branch)
            log.status(True, f"Pushed {branch}")
            return
        except Exception as e:
            log.warning(f"Push failed (attempt {attempt}): {e}")
            if attempt < _MAX_PUSH_RETRIES:
                time.sleep(_RETRY_DELAY)
    raise RuntimeError(f"Push failed after {_MAX_PUSH_RETRIES} attempts")


def _create_pr_with_retry(
    repo: Path, github_repo: str, branch: str,
    summary_path: Path | None, target_commit: str,
) -> str:
    """Create PR via gh CLI, with fallback to REST API."""
    pr_body = ""
    if summary_path and summary_path.exists():
        pr_body = summary_path.read_text(encoding="utf-8")

    title = f"sync: upstream triton merge {target_commit[:12]}" if target_commit else \
            f"sync: upstream triton merge — {branch}"

    # Try gh CLI first
    for attempt in range(1, _MAX_PR_RETRIES + 1):
        log.info(f"PR creation attempt {attempt}/{_MAX_PR_RETRIES} via gh CLI...")
        try:
            cmd = [
                "gh", "pr", "create",
                "--repo", github_repo,
                "--head", branch,
                "--base", "main",
                "--title", title,
            ]
            if pr_body:
                cmd.extend(["--body", pr_body])

            result = subprocess.run(
                cmd, cwd=repo, capture_output=True, text=True, timeout=60,
            )
            if result.returncode == 0:
                pr_url = result.stdout.strip()
                log.status(True, f"PR created: {pr_url}")
                return pr_url

            log.warning(f"gh pr create failed: {result.stderr.strip()}")
        except Exception as e:
            log.warning(f"gh CLI error: {e}")

        if attempt < _MAX_PR_RETRIES:
            time.sleep(_RETRY_DELAY)

    # Fallback: GitHub REST API
    log.info("Falling back to GitHub REST API...")
    try:
        return _create_pr_via_api(github_repo, branch, title, pr_body)
    except Exception as e:
        raise RuntimeError(f"PR creation failed after all attempts: {e}")


def _create_pr_via_api(
    github_repo: str, head: str, title: str, body: str = "",
) -> str:
    """Create PR via GitHub REST API (fallback)."""
    token = os.getenv("GH_TOKEN", "") or os.getenv("GITHUB_TOKEN", "")
    if not token:
        raise RuntimeError("No GH_TOKEN or GITHUB_TOKEN set")

    data = {
        "title": title,
        "head": head,
        "base": "main",
        "body": body or f"🤖 Generated with [Claude Code](https://claude.com/claude-code)",
    }

    url = f"https://api.github.com/repos/{github_repo}/pulls"
    cmd = [
        "curl", "-s", "-X", "POST", url,
        "-H", f"Authorization: token {token}",
        "-H", "Accept: application/vnd.github.v3+json",
        "-H", "Content-Type: application/json",
        "-d", json.dumps(data),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        raise RuntimeError(f"API PR creation failed: {result.stderr}")

    try:
        resp = json.loads(result.stdout)
        if "html_url" in resp:
            return resp["html_url"]
        if "message" in resp:
            raise RuntimeError(f"GitHub API error: {resp['message']}")
    except json.JSONDecodeError:
        pass

    raise RuntimeError(f"Unexpected API response: {result.stdout[:500]}")
