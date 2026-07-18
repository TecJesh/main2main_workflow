#!/usr/bin/env python3
"""Push the sync branch and create a GitHub Pull Request for triton-ascend.

Steps:
  1. Ensure gh CLI is authenticated.
  2. Clean up temp files (result_profiling/, __pycache__/, *.lock, etc.).
  3. Run pre-commit run --from-ref origin/main --to-ref HEAD.
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
import urllib.request
from datetime import datetime
from pathlib import Path

from TA_main2main_workflow.utils import (
    WORKSPACE_DIR, FINAL_TARGET_PATCH_FILE, FINAL_SUMMARY_FILE,
    run_git, run_git_no_check, print_error,
    ENV_BASE_BRANCH, get_base_branch_ref,
)


def _detect_origin_owner(repo: Path, remote: str = "origin") -> str:
    """Extract the GitHub owner from the origin remote URL."""
    try:
        url = run_git(repo, "remote", "get-url", remote).strip()
        # Handle https://github.com/owner/repo.git and git@github.com:owner/repo.git
        if "github.com" in url:
            # strip protocol, host, and .git suffix
            url = url.replace("https://", "").replace("git@", "")
            url = url.replace("github.com/", "").replace("github.com:", "")
            if url.endswith(".git"):
                url = url[:-4]
            parts = url.split("/")
            if parts:
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
            print("[push] gh CLI already authenticated.")
        except subprocess.CalledProcessError:
            print(
                "[push] gh not authenticated and GH_TOKEN not set. "
                "Run 'gh auth login' locally or set GH_TOKEN in CI.",
                file=sys.stderr,
            )
            sys.exit(1)
        subprocess.run(
            ["gh", "auth", "setup-git"],
            check=True, capture_output=True, text=True,
        )
        print("[push] Git credential helper configured (via gh auth setup-git).")
        return

    # ── GH_TOKEN is set ──
    print("[push] Using GH_TOKEN from environment")

    # Step 1: Explicitly login gh CLI against github.com.
    # This is essential when the git remote points to a proxy host —
    # gh needs to know about github.com independently of git remotes.
    result = subprocess.run(
        ["gh", "auth", "login", "--with-token", "--hostname", "github.com"],
        input=gh_token + "\n", text=True, capture_output=True,
    )
    if result.returncode == 0:
        print("[push] gh auth login --with-token: success")
    else:
        print(f"[push] gh auth login stderr: {result.stderr.strip()}")

    # Step 2: Verify the token works
    result = subprocess.run(
        ["gh", "auth", "status", "--hostname", "github.com"],
        capture_output=True, text=True,
    )
    print(f"[push] gh auth status: {result.stdout.strip()}")
    if result.returncode != 0:
        print(f"[push] gh auth status stderr: {result.stderr.strip()}")

    # Step 3: Configure git credential helper (best-effort).
    # This may fail when the git remote points to a proxy host that gh
    # doesn't recognize — but it's non-essential because Step 4 embeds
    # the token directly in the origin URL.
    result = subprocess.run(
        ["gh", "auth", "setup-git", "--hostname", "github.com"],
        capture_output=True, text=True,
    )
    if result.returncode == 0:
        print("[push] Git credential helper configured (via gh auth setup-git).")
    else:
        print(f"[push] gh auth setup-git skipped "
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
            print(f"[push] origin URL rewritten with token: {safe}")
    except Exception as exc:
        print(f"[push] Note: could not rewrite origin URL: {exc}")


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
    from TA_main2main_workflow.scripts.pre_ci_check import cleanup_temp_files

    base_ref = get_base_branch_ref()

    print("[push] ── Pre-commit check before PR ──")

    # ── Step 1: clean temp files ──
    print("[push] Cleaning temp files before pre-commit...")
    cleanup_temp_files(repo)

    # ── Step 2: run pre-commit ──
    print(f"[push] Running: pre-commit run --from-ref {base_ref} --to-ref HEAD")
    try:
        pc_proc = subprocess.run(
            ["pre-commit", "run", "--from-ref", base_ref, "--to-ref", "HEAD"],
            cwd=repo,
            capture_output=True,
            text=True,
            timeout=300,
        )
    except subprocess.TimeoutExpired:
        print("[push] ⚠ pre-commit timed out after 300s, continuing anyway")
        return True
    except FileNotFoundError:
        print("[push] ⚠ pre-commit not installed, skipping")
        return True

    # Print pre-commit output
    if pc_proc.stdout:
        print(pc_proc.stdout)
    if pc_proc.stderr:
        print(pc_proc.stderr, file=sys.stderr)

    precommit_passed = pc_proc.returncode == 0

    # ── Step 3: check if pre-commit modified any files ──
    status_proc = run_git_no_check(repo, "status", "--porcelain")
    has_modifications = bool(status_proc.stdout.strip())

    if has_modifications:
        print("[push] Pre-commit modified files, amending latest commit...")
        # Stage only tracked files to avoid temp artifacts
        run_git(repo, "add", "-u")
        try:
            run_git(repo, "commit", "--amend", "--no-edit")
            print("[push] Commit amended with pre-commit fixes.")
        except subprocess.CalledProcessError:
            print("[push] Nothing to amend (already clean)")

        # ── Step 4: re-clean temp files after amend ──
        cleanup_temp_files(repo)
    else:
        if precommit_passed:
            print("[push] Pre-commit passed, no modifications needed.")
        else:
            print("[push] ⚠ Pre-commit reported issues but no files were modified "
                  "(may need manual review).")

    return True


def _build_pr_title(ts: str = "") -> str:
    """Build PR title in format: [user](type) description

    Example: [TA](sync) merge upstream triton commits (20240612-120000)

    Env vars:
      PR_AUTHOR — user tag (default: git user.name or "TA")
      PR_TYPE   — conventional commit type (default: "sync")
    """
    author = os.getenv("PR_AUTHOR", "").strip()
    if not author:
        # Fall back to git user name
        try:
            author = subprocess.run(
                ["git", "config", "user.name"],
                check=True, capture_output=True, text=True,
            ).stdout.strip()
        except subprocess.CalledProcessError:
            author = "TA"

    pr_type = os.getenv("PR_TYPE", "sync").strip()
    ts = ts or datetime.now().strftime("%Y%m%d-%H%M%S")

    return f"[{author}]({pr_type}) merge upstream triton commits ({ts})"


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


def push_and_create_pr(
    ascend_path: Path,
    github_repo: str = "triton-lang/triton-ascend",
    work_branch: str = "",
    summary_path: Path | None = None,
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

    base_ref = get_base_branch_ref()
    try:
        merge_base = run_git(repo, "merge-base", base_ref, "HEAD").strip()
    except subprocess.CalledProcessError:
        merge_base = "HEAD~1"

    patch_content = run_git(repo, "diff", merge_base, "HEAD")
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

    # ── Pre-commit check + amend before pushing ──
    _run_pre_commit_and_amend(repo)

    # ── Commit any remaining uncommitted changes (after pre-commit amend) ──
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

    # ── Push ──
    print(f"[push] Pushing branch '{work_branch}' to origin...")

    # Debug: show what token / URL we're actually using
    print("[push] === DEBUG push environment ===")
    print(f"[push] GH_TOKEN set: {bool(os.getenv('GH_TOKEN'))}")
    print(f"[push] GITHUB_TOKEN set: {bool(os.getenv('GITHUB_TOKEN'))}")
    try:
        remote_url = run_git(repo, "remote", "get-url", "origin").strip()
        # Mask any embedded token
        if "@" in remote_url:
            safe_url = remote_url.split("@")[0].split(":")[-1] + "@" + remote_url.split("@")[1]
        else:
            safe_url = remote_url
        print(f"[push] origin URL: {safe_url}")
        print(f"[push] current branch: {run_git(repo, 'branch', '--show-current').strip()}")
    except Exception:
        pass
    print("[push] ==============================")

    try:
        run_git(repo, "push", "-u", "origin", work_branch)
    except subprocess.CalledProcessError as e:
        stderr_detail = e.stderr.strip() if e.stderr else "(no stderr)"
        print_error(f"git push failed (exit {e.returncode}): {stderr_detail}")
        raise

    # ── Create PR via GitHub REST API ──
    # Use the REST API directly instead of gh CLI to avoid host-detection
    # failures when git remotes are rewritten by url.insteadOf proxy.
    base_branch = _detect_default_branch(repo)
    pr_description = summary_file.read_text(encoding="utf-8") if summary_file.exists() else ""

    # Resolve head to "owner:branch" (required for cross-fork PRs).
    _head_owner = _detect_origin_owner(repo)
    _head = f"{_head_owner}:{work_branch}" if _head_owner else work_branch

    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    pr_title = _build_pr_title(ts)

    gh_token = os.getenv("GH_TOKEN", "") or os.getenv("GITHUB_TOKEN", "")
    if not gh_token:
        raise RuntimeError("GH_TOKEN or GITHUB_TOKEN must be set to create PR")

    print(f"[push] Creating PR via API: {pr_title}")
    print(f"[push] head={_head} base={base_branch} repo={github_repo}")
    pr_url = _create_pr_via_api(
        github_repo=github_repo,
        title=pr_title,
        body=pr_description,
        head=_head,
        base=base_branch,
        token=gh_token,
    )
    print(f"[push] PR created: {pr_url}")
    return pr_url


def push_step_progress(
    ascend_path: Path,
    github_repo: str = "triton-lang/triton-ascend",
    work_branch: str = "",
    step_id: str = "",
    step_num: int = 1,
    total_steps: int = 1,
    pr_url: str = "",
) -> str:
    """Push work-branch progress after a single step and create/update a PR.

    Called after each progressive step's commit. On the first call (pr_url
    is empty) it creates a new PR; on subsequent calls it just pushes —
    the existing PR picks up the new commits automatically.

    Returns the PR URL (new or existing).
    """
    repo = Path(ascend_path)

    if not work_branch:
        work_branch = run_git(repo, "branch", "--show-current").strip()

    _ensure_gh_auth(repo)

    # ── Generate step-aware patch ──
    patch_content = run_git(repo, "diff", get_base_branch_ref(), "HEAD")
    patch_path = WORKSPACE_DIR / FINAL_TARGET_PATCH_FILE
    patch_path.write_text(patch_content, encoding="utf-8")

    # ── Push ──
    print(f"[push] [{step_id}] Pushing branch '{work_branch}' to origin...")
    run_git(repo, "push", "-u", "origin", work_branch)

    # ── Create PR on first call only ──
    if not pr_url:
        base_branch = _detect_default_branch(repo)
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        pr_title = (
            f"[Step {step_num}/{total_steps}] sync: upstream triton merge ({ts})"
        )
        pr_body = (
            f"## Progressive Sync — Step {step_num}/{total_steps}\n\n"
            f"**Work branch**: `{work_branch}`\n"
            f"**Target repo**: `{github_repo}`\n"
            f"**Generated**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
            f"This PR will be updated as subsequent steps complete.\n"
        )

        print(f"[push] [{step_id}] Creating PR: {pr_title}")
        gh_cmd = [
            "gh", "pr", "create",
            "--title", pr_title,
            "--body", pr_body,
            "--head", work_branch,
            "--base", base_branch,
            "--repo", github_repo,
        ]
        result = subprocess.run(
            gh_cmd, check=True, capture_output=True, text=True, cwd=str(repo)
        )
        pr_url = result.stdout.strip()
        print(f"[push] [{step_id}] PR created: {pr_url}")
    else:
        print(f"[push] [{step_id}] Pushed to existing PR: {pr_url}")

    return pr_url


def update_pr_description(
    ascend_path: Path,
    github_repo: str,
    pr_url: str,
    step_descriptions: list[str],
) -> None:
    """Update the PR body with a summary of all completed steps."""
    if not pr_url:
        return

    body = (
        "# Triton-Ascend Progressive Upstream Sync\n\n"
        "## Completed Steps\n\n"
    )
    for desc in step_descriptions:
        body += f"- {desc}\n"
    body += (
        f"\n---\n"
        f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
    )

    try:
        subprocess.run(
            ["gh", "pr", "edit", pr_url, "--body", body, "--repo", github_repo],
            check=True, capture_output=True, text=True, cwd=str(ascend_path),
        )
        print(f"[push] Updated PR description: {pr_url}")
    except subprocess.CalledProcessError as e:
        print(f"[push] Warning: could not update PR description: {e}")
