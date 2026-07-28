"""Simple phase timer for the workflow pipeline.

Usage::

    from TA_main2main_workflow.utils.tracker import timed, total_elapsed

    with timed("build"):
        ...  # build work

    print(f"Total: {total_elapsed():.1f}s")
"""

from __future__ import annotations

import time
from contextlib import contextmanager

_flow_start_time: float | None = None
_phase_times: dict[str, float] = {}


@contextmanager
def timed(name: str):
    """Context manager that records elapsed wall-clock time for *name*."""
    global _flow_start_time
    if _flow_start_time is None:
        _flow_start_time = time.time()
    start = time.time()
    try:
        yield
    finally:
        elapsed = time.time() - start
        _phase_times[name] = elapsed


def total_elapsed() -> float:
    """Return total seconds since the first ``timed()`` call."""
    if _flow_start_time is None:
        return 0.0
    return time.time() - _flow_start_time
