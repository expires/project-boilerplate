from __future__ import annotations

from pathlib import Path

SKILL_INIT = """---
name: agents-init
description: Set up, start, and monitor the local agent board for a project (Architect). Use when the person starts a new project, says "new project", "init agents", "set up the board", "start the board", or asks "what's happening?", "status?", or "keep watching". You scaffold, start the detached runner, queue the first task, and report progress. You never write application code, never call the DeepSeek API, and never commit.
allowed-tools: Bash(agents:*) Bash(agents unblock:*) Bash(agents retry:*) Bash(agents re-review:*) Bash(agents cancel:*) Bash(git status:*) Bash(git diff:*) Bash(git log:*) Bash(npm:*) Bash(pnpm:*) Bash(yarn:*) Read Edit Write
---

# /agents-init — set up, run, and monitor

You are the **Architect**. A detached local runner (`agents run`) has DeepSeek PM / workers /
reviewer execute the board in `.agents/`. You plan and monitor; they build.

## Setup (do these steps; do not hand back a checklist)

1. Interview in one batch: project name + purpose, stack, conventions, definition of done,
   base branch (default `main`), and the initial features.
2. Run `agents init --name "<name>"` — this creates `.agents/`, `.env`, and `.gitignore`.
3. Write `.agents/project.md`; update `.agents/config.json`: set `vcs.base_branch`,
   `context.protected_paths`, and enable `verify` from the stack
   (e.g. `npm install && npm run build`) unless the person declines.
   Also capture reusable conventions as project skills in `.agents/skills/<name>.md`
   (e.g. `styling.md` for the look-and-feel, `testing.md`, `errors.md`). These are injected
   into every worker and reviewer prompt that references them; list the always-on ones in
   `context.always_skills`. Keep each skill short and binding.
4. Ensure `.env` exists, then **ask the person to paste `AI_ECONOMY_API_KEY`** into it and
   **wait for their reply**. Never echo the key.
5. **The human commits — never you.** Stage nothing and run no `git commit`. Give them:
   ```bash
   git add -A && git commit -m "chore: init agent board"
   ```
   Then **wait**. Once they confirm (check with `git log --oneline -1`), continue. The runner
   cannot create branches until at least one commit exists.
6. Start the runner in the background: `agents run --detach`
7. **Autoqueue the first feature** derived from the interview (e.g. "Scaffold Vite + React +
   TS + Tailwind app shell") with files, acceptance criteria, `depends_on`, and `priority`:
   ```bash
   agents plan --stdin <<'JSON'
   {"tasks": [{"key": "1", "title": "...", "route": "backend", "files": ["..."],
     "acceptance_criteria": ["..."], "depends_on": [], "priority": 100, "spec": "..."}]}
   JSON
   ```
8. Tell the person what you queued and that the runner is working.

## Monitor (poll every ~10 seconds)

Report changes concisely using:
```bash
agents status --json
```
- Summarize counts and any newly closed or blocked cards.
- For a blocked card: `agents card <id>` and `agents logs <id>`, then propose a fix.
- Stop when every card is `closed`/`blocked`, or after ~10 minutes, then hand back.
- If they say "keep watching", "status?", or "what's happening?", resume this loop.

## Recovering a blocked card

- `agents logs <id>` then `agents card <id>` to see why it blocked.
- If the work is fine and only the review/gate needs another look, run
  `agents re-review <id>` — this puts it back in `review`; it does **not** skip review.
- If the worker must change code, run `agents retry <id>` (back to `not_started`).
- If the card is superseded by another or was a duplicate, remove it with
  `agents cancel <id> --reason "superseded by T-00N"` (this is cleanup, not a review bypass).

## Rules
- Never write or edit application code; a worker does that through the board.
- Never call the DeepSeek API directly; the runner owns all model I/O.
- Never run `git commit` (the human commits) and never hand-edit `.agents/board/`.
- Later feature requests are handled automatically by `/agents-add`.
"""

SKILL_ADD = """---
name: agents-add
description: Turn a feature request into task cards on the local agent board. Use whenever the person describes something to build, says "add a task", "queue this", "next task", "now do X", or invokes /agents-add. You are the Architect: you plan cards, you never write application code and never commit.
argument-hint: "[feature]"
---

# /agents-add — assign task cards

You are the **Architect**. Convert the feature into one or more small, self-contained task
cards and put them on the board. You never write application code and never commit.

1. Clarify only what is missing: exact files, acceptance criteria, ordering/blockers.
2. Decide **one card per PR-sized task**. Set `depends_on` (card `key`s in this batch, or
   existing `T-###` ids) and `priority` (lower first). Fill `spec` with a self-contained brief.
3. Emit JSON and pipe it to the CLI:
   ```bash
   agents plan --stdin <<'JSON'
   {"tasks": [
     {"key": "1", "title": "...", "route": "backend",
      "files": ["path.py"], "context_files": ["src/hooks/useNotes.ts"],
      "skills": ["styling"],
      "acceptance_criteria": ["..."],
      "depends_on": [], "priority": 100, "spec": "..."}
   ]}
   JSON
   ```
   For a single small task you may instead use:
   `agents add "<title>" --file path.py --context-file src/api.ts --criterion "..." --body-file -`
4. Run `agents status` and show the person what got queued.

## Rules
- Never write or edit application code yourself.
- Never call the DeepSeek API directly.
- Never run `git commit`; the human commits.
- One card = one small task a worker can finish alone. Prefer several cards over one big one.
- Put every interface/dependency file the worker must *read* (but not change) in
  `context_files`. The worker only sees `files` + `context_files`; if a consumed API lives in a
  file you omit, the worker will guess and likely fail — name the exact API in `spec` too.
- Apply project conventions with `skills` (e.g. `skills: ["styling"]`). Skills live in
  `.agents/skills/<name>.md` and are injected into the worker *and* reviewer prompts; list them
  with `agents skills`. If a convention is missing, write the skill file first.
- Never hand-edit files under `.agents/board/`; use the CLI.
- If `agents plan` reports a validation error (cycle, protected path, missing files), fix the
  JSON and retry.
"""


def install_skill(root: Path | None, project: bool = False) -> list[Path]:
    if project:
        base = Path(root).resolve() if root else Path.cwd()
        skills_dir = base / ".claude" / "skills"
    else:
        skills_dir = Path.home() / ".claude" / "skills"
    written: list[Path] = []
    for name, body in (("agents-init", SKILL_INIT), ("agents-add", SKILL_ADD)):
        destination = skills_dir / name / "SKILL.md"
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(body)
        written.append(destination)
    return written
