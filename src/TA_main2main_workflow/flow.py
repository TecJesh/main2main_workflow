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
import subprocess
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
    STEPS_DIR, STEPS_FILE, LINE_BUDGET,
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

    # ── Per-step tracking for sync report ──
    build_fix_count: int = 0        # AI fix attempts for build failures
    test_fix_count: int = 0         # AI fix attempts for test failures
    conflict_files_resolved: int = 0  # Total merge conflicts resolved
    step_details: list = []         # Per-step breakdown for report
    fix_attempts: list = []         # Detailed fix attempt records

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
    # Mode dispatch — supports full (CrewAI), merge-only, and fix-only modes
    # ═══════════════════════════════════════════════════════════════════════════

    def kickoff(self, inputs: dict | None = None):
        """Override CrewAI Flow.kickoff() to support TA_MODE dispatch.

        TA_MODE values:
          full   — Original CrewAI flow: merge → resolve → build → test → fix → PR
          merge  — Merge + AI resolve only, push work branch, skip build/test.
                   Used on ubuntu-latest CI to prepare the work branch before
                   NPU testing.
          fix    — AI fix on an existing work branch. Reads error logs from
                   TA_ERROR_LOGS_PATH, runs AI fix, commits & pushes.
        """
        mode = os.getenv("TA_MODE", "full")
        if mode == "merge":
            return self._run_merge_mode(inputs)
        elif mode == "fix":
            return self._run_fix_mode(inputs)
        else:
            return super().kickoff(inputs=inputs)

    # ═══════════════════════════════════════════════════════════════════════════
    # Mode: merge — AI merge + resolve ONE step, then push work branch
    # ═══════════════════════════════════════════════════════════════════════════

    def _run_merge_mode(self, inputs: dict | None) -> str:
        """Merge + AI-resolve for ONE progressive step. Push work branch, no build/test.

        Used in CI (ubuntu-latest) as the merge phase of the per-step pipeline.
        Each call merges exactly one step's batch of upstream commits. The CI
        workflow orchestrates the per-step loop:

          For each step N:
            → ta-kickoff --mode=merge (merges step N, resolves conflicts, pushes)
            → NPU build+test
            → AI fix retries (if needed)
            → advance to step N+1

        Env vars:
          TA_CURRENT_STEP   — which step index to merge (0-based, default 0).
                              Step 0 does full init + detect + plan first.
                              Step N>0 resumes from an existing work branch.
        """
        current_step = int(os.getenv("TA_CURRENT_STEP", "0"))

        # ── Apply inputs to state ──
        if inputs:
            for key, value in inputs.items():
                if hasattr(self.state, key):
                    setattr(self.state, key, value)

        # ── Force skip build/test in merge mode ──
        os.environ["SKIP_BUILD"] = "true"
        os.environ["SKIP_E2E_TEST"] = "true"

        ascend_path = Path(self.state.triton_ascend_path)

        if current_step == 0:
            # ── First step: full init + detect + plan ──
            self.initialize()
            result = self.detect_commits()
            if result == HasNoNewCommits:
                print_info("No new commits — nothing to merge")
                metadata_dir = WORKSPACE_DIR / "merge-metadata"
                metadata_dir.mkdir(parents=True, exist_ok=True)
                (metadata_dir / "no_changes.txt").write_text("true", encoding="utf-8")
                self.state.summary_rows.append(
                    ("MERGE PHASE", "SKIP", "No new upstream commits")
                )
                print_summary_table(self.state.summary_rows)
                return UpgradeCompleted

            # Store the plan for subsequent steps
            self._write_step_plan()
        else:
            # ── Resume: checkout existing work branch ──
            work_branch = os.getenv("TA_WORK_BRANCH", self.state.work_branch)
            if not work_branch:
                print_error("TA_WORK_BRANCH is required for TA_CURRENT_STEP > 0")
                return UpgradeFailed

            # Restore state from work branch metadata
            self.state.work_branch = work_branch
            self.state.triton_ascend_path = (
                self.state.triton_ascend_path
                or os.getenv("TRITON_ASCEND_PATH")
                or str(Path.cwd())
            )
            self.state.target_commit = (
                self.state.target_commit or os.getenv("TRITON_TARGET_COMMIT", "")
            )

            # Read step plan saved from step 0
            plan_file = WORKSPACE_DIR / "merge-metadata" / "step_plan.json"
            if not plan_file.exists():
                print_warn("Step plan file not found — re-detecting commits")
                # Lightweight re-init without full initialize
                self.state.triton_ascend_path = self.state.triton_ascend_path or str(Path.cwd())
                self.state.triton_path = os.getenv("TRITON_PATH", self.state.triton_ascend_path)
                ascend_path = Path(self.state.triton_ascend_path)
                # Fetch and checkout work branch
                try:
                    run_git(ascend_path, "fetch", "origin", work_branch)
                except Exception:
                    pass
                run_git(ascend_path, "checkout", work_branch)
                result = self.detect_commits()
                if result == HasNoNewCommits:
                    return UpgradeCompleted
                self._write_step_plan()
            else:
                import json
                plan_data = json.loads(plan_file.read_text(encoding="utf-8"))
                self.state.total_steps = plan_data["total_steps"]
                self.state.steps = plan_data["steps"]
                self.state.upstream_commits_count = plan_data.get("upstream_commits_count", 0)

                # Minimal init for resume
                self.state.triton_path = os.getenv("TRITON_PATH", str(ascend_path))
                # Fetch and checkout work branch
                try:
                    run_git(ascend_path, "fetch", "origin", work_branch)
                except Exception:
                    pass
                run_git(ascend_path, "checkout", work_branch)

        # ── Validate step index ──
        if current_step >= self.state.total_steps:
            print_info(f"current_step={current_step} >= total_steps={self.state.total_steps} — "
                       f"all steps already merged")
            metadata_dir = WORKSPACE_DIR / "merge-metadata"
            metadata_dir.mkdir(parents=True, exist_ok=True)
            (metadata_dir / "all_steps_done.txt").write_text("true", encoding="utf-8")
            self._write_merge_metadata()
            return UpgradeCompleted

        step = self.state.steps[current_step]
        step_id = step["id"]
        is_last_step = (current_step == self.state.total_steps - 1)
        self.state.current_step = current_step
        self.state.retry_count = 0

        print_header(
            f"Step {current_step + 1}/{self.state.total_steps}: {step_id}"
        )
        print_key_value("commits in step", str(step["commit_count"]))
        print_key_value("end commit", step["end_commit"][:12])
        print_key_value("is last step", str(is_last_step))

        # ── Work-branch guard ──
        if current_step > 0 and self.state.work_branch:
            current_branch = run_git(ascend_path, "branch", "--show-current").strip()
            if current_branch != self.state.work_branch:
                print_warn(
                    f"Expected work branch '{self.state.work_branch}' "
                    f"but on '{current_branch}' — switching"
                )
                run_git(ascend_path, "checkout", self.state.work_branch)

        self.state.step_start_ascend_head = run_git(
            ascend_path, "rev-parse", "HEAD"
        ).strip()

        # ── Step A: git merge this step's commits ──
        merge_result = self._do_step_merge(step)
        if merge_result == UpgradeFailed:
            self.state.final_status = UpgradeFailed
            self._write_merge_metadata()
            return UpgradeFailed

        # ── Step B: AI resolve conflicts ──
        if self.state.merge_has_conflicts:
            if not self._do_resolve_conflicts():
                self.state.final_status = UpgradeFailed
                self._write_merge_metadata()
                return UpgradeFailed

        # ── Step C: Skip build/test (NPU CI runs these) ──
        print_header("Build & Test — Merge Mode")
        print_info(f"Merge mode: deferring build/test for step {step_id} to NPU CI")
        self.state.build_passed = True
        self.state.test_passed = True
        self.state.summary_rows.append(("Build", "DEFER", "Runs on NPU CI"))
        self.state.summary_rows.append(("Tests", "DEFER", "Runs on NPU CI"))

        # ── Step D: Commit step merge progress ──
        self._do_commit_step(step)

        # Record step description
        desc = (
            f"✅ **{step_id}**: {step['commit_count']} commits, "
            f"end_commit=`{step['end_commit'][:12]}`, "
            f"source lines={step.get('source_changed_lines', '?')}"
        )
        self.state.step_pr_descriptions.append(desc)
        print_status(True, f"Step {step_id} merge committed")

        # ── Push work branch ──
        self._push_work_branch_to_remote()

        # ── Write metadata for CI orchestration ──
        self._write_merge_metadata()

        # Print summary
        print_header(f"Merge Phase Complete — Step {step_id}")
        print_key_value("Work branch", self.state.work_branch)
        print_key_value("Current step", f"{current_step + 1}/{self.state.total_steps}")
        print_key_value("Target commit", self.state.target_commit[:12])
        print_key_value("Is last step", str(is_last_step))
        print_info(f"Pushed to origin/{self.state.work_branch}")
        if is_last_step:
            print_info("This is the last step — PR will be created if tests pass")
        else:
            next_step_id = self.state.steps[current_step + 1]["id"]
            print_info(f"Next: NPU tests on this step, then merge step {next_step_id}")

        self.state.summary_rows.append(
            ("MERGE PHASE", "PASS", f"Step {step_id}, branch: {self.state.work_branch}")
        )
        print_summary_table(self.state.summary_rows)

        return UpgradeCompleted

    def _write_step_plan(self) -> None:
        """Persist the step plan so subsequent merge-mode calls can resume."""
        import json
        metadata_dir = WORKSPACE_DIR / "merge-metadata"
        metadata_dir.mkdir(parents=True, exist_ok=True)
        plan_data = {
            "total_steps": self.state.total_steps,
            "steps": self.state.steps,
            "upstream_commits_count": self.state.upstream_commits_count,
        }
        (metadata_dir / "step_plan.json").write_text(
            json.dumps(plan_data, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        print_info(f"Step plan saved: {self.state.total_steps} step(s)")

    def _push_work_branch_to_remote(self) -> None:
        """Push the work branch to origin so NPU CI can access it."""
        ascend_path = Path(self.state.triton_ascend_path)

        # Check we're on the work branch
        current = run_git(ascend_path, "branch", "--show-current").strip()
        if current != self.state.work_branch:
            run_git(ascend_path, "checkout", self.state.work_branch)

        # ── Configure git auth (same logic as push_to_github._ensure_gh_auth) ──
        self._setup_git_auth_for_push(ascend_path)

        print_header("Push Work Branch")
        try:
            run_git(ascend_path, "push", "-u", "origin", self.state.work_branch)
            print_status(True, f"Pushed {self.state.work_branch} to origin")
            self.state.summary_rows.append(
                ("Push branch", "PASS", self.state.work_branch)
            )
        except Exception as e:
            print_error(f"Failed to push work branch: {e}")
            # Try with force if normal push fails (e.g., branch exists from prior run)
            try:
                print_warn("Retrying with --force...")
                run_git(
                    ascend_path, "push", "-u", "--force",
                    "origin", self.state.work_branch,
                )
                print_status(True, f"Force-pushed {self.state.work_branch}")
            except Exception:
                print_error("Force push also failed")
                raise

    def _setup_git_auth_for_push(self, repo: Path) -> None:
        """Configure git authentication for pushing to GitHub.

        Mirrors the logic in push_to_github._ensure_gh_auth():
        1. Run 'gh auth setup-git' to configure the git credential helper.
        2. When GH_TOKEN is set, rewrite the origin URL to embed the token
           so git push works even if the credential helper misbehaves.
        """
        gh_token = os.getenv("GH_TOKEN", "")
        if gh_token:
            print_info("GH_TOKEN set — configuring git credential helper")
            subprocess.run(
                ["gh", "auth", "setup-git"],
                check=True, capture_output=True, text=True,
            )
            # Rewrite origin URL to embed token
            try:
                origin_url = run_git(repo, "remote", "get-url", "origin").strip()
                if origin_url.startswith("https://"):
                    clean_url = origin_url.replace("https://", "", 1)
                    if "@" in clean_url:
                        clean_url = clean_url.split("@", 1)[1]
                    new_url = f"https://x-access-token:{gh_token}@{clean_url}"
                    run_git(repo, "remote", "set-url", "origin", new_url)
                    safe = f"https://x-access-token:***@{clean_url}"
                    print_info(f"origin URL rewritten with token: {safe}")
            except Exception as exc:
                print_warn(f"Could not rewrite origin URL: {exc}")
        else:
            # Verify gh CLI is authenticated (interactive or env-based)
            try:
                subprocess.run(
                    ["gh", "auth", "status"],
                    check=True, capture_output=True, text=True,
                )
                subprocess.run(
                    ["gh", "auth", "setup-git"],
                    check=True, capture_output=True, text=True,
                )
                print_info("Git credential helper configured via gh")
            except subprocess.CalledProcessError as e:
                print_error(
                    f"gh not authenticated and GH_TOKEN not set: {e.stderr.strip()}"
                )
                raise RuntimeError(
                    "Cannot push to GitHub: no GH_TOKEN and gh CLI not authenticated. "
                    "Run 'gh auth login' locally or set GH_TOKEN in CI."
                )

    def _write_merge_metadata(self) -> None:
        """Write work branch, target commit, and step progress for CI orchestration."""
        metadata_dir = WORKSPACE_DIR / "merge-metadata"
        metadata_dir.mkdir(parents=True, exist_ok=True)

        (metadata_dir / "work_branch.txt").write_text(
            self.state.work_branch, encoding="utf-8"
        )
        (metadata_dir / "target_commit.txt").write_text(
            self.state.target_commit, encoding="utf-8"
        )
        (metadata_dir / "current_step.txt").write_text(
            str(self.state.current_step), encoding="utf-8"
        )
        (metadata_dir / "total_steps.txt").write_text(
            str(self.state.total_steps), encoding="utf-8"
        )
        is_last = (self.state.current_step >= self.state.total_steps - 1)
        (metadata_dir / "is_last_step.txt").write_text(
            str(is_last).lower(), encoding="utf-8"
        )
        print_info(f"Metadata written to {metadata_dir} "
                   f"(step {self.state.current_step + 1}/{self.state.total_steps}, "
                   f"is_last={is_last})")

    # ═══════════════════════════════════════════════════════════════════════════
    # Mode: fix — AI fix on existing work branch
    # ═══════════════════════════════════════════════════════════════════════════

    def _run_fix_mode(self, inputs: dict | None) -> str:
        """AI fix on an existing work branch.

        Reads error logs from TA_ERROR_LOGS_PATH, calls the AI fix engine
        (_do_ai_fix), commits & pushes fixes. Used in CI after NPU tests fail.
        """
        ascend_path_str = (
            (inputs or {}).get("triton_ascend_path")
            or os.getenv("TRITON_ASCEND_PATH")
            or str(Path.cwd())
        )
        work_branch = os.getenv("TA_WORK_BRANCH", "")
        error_logs_path = os.getenv("TA_ERROR_LOGS_PATH", "")
        attempt = int(os.getenv("TA_FIX_ATTEMPT", "1"))
        target_commit = (
            (inputs or {}).get("target_commit")
            or os.getenv("TRITON_TARGET_COMMIT", "")
        )

        if not work_branch:
            print_error("TA_WORK_BRANCH is required for fix mode")
            return UpgradeFailed

        ascend_path = Path(ascend_path_str)

        # ── Setup ──
        print_header(f"Fix Mode — Attempt {attempt}")
        print_key_value("work branch", work_branch)
        print_key_value("error logs", error_logs_path or "<none>")
        print_key_value("target commit", target_commit[:12] if target_commit else "<none>")
        print_key_value("repo path", str(ascend_path))

        # Clean old workspace
        if WORKSPACE_DIR.exists():
            shutil.rmtree(WORKSPACE_DIR)
        WORKSPACE_DIR.mkdir(parents=True)

        # Populate minimal state
        self.state.triton_ascend_path = str(ascend_path)
        self.state.triton_path = os.getenv("TRITON_PATH", str(ascend_path))
        self.state.target_commit = target_commit
        self.state.work_branch = work_branch
        self.state.original_branch = work_branch
        self.state.current_step = 0
        self.state.total_steps = 1
        self.state.steps = [{
            "index": 1,
            "id": "fix-step-1",
            "commit_count": 0,
            "end_commit": target_commit or "",
            "source_changed_lines": 0,
        }]

        # ── Checkout work branch ──
        print_section("Checkout Work Branch")
        try:
            run_git(ascend_path, "fetch", "origin", work_branch)
        except Exception as e:
            print_warn(f"Could not fetch {work_branch}: {e}")
        run_git(ascend_path, "checkout", work_branch)
        print_status(True, f"Checked out {work_branch}")

        # Pull latest (in case previous fix attempts pushed)
        try:
            run_git(ascend_path, "pull", "origin", work_branch)
            print_info("Pulled latest changes")
        except Exception:
            print_warn("Could not pull latest — continuing with local state")

        # ── Collect error logs ──
        fix_errors: list[str] = []
        if error_logs_path:
            error_path = Path(error_logs_path)
            if error_path.exists():
                if error_path.is_dir():
                    fix_errors = sorted(
                        str(p) for p in error_path.rglob("*") if p.is_file()
                    )
                    print_info(f"Found {len(fix_errors)} error log file(s)")
                else:
                    fix_errors = [str(error_path)]
                    print_info(f"Using error log: {error_path}")

        if not fix_errors:
            print_warn("No error logs found — AI will analyze the codebase directly")
            # Create a stub so _do_ai_fix has something to work with
            stub_log = WORKSPACE_DIR / "no-error-logs.txt"
            stub_log.write_text(
                "No specific error logs were provided from the NPU CI run.\n"
                "Please analyze the triton-ascend codebase for potential issues\n"
                f"that could cause build or test failures after merging upstream triton.\n"
                f"Target upstream commit: {target_commit}\n"
                f"Work branch: {work_branch}\n"
            )
            fix_errors = [str(stub_log)]

        self.state.fix_errors = fix_errors

        # ── Set up step directory ──
        step_dir = WORKSPACE_DIR / "fix-step-1"
        step_dir.mkdir(parents=True, exist_ok=True)

        # ── Run AI fix ──
        print_header("AI Fix Analysis")
        print_info(f"Error sources ({len(fix_errors)}):")
        for e in fix_errors[:10]:
            print(f"      • {e}")
        if len(fix_errors) > 10:
            print(f"      ... and {len(fix_errors) - 10} more")

        try:
            fix_ok = self._do_ai_fix(ascend_path, step_dir, attempt)
        except Exception as e:
            print_error(f"AI fix crashed: {e}")
            import traceback
            traceback.print_exc()
            fix_ok = False

        if not fix_ok:
            print_error("AI fix did not produce any changes")
            self.state.summary_rows.append(
                ("AI fix", "FAIL", "No changes produced")
            )
            print_summary_table(self.state.summary_rows)
            return UpgradeFailed

        # ── Commit and push ──
        print_section("Commit & Push Fixes")
        status = run_git(ascend_path, "status", "--porcelain").strip()
        if status:
            run_git(ascend_path, "add", "-u")
            commit_target = target_commit[:12] if target_commit else "upstream"
            commit_msg = (
                f"fix: AI-generated fix for build/test failures\n\n"
                f"Upstream target: {commit_target}\n"
                f"Fix attempt: {attempt}\n"
                f"Work branch: {work_branch}\n"
            )
            run_git(ascend_path, "commit", "-s", "-m", commit_msg)
            print_status(True, "Committed AI fix")

            run_git(ascend_path, "push", "origin", work_branch)
            print_status(True, f"Pushed to origin/{work_branch}")
            self.state.summary_rows.append(
                ("AI fix", "PASS", f"Attempt {attempt}")
            )
        else:
            print_info("No changes to commit after AI fix")
            self.state.summary_rows.append(
                ("AI fix", "NOOP", "No changes needed")
            )

        print_header("Fix Phase Complete!")
        print_key_value("work branch", work_branch)
        print_key_value("attempt", str(attempt))
        print_info(f"Next: re-trigger NPU tests on branch '{work_branch}'")

        print_summary_table(self.state.summary_rows)
        return UpgradeCompleted

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

        # ── Plan steps: split commits into chunks based on line budget ──
        if self.state.progressive_merge and self.state.upstream_commits_count > 1:
            print_section("Step Planning")
            line_budget = int(os.getenv("TA_LINE_BUDGET", str(LINE_BUDGET)))
            print_key_value("line budget", str(line_budget))

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

            # ── Record per-step detail for sync report ──
            conflicts_in_step = len(self.state.conflict_files)
            self.state.step_details.append({
                "step_id": step_id,
                "step_index": self.state.current_step + 1,
                "commits": step["commit_count"],
                "end_commit": step["end_commit"][:12],
                "source_lines": step.get("source_changed_lines", 0),
                "conflict_files": conflicts_in_step,
                "build_fixes": self.state.build_fix_count,
                "test_fixes": self.state.test_fix_count,
                "retries": self.state.retry_count,
            })

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
        original_conflict_count = len(conflict_files)

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
                self.state.conflict_files_resolved += original_conflict_count
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
        last_build_failed = False  # track what failed for fix-type attribution

        for attempt in range(self.state.max_retries + 1):
            is_fix_attempt = attempt > 0
            self.state.retry_count = attempt
            fix_type = ""  # "build" or "test" — determined by previous failure

            # AI fix bug (skip on first round — build & test first)
            if is_fix_attempt:
                fix_type = "build" if last_build_failed else "test"
                print_header(f"Fix Attempt {attempt}/{self.state.max_retries} ({fix_type})")
                ai_ok = self._do_ai_fix(ascend_path, step_dir, attempt)
                # Collect fix detail
                modified_files: list[str] = []
                ai_summary = ""
                if hasattr(self, '_last_ai_result') and self._last_ai_result:
                    modified_files = self._last_ai_result.get("modified_files", [])
                    ai_summary = self._last_ai_result.get("step_summary", "")
                # Read error log snippet for context
                error_snippet = ""
                for err_path in self.state.fix_errors:
                    try:
                        content = Path(err_path).read_text(encoding="utf-8", errors="replace")
                        error_snippet += content[-2000:] if len(content) > 2000 else content
                    except Exception:
                        pass
                self.state.fix_attempts.append({
                    "step_id": current_step_id,
                    "attempt": attempt,
                    "fix_type": fix_type,
                    "error_logs": list(self.state.fix_errors),
                    "error_snippet": error_snippet[-1500:],
                    "modified_files": modified_files,
                    "ai_summary": (ai_summary or "")[:2000],
                    "ai_ok": ai_ok,
                })
                if not ai_ok:
                    pass

            # build triton-ascend
            if not self._do_build(ascend_path, clean=(attempt == 0)):
                if os.getenv("SKIP_AI_ANALYSIS", "false").lower() == "true":
                    return False
                self.state.fix_errors = [str(WORKSPACE_DIR / BUILD_RESULT_FILE)]
                self.state.build_fix_count += 1
                last_build_failed = True
                print_warn(f"Build failed (attempt {attempt + 1}/{self.state.max_retries + 1}) — "
                           f"skipping tests, will retry after AI fix")
                print_info(f"Build log: {WORKSPACE_DIR / BUILD_LOG_FILE}")
                continue

            last_build_failed = False

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
                self.state.test_fix_count += 1
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
            self._last_ai_result = None
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

            # Store result for caller to capture fix details
            self._last_ai_result = {
                "modified_files": ai_result.modified_files,
                "step_summary": ai_result.step_summary or "",
                "is_noop": ai_result.is_noop,
                "elapsed_seconds": ai_result.elapsed_seconds,
            }

            print_info("Running pre-CI check after fix...")
            run_pre_ci_check(ascend_path, step_id=f"fix-{attempt}")

            return bool(ai_result.modified_files)

        except Exception as e:
            print_error(f"AI fix call failed: {e}")
            self._last_ai_result = None
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

        # ── Generate sync report ──
        self._write_sync_report()

        print_section("Output Files")
        for f in sorted(WORKSPACE_DIR.rglob("*")):
            if f.is_file() and ".git" not in str(f):
                print(f"    {f.relative_to(WORKSPACE_DIR)}")
        print_info(f"Work branch preserved: {self.state.work_branch}")
        print_info(f"To inspect: cd {ascend_path} && git checkout {self.state.work_branch}")

    def _write_sync_report(self) -> None:
        """Generate SYNC_REPORT.md via AI — let Claude Code write the report.

        Collects all sync data (fix attempt details, step summaries, error logs,
        modified files) into a context file, then calls the AI backend to produce
        a comprehensive, human-readable sync report.
        """
        report_path = WORKSPACE_DIR / "SYNC_REPORT.md"

        # ── Collect context for AI ──
        context = self._build_report_context()
        context_path = WORKSPACE_DIR / "report-context.json"
        context_path.write_text(
            json.dumps(context, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        print_info(f"Report context written to {context_path}")

        # ── Build report prompt ──
        prompt = self._build_report_prompt(context)

        # ── Call AI backend to generate the report ──
        try:
            from TA_main2main_workflow.agent.opencode_adapter import (
                _detect_backend,
            )
            backend = _detect_backend()
            print_info(f"AI backend for report: {backend}")

            # Write prompt file for debugging
            prompt_path = WORKSPACE_DIR / "report-prompt.txt"
            prompt_path.write_text(prompt, encoding="utf-8")

            print_header("AI Report Generation")
            print_info("Calling AI backend to generate sync report...")

            ai_result = run_opencode_adapter({
                "step_id": "sync-report",
                "previous_step_id": "",
                "previous_step_summary_path": "",
                "is_last_step": "true",
                "step_index": "final",
                "step_dir": str(WORKSPACE_DIR),
                "fix_dir": str(WORKSPACE_DIR / "report-fix"),
                "conflict_dir": "",
                "ascend_path": self.state.triton_ascend_path,
                "triton_path": self.state.triton_path,
                "reference_dir": _REFERENCE_DIR,
                "mode": "report",
                "error_logs": json.dumps([str(context_path)], ensure_ascii=False),
                "target_commit": self.state.target_commit,
            })

            # AI writes report to step_dir/step_summary.md; we read it from there.
            # (ai_result return value is not used directly — report is file-based.)
            _ = ai_result  # suppress unused-var warning
            ai_report_path = WORKSPACE_DIR / EACH_STEP_SUMMARY_FILE
            if ai_report_path.exists():
                report_content = ai_report_path.read_text(encoding="utf-8")
                # Add metadata header
                header = (
                    f"# Triton-Ascend Upstream Sync Report\n\n"
                    f"- **Date**: {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
                    f"- **Target commit**: `{self.state.target_commit[:12]}`\n"
                    f"- **Work branch**: `{self.state.work_branch}`\n"
                    f"- **Upstream commits**: {self.state.upstream_commits_count}\n"
                    f"- **Steps**: {self.state.total_steps}\n"
                    f"- **Merge conflicts resolved**: {self.state.conflict_files_resolved}\n"
                    f"- **Build errors fixed**: {sum(s['build_fixes'] for s in self.state.step_details)}\n"
                    f"- **Test failures fixed**: {sum(s['test_fixes'] for s in self.state.step_details)}\n"
                    f"- **Total AI fix rounds**: {sum(s['retries'] for s in self.state.step_details)}\n\n"
                    f"---\n\n"
                )
                report_path.write_text(header + report_content, encoding="utf-8")
                print_status(True, f"AI-generated sync report: {report_path}")
            else:
                print_warn("AI did not produce a report — using fallback")
                self._write_sync_report_fallback()
        except Exception as e:
            print_error(f"AI report generation failed: {e}")
            print_info("Using fallback report generator...")
            self._write_sync_report_fallback()

    def _build_report_context(self) -> dict:
        """Collect all sync data into a structured context for AI report generation."""
        total_build_fixes = sum(s["build_fixes"] for s in self.state.step_details)
        total_test_fixes = sum(s["test_fixes"] for s in self.state.step_details)
        total_retries = sum(s["retries"] for s in self.state.step_details)

        # Collect step AI summaries
        step_summaries: dict[str, str] = {}
        steps_dir = WORKSPACE_DIR / STEPS_DIR
        if steps_dir.exists():
            for step in self.state.steps:
                step_dir = steps_dir / step["id"]
                parts = []
                for fname in ["analysis.md", "step_summary.md", "review.md"]:
                    fp = step_dir / fname
                    if fp.exists():
                        parts.append(
                            f"### {fname}\n\n"
                            f"{fp.read_text(encoding='utf-8', errors='replace').strip()}"
                        )
                if parts:
                    step_summaries[step["id"]] = "\n\n".join(parts)

        return {
            "overview": {
                "date": time.strftime('%Y-%m-%d %H:%M:%S'),
                "target_commit": self.state.target_commit[:12],
                "work_branch": self.state.work_branch,
                "upstream_commits_count": self.state.upstream_commits_count,
                "total_steps": self.state.total_steps,
                "conflict_files_resolved": self.state.conflict_files_resolved,
                "build_fix_count": total_build_fixes,
                "test_fix_count": total_test_fixes,
                "total_retries": total_retries,
            },
            "step_details": self.state.step_details,
            "fix_attempts": self.state.fix_attempts,
            "step_ai_summaries": step_summaries,
            "step_pr_descriptions": self.state.step_pr_descriptions,
        }

    def _build_report_prompt(self, context: dict) -> str:
        """Build the AI prompt for generating the sync report."""
        summary_json = json.dumps(context, indent=2, ensure_ascii=False)
        return (
            "Generate a comprehensive sync report in Chinese (中文) based on "
            "the structured context below. The report should be written as "
            "step_summary.md in the output directory.\n\n"
            "The report MUST include:\n\n"
            "## 1. Executive Summary\n"
            "- Brief overview of this sync (how many upstream commits, "
            "how many steps, overall outcome)\n"
            "- Key metrics (conflicts resolved, build errors fixed, "
            "test failures fixed, AI fix rounds)\n\n"
            "## 2. Per-Step Analysis\n"
            "- For each step, explain:\n"
            "  - Which upstream commits were merged and what areas they touched\n"
            "  - What merge conflicts arose and how they were resolved\n"
            "  - What build errors occurred, root causes, and how AI fixed them\n"
            "  - What test failures occurred, root causes, and how AI fixed them\n"
            "- Include specific file paths and error messages where relevant\n\n"
            "## 3. Fix Pattern Analysis\n"
            "- Identify recurring patterns across fixes (e.g., API changes, "
            "missing includes, signature mismatches)\n"
            "- Highlight any fixes that required multiple attempts\n\n"
            "## 4. Recommendations\n"
            "- Suggest preventative measures for future syncs\n"
            "- Flag any areas of the codebase that are particularly fragile\n\n"
            "Rules:\n"
            "- Write in Chinese (中文)\n"
            "- Be specific — include file paths, error messages, commit ranges\n"
            "- Write the output to {step_dir}/step_summary.md\n"
            "- DO NOT modify any source code — this is a report-only task\n\n"
            f"CONTEXT DATA:\n\n{summary_json}"
        )

    def _write_sync_report_fallback(self) -> None:
        """Fallback: assemble report from template (no AI)."""
        report_path = WORKSPACE_DIR / "SYNC_REPORT.md"
        L: list[str] = []

        total_build_fixes = sum(s["build_fixes"] for s in self.state.step_details)
        total_test_fixes = sum(s["test_fixes"] for s in self.state.step_details)
        total_retries = sum(s["retries"] for s in self.state.step_details)

        L.append("# Triton-Ascend Upstream Sync Report\n")
        L.append(f"**Date**: {time.strftime('%Y-%m-%d %H:%M:%S')}")
        L.append(f"**Target commit**: `{self.state.target_commit[:12]}`")
        L.append(f"**Work branch**: `{self.state.work_branch}`")
        L.append(f"**Status**: Success\n")

        L.append("## Summary\n")
        L.append("| Metric | Count |")
        L.append("|--------|-------|")
        L.append(f"| Upstream commits synced | {self.state.upstream_commits_count} |")
        L.append(f"| Steps | {self.state.total_steps} |")
        L.append(f"| Merge conflicts resolved | {self.state.conflict_files_resolved} |")
        L.append(f"| Build errors fixed | {total_build_fixes} |")
        L.append(f"| Test failures fixed | {total_test_fixes} |")
        L.append(f"| AI fix rounds | {total_retries} |")

        if self.state.step_details:
            L.append("\n## Per-Step Breakdown\n")
            L.append("| Step | Commits | Lines | Conflicts | Build Fixes | Test Fixes | Retries |")
            L.append("|------|---------|-------|-----------|-------------|------------|---------|")
            for s in self.state.step_details:
                L.append(
                    f"| {s['step_id']} ({s['step_index']}/{self.state.total_steps}) "
                    f"| {s['commits']} | {s['source_lines']} | {s['conflict_files']} "
                    f"| {s['build_fixes']} | {s['test_fixes']} | {s['retries']} |"
                )

        for fa in self.state.fix_attempts:
            ftype = fa["fix_type"].upper()
            L.append(
                f"\n### {fa['step_id']} — Fix {fa['attempt']} ({ftype})\n"
            )
            if fa["modified_files"]:
                L.append(f"**Files**: {', '.join(f'`{f}`' for f in fa['modified_files'])}")
            ai_sum = fa.get("ai_summary", "").strip()
            if ai_sum:
                L.append(f"\n{ai_sum}")

        steps_dir = WORKSPACE_DIR / STEPS_DIR
        if steps_dir.exists():
            for step in self.state.steps:
                step_dir = steps_dir / step["id"]
                for fname in ["analysis.md", "step_summary.md", "review.md"]:
                    fp = step_dir / fname
                    if fp.exists():
                        L.append(
                            f"\n### {step['id']} — {fname}\n\n"
                            f"{fp.read_text(encoding='utf-8', errors='replace').strip()}\n"
                        )

        L.append(f"\n---\n🤖 Generated at {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
        report_path.write_text("\n".join(L), encoding="utf-8")
        print_info(f"Fallback sync report: {report_path}")

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
