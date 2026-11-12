---
name: Worktracker Attribution
description: Change request attribution, identity headers, commit linkage, or budget enforcement without breaking the plugin/hook/API contract
---

# Attribution contract

`worktracker/attribution.py` is the single source of truth. Read it before
editing `integrations/opencode/plugins/router-tags/index.ts`, `gateway.py`
request handling, or `worktracker/store.py` attribution logic.

- Required identity: `kind`, `session`, `project`, `project_root`,
  `session_directory`, `device`, `tool`; plus `branch` (or the explicit
  no-git marker) and `git_head` when a branch exists.
- Headers are the `x-oc-*` names in `FIELD_HEADERS`; `_ALIASES` lists accepted
  legacy names. `missing()` decides what is absent, `apply()` normalizes the
  event, `error_payload()` builds the actionable 400 response.
- Modes: `warn` (default) records and reports a warning header, `enforce`
  rejects the request before it reaches the provider, `off` disables checks.
  Keep all three working.

## Rules when changing this area

1. Update the plugin and the Python contract together and add/adjust tests in
   `tests/test_worktracker_routing.py`,
   `integrations/opencode/plugins/router-tags/__tests__/tracking.test.ts` and
   `tests/test_worktracker_api.py`.
2. Usage history is immutable: replaying an event id must never rewrite cost,
   tokens, latency or outcome. Link commits with new rows, never by mutating
   stored usage.
3. `store.attribute_commit` combines time windows, branch match, git snapshots
   and tool/file evidence; auxiliary (`kind != primary`) spend stays linked and
   labelled instead of being dropped. Recompute with
   `python -m worktracker recompute`.
4. Budgets are evaluated per project/branch and period; `reserve_usd` projects
   the cost of the in-flight request. `RemoteBudgetClient` applies the reserve
   locally, so the HTTP endpoint must not apply it a second time. Fail-open by
   default; `TRACKER_BUDGET_FAIL_CLOSED=true` returns a blocked payload.
5. Never store prompts, responses or code content — operational metadata only.
6. Schema changes are idempotent (`CREATE ... IF NOT EXISTS`,
   `ALTER ... ADD COLUMN IF NOT EXISTS`); no external migration tool.
7. Keep `docs/worktracker.md`, `README.md`, `deploy/DOKPLOY.md` and
   `integrations/git/README.md` in sync, then run the verification skill
   (`worktracker-verify`).
