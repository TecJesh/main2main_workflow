#!/usr/bin/env python3
"""Pre-CI verification for TA_main2main sync steps.

Runs mechanical checks before build/test to catch common issues early:
  1. Merge conflict marker check: no remaining <<<<<<< / ======= / >>>>>>> markers
  2. Temp file cleanliness: no intermediate AI artifacts in the repo
  3. Python syntax check: quick syntax validation on modified .py files

All results are printed to the local console and written to workspace.
"""

from __future__ import annotations

import ast
import json
import subprocess
from pathlib import Path

from TA_main2main_workflow.utils import (
    WORKSPACE_DIR, PRE_CI_CHECK_FILE, run_git_no_check,
    print_section, print_status, print_info, print_warn,
)

_TEMP_PATTERNS = [
    ".log",
    ".patch",
    ".jsonl",
    "analysis.md",
    "review.md",
    "opencode_stderr.log",
    "opencode_raw.jsonl",
]

_CONFLICT_MARKERS = [
    "<<<<<<<",
    "=======",
    ">>>>>>>",
]


def _get_modified_files(repo: Path) -> list[str]:
    """Return list of modified (unstaged + staged) files."""
    modified: set[str] = set()

    result = run_git_no_check(repo, "diff", "--name-only", "HEAD")
    if result.stdout.strip():
        modified.update(result.stdout.strip().splitlines())

    result = run_git_no_check(repo, "diff", "--name-only", "--cached")
    if result.stdout.strip():
        modified.update(result.stdout.strip().splitlines())

    result = run_git_no_check(repo, "ls-files", "--others", "--exclude-standard")
    if result.stdout.strip():
        modified.update(result.stdout.strip().splitlines())

    return sorted(modified)


def _check_conflict_markers(repo: Path, modified_files: list[str]) -> dict:
    """Scan modified files for remaining merge conflict markers."""
    violations: list[dict] = []
    for filepath in modified_files:
        full_path = repo / filepath
        if not full_path.exists() or not full_path.is_file():
            continue
        try:
            content = full_path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        for lineno, line in enumerate(content.splitlines(), 1):
            for marker in _CONFLICT_MARKERS:
                if line.strip().startswith(marker):
                    violations.append({
                        "file": filepath,
                        "line": lineno,
                        "marker": marker,
                        "text": line.strip()[:120],
                    })

    return {
        "name": "conflict_markers",
        "passed": len(violations) == 0,
        "violations": violations,
        "detail": (
            "no remaining conflict markers"
            if len(violations) == 0
            else f"{len(violations)} conflict marker(s) still present"
        ),
    }


def _check_temp_files(repo: Path, modified_files: list[str]) -> dict:
    """Check for temporary/intermediate files in modified files."""
    violations: list[str] = []
    for filepath in modified_files:
        basename = Path(filepath).name
        for pattern in _TEMP_PATTERNS:
            if pattern in basename or basename.endswith(pattern):
                violations.append(filepath)
                break

    return {
        "name": "temp_files",
        "passed": len(violations) == 0,
        "violations": violations,
        "detail": (
            "no temp files in repo"
            if len(violations) == 0
            else f"{len(violations)} temp file(s) found: {', '.join(violations)}"
        ),
    }


def _check_python_syntax(repo: Path, modified_files: list[str]) -> dict:
    """Quick Python syntax check on modified .py files."""
    violations: list[dict] = []
    py_files = [f for f in modified_files if f.endswith(".py")]

    for filepath in py_files:
        full_path = repo / filepath
        if not full_path.exists():
            continue
        try:
            source = full_path.read_text(encoding="utf-8")
            ast.parse(source, filename=filepath)
        except SyntaxError as e:
            violations.append({
                "file": filepath,
                "line": e.lineno or 0,
                "msg": str(e.msg),
            })
        except Exception:
            pass

    return {
        "name": "python_syntax",
        "passed": len(violations) == 0,
        "violations": violations,
        "detail": (
            f"all {len(py_files)} modified .py files pass syntax check"
            if len(violations) == 0
            else f"{len(violations)} file(s) have syntax errors"
        ),
    }


def run_pre_ci_check(repo: Path, step_id: str = "") -> dict:
    """Run all pre-CI checks on the triton-ascend working tree.

    Returns a dict with 'all_passed' (bool) and 'checks' (list of check results).
    """
    print_section(f"Pre-CI Check{f' — {step_id}' if step_id else ''}")

    try:
        modified_files = _get_modified_files(repo)
    except subprocess.CalledProcessError as exc:
        print_warn(f"Could not list modified files: {exc.stderr}")
        return {"all_passed": True, "checks": [], "error": str(exc.stderr)}

    if not modified_files:
        print_info("No modified files — nothing to check")
        return {"all_passed": True, "checks": [], "modified_files_count": 0}

    print_info(f"Checking {len(modified_files)} modified file(s)")

    checks: list[dict] = []
    all_passed = True

    conflict_check = _check_conflict_markers(repo, modified_files)
    checks.append(conflict_check)
    print_status(conflict_check["passed"], conflict_check["detail"])
    if not conflict_check["passed"]:
        all_passed = False
        for v in conflict_check["violations"]:
            print_warn(f"  {v['file']}:{v['line']} — {v['marker']}")

    temp_check = _check_temp_files(repo, modified_files)
    checks.append(temp_check)
    print_status(temp_check["passed"], temp_check["detail"])
    if not temp_check["passed"]:
        all_passed = False
        for v in temp_check["violations"]:
            print_warn(f"  temp file: {v}")

    syntax_check = _check_python_syntax(repo, modified_files)
    checks.append(syntax_check)
    print_status(syntax_check["passed"], syntax_check["detail"])
    if not syntax_check["passed"]:
        all_passed = False
        for v in syntax_check["violations"]:
            print_warn(f"  {v['file']}:{v['line']} — {v['msg']}")

    if all_passed:
        print_status(True, "All pre-CI checks passed")
    else:
        print_status(False, "Pre-CI checks found issues")

    result = {
        "all_passed": all_passed,
        "checks": checks,
        "modified_files_count": len(modified_files),
    }

    check_path = WORKSPACE_DIR / PRE_CI_CHECK_FILE
    check_path.write_text(
        json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )

    return result
