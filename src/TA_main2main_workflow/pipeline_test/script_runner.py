"""Pipeline test: subprocess runners for the test script and per-case reruns.

Deliberately NOT built on ``utils/git.py::stream_cmd`` — that helper
does not kill the child on timeout.  The full pipeline-test script runs
for hours, so timeout handling must actually terminate the process
group (bash + pytest + xdist workers).
"""

from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
from pathlib import Path

from TA_main2main_workflow.utils.logging import get_logger

log = get_logger(__name__)


def run_script_streaming(
    script: str,
    cwd: Path,
    log_path: Path,
    timeout: int,
    env: dict[str, str] | None = None,
) -> int:
    """Run *script* via ``bash`` in *cwd*, tee output to console + log.

    Writes a header into *log_path*, then streams every output line to
    both the console and the log file (line-buffered so nothing is lost
    on kill).  Returns the exit code, or -1 on timeout.
    """
    full_env = {**os.environ, **(env or {})}
    with open(log_path, "w", encoding="utf-8", buffering=1) as fh:
        fh.write(f"=== Pipeline test script ===\n{script}\n\n")
        fh.flush()
        proc = subprocess.Popen(
            ["bash", script],
            cwd=str(cwd),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            env=full_env,
            start_new_session=True,
        )
        return _stream_and_wait(proc, fh, timeout)


def run_rerun_command(
    command: str,
    cwd: Path,
    log_path: Path,
    timeout: int,
) -> int:
    """Run an AI-generated single-case rerun command via ``bash -c``.

    Output is captured to *log_path*: when the command has no redirect
    of its own, one is appended so the result is always parseable.
    Returns the exit code, or -1 on timeout.
    """
    cmd = command.strip()
    if ">" not in cmd:
        cmd += f" >> {_shq(str(log_path))} 2>&1"
    with open(log_path, "a", encoding="utf-8", buffering=1) as fh:
        fh.write(f"\n=== rerun: {command.strip()} ===\n")
        fh.flush()
        proc = subprocess.Popen(
            ["bash", "-c", cmd],
            cwd=str(cwd),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            start_new_session=True,
        )
        return _stream_and_wait(proc, fh, timeout)


def _stream_and_wait(proc: subprocess.Popen, fh, timeout: int) -> int:
    """Tee the child's stdout to *fh* and the console; kill on timeout."""
    stop = threading.Event()

    def _reader() -> None:
        try:
            for line in proc.stdout:  # type: ignore[union-attr]
                if stop.is_set():
                    return
                fh.write(line)
                print(line, end="", flush=True)
            fh.flush()
        except (ValueError, OSError):
            pass

    reader = threading.Thread(target=_reader, daemon=True)
    reader.start()
    _start = time.time()
    try:
        rc = proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        stop.set()
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            proc.kill()
        proc.wait()
        rc = -1
        log.warning(f"Command timed out after {timeout}s — process group killed")
    reader.join(timeout=10)
    log.info(f"Command finished in {time.time() - _start:.0f}s, returncode={rc}")
    return rc


def _shq(text: str) -> str:
    """Single-quote *text* for safe embedding in a shell command."""
    return "'" + text.replace("'", "'\\''") + "'"
