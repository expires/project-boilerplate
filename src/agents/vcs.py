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


def require_head(root: Path) -> None:
    if not has_head(root):
        raise BoardError("this repository has no commits yet; make a first commit before running the board")


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


def diff_stat(root: Path, base: str, branch: str) -> str:
    return git(root, "diff", "--stat", f"{base}...{branch}", check=False).stdout


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


def is_ancestor(root: Path, ancestor: str, descendant: str) -> bool:
    return git(root, "merge-base", "--is-ancestor", ancestor, descendant, check=False).returncode == 0


def tracked_changes(root: Path) -> list[str]:
    output = git(root, "status", "--porcelain", "--untracked-files=no", check=False).stdout
    paths: list[str] = []
    for line in output.splitlines():
        if len(line) < 4:
            continue
        path = line[3:].strip()
        if " -> " in path:
            path = path.split(" -> ")[-1]
        paths.append(path.strip('"'))
    return paths


def stash_paths(root: Path, paths: list[str]) -> bool:
    if not paths:
        return False
    return git(root, "stash", "push", "--", *paths, check=False).returncode == 0


def stash_pop(root: Path) -> bool:
    return git(root, "stash", "pop", check=False).returncode == 0


def prepare_branch(root: Path, base: str, branch: str) -> bool:
    if not branch_exists(root, branch) or is_ancestor(root, base, branch):
        return True
    git(root, "checkout", base, check=False)
    rebase = git(root, "rebase", base, branch, check=False)
    git(root, "checkout", base, check=False)
    if rebase.returncode != 0:
        git(root, "rebase", "--abort", check=False)
        return False
    return True


def _abort(root: Path, base: str) -> None:
    git(root, "rebase", "--abort", check=False)
    git(root, "checkout", base, check=False)


def integrate(
    root: Path,
    base: str,
    branch: str,
    resolver=None,
    max_rounds: int = 3,
) -> str:
    dirty = tracked_changes(root)
    others = [path for path in dirty if not path.startswith(AGENTS_DIRNAME + "/")]
    if others:
        return "dirty"
    agents_dirty = [path for path in dirty if path.startswith(AGENTS_DIRNAME + "/")]
    stashed = stash_paths(root, agents_dirty)
    try:
        git(root, "checkout", base, check=False)
        if not is_ancestor(root, base, branch):
            rebase = git(root, "rebase", base, branch, check=False)
            if rebase.returncode != 0:
                status = "conflict"
                for _ in range(max_rounds):
                    conflicts = unmerged(root)
                    if not conflicts or resolver is None:
                        status = "conflict"
                        break
                    resolved = resolver(conflicts)
                    if not resolved:
                        status = "conflict"
                        break
                    for relative, content in resolved.items():
                        target = root / relative
                        target.parent.mkdir(parents=True, exist_ok=True)
                        target.write_text(content)
                        git(root, "add", "--", relative)
                    cont = git(root, "rebase", "--continue", check=False, env={"GIT_EDITOR": "true"})
                    if cont.returncode == 0:
                        status = "ok"
                        break
                if status != "ok":
                    _abort(root, base)
                    return "conflict"
            git(root, "checkout", base, check=False)
        merge = git(root, "merge", "--ff-only", branch, check=False)
        return "merged" if merge.returncode == 0 else "behind"
    finally:
        if stashed:
            stash_pop(root)


def delete_branch(root: Path, branch: str) -> None:
    git(root, "branch", "-D", branch, check=False)
