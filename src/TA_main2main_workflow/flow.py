"""TA Main2Main Workflow — Triton-Ascend upstream sync orchestrator.

Assembles pipeline steps for single-step mode::

    prepare → detect → plan → [build_baseline_llvm] → for each step:
      merge → [resolve] →
        if LLVM hash changed: per_step_ir_patch (apply existing → build LLVM →
          build TA → test → supplement IR → loop)
        else: build_and_fix_loop → test_and_fix_loop
      → [external_test] → commit
    → finalize → [push_pr]
"""

from __future__ import annotations

from pathlib import Path

from TA_main2main_workflow.utils.config import TAConfig
from TA_main2main_workflow.utils.context import WorkflowContext
from TA_main2main_workflow.utils.git import run_git
from TA_main2main_workflow.utils.logging import get_logger
from TA_main2main_workflow.utils.tracker import timed, total_elapsed
from TA_main2main_workflow.utils import (
    UpgradeCompleted,
    UpgradeFailed,
    WORKSPACE_DIR,
)
from TA_main2main_workflow.pipeline.prepare import prepare
from TA_main2main_workflow.pipeline.detect import run_detect
from TA_main2main_workflow.pipeline.plan import run_plan, llvm_hash_changed_after_merge
from TA_main2main_workflow.pipeline.merge import merge_upstream_commit
from TA_main2main_workflow.pipeline.resolve import resolve_conflicts
from TA_main2main_workflow.pipeline.build import build_and_fix_loop
from TA_main2main_workflow.pipeline.test import test_and_fix_loop
from TA_main2main_workflow.external_test import (
    load_external_test_config,
    run_external_tests,
)
from TA_main2main_workflow.pipeline.commit import commit_step
from TA_main2main_workflow.pipeline.finalize import finalize
from TA_main2main_workflow.pipeline.ir_patch import (
    build_baseline_llvm,
    per_step_ir_patch,
)

log = get_logger(__name__)


class TA_Main2MainFlow:
    """Orchestrator — builds context, runs pipeline steps, handles PR.

    Single-step mode is the only supported mode.  Each step runs the full
    pipeline: merge → resolve conflicts → build → fix → test → fix →
    external_test → commit.
    """

    def __init__(self, config: TAConfig | None = None) -> None:
        self.config = config or TAConfig.from_env()

    def run(self) -> str:
        """Execute the full sync pipeline. Returns UpgradeCompleted or UpgradeFailed."""
        log.header("Triton-Ascend Upstream Sync (Single-Step Mode)")
        log.key_value("AI Backend", self.config.ai_backend)
        log.key_value("Max Retries", str(self.config.max_retries))
        log.key_value("Line Budget", str(self.config.line_budget))

        # ── Phase 0: Prepare workspace ──────────────────────────────
        with timed("prepare"):
            ctx = prepare(WorkflowContext(), self.config)

        # ── Phase 1: Detect ─────────────────────────────────────────
        log.header("Phase 1: Detect Upstream Commits")
        with timed("detect"):
            ctx = run_detect(ctx, self.config)
        if not ctx.has_new_commits:
            log.status(True, "Already up to date")
            ctx = ctx.copy_with(final_status=UpgradeCompleted)
            ctx.summary_rows.append(("Detect", "SKIP", "No new upstream commits"))
            return UpgradeCompleted
        log.status(True, f"Found {ctx.upstream_commits_count} upstream commits")

        # ── Phase 2: Plan ───────────────────────────────────────────
        log.header("Phase 2: Plan Steps")
        with timed("plan"):
            ctx = run_plan(ctx, self.config)
        log.status(True, f"Planned {ctx.total_steps} step(s)")

        # ── Phase 2.5: Build baseline LLVM (pre-merge) ──────────────
        log.header("Build Baseline LLVM")
        with timed("baseline-llvm"):
            ctx = build_baseline_llvm(ctx, self.config)
        if not ctx.build_passed:
            log.error("Baseline LLVM build failed — cannot proceed")
            return UpgradeFailed
        log.status(True, "Baseline LLVM ready")

        # ── Phase 3: Per-step loop ──────────────────────────────────
        log.header("Single-Step Mode — Per-Step Full Pipeline")
        log.key_value("Total steps", str(ctx.total_steps))

        while ctx.current_step < ctx.total_steps:
            step = ctx.steps[ctx.current_step]
            step_id = step["id"]
            ctx = ctx.copy_with(retry_count=0)

            log.header(f"Step {ctx.current_step + 1}/{ctx.total_steps}: {step_id}")
            log.key_value("commits in step", str(step["commit_count"]))
            log.key_value("end commit", step["end_commit"][:12])
            reason = step.get("reason", "line_budget")
            log.key_value("step reason", reason)

            # Record ascend HEAD before this step
            ascend_path = Path(ctx.triton_ascend_path)
            step_start_head = run_git(ascend_path, "rev-parse", "HEAD").strip()
            ctx = ctx.copy_with(step_start_ascend_head=step_start_head)

            # ── Step A: Merge ───────────────────────────────────
            with timed("merge"):
                ctx = merge_upstream_commit(ctx, self.config)
            if ctx.merge_has_conflicts:
                log.status(False, f"Merge has {len(ctx.conflict_files)} conflict(s)")
            else:
                log.status(True, "Merge clean")

            # ── Step B: Resolve conflicts ───────────────────────
            if ctx.merge_has_conflicts:
                with timed("resolve"):
                    ctx = resolve_conflicts(ctx, self.config)
                if ctx.merge_has_conflicts:
                    log.error(f"Conflicts unresolved for {step_id}")
                    ctx = ctx.copy_with(final_status=UpgradeFailed)
                    return UpgradeFailed
                log.status(True, "Conflicts resolved")

            # ── Step C: Build/Test — IR patch or standard ───────
            llvm_hash_changed = reason == "llvm_version"
            if not llvm_hash_changed:
                llvm_hash_changed = llvm_hash_changed_after_merge(ctx)
            if llvm_hash_changed:
                if reason != "llvm_version":
                    log.info(
                        f"[{step_id}] LLVM hash changed during merge "
                        f"(post-merge detection) — routing to IR patch pipeline"
                    )
                log.section(f"LLVM Version Change in {step_id} — IR Patch Pipeline")
                with timed("ir-patch"):
                    ctx = per_step_ir_patch(ctx, self.config, step)
                if not ctx.build_passed:
                    log.error(f"IR patch pipeline failed for {step_id}")
                    ctx = ctx.copy_with(final_status=UpgradeFailed)
                    return UpgradeFailed
            else:
                # Standard build + fix
                log.section(f"Build & Fix — {step_id}")
                with timed("build"):
                    ctx = build_and_fix_loop(ctx, self.config)
                if not ctx.build_passed:
                    log.error(f"Build failed for {step_id}")
                    ctx = ctx.copy_with(final_status=UpgradeFailed)
                    return UpgradeFailed

                # Standard test + fix
                log.section(f"Test & Fix — {step_id}")
                with timed("test"):
                    ctx = test_and_fix_loop(ctx, self.config)
                if not ctx.test_passed:
                    log.error(f"Tests failed for {step_id}")
                    ctx = ctx.copy_with(final_status=UpgradeFailed)
                    return UpgradeFailed

            # ── Step D: External test ──────────────────────────
            ctx = self._run_external_test_stage(ctx)
            if ctx.final_status == UpgradeFailed:
                return UpgradeFailed

            # ── Step E: Commit ──────────────────────────────────
            with timed("commit"):
                ctx = commit_step(ctx, self.config)

            # Record step description for PR body
            desc = (
                f"✅ **{step_id}**: {step['commit_count']} commits, "
                f"end_commit=`{step['end_commit'][:12]}`, "
                f"source lines={step.get('source_changed_lines', '?')}, "
                f"reason={reason}"
            )
            ctx.step_pr_descriptions.append(desc)

            # Record per-step detail for sync report
            ctx.step_details.append(
                {
                    "step_id": step_id,
                    "step_index": ctx.current_step + 1,
                    "commits": step["commit_count"],
                    "end_commit": step["end_commit"][:12],
                    "source_lines": step.get("source_changed_lines", 0),
                    "conflict_files": len(ctx.conflict_files),
                    "build_fixes": ctx.build_fix_count,
                    "test_fixes": ctx.test_fix_count,
                    "retries": ctx.retry_count,
                    "reason": reason,
                }
            )

            # Advance to next step
            ctx = ctx.copy_with(current_step=ctx.current_step + 1)
            log.status(
                True,
                f"Step {step_id} completed ({ctx.current_step}/{ctx.total_steps})",
            )

        # ── Phase 4: Finalize ───────────────────────────────────────
        log.header("Phase 4: Finalize")
        with timed("finalize"):
            ctx = finalize(ctx, self.config)

        # ── Phase 5: Push PR ────────────────────────────────────────
        if self.config.push_to_github:
            self._push_pr(ctx)

        ctx.summary_rows.append(
            (
                "Single-Step Sync",
                "PASS",
                f"{ctx.total_steps} step(s), branch: {ctx.work_branch}",
            )
        )
        log.table(ctx.summary_rows)
        log.elapsed(total_elapsed())

        ctx = ctx.copy_with(final_status=UpgradeCompleted)
        return UpgradeCompleted

    def _run_external_test_stage(self, ctx: WorkflowContext) -> WorkflowContext:
        """Run external operator repo tests (if enabled).

        Loads the YAML config, checks the enabled flag, and executes the
        external test pipeline.  In ``inline`` mode failures are fatal;
        in ``standalone`` mode failures are recorded but don't block.
        """
        external_cfg = load_external_test_config(self.config.external_test_config)

        if external_cfg is None:
            # No config file found — not an error, just nothing to do
            return ctx

        if not external_cfg.enabled:
            log.info("External test is disabled (enabled=false) — skipped")
            return ctx

        if external_cfg.mode == "off":
            log.info("External test mode is 'off' — skipped")
            return ctx

        log.section(f"External Test — {external_cfg.mode} mode")
        with timed("external-test"):
            ctx = run_external_tests(ctx, self.config, external_cfg)

        if external_cfg.mode == "inline" and not ctx.external_test_passed:
            log.error("External tests failed (inline mode)")
            ctx = ctx.copy_with(final_status=UpgradeFailed)

        if ctx.external_test_passed:
            log.status(True, "External tests passed")
        else:
            log.status(False, "External tests failed")

        return ctx

    def _push_pr(self, ctx: WorkflowContext) -> None:
        """Push work branch and create GitHub PR."""
        from TA_main2main_workflow.pipeline.push_pr import push_and_create_pr

        try:
            pr_url = push_and_create_pr(
                ascend_path=ctx.ascend_path,
                github_repo=self.config.github_repo,
                summary_path=WORKSPACE_DIR / "final_summary.md",
                target_commit=ctx.target_commit,
                work_branch=ctx.work_branch,
            )
            log.status(True, f"PR created: {pr_url}")
        except Exception as e:
            log.error(f"Failed to create PR: {e}")
