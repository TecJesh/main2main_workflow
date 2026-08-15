"""Pipeline step (ta_main mode): adjust and apply TA source patches.

The TA repo keeps intrusive source modifications as patch files under
``third_party/ascend/patch/`` (``triton-ascend-*.patch``) instead of
committing them directly.  After each TA-main merge, code line numbers
shift and ``git apply`` may fail.  This module:

1. Checks each patch with ``git apply --check``
2. On failure, asks the AI (mode ``patch_fix``) to adjust the hunk
   positions in the patch file WITHOUT changing its semantics
3. Applies the patches to the working tree so build/test run on the
   patched code
4. Provides helpers to exclude the patched source files from commits
   and to revert them after the step, keeping the committed tree clean
"""

from __future__ import annotations

import json
from pathlib import Path

from TA_main2main_workflow.agent.opencode_adapter import run_opencode_adapter
from TA_main2main_workflow.utils.config import TAConfig
from TA_main2main_workflow.utils.context import WorkflowContext
from TA_main2main_workflow.utils.logging import get_logger
from TA_main2main_workflow.utils.git import run_git, run_git_no_check
from TA_main2main_workflow.utils import STEPS_DIR, WORKSPACE_DIR

log = get_logger(__name__)

# Patch files to maintain, relative to the TA repo root.
# llvm_patch_*.patch files are LLVM-project patches handled by build.py —
# they are NOT managed here.
_TA_PATCH_GLOB = "triton-ascend-*.patch"
_TA_PATCH_DIR = "third_party/ascend/patch"

# Max AI adjustment attempts per patch file before giving up.
_MAX_ADJUST_RETRIES = 3


# ═══════════════════════════════════════════════════════════════════════════
# Public API
# ═══════════════════════════════════════════════════════════════════════════


def adjust_and_apply_patches(
    ctx: WorkflowContext, config: TAConfig
) -> WorkflowContext:
    """Check/adjust/apply the TA source patches. ta_main mode only.

    Called after merge/resolve and before build.  Returns an updated
    context carrying the applied patch files and the source files they
    touched.
    """
    if ctx.merge_mode != "ta_main":
        return ctx

    ascend_path = Path(ctx.triton_ascend_path)
    patch_dir = ascend_path / _TA_PATCH_DIR
    patch_files = sorted(patch_dir.glob(_TA_PATCH_GLOB)) if patch_dir.exists() else []

    if not patch_files:
        log.info(
            f"No '{_TA_PATCH_GLOB}' files found under {_TA_PATCH_DIR} — "
            "nothing to apply"
        )
        return ctx

    log.section(f"TA Source Patches ({len(patch_files)} file(s))")

    # ── Phase 1: verify / adjust each patch so `git apply --check` passes ──
    for patch_file in patch_files:
        log.key_value("patch", str(patch_file))
        if _check_applies(ascend_path, patch_file):
            log.status(True, f"{patch_file.name} applies cleanly")
            continue
        _adjust_patch_with_ai(ctx, config, ascend_path, patch_file)

    # ── Phase 2: apply all patches to the working tree ───────────────────
    applied: list[str] = []
    touched: list[str] = []
    all_ok = True
    for patch_file in patch_files:
        if not _check_applies(ascend_path, patch_file):
            log.error(
                f"Patch {patch_file.name} still does not apply after AI "
                "adjustment"
            )
            all_ok = False
            continue
        run_git(ascend_path, "apply", str(patch_file))
        applied.append(str(patch_file))
        touched.extend(_diff_names(ascend_path))
        log.status(True, f"Applied {patch_file.name}")

    if touched:
        # dedupe, keep order
        touched = list(dict.fromkeys(touched))
        log.key_value("patched source files", str(len(touched)))

    # ── Phase 3: clean work area (.orig/.rej/pycache residue) ───────────
    if applied:
        from TA_main2main_workflow.pipeline.pre_ci import cleanup_temp_files

        cleanup_temp_files(ascend_path)

    return ctx.copy_with(
        ta_patch_ok=all_ok,
        ta_patch_applied=applied,
        ta_patch_touched_files=touched,
    )


def exclude_patch_files_from_index(
    ascend_path: Path, touched_files: list[str]
) -> None:
    """Unstage patch-touched source files so commits keep the tree clean.

    The working tree keeps the applied changes (needed for build/test);
    only the index excludes them.
    """
    if not touched_files:
        return
    run_git_no_check(ascend_path, "restore", "--staged", "--", *touched_files)
    log.info(
        f"Excluded {len(touched_files)} patch-touched file(s) from the index"
    )


def revert_applied_patches(ascend_path: Path, touched_files: list[str]) -> None:
    """Revert patch-touched source files in the working tree.

    Called after the step's commit so the next merge starts from a
    clean tree.  Uses ``git checkout`` on the touched files — the
    committed merge result is unaffected.
    """
    if not touched_files:
        return
    run_git_no_check(ascend_path, "checkout", "--", *touched_files)
    log.info(f"Reverted {len(touched_files)} patch-touched file(s) in work tree")


# ═══════════════════════════════════════════════════════════════════════════
# Internal helpers
# ═══════════════════════════════════════════════════════════════════════════


def _check_applies(ascend_path: Path, patch_file: Path) -> bool:
    """Return True if *patch_file* applies cleanly to the current tree."""
    result = run_git_no_check(ascend_path, "apply", "--check", str(patch_file))
    return result.returncode == 0


def _diff_names(ascend_path: Path) -> list[str]:
    """List files with uncommitted changes in the working tree."""
    raw = run_git(ascend_path, "diff", "--name-only").strip()
    return [f for f in raw.splitlines() if f]


def _adjust_patch_with_ai(
    ctx: WorkflowContext,
    config: TAConfig,
    ascend_path: Path,
    patch_file: Path,
) -> None:
    """AI-adjust hunk positions in *patch_file* until `git apply --check` passes.

    The AI is instructed to change ONLY hunk line numbers/context so the
    patch applies to the merged tree — never the semantic content.
    """
    if config.skip_ai_analysis:
        log.warning(
            f"SKIP_AI_ANALYSIS=true — cannot adjust patch {patch_file.name}"
        )
        return

    step = ctx.steps[ctx.current_step] if ctx.current_step < len(ctx.steps) else None
    step_id = step["id"] if step else "step-0"
    step_dir = WORKSPACE_DIR / STEPS_DIR / step_id
    step_dir.mkdir(parents=True, exist_ok=True)

    for attempt in range(1, _MAX_ADJUST_RETRIES + 1):
        check = run_git_no_check(ascend_path, "apply", "--check", str(patch_file))
        if check.returncode == 0:
            log.status(True, f"{patch_file.name} applies after adjustment")
            return

        log.header(
            f"Adjust Patch {patch_file.name} — attempt "
            f"{attempt}/{_MAX_ADJUST_RETRIES}"
        )
        fix_dir = (
            WORKSPACE_DIR / "fixes" / f"{step_id}-patch-{patch_file.stem}-{attempt}"
        )
        fix_dir.mkdir(parents=True, exist_ok=True)
        try:
            result = run_opencode_adapter(
                {
                    "step_id": f"{step_id}-patch-{attempt}",
                    "previous_step_id": "",
                    "previous_step_summary_path": "",
                    "is_last_step": "true",
                    "step_dir": str(step_dir),
                    "fix_dir": str(fix_dir),
                    "conflict_dir": "",
                    "ascend_path": str(ascend_path),
                    "triton_path": ctx.triton_ascend_path,
                    "reference_dir": "",
                    "mode": "patch_fix",
                    "patch_file": str(patch_file),
                    "apply_error": (check.stderr or check.stdout or "")[-4000:],
                    "merge_mode": ctx.merge_mode,
                    "error_logs": json.dumps([str(patch_file)], ensure_ascii=False),
                    "target_commit": ctx.target_commit,
                    "step_index": f"{ctx.current_step + 1}/{ctx.total_steps}",
                    "ascend_npu_ir_fix": "false",
                    "ascend_npu_ir_compat_ref": "",
                }
            )
            log.ai_result(
                bool(result.modified_files),
                result.modified_files,
                (result.step_summary or "")[:500],
            )
        except Exception as exc:
            log.error(f"AI patch adjustment failed: {exc}")

    log.error(
        f"Patch {patch_file.name} could not be adjusted after "
        f"{_MAX_ADJUST_RETRIES} attempts"
    )
