"""Pipeline step: AI fix build/test failures with fix validation gate."""

from __future__ import annotations

import json
from pathlib import Path

from TA_main2main_workflow.agent.opencode_adapter import run_opencode_adapter
from TA_main2main_workflow.utils.config import TAConfig
from TA_main2main_workflow.utils.context import WorkflowContext
from TA_main2main_workflow.utils.logging import get_logger
from TA_main2main_workflow.utils.git import run_git, run_git_no_check
from TA_main2main_workflow.utils import FIX_LOG_DIR, STEPS_DIR, WORKSPACE_DIR

log = get_logger(__name__)
_REF = str(Path(__file__).parent.parent / "reference")


def ai_fix(
    ctx: WorkflowContext, config: TAConfig, attempt: int = 1, mode: str = "fix"
) -> WorkflowContext:
    """Invoke AI to fix build or test failures.

    Args:
        ctx: Current workflow context
        config: Workflow configuration
        attempt: Fix attempt number (1-based)
        mode: AI mode — ``"fix"`` for build/test failures,
              ``"ir_patch"`` for IR patch adjustments

    Returns updated context.  On success, the AI will have modified files
    on disk; caller is responsible for committing and rebuilding/retesting.
    """
    if config.skip_ai_analysis:
        log.info("SKIP_AI_ANALYSIS=true — skipping AI fix")
        return ctx

    ascend_path = Path(ctx.triton_ascend_path)
    step = ctx.steps[ctx.current_step] if ctx.current_step < len(ctx.steps) else None
    step_id = step["id"] if step else "step-0"
    step_dir = WORKSPACE_DIR / STEPS_DIR / step_id
    step_dir.mkdir(parents=True, exist_ok=True)
    fix_dir = WORKSPACE_DIR / FIX_LOG_DIR / f"{step_id}-fix-{attempt}"
    fix_dir.mkdir(parents=True, exist_ok=True)

    # ── Compute AI context: previous step info ──────────────────────────
    prev_step_id = ""
    prev_summary_path = ""
    if ctx.current_step > 0 and ctx.current_step <= len(ctx.steps):
        prev = ctx.steps[ctx.current_step - 1]
        prev_step_id = prev["id"]
        prev_summary = WORKSPACE_DIR / STEPS_DIR / prev_step_id / "step_summary.md"
        prev_summary_path = str(prev_summary) if prev_summary.exists() else ""
    is_last_step = ctx.current_step >= ctx.total_steps - 1
    ascend_npu_ir_fix = _detect_ascend_npu_ir_errors(ascend_path, step_id)
    if ctx.build_passed and not _detect_npu_ir_in_test_logs(ctx):
        # Test-failure fix: the build already passed, so the build.log
        # heuristic alone is noise ("llvm::" / "mlir::" appear in every
        # successful build log).  Keep the npu-ir guidance only when the
        # failure logs themselves carry strong npu-ir indicators —
        # otherwise telling the AI the failure comes from AscendNPU-IR
        # would steer it away from the real failing code (e.g.
        # python/triton/extension).
        ascend_npu_ir_fix = False
    ascend_npu_ir_compat_ref = str(
        Path(__file__).parent.parent
        / "reference"
        / "AscendNPU-IR_LLVM_VERSION_COMPAT.md"
    )
    conflict_dir = str(WORKSPACE_DIR / "conflicts")

    log.step(attempt, config.max_retries, f"AI {mode}")
    try:
        # Record pre-fix file list for validation
        pre_files = _list_tracked_files(ascend_path)
        # Snapshot dirty submodule files so the AI's AscendNPU-IR edits
        # can be identified precisely afterwards (the adapter reports the
        # submodule only as the gitlink entry, not its individual files).
        pre_submodule_snapshot = _snapshot_submodule_dirty(ascend_path)

        result = run_opencode_adapter(
            {
                "step_id": f"{step_id}-{mode}-{attempt}",
                "previous_step_id": prev_step_id,
                "previous_step_summary_path": prev_summary_path,
                "is_last_step": str(is_last_step).lower(),
                "step_dir": str(step_dir),
                "fix_dir": str(fix_dir),
                "conflict_dir": conflict_dir,
                "ascend_path": str(ascend_path),
                "triton_path": ctx.triton_ascend_path,
                "reference_dir": _REF,
                "mode": mode,
                "error_logs": json.dumps(ctx.fix_errors, ensure_ascii=False),
                "target_commit": ctx.target_commit,
                "step_index": f"{ctx.current_step + 1}/{ctx.total_steps}",
                # Fix-attempt position — the prompt uses these to detect
                # the FINAL retry (attempt == max_retries) and skip
                # still-failing tests instead of fixing them.
                "attempt": str(attempt),
                "max_retries": str(config.max_retries),
                "ascend_npu_ir_fix": str(ascend_npu_ir_fix).lower(),
                "ascend_npu_ir_compat_ref": ascend_npu_ir_compat_ref,
                "patch_touched_parent": json.dumps(
                    ctx.ta_patch_touched_files, ensure_ascii=False
                ),
                "patch_touched_submodule": json.dumps(
                    ctx.ta_patch_submodule_files, ensure_ascii=False
                ),
            }
        )

        # ── Fix validation gate ────────────────────────────────────────
        # Preferred: the AI's own report (modified_files.txt) — the
        # git-diff based list is polluted by build-applied patch content
        # (setup.py applies the triton-ascend patches in place) and
        # would reject perfectly valid fixes because of files the AI
        # never touched.
        # AscendNPU-IR fixes pass the third_party/ascend/ prefix via the
        # gitlink entry and are regenerated into the npuir patch later.
        reported = _read_ai_modified_files(step_dir)
        if reported:
            ai_edits = reported
            log.info(
                f"Using AI-reported modified files ({len(ai_edits)}): "
                f"{', '.join(ai_edits[:20])}"
            )
        else:
            patch_touched = set(ctx.ta_patch_touched_files)
            ai_edits = [f for f in result.modified_files if f not in patch_touched]
            log.info(
                "modified_files.txt missing or empty — falling back to "
                "git diff (patch-touched files filtered)"
            )
        # Op restorations reported under the deleted-frontend-op
        # strategy — these flow into the TA source patches instead of
        # being committed directly.
        op_restore = _read_ai_op_restore_files(step_dir)

        is_valid, reason = validate_fix(ascend_path, pre_files, ai_edits, op_restore)
        if not is_valid:
            log.warning(f"Fix validation FAILED: {reason}")
            log.warning("Reverting invalid changes...")
            # Write rejection feedback so AI can adjust on next attempt
            rejection_file = fix_dir / "fix_rejection.txt"
            rejection_file.write_text(
                f"VALIDATION REJECTED: {reason}\n"
                f"Allowed paths: {', '.join(_ALLOWED_FIX_PREFIXES)}\n"
                f"Op-restore paths: {', '.join(_OP_RESTORE_PREFIXES)}\n"
                f"Modified files (AI edits): {ai_edits}\n",
                encoding="utf-8",
            )
            _revert_illegal_changes(ascend_path)
            return ctx.copy_with(
                last_fix_modified_files=[], last_fix_op_restore_files=[]
            )

        log.ai_result(
            bool(ai_edits),
            ai_edits,
            (result.step_summary or "")[:500],
        )
        if op_restore:
            log.info(f"Op-restore files ({len(op_restore)}): {', '.join(op_restore)}")
        # Expand the gitlink entry (real submodule) into the individual
        # AscendNPU-IR files the AI actually changed.
        sub_changed = _submodule_files_changed(ascend_path, pre_submodule_snapshot)
        expanded = _expand_modified_files(ai_edits, sub_changed)
        return ctx.copy_with(
            last_fix_modified_files=expanded,
            last_fix_op_restore_files=op_restore,
        )
    except Exception as e:
        log.error(f"AI fix failed: {e}")
        return ctx.copy_with(last_fix_modified_files=[], last_fix_op_restore_files=[])


# Paths AI is allowed to modify when fixing test failures.
# Build fixes are restricted to third_party/ascend/ plus the Ascend
# build entry (setup_ascend.py) — triton-ascend builds via
# `setup_ascend.py install`, so build fixes may legitimately touch it.
_ALLOWED_FIX_PREFIXES = [
    "third_party/ascend/",
    "python/triton/extension",
    "python/triton/runtime/libentry.py",
    "setup_ascend.py",
]

# Frontend TritonOp definition paths where the AI may RESTORE deleted
# op code under the deleted-frontend-op strategy (upstream deleted a
# TritonOp from its .td file).  Only files the AI lists in
# op_restore_files.txt are allowed here — the workflow regenerates
# those edits into the TA source patches and restores the sources, so
# they are never committed directly.
_OP_RESTORE_PREFIXES = [
    "include/triton/Dialect/Triton/IR/",
    "lib/Dialect/Triton/IR/",
]


def validate_fix(
    ascend_path: Path,
    pre_fix_files: set[str],
    modified_files: list[str],
    op_restore_files: list[str] | None = None,
) -> tuple[bool, str]:
    """Validate that AI fixes only touch allowed paths.

    *op_restore_files*: files the AI reported as restored deleted
    frontend op code (``op_restore_files.txt``).  They are allowed
    ONLY under the op-restore prefixes — the workflow regenerates
    them into the TA source patches instead of committing them.

    Returns (is_valid, reason).
    """
    restore_set = set(op_restore_files or [])
    if not modified_files and not restore_set:
        return False, "No files were modified"

    for f in modified_files:
        if f in restore_set:
            if not any(f.startswith(p) for p in _OP_RESTORE_PREFIXES):
                return False, (
                    f"Op-restore file '{f}' is outside the frontend op "
                    f"paths. Allowed: {', '.join(_OP_RESTORE_PREFIXES)}"
                )
            continue
        if not any(f.startswith(p) for p in _ALLOWED_FIX_PREFIXES):
            return False, (
                f"File '{f}' is outside allowed paths. "
                f"Allowed: {', '.join(_ALLOWED_FIX_PREFIXES)}"
            )

    # Op-restore claims that were not reported as modified are
    # suspicious too — reject them so patch regeneration never picks
    # up stray paths.
    for f in restore_set:
        if not any(f.startswith(p) for p in _OP_RESTORE_PREFIXES):
            return False, (
                f"Op-restore file '{f}' is outside the frontend op paths. "
                f"Allowed: {', '.join(_OP_RESTORE_PREFIXES)}"
            )

    return True, "all changes within allowed paths"


def _list_tracked_files(repo: Path) -> set[str]:
    """Return the set of all tracked files in the repo."""
    try:
        output = run_git(repo, "ls-files")
        return set(output.strip().splitlines())
    except Exception:
        return set()


def _read_ai_modified_files(step_dir: Path) -> list[str]:
    """AI-reported modified files from ``modified_files.txt``.

    Returns [] when the file is missing or empty — the caller falls
    back to the git-diff based list in that case.
    """
    report = step_dir / "modified_files.txt"
    if not report.exists():
        return []
    try:
        return [
            ln.strip()
            for ln in report.read_text(encoding="utf-8").splitlines()
            if ln.strip()
        ]
    except Exception:
        return []


def _read_ai_op_restore_files(step_dir: Path) -> list[str]:
    """AI-reported restored files from ``op_restore_files.txt``.

    Written only when the deleted-frontend-op strategy was applied.
    Returns [] when the file is missing or empty.
    """
    report = step_dir / "op_restore_files.txt"
    if not report.exists():
        return []
    try:
        return [
            ln.strip()
            for ln in report.read_text(encoding="utf-8").splitlines()
            if ln.strip()
        ]
    except Exception:
        return []


def _submodule_path(ascend_path: Path) -> Path:
    from TA_main2main_workflow.utils.submodule import SUBMODULE_DIR

    return ascend_path / SUBMODULE_DIR


def _submodule_dirty_paths(sub_path: Path) -> list[str]:
    """Submodule-internal paths with uncommitted changes (porcelain)."""
    if not (sub_path / ".git").exists():
        return []
    raw = run_git_no_check(sub_path, "status", "--porcelain")
    paths: list[str] = []
    seen: set[str] = set()
    for line in raw.stdout.splitlines():
        if not line.strip():
            continue
        path = line[3:].strip()
        if " -> " in path:  # rename: "old -> new"
            path = path.split(" -> ", 1)[1].strip()
        if path and path not in seen:
            seen.add(path)
            paths.append(path)
    return paths


def _snapshot_submodule_dirty(
    ascend_path: Path,
) -> dict[str, bytes | None]:
    """Snapshot the content of every dirty file inside the submodule.

    ``None`` marks a file that does not exist (untracked/new).  Used to
    detect exactly which AscendNPU-IR files the AI changed — the
    adapter reports the submodule only as its gitlink entry.
    """
    sub_path = _submodule_path(ascend_path)
    snapshot: dict[str, bytes | None] = {}
    for path in _submodule_dirty_paths(sub_path):
        p = sub_path / path
        snapshot[path] = p.read_bytes() if p.is_file() else None
    return snapshot


def _submodule_files_changed(
    ascend_path: Path, snapshot: dict[str, bytes | None]
) -> list[str]:
    """Submodule-internal paths whose content differs from *snapshot*."""
    sub_path = _submodule_path(ascend_path)
    changed: list[str] = []
    for path in _submodule_dirty_paths(sub_path):
        p = sub_path / path
        current = p.read_bytes() if p.is_file() else None
        if path not in snapshot or snapshot[path] != current:
            changed.append(path)
    return changed


def _expand_modified_files(
    modified_files: list[str], sub_changed: list[str]
) -> list[str]:
    """Expand the submodule gitlink entry into individual file paths.

    The adapter's ``git diff --name-only HEAD`` in the parent repo
    reports a real (gitlink) submodule as a single ``<dir>`` entry —
    patch regeneration needs the individual files instead.
    """
    from TA_main2main_workflow.utils.submodule import SUBMODULE_DIR

    expanded: list[str] = []
    for f in modified_files:
        if f == SUBMODULE_DIR:
            expanded.extend(f"{SUBMODULE_DIR}/{p}" for p in sub_changed)
        else:
            expanded.append(f)
    return list(dict.fromkeys(expanded))


def _revert_illegal_changes(repo: Path) -> None:
    """Revert all uncommitted changes and remove untracked files."""
    try:
        run_git(repo, "checkout", "--", ".")
        run_git(repo, "clean", "-fd")
    except Exception as e:
        log.error(f"Failed to revert changes: {e}")


def _detect_ascend_npu_ir_errors(ascend_path: Path, step_id: str) -> bool:
    """Check if build errors are from AscendNPU-IR compilation failures."""
    build_log = WORKSPACE_DIR / STEPS_DIR / step_id / "build.log"
    if not build_log.exists():
        return False
    try:
        content = build_log.read_text(encoding="utf-8", errors="replace").lower()
        indicators = [
            "AscendNPU-IR".lower(),
            "ascendnpu-ir",
            "llvm::",
            "mlir::",
            "fatal error",
            "undefined reference",
        ]
        return any(ind in content for ind in indicators)
    except Exception:
        return False


def _detect_npu_ir_in_test_logs(ctx: WorkflowContext) -> bool:
    """Strong npu-ir indicators in the CURRENT failure logs.

    Used when the build already passed: a test failure whose root
    cause really is AscendNPU-IR (e.g. bishengir-generated IR) will
    mention these markers in the failure output, so the AI keeps the
    npu-ir guidance.  Weak markers like "llvm::" are intentionally
    NOT used — they would re-introduce false positives.
    """
    indicators = ["ascendnpu-ir", "bishengir", "npuir"]
    for path in ctx.fix_errors:
        p = Path(path)
        if not p.is_file():
            continue
        try:
            content = p.read_text(encoding="utf-8", errors="replace").lower()
        except Exception:
            continue
        if any(ind in content for ind in indicators):
            return True
    return False
