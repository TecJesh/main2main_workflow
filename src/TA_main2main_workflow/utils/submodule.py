"""AscendNPU-IR submodule helpers."""
from __future__ import annotations
import os, subprocess
from pathlib import Path
from TA_main2main_workflow.utils.logging import get_logger
from TA_main2main_workflow.utils.git import run_git, run_git_no_check

log = get_logger(__name__)
_ASCENDNPU_IR_SUBMODULE = "third_party/ascend/AscendNPU-IR"
_ASCENDNPU_IR_REMOTE = "https://github.com/TecJesh/AscendNPU-IR.git"
_ASCENDNPU_IR_REMOTE_NAME = "npuir-push"

def _submodule_path(repo: Path) -> Path:
    return repo / _ASCENDNPU_IR_SUBMODULE

def submodule_has_changes(repo: Path) -> bool:
    sm = _submodule_path(repo)
    if not sm.exists(): return False
    return bool(run_git_no_check(sm, "status", "--porcelain").stdout.strip())

def commit_submodule(repo: Path, commit_msg: str) -> bool:
    sm = _submodule_path(repo)
    if not sm.exists(): return False
    if not submodule_has_changes(repo):
        log.info("[submodule] No changes")
        return False
    log.section("Commit AscendNPU-IR Submodule")
    try:
        run_git(sm, "add", "-A")
        run_git(sm, "commit", "-s", "-m", commit_msg)
        log.status(True, f"Committed AscendNPU-IR: {run_git(sm, 'rev-parse', 'HEAD').strip()[:12]}")
        return True
    except subprocess.CalledProcessError as e:
        if "nothing to commit" in str(getattr(e, 'stderr', '')).lower():
            log.info("[submodule] Nothing to commit")
            return False
        log.warning(f"Could not commit submodule: {e}")
        return False

def push_submodule(repo: Path, branch: str, remote: str = _ASCENDNPU_IR_REMOTE, remote_name: str = _ASCENDNPU_IR_REMOTE_NAME, force: bool = False) -> bool:
    sm = _submodule_path(repo)
    if not sm.exists():
        log.warning("[submodule] Not found")
        return False
    log.section("Push AscendNPU-IR Submodule")
    run_git_no_check(sm, "remote", "remove", remote_name)
    run_git(sm, "remote", "add", remote_name, remote)
    head = run_git(sm, "rev-parse", "HEAD").strip()
    run_git_no_check(sm, "branch", "-f", branch, head)
    gh_token = os.getenv("GH_TOKEN", "")
    if gh_token:
        try:
            url = run_git(sm, "remote", "get-url", remote_name).strip()
            if url.startswith("https://") and "x-access-token" not in url:
                clean = url.replace("https://", "", 1).split("@", 1)[-1] if "@" in url else url.replace("https://", "", 1)
                run_git(sm, "remote", "set-url", remote_name, f"https://x-access-token:{gh_token}@{clean}")
        except Exception: pass
    try:
        run_git(sm, "push", "--force-with-lease" if not force else "--force", remote_name, branch)
        log.status(True, f"Pushed AscendNPU-IR branch '{branch}'")
        return True
    except Exception as e:
        log.error(f"Failed to push AscendNPU-IR: {e}")
        return False
