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
