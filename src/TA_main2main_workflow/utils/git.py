"""Git helpers — thin wrappers around ``subprocess.run`` for git operations.

``run_git`` automatically retries on network-related failures (fetch, clone, push).
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

_RETRIES = 3
_RETRY_DELAY = 5  # seconds, multiplied by attempt number

# Operations that auto-retry on any failure (typically network-related)
_RETRY_OPS = {"fetch", "clone", "push", "pull", "remote", "ls-remote"}


def run_git(repo: Path | str, *args: str, quiet: bool = False) -> str:
    """Run a git command in *repo*, return stdout. Raises on failure.

    Automatically retries fetch/clone/push/pull on any failure.
    Set *quiet* to suppress logging (useful for bulk calls like scanning commits).
    """
    from TA_main2main_workflow.utils.logging import get_logger

    log = get_logger("git")
    if not quiet:
        log.info(f"[{Path(repo).name}] $ git {' '.join(args)}")
    operation = args[0] if args else ""

    last_error = ""
    for attempt in range(1, _RETRIES + 1):
        result = subprocess.run(
            ["git", *args],
            cwd=str(repo),
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if result.returncode == 0:
            out = result.stdout.strip()
            if out and not quiet:
                preview = out[:200] + "..." if len(out) > 200 else out
                log.info(f"  → {preview}")
            return result.stdout

        stderr = (result.stderr or "").strip()
        if operation in _RETRY_OPS and attempt < _RETRIES:
            log.warning(f"  retry {attempt}/{_RETRIES}: {stderr[:120]}")
            time.sleep(_RETRY_DELAY * attempt)
            last_error = stderr
            continue

        raise subprocess.CalledProcessError(
            result.returncode, ["git", *args], result.stdout, result.stderr
        )

    raise subprocess.CalledProcessError(
        -1, ["git", *args], "", f"Failed after {_RETRIES} retries: {last_error}"
    )


def run_git_no_check(repo: Path | str, *args: str) -> subprocess.CompletedProcess:
    """Run a git command in *repo*, never raise on non-zero exit."""
    return subprocess.run(
        ["git", *args],
        cwd=str(repo),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
