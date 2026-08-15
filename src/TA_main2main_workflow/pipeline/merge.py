"""Pipeline step: Execute git merge of upstream commits into triton-ascend.

For the first step, creates a work branch.  Subsequent steps merge
incrementally on the same work branch.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from TA_main2main_workflow.utils.config import TAConfig
from TA_main2main_workflow.utils.context import WorkflowContext
from TA_main2main_workflow.utils.logging import get_logger
from TA_main2main_workflow.utils.git import run_git, run_git_no_check
from TA_main2main_workflow.utils import (
    WORKSPACE_DIR,
    STEPS_DIR,
    get_base_branch_ref,
)

log = get_logger(__name__)

_MERGE_RESULT = "merge_result.json"


def merge_upstream_commit(ctx: WorkflowContext, config: TAConfig) -> WorkflowContext:
    """Merge this step's upstream commits.

    On the first step (current_step == 0): creates a new work branch.
    On subsequent steps: merges incrementally on the existing work branch.
    """
    ascend_path = Path(ctx.triton_ascend_path)
    step = (
        ctx.steps[ctx.current_step]
        if ctx.steps
        else {"id": "step-0", "end_commit": ctx.target_commit}
    )
    step_id = step.get("id", "step-0")
    step_dir = WORKSPACE_DIR / STEPS_DIR / step_id
    step_dir.mkdir(parents=True, exist_ok=True)
    result_file = step_dir / _MERGE_RESULT

    # Resume: skip if merge_result.json already exists for this step
    if config.resume and result_file.exists():
        log.info(f"Resume: {_MERGE_RESULT} exists, skipping merge")
        mr = json.loads(result_file.read_text(encoding="utf-8"))
        return ctx.copy_with(
            merge_has_conflicts=mr.get("has_conflicts", False),
            conflict_files=mr.get("conflict_files", []),
        )

    # ── Step 0: Create work branch ──────────────────────────────────
    if ctx.current_step == 0:
        _create_work_branch(ascend_path, config)
        work_branch = run_git(ascend_path, "branch", "--show-current").strip()
        ctx = ctx.copy_with(work_branch=work_branch)
        log.info(f"Work branch: {work_branch}")
    else:
        # Ensure we're on the work branch
        current_branch = run_git(ascend_path, "branch", "--show-current").strip()
        if ctx.work_branch and current_branch != ctx.work_branch:
            log.warning(
                f"Expected '{ctx.work_branch}' but on '{current_branch}' — switching"
            )
            run_git(ascend_path, "checkout", ctx.work_branch)

    # ── Do the merge ────────────────────────────────────────────────
    # ta_main mode: resolve text conflicts in favor of the incoming
    # TA main code (the merged-in side) via -X theirs.  Remaining
    # conflicts (e.g. modify/delete) are handled by the AI resolve step.
    log.info(f"Merging {step['end_commit'][:12]} ...")
    if ctx.merge_mode == "ta_main":
        merge_proc = run_git_no_check(
            ascend_path,
            "merge",
            "-X",
            "theirs",
            "--no-ff",
            "--no-edit",
            step["end_commit"],
        )
    else:
        merge_proc = run_git_no_check(
            ascend_path, "merge", "--no-ff", "--no-edit", step["end_commit"]
        )

    conflict_raw = run_git(
        ascend_path, "diff", "--name-only", "--diff-filter=U"
    ).strip()
    conflict_files: list[str] = (
        [f for f in conflict_raw.splitlines() if f] if conflict_raw else []
    )
    has_conflicts = len(conflict_files) > 0

    result = {
        "target_commit": step["end_commit"],
        "merge_exit_code": merge_proc.returncode,
        "has_conflicts": has_conflicts,
        "conflict_files": conflict_files,
    }
    result_file.write_text(
        json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )

    if has_conflicts:
        log.conflict_list(conflict_files)
    elif merge_proc.returncode != 0:
        log.warning(
            f"Merge exited with code {merge_proc.returncode} "
            f"but no conflict markers found — continuing"
        )
    else:
        log.key_value("merge exit code", str(merge_proc.returncode))
        log.key_value("conflict files", "0")

    return ctx.copy_with(
        merge_has_conflicts=has_conflicts, conflict_files=conflict_files
    )


def _create_work_branch(repo: Path, config: TAConfig) -> None:
    """Create a work branch from the configured base ref.

    Cleans the working tree before branching: aborts stale merges,
    resets to a pristine state, then creates the work branch.
    """
    base_ref = get_base_branch_ref(config.work_branch_base)
    timestamp = time.strftime("%Y%m%d-%H%M%S")
    branch_name = f"sync/main2main-{timestamp}"

    # ── 1. Abort any stale merge ────────────────────────────────────
    if (repo / ".git" / "MERGE_HEAD").exists():
        log.warning("Stale merge in progress — aborting")
        try:
            run_git(repo, "merge", "--abort")
        except Exception:
            run_git(repo, "reset", "--hard", "HEAD")
        # Clean up leftover merge files
        for fname in ("MERGE_MODE", "MERGE_MSG", "CHERRY_PICK_HEAD"):
            p = repo / ".git" / fname
            if p.exists():
                p.unlink()

    # ── 2. Reset to pristine state ─────────────────────────────────
    try:
        run_git(repo, "checkout", "--detach")
    except Exception:
        pass
    run_git(repo, "reset", "--hard", "HEAD")
    run_git(repo, "clean", "-fd")

    # ── 3. Fetch the work branch base remote ────────────────────────
    try:
        run_git(repo, "fetch", config.work_branch_base)
    except Exception:
        log.warning(
            f"Could not fetch remote '{config.work_branch_base}' — using origin"
        )

    # ── 4. Checkout base ref and create work branch ────────────────
    try:
        run_git(repo, "checkout", "-B", branch_name, base_ref)
    except Exception:
        # Fallback: use origin/base_branch
        fallback = f"origin/{config.base_branch}"
        log.warning(f"Could not use {base_ref}, falling back to {fallback}")
        run_git(repo, "checkout", "-B", branch_name, fallback)

    log.info(f"Created work branch: {branch_name}")
