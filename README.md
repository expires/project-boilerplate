# Multi-Agent Hackathon Boilerplate

A cost-optimized, hierarchical autonomous development pipeline built for a **dual-repo, single-terminal workflow**:

- **Factory repo (this repository)** — architecture, Master Specs, `tasks.json`, guardrails, and orchestration. Local Claude Code acts as the Architect/Product Owner here.
- **Clean project repo (Repo B, `TARGET_REPO`)** — all application code changes and pull requests. Cloud workers never touch the factory repo; Orchestration never writes app code directly.

Every automated background task (PM decomposition, worker implementation, PR review) runs in GitHub Actions on **DeepSeek only**, billed through one key (`AI_ECONOMY_API_KEY`).

```
 Human (Stakeholder) + Local Claude Code (Architect / Product Owner)   [Factory repo]
        │  fills docs/MASTER_SPEC_TEMPLATE.md
        │  gh issue create --label type:master-spec
        ▼
 GitHub Issue (Master Spec)                                            [Factory repo]
        │  Actions job `pm-plan` (DeepSeek)
        ▼
 tasks.json queue ──────────────────────► claim ≤ 2 tasks (concurrency cap)
        │
        ▼
 Worker Agents (DeepSeek, depth 1, max 2 parallel)                     [Target repo]
        │  context-isolated diff on exact files in app-workspace
        ▼
 Pull Request (`agent:auto`)                                           [Target repo]
        │
        ▼
 Deterministic CI in the target repo  ── fail ──► PR blocked
        │ pass
        ▼
 Reviewer Agent (DeepSeek) via scheduled sweep                         [Target repo]
        ├── approve ──────────────► merge, unblock dependents
        └── changes ──────────────► strike (1..3)
                                      │ 3 strikes
                                      ▼
                            Circuit Breaker: close PR, open Escalation Issue
                                      │                                      [Factory repo]
                                      ▼
                     Back to local Claude Code to re-scope
```

The human writes zero code and never joins CI/CD loops.

## Repositories and Responsibilities

| Concern | Factory repo (A) | Target repo (B) |
|---|---|---|
| Master Specs / issues | yes | no |
| `tasks.json` queue, usage state | yes | no |
| `config/agents.json`, bridge, workflows | yes | no |
| Application files, branches, PRs | no | yes |
| Deterministic CI for app code | no | yes |
| Agent review comments, strikes, merge | orchestrated from A | applied to PRs in B |

`TARGET_REPO` (`owner/name`) is read by the bridge locally and via the workflow's repository variable. In Actions, Repo B is cloned into `app-workspace` with `AI_BRIDGE_PAT`.

## Initializing the Application Repository

Local Claude Code (or the human) creates Repo B with one command. It prompts for the repository name, creates the repo, seeds labels, and records `TARGET_REPO` in `.env`:

```bash
python3 scripts/ai_bridge.py init
# Name for the new application repository: my-clean-app
```

Options: `--name my-clean-app`, `--owner my-org`, `--public`, `--with-ci` (seeds a minimal CI workflow in Repo B so the review gate has checks to read). Non-interactive sessions must pass `--name`.

Then:
1. Add repository variable `TARGET_REPO` in the factory repo (`Settings -> Secrets and variables -> Actions -> Variables`).
2. Grant the fine-grained `AI_BRIDGE_PAT` access to **both** repositories (factory: contents/issues/pull-requests/actions write; target: contents/pull-requests write).

## Role Contracts

| Role | Runtime | Trigger | Output | Never does |
|---|---|---|---|---|
| Stakeholder | human | — | Feature request | Code, CI, reviews |
| Architect / Product Owner | **local Claude Code** | human request | Master Spec Issue (`type:master-spec`) | Implementation code |
| Project Manager (DeepSeek) | Actions in factory repo | Master Spec Issue | `tasks.json` entries + labels | Code |
| Worker (DeepSeek) | queue engine | claimed task | Branch + PR in target repo | Spawn agents, touch protected files, read whole repo |
| Reviewer (DeepSeek) | scheduled sweep / dispatch | green CI on `agent:auto` PR | Approve / request changes | Write code |

## The Queue (`tasks.json`)

Every unit of work is one object, stored only in the factory repo. The queue is the single source of truth and the only place agents coordinate state.

| Status | Meaning | Transitions to |
|---|---|---|
| `pending` | ready when `depends_on` all `merged` | `claimed` |
| `claimed` | leased by a worker, lease expires via `requeue-stale` | `in_progress`, `pending` |
| `in_progress` | DeepSeek worker executing against Repo B | `review`, `pending` (retry), `failed` |
| `review` | PR open in Repo B, CI/reviewer running | `merged`, `changes_requested`, `escalated` |
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
| CI before review | required | `gh pr checks` verification against Repo B in the bridge |
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
- If a role is pointed at another provider whose key is missing, the router falls back to the economy provider and `fallback_model_default`.
- Architect planning never calls an API: local Claude Code does it under the human's own subscription.

## Circuit Breaker

1. Reviewer requests changes → `strike:1`, `strike:2`, `strike:3` labels on the Repo B PR.
2. On strike 3 the bridge:
   - posts the full review history on the PR,
   - closes (and optionally deletes the branch of) the PR,
   - opens `[Escalation] <task>` in the factory repo with labels `escalation` + `agent:architect`,
   - marks the task `escalated` and labels the Master Spec `pipeline:halted`.
3. Local Claude Code re-scopes the feature and creates a new Master Spec Issue. No automatic re-entry.

Technical failures follow the same path: `max_task_attempts` exceeded → escalation, no PR loop.

## Quickstart

1. Push the factory repo to GitHub (Actions enabled).
2. Add two repository secrets (`Settings -> Secrets and variables -> Actions`):

   | Name | Purpose |
   |---|---|
   | `AI_ECONOMY_API_KEY` | DeepSeek key used by PM, Worker, and Reviewer |
   | `AI_BRIDGE_PAT` | Fine-grained PAT with access to **both** repos. Factory: `contents`, `issues`, `pull-requests`, `actions` write. Target: `contents`, `pull-requests` write. **Required for autonomous loops** because `GITHUB_TOKEN`-created PRs do not trigger workflows |

3. Initialize Repo B locally, then set the `TARGET_REPO` repository variable:

   ```bash
   python3 scripts/ai_bridge.py init --with-ci
   ```

4. Create factory labels: `type:master-spec`, `spec:planned`, `agent:auto`, `pipeline:halted`, `escalation`, `agent:architect`, `strike:1`, `strike:2`, `strike:3`. Repo B labels are created automatically by `init`.
5. Ask local Claude Code to build a feature. It fills `docs/MASTER_SPEC_TEMPLATE.md` and creates the issue:

   ```bash
   gh issue create --title "Master Spec: <feature>" --body-file /tmp/master-spec.md --label type:master-spec
   ```

6. The PM plans in the factory repo, DeepSeek workers push branches and open PRs in Repo B, Repo B CI runs, and the DeepSeek reviewer decides during the scheduled sweep. Intervene only in escalation issues.

### Local dry run (no tokens, no mutations)

```bash
cp .env.example .env        # optional for live calls; --dry-run needs no credentials
python3 scripts/ai_bridge.py status
python3 scripts/ai_bridge.py plan --issue 1 --dry-run
python3 scripts/ai_bridge.py claim --limit 2
python3 scripts/ai_bridge.py run --task-id T-001 --dry-run
python3 scripts/ai_bridge.py sweep --dry-run
python3 scripts/ai_bridge.py review --pr 42 --dry-run
python3 scripts/ai_bridge.py requeue-stale --minutes 45
```

## Configuration (`config/agents.json`)

- `governance` — caps: `max_concurrency`, `max_open_prs`, `max_depth`, `review_retries`, `max_task_attempts`, `auto_merge`, `lease_minutes`.
- `cost_controls` — token/char/diff ceilings, monthly USD budget with hard halt, and `restrict_worker_to_economy`.
- `context` — `max_files_per_task`, `max_context_tokens`, `protected_paths`.
- `roles` + `providers` + `pricing` — every cloud role routes to the DeepSeek endpoint via `AI_ECONOMY_API_KEY`.
- `target` — `TARGET_REPO` env name, `AI_BRIDGE_APP_DIR` override, and the default `app-workspace` path.
- `blind_collaboration.strip_identity_terms` — words scrubbed from every comment the bridge posts.
- `labels` and `escalation` — GitHub primitive names and Architect assignment.

Model routing is human-owned. Agents themselves never see or publish model identities.

## Workflow (`.github/workflows/ai-orchestration.yml`)

| Trigger | Job | Behavior |
|---|---|---|
| Issue labeled `type:master-spec` | `pm-plan` | PM decomposes into queue in the factory repo |
| `pull_request` (factory only) | `ci` | Deterministic lint/test/compile for pipeline changes |
| schedule / dispatch `review` | `review` | Sweeps open `agent:auto` PRs in Repo B; single-repo mode reviews locally |
| schedule / dispatch `pump` | `claim` → `workers` | Claims ≤ 2 tasks; workers clone Repo B into `app-workspace` and open PRs there |
| dispatch `status` / `requeue` / schedule | `maintenance` | Lease recovery, queue report |

Queue-writing jobs share the concurrency group `ai-queue-<repo>` so claims can never race. Every dirty queue transaction is committed and pushed back to the factory repo by the bridge (`AI_BRIDGE_PERSIST=1` in Actions), which is how `pm-plan` → `claim` → `workers` → `review` share state across runs. Repo B must provide its own CI workflow (or use `init --with-ci`) so the reviewer's `gh pr checks` gate has results to read.

## Cost Model

- **Deterministic CI first**: the reviewer is only paid after Repo B's lint/tests pass.
- **One provider**: PM, workers, and reviewer all use DeepSeek; the local Architect costs no API tokens.
- **Queue batching**: at most 2 workers and 2 open PRs; no speculative parallelism.
- **Context isolation**: workers receive only `task.files` contents from Repo B under `max_context_tokens`.
- **Budget halt**: `.ai-bridge/usage.json` is persisted via `actions/cache`; at 100% of the monthly budget the bridge refuses further LLM calls. Cache eviction resets the counter — for production, mirror it to an artifact or external store.
- **Escalation instead of retry storms**: 3 review strikes is the hard stop.

## File Map

```
CLAUDE.md                                local Architect role, dual-repo rules, issue workflow
.github/workflows/ai-orchestration.yml   pipeline: PM, CI, queue pump, review sweep
config/agents.json                       routing, caps, budget, target repo, protected paths
tasks.json                               PM queue (single source of truth, factory repo)
docs/MASTER_SPEC_TEMPLATE.md             Architect template
scripts/ai_bridge.py                     orchestration engine (stdlib only)
.env.example                             TARGET_REPO + AI_ECONOMY_API_KEY + GH_TOKEN reference
.ai-bridge/                              local usage/state (gitignored)
```

## Known Constraints

- Autonomous chaining requires `AI_BRIDGE_PAT`; without it, PRs opened by `github-actions[bot]` will not trigger CI/review workflows. Manual `workflow_dispatch` still works.
- Queue persistence pushes `tasks.json` to the factory branch on every mutation (up to 3 rebase/push retries). Parallel workers can briefly contend on the file; a rebase conflict fails the run loudly rather than losing state.
- The factory workflow cannot listen to `pull_request` events in Repo B, so agent reviews run from the scheduled sweep (every 20 minutes) or an explicit dispatch. Repo B CI still gates the review itself.
- Repo B must expose CI checks; otherwise the reviewer refuses to run (`no CI checks found`). Use `init --with-ci` for a starter workflow.
- Budget accounting via `actions/cache` is best-effort (GitHub evicts caches); mirror usage for strict accounting.
- The bridge intentionally refuses to modify its own guardrails (`scripts/ai_bridge.py`, `config/agents.json`, `.github/workflows/**`, `tasks.json`, `CLAUDE.md`). Changing them is a human/Architect action.
