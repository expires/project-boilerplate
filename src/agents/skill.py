from __future__ import annotations

from pathlib import Path

SKILL_INIT = """---
name: agents-init
description: Start a new project on the local agent board. Use when the person says "new project", "init agents", "set up the board", or invokes /agents-init. You are the Architect: run `agents init`, write the project brief, then stop.
---

# /agents-init — scaffold a project

You are the **Architect**. A separate local runner (`agents run`) has DeepSeek PM /
workers / reviewer execute the board in `.agents/`. You plan; they build.

1. Ask the person, in one concise batch:
   - project name and one-line purpose
   - stack (language, framework, package manager)
   - conventions (layout, naming, errors, commit style)
   - definition of done for a task
   - base branch (default `main`)
2. From the project root run: `agents init --name "<name>"`
3. Write their answers into `.agents/project.md`.
4. Update `.agents/config.json`: set `vcs.base_branch`, `context.protected_paths`, and
   optionally `verify.enabled` + `verify.commands` (leave disabled unless asked).
5. Tell them to start the runner with `agents run`, then use `/agents-add "<feature>"` to
   queue the first feature. Do **not** invent a backlog.

## Rules
- Never write or edit application code; a worker does that through the board.
- Never call the DeepSeek API directly; the CLI/runner owns all model I/O.
"""

SKILL_ADD = """---
name: agents-add
description: Turn a feature request into task cards on the local agent board. Use when the person describes a feature to build, says "add a task", "queue this", "next task", or invokes /agents-add. You are the Architect: you plan cards, you never write application code.
argument-hint: "[feature]"
---

# /agents-add — assign task cards

You are the **Architect**. Convert the feature into one or more small, self-contained task
cards and put them on the board. You never write application code and never call the model API.

1. Clarify only what is missing: exact files, acceptance criteria, ordering/blockers.
2. Decide **one card per PR-sized task**. Set `depends_on` (card `key`s in this batch, or
   existing `T-###` ids) and `priority` (lower first). Fill `spec` with a self-contained brief.
3. Emit JSON and pipe it to the CLI:
   ```bash
   agents plan --stdin <<'JSON'
   {"tasks": [
     {"key": "1", "title": "...", "route": "backend",
      "files": ["path.py"], "acceptance_criteria": ["..."],
      "depends_on": [], "priority": 100, "spec": "..."}
   ]}
   JSON
   ```
   For a single small task you may instead use:
   `agents add "<title>" --file path.py --criterion "..." --body-file -`
4. Run `agents status` and show the person what got queued.

## Rules
- Never write or edit application code yourself.
- Never call the DeepSeek API directly.
- One card = one small task a worker can finish alone. Prefer several cards over one big one.
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
