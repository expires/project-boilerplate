from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

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


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def is_running(root: Path | str) -> bool:
    pid = read_pid(root)
    return bool(pid) and _alive(pid)


def runner_state(root: Path | str) -> dict[str, Any]:
    _, log_path = _paths(root)
    pid = read_pid(root)
    running = bool(pid) and _alive(pid)
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
        log.flush()
        process = subprocess.Popen(
            command,
            cwd=str(root),
            stdout=log,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
    pid_path.write_text(str(process.pid))
    return process.pid


def stop(root: Path | str, timeout: float = 5.0) -> bool:
    pid = read_pid(root)
    pid_path, _ = _paths(root)
    if not pid:
        pid_path.unlink(missing_ok=True)
        return False
    had_runner = _alive(pid)
    if had_runner:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        deadline = time.time() + timeout
        while time.time() < deadline and _alive(pid):
            time.sleep(0.1)
        if _alive(pid):
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
    pid_path.unlink(missing_ok=True)
    return had_runner
