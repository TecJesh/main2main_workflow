"""Pipeline step: Commit step progress and AI fix changes.

Handles submodule commit first (AscendNPU-IR), then parent repo commit.
Fix commits stage through a whitelist produced by the AI (commit_plan
mode) so patch-applied source changes never enter commits.
"""

from __future__ import annotations

import json
from pathlib import Path

from TA_main2main_workflow.agent.opencode_adapter import run_opencode_adapter
from TA_main2main_workflow.utils.config import TAConfig
from TA_main2main_workflow.utils.context import WorkflowContext
from TA_main2main_workflow.utils.logging import get_logger
from TA_main2main_workflow.utils.git import run_git, run_git_no_check
from TA_main2main_workflow.utils.submodule import (
    SUBMODULE_DIR,
    commit_submodule,
    submodule_has_changes,
)
from TA_main2main_workflow.utils import STEPS_DIR, WORKSPACE_DIR
from TA_main2main_workflow.pipeline.pre_ci import cleanup_temp_files
from TA_main2main_workflow.pipeline.ta_patch import exclude_patch_files_from_index

log = get_logger(__name__)


def commit_step(ctx: WorkflowContext, config: TAConfig) -> WorkflowContext:
    """Commit all remaining changes for the current step.

    Called after ``restore_workspace`` so the tree holds no patch-applied
    content.  Order:

    1. Commit AscendNPU-IR submodule if it has changes (a merge may have
       moved the submodule pointer; patch-applied dirt was restored)
    2. Clean temp files
    3. Stage and commit parent repo (AI whitelist when available, else
       ``git add -A`` with patch-touched exclusion as defense)
    """
    ascend_path = Path(ctx.triton_ascend_path)
    step = ctx.steps[ctx.current_step]
    step_id = step["id"]

    # ── 1. Submodule first ────────────────────────────────────────────
    if submodule_has_changes(ascend_path):
        target_short = ctx.target_commit[:12] if ctx.target_commit else "HEAD"
        commit_submodule(ascend_path, f"[Sync](fix) AI fix for {target_short}\n")

    # ── 2. Clean temp files ───────────────────────────────────────────
    cleanup_temp_files(ascend_path)

    # ── 3. Stage and commit parent repo ───────────────────────────────
    staged = run_git(ascend_path, "status", "--porcelain").strip()
    if not staged:
        log.info(f"[{step_id}] Nothing to commit")
        return ctx

    # Print staged files for visibility
    staged_files = [line[3:] for line in staged.splitlines() if line.strip()]
    log.info(f"Files staged ({len(staged_files)}):")
    for f in staged_files[:30]:
        log.info(f"  {f}")
    if len(staged_files) > 30:
        log.info(f"  ... and {len(staged_files) - 30} more")

    start_short = step.get("start_commit", "?")[:12]
    end_short = step["end_commit"][:12]
    msg = (
        f"[Sync](feat) Merge upstream commits for step {step_id}"
        f"({start_short}..{end_short}, {step['commit_count']} commits)\n\n"
        f"Upstream range: {start_short}..{end_short}\n"
        f"Step: {ctx.current_step + 1}/{ctx.total_steps}\n"
        f"Commits: {step['commit_count']}\n"
    )
    try:
        step_dir = WORKSPACE_DIR / STEPS_DIR / step_id
        # No whitelist here: commit_files.txt belongs to a previous fix
        # commit, not to the step commit.
        stage_changes(
            ascend_path,
            step_dir,
            ctx.ta_patch_touched_files,
            ctx.ta_patch_submodule_files,
            use_whitelist=False,
        )
        if not run_git(ascend_path, "diff", "--cached", "--name-only").strip():
            log.info(f"[{step_id}] Nothing to commit after staging")
            return ctx
        run_git(ascend_path, "commit", "-s", "-m", msg)
        log.status(True, f"Committed step {step_id}")
    except Exception as e:
        if "nothing to commit" not in str(getattr(e, "stderr", "")):
            log.warning(f"Commit failed: {e}")

    return ctx


def run_commit_plan(ctx: WorkflowContext, config: TAConfig) -> None:
    """Ask the AI to analyze the working tree and produce, in *step_dir*:

    - ``commit_files.txt`` — one repo-root relative path per line
      (the whitelist of files to commit)
    - ``commit_message.txt`` — one-line commit subject

    The patch-touched file lists are passed in so the AI can exclude
    them.  Best-effort: any failure falls back to ``git add -A`` +
    exclusion staging and the default commit subject.
    """
    if config.skip_ai_analysis:
        log.info("SKIP_AI_ANALYSIS=true — skipping commit_plan")
        return

    ascend_path = Path(ctx.triton_ascend_path)
    step = ctx.steps[ctx.current_step] if ctx.current_step < len(ctx.steps) else None
    step_id = step["id"] if step else "step-0"
    step_dir = WORKSPACE_DIR / STEPS_DIR / step_id
    step_dir.mkdir(parents=True, exist_ok=True)

    status_summary = run_git(ascend_path, "status", "--porcelain").strip()
    if not status_summary:
        return
    diff_summary = run_git_no_check(
        ascend_path, "diff", "--stat"
    ).stdout.strip()[-6000:]

    fix_dir = WORKSPACE_DIR / "fixes" / f"{step_id}-commit-plan"
    fix_dir.mkdir(parents=True, exist_ok=True)
    try:
        run_opencode_adapter(
            {
                "step_id": f"{step_id}-commit-plan",
                "previous_step_id": "",
                "previous_step_summary_path": "",
                "is_last_step": "true",
                "step_dir": str(step_dir),
                "fix_dir": str(fix_dir),
                "conflict_dir": "",
                "ascend_path": str(ascend_path),
                "triton_path": ctx.triton_ascend_path,
                "reference_dir": "",
                "mode": "commit_plan",
                "error_logs": json.dumps(
                    {
                        "status": status_summary,
                        "diff_stat": diff_summary,
                        "patch_touched_parent": ctx.ta_patch_touched_files,
                        "patch_touched_submodule": ctx.ta_patch_submodule_files,
                    },
                    ensure_ascii=False,
                ),
                "target_commit": ctx.target_commit,
                "step_index": f"{ctx.current_step + 1}/{ctx.total_steps}",
                "ascend_npu_ir_fix": "false",
                "ascend_npu_ir_compat_ref": "",
            }
        )
    except Exception as e:
        log.warning(f"commit_plan AI call failed: {e}")


def stage_changes(
    ascend_path: Path,
    step_dir: Path,
    touched_parent: list[str],
    touched_submodule: list[str],
    use_whitelist: bool = True,
) -> bool:
    """Stage working-tree changes for a commit.

    Whitelist first: the AI (commit_plan mode) may have written
    ``commit_files.txt`` into *step_dir*.  Staged files are hard-gated
    against the patch-touched lists — patch-applied source changes
    never enter a commit, even if the AI listed them by mistake.

    Falls back to ``git add -A`` + exclusion when no whitelist exists.

    *use_whitelist* must be False for the step commit: a whitelist
    written by an earlier fix commit describes different files and
    would wrongly limit what gets committed.

    Returns True when anything is staged.
    """
    touched_set = set(touched_parent)

    whitelist = _read_commit_whitelist(step_dir) if use_whitelist else []
    if whitelist:
        filtered = [
            f for f in whitelist if f not in touched_set and f != SUBMODULE_DIR
        ]
        if filtered:
            run_git(ascend_path, "add", "--", *filtered)
            staged = set(
                run_git(ascend_path, "diff", "--cached", "--name-only").split()
            )
            banned = staged & touched_set
            if banned:
                run_git_no_check(
                    ascend_path, "restore", "--staged", "--", *sorted(banned)
                )
                log.warning(
                    f"Dropped patch-touched file(s) from whitelist: {sorted(banned)}"
                )
        return bool(
            run_git(ascend_path, "diff", "--cached", "--name-only").strip()
        )

    # Fallback: stage everything, then unstage patch-touched changes.
    run_git(ascend_path, "add", "-A")
    exclude_patch_files_from_index(
        ascend_path, touched_parent, touched_submodule
    )
    return bool(run_git(ascend_path, "diff", "--cached", "--name-only").strip())


def _read_commit_whitelist(step_dir: Path) -> list[str]:
    """Parse ``commit_files.txt`` (one repo-root relative path per line)."""
    whitelist_file = step_dir / "commit_files.txt"
    if not whitelist_file.exists():
        return []
    return [
        ln.strip()
        for ln in whitelist_file.read_text(encoding="utf-8").splitlines()
        if ln.strip()
    ]
