---
name: Worktracker Verify
description: Run the Worktracker quality gates (ruff, mypy, PostgreSQL tests with the 85% coverage gate, plugin checks, Docker smoke) and the live end-to-end verification for a running deployment
---

# Verify the Worktracker

Run from the repository root. Python comes from the repo virtualenv (bare
`python` is not on PATH). Never commit database credentials; the PostgreSQL
tests use a disposable database and skip when `TEST_ROUTER_DATABASE_URL` is
unset.

## Static gates

```bash
.venv/bin/ruff check .
.venv/bin/mypy
(cd integrations/opencode/plugins/router-tags && npm run check && npm test -- --run)
```

## PostgreSQL tests + coverage gate

```bash
TEST_ROUTER_DATABASE_URL='postgresql://user:pass@localhost:55432/db' \
  .venv/bin/python -m pytest --cov
.venv/bin/python -m coverage report --include='worktracker/*,integrations/git/*' --fail-under=85
```

A skipped `postgres` test means the database was unreachable: fix the URL
instead of accepting the skip. Coverage below 85% fails CI; add tests, do not
lower the gate or exclude code to hide a regression.

## Docker smoke

```bash
docker build -t llm-auto-router:ci .
docker run -d --name router-ci -p 4141:4141 llm-auto-router:ci
curl -fsS http://127.0.0.1:4141/healthz
curl -fsS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:4141/worktracker
docker run --rm llm-auto-router:ci python -m worktracker --help
docker rm -f router-ci
```

`{"worktracker": false}` in `/healthz` means no tracker store was opened; read
the container logs for `Worktracker store unavailable` before continuing.

## Live end-to-end

Requires a running deployment with a reachable database and its four tokens.
Use a scratch repository, never a real one:

```bash
TRACKER_ADMIN_TOKEN=… TRACKER_EDITOR_KEY=… TRACKER_HOOK_TOKEN=… \
TRACKER_READ_TOKEN=… WORKTRACKER_URL=http://localhost:4143 \
  python3 scripts/e2e_worktracker.py
```

The script posts usage/snapshots/tool events/presence, sends WakaTime
heartbeats, installs the real git hook, commits, and asserts attribution,
budgets and every read surface. Remove the scratch repository
(`WORKTRACKER_E2E_REPO`) before a clean rerun; usage rows are immutable, so the
script generates fresh event ids per run.

Also verify `python -m worktracker migrate|backfill|recompute|status` against
the same database. Split deployments: `APP_MODE=tracker` serves `/worktracker`
and `/api/v1` but returns 404 for `/v1/chat/completions`; `APP_MODE=router`
needs `TRACKER_REMOTE_URL` and a hook/editor token.
