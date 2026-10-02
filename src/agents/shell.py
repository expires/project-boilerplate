from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from .board import BoardError

TAIL = 2000


def run_commands(cwd: Path, commands: list[str], timeout: int = 300) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for command in commands:
        try:
            proc = subprocess.run(
                command, cwd=str(cwd), shell=True, capture_output=True, text=True, timeout=timeout
            )
        except subprocess.TimeoutExpired as exc:
            raise BoardError(f"verify timed out after {timeout}s: {command}") from exc
        output = (proc.stdout or "") + (proc.stderr or "")
        results.append({"command": command, "returncode": proc.returncode, "output": output[-TAIL:]})
        if proc.returncode != 0:
            raise BoardError(f"verify failed ({command}): {output[-TAIL:].strip() or 'no output'}")
    return results
