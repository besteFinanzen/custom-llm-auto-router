# AI / agent guide

Orientation for AI agents (and humans) working on `llm-auto-router`. The repo
root [`AGENTS.md`](../../AGENTS.md) has the condensed rules; this file is the
detailed map.

## Where a change belongs

| Change | Files |
| --- | --- |
| Routing / model selection | `selector.py`, `catalog.py`, `catalog.yaml`, `matching.py` |
| Gateway endpoints, tracker mount | `gateway.py`, `sse.py` |
| Usage recording / live state | `tracker.py`, `state.py` |
| Tracker API routes | `worktracker/api.py`, `worktracker/app.py` |
| Tracker storage & schema | `worktracker/store.py`, `worktracker/cli.py` |
| Attribution contract | `worktracker/attribution.py` |
| Budgets & remote budget checks | `worktracker/budgets.py` |
| Job profiles & validation | `worktracker/jobs.py`, `jobs.yaml` |
| Benchmarks & refresh | `worktracker/benchmarks.py`, `worktracker/refresh_scheduler.py`, `refresh.py` |
| Tracker UI | `worktracker/ui/` |
| OpenCode identity headers | `integrations/opencode/plugins/router-tags/` |
| Git hook | `integrations/git/` |
| Deploy | `deploy/`, `Dockerfile`, `.env.example` |
| Live end-to-end verification | `scripts/e2e_worktracker.py` |

## Data flow

1. OpenCode (with the `router-tags` plugin) sends `x-oc-*` headers.
2. `gateway.py` extracts them via `worktracker/attribution.py`; in `enforce` mode
   a missing field returns an actionable `400` before any upstream call.
3. The request is routed; usage (tokens, cost, latency, model) is recorded in the
   immutable `usage_events` table through `TrackerStore`.
4. Commit hooks and WakaTime heartbeats attach git state, tool/file events and
   presence, giving per-project / per-branch / per-commit accounting.
5. `budgets.py` gates the next request; `jobs.py` + `benchmarks.py` score models.

## Contracts that must not drift

- **Attribution** (`worktracker/attribution.py`): `FIELD_HEADERS`, `REQUIRED`,
  `FIXES`, `_ALIASES`. The plugin must send the same names.
- **Immutability**: stored tokens/cost/latency are never rewritten.
- **Idempotent schema**: safe to apply on every connect.
- **Scopes**: `admin > editor`/`hook` > `read`; any authenticated token may read.
- **Budget failure mode**: fail-open by default, fail-closed only with
  `TRACKER_BUDGET_FAIL_CLOSED=true`.
- **Split mode**: `APP_MODE=router` mirrors usage to
  `<TRACKER_REMOTE_URL>/worktracker/api/usage` and checks
  `<TRACKER_REMOTE_URL>/worktracker/api/budget/check`.

## Project agent roster

Defined in `.opencode/agents/` (default agent `orchestrator`, todo lists enabled):

| Agent | Mode | Model | Owns |
| --- | --- | --- | --- |
| `orchestrator` | primary | `router/orchestrator` | planning, routing, cross-cutting changes |
| `backend-worker` | subagent | `router/coder` | Python tracker/router code and tests |
| `plugin-worker` | subagent | `router/coder` | OpenCode plugin TypeScript |
| `db-migration` | subagent | `router/coder` | schema changes, backfills |
| `architect` | subagent | `router/medium` | read-only design advice |
| `reviewer` | subagent | `router/medium` | read-only review and gates |
| `release` | all | `router/medium` | Docker build, Dokploy deploy, release gates |

## Project skills

Defined in `.opencode/skills/`:

- `worktracker-verify` — quality gates, coverage, Docker smoke, live E2E.
- `worktracker-attribution` — the header/attribution/budget contract to follow
  when touching identity headers, commit linkage or budget enforcement.

## Validation pipeline

Run from the repo root:

```bash
.venv/bin/ruff check .
.venv/bin/mypy
TEST_ROUTER_DATABASE_URL='postgresql://user:pass@localhost:55432/db' \
  .venv/bin/python -m pytest --cov
```

Plugin changes additionally run `npm run check && npm test -- --run` in
`integrations/opencode/plugins/router-tags`. CI also builds the Docker image and
smoke-tests `/healthz` and `/worktracker`, audits Python and npm dependencies,
and enforces >=85% coverage over `worktracker/` + `integrations/git/`.

## Maintenance CLI

```bash
python -m worktracker migrate | backfill | recompute | status
```
