"""WorkflowContext — shared state carrier between pipeline steps."""

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

    # ── Input paths (set once at start) ────────────────────────────────
    triton_ascend_path: str = ""
    triton_path: str = ""

    # ── Remote names (set by prepare step) ─────────────────────────────
    origin_remote: str = "origin"
    upstream_remote: str = "triton-upstream"

    # ── Git state ──────────────────────────────────────────────────────
    merge_base: str = ""
    ascend_head: str = ""
    target_commit: str = ""
    work_branch: str = ""
    original_branch: str = ""

    # ── Detection results ──────────────────────────────────────────────
    upstream_commits: list[dict] = field(default_factory=list)
    upstream_commits_count: int = 0
    changed_files_count: int = 0
    changed_lines_total: int = 0
    has_new_commits: bool = False

    # ── Step plan ──────────────────────────────────────────────────────
    steps: list[dict] = field(default_factory=list)
    total_steps: int = 0
    current_step: int = 0
    step_start_ascend_head: str = ""
    step_pr_descriptions: list = field(default_factory=list)

    # ── Merge results ──────────────────────────────────────────────────
    merge_has_conflicts: bool = False
    conflict_files: list[str] = field(default_factory=list)
    conflict_files_resolved: int = 0

    # ── Build / test results ───────────────────────────────────────────
    build_passed: bool = False
    test_passed: bool = False
    fix_errors: list[str] = field(default_factory=list)

    # ── Retry tracking ─────────────────────────────────────────────────
    retry_count: int = 0
    build_fix_count: int = 0
    test_fix_count: int = 0
    fix_attempts: list = field(default_factory=list)
    step_details: list = field(default_factory=list)

    # ── LLVM / IR patch state ──────────────────────────────────────────
    llvm_prefix: str = ""
    llvm_hash_changed: bool = False
    ir_analysis_done: bool = False
    ir_ops_report: dict = field(default_factory=dict)
    ir_changes_report: dict = field(default_factory=dict)
    ir_patches: list = field(default_factory=list)
    ir_patch_iteration: int = 0
    ir_issues_found: int = 0
    ir_fix_count: int = 0
    ir_loop_details: list = field(default_factory=list)

    # ── Pytest state ───────────────────────────────────────────────────
    pytest_passed: bool = False
    test_failures_by_python: dict = field(default_factory=dict)
    test_log_dir: str = ""
    test_dir: str = "third_party/ascend/unittest/pytest_ut"
    num_procs: int = 16
    conda_env: str = ""

    # ── Status / output ────────────────────────────────────────────────
    final_status: str = ""
    pr_url: str = ""
    summary_rows: list = field(default_factory=list)

    # ═══════════════════════════════════════════════════════════════════
    # Helpers
    # ═══════════════════════════════════════════════════════════════════

    def copy_with(self, **kwargs) -> WorkflowContext:
        """Return a new WorkflowContext with the given fields updated."""
        return replace(self, **kwargs)

    @property
    def ascend_path(self) -> Path:
        return Path(self.triton_ascend_path)
