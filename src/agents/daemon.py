from __future__ import annotations

import os
import signal
import time
from pathlib import Path


def alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def spawn(command: list[str], cwd: str, log_path: Path) -> int:
    devnull = os.open(os.devnull, os.O_RDONLY)
    log_fd = os.open(str(log_path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    try:
        pid = os.fork()
    except OSError:
        os.close(devnull)
        os.close(log_fd)
        raise
    if pid == 0:
        try:
            os.setsid()
            os.chdir(cwd)
            os.dup2(devnull, 0)
            os.dup2(log_fd, 1)
            os.dup2(log_fd, 2)
            os.execvp(command[0], command)
        except BaseException:
            os._exit(127)
    os.close(devnull)
    os.close(log_fd)
    return pid


def terminate(pid: int | None, timeout: float = 5.0) -> bool:
    if not alive(pid):
        return False
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return False
    deadline = time.time() + timeout
    while time.time() < deadline and alive(pid):
        time.sleep(0.1)
    if alive(pid):
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    return True
