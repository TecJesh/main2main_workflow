"""Pipeline step 0: Prepare workspace — clone repos, configure remotes, fetch.

This is the first step of the workflow.  It ensures the local environment
is ready before any detection or merge work begins:

1. Clone triton-ascend if no local path is given (skip if already exists)
2. Verify ``origin`` points to the correct remote URL; fix if not
3. Ensure ``triton-upstream`` remote exists, pointing to upstream Triton
4. Fetch both remotes (with built-in retry)
5. Checkout the configured base branch, fast-forward to origin

Output context fields set:
  - ``origin_remote``, ``upstream_remote`` — remote names
  - ``triton_ascend_path`` — absolute path to the triton-ascend repo
  - ``target_commit`` — the upstream commit to sync to (HEAD of
    triton-upstream/main when not explicitly given)
  - ``ascend_head`` — the HEAD of the configured base branch
  - ``original_branch`` — the base branch name
"""

from __future__ import annotations

from pathlib import Path

from TA_main2main_workflow.utils.config import TAConfig
from TA_main2main_workflow.utils.context import WorkflowContext
from TA_main2main_workflow.utils.logging import get_logger
from TA_main2main_workflow.utils.git import run_git, run_git_no_check
from TA_main2main_workflow.utils import WORKSPACE_DIR

log = get_logger(__name__)

ORIGIN_REMOTE = "origin"
UPSTREAM_REMOTE = "triton-upstream"


def prepare(ctx: WorkflowContext, config: TAConfig) -> WorkflowContext:
    """Set up workspace: clone, remotes, fetch, checkout.

    This is idempotent — safe to call on an already-prepared workspace.
    """
    WORKSPACE_DIR.mkdir(parents=True, exist_ok=True)

    # ── 1. Ensure triton-ascend exists ──────────────────────────────────
    ascend_path = _ensure_repo(config, WORKSPACE_DIR)

    # ── 2. Ensure origin points to the correct remote URL ───────────────
    _fix_origin(ascend_path, config.triton_ascend_url)

    # ── 3. Ensure triton-upstream remote ────────────────────────────────
    _ensure_remote(ascend_path, UPSTREAM_REMOTE, config.triton_upstream_url)

    # ── 4. Fetch both remotes ───────────────────────────────────────────
    log.info(f"Fetching {ORIGIN_REMOTE} ...")
    run_git(ascend_path, "fetch", ORIGIN_REMOTE)
    log.info(f"Fetching {UPSTREAM_REMOTE} ...")
    run_git(ascend_path, "fetch", UPSTREAM_REMOTE)

    # ── 5. Checkout base branch ─────────────────────────────────────────
    base_branch = config.base_branch
    base_ref = f"{ORIGIN_REMOTE}/{base_branch}"

    # Force checkout to origin's version of the base branch
    run_git(ascend_path, "checkout", "-B", base_branch, base_ref)

    # ── 6. Resolve ascend HEAD ──────────────────────────────────────────
    try:
        ascend_head = run_git(ascend_path, "rev-parse", base_ref).strip()
    except Exception:
        raise RuntimeError(
            f"Cannot resolve '{base_ref}'. "
            f"Fetch it first:\n"
            f"  cd {ascend_path} && git fetch {ORIGIN_REMOTE} {base_branch}"
        )

    # ── 7. Resolve target commit (default: triton-upstream/main HEAD) ──
    target_commit = config.target_commit
    if not target_commit:
        upstream_ref = f"{UPSTREAM_REMOTE}/main"
        try:
            target_commit = run_git(ascend_path, "rev-parse", upstream_ref).strip()
        except Exception:
            raise RuntimeError(
                f"Cannot resolve upstream HEAD from '{upstream_ref}'. "
                f"Specify --target-commit or ensure '{upstream_ref}' exists. "
                f"Try: cd {ascend_path} && git fetch {UPSTREAM_REMOTE}"
            )

    log.section("Workspace ready")
    log.key_value("triton-ascend", str(ascend_path))
    log.key_value("base branch", base_branch)
    log.key_value("ascend HEAD", ascend_head[:12])
    log.key_value("target commit", target_commit[:12])

    return ctx.copy_with(
        triton_ascend_path=str(ascend_path),
        triton_path=str(ascend_path),  # same repo, upstream via remote
        target_commit=target_commit,
        ascend_head=ascend_head,
        original_branch=base_branch,
        origin_remote=ORIGIN_REMOTE,
        upstream_remote=UPSTREAM_REMOTE,
    )


# ═══════════════════════════════════════════════════════════════════════════
# Internal helpers
# ═══════════════════════════════════════════════════════════════════════════

def _ensure_repo(config: TAConfig, workspace: Path) -> Path:
    """Return path to triton-ascend repo, cloning if necessary."""
    if config.triton_ascend_path:
        path = Path(config.triton_ascend_path)
        if not path.exists():
            raise FileNotFoundError(f"triton-ascend path does not exist: {path}")
        log.info(f"Using existing repo: {path}")
        return path

    target = workspace / "triton-ascend"
    if target.exists():
        log.info(f"Repo exists, skip clone: {target}")
    else:
        log.info(f"Cloning {config.triton_ascend_url} → {target}")
        run_git(workspace, "clone", config.triton_ascend_url, str(target))
    return target


def _ensure_remote(repo: Path, name: str, url: str) -> None:
    """Add a git remote if it doesn't already exist."""
    result = run_git_no_check(repo, "remote")
    if name not in result.stdout:
        run_git(repo, "remote", "add", name, url)


def _fix_origin(repo: Path, expected_url: str) -> None:
    """Ensure ``origin`` points to *expected_url*.  Update it if not."""
    current = _get_remote_url(repo, ORIGIN_REMOTE)
    if current is None:
        log.warning(f"No '{ORIGIN_REMOTE}' remote found — adding it")
        run_git(repo, "remote", "add", ORIGIN_REMOTE, expected_url)
        return

    if current.rstrip("/") == expected_url.rstrip("/"):
        log.info(f"origin URL OK: {current}")
        return

    log.warning(f"origin URL mismatch — updating to {expected_url}")
    run_git(repo, "remote", "set-url", ORIGIN_REMOTE, expected_url)


def _get_remote_url(repo: Path, name: str) -> str | None:
    """Return the fetch URL of remote *name*, or None if it doesn't exist."""
    result = run_git_no_check(repo, "remote", "get-url", name)
    if result.returncode == 0:
        return result.stdout.strip()
    return None
