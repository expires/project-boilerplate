from __future__ import annotations

import difflib
import fnmatch
from pathlib import Path
from typing import Any

from .board import BoardError


def normalize_path(value: str) -> str:
    text = str(value).replace("\\", "/").strip()
    while text.startswith("./"):
        text = text[2:]
    return text.lstrip("/")


def is_protected(config: dict[str, Any], path: str) -> bool:
    normalized = normalize_path(path)
    patterns = config.get("context", {}).get("protected_paths", [])
    for pattern in patterns:
        candidate = normalize_path(pattern)
        if fnmatch.fnmatch(normalized, candidate):
            return True
        if candidate.endswith("/**") and normalized == candidate[:-3].rstrip("/"):
            return True
    return False


def safe_path(worktree: Path, relpath: str) -> Path:
    rel = normalize_path(relpath)
    if not rel or rel.startswith("..") or "/../" in rel:
        raise BoardError(f"unsafe path: {relpath!r}")
    base = Path(worktree).resolve()
    target = (base / rel).resolve()
    if target != base and base not in target.parents:
        raise BoardError(f"path escapes the workspace: {relpath!r}")
    return target


def changed_line_count(before: str, after: str) -> int:
    if before == after:
        return 0
    diff = difflib.unified_diff(before.splitlines(), after.splitlines(), lineterm="")
    return sum(
        1
        for line in diff
        if line[:1] in ("+", "-") and not line.startswith(("+++", "---"))
    )
