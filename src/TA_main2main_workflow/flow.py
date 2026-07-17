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
    commit_submodule, push_submodule, submodule_has_changes,
    IR_ANALYSIS_DIR, IR_OPS_REPORT_FILE,
    IR_CHANGES_REPORT_FILE, IR_DIAGNOSIS_FILE, IR_MAX_ITERATIONS,
    ENV_SINGLE_STEP_MODE, ENV_BASE_BRANCH, get_base_branch_ref, LLVM_CHANGE_ANALYSIS_DIR,
    print_header, print_section, print_step, print_status, print_info,
    print_warn, print_error, print_key_value,
    print_flow_progress, print_conflict_list, print_summary_table,
    print_ai_call_info, print_ai_result, print_elapsed_total,
    start_timer, stop_timer,
)

_REFERENCE_DIR = str(Path(__file__).parent / "reference")

# Baseline LLVM version that Ascend backend OP usage is built against.
# IR compatibility patches bridge from this version to the target LLVM.
_ASCEND_BASELINE_LLVM_HASH = "b5cc222d7429fe6f18c787f633d5262fac2e676f"


def _llvm_project_path() -> Path:
    """Return the resolved llvm-project path (expands ~ and $HOME)."""
    return Path(os.path.expanduser(
        os.getenv("LLVM_PROJECT_PATH", "~/llvm-project")))


def _llvm_install_prefix() -> Path:
    """Return the resolved LLVM install prefix (expands ~ and $HOME)."""
    return Path(os.path.expanduser(
        os.getenv("LLVM_INSTALL_PREFIX_SYNC", "~/llvm-install-sync")))


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
    max_retries: int = 10
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

    # ── IR Patch Loop State ──
    ir_analysis_done: bool = False
    ir_ops_report: dict = {}
    ir_changes_report: dict = {}
    ir_patches: list = []
    ir_patch_iteration: int = 0
    ir_max_iterations: int = 3
    ir_issues_found: int = 0
    ir_fix_count: int = 0
    llvm_hash_changed: bool = False

    # ── Pytest State ──
    pytest_passed: bool = False
    test_failures_by_python: dict = {}
    ir_loop_details: list = []

    summary_rows: list = []


class TA_Main2MainFlow(Flow[TA_Main2MainState]):

    def __init__(self, **kwargs):
        super().__init__(**kwargs)

    # ═══════════════════════════════════════════════════════════════════════════
    # Workspace info helper — prints paths, branches, and git status
    # ═══════════════════════════════════════════════════════════════════════════

    def _print_workspace_info(self, label: str = "") -> None:
        """Print all relevant repo paths, current branches, and git status.

        Called at key workflow steps to provide full visibility into the
        workspace state — which repos are in play, what branches they're on,
        and whether there are uncommitted changes.
        """
        header = f"Workspace Info{f' — {label}' if label else ''}"
        print_section(header)

        # ── Resolve paths ──
        llvm_proj = _llvm_project_path()
        llvm_install = _llvm_install_prefix()
        ascend_str = self.state.triton_ascend_path
        triton_str = self.state.triton_path

        # ── Print all relevant paths ──
        print_key_value("LLVM_PROJECT_PATH", str(llvm_proj))
        print_key_value("LLVM_INSTALL_PREFIX_SYNC", str(llvm_install))
        if self.state.llvm_prefix:
            print_key_value("LLVM_INSTALL_PREFIX", self.state.llvm_prefix)
        if ascend_str:
            print_key_value("TRITON_ASCEND_PATH", ascend_str)
        if triton_str:
            print_key_value("TRITON_PATH", triton_str)

        # ── Print git branch + status for each repo ──
        repos: list[tuple[str, Path]] = []
        if ascend_str:
            ap = Path(ascend_str)
            if ap.exists():
                repos.append(("triton-ascend", ap))
        if triton_str:
            tp = Path(triton_str)
            if tp.exists():
                # Skip triton if it's the same directory as triton-ascend
                if not ascend_str or tp != Path(ascend_str):
                    repos.append(("triton", tp))
        if llvm_proj.exists():
            repos.append(("llvm-project", llvm_proj))

        for repo_label, repo_path in repos:
            try:
                branch = run_git(repo_path, "branch", "--show-current").strip()
                print_key_value(f"{repo_label} branch", branch)
                status = run_git(repo_path, "status", "--porcelain").strip()
                if status:
                    lines = status.splitlines()
                    print_info(
                        f"{repo_label} uncommitted changes ({len(lines)} files):"
                    )
                    for line in lines[:10]:
                        print(f"      {line}")
                    if len(lines) > 10:
                        print(f"      ... and {len(lines) - 10} more")
                else:
                    print_info(f"{repo_label} status: clean")
            except Exception as e:
                print_warn(f"Could not get git info for {repo_label}: {e}")

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
        if os.getenv(ENV_SINGLE_STEP_MODE, "false").lower() == "true":
            return self._run_single_step_mode(inputs)
        elif mode == "merge":
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

        self._print_workspace_info(f"Merge Mode — step {current_step}")

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
                self.state.final_status = UpgradeCompleted
                return UpgradeCompleted

            # Store the plan for subsequent steps
            self._write_step_plan()
        else:
            # ── Resume: checkout existing work branch ──
            work_branch = os.getenv("TA_WORK_BRANCH", self.state.work_branch)
            if not work_branch:
                print_error("TA_WORK_BRANCH is required for TA_CURRENT_STEP > 0")
                self.state.final_status = UpgradeFailed
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
                self.state.triton_path = os.path.expanduser(
                os.getenv("TRITON_PATH", self.state.triton_ascend_path))
                ascend_path = Path(self.state.triton_ascend_path)
                # Fetch and checkout work branch
                try:
                    run_git(ascend_path, "fetch", "origin", work_branch)
                except Exception:
                    pass
                run_git(ascend_path, "checkout", work_branch)
                result = self.detect_commits()
                if result == HasNoNewCommits:
                    self.state.final_status = UpgradeCompleted
                    return UpgradeCompleted
                self._write_step_plan()
            else:
                import json
                plan_data = json.loads(plan_file.read_text(encoding="utf-8"))
                self.state.total_steps = plan_data["total_steps"]
                self.state.steps = plan_data["steps"]
                self.state.upstream_commits_count = plan_data.get("upstream_commits_count", 0)

                # Minimal init for resume
                self.state.triton_path = os.path.expanduser(
                os.getenv("TRITON_PATH", str(ascend_path)))
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
            self.state.final_status = UpgradeCompleted
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

        self.state.final_status = UpgradeCompleted
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

        # ── Push AscendNPU-IR submodule first ──
        self._push_submodule_if_needed()

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

        1. Login gh CLI explicitly against github.com (needed when git
           remotes point to a proxy host that gh doesn't recognize).
        2. Run 'gh auth setup-git' to configure the git credential helper.
        3. Rewrite the origin URL to embed the token so git push works
           even through url.insteadOf proxy rewriting.
        """
        gh_token = os.getenv("GH_TOKEN", "")
        if gh_token:
            print_info("GH_TOKEN set — configuring git credential helper")

            # Explicit gh login against github.com — essential when the
            # git remote points to a proxy host (gh needs to know about
            # github.com independently of git remotes).
            result = subprocess.run(
                ["gh", "auth", "login", "--with-token", "--hostname", "github.com"],
                input=gh_token + "\n", text=True, capture_output=True,
            )
            if result.returncode == 0:
                print_info("gh auth login --with-token: success")
            else:
                print_warn(f"gh auth login stderr: {result.stderr.strip()}")

            result = subprocess.run(
                ["gh", "auth", "setup-git", "--hostname", "github.com"],
                capture_output=True, text=True,
            )
            if result.returncode == 0:
                print_info("gh auth setup-git: success")
            else:
                print_warn(f"gh auth setup-git skipped "
                           f"(exit {result.returncode}): {result.stderr.strip()}")
            # Rewrite origin URL to embed token (for git push through proxy)
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

    def _push_submodule_if_needed(self) -> None:
        """Push AscendNPU-IR submodule changes to its remote.

        Uses the same branch name as the parent repo's work branch so the
        two repos stay in sync. Pushes with --force-with-lease to avoid
        clobbering existing remote state.

        Failure is non-fatal — the parent repo push proceeds regardless.
        Records the result in summary_rows for visibility.
        """
        ascend_path = Path(self.state.triton_ascend_path)
        submodule_push_state = push_submodule(ascend_path, self.state.work_branch)
        if submodule_push_state:
            self.state.summary_rows.append(
                ("Push AscendNPU-IR", "PASS", self.state.work_branch)
            )
        else:
            self.state.summary_rows.append(
                ("Push AscendNPU-IR", "WARN", "Push failed — commit may only exist locally")
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
    # Mode: single-step — per-step merge → IR → build → test → fix
    # ═══════════════════════════════════════════════════════════════════════════

    def _run_single_step_mode(self, inputs: dict | None) -> str:
        """Single-step mode: each planned step runs the full pipeline independently.

        For each step:
          1. Merge upstream commits + resolve conflicts
          2. If LLVM hash changed: IR analysis → patch → rebuild LLVM
          3. Build Triton-Ascend + AI fix compile errors
          4. Run tests + AI fix test failures
          5. Commit step progress

        After all steps: finalize + push + create PR.

        Controlled by TA_SINGLE_STEP_MODE=true env var.
        """
        # ── Apply inputs to state ──
        if inputs:
            for key, value in inputs.items():
                if hasattr(self.state, key):
                    setattr(self.state, key, value)

        ascend_path = Path(self.state.triton_ascend_path)

        # ── Phase 0: Initialize ──
        self.initialize()

        # ── Phase 1: Detect commits & plan steps ──
        detect_result = self.detect_commits()
        if detect_result == HasNoNewCommits:
            print_info("No new commits — nothing to merge")
            self.state.summary_rows.append(
                ("Detect", "SKIP", "No new upstream commits"))
            self.state.final_status = UpgradeCompleted
            return UpgradeCompleted

        print_header("Single-Step Mode — Per-Step Full Pipeline")
        print_key_value("Total steps", str(self.state.total_steps))
        print_info("Each step: merge → [IR patch] → build → fix → test → fix → commit")

        # ── Phase 1.5: Build baseline LLVM (pre-merge, with Ascend patch) ──
        if not self._build_baseline_llvm():
            print_error("Baseline LLVM build failed — cannot proceed")
            self.state.final_status = UpgradeFailed
            return UpgradeFailed

        # ── Phase 2: Per-step loop ──
        while self.state.current_step < self.state.total_steps:
            step = self.state.steps[self.state.current_step]
            step_id = step["id"]
            self.state.retry_count = 0

            print_header(
                f"Single-Step {self.state.current_step + 1}/{self.state.total_steps}: {step_id}"
            )
            print_key_value("commits in step", str(step["commit_count"]))
            print_key_value("end commit", step["end_commit"][:12])
            reason = step.get("reason", "line_budget")
            print_key_value("step reason", reason)

            self._print_workspace_info(f"Single-Step Mode — {step_id}")

            # Record ascend HEAD before this step
            self.state.step_start_ascend_head = run_git(
                ascend_path, "rev-parse", "HEAD"
            ).strip()

            # ── Step A: git merge ──
            merge_result = self._do_step_merge(step)
            if merge_result == UpgradeFailed:
                self._backup_code_state(f"failed-merge-{step_id}")
                self.state.final_status = UpgradeFailed
                return UpgradeFailed

            # ── Step B: AI resolve conflicts ──
            if self.state.merge_has_conflicts:
                if not self._do_resolve_conflicts():
                    self._backup_code_state(f"failed-conflict-{step_id}")
                    self.state.final_status = UpgradeFailed
                    return UpgradeFailed

            # ── Step C: IR patch if LLVM hash changed in this step ──
            if reason == "llvm_version":
                print_section(f"LLVM Version Change in {step_id} — IR Patch Pipeline")
                if not self._do_per_step_ir_patch(step):
                    self.state.final_status = UpgradeFailed
                    return UpgradeFailed

            # ── Step D: Build + AI fix compile errors ──
            print_section(f"Build & Fix — {step_id}")
            if not self._do_build_and_fix_loop():
                self._backup_code_state(f"failed-build-{step_id}")
                self.state.final_status = UpgradeFailed
                return UpgradeFailed

            # ── Step E: Test + AI fix test failures ──
            if not self._do_test_and_fix_loop():
                self._backup_code_state(f"failed-test-{step_id}")
                self.state.final_status = UpgradeFailed
                return UpgradeFailed

            # ── Step F: Commit step progress ──
            self._do_commit_step(step)

            # Record step description for PR body
            desc = (
                f"✅ **{step_id}**: {step['commit_count']} commits, "
                f"end_commit=`{step['end_commit'][:12]}`, "
                f"source lines={step.get('source_changed_lines', '?')}, "
                f"reason={reason}"
            )
            self.state.step_pr_descriptions.append(desc)

            # Record per-step detail for sync report
            self.state.step_details.append({
                "step_id": step_id,
                "step_index": self.state.current_step + 1,
                "commits": step["commit_count"],
                "end_commit": step["end_commit"][:12],
                "source_lines": step.get("source_changed_lines", 0),
                "conflict_files": len(self.state.conflict_files),
                "build_fixes": self.state.build_fix_count,
                "test_fixes": self.state.test_fix_count,
                "retries": self.state.retry_count,
                "reason": reason,
            })

            self.state.current_step += 1
            print_status(True, f"Step {step_id} completed "
                         f"({self.state.current_step}/{self.state.total_steps})")

        # ── Phase 3: Finalize ──
        print_header("Finalize — Generate Summary & Push")
        self._do_finalize()

        # ── Phase 4: Push to GitHub + create PR ──
        self.push_to_github()

        self.state.summary_rows.append(
            ("Single-Step Sync", "PASS",
             f"{self.state.total_steps} step(s), branch: {self.state.work_branch}")
        )
        print_summary_table(self.state.summary_rows)
        print_elapsed_total()

        self.state.final_status = UpgradeCompleted
        return UpgradeCompleted

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
            self.state.final_status = UpgradeFailed
            return UpgradeFailed

        ascend_path = Path(ascend_path_str)

        # ── Setup ──
        print_header(f"Fix Mode — Attempt {attempt}")
        print_key_value("work branch", work_branch)
        print_key_value("error logs", error_logs_path or "<none>")
        print_key_value("target commit", target_commit[:12] if target_commit else "<none>")
        print_key_value("repo path", str(ascend_path))

        self._print_workspace_info(f"Fix Mode — Attempt {attempt}")

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
            self.state.final_status = UpgradeFailed
            return UpgradeFailed

        # ── Commit and push ──
        print_section("Commit & Push Fixes")

        # ── Commit submodule changes first ──
        self.state.retry_count = attempt - 1
        self._commit_submodule_if_needed()

        # ── Clean temp artifacts BEFORE staging ──
        cleanup_temp_files(ascend_path)

        status = run_git(ascend_path, "status", "--porcelain").strip()
        if status:
            run_git(ascend_path, "add", "-A")
            staged = run_git(ascend_path, "diff", "--cached", "--name-only").strip()
            if staged:
                print_info(f"Files staged ({len(staged.splitlines())}):")
                for f in staged.splitlines()[:10]:
                    print_info(f"  - {f}")
            commit_target = target_commit[:12] if target_commit else "upstream"
            commit_msg = (
                f"fix: AI-generated fix for build/test failures\n\n"
                f"Upstream target: {commit_target}\n"
                f"Fix attempt: {attempt}\n"
                f"Work branch: {work_branch}\n"
            )
            run_git(ascend_path, "commit", "-s", "-m", commit_msg)
            print_status(True, "Committed AI fix")

            # ── Push AscendNPU-IR submodule first ──
            self._push_submodule_if_needed()

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
        self.state.final_status = UpgradeCompleted
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
        self.state.triton_path = os.path.expanduser(raw_triton)
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

        # ── Use the configured base branch for patch diffs ──
        # The work branch is created from the base branch, so all diffs should
        # be computed against it, not the checkout HEAD.
        base_branch = os.getenv(ENV_BASE_BRANCH, "main")
        base_ref = get_base_branch_ref()
        try:
            run_git(ascend_path, "fetch", "origin", base_branch)
        except Exception:
            print_warn(f"Could not fetch {base_ref}, using checkout HEAD as base")
        try:
            self.state.ascend_head = run_git(
                ascend_path, "rev-parse", base_ref).strip()
        except Exception:
            self.state.ascend_head = run_git(ascend_path, "rev-parse", "HEAD").strip()

        print_section("Repository Configuration")
        print_key_value("triton-ascend", self.state.triton_ascend_path)
        print_key_value("upstream triton", self.state.triton_path)
        print_key_value("target commit", self.state.target_commit or "<upstream HEAD>")
        print_key_value("original branch", self.state.original_branch)
        print_key_value(f"base ({base_ref})", self.state.ascend_head[:12])

        self._print_workspace_info("Phase 0: Initialize")

        self.state.summary_rows = []

    # ═══════════════════════════════════════════════════════════════════════════
    # Phase 1: Detect upstream commits
    # ═══════════════════════════════════════════════════════════════════════════

    @router(initialize)
    def detect_commits(self) -> Literal["HasNewCommits", "HasNoNewCommits"]:
        start_timer("detect")
        print_header("Phase 1: Detect Upstream Commits & Plan Steps")

        self._print_workspace_info("Phase 1: Detect Commits")

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
        try:
            return self._execute_sync_inner()
        except Exception as exc:
            print_error(f"Unexpected error in execute_sync: {exc}")
            import traceback
            traceback.print_exc()
            # Backup code before failing so partial work is preserved
            self._backup_code_state(f"crash-step{self.state.current_step + 1}")
            self.state.final_status = UpgradeFailed
            return UpgradeFailed

    def _execute_sync_inner(self) -> Literal["UpgradeCompleted", "UpgradeFailed"]:
        """Inner body of execute_sync — wrapped by try/except for crash backup."""

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

            self._print_workspace_info(f"Phase 2: Execute Sync — {step_id}")

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

        # ── Phase 3+4: IR compatibility patches + pytest ut test ──
        ir_ok = self._do_ir_patch_loop()
        if not ir_ok:
            print_error("IR patch loop did not converge — sync failed")
            self.state.final_status = UpgradeFailed
            return UpgradeFailed

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
        # Clean temp artifacts first, then use git add -A to ensure
        # AI-created files are NOT dropped.
        cleanup_temp_files(ascend_path)
        try:
            run_git(ascend_path, "add", "-A")
            staged = run_git(ascend_path, "diff", "--cached", "--name-only").strip()
            if staged:
                print_info(f"Files staged ({len(staged.splitlines())}):")
                for f in staged.splitlines()[:10]:
                    print_info(f"  - {f}")
            run_git(ascend_path, "commit", "--no-edit", "-s")
            print_status(True, "Committed conflict resolution")
        except subprocess.CalledProcessError as e:
            stderr = (e.stderr or "").strip() if hasattr(e, 'stderr') else str(e)
            if "nothing to commit" in stderr.lower():
                print_info("Nothing to commit — resolution may already be committed")
            else:
                print_warn(f"Commit may have failed: {stderr[-200:]}")

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
        """build → AI fix compile-error loop (up to max_retries rounds).

        Only handles compilation errors. Tests are deferred to after all
        upstream commits are merged and the final build passes.
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

        build_passed = False

        for attempt in range(self.state.max_retries + 1):
            is_fix_attempt = attempt > 0
            self.state.retry_count = attempt

            # AI fix compile errors (skip on first round)
            if is_fix_attempt:
                print_header(f"Fix Attempt {attempt}/{self.state.max_retries} (build)")
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
                    "fix_type": "build",
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
                print_warn(f"Build failed (attempt {attempt + 1}/{self.state.max_retries + 1}) — "
                           f"will retry after AI fix")
                print_info(f"Build log: {WORKSPACE_DIR / BUILD_LOG_FILE}")
                continue

            # Build passed — tests are deferred to after all merges complete
            build_passed = True
            break

        if not build_passed:
            print_error(f"All {self.state.max_retries} fix attempts exhausted — build still failing")
            self.state.summary_rows.append(
                ("AI fix", "FAIL", f"Failed after {self.state.max_retries} attempts")
            )
            return False

        # Commit build fixes
        self._commit_fixes(ascend_path, step_dir)

        return True

    def _commit_submodule_if_needed(self) -> None:
        """Commit uncommitted changes inside the AscendNPU-IR submodule.

        Must be called BEFORE parent 'git add -A' so that the submodule
        pointer update is picked up by the parent commit.
        """
        ascend_path = Path(self.state.triton_ascend_path)
        if not submodule_has_changes(ascend_path):
            return

        target_short = self.state.target_commit[:12]
        commit_msg = (
            f"fix: AI-generated fix for build/test failures\n\n"
            f"Upstream target: {target_short}\n"
            f"Fix attempt: {self.state.retry_count}\n"
            f"Work branch: {self.state.work_branch}\n"
        )
        commit_submodule(ascend_path, commit_msg)

    def _commit_fixes(self, ascend_path: Path, step_dir: Path) -> None:
        """Commit AI bug fixes with a meaningful message.

        Only commits if there are uncommitted changes. Commits submodule
        changes first (AscendNPU-IR), then returns to triton-ascend for the
        parent commit. Uses git add -A so AI-created files are not dropped.

        Commit message priority:
          1. AI-written commit_message.txt (one-line subject)
          2. First line of step_summary.md
          3. Default generic message
        """
        # ── Commit submodule changes first (inside AscendNPU-IR) ──
        self._commit_submodule_if_needed()

        status = run_git(ascend_path, "status", "--porcelain").strip()
        if not status:
            print_info("No uncommitted fix changes — nothing to commit")
            return

        print_section("Commit Bug Fixes")

        # ── Clean temp artifacts BEFORE staging ──
        # git add -A would otherwise pick up build outputs, cache dirs, etc.
        cleanup_temp_files(ascend_path)

        # ── Read AI-written commit message ──
        commit_msg_path = step_dir / "commit_message.txt"
        if commit_msg_path.exists():
            commit_summary = commit_msg_path.read_text(encoding="utf-8").strip()
            # Take first line only for the subject
            commit_summary = commit_summary.split("\n")[0].strip()[:72]
            print_info(f"Using AI-written commit message: {commit_summary}")
        else:
            # Fallback: first line of step_summary.md
            summary_path = step_dir / EACH_STEP_SUMMARY_FILE
            if summary_path.exists():
                summary_text = summary_path.read_text(encoding="utf-8").strip()
                commit_summary = summary_text.split("\n")[0].lstrip("#").strip()[:72]
            else:
                commit_summary = "fix: resolve build/test failures for upstream sync"

        target_short = self.state.target_commit[:12]
        commit_msg = (
            f"fix: {commit_summary}\n\n"
            f"Upstream target: {target_short}\n"
            f"Fix attempt: {self.state.retry_count}\n"
            f"Work branch: {self.state.work_branch}\n"
            f"Co-Authored-By: Claude <noreply@anthropic.com>\n"
        )

        # ── Stage and commit (already changed to -A above via replace_all) ──
        try:
            staged_before = run_git(ascend_path, "diff", "--cached", "--name-only").strip()
            if not staged_before:
                # git add -A was already called; if no files staged yet, stage now
                run_git(ascend_path, "add", "-A")
            staged = run_git(ascend_path, "diff", "--cached", "--name-only").strip()
            if staged:
                print_info(f"Files staged for commit ({len(staged.splitlines())}):")
                for f in staged.splitlines()[:15]:
                    print_info(f"  - {f}")
                if len(staged.splitlines()) > 15:
                    print_info(f"  ... and {len(staged.splitlines()) - 15} more")
            run_git(ascend_path, "commit", "-s", "-m", commit_msg)
            print_status(True, f"Committed fix: {commit_summary[:60]}")
            self.state.summary_rows.append(("Commit fixes", "PASS", commit_summary[:40]))
        except subprocess.CalledProcessError as e:
            stderr = (e.stderr or "").strip() if hasattr(e, 'stderr') else str(e)
            if "nothing to commit" in stderr.lower():
                print_info("Nothing to commit (AI made no changes)")
                self.state.summary_rows.append(("Commit fixes", "PASS", "No changes"))
            else:
                print_warn(f"Could not commit fixes: {stderr[-200:]}")
                self.state.summary_rows.append(("Commit fixes", "WARN", stderr[:40]))

    def _do_build(self, ascend_path: Path, clean: bool = False,
                  python_exe: str = "python3") -> bool:
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
            python_exe=python_exe,
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

    def _do_test(self, ascend_path: Path, python_exe: str = "") -> bool | None:
        start_timer("test")
        print_section("Run Tests")

        if os.getenv("SKIP_E2E_TEST", "false").lower() == "true":
            print_info("SKIP_E2E_TEST=true — treating tests as passed")
            self.state.test_passed = True
            stop_timer("test")
            self.state.summary_rows.append(("Tests", "SKIP", "SKIP_E2E_TEST set"))
            return None

        test_dir_path = ascend_path / self.state.test_dir
        py_label = python_exe or os.getenv("PYTHON", "python3.10")
        print_info(f"Test directory: {test_dir_path}")
        print_info(f"Python: {py_label}, procs: {self.state.num_procs}")

        try:
            test_result = run_tests(
                ascend_path,
                test_dir=self.state.test_dir,
                num_procs=self.state.num_procs,
                conda_env=self.state.conda_env,
                python_exe=python_exe,
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

        Commits AscendNPU-IR submodule changes first (if any), so the parent
        repo records the updated submodule pointer.
        """
        ascend_path = Path(self.state.triton_ascend_path)
        step_id = step["id"]

        # ── Commit submodule changes first ──
        self._commit_submodule_if_needed()

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
            run_git(ascend_path, "add", "-A")
            staged = run_git(ascend_path, "diff", "--cached", "--name-only").strip()
            if staged:
                print_info(f"Files staged ({len(staged.splitlines())}):")
                for f in staged.splitlines()[:10]:
                    print_info(f"  - {f}")
                if len(staged.splitlines()) > 10:
                    print_info(f"  ... and {len(staged.splitlines()) - 10} more")
            run_git(ascend_path, "commit", "-s", "-m", commit_msg)
            print_status(True, f"Committed step {step_id}")
            self.state.summary_rows.append(
                (f"Commit {step_id}", "PASS", f"{step['commit_count']} commits")
            )
        except subprocess.CalledProcessError as e:
            stderr = (e.stderr or "").strip() if hasattr(e, 'stderr') else str(e)
            if "nothing to commit" in stderr.lower():
                print_info(f"[{step_id}] Nothing to commit (clean merge)")
                self.state.summary_rows.append(
                    (f"Commit {step_id}", "PASS", "No changes (clean merge)"))
            else:
                print_warn(f"Could not commit step {step_id}: {stderr[-200:]}")
                self.state.summary_rows.append(
                    (f"Commit {step_id}", "WARN", stderr[:40]))

    # ═══════════════════════════════════════════════════════════════════════════
    # Phase 3+4: IR Compatibility Patch Loop
    # ═══════════════════════════════════════════════════════════════════════════

    def _llvm_hash_did_change(self) -> bool:
        """Check if cmake/llvm-hash.txt differs from the Ascend baseline LLVM.

        The Ascend backend OP usage is based on a fixed baseline LLVM version.
        If the target LLVM hash differs from the baseline, IR compatibility
        patches need to be generated.
        """
        ascend_path = Path(self.state.triton_ascend_path)
        try:
            current_hash = (ascend_path / "cmake" / "llvm-hash.txt") \
                .read_text(encoding="utf-8").strip()
        except Exception:
            return False

        old_hash = _ASCEND_BASELINE_LLVM_HASH
        changed = old_hash != current_hash
        if changed:
            print_info(f"LLVM hash changed from baseline: "
                       f"{old_hash[:12]} → {current_hash[:12]}")
        else:
            print_info("LLVM hash matches baseline — skipping IR patch phase")
        return changed

    def _do_ir_patch_loop(self) -> bool:
        """Phase 3+4 outer loop: IR analysis → patch → rebuild → test → fix.

        Runs AFTER all progressive merge steps have completed. If
        cmake/llvm-hash.txt didn't change, skips IR patches and goes
        directly to pytest.

        Outer loop (max IR_MAX_ITERATIONS rounds):
          [3.1-3.3] AI: analyze OPs, analyze changes, generate patches
          [3.4-3.5] Apply patches to LLVM + rebuild
          [4.1-4.2] Build TA + run pytest
          [4.3]     If failures, AI classifies (IR vs code)
                    → IR issues: loop back to modify patches
                    → Code issues: AI fix inner loop
        Returns True if all tests pass, False on exhaustion.
        """
        ascend_path = Path(self.state.triton_ascend_path)

        # ── Skip IR patch phase via env var  ──
        if os.getenv("SKIP_IR_PATCH", "false").lower() == "true":
            print_header("Phase 3+4: IR Patch + Pytest — SKIPPED (SKIP_IR_PATCH=true)")
            self.state.summary_rows.append(
                ("IR Patch", "SKIP", "SKIP_IR_PATCH set"))
            return True

        # ── Skip if LLVM hash unchanged ──
        self.state.llvm_hash_changed = self._llvm_hash_did_change()
        if not self.state.llvm_hash_changed:
            print_header("Phase 4: Pytest (LLVM unchanged)")
            print_info("LLVM hash unchanged — skipping IR analysis and patch generation")
            return self._do_pytest()

        print_header("Phase 3: IR Compatibility Patch Auto-Generation")
        print_info(f"LLVM hash changed — IR compatibility analysis required")
        print_key_value("Baseline LLVM", _ASCEND_BASELINE_LLVM_HASH[:12])

        self._print_workspace_info("Phase 3: IR Patch Loop")
        print_key_value("Max IR iterations", str(self.state.ir_max_iterations))

        for iteration in range(self.state.ir_max_iterations):
            self.state.ir_patch_iteration = iteration
            print_header(
                f"IR Patch Loop — Iteration {iteration + 1}/"
                f"{self.state.ir_max_iterations}"
            )

            # ── [3.1 + 3.2] Analysis (only on first iteration) ──
            if iteration == 0:
                print_info("First iteration — running full OP analysis pipeline")
                if not self._do_ir_op_analysis():
                    return False
                if not self._do_ir_change_analysis():
                    return False
            else:
                # On retry, re-analyze changes (patches from previous
                # iteration may have altered the picture)
                print_info("Re-analyzing OP changes after patch retry...")
                if not self._do_ir_change_analysis():
                    return False

            # ── [3.3] Generate patches ──
            print_info("Step 3.3: Invoking AI to generate IR compatibility patches...")
            if not self._do_ir_generate_patches():
                return False

            # ── [3.4 + 3.5] Apply patches + rebuild LLVM ──
            print_info("Step 3.4-3.5: Applying patches and rebuilding LLVM (this may take a while)...")
            if not self._do_ir_apply_patches_and_rebuild():
                return False

            # ── [4.1] Build TA ──
            print_info("Step 4.1: Building Triton-Ascend with patched LLVM...")
            if not self._do_build(ascend_path, clean=True):
                if os.getenv("SKIP_AI_ANALYSIS", "false").lower() == "true":
                    return False
                self.state.fix_errors = [str(WORKSPACE_DIR / BUILD_RESULT_FILE)]
                self.state.build_fix_count += 1
                print_warn("Build failed after IR patches — will attempt AI fix")
                self._do_ai_fix(ascend_path, WORKSPACE_DIR, 1)
                continue

            # ── [4.2] Pytest ──
            print_info("Step 4.2: Running pytest suite...")
            if self._do_pytest():
                print_status(True, "All tests pass!")
                self._commit_fixes(ascend_path, WORKSPACE_DIR)
                self.state.ir_loop_details.append({
                    "iteration": iteration + 1,
                    "result": "ALL_PASS",
                })
                return True

            # ── [4.3] Diagnose failures ──
            print_info("Step 4.3: Invoking AI to classify test failures (IR vs code)...")
            has_ir_issues = self._do_ir_diagnose_failures()
            if has_ir_issues:
                self.state.ir_issues_found += 1
                print_warn(
                    f"IR compatibility issues found in iteration "
                    f"{iteration + 1} — retrying with modified patches"
                )
                self.state.ir_loop_details.append({
                    "iteration": iteration + 1,
                    "result": "IR_RETRY",
                    "ir_issues": self.state.ir_issues_found,
                })
                continue

            # ── [4.4] Non-IR issues → AI fix inner loop ──
            print_info("Non-IR failures detected — entering AI fix loop")
            print_key_value("Max fix attempts", str(self.state.max_retries))
            for fix_attempt in range(1, self.state.max_retries + 1):
                print_header(f"AI Fix Attempt {fix_attempt}/{self.state.max_retries}")
                self.state.retry_count = fix_attempt
                self._do_ai_fix(ascend_path, WORKSPACE_DIR, fix_attempt)
                if not self._do_build(ascend_path, clean=False):
                    self.state.fix_errors = [str(WORKSPACE_DIR / BUILD_RESULT_FILE)]
                    continue
                if self._do_pytest():
                    self._commit_fixes(ascend_path, WORKSPACE_DIR)
                    self.state.ir_loop_details.append({
                        "iteration": iteration + 1,
                        "result": "PASS_AFTER_FIX",
                        "fix_attempts": fix_attempt,
                    })
                    return True

            print_error(f"All {self.state.max_retries} fix attempts exhausted "
                        f"in iteration {iteration + 1}")
            self.state.ir_loop_details.append({
                "iteration": iteration + 1,
                "result": "FIX_EXHAUSTED",
            })

        print_error(f"IR patch loop exhausted {self.state.ir_max_iterations} "
                    f"iterations")
        return False

    def _do_ir_op_analysis(self) -> bool:
        """[3.1] AI analyzes which MLIR OPs the Ascend backend uses."""
        print_header("Phase 3.1: IR OP Analysis")
        ascend_path = Path(self.state.triton_ascend_path)

        self._print_workspace_info("Phase 3.1: IR OP Analysis")

        ir_dir = WORKSPACE_DIR / IR_ANALYSIS_DIR
        ir_dir.mkdir(parents=True, exist_ok=True)

        from TA_main2main_workflow.agent.opencode_adapter import _detect_backend
        backend = _detect_backend()
        print_info(f"AI backend: {backend}")
        print_key_value("Triton-Ascend", str(ascend_path))
        print_key_value("Output dir", str(ir_dir))

        # ── Pre-scan: find candidate files with MLIR OP usage ──
        print_info("Pre-scanning Ascend backend for MLIR OP patterns...")
        candidate_files: list[str] = []
        scan_dirs = [
            ascend_path / "third_party" / "ascend" / "lib",
            ascend_path / "lib" / "Target" / "Ascend",
        ]
        op_patterns = [
            r'::create\b', r'::get\b', r'\.match\b', r'\.walk\b',
            r'isa<', r'cast<', r'dyn_cast<',
        ]
        for sd in scan_dirs:
            if not sd.exists():
                print_warn(f"Scan dir not found: {sd}")
                continue
            for pattern in op_patterns:
                try:
                    result = subprocess.run(
                        ["grep", "-rl", pattern, str(sd)],
                        capture_output=True, text=True, timeout=30,
                    )
                    for f in result.stdout.splitlines():
                        if f not in candidate_files:
                            candidate_files.append(f)
                except (subprocess.TimeoutExpired, Exception):
                    pass

        candidate_files.sort()
        print_info(f"Found {len(candidate_files)} candidate files with MLIR OP patterns")
        for f in candidate_files[:15]:
            print_info(f"  - {Path(f).relative_to(ascend_path)}")
        if len(candidate_files) > 15:
            print_info(f"  ... and {len(candidate_files) - 15} more files")

        # Write candidate file list for AI reference
        hint_path = ir_dir / "candidate_files.txt"
        hint_path.write_text("\n".join(candidate_files), encoding="utf-8")
        print_info(f"Candidate file list written to {hint_path}")

        print_info("AI will scan candidate files for MLIR OP usage and output structured JSON")
        print_info("Invoking AI for IR OP analysis (this may take several minutes)...")

        try:
            ai_result = run_opencode_adapter({
                "step_id": "ir-analyze-ops",
                "previous_step_id": "",
                "previous_step_summary_path": "",
                "is_last_step": "true",
                "step_index": "ir",
                "step_dir": str(ir_dir),
                "fix_dir": str(ir_dir),
                "conflict_dir": "",
                "ascend_path": str(ascend_path),
                "triton_path": self.state.triton_path,
                "reference_dir": _REFERENCE_DIR,
                "mode": "ir_analyze_ops",
                "error_logs": "[]",
                "target_commit": self.state.target_commit,
                "llvm_project_path": str(_llvm_project_path()),
            })
            _ = ai_result
        except Exception as e:
            print_error(f"IR OP analysis failed: {e}")
            self.state.summary_rows.append(("IR OP Analysis", "FAIL", str(e)[:60]))
            return False

        ops_report = ir_dir / IR_OPS_REPORT_FILE
        if ops_report.exists():
            try:
                data = json.loads(ops_report.read_text(encoding="utf-8"))
                # ── Content validation: must have 'ops' array with real OP data ──
                ops_list = data.get("ops", [])
                if not ops_list or not isinstance(ops_list, list):
                    print_error(
                        f"AI output is NOT a valid OP report! "
                        f"Missing or empty 'ops' array. "
                        f"Top-level keys: {list(data.keys())}")
                    print_warn(
                        f"AI may have produced a merge analysis instead of IR OP scan. "
                        f"Check {ops_report} for content.")
                    self.state.summary_rows.append(
                        ("IR OP Analysis", "FAIL",
                         f"No 'ops' array — AI produced wrong output type"))
                    return False
                # Check that ops have expected fields
                valid_ops = [o for o in ops_list if isinstance(o, dict) and "name" in o]
                if len(valid_ops) < len(ops_list):
                    print_warn(
                        f"{len(ops_list) - len(valid_ops)} entries missing 'name' field — filtered")
                if not valid_ops:
                    print_error("No valid OP entries with 'name' field found!")
                    self.state.summary_rows.append(
                        ("IR OP Analysis", "FAIL", "No valid OP entries"))
                    return False

                self.state.ir_ops_report = data
                dialects = data.get("dialects", [])
                print_status(True,
                    f"OP analysis complete: "
                    f"{data.get('total_ops', len(valid_ops))} OPs, "
                    f"{len(dialects)} dialects — "
                    f"{', '.join(dialects[:10])}")
                self.state.summary_rows.append(
                    ("IR OP Analysis", "PASS",
                     f"{data.get('total_ops', len(valid_ops))} OPs"))
                return True
            except Exception as e:
                print_warn(f"Could not parse ops report: {e}")

        self.state.summary_rows.append(("IR OP Analysis", "FAIL", "No report"))
        return False

    def _do_ir_change_analysis(self) -> bool:
        """[3.2] AI analyzes OP definition changes between LLVM versions."""
        print_header("Phase 3.2: IR OP Change Analysis")
        ascend_path = Path(self.state.triton_ascend_path)

        self._print_workspace_info("Phase 3.2: IR Change Analysis")

        ir_dir = WORKSPACE_DIR / IR_ANALYSIS_DIR
        ir_dir.mkdir(parents=True, exist_ok=True)

        baseline_hash = _ASCEND_BASELINE_LLVM_HASH
        llvm_project = _llvm_project_path()

        # Read target LLVM hash from ascend repo
        llvm_hash_file = ascend_path / "cmake" / "llvm-hash.txt"
        if not llvm_hash_file.exists():
            print_error(f"llvm-hash.txt not found at {llvm_hash_file}")
            self.state.summary_rows.append(
                ("IR Change Analysis", "FAIL", "llvm-hash.txt missing"))
            return False
        target_hash = llvm_hash_file.read_text(encoding="utf-8").strip()

        print_key_value("Input ops report", str(ir_dir / IR_OPS_REPORT_FILE))
        print_key_value("LLVM project", str(llvm_project))
        print_key_value("Baseline LLVM", f"{baseline_hash[:12]} ({baseline_hash})")
        print_key_value("Target LLVM", f"{target_hash[:12]} ({target_hash})")

        # ── Pre-flight: verify both commits exist in llvm-project ──
        if not llvm_project.exists():
            print_error(f"llvm-project not found at {llvm_project}")
            self.state.summary_rows.append(
                ("IR Change Analysis", "FAIL", "llvm-project not found"))
            return False

        print_info("Verifying LLVM commits are available in llvm-project...")
        for label, h in [("Baseline", baseline_hash), ("Target", target_hash)]:
            try:
                result = subprocess.run(
                    ["git", "cat-file", "-t", h],
                    cwd=str(llvm_project),
                    capture_output=True, text=True, timeout=30,
                )
                if result.returncode == 0:
                    print_status(True, f"{label} commit {h[:12]} — found in llvm-project")
                else:
                    print_error(
                        f"{label} commit {h[:12]} NOT found in llvm-project! "
                        f"(git cat-file -t returned: {result.stderr.strip()})")
                    print_warn(
                        f"Try: cd {llvm_project} && git fetch origin {h}")
                    self.state.summary_rows.append(
                        ("IR Change Analysis", "FAIL",
                         f"{label} commit {h[:12]} not in llvm-project"))
                    return False
            except subprocess.TimeoutExpired:
                print_error(f"Timeout checking {label} commit {h[:12]}")
                return False
            except Exception as e:
                print_error(f"Failed to verify {label} commit: {e}")
                return False

        # ── Pre-flight: show MLIR .td file changes between the two commits ──
        print_info("Scanning MLIR .td file changes between baseline and target...")
        try:
            diff_result = subprocess.run(
                ["git", "diff", "--name-only", baseline_hash, target_hash,
                 "--", "mlir/include/"],
                cwd=str(llvm_project),
                capture_output=True, text=True, timeout=60,
            )
            if diff_result.returncode == 0:
                changed_files = [f for f in diff_result.stdout.splitlines()
                                 if f.endswith(".td")]
                print_info(f"Found {len(changed_files)} changed .td files in mlir/include/ "
                           f"between baseline and target")
                for f in changed_files[:20]:
                    print_info(f"  - {f}")
                if len(changed_files) > 20:
                    print_info(f"  ... and {len(changed_files) - 20} more .td files")
            else:
                print_warn(f"git diff returned non-zero: {diff_result.stderr.strip()}")
        except subprocess.TimeoutExpired:
            print_warn("git diff timed out after 60s — continuing anyway")
        except Exception as e:
            print_warn(f"Could not run git diff for .td files: {e}")

        # ── Pre-flight: show ops report summary for AI context ──
        ops_report_path = ir_dir / IR_OPS_REPORT_FILE
        if ops_report_path.exists():
            try:
                ops = json.loads(ops_report_path.read_text(encoding="utf-8"))
                print_info(
                    f"Ops report: {ops.get('total_ops', '?')} OPs across "
                    f"{len(ops.get('dialects', []))} dialects — "
                    f"{', '.join(ops.get('dialects', [])[:8])}")
            except Exception:
                print_warn("Could not read ops_report.json for summary")
        else:
            print_warn(f"Ops report not found at {ops_report_path} — "
                       f"AI will need to discover OPs on its own")

        print_info("AI will compare each OP's .td definition with:")
        print_info(f"  git show {baseline_hash[:12]}:mlir/include/.../<Op>.td")
        print_info(f"  git show {target_hash[:12]}:mlir/include/.../<Op>.td")
        print_info("Invoking AI for OP change analysis (this may take several minutes)...")

        try:
            ai_result = run_opencode_adapter({
                "step_id": "ir-analyze-changes",
                "previous_step_id": "ir-analyze-ops",
                "previous_step_summary_path": str(ir_dir / IR_OPS_REPORT_FILE),
                "is_last_step": "true",
                "step_index": "ir",
                "step_dir": str(ir_dir),
                "fix_dir": str(ir_dir),
                "conflict_dir": "",
                "ascend_path": str(ascend_path),
                "triton_path": self.state.triton_path,
                "reference_dir": _REFERENCE_DIR,
                "mode": "ir_analyze_changes",
                "error_logs": json.dumps(
                    [str(ir_dir / IR_OPS_REPORT_FILE)], ensure_ascii=False),
                "target_commit": self.state.target_commit,
                "llvm_project_path": str(_llvm_project_path()),
                "baseline_llvm_hash": _ASCEND_BASELINE_LLVM_HASH,
                "target_llvm_hash": target_hash,
            })
            _ = ai_result
        except Exception as e:
            print_error(f"IR change analysis failed: {e}")
            self.state.summary_rows.append(
                ("IR Change Analysis", "FAIL", str(e)[:60]))
            return False

        changes_report = ir_dir / IR_CHANGES_REPORT_FILE
        if changes_report.exists():
            try:
                data = json.loads(changes_report.read_text(encoding="utf-8"))
                # ── Content validation: must have 'changes' array and 'summary' ──
                changes_list = data.get("changes", [])
                summary = data.get("summary", {})
                if not changes_list or not isinstance(changes_list, list):
                    print_error(
                        f"AI output is NOT a valid changes report! "
                        f"Missing or empty 'changes' array. "
                        f"Top-level keys: {list(data.keys())}")
                    print_warn(
                        f"AI may have produced a merge analysis instead of "
                        f"OP change comparison. Check {changes_report} for content.")
                    self.state.summary_rows.append(
                        ("IR Change Analysis", "FAIL",
                         "No 'changes' array — AI produced wrong output type"))
                    return False

                self.state.ir_changes_report = data
                print_status(True,
                    f"Change analysis: {summary.get('total_ops_analyzed', '?')} "
                    f"OPs, {summary.get('ops_needing_patch', '?')} need patch, "
                    f"{summary.get('renamed_ops', 0)} renamed, "
                    f"{summary.get('signature_changes', 0)} signature changes")
                self.state.summary_rows.append(
                    ("IR Change Analysis", "PASS",
                     f"{summary.get('ops_needing_patch', '?')} OPs need patch"))
                # If no OPs need patching, still return True (Phase 3 is a no-op)
                return True
            except Exception as e:
                print_warn(f"Could not parse changes report: {e}")

        self.state.summary_rows.append(
            ("IR Change Analysis", "FAIL", "No report"))
        return False

    def _do_ir_generate_patches(self) -> bool:
        """[3.3] AI modifies the Ascend LLVM patch for IR compatibility.

        The AI directly edits the existing patch file at
        ``third_party/ascend/patch/llvm_patch_f6ded0b.patch`` rather than
        creating a new file from scratch — this lets it start from a known-
        working baseline and only adjust the parts that need changing for
        the current LLVM version.
        """
        print_header("Phase 3.3: IR Patch Generation")
        ascend_path = Path(self.state.triton_ascend_path)

        self._print_workspace_info("Phase 3.3: IR Patch Generation")

        ir_dir = WORKSPACE_DIR / IR_ANALYSIS_DIR

        # The patch file that AI modifies in-place
        ascend_patch = (ascend_path / "third_party" / "ascend" / "patch"
                        / "llvm_patch_f6ded0b.patch")
        print_key_value("Target patch", str(ascend_patch))

        changes_report = ir_dir / IR_CHANGES_REPORT_FILE
        if changes_report.exists():
            try:
                report = json.loads(changes_report.read_text(encoding="utf-8"))
                summary = report.get("summary", {})
                print_info(f"Changes report: {summary.get('total_ops_analyzed', '?')} OPs analyzed, "
                           f"{summary.get('ops_needing_patch', '?')} need patches")
            except Exception:
                pass
        print_info("Invoking AI to modify the Ascend LLVM compatibility patch...")

        try:
            ai_result = run_opencode_adapter({
                "step_id": "ir-generate-patch",
                "previous_step_id": "ir-analyze-changes",
                "previous_step_summary_path": str(ir_dir / IR_CHANGES_REPORT_FILE),
                "is_last_step": "true",
                "step_index": "ir",
                "step_dir": str(ascend_patch.parent),
                "fix_dir": str(ascend_patch.parent),
                "conflict_dir": "",
                "ascend_path": str(ascend_path),
                "triton_path": self.state.triton_path,
                "reference_dir": _REFERENCE_DIR,
                "mode": "ir_generate_patch",
                "error_logs": json.dumps(
                    [str(ir_dir / IR_CHANGES_REPORT_FILE)], ensure_ascii=False),
                "target_commit": self.state.target_commit,
                "llvm_project_path": str(_llvm_project_path()),
                "baseline_llvm_hash": _ASCEND_BASELINE_LLVM_HASH,
                "ascend_patch_file": str(ascend_patch),
            })
            _ = ai_result
        except Exception as e:
            print_error(f"IR patch generation failed: {e}")
            self.state.summary_rows.append(
                ("IR Patch Gen", "FAIL", str(e)[:60]))
            return False

        # Check the ascend patch was modified
        if ascend_patch.exists():
            print_status(True, f"Modified {ascend_patch.name}")
            self.state.ir_patches = [str(ascend_patch)]
            self.state.summary_rows.append(
                ("IR Patch Gen", "PASS", ascend_patch.name))
            return True

        # No changes needed — valid if changes_report showed no issues
        print_info(f"{ascend_patch.name} unchanged — "
                   "IR compatibility may already be satisfied")
        self.state.summary_rows.append(
            ("IR Patch Gen", "PASS", "No changes needed"))
        return True

    def _do_ir_apply_patches_and_rebuild(self) -> bool:
        """[3.4 + 3.5] Apply the Ascend LLVM patch and rebuild."""
        print_header("Phase 3.4-3.5: Apply Patches + Rebuild LLVM")
        ascend_path = Path(self.state.triton_ascend_path)

        self._print_workspace_info("Phase 3.4-3.5: Apply Patches + Rebuild LLVM")

        llvm_project = _llvm_project_path()
        # The in-repo Ascend LLVM patch (modified by AI in step 3.3)
        ascend_patch = (ascend_path / "third_party" / "ascend" / "patch"
                        / "llvm_patch_f6ded0b.patch")
        print_key_value("LLVM project", str(llvm_project))
        print_key_value("Patch file", str(ascend_patch))

        if not llvm_project.exists():
            print_error(f"LLVM project not found at {llvm_project}")
            self.state.summary_rows.append(
                ("IR Apply+Rebuild", "FAIL", "llvm-project not found"))
            return False

        # ── Ensure llvm-project workspace is clean before checkout + patch ──
        if not self._ensure_llvm_workspace_clean(reason="ir-apply-patches"):
            print_error("Cannot clean llvm-project workspace — aborting IR patch rebuild")
            self.state.summary_rows.append(
                ("IR Apply+Rebuild", "FAIL", "workspace not clean"))
            return False

        # ── [3.4] Apply patch to llvm-project ──
        # Deterministic: clean → checkout → apply. No AI involved.
        from TA_main2main_workflow.scripts.build_test import apply_llvm_patches

        # Read the target LLVM hash from triton-ascend
        llvm_hash_file = ascend_path / "cmake" / "llvm-hash.txt"
        target_llvm_hash = ""
        if llvm_hash_file.exists():
            target_llvm_hash = llvm_hash_file.read_text(encoding="utf-8").strip()
            print_key_value("Target LLVM hash", target_llvm_hash[:12])

        print_info(f"Step 3.4: Applying {ascend_patch.name} to llvm-project...")
        patch_result = apply_llvm_patches(
            ascend_patch.parent, llvm_project,
            target_hash=target_llvm_hash, patch_file=ascend_patch)
        if not patch_result["all_ok"]:
            failed = patch_result["failed"]
            print_error(f"LLVM patch apply failed: "
                        f"{failed[0]['error'][:200] if failed else 'unknown'}")
            self.state.summary_rows.append(
                ("IR Apply+Rebuild", "FAIL", "patch did not apply cleanly"))
            return False

        print_status(True, f"{ascend_patch.name} applied to llvm-project")

        # ── [3.5] Rebuild LLVM ──
        from TA_main2main_workflow.scripts.build_test import \
            _check_and_rebuild_llvm
        try:
            print_info("Step 3.5: Rebuilding LLVM (this takes ~1-2 hours)...")
            llvm_prefix = _check_and_rebuild_llvm(
                ascend_path, force_rebuild=True)
            if llvm_prefix and not self.state.llvm_prefix:
                self.state.llvm_prefix = llvm_prefix
            print_status(True, "LLVM rebuild complete")
            self.state.summary_rows.append(
                ("LLVM Patch Apply+Rebuild", "PASS", "patch applied, LLVM rebuilt"))
            return True
        except Exception as e:
            print_error(f"LLVM rebuild failed: {e}")
            self.state.summary_rows.append(
                ("LLVM Patch Apply+Rebuild", "FAIL", str(e)[:60]))
            return False

    def _do_pytest(self) -> bool:
        """[4.2] Build TA and run pytest.

        Returns True when all tests pass.
        """
        ascend_path = Path(self.state.triton_ascend_path)
        py_exe = os.getenv("PYTHON", "python3.10")

        import shutil
        if not shutil.which(py_exe):
            print_warn(f"{py_exe} not found on PATH — skipping tests")
            self.state.pytest_passed = False
            self.state.summary_rows.append(
                ("Pytest", "SKIP", f"{py_exe} not found"))
            return False

        print_section(f"Pytest ({py_exe})")
        print_key_value("Ascend path", str(ascend_path))

        # Build with test python
        print_info(f"Building Triton-Ascend with {py_exe}...")
        if not self._do_build(ascend_path, clean=True, python_exe=py_exe):
            print_error(f"Build failed ({py_exe})")
            self.state.pytest_passed = False
            self.state.summary_rows.append(
                ("Pytest", "FAIL", "Build failed"))
            return False

        # Run tests
        result = self._do_test(ascend_path, python_exe=py_exe)
        if result is None:
            passed = True  # SKIP_E2E_TEST
        else:
            passed = bool(result)

        self.state.pytest_passed = passed
        if not passed:
            print_error(f"Pytest FAILED ({py_exe})")

        self.state.summary_rows.append(
            ("Pytest", "PASS" if passed else "FAIL", py_exe))
        return passed

    def _do_ir_diagnose_failures(self) -> bool:
        """[4.3] AI classifies test failures: IR compatibility vs code issues.

        Returns True if IR issues are found (triggering outer loop retry).
        Returns False if failures are all code/environment issues.
        """
        print_header("Phase 4.3: IR Failure Diagnosis")

        ir_dir = WORKSPACE_DIR / IR_ANALYSIS_DIR
        ir_dir.mkdir(parents=True, exist_ok=True)
        print_key_value("Diagnosis output", str(ir_dir / IR_DIAGNOSIS_FILE))

        # Collect test failure logs from both Python runs
        error_log_paths: list[str] = []
        test_log_dir = WORKSPACE_DIR / "test-logs"
        if test_log_dir.exists():
            for log_file in sorted(test_log_dir.rglob("*.log")):
                error_log_paths.append(str(log_file))
        # Also include test result files
        test_result = WORKSPACE_DIR / TEST_RESULT_FILE
        if test_result.exists():
            error_log_paths.append(str(test_result))

        if not error_log_paths:
            print_warn("No test failure logs found — assuming code issues")
            return False

        print_info(f"Collected {len(error_log_paths)} log file(s) for AI diagnosis")
        for p in error_log_paths[:5]:
            print_info(f"  - {p}")
        if len(error_log_paths) > 5:
            print_info(f"  ... and {len(error_log_paths) - 5} more")
        print_info("Invoking AI to classify failures (IR compatibility vs code vs environment)...")

        try:
            ai_result = run_opencode_adapter({
                "step_id": "ir-diagnose",
                "previous_step_id": "ir-generate-patch",
                "previous_step_summary_path": str(ir_dir / IR_CHANGES_REPORT_FILE),
                "is_last_step": "true",
                "step_index": "ir",
                "step_dir": str(ir_dir),
                "fix_dir": str(ir_dir),
                "conflict_dir": "",
                "ascend_path": str(Path(self.state.triton_ascend_path)),
                "triton_path": self.state.triton_path,
                "reference_dir": _REFERENCE_DIR,
                "mode": "ir_diagnose",
                "error_logs": json.dumps(error_log_paths, ensure_ascii=False),
                "target_commit": self.state.target_commit,
            })
            _ = ai_result
        except Exception as e:
            print_error(f"IR diagnosis failed: {e}")
            return False

        diagnosis_path = ir_dir / IR_DIAGNOSIS_FILE
        if not diagnosis_path.exists():
            print_warn("No diagnosis report generated")
            return False

        try:
            diagnosis = json.loads(
                diagnosis_path.read_text(encoding="utf-8"))
            summary = diagnosis.get("summary", {})
            has_ir = summary.get("has_ir_issues", False)
            print_key_value("total failures", str(summary.get("total_failures", "?")))
            print_key_value("IR issues", str(summary.get("ir_issues", "?")))
            print_key_value("code issues", str(summary.get("code_issues", "?")))
            print_key_value("env issues", str(summary.get("environment_issues", "?")))
            self.state.summary_rows.append(
                ("IR Diagnosis", "PASS",
                 f"IR={summary.get('ir_issues', '?')} "
                 f"code={summary.get('code_issues', '?')} "
                 f"env={summary.get('environment_issues', '?')}"))
            return bool(has_ir)
        except Exception as e:
            print_warn(f"Could not parse diagnosis: {e}")
            return False

    # ═══════════════════════════════════════════════════════════════════════════
    # Per-step IR patch pipeline (single-step mode)
    # ═══════════════════════════════════════════════════════════════════════════

    def _do_per_step_ir_patch(self, step: dict) -> bool:
        """Per-step IR compatibility patch generation + LLVM rebuild.

        Called from _run_single_step_mode() when a step's merge included an
        LLVM hash change.  Reuses the shared IR analysis / patch-generation
        methods but adds a patch→rebuild retry loop specific to the
        single-step context.

        Pipeline:
          1. Verify LLVM hash changed
          2. Create llvm_change_analysis/<step_id>/ workspace
          3. IR analysis → patch → apply → rebuild LLVM (max 3 iterations)
          4. On patch failure: stash/drop, loop back for AI to fix
        """
        step_id = step["id"]

        # ── Guard: check LLVM hash actually changed ──
        if not self._llvm_hash_did_change():
            print_info(f"[{step_id}] LLVM hash unchanged — skipping IR patch")
            return True

        # ── Create per-step analysis workspace ──
        analysis_dir = WORKSPACE_DIR / LLVM_CHANGE_ANALYSIS_DIR / step_id
        analysis_dir.mkdir(parents=True, exist_ok=True)
        print_key_value("IR analysis dir", str(analysis_dir))

        # ── IR analysis → patch → rebuild loop ──
        for iteration in range(self.state.ir_max_iterations):
            self.state.ir_patch_iteration = iteration
            print_header(
                f"Per-Step IR Patch — {step_id} "
                f"(iter {iteration + 1}/{self.state.ir_max_iterations})"
            )

            # [3.1 + 3.2] Analysis (first iteration only for OP scan)
            if iteration == 0:
                print_info("First iteration — running full OP analysis pipeline")
                if not self._do_ir_op_analysis():
                    return False
                if not self._do_ir_change_analysis():
                    return False
            else:
                print_info("Re-analyzing OP changes after patch retry...")
                if not self._do_ir_change_analysis():
                    return False

            # [3.3] Generate patches
            if not self._do_ir_generate_patches():
                return False

            # [3.4 + 3.5] Apply patches + rebuild LLVM (with retry for patch failures)
            rebuild_ok = False
            for patch_attempt in range(IR_MAX_ITERATIONS):
                print_info(
                    f"Patch apply attempt {patch_attempt + 1}/{IR_MAX_ITERATIONS}"
                )
                if self._do_ir_apply_patches_and_rebuild():
                    rebuild_ok = True
                    break
                # Patch failed — stash/drop, let AI regenerate
                print_warn(
                    f"LLVM rebuild failed (patch attempt {patch_attempt + 1}) — "
                    f"will stash changes and retry patch generation"
                )
                self._stash_and_drop_llvm_patch()
                if not self._do_ir_generate_patches():
                    break

            if rebuild_ok:
                print_status(True, f"IR patch + LLVM rebuild OK for {step_id}")
                self.state.ir_loop_details.append({
                    "step_id": step_id,
                    "iteration": iteration + 1,
                    "result": "PASS",
                })
                return True

            print_warn(f"IR patch iteration {iteration + 1} exhausted "
                       f"— retrying outer loop")

        print_error(f"IR patch loop exhausted {self.state.ir_max_iterations} "
                    f"iterations for {step_id}")
        return False

    def _build_baseline_llvm(self) -> bool:
        """Build baseline LLVM (pre-merge state) before any merge steps.

        Called once at the start of _run_single_step_mode().  Reads the
        current cmake/llvm-hash.txt from triton-ascend, checks out that
        commit in llvm-project, applies the Ascend backend LLVM patch,
        builds LLVM, then stashes + drops the patch to leave a clean tree.

        The baseline LLVM must be built before merging because the Ascend
        backend code depends on it for compilation.
        """
        print_header("Build Baseline LLVM (pre-merge)")
        ascend_path = Path(self.state.triton_ascend_path)

        self._print_workspace_info("Build Baseline LLVM")

        # ── Allow skipping baseline LLVM build for debugging ──
        if os.getenv("SKIP_BASELINE_LLVM", "false").lower() == "true":
            print_info("SKIP_BASELINE_LLVM=true — skipping baseline LLVM build")
            print_warn("Ensure LLVM is already built at LLVM_INSTALL_PREFIX_SYNC")
            if not self.state.llvm_prefix:
                self.state.llvm_prefix = str(_llvm_install_prefix())
            self.state.summary_rows.append(
                ("Baseline LLVM", "SKIP", "SKIP_BASELINE_LLVM set"))
            return True

        llvm_project = _llvm_project_path()
        llvm_install = _llvm_install_prefix()

        if not llvm_project.exists():
            print_error(f"llvm-project not found at {llvm_project}")
            return False

        # ── 1. Read LLVM hash from base branch (work branch base) ──
        # Use git show to get the hash from the base branch, NOT the checkout
        # filesystem — the checkout may be on a stale branch.
        base_branch = os.getenv(ENV_BASE_BRANCH, "main")
        base_ref = get_base_branch_ref()
        try:
            run_git(ascend_path, "fetch", "origin", base_branch)
        except Exception:
            print_warn(f"[baseline-llvm] Could not fetch {base_ref}, using local ref")
        try:
            llvm_hash = run_git(
                ascend_path, "show", f"{base_ref}:cmake/llvm-hash.txt"
            ).strip()
        except Exception:
            # Fallback: read from checkout filesystem
            llvm_hash_file = ascend_path / "cmake" / "llvm-hash.txt"
            if not llvm_hash_file.exists():
                print_error(f"LLVM hash file not found: {llvm_hash_file}")
                return False
            llvm_hash = llvm_hash_file.read_text(encoding="utf-8").strip()
            print_warn(f"[baseline-llvm] Using checkout llvm-hash.txt ({base_ref} not available)")
        if not llvm_hash:
            print_error("LLVM hash is empty")
            return False
        print_key_value("LLVM commit", llvm_hash[:12])
        print_info(f"  (from {base_ref})")

        # ── Ensure llvm-project workspace is clean before checkout ──
        if not self._ensure_llvm_workspace_clean(reason="baseline-llvm-build"):
            print_error("Cannot clean llvm-project workspace — aborting baseline build")
            return False

        # ── 2. Checkout the LLVM commit ──
        print_info(f"Checking out LLVM commit {llvm_hash[:12]} in llvm-project...")
        try:
            # Fetch the specific commit with retries
            for attempt in range(1, 7):
                fetch_proc = subprocess.run(
                    ["git", "fetch", "origin", llvm_hash],
                    cwd=str(llvm_project), capture_output=True, text=True, timeout=2000,
                )
                if fetch_proc.returncode == 0:
                    break
                print_warn(f"git fetch attempt {attempt}/6 failed: "
                           f"{fetch_proc.stderr.strip()[-150:]}")
            else:
                raise RuntimeError(
                    f"Failed to fetch LLVM commit {llvm_hash[:12]} after 6 attempts")

            subprocess.run(
                ["git", "checkout", llvm_hash],
                cwd=str(llvm_project), check=True, capture_output=True, text=True,
                timeout=2000,
            )
            print_status(True, f"Checked out {llvm_hash[:12]}")
        except Exception as e:
            print_error(f"Failed to checkout LLVM commit: {e}")
            log_proc = subprocess.run(
                ["git", "log", "--oneline", "-5"],
                cwd=str(llvm_project), capture_output=True, text=True, timeout=10,
            )
            print_info(f"llvm-project HEAD and recent commits:\n{log_proc.stdout.strip()}")
            return False

        # ── 3. Apply Ascend backend LLVM patch ──
        ascend_patch = ascend_path / "third_party" / "ascend" / "patch" / "llvm_patch_f6ded0b.patch"
        if ascend_patch.exists():
            print_info(f"Applying Ascend LLVM patch: {ascend_patch.name}")
            # Dry-run first
            dry_run = subprocess.run(
                ["git", "apply", "--check", str(ascend_patch)],
                cwd=str(llvm_project), capture_output=True, text=True, timeout=30,
            )
            if dry_run.returncode != 0:
                print_error(f"Patch does not apply cleanly: {dry_run.stderr.strip()[-400:]}")
                return False
            try:
                subprocess.run(
                    ["git", "apply", str(ascend_patch)],
                    cwd=str(llvm_project), check=True, capture_output=True, text=True, timeout=30,
                )
                print_status(True, "Ascend LLVM patch applied")
            except Exception as e:
                print_error(f"Failed to apply patch: {e}")
                return False
        else:
            print_warn(f"Ascend LLVM patch not found at {ascend_patch} — continuing without it")

        # ── 4. Build LLVM ──
        llvm_build_log = WORKSPACE_DIR / "llvm_build_baseline.log"
        llvm_build_log.parent.mkdir(parents=True, exist_ok=True)

        build_dir = llvm_project / "build"
        if build_dir.exists():
            import shutil
            shutil.rmtree(build_dir)
        build_dir.mkdir()

        cmake_cmd = [
            "cmake", str(llvm_project / "llvm"),
            "-G", "Ninja",
            "-DCMAKE_BUILD_TYPE=Release",
            "-DLLVM_ENABLE_ASSERTIONS=ON",
            "-DLLVM_ENABLE_PROJECTS=mlir;llvm;lld",
            "-DLLVM_TARGETS_TO_BUILD=host;NVPTX;AMDGPU",
            f"-DCMAKE_INSTALL_PREFIX={llvm_install}",
            "-DCMAKE_C_COMPILER=clang",
            "-DCMAKE_CXX_COMPILER=clang++",
        ]

        # ── Helper: run a command with live output streaming ──
        def _stream_cmd(cmd: list[str], cwd: Path, log_fh, timeout: int,
                        label: str) -> int:
            """Stream subprocess output line-by-line to console and log file.
            Returns the process exit code."""
            print_info(f"{label} (streaming to {llvm_build_log.name})...")
            proc = subprocess.Popen(
                cmd, cwd=str(cwd),
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            )
            assert proc.stdout is not None
            last_line = ""
            for line in proc.stdout:
                log_fh.write(line)
                stripped = line.rstrip()
                if stripped:
                    last_line = stripped
                    # \r returns to line start, \033[K clears trailing residue
                    print(f"\r  {stripped[:140]}\033[K", end="", flush=True)
            proc.wait(timeout=timeout)
            if last_line:
                print()  # final newline after \r lines
            return proc.returncode

        # ── cmake configure ──
        with llvm_build_log.open("w", encoding="utf-8") as fh:
            fh.write(f"=== cmake ===\n{' '.join(cmake_cmd)}\n\n")
            fh.flush()
            rc = _stream_cmd(cmake_cmd, build_dir, fh, timeout=300,
                             label="Configuring LLVM with cmake")
        if rc != 0:
            print_error(f"cmake failed (exit {rc}) — see {llvm_build_log}")
            return False
        print_status(True, "cmake configure OK")

        # ── ninja build + install ──
        print_info("Building LLVM with ninja (this may take ~0.5 hours)...")
        with llvm_build_log.open("a", encoding="utf-8") as fh:
            fh.write(f"\n=== ninja install ===\n")
            fh.flush()
            rc = _stream_cmd(["ninja", "install"], build_dir, fh, timeout=7200,
                             label="ninja install")
        if rc != 0:
            print_error(f"ninja install failed (exit {rc}) — see {llvm_build_log}")
            return False
        print_status(True, "ninja install OK")

        # Copy FileCheck
        import shutil
        filecheck_src = build_dir / "bin" / "FileCheck"
        filecheck_dst = llvm_install / "bin" / "FileCheck"
        if filecheck_src.exists():
            filecheck_dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(filecheck_src, filecheck_dst)
            print_info("Copied FileCheck to install prefix")

        # Write hash cache
        hash_cache = llvm_install / ".llvm_hash"
        llvm_install.mkdir(parents=True, exist_ok=True)
        hash_cache.write_text(llvm_hash, encoding="utf-8")
        print_status(True, "Baseline LLVM build complete")

        # Store llvm_prefix for later use
        if not self.state.llvm_prefix:
            self.state.llvm_prefix = str(llvm_install)

        # ── 5. Stash + drop the patch to leave a clean tree ──
        print_info("Stashing and dropping Ascend LLVM patch to clean working tree...")
        try:
            subprocess.run(
                ["git", "stash", "push", "-u", "-m", "ta-baseline-llvm-patch"],
                cwd=str(llvm_project), capture_output=True, text=True, timeout=30,
            )
            subprocess.run(
                ["git", "stash", "drop", "stash@{0}"],
                cwd=str(llvm_project), capture_output=True, text=True, timeout=30,
            )
            print_status(True, "LLVM working tree clean (patch stashed + dropped)")
        except Exception as e:
            print_warn(f"Stash/drop failed: {e} — forcing clean with checkout")
            subprocess.run(
                ["git", "checkout", "--", "."],
                cwd=str(llvm_project), capture_output=True, text=True, timeout=60,
            )
            subprocess.run(
                ["git", "clean", "-fd"],
                cwd=str(llvm_project), capture_output=True, text=True, timeout=60,
            )

        self.state.summary_rows.append(
            ("Baseline LLVM", "PASS", f"Built {llvm_hash[:12]}"))
        return True

    def _ensure_llvm_workspace_clean(self, reason: str = "") -> bool:
        """Ensure the llvm-project working tree is clean before building.

        Checks git status; if dirty, stashes and drops all uncommitted
        changes (including untracked files).  Falls back to 'git checkout
        -- .' + 'git clean -fd' if stash fails.

        Returns True if the workspace is clean (or was cleaned successfully).
        """
        llvm_project = _llvm_project_path()
        if not llvm_project.exists():
            print_warn("[llvm-clean] llvm-project not found — cannot verify workspace")
            return True  # nothing to clean

        # ── Check if working tree is dirty ──
        try:
            status = subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=str(llvm_project),
                capture_output=True, text=True, timeout=15,
            ).stdout.strip()
        except Exception as e:
            print_warn(f"[llvm-clean] Could not check git status: {e}")
            return True  # proceed and let the build step surface errors

        if not status:
            print_info(f"[llvm-clean] llvm-project workspace is clean"
                       f"{f' ({reason})' if reason else ''}")
            return True

        # ── Workspace is dirty — clean it ──
        dirty_files = status.splitlines()
        print_warn(f"[llvm-clean] llvm-project has {len(dirty_files)} uncommitted"
                   f" file(s){f' ({reason})' if reason else ''} — cleaning...")
        for f in dirty_files[:10]:
            print(f"      {f}")
        if len(dirty_files) > 10:
            print(f"      ... and {len(dirty_files) - 10} more")

        try:
            subprocess.run(
                ["git", "stash", "push", "-u", "-m",
                 f"ta-auto-clean{': ' + reason if reason else ''}"],
                cwd=str(llvm_project),
                capture_output=True, text=True, timeout=30,
            )
            subprocess.run(
                ["git", "stash", "drop", "stash@{0}"],
                cwd=str(llvm_project),
                capture_output=True, text=True, timeout=30,
            )
            print_status(True, "[llvm-clean] Workspace cleaned (stash + drop)")
            return True
        except Exception as e:
            print_warn(f"[llvm-clean] Stash/drop failed: {e} — "
                       f"forcing clean with checkout")
            try:
                subprocess.run(
                    ["git", "checkout", "--", "."],
                    cwd=str(llvm_project),
                    capture_output=True, text=True, timeout=60,
                )
                subprocess.run(
                    ["git", "clean", "-fd"],
                    cwd=str(llvm_project),
                    capture_output=True, text=True, timeout=60,
                )
                print_status(True, "[llvm-clean] Workspace cleaned (checkout + clean)")
                return True
            except Exception as e2:
                print_error(f"[llvm-clean] Failed to clean workspace: {e2}")
                return False

    def _stash_and_drop_llvm_patch(self) -> None:
        """Deprecated: use _ensure_llvm_workspace_clean() instead."""
        self._ensure_llvm_workspace_clean(reason="ir-patch-failed")

    # ═══════════════════════════════════════════════════════════════════════════
    # Per-step test + fix loop (single-step mode)
    # ═══════════════════════════════════════════════════════════════════════════

    def _do_test_and_fix_loop(self) -> bool:
        """Run tests + AI-fix loop for the current step.

        Returns True if all tests pass, False on exhaustion.
        """
        ascend_path = Path(self.state.triton_ascend_path)
        step = (self.state.steps[self.state.current_step]
                if self.state.steps else None)
        step_id = step["id"] if step else "step-0"
        step_dir = WORKSPACE_DIR / STEPS_DIR / step_id
        step_dir.mkdir(parents=True, exist_ok=True)

        test_passed = False

        for attempt in range(self.state.max_retries + 1):
            is_fix_attempt = attempt > 0
            self.state.retry_count = attempt

            # AI fix test failures (skip on first round)
            if is_fix_attempt:
                print_header(f"Fix Attempt {attempt}/{self.state.max_retries} (test)")
                self.state.fix_errors = self._collect_test_error_logs()
                if self.state.fix_errors:
                    ai_ok = self._do_ai_fix(ascend_path, step_dir, attempt)
                    # Record fix attempt
                    modified_files: list[str] = []
                    ai_summary = ""
                    if hasattr(self, '_last_ai_result') and self._last_ai_result:
                        modified_files = self._last_ai_result.get("modified_files", [])
                        ai_summary = self._last_ai_result.get("step_summary", "")
                    error_snippet = ""
                    for err_path in self.state.fix_errors:
                        try:
                            content = Path(err_path).read_text(
                                encoding="utf-8", errors="replace")
                            error_snippet += (content[-2000:]
                                             if len(content) > 2000 else content)
                        except Exception:
                            pass
                    self.state.fix_attempts.append({
                        "step_id": step_id,
                        "attempt": attempt,
                        "fix_type": "test",
                        "error_logs": list(self.state.fix_errors),
                        "error_snippet": error_snippet[-1500:],
                        "modified_files": modified_files,
                        "ai_summary": (ai_summary or "")[:2000],
                        "ai_ok": ai_ok,
                    })
                    self.state.test_fix_count += 1
                else:
                    print_warn("No test error logs found — cannot fix")

            # Rebuild after fix (skip on first attempt since build_and_fix already built)
            if is_fix_attempt:
                if not self._do_build(ascend_path, clean=False):
                    print_warn(f"Build failed after test fix (attempt {attempt})")
                    continue

            # Run tests
            test_result = self._do_test(ascend_path)
            if test_result is None:
                # SKIP_E2E_TEST — treat as pass
                test_passed = True
                break
            if test_result:
                test_passed = True
                break

            print_warn(f"Tests failed (attempt {attempt + 1}/"
                       f"{self.state.max_retries + 1})")

            if os.getenv("SKIP_AI_ANALYSIS", "false").lower() == "true":
                print_warn("SKIP_AI_ANALYSIS=true — stopping test fix loop")
                break

        if test_passed:
            self.state.test_passed = True
            # Commit test fixes if any were applied
            if self.state.retry_count > 0:
                self._commit_fixes(ascend_path, step_dir)
            self.state.summary_rows.append(
                ("Tests", "PASS", f"{step_id}"))
        else:
            print_error(f"All {self.state.max_retries} fix attempts exhausted "
                        f"— tests still failing")
            self.state.summary_rows.append(
                ("Tests", "FAIL", f"After {self.state.max_retries} attempts"))
            self.state.test_passed = False

        return test_passed

    def _collect_test_error_logs(self) -> list[str]:
        """Collect test failure log paths for AI fix context.

        Returns a list of file paths pointing to test logs and test result
        files in the workspace.
        """
        error_logs: list[str] = []

        # Test log directory — includes raw logs and JUnit XML reports
        test_log_dir = WORKSPACE_DIR / "test-logs"
        if test_log_dir.exists():
            for log_file in sorted(test_log_dir.rglob("*.log")):
                error_logs.append(str(log_file))
            for xml_file in sorted(test_log_dir.rglob("*.xml")):
                error_logs.append(str(xml_file))

        # Test result JSON
        test_result_path = WORKSPACE_DIR / TEST_RESULT_FILE
        if test_result_path.exists():
            error_logs.append(str(test_result_path))

        # Build result JSON (may contain build errors that affect tests)
        build_result_path = WORKSPACE_DIR / BUILD_RESULT_FILE
        if build_result_path.exists():
            error_logs.append(str(build_result_path))

        if error_logs:
            print_info(f"Collected {len(error_logs)} error log(s) for AI fix")
            for p in error_logs[:5]:
                print_info(f"  - {p}")
            if len(error_logs) > 5:
                print_info(f"  ... and {len(error_logs) - 5} more")

        return error_logs

    def _backup_code_state(self, label: str = "snapshot") -> Path | None:
        """Backup triton-ascend working tree to workspace for CI artifact retention.

        Copies the entire working tree (tracked + untracked) excluding .git
        and build artifacts. Used both on success (label="final") and on
        failure (label="failed-step-N") so no AI fix or conflict resolution
        work is ever lost.
        """
        ascend_path = Path(self.state.triton_ascend_path)
        ts = time.strftime("%Y%m%d-%H%M%S")
        backup_dir = WORKSPACE_DIR / "code-backups" / f"{label}_{ts}"
        backup_dir.parent.mkdir(parents=True, exist_ok=True)

        _ignore_patterns = shutil.ignore_patterns(
            ".git", "__pycache__", "*.pyc", "*.pyo",
            "*.o", "*.a", "*.so", "*.dylib",
            "build", "dist", "*.egg-info",
            ".mypy_cache", ".pytest_cache", ".ruff_cache",
            "result_profiling", "*.lock",
        )
        try:
            shutil.copytree(str(ascend_path), str(backup_dir),
                            ignore=_ignore_patterns, symlinks=False)
            file_count = sum(1 for _ in backup_dir.rglob("*") if _.is_file())
            print_info(f"Code backup [{label}]: {backup_dir} ({file_count} files)")

            # ── Also record git state snapshot ──
            try:
                head = run_git(ascend_path, "rev-parse", "HEAD").strip()
                branch = run_git(ascend_path, "branch", "--show-current").strip()
                status = run_git(ascend_path, "status", "--porcelain").strip()
                info = (
                    f"# Backup: {label}\n"
                    f"# Time: {ts}\n"
                    f"# Branch: {branch}\n"
                    f"# HEAD: {head}\n"
                    f"# Uncommitted changes: {'yes' if status else 'none'}\n"
                )
                (backup_dir / "_BACKUP_INFO.txt").write_text(info, encoding="utf-8")
            except Exception:
                pass

            return backup_dir
        except Exception as e:
            print_warn(f"Could not create code backup [{label}]: {e}")
            return None

    def _do_finalize(self):
        """Generate patch, summary & print final report.

        Does NOT restore the original branch — the work branch must stay
        checked out so push_to_github can push it. Branch restore happens
        at the end of push_to_github (or handle_failure).
        """
        print_header("Phase Final: Finalize & Summary")

        self._print_workspace_info("Phase Final: Finalize")

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

        # ── Backup work branch code ──
        self._backup_code_state("final")

        self.state.summary_rows.append(
            ("Finalize", "PASS", f"{self.state.total_steps} step(s) completed")
        )

        # ── Print final summary table ──
        print_header("Sync Complete — Success!")
        print_elapsed_total()
        # Add IR loop metrics if applicable
        if self.state.llvm_hash_changed:
            self.state.summary_rows.append(
                ("IR Loop", "PASS",
                 f"{len(self.state.ir_loop_details)} iteration(s)"))
        # Add pytest result
        pytest_status = "PASS" if self.state.pytest_passed else "N/A"
        self.state.summary_rows.append(("Pytest", pytest_status, ""))
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

        self._print_workspace_info("Push to GitHub & Create PR")

        github_repo = os.getenv("GITHUB_REPO", "TecJesh/triton-ascend")
        if not github_repo:
            print_error("GITHUB_REPO is empty — cannot create PR")
            self.state.summary_rows.append(("Push & PR", "FAIL", "GITHUB_REPO empty"))
            self.state.final_status = UpgradeFailed
            return UpgradeFailed

        # ── Build a comprehensive PR body from step summaries ──
        pr_body_path = WORKSPACE_DIR / FINAL_SUMMARY_FILE
        self._build_pr_body(pr_body_path)

        # ── Push AscendNPU-IR submodule first ──
        self._push_submodule_if_needed()

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
            # ── Print detailed failure diagnostics ──
            if isinstance(e, subprocess.CalledProcessError):
                print_section("Push/PR Failure Details")
                print_key_value("Command", " ".join(e.cmd) if e.cmd else "N/A")
                print_key_value("Exit code", str(e.returncode))
                if e.stdout:
                    print_info(f"stdout:\n{e.stdout.strip()}")
                if e.stderr:
                    print_error(f"stderr:\n{e.stderr.strip()}")
            else:
                import traceback
                print_info(f"Traceback:\n{traceback.format_exc()}")
            # Print git context for debugging
            ascend_path = Path(self.state.triton_ascend_path)
            print_section("Git Context at Failure")
            print_key_value("Work branch", self.state.work_branch)
            try:
                current_branch = run_git(ascend_path, "branch", "--show-current").strip()
                print_key_value("Current branch", current_branch)
                status_out = run_git(ascend_path, "status", "--short").strip()
                print_info(f"Git status:\n{status_out}" if status_out else "Git status: (clean)")
                log_out = run_git(ascend_path, "log", "--oneline", "-5")
                print_info(f"Recent commits:\n{log_out.strip()}")
            except Exception:
                pass
            self.state.summary_rows.append(("Push & PR", "FAIL", str(e)[:60]))
            self.state.final_status = UpgradeFailed
            # Still try to restore branch, then signal failure
            self._restore_branch()
            return UpgradeFailed

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

        self._print_workspace_info("Handle Failure")

        ascend_path = Path(self.state.triton_ascend_path)

        # ── Backup code state BEFORE anything else ──
        # Capture the working tree so AI fixes, conflict resolutions, and
        # partial merge progress are preserved as CI artifacts even on failure.
        failed_step = self.state.current_step + 1 if self.state.current_step < self.state.total_steps else self.state.total_steps
        self._backup_code_state(f"failed-step{failed_step}")

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

        self.state.final_status = UpgradeFailed
        return UpgradeFailed
