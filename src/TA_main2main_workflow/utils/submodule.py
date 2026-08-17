"""AscendNPU-IR submodule helpers.

Detects changes in ``third_party/ascend/AscendNPU-IR`` and handles
commit + push for the submodule before the parent repo is committed.
"""

from __future__ import annotations

import os
from pathlib import Path

from TA_main2main_workflow.utils.git import run_git, run_git_no_check
from TA_main2main_workflow.utils.logging import get_logger

log = get_logger(__name__)

_SUBMODULE_DIR = "third_party/ascend/AscendNPU-IR"
SUBMODULE_DIR = _SUBMODULE_DIR  # public alias for other pipeline modules
_NPUIR_REMOTE = "npuir-push"


def _submodule_path(repo: Path) -> Path:
    """Return the absolute path to the AscendNPU-IR submodule."""
    return repo / _SUBMODULE_DIR


def submodule_has_changes(repo: Path) -> bool:
    """Return True if the AscendNPU-IR submodule has uncommitted changes."""
    sp = _submodule_path(repo)
    if not sp.exists():
        return False
    result = run_git_no_check(sp, "status", "--porcelain")
    return bool(result.stdout.strip())


def commit_submodule(repo: Path, commit_msg: str) -> bool:
    """Stage all changes and commit in the AscendNPU-IR submodule.

    Returns True if a commit was created, False if there was nothing to commit.
    """
    sp = _submodule_path(repo)
    if not sp.exists():
        log.info("AscendNPU-IR submodule not found — skipping")
        return False

    if not submodule_has_changes(repo):
        return False

    log.info("Committing AscendNPU-IR submodule changes...")
    try:
        # -u: stage tracked changes only — submodule runtime artifacts
        # must not enter commits.
        run_git(sp, "add", "-u")
        run_git(sp, "commit", "-s", "-m", commit_msg)
        log.status(True, "AscendNPU-IR submodule committed")
        return True
    except Exception as e:
        if "nothing to commit" not in str(getattr(e, "stderr", "")):
            log.warning(f"Submodule commit failed: {e}")
        return False


def push_submodule(repo: Path, branch: str | None = None, force: bool = True) -> bool:
    """Push the AscendNPU-IR submodule to its dedicated remote.

    Creates/updates a branch at current HEAD and pushes using
    GH_TOKEN for authentication.  By default uses ``--force-with-lease``.

    Returns True on success.
    """
    sp = _submodule_path(repo)
    if not sp.exists():
        log.info("AscendNPU-IR submodule not found — skipping push")
        return False

    # Ensure the npuir-push remote exists
    result = run_git_no_check(sp, "remote")
    if _NPUIR_REMOTE not in result.stdout:
        npuir_url = os.getenv("ASCENDNPU_IR_PUSH_URL", "")
        if not npuir_url:
            log.warning("ASCENDNPU_IR_PUSH_URL not set — cannot push submodule")
            return False
        # Embed GH_TOKEN in URL if available
        token = os.getenv("GH_TOKEN", "")
        if token and "@" not in npuir_url and npuir_url.startswith("https://"):
            npuir_url = npuir_url.replace("https://", f"https://{token}@")
        run_git(sp, "remote", "add", _NPUIR_REMOTE, npuir_url)

    if branch is None:
        branch = f"sync-{run_git(sp, 'rev-parse', '--short', 'HEAD').strip()}"

    # Create/update branch at current HEAD
    run_git(sp, "checkout", "-B", branch)

    log.info(f"Pushing AscendNPU-IR branch '{branch}'...")
    try:
        push_args = ["push"]
        if force:
            push_args.append("--force-with-lease")
        push_args.extend([_NPUIR_REMOTE, branch])
        run_git(sp, *push_args)
        log.status(True, f"AscendNPU-IR pushed to {branch}")
        return True
    except Exception as e:
        log.error(f"Submodule push failed: {e}")
        return False
