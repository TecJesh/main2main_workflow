"""Shared constants, git helpers, and console output formatting for TA_main2main_workflow."""

import os
import shutil
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Any

# ── Flow routing signals ─────────────────────────────────────────────────────
UpgradeCompleted = "UpgradeCompleted"
UpgradeFailed = "UpgradeFailed"
HasNewCommits = "HasNewCommits"
HasNoNewCommits = "HasNoNewCommits"
MergeSuccess = "MergeSuccess"
MergeConflict = "MergeConflict"
TestsPassed = "TestsPassed"
TestsFailed = "TestsFailed"

# ── Workspace paths ──────────────────────────────────────────────────────────
_PACKAGE_DIR = Path(__file__).resolve().parent  # TA_main2main_workflow package dir
_WORKSPACE_DEFAULT = _PACKAGE_DIR / "workspace"
WORKSPACE_DIR = Path(os.getenv("TA_MAIN2MAIN_WORKSPACE", str(_WORKSPACE_DEFAULT)))
REPOS_DIR_NAME = "repos"
TRITON_REPO_NAME = "triton"
TRITON_ASCEND_REPO_NAME = "triton-ascend"

# ── Step-planning constants ──────────────────────────────────────────────────
LINE_BUDGET = 1000
BASE_LINE_BUDGET = 1000
BASE_COMMIT_COUNT_BUDGET = 5      # max commits per step (overridable via TA_COMMIT_BUDGET)
# Directories in upstream triton whose changed lines count toward the budget
SOURCE_DIRS = ["python/triton/", "lib/", "include/"]
# Env var to control the line budget at runtime
ENV_LINE_BUDGET = "TA_LINE_BUDGET"
# Env var to control the commit-count budget at runtime
ENV_COMMIT_BUDGET = "TA_COMMIT_BUDGET"

# ── Output file names ────────────────────────────────────────────────────────
DETECT_FILE = "detect.json"
STEPS_FILE = "steps.json"
MERGE_LOG_FILE = "merge.log"
MERGE_RESULT_FILE = "merge_result.json"
BUILD_LOG_FILE = "build.log"
BUILD_RESULT_FILE = "build_result.json"
TEST_RESULT_FILE = "test_result.json"
CONFLICT_LOG_DIR = "conflicts"
FIX_LOG_DIR = "fixes"
STEPS_DIR = "steps"
FINAL_SUMMARY_FILE = "final_summary.md"
FINAL_TARGET_PATCH_FILE = "final_target.patch"
EACH_STEP_SUMMARY_FILE = "step_summary.md"
EACH_STEP_TARGET_PATCH_FILE = "step_target.patch"
PRE_CI_CHECK_FILE = "pre_ci_check.json"
CODE_STRUCTURE_GUIDE_FILE = "code-structure-guide.md"

# ── Timing tracker ───────────────────────────────────────────────────────────
_phase_timers: dict[str, float] = {}
_flow_start_time: float = 0.0


def commit_count_budget(line_budget: int = LINE_BUDGET) -> int:
    """Compute the max commits per step from the line budget.

    Uses the same formula as vllm-ascend's main2main_flow:
    max(1, round(BASE_COMMIT_COUNT_BUDGET * sqrt(line_budget / BASE_LINE_BUDGET)))

    The base commit count can be overridden at runtime via TA_COMMIT_BUDGET
    env var (e.g., TA_COMMIT_BUDGET=3 for finer granularity).
    """
    import math
    import os
    base = int(os.getenv(ENV_COMMIT_BUDGET, str(BASE_COMMIT_COUNT_BUDGET)))
    return max(1, round(base * math.sqrt(line_budget / BASE_LINE_BUDGET)))


def _ts() -> str:
    return datetime.now().strftime("%H:%M:%S")


# ═══════════════════════════════════════════════════════════════════════════════
# Console Output Helpers — all progress printed locally, no CrewAI web UI needed
# ═══════════════════════════════════════════════════════════════════════════════

def print_header(title: str) -> None:
    width = 72
    print(f"\n╔{'═' * width}╗", flush=True)
    print(f"║ {title:^{width}} ║", flush=True)
    print(f"╚{'═' * width}╝", flush=True)


def print_section(title: str) -> None:
    print(f"\n{'─' * 60}", flush=True)
    print(f"  [{_ts()}] {title}", flush=True)
    print(f"{'─' * 60}", flush=True)


def print_step(step_num: int, total: int, name: str) -> None:
    print(f"\n  ▸ [{step_num}/{total}] {name}  @ {_ts()}", flush=True)


def print_status(ok: bool, msg: str) -> None:
    icon = "✔" if ok else "✘"
    print(f"    {icon} {msg}", flush=True)


def print_info(msg: str) -> None:
    print(f"    ℹ {msg}", flush=True)


def print_warn(msg: str) -> None:
    print(f"    ⚠ {msg}", flush=True)


def print_error(msg: str) -> None:
    print(f"    ✘ {msg}", flush=True)


def print_key_value(key: str, value: Any) -> None:
    print(f"    {key}: {value}", flush=True)


def print_separator() -> None:
    print(f"  {'─' * 56}", flush=True)


def print_flow_progress(phase: str, detail: str = "") -> None:
    msg = f"[{_ts()}] [{phase}] {detail}" if detail else f"[{_ts()}] [{phase}]"
    print(msg, flush=True)


def start_timer(name: str) -> None:
    global _flow_start_time
    _phase_timers[name] = time.monotonic()
    if not _flow_start_time:
        _flow_start_time = time.monotonic()


def stop_timer(name: str) -> float:
    start = _phase_timers.pop(name, None)
    if start is None:
        return 0.0
    elapsed = time.monotonic() - start
    print(f"    ⏱  {name} took {elapsed:.1f}s", flush=True)
    return elapsed


def print_elapsed_total() -> None:
    if _flow_start_time:
        total = time.monotonic() - _flow_start_time
        print(f"\n  ⏱  Total elapsed: {total:.1f}s ({total/60:.1f}m)", flush=True)


def print_summary_table(rows: list[tuple[str, str, str]]) -> None:
    status_icons = {"PASS": "✔", "FAIL": "✘", "SKIP": "○", "WARN": "⚠"}
    print(f"\n{'═' * 72}", flush=True)
    print(f"  SYNC SUMMARY  @ {_ts()}", flush=True)
    print(f"{'═' * 72}", flush=True)
    print(f"  {'Phase':<30} {'Status':<8} {'Details'}", flush=True)
    print(f"  {'─' * 30} {'─' * 8} {'─' * 32}", flush=True)
    for step, status, detail in rows:
        icon = status_icons.get(status, "?")
        print(f"  {step:<30} {icon} {status:<5} {detail}", flush=True)
    print(f"{'═' * 72}", flush=True)


def print_conflict_list(files: list[str]) -> None:
    if not files:
        print_info("No conflicts")
        return
    print(f"    Conflicted files ({len(files)}):")
    for i, f in enumerate(files, 1):
        print(f"      {i}. {f}")


def print_ai_call_info(backend: str, mode: str, attempt: int, max_attempts: int) -> None:
    print(f"\n  ╭─ AI Call ─────────────────────────────────────────────", flush=True)
    print(f"  │ Backend:  {backend}", flush=True)
    print(f"  │ Mode:     {mode}", flush=True)
    print(f"  │ Attempt:  {attempt}/{max_attempts}", flush=True)
    print(f"  │ Time:     {_ts()}", flush=True)
    print(f"  ╰──────────────────────────────────────────────────────", flush=True)


def print_ai_result(ok: bool, modified_files: list[str] = (), summary: str = "") -> None:
    icon = "✔" if ok else "✘"
    print(f"\n  ╭─ AI Result ───────────────────────────────────────────", flush=True)
    print(f"  │ Status: {icon} {'Success' if ok else 'Failed'}", flush=True)
    if modified_files:
        print(f"  │ Modified files ({len(modified_files)}):", flush=True)
        for f in modified_files:
            print(f"  │   • {f}", flush=True)
    if summary:
        preview = summary[:500] + "..." if len(summary) > 500 else summary
        print(f"  │ Summary: {preview}", flush=True)
    print(f"  ╰──────────────────────────────────────────────────────", flush=True)


# ═══════════════════════════════════════════════════════════════════════════════
# Git helpers
# ═══════════════════════════════════════════════════════════════════════════════

def run_git(repo: Path | str, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=str(repo),
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout


def run_git_no_check(repo: Path | str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=str(repo),
        capture_output=True,
        text=True,
    )


def is_git_url(path: str) -> bool:
    return path.startswith(("https://", "http://", "git@"))


def clone_repo(url: str, target: str) -> None:
    print(f"[init] Cloning {url} → {target}")
    subprocess.run(["git", "clone", url, target], check=True)


def resolve_path(raw: str, name: str) -> str:
    if is_git_url(raw):
        target = WORKSPACE_DIR / REPOS_DIR_NAME / name
        if target.exists():
            shutil.rmtree(target)
        target.mkdir(parents=True, exist_ok=True)
        clone_repo(raw, str(target))
        return str(target)
    return raw


def get_repo_head(repo: Path) -> str:
    if not repo.exists():
        raise FileNotFoundError(f"Repository path does not exist: {repo}")
    return run_git(repo, "rev-parse", "HEAD").strip()


def get_merge_base(repo: Path, commit_a: str, commit_b: str) -> str:
    return run_git(repo, "merge-base", commit_a, commit_b).strip()


def has_merge_conflicts(repo: Path) -> bool:
    result = run_git_no_check(repo, "diff", "--name-only", "--diff-filter=U")
    return bool(result.stdout.strip())


def get_conflict_files(repo: Path) -> list[str]:
    result = run_git(repo, "diff", "--name-only", "--diff-filter=U")
    return [f for f in result.strip().splitlines() if f]


def get_modified_files(repo: Path, base_ref: str = "HEAD") -> list[str]:
    result = run_git(repo, "diff", "--name-only", base_ref)
    return [f for f in result.strip().splitlines() if f]


def get_unstaged_diff(repo: Path) -> str:
    return run_git(repo, "diff")


def get_staged_diff(repo: Path) -> str:
    return run_git(repo, "diff", "--cached")
