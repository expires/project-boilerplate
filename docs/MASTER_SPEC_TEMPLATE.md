# Master Spec: <Feature Name>

> Stakeholder: file this as a GitHub Issue, apply the `master-spec` label, and stop there.
> The Architect owns this document; the PM reads it and writes `tasks.json`; workers never see this issue.

## 1. Stakeholder Request

One paragraph in product language. What user problem is being solved and why now?

## 2. Goals

- [ ] Goal 1
- [ ] Goal 2

## 3. Non-Goals / Out of Scope

- Explicitly excluded behavior the PM must not create tasks for.

## 4. Acceptance Criteria

Each criterion must be observable by deterministic CI or a reviewer agent. Replace examples.

- [ ] AC1: Given <state>, when <action>, then <observable result>.
- [ ] AC2: `GET /health` returns HTTP 200 with `{"status":"ok"}`.
- [ ] AC3: Existing behavior X is unchanged.

## 5. Technical Constraints

- Stack / versions:
- API contracts (request/response shapes, schemas):
- Performance or security requirements:
- Migration or backward-compatibility notes:

## 6. Impacted Areas

| Area | Path(s) | Routing label |
|---|---|---|
| Backend | `src/api/**` | `agent:backend` |
| Frontend | `src/ui/**` | `agent:frontend` |
| Infra / CI | `infra/**` | `agent:infra` |
| Tests | `tests/**` | `agent:test` |
| Docs | `docs/**` | `agent:docs` |

## 7. Suggested Task Breakdown (non-binding)

The PM may deviate, but each item should become one PR-sized task with exact file paths.

1. Task A — files: `...` — depends on: none
2. Task B — files: `...` — depends on: Task A

## 8. Risks and Open Questions

- Risk:
- Open question (answer before planning if possible):

## 9. Cost Estimate

| Field | Value |
|---|---|
| Expected tasks | e.g. 4 |
| Expected worker runs | e.g. 5 |
| Budget note | e.g. within standard monthly budget |

## 10. Definition of Done

- [ ] All tasks `merged` in `tasks.json`
- [ ] All acceptance criteria check out
- [ ] No `strike:*` labels remain
- [ ] No `pipeline:halted` label on this issue

---

### Architect Checklist Before Labeling

- [ ] Acceptance criteria are testable, not aspirational
- [ ] Affected paths are listed and PR-sized
- [ ] No task requires more than 8 files or 600 changed lines
- [ ] Dependencies form a DAG with no cycles
- [ ] No task modifies protected paths (`.github/workflows/**`, `config/agents.json`, `scripts/ai_bridge.py`, `tasks.json`, secrets)
