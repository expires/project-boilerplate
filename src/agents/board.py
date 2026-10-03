from __future__ import annotations

import contextlib
import dataclasses
import fcntl
import os
import re
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

from . import frontmatter

COLUMNS = ("not_started", "in_progress", "review", "blocked", "closed")
ACTIVE_COLUMNS = ("not_started", "in_progress", "review")
AGENTS_DIRNAME = ".agents"

_SLUG = re.compile(r"[^a-z0-9]+")
_ID = re.compile(r"T-(\d+)")


class BoardError(Exception):
    pass


class OversizeError(BoardError):
    pass


def log(message: str) -> None:
    print(f"[agents] {message}", file=sys.stderr)


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def iso(moment: datetime) -> str:
    return moment.isoformat().replace("+00:00", "Z")


def now_iso() -> str:
    return iso(utcnow())


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def slugify(text: str) -> str:
    slug = _SLUG.sub("-", text.strip().lower()).strip("-")
    return slug[:48] or "task"


def find_project_root(start: Path | str | None = None) -> Path:
    current = Path(start or Path.cwd()).resolve()
    for candidate in (current, *current.parents):
        if (candidate / AGENTS_DIRNAME).is_dir():
            return candidate
    raise BoardError(
        f"no {AGENTS_DIRNAME}/ found in {current} or its parents; run `agents init` first"
    )


@dataclass
class Card:
    id: str
    title: str
    status: str = "not_started"
    priority: int = 100
    depends_on: list[str] = field(default_factory=list)
    route: str = "worker"
    acceptance_criteria: list[str] = field(default_factory=list)
    files_hint: list[str] = field(default_factory=list)
    context_files: list[str] = field(default_factory=list)
    skills: list[str] = field(default_factory=list)
    group: str = ""
    conflicts_with: list[str] = field(default_factory=list)
    max_diff_lines: int = 0
    max_output_tokens: int = 0
    depth: int = 1
    attempts: int = 0
    review_cycles: int = 0
    branch: str = ""
    base: str = "main"
    lease_until: str = ""
    spec: str = ""
    last_review_summary: str = ""
    blocking_issues: list[str] = field(default_factory=list)
    created_at: str = ""
    updated_at: str = ""
    history: list[dict[str, str]] = field(default_factory=list)
    body: str = ""

    _FIELDS = (
        "id",
        "title",
        "status",
        "priority",
        "depends_on",
        "route",
        "acceptance_criteria",
        "files_hint",
        "context_files",
        "skills",
        "group",
        "conflicts_with",
        "max_diff_lines",
        "max_output_tokens",
        "depth",
        "attempts",
        "review_cycles",
        "branch",
        "base",
        "lease_until",
        "spec",
        "last_review_summary",
        "blocking_issues",
        "created_at",
        "updated_at",
        "history",
    )

    def to_frontmatter(self) -> dict[str, Any]:
        return {name: getattr(self, name) for name in self._FIELDS}

    def to_text(self) -> str:
        block = frontmatter.dumps(self.to_frontmatter())
        body = self.body.rstrip()
        return f"---\n{block}\n---\n\n{body}\n"

    def note(self, text: str) -> None:
        self.updated_at = now_iso()
        self.history.append({"at": self.updated_at, "note": text})
        self.history = self.history[-20:]

    @classmethod
    def from_text(cls, text: str) -> "Card":
        data, body = frontmatter.parse(text)
        kwargs = {name: data[name] for name in cls._FIELDS if name in data}
        if "id" not in kwargs or "title" not in kwargs:
            raise BoardError("card is missing required 'id' or 'title' frontmatter")
        return cls(body=body, **kwargs)


class Board:
    def __init__(self, root: Path | str):
        self.root = Path(root).resolve()
        self.agents = self.root / AGENTS_DIRNAME

    @property
    def board_dir(self) -> Path:
        return self.agents / "board"

    def column_dir(self, column: str) -> Path:
        if column not in COLUMNS:
            raise BoardError(f"unknown column '{column}'; expected one of {', '.join(COLUMNS)}")
        return self.board_dir / column

    @contextlib.contextmanager
    def lock(self) -> Iterator[None]:
        lock_path = self.board_dir / ".lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(lock_path, "w")
        try:
            fcntl.flock(handle, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)
            handle.close()

    def _write(self, card: Card, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        handle = tempfile.NamedTemporaryFile(
            "w", dir=destination.parent, prefix=".tmp-", suffix=".md", delete=False
        )
        try:
            handle.write(card.to_text())
            handle.flush()
            os.fsync(handle.fileno())
        finally:
            handle.close()
        os.replace(handle.name, destination)

    def _find(self, card_id: str) -> tuple[Card, Path] | None:
        for column in COLUMNS:
            directory = self.column_dir(column)
            if not directory.is_dir():
                continue
            for path in sorted(directory.glob(f"{card_id}*.md")):
                if path.name == f"{card_id}.md" or path.name.startswith(f"{card_id}-"):
                    return Card.from_text(path.read_text()), path
        return None

    def find(self, card_id: str) -> Card:
        found = self._find(card_id)
        if not found:
            raise BoardError(f"card '{card_id}' not found")
        return found[0]

    def cards(self, columns: tuple[str, ...] = COLUMNS) -> list[Card]:
        result: list[Card] = []
        for column in columns:
            directory = self.column_dir(column)
            if not directory.is_dir():
                continue
            for path in sorted(directory.glob("*.md")):
                if path.name.startswith(".tmp-"):
                    continue
                result.append(Card.from_text(path.read_text()))
        return result

    def closed_ids(self) -> set[str]:
        return {card.id for card in self.cards(("closed",))}

    def next_id(self) -> str:
        highest = 0
        for card in self.cards():
            match = _ID.fullmatch(card.id.strip())
            if match:
                highest = max(highest, int(match.group(1)))
        return f"T-{highest + 1:03d}"

    def create(self, title: str, body: str = "", **fields: Any) -> Card:
        with self.lock():
            card_id = fields.pop("id", None) or self.next_id()
            card = Card(
                id=card_id,
                title=title,
                created_at=now_iso(),
                updated_at=now_iso(),
                body=body,
                **fields,
            )
            card.status = "not_started"
            card.note("created")
            self._write(card, self.column_dir("not_started") / f"{card.id}-{slugify(title)}.md")
            return card

    def update(self, card_id: str, **fields: Any) -> Card:
        with self.lock():
            found = self._find(card_id)
            if not found:
                raise BoardError(f"card '{card_id}' not found")
            card, path = found
            for name, value in fields.items():
                if name not in Card._FIELDS and name != "body":
                    raise BoardError(f"unknown card field '{name}'")
                setattr(card, name, value)
            card.note("updated")
            self._write(card, path)
            return card

    def move(self, card_id: str, column: str, note: str = "") -> Card:
        self.column_dir(column)
        with self.lock():
            found = self._find(card_id)
            if not found:
                raise BoardError(f"card '{card_id}' not found")
            card, path = found
            if card.status == column:
                return card
            card.status = column
            if column != "in_progress":
                card.lease_until = ""
            card.note(note or f"moved to {column}")
            destination = self.column_dir(column) / path.name
            self._write(card, destination)
            if destination != path and path.exists():
                path.unlink()
            return card

    def delete(self, card_id: str) -> Card:
        with self.lock():
            found = self._find(card_id)
            if not found:
                raise BoardError(f"card '{card_id}' not found")
            card, path = found
            path.unlink()
            return card

    def claim(self, card_id: str, lease_minutes: int = 45) -> Card:
        with self.lock():
            found = self._find(card_id)
            if not found:
                raise BoardError(f"card '{card_id}' not found")
            card, path = found
            if card.status not in ("not_started", "changes_requested"):
                raise BoardError(f"card '{card_id}' is '{card.status}'; cannot claim")
            card.status = "in_progress"
            card.lease_until = iso(utcnow() + timedelta(minutes=lease_minutes))
            card.note("claimed")
            destination = self.column_dir("in_progress") / path.name
            self._write(card, destination)
            if destination != path and path.exists():
                path.unlink()
            return card

    def eligible(self, card: Card, closed: set[str]) -> bool:
        return card.status == "not_started" and all(dep in closed for dep in card.depends_on)

    def claimable(self, closed: set[str]) -> list[Card]:
        return sorted(
            (card for card in self.cards(("not_started",)) if self.eligible(card, closed)),
            key=lambda card: (int(card.priority), card.id),
        )

    def requeue_expired(self) -> list[str]:
        requeued: list[str] = []
        current = utcnow()
        for card in self.cards(("in_progress",)):
            lease = parse_iso(card.lease_until)
            if lease is None or lease >= current:
                continue
            self.move(card.id, "not_started", note="lease expired; requeued")
            requeued.append(card.id)
        return requeued

    def update_lease(self, card_id: str, lease_minutes: int = 45) -> Card:
        return self.update(card_id, lease_until=iso(utcnow() + timedelta(minutes=lease_minutes)))


def card_as_dict(card: Card) -> dict[str, Any]:
    data = dataclasses.asdict(card)
    return data
