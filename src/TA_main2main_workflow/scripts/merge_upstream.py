#!/usr/bin/env python3
"""Perform git merge of upstream triton commits into triton-ascend work branch.

Creates a work branch based on the latest main from triton-lang/triton-ascend
(fetched fresh each run), then merges the target upstream commit.
If merge conflicts occur, they are recorded for later AI resolution.

Output:
  - workspace/merge_result.json
  - workspace/merge.log (raw git merge output)
"""

from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime
from pathlib import Path

from TA_main2main_workflow.utils import (
    WORKSPACE_DIR, MERGE_RESULT_FILE, MERGE_LOG_FILE, CONFLICT_LOG_DIR,
    run_git, run_git_no_check, has_merge_conflicts, get_conflict_files,
)


def _check_tracked_changes(repo: Path) -> bool:
    """Return True if tracked files have uncommitted changes (modified or staged)."""
    unstaged = run_git_no_check(repo, "diff", "--quiet")
    staged = run_git_no_check(repo, "diff", "--cached", "--quiet")
    return unstaged.returncode != 0 or staged.returncode != 0


def _auto_stash(repo: Path) -> str:
    """Stash all changes (including untracked). Returns the stash name."""
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    name = f"ta-sync-{ts}"
    run_git(repo, "stash", "push", "-u", "-m", name)
    print(f"[merge] Auto-stashed changes as '{name}'")
    return name


def _abort_stale_merge(repo: Path) -> None:
    """Abort any stale merge in progress."""
    merge_head = repo / ".git" / "MERGE_HEAD"
    if merge_head.exists():
        print("[merge] Found stale MERGE_HEAD, running git merge --abort")
        try:
            run_git(repo, "merge", "--abort")
        except subprocess.CalledProcessError:
            print("[merge] Warning: git merge --abort failed, trying git reset --hard HEAD")
            run_git(repo, "reset", "--hard", "HEAD")
        for f in [".git/MERGE_MODE", ".git/MERGE_MSG", ".git/CHERRY_PICK_HEAD"]:
            p = repo / f
            if p.exists():
                p.unlink()


def _ensure_upstream_ascend_remote(repo: Path) -> str:
    """Ensure a remote for triton-lang/triton-ascend exists and return its name.

    Checks existing remotes for one that points to triton-lang/triton-ascend.
    If none found, adds a remote named 'upstream-ascend'.
    Returns the remote name to use for fetching.
    """
    ASCEND_UPSTREAM_URL = "https://github.com/triton-lang/triton-ascend.git"

    # Check if any existing remote already points to the ascend upstream
    remotes_proc = run_git_no_check(repo, "remote", "-v")
    for line in remotes_proc.stdout.strip().splitlines():
        if ASCEND_UPSTREAM_URL in line:
            remote_name = line.split()[0]
            print(f"[merge] Found existing remote '{remote_name}' → {ASCEND_UPSTREAM_URL}")
            return remote_name

    # Not found — add a new remote
    remote_name = "upstream-ascend"
    print(f"[merge] Adding remote '{remote_name}' → {ASCEND_UPSTREAM_URL}")
    run_git(repo, "remote", "add", remote_name, ASCEND_UPSTREAM_URL)
    return remote_name


def _create_work_branch(repo: Path, suffix: str = "") -> str:
    """Create and checkout a work branch for the merge.

    The branch is always based on the latest main branch from
    triton-lang/triton-ascend (fetched fresh each run).
    """
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    branch = f"auto/upstream-sync-{ts}{'-' + suffix if suffix else ''}"

    if _check_tracked_changes(repo):
        auto_stash = os.getenv("AUTO_STASH", "false").lower() == "true"
        if auto_stash:
            _auto_stash(repo)
        else:
            print("[merge] ERROR: Tracked files have uncommitted changes.")
            print("[merge] Hint:  git stash push -u -m 'pre-sync-stash'")
            print("[merge]    or  set AUTO_STASH=true to auto-stash before sync")
            raise RuntimeError(
                "Working tree has uncommitted changes to tracked files. "
                "Commit or stash changes before running sync."
            )

    _abort_stale_merge(repo)

    # Ensure we have the latest main from triton-lang/triton-ascend
    upstream_remote = _ensure_upstream_ascend_remote(repo)
    print(f"[merge] Fetching latest main from '{upstream_remote}'...")
    run_git(repo, "fetch", upstream_remote, "main")

    # Create work branch based on the latest upstream main
    base_ref = f"{upstream_remote}/main"
    print(f"[merge] Creating work branch '{branch}' from {base_ref}")
    proc = run_git_no_check(repo, "checkout", "-B", branch, base_ref)
    if proc.returncode != 0:
        print(f"[merge] ERROR: git checkout -B {branch} {base_ref} failed")
        print(f"[merge] stderr: {proc.stderr.strip()}")
        raise RuntimeError(f"Failed to create work branch '{branch}': {proc.stderr.strip()}")

    print(f"[merge] Created work branch: {branch} (based on {base_ref})")
    return branch


def _get_conflict_content(repo: Path, filepath: str) -> str:
    """Get the content of a conflicted file (with conflict markers)."""
    file_path = Path(repo) / filepath
    if file_path.exists():
        return file_path.read_text(encoding="utf-8", errors="replace")
    return ""


def _save_conflict_info(repo: Path, conflict_files: list[str], log_dir: Path) -> list[dict]:
    """Save conflict file contents and return structured conflict info."""
    conflicts = []
    for f in conflict_files:
        content = _get_conflict_content(repo, f)
        conflict_file = log_dir / f"{f.replace('/', '_')}.conflict"
        conflict_file.parent.mkdir(parents=True, exist_ok=True)
        conflict_file.write_text(content, encoding="utf-8")
        conflicts.append({
            "file": f,
            "conflict_snapshot": str(conflict_file),
            "size_bytes": len(content),
        })
    return conflicts


def run_merge(
    triton_ascend_path: Path,
    triton_path: Path,
    target_commit: str,
) -> dict:
    """Merge upstream triton *target_commit* into triton-ascend.

    Returns a dict with merge status, branch name, conflict info.
    """
    ascend_path = Path(triton_ascend_path)

    original_branch = run_git(ascend_path, "branch", "--show-current").strip()
    if not original_branch:
        original_branch = run_git(ascend_path, "rev-parse", "HEAD").strip()

    work_branch = _create_work_branch(ascend_path)

    try:
        run_git(ascend_path, "fetch", "upstream-triton", "--prune")
    except subprocess.CalledProcessError:
        print("[merge] Warning: could not fetch upstream-triton, assuming target is reachable")

    if Path(triton_path) != ascend_path:
        try:
            run_git(ascend_path, "fetch", str(triton_path), target_commit)
        except subprocess.CalledProcessError:
            print("[merge] Warning: could not fetch target from triton path")

    print(f"[merge] Merging {target_commit[:12]} into {work_branch}")
    merge_proc = run_git_no_check(
        ascend_path, "merge", "--no-ff", "--no-edit", target_commit
    )

    # Use a timestamped log file so each merge step's output is preserved
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    merge_log_path = WORKSPACE_DIR / f"merge-{ts}.log"
    merge_log_path.write_text(
        f"STDOUT:\n{merge_proc.stdout}\n\nSTDERR:\n{merge_proc.stderr}\n",
        encoding="utf-8",
    )
    # Also write/update the canonical merge log for quick access to the latest
    (WORKSPACE_DIR / MERGE_LOG_FILE).write_text(
        f"STDOUT:\n{merge_proc.stdout}\n\nSTDERR:\n{merge_proc.stderr}\n",
        encoding="utf-8",
    )

    has_conflicts = has_merge_conflicts(ascend_path)
    conflict_files = get_conflict_files(ascend_path) if has_conflicts else []

    conflict_dir = WORKSPACE_DIR / CONFLICT_LOG_DIR
    conflict_info = []
    if has_conflicts:
        conflict_dir.mkdir(parents=True, exist_ok=True)
        conflict_info = _save_conflict_info(ascend_path, conflict_files, conflict_dir)

    result = {
        "work_branch": work_branch,
        "original_branch": original_branch,
        "target_commit": target_commit,
        "merge_exit_code": merge_proc.returncode,
        "has_conflicts": has_conflicts,
        "conflict_files": conflict_files,
        "conflict_count": len(conflict_files),
        "conflicts": conflict_info,
        "merge_log": str(merge_log_path),
        "conflict_dir": str(conflict_dir) if has_conflicts else "",
    }

    result_path = WORKSPACE_DIR / f"merge_result-{ts}.json"
    result_path.write_text(
        json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    # Also write/update the canonical result for quick access to the latest
    (WORKSPACE_DIR / MERGE_RESULT_FILE).write_text(
        json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )

    return result


def run_merge_incremental(
    triton_ascend_path: Path,
    triton_path: Path,
    target_commit: str,
    work_branch: str,
) -> dict:
    """Merge *target_commit* into an already-existing work branch.

    Used for progressive step-by-step merging: the first step calls
    run_merge() to create the work branch, and subsequent steps call
    run_merge_incremental() to merge their end_commit on top.

    Does NOT create a new branch or stash changes — it assumes we're
    already on the work branch from a previous step.
    """
    ascend_path = Path(triton_ascend_path)

    # Verify we're on the expected work branch
    current_branch = run_git(ascend_path, "branch", "--show-current").strip()
    if current_branch != work_branch:
        print(f"[merge] Switching from '{current_branch}' to work branch '{work_branch}'")
        run_git(ascend_path, "checkout", work_branch)

    # Fetch the target commit if needed
    try:
        run_git(ascend_path, "fetch", "upstream-triton", "--prune")
    except subprocess.CalledProcessError:
        print("[merge] Warning: could not fetch upstream-triton, assuming target is reachable")

    if Path(triton_path) != ascend_path:
        try:
            run_git(ascend_path, "fetch", str(triton_path), target_commit)
        except subprocess.CalledProcessError:
            print("[merge] Warning: could not fetch target from triton path")

    print(f"[merge] Incremental merge {target_commit[:12]} into {work_branch}")
    merge_proc = run_git_no_check(
        ascend_path, "merge", "--no-ff", "--no-edit", target_commit
    )

    # Use a timestamped log file so each merge step's output is preserved
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    merge_log_path = WORKSPACE_DIR / f"merge-{ts}.log"
    merge_log_path.write_text(
        f"STDOUT:\n{merge_proc.stdout}\n\nSTDERR:\n{merge_proc.stderr}\n",
        encoding="utf-8",
    )
    # Also write/update the canonical merge log for quick access to the latest
    (WORKSPACE_DIR / MERGE_LOG_FILE).write_text(
        f"STDOUT:\n{merge_proc.stdout}\n\nSTDERR:\n{merge_proc.stderr}\n",
        encoding="utf-8",
    )

    has_conflicts = has_merge_conflicts(ascend_path)
    conflict_files = get_conflict_files(ascend_path) if has_conflicts else []

    conflict_dir = WORKSPACE_DIR / CONFLICT_LOG_DIR
    conflict_info = []
    if has_conflicts:
        conflict_dir.mkdir(parents=True, exist_ok=True)
        conflict_info = _save_conflict_info(ascend_path, conflict_files, conflict_dir)

    result = {
        "work_branch": work_branch,
        "original_branch": current_branch,
        "target_commit": target_commit,
        "merge_exit_code": merge_proc.returncode,
        "has_conflicts": has_conflicts,
        "conflict_files": conflict_files,
        "conflict_count": len(conflict_files),
        "conflicts": conflict_info,
        "merge_log": str(merge_log_path),
        "conflict_dir": str(conflict_dir) if has_conflicts else "",
    }

    result_path = WORKSPACE_DIR / f"merge_result-{ts}.json"
    result_path.write_text(
        json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    # Also write/update the canonical result for quick access to the latest
    (WORKSPACE_DIR / MERGE_RESULT_FILE).write_text(
        json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )

    return result
