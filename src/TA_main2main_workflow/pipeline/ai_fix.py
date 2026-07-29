"""AI fix pipeline step — spawns opencode to resolve build/test failures.

Also used by the merge conflict resolution step via :func:`run_opencode_adapter`.
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from TA_main2main_workflow.utils.config import TAConfig
from TA_main2main_workflow.utils.context import WorkflowContext
from TA_main2main_workflow.utils.logging import get_logger
from TA_main2main_workflow.utils import STEPS_DIR, WORKSPACE_DIR

log = get_logger(__name__)
_REF = str(Path(__file__).parent.parent / "reference")
_PROMPT_DIR = Path(__file__).parent.parent / "agent"

_PROMPT_FILES: dict[str, str] = {
    "conflict": "prompt_conflict.md",
    "build_fix": "prompt_build_fix.md",
    "test_fix": "prompt_test_fix.md",
}

_TIMEOUT_MINUTES = 30
_STALE_SECONDS = 1200

_MODE_LABELS: dict[str, str] = {
    "conflict": "CONFLICT RESOLUTION",
    "build_fix": "BUILD FIX",
    "test_fix": "TEST FIX",
}


# ═══════════════════════════════════════════════════════════════════════════════
# AIResult
# ═══════════════════════════════════════════════════════════════════════════════


class AIResult(BaseModel):
    modified_files: list[str] = Field(default_factory=list)
    is_noop: bool = Field(default=False)
    step_summary: str = Field(default="")
    elapsed_seconds: float = Field(default=0.0)


# ═══════════════════════════════════════════════════════════════════════════════
# pipeline entry point
# ═══════════════════════════════════════════════════════════════════════════════


def ai_fix(
    ctx: WorkflowContext, config: TAConfig, attempt: int = 1, mode: str = "build_fix"
) -> WorkflowContext:
    """AI fix step — called by build and test phases on failure."""
    if config.skip_ai_analysis:
        log.info("SKIP_AI_ANALYSIS=true — skipping AI fix")
        return ctx
    ascend_path = Path(ctx.triton_ascend_path)
    step = ctx.steps[ctx.current_step] if ctx.current_step < len(ctx.steps) else None
    step_id = step["id"] if step else "step-0"
    step_dir = WORKSPACE_DIR / STEPS_DIR / step_id
    step_dir.mkdir(parents=True, exist_ok=True)

    log.step(attempt, config.max_retries, "AI fix")
    try:
        result = run_opencode_adapter(
            {
                "step_id": f"{step_id}-fix-{attempt}",
                "step_dir": str(step_dir),
                "ascend_path": str(ascend_path),
                "triton_path": ctx.triton_ascend_path,
                "reference_dir": _REF,
                "mode": mode,
                "error_logs": json.dumps(ctx.fix_errors, ensure_ascii=False),
                "target_commit": ctx.target_commit,
                "step_index": f"{ctx.current_step + 1}/{ctx.total_steps}",
            }
        )
        log.ai_result(
            bool(result.modified_files),
            result.modified_files,
            (result.step_summary or "")[:500],
        )
        return ctx
    except Exception as e:
        log.error(f"AI fix failed: {e}")
        return ctx


# ═══════════════════════════════════════════════════════════════════════════════
# opencode adapter
# ═══════════════════════════════════════════════════════════════════════════════


def run_opencode_adapter(inputs: dict[str, Any]) -> AIResult:
    """Run opencode for conflict resolution or test/build fixing."""
    mode = inputs.get("mode", "unknown")
    step_id = inputs.get("step_id", "?")
    mode_label = _MODE_LABELS.get(mode, mode.upper())

    log.info(f"{'═' * 60}")
    log.info(f"  {mode_label}")
    log.info(f"  Step: {step_id}")
    log.info(f"  Time: {time.strftime('%H:%M:%S')}")
    log.info(f"{'═' * 60}")

    t0 = time.monotonic()
    result = _run_opencode(inputs)
    result.elapsed_seconds = time.monotonic() - t0

    icon = "✔" if result.modified_files else "○"
    log.info(f"  {icon} AI task completed in {result.elapsed_seconds:.1f}s")
    if result.modified_files:
        log.info(f"    Modified: {', '.join(result.modified_files)}")
    if result.is_noop:
        log.info(f"    (no changes needed)")

    return result


def _run_opencode(inputs: dict[str, Any]) -> AIResult:
    """Run opencode with JSONL streaming and stale/total timeout protection."""
    prompt = _build_prompt(inputs)
    step_dir = inputs.get("step_dir", "")
    step_path = Path(step_dir) if step_dir else None
    log_path = step_path / "opencode.log" if step_path else None
    raw_path = step_path / "opencode_raw.jsonl" if step_path else None
    stderr_path = step_path / "opencode_stderr.log" if step_path else None

    for p in (log_path, raw_path, stderr_path):
        if p:
            p.write_text("")

    _print_prompt(prompt)
    if log_path:
        _log_prompt(prompt, log_path)

    lines, stop_reason = _run_opencode_once(prompt, log_path, raw_path, stderr_path)

    if stop_reason and stderr_path and stderr_path.exists():
        stderr_content = stderr_path.read_text(encoding="utf-8", errors="replace")[
            -2000:
        ]
        if stderr_content:
            log.info(f"[opencode] stderr tail:\n{stderr_content}")

    result = _build_result(step_path, inputs.get("ascend_path", ""), "".join(lines))
    if stop_reason and not result.step_summary:
        result.step_summary = f"opencode stopped due to {stop_reason}"
    return result


def _run_opencode_once(
    prompt: str,
    log_path: Path | None,
    raw_path: Path | None,
    stderr_path: Path | None,
) -> tuple[list[str], str | None]:
    """Launch opencode, stream JSONL output, enforce timeout. Returns (lines, stop_reason)."""
    stderr_fh = stderr_path.open("a", encoding="utf-8") if stderr_path else None
    proc = subprocess.Popen(
        [
            "opencode",
            "run",
            "--format",
            "json",
            "--dangerously-skip-permissions",
            prompt,
        ],
        stdout=subprocess.PIPE,
        stderr=stderr_fh or subprocess.DEVNULL,
        text=True,
        bufsize=1,
        env=_subprocess_env(),
    )

    lines_queue: queue.Queue[str | None] = queue.Queue()

    def _stdout_reader() -> None:
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
    stop_reason: str | None = None

    try:
        while True:
            try:
                line = lines_queue.get(timeout=1.0)
            except queue.Empty:
                now = time.monotonic()
                if now > deadline:
                    log.info(
                        f"[opencode] TOTAL TIMEOUT ({_TIMEOUT_MINUTES}min), killing process"
                    )
                    proc.kill()
                    stop_reason = "total_timeout"
                    break
                if now - last_output_time > _STALE_SECONDS:
                    log.info(
                        f"[opencode] STALE TIMEOUT ({_STALE_SECONDS}s no output), killing process"
                    )
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
                _log_opencode_event(line, log_fh)
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
                display = (
                    output
                    if len(output) <= 2000
                    else output[:2000] + "\n... [truncated]"
                )
                print(
                    f"\n  {'─' * 56}\n  [AI output]\n  {display}\n  {'─' * 56}",
                    flush=True,
                )


def _log_opencode_event(line: str, fh: Any) -> None:
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
# helpers
# ═══════════════════════════════════════════════════════════════════════════════


def _build_prompt(inputs: dict[str, Any]) -> str:
    from collections import defaultdict

    mode = inputs.get("mode", "build_fix")
    prompt_file = _PROMPT_FILES.get(mode, "prompt_build_fix.md")
    template = (_PROMPT_DIR / prompt_file).read_text(encoding="utf-8")
    ctx = defaultdict(str, {k: str(v) for k, v in inputs.items()})
    return template.format_map(ctx)


def _print_prompt(prompt: str) -> None:
    log.info(f"{'━' * 60}")
    log.info(f"  AI TASK PROMPT")
    log.info(f"{'━' * 60}")
    if len(prompt) > 8000:
        print(prompt[:4000])
        print(
            f"\n... [{len(prompt) - 8000} chars truncated, see log for full prompt] ...\n"
        )
        print(prompt[-4000:])
    else:
        print(prompt)
    log.info(f"{'━' * 60}")


def _log_prompt(prompt: str, log_path: Path) -> None:
    with log_path.open("a", encoding="utf-8") as fh:
        fh.write(f"{'═' * 60}\nAI TASK PROMPT:\n{'═' * 60}\n{prompt}\n{'═' * 60}\n\n")


def _subprocess_env() -> dict:
    """Environment for launching opencode.

    Sets IS_SANDBOX=1 when running as root (CI containers) to allow
    --dangerously-skip-permissions.
    """
    env = os.environ.copy()
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        env.setdefault("IS_SANDBOX", "1")
    return env


def _build_result(
    step_dir: Path | None, ascend_path: str, output_text: str
) -> AIResult:
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
