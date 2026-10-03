from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__, roles, runner, ui, vcs
from .board import AGENTS_DIRNAME, COLUMNS, Board, BoardError, card_as_dict, find_project_root
from .config import load_config, scaffold
from .llm import load_dotenv, load_usage
from .logs import log_event, read_log
from .orchestrator import Orchestrator
from .skill import install_skill
from .skills import available_skills, validate_skills


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
    print(f"  - set AI_ECONOMY_API_KEY in {root / '.env'}")
    print(f"  - edit {root / AGENTS_DIRNAME / 'project.md'}")
    print(f"  - edit {root / AGENTS_DIRNAME / 'config.json'}")
    print("  - add work: agents add \"<task title>\"")
    print("  - run it:   agents run")
    config = load_config(root)
    ui_cfg = config.get("ui", {})
    if not args.no_ui and ui_cfg.get("enabled", True) and ui_cfg.get("autostart", True):
        url = ui.start_detached(root, ui_cfg.get("host", "127.0.0.1"), int(ui_cfg.get("port", 8765)))
        if url:
            print(f"  - board ui: {url}")
    return 0


def cmd_ui(args: argparse.Namespace) -> int:
    root = _resolve_root(args)
    config = load_config(root)
    ui_cfg = config.get("ui", {})
    host = args.host or ui_cfg.get("host", "127.0.0.1")
    port = args.port or int(ui_cfg.get("port", 8765))
    if args.stop:
        stopped = ui.stop(root)
        print("ui stopped" if stopped else "no active ui")
        return 0
    if args.detach:
        print(f"ui: {ui.start_detached(root, host, port)}")
        return 0
    ui.serve(root, host, port)
    return 0


def _read_text(value: str | None) -> str:
    if not value or value == "-":
        return sys.stdin.read()
    return Path(value).read_text()


def cmd_add(args: argparse.Namespace) -> int:
    root = _resolve_root(args)
    board = Board(root)
    body = _read_text(args.body_file).strip() if args.body_file else ""
    spec = body or args.text
    for dep in [*(args.depends_on or []), *(args.conflicts_with or [])]:
        board.find(dep)
    validate_skills(root, list(args.skill or []))
    card = board.create(
        args.text,
        body=spec,
        spec=spec,
        route=args.route or "backend",
        priority=args.priority if args.priority is not None else 100,
        files_hint=list(args.file or []),
        context_files=list(args.context_file or []),
        skills=list(args.skill or []),
        group=args.group or "",
        conflicts_with=list(args.conflicts_with or []),
        max_diff_lines=args.max_diff_lines or 0,
        max_output_tokens=args.max_output_tokens or 0,
        acceptance_criteria=list(args.criterion or []),
        depends_on=list(args.depends_on or []),
    )
    print(card.id)
    return 0


def cmd_plan(args: argparse.Namespace) -> int:
    root = _resolve_root(args)
    board = Board(root)
    config = load_config(root)
    raw = _read_text(args.file)
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise BoardError(f"invalid plan JSON: {exc}") from exc
    tasks = payload.get("tasks") if isinstance(payload, dict) else payload
    planned = roles.validate_planned_tasks(config, tasks)
    keys = {task["key"] for task in planned}
    for task in planned:
        validate_skills(root, task.get("skills", []))
        for ref in [*task["depends_on"], *task.get("conflicts_with", [])]:
            if ref not in keys:
                board.find(ref)
    if getattr(args, "check", False):
        group_by_route = bool(config.get("governance", {}).get("group_by_route", True))
        warnings = []
        for i, a in enumerate(planned):
            for b in planned[i + 1 :]:
                ga = a.get("group") or (a.get("route") if group_by_route else "")
                gb = b.get("group") or (b.get("route") if group_by_route else "")
                if set(a["files"]) & set(b["files"]) or (ga and ga == gb):
                    warnings.append(f"{a['key']} and {b['key']} may not run together ({ga or 'files'})")
        print(json.dumps({"valid": True, "tasks": [t["title"] for t in planned], "warnings": warnings}))
        return 0
    id_by_key: dict[str, str] = {}
    for task in planned:
        card = board.create(
            task["title"],
            body=task["spec"],
            spec=task["spec"],
            route=task["route"],
            priority=task["priority"],
            files_hint=task["files"],
            context_files=task.get("context_files", []),
            skills=task.get("skills", []),
            group=task.get("group", ""),
            max_diff_lines=task.get("max_diff_lines", 0),
            max_output_tokens=task.get("max_output_tokens", 0),
            acceptance_criteria=task["acceptance_criteria"],
        )
        id_by_key[task["key"]] = card.id
    for task in planned:
        deps = [id_by_key[dep] if dep in id_by_key else dep for dep in task["depends_on"]]
        conflicts = [id_by_key[c] if c in id_by_key else c for c in task.get("conflicts_with", [])]
        fields = {}
        if deps:
            fields["depends_on"] = deps
        if conflicts:
            fields["conflicts_with"] = conflicts
        if fields:
            board.update(id_by_key[task["key"]], **fields)
    print(json.dumps({"created": [id_by_key[task["key"]] for task in planned]}))
    return 0


def cmd_install_skill(args: argparse.Namespace) -> int:
    project = bool(args.project)
    root = (Path(args.root).resolve() if args.root else Path.cwd()) if project else None
    destinations = install_skill(root, project=project)
    scope = "project" if project else "user"
    for destination in destinations:
        print(f"installed ({scope}): {destination}")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    root = _resolve_root(args)
    load_dotenv(root / ".env")
    if args.detach:
        pid = runner.start_detached(root, concurrency=args.concurrency)
        print(f"runner started (pid {pid}); log: {runner.runner_state(root)['log']}")
        ui_cfg = load_config(root).get("ui", {})
        if ui_cfg.get("enabled", True) and not ui.is_running(root):
            url = ui.start_detached(root, ui_cfg.get("host", "127.0.0.1"), int(ui_cfg.get("port", 8765)))
            if url:
                print(f"ui: {url}")
        return 0
    config = load_config(root)
    if args.concurrency:
        config["governance"]["max_concurrency"] = args.concurrency
    orchestrator = Orchestrator(root, config)
    ticks = orchestrator.run(once=args.once)
    print(f"processed {ticks} tick(s)")
    return cmd_status(argparse.Namespace(root=str(root), json=False))


def cmd_stop(args: argparse.Namespace) -> int:
    root = _resolve_root(args)
    stopped = runner.stop(root)
    print("runner stopped" if stopped else "no active runner")
    if ui.stop(root):
        print("ui stopped")
    return 0


def _budget(root: Path) -> dict:
    config = load_config(root)
    usage = load_usage(root)
    return {
        "spent_usd": round(float(usage.get("estimated_usd", 0.0)), 6),
        "monthly_usd": config.get("cost_controls", {}).get("monthly_budget_usd"),
    }


def cmd_status(args: argparse.Namespace) -> int:
    root = _resolve_root(args)
    board = Board(root)
    cards = board.cards()
    if args.json:
        payload = {
            "cards": [card_as_dict(card) for card in cards],
            "runner": runner.runner_state(root),
            "ui": ui.ui_state(root),
            "budget": _budget(root),
        }
        print(json.dumps(payload, indent=2))
        return 0
    for column in COLUMNS:
        group = [card for card in cards if card.status == column]
        print(f"{column} ({len(group)})")
        for card in sorted(group, key=lambda item: (int(item.priority), item.id)):
            print(f"  {card.id}  {card.title}")
    state = runner.runner_state(root)
    label = f"running (pid {state['pid']})" if state["running"] else "stopped"
    budget = _budget(root)
    ui_state = ui.ui_state(root)
    ui_label = f"running ({ui_state['url']})" if ui_state["running"] else "stopped"
    print(f"\nrunner: {label}")
    print(f"ui: {ui_label}")
    print(f"budget: ${budget['spent_usd']:.4f} / ${budget['monthly_usd']}")
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


def cmd_logs(args: argparse.Namespace) -> int:
    text = read_log(_resolve_root(args), args.card_id)
    if not text:
        print(f"no log for {args.card_id}", file=sys.stderr)
        return 1
    print(text, end="")
    return 0


def cmd_retry(args: argparse.Namespace) -> int:
    root = _resolve_root(args)
    board = Board(root)
    card = board.find(args.card_id)
    if card.branch:
        vcs.delete_branch(root, card.branch)
    board.update(args.card_id, branch="", attempts=0, blocking_issues=[], last_review_summary="")
    card = board.move(args.card_id, "not_started", note="manually reopened for rework (fresh branch)")
    print(f"{card.id} -> {card.status}")
    return 0


def cmd_unblock(args: argparse.Namespace) -> int:
    board = Board(_resolve_root(args))
    card = board.find(args.card_id)
    board.update(args.card_id, attempts=0, review_cycles=0, blocking_issues=[], last_review_summary="")
    target = "review" if card.branch else "not_started"
    note = "manually reopened for re-review" if target == "review" else "manually reopened"
    card = board.move(args.card_id, target, note=note)
    print(f"{card.id} -> {card.status}")
    return 0


def cmd_re_review(args: argparse.Namespace) -> int:
    return cmd_unblock(args)


def cmd_schedule(args: argparse.Namespace) -> int:
    root = _resolve_root(args)
    waves = Orchestrator(root, load_config(root)).plan_waves()
    if args.json:
        print(json.dumps({"waves": waves}))
        return 0
    if not waves:
        print("nothing to schedule")
        return 0
    for index, wave in enumerate(waves, 1):
        print(f"wave {index}: {', '.join(wave)}")
    return 0


def cmd_skills(args: argparse.Namespace) -> int:
    root = _resolve_root(args)
    names = available_skills(root)
    if args.json:
        print(json.dumps(names))
        return 0
    if not names:
        print("no skills in .agents/skills/")
        return 0
    for name in names:
        print(name)
    return 0


def cmd_cancel(args: argparse.Namespace) -> int:
    root = _resolve_root(args)
    board = Board(root)
    card = board.find(args.card_id)
    if args.reason:
        log_event(root, card.id, f"cancelled: {args.reason}")
    if card.branch:
        vcs.worktree_remove(root, root / AGENTS_DIRNAME / "worktrees" / card.id)
        vcs.delete_branch(root, card.branch)
    board.delete(args.card_id)
    print(f"cancelled {card.id}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agents", description="Local filesystem agent board")
    parser.add_argument("--version", action="version", version=f"agent-board {__version__}")
    parser.add_argument("--root", help="project root containing .agents (defaults to searching upward)")
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init", help="scaffold .agents/ in the current directory")
    init.add_argument("--name", help="project name for the brief header")
    init.add_argument("--no-ui", action="store_true", help="do not start the local board UI")
    init.set_defaults(func=cmd_init)

    add = sub.add_parser("add", help="create one task card on the board")
    add.add_argument("text", help="task title")
    add.add_argument("--body-file", help="file with the task spec, or - for stdin")
    add.add_argument("--file", action="append", help="file the task may change (repeatable)")
    add.add_argument("--context-file", action="append", help="read-only context file (repeatable)")
    add.add_argument("--skill", action="append", help="project skill to apply (repeatable)")
    add.add_argument("--criterion", action="append", help="acceptance criterion (repeatable)")
    add.add_argument("--depends-on", action="append", help="existing card id dependency (repeatable)")
    add.add_argument("--conflicts-with", action="append", help="card id that must not run concurrently (repeatable)")
    add.add_argument("--group", default="", help="lane; same-group cards never run concurrently")
    add.add_argument("--route", default="", help="routing label (default backend)")
    add.add_argument("--priority", type=int, help="lower runs first (default 100)")
    add.add_argument("--max-diff-lines", type=int, help="per-card diff cap override")
    add.add_argument("--max-output-tokens", type=int, help="per-card output token cap override")
    add.set_defaults(func=cmd_add)

    plan = sub.add_parser("plan", help="bulk-import task cards from JSON")
    plan.add_argument("--file", help="JSON file, or - to read stdin (stdin is the default)")
    plan.add_argument("--stdin", action="store_true", help="read JSON from stdin")
    plan.add_argument("--check", action="store_true", help="validate and preview without creating cards")
    plan.set_defaults(func=cmd_plan)

    schedule = sub.add_parser("schedule", help="preview the parallel waves for the current board")
    schedule.add_argument("--json", action="store_true")
    schedule.set_defaults(func=cmd_schedule)

    run = sub.add_parser("run", help="run the orchestrator loop (watch mode by default)")
    run.add_argument("--once", action="store_true", help="run a single tick and exit")
    run.add_argument("--detach", action="store_true", help="start in the background (pidfile + log)")
    run.add_argument("--concurrency", type=int, help="override governance.max_concurrency")
    run.set_defaults(func=cmd_run)

    stop = sub.add_parser("stop", help="stop the runner and the UI")
    stop.set_defaults(func=cmd_stop)

    ui_parser = sub.add_parser("ui", help="serve the local kanban board")
    ui_parser.add_argument("--host")
    ui_parser.add_argument("--port", type=int)
    ui_parser.add_argument("--detach", action="store_true", help="run in the background")
    ui_parser.add_argument("--stop", action="store_true", help="stop the background UI")
    ui_parser.set_defaults(func=cmd_ui)

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

    logs = sub.add_parser("logs", help="show a card's log")
    logs.add_argument("card_id")
    logs.set_defaults(func=cmd_logs)

    retry = sub.add_parser("retry", help="reopen a card for another worker attempt")
    retry.add_argument("card_id")
    retry.set_defaults(func=cmd_retry)

    unblock = sub.add_parser("unblock", help="reopen a card (re-review if it has a branch, else rework)")
    unblock.add_argument("card_id")
    unblock.set_defaults(func=cmd_unblock)

    re_review = sub.add_parser("re-review", help="send an existing card back to the reviewer")
    re_review.add_argument("card_id")
    re_review.set_defaults(func=cmd_re_review)

    cancel = sub.add_parser("cancel", help="remove a superseded or duplicate card from the board")
    cancel.add_argument("card_id")
    cancel.add_argument("--reason", default="")
    cancel.set_defaults(func=cmd_cancel)

    skills = sub.add_parser("skills", help="list project skills in .agents/skills/")
    skills.add_argument("--json", action="store_true")
    skills.set_defaults(func=cmd_skills)

    skill = sub.add_parser("install-skill", help="install the Architect skill for Claude Code")
    skill.add_argument("--project", action="store_true", help="install into the project instead of the user dir")
    skill.set_defaults(func=cmd_install_skill)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except BoardError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
