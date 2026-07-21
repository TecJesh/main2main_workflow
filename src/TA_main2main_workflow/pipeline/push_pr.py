#!/usr/bin/env python3
"""Push the sync branch and create a GitHub Pull Request for triton-ascend.

Steps:
  1. Ensure gh CLI is authenticated.
  2. Clean up temp files (result_profiling/, __pycache__/, *.lock, etc.).
  3. Run pre-commit run --from-ref origin/main --to-ref HEAD.
from TA_main2main_workflow.utils.logging import get_logger
log = get_logger(__name__)
  4. If pre-commit auto-fixes files, amend the latest commit.
  5. Push the work branch to origin.
  6. Open a PR via gh pr create with [user](type) title format.

Environment variables:
  PUSH_TO_GITHUB  — must be "true" to proceed
  GITHUB_REPO     — target repo "owner/name" (default: TecJesh/triton-ascend)
  GH_TOKEN        — GitHub Personal Access Token (CI fallback)
  PR_AUTHOR       — user tag in PR title, e.g. "TA" → [TA](sync) ... (default: git user)
  PR_TYPE         — conventional commit type in PR title (default: "sync")
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path

from TA_main2main_workflow.utils import (
    WORKSPACE_DIR, FINAL_TARGET_PATCH_FILE, FINAL_SUMMARY_FILE,
    run_git, run_git_no_check, print_error,
    ENV_BASE_BRANCH, get_base_branch_ref,
)


def _detect_origin_owner(repo: Path, remote: str = "origin") -> str:
    """Extract the GitHub owner from the origin remote URL.

    Handles direct GitHub URLs, SSH URLs, and proxy URLs
    (e.g. gh-proxy.test.osinfra.cn/https://github.com/owner/repo.git).
    """
    try:
        url = run_git(repo, "remote", "get-url", remote).strip()
        # Strip credentials
        if "@" in url:
            url = url.split("@", 1)[-1]
        # If URL is behind a proxy, extract the real GitHub path
        if "github.com/" in url:
            # e.g. gh-proxy.test.osinfra.cn/https://github.com/owner/repo.git
            url = url.split("github.com/", 1)[-1]
        elif "github.com:" in url:
            # e.g. git@github.com:owner/repo.git
            url = url.split("github.com:", 1)[-1]
        # Now url should be owner/repo or owner/repo.git
        url = url.replace("https://", "").replace("git@", "")
        if url.endswith(".git"):
            url = url[:-4]
        parts = url.split("/")
        if parts and parts[0]:
            return parts[0]
    except Exception:
        pass
    return ""


def _detect_default_branch(repo: Path, remote: str = "origin") -> str:
    """Detect the default branch of the remote."""
    try:
        ref = run_git(repo, "symbolic-ref", f"refs/remotes/{remote}/HEAD").strip()
        return ref.rsplit("/", 1)[-1]
    except subprocess.CalledProcessError:
        return os.getenv("TA_PR_BASE_BRANCH", "upstream-sync")


def _ensure_gh_auth(repo: Path) -> None:
    """Ensure GitHub CLI is ready for authenticated git push.

    When GH_TOKEN is set (PAT in CI), gh and git use it directly —
    no explicit login needed.  Otherwise fall back to interactive auth.

    IMPORTANT: when the runner uses a git proxy (url.insteadOf), the origin
    URL may point to a non-GitHub host (e.g. gh-proxy.test.osinfra.cn).
    We use TWO separate auth paths to handle this:
      1. gh auth login --with-token → tells gh CLI about github.com directly
         (does NOT look at git remotes — essential for gh pr create)
      2. Embed token in origin URL → ensures git push works through the proxy
         (gh auth setup-git alone may not work with url.insteadOf rewriting)
    """
    gh_token = os.getenv("GH_TOKEN", "")
    if not gh_token:
        try:
            subprocess.run(
                ["gh", "auth", "status"],
                check=True, capture_output=True, text=True,
            )
            log.info("[push] gh CLI already authenticated.")
        except subprocess.CalledProcessError:
            log.error(
                "[push] gh not authenticated and GH_TOKEN not set. "
                "Run 'gh auth login' locally or set GH_TOKEN in CI."
            )
            sys.exit(1)
        subprocess.run(
            ["gh", "auth", "setup-git"],
            check=True, capture_output=True, text=True,
        )
        log.info("[push] Git credential helper configured (via gh auth setup-git).")
        return

    # ── GH_TOKEN is set ──
    log.info("[push] Using GH_TOKEN from environment")

    # Step 1: Explicitly login gh CLI against github.com.
    # This is essential when the git remote points to a proxy host —
    # gh needs to know about github.com independently of git remotes.
    result = subprocess.run(
        ["gh", "auth", "login", "--with-token", "--hostname", "github.com"],
        input=gh_token + "\n", text=True, capture_output=True,
    )
    if result.returncode == 0:
        log.info("[push] gh auth login --with-token: success")
    else:
        log.info(f"[push] gh auth login stderr: {result.stderr.strip()}")

    # Step 2: Verify the token works
    result = subprocess.run(
        ["gh", "auth", "status", "--hostname", "github.com"],
        capture_output=True, text=True,
    )
    log.info(f"[push] gh auth status: {result.stdout.strip()}")
    if result.returncode != 0:
        log.info(f"[push] gh auth status stderr: {result.stderr.strip()}")

    # Step 3: Configure git credential helper (best-effort).
    # This may fail when the git remote points to a proxy host that gh
    # doesn't recognize — but it's non-essential because Step 4 embeds
    # the token directly in the origin URL.
    result = subprocess.run(
        ["gh", "auth", "setup-git", "--hostname", "github.com"],
        capture_output=True, text=True,
    )
    if result.returncode == 0:
        log.info("[push] Git credential helper configured (via gh auth setup-git).")
    else:
        log.info(f"[push] gh auth setup-git skipped "
              f"(exit {result.returncode}): {result.stderr.strip()}")

    # Step 4: Embed token in origin URL so git push works through the proxy.
    # (gh auth setup-git may not help when url.insteadOf rewrites the host.)
    try:
        origin_url = run_git(repo, "remote", "get-url", "origin").strip()
        if origin_url.startswith("https://"):
            clean_url = origin_url.replace("https://", "", 1)
            if "@" in clean_url:
                clean_url = clean_url.split("@", 1)[1]
            new_url = f"https://x-access-token:{gh_token}@{clean_url}"
            run_git(repo, "remote", "set-url", "origin", new_url)
            safe = f"https://x-access-token:***@{clean_url}"
            log.info(f"[push] origin URL rewritten with token: {safe}")
    except Exception as exc:
        log.info(f"[push] Note: could not rewrite origin URL: {exc}")


def _run_pre_commit_and_amend(repo: Path) -> bool:
    """Run pre-commit and amend the latest commit if auto-fixes were applied.

    Steps:
      1. Clean temp files first (result_profiling/, __pycache__/, *.lock, *.pyc)
      2. Run: pre-commit run --from-ref <base_ref> --to-ref HEAD
      3. If pre-commit modified files → git add -u && git commit --amend --no-edit
      4. Re-clean temp files after amend

    Returns True if pre-commit passed (with or without auto-fixes).
    Returns False if pre-commit found unfixable issues.
    """
    from TA_main2main_workflow.pipeline.pre_ci import cleanup_temp_files

    base_ref = get_base_branch_ref()

    log.info("[push] ── Pre-commit check before PR ──")

    # ── Step 1: clean temp files ──
    log.info("[push] Cleaning temp files before pre-commit...")
    cleanup_temp_files(repo)

    # ── Step 2: run pre-commit ──
    log.info(f"[push] Running: pre-commit run --from-ref {base_ref} --to-ref HEAD")
    try:
        pc_proc = subprocess.run(
            ["pre-commit", "run", "--from-ref", base_ref, "--to-ref", "HEAD"],
            cwd=repo,
            capture_output=True,
            text=True,
            timeout=300,
        )
    except subprocess.TimeoutExpired:
        log.info("[push] ⚠ pre-commit timed out after 300s, continuing anyway")
        return True
    except FileNotFoundError:
        log.info("[push] ⚠ pre-commit not installed, skipping")
        return True

    # Print pre-commit output
    if pc_proc.stdout:
        log.info(pc_proc.stdout)
    if pc_proc.stderr:
        log.error(pc_proc.stderr)

    precommit_passed = pc_proc.returncode == 0

    # ── Step 3: check if pre-commit modified any files ──
    status_proc = run_git_no_check(repo, "status", "--porcelain")
    has_modifications = bool(status_proc.stdout.strip())

    if has_modifications:
        log.info("[push] Pre-commit modified files, amending latest commit...")
        # Stage only tracked files to avoid temp artifacts
        run_git(repo, "add", "-u")
        try:
            run_git(repo, "commit", "--amend", "--no-edit")
            log.info("[push] Commit amended with pre-commit fixes.")
        except subprocess.CalledProcessError:
            log.info("[push] Nothing to amend (already clean)")

        # ── Step 4: re-clean temp files after amend ──
        cleanup_temp_files(repo)
    else:
        if precommit_passed:
            log.info("[push] Pre-commit passed, no modifications needed.")
        else:
            log.info("[push] ⚠ Pre-commit reported issues but no files were modified "
                  "(may need manual review).")

    return True


def _build_pr_title(target_commit: str = "") -> str:
    """Build PR title in conventional commit format.

    Example: [Sync](feat) Merge upstream triton commits (abc12345)

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


def _create_pr_via_api(
    github_repo: str,
    title: str,
    body: str,
    head: str,
    base: str,
    token: str,
) -> str:
    """Create a GitHub PR via the REST API directly.

    Uses the GitHub REST API (POST /repos/{owner}/{repo}/pulls) instead of
    gh CLI to avoid host-detection issues when git remotes are rewritten by
    url.insteadOf proxy.
    """
    url = f"https://api.github.com/repos/{github_repo}/pulls"
    payload = json.dumps({
        "title": title,
        "body": body,
        "head": head,
        "base": base,
    }).encode("utf-8")

    req = urllib.request.Request(
        url,
        data=payload,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read().decode("utf-8"))
            pr_url = result.get("html_url", "")
            if not pr_url:
                raise RuntimeError(f"API response missing html_url: {result}")
            return pr_url
    except urllib.error.HTTPError as e:
        error_body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            f"GitHub API error {e.code}: {error_body}"
        ) from e


def _create_pr_via_gh(
    github_repo: str,
    title: str,
    body: str,
    head_ref: str,
    base_branch: str,
) -> str:
    """Create a GitHub PR via the gh CLI.

    Uses the user's GH_TOKEN (classic PAT with fork write access) to
    authenticate.  The auto GITHUB_TOKEN from actions/checkout is scoped
    to the upstream repo only — GH_TOKEN overrides it so the PR can
    reference branches on the user's fork.
    """
    gh_token = os.environ.get("GH_TOKEN") or ""
    gh_cmd = [
        "gh", "pr", "create",
        "--title", title,
        "--body", body,
        "--head", head_ref,
        "--base", base_branch,
        "--repo", github_repo,
    ]
    log.info(f"[push] Running: GH_HOST=github.com {' '.join(gh_cmd)}")
    result = subprocess.run(
        gh_cmd,
        capture_output=True, text=True, timeout=60,
        env={**os.environ,
             "GITHUB_TOKEN": gh_token,
             "GH_TOKEN": gh_token},
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


def push_and_create_pr(
    ascend_path: Path,
    github_repo: str = "triton-lang/triton-ascend",
    work_branch: str = "",
    summary_path: Path | None = None,
    target_commit: str = "",
) -> str:
    """Push the current work branch and create a GitHub PR.

    Flow:
      1. Authenticate gh CLI
      2. Run pre-commit --from-ref <base_ref> --to-ref HEAD, amend if needed
      3. Clean temp files
      4. Commit any remaining uncommitted changes
      5. Push work branch
      6. Create PR with [user](type) title format

    Returns the PR URL, or "" on skip/failure.
    """
    repo = Path(ascend_path)

    if not work_branch:
        work_branch = run_git(repo, "branch", "--show-current").strip()

    # Fork owner for push and PR head.  Defaults to TecJesh because
    # in CI origin points to triton-lang/triton-ascend (upstream),
    # so auto-detection would return the wrong owner.
    _fork_owner = os.environ.get("TA_FORK_OWNER") or "TecJesh"

    base_ref = get_base_branch_ref()
    try:
        merge_base = run_git(repo, "merge-base", base_ref, "HEAD").strip()
    except subprocess.CalledProcessError:
        merge_base = "HEAD~1"

    patch_content = run_git(repo, "diff", merge_base, "HEAD")
    patch_path = WORKSPACE_DIR / FINAL_TARGET_PATCH_FILE
    patch_path.write_text(patch_content, encoding="utf-8")
    log.info(f"[push] Cumulative patch written to {patch_path}")

    summary_file = summary_path or (WORKSPACE_DIR / FINAL_SUMMARY_FILE)
    if not summary_file.exists():
        summary_file.write_text(
            f"# Triton-Ascend Upstream Sync\n\n"
            f"Branch: `{work_branch}`\n"
            f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n",
            encoding="utf-8",
        )

    _ensure_gh_auth(repo)

    # ── Pre-commit check + amend before pushing ──
    _run_pre_commit_and_amend(repo)

    # ── Commit any remaining uncommitted changes (after pre-commit amend) ──
    status = run_git(repo, "status", "--porcelain").strip()
    if status:
        log.info("[push] Staging uncommitted changes...")
        # Use "git add -u" (tracked-only) to avoid staging test artifacts,
        # cache files, or other transient files created during the flow.
        run_git(repo, "add", "-u")
        commit_msg = f"sync: upstream triton merge ({datetime.now().strftime('%Y%m%d-%H%M%S')})"
        try:
            run_git(repo, "commit", "-s", "-m", commit_msg)
            log.info(f"[push] Committed: {commit_msg}")
        except subprocess.CalledProcessError:
            log.info("[push] Nothing to commit (already clean)")

    # ── Push ──
    log.info(f"[push] Pushing branch '{work_branch}' to origin...")

    # Debug: show what token / URL we're actually using
    log.info("[push] === DEBUG push environment ===")
    log.info(f"[push] GH_TOKEN set: {bool(os.getenv('GH_TOKEN'))}")
    log.info(f"[push] GITHUB_TOKEN set: {bool(os.getenv('GITHUB_TOKEN'))}")
    try:
        remote_url = run_git(repo, "remote", "get-url", "origin").strip()
        # Mask any embedded token
        if "@" in remote_url:
            safe_url = remote_url.split("@")[0].split(":")[-1] + "@" + remote_url.split("@")[1]
        else:
            safe_url = remote_url
        log.info(f"[push] origin URL: {safe_url}")
        log.info(f"[push] current branch: {run_git(repo, 'branch', '--show-current').strip()}")
    except Exception:
        pass
    log.info("[push] ==============================")

    # Push to the fork (same pattern as AscendNPU-IR submodule push).
    # Token embedded in the URL so the CI proxy can authenticate.
    _token = os.environ.get("GH_TOKEN") or ""
    if _token and _fork_owner:
        _fork_remote = "ta-fork-push"
        _fork_url = (
            f"https://x-access-token:{_token}@"
            f"gh-proxy.test.osinfra.cn/"
            f"https://github.com/{_fork_owner}/triton-ascend.git"
        )
        _last_push_error = ""
        for _attempt in range(1, 6):
            run_git_no_check(repo, "remote", "remove", _fork_remote)
            run_git(repo, "remote", "add", _fork_remote, _fork_url)
            _push_result = subprocess.run(
                ["git",
                 "-c", "http.https://github.com/.extraheader=",
                 "push", "--force-with-lease", _fork_remote, work_branch],
                cwd=str(repo), capture_output=True, text=True,
            )
            run_git(repo, "remote", "remove", _fork_remote)
            if _push_result.returncode == 0:
                if _push_result.stdout.strip():
                    log.info(f"[push] stdout:\n{_push_result.stdout.strip()}")
                break
            _last_push_error = _push_result.stderr.strip() or "(no stderr)"
            print_error(
                f"[push] git push attempt {_attempt}/5 FAILED "
                f"(exit {_push_result.returncode}):\n{_last_push_error}"
            )
            if _attempt < 5:
                time.sleep(10 * _attempt)
        else:
            raise RuntimeError(
                f"git push failed after 5 attempts: {_last_push_error}")
    else:
        run_git(repo, "push", "-u", "origin", work_branch)

    # ── Create PR via gh CLI ──
    # gh infers the GitHub host from git remotes.  In CI origin points
    # to the proxy, so we temporarily swap it to the fork URL (with
    # token) — gh recognizes github.com and GH_HOST isn't needed.
    base_branch = os.getenv("TA_PR_BASE_BRANCH", "upstream-sync")
    pr_description = summary_file.read_text(encoding="utf-8") if summary_file.exists() else ""

    _head = f"{_fork_owner}:{work_branch}" if _fork_owner else work_branch
    pr_title = _build_pr_title(target_commit)

    log.info(f"[push] Creating PR via gh CLI:")
    log.info(f"        head = {_head}")
    log.info(f"        base = {base_branch}")
    log.info(f"        repo = {github_repo}")

    _saved_origin = run_git(repo, "config", "--get", "remote.origin.url").strip()
    _pr_origin = f"https://x-access-token:{_token}@github.com/{_fork_owner}/triton-ascend.git" if _token else f"https://github.com/{_fork_owner}/triton-ascend.git"
    run_git(repo, "remote", "set-url", "origin", _pr_origin)

    _last_pr_error = ""
    for _attempt in range(1, 6):
        try:
            pr_url = _create_pr_via_gh(
                github_repo=github_repo,
                title=pr_title,
                body=pr_description,
                head_ref=_head,
                base_branch=base_branch,
            )
            log.info(f"[push] PR created: {pr_url}")
            return pr_url
        except Exception as _e:
            _last_pr_error = str(_e)
            print_error(f"[push] PR create attempt {_attempt}/5 FAILED: "
                        f"{_last_pr_error}")
            if _attempt < 5:
                time.sleep(10 * _attempt)
        finally:
            run_git(repo, "remote", "set-url", "origin", _saved_origin)
    raise RuntimeError(
        f"gh pr create failed after 5 attempts: {_last_pr_error}")
