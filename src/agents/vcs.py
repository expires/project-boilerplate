from __future__ import annotations

import os
import subprocess
from pathlib import Path

from .board import AGENTS_DIRNAME, BoardError


def git(cwd: Path | str, *args: str, check: bool = True, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    run_env = {**os.environ, **env} if env else None
    proc = subprocess.run(
        ["git", "-C", str(cwd), *args], capture_output=True, text=True, env=run_env
    )
    if check and proc.returncode != 0:
        raise BoardError(f"git {' '.join(args)} failed: {proc.stderr.strip() or proc.stdout.strip()}")
    return proc


def is_repo(root: Path) -> bool:
    return git(root, "rev-parse", "--is-inside-work-tree", check=False).returncode == 0


def has_head(root: Path) -> bool:
    return git(root, "rev-parse", "--verify", "HEAD", check=False).returncode == 0


def ensure_identity(root: Path) -> None:
    for key, value in (("user.name", "agent-board"), ("user.email", "agent-board@localhost")):
        if git(root, "config", key, check=False).returncode != 0:
            git(root, "config", key, value)


def ensure_repo(root: Path, base: str) -> None:
    if not is_repo(root):
        git(root, "init", "-b", base)
    ensure_identity(root)
    (root / AGENTS_DIRNAME).mkdir(exist_ok=True)
    if not has_head(root):
        git(root, "commit", "--allow-empty", "-m", "chore: initialize")


def branch_exists(root: Path, branch: str) -> bool:
    return git(root, "rev-parse", "--verify", f"refs/heads/{branch}", check=False).returncode == 0


def worktree_add(root: Path, path: Path, branch: str, base: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if branch_exists(root, branch):
        git(root, "worktree", "add", str(path), branch)
    else:
        git(root, "worktree", "add", "-b", branch, str(path), base)


def worktree_remove(root: Path, path: Path) -> None:
    git(root, "worktree", "remove", "--force", str(path), check=False)
    git(root, "worktree", "prune", check=False)


def commit_all(worktree: Path, message: str) -> str | None:
    git(worktree, "add", "-A")
    if git(worktree, "diff", "--cached", "--quiet", check=False).returncode == 0:
        return None
    git(worktree, "commit", "-m", message)
    return git(worktree, "rev-parse", "HEAD").stdout.strip()


def diff(root: Path, base: str, branch: str) -> str:
    return git(root, "diff", f"{base}...{branch}", check=False).stdout


def unmerged(root: Path) -> dict[str, str]:
    output = git(root, "diff", "--name-only", "--diff-filter=U", check=False).stdout
    conflicts: dict[str, str] = {}
    for line in output.splitlines():
        relative = line.strip()
        if not relative:
            continue
        target = Path(root) / relative
        conflicts[relative] = target.read_text(errors="replace") if target.is_file() else ""
    return conflicts


def _abort(root: Path, base: str) -> None:
    git(root, "rebase", "--abort", check=False)
    git(root, "checkout", base, check=False)


def integrate(
    root: Path,
    base: str,
    branch: str,
    resolver=None,
    max_rounds: int = 3,
) -> bool:
    rebase = git(root, "rebase", base, branch, check=False)
    if rebase.returncode != 0:
        for _ in range(max_rounds):
            conflicts = unmerged(root)
            if not conflicts or resolver is None:
                _abort(root, base)
                return False
            resolved = resolver(conflicts)
            if not resolved:
                _abort(root, base)
                return False
            for relative, content in resolved.items():
                target = root / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content)
                git(root, "add", "--", relative)
            cont = git(root, "rebase", "--continue", check=False, env={"GIT_EDITOR": "true"})
            if cont.returncode == 0:
                break
        else:
            _abort(root, base)
            return False
    git(root, "checkout", base, check=False)
    merge = git(root, "merge", "--ff-only", branch, check=False)
    return merge.returncode == 0


def delete_branch(root: Path, branch: str) -> None:
    git(root, "branch", "-D", branch, check=False)
