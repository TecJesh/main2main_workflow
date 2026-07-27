"""Pipeline step: Prepare workspace — configure remotes, fetch, set up refs."""

from __future__ import annotations
import os
from pathlib import Path
from TA_main2main_workflow.utils.config import TAConfig
from TA_main2main_workflow.utils.context import WorkflowContext
from TA_main2main_workflow.utils.logging import get_logger
from TA_main2main_workflow.utils.git import run_git, run_git_no_check
from TA_main2main_workflow.utils import WORKSPACE_DIR, ENV_BASE_BRANCH, get_base_branch_ref

log = get_logger(__name__)


def prepare(ctx: WorkflowContext, config: TAConfig) -> WorkflowContext:
    """Set up workspace: remotes, fetch, resolve ascend_head.

    Does NOT force origin to point to any specific URL — the user may have
    origin configured as their private fork (e.g. TecJesh/triton-ascend).
    ascend_head is resolved from origin/{TA_BASE_BRANCH} so that previously
    merged commits on the fork are reflected in the merge-base calculation.
    """
    WORKSPACE_DIR.mkdir(parents=True, exist_ok=True)

    ascend_path = _ensure_repo(config)
    _ensure_remote(ascend_path, "triton-upstream", config.triton_upstream_url)

    # ── Record original branch before any checkout ──
    try:
        original_branch = run_git(ascend_path, "branch", "--show-current").strip()
        if not original_branch:
            original_branch = run_git(ascend_path, "rev-parse", "HEAD").strip()
    except Exception:
        original_branch = ""

    # ── Abort any stale merge from a previous crashed run ──
    merge_head = ascend_path / ".git" / "MERGE_HEAD"
    if merge_head.exists():
        log.warning("Found stale MERGE_HEAD from previous run, aborting it")
        try:
            run_git(ascend_path, "merge", "--abort")
            log.info("Stale merge aborted successfully")
        except Exception:
            log.warning("merge --abort failed, trying reset --hard")
            try:
                run_git(ascend_path, "reset", "--hard", "HEAD")
            except Exception:
                pass
        for stale in [".git/MERGE_MODE", ".git/MERGE_MSG", ".git/CHERRY_PICK_HEAD"]:
            p = ascend_path / stale
            if p.exists():
                p.unlink()

    # ── Fetch origin (user's fork or upstream) ──
    log.info("Fetching origin ...")
    run_git(ascend_path, "fetch", "origin")

    # ── Fetch triton-upstream ──
    log.info("Fetching triton-upstream ...")
    run_git(ascend_path, "fetch", "triton-upstream")

    # ── Resolve ascend_head from the configured base branch ──
    # Uses origin/{TA_BASE_BRANCH} so that when origin is a private fork
    # (e.g. TecJesh/triton-ascend), previously merged commits are reflected
    # in the merge-base calculation.  Falls back to checkout HEAD gracefully.
    base_branch = os.getenv(ENV_BASE_BRANCH, "main")
    base_ref = get_base_branch_ref()
    try:
        run_git(ascend_path, "fetch", "origin", base_branch)
    except Exception:
        log.warning(f"Could not fetch {base_ref}, using checkout HEAD as base")
    try:
        ascend_head = run_git(ascend_path, "rev-parse", base_ref).strip()
    except Exception:
        ascend_head = run_git(ascend_path, "rev-parse", "HEAD").strip()
        log.warning(f"{base_ref} not available, using checkout HEAD")

    # ── Resolve target commit ──
    target_commit = config.target_commit
    if not target_commit:
        upstream_ref = "triton-upstream/main"
        try:
            target_commit = run_git(ascend_path, "rev-parse", upstream_ref).strip()
        except Exception:
            raise RuntimeError(f"Cannot resolve upstream HEAD from '{upstream_ref}'.")

    log.section("Workspace ready")
    log.key_value("triton-ascend", str(ascend_path))
    log.key_value("base ref", base_ref)
    log.key_value("ascend HEAD", ascend_head[:12])
    log.key_value("target commit", target_commit[:12])
    log.key_value("original branch", original_branch)

    return ctx.copy_with(
        triton_ascend_path=str(ascend_path),
        target_commit=target_commit,
        ascend_head=ascend_head,
        original_branch=original_branch,
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
