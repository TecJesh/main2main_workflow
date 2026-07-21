"""Execution timer — tracks phase-level and total elapsed time."""

from __future__ import annotations

import time
from contextlib import contextmanager

# Module-level state
_flow_start_time: float = 0.0


@contextmanager
def timed(name: str):
    """Context manager: record elapsed time for a named phase.

    Usage::

        with timed("merge"):
            ctx = merge_upstream_commit(ctx, config)
    """
    global _flow_start_time
    if not _flow_start_time:
        _flow_start_time = time.monotonic()
    start = time.monotonic()
    try:
        yield
    finally:
        elapsed = time.monotonic() - start
        from TA_main2main_workflow.utils.logging import get_logger

        get_logger(__name__).info(f"⏱  {name} took {elapsed:.1f}s")


def total_elapsed() -> float:
    """Return total seconds since the first timed() call."""
    if _flow_start_time:
        return time.monotonic() - _flow_start_time
    return 0.0


# Backward compat
def start_timer(name: str) -> None:
    """Deprecated: use ``with timed(name):`` instead."""
    pass


def stop_timer(name: str) -> float:
    """Deprecated: use ``with timed(name):`` instead."""
    return 0.0
