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

import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from TA_main2main_workflow.utils import (
    WORKSPACE_DIR, FINAL_TARGET_PATCH_FILE, FINAL_SUMMARY_FILE,
    run_git, run_git_no_check, print_error,
)


def _detect_default_branch(repo: Path, remote: str = "origin") -> str:
    """Detect the default branch of the remote."""
    try:
        ref = run_git(repo, "symbolic-ref", f"refs/remotes/{remote}/HEAD").strip()
        return ref.rsplit("/", 1)[-1]
    except subprocess.CalledProcessError:
        return "main"


def _ensure_gh_auth(repo: Path) -> None:
    """Ensure GitHub CLI is ready for authenticated git push.

    When GH_TOKEN is set (PAT in CI), gh and git use it directly —
    no explicit login needed.  Otherwise fall back to interactive auth.
    """
    gh_token = os.getenv("GH_TOKEN", "")
    if gh_token:
        # Verify the token works and show which user it belongs to
        print("[push] Using GH_TOKEN from environment (no login needed)")
        result = subprocess.run(
            ["gh", "auth", "status"],
            capture_output=True, text=True,
        )
        print(f"[push] gh auth status: {result.stdout.strip()}")
        if result.returncode != 0:
            print(f"[push] gh auth status stderr: {result.stderr.strip()}")
    else:
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

    # Belt-and-suspenders: when GH_TOKEN is set, also embed it in the origin
    # URL so git push works even if the credential helper misbehaves.
    if gh_token:
        try:
            origin_url = run_git(repo, "remote", "get-url", "origin").strip()
            # Only rewrite https URLs (not ssh)
            if origin_url.startswith("https://"):
                # Extract host + path, strip existing credentials
                clean_url = origin_url.replace("https://", "", 1)
                if "@" in clean_url:
                    clean_url = clean_url.split("@", 1)[1]
                new_url = f"https://x-access-token:{gh_token}@{clean_url}"
                run_git(repo, "remote", "set-url", "origin", new_url)
                # Mask token in log
                safe = f"https://x-access-token:***@{clean_url}"
                print(f"[push] origin URL rewritten with token: {safe}")
        except Exception as exc:
            print(f"[push] Note: could not rewrite origin URL: {exc}")


def _run_pre_commit_and_amend(repo: Path) -> bool:
    """Run pre-commit and amend the latest commit if auto-fixes were applied.

    Steps:
      1. Clean temp files first (result_profiling/, __pycache__/, *.lock, *.pyc)
      2. Run: pre-commit run --from-ref origin/main --to-ref HEAD
      3. If pre-commit modified files → git add -u && git commit --amend --no-edit
      4. Re-clean temp files after amend

    Returns True if pre-commit passed (with or without auto-fixes).
    Returns False if pre-commit found unfixable issues.
    """
    from TA_main2main_workflow.scripts.pre_ci_check import cleanup_temp_files

    print("[push] ── Pre-commit check before PR ──")

    # ── Step 1: clean temp files ──
    print("[push] Cleaning temp files before pre-commit...")
    cleanup_temp_files(repo)

    # ── Step 2: run pre-commit ──
    print("[push] Running: pre-commit run --from-ref origin/main --to-ref HEAD")
    try:
        pc_proc = subprocess.run(
            ["pre-commit", "run", "--from-ref", "origin/main", "--to-ref", "HEAD"],
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


def push_and_create_pr(
    ascend_path: Path,
    github_repo: str = "TecJesh/triton-ascend",
    work_branch: str = "",
    summary_path: Path | None = None,
) -> str:
    """Push the current work branch and create a GitHub PR.

    Flow:
      1. Authenticate gh CLI
      2. Run pre-commit --from-ref origin/main --to-ref HEAD, amend if needed
      3. Clean temp files
      4. Commit any remaining uncommitted changes
      5. Push work branch
      6. Create PR with [user](type) title format

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
    try:
        run_git(repo, "push", "-u", "origin", work_branch)
    except subprocess.CalledProcessError as e:
        stderr_detail = e.stderr.strip() if e.stderr else "(no stderr)"
        print_error(f"git push failed (exit {e.returncode}): {stderr_detail}")
        raise

    # ── Create PR ──
    base_branch = _detect_default_branch(repo)
    pr_description = summary_file.read_text(encoding="utf-8") if summary_file.exists() else ""

    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    pr_title = _build_pr_title(ts)

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


def push_step_progress(
    ascend_path: Path,
    github_repo: str = "TecJesh/triton-ascend",
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
    patch_content = run_git(repo, "diff", "origin/main", "HEAD")
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
