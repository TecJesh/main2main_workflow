"""Pipeline step: Detect upstream commits to merge."""

from __future__ import annotations
import json
from pathlib import Path
from TA_main2main_workflow.utils.config import TAConfig
from TA_main2main_workflow.utils.context import WorkflowContext
from TA_main2main_workflow.utils.git import run_git
from TA_main2main_workflow.utils.logging import get_logger
from TA_main2main_workflow.utils import DETECT_FILE, WORKSPACE_DIR

log = get_logger(__name__)


def run_detect(ctx: WorkflowContext, config: TAConfig) -> WorkflowContext:
    detect_file = WORKSPACE_DIR / DETECT_FILE
    if config.resume and detect_file.exists():
        log.info("Resume: detect.json exists, skipping detect")
        data = json.loads(detect_file.read_text(encoding="utf-8"))
        return ctx.copy_with(
            merge_base=data["merge_base"],
            target_commit=data["target_commit"],
            upstream_commits=data.get("upstream_commits", []),
            upstream_commits_count=data["upstream_commits_count"],
            changed_files_count=data.get("changed_files_count", 0),
            changed_lines_total=data.get("changed_lines", 0),
            has_new_commits=True,
            ascend_head=data.get("ascend_head", ""),
        )
    return _detect_commits(ctx, config)


def _detect_commits(ctx: WorkflowContext, config: TAConfig) -> WorkflowContext:
    ascend_path = Path(ctx.triton_ascend_path)
    ascend_head = ctx.ascend_head
    target = ctx.target_commit

    try:
        merge_base = run_git(ascend_path, "merge-base", ascend_head, target).strip()
    except Exception:
        raise RuntimeError(
            f"No common ancestor between ascend HEAD ({ascend_head[:12]}) "
            f"and target ({target[:12]}).")

    commits = _list_commits(ascend_path, merge_base, target)
    has_new = len(commits) > 0 and merge_base != target
    changed_files = _changed_files(ascend_path, merge_base, target)
    changed_lines = _count_changed_lines(ascend_path, merge_base, target)

    result = {
        "ascend_head": ascend_head,
        "target_commit": target,
        "merge_base": merge_base,
        "upstream_commits_count": len(commits),
        "upstream_commits": commits,
        "changed_lines": changed_lines,
        "changed_files": changed_files,
        "changed_files_count": len(changed_files),
    }
    (WORKSPACE_DIR / DETECT_FILE).write_text(
        json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    return ctx.copy_with(
        merge_base=merge_base, target_commit=target, ascend_head=ascend_head,
        upstream_commits=commits, upstream_commits_count=len(commits),
        changed_files_count=len(changed_files), changed_lines_total=changed_lines,
        has_new_commits=has_new,
    )


def _list_commits(repo: Path, merge_base: str, target: str) -> list[dict]:
    output = run_git(
        repo, "log", "--reverse", "--format=%H%x1f%s", f"{merge_base}..{target}")
    commits: list[dict] = []
    for line in output.strip().splitlines():
        if not line.strip():
            continue
        parts = line.split("\x1f", 1)
        commits.append(
            {"sha": parts[0].strip(), "subject": parts[1].strip() if len(parts) > 1 else ""})
    return commits


def _count_changed_lines(repo: Path, merge_base: str, target: str) -> int:
    try:
        output = run_git(repo, "diff", "--shortstat", merge_base, target)
    except Exception:
        return 0
    total = 0
    for part in output.split(","):
        part = part.strip()
        if "insertion" in part or "deletion" in part:
            try:
                total += int(part.split()[0])
            except ValueError:
                pass
    return total


def _changed_files(repo: Path, merge_base: str, target: str) -> list[str]:
    output = run_git(repo, "diff", "--name-only", merge_base, target)
    return sorted(f for f in output.strip().splitlines() if f)
