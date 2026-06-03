#!/usr/bin/env python3
"""Update version tracking references after a successful upstream sync.

For Triton-Ascend (a fork of Triton), update the version tracking file
(version.txt) to record the new upstream commit that was synced. Also
creates a sync metadata file in the workspace for audit trail.

Output:
  - Updated version.txt in triton-ascend repo
  - workspace/sync_meta.json with sync details
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from TA_main2main_workflow.utils import (
    WORKSPACE_DIR, run_git, run_git_no_check,
    print_section, print_status, print_info, print_key_value,
)


def _read_version_file(repo: Path) -> str | None:
    """Read the current version.txt if it exists."""
    version_path = repo / "version.txt"
    if version_path.exists():
        return version_path.read_text(encoding="utf-8").strip()
    return None


def _write_version_file(repo: Path, version: str) -> None:
    """Write the version.txt file."""
    version_path = repo / "version.txt"
    version_path.write_text(version + "\n", encoding="utf-8")


def _get_commit_date(repo: Path, commit: str) -> str:
    """Get ISO date of a commit."""
    try:
        return run_git(repo, "log", "-1", "--format=%cI", commit).strip()
    except Exception:
        return ""


def run_update(
    ascend_path: Path,
    old_commit: str,
    new_commit: str,
    work_branch: str = "",
) -> dict:
    """Update version tracking after successful upstream sync.

    Args:
        ascend_path: Path to the triton-ascend repository
        old_commit: Previous upstream commit (merge-base before sync)
        new_commit: New upstream commit that was synced
        work_branch: Name of the work branch used for the sync

    Returns:
        dict with 'files_updated' list and 'sync_meta'
    """
    print_section("Update Commit Reference")

    files_updated: list[str] = []

    old_version = _read_version_file(ascend_path)
    short_sha = new_commit[:12]
    sync_date = datetime.now().strftime("%Y-%m-%d")

    if old_version:
        print_info(f"Current version.txt: {old_version}")
    else:
        print_info("No version.txt found — creating one")

    new_version = f"upstream-triton-{short_sha}-synced-{sync_date}"
    _write_version_file(ascend_path, new_version)
    files_updated.append("version.txt")
    print_status(True, f"version.txt updated: {new_version}")

    try:
        run_git(ascend_path, "add", "version.txt")
    except Exception:
        pass

    ascend_head = run_git(ascend_path, "rev-parse", "HEAD").strip()
    old_commit_date = _get_commit_date(ascend_path, old_commit)
    new_commit_date = _get_commit_date(ascend_path, new_commit)

    sync_meta = {
        "sync_date": sync_date,
        "old_upstream_commit": old_commit,
        "new_upstream_commit": new_commit,
        "old_commit_date": old_commit_date,
        "new_commit_date": new_commit_date,
        "triton_ascend_head": ascend_head,
        "work_branch": work_branch,
        "version_txt": new_version,
    }

    meta_path = WORKSPACE_DIR / "sync_meta.json"
    meta_path.write_text(
        json.dumps(sync_meta, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    print_key_value("Old upstream", f"{old_commit[:12]} ({old_commit_date[:10]})")
    print_key_value("New upstream", f"{new_commit[:12]} ({new_commit_date[:10]})")
    print_key_value("Ascend HEAD", ascend_head[:12])
    print_key_value("Sync metadata", str(meta_path))
    print_status(True, f"Updated {len(files_updated)} file(s)")

    return {
        "files_updated": files_updated,
        "sync_meta": sync_meta,
    }
