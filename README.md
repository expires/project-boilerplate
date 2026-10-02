# Multi-Agent Hackathon Boilerplate

A cost-optimized, hierarchical autonomous development pipeline for GitHub, built for a **single-terminal local workflow**. The human works with local Claude Code as the Architect/Product Owner; every automated background task runs in GitHub Actions on **DeepSeek only**, billed through one key (`AI_ECONOMY_API_KEY`).

```
 Human (Stakeholder) + Local Claude Code (Architect / Product Owner)
        │  fills docs/MASTER_SPEC_TEMPLATE.md
        │  gh issue create --label type:master-spec
        ▼
 GitHub Issue (Master Spec)
        │  Actions job `pm-plan` (DeepSeek)
        ▼
 tasks.json queue ──────────────────────► claim ≤ 2 tasks (concurrency cap)
        │
        ▼
 Worker Agents (DeepSeek, depth 1, max 2 parallel)
        │  context-isolated diff on exact files
        ▼
 Pull Request (`agent:auto`)
        │
        ▼
 Deterministic CI (lint · test · compile)  ── fail ──► PR blocked
        │ pass
        ▼
 Reviewer Agent (DeepSeek)
        ├── approve ──────────────► merge, unblock dependents
        └── changes ──────────────► strike (1..3)
                                      │ 3 strikes
                                      ▼
                            Circuit Breaker: close PR, open Escalation Issue
                                      │
                                      ▼
                     Back to local Claude Code to re-scope
```

The human writes zero code and never joins CI/CD loops.

## Role Contracts

| Role | Runtime | Trigger | Output | Never does |
|---|---|---|---|---|
| Stakeholder | human | — | Feature request | Code, CI, reviews |
| Architect / Product Owner | **local Claude Code** | human request | Master Spec Issue (`type:master-spec`) | Implementation code |
| Project Manager (DeepSeek) | GitHub Actions | Master Spec Issue | `tasks.json` entries + labels | Code |
| Worker (DeepSeek) | queue engine | claimed task | Branch + PR | Spawn agents, touch protected files, read whole repo |
| Reviewer (DeepSeek) | CI green on `agent:auto` PR | — | Approve / request changes | Write code |

## The Queue (`tasks.json`)

Every unit of work is one object. The queue is the single source of truth and the only place agents coordinate state.

| Status | Meaning | Transitions to |
|---|---|---|
| `pending` | ready when `depends_on` all `merged` | `claimed` |
| `claimed` | leased by a worker, lease expires via `requeue-stale` | `in_progress`, `pending` |
| `in_progress` | DeepSeek worker executing | `review`, `pending` (retry), `failed` |
| `review` | PR open, CI/reviewer running | `merged`, `changes_requested`, `escalated` |
| `changes_requested` | reviewer asked for fixes, strike recorded | `claimed` (re-run with feedback) |
| `merged` | done; unblocks dependents | terminal |
| `failed` | attempts exhausted | `escalated` |
| `escalated` | circuit breaker tripped | local Architect re-plans |

## Guardrails

| Guardrail | Value | Enforced by |
|---|---|---|
| Max worker concurrency | 2 | `config/agents.json` + matrix `max-parallel` + claim-time count |
| Max open agent PRs | 2 | queue engine (`max_open_prs`) |
| Max agent depth | 1 (workers cannot spawn agents) | plan validation + worker prompt + no queue-append command |
| Review retries | 3 | `review_cycles` counter + strike labels |
| CI before review | required | `needs: ci` in workflow + `gh pr checks` verification in bridge |
| Context isolation | target files only | `task.files` / `task.allowed_paths`, char/token cap |
| Protected paths | CI, config, bridge, queue, CLAUDE.md, secrets | `is_protected()` in bridge; worker diff rejected |
| Monthly LLM budget | `cost_controls.monthly_budget_usd` | usage cache + hard halt |
| Blind collaboration | on | model identity terms scrubbed from all GitHub output |
| Loop prevention | bot actors ignored, idempotent planning, sticky terminal states | workflow `if:` + bridge state checks |

## Model Routing

All cloud roles run on DeepSeek through the single **economy** provider:

| Role | Default model | Env override |
|---|---|---|
| PM | `deepseek-chat` | `PM_MODEL` |
| Worker | `deepseek-coder` | `WORKER_MODEL` |
| Reviewer | `deepseek-chat` | `REVIEWER_MODEL` |

- Endpoint: `https://api.deepseek.com/v1` (override with `AI_ECONOMY_BASE_URL`).
- Key: `AI_ECONOMY_API_KEY` only. No Anthropic/OpenAI key is used anywhere in the cloud.
- If a role is ever pointed at another provider whose key is missing, the router falls back to the economy provider and `fallback_model_default`.
- Architect planning never calls an API: local Claude Code does it under the human's own subscription.

## Circuit Breaker

1. Reviewer requests changes → `strike:1`, `strike:2`, `strike:3` labels.
2. On strike 3 the bridge:
   - posts the full review history on the PR,
   - closes (and optionally deletes the branch of) the PR,
   - opens `[Escalation] <task>` with labels `escalation` + `agent:architect`,
   - marks the task `escalated` and labels the Master Spec `pipeline:halted`.
3. Local Claude Code re-scopes the feature and creates a new Master Spec Issue. No automatic re-entry.

Technical failures follow the same path: `max_task_attempts` exceeded → escalation, no PR loop.

## Quickstart

1. Push this repository to GitHub (Actions enabled).
2. Add exactly two repository secrets (`Settings -> Secrets and variables -> Actions`):

   | Name | Purpose |
   |---|---|
   | `AI_ECONOMY_API_KEY` | DeepSeek key used by PM, Worker, and Reviewer |
   | `AI_BRIDGE_PAT` | Fine-grained PAT with `contents`, `issues`, `pull-requests`, `actions` write. **Required for autonomous loops** because `GITHUB_TOKEN`-created PRs do not trigger `pull_request` workflows |

   Optional repository variables: `AI_ECONOMY_BASE_URL`, `PM_MODEL`, `WORKER_MODEL`, `REVIEWER_MODEL`.
3. Create labels: `type:master-spec`, `spec:planned`, `agent:auto`, `pipeline:halted`, `escalation`, `agent:architect`, `strike:1`, `strike:2`, `strike:3`, plus routing labels `agent:frontend`, `agent:backend`, `agent:infra`, `agent:test`, `agent:docs`. The bridge auto-creates labels that are missing.
4. In the local terminal, ask Claude Code to build a feature. It fills `docs/MASTER_SPEC_TEMPLATE.md` and creates the issue:

   ```bash
   gh issue create --title "Master Spec: <feature>" --body-file /tmp/master-spec.md --label type:master-spec
   ```

5. The PM plans, DeepSeek workers execute, CI runs, the DeepSeek reviewer decides. Intervene only in escalation issues.

### Local dry run (no tokens, no mutations)

```bash
cp .env.example .env        # optional for live calls; --dry-run needs no credentials
python3 scripts/ai_bridge.py status
python3 scripts/ai_bridge.py plan --issue 1 --dry-run
python3 scripts/ai_bridge.py claim --limit 2
python3 scripts/ai_bridge.py run --task-id T-001 --dry-run
python3 scripts/ai_bridge.py review --pr 42 --dry-run
python3 scripts/ai_bridge.py requeue-stale --minutes 45
```

## Configuration (`config/agents.json`)

- `governance` — caps: `max_concurrency`, `max_open_prs`, `max_depth`, `review_retries`, `max_task_attempts`, `auto_merge`, `lease_minutes`.
- `cost_controls` — token/char/diff ceilings, monthly USD budget with hard halt, and `restrict_worker_to_economy`.
- `context` — `max_files_per_task`, `max_context_tokens`, `protected_paths`.
- `roles` + `providers` + `pricing` — every cloud role routes to the DeepSeek endpoint via `AI_ECONOMY_API_KEY`.
- `blind_collaboration.strip_identity_terms` — words scrubbed from every comment the bridge posts.
- `labels` and `escalation` — GitHub primitive names and Architect assignment.

Model routing is human-owned. Agents themselves never see or publish model identities.

## Workflow (`.github/workflows/ai-orchestration.yml`)

| Trigger | Job | Behavior |
|---|---|---|
| Issue labeled `type:master-spec` | `pm-plan` | PM decomposes into queue, comments task table |
| `pull_request` | `ci` | Deterministic lint/test/compile; no LLM cost |
| PR CI success on `agent:auto` | `review` | Reviewer verdict, strikes, circuit breaker |
| schedule / dispatch `pump` | `claim` → `workers` | Claims ≤ 2 tasks, matrix `max-parallel: 2` |
| dispatch `status` / `requeue` / schedule | `maintenance` | Lease recovery, queue report |

Queue-writing jobs share the concurrency group `ai-queue-<repo>` so claims can never race.

## Cost Model

- **Deterministic CI first**: the reviewer is only paid after lint/tests pass.
- **One provider**: PM, workers, and reviewer all use DeepSeek; the local Architect costs no API tokens.
- **Queue batching**: at most 2 workers and 2 open PRs; no speculative parallelism.
- **Context isolation**: workers receive only `task.files` contents under `max_context_tokens`.
- **Budget halt**: `.ai-bridge/usage.json` is persisted via `actions/cache`; at 100% of the monthly budget the bridge refuses further LLM calls. Cache eviction resets the counter — for production, mirror it to an artifact or external store.
- **Escalation instead of retry storms**: 3 review strikes is the hard stop.

## File Map

```
CLAUDE.md                                local Architect role and issue-creation workflow
.github/workflows/ai-orchestration.yml   pipeline: PM, CI, queue pump, review
config/agents.json                       DeepSeek routing, caps, budget, protected paths
tasks.json                               PM queue (single source of truth)
docs/MASTER_SPEC_TEMPLATE.md             Architect template
scripts/ai_bridge.py                     orchestration engine (stdlib only)
.env.example                             AI_ECONOMY_API_KEY + GH_TOKEN reference
.ai-bridge/                              local usage/state (gitignored)
```

## Known Constraints

- Autonomous chaining requires `AI_BRIDGE_PAT`; without it, PRs opened by `github-actions[bot]` will not trigger CI/review workflows. Manual `workflow_dispatch` still works.
- Budget accounting via `actions/cache` is best-effort (GitHub evicts caches); mirror usage for strict accounting.
- The bridge intentionally refuses to modify its own guardrails (`scripts/ai_bridge.py`, `config/agents.json`, `.github/workflows/**`, `tasks.json`, `CLAUDE.md`). Changing them is a human/Architect action.
