# llm-auto-router — implementation steps

Plan: slim Python gateway routing OpenAI-compatible requests across providers
by effort preset (low/medium/high/orchestrator/coder), with live rate-limit,
balance and availability awareness, and automated ranks/costs from public data.

- [x] 1. Recon environment (python 3.14 ✓, no keys in env, e-Infra unauthenticated,
       OpenCode free inference endpoint reachable, no todowrite tool available
       → this file tracks steps instead)
- [x] 2. Scaffold project: venv + pinned requirements + directory layout
- [x] 3. catalog.yaml: providers (e-Infra, OpenCode-free, OpenRouter, DeepSeek),
       models with seeds/overrides/id mappings, presets with weights+ceilings
- [x] 4. Core modules: catalog loader, state store, selector, balancer
- [x] 5. gateway.py: /v1/chat/completions (streaming passthrough, failover,
       x-oc-kind forcing, decision log), /v1/models, /v1/status, /v1/reload
- [x] 6. refresh.py: AA Data API + opencode.ai/data + models.dev → state/ranks.json
- [x] 7. Tests: selector unit tests + mock-upstream integration
       (18 passing: ceilings, ctx floor, tools, cooldown, balance, sorting,
       429→failover replay, aux-kind forcing, prefix handling, 404, direct
       passthrough, SSE, budget exhaustion 503, all-fail 502)
- [x] 8. Live smoke: /healthz ok, /v1/models lists 5 presets, /v1/status shows
       all providers disabled (no keys), chat → 503 no_candidates as expected
- [x] 9. OpenCode plugin: ~/.config/opencode/plugins/router-tags (x-oc-kind header)
- [x] 10. OpenCode config: add `router` provider to opencode.jsonc (preserve existing)
- [ ] 11. E2E verify: gateway-level done (real chat → e-infra/kimi, headers +
       decision log correct); remaining: opencode TUI/run per tier + kind forcing
       observed in decisions (title calls → low)
- [x] 12. README.md + systemd user units + final report to user
- [x] 13. Costs: approximate $/1M per preset in /v1/models + /v1/status;
       actual per-request cost_usd in decision log + totals/by_preset/by_model;
       x-router-cost-usd header; optional daily_cost_usd budget (22 tests pass)
- [ ] 14. User action: fill remaining .env keys (AA_API_KEY, DEEPSEEK_API_KEY),
       review fuzzy matches, map aa_slug/oc_id for e-Infra models
- [x] 15. Discovery: GET /models per enabled provider → state/discovered.json
       (549 candidates: 18 curated + 531 discovered), curated wins on conflict,
       blocklist, exclude_patterns, models_url for OpenCode's split endpoint
- [x] 16. Fuzzy matching (matching.py): explicit → exact → similarity → family,
       ambiguity handling, review list in ranks.json + /v1/status (25 entries),
       unmatched list for manual pinning
- [x] 17. Tests: matching (6) + discovery (4) → 32 passing
- [x] 18. Live dashboard: /dashboard (HTML, 2s polling) + /v1/spend snapshot;
       today buckets per preset/model in state; startup log line with dashboard URL;
       dashboard.html + tests (36 total)
- [x] 19. Dashboard v2: live per-minute spend chart (bars + cumulative line),
       sessions table grouped via x-oc-session header (plugin forwards
       event.sessionID), click-to-filter request feed, daily session/minute
       buckets in state, 37 tests
- [x] 20. Session titles: titles.py TitleCache lazily resolves x-oc-session ids
       via `opencode api get /api/session` (throttled 60s background task,
       cached in state/titles.json, ROUTER_TITLES_CMD override/disable),
       dashboard shows titles, 44 tests
- [x] 21. Tokens-over-time diagram: per-minute input/output token buckets in
       state, durable daily_history (90 days, backfilled from usage buckets),
       /v1/spend exposes tokens_daily + token fields on the timeline, dashboard
       shows a stacked per-minute token chart and a per-day token list, 46 tests
