"""Git utilities with auto-retry for network operations."""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

from TA_main2main_workflow.utils.logging import get_logger

log = get_logger("git")

_RETRIES = 3
_RETRY_DELAY = 5
_RETRY_OPS = {"fetch", "clone", "push", "pull", "remote", "ls-remote"}


def run_git(repo: Path | str, *args: str, quiet: bool = False) -> str:
    """Run a git command in *repo*, return stdout.

    Auto-retries for network operations (fetch, clone, push, etc.).
    Raises subprocess.CalledProcessError on failure.
    """
    repo_path = Path(repo)
    repo_name = repo_path.name if repo_path.is_dir() else str(repo)
    cmd = ["git", "-C", str(repo_path), *args]
    op = args[0] if args else ""

    if not quiet:
        log.info(f"[{repo_name}] $ git {' '.join(args)}")

    last_exc = None
    for attempt in range(1, (_RETRIES if op in _RETRY_OPS else 1) + 1):
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, check=True, timeout=300)
            # Log preview on success
            if not quiet and result.stdout.strip():
                preview = result.stdout.strip()[:200]
                if len(result.stdout.strip()) > 200:
                    preview += "..."
                log.info(f"  → {preview}")
            return result.stdout
        except subprocess.CalledProcessError as e:
            last_exc = e
            if op in _RETRY_OPS and attempt < _RETRIES:
                delay = _RETRY_DELAY * attempt
                log.warning(
                    f"git {op} failed (attempt {attempt}/{_RETRIES}) — "
                    f"retrying in {delay}s: {e.stderr.strip()[-200:]}")
                time.sleep(delay)
            else:
                log.error(f"git {op} failed: {e.stderr.strip()[-500:]}")
                raise

    raise last_exc  # type: ignore[misc]


def run_git_no_check(repo: Path | str, *args: str) -> subprocess.CompletedProcess:
    """Run a git command, never raise on non-zero exit. Returns CompletedProcess."""
    repo_path = Path(repo)
    cmd = ["git", "-C", str(repo_path), *args]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=300)


def submodule_has_changes(repo: Path) -> bool:
    """Check whether the AscendNPU-IR submodule has uncommitted changes."""
    ascend_npu_ir = repo / "third_party" / "ascend" / "AscendNPU-IR"
    if not ascend_npu_ir.exists():
        return False
    result = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=str(ascend_npu_ir),
        capture_output=True, text=True, timeout=30,
    )
    return bool(result.stdout.strip())


def commit_submodule(repo: Path, message: str) -> None:
    """Commit changes inside the AscendNPU-IR submodule."""
    ascend_npu_ir = repo / "third_party" / "ascend" / "AscendNPU-IR"
    if not submodule_has_changes(repo):
        return
    subprocess.run(
        ["git", "add", "-A"], cwd=str(ascend_npu_ir),
        capture_output=True, text=True, timeout=30,
    )
    subprocess.run(
        ["git", "commit", "-s", "-m", message],
        cwd=str(ascend_npu_ir),
        capture_output=True, text=True, timeout=30,
    )
    log.info("Committed AscendNPU-IR submodule changes")
