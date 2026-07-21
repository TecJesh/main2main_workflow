"""Logging setup for TA_main2main_workflow.

Uses Python's standard ``logging`` module with a custom formatter that
preserves the visual style of the old ``console.py`` (headers, sections,
status icons) while routing everything through the logging framework.

Usage::

    from TA_main2main_workflow.utils.logging import get_logger
    log = get_logger(__name__)
    log.info("Starting sync...")
    log.header("Phase 1: Detect")       # boxed header
    log.section("Build Triton-Ascend")  # section divider
    log.step(1, 3, "AI fix")           # step indicator
    log.status(True, "Build passed")    # ✔ / ✘
    log.key_value("target", "abc123")   # key: value
    log.table(rows)                     # summary table
"""

from __future__ import annotations

import logging
import sys
from datetime import datetime
from typing import Any


# ═══════════════════════════════════════════════════════════════════════════
# Custom logger class
# ═══════════════════════════════════════════════════════════════════════════


class TALogger(logging.getLoggerClass()):
    """Logger with extra formatting methods for workflow output."""

    def header(self, title: str) -> None:
        width = 72
        self.info(f"\n╔{'═' * width}╗")
        self.info(f"║ {title:^{width}} ║")
        self.info(f"╚{'═' * width}╝")

    def section(self, title: str) -> None:
        ts = datetime.now().strftime("%H:%M:%S")
        self.info(f"\n{'─' * 60}")
        self.info(f"  [{ts}] {title}")
        self.info(f"{'─' * 60}")

    def step(self, num: int, total: int, name: str) -> None:
        ts = datetime.now().strftime("%H:%M:%S")
        self.info(f"\n  ▸ [{num}/{total}] {name}  @ {ts}")

    def status(self, ok: bool, msg: str) -> None:
        icon = "✔" if ok else "✘"
        self.info(f"    {icon} {msg}")

    def warn(self, msg: str, *args, **kwargs) -> None:
        # Override to use consistent prefix
        super().warning(f"    ⚠ {msg}", *args, **kwargs)

    def error(self, msg: str, *args, **kwargs) -> None:
        super().error(f"    ✘ {msg}", *args, **kwargs)

    def key_value(self, key: str, value: Any) -> None:
        self.info(f"    {key}: {value}")

    def flow_progress(self, phase: str, detail: str = "") -> None:
        ts = datetime.now().strftime("%H:%M:%S")
        msg = f"[{ts}] [{phase}] {detail}" if detail else f"[{ts}] [{phase}]"
        self.info(msg)

    def conflict_list(self, files: list[str]) -> None:
        if not files:
            self.info("    ℹ No conflicts")
            return
        self.info(f"    Conflicted files ({len(files)}):")
        for i, f in enumerate(files, 1):
            self.info(f"      {i}. {f}")

    def ai_call(self, backend: str, mode: str, attempt: int, max_attempts: int) -> None:
        ts = datetime.now().strftime("%H:%M:%S")
        self.info(f"\n  ╭─ AI Call ─────────────────────────────────────────────")
        self.info(f"  │ Backend:  {backend}")
        self.info(f"  │ Mode:     {mode}")
        self.info(f"  │ Attempt:  {attempt}/{max_attempts}")
        self.info(f"  │ Time:     {ts}")
        self.info(f"  ╰──────────────────────────────────────────────────────")

    def ai_result(
        self, ok: bool, modified_files: list[str] = (), summary: str = ""
    ) -> None:
        icon = "✔" if ok else "✘"
        self.info(f"\n  ╭─ AI Result ───────────────────────────────────────────")
        self.info(f"  │ Status: {icon} {'Success' if ok else 'Failed'}")
        if modified_files:
            self.info(f"  │ Modified files ({len(modified_files)}):")
            for f in modified_files:
                self.info(f"  │   • {f}")
        if summary:
            preview = summary[:500] + "..." if len(summary) > 500 else summary
            self.info(f"  │ Summary: {preview}")
        self.info(f"  ╰──────────────────────────────────────────────────────")

    def table(self, rows: list[tuple[str, str, str]]) -> None:
        ts = datetime.now().strftime("%H:%M:%S")
        status_icons = {"PASS": "✔", "FAIL": "✘", "SKIP": "○", "WARN": "⚠"}
        self.info(f"\n{'═' * 72}")
        self.info(f"  SYNC SUMMARY  @ {ts}")
        self.info(f"{'═' * 72}")
        self.info(f"  {'Phase':<30} {'Status':<8} {'Details'}")
        self.info(f"  {'─' * 30} {'─' * 8} {'─' * 32}")
        for step, status, detail in rows:
            icon = status_icons.get(status, "?")
            self.info(f"  {step:<30} {icon} {status:<5} {detail}")
        self.info(f"{'═' * 72}")

    def elapsed(self, seconds: float) -> None:
        self.info(f"\n  ⏱  Total elapsed: {seconds:.1f}s ({seconds / 60:.1f}m)")


# ═══════════════════════════════════════════════════════════════════════════
# Setup
# ═══════════════════════════════════════════════════════════════════════════

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


# Default logger for simple imports
default_logger = get_logger("ta-workflow")
