"""Pipeline step: Prepare workspace — clone repos, configure remotes, fetch."""

from __future__ import annotations
import os
from pathlib import Path
from TA_main2main_workflow.utils.config import TAConfig
from TA_main2main_workflow.utils.context import WorkflowContext
from TA_main2main_workflow.utils.logging import get_logger
from TA_main2main_workflow.utils.git import run_git, run_git_no_check
from TA_main2main_workflow.utils import WORKSPACE_DIR

log = get_logger(__name__)


def prepare(ctx: WorkflowContext, config: TAConfig) -> WorkflowContext:
    """Set up workspace: clone, remotes, fetch, checkout."""
    WORKSPACE_DIR.mkdir(parents=True, exist_ok=True)

    ascend_path = _ensure_repo(config)
    _fix_origin(ascend_path, config.triton_ascend_url)
    _ensure_remote(ascend_path, "triton-upstream", config.triton_upstream_url)

    log.info("Fetching origin ...")
    run_git(ascend_path, "fetch", "origin")
    log.info("Fetching triton-upstream ...")
    run_git(ascend_path, "fetch", "triton-upstream")

    base_branch = config.base_branch
    base_ref = f"origin/{base_branch}"
    run_git(ascend_path, "checkout", "-B", base_branch, base_ref)

    try:
        ascend_head = run_git(ascend_path, "rev-parse", base_ref).strip()
    except Exception:
        raise RuntimeError(f"Cannot resolve '{base_ref}'.")

    target_commit = config.target_commit
    if not target_commit:
        upstream_ref = "triton-upstream/main"
        try:
            target_commit = run_git(ascend_path, "rev-parse", upstream_ref).strip()
        except Exception:
            raise RuntimeError(f"Cannot resolve upstream HEAD from '{upstream_ref}'.")

    log.section("Workspace ready")
    log.key_value("triton-ascend", str(ascend_path))
    log.key_value("base branch", base_branch)
    log.key_value("ascend HEAD", ascend_head[:12])
    log.key_value("target commit", target_commit[:12])

    return ctx.copy_with(
        triton_ascend_path=str(ascend_path),
        target_commit=target_commit,
        ascend_head=ascend_head,
        original_branch=base_branch,
    )


def _ensure_repo(config: TAConfig) -> Path:
    if config.triton_ascend_path:
        path = Path(config.triton_ascend_path)
        if not path.exists():
            raise FileNotFoundError(f"triton-ascend path does not exist: {path}")
        log.info(f"Using existing repo: {path}")
        return path
    target = WORKSPACE_DIR / "triton-ascend"
    if target.exists():
        log.info(f"Repo exists, skip clone: {target}")
    else:
        log.info(f"Cloning {config.triton_ascend_url} -> {target}")
        run_git(WORKSPACE_DIR, "clone", config.triton_ascend_url, str(target))
    return target


def _ensure_remote(repo: Path, name: str, url: str) -> None:
    result = run_git_no_check(repo, "remote")
    if name not in result.stdout:
        run_git(repo, "remote", "add", name, url)


def _fix_origin(repo: Path, expected_url: str) -> None:
    current = _get_remote_url(repo, "origin")
    if current is None:
        run_git(repo, "remote", "add", "origin", expected_url)
        return
    if current.rstrip("/") == expected_url.rstrip("/"):
        return
    log.warning(f"origin URL mismatch -- updating to {expected_url}")
    run_git(repo, "remote", "set-url", "origin", expected_url)


def _get_remote_url(repo: Path, name: str) -> str | None:
    result = run_git_no_check(repo, "remote", "get-url", name)
    if result.returncode == 0:
        return result.stdout.strip()
    return None
