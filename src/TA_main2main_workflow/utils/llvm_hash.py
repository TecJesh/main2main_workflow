"""LLVM hash pin resolution helpers.

Upstream triton-ascend historically pinned its LLVM commit in
``cmake/llvm-hash.txt`` (a plain 40-char hash).  Newer upstream code
moved the pin to ``cmake/llvm-info.json`` under the ``llvm_hash``
field.  These helpers resolve the pin from either location so the
workflow keeps working across the format transition.

Precedence: the legacy txt file wins when present; otherwise the
``llvm_hash`` field of ``cmake/llvm-info.json``.  Both resolve to the
bare hash string used for ``git checkout``.
"""

from __future__ import annotations

import json
from pathlib import Path

from TA_main2main_workflow.utils.git import run_git

LEGACY_LLVM_HASH_FILE = "cmake/llvm-hash.txt"
LLVM_INFO_FILE = "cmake/llvm-info.json"

#: Both pin locations, in precedence order — used to detect commits
#: that touch the LLVM pin during step planning.
LLVM_HASH_PATHS = (LEGACY_LLVM_HASH_FILE, LLVM_INFO_FILE)


def _parse_llvm_info(text: str) -> str:
    """Extract the ``llvm_hash`` field from llvm-info.json content.

    Returns "" if the content is missing or malformed.
    """
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return ""
    if isinstance(data, dict):
        return str(data.get("llvm_hash", "")).strip()
    return ""


def read_llvm_hash(repo: Path) -> str:
    """Read the LLVM pin from the working tree.

    Tries ``cmake/llvm-hash.txt`` first (legacy), then the ``llvm_hash``
    field of ``cmake/llvm-info.json``.  Returns "" when neither exists.
    """
    legacy = repo / LEGACY_LLVM_HASH_FILE
    if legacy.exists():
        value = legacy.read_text(encoding="utf-8").strip()
        if value:
            return value
    info = repo / LLVM_INFO_FILE
    if info.exists():
        return _parse_llvm_info(info.read_text(encoding="utf-8"))
    return ""


def llvm_hash_at_rev(repo: Path, rev: str) -> str:
    """Resolve the LLVM pin as of a git revision.

    Uses ``git show`` so both pin locations are honored at any point in
    history (e.g. a rev from before the format migration).  Returns ""
    when neither exists at *rev*.
    """
    for path in LLVM_HASH_PATHS:
        try:
            content = run_git(repo, "show", f"{rev}:{path}")
        except Exception:
            continue
        value = (
            content.strip()
            if path == LEGACY_LLVM_HASH_FILE
            else _parse_llvm_info(content)
        )
        if value:
            return value
    return ""
