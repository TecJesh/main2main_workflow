"""Submodule helpers for AscendNPU-IR."""

from __future__ import annotations

from pathlib import Path

from TA_main2main_workflow.utils.git import submodule_has_changes, commit_submodule, run_git

__all__ = ["submodule_has_changes", "commit_submodule", "push_submodule"]


def push_submodule(repo: Path, branch: str) -> bool:
    """Push AscendNPU-IR submodule changes to its remote.

    Returns True on success, False on failure.
    """
    ascend_npu_ir = repo / "third_party" / "ascend" / "AscendNPU-IR"
    if not ascend_npu_ir.exists():
        return False
    try:
        run_git(ascend_npu_ir, "push", "-u", "origin", branch)
        return True
    except Exception:
        return False
