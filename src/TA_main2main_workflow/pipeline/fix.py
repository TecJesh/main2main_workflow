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
        # AscendNPU-IR files pass the third_party/ascend/ prefix naturally;
        # their fixes are regenerated into the npuir patch afterwards.
        is_valid, reason = validate_fix(
            ascend_path, pre_files, result.modified_files
        )
        if not is_valid:
            log.warning(f"Fix validation FAILED: {reason}")
            log.warning("Reverting invalid changes...")
            # Write rejection feedback so AI can adjust on next attempt
            rejection_file = fix_dir / "fix_rejection.txt"
            rejection_file.write_text(
                f"VALIDATION REJECTED: {reason}\n"
                f"Allowed paths: {', '.join(_ALLOWED_FIX_PREFIXES)}\n"
                f"Modified files: {result.modified_files}\n",
                encoding="utf-8",
            )
            _revert_illegal_changes(ascend_path)
            return ctx.copy_with(last_fix_modified_files=[])

        log.ai_result(
            bool(result.modified_files),
            result.modified_files,
            (result.step_summary or "")[:500],
        )
        # Expand the gitlink entry (real submodule) into the individual
        # AscendNPU-IR files the AI actually changed.
        sub_changed = _submodule_files_changed(
            ascend_path, pre_submodule_snapshot
        )
        expanded = _expand_modified_files(result.modified_files, sub_changed)
        return ctx.copy_with(last_fix_modified_files=expanded)
    except Exception as e:
        log.error(f"AI fix failed: {e}")
        return ctx.copy_with(last_fix_modified_files=[])


# Paths AI is allowed to modify when fixing test failures.
# Build fixes are restricted to third_party/ascend/ only.
_ALLOWED_FIX_PREFIXES = [
    "third_party/ascend/",
    "python/triton/extension",
    "python/triton/runtime/libentry.py",
]


def validate_fix(
    ascend_path: Path,
    pre_fix_files: set[str],
    modified_files: list[str],
) -> tuple[bool, str]:
    """Validate that AI fixes only touch allowed paths.

    Returns (is_valid, reason).
    """
    if not modified_files:
        return False, "No files were modified"

    for f in modified_files:
        if not any(f.startswith(p) for p in _ALLOWED_FIX_PREFIXES):
            return False, (
                f"File '{f}' is outside allowed paths. "
                f"Allowed: {', '.join(_ALLOWED_FIX_PREFIXES)}"
            )

    return True, "all changes within allowed paths"


def _list_tracked_files(repo: Path) -> set[str]:
    """Return the set of all tracked files in the repo."""
    try:
        output = run_git(repo, "ls-files")
        return set(output.strip().splitlines())
    except Exception:
        return set()


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
