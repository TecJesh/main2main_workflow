#!/usr/bin/env python3
"""Detect the commit gap between triton-ascend and upstream triton.

For a merge-based workflow (Triton-Ascend is a fork of Triton), we:
  1. Find the merge-base between the current triton-ascend branch and the
     upstream triton target commit.
  2. List commits on the upstream side since that merge-base.
  3. Determine total changed files and lines for planning.

Output: workspace/detect.json
"""

from __future__ import annotations

import json
from pathlib import Path

from TA_main2main_workflow.utils import (
    WORKSPACE_DIR, DETECT_FILE, run_git, get_repo_head, get_merge_base,
)


def _list_upstream_commits(repo: Path, merge_base: str, target: str) -> list[dict]:
    """List commits between merge_base and target, ordered chronologically."""
    log_output = run_git(
        repo, "log", "--reverse", "--format=%H%x1f%s",
        f"{merge_base}..{target}"
    )
    commits: list[dict] = []
    for line in log_output.strip().splitlines():
        if not line.strip():
            continue
        parts = line.split("\x1f", 1)
        commits.append({
            "sha": parts[0].strip(),
            "subject": parts[1].strip() if len(parts) > 1 else "",
        })
    return commits


def _count_changed_lines(repo: Path, merge_base: str, target: str) -> dict:
    """Count changed lines in key source directories."""
    dirs = ["python/triton/", "lib/", "include/", "third_party/nvidia/", "third_party/amd/"]
    result = {}
    total = 0
    for d in dirs:
        try:
            output = run_git(
                repo, "diff", "--numstat", merge_base, target, "--", f":(top){d}"
            )
        except Exception:
            result[d] = 0
            continue
        lines = 0
        for line in output.strip().splitlines():
            if not line.strip():
                continue
            parts = line.split("\t")
            if len(parts) >= 3:
                added = int(parts[0]) if parts[0] != "-" else 0
                deleted = int(parts[1]) if parts[1] != "-" else 0
                lines += added + deleted
        result[d] = lines
        total += lines
    result["total"] = total
    return result


def _changed_files(repo: Path, merge_base: str, target: str) -> list[str]:
    """Return list of changed files between merge_base and target."""
    output = run_git(repo, "diff", "--name-only", merge_base, target)
    return sorted(f for f in output.strip().splitlines() if f)


def detect(
    triton_ascend_path: Path,
    triton_path: Path,
    target_commit: str | None = None,
) -> tuple[dict, bool]:
    """Detect upstream commits that need to be merged.

    Returns (result_dict, has_new_commits).
    """
    ascend_head = get_repo_head(triton_ascend_path)
    target = target_commit if target_commit else get_repo_head(triton_path)

    if not target_commit:
        try:
            run_git(triton_ascend_path, "fetch", "upstream-triton", "--prune")
        except Exception:
            print("[detect] Warning: could not fetch upstream-triton, using local refs")

    merge_base = get_merge_base(triton_ascend_path, ascend_head, target)
    commits = _list_upstream_commits(triton_path, merge_base, target)
    has_new = len(commits) > 0 and merge_base != target

    result = {
        "ascend_head": ascend_head,
        "target_commit": target,
        "merge_base": merge_base,
        "upstream_commits_count": len(commits),
        "upstream_commits": commits,
        "changed_lines": _count_changed_lines(triton_path, merge_base, target),
        "changed_files": _changed_files(triton_path, merge_base, target),
        "changed_files_count": len(_changed_files(triton_path, merge_base, target)),
    }

    (WORKSPACE_DIR / DETECT_FILE).write_text(
        json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )

    return result, has_new
