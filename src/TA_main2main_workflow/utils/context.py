"""WorkflowContext — shared state carrier between pipeline steps.

A flat dataclass that each pipeline step reads from and returns an updated
copy of.  Steps never mutate the context in place.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any


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

    # ── Remote names (set by prepare step) ─────────────────────────────────
    origin_remote: str = "origin"
    upstream_remote: str = "triton-upstream"

    # ── Git state ──────────────────────────────────────────────────────────
    merge_base: str = ""
    ascend_head: str = ""
    target_commit: str = ""
    work_branch: str = ""
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
    step_start_ascend_head: str = ""

    # ── Merge results (produced by merge step) ────────────────────────────
    merge_has_conflicts: bool = False
    conflict_files: list[str] = field(default_factory=list)

    # ── Build / test results ──────────────────────────────────────────────
    build_passed: bool = False
    test_passed: bool = False
    pytest_passed: bool = False
    fix_errors: list[str] = field(default_factory=list)
    test_log_dir: str = ""
    test_failures_by_python: dict = field(default_factory=dict)

    # ── Fix tracking ──────────────────────────────────────────────────────
    build_fix_count: int = 0
    test_fix_count: int = 0
    conflict_files_resolved: int = 0
    retry_count: int = 0
    fix_attempts: list[dict] = field(default_factory=list)

    # ── IR patch state ────────────────────────────────────────────────────
    ir_analysis_done: bool = False
    ir_ops_report: dict = field(default_factory=dict)
    ir_changes_report: dict = field(default_factory=dict)
    ir_patches: list = field(default_factory=list)
    ir_patch_iteration: int = 0
    ir_max_iterations: int = 3
    ir_issues_found: int = 0
    ir_fix_count: int = 0
    llvm_hash_changed: bool = False
    ir_loop_details: list[dict] = field(default_factory=list)

    # ── Step tracking / reporting ─────────────────────────────────────────
    step_details: list[dict] = field(default_factory=list)
    step_pr_descriptions: list[str] = field(default_factory=list)
    summary_rows: list[tuple] = field(default_factory=list)

    # ── External test results ─────────────────────────────────────────────
    external_test_passed: bool = False
    external_test_results: list[dict] = field(default_factory=list)
    # Per-repo entry: {"repo": "Liger-Kernel", "passed": True, "failed_cases": [], "fix_count": 0}

    # ── Final state ───────────────────────────────────────────────────────
    final_status: str = ""
    pr_url: str = ""

    # ═══════════════════════════════════════════════════════════════════════
    # Helpers
    # ═══════════════════════════════════════════════════════════════════════

    def copy_with(self, **kwargs: Any) -> WorkflowContext:
        """Return a new WorkflowContext with the given fields updated.

        Usage::

            ctx = ctx.copy_with(build_passed=True, retry_count=1)
        """
        return replace(self, **kwargs)

    @property
    def ascend_path(self) -> Path:
        return Path(self.triton_ascend_path)
