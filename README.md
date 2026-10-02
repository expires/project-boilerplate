# agent-board

A local, filesystem-based multi-agent board. Claude is the **Architect**; DeepSeek is the **PM, workers, and reviewer**. No GitHub, no CI, no remote services, no execution of model-generated shell commands.

Everything lives in a `.agents/` directory inside your project:

```
.agents/
  project.md          # architect brief (purpose, stack, conventions, definition of done)
  config.json         # roles/models, governance, protected paths, optional verify commands
  board/
    not_started/      # the Architect assigns cards here; workers pull
    in_progress/      # claimed + leased
    review/           # implementation done, awaiting reviewer
    blocked/          # retries exhausted / escalations
    closed/           # approved + merged
  specs/              # optional: raw specs for the PM to decompose (off by default)
  worktrees/<id>/     # isolated git worktree per in-flight task
  logs/               # per-card logs
  usage.json          # per-project token/cost ledger
```

Cards are Markdown files with YAML frontmatter:

```markdown
---
id: T-001
title: Add hello.py and its pytest
status: not_started
priority: 1
depends_on: []
route: backend
acceptance_criteria: ['hello.py prints "hello"', 'tests/test_hello.py passes']
files_hint: [hello.py, tests/test_hello.py]
depth: 1
attempts: 0
review_cycles: 0
branch: agent/t-001
base: main
---

Implement `hello.py` with a `greet()` function and a pytest test.
```

## Requirements

- Python 3.11+
- A DeepSeek API key in `AI_ECONOMY_API_KEY` (`agents init` creates `.env` for it)

## Install

```bash
uv tool install --editable .     # recommended: puts `agents` on your PATH
# or: pip install -e .
```

## Quickstart

```bash
cd your-project
agents init --name my-app          # scaffold .agents/ + .env, and prints next steps
# put your key in .env
agents add "Add hello.py and its pytest" --file hello.py --criterion "greet() returns 'hello'"
agents status
```

Run the board (watch mode is the default; Ctrl-C to stop):

```bash
agents run             # loop: PM -> workers -> reviewer
agents run --once      # a single tick, then exit
```

## CLI

| Command | Purpose |
|---|---|
| `agents init [--name N]` | scaffold `.agents/` + `.env` in the current directory |
| `agents add "<title>" [--file F…] [--criterion C…] [--depends-on ID…] [--route R] [--priority N] [--body-file -]` | create one task card |
| `agents plan [--file F \| --stdin]` | bulk-import task cards from JSON |
| `agents run [--once] [--concurrency N]` | run the loop in watch mode (default) or a single tick |
| `agents status [--json]` | print the board |
| `agents card <id> [--json]` | show one card |
| `agents move <id> --to <column> [--note N]` | move a card between columns |
| `agents logs <id>` | show a card's event log |
| `agents retry <id>` | reopen a card for another worker attempt |
| `agents unblock <id>` | reopen a blocked card and reset review cycles |
| `agents install-skill [--project]` | install the Architect skill for Claude Code |

## Architect skill (Claude Code)

```bash
agents install-skill            # -> ~/.claude/skills/agents-init and /agents-add
```

Then, in Claude Code inside a project:
- **`/agents-init`** interviews you, scaffolds the board + brief, and points you at the runner.
- **`/agents-add "<feature>"`** turns a feature into task cards on the board (turn-by-turn).

The Architect never writes application code and never calls the model API — it plans, the
local runner builds. Describing a feature in plain language also triggers `/agents-add`.

## Optional verification

The reviewer is the only quality gate by default. If you want a deterministic check before
review, enable it in `.agents/config.json` (commands run by the trusted orchestrator, never
the model):

```json
"verify": { "enabled": true, "commands": ["python -m pytest -q", "ruff check ."] }
```

## Role contracts

- **Architect (Claude)** — interviews you, writes `project.md`, and **authors task cards** (turn-by-turn). Never edits code, never calls the model API.
- **PM (DeepSeek)** — orchestrates the board: scheduling, dependencies, and merge/conflict resolution. Can also decompose raw `specs/` when `governance.decompose_specs` is enabled.
- **Worker (DeepSeek)** — returns file contents for exactly one card; the trusted orchestrator writes files and commits to the task branch. No shell.
- **Reviewer (DeepSeek)** — reads `git diff main...agent/<id>`; approves (merge + close) or requests changes.

## State machine

`not_started → in_progress → review → closed`, with `review → in_progress` on changes requested and any state → `blocked` when retries are exhausted. Merges are serialized and linear (`rebase` + `--ff-only`).

## Isolation

The CLI locates the nearest `.agents/` from the current directory and reads only that. Usage, logs, and cards are per project; the package keeps no cross-project state. Only credentials come from the environment.

## Status

**P1:** package, board model, CLI (`init/add/status/card/move`), project discovery.

**P2:** PM decomposition, worker via git worktree, reviewer, full close loop, budget accounting, `agents run`.

**P3:** PM-assisted merge-conflict resolution, per-card logs, optional `verify` commands, `retry`/`unblock`.

**P4:** Claude Code Architect skills (`/agents-init`, `/agents-add`).

**P5:** dogfood on a throwaway repo (real DeepSeek run).

**P6 (current):** Architect authors cards directly (`agents add`, `agents plan`), PM orchestrates, `agents run` watches by default, `agents init` seeds `.env`.

## Development

```bash
python -m unittest discover -s tests
```

Stdlib-only at runtime; no third-party dependencies.
