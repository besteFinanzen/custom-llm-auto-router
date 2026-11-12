name: Pull request

## What and why

<!-- Motivation, linked issue, and the behavior that changes. -->

## Worktracker checklist

Only relevant when `worktracker/`, `gateway.py`, `selector.py`, `catalog.yaml`,
`jobs.yaml`, `integrations/` or `deploy/` change.

- [ ] New/changed tracking behavior has unit tests, and PostgreSQL behavior has
      a test in `tests/test_worktracker_postgres.py` (skips without
      `TEST_ROUTER_DATABASE_URL`; never hardcode credentials).
- [ ] Attribution fields stay documented in `worktracker/attribution.py` +
      `docs/worktracker.md` when headers or required fields change.
- [ ] Usage events stay immutable: never rewrite stored tokens/cost/latency;
      add columns or events instead of mutating history.
- [ ] Schema changes are idempotent (`CREATE ... IF NOT EXISTS`,
      `ALTER ... ADD COLUMN IF NOT EXISTS`) and safe for existing deployments.
- [ ] Budget and attribution behavior in `warn` / `enforce` / `off` modes is
      unchanged unless the PR says otherwise.
- [ ] OpenCode plugin changes: `npm run check && npm test -- --run` in
      `integrations/opencode/plugins/router-tags`.
- [ ] Git hook changes: `worktracker_hook.py` stays stdlib-only and runs on the
      system `python3` (not `.venv`).

## Verification

<!-- Commands you ran, e.g. the coverage + PostgreSQL run from CONTRIBUTING.md -->

## Risk / rollback

<!-- Split-mode (`APP_MODE=router` + remote tracker) impact, migrations, etc. -->
