"""Pipeline step: manage TA source patches (the build auto-applies them).

The TA repo keeps intrusive source modifications as patch files under
``third_party/ascend/patch/`` and ``setup.py`` applies them at build
time (checking each target file out to HEAD first).  After every merge
the code line positions drift, so this module:

1. Adjusts each patch via AI (``patch_fix`` mode) until
   ``git apply --check`` passes, then commits that adjustment
   immediately (before build).
2. Parses patch headers statically to derive the files each patch
   touches — the workflow never applies patches itself (the build
   does; re-applying here would conflict with it).
3. Regenerates patch content from working-tree diffs after AI fixes
   (fix-then-regenerate: the AI fixes source code, the fix flows back
   into the ``.patch`` file, and the touched source is restored).
4. Restores touched source files to HEAD so commits stay clean.

The npuir patch is special: its internal paths are relative to the
AscendNPU-IR submodule and setup.py applies it with ``--directory``
from the repo root.  All git operations here mirror that.

Works in ALL merge modes — the build applies the patches regardless
of ``TA_MERGE_MODE``.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from TA_main2main_workflow.agent.opencode_adapter import run_opencode_adapter
from TA_main2main_workflow.utils.config import TAConfig
from TA_main2main_workflow.utils.context import WorkflowContext
from TA_main2main_workflow.utils.logging import get_logger
from TA_main2main_workflow.utils.git import run_git, run_git_no_check
from TA_main2main_workflow.utils.submodule import SUBMODULE_DIR
from TA_main2main_workflow.utils import STEPS_DIR, WORKSPACE_DIR

log = get_logger(__name__)

# Patch files to maintain, relative to the TA repo root.
# llvm_patch_*.patch files are LLVM-project patches handled only by the
# IR patch flow (build.py / ir_patch.py) when the LLVM version changes —
# they are NEVER managed here.
_TA_PATCH_DIR = "third_party/ascend/patch"
_TA_PATCH_GLOB = "triton-ascend-*.patch"
_NPUIR_PATCH_PREFIX = "npuir"
_LLVM_PATCH_PREFIX = "llvm_patch_"

# Max AI adjustment attempts per patch file before giving up.
_MAX_ADJUST_RETRIES = 3


@dataclass
class PatchTouchInfo:
    """Files touched by the managed patches, split by repository."""

    parent: list[str] = field(default_factory=list)  # repo-root relative paths
    submodule: list[str] = field(default_factory=list)  # submodule-relative paths


# ═══════════════════════════════════════════════════════════════════════════
# Public API
# ═══════════════════════════════════════════════════════════════════════════


def resolve_source_patches(ascend_path: Path, config: TAConfig) -> list[Path]:
    """Return the managed patch files that exist in the repo.

    Uses the explicit ``TA_SOURCE_PATCHES`` list when available; falls
    back to glob discovery.  Missing files are skipped with a warning —
    a version bump may legitimately leave old names behind.
    """
    patch_dir = ascend_path / _TA_PATCH_DIR
    if not patch_dir.is_dir():
        log.info(f"No '{_TA_PATCH_DIR}' directory — nothing to manage")
        return []

    if config.source_patches:
        patches: list[Path] = []
        for name in config.source_patches:
            p = patch_dir / name
            if not p.is_file():
                log.warning(f"Source patch not found, skipping: {name}")
                continue
            if p.name.startswith(_LLVM_PATCH_PREFIX):
                # LLVM patches are managed exclusively by the IR patch
                # flow (build.py / ir_patch.py) — never here.
                log.warning(
                    f"Skipping LLVM patch in TA_SOURCE_PATCHES: {name}"
                )
                continue
            patches.append(p)
        return patches

    log.warning("TA_SOURCE_PATCHES empty — falling back to glob discovery")
    found = sorted(patch_dir.glob(_TA_PATCH_GLOB))
    found += sorted(patch_dir.glob(f"{_NPUIR_PATCH_PREFIX}_*.patch"))
    return list(dict.fromkeys(found))


def adjust_patches(
    ctx: WorkflowContext,
    config: TAConfig,
    patch_files: list[Path],
) -> WorkflowContext:
    """Check/adjust each patch so ``git apply --check`` passes.

    The workflow does NOT apply the patches — setup.py does at build
    time.  Adjustment edits the patch files in the working tree; the
    caller commits those edits (``commit_patch_adjustments``) right
    after.  Returns the context with ``ta_patch_ok`` and the list of
    ready patches.
    """
    if not patch_files:
        return ctx

    ascend_path = Path(ctx.triton_ascend_path)
    log.section(f"Adjust Source Patches ({len(patch_files)} file(s))")

    all_ok = True
    ready: list[str] = []
    for patch_file in patch_files:
        log.key_value("patch", str(patch_file))
        if _check_applies(ascend_path, patch_file):
            log.status(True, f"{patch_file.name} applies cleanly")
            ready.append(str(patch_file))
            continue
        _adjust_patch_with_ai(ctx, config, ascend_path, patch_file)
        if _check_applies(ascend_path, patch_file):
            log.status(True, f"{patch_file.name} applies after adjustment")
            ready.append(str(patch_file))
        else:
            log.error(
                f"Patch {patch_file.name} still does not apply after "
                "AI adjustment — build will fail to apply it"
            )
            all_ok = False

    return ctx.copy_with(ta_patch_ok=all_ok, ta_patch_applied=ready)


def commit_patch_adjustments(
    ascend_path: Path, step_id: str, target_commit: str
) -> bool:
    """Commit the patch-file adjustments made by ``adjust_patches``.

    Runs right after adjustment succeeds and before build, so the
    rebased patches are locked in even if build/test later fails.
    Only files under the patch dir are staged (hard gate).  Returns
    True when a commit was created.
    """
    patch_dir = ascend_path / _TA_PATCH_DIR
    changed = _diff_names(ascend_path)
    candidates = [
        f
        for f in changed
        if (ascend_path / f).is_file()
        and (ascend_path / f).resolve().is_relative_to(patch_dir.resolve())
        and not Path(f).name.startswith(_LLVM_PATCH_PREFIX)
    ]
    if not candidates:
        log.info("No patch-file adjustments to commit")
        return False

    run_git(ascend_path, "add", "--", *candidates)
    staged = run_git(ascend_path, "diff", "--cached", "--name-only").strip()
    if not staged:
        return False

    target_short = target_commit[:12] if target_commit else "HEAD"
    msg = (
        f"[Sync](fix) Rebase ascend patches onto merged tree ({step_id})\n\n"
        f"Adjusted hunk positions so the patches apply after the merge.\n"
        f"Upstream target: {target_short}\n"
    )
    try:
        run_git(ascend_path, "commit", "-s", "-m", msg)
        log.status(True, f"Committed patch adjustments ({len(candidates)} file(s))")
        return True
    except Exception as e:
        # Non-fatal: commit_step's add -A at step end will pick them up.
        log.warning(f"Could not commit patch adjustments: {e}")
        return False


def parse_patches(patch_files: list[Path]) -> PatchTouchInfo:
    """Statically derive the files each patch touches (never applies).

    npuir patch paths are submodule-relative (setup.py applies them
    with ``--directory``) and are classified as such.
    """
    info = PatchTouchInfo()
    for patch_file in patch_files:
        in_submodule = patch_file.name.startswith(_NPUIR_PATCH_PREFIX)
        for path in _parse_patch_paths(patch_file):
            target = info.submodule if in_submodule else info.parent
            if path not in target:
                target.append(path)
    log.key_value(
        "touched files",
        f"parent={len(info.parent)}, submodule={len(info.submodule)}",
    )
    return info


def regenerate_and_reapply(
    ascend_path: Path,
    patch_files: list[Path],
    touched_parent: list[str],
    touched_submodule: list[str],
    ai_modified_files: list[str],
) -> bool:
    """fix-then-regenerate: flow AI fixes on touched files into patches.

    Only files the AI actually modified (``ai_modified_files``) are
    considered — the working tree also carries patch-applied changes,
    which must NOT be re-generated.  For each such file:

    1. its ``git diff HEAD`` is captured and merged into the owning
       patch file (the diff naturally contains old patch content +
       the AI fix),
    2. touched files are restored to HEAD,
    3. the updated patch set is re-checked and re-applied so build/
       test continue to see the patched code.

    Returns True on success (including the no-op case).  On failure
    the patch-file edits are reverted and the tree is restored.
    """
    if not patch_files:
        return True

    hits_parent = [f for f in touched_parent if f in ai_modified_files]
    sub_prefix = f"{SUBMODULE_DIR}/"
    hits_submodule = [
        f[len(sub_prefix):]
        for f in ai_modified_files
        if f.startswith(sub_prefix) and f[len(sub_prefix):] in touched_submodule
    ]
    if not hits_parent and not hits_submodule:
        return True

    log.section("Regenerate Patches From AI Fixes")
    updated: list[Path] = []
    for patch_file in patch_files:
        in_submodule = patch_file.name.startswith(_NPUIR_PATCH_PREFIX)
        hits = hits_submodule if in_submodule else hits_parent
        if not hits:
            continue
        if in_submodule:
            sub_path = ascend_path / SUBMODULE_DIR
            sections = {
                f: run_git(sub_path, "diff", "HEAD", "--", f) for f in hits
            }
        else:
            sections = {f: run_git(ascend_path, "diff", "HEAD", "--", f) for f in hits}
        _replace_patch_sections(patch_file, sections)
        updated.append(patch_file)
        log.info(
            f"Regenerated {patch_file.name} from {len(hits)} touched file(s)"
        )

    restore_workspace(ascend_path, touched_parent, touched_submodule)
    if not apply_patches(ascend_path, patch_files):
        log.error(
            "Patch check/apply failed after regeneration — discarding the fix"
        )
        revert_patch_files(ascend_path, updated)
        restore_workspace(ascend_path, touched_parent, touched_submodule)
        return False
    log.status(True, f"Regenerated {len(updated)} patch file(s) and re-applied")
    return True


def apply_patches(ascend_path: Path, patch_files: list[Path]) -> bool:
    """``git apply --check`` then ``git apply`` every patch.

    Assumes the touched files are at HEAD (restored) — checking against
    an already-patched tree always fails.  npuir patches are applied
    with ``--directory <submodule>`` exactly like setup.py does.
    """
    for patch_file in patch_files:
        if not _check_applies(ascend_path, patch_file):
            log.error(
                f"Patch {patch_file.name} does not apply cleanly — "
                "cannot (re)apply patch set"
            )
            return False
    for patch_file in patch_files:
        args = ["apply"]
        if patch_file.name.startswith(_NPUIR_PATCH_PREFIX):
            args += ["--directory", SUBMODULE_DIR]
        args.append(str(patch_file))
        result = run_git_no_check(ascend_path, *args)
        if result.returncode != 0:
            log.error(
                f"Failed to apply {patch_file.name}: "
                f"{(result.stderr or '').strip()[-400:]}"
            )
            return False
    return True


def regen_ai_fixes(ctx: WorkflowContext, config: TAConfig) -> None:
    """Flow the latest AI fixes on patch-touched files into the patches.

    No-op when the last ai_fix did not touch any patch-touched file.
    Must run BEFORE the next build — setup.py checks touched files out
    to HEAD before applying patches, which wipes fixes left in source.
    """
    if not ctx.last_fix_modified_files:
        return
    ascend_path = Path(ctx.triton_ascend_path)
    patch_files = resolve_source_patches(ascend_path, config)
    if not patch_files:
        return
    regenerate_and_reapply(
        ascend_path,
        patch_files,
        ctx.ta_patch_touched_files,
        ctx.ta_patch_submodule_files,
        ctx.last_fix_modified_files,
    )


def restore_workspace(
    ascend_path: Path,
    touched_parent: list[str],
    touched_submodule: list[str],
) -> None:
    """Restore patch-touched files to HEAD (parent repo + submodule).

    The applied patch content is discarded from the working tree — the
    next build re-applies the patches automatically.
    """
    if touched_parent:
        run_git_no_check(ascend_path, "checkout", "--", *touched_parent)
        log.info(f"Restored {len(touched_parent)} patch-touched file(s)")
    if touched_submodule:
        sub_path = ascend_path / SUBMODULE_DIR
        if sub_path.is_dir():
            run_git_no_check(sub_path, "checkout", "--", *touched_submodule)
            log.info(
                f"Restored {len(touched_submodule)} patch-touched "
                "file(s) in submodule"
            )
    if touched_parent or touched_submodule:
        from TA_main2main_workflow.pipeline.pre_ci import cleanup_temp_files

        cleanup_temp_files(ascend_path)


def revert_patch_files(ascend_path: Path, patch_files: list[Path]) -> None:
    """Discard uncommitted edits to the patch files themselves."""
    changed = _diff_names(ascend_path)
    dirty = [
        str(p.relative_to(ascend_path))
        for p in patch_files
        if str(p.relative_to(ascend_path)) in changed
    ]
    if dirty:
        run_git_no_check(ascend_path, "checkout", "--", *dirty)
        log.info(f"Reverted {len(dirty)} patch file(s) to HEAD")


def exclude_patch_files_from_index(
    ascend_path: Path,
    touched_parent: list[str],
    touched_submodule: list[str],
) -> None:
    """Unstage patch-touched changes so commits keep the tree clean.

    Defensive fallback for commits that use ``git add -A``: the
    working tree may carry applied patch content, which must never
    enter a commit.
    """
    if touched_parent:
        run_git_no_check(ascend_path, "restore", "--staged", "--", *touched_parent)
    if touched_submodule:
        # Unstage the submodule pointer change in the parent index...
        run_git_no_check(ascend_path, "restore", "--staged", "--", SUBMODULE_DIR)
        # ...and any staged files inside the submodule itself.
        sub_path = ascend_path / SUBMODULE_DIR
        if sub_path.is_dir():
            run_git_no_check(sub_path, "restore", "--staged", "--", *touched_submodule)
    if touched_parent or touched_submodule:
        log.info("Excluded patch-touched changes from the index")


# ═══════════════════════════════════════════════════════════════════════════
# Internal helpers
# ═══════════════════════════════════════════════════════════════════════════


def _check_applies(ascend_path: Path, patch_file: Path) -> bool:
    """Return True if *patch_file* applies cleanly to the current tree."""
    args = ["apply", "--check"]
    if patch_file.name.startswith(_NPUIR_PATCH_PREFIX):
        args += ["--directory", SUBMODULE_DIR]
    args.append(str(patch_file))
    result = run_git_no_check(ascend_path, *args)
    return result.returncode == 0


def _diff_names(ascend_path: Path) -> set[str]:
    """Repo-root-relative paths with uncommitted changes."""
    raw = run_git(ascend_path, "diff", "--name-only").strip()
    return {f for f in raw.splitlines() if f}


def _parse_patch_paths(patch_file: Path) -> list[str]:
    """Extract target paths from git-diff patch headers (static)."""
    text = patch_file.read_text(encoding="utf-8", errors="replace")
    paths: list[str] = []
    for m in re.finditer(r"^diff --git a/(\S+) b/(\S+)$", text, re.MULTILINE):
        b_path = m.group(2)
        if b_path != "/dev/null":
            paths.append(b_path)
    if not paths:
        # Plain unified diff fallback: the --- a/<path> side.
        for m in re.finditer(r"^--- a/(\S+)$", text, re.MULTILINE):
            if m.group(1) != "/dev/null":
                paths.append(m.group(1))
    return paths


def _replace_patch_sections(patch_file: Path, sections: dict[str, str]) -> None:
    """Replace/append per-file sections inside *patch_file*."""
    text = patch_file.read_text(encoding="utf-8", errors="replace")
    for path, new_diff in sections.items():
        if not new_diff.strip():
            log.warning(f"Empty diff for {path} — skipping section replacement")
            continue
        text = _replace_one_section(text, path, new_diff)
    patch_file.write_text(text, encoding="utf-8")


def _replace_one_section(text: str, path: str, new_diff: str) -> str:
    """Replace the ``diff --git a/<path> b/<path>`` section in *text*."""
    pattern = re.compile(
        rf"^diff --git a/{re.escape(path)} b/{re.escape(path)}[ \t]*$.*?"
        rf"(?=^diff --git |\Z)",
        re.MULTILINE | re.DOTALL,
    )
    new_section = new_diff.rstrip("\n") + "\n"
    if pattern.search(text):
        return pattern.sub(lambda _: new_section, text, count=1)
    # New file section: append at the end.
    return text.rstrip("\n") + "\n\n" + new_section


def _adjust_patch_with_ai(
    ctx: WorkflowContext,
    config: TAConfig,
    ascend_path: Path,
    patch_file: Path,
) -> None:
    """AI-adjust hunk positions in *patch_file* until ``git apply --check`` passes.

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
        check = run_git_no_check(ascend_path, "apply", "--check", *_check_args(patch_file))
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


def _check_args(patch_file: Path) -> list[str]:
    """git apply --check args mirroring setup.py's apply layout."""
    args: list[str] = []
    if patch_file.name.startswith(_NPUIR_PATCH_PREFIX):
        args += ["--directory", SUBMODULE_DIR]
    args.append(str(patch_file))
    return args
