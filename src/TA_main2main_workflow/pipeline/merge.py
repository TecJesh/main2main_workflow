"""Pipeline step: Execute git merge of upstream commits into triton-ascend."""

from __future__ import annotations
import json
from pathlib import Path
from TA_main2main_workflow.utils.config import TAConfig
from TA_main2main_workflow.utils.context import WorkflowContext
from TA_main2main_workflow.utils.logging import get_logger
from TA_main2main_workflow.utils.git import run_git, run_git_no_check
from TA_main2main_workflow.utils import WORKSPACE_DIR, STEPS_DIR

log = get_logger(__name__)

_MERGE_RESULT = "merge_result.json"


def merge_upstream_commit(ctx: WorkflowContext, config: TAConfig) -> WorkflowContext:
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

    if config.resume and result_file.exists():
        log.info(f"Resume: {_MERGE_RESULT} exists, skipping merge")
        mr = json.loads(result_file.read_text(encoding="utf-8"))
        return ctx.copy_with(
            merge_has_conflicts=mr.get("has_conflicts", False),
            conflict_files=mr.get("conflict_files", []),
        )

    if (ascend_path / ".git" / "MERGE_HEAD").exists():
        try:
            run_git(ascend_path, "merge", "--abort")
        except Exception:
            run_git(ascend_path, "reset", "--hard", "HEAD")

    log.info(f"Merging {step['end_commit'][:12]} ...")
    merge_proc = run_git_no_check(
        ascend_path, "merge", "--no-ff", "--no-edit", step["end_commit"]
    )

    conflict_files = run_git(
        ascend_path, "diff", "--name-only", "--diff-filter=U"
    ).strip()
    conflict_files = (
        [f for f in conflict_files.splitlines() if f] if conflict_files else []
    )
    has_conflicts = len(conflict_files) > 0

    result = {
        "target_commit": step["end_commit"],
        "merge_exit_code": merge_proc.returncode,
        "has_conflicts": has_conflicts,
        "conflict_files": conflict_files,
    }
    result_file.write_text(
        json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    return ctx.copy_with(
        merge_has_conflicts=has_conflicts, conflict_files=conflict_files
    )
