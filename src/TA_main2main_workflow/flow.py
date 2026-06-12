"""CrewAI Flow — Triton-Ascend main2main upstream sync (merge-based).

Node order:
  initialize → detect_commits → execute_sync → push_to_github / handle_failure

The flow uses a single orchestration node (execute_sync) that internally
runs merge → AI resolve conflicts → build → test → AI fix in a loop.
This avoids relying on CrewAI @listen → @listen signal chaining which
fails to propagate return values in some CrewAI versions.

ALL progress is printed to the local console — no CrewAI web UI needed.
AI (opencode or claude) is invoked via subprocess for conflict resolution
and test fixing.
"""

import json
import os
import shutil
import time
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

from crewai.flow import Flow, listen, start, router

from TA_main2main_workflow.agent.opencode_adapter import AIResult, run_opencode_adapter
from TA_main2main_workflow.scripts.build_test import build_triton_ascend, run_tests
from TA_main2main_workflow.scripts.detect_commits import detect
from TA_main2main_workflow.scripts.merge_upstream import run_merge, run_merge_incremental
from TA_main2main_workflow.scripts.plan_steps import run_plan
from TA_main2main_workflow.scripts.pre_ci_check import run_pre_ci_check, cleanup_temp_files
from TA_main2main_workflow.scripts.push_to_github import (
    push_and_create_pr,
)

from TA_main2main_workflow.utils import (
    BUILD_LOG_FILE, BUILD_RESULT_FILE, CONFLICT_LOG_DIR,
    EACH_STEP_SUMMARY_FILE, EACH_STEP_TARGET_PATCH_FILE,
    FINAL_SUMMARY_FILE, FINAL_TARGET_PATCH_FILE, FIX_LOG_DIR,
    HasNewCommits, HasNoNewCommits,
    STEPS_DIR, STEPS_FILE, LINE_BUDGET, commit_count_budget,
    TEST_RESULT_FILE, UpgradeCompleted, UpgradeFailed,
    WORKSPACE_DIR, has_merge_conflicts, run_git, get_conflict_files,
    print_header, print_section, print_step, print_status, print_info,
    print_warn, print_error, print_key_value,
    print_flow_progress, print_conflict_list, print_summary_table,
    print_ai_call_info, print_ai_result, print_elapsed_total,
    start_timer, stop_timer,
)

_REFERENCE_DIR = str(Path(__file__).parent / "reference")


class TA_Main2MainState(BaseModel):
    triton_ascend_path: str = ""
    triton_path: str = ""
    target_commit: str = ""
    test_log_dir: str = ""

    merge_base: str = ""
    ascend_head: str = ""
    work_branch: str = ""
    original_branch: str = ""

    upstream_commits_count: int = 0
    merge_has_conflicts: bool = False
    conflict_files: list = []

    build_passed: bool = False
    test_passed: bool = False

    retry_count: int = 0
    max_retries: int = 3
    fix_errors: list = []

    final_status: str = ""
    pr_url: str = ""

    llvm_prefix: str = ""
    conda_env: str = ""
    test_dir: str = "third_party/ascend/unittest/pytest_ut"
    num_procs: int = 16

    # ── Progressive step-by-step merge ──
    steps: list = []
    total_steps: int = 0
    current_step: int = 0
    step_start_ascend_head: str = ""  # ascend HEAD before current step
    progressive_merge: bool = True
    step_pr_descriptions: list = []  # accumulated step descriptions for PR body

    summary_rows: list = []


class TA_Main2MainFlow(Flow[TA_Main2MainState]):

    def __init__(self, **kwargs):
        super().__init__(**kwargs)

    # ═══════════════════════════════════════════════════════════════════════════
    # Phase 0: Initialize
    # ═══════════════════════════════════════════════════════════════════════════

    @start()
    def initialize(self):
        start_timer("flow-total")

        print_header("Triton-Ascend Upstream Sync — Main2Main Flow")
        print(f"  Started: {time.strftime('%Y-%m-%d %H:%M:%S')}", flush=True)
        print(f"  AI Backend: {os.getenv('AI_BACKEND', 'auto-detect')}", flush=True)
        print(f"  Max Retries: {self.state.max_retries}", flush=True)

        if WORKSPACE_DIR.exists():
            shutil.rmtree(WORKSPACE_DIR)
        WORKSPACE_DIR.mkdir(parents=True)

        raw_ascend = (
            self.state.triton_ascend_path
            or os.getenv("TRITON_ASCEND_PATH")
            or str(Path.cwd())
        )
        raw_triton = (
            self.state.triton_path
            or os.getenv("TRITON_PATH")
            or str(Path.cwd())
        )

        self.state.triton_ascend_path = raw_ascend
        self.state.triton_path = raw_triton
        self.state.target_commit = (
            self.state.target_commit or os.getenv("TRITON_TARGET_COMMIT", "")
        )
        self.state.llvm_prefix = os.getenv("LLVM_INSTALL_PREFIX", "")
        self.state.conda_env = os.getenv("CONDA_ENV", "ta-upgrade")
        self.state.num_procs = int(os.getenv("NUM_PROCS", "16"))

        if not self.state.test_log_dir:
            self.state.test_log_dir = str(WORKSPACE_DIR / "test-logs")

        ascend_path = Path(self.state.triton_ascend_path)

        # ── safety: abort any stale merge ──
        merge_head = ascend_path / ".git" / "MERGE_HEAD"
        if merge_head.exists():
            print_warn("Found stale MERGE_HEAD from previous run, aborting it")
            try:
                run_git(ascend_path, "merge", "--abort")
                print_info("Stale merge aborted successfully")
            except Exception:
                print_warn("merge --abort failed, trying reset --hard")
                try:
                    run_git(ascend_path, "reset", "--hard", "HEAD")
                except Exception:
                    pass
            for stale in [".git/MERGE_MODE", ".git/MERGE_MSG", ".git/CHERRY_PICK_HEAD"]:
                p = ascend_path / stale
                if p.exists():
                    p.unlink()

        ascend_branch = run_git(ascend_path, "branch", "--show-current").strip()
        self.state.original_branch = ascend_branch or run_git(
            ascend_path, "rev-parse", "HEAD"
        ).strip()
        self.state.ascend_head = run_git(ascend_path, "rev-parse", "HEAD").strip()

        print_section("Repository Configuration")
        print_key_value("triton-ascend", self.state.triton_ascend_path)
        print_key_value("upstream triton", self.state.triton_path)
        print_key_value("target commit", self.state.target_commit or "<upstream HEAD>")
        print_key_value("original branch", self.state.original_branch)
        print_key_value("ascend HEAD", self.state.ascend_head[:12])

        self.state.summary_rows = []

    # ═══════════════════════════════════════════════════════════════════════════
    # Phase 1: Detect upstream commits
    # ═══════════════════════════════════════════════════════════════════════════

    @router(initialize)
    def detect_commits(self) -> Literal["HasNewCommits", "HasNoNewCommits"]:
        start_timer("detect")
        print_header("Phase 1: Detect Upstream Commits & Plan Steps")

        ascend_path = Path(self.state.triton_ascend_path)
        triton_path = Path(self.state.triton_path)

        result, has_new = detect(
            ascend_path,
            triton_path,
            self.state.target_commit or None,
        )

        self.state.merge_base = result["merge_base"]
        self.state.target_commit = result["target_commit"]
        self.state.upstream_commits_count = result["upstream_commits_count"]

        print_key_value("merge_base", self.state.merge_base[:12])
        print_key_value("target", self.state.target_commit[:12])
        print_key_value("upstream commits", str(self.state.upstream_commits_count))
        print_key_value("changed files", str(result["changed_files_count"]))
        print_key_value("changed lines", str(result["changed_lines"]["total"]))

        commits = result.get("upstream_commits", [])
        if commits:
            print_info(f"Commits to merge ({len(commits)}):")
            for c in commits[:20]:
                print(f"    {c['sha'][:8]} {c['subject'][:80]}")
            if len(commits) > 20:
                print(f"    ... and {len(commits) - 20} more")

        if not has_new:
            print_status(True, "Already up to date — nothing to merge")
            self.state.summary_rows.append(("Detect commits", "PASS", "No new commits"))
            stop_timer("detect")
            return HasNoNewCommits

        # ── Check if progressive merge is enabled ──
        progressive_env = os.getenv("TA_PROGRESSIVE_MERGE", "true").lower()
        self.state.progressive_merge = progressive_env != "false"

        # ── Plan steps: split commits into chunks based on line/commit budget ──
        if self.state.progressive_merge and self.state.upstream_commits_count > 1:
            print_section("Step Planning")
            line_budget = int(os.getenv("TA_LINE_BUDGET", str(LINE_BUDGET)))
            ccb = commit_count_budget(line_budget)
            print_key_value("line budget", str(line_budget))
            print_key_value("commit-count budget", str(ccb))

            plan = run_plan(
                triton_path,
                self.state.merge_base,
                self.state.target_commit,
                line_budget=line_budget,
            )
            self.state.steps = plan["steps"]
            self.state.total_steps = len(plan["steps"])

            # ── Guard: if planner produced 0 steps (e.g., all commits filtered
            # out), fall back to single-step mode so something still gets merged ──
            if self.state.total_steps == 0:
                print_warn("Plan returned 0 steps — falling back to single-step merge")
                self.state.total_steps = 1
                self.state.steps = [{
                    "index": 1,
                    "id": "step-1",
                    "commit_count": self.state.upstream_commits_count,
                    "start_commit": self.state.merge_base,
                    "end_commit": self.state.target_commit,
                    "source_changed_lines": result["changed_lines"]["total"],
                }]

            print_status(True, f"Planned {self.state.total_steps} step(s) "
                         f"from {plan['total_source_commits']} source-touching commits "
                         f"({plan['total_commits']} total upstream commits)")
        else:
            # Single-step mode: treat everything as one step
            self.state.total_steps = 1
            self.state.steps = [{
                "index": 1,
                "id": "step-1",
                "commit_count": self.state.upstream_commits_count,
                "start_commit": self.state.merge_base,
                "end_commit": self.state.target_commit,
                "source_changed_lines": result["changed_lines"]["total"],
            }]
            if not self.state.progressive_merge:
                print_info("TA_PROGRESSIVE_MERGE=false — using single-step mode")
            else:
                print_info("Only 1 upstream commit — using single-step mode")

        stop_timer("detect")
        print_status(True, f"Found {self.state.upstream_commits_count} upstream commits to merge "
                     f"across {self.state.total_steps} step(s)")
        self.state.summary_rows.append(
            ("Detect commits", "PASS",
             f"{self.state.upstream_commits_count} commits, {self.state.total_steps} step(s)")
        )
        return HasNewCommits

    @listen(HasNoNewCommits)
    def has_no_commits(self):
        print_header("Sync Complete — Already Up To Date")
        print_elapsed_total()
        print_summary_table(self.state.summary_rows)

    # ═══════════════════════════════════════════════════════════════════════════
    # Phase 2: Execute Sync (orchestrates merge → resolve → build → test → fix)
    # ═══════════════════════════════════════════════════════════════════════════
    #
    # This is the core loop. It runs as a SINGLE @router node to avoid
    # CrewAI @listen → @listen signal chaining issues. All sub-steps are
    # internal method calls, not CrewAI routing targets.

    @router(detect_commits)
    def execute_sync(self) -> Literal["UpgradeCompleted", "UpgradeFailed"]:
        """Orchestrate the full sync pipeline — progressively or single-step.

        When progressive_merge is True (default), each planned step is merged
        and validated independently before moving to the next. This keeps
        AI conflict resolution and fix scopes small and manageable.

        The internal per-step call chain is:
          _do_step_merge → _do_resolve_conflicts → _do_build_and_fix_loop → _do_commit_step → _push_step_progress
        """
        # ── Iterate over each planned step ──
        while self.state.current_step < self.state.total_steps:
            step = self.state.steps[self.state.current_step]
            step_id = step["id"]
            self.state.retry_count = 0

            print_header(f"Step {self.state.current_step + 1}/{self.state.total_steps}: {step_id}")
            print_key_value("commits in step", str(step["commit_count"]))
            print_key_value("end commit", step["end_commit"][:12])
            if "source_changed_lines" in step:
                print_key_value("source lines", str(step["source_changed_lines"]))

            ascend_path = Path(self.state.triton_ascend_path)

            # ── Work-branch guard: verify we're on the right branch ──
            if self.state.current_step > 0 and self.state.work_branch:
                current_branch = run_git(ascend_path, "branch", "--show-current").strip()
                if current_branch != self.state.work_branch:
                    print_warn(f"Expected work branch '{self.state.work_branch}' "
                               f"but currently on '{current_branch}' — switching back")
                    run_git(ascend_path, "checkout", self.state.work_branch)
                print_info(f"Same work branch: '{self.state.work_branch}' "
                           f"(step {self.state.current_step + 1}/{self.state.total_steps})")

            # Record ascend HEAD before this step (for per-step patch generation)
            self.state.step_start_ascend_head = run_git(
                ascend_path, "rev-parse", "HEAD"
            ).strip()

            # ── Step A: git merge this step's end commit ──
            result = self._do_step_merge(step)
            if result == UpgradeFailed:
                self.state.final_status = UpgradeFailed
                return UpgradeFailed

            # ── Step B: AI resolve conflict (if merge had conflicts) ──
            if self.state.merge_has_conflicts:
                if not self._do_resolve_conflicts():
                    self.state.final_status = UpgradeFailed
                    return UpgradeFailed

            # ── Step C: build → test → AI fix bug loop ──
            try:
                build_ok = self._do_build_and_fix_loop()
            except Exception as exc:
                print_error(f"_do_build_and_fix_loop crashed: {exc}")
                import traceback
                traceback.print_exc()
                self.state.final_status = UpgradeFailed
                return UpgradeFailed

            if not build_ok:
                self.state.final_status = UpgradeFailed
                return UpgradeFailed

            # ── Step D: commit step progress ──
            self._do_commit_step(step)

            # ── Record step description for final PR body ──
            desc = (
                f"✅ **{step_id}**: {step['commit_count']} commits, "
                f"end_commit=`{step['end_commit'][:12]}`, "
                f"source lines={step.get('source_changed_lines', '?')}"
            )
            self.state.step_pr_descriptions.append(desc)

            # Move to next step
            self.state.current_step += 1
            print_status(True, f"Step {step_id} completed successfully "
                         f"({self.state.current_step}/{self.state.total_steps})")

        # ── Finalize: generate cumulative patch & summary ──
        self._do_finalize()
        self.state.final_status = UpgradeCompleted
        return UpgradeCompleted

    # ═══════════════════════════════════════════════════════════════════════════
    # Internal step implementations
    # ═══════════════════════════════════════════════════════════════════════════

    def _do_step_merge(self, step: dict) -> Literal["HasNewCommits"] | Literal["UpgradeFailed"]:
        """Merge this step's end_commit into triton-ascend.

        The first step creates a fresh work branch from upstream-ascend/main
        and merges its end_commit. Subsequent steps merge their end_commit
        on top of the SAME work branch — git handles the incremental merge
        automatically by computing the diff between the previous end_commit
        and the new one.

        ALL steps share ONE work branch. This is critical: we accumulate
        changes on a single branch so the final PR contains the full history.
        """
        start_timer("merge")
        step_id = step["id"]
        is_first_step = self.state.current_step == 0

        ascend_path = Path(self.state.triton_ascend_path)
        triton_path = Path(self.state.triton_path)

        # ── Verify / log work branch consistency ──
        if is_first_step:
            print_info(f"No work branch yet — will create one for step {step_id}")
        else:
            current_branch = run_git(ascend_path, "branch", "--show-current").strip()
            if current_branch != self.state.work_branch:
                print_warn(f"Expected work branch '{self.state.work_branch}' "
                           f"but currently on '{current_branch}' — switching back")
                run_git(ascend_path, "checkout", self.state.work_branch)
            print_info(f"Continuing on work branch: '{self.state.work_branch}' "
                       f"(verified same branch as step 1)")

        print_flow_progress("merge", f"[{step_id}] merging {step['end_commit'][:12]}")

        try:
            if is_first_step:
                # First step: create work branch and do full merge
                merge_result = run_merge(
                    ascend_path,
                    triton_path,
                    step["end_commit"],
                )
                self.state.work_branch = merge_result["work_branch"]
                print_info(f"Created work branch: '{self.state.work_branch}' "
                           f"(all {self.state.total_steps} step(s) will use this branch)")
            else:
                # Subsequent step: merge on top of existing work branch
                # fetch the new target if it's not already present
                try:
                    run_git(ascend_path, "fetch", "upstream-triton", "--prune")
                except Exception:
                    print_info("Could not fetch upstream-triton, assuming target is reachable")

                merge_result = run_merge_incremental(
                    ascend_path,
                    triton_path,
                    step["end_commit"],
                    self.state.work_branch,
                )
        except Exception as exc:
            print_error(f"Merge failed with exception: {exc}")
            stop_timer("merge")
            self.state.summary_rows.append(
                (f"Merge step {step_id}", "FAIL", str(exc)[:60])
            )
            return UpgradeFailed

        self.state.merge_has_conflicts = merge_result["has_conflicts"]
        self.state.conflict_files = merge_result.get("conflict_files", [])

        print_key_value("work branch", self.state.work_branch)
        print_key_value("has conflicts", str(self.state.merge_has_conflicts))
        print_key_value("exit code", str(merge_result["merge_exit_code"]))
        print_key_value("step", f"{self.state.current_step + 1}/{self.state.total_steps}")

        # If merge had non-zero exit but no conflict markers, that's a hard failure
        if merge_result["merge_exit_code"] != 0 and not self.state.merge_has_conflicts:
            print_error(f"Merge exited with code {merge_result['merge_exit_code']} "
                        f"but no conflict markers found — this is an unexpected failure")
            stop_timer("merge")
            self.state.summary_rows.append(
                (f"Merge step {step_id}", "FAIL",
                 f"exit code {merge_result['merge_exit_code']}")
            )
            return UpgradeFailed

        if self.state.merge_has_conflicts:
            print_conflict_list(self.state.conflict_files)
            stop_timer("merge")
            self.state.summary_rows.append(
                (f"Merge step {step_id}", "WARN", f"{len(self.state.conflict_files)} conflicts")
            )
        else:
            stop_timer("merge")
            print_status(True, f"Step {step_id} merge succeeded with no conflicts")
            self.state.summary_rows.append(
                (f"Merge step {step_id}", "PASS", f"{step['commit_count']} commits")
            )

        return HasNewCommits

    def _do_resolve_conflicts(self) -> bool:
        """AI-driven merge conflict resolution with retry loop.

        For each attempt (up to max_retries):
          1. Refresh the conflict file list from git
          2. Call opencode/claude with the conflict snapshots
          3. Check if all conflicts are resolved
          4. If not, retry with refreshed conflict list

        AI context includes: step index (N/total), is_last_step flag,
        previous_step_id and previous_step_summary_path for continuity
        (matching vllm-ascend's main2main_flow pattern).

        After all conflicts are resolved:
          - git commit the resolution
          - Run pre-CI checks (conflict markers, temp files, syntax)
          - Write step summary and cumulative patch

        Returns True if all conflicts resolved, False otherwise.
        """
        start_timer("resolve")
        print_header("Phase 3: AI Conflict Resolution")

        ascend_path = Path(self.state.triton_ascend_path)

        step = self.state.steps[self.state.current_step] if self.state.steps else None
        current_step_id = step["id"] if step else "step-0"
        is_last_step = self.state.current_step == self.state.total_steps - 1

        # Use step-specific directory in progressive mode, fall back to step-0
        if self.state.total_steps > 1 and self.state.steps:
            step_dir = WORKSPACE_DIR / STEPS_DIR / current_step_id
        else:
            step_dir = WORKSPACE_DIR / "step-0"
        step_dir.mkdir(parents=True, exist_ok=True)

        # ── Previous step context (matching vllm-ascend pattern) ──
        previous_step = (
            self.state.steps[self.state.current_step - 1]
            if self.state.current_step > 0 and self.state.steps else None
        )
        previous_step_id = previous_step["id"] if previous_step else ""
        previous_step_summary_path = (
            str(WORKSPACE_DIR / STEPS_DIR / previous_step_id / EACH_STEP_SUMMARY_FILE)
            if previous_step_id else ""
        )

        conflict_dir = WORKSPACE_DIR / CONFLICT_LOG_DIR

        # AI resolve conflict: check if AI is disabled
        if os.getenv("SKIP_AI_ANALYSIS", "false").lower() == "true":
            print_warn("SKIP_AI_ANALYSIS=true — skipping AI conflict resolution!")
            print_warn("Conflicts will NOT be resolved automatically.")
            print_conflict_list(self.state.conflict_files)
            print_info("To resolve: manually edit conflicted files, then run:")
            print_info(f"  cd {ascend_path} && git add -u && git commit --no-edit")
            self.state.summary_rows.append(("AI resolve conflicts", "SKIP", "SKIP_AI_ANALYSIS set"))
            return False

        # AI resolve conflict: detect backend (opencode / claude)
        try:
            from TA_main2main_workflow.agent.opencode_adapter import _detect_backend
            backend = _detect_backend()
            print_info(f"AI backend detected: {backend}")
        except RuntimeError as e:
            print_error(f"AI backend not available: {e}")
            print_info("Install 'opencode' or 'claude' CLI, or set AI_BACKEND env var.")
            self.state.summary_rows.append(("AI resolve conflicts", "FAIL", str(e)[:50]))
            return False

        resolved_all = False
        ai_result: AIResult | None = None
        conflict_files = list(self.state.conflict_files)

        # AI resolve conflict: retry loop (up to max_retries)
        for attempt in range(1, self.state.max_retries + 1):
            print_step(attempt, self.state.max_retries, "AI conflict resolution")

            conflict_files = get_conflict_files(ascend_path)
            if not conflict_files:
                print_status(True, "No conflicts detected — already resolved!")
                resolved_all = True
                break

            print_info(f"Files with conflicts: {len(conflict_files)}")
            for f in conflict_files:
                print(f"      • {f}")

            print_ai_call_info(
                backend=backend,
                mode="conflict",
                attempt=attempt,
                max_attempts=self.state.max_retries,
            )

            # AI resolve conflict: invoke opencode/claude
            # Context matches vllm-ascend pattern: is_last_step,
            # previous_step_id, previous_step_summary_path, step index
            try:
                ai_result = run_opencode_adapter({
                    "step_id": f"{current_step_id}-conflict-{attempt}",
                    "previous_step_id": previous_step_id,
                    "previous_step_summary_path": previous_step_summary_path,
                    "is_last_step": str(is_last_step).lower(),
                    "step_index": f"{self.state.current_step + 1}/{self.state.total_steps}",
                    "step_dir": str(step_dir),
                    "conflict_dir": str(conflict_dir),
                    "ascend_path": str(ascend_path),
                    "triton_path": self.state.triton_path,
                    "reference_dir": _REFERENCE_DIR,
                    "mode": "conflict",
                    "error_logs": json.dumps(conflict_files, ensure_ascii=False),
                    "target_commit": self.state.target_commit,
                })
            except Exception as e:
                print_error(f"AI call failed: {e}")
                if attempt < self.state.max_retries:
                    print_info(f"Retrying... ({attempt}/{self.state.max_retries})")
                    continue
                break

            if not has_merge_conflicts(ascend_path):
                print_status(True, f"All conflicts resolved! (attempt {attempt})")
                resolved_all = True
                break
            else:
                still_conflicted = len(get_conflict_files(ascend_path))
                print_status(False, f"{still_conflicted} conflict(s) remain after attempt {attempt}")
                conflict_files = get_conflict_files(ascend_path)

        if not resolved_all:
            remaining = get_conflict_files(ascend_path)
            print_error(f"Failed to resolve all conflicts after {self.state.max_retries} attempts")
            print_conflict_list(remaining)
            stop_timer("resolve")
            self.state.summary_rows.append(("AI resolve conflicts", "FAIL", "Conflicts remain"))
            return False

        # AI resolve conflict: git commit the resolution
        # Use "git add -u" (tracked-only) to avoid staging test artifacts,
        # cache files, or other transient files created during the flow.
        try:
            run_git(ascend_path, "add", "-u")
            run_git(ascend_path, "commit", "--no-edit", "-s")
            print_status(True, "Committed conflict resolution")
        except Exception:
            print_info("Note: commit may have already been applied (nothing to commit)")

        # pre-CI check: scan for leftover conflict markers, temp files, syntax errors
        print_info("Running pre-CI check after conflict resolution...")
        pre_ci_result = run_pre_ci_check(ascend_path, step_id="conflict-resolution")
        if not pre_ci_result["all_passed"]:
            print_warn("Pre-CI check found issues — review before proceeding")
        self.state.summary_rows.append(
            ("Pre-CI check", "PASS" if pre_ci_result["all_passed"] else "WARN",
             f"{pre_ci_result.get('modified_files_count', 0)} files checked")
        )

        # ── Write step summary ──
        summary_path = step_dir / EACH_STEP_SUMMARY_FILE
        if ai_result and ai_result.step_summary and not summary_path.exists():
            summary_path.write_text(ai_result.step_summary, encoding="utf-8")

        # ── Generate step patch ──
        try:
            patch = run_git(ascend_path, "diff", self.state.ascend_head, "HEAD")
            (step_dir / EACH_STEP_TARGET_PATCH_FILE).write_text(patch, encoding="utf-8")
        except Exception:
            pass

        stop_timer("resolve")
        elapsed = ai_result.elapsed_seconds if ai_result else 0
        print_status(True, f"Conflict resolution complete ({elapsed:.0f}s AI time)")
        self.state.summary_rows.append(
            ("AI resolve conflicts", "PASS", f"{elapsed:.0f}s" if elapsed else "done")
        )
        self.state.merge_has_conflicts = False
        return True

    def _do_build_and_fix_loop(self) -> bool:
        """build → test → AI fix bug loop (up to max_retries rounds).

        Step-aware: uses step-specific directory and includes step context
        in fix attempts (matching vllm-ascend's per-step AI context pattern).
        """
        ascend_path = Path(self.state.triton_ascend_path)
        step = self.state.steps[self.state.current_step] if self.state.steps else None
        current_step_id = step["id"] if step else "step-0"

        # Use step-specific directory in progressive mode, fall back to step-0
        if self.state.total_steps > 1 and self.state.steps:
            step_dir = WORKSPACE_DIR / STEPS_DIR / current_step_id
        else:
            step_dir = WORKSPACE_DIR / "step-0"
        step_dir.mkdir(parents=True, exist_ok=True)

        test_passed = False

        for attempt in range(self.state.max_retries + 1):
            is_fix_attempt = attempt > 0
            self.state.retry_count = attempt

            # AI fix bug (skip on first round — build & test first)
            if is_fix_attempt:
                print_header(f"Fix Attempt {attempt}/{self.state.max_retries}")
                if not self._do_ai_fix(ascend_path, step_dir, attempt):
                    pass

            # build triton-ascend
            if not self._do_build(ascend_path, clean=(attempt == 0)):
                if os.getenv("SKIP_AI_ANALYSIS", "false").lower() == "true":
                    return False
                self.state.fix_errors = [str(WORKSPACE_DIR / BUILD_RESULT_FILE)]
                print_warn(f"Build failed (attempt {attempt + 1}/{self.state.max_retries + 1}) — "
                           f"skipping tests, will retry after AI fix")
                print_info(f"Build log: {WORKSPACE_DIR / BUILD_LOG_FILE}")
                continue

            # run pytest
            test_result = self._do_test(ascend_path)
            if test_result is None:
                test_passed = True
                break
            elif test_result:
                test_passed = True
                break
            else:
                if os.getenv("SKIP_AI_ANALYSIS", "false").lower() == "true":
                    return False
                self.state.fix_errors = [str(WORKSPACE_DIR / TEST_RESULT_FILE)]
                continue

        if not test_passed:
            print_error(f"All {self.state.max_retries} fix attempts exhausted")
            self.state.summary_rows.append(
                ("AI fix", "FAIL", f"Failed after {self.state.max_retries} attempts")
            )
            return False

        # Commit bug fixes after all tests pass
        self._commit_fixes(ascend_path, step_dir)

        return True

    def _commit_fixes(self, ascend_path: Path, step_dir: Path) -> None:
        """Commit AI bug fixes with a meaningful message.

        Only commits if there are uncommitted changes (i.e., the AI actually
        modified files to fix build/test failures). Uses "git add -u" to
        avoid staging test artifacts or transient files.
        """
        status = run_git(ascend_path, "status", "--porcelain").strip()
        if not status:
            print_info("No uncommitted fix changes — nothing to commit")
            return

        print_section("Commit Bug Fixes")

        # Build commit message from AI summary if available
        summary_path = step_dir / EACH_STEP_SUMMARY_FILE
        if summary_path.exists():
            summary_text = summary_path.read_text(encoding="utf-8").strip()
            # Use first heading or first line as short description
            commit_summary = summary_text.split("\n")[0].lstrip("#").strip()[:72]
        else:
            commit_summary = f"fix: resolve build/test failures for upstream sync"

        target_short = self.state.target_commit[:12]
        commit_msg = (
            f"fix: {commit_summary}\n\n"
            f"Upstream target: {target_short}\n"
            f"Fix attempt: {self.state.retry_count}\n"
            f"Work branch: {self.state.work_branch}\n"
        )

        try:
            run_git(ascend_path, "add", "-u")
            run_git(ascend_path, "commit", "-s", "-m", commit_msg)
            print_status(True, f"Committed fix: {commit_summary[:60]}")
            self.state.summary_rows.append(("Commit fixes", "PASS", commit_summary[:40]))
        except Exception as e:
            print_warn(f"Could not commit fixes: {e}")
            self.state.summary_rows.append(("Commit fixes", "WARN", str(e)[:40]))

    def _do_build(self, ascend_path: Path, clean: bool = False) -> bool:
        start_timer("build")
        print_section("Build Triton-Ascend")

        if os.getenv("SKIP_BUILD", "false").lower() == "true":
            print_info("SKIP_BUILD=true — skipping build")
            self.state.build_passed = True
            stop_timer("build")
            self.state.summary_rows.append(("Build", "SKIP", "SKIP_BUILD set"))
            return True

        build_result = build_triton_ascend(
            ascend_path,
            llvm_prefix=self.state.llvm_prefix,
            conda_env=self.state.conda_env,
            clean_build=clean,
        )
        self.state.build_passed = build_result["all_passed"]
        stop_timer("build")

        if not self.state.build_passed:
            print_error("Build FAILED")
            self.state.summary_rows.append(("Build", "FAIL", "See build log"))
            return False

        print_status(True, "Build passed")
        self.state.summary_rows.append(("Build", "PASS", ""))
        return True

    def _do_test(self, ascend_path: Path) -> bool | None:
        start_timer("test")
        print_section("Run Tests")

        if os.getenv("SKIP_E2E_TEST", "false").lower() == "true":
            print_info("SKIP_E2E_TEST=true — treating tests as passed")
            self.state.test_passed = True
            stop_timer("test")
            self.state.summary_rows.append(("Tests", "SKIP", "SKIP_E2E_TEST set"))
            return None

        test_dir_path = ascend_path / self.state.test_dir
        print_info(f"Test directory: {test_dir_path}")
        print_info(f"Python: {os.getenv('TA_PYTHON', 'python3')}, procs: {self.state.num_procs}")

        try:
            test_result = run_tests(
                ascend_path,
                test_dir=self.state.test_dir,
                num_procs=self.state.num_procs,
                conda_env=self.state.conda_env,
            )
        except Exception as exc:
            print_error(f"run_tests raised exception: {exc}")
            import traceback
            traceback.print_exc()
            self.state.test_passed = False
            stop_timer("test")
            self.state.summary_rows.append(("Tests", "FAIL", f"Exception: {exc}"))
            return False

        self.state.test_passed = test_result["passed"]
        stop_timer("test")

        if test_result["passed"]:
            passed_count = test_result.get("passed_count", "?")
            print_status(True, f"All tests passed ({passed_count} passed)")
            self.state.summary_rows.append(("Tests", "PASS", f"{passed_count} passed"))
            return True
        else:
            failed_count = test_result.get("failed_count", "?")
            error_count = test_result.get("error_count", 0)
            error_msg = test_result.get("error", "")
            if error_msg:
                print_error(f"Tests FAILED — {error_msg}")
            else:
                print_error(f"Tests FAILED ({failed_count} failed, {error_count} errors)")
            self.state.summary_rows.append(
                ("Tests", "FAIL", f"{failed_count} failed, {error_count} errors")
            )
            return False

    def _do_ai_fix(self, ascend_path: Path, step_dir: Path, attempt: int) -> bool:
        """AI fix bug: invoke opencode/claude to fix build/test failures.

        AI context includes: step index, is_last_step, previous_step_summary
        (matching vllm-ascend's main2main_flow pattern).
        """
        print_step(attempt, self.state.max_retries, "AI fix attempt")

        step = self.state.steps[self.state.current_step] if self.state.steps else None
        current_step_id = step["id"] if step else "step-0"
        is_last_step = self.state.current_step == self.state.total_steps - 1

        # ── Previous step context (matching vllm-ascend pattern) ──
        previous_step = (
            self.state.steps[self.state.current_step - 1]
            if self.state.current_step > 0 and self.state.steps else None
        )
        previous_step_id = previous_step["id"] if previous_step else ""
        previous_step_summary_path = (
            str(WORKSPACE_DIR / STEPS_DIR / previous_step_id / EACH_STEP_SUMMARY_FILE)
            if previous_step_id else ""
        )

        # Per-attempt fix directory for logs/artifacts. The step_dir is the
        # canonical per-step directory (matching vllm-ascend pattern).
        fix_dir = WORKSPACE_DIR / FIX_LOG_DIR / f"{current_step_id}-fix-{attempt}"
        fix_dir.mkdir(parents=True, exist_ok=True)

        print_info(f"Error sources ({len(self.state.fix_errors)}):")
        for e in self.state.fix_errors:
            print(f"      • {e}")

        # AI fix bug: detect backend
        try:
            from TA_main2main_workflow.agent.opencode_adapter import _detect_backend
            backend = _detect_backend()
        except RuntimeError as e:
            print_error(f"AI backend not available: {e}")
            return False

        print_ai_call_info(
            backend=backend,
            mode="fix",
            attempt=attempt,
            max_attempts=self.state.max_retries,
        )

        # AI fix bug: invoke opencode/claude with error logs
        # Context matches vllm-ascend pattern: is_last_step,
        # previous_step_id, previous_step_summary_path, step index.
        # step_dir points to the canonical step directory (like vllm-ascend);
        # fix_dir captures per-attempt fix artifacts separately.
        error_logs = json.dumps(self.state.fix_errors, ensure_ascii=False)
        try:
            ai_result = run_opencode_adapter({
                "step_id": f"{current_step_id}-fix-{attempt}",
                "previous_step_id": previous_step_id,
                "previous_step_summary_path": previous_step_summary_path,
                "is_last_step": str(is_last_step).lower(),
                "step_index": f"{self.state.current_step + 1}/{self.state.total_steps}",
                "step_dir": str(step_dir),
                "fix_dir": str(fix_dir),
                "conflict_dir": "",
                "ascend_path": str(ascend_path),
                "triton_path": self.state.triton_path,
                "reference_dir": _REFERENCE_DIR,
                "mode": "fix",
                "error_logs": error_logs,
                "target_commit": self.state.target_commit,
            })

            print_ai_result(
                ok=bool(ai_result.modified_files),
                modified_files=ai_result.modified_files,
                summary=(ai_result.step_summary or "")[:500],
            )

            print_info("Running pre-CI check after fix...")
            run_pre_ci_check(ascend_path, step_id=f"fix-{attempt}")

            return bool(ai_result.modified_files)

        except Exception as e:
            print_error(f"AI fix call failed: {e}")
            return False

    def _do_commit_step(self, step: dict) -> None:
        """Commit the current step's progress with a descriptive message.

        Only commits if there are uncommitted changes. Uses "git add -u" to
        avoid staging test artifacts or transient files.
        """
        ascend_path = Path(self.state.triton_ascend_path)
        step_id = step["id"]
        status = run_git(ascend_path, "status", "--porcelain").strip()

        if not status:
            print_info(f"[{step_id}] No uncommitted changes — nothing to commit")
            self.state.summary_rows.append(
                (f"Commit {step_id}", "PASS", "No changes (clean merge)")
            )
            return

        print_section(f"Commit Step {step_id}")

        # Clean up temp artifacts before staging to avoid committing them
        cleanup_temp_files(ascend_path)

        end_commit_short = step["end_commit"][:12]
        commit_msg = (
            f"sync: merge upstream commits for step {step_id}\n\n"
            f"Upstream range: {step.get('start_commit', '?')[:12]}..{end_commit_short}\n"
            f"Step: {self.state.current_step + 1}/{self.state.total_steps}\n"
            f"Commits in step: {step['commit_count']}\n"
            f"Work branch: {self.state.work_branch}\n"
            f"All steps on single branch: {self.state.work_branch}\n"
        )

        try:
            run_git(ascend_path, "add", "-u")
            run_git(ascend_path, "commit", "-s", "-m", commit_msg)
            print_status(True, f"Committed step {step_id}")
            self.state.summary_rows.append(
                (f"Commit {step_id}", "PASS", f"{step['commit_count']} commits")
            )
        except Exception as e:
            print_warn(f"Could not commit step {step_id}: {e}")
            self.state.summary_rows.append(
                (f"Commit {step_id}", "WARN", str(e)[:40])
            )

    def _do_finalize(self):
        """Generate patch, summary & print final report.

        Does NOT restore the original branch — the work branch must stay
        checked out so push_to_github can push it. Branch restore happens
        at the end of push_to_github (or handle_failure).
        """
        print_header("Phase Final: Finalize & Summary")

        ascend_path = Path(self.state.triton_ascend_path)

        # ── Generate final summary ──
        print_section("Generate Final Summary")
        final_summary_path = WORKSPACE_DIR / FINAL_SUMMARY_FILE

        # Collect step summaries if available
        steps_dir = WORKSPACE_DIR / STEPS_DIR
        if self.state.total_steps > 1 and steps_dir.exists():
            summaries = []
            for step in self.state.steps:
                step_dir = steps_dir / step["id"]
                summary_file = step_dir / EACH_STEP_SUMMARY_FILE
                if summary_file.exists():
                    summaries.append(
                        f"## {step['id']}\n\n"
                        f"{summary_file.read_text(encoding='utf-8').strip()}"
                    )
            if summaries:
                final_summary_path.write_text("\n\n".join(summaries), encoding="utf-8")
            else:
                final_summary_path.write_text(
                    f"# Triton-Ascend Upstream Sync\n\n"
                    f"- **Target**: `{self.state.target_commit[:12]}`\n"
                    f"- **Steps**: {self.state.total_steps}\n"
                    f"- **Work branch**: `{self.state.work_branch}`\n"
                    f"- **Status**: Success\n"
                    f"- **Date**: {time.strftime('%Y-%m-%d %H:%M:%S')}\n",
                    encoding="utf-8",
                )
        else:
            step_dir = WORKSPACE_DIR / "step-0"
            last_summary_path = step_dir / EACH_STEP_SUMMARY_FILE
            if last_summary_path.exists():
                shutil.copy2(last_summary_path, final_summary_path)
            else:
                final_summary_path.write_text(
                    f"# Triton-Ascend Upstream Sync\n\n"
                    f"- **Target**: `{self.state.target_commit[:12]}`\n"
                    f"- **Work branch**: `{self.state.work_branch}`\n"
                    f"- **Status**: Success\n"
                    f"- **Upstream commits merged**: {self.state.upstream_commits_count}\n"
                    f"- **Date**: {time.strftime('%Y-%m-%d %H:%M:%S')}\n",
                    encoding="utf-8",
                )

        print_info(f"Final summary: {final_summary_path}")

        # ── Generate cumulative patch (from original ascend HEAD to latest) ──
        try:
            patch = run_git(ascend_path, "diff", self.state.ascend_head, "HEAD")
            patch_path = WORKSPACE_DIR / FINAL_TARGET_PATCH_FILE
            patch_path.write_text(patch, encoding="utf-8")
            print_info(f"Cumulative patch: {patch_path} ({len(patch)} bytes)")
        except Exception as e:
            print_warn(f"Could not generate final patch: {e}")

        self.state.summary_rows.append(
            ("Finalize", "PASS", f"{self.state.total_steps} step(s) completed")
        )

        # ── Print final summary table ──
        print_header("Sync Complete — Success!")
        print_elapsed_total()
        self.state.summary_rows.append(("OVERALL", "PASS", f"{self.state.total_steps} step(s) completed"))
        print_summary_table(self.state.summary_rows)

        print_section("Output Files")
        for f in sorted(WORKSPACE_DIR.rglob("*")):
            if f.is_file() and ".git" not in str(f):
                print(f"    {f.relative_to(WORKSPACE_DIR)}")
        print_info(f"Work branch preserved: {self.state.work_branch}")
        print_info(f"To inspect: cd {ascend_path} && git checkout {self.state.work_branch}")

    # ═══════════════════════════════════════════════════════════════════════════
    # Terminal nodes (routed from execute_sync)
    # ═══════════════════════════════════════════════════════════════════════════

    @listen(UpgradeCompleted)
    def push_to_github(self):
        """Push work branch & create a single GitHub PR after ALL steps complete.

        In the vllm-ascend step-by-step merge style, all step commits accumulate
        on the work branch locally. Only after every step passes (merge →
        resolve → build → test → fix → commit) do we push and open one PR.
        """
        if os.getenv("PUSH_TO_GITHUB", "false").lower() != "true":
            print_info("PUSH_TO_GITHUB is not 'true' — skipping PR creation")
            print_info("To push manually:")
            print_info(f"  cd {self.state.triton_ascend_path}")
            print_info(f"  git checkout {self.state.work_branch}")
            print_info(f"  git push -u origin {self.state.work_branch}")
            self.state.summary_rows.append(("Push & PR", "SKIP", "PUSH_TO_GITHUB not set"))
            return "SKIP_PUSH"

        print_header("Push to GitHub & Create PR")
        github_repo = os.getenv("GITHUB_REPO", "TecJesh/triton-ascend")
        if not github_repo:
            print_error("GITHUB_REPO is empty — cannot create PR")
            self.state.summary_rows.append(("Push & PR", "FAIL", "GITHUB_REPO empty"))
            return "SKIP_PUSH"

        # ── Build a comprehensive PR body from step summaries ──
        pr_body_path = WORKSPACE_DIR / FINAL_SUMMARY_FILE
        self._build_pr_body(pr_body_path)

        try:
            pr_url = push_and_create_pr(
                ascend_path=Path(self.state.triton_ascend_path),
                github_repo=github_repo,
                work_branch=self.state.work_branch,
                summary_path=pr_body_path,
            )
            self.state.pr_url = pr_url
            print_status(True, f"PR created: {pr_url}")
            self.state.summary_rows.append(("Push & PR", "PASS", pr_url))
        except Exception as e:
            print_error(f"Failed to push/create PR: {e}")
            self.state.summary_rows.append(("Push & PR", "FAIL", str(e)[:60]))

        # ── Restore original branch after push ──
        self._restore_branch()
        return self.state.pr_url if self.state.pr_url else "SKIP_PUSH"

    def _restore_branch(self) -> None:
        """Restore the original branch after all work is done."""
        ascend_path = Path(self.state.triton_ascend_path)
        print_section("Restore Original Branch")
        try:
            current = run_git(ascend_path, "branch", "--show-current").strip()
            if current != self.state.original_branch:
                run_git(ascend_path, "checkout", self.state.original_branch)
                print_status(True, f"Restored to '{self.state.original_branch}'")
            else:
                print_info(f"Already on '{self.state.original_branch}'")
        except Exception as e:
            print_warn(f"Could not restore branch: {e}")
            print_info(f"Work branch '{self.state.work_branch}' left checked out")

    def _build_pr_body(self, output_path: Path) -> None:
        """Build a comprehensive PR body from all step descriptions and summaries."""
        parts: list[str] = []

        # Title / overview
        parts.append(
            "# Triton-Ascend Upstream Sync\n\n"
            f"- **Target commit**: `{self.state.target_commit[:12]}`\n"
            f"- **Work branch**: `{self.state.work_branch}`\n"
            f"- **Steps completed**: {self.state.total_steps}\n"
            f"- **Upstream commits merged**: {self.state.upstream_commits_count}\n"
            f"- **Date**: {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
        )

        # Per-step progress
        if self.state.step_pr_descriptions:
            parts.append("## Step Progress\n")
            for desc in self.state.step_pr_descriptions:
                parts.append(f"- {desc}\n")

        # Per-step AI summaries (if available)
        steps_dir = WORKSPACE_DIR / STEPS_DIR
        if self.state.total_steps > 1 and steps_dir.exists():
            parts.append("\n## Step Details\n")
            for step in self.state.steps:
                step_dir = steps_dir / step["id"]
                summary_file = step_dir / EACH_STEP_SUMMARY_FILE
                if summary_file.exists():
                    parts.append(
                        f"### {step['id']}\n\n"
                        f"{summary_file.read_text(encoding='utf-8').strip()}\n\n"
                    )
                else:
                    parts.append(
                        f"### {step['id']}\n\n"
                        f"- Commits: {step['commit_count']}\n"
                        f"- End commit: `{step['end_commit'][:12]}`\n"
                        f"- Source lines changed: {step.get('source_changed_lines', '?')}\n\n"
                    )
        elif steps_dir.exists():
            # Single step: include its summary
            step_dir = WORKSPACE_DIR / "step-0"
            summary_file = step_dir / EACH_STEP_SUMMARY_FILE
            if summary_file.exists():
                parts.append(
                    "\n## Summary\n\n"
                    f"{summary_file.read_text(encoding='utf-8').strip()}\n"
                )
        else:
            # Fallback: just the final summary
            fallback = WORKSPACE_DIR / FINAL_SUMMARY_FILE
            if fallback.exists():
                parts.append(fallback.read_text(encoding='utf-8'))

        parts.append(
            f"\n---\n"
            f"🤖 Generated with [TA_main2main_workflow]"
            f"(https://github.com/TecJesh/TA-AI-WorkFlow)"
            f" at {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
        )

        output_path.write_text("".join(parts), encoding="utf-8")
        print_info(f"PR body written to {output_path}")

    @listen(UpgradeFailed)
    def handle_failure(self):
        """write FAILURE.md, print diagnostics & summary, suggest recovery commands."""
        print_header("Sync Failed — Diagnostics")

        ascend_path = Path(self.state.triton_ascend_path)

        print_error(f"Upgrade failed after {self.state.retry_count} retries")

        print_section("Failure Details")
        print_key_value("Target commit", self.state.target_commit[:12])
        print_key_value("Work branch", self.state.work_branch)
        print_key_value("Original branch", self.state.original_branch)
        print_key_value("Conflict files", ", ".join(self.state.conflict_files) if self.state.conflict_files else "none")
        print_key_value("Build passed", str(self.state.build_passed))
        print_key_value("Test passed", str(self.state.test_passed))

        failure_path = WORKSPACE_DIR / "FAILURE.md"
        failure_text = (
            f"# Upgrade Failed\n\n"
            f"- **Target**: `{self.state.target_commit[:12]}`\n"
            f"- **Work branch**: `{self.state.work_branch}`\n"
            f"- **Original branch**: `{self.state.original_branch}`\n"
            f"- **Retries**: {self.state.retry_count}/{self.state.max_retries}\n"
            f"- **Conflict files**: {', '.join(self.state.conflict_files) if self.state.conflict_files else 'none'}\n"
            f"- **Build passed**: {self.state.build_passed}\n"
            f"- **Test passed**: {self.state.test_passed}\n"
            f"- **Date**: {time.strftime('%Y-%m-%d %H:%M:%S')}\n\n"
            f"## Recovery\n\n"
            f"```bash\n"
            f"cd {ascend_path}\n"
            f"git checkout {self.state.original_branch}\n"
            f"# Work branch '{self.state.work_branch}' has the partial merge\n"
            f"# git branch -D {self.state.work_branch}\n"
            f"```\n"
        )
        failure_path.write_text(failure_text, encoding="utf-8")
        print_info(f"Failure report: {failure_path}")

        print_elapsed_total()
        self.state.summary_rows.append(("OVERALL", "FAIL", f"Failed after {self.state.retry_count} retries"))
        print_summary_table(self.state.summary_rows)

        print_section("Recovery")
        print_info(f"Work branch '{self.state.work_branch}' preserved for manual inspection")
        print_info(f"To restore:  cd {ascend_path} && git checkout {self.state.original_branch}")
        print_info(f"To clean up: cd {ascend_path} && git branch -D {self.state.work_branch}")
