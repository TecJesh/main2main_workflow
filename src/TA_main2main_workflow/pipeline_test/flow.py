"""Pipeline test flow — orchestrator for the full-test-pipeline (ta-pipeline-test).

Independent of the main2main sync flow.  Order::

    validate → create test workspace → prepare context (patch touch info)
    → for each round:
        run script → collect logs → AI analyze → Excel reports
        all pass?  → done (PR only when fixes were committed)
        else       → fix failing cases one by one (category-guided)
                     → next round reruns the full script
    → rounds exhausted with failures → finalize (PR + Known Issues, exit 1)
"""

from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path

from TA_main2main_workflow.agent.opencode_adapter import run_opencode_adapter
from TA_main2main_workflow.pipeline.build import build_and_fix_loop
from TA_main2main_workflow.pipeline.ta_patch import (
    parse_patches,
    resolve_source_patches,
)
from TA_main2main_workflow.pipeline_test.analyze import (
    analysis_dir,
    generate_excel_reports,
    run_ai_analysis,
)
from TA_main2main_workflow.pipeline_test.fix_loop import fix_case_loop
from TA_main2main_workflow.pipeline_test.logs import (
    collect_repo_logs,
    failing_cases,
    fallback_analysis,
    has_failures,
)
from TA_main2main_workflow.pipeline_test.script_runner import run_script_streaming
from TA_main2main_workflow.utils import (
    BUILD_LOG_FILE,
    FINAL_SUMMARY_FILE,
    FIX_LOG_DIR,
    PIPELINE_TEST_FIX_SUMMARIES,
    PIPELINE_TEST_LOG_DIR_NAME,
    PIPELINE_TEST_RUN_LOG,
    PIPELINE_TEST_STEP_ID,
    STEPS_DIR,
    WORKSPACE_DIR,
)
from TA_main2main_workflow.utils.config import TAConfig
from TA_main2main_workflow.utils.context import WorkflowContext
from TA_main2main_workflow.utils.git import run_git
from TA_main2main_workflow.utils.logging import get_logger
from TA_main2main_workflow.utils.tracker import timed

log = get_logger(__name__)
_REF = str(Path(__file__).parent.parent / "reference")

_WORK_BRANCH_PREFIX = "fix/pipeline-test"


class PipelineTestFlow:
    """Orchestrates the full-test-pipeline run."""

    def __init__(self, config: TAConfig | None = None) -> None:
        self.config = config or TAConfig.from_env()

    def run(self) -> int:
        """Execute the pipeline test.  Returns the process exit code."""
        config = self.config
        ascend_path = self._validate_and_resolve()
        test_workspace = config.test_workspace_dir
        step_dir = WORKSPACE_DIR / STEPS_DIR / PIPELINE_TEST_STEP_ID

        # ── Reset flow-owned step artifacts ──────────────────────────────
        # The main flow wipes workspace/ in its initialize step; this flow
        # only cleans its own step dir + fix dirs so a previous run's
        # fix_summaries.md / analysis.json / commit whitelists can never
        # leak into the next run.
        if step_dir.exists():
            shutil.rmtree(step_dir, ignore_errors=True)
        step_dir.mkdir(parents=True, exist_ok=True)
        fixes_root = WORKSPACE_DIR / FIX_LOG_DIR
        if fixes_root.is_dir():
            for stale in fixes_root.glob("pipeline-test-*"):
                shutil.rmtree(stale, ignore_errors=True)

        # ── Branch bookkeeping (before any checkout) ────────────────────
        original_branch = run_git(ascend_path, "branch", "--show-current").strip()
        original_head = run_git(ascend_path, "rev-parse", "HEAD").strip()

        # ── Context + patch touch info (mirrors the main flow's step C) ──
        ctx = WorkflowContext(
            triton_ascend_path=str(ascend_path),
            steps=[{"id": PIPELINE_TEST_STEP_ID}],
            current_step=0,
            total_steps=1,
            target_commit=original_head,
            original_branch=original_branch,
        )
        patch_files = resolve_source_patches(ascend_path, config)
        if patch_files:
            touch = parse_patches(patch_files)
            ctx = ctx.copy_with(
                ta_patch_touched_files=touch.parent,
                ta_patch_submodule_files=touch.submodule,
            )
            log.key_value("patch touched (parent)", str(len(touch.parent)))
            log.key_value("patch touched (submodule)", str(len(touch.submodule)))

        # ── Pre-test build: compile + install triton-ascend ─────────────
        # The test script runs against the installed triton-ascend
        # package, so the build must succeed (with the main flow's
        # build-and-fix loop) BEFORE the first script run.  The work
        # branch is created first — build fixes are code changes and
        # must never land on the original branch.  SKIP_BUILD=true
        # skips this step (assumes the env is already built).
        work_branch = ""
        fixed_cases: list[str] = []
        remaining: list[dict] = []
        analysis: dict = {}
        build_fixed = False

        log.header("Build & Install Triton-Ascend (pre-test)")
        work_branch = self._create_work_branch(ascend_path)
        ctx = ctx.copy_with(work_branch=work_branch)
        with timed("pre-test-build"):
            ctx = build_and_fix_loop(ctx, config)
        if not ctx.build_passed:
            log.error(
                f"Pre-test build failed — see "
                f"{step_dir / BUILD_LOG_FILE}; cannot run pipeline tests"
            )
            self._restore_branch(ascend_path, original_branch, original_head)
            return 1
        build_fixed = ctx.build_fix_count > 0
        if build_fixed:
            log.status(
                True, f"Pre-test build fixed ({ctx.build_fix_count} AI fix round(s))"
            )
            with open(
                step_dir / PIPELINE_TEST_FIX_SUMMARIES, "a", encoding="utf-8"
            ) as fh:
                fh.write(
                    f"- [build] pre-test triton-ascend build fixed "
                    f"({ctx.build_fix_count} AI fix round(s))\n\n"
                )
        else:
            log.status(True, "Pre-test build passed")

        # ── Round loop ──────────────────────────────────────────────────
        # Bounded fix rounds + one final verification run: fixes are
        # applied after each round's analysis, and the loop only ends
        # once a full run passes (or the budget is exhausted with known
        # remaining failures).  The work branch already exists (created
        # before the pre-test build).
        for round_idx in range(1, config.pipeline_test_rounds + 1):
            log.header(f"Pipeline Test Round {round_idx}/{config.pipeline_test_rounds}")

            analysis = self._run_and_analyze(ctx, config, test_workspace, step_dir)
            if analysis is None:
                self._restore_branch(ascend_path, original_branch, original_head)
                return 1

            # ── All pass? ──────────────────────────────────────────────
            if not has_failures(analysis):
                log.status(True, "All pipeline tests passed")
                self._finalize(
                    ctx, config, test_workspace, step_dir, fixed_cases,
                    remaining, work_branch, ascend_path, analysis, build_fixed,
                )
                self._finish(
                    ascend_path, original_branch, original_head,
                    work_branch, fixed_cases, build_fixed,
                )
                log.table(ctx.summary_rows)
                return 0

            # ── Failures — fix round ───────────────────────────────────
            cases = failing_cases(analysis)
            if not cases:
                log.error(
                    "Failure counts reported but no failing case ids were "
                    "parsed — cannot fix per case; aborting"
                )
                self._restore_branch(ascend_path, original_branch, original_head)
                return 1

            remaining = []
            for repo, case in cases:
                fixed, ctx = fix_case_loop(
                    ctx, config, repo, case, test_workspace, step_dir
                )
                if fixed:
                    fixed_cases.append(f"[{repo}] {case.get('case', '')}")
                else:
                    remaining.append({"repo": repo, **case})

            if round_idx >= config.pipeline_test_rounds:
                if remaining:
                    log.error(
                        f"Rounds exhausted — {len(remaining)} case(s) still failing"
                    )
                    self._finalize(
                        ctx, config, test_workspace, step_dir, fixed_cases,
                        remaining, work_branch, ascend_path, analysis, build_fixed,
                    )
                    self._finish(
                        ascend_path, original_branch, original_head,
                        work_branch, fixed_cases, build_fixed,
                    )
                    log.table(ctx.summary_rows)
                    return 1
                log.info("All cases fixed in the final round — verifying")
                break

            log.info(
                f"Round {round_idx}: {len(fixed_cases)} fixed, "
                f"{len(remaining)} remaining — rerunning full tests"
            )

        # ── Final verification run (last round fixed everything) ──────────
        log.header("Final Verification Run")
        analysis = self._run_and_analyze(ctx, config, test_workspace, step_dir)
        if analysis is None:
            self._restore_branch(ascend_path, original_branch, original_head)
            return 1
        if not has_failures(analysis):
            log.status(True, "All pipeline tests passed after fixes")
            remaining = []
            self._finalize(
                ctx, config, test_workspace, step_dir, fixed_cases,
                remaining, work_branch, ascend_path, analysis, build_fixed,
            )
            self._finish(
                ascend_path, original_branch, original_head,
                work_branch, fixed_cases, build_fixed,
            )
            log.table(ctx.summary_rows)
            return 0

        remaining = [{"repo": repo, **case} for repo, case in failing_cases(analysis)]
        log.error(f"Verification run failed — {len(remaining)} case(s) failing")
        self._finalize(
            ctx, config, test_workspace, step_dir, fixed_cases,
            remaining, work_branch, ascend_path, analysis, build_fixed,
        )
        self._finish(
            ascend_path, original_branch, original_head,
            work_branch, fixed_cases, build_fixed,
        )
        log.table(ctx.summary_rows)
        return 1

    # ═══════════════════════════════════════════════════════════════════════
    # Internal
    # ═══════════════════════════════════════════════════════════════════════

    def _run_and_analyze(
        self,
        ctx: WorkflowContext,
        config: TAConfig,
        test_workspace: Path,
        step_dir: Path,
    ) -> dict | None:
        """Run the script, collect logs, analyze (AI + fallback), Excel.

        Returns the analysis dict, or None when the script produced no
        logs at all (hard abort).
        """
        self._reset_test_log(test_workspace)
        with timed("pipeline-test-script"):
            rc = run_script_streaming(
                config.pipeline_test_script,
                test_workspace,
                test_workspace / PIPELINE_TEST_RUN_LOG,
                timeout=config.pipeline_test_timeout,
            )
        log.key_value("script exit code", str(rc))

        repo_logs = collect_repo_logs(test_workspace)
        if not repo_logs:
            log.error(
                "The test script produced no logs under "
                f"{test_workspace / PIPELINE_TEST_LOG_DIR_NAME} — aborting"
            )
            return None

        deterministic = fallback_analysis(test_workspace)
        with timed("pipeline-test-analyze"):
            analysis = run_ai_analysis(
                ctx, config, test_workspace, step_dir, deterministic
            )

        # Excel reports (refreshed every round)
        generate_excel_reports(analysis, analysis_dir(test_workspace))
        return analysis

    def _validate_and_resolve(self) -> Path:
        """Validate config; return the triton-ascend repo path."""
        config = self.config

        if not config.triton_ascend_path:
            raise RuntimeError(
                "TRITON_ASCEND_PATH is required — the pipeline test fixes "
                "code in a real triton-ascend checkout"
            )
        ascend_path = Path(config.triton_ascend_path)
        if not ascend_path.exists():
            raise FileNotFoundError(
                f"triton-ascend path does not exist: {ascend_path}"
            )
        if not (ascend_path / ".git").exists():
            raise RuntimeError(f"Not a git checkout: {ascend_path}")

        if not config.pipeline_test_script:
            raise RuntimeError(
                "TA_PIPELINE_TEST_SCRIPT is required — point it at the "
                "executable script that clones test repos and runs pytest"
            )
        script_path = Path(os.path.expanduser(config.pipeline_test_script))
        if not script_path.is_file():
            raise FileNotFoundError(
                f"Test script not found: {script_path}"
            )

        # Build needs a valid LLVM prefix — fail fast instead of
        # discovering an empty prefix mid-fix.
        if not config.llvm_install_prefix and not config.llvm_install.exists():
            raise RuntimeError(
                "No LLVM install found — set LLVM_INSTALL_PREFIX or "
                "LLVM_INSTALL_PREFIX_SYNC (build_triton needs it)"
            )

        # Fresh volatile test workspace (like the main flow's workspace/).
        test_workspace = config.test_workspace_dir
        if test_workspace.exists():
            shutil.rmtree(test_workspace, ignore_errors=True)
        test_workspace.mkdir(parents=True, exist_ok=True)
        (test_workspace / PIPELINE_TEST_LOG_DIR_NAME).mkdir(parents=True, exist_ok=True)
        analysis_dir(test_workspace).mkdir(parents=True, exist_ok=True)

        return ascend_path

    def _reset_test_log(self, test_workspace: Path) -> None:
        """Wipe the log dir between rounds (repos stay cloned)."""
        log_dir = test_workspace / PIPELINE_TEST_LOG_DIR_NAME
        if log_dir.exists():
            shutil.rmtree(log_dir, ignore_errors=True)
        log_dir.mkdir(parents=True, exist_ok=True)

    def _create_work_branch(self, ascend_path: Path) -> str:
        """Create the fix work branch from the configured base ref.

        Reuses the main flow's branch semantics exactly: the base ref is
        ``{TA_WORK_BRANCH_BASE}/{TA_BASE_BRANCH}`` (fetched fresh, with
        an ``origin/{TA_BASE_BRANCH}`` fallback), only the branch name
        namespace differs (``fix/pipeline-test``).
        """
        from TA_main2main_workflow.pipeline.merge import _create_work_branch

        branch = _create_work_branch(ascend_path, self.config, _WORK_BRANCH_PREFIX)
        log.status(True, f"Work branch: {branch}")
        return branch

    def _finalize(
        self,
        ctx: WorkflowContext,
        config: TAConfig,
        test_workspace: Path,
        step_dir: Path,
        fixed_cases: list[str],
        remaining: list[dict],
        work_branch: str,
        ascend_path: Path,
        analysis: dict,
        build_fixed: bool = False,
    ) -> None:
        """PR description + (optional) PR creation.  No-op without fixes."""
        fixes_total = len(fixed_cases) + (1 if build_fixed else 0)
        ctx.summary_rows.append(
            (
                "Pipeline Test",
                "PASS" if not remaining else "FAIL",
                f"{fixes_total} fixed, {len(remaining)} remaining",
            )
        )
        summary_path = WORKSPACE_DIR / FINAL_SUMMARY_FILE
        self._generate_pr_description(
            ctx, config, test_workspace, step_dir, fixed_cases, remaining,
            analysis, summary_path,
        )

        if not fixed_cases and not build_fixed:
            if remaining:
                log.info(
                    "No fixes committed — skipping PR "
                    f"({len(remaining)} case(s) still failing)"
                )
            else:
                log.info("No fixes committed — skipping PR (tests passed as-is)")
            return
        if not config.push_to_github:
            log.warning(
                f"{fixes_total} fix(es) committed on branch "
                f"'{work_branch}' — set PUSH_TO_GITHUB=true to open a PR"
            )
            return

        from TA_main2main_workflow.pipeline.push_pr import push_and_create_pr

        # Pipeline-test PRs carry their own identity (NOT the main
        # flow's PR_AUTHOR "Sync"); TA_PIPELINE_TEST_PR_TITLE overrides
        # the whole title when set.
        if fixed_cases:
            subject = f"Fix {len(fixed_cases)} failing test case(s)"
        else:
            subject = "Fix triton-ascend build"
        title = os.getenv("TA_PIPELINE_TEST_PR_TITLE", "").strip() or (
            f"[PipelineTest](fix) {subject} "
            f"({time.strftime('%Y%m%d-%H%M%S')})"
        )
        try:
            pr_url = push_and_create_pr(
                ascend_path=ascend_path,
                github_repo=config.github_repo,
                summary_path=summary_path,
                target_commit=ctx.target_commit,
                work_branch=work_branch,
                title=title,
            )
            log.status(True, f"PR created: {pr_url}")
        except Exception as exc:
            log.error(f"Failed to create PR: {exc}")

    def _generate_pr_description(
        self,
        ctx: WorkflowContext,
        config: TAConfig,
        test_workspace: Path,
        step_dir: Path,
        fixed_cases: list[str],
        remaining: list[dict],
        analysis: dict,
        summary_path: Path,
    ) -> None:
        """AI (pipeline_test_report mode) writes the PR body; else fallback."""
        overview = [
            {
                "repo": r["repo"],
                "failed": r.get("failed", 0),
                "errors": r.get("errors", 0),
            }
            for r in analysis.get("repos", [])
        ]
        fix_summaries = ""
        fix_file = step_dir / PIPELINE_TEST_FIX_SUMMARIES
        if fix_file.exists():
            fix_summaries = fix_file.read_text(encoding="utf-8")

        context = {
            "fix_summaries": fix_summaries,
            "remaining_failures": [
                {
                    "repo": r.get("repo", ""),
                    "case": r.get("case", ""),
                    "error": r.get("error", ""),
                    "category": r.get("category", ""),
                }
                for r in remaining
            ],
            "overview": overview,
        }

        if not config.skip_ai_analysis:
            try:
                run_opencode_adapter(
                    {
                        "step_id": "pipeline-test-report",
                        "previous_step_id": "",
                        "previous_step_summary_path": "",
                        "is_last_step": "true",
                        "step_dir": str(step_dir),
                        "fix_dir": str(step_dir),
                        "conflict_dir": "",
                        "ascend_path": ctx.triton_ascend_path,
                        "triton_path": ctx.triton_ascend_path,
                        "reference_dir": _REF,
                        "mode": "pipeline_test_report",
                        "error_logs": json.dumps(context, ensure_ascii=False),
                        "target_commit": ctx.target_commit,
                        "step_index": "1/1",
                        "test_workspace": str(test_workspace),
                        "ascend_npu_ir_fix": "false",
                        "ascend_npu_ir_compat_ref": "",
                    }
                )
                ai_summary = step_dir / "step_summary.md"
                if ai_summary.exists():
                    summary_path.write_text(
                        ai_summary.read_text(encoding="utf-8"), encoding="utf-8"
                    )
                    log.status(True, f"AI PR description: {summary_path}")
                    return
            except Exception as exc:
                log.warning(f"AI PR description failed: {exc} — using fallback")

        _write_summary_fallback(summary_path, fixed_cases, remaining, test_workspace)

    def _restore_branch(
        self, ascend_path: Path, original_branch: str, original_head: str
    ) -> None:
        """Check the original branch (or detached HEAD sha) back out."""
        try:
            if original_branch:
                run_git(ascend_path, "checkout", original_branch)
            else:
                run_git(ascend_path, "checkout", original_head)
            log.info(
                f"Restored original branch/HEAD: "
                f"{original_branch or original_head[:12]}"
            )
        except Exception as exc:
            log.warning(f"Could not restore original branch: {exc}")

    def _finish(
        self,
        ascend_path: Path,
        original_branch: str,
        original_head: str,
        work_branch: str,
        fixed_cases: list[str],
        build_fixed: bool = False,
    ) -> None:
        """Restore the original branch and drop an empty work branch."""
        self._restore_branch(ascend_path, original_branch, original_head)
        if work_branch and not fixed_cases and not build_fixed:
            # The work branch has no commits (no fixes) — drop the litter.
            try:
                run_git(ascend_path, "branch", "-D", work_branch)
                log.info(f"Deleted empty work branch: {work_branch}")
            except Exception as exc:
                log.warning(f"Could not delete empty work branch: {exc}")


def _write_summary_fallback(
    summary_path: Path,
    fixed_cases: list[str],
    remaining: list[dict],
    test_workspace: Path,
) -> None:
    """Deterministic PR description when the AI is unavailable."""
    parts = [
        "## Summary",
        f"- Fixed cases: {len(fixed_cases)}",
        f"- Still failing: {len(remaining)}",
        f"- Date: {time.strftime('%Y-%m-%d %H:%M:%S')}",
    ]
    if fixed_cases:
        parts.append("\n## Fixed Cases\n")
        parts.extend(f"- {c}" for c in fixed_cases)
    if remaining:
        parts.append("\n## Known Issues\n")
        for r in remaining:
            parts.append(
                f"- [{r.get('repo', '')}] `{r.get('case', '')}` "
                f"({r.get('category', '')}): {r.get('error', '')[:200]}"
            )
    parts.append(
        "\n## Test Reports\n"
        f"- Excel reports: {test_workspace}/analysis/"
    )
    summary_path.write_text("\n".join(parts) + "\n", encoding="utf-8")
    log.info(f"PR description (fallback): {summary_path}")
