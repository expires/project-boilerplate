---
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
