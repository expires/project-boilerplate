from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Any

from . import daemon
from .board import AGENTS_DIRNAME, BoardError


def _paths(root: Path | str) -> tuple[Path, Path]:
    agents = Path(root) / AGENTS_DIRNAME
    return agents / "runner.pid", agents / "runner.log"


def read_pid(root: Path | str) -> int | None:
    pid_path, _ = _paths(root)
    if not pid_path.is_file():
        return None
    try:
        return int(pid_path.read_text().strip())
    except (ValueError, OSError):
        return None


def is_running(root: Path | str) -> bool:
    return daemon.alive(read_pid(root))


def runner_state(root: Path | str) -> dict[str, Any]:
    _, log_path = _paths(root)
    pid = read_pid(root)
    running = daemon.alive(pid)
    return {"running": running, "pid": pid if running else None, "log": str(log_path)}


def start_detached(
    root: Path | str,
    command: list[str] | None = None,
    concurrency: int | None = None,
) -> int:
    root = Path(root).resolve()
    if is_running(root):
        raise BoardError(f"a runner is already active (pid {read_pid(root)}); use `agents stop` first")
    pid_path, log_path = _paths(root)
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    pid_path.unlink(missing_ok=True)
    if command is None:
        command = [sys.executable, "-m", "agents.cli", "--root", str(root), "run"]
        if concurrency:
            command += ["--concurrency", str(concurrency)]
    with log_path.open("a") as log:
        log.write(f"--- runner start {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} ---\n")
    pid = daemon.spawn(command, str(root), log_path)
    pid_path.write_text(str(pid))
    return pid


def stop(root: Path | str, timeout: float = 5.0) -> bool:
    pid_path, _ = _paths(root)
    stopped = daemon.terminate(read_pid(root), timeout)
    pid_path.unlink(missing_ok=True)
    return stopped
