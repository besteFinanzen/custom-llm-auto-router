# Work tracker

The work tracker turns the router's usage stream into per-project, per-branch,
per-commit AI work accounting: tokens, USD cost, latency, sessions, devices,
tools and human/AI/mixed time — plus budgets, benchmarks and job profiles. It
runs in the same image as the router (`APP_MODE=all`, the default), or as a
standalone service (`APP_MODE=tracker`).

- UI: `/worktracker`
- WakaTime-compatible API (official VS Code extension): `/api/v1`
- Health (includes tracker status): `/healthz`
- Deployment guide: [`deploy/DOKPLOY.md`](../deploy/DOKPLOY.md)

## Modes

| `APP_MODE` | Behaviour |
| --- | --- |
| `all` (default) | Router **and** tracker in one process. One database, one volume. |
| `router` | Router only. Mirrors usage to, and runs budget checks against, `TRACKER_REMOTE_URL`. Never opens a local analytics database. |
| `tracker` | Tracker only. Serves the UI and WakaTime API; does not route model requests. |

In `router` mode a remote tracker that is unreachable makes budget checks **fail
open** unless `TRACKER_BUDGET_FAIL_CLOSED=true`.

## Attribution

Every LLM request carries compact headers (set automatically by the
`integrations/opencode/plugins/router-tags` plugin) or an `x_oc` object in the
JSON body. The fields live in [`worktracker/attribution.py`](../worktracker/attribution.py).

Required: `kind`, `session`, `project`, `project_root`, `session_directory`,
`device`, `tool`, plus `branch` **or** `x-oc-no-git: 1`, and `git_head` when a
branch is present.

| Header | Meaning |
| --- | --- |
| `x-oc-kind` | Request kind: `primary`, `compaction`, `title`, `generate`, … |
| `x-oc-session` | OpenCode session id |
| `x-oc-project` | Stable project id |
| `x-oc-project-root` | Canonical project directory |
| `x-oc-session-directory` | Per-session working directory (project identity source) |
| `x-oc-device` | Hostname (`os.hostname()`) |
| `x-oc-tool` | Client: `opencode`, `vscode`, `cursor`, `cli`, … |
| `x-oc-branch` / `x-oc-no-git` | Current branch, or explicit "not a git session" |
| `x-oc-git-head` | Current commit SHA |
| `x-oc-job` / `x-oc-agent` | Job profile and agent name |
| `x-oc-repo-remote` | Normalised git remote (used for project matching) |

Attribution mode is set with `TRACKER_ATTRIBUTION_MODE`:

- `warn` (default): record the request, log missing fields.
- `enforce`: reject with an actionable `400` (`code: missing_attribution`) that
  lists each missing header and its fix; the request never reaches upstream.
- `off`: store usage without commit attribution.

## Data model

`usage_events` is immutable: stored tokens, cost and latency are never rewritten.
Everything else hangs off it:

- `tracker_projects` / `tracker_project_aliases` — user-named projects with
  aliases; `resolve_project()` maps a key, root or remote to one canonical row.
- `tracker_commit_usage` — commit → AI request attribution with confidence and
  evidence; `tracker_commit_files` holds churn.
- `tracker_git_snapshots`, `tracker_tool_events`, `tracker_heartbeats`,
  `tracker_presence` — git state, tool/file activity, WakaTime heartbeats, and
  live presence.
- `tracker_budgets`, `tracker_job_profiles` — caps and job definitions.
- `tracker_rejected_requests`, `tracker_refresh_runs`, `router_model_stats` —
  audit, refresh history and per-model statistics.

Opening a `TrackerStore` applies the idempotent schema (`CREATE TABLE IF NOT
EXISTS` / `ALTER TABLE … ADD COLUMN IF NOT EXISTS`).

## Commits and spend

Each commit traces to the AI requests that produced it: tokens, USD cost,
latency, model, session, device and tool, split into human / AI / mixed time.
Uncommitted work is shown as *uncommitted spend* and can be recomputed:

- `GET /worktracker/api/commit?key=…&sha=…`
- `POST /worktracker/api/commit/recompute?key=…&sha=…`

Project and commit routes are query-addressed (`?key=…`) so project keys with
slashes (e.g. `path:/repo/demo`) never break routing.

## Budgets

Budgets are per project or branch, in USD (`cost_usd`), `tokens` or `requests`,
for a `daily`, `weekly`, `monthly` or `total` period. A budget warns at
`warn_pct`, then hard-blocks when `hard_block` is set. The router returns an
actionable `budget_exceeded` error (`code: budget_exceeded`) listing every
blocked budget and the fix.

Budget checks are cached for `TRACKER_BUDGET_CACHE_SECONDS`; a per-request cost
reservation is applied on top of the cached value so two concurrent requests
cannot both slip under a cap.

## Benchmarks and job profiles

`refresh.py` merges public benchmarks (Aider polyglot, SWE-bench, Terminal-Bench,
BFCL, LiveBench; optional Artificial Analysis with `AA_API_KEY`) plus observed
latency/success metrics. Job profiles combine score floors, context/cost limits,
`max_latency_p95_ms` and `min_success_rate`; they are edited in the UI and stored
in PostgreSQL, overriding the YAML defaults in [`jobs.yaml`](../jobs.yaml).

Job gates enforce a sample floor (≥5 observations) and never downgrade on
escalation. Preview (`POST /worktracker/api/jobs/preview`) requires the router
catalog (`APP_MODE=all`) and returns `503` otherwise.

## API surface

Tracker management routes are under `/worktracker/api` (scopes: `admin` >
`editor`/`hook` > `read`; every authenticated token may read):

- Overview / stats / models / providers / sessions / presence / heatmap /
  calendar / day / model
- Projects: create, merge, list, aliases, rename, commits, commit detail,
  recompute
- Ingestion: `heartbeats`, `snapshots`, `tool-events`, `commits`, `presence`,
  `usage` (batch; rejects invalid rows individually)
- Budgets: list, create, delete, `budget/check`
- Jobs: list, put, delete, preview, export, import
- Refresh: status (`GET`) and run (`POST`)
- Live presence stream: `GET /worktracker/api/events` (SSE)
- WakaTime: `/api/v1/users/current`, `/heartbeats`, `/heartbeats.bulk`,
  `/users/current/heartbeats`, `/users/current/heartbeats.bulk` (the official
  wakatime-cli spelling, including the VS Code extension),
  `/users/current/status_bar/today`, `/users/current/summaries`,
  `/users/current/durations`, `/projects`

## Maintenance CLI

```bash
python -m worktracker migrate      # create/upgrade every tracker table
python -m worktracker backfill     # project rows for legacy usage events
python -m worktracker recompute --days 90   # re-derive commit -> AI attribution
python -m worktracker status       # connectivity check + row counts (JSON)
```

## Tests and coverage

Tracker tests run against a disposable PostgreSQL database via
`TEST_ROUTER_DATABASE_URL` (marker `postgres`; never commit credentials):

```bash
TEST_ROUTER_DATABASE_URL=postgresql://user:pass@localhost:55432/db \
  .venv/bin/python -m pytest --cov
```

CI enforces ruff, mypy, the PostgreSQL test suite, plugin checks
(`npm run check && npm test -- --run`), Docker build + `/healthz` + `/worktracker`
smoke tests, dependency audits (`pip-audit`, `npm audit`), and ≥85% coverage over
`worktracker/` + `integrations/git/`.

For a live end-to-end check of a running deployment (real git hook, VS Code
heartbeats, attribution, budgets and the read surfaces):

```bash
TRACKER_ADMIN_TOKEN=… TRACKER_EDITOR_KEY=… TRACKER_HOOK_TOKEN=… \
TRACKER_READ_TOKEN=… WORKTRACKER_URL=http://localhost:4143 \
  python3 scripts/e2e_worktracker.py
```
