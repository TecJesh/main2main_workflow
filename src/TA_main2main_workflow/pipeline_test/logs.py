"""Pipeline test: raw log collection and deterministic pytest parsing.

The human-configured test script writes raw pytest logs under
``<test_workspace>/test_log/<repo>_test_log/``.  These helpers collect
the per-repo log files and parse pytest summary lines / FAILED-ERROR
short-summary lines best-effort.  The AI (pipeline_test_analyze mode)
refines these numbers; they are the deterministic fallback.
"""

from __future__ import annotations

import re
from pathlib import Path

from TA_main2main_workflow.utils import (
    PIPELINE_TEST_CATEGORY_NPU_IR,
    PIPELINE_TEST_CATEGORY_OPERATOR,
    PIPELINE_TEST_CATEGORY_TRITON_ASCEND,
    PIPELINE_TEST_LOG_DIR_NAME,
    PIPELINE_TEST_RERUN_PREFIX,
)

# "1234 passed, 5 failed, 2 errors, 3 skipped" — the last such line per
# log file is the final short summary.
_SUMMARY_RE = re.compile(
    r"(\d+)\s+passed[,\s]+(\d+)\s+failed[,\s]+(\d+)\s+errors?[,\s]+(\d+)\s+skipped"
)

# Short-summary failure lines: "FAILED path::test - message" /
# "ERROR path::test - message".
_FAILED_LINE_RE = re.compile(r"^(FAILED|ERROR)\s+(\S+?)(?:\s+-\s+(.*))?$", re.M)

# Verbose section header for one case: "____ path::test ____".
_SECTION_RE = re.compile(r"_{5,}\s*(\S+?)\s*_{5,}")

# Category inference markers for the deterministic fallback.
_NPU_IR_MARKERS = ("ascendnpu-ir", "bishengir", "npuir")
_OPERATOR_MARKERS = (
    "lib/dialect/",
    "lib/conversion/",
    "lib/target/ascend/",
    "include/triton/dialect/",
)

_LOG_SUFFIX = "_test_log"


def collect_repo_logs(test_workspace: Path) -> dict[str, list[Path]]:
    """Scan ``<test_workspace>/test_log/<repo>_test_log/`` for log files.

    Returns ``{repo_name: [log_paths]}`` in sorted order.  The repo name
    is the directory name minus the ``_test_log`` suffix (matching the
    human script convention).
    """
    log_root = test_workspace / PIPELINE_TEST_LOG_DIR_NAME
    repos: dict[str, list[Path]] = {}
    if not log_root.is_dir():
        return repos
    for entry in sorted(log_root.iterdir()):
        if not entry.is_dir():
            continue
        name = entry.name
        if name.endswith(_LOG_SUFFIX):
            name = name[: -len(_LOG_SUFFIX)]
        logs = sorted(p for p in entry.rglob("*.log") if p.is_file())
        if logs:
            repos.setdefault(name, []).extend(logs)
    return repos


def parse_pytest_summary(text: str) -> dict[str, int] | None:
    """Parse the LAST pytest summary line in *text*.

    Returns ``{"total", "passed", "failed", "errors", "skipped"}`` or
    None when no summary line exists (e.g. ``-x`` early exit).
    """
    matches = list(_SUMMARY_RE.finditer(text))
    if not matches:
        return None
    passed, failed, errors, skipped = (int(g) for g in matches[-1].groups())
    return {
        "total": passed + failed + errors + skipped,
        "passed": passed,
        "failed": failed,
        "errors": errors,
        "skipped": skipped,
    }


def parse_failed_cases(text: str) -> list[dict]:
    """Parse FAILED / ERROR short-summary lines into case dicts.

    Each dict: ``{"case": node_id, "error": excerpt, "kind": "failed"|"error"}``.
    The excerpt is taken from the case's verbose failure section when
    present, else from the short-summary message.
    """
    cases: list[dict] = []
    for m in _FAILED_LINE_RE.finditer(text):
        kind = "error" if m.group(1) == "ERROR" else "failed"
        case_id = m.group(2)
        short_msg = (m.group(3) or "").strip()
        excerpt = _traceback_excerpt(text, case_id, m.start()) or short_msg
        cases.append(
            {
                "case": case_id,
                "error": (excerpt or "no error message")[:300],
                "kind": kind,
            }
        )
    return cases


def _traceback_excerpt(text: str, case_id: str, failed_line_pos: int) -> str:
    """Bounded failure text for *case_id* around the verbose section.

    Looks backwards from the short-summary line for the section header
    (``____ <node-id> ____``) and returns a capped slice of the failure
    output that follows it.
    """
    before = text[:failed_line_pos]
    header_matches = list(_SECTION_RE.finditer(before))
    base = case_id
    # Section headers carry the full node id (file::test[param]) — match
    # on the tail (test name) because the short line may wrap.
    tail = base.rsplit("::", 1)[-1][:60]
    start = -1
    for hm in reversed(header_matches):
        if hm.group(1).endswith(tail) or base in hm.group(1):
            start = hm.end()
            break
    if start < 0:
        return ""
    block = text[start:failed_line_pos].strip()
    # Stop at the next section header or the short-summary banner.
    stop = len(block)
    for sm in _SECTION_RE.finditer(block):
        stop = sm.start()
        break
    block = block[:stop].strip()
    lines = [ln.strip() for ln in block.splitlines() if ln.strip()]
    if not lines:
        return ""
    # Skip the repeated node-id line pytest prints right after the header.
    if lines[0].startswith(base[:40]) or lines[0].startswith(tail):
        lines = lines[1:]
    return "\n".join(lines)[:400]


def infer_category(error_text: str) -> str:
    """Best-effort problem classification for the deterministic fallback."""
    lowered = error_text.lower()
    if any(m in lowered for m in _NPU_IR_MARKERS):
        return PIPELINE_TEST_CATEGORY_NPU_IR
    if any(m in lowered for m in _OPERATOR_MARKERS):
        return PIPELINE_TEST_CATEGORY_OPERATOR
    return PIPELINE_TEST_CATEGORY_TRITON_ASCEND


def deterministic_repo_analysis(
    repo_logs: list[Path], test_workspace: Path
) -> dict:
    """Deterministic per-repo counts + failing cases for one repo.

    Rerun logs (``rerun_*``) are excluded — they capture single cases
    and would double-count.  Missing summary lines are tolerated:
    counts then fall back to the parsed failure lines.
    """
    counts = {"total": 0, "passed": 0, "failed": 0, "errors": 0, "skipped": 0}
    cases: list[dict] = []
    seen_summary = False
    for path in repo_logs:
        if path.name.startswith(PIPELINE_TEST_RERUN_PREFIX):
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        summary = parse_pytest_summary(text)
        if summary:
            seen_summary = True
            for key in counts:
                counts[key] += summary[key]
        for c in parse_failed_cases(text):
            c["category"] = infer_category(c["error"])
            c["log_file"] = _relative_to_workspace(path, test_workspace)
            cases.append(c)

    if not seen_summary and cases:
        counts["failed"] = sum(1 for c in cases if c["kind"] == "failed")
        counts["errors"] = sum(1 for c in cases if c["kind"] == "error")
        counts["total"] = len(cases)
    return {"counts": counts, "cases": cases}


def fallback_analysis(test_workspace: Path) -> dict:
    """Deterministic analysis in the analysis.json schema (AI fallback)."""
    repos = []
    for repo, logs in collect_repo_logs(test_workspace).items():
        per_repo = deterministic_repo_analysis(logs, test_workspace)
        repos.append(
            {
                "repo": repo,
                **per_repo["counts"],
                "cases": [
                    {
                        "case": c["case"],
                        "error": c["error"],
                        "category": c["category"],
                        "log_file": c["log_file"],
                    }
                    for c in per_repo["cases"]
                ],
            }
        )
    return {"repos": repos}


def _relative_to_workspace(path: Path, test_workspace: Path) -> str:
    try:
        return str(path.relative_to(test_workspace))
    except ValueError:
        return str(path)


def failing_cases(analysis: dict) -> list[tuple[str, dict]]:
    """Flatten *analysis* into ``[(repo, case_dict)]`` in repo order."""
    out: list[tuple[str, dict]] = []
    for repo in analysis.get("repos", []):
        for case in repo.get("cases", []):
            out.append((repo["repo"], case))
    return out


def has_failures(analysis: dict) -> bool:
    """True when any repo reports failed or error cases/counts."""
    for repo in analysis.get("repos", []):
        if repo.get("failed") or repo.get("errors") or repo.get("cases"):
            return True
    return False
