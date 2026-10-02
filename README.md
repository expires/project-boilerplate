# agent-board

A local, filesystem-based multi-agent board that you drive entirely from **Claude Code**. Claude is the Architect; a local runner executes the board with DeepSeek workers and a reviewer. No GitHub, no CI, no remote services, no execution of model-generated shell commands.

```
You  ⟷  Claude Code (Architect)              turns the work into cards
              └── .agents/board/not_started/
                        │
                        │  agents run   (local runner)
                        ▼
        DeepSeek PM → worker → reviewer
              in_progress → review → closed
```

- **Claude Code** interviews you and lays the work out as task cards. It never writes application code.
- **The runner** (`agents run`) takes each card, has a DeepSeek worker implement it on a git branch in an isolated worktree, has a DeepSeek reviewer approve it, then merges into your base branch.

## One-time setup

```bash
uv tool install --editable /path/to/project-boilerplate   # installs the `agents` command
agents install-skill                                      # installs /agents-init and /agents-add
```

## Use it — Claude Code only

1. **Open Claude Code** in your project directory.

2. **`/agents-init`** — Claude interviews you (project name/purpose, stack, conventions, definition of done, base branch), then:
   - runs `agents init --name "…"`,
   - writes `.agents/project.md` and updates `.agents/config.json`,
   - tells you to start the runner.

3. **Put your DeepSeek key in `.env`** (created by `init`, along with a `.gitignore`):
   ```
   AI_ECONOMY_API_KEY=sk-...
   ```

4. **Start the runner once** and leave it running in a terminal:
   ```bash
   agents run
   ```

5. **`/agents-add "<feature>"`** — describe a feature; Claude turns it into one or more task
   cards (files, acceptance criteria, dependencies, priority). Repeat as you go, turn by turn.

6. **Ask Claude anything about the board** — "what's on the board?", "what's blocked?", "why
   did T-004 fail?" Claude runs `agents status` / `agents card` / `agents logs` and reports.

That's the whole loop: you talk to Claude Code, the runner does the work.

## What Claude will and won't do

| Will | Won't |
|---|---|
| Interview you and write the project brief | Write or edit application code |
| Create and update task cards | Call the DeepSeek API |
| Read board state and explain failures | Hand-edit files under `.agents/board/` |
| Reset/reopen blocked cards for you | Push to any remote or touch GitHub |

## The runner

| Command | Purpose |
|---|---|
| `agents run` | Watch mode (default): loops PM → worker → reviewer. `Ctrl-C` to stop. |
| `agents run --once` | A single pass, then exit. |

The runner reads `AI_ECONOMY_API_KEY` from `.env`. It is the only thing that calls DeepSeek.

## The board

```
.agents/
  project.md          # the brief Claude wrote
  config.json         # base branch, protected paths, optional verify
  board/
    not_started/      # cards Claude assigned; the runner picks these up
    in_progress/      # claimed + leased
    review/           # implemented, awaiting the reviewer
    blocked/          # retries exhausted — ask Claude to look
    closed/           # approved + merged
  worktrees/<id>/     # one git worktree per in-flight task
  logs/<id>.log       # per-card history
  usage.json          # per-project token/cost ledger
```

A card is a Markdown file with YAML frontmatter (`id`, `title`, `status`, `priority`,
`depends_on`, `route`, `acceptance_criteria`, `files_hint`, `branch`, `history`, …). You
never write these by hand — Claude does, through the CLI.

## Configuration

`.agents/config.json` — Claude sets the important parts during `/agents-init`:

- `vcs.base_branch` — where approved work merges (default `main`).
- `context.protected_paths` — files agents may never write (defaults cover `.agents/**`, `.git/**`, `.env*`, secrets).
- `verify` — optional deterministic commands the runner executes before review (off by default).
- `governance.decompose_specs` — off by default: Claude authors cards directly. Turn on to let the DeepSeek PM decompose raw specs in `.agents/specs/`.

## Requirements

- Python 3.11+
- A DeepSeek API key (`agents init` creates `.env` for it)

## Troubleshooting

- **Cards never move** — is `agents run` actually running? Is `AI_ECONOMY_API_KEY` set in `.env`?
- **A card is stuck in `blocked/`** — ask Claude: *"show me `agents logs T-004` and propose a fix, then unblock it."*
- **`/agents-add` didn't trigger** — invoke it directly as `/agents-add <feature>`, or ask Claude to "add a task for …".
- **Skills missing** — re-run `agents install-skill`.

## Development

```bash
python -m unittest discover -s tests
```

Stdlib-only at runtime; no third-party dependencies.
