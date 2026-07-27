"""Timing utilities — context manager for phase-level timing."""

from __future__ import annotations

import time
from contextlib import contextmanager

from TA_main2main_workflow.utils.logging import get_logger

log = get_logger("tracker")

_flow_start_time: float = 0.0


@contextmanager
def timed(name: str):
    """Context manager that records elapsed time for a named phase."""
    global _flow_start_time
    if _flow_start_time == 0.0:
        _flow_start_time = time.time()
    start = time.time()
    try:
        yield
    finally:
        elapsed = time.time() - start
        log.info(f"  ⏱  {name} took {elapsed:.1f}s")


def total_elapsed() -> float:
    """Return total seconds since the first timed() call."""
    if _flow_start_time == 0.0:
        return 0.0
    return time.time() - _flow_start_time
