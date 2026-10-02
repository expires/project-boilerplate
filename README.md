# Multi-Agent Hackathon Boilerplate

A cost-optimized, hierarchical autonomous development pipeline for GitHub. A human **Stakeholder** files feature requests; a **Planner/Architect** scopes them into a Master Spec Issue; a **Project Manager (PM)** decomposes the spec into a task queue; **Worker sub-agents** execute single tasks in isolation and open PRs; a **Reviewer agent** gates every PR on deterministic CI first.

The human writes zero code and never joins CI/CD loops.

```
 Stakeholder (human)
        │  opens Issue labeled `master-spec`
        ▼
 Architect / Planner ──────────────► updates Master Spec Issue
        │
        ▼
 PM Agent ─────────────────────────► writes tasks.json (queue) + labels
        │
        ▼
 Queue Engine ─────────────────────► claim ≤ 2 tasks (concurrency cap)
        │
        ▼
 Worker Agents (depth 1, max 2 parallel)
        │  context-isolated diff on exact files
        ▼
 Pull Request (`agent:auto`)
        │
        ▼
 Deterministic CI (lint · test · compile)  ── fail ──► PR blocked
        │ pass
        ▼
 Reviewer Agent
        ├── approve ──────────────► merge, unblock dependents
        └── changes ──────────────► strike (1..3)
                                      │ 3 strikes
                                      ▼
                            Circuit Breaker: close PR,
                            open Escalation Issue for Architect
```

## Role Contracts

| Role | Agent | Trigger | Output | Never does |
|---|---|---|---|---|
| Stakeholder | human | — | Feature request issue | Code, CI, reviews |
| Architect / Planner | 1 | `master-spec` label | Master Spec Issue | Implementation |
| Project Manager | 2 | Master Spec Issue | `tasks.json` entries + labels | Code |
| Worker | 3+ | Queue engine | Branch + PR | Spawn agents, touch protected files, read whole repo |
| Reviewer | n | CI green on `agent:auto` PR | Approve / request changes | Write code |

## The Queue (`tasks.json`)

Every unit of work is one object. The queue is the single source of truth and is the only place agents coordinate state.

| Status | Meaning | Transitions to |
|---|---|---|
| `pending` | ready when `depends_on` all `merged` | `claimed` |
| `claimed` | leased by a worker, lease expires via `requeue-stale` | `in_progress`, `pending` |
| `in_progress` | LLM executing | `review`, `pending` (retry), `failed` |
| `review` | PR open, CI/reviewer running | `merged`, `changes_requested`, `escalated` |
| `changes_requested` | reviewer asked for fixes, strike recorded | `claimed` (re-run with feedback) |
| `merged` | done; unblocks dependents | terminal |
| `failed` | attempts exhausted | `escalated` |
| `escalated` | circuit breaker tripped | Architect re-plans |

## Guardrails

| Guardrail | Value | Enforced by |
|---|---|---|
| Max worker concurrency | 2 | `config/agents.json` + matrix `max-parallel` + claim-time count |
| Max open agent PRs | 2 | queue engine (`max_open_prs`) |
| Max agent depth | 1 (workers cannot spawn agents) | plan validation + worker prompt + no queue-append command |
| Review retries | 3 | `review_cycles` counter + strike labels |
| CI before review | required | `needs: ci` in workflow + `gh pr checks` verification in bridge |
| Context isolation | N target files only | `task.files` / `task.allowed_paths`, char/token cap |
| Protected paths | CI, config, bridge, queue, secrets | `is_protected()` in bridge; worker diff rejected |
| Monthly LLM budget | `cost_controls.monthly_budget_usd` | usage cache + hard halt |
| Blind collaboration | on | model identity terms scrubbed from all GitHub output |
| Loop prevention | bot actors ignored, idempotent planning, sticky terminal states | workflow `if:` + bridge state checks |

## Circuit Breaker

1. Reviewer requests changes → `strike:1`, `strike:2`, `strike:3` labels.
2. On strike 3 the bridge:
   - posts the full review history on the PR,
   - closes (and optionally deletes the branch of) the PR,
   - opens `[Escalation] <task>` with labels `escalation` + `agent:architect`,
   - marks the task `escalated` and labels the Master Spec `pipeline:halted`.
3. The Architect must re-scope the feature and file a new Master Spec Issue. No automatic re-entry.

Technical failures follow the same path: `max_task_attempts` exceeded → escalation, no PR loop.

## Quickstart

1. Push this repository to GitHub (Actions enabled).
2. Create provider secrets/variables:

   | Name | Type | Purpose |
   |---|---|---|
   | `AI_PREMIUM_API_KEY` | secret | Reviewer/Architect route |
   | `AI_ECONOMY_API_KEY` | secret | PM/Worker route |
   | `AI_PREMIUM_BASE_URL`, `AI_ECONOMY_BASE_URL` | variable | Any OpenAI-compatible gateway (optional, defaults in config) |
   | `AI_BRIDGE_PAT` | secret | Fine-grained PAT with `contents`, `issues`, `pull-requests`, `actions` write. **Required for autonomous loops** because `GITHUB_TOKEN`-created PRs do not trigger `pull_request` workflows |

3. Create labels: `master-spec`, `spec:planned`, `agent:auto`, `pipeline:halted`, `escalation`, `agent:architect`, `strike:1`, `strike:2`, `strike:3`, plus routing labels `agent:frontend`, `agent:backend`, `agent:infra`, `agent:test`, `agent:docs`. The bridge auto-creates labels that are missing.
4. Have the Stakeholder open an issue using `docs/MASTER_SPEC_TEMPLATE.md` and add the `master-spec` label.
5. PM plans, workers execute, CI runs, reviewer decides. Intervene only in escalation issues.

### Local dry run (no tokens, no mutations)

```bash
export AI_BRIDGE_OFFLINE=1
python3 scripts/ai_bridge.py status
python3 scripts/ai_bridge.py plan --issue 1 --dry-run
python3 scripts/ai_bridge.py claim --limit 2
python3 scripts/ai_bridge.py run --task-id T-001 --dry-run
python3 scripts/ai_bridge.py review --pr 42 --dry-run
python3 scripts/ai_bridge.py requeue-stale --minutes 45
```

## Configuration (`config/agents.json`)

- `governance` — caps: `max_concurrency`, `max_open_prs`, `max_depth`, `review_retries`, `max_task_attempts`, `auto_merge`, `lease_minutes`.
- `cost_controls` — token/char/diff ceilings and monthly USD budget with hard halt.
- `context` — `max_files_per_task`, `max_context_tokens`, `protected_paths`.
- `roles` + `providers` + `pricing` — every role routes to an OpenAI-compatible endpoint via env vars; cheap models for PM/Workers, premium for Review/Architect.
- `blind_collaboration.strip_identity_terms` — words scrubbed from every comment the bridge posts.
- `labels` and `escalation` — GitHub primitive names and architect assignment.

Model routing is human-owned. Agents themselves never see or publish model identities.

## Workflow (`.github/workflows/ai-orchestration.yml`)

| Trigger | Job | Behavior |
|---|---|---|
| Issue labeled `master-spec` | `pm-plan` | PM decomposes into queue, comments task table |
| `pull_request` | `ci` | Deterministic lint/test/compile; no LLM cost |
| PR CI success on `agent:auto` | `review` | Reviewer verdict, strikes, circuit breaker |
| schedule / dispatch `pump` | `claim` → `workers` | Claims ≤ 2 tasks, matrix `max-parallel: 2` |
| dispatch `status` / `requeue` / schedule | `maintenance` | Lease recovery, queue report |

All queue writes share the concurrency group `ai-queue-<repo>` so claims can never race.

## Cost Model

- **Deterministic CI first**: reviewers are only paid after lint/tests pass.
- **Queue batching**: at most 2 workers and 2 open PRs; no speculative parallelism.
- **Context isolation**: workers receive only `task.files` contents under `max_context_tokens`.
- **Budget halt**: `.ai-bridge/usage.json` is persisted via `actions/cache`; at 100% of the monthly budget the bridge refuses further LLM calls. Cache eviction resets the counter — for production, mirror it to an artifact or external store.
- **Escalation instead of retry storms**: 3 review strikes is the hard stop.

## File Map

```
.github/workflows/ai-orchestration.yml   pipeline: PM, CI, queue pump, review
config/agents.json                       routing, caps, budget, protected paths
tasks.json                               PM queue (single source of truth)
docs/MASTER_SPEC_TEMPLATE.md             Architect template
scripts/ai_bridge.py                     orchestration engine (stdlib only)
.ai-bridge/                              local usage/state (gitignored)
```

## Known Constraints

- Autonomous chaining requires `AI_BRIDGE_PAT`; without it, PRs opened by `github-actions[bot]` will not trigger CI/review workflows. Manual `workflow_dispatch` still works.
- Budget accounting via `actions/cache` is best-effort (GitHub evicts caches); mirror usage for strict accounting.
- The bridge intentionally refuses to modify its own guardrails (`scripts/ai_bridge.py`, `config/agents.json`, `.github/workflows/**`, `tasks.json`). Changing them is a human/stakeholder action.
