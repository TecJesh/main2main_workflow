"""WorkflowContext — shared state carrier between pipeline steps.

A flat dataclass that each pipeline step reads from and returns an updated
copy of.  Steps never mutate the context in place.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path


@dataclass
class WorkflowContext:
    """All mutable state that flows through the sync pipeline.

    Each step function takes a ``WorkflowContext``, reads what it needs,
    and returns a **new** instance with updated fields (via
    :meth:`copy_with`).  This makes data flow explicit and testable.
    """

    # ── Input configuration (set once at start) ────────────────────────────
    triton_ascend_path: str = ""

    # ── Remote names (set by prepare step) ─────────────────────────────────
    origin_remote: str = "origin"
    upstream_remote: str = "triton-upstream"

    # ── Git state ──────────────────────────────────────────────────────────
    merge_base: str = ""
    ascend_head: str = ""
    target_commit: str = ""

    # ── Detection results (produced by detect step) ───────────────────────
    upstream_commits: list[dict] = field(default_factory=list)
    upstream_commits_count: int = 0
    changed_files_count: int = 0
    changed_lines_total: int = 0
    has_new_commits: bool = False

    # ── Step plan (produced by plan step) ──────────────────────────────────
    steps: list[dict] = field(default_factory=list)
    total_steps: int = 0
    current_step: int = 0

    # ── Merge results (produced by merge step) ────────────────────────────
    merge_has_conflicts: bool = False
    conflict_files: list[str] = field(default_factory=list)

    # ── Build / test results ──────────────────────────────────────────────
    build_passed: bool = False
    test_passed: bool = False
    fix_errors: list[str] = field(default_factory=list)

    # ── Retry tracking ────────────────────────────────────────────────────
    retry_count: int = 0

    # ═══════════════════════════════════════════════════════════════════════
    # Helpers
    # ═══════════════════════════════════════════════════════════════════════

    def copy_with(self, **kwargs) -> WorkflowContext:
        """Return a new WorkflowContext with the given fields updated.

        Usage::

            ctx = ctx.copy_with(build_passed=True, retry_count=1)
        """
        return replace(self, **kwargs)

    @property
    def ascend_path(self) -> Path:
        return Path(self.triton_ascend_path)
