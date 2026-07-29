"""Git helpers with automatic retry for transient failures.

``run_git`` raises on non-zero exit (after retries for
fetch/clone/push/pull).  ``run_git_no_check`` returns the
``CompletedProcess`` and never raises.
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

RETRYABLE_OPERATIONS = ("fetch", "clone", "push", "pull", "ls-remote")
MAX_RETRIES = 5
RETRY_DELAY = 3  # seconds


def run_git(repo: Path | str, *args: str) -> str:
    """Run a git command in *repo*, return stdout, raise on failure.

    Commands that start with fetch/clone/push/pull/ls-remote are
    automatically retried up to *MAX_RETRIES* times on failure.
    """
    repo = Path(repo)
    cmd = ["git", "-C", str(repo), *args]

    is_retryable = args and args[0] in RETRYABLE_OPERATIONS

    for attempt in range(1, MAX_RETRIES + 1 if is_retryable else 2):
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=600,
        )
        if result.returncode == 0:
            return result.stdout
        if not is_retryable or attempt == MAX_RETRIES:
            raise RuntimeError(
                f"git {args[0]} failed (exit {result.returncode}):\n"
                f"{result.stderr.strip()}"
            )
        time.sleep(RETRY_DELAY)
    return ""  # unreachable


def run_git_no_check(repo: Path | str, *args: str) -> subprocess.CompletedProcess:
    """Run a git command in *repo*, return ``CompletedProcess``, never raise."""
    repo = Path(repo)
    cmd = ["git", "-C", str(repo), *args]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=600)


def stream_cmd(cmd: list[str], cwd: Path, log_fh, timeout: int,
               label: str = "", env: dict | None = None) -> int:
    """Stream subprocess output line-by-line to console and log file.

    Each output line is:
    - written in full to *log_fh* (for post-mortem debugging)
    - printed to the terminal as a single self-updating ``\\r`` line showing
      the last non-empty line (real-time progress)

    If *env* is given, it is merged on top of the parent environment
    (os.environ) before the subprocess is launched.

    Returns the process exit code.  Does NOT raise on non-zero.
    """
    import os as _os
    import sys
    proc_env = _os.environ.copy()
    if env:
        proc_env.update(env)
    proc = subprocess.Popen(
        cmd, cwd=str(cwd), env=proc_env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    assert proc.stdout is not None
    last_line = ""
    for line in proc.stdout:
        log_fh.write(line)
        stripped = line.rstrip()
        if stripped:
            last_line = stripped
            print(f"\r  {stripped[:140]}\033[K", end="", file=sys.stderr, flush=True)
    proc.wait(timeout=timeout)
    if last_line:
        print(file=sys.stderr)  # final newline after \r lines
    return proc.returncode
