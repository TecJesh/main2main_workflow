"""WorkflowContext — shared state carrier between pipeline steps.

Replaces the monolithic ``TA_Main2MainState`` Pydantic model with a
flat dataclass that each pipeline step reads from and returns an updated
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
    triton_path: str = ""
    target_commit: str = ""

    # ── Remote names (set by prepare step) ─────────────────────────────────
    origin_remote: str = "origin"
    upstream_remote: str = "triton-upstream"

    # ── Git state ──────────────────────────────────────────────────────────
    merge_base: str = ""
    ascend_head: str = ""
    original_branch: str = ""

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
    progressive_merge: bool = True

    # ── Merge results (produced by merge step) ────────────────────────────
    merge_has_conflicts: bool = False
    conflict_files: list[str] = field(default_factory=list)

    # ── Build / test results ──────────────────────────────────────────────
    build_passed: bool = False
    test_passed: bool = False
    pytest_passed: bool = False
    fix_errors: list[str] = field(default_factory=list)

    # ── IR patch state ────────────────────────────────────────────────────
    llvm_hash_changed: bool = False
    ir_ops_report: dict = field(default_factory=dict)
    ir_changes_report: dict = field(default_factory=dict)
    ir_patches: list[str] = field(default_factory=list)
    ir_patch_iteration: int = 0
    ir_issues_found: int = 0
    ir_loop_details: list[dict] = field(default_factory=list)

    # ── Retry / fix tracking ──────────────────────────────────────────────
    retry_count: int = 0
    build_fix_count: int = 0
    test_fix_count: int = 0
    conflict_files_resolved: int = 0
    fix_attempts: list[dict] = field(default_factory=list)
    step_details: list[dict] = field(default_factory=list)
    step_pr_descriptions: list[str] = field(default_factory=list)

    # ── Final status ──────────────────────────────────────────────────────
    final_status: str = ""
    pr_url: str = ""
    summary_rows: list[tuple] = field(default_factory=list)

    # ── Other ─────────────────────────────────────────────────────────────
    step_start_ascend_head: str = ""
    ir_analysis_done: bool = False
    ir_fix_count: int = 0
    test_failures_by_python: dict = field(default_factory=dict)

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

    @property
    def step_dir_name(self) -> str:
        """Directory name for the current step's artifacts."""
        if self.total_steps > 1 and self.steps:
            step = self.steps[self.current_step] if self.current_step < len(self.steps) else self.steps[-1]
            return f"steps/{step['id']}"
        return "step-0"
