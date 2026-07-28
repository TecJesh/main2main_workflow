"""Pre-CI verification checks.

Runs before committing: scans for leftover merge conflict markers,
validates Python syntax, and removes temporary build/test artifacts.
"""

from __future__ import annotations

import ast
import shutil
from pathlib import Path

from TA_main2main_workflow.utils.logging import get_logger

log = get_logger(__name__)

_CONFLICT_MARKERS = (b"<<<<<<<", b"=======", b">>>>>>>")
_TEMP_PATTERNS = [
    "result_profiling",
    "__pycache__",
    ".pytest_cache",
    "*.pyc",
    "*.pyo",
    "*.orig",
    "*.rej",
    "*.log",
    ".DS_Store",
    "*.lock",
]


def run_pre_ci_check(repo: str | Path, step_id: str = "") -> bool:
    """Run pre-CI checks on *repo* and return True if all pass."""
    repo = Path(repo)
    ok = _check_conflict_markers(repo)
    if not ok:
        log.error(f"Pre-CI [{step_id}]: conflict markers found!")
    py_ok = _check_python_syntax(repo)
    if not py_ok:
        log.error(f"Pre-CI [{step_id}]: Python syntax errors found!")
    return ok and py_ok


def cleanup_temp_files(repo: str | Path) -> None:
    """Remove temporary/build artifacts from *repo*."""
    repo = Path(repo)
    for pattern in _TEMP_PATTERNS:
        if "*" in pattern:
            ext = pattern.lstrip("*")
            for f in repo.rglob(ext):
                if f.is_file():
                    try:
                        f.unlink()
                    except OSError:
                        pass
        else:
            for d in repo.rglob(pattern):
                if d.is_dir():
                    try:
                        shutil.rmtree(d)
                    except OSError:
                        pass
    log.info("Temp files cleaned")


# ═══════════════════════════════════════════════════════════════════════════
# Internal
# ═══════════════════════════════════════════════════════════════════════════


def _check_conflict_markers(repo: Path) -> bool:
    """Scan tracked source files for unresolved merge conflict markers."""
    dirty = False
    for ext in (".py", ".cpp", ".c", ".h", ".hpp", ".td", ".mlir", ".txt", ".md"):
        for f in repo.rglob(f"*{ext}"):
            if ".git" in f.parts:
                continue
            try:
                content = f.read_bytes()
                if any(m in content for m in _CONFLICT_MARKERS):
                    log.warning(f"Conflict marker in: {f}")
                    dirty = True
            except OSError:
                pass
    return not dirty


def _check_python_syntax(repo: Path) -> bool:
    """Validate Python syntax for all .py files in the repo."""
    ok = True
    for py_file in repo.rglob("*.py"):
        if ".git" in py_file.parts or "__pycache__" in py_file.parts:
            continue
        try:
            ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
        except SyntaxError as e:
            log.warning(f"Syntax error in {py_file}: {e}")
            ok = False
        except Exception:
            pass
    return ok
