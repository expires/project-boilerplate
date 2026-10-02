from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__
from .board import AGENTS_DIRNAME, COLUMNS, Board, BoardError, card_as_dict, find_project_root, slugify, utcnow
from .config import scaffold


def _resolve_root(args: argparse.Namespace) -> Path:
    if args.root:
        return Path(args.root).resolve()
    return find_project_root()


def _print_card(card, as_json: bool) -> None:
    if as_json:
        print(json.dumps(card_as_dict(card), indent=2))
        return
    print(f"{card.id}  [{card.status}]  {card.title}")
    meta = {
        "priority": card.priority,
        "route": card.route,
        "depends_on": card.depends_on,
        "attempts": card.attempts,
        "review_cycles": card.review_cycles,
        "branch": card.branch,
    }
    for key, value in meta.items():
        print(f"  {key}: {value}")
    if card.acceptance_criteria:
        print("  acceptance_criteria:")
        for item in card.acceptance_criteria:
            print(f"    - {item}")
    if card.body:
        print()
        print(card.body)


def cmd_init(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve() if args.root else Path.cwd()
    scaffold(root, project_name=args.name or "")
    print(f"initialized {root / AGENTS_DIRNAME}")
    print(f"  board: {', '.join(COLUMNS)}")
    print("next:")
    print(f"  - edit {root / AGENTS_DIRNAME / 'project.md'}")
    print(f"  - edit {root / AGENTS_DIRNAME / 'config.json'}")
    print("  - add work: agents add \"<feature>\"")
    return 0


def cmd_add(args: argparse.Namespace) -> int:
    board = Board(_resolve_root(args))
    specs = board.agents / "specs"
    specs.mkdir(parents=True, exist_ok=True)
    stamp = utcnow().strftime("%Y%m%d-%H%M%S")
    path = specs / f"{stamp}-{slugify(args.text)}.md"
    path.write_text(args.text.strip() + "\n")
    print(str(path))
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    board = Board(_resolve_root(args))
    cards = board.cards()
    if args.json:
        print(json.dumps([card_as_dict(card) for card in cards], indent=2))
        return 0
    for column in COLUMNS:
        group = [card for card in cards if card.status == column]
        print(f"{column} ({len(group)})")
        for card in sorted(group, key=lambda item: (int(item.priority), item.id)):
            print(f"  {card.id}  {card.title}")
    return 0


def cmd_card(args: argparse.Namespace) -> int:
    board = Board(_resolve_root(args))
    _print_card(board.find(args.card_id), args.json)
    return 0


def cmd_move(args: argparse.Namespace) -> int:
    board = Board(_resolve_root(args))
    card = board.move(args.card_id, args.to, note=args.note or "")
    print(f"{card.id} -> {card.status}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agents", description="Local filesystem agent board")
    parser.add_argument("--version", action="version", version=f"agent-board {__version__}")
    parser.add_argument("--root", help="project root containing .agents (defaults to searching upward)")
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init", help="scaffold .agents/ in the current directory")
    init.add_argument("--name", help="project name for the brief header")
    init.set_defaults(func=cmd_init)

    add = sub.add_parser("add", help="drop a feature spec for the PM to decompose")
    add.add_argument("text", help="feature description")
    add.set_defaults(func=cmd_add)

    status = sub.add_parser("status", help="show the board")
    status.add_argument("--json", action="store_true")
    status.set_defaults(func=cmd_status)

    card = sub.add_parser("card", help="show one card")
    card.add_argument("card_id")
    card.add_argument("--json", action="store_true")
    card.set_defaults(func=cmd_card)

    move = sub.add_parser("move", help="move a card between columns")
    move.add_argument("card_id")
    move.add_argument("--to", required=True, choices=list(COLUMNS))
    move.add_argument("--note", default="")
    move.set_defaults(func=cmd_move)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except BoardError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
