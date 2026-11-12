# Agent guide: llm-auto-router

OpenAI-compatible LLM gateway that routes each request to the best available
model, plus a self-hosted AI work tracker. One image runs either role, or both:

- `APP_MODE=all` (default) — router + tracker in one process.
- `APP_MODE=router` — router only; forwards usage and budget checks to
  `TRACKER_REMOTE_URL`.
- `APP_MODE=tracker` — tracker only; serves `/worktracker` and `/api/v1`.

## Layout

| Path | What it is |
| --- | --- |
| `gateway.py` | FastAPI app, routing endpoint, tracker mount |
| `selector.py`, `catalog.py`, `catalog.yaml` | model catalog and selection |
| `state.py`, `tracker.py`, `sse.py`, `refresh.py` | runtime state, usage, live feed, refresh |
| `worktracker/` | tracker: `api.py`, `store.py`, `attribution.py`, `budgets.py`, `jobs.py`, `benchmarks.py`, `cli.py`, `ui/` |
| `integrations/opencode/plugins/router-tags` | TypeScript plugin that tags requests with identity |
| `integrations/git/` | stdlib `post-commit` hook + installer |
| `tests/` | pytest suite (`postgres` marker for DB tests) |
| `deploy/` | Dokploy guide, systemd units, `setup-postgres.sh` |

## Toolchain

- Python via the repo virtualenv: `.venv/bin/python`, `.venv/bin/ruff`,
  `.venv/bin/mypy`. Bare `python` is not on PATH.
- Node plugin commands run inside `integrations/opencode/plugins/router-tags`
  with `npm ci`, `npm run check`, `npm test -- --run`.
- Line length 100; ruff + mypy config live in `pyproject.toml`.

## Gates (all must pass before "done")

```bash
.venv/bin/ruff check .
.venv/bin/mypy
TEST_ROUTER_DATABASE_URL='postgresql://user:pass@localhost:55432/db' \
  .venv/bin/python -m pytest --cov
# plugin changes only:
(cd integrations/opencode/plugins/router-tags && npm run check && npm test -- --run)
```

CI (`.github/workflows/ci.yml`) enforces these plus a Docker build with `/healthz`
and `/worktracker` smoke tests and >=85% coverage over `worktracker/` +
`integrations/git/`.

## Rules

- **Usage history is immutable.** Never rewrite stored tokens, cost or latency;
  add columns or events instead.
- **Schema changes are idempotent** (`CREATE ... IF NOT EXISTS`,
  `ALTER ... ADD COLUMN IF NOT EXISTS`) so existing deployments self-migrate.
- **Never commit credentials.** PostgreSQL tests use a disposable database via
  `TEST_ROUTER_DATABASE_URL` and skip when it is unset.
- **Attribution contract** lives in `worktracker/attribution.py`; keep
  `warn` / `enforce` / `off` behaviour and the header names in sync with the
  OpenCode plugin.
- **No prompt/response storage**; only operational metadata is recorded.
- Docs to keep current: `README.md`, `docs/worktracker.md`, `deploy/DOKPLOY.md`,
  `integrations/git/README.md`, `.env.example`.

## Project agents

This repo ships a project agent team under `.opencode/agents/` (default agent:
`orchestrator`) and two skills under `.opencode/skills/` (`worktracker-verify`,
`worktracker-attribution`). See `docs/ai/README.md` for the roster and the map
of where each kind of change belongs.
