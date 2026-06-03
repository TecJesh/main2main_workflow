"""AI adapter — spawns opencode or claude subprocess for AI-driven tasks.

Supports two backends (auto-detected or set via AI_BACKEND env var):
  - opencode: `opencode run --format json --dangerously-skip-permissions <prompt>`
  - claude:   `claude -p --dangerously-skip-permissions <prompt>`

Used for both merge conflict resolution and test failure fixing.
All progress is printed to the local console — no CrewAI web UI needed.

Key design for claude backend:
  - Do NOT use proc.communicate() — it blocks until process exit with zero output.
  - Instead, write prompt to stdin in a background thread, read stdout line-by-line
    in real time, just like the opencode backend.
  - Print a heartbeat "." every 15 seconds of silence so the user knows it's alive.
  - Same stale-timeout / total-timeout logic as opencode.
"""

from __future__ import annotations

import json
import os
import queue
import select
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

_PROMPT_PATH = Path(__file__).parent / "prompt.md"

_TIMEOUT_MINUTES = 30
_STALE_SECONDS = 300
_MAX_STALE_RETRIES = 3
_HEARTBEAT_INTERVAL = 15  # print "." every 15s of silence (claude only)


# ── backend detection ────────────────────────────────────────────────────────

def _detect_backend() -> str:
    """Detect which AI backend to use. Checks AI_BACKEND env var first,
    then falls back to whatever is available on PATH."""
    explicit = os.getenv("AI_BACKEND", "").lower()
    if explicit in ("claude", "opencode"):
        return explicit
    if shutil.which("opencode"):
        return "opencode"
    if shutil.which("claude"):
        return "claude"
    raise RuntimeError(
        "No AI backend found. Install 'opencode' or 'claude' CLI, "
        "or set AI_BACKEND env var."
    )


# ── mode labels for display ──────────────────────────────────────────────────

_MODE_LABELS: dict[str, str] = {
    "conflict": "CONFLICT RESOLUTION",
    "fix": "TEST/BUILD FAILURE FIX",
    "adapt": "CODE ADAPTATION",
}


# ── prompt loader ────────────────────────────────────────────────────────────

def _build_prompt(inputs: dict[str, Any]) -> str:
    template = _PROMPT_PATH.read_text(encoding="utf-8")
    ctx = {k: str(v) for k, v in inputs.items()}
    return template.format_map(ctx)


# ── result model ─────────────────────────────────────────────────────────────

class AIResult(BaseModel):
    modified_files: list[str] = Field(default_factory=list)
    is_noop: bool = Field(default=False)
    step_summary: str = Field(default="")
    resolved_conflicts: list[str] = Field(default_factory=list)
    fixed_tests: list[str] = Field(default_factory=list)
    elapsed_seconds: float = Field(default=0.0)


# ── main entry point ─────────────────────────────────────────────────────────

def run_opencode_adapter(inputs: dict[str, Any]) -> AIResult:
    """Run the AI adapter for conflict resolution or test fixing.

    Auto-detects available backend (opencode or claude) and streams
    all output to the local console.
    """
    backend = _detect_backend()
    mode = inputs.get("mode", "unknown")
    step_id = inputs.get("step_id", "?")
    mode_label = _MODE_LABELS.get(mode, f"AI TASK: {mode}")

    print(f"\n{'═' * 60}", flush=True)
    print(f"  {mode_label}", flush=True)
    print(f"  Backend: {backend}  |  Step: {step_id}", flush=True)
    print(f"  Time: {time.strftime('%H:%M:%S')}", flush=True)
    print(f"{'═' * 60}", flush=True)

    t0 = time.monotonic()

    # AI call: dispatch to claude or opencode backend
    if backend == "claude":
        result = _run_claude(inputs)
    else:
        result = _run_opencode(inputs)

    result.elapsed_seconds = time.monotonic() - t0

    # AI call: print completion summary
    icon = "✔" if result.modified_files or result.resolved_conflicts else "○"
    print(f"\n  {icon} AI task completed in {result.elapsed_seconds:.1f}s", flush=True)
    if result.modified_files:
        print(f"    Modified: {', '.join(result.modified_files)}", flush=True)
    if result.resolved_conflicts:
        print(f"    Resolved conflicts: {', '.join(result.resolved_conflicts)}", flush=True)
    if result.is_noop:
        print(f"    (no changes needed)", flush=True)

    return result


# ═══════════════════════════════════════════════════════════════════════════════
# opencode backend (JSONL streaming)
# ═══════════════════════════════════════════════════════════════════════════════

def _run_opencode(inputs: dict[str, Any]) -> AIResult:
    """opencode backend: JSONL streaming with stale-timeout retry."""
    base_prompt = _build_prompt(inputs)
    prompt = base_prompt
    step_dir = inputs.get("step_dir", "")
    step_path = Path(step_dir) if step_dir else None
    log_path = step_path / "opencode.log" if step_path else None
    raw_path = step_path / "opencode_raw.jsonl" if step_path else None
    stderr_path = step_path / "opencode_stderr.log" if step_path else None

    if log_path:
        log_path.write_text("")
    if raw_path:
        raw_path.write_text("")
    if stderr_path:
        stderr_path.write_text("")

    all_lines: list[str] = []
    last_reason: _StopReason | None = None

    for attempt in range(_MAX_STALE_RETRIES + 1):
        _print_prompt(prompt, attempt)
        if log_path:
            _log_prompt(prompt, attempt, log_path)

        lines, reason = _run_opencode_once(prompt, log_path, raw_path, stderr_path)
        all_lines.extend(lines)
        last_reason = reason

        if reason is None:
            break

        if reason == "stale_timeout" and attempt < _MAX_STALE_RETRIES:
            retry = attempt + 1
            print(f"\n[opencode] retrying after stale timeout ({retry}/{_MAX_STALE_RETRIES})", flush=True)
            prompt = _build_opencode_continue(base_prompt, inputs, retry)
            continue

        if stderr_path and stderr_path.exists():
            stderr_content = stderr_path.read_text(encoding="utf-8", errors="replace")[-2000:]
            if stderr_content:
                print(f"\n[opencode] stderr tail:\n{stderr_content}", flush=True)
        break

    result = _build_result(step_path, inputs.get("ascend_path", ""), "".join(all_lines))
    if last_reason and not result.step_summary:
        result.step_summary = f"opencode process stopped due to {last_reason}"
    return result


def _build_opencode_continue(base_prompt: str, inputs: dict[str, Any], retry: int) -> str:
    return f"""Continue the task for step {inputs.get('step_id', '')}.

The previous opencode run produced no output for {_STALE_SECONDS} seconds and
was terminated. This is continuation retry {retry}/{_MAX_STALE_RETRIES}.

Do not start from scratch. The triton-ascend working tree may already contain
partial changes from the previous attempt. Inspect existing changes, reuse prior
work, and continue from where you left off.

━━━ ORIGINAL TASK ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

{base_prompt}
"""


_StopReason = Literal["stale_timeout", "total_timeout"]


def _print_prompt(prompt: str, attempt: int) -> None:
    title = "AI TASK PROMPT" if attempt == 0 else f"AI CONTINUE PROMPT #{attempt}"
    print(f"\n{'━' * 60}", flush=True)
    print(f"  {title}", flush=True)
    print(f"{'━' * 60}", flush=True)
    if len(prompt) > 8000:
        print(prompt[:4000])
        print(f"\n... [{len(prompt) - 8000} chars truncated, see log for full prompt] ...\n")
        print(prompt[-4000:])
    else:
        print(prompt)
    print(f"{'━' * 60}\n", flush=True)


def _log_prompt(prompt: str, attempt: int, log_path: Path) -> None:
    title = "AI TASK PROMPT" if attempt == 0 else f"AI CONTINUE PROMPT #{attempt}"
    with log_path.open("a", encoding="utf-8") as fh:
        fh.write(f"{'═' * 60}\n{title}:\n{'═' * 60}\n{prompt}\n{'═' * 60}\n\n")


def _run_opencode_once(
    prompt: str,
    log_path: Path | None,
    raw_path: Path | None,
    stderr_path: Path | None,
) -> tuple[list[str], _StopReason | None]:
    stderr_fh = stderr_path.open("a", encoding="utf-8") if stderr_path else None
    proc = subprocess.Popen(
        [
            "opencode", "run",
            "--format", "json",
            "--dangerously-skip-permissions",
            prompt,
        ],
        stdout=subprocess.PIPE,
        stderr=stderr_fh or subprocess.DEVNULL,
        text=True,
        bufsize=1,
    )

    lines_queue: queue.Queue[str | None] = queue.Queue()

    def _stdout_reader():
        assert proc.stdout is not None
        for line in proc.stdout:
            lines_queue.put(line)
        lines_queue.put(None)

    reader_thread = threading.Thread(target=_stdout_reader, daemon=True)
    reader_thread.start()

    state = _EventState()
    log_fh = log_path.open("a", encoding="utf-8") if log_path else None
    raw_fh = raw_path.open("a", encoding="utf-8") if raw_path else None

    deadline = time.monotonic() + _TIMEOUT_MINUTES * 60
    last_output_time = time.monotonic()
    stop_reason: _StopReason | None = None

    try:
        while True:
            try:
                line = lines_queue.get(timeout=1.0)
            except queue.Empty:
                now = time.monotonic()
                if now > deadline:
                    print(f"\n[opencode] TOTAL TIMEOUT ({_TIMEOUT_MINUTES}min), killing process", flush=True)
                    proc.kill()
                    stop_reason = "total_timeout"
                    break
                if now - last_output_time > _STALE_SECONDS:
                    print(f"\n[opencode] STALE TIMEOUT ({_STALE_SECONDS}s no output), killing process", flush=True)
                    proc.kill()
                    stop_reason = "stale_timeout"
                    break
                continue

            if line is None:
                break

            last_output_time = time.monotonic()
            state.lines.append(line)
            if raw_fh:
                raw_fh.write(line)
            _print_opencode_event(line, state)
            if log_fh:
                _log_opencode_event(line, state, log_fh)
    finally:
        if log_fh:
            log_fh.close()
        if raw_fh:
            raw_fh.close()
        if stderr_fh:
            stderr_fh.close()

    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        stop_reason = stop_reason or "total_timeout"
        proc.wait(timeout=10)

    return state.lines, stop_reason


class _EventState:
    def __init__(self) -> None:
        self.lines: list[str] = []
        self._tool_by_call: dict[str, str] = {}
        self._line_count: int = 0


def _print_opencode_event(line: str, state: _EventState) -> None:
    try:
        ev = json.loads(line)
    except json.JSONDecodeError:
        return

    t = ev.get("type")
    part = ev.get("part", {})

    if t == "text":
        text = part.get("text", "")
        if text:
            print(text, end="", flush=True)
            state._line_count += text.count("\n")

    elif t == "tool_use":
        tool = part.get("tool", "")
        call_id = part.get("callID", "")
        st = part.get("state", {})
        status = st.get("status", "")
        inp = st.get("input", {})

        if status == "pending":
            state._tool_by_call[call_id] = tool
            brief = json.dumps(inp, ensure_ascii=False)[:200]
            print(f"\n  > [AI: {tool}] {brief}", flush=True)

        elif status == "completed":
            output = st.get("output", "")
            if output:
                display = output if len(output) <= 2000 else output[:2000] + "\n... [truncated]"
                print(f"\n  {'─' * 56}\n  [AI output]\n  {display}\n  {'─' * 56}", flush=True)


def _log_opencode_event(line: str, state: _EventState, fh: Any) -> None:
    try:
        ev = json.loads(line)
    except json.JSONDecodeError:
        fh.write(line)
        return

    t = ev.get("type")
    part = ev.get("part", {})

    if t == "text":
        text = part.get("text", "")
        if text:
            fh.write(text)

    elif t == "tool_use":
        tool = part.get("tool", "")
        st = part.get("state", {})
        inp = json.dumps(st.get("input", {}), ensure_ascii=False)
        fh.write(f"\n[AI: {tool}] <- {inp[:500]}\n")
        output = st.get("output", "")
        if output:
            fh.write(f"{'─' * 60}\n[output]\n{output[:4000]}\n{'─' * 60}\n")

    fh.flush()


# ═══════════════════════════════════════════════════════════════════════════════
# claude backend (streaming via `claude -p`)
# ═══════════════════════════════════════════════════════════════════════════════
#
# Unlike proc.communicate() which blocks until the process exits (zero output
# in the meantime), this implementation streams stdout line-by-line in real
# time. A heartbeat "." is printed every 15s of silence to show liveness.

def _run_claude(inputs: dict[str, Any]) -> AIResult:
    """Run Claude Code with real-time streaming output.

    DESIGN NOTE — why we don't use proc.communicate():
      communicate() blocks until the process EXITS. This means ZERO output
      is visible for up to 30 minutes, making it look like a hang.
      Instead, we:
        1. Write the prompt to stdin in a background thread
        2. Read stdout line-by-line with a 1-second select() timeout
        3. Print each line immediately to the terminal
        4. Print a heartbeat "." every 15s of silence
        5. Kill the process if total timeout (30min) or stale timeout (5min
           no output) is reached
    """
    prompt = _build_prompt(inputs)
    step_dir = inputs.get("step_dir", "")
    step_path = Path(step_dir) if step_dir else None
    log_path = step_path / "opencode.log" if step_path else None
    stderr_path = step_path / "opencode_stderr.log" if step_path else None

    if log_path:
        log_path.write_text("")
    if stderr_path:
        stderr_path.write_text("")

    _print_prompt(prompt, 0)
    if log_path:
        _log_prompt(prompt, 0, log_path)

    print(f"\n  > [claude] Starting Claude Code (timeout={_TIMEOUT_MINUTES}min)...", flush=True)
    print(f"     (streaming output in real time — '.' = still thinking)", flush=True)

    # ── Launch claude ──────────────────────────────────────────────────────
    stderr_fh = stderr_path.open("a", encoding="utf-8") if stderr_path else None
    proc = subprocess.Popen(
        ["claude", "-p", "--dangerously-skip-permissions"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=stderr_fh or subprocess.DEVNULL,
        text=True,
        bufsize=1,
    )

    # ── Write prompt to stdin in background thread ─────────────────────────
    def _write_stdin():
        assert proc.stdin is not None
        try:
            proc.stdin.write(prompt)
            proc.stdin.close()
        except (BrokenPipeError, OSError):
            pass

    stdin_thread = threading.Thread(target=_write_stdin, daemon=True)
    stdin_thread.start()

    # ── Read stdout line-by-line in real time ──────────────────────────────
    output_lines: list[str] = []
    log_fh = log_path.open("a", encoding="utf-8") if log_path else None

    deadline = time.monotonic() + _TIMEOUT_MINUTES * 60
    last_output_time = time.monotonic()
    last_heartbeat = time.monotonic()
    stop_reason: _StopReason | None = None

    try:
        assert proc.stdout is not None
        while True:
            line = _read_line_with_timeout(proc.stdout, timeout=1.0)

            if line is None:
                now = time.monotonic()
                if now > deadline:
                    print(f"\n  [claude] TOTAL TIMEOUT ({_TIMEOUT_MINUTES}min), killing process", flush=True)
                    proc.kill()
                    stop_reason = "total_timeout"
                    break
                if now - last_output_time > _STALE_SECONDS:
                    print(f"\n  [claude] STALE TIMEOUT ({_STALE_SECONDS}s no output), killing process", flush=True)
                    proc.kill()
                    stop_reason = "stale_timeout"
                    break
                if now - last_heartbeat > _HEARTBEAT_INTERVAL:
                    print(".", end="", flush=True)
                    last_heartbeat = now
                continue

            if line == "":
                break

            last_output_time = time.monotonic()
            last_heartbeat = time.monotonic()

            print(line, end="", flush=True)
            output_lines.append(line)

            if log_fh:
                log_fh.write(line)
    finally:
        if log_fh:
            log_fh.close()
        if stderr_fh:
            stderr_fh.close()

    # ── Wait for process to finish ─────────────────────────────────────────
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        stop_reason = stop_reason or "total_timeout"
        proc.wait(timeout=10)

    stdout_data = "".join(output_lines)

    # ── Print exit status ──────────────────────────────────────────────────
    if proc.returncode != 0:
        print(f"\n  [claude] exited with code {proc.returncode}", flush=True)
        if stderr_path and stderr_path.exists():
            stderr_tail = stderr_path.read_text(encoding="utf-8", errors="replace")[-2000:]
            if stderr_tail:
                print(f"  [claude] stderr tail:\n{stderr_tail}", flush=True)
    else:
        if last_heartbeat > last_output_time:
            print(flush=True)

    if stop_reason:
        print(f"  [claude] Stopped due to: {stop_reason}", flush=True)

    return _build_result(step_path, inputs.get("ascend_path", ""), stdout_data)


def _read_line_with_timeout(stream: Any, timeout: float) -> str | None:
    """Read a line from *stream* with a per-read *timeout* using select().

    This is the key to non-blocking stdout reading. Without it, readline()
    blocks until data arrives, preventing us from checking timeout/deadline
    conditions.

    Returns:
        A line string (with trailing newline) when data is available,
        "" (empty string) on EOF,
        None when *timeout* expires with no data available.
    """
    ready, _, _ = select.select([stream], [], [], timeout)
    if not ready:
        return None
    line = stream.readline()
    return line  # "" on EOF, "text\n" otherwise


# ═══════════════════════════════════════════════════════════════════════════════
# shared result builder
# ═══════════════════════════════════════════════════════════════════════════════

def _build_result(step_dir: Path | None, ascend_path: str, output_text: str) -> AIResult:
    """Build AIResult from AI output: extract summary, detect modified files."""
    summary = ""
    if step_dir:
        summary_path = step_dir / "step_summary.md"
        if summary_path.exists():
            summary = summary_path.read_text(encoding="utf-8")

    if not summary:
        summary = output_text[-4000:] if output_text else ""

    modified_files = _modified_files(ascend_path)
    return AIResult(
        modified_files=modified_files,
        is_noop=not modified_files,
        step_summary=summary,
    )


def _modified_files(ascend_path: str) -> list[str]:
    if not ascend_path:
        return []
    try:
        result = subprocess.run(
            ["git", "diff", "--name-only", "HEAD"],
            cwd=ascend_path,
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError:
        return []
    return [line for line in result.stdout.splitlines() if line]
