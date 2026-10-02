from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from . import roles, shell, vcs
from .board import AGENTS_DIRNAME, Board, BoardError, Card, log
from .config import load_config
from .logs import log_event
from .validate import changed_line_count, is_protected, safe_path


class Orchestrator:
    def __init__(self, root: Path | str, config: dict[str, Any] | None = None, llm: roles.LLM | None = None):
        self.root = Path(root).resolve()
        self.board = Board(self.root)
        self.config = config or load_config(self.root)
        self.llm = llm or roles.default_llm
        self.base = self.config.get("vcs", {}).get("base_branch", "main")
        self.branch_prefix = self.config.get("vcs", {}).get("branch_prefix", "agent/")
        self.commit_prefix = self.config.get("vcs", {}).get("commit_prefix", "feat(agent)")

    @property
    def agents(self) -> Path:
        return self.root / AGENTS_DIRNAME

    def _requeue(self) -> int:
        return len(self.board.requeue_expired())

    def _intake(self) -> int:
        if not self.config.get("governance", {}).get("decompose_specs", False):
            return 0
        specs = self.agents / "specs"
        if not specs.is_dir():
            return 0
        processed = specs / "processed"
        failed = specs / "failed"
        processed.mkdir(exist_ok=True)
        failed.mkdir(exist_ok=True)
        count = 0
        for path in sorted(specs.glob("*.md")):
            text = path.read_text()
            try:
                tasks = roles.pm_decompose(self.config, self.root, text, path.stem, self.llm)
            except BoardError as exc:
                log(f"spec {path.name} failed: {exc}")
                path.rename(failed / path.name)
                continue
            id_by_key: dict[str, str] = {}
            for task in tasks:
                card = self.board.create(
                    task["title"],
                    body=task.get("spec") or text,
                    route=task["route"],
                    files_hint=task["files"],
                    acceptance_criteria=task["acceptance_criteria"],
                    priority=task.get("priority", 100),
                    spec=task.get("spec") or text,
                )
                id_by_key[task["key"]] = card.id
            for task in tasks:
                deps = [id_by_key[key] for key in task["depends_on"]]
                if deps:
                    self.board.update(id_by_key[task["key"]], depends_on=deps)
            path.rename(processed / path.name)
            count += 1
            log(f"planned {len(tasks)} task(s) from {path.name}")
        return count

    def _claim(self) -> int:
        cap = int(self.config["governance"]["max_concurrency"])
        lease = int(self.config["governance"].get("lease_minutes", 45))
        open_prs = sum(1 for card in self.board.cards() if card.status == "review")
        slots = min(cap - len(self.board.cards(("in_progress",))), cap - open_prs)
        claimed = 0
        if slots <= 0:
            return 0
        for card in self.board.claimable(self.board.closed_ids())[:slots]:
            self.board.claim(card.id, lease)
            claimed += 1
        return claimed

    def _context(self, card: Card, worktree: Path) -> str:
        limit = int(self.config.get("cost_controls", {}).get("max_input_chars", 48000)) // 2
        chunks: list[str] = []
        for rel in card.files_hint:
            try:
                target = safe_path(worktree, rel)
            except BoardError:
                continue
            if target.is_file():
                chunks.append(f"### {rel}\n{target.read_text(errors='replace')}")
            else:
                chunks.append(f"### {rel}\n(new file)")
        text = "\n\n".join(chunks)
        return text[:limit]

    def _feedback(self, card: Card) -> str:
        parts = [card.last_review_summary.strip()] if card.last_review_summary else []
        parts.extend(f"- {item}" for item in card.blocking_issues)
        return "\n".join(parts).strip()

    def _validate_writes(self, card: Card, items: Any, worktree: Path) -> list[tuple[Path, str]]:
        if not isinstance(items, list) or not items:
            raise BoardError("worker response contained no files")
        max_files = int(self.config["context"].get("max_files_per_task", 8)) + 2
        if len(items) > max_files:
            raise BoardError(f"worker produced {len(items)} files; cap is {max_files}")
        max_diff = int(self.config.get("cost_controls", {}).get("max_diff_lines", 600))
        writes: list[tuple[Path, str]] = []
        total = 0
        for item in items:
            if not isinstance(item, dict) or not item.get("path") or not isinstance(item.get("content"), str):
                raise BoardError("each file needs a 'path' and string 'content'")
            relative = str(item["path"])
            if is_protected(self.config, relative):
                raise BoardError(f"worker attempted to write protected path '{relative}'")
            target = safe_path(worktree, relative)
            before = target.read_text(errors="replace") if target.is_file() else ""
            total += changed_line_count(before, item["content"])
            writes.append((target, item["content"]))
        if total > max_diff:
            raise BoardError(f"worker diff is {total} lines; cap is {max_diff}")
        if total == 0:
            raise BoardError("worker produced no changes")
        return writes

    def _verify(self, card: Card, worktree: Path) -> None:
        verify = self.config.get("verify", {})
        commands = list(verify.get("commands") or [])
        if not verify.get("enabled") or not commands:
            return
        results = shell.run_commands(worktree, commands)
        for result in results:
            log_event(self.root, card.id, f"verify ok ({result['command']})")
            if result["output"].strip():
                log_event(self.root, card.id, result["output"].strip())

    def _fail_card(self, card: Card, reason: str) -> None:
        attempts = int(card.attempts) + 1
        max_attempts = int(self.config["governance"].get("max_task_attempts", 2))
        self.board.update(card.id, attempts=attempts)
        log_event(self.root, card.id, f"attempt {attempts}/{max_attempts} failed: {reason}")
        if attempts >= max_attempts:
            self.board.move(card.id, "blocked", note=f"failed: {reason}")
        else:
            self.board.move(card.id, "not_started", note=f"retry {attempts}/{max_attempts}: {reason}")

    def _resolver(self, card: Card, base: str, branch: str):
        def resolve(conflicts: dict[str, str]) -> dict[str, str]:
            log_event(self.root, card.id, f"resolving {len(conflicts)} conflict(s) on {branch}")
            return roles.resolve_conflicts(self.config, self.root, base, branch, conflicts, self.llm)

        return resolve

    def _work(self, card: Card) -> int:
        vcs.ensure_repo(self.root, self.base)
        vcs.require_head(self.root)
        worktree = self.agents / "worktrees" / card.id
        branch = card.branch or f"{self.branch_prefix}{card.id.lower()}"
        vcs.worktree_remove(self.root, worktree)
        try:
            log_event(self.root, card.id, f"worker started on {branch}")
            vcs.worktree_add(self.root, worktree, branch, self.base)
            context = self._context(card, worktree)
            payload = roles.worker_propose(self.config, self.root, card, context, self._feedback(card), self.llm)
            writes = self._validate_writes(card, payload.get("files"), worktree)
            for target, content in writes:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content)
            self._verify(card, worktree)
            commit = vcs.commit_all(worktree, f"{self.commit_prefix}: {card.id} {card.title}")
            if commit is None:
                raise BoardError("worker produced no changes")
            self.board.update(card.id, branch=branch, attempts=int(card.attempts) + 1)
            self.board.move(card.id, "review", note="worker produced changes")
            log_event(self.root, card.id, f"committed {len(writes)} file(s) to {branch}")
            log(f"{card.id}: {len(writes)} file(s) committed to {branch}")
            return 1
        except BoardError as exc:
            log(f"{card.id} failed: {exc}")
            self._fail_card(card, str(exc))
            return 1
        finally:
            vcs.worktree_remove(self.root, worktree)

    def _review(self, card: Card) -> int:
        if not card.branch:
            self._fail_card(card, "card has no branch to review")
            return 1
        max_cycles = int(self.config["governance"].get("review_retries", 3))
        try:
            diff_text = vcs.diff(self.root, self.base, card.branch)
            verdict = roles.reviewer_verdict(
                self.config, self.root, card, diff_text, int(card.review_cycles) + 1, max_cycles, self.llm
            )
        except BoardError as exc:
            log(f"{card.id} review failed: {exc}")
            self._fail_card(card, str(exc))
            return 1
        log_event(self.root, card.id, f"review verdict: {verdict['verdict']} ({verdict['summary']})")
        if verdict["verdict"] == "approve":
            resolver = self._resolver(card, self.base, card.branch)
            if vcs.integrate(self.root, self.base, card.branch, resolver):
                self.board.move(card.id, "closed", note="review approved and merged")
                vcs.delete_branch(self.root, card.branch)
                log_event(self.root, card.id, f"merged into {self.base}")
                log(f"{card.id}: approved and merged")
            else:
                self.board.move(card.id, "blocked", note="merge conflict; needs resolution")
                log_event(self.root, card.id, "merge conflict unresolved")
                log(f"{card.id}: merge conflict")
            return 1
        cycles = int(card.review_cycles) + 1
        self.board.update(
            card.id,
            review_cycles=cycles,
            last_review_summary=verdict["summary"],
            blocking_issues=verdict["blocking_issues"],
        )
        if cycles >= max_cycles:
            self.board.move(card.id, "blocked", note=f"review retries exhausted ({cycles})")
        else:
            self.board.move(card.id, "not_started", note=f"changes requested (cycle {cycles})")
        log(f"{card.id}: changes requested (cycle {cycles}/{max_cycles})")
        return 1

    def tick(self) -> int:
        actions = self._requeue()
        actions += self._intake()
        actions += self._claim()
        for card in self.board.cards(("in_progress",)):
            actions += self._work(card)
        for card in self.board.cards(("review",)):
            actions += self._review(card)
        return actions

    def run(self, once: bool = False, watch: bool = True, max_ticks: int | None = None) -> int:
        ticks = 0
        poll = float(self.config.get("governance", {}).get("poll_seconds", 5) or 5)
        while True:
            actions = self.tick()
            ticks += 1
            log(f"tick {ticks}: {actions} action(s)")
            if once or (max_ticks is not None and ticks >= max_ticks):
                break
            if actions == 0:
                if not watch:
                    break
                time.sleep(poll)
        return ticks
