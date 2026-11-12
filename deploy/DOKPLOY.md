# Deploy on Dokploy with PostgreSQL

The app image contains the OpenAI-compatible gateway, live dashboard, and history
dashboard. PostgreSQL can be a Dokploy-managed service, any external PostgreSQL
server, or a separately managed database. **Do not put the database connection
inside the image.**

## 1. Create or select PostgreSQL

Create a database and a login for this application. Grant that login permission
to create tables and indexes in its database: the app applies its idempotent
schema creation on startup. It does not create the database itself. Back up this
database; it contains the unlimited usage and work-event history.

For an external database that requires TLS, use its supplied URL parameters, for
example `?sslmode=require`. The `psycopg` driver supports normal PostgreSQL URL
settings including host, port, database, username, password, and SSL mode.

## 2. Create the Dokploy application

Choose **Dockerfile** as the build type with the repository root as context. The
root `Dockerfile` runs as an unprivileged user and exposes port **4141**.

Set these environment variables in Dokploy (Secrets where available):

```dotenv
DATABASE_URL=postgresql://router:REPLACE_ME@postgres:5432/router
ROUTER_STATE_DIR=/data
PORT=4141
```

Use the database's **private service hostname**, not `127.0.0.1` or `localhost`.
Those loopback names refer to the application container itself. For an external
provider use its externally reachable hostname and credentials.

Also configure the provider keys used by `catalog.yaml`, such as
`OPENROUTER_API_KEY`, `E_INFRA_API_KEY`, `OPENCODE_API_KEY`, `DEEPSEEK_API_KEY`,
and `GEMINI_API_KEY`. Only configured providers are enabled. `AA_API_KEY` is
optional for scheduled rank refreshes; rank/discovery snapshots are persisted
separately.

Mount a persistent volume at **`/data`** for cooldown/budget state, discovery and
rank caches, titles, and the bounded recent-decision log. The image defaults
`ROUTER_STATE_DIR=/data`. Usage history itself is in PostgreSQL, not the volume.
Use a single application replica unless external coordination is added for the
JSON routing state and background refresh jobs.

Set the health-check path to **`/healthz`** on port `4141`. The container also
declares a Docker `HEALTHCHECK`. TCP `PORT` can be changed if needed;
`ROUTER_HOST` defaults to `0.0.0.0` in the image.

## 3. Connect clients and dashboards

Point OpenCode's provider base URL at the deployed gateway plus `/v1`, and install
`integrations/opencode/plugins/router-tags` in the OpenCode client. The plugin
supplies the session, stable project ID, canonical project root and worktree in
request headers. Usage then appears under `/dashboard/history`.

**The gateway currently has no built-in authentication.** Keep the service on a
private network or protect it with authenticated reverse-proxy access before
publishing it through a public Dokploy domain. `/v1/work/events` accepts work
activity, while `/v1/reload` and `/v1/reset-failures` are operator controls; these
must not be exposed to an untrusted network.

## Configuration

- `DATABASE_URL`: preferred standard PostgreSQL connection URL.
- `ROUTER_DATABASE_URL`: backward-compatible alias, used only when
  `DATABASE_URL` is absent. No local database is needed in production.
- `ROUTER_STATE_DIR`: writable state/cache directory (default `/data` in Docker).
- `ROUTER_CONFIG_DIR`: alternate directory containing `catalog.yaml` and `.env`,
  optional; environment variables are preferred in Dokploy.
- `ROUTER_HOST`: bind address; Docker defaults to `0.0.0.0`.
- `PORT`: HTTP port, default `4141`.
- `ROUTER_TITLES_CMD`: leave empty in containers without the OpenCode CLI.

If no database URL is set, routing and today's JSON aggregates still work, but
history endpoints return `503` and the live page clearly reports that durable
tracking is disabled.

## 4. Work tracker: single-app (recommended)

The same image runs the router **and** the work tracker in one process. This is
the default (`APP_MODE=all`) and the simplest Dokploy topology: one application,
one volume, one database.

```dotenv
APP_MODE=all
TRACKER_DATABASE_URL=postgresql://router:REPLACE_ME@postgres:5432/router
TRACKER_ADMIN_TOKEN=change-me-admin
TRACKER_EDITOR_KEY=change-me-editor
TRACKER_HOOK_TOKEN=change-me-hook
TRACKER_READ_TOKEN=change-me-read
TRACKER_ATTRIBUTION_MODE=warn
```

`TRACKER_DATABASE_URL` may equal `DATABASE_URL` (shared database) or point at a
dedicated analytics database. When it is empty the tracker is disabled and the
router still serves `/v1` normally.

Scopes are ordered `admin > editor/hook > read`. Every authenticated token may
read; `editor`/`hook` may ingest heartbeats, snapshots, tool events, commits and
usage; only `admin` may edit budgets and job profiles. An empty token disables
that scope's requirement, so set all four in production.

- Dashboard: `https://<domain>/worktracker`
- Health (includes tracker status): `https://<domain>/healthz`
- WakaTime-compatible API for the official VS Code extension: `https://<domain>/api/v1`

## 5. Work tracker: split deployment (APP_MODE=router | tracker)

Run two Dokploy applications from the same image when the router and tracker must
scale or be secured independently.

**Tracker application** (`APP_MODE=tracker`): mount `/data`, set
`TRACKER_DATABASE_URL` and the four tokens, expose port `4141`. It serves
`/worktracker`, `/api/v1` (WakaTime) and `/healthz`; it does **not** route model
requests.

**Router application** (`APP_MODE=router`): set `TRACKER_REMOTE_URL` to the
tracker's private URL and reuse one of the tracker tokens as the bearer:

```dotenv
APP_MODE=router
TRACKER_REMOTE_URL=http://worktracker.internal:4141
TRACKER_HOOK_TOKEN=change-me-hook
```

In router mode the router never opens a local analytics database. It mirrors each
usage event to `<TRACKER_REMOTE_URL>/worktracker/api/usage` and evaluates budgets
via `<TRACKER_REMOTE_URL>/worktracker/api/budget/check`. If the tracker is
unreachable the budget check **fails open** by default; set
`TRACKER_BUDGET_FAIL_CLOSED=true` to block requests instead.

## 6. Schema migrations and backfill

Opening the tracker (or running any command) applies the idempotent schema, so a
fresh deploy migrates itself on first request. To migrate explicitly, backfill
project rows for pre-existing usage, or re-derive commit attribution, run the
bundled CLI inside the container:

```bash
python -m worktracker migrate      # create/upgrade every tracker table
python -m worktracker backfill     # project rows for legacy usage events
python -m worktracker recompute --days 90   # re-derive commit -> AI attribution
python -m worktracker status       # connectivity check + row counts (JSON)
```

## 7. Track OpenCode, git commits and VS Code

1. **OpenCode plugin** — install `integrations/opencode/plugins/router-tags`
   (see its README). It adds session, project, worktree and job headers to every
   routed request, so usage is attributed to the right project automatically.
2. **Git hook (optional)** — install the stdlib `post-commit` hook, which prompts
   for the tracker URL and token, validates them, and stores the token in
   `.git/worktracker.env` (mode `0600`):

   ```bash
   curl -fsSL https://raw.githubusercontent.com/besteFinanzen/custom-llm-auto-router/feat/worktracker/integrations/git/install-hook.sh | sh
   ```

   (Or run `sh integrations/git/install-hook.sh` from a checkout. Point
   `TRACKER_PUBLIC_URL` at `https://<domain>` so the installer prints the exact
   command for your deployment.)

   See `integrations/git/README.md` for `--dry-run`, `--uninstall`, and the
   non-interactive `--url` / `--token` flags.
3. **VS Code** — point the official WakaTime extension at `https://<domain>/api/v1`
   with a tracker token. Heartbeats feed presence, heatmaps and time accounting.

## 8. Budgets and job profiles

Budgets are per project or branch, in USD, tokens or requests, for daily, weekly,
monthly or total periods. A budget warns at `warn_pct`, then hard-blocks when
`hard_block` is set (the router returns an actionable `budget_exceeded` error and
`/v1` calls fail with `429`). Job profiles (score floors, context/cost limits,
latency p95 and success-rate gates) are edited in the UI and stored in
PostgreSQL, overriding the YAML defaults in `jobs.yaml`.
