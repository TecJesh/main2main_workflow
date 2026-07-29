"""Pipeline step: Push work branch and create GitHub PR.

Pushes to the user's fork through the CI proxy, then creates a PR
from the fork branch to the upstream repo via ``gh`` CLI or REST API.
Matches pre-refactor behaviour exactly.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from datetime import datetime
from pathlib import Path

from TA_main2main_workflow.utils.logging import get_logger
from TA_main2main_workflow.utils.git import run_git, run_git_no_check
from TA_main2main_workflow.utils.submodule import push_submodule
from TA_main2main_workflow.utils import WORKSPACE_DIR, FINAL_TARGET_PATCH_FILE, FINAL_SUMMARY_FILE

log = get_logger(__name__)

_MAX_PUSH_RETRIES = 5
_MAX_PR_RETRIES = 5
_RETRY_DELAY_BASE = 10  # seconds, multiplied by attempt number


def push_and_create_pr(
    ascend_path: str | Path,
    github_repo: str,
    summary_path: str | Path | None = None,
    target_commit: str = "",
    work_branch: str = "",
) -> str:
    """Push work branch to fork (via proxy) and create a GitHub PR.

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

    # ── 2. Fork owner (pushing to private fork, not upstream) ──────
    fork_owner = os.environ.get("TA_FORK_OWNER") or "TecJesh"

    # ── 3. Generate summary if missing ─────────────────────────────
    summary_file = summary_path or (WORKSPACE_DIR / FINAL_SUMMARY_FILE)
    if not summary_file.exists():
        summary_file.write_text(
            f"# Triton-Ascend Upstream Sync\n\n"
            f"Branch: `{branch}`\n"
            f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n",
            encoding="utf-8",
        )

    # ── 4. Ensure gh auth ──────────────────────────────────────────
    _ensure_gh_auth(ascend_path)

    # ── 5. Push to fork through proxy ──────────────────────────────
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN") or ""
    _push_to_fork(ascend_path, branch, fork_owner, token)

    # ── 6. Create PR from fork → upstream ──────────────────────────
    pr_url = _create_pr(
        ascend_path, github_repo, branch, fork_owner, token,
        summary_file, target_commit,
    )

    return pr_url


# ═══════════════════════════════════════════════════════════════════════════
# Internal
# ═══════════════════════════════════════════════════════════════════════════


def _ensure_gh_auth(repo: Path) -> None:
    """Ensure gh CLI is authenticated.

    When GH_TOKEN is set, logs gh into github.com directly (necessary
    when the git remote points to a proxy host).  Also embeds the token
    in the origin URL as a fallback for git push through proxy.
    """
    token = os.getenv("GH_TOKEN", "") or os.getenv("GITHUB_TOKEN", "")
    if not token:
        log.warning("No GH_TOKEN set — push/PR may fail")

    if not token:
        # Fall back to interactive auth check
        try:
            subprocess.run(
                ["gh", "auth", "status"],
                check=True, capture_output=True, text=True,
            )
            log.info("gh CLI already authenticated")
        except subprocess.CalledProcessError:
            log.warning("gh not authenticated and GH_TOKEN not set")
        try:
            subprocess.run(
                ["gh", "auth", "setup-git"],
                check=True, capture_output=True, text=True,
            )
        except Exception:
            pass
        return

    log.info("Using GH_TOKEN from environment")

    # Step 1: Explicitly login gh CLI against github.com.
    # This is essential when the git remote points to a proxy host —
    # gh needs to know about github.com independently of git remotes.
    try:
        subprocess.run(
            ["gh", "auth", "login", "--with-token", "--hostname", "github.com"],
            input=token.encode(), capture_output=True, timeout=30,
        )
        log.info("gh auth login OK")
    except Exception as e:
        log.warning(f"gh auth login failed: {e}")

    # Step 2: Verify
    try:
        result = subprocess.run(
            ["gh", "auth", "status", "--hostname", "github.com"],
            capture_output=True, text=True, timeout=30,
        )
        log.info(f"gh auth status: {result.stdout.strip()}")
    except Exception:
        pass

    # Step 3: Configure git credential helper (best-effort)
    try:
        subprocess.run(
            ["gh", "auth", "setup-git", "--hostname", "github.com"],
            capture_output=True, text=True, timeout=30,
        )
        log.info("gh auth setup-git OK")
    except Exception:
        pass

    # Step 4: Embed token in origin URL (fallback for push through proxy)
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


def _push_to_fork(repo: Path, branch: str, fork_owner: str, token: str) -> None:
    """Push work branch to the user's fork through the CI proxy.

    Creates a temporary remote ``ta-fork-push`` that goes through
    ``gh-proxy.test.osinfra.cn`` to the user's fork, pushes, and
    removes the temporary remote.  Retries up to 5 times.
    """
    if not token or not fork_owner:
        log.warning("No GH_TOKEN or TA_FORK_OWNER — falling back to direct push")
        for attempt in range(1, _MAX_PUSH_RETRIES + 1):
            log.info(f"Push attempt {attempt}/{_MAX_PUSH_RETRIES}...")
            try:
                run_git(repo, "push", "--force-with-lease", "origin", branch)
                log.status(True, f"Pushed {branch}")
                return
            except Exception as e:
                log.warning(f"Push failed (attempt {attempt}): {e}")
                if attempt < _MAX_PUSH_RETRIES:
                    time.sleep(_RETRY_DELAY_BASE * attempt)
        raise RuntimeError(f"Push failed after {_MAX_PUSH_RETRIES} attempts")
        return

    fork_remote = "ta-fork-push"
    fork_url = (
        f"https://x-access-token:{token}@"
        f"gh-proxy.test.osinfra.cn/"
        f"https://github.com/{fork_owner}/triton-ascend.git"
    )

    log.info(f"Pushing to fork {fork_owner}/triton-ascend via proxy...")
    log.debug(f"fork remote: {fork_remote}")
    log.debug(f"fork URL (masked): https://x-access-token:***@gh-proxy.test.osinfra.cn/https://github.com/{fork_owner}/triton-ascend.git")

    last_error = ""
    for attempt in range(1, _MAX_PUSH_RETRIES + 1):
        log.info(f"Push attempt {attempt}/{_MAX_PUSH_RETRIES}...")
        try:
            # Remove stale temp remote
            run_git_no_check(repo, "remote", "remove", fork_remote)
            run_git(repo, "remote", "add", fork_remote, fork_url)

            push_result = subprocess.run(
                ["git",
                 "-c", "http.https://github.com/.extraheader=",
                 "push", "--force-with-lease", fork_remote, branch],
                cwd=str(repo), capture_output=True, text=True,
            )
            # Clean up temp remote
            run_git_no_check(repo, "remote", "remove", fork_remote)

            if push_result.returncode == 0:
                if push_result.stdout.strip():
                    log.info(f"push stdout: {push_result.stdout.strip()}")
                log.status(True, f"Pushed {branch} to fork")
                return

            last_error = push_result.stderr.strip() or "(no stderr)"
            log.warning(f"Push failed (attempt {attempt}): {last_error}")
        except Exception as e:
            last_error = str(e)
            log.warning(f"Push failed (attempt {attempt}): {e}")
            try:
                run_git_no_check(repo, "remote", "remove", fork_remote)
            except Exception:
                pass

        if attempt < _MAX_PUSH_RETRIES:
            time.sleep(_RETRY_DELAY_BASE * attempt)

    raise RuntimeError(f"Push failed after {_MAX_PUSH_RETRIES} attempts: {last_error}")


def _create_pr(
    repo: Path, github_repo: str, branch: str, fork_owner: str, token: str,
    summary_file: Path, target_commit: str,
) -> str:
    """Create PR via gh CLI (with fork-aware origin swap).

    Temporarily sets origin to the fork URL so ``gh`` can detect the
    GitHub host, creates the PR from ``fork_owner:branch`` to the
    upstream repo, then restores the saved origin.
    """
    pr_body = summary_file.read_text(encoding="utf-8") if summary_file.exists() else ""

    title = _build_pr_title(target_commit)
    head = f"{fork_owner}:{branch}" if fork_owner else branch
    base_branch = os.getenv("TA_PR_BASE_BRANCH", "upstream-sync")

    log.info(f"Creating PR: head={head}, base={base_branch}, repo={github_repo}")

    # Save origin, swap to fork URL so gh CLI recognizes github.com
    saved_origin = run_git(repo, "config", "--get", "remote.origin.url").strip()
    if token and fork_owner:
        pr_origin = (
            f"https://x-access-token:{token}@"
            f"github.com/{fork_owner}/triton-ascend.git"
        )
    else:
        pr_origin = saved_origin

    run_git(repo, "remote", "set-url", "origin", pr_origin)

    last_error = ""
    for attempt in range(1, _MAX_PR_RETRIES + 1):
        try:
            pr_url = _create_pr_via_gh(github_repo, title, pr_body, head, base_branch)
            log.status(True, f"PR created: {pr_url}")
            return pr_url
        except Exception as e:
            last_error = str(e)
            log.warning(f"PR create attempt {attempt}/{_MAX_PR_RETRIES} FAILED: {last_error}")
            if attempt < _MAX_PR_RETRIES:
                time.sleep(_RETRY_DELAY_BASE * attempt)
        finally:
            # Always restore origin
            try:
                run_git(repo, "remote", "set-url", "origin", saved_origin)
            except Exception:
                pass

    # Restore origin one more time in case of exception path
    try:
        run_git(repo, "remote", "set-url", "origin", saved_origin)
    except Exception:
        pass

    # Fallback: GitHub REST API
    log.info("Falling back to GitHub REST API...")
    try:
        return _create_pr_via_api(github_repo, head, title, pr_body, base_branch, token)
    except Exception as e:
        raise RuntimeError(f"PR creation failed after all attempts: {last_error}; API fallback: {e}")


def _build_pr_title(target_commit: str = "") -> str:
    """Build PR title in conventional commit format.

    Example: [Sync](feat) Merge upstream triton commits abc12345

    Env vars:
      PR_AUTHOR — user tag (default: "Sync")
      PR_TYPE   — conventional commit type (default: "feat")
    """
    author = os.getenv("PR_AUTHOR", "Sync").strip()
    pr_type = os.getenv("PR_TYPE", "feat").strip()
    if target_commit:
        return f"[{author}]({pr_type}) Merge upstream triton commits {target_commit[:8]}"
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    return f"[{author}]({pr_type}) Merge upstream triton commits {ts}"


def _create_pr_via_gh(
    github_repo: str, title: str, body: str,
    head_ref: str, base_branch: str,
) -> str:
    """Create a GitHub PR via the gh CLI.

    Uses GH_TOKEN env var directly (overrides any auto GITHUB_TOKEN from
    actions/checkout) so the PR can reference branches on the user's fork.
    """
    gh_token = os.environ.get("GH_TOKEN") or ""
    cmd = [
        "gh", "pr", "create",
        "--title", title,
        "--body", body,
        "--head", head_ref,
        "--base", base_branch,
        "--repo", github_repo,
    ]
    log.info(f"Running: GH_HOST=github.com {' '.join(cmd)}")
    result = subprocess.run(
        cmd,
        capture_output=True, text=True, timeout=60,
        env={**os.environ,
             "GITHUB_TOKEN": gh_token,
             "GH_TOKEN": gh_token,
             "GH_HOST": "github.com"},
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"gh pr create failed (exit {result.returncode}): "
            f"{result.stderr.strip()}"
        )
    pr_url = result.stdout.strip()
    if not pr_url:
        raise RuntimeError("gh pr create returned empty output")
    return pr_url


def _create_pr_via_api(
    github_repo: str, head: str, title: str, body: str,
    base: str, token: str,
) -> str:
    """Create a GitHub PR via the REST API (fallback).

    Uses the REST API directly to avoid host-detection issues when git
    remotes are rewritten by url.insteadOf proxy.
    """
    if not token:
        raise RuntimeError("No GH_TOKEN or GITHUB_TOKEN set")

    data = {
        "title": title,
        "head": head,
        "base": base,
        "body": body or "",
    }

    url = f"https://api.github.com/repos/{github_repo}/pulls"
    cmd = [
        "curl", "-s", "-X", "POST", url,
        "-H", f"Authorization: Bearer {token}",
        "-H", "Accept: application/vnd.github+json",
        "-H", "X-GitHub-Api-Version: 2022-11-28",
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
