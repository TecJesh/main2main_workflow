"""Rich logging for pipeline steps — TALogger with formatted output."""

from __future__ import annotations

import logging
import sys
import time
from datetime import datetime
from typing import Any


class TALogger(logging.getLoggerClass()):  # type: ignore
    """Logger with pipeline-specific formatting methods."""

    def header(self, title: str) -> None:
        width = max(68, len(title) + 6)
        self.info("")
        self.info("╔" + "═" * (width - 2) + "╗")
        self.info(f"║  {title}".ljust(width - 1) + "║")
        self.info("╚" + "═" * (width - 2) + "╝")

    def section(self, title: str) -> None:
        ts = datetime.now().strftime("%H:%M:%S")
        self.info(f"\n── {title}  [{ts}] ──")

    def step(self, num: int, total: int, name: str) -> None:
        ts = datetime.now().strftime("%H:%M:%S")
        self.info(f"\n▸ [{num}/{total}] {name} @ {ts}")

    def status(self, ok: bool, msg: str) -> None:
        icon = "✔" if ok else "✘"
        self.info(f"  {icon} {msg}")

    def warn(self, msg: str, *args: Any, **kwargs: Any) -> None:
        self.warning(f"  ⚠ {msg}", *args, **kwargs)

    def error(self, msg: str, *args: Any, **kwargs: Any) -> None:
        self.error_raw(f"  ✘ {msg}", *args, **kwargs)

    def error_raw(self, msg: str, *args: Any, **kwargs: Any) -> None:
        super().error(msg, *args, **kwargs)

    def key_value(self, key: str, value: Any) -> None:
        self.info(f"    {key}: {value}")

    def info(self, msg: str = "", *args: Any, **kwargs: Any) -> None:
        super().info(msg, *args, **kwargs)

    def ai_call(self, backend: str, mode: str, attempt: int, max_attempts: int) -> None:
        self.info("╭─ AI Call ──────────────────────────────────────────╮")
        self.info(f"│  Backend: {backend}   Mode: {mode}   Attempt: {attempt}/{max_attempts}")
        self.info("╰────────────────────────────────────────────────────╯")

    def ai_result(self, ok: bool, modified_files: list = (), summary: str = "") -> None:
        self.info("╭─ AI Result ────────────────────────────────────────╮")
        self.info(f"│  {'✔' if ok else '✘'} modified: {len(modified_files) if modified_files else 0} file(s)")
        if summary:
            for line in summary[:500].splitlines()[:5]:
                self.info(f"│  {line[:72]}")
        self.info("╰────────────────────────────────────────────────────╯")

    def table(self, rows: list[tuple[str, str, str]]) -> None:
        self.info("")
        self.info("  SYNC SUMMARY")
        self.info("  " + "─" * 50)
        for phase, status, detail in rows:
            icon = "✔" if status == "PASS" else ("✘" if status == "FAIL" else "⚠")
            self.info(f"  {icon} {phase:<25} {detail}")
        self.info("  " + "─" * 50)

    def elapsed(self, seconds: float) -> None:
        m, s = divmod(int(seconds), 60)
        h, m = divmod(m, 60)
        if h:
            self.info(f"  Total elapsed: {h}h {m}m {s}s")
        elif m:
            self.info(f"  Total elapsed: {m}m {s}s")
        else:
            self.info(f"  Total elapsed: {s}s")

    def flow_progress(self, phase: str, detail: str = "") -> None:
        ts = datetime.now().strftime("%H:%M:%S")
        msg = f"[{ts}] [{phase}] {detail}" if detail else f"[{ts}] [{phase}]"
        self.info(msg)

    def conflict_list(self, files: list[str]) -> None:
        self.info(f"    Conflicted files ({len(files)}):")
        for i, f in enumerate(files, 1):
            self.info(f"      {i}. {f}")


# ── Module-level setup ────────────────────────────────────────────────────
logging.setLoggerClass(TALogger)


def get_logger(name: str) -> TALogger:
    """Return a configured TALogger for *name*."""
    log = logging.getLogger(name)
    if not log.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(logging.Formatter("%(message)s"))
        log.addHandler(handler)
        log.setLevel(logging.INFO)
        log.propagate = False
    return log  # type: ignore[return-value]


# Default logger for quick imports
default_logger = get_logger("ta-workflow")
