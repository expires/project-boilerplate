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

You talk to Claude Code; it does the setup, starts the runner, queues the work, and monitors.

1. **Open Claude Code** in your project directory and run **`/agents-init`**.

2. **Claude interviews you** (name/purpose, stack, conventions, definition of done, base
   branch, initial features), then runs `agents init` (creating `.agents/`, `.env`, `.gitignore`),
   writes `.agents/project.md`, and enables `verify` for your stack in `.agents/config.json`.

3. **Claude asks you to paste `AI_ECONOMY_API_KEY`** into `.env` and waits. It never prints the key.

4. **Claude commits the scaffold** (it may run local git, but never pushes):
   ```bash
   git add -A && git commit -m "chore: init agent board"
   ```
   You push to the remote whenever you're ready.

5. **Claude starts the runner detached** (`agents run --detach`) and **auto-queues the first
   feature** it derived from the interview.

6. **Then just keep talking.** Every feature you describe ("now add notes CRUD", "next, the
   editor") is turned into task cards automatically via `/agents-add`. Ask "what's happening?"
   any time and Claude reports the board.

## What Claude will and won't do

| Will | Won't |
|---|---|
| Interview you, write the brief, scaffold the board | Write or edit application code |
| Create/update task cards, start the runner, monitor | Call the DeepSeek API |
| Read board state and explain failures | Hand-edit `.agents/board/` |
| Commit and merge locally (`git add/commit/merge/rebase/branch/checkout/stash`) | **`git push` — the human pushes** |

## The runner

Claude starts it for you; these are the underlying commands.

| Command | Purpose |
|---|---|
| `agents run --detach` | Start in the background (pidfile + `.agents/runner.log`). |
| `agents run` | Run in watch mode in the foreground. `Ctrl-C` to stop. |
| `agents run --once` | A single pass, then exit. |
| `agents stop` | Stop the detached runner. |
| `agents status` | Board columns, runner state, and budget spend. |

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
`depends_on`, `route`, `acceptance_criteria`, `files_hint`, `context_files`, `skills`,
`branch`, `history`, …). You never write these by hand — Claude does, through the CLI.

## Project skills (conventions for the workers)

Reusable conventions live in `.agents/skills/<name>.md` (e.g. `styling.md`, `testing.md`).
They are injected into the **worker and reviewer** prompts so implementations stay consistent:

- Reference one per card: `skills: ["styling"]` (CLI: `agents add … --skill styling`).
- Or apply everywhere via `context.always_skills: ["styling"]` in `.agents/config.json`.
- List what exists with `agents skills`.
- Keep each skill short and binding (a page of hard rules beats an essay).

Example — `.agents/skills/styling.md`:
```markdown
# Styling
- Use Blueprint components and the `Classes.DARK` theme; no bespoke widgets.
- Dense spacing: 1px `#2F343C` dividers, 4px radius, no large shadows.
- All custom CSS in `src/styles.css`; no inline styles.
```

Writing the skill file first is part of planning: if a convention matters, capture it as a
skill so every future worker (and the reviewer checking them) follows it.

## Local vs push

Everything is local. The runner commits each card to its own branch and merges approved work
into your base branch; Claude and subagents may also commit/merge locally. **Only `git push`
is yours** — nothing in this tool ever touches the network. (Recommend denying `Bash(git push:*)`
in your Claude Code settings.)

Merges are conflict-free by construction: **`governance.max_concurrency: 1`** runs one card at
a time and merges it before the next is claimed, so every branch is cut from current `main` and
integration is a fast-forward. Raise the concurrency and cards whose files overlap are serialized.

## Ticket size & context

A single card is bounded by the worker's **output** tokens (the worker emits whole file contents
in one reply), not by context. The caps are generous by default and per-card tunable:

- `cost_controls.max_input_chars: 120000`, `context.max_files_per_task: 16`, `cost_controls.max_diff_lines: 1500`.
- Per card: `agents add "<title>" --max-diff-lines N --max-output-tokens N` (or the same fields in `agents plan` JSON).
- If a card is genuinely too big, the runner blocks it immediately with a clear message — split it.

## Parallel work

You (via the Architect) decide what may run together; the runner enforces it safely.

- Cards default to a **lane per `route`** (`governance.group_by_route`): same-route cards
  serialize, different routes can run concurrently.
- Override a card's lane with `group` (`agents add … --group ui`), and mark cards that share
  files that are not in `files_hint` with `conflicts_with`.
- Any card touching a `context.shared_paths` entry (lockfiles, `package.json`, …) runs **exclusively**.
- `governance.max_concurrency` is the **upper bound** on simultaneous cards.
- Preview the schedule: **`agents schedule`** (or `agents plan --check` before creating cards).
  Merges stay serialized and fast-forward.

## Configuration

`.agents/config.json` — Claude sets the important parts during `/agents-init`:

- `vcs.base_branch` — where approved work merges (default `main`); `vcs.conflict_strategy: "serialize"`.
- `governance.max_concurrency` — 1 by default (serialized, conflict-free).
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
- **Architect skill missing** (`/agents-init`, `/agents-add`) — re-run `agents install-skill`.
- **Workers ignore a convention** — add it as a project skill in `.agents/skills/` and reference it (`skills: ["<name>"]`) or set `context.always_skills`.

## Development

```bash
python -m unittest discover -s tests
```

Stdlib-only at runtime; no third-party dependencies.
