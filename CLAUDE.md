# CLAUDE.md

## What this repository is

`agent-board` — a reusable Python CLI for running a local, filesystem-based multi-agent board. This repo is the **tool source**, not an application. Application work happens in *target* projects that install this CLI and contain their own `.agents/` directory.

## Architecture

Two layers, deliberately decoupled:

```
You (human)
  -> Architect (Claude Code, via /agents-init and /agents-add)   # interviews, plans, assigns cards
      -> .agents/board/not_started/
          -> agents run (DeepSeek PM / workers / reviewer)   # orchestrates + executes, calls the DeepSeek API
              -> .agents/board/{in_progress,review,closed,blocked}/
```

- **Claude = Architect.** It writes `project.md` and **authors task cards** onto the board turn-by-turn. It edits no code and must never call the DeepSeek API itself (the CLI/runner does that).
- **DeepSeek = PM, worker, reviewer.** The PM orchestrates scheduling/dependencies and merge/conflict resolution (it can also decompose raw `specs/` when `governance.decompose_specs` is on). All three run through the single `economy` provider (`AI_ECONOMY_API_KEY`).

## Hard rules

1. **No GitHub layer.** There are no issues, PRs, labels, workflows, or PATs. Do not reintroduce `gh`, `.github/`, or remote git operations.
2. **No shell for workers.** A worker returns `{summary, files:[{path, content}]}`; the trusted orchestrator writes files and commits. The model never executes commands.
3. **Per-project isolation.** All state lives under a project's `.agents/`. The package must never read another project's `.agents/` or keep cross-project context.
4. **One provider.** DeepSeek via `economy`. Do not add Anthropic/OpenAI/other providers.
5. **Protected paths.** `.agents/**`, `.git/**`, `.env*`, secrets, and key material are never writable by agents.
6. **Stdlib only at runtime.** No third-party dependencies.

## Layout

```
src/agents/
  frontmatter.py   # stdlib YAML-subset codec for card frontmatter
  board.py         # project discovery, Card, atomic moves, lock, deps, leases
  config.py        # default config + .agents/ scaffold
  validate.py      # path normalization + protected-path checks
  cli.py           # `agents` console entrypoint
tests/             # unittest (stdlib)
```

## Conventions

- Python 3.11+ typing (`from __future__ import annotations`).
- Board mutations go through `Board` under `board/.lock`; card moves are atomic.
- Keep the CLI and board logic free of model calls; model I/O lives in a dedicated module.

## Commands

```bash
python -m unittest discover -s tests      # run tests
agents init --name <project>              # scaffold .agents/ + .env in a target project
agents add "<title>" --file f.py --criterion "..."   # create one task card
agents plan --stdin                       # bulk-import cards from JSON
agents run                                # watch mode (default)
agents status
```

## Roadmap

P1 board + CLI · P2 PM/worker/reviewer loop · P3 merge-conflict PM + logs + verify · P4 Architect skills · P5 dogfood · P6 Architect-authored cards + `plan`/watch + `.env` on init — all done.
