from __future__ import annotations

import fnmatch
from typing import Any


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
