"""Pipeline step: keep setup_ascend.py in sync with setup.py.

triton-ascend builds via ``python setup_ascend.py install`` — the
Ascend fork of setup.py — so upstream changes merged into setup.py
must be ported into setup_ascend.py while preserving its
Ascend-specific parts.  setup_ascend.py may also be modified later
during build-error fixing (see the allowed prefixes in fix.py).
"""

from __future__ import annotations

from pathlib import Path

from TA_main2main_workflow.agent.opencode_adapter import run_opencode_adapter
from TA_main2main_workflow.utils.config import TAConfig
from TA_main2main_workflow.utils.context import WorkflowContext
from TA_main2main_workflow.utils.logging import get_logger
from TA_main2main_workflow.utils.git import run_git, run_git_no_check
from TA_main2main_workflow.utils import STEPS_DIR, WORKSPACE_DIR

log = get_logger(__name__)

SETUP_FILE = "setup.py"
SETUP_ASCEND_FILE = "setup_ascend.py"

# Max AI sync attempts (same spirit as the patch adjustment loop).
_MAX_SYNC_ATTEMPTS = 3


def sync_setup_ascend(ctx: WorkflowContext, config: TAConfig) -> WorkflowContext:
    """Port this step's upstream setup.py changes into setup_ascend.py.

    No-op when setup_ascend.py does not exist or setup.py was not
    changed by this step's merge.  The AI is allowed to edit ONLY
    setup_ascend.py; any other modification is reverted.  Sync edits
    stay in the working tree and are committed with the step.
    """
    ascend_path = Path(ctx.triton_ascend_path)
    if not (ascend_path / SETUP_ASCEND_FILE).exists():
        log.info("setup_ascend.py not present — nothing to sync")
        return ctx

    # Did this step's merge (including AI conflict resolution, already
    # committed) change setup.py at all?
    if ctx.step_start_ascend_head:
        changed = run_git(
            ascend_path,
            "diff",
            "--name-only",
            ctx.step_start_ascend_head,
            "HEAD",
            "--",
            SETUP_FILE,
        ).strip()
        if not changed:
            log.info(f"{SETUP_FILE} unchanged this step — sync not needed")
            return ctx

    step = ctx.steps[ctx.current_step] if ctx.current_step < len(ctx.steps) else None
    if not step:
        return ctx
    step_id = step["id"]
    step_dir = WORKSPACE_DIR / STEPS_DIR / step_id
    step_dir.mkdir(parents=True, exist_ok=True)

    # Upstream-range diff of setup.py — the AI's reference for what to
    # port into setup_ascend.py.
    setup_diff = run_git(
        ascend_path,
        "diff",
        step.get("start_commit", ""),
        step.get("end_commit", ""),
        "--",
        SETUP_FILE,
    ).strip()
    if not setup_diff:
        log.info(f"{SETUP_FILE} has no upstream diff this step — sync not needed")
        return ctx
    diff_path = step_dir / "setup_diff.txt"
    diff_path.write_text(setup_diff + "\n", encoding="utf-8")

    if config.skip_ai_analysis:
        log.warning("SKIP_AI_ANALYSIS=true — skipping setup_ascend.py sync")
        return ctx

    log.section("Sync setup_ascend.py with setup.py")
    for attempt in range(1, _MAX_SYNC_ATTEMPTS + 1):
        log.step(attempt, _MAX_SYNC_ATTEMPTS, "AI setup_ascend.py sync")
        fix_dir = WORKSPACE_DIR / "fixes" / f"{step_id}-setup-sync-{attempt}"
        fix_dir.mkdir(parents=True, exist_ok=True)
        try:
            result = run_opencode_adapter(
                {
                    "step_id": f"{step_id}-setup-sync-{attempt}",
                    "previous_step_id": "",
                    "previous_step_summary_path": "",
                    "is_last_step": "true",
                    "step_dir": str(step_dir),
                    "fix_dir": str(fix_dir),
                    "conflict_dir": "",
                    "ascend_path": str(ascend_path),
                    "triton_path": ctx.triton_ascend_path,
                    "reference_dir": "",
                    "mode": "setup_sync",
                    "merge_mode": ctx.merge_mode,
                    "error_logs": str(diff_path),
                    "target_commit": ctx.target_commit,
                    "step_index": f"{ctx.current_step + 1}/{ctx.total_steps}",
                }
            )
            log.ai_result(
                bool(result.modified_files),
                result.modified_files,
                (result.step_summary or "")[:500],
            )
        except Exception as exc:
            log.error(f"AI setup_ascend.py sync failed: {exc}")
            continue

        modified = _diff_names(ascend_path)
        illegal = sorted(f for f in modified if f != SETUP_ASCEND_FILE)
        if illegal:
            log.warning(
                f"AI modified disallowed files during setup sync — reverting: {illegal}"
            )
            run_git_no_check(ascend_path, "checkout", "--", *illegal)
        if SETUP_ASCEND_FILE in modified:
            log.status(True, f"{SETUP_ASCEND_FILE} synced (attempt {attempt})")
            return ctx

    log.warning(
        f"{SETUP_ASCEND_FILE} was not updated after {_MAX_SYNC_ATTEMPTS} "
        "attempts — the build may fail; the build-fix loop can repair it"
    )
    return ctx


def _diff_names(ascend_path: Path) -> set[str]:
    """Repo-root-relative paths with uncommitted changes."""
    raw = run_git(ascend_path, "diff", "--name-only").strip()
    return {f for f in raw.splitlines() if f}
