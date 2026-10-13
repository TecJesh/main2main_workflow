"""Pipeline test: per-case fix loop.

Fixes failing pipeline-test cases one by one, reusing the main flow's
machinery: AI fix (``pipeline_test_fix`` mode) → category-aware
validation → patch regeneration → ``build_triton`` → precise single-case
rerun → ``commit_fixes`` (one commit per fixed case).

Category → fix location mapping:

  operator       — triton-ascend operator code (lib/Dialect, lib/Conversion,
                   lib/Target/Ascend, include/triton .td, third_party/ascend)
  triton_ascend  — Ascend adaptation layer (third_party/ascend,
                   python/triton/extension, libentry, setup_ascend.py)
  npu_ir         — AscendNPU-IR submodule (flows into the npuir patch via
                   ``regen_ai_fixes``; sources are restored before the build)
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from TA_main2main_workflow.agent.opencode_adapter import run_opencode_adapter
from TA_main2main_workflow.pipeline import fix as pipeline_fix
from TA_main2main_workflow.pipeline.build import build_triton, commit_fixes
from TA_main2main_workflow.pipeline.ta_patch import regen_ai_fixes
from TA_main2main_workflow.pipeline_test.script_runner import run_rerun_command
from TA_main2main_workflow.utils import (
    FIX_LOG_DIR,
    PIPELINE_TEST_CATEGORY_LABELS,
    PIPELINE_TEST_CATEGORY_NPU_IR,
    PIPELINE_TEST_CATEGORY_OPERATOR,
    PIPELINE_TEST_CATEGORY_TRITON_ASCEND,
    PIPELINE_TEST_FIX_SUMMARIES,
    PIPELINE_TEST_LOG_DIR_NAME,
    PIPELINE_TEST_RERUN_PREFIX,
    WORKSPACE_DIR,
)
from TA_main2main_workflow.utils.config import TAConfig
from TA_main2main_workflow.utils.context import WorkflowContext
from TA_main2main_workflow.utils.logging import get_logger
from TA_main2main_workflow.utils.submodule import SUBMODULE_DIR

log = get_logger(__name__)
_REF = str(Path(__file__).parent.parent / "reference")

# Allowed fix prefixes per problem category (repo-root relative).
# npu_ir edits inside the AscendNPU-IR submodule flow into the npuir
# patch and are never committed directly — the path prefix matches the
# expanded submodule form ``<SUBMODULE_DIR>/<sub-relative>``.
_CATEGORY_PREFIXES: dict[str, list[str]] = {
    PIPELINE_TEST_CATEGORY_OPERATOR: [
        "lib/Dialect/",
        "lib/Conversion/",
        "lib/Target/Ascend/",
        "include/triton/Dialect/Triton/IR/",
        "third_party/ascend/",
        "python/triton_ascend/",
    ],
    PIPELINE_TEST_CATEGORY_TRITON_ASCEND: [
        "third_party/ascend/",
        "python/triton_ascend/",
        "python/triton/extension/",
        "python/triton/runtime/libentry.py",
        "setup_ascend.py",
    ],
    PIPELINE_TEST_CATEGORY_NPU_IR: [f"{SUBMODULE_DIR}/"],
}

# Files the per-case AI call (re)writes in the shared step dir.  They
# are removed before each fix attempt so a stale whitelist / rerun
# command from a previous case can never leak into the next one.
_STALE_STEP_FILES = (
    "modified_files.txt",
    "op_restore_files.txt",
    "rerun_command.txt",
    "commit_files.txt",
    "commit_message.txt",
)

_DEFAULT_RERUN_TIMEOUT = 3600


# ═══════════════════════════════════════════════════════════════════════════
# Category-aware validation
# ═══════════════════════════════════════════════════════════════════════════


def validate_fix_for_category(
    modified_files: list[str], category: str
) -> tuple[bool, str]:
    """Validate that AI fixes stay inside the category's allowed paths.

    Modeled on ``pipeline/fix.py::validate_fix`` but with per-category
    prefixes (the main flow's list is too narrow for operator fixes).
    Returns ``(is_valid, reason)``.
    """
    if not modified_files:
        return False, "No files were modified"
    prefixes = _CATEGORY_PREFIXES.get(category)
    if not prefixes:
        return False, f"Unknown category: {category}"
    for f in modified_files:
        if not any(f.startswith(p) for p in prefixes):
            return False, (
                f"File '{f}' is outside the {category} category paths. "
                f"Allowed: {', '.join(prefixes)}"
            )
    return True, "all changes within category paths"


def revert_ai_edits(ascend_path: Path, files: list[str]) -> None:
    """Revert exactly the AI-reported files (parent + submodule).

    Unlike ``fix._revert_illegal_changes`` this does NOT run
    ``git clean -fd`` on the submodule — npuir patch new-file sections
    create untracked files there that must survive.
    """
    from TA_main2main_workflow.pipeline.ta_patch import _restore_from_head

    parent_files = [f for f in files if not f.startswith(SUBMODULE_DIR + "/")]
    sub_files = [
        f[len(SUBMODULE_DIR) + 1 :] for f in files if f.startswith(SUBMODULE_DIR + "/")
    ]
    if parent_files:
        _restore_from_head(ascend_path, parent_files, "pipeline-test revert")
    if sub_files:
        sub = ascend_path / SUBMODULE_DIR
        _restore_from_head(sub, sub_files, "pipeline-test submodule revert")


# ═══════════════════════════════════════════════════════════════════════════
# Per-case fix
# ═══════════════════════════════════════════════════════════════════════════


def fix_case_loop(
    ctx: WorkflowContext,
    config: TAConfig,
    repo: str,
    case: dict,
    test_workspace: Path,
    step_dir: Path,
) -> tuple[bool, WorkflowContext]:
    """Fix ONE failing case; returns ``(fixed, ctx)``.

    Attempts bounded by ``config.pipeline_test_max_retries``.  On pass:
    ``commit_fixes`` (per-case commit) + fix-summary accumulation.  On
    exhaustion: the last attempt's edits are reverted so they cannot
    leak into other cases' commits or the final PR.
    """
    case_id = case.get("case", "")
    category = case.get("category", PIPELINE_TEST_CATEGORY_TRITON_ASCEND)
    label = PIPELINE_TEST_CATEGORY_LABELS.get(category, category)
    ascend_path = Path(ctx.triton_ascend_path)
    repo_dir = _repo_dir(test_workspace, repo)
    # Union of every attempt's edits — fixes accumulate across attempts,
    # so an exhausted case must revert ALL of them, not just the last.
    all_edits: list[str] = []

    log.header(f"Fix case [{repo}] {case_id} ({label})")

    if config.skip_ai_analysis:
        log.warning(
            "SKIP_AI_ANALYSIS=true — cannot fix, marking case as unfixed"
        )
        return False, ctx

    for attempt in range(config.pipeline_test_max_retries):
        log.section(f"Fix attempt {attempt + 1}/{config.pipeline_test_max_retries}")
        ctx = ctx.copy_with(retry_count=attempt)
        rerun_log = (
            test_workspace
            / PIPELINE_TEST_LOG_DIR_NAME
            / f"{repo}_test_log"
            / f"{PIPELINE_TEST_RERUN_PREFIX}{_safe_name(case_id)}-{attempt}.log"
        )

        # ── 1. AI fix ────────────────────────────────────────────────
        ctx, edited = _ai_fix_attempt(
            ctx, config, repo, case, repo_dir, rerun_log, step_dir, attempt,
            test_workspace,
        )
        if not edited:
            continue
        for f in edited:
            if f not in all_edits:
                all_edits.append(f)

        # ── 2. Patch regeneration (npu-ir / patch-touched fixes) ─────
        ctx = regen_ai_fixes(ctx, config)

        # ── 3. Rebuild (SKIP_BUILD reuses the main flow's semantics) ──
        if config.skip_build:
            log.info("SKIP_BUILD=true — treating rebuild as passed")
            ctx = ctx.copy_with(build_passed=True, fix_errors=[])
        else:
            ctx = build_triton(ctx, config, clean=False)
        if not ctx.build_passed:
            log.error("Rebuild after fix failed — retrying with build log")
            continue

        # ── 4. Precise single-case rerun ─────────────────────────────
        if not repo_dir.exists():
            log.warning(
                f"Test repo dir missing ({repo_dir}) — cannot rerun; "
                "the test script must clone the repo first"
            )
            continue
        command = _read_rerun_command(step_dir) or _default_rerun_command(
            repo_dir, case_id, rerun_log, config.test_procs
        )
        rc = run_rerun_command(
            command, repo_dir, rerun_log, timeout=_DEFAULT_RERUN_TIMEOUT
        )
        if _case_passed(rerun_log, case_id, rc):
            # ── 5. Per-case commit + summary accumulation ──────────
            commit_fixes(ctx, config)
            _accumulate_fix_summary(step_dir, repo, case_id, category, attempt)
            log.status(True, f"Case fixed and committed: {case_id}")
            return True, ctx

        log.info(f"Case still failing (attempt {attempt + 1}) — retrying")
        # Feed the rerun output to the next attempt's AI context.
        ctx = ctx.copy_with(fix_errors=list(ctx.fix_errors) + [str(rerun_log)])

    # Exhausted: revert all of this case's edits (P3 — keep the tree
    # clean for the next case / round) and report the case as unfixed.
    if all_edits:
        log.warning(
            f"Case {case_id} not fixed after {config.pipeline_test_max_retries} "
            "attempts — reverting this case's edits"
        )
        revert_ai_edits(ascend_path, all_edits)
    return False, ctx


def _ai_fix_attempt(
    ctx: WorkflowContext,
    config: TAConfig,
    repo: str,
    case: dict,
    repo_dir: Path,
    rerun_log: Path,
    step_dir: Path,
    attempt: int,
    test_workspace: Path,
) -> tuple[WorkflowContext, list[str]]:
    """One AI fix attempt.  Returns ``(ctx, expanded_modified_files)``.

    The expanded list uses the submodule snapshot helpers from
    ``pipeline/fix.py`` — the AI's ``modified_files.txt`` reports the
    submodule only as the bare gitlink entry, never its inner files.
    """
    ascend_path = Path(ctx.triton_ascend_path)
    case_id = case.get("case", "")
    category = case.get("category", PIPELINE_TEST_CATEGORY_TRITON_ASCEND)

    for name in _STALE_STEP_FILES:
        (step_dir / name).unlink(missing_ok=True)

    pre_submodule_snapshot = pipeline_fix._snapshot_submodule_dirty(ascend_path)
    fix_dir = WORKSPACE_DIR / FIX_LOG_DIR / f"pipeline-test-{attempt}"
    fix_dir.mkdir(parents=True, exist_ok=True)

    error_context = {
        "case": case_id,
        "category": category,
        "category_label": PIPELINE_TEST_CATEGORY_LABELS.get(category, category),
        "error": case.get("error", ""),
        "log_file": str(case.get("log_file", "")),
        "rerun_log": str(rerun_log),
        "repo_dir": str(repo_dir),
        "attempt": str(attempt + 1),
        "max_retries": str(config.pipeline_test_max_retries),
        # Previous failure evidence (build.log / rerun logs).
        "previous_error_logs": list(ctx.fix_errors),
    }

    log.step(attempt + 1, config.pipeline_test_max_retries, "AI pipeline_test_fix")
    try:
        result = run_opencode_adapter(
            {
                "step_id": f"pipeline-test-{repo}-{attempt}",
                "previous_step_id": "",
                "previous_step_summary_path": "",
                "is_last_step": "true",
                "step_dir": str(step_dir),
                "fix_dir": str(fix_dir),
                "conflict_dir": "",
                "ascend_path": str(ascend_path),
                "triton_path": str(ascend_path),
                "reference_dir": _REF,
                "mode": "pipeline_test_fix",
                "error_logs": json.dumps(error_context, ensure_ascii=False),
                "target_commit": ctx.target_commit,
                "step_index": "1/1",
                "attempt": str(attempt),
                "max_retries": str(config.pipeline_test_max_retries),
                "test_workspace": str(test_workspace),
                "ascend_npu_ir_fix": str(
                    category == PIPELINE_TEST_CATEGORY_NPU_IR
                ).lower(),
                "ascend_npu_ir_compat_ref": str(
                    _REF
                    + "/AscendNPU-IR_LLVM_VERSION_COMPAT.md"
                ),
                "patch_touched_parent": json.dumps(
                    ctx.ta_patch_touched_files, ensure_ascii=False
                ),
                "patch_touched_submodule": json.dumps(
                    ctx.ta_patch_submodule_files, ensure_ascii=False
                ),
            }
        )
    except Exception as exc:
        log.error(f"AI fix failed: {exc}")
        return ctx, []

    reported = pipeline_fix._read_ai_modified_files(step_dir)
    if not reported:
        # Fall back to the adapter's git-diff list, filtered against
        # patch-touched content (mirrors pipeline/fix.py::ai_fix).
        patch_touched = set(ctx.ta_patch_touched_files)
        reported = [
            f for f in result.modified_files if f not in patch_touched
        ]
    sub_changed = pipeline_fix._submodule_files_changed(
        ascend_path, pre_submodule_snapshot
    )
    expanded = pipeline_fix._expand_modified_files(reported, sub_changed)
    if not expanded:
        log.info("AI made no source changes")
        return ctx, []

    ok, reason = validate_fix_for_category(expanded, category)
    if not ok:
        log.warning(f"Fix validation FAILED: {reason} — reverting")
        (fix_dir / "fix_rejection.txt").write_text(
            f"VALIDATION REJECTED: {reason}\n"
            f"Modified files (AI edits): {expanded}\n",
            encoding="utf-8",
        )
        revert_ai_edits(ascend_path, expanded)
        return ctx, []

    log.ai_result(True, expanded, (result.step_summary or "")[:500])
    return ctx.copy_with(
        last_fix_modified_files=expanded,
        last_fix_op_restore_files=[],
    ), expanded


# ═══════════════════════════════════════════════════════════════════════════
# Rerun plumbing
# ═══════════════════════════════════════════════════════════════════════════


def _read_rerun_command(step_dir: Path) -> str:
    """The AI-written precise single-case rerun command (may be empty)."""
    rerun_file = step_dir / "rerun_command.txt"
    if not rerun_file.exists():
        return ""
    try:
        return rerun_file.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _default_rerun_command(
    repo_dir: Path, case_id: str, rerun_log: Path, procs: int
) -> str:
    """Fallback single-case pytest command when the AI wrote none.

    *procs* reuses the workflow's pytest worker count (TEST_PROCS /
    NUM_PROCS, ``config.test_procs``).
    """
    return (
        f"cd {_shq(str(repo_dir))} && "
        f"pytest -sv -n {procs} {_shq(case_id)} "
        f">> {_shq(str(rerun_log))} 2>&1"
    )


def _case_passed(rerun_log: Path, case_id: str, rc: int) -> bool:
    """True when the single-case rerun passed for *case_id*."""
    if rc != 0:
        return False
    if not rerun_log.exists():
        return False
    try:
        text = rerun_log.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    # The case must not appear on a FAILED/ERROR short-summary line.
    # Long node ids get truncated in the summary — match on the tail.
    tail = case_id.rsplit("::", 1)[-1][:60]
    for m in re.finditer(r"^(FAILED|ERROR)\s+(\S+)", text, re.M):
        if tail in m.group(2) or case_id in m.group(2):
            return False
    return True


def _repo_dir(test_workspace: Path, repo: str) -> Path:
    """The external test repo checkout directory (may not exist yet)."""
    return test_workspace / repo


def _safe_name(case_id: str) -> str:
    """Filesystem-safe slug for a test case id."""
    keep = []
    for ch in case_id:
        keep.append(ch if ch.isalnum() or ch in "._-" else "_")
    return "".join(keep)[:80] or "case"


def _accumulate_fix_summary(
    step_dir: Path, repo: str, case_id: str, category: str, attempt: int
) -> None:
    """Append the AI's fix note to the per-flow fix-summaries file."""
    summary_file = step_dir / "step_summary.md"
    entry = f"- [{repo}] {case_id} ({category}, attempt {attempt + 1})"
    body = ""
    if summary_file.exists():
        try:
            body = summary_file.read_text(encoding="utf-8").strip()
        except OSError:
            body = ""
    lines = [entry]
    if body:
        lines.append("")
        lines.append(body)
    lines.append("")
    with open(step_dir / PIPELINE_TEST_FIX_SUMMARIES, "a", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")


def _shq(text: str) -> str:
    """Single-quote *text* for safe embedding in a shell command."""
    return "'" + text.replace("'", "'\\''") + "'"
