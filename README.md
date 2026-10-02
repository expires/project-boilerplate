# agent-board

A local, filesystem-based multi-agent board. Claude is the **Architect**; DeepSeek is the **PM, workers, and reviewer**. No GitHub, no CI, no remote services, no execution of model-generated shell commands.

Everything lives in a `.agents/` directory inside your project:

```
.agents/
  project.md          # architect brief (purpose, stack, conventions, definition of done)
  config.json         # roles/models, governance, protected paths, optional verify commands
  board/
    not_started/      # PM writes cards; workers pull
    in_progress/      # claimed + leased
    review/           # implementation done, awaiting reviewer
    blocked/          # retries exhausted / escalations
    closed/           # approved + merged
  specs/              # feature specs the Architect drops for the PM to decompose
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
- A DeepSeek API key in `AI_ECONOMY_API_KEY` (see `.env.example`)

## Install

```bash
pip install -e .
```

## Quickstart

```bash
cd your-project
agents init --name my-app          # scaffold .agents/
agents add "Add hello.py and its pytest"
agents status
```

The Architect skill (Claude Code) wraps `init` and `add` with an interview so you never have to hand-write the brief. Until the skill ships, drive the CLI directly.

## CLI

| Command | Purpose |
|---|---|
| `agents init [--name N]` | scaffold `.agents/` in the current directory |
| `agents add "<feature>"` | drop a spec into `.agents/specs/` |
| `agents run [--once] [--concurrency N]` | run the PM → worker → reviewer loop |
| `agents status [--json]` | print the board |
| `agents card <id> [--json]` | show one card |
| `agents move <id> --to <column> [--note N]` | move a card between columns |

## Role contracts

- **Architect (Claude)** — interviews you, writes `project.md`, drops specs. Never edits code.
- **PM (DeepSeek)** — decomposes specs into cards; owns ordering and merge/conflict resolution.
- **Worker (DeepSeek)** — returns file contents for exactly one card; the trusted orchestrator writes files and commits to the task branch. No shell.
- **Reviewer (DeepSeek)** — reads `git diff main...agent/<id>`; approves (merge + close) or requests changes.

## State machine

`not_started → in_progress → review → closed`, with `review → in_progress` on changes requested and any state → `blocked` when retries are exhausted. Merges are serialized and linear (`rebase` + `--ff-only`).

## Isolation

The CLI locates the nearest `.agents/` from the current directory and reads only that. Usage, logs, and cards are per project; the package keeps no cross-project state. Only credentials come from the environment.

## Status

**P1:** package, board model, CLI (`init/add/status/card/move`), project discovery.

**P2 (current):** PM decomposition, worker via git worktree, reviewer, full close loop, budget accounting, `agents run`.

**Next:**
- **P3** — PM merge/conflict resolution, circuit breaker polish, optional `verify` commands.
- **P4** — Claude Code Architect skill, docs.
- **P5** — dogfood on a throwaway repo.

## Development

```bash
python -m unittest discover -s tests
```

Stdlib-only at runtime; no third-party dependencies.
