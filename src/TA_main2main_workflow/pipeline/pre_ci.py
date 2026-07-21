"""Pre-CI verification: conflict markers, Python syntax, temp file cleanup."""

from __future__ import annotations
import ast, json, subprocess
from pathlib import Path
from TA_main2main_workflow.utils.logging import get_logger
from TA_main2main_workflow.utils.git import run_git_no_check
from TA_main2main_workflow.utils import PRE_CI_CHECK_FILE, WORKSPACE_DIR

log = get_logger(__name__)

_CLEANUP_DIRS = [
    "result_profiling",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    "*.egg-info",
]
_CLEANUP_SUFFIXES = [".lock", ".pyc", ".pyo", ".orig", ".rej", ".log"]
_CLEANUP_BASENAMES = [".DS_Store"]
_CONFLICT_MARKERS = ["<<<<<<<", "=======", ">>>>>>>"]


def cleanup_temp_files(repo: Path):
    import shutil

    removed_dirs, removed_files = [], []
    for d in _CLEANUP_DIRS:
        for found in repo.rglob(d):
            if found.is_dir() and ".git" not in found.parts:
                try:
                    shutil.rmtree(found, ignore_errors=True)
                    removed_dirs.append(str(found.relative_to(repo)))
                except Exception:
                    pass
    for suffix in _CLEANUP_SUFFIXES:
        for found in repo.rglob(f"*{suffix}"):
            if found.is_file() and ".git" not in found.parts:
                try:
                    found.unlink()
                    removed_files.append(str(found.relative_to(repo)))
                except Exception:
                    pass
    for name in _CLEANUP_BASENAMES:
        for found in repo.rglob(name):
            if found.is_file() and ".git" not in found.parts:
                try:
                    found.unlink()
                    removed_files.append(str(found.relative_to(repo)))
                except Exception:
                    pass
    total = len(removed_dirs) + len(removed_files)
    if total > 0:
        log.info(f"Cleaned up {total} temp artifact(s)")


def _get_modified_files(repo: Path) -> list[str]:
    modified: set[str] = set()
    for args in [
        ("diff", "--name-only", "HEAD"),
        ("diff", "--name-only", "--cached"),
        ("ls-files", "--others", "--exclude-standard"),
    ]:
        r = run_git_no_check(repo, *args)
        if r.stdout.strip():
            modified.update(r.stdout.strip().splitlines())
    return sorted(modified)


def _check_conflict_markers(repo: Path, files: list[str]) -> dict:
    violations = []
    for fp in files:
        full = repo / fp
        if not full.is_file():
            continue
        try:
            content = full.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        for lineno, line in enumerate(content.splitlines(), 1):
            for m in _CONFLICT_MARKERS:
                if line.strip().startswith(m):
                    violations.append({"file": fp, "line": lineno, "marker": m})
    return {
        "name": "conflict_markers",
        "passed": len(violations) == 0,
        "violations": violations,
    }


def _check_python_syntax(repo: Path, files: list[str]) -> dict:
    violations = []
    for fp in [f for f in files if f.endswith(".py")]:
        full = repo / fp
        if not full.is_file():
            continue
        try:
            ast.parse(full.read_text(encoding="utf-8"), filename=fp)
        except SyntaxError as e:
            violations.append({"file": fp, "line": e.lineno or 0, "msg": str(e.msg)})
        except Exception:
            pass
    return {
        "name": "python_syntax",
        "passed": len(violations) == 0,
        "violations": violations,
    }


def run_pre_ci_check(repo: Path, step_id: str = "") -> dict:
    log.section(f"Pre-CI Check{' — ' + step_id if step_id else ''}")
    try:
        modified_files = _get_modified_files(repo)
    except Exception as e:
        log.warning(f"Could not list modified files: {e}")
        return {"all_passed": True, "checks": []}
    if not modified_files:
        log.info("No modified files")
        return {"all_passed": True, "checks": [], "modified_files_count": 0}

    cleanup_temp_files(repo)
    try:
        modified_files = _get_modified_files(repo)
    except Exception:
        pass

    all_passed = True
    conflict = _check_conflict_markers(repo, modified_files)
    log.status(conflict["passed"], conflict.get("detail", ""))
    if not conflict["passed"]:
        all_passed = False
    syntax = _check_python_syntax(repo, modified_files)
    log.status(syntax["passed"], syntax.get("detail", ""))
    if not syntax["passed"]:
        all_passed = False

    result = {
        "all_passed": all_passed,
        "checks": [conflict, syntax],
        "modified_files_count": len(modified_files),
    }
    (WORKSPACE_DIR / PRE_CI_CHECK_FILE).write_text(
        json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return result
