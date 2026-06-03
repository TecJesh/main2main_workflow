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
from TA_main2main_workflow.scripts.merge_upstream import run_merge
from TA_main2main_workflow.scripts.pre_ci_check import run_pre_ci_check
from TA_main2main_workflow.scripts.push_to_github import push_and_create_pr

from TA_main2main_workflow.utils import (
    BUILD_RESULT_FILE, CONFLICT_LOG_DIR,
    EACH_STEP_SUMMARY_FILE, EACH_STEP_TARGET_PATCH_FILE,
    FINAL_SUMMARY_FILE, FINAL_TARGET_PATCH_FILE, FIX_LOG_DIR,
    HasNewCommits, HasNoNewCommits,
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
        print_header("Phase 1: Detect Upstream Commits")

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

        stop_timer("detect")

        if not has_new:
            print_status(True, "Already up to date — nothing to merge")
            self.state.summary_rows.append(("Detect commits", "PASS", "No new commits"))
            return HasNoNewCommits

        print_status(True, f"Found {self.state.upstream_commits_count} upstream commits to merge")
        self.state.summary_rows.append(
            ("Detect commits", "PASS", f"{self.state.upstream_commits_count} commits found")
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
        """Orchestrate the full sync pipeline.

        IMPORTANT: This is a SINGLE @router node that internally calls all
        sub-steps as plain Python methods. We do NOT rely on CrewAI's
        @listen → @listen signal chaining because:
          - In some CrewAI versions, @listen method return values are NOT
            forwarded as routing signals.
          - Only @router method return values reliably route.
          - This caused resolve_conflicts to never be triggered even when
            merge_upstream returned MergeConflict.

        The internal call chain is:
          _do_merge → _do_resolve_conflicts → _do_build_and_fix_loop → _do_finalize
        """
        # ── Step A: git merge upstream ──
        if not self._do_merge():
            self.state.final_status = UpgradeFailed
            return UpgradeFailed

        # ── Step B: AI resolve conflict (if merge has conflicts) ──
        if self.state.merge_has_conflicts:
            if not self._do_resolve_conflicts():
                self.state.final_status = UpgradeFailed
                return UpgradeFailed

        # ── Step C: build → test → AI fix bug loop ──
        if not self._do_build_and_fix_loop():
            self.state.final_status = UpgradeFailed
            return UpgradeFailed

        # ── Step D: finalize (update version, generate patch & summary) ──
        self._do_finalize()
        self.state.final_status = UpgradeCompleted
        return UpgradeCompleted

    # ═══════════════════════════════════════════════════════════════════════════
    # Internal step implementations
    # ═══════════════════════════════════════════════════════════════════════════

    def _do_merge(self) -> bool:
        start_timer("merge")
        print_header("Phase 2: Merge Upstream Triton")

        ascend_path = Path(self.state.triton_ascend_path)
        triton_path = Path(self.state.triton_path)

        print_flow_progress("merge", f"merging {self.state.target_commit[:12]} into triton-ascend")

        merge_result = run_merge(
            ascend_path,
            triton_path,
            self.state.target_commit,
        )

        self.state.work_branch = merge_result["work_branch"]
        self.state.merge_has_conflicts = merge_result["has_conflicts"]
        self.state.conflict_files = merge_result.get("conflict_files", [])

        print_key_value("work branch", self.state.work_branch)
        print_key_value("has conflicts", str(self.state.merge_has_conflicts))
        print_key_value("exit code", str(merge_result["merge_exit_code"]))

        if self.state.merge_has_conflicts:
            print_conflict_list(self.state.conflict_files)
            stop_timer("merge")
            self.state.summary_rows.append(
                ("Merge upstream", "WARN", f"{len(self.state.conflict_files)} conflicts")
            )
        else:
            stop_timer("merge")
            print_status(True, "Merge succeeded with no conflicts")
            self.state.summary_rows.append(("Merge upstream", "PASS", "Clean merge"))

        return True

    def _do_resolve_conflicts(self) -> bool:
        """AI-driven merge conflict resolution with retry loop.

        For each attempt (up to max_retries):
          1. Refresh the conflict file list from git
          2. Call opencode/claude with the conflict snapshots
          3. Check if all conflicts are resolved
          4. If not, retry with refreshed conflict list

        After all conflicts are resolved:
          - git commit the resolution
          - Run pre-CI checks (conflict markers, temp files, syntax)
          - Write step summary and cumulative patch

        Returns True if all conflicts resolved, False otherwise.
        """
        start_timer("resolve")
        print_header("Phase 3: AI Conflict Resolution")

        ascend_path = Path(self.state.triton_ascend_path)
        step_dir = WORKSPACE_DIR / "step-0"
        step_dir.mkdir(parents=True, exist_ok=True)

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
            try:
                ai_result = run_opencode_adapter({
                    "step_id": f"conflict-resolution-{attempt}",
                    "previous_step_id": "",
                    "previous_step_summary_path": "",
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
        """build → test → AI fix bug loop (up to max_retries rounds)."""
        ascend_path = Path(self.state.triton_ascend_path)
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

        test_result = run_tests(
            ascend_path,
            test_dir=self.state.test_dir,
            num_procs=self.state.num_procs,
            conda_env=self.state.conda_env,
        )
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
            print_error(f"Tests FAILED ({failed_count} failed, {error_count} errors)")
            self.state.summary_rows.append(
                ("Tests", "FAIL", f"{failed_count} failed, {error_count} errors")
            )
            return False

    def _do_ai_fix(self, ascend_path: Path, step_dir: Path, attempt: int) -> bool:
        """AI fix bug: invoke opencode/claude to fix build/test failures."""
        print_step(attempt, self.state.max_retries, "AI fix attempt")

        fix_dir = WORKSPACE_DIR / FIX_LOG_DIR / f"fix-{attempt}"
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
        error_logs = json.dumps(self.state.fix_errors, ensure_ascii=False)
        try:
            ai_result = run_opencode_adapter({
                "step_id": f"fix-{attempt}",
                "previous_step_id": "",
                "previous_step_summary_path": str(step_dir / EACH_STEP_SUMMARY_FILE),
                "step_dir": str(fix_dir),
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

    def _do_finalize(self):
        """Generate patch & summary → restore branch."""
        print_header("Phase Final: Finalize & Summary")

        ascend_path = Path(self.state.triton_ascend_path)
        step_dir = WORKSPACE_DIR / "step-0"

        # generate final summary & cumulative patch
        print_section("Generate Final Summary")
        final_summary_path = WORKSPACE_DIR / FINAL_SUMMARY_FILE
        last_summary_path = step_dir / EACH_STEP_SUMMARY_FILE

        if last_summary_path.exists():
            shutil.copy2(last_summary_path, final_summary_path)
            print_info(f"Final summary: {final_summary_path}")
        else:
            summary_text = (
                f"# Triton-Ascend Upstream Sync\n\n"
                f"- **Target**: `{self.state.target_commit[:12]}`\n"
                f"- **Work branch**: `{self.state.work_branch}`\n"
                f"- **Status**: Success\n"
                f"- **Upstream commits merged**: {self.state.upstream_commits_count}\n"
                f"- **Merge conflicts**: {len(self.state.conflict_files) > 0}\n"
                f"- **Retries needed**: {self.state.retry_count}\n"
                f"- **Date**: {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
            )
            final_summary_path.write_text(summary_text, encoding="utf-8")
            print_info(f"Generated summary: {final_summary_path}")

        # ── Generate cumulative patch ──
        try:
            patch = run_git(ascend_path, "diff", self.state.ascend_head, "HEAD")
            patch_path = WORKSPACE_DIR / FINAL_TARGET_PATCH_FILE
            patch_path.write_text(patch, encoding="utf-8")
            print_info(f"Cumulative patch: {patch_path} ({len(patch)} bytes)")
        except Exception as e:
            print_warn(f"Could not generate final patch: {e}")

        self.state.summary_rows.append(("Finalize", "PASS", "Summary and patch generated"))

        # restore original branch (keep work branch for inspection)
        print_section("Restore Original Branch")
        try:
            run_git(ascend_path, "checkout", self.state.original_branch)
            print_status(True, f"Restored to '{self.state.original_branch}'")
        except Exception as e:
            print_warn(f"Could not restore branch: {e}")
            print_info(f"Work branch '{self.state.work_branch}' left checked out")

        # ── Print final summary table ──
        print_header("Sync Complete — Success!")
        print_elapsed_total()
        self.state.summary_rows.append(("OVERALL", "PASS", "Upgrade completed"))
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
        """push work branch & create GitHub PR (optional, gated by PUSH_TO_GITHUB env)."""
        if os.getenv("PUSH_TO_GITHUB", "false").lower() != "true":
            print_info("PUSH_TO_GITHUB is not 'true' — skipping PR creation")
            print_info("To push manually:")
            print_info(f"  cd {self.state.triton_ascend_path}")
            print_info(f"  git checkout {self.state.work_branch}")
            print_info(f"  git push -u origin {self.state.work_branch}")
            self.state.summary_rows.append(("Push & PR", "SKIP", "PUSH_TO_GITHUB not set"))
            return "SKIP_PUSH"

        print_section("Push to GitHub & Create PR")
        github_repo = os.getenv("GITHUB_REPO", "triton-lang/triton-ascend")
        if not github_repo:
            print_error("GITHUB_REPO is empty — cannot create PR")
            self.state.summary_rows.append(("Push & PR", "FAIL", "GITHUB_REPO empty"))
            return "SKIP_PUSH"

        try:
            pr_url = push_and_create_pr(
                ascend_path=Path(self.state.triton_ascend_path),
                github_repo=github_repo,
                work_branch=self.state.work_branch,
                summary_path=WORKSPACE_DIR / FINAL_SUMMARY_FILE,
            )
            self.state.pr_url = pr_url
            print_status(True, f"PR created: {pr_url}")
            self.state.summary_rows.append(("Push & PR", "PASS", pr_url))
            return pr_url
        except Exception as e:
            print_error(f"Failed to create PR: {e}")
            self.state.summary_rows.append(("Push & PR", "FAIL", str(e)[:60]))
            return "SKIP_PUSH"

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
