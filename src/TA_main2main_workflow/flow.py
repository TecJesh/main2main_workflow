"""TA Main2Main Workflow — Triton-Ascend upstream sync orchestrator.

Assembles pipeline steps::

    prepare → detect → plan → for each step:
      merge → [resolve] → build⇄fix → test⇄fix → commit
    → finalize → [push_pr]
"""

from __future__ import annotations

from pathlib import Path

from TA_main2main_workflow.utils.config import TAConfig
from TA_main2main_workflow.utils.context import WorkflowContext
from TA_main2main_workflow.utils.logging import get_logger
from TA_main2main_workflow.utils.tracker import timed
from TA_main2main_workflow.utils import (
    UpgradeCompleted,
    UpgradeFailed,
    WORKSPACE_DIR,
)
from TA_main2main_workflow.pipeline.prepare import prepare
from TA_main2main_workflow.pipeline.detect import run_detect
from TA_main2main_workflow.pipeline.plan import run_plan
from TA_main2main_workflow.pipeline.merge import merge_upstream_commit
from TA_main2main_workflow.pipeline.resolve import resolve_conflicts
from TA_main2main_workflow.pipeline.build import build
from TA_main2main_workflow.pipeline.test import test
from TA_main2main_workflow.pipeline.commit import commit_step
from TA_main2main_workflow.pipeline.finalize import finalize

log = get_logger(__name__)


class TA_Main2MainFlow:
    """Orchestrator — builds context, runs pipeline steps, handles PR."""

    def __init__(self, config: TAConfig | None = None) -> None:
        self.config = config or TAConfig.from_env()

    def run(self) -> str:
        """Execute the full sync pipeline. Returns UpgradeCompleted or UpgradeFailed."""
        log.header("Triton-Ascend Upstream Sync")
        log.key_value("AI Backend", self.config.ai_backend)
        log.key_value("Max Retries", str(self.config.max_retries))

        # ── Phase 0: Prepare workspace ────────────────────────────────────
        with timed("prepare"):
            ctx = prepare(WorkflowContext(), self.config)

        # ── Phase 1: Detect ─────────────────────────────────────────────
        log.header("Phase 1: Detect Upstream Commits")
        with timed("detect"):
            ctx = run_detect(ctx, self.config)
        if not ctx.has_new_commits:
            log.status(True, "Already up to date")
            return UpgradeCompleted
        log.status(True, f"Found {ctx.upstream_commits_count} upstream commits")

        # ── Phase 2: Plan ───────────────────────────────────────────────
        log.header("Phase 2: Plan Steps")
        with timed("plan"):
            ctx = run_plan(ctx, self.config)
        log.status(True, f"Planned {ctx.total_steps} step(s)")

        # ── Phase 3: Per-step loop ──────────────────────────────────────
        while ctx.current_step < ctx.total_steps:
            step = ctx.steps[ctx.current_step]
            sid = step["id"]
            log.header(f"Step {ctx.current_step + 1}/{ctx.total_steps}: {sid}")
            log.key_value("commits", str(step["commit_count"]))
            log.key_value("end commit", step["end_commit"][:12])
            ctx = ctx.copy_with(retry_count=0)

            with timed("merge"):
                ctx = merge_upstream_commit(ctx, self.config)
            if ctx.merge_has_conflicts:
                log.status(False, f"Merge has {len(ctx.conflict_files)} conflict(s)")
            else:
                log.status(True, "Merge clean")

            if ctx.merge_has_conflicts:
                with timed("resolve"):
                    ctx = resolve_conflicts(ctx, self.config)
                if ctx.merge_has_conflicts:
                    log.error(f"Conflicts unresolved for {sid}")
                    return UpgradeFailed
                log.status(True, "Conflicts resolved")

            ctx = build(ctx, self.config)
            if not ctx.build_passed:
                log.error(f"Build failed for {sid}")
                return UpgradeFailed

            ctx = test(ctx, self.config)
            if not ctx.test_passed:
                log.error(f"Tests failed for {sid}")
                return UpgradeFailed

            ctx = commit_step(ctx, self.config)
            ctx = ctx.copy_with(current_step=ctx.current_step + 1)
            log.status(
                True, f"Step {sid} completed ({ctx.current_step}/{ctx.total_steps})"
            )

        # ── Phase 4: Finalize ───────────────────────────────────────────
        ctx = finalize(ctx)

        if self.config.push_to_github:
            self._push_pr(ctx)

        return UpgradeCompleted

    def _push_pr(self, ctx: WorkflowContext) -> None:
        from TA_main2main_workflow.pipeline.push_pr import push_and_create_pr

        try:
            pr_url = push_and_create_pr(
                ascend_path=Path(ctx.triton_ascend_path),
                github_repo=self.config.github_repo,
                summary_path=WORKSPACE_DIR / "final_summary.md",
                target_commit=ctx.target_commit,
            )
            log.status(True, f"PR created: {pr_url}")
        except Exception as e:
            log.error(f"Failed to create PR: {e}")
