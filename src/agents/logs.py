from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from .board import AGENTS_DIRNAME


def log_event(root: Path, card_id: str, message: str) -> None:
    directory = Path(root) / AGENTS_DIRNAME / "logs"
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    with (directory / f"{card_id}.log").open("a") as handle:
        for line in str(message).splitlines() or [""]:
            handle.write(f"{stamp} {line}\n")


def read_log(root: Path, card_id: str) -> str:
    path = Path(root) / AGENTS_DIRNAME / "logs" / f"{card_id}.log"
    return path.read_text(errors="replace") if path.is_file() else ""
