# llm-auto-router

OpenAI-compatible gateway that routes each request to your best available model,
chosen by **requested intelligence tier** → **blended cost ceiling** →
**highest-ranked eligible model**, with live rate-limit, balance and availability
handling.

```
OpenCode ──► router/low|medium|high|orchestrator|coder
              │  (+ plugin header x-oc-kind: forced to `low` for title/compaction/generate)
              ▼
        llm-auto-router :4141
          presets → cost ceiling + floors → rank
          ranks  ← Artificial Analysis + opencode.ai/data + models.dev (daily)
          state  ← 429 cooldowns, token/request budgets, balance cache
              ▼
        e-Infra · OpenCode free · OpenRouter · DeepSeek · Google AI
```

## Quick start

```bash
cd ~/llm-auto-router
source .venv/bin/activate
# 1. create your env file and fill in the keys you have (empty ones = provider auto-disabled)
cp .env.example .env
$EDITOR .env
# 2. refresh ranks/costs (works without AA key; AA adds benchmark indices)
python refresh.py
# 3. optional durable history (PostgreSQL 15+; does not modify system PostgreSQL)
deploy/setup-postgres.sh

# 4. run
python gateway.py            # 127.0.0.1:4141
#    or: uvicorn gateway:app --host 127.0.0.1 --port 4141
```

In OpenCode, register the provider `router` and copy the plugin from
[`integrations/opencode/`](integrations/opencode/) (see that directory for the
exact provider config). It exposes models `low`, `medium`, `high`, `orchestrator`,
`coder`. Pick one with `/models`, `--model router/high`, or pin it in an agent
definition:

```jsonc
"model": "router/orchestrator"   // primary agent
"model": "router/coder"          // execution subagent
```

The companion plugin (`integrations/opencode/plugins/router-tags`) tags every
request with `x-oc-kind`, so title/compaction calls always use the `low` tier.
It also forwards the session, stable project identifier (`x-oc-project`),
canonical project root and worktree, enabling project costs and work attribution
without an additional session lookup.

## Durable usage and history

Set the standard `DATABASE_URL` to a local or external PostgreSQL database to
store one event per successful upstream call, including tokens, cost, selected
model/provider/preset, latency, session and optional project identity. Schema
creation and the one-time legacy aggregate import run automatically at startup;
the original JSON state remains intact for routing/budget checks. No prompts or
responses are stored. `ROUTER_DATABASE_URL` remains a supported legacy alias.

Daily history is no longer pruned. Older lifetime totals and daily aggregates
are migrated as separate baselines, avoiding double-counting overlapping JSON
buckets. Migrate only once; a database marker makes restarts idempotent.

```bash
# Optional local PostgreSQL 18 Docker instance on localhost:55432
deploy/setup-postgres.sh

# Or use an existing, external, or managed instance (works with SSL URL options)
export DATABASE_URL='postgresql://USER:PASSWORD@HOST:5432/DATABASE'

# Test-only, disposable database (never a database containing real history)
TEST_ROUTER_DATABASE_URL='postgresql://USER:PASSWORD@HOST:5432/router_test' \
  .venv/bin/python -m pytest -q tests/test_postgres.py
```

The JSON totals continue to drive budgets even if the analytics database is
temporarily unavailable; usage-event write failures are exposed in `/v1/status`
under `tracking`. For production PostgreSQL (including Dokploy/private-network
services), see **[deploy/DOKPLOY.md](deploy/DOKPLOY.md)**. No database container is
required alongside the application when `DATABASE_URL` points elsewhere.

## Presets

| Preset | Cost ceiling (blended $/1M) | Floors | Best for |
|---|---|---|---|
| `low` | 0 (free only) | 32k ctx | aux calls, trivial work |
| `medium` | 0.60 | 131k ctx, tools | everyday coding |
| `high` | 5.00 | 131k ctx, tools | hard problems |
| `orchestrator` | 5.00 | 200k ctx, tools, agentic-weighted | primary agent that spawns subagents |
| `coder` | 2.00 | 131k ctx, tools, coding-weighted | execution subagents |

Tune everything in `catalog.yaml` (`presets.*.max_cost`, `weights`, `min_context`).

## Selection algorithm

1. `model` names the preset (effort parameter; `router/` prefix tolerated).
2. `x-oc-kind` ∈ `{title, compaction, generate}` → forced to `policy.aux_preset` (`low`).
3. Candidates = models with `enabled`, provider key present, blended cost ≤ ceiling,
   context ≥ floor, tools if required, not cooling down, balance/capacity OK,
   and not excluded by consecutive-failure thresholds.
4. Sort by score (weighted blend of AA intelligence/coding/agentic indices,
   opencode.ai retention + popularity share, or `seed_rank` prior) desc, cost asc.
5. Try in order; on 429/5xx/timeout → cooldown (honors `Retry-After`) → next model.
   Models/providers already excluded by failure tracking are skipped without
   consuming a failover attempt.
6. Every decision is appended to `state/decisions.jsonl`:
   `{ts, kind, requested, preset, rejected[], tried[], chosen, latency_ms, usage}`.
   `rejected[]` is capped at 40 entries (`rejected_total` keeps the full count);
   the log rotates to `decisions.jsonl.1` at 64 MB.

Inspect live state: `curl -s localhost:4141/v1/status | python -m json.tool`.

## Costs

**Approximate cost per preset** — for each preset the router reports the blended
price ($/1M tokens, 0.75·input + 0.25·output) of the model it would pick right now:

- `GET /v1/models` → each preset entry has `"router": {approx_cost_per_1m,
  approx_cost_in, approx_cost_out, top_model, max_cost, candidates}`
- `GET /v1/status` → `preset_estimates` for all presets
- OpenCode `/models` shows the cost *ceiling* in each preset's display name
  (`Router Low (~free)`, `Router High (≤$5/1M)`, …)

**Actual cost per request** — computed from real token usage (or a chars/4
estimate for streams) × the chosen model's prices:

- appended to every decision line: `cost_usd` + `price: {in, out}`
- response headers: `x-router-model`, `x-router-cost-usd` (non-stream)
- aggregated in `state/state.json` and exposed by `GET /v1/status` →
  `costs.{today, totals, by_preset, by_model}` (today's buckets pruned after 3
  days; totals kept forever)

Optional spend caps: `balance.budget.daily_cost_usd` on a budget provider stops
routing to it once today's recorded cost reaches the cap (e.g. e-Infra, or any
provider without a balance API).

## History, projects and work tracking

**http://127.0.0.1:4141/dashboard/history** provides 7/30/90-day and all-time
ranges; token/input/output/cost/request metrics; a daily chart with a cumulative
line; detailed daily rows; provider/preset/model breakdowns; project selection;
and separate human/AI/mixed activity durations. Selecting a project filters the
historical usage and work panels; lifetime aggregates cannot be assigned to a
project retroactively. The live dashboard also shows total cost, requests and
input/output tokens, with per-provider lifetime tokens.

`GET /v1/history?days=90&metric=tokens&project=PROJECT_ID` returns the same data;
`days=0` means unlimited history. `GET /v1/spend` now contains `all_time` and
`tracking`. The `POST /v1/work/events` endpoint accepts optional external
activity records (`actor: human|ai|mixed`, `duration_s`, project/session/root,
source, kind and category). These are explicit caller-reported durations, not a
claim that model latency equals total human/AI coding time. Widget or WakaTime
adapters can use stable UUIDs for idempotent event ingestion; operational
metadata is sanitized to exclude prompts, responses and other content fields.

See the integration guide for the plugin setup. Upgrade/recopy it to start
project attribution for new events.

### Work tracker

**http://127.0.0.1:4141/worktracker** is the self-hosted work tracker: every
commit is linked to the AI requests, tokens, USD cost, latency, model, session,
device, tool and job that produced it (plus uncommitted spend and re-derivable
attribution). It adds project/branch budgets with warnings and hard blocks,
day/week heatmaps, live presence, model/provider statistics and UI-editable job
profiles backed by the benchmark refresh.

Open `/worktracker` and sign in when prompted. The UI stores the token in the
browser tab's session storage and sends it as `Authorization: Bearer …`; use
`TRACKER_ADMIN_TOKEN` for full control, or `TRACKER_READ_TOKEN` for viewing.

The gateway's `/v1/*` endpoints require a bearer key (OpenCode sends it via
`settings.apiKey`). Set `ROUTER_API_KEY` (or `ROUTER_API_KEYS`) in `.env`, or
generate router-scoped keys in the UI under API Keys. If no key is configured,
`/v1` stays open for backward compatibility; creating the first router key turns
enforcement on.

The same image runs three roles (`APP_MODE=all|router|tracker`); the router-only
role forwards usage and budget checks to `TRACKER_REMOTE_URL`. Configure
`TRACKER_DATABASE_URL` and the `TRACKER_ADMIN_TOKEN` / `TRACKER_EDITOR_KEY` /
`TRACKER_HOOK_TOKEN` / `TRACKER_READ_TOKEN` scopes (see `.env.example`).

```bash
# storage is migrated automatically on start; the CLI also works in the image
python -m worktracker migrate     # create/upgrade every tracker table
python -m worktracker backfill    # project rows for pre-existing usage events
python -m worktracker recompute   # re-derive commit -> AI attribution
python -m worktracker status      # connectivity + row counts
python -m worktracker keys create --name laptop --scope router   # mint a key
python -m worktracker keys list                                        # list keys
python -m worktracker keys revoke <id>                                 # revoke a key
```

Tracking is plugin-first (OpenCode plugin tags each request with session,
project, device, tool and job identity); `sh integrations/git/install-hook.sh`
adds an optional stdlib `post-commit` hook that never blocks a commit and queues
reports during outages. VS Code uses the WakaTime-compatible API at
`https://<host>/api/v1`.

- Full reference: `docs/worktracker.md`
- Dokploy single-app and split deployments: `deploy/DOKPLOY.md`
- Live verification of a running deployment: `scripts/e2e_worktracker.py`
  (usage, presence, snapshots, tool events, VS Code heartbeats, a real git
  commit through the hook, attribution, budgets and every read surface).

## Live dashboard

The gateway serves a self-contained live spending dashboard (no extra deps):

**http://127.0.0.1:4141/dashboard** — also logged at startup:

```
llm-auto-router: dashboard at http://127.0.0.1:4141/dashboard
```

Shows, refreshing every 2 s (pause button available): today's spend / requests /
tokens, per-provider cards with live balance chips (OpenRouter credits, DeepSeek
balance, e-Infra budget, rate caps), spend by preset and top models for today,
a **live per-minute spend chart** (bars = cost/minute, line = cumulative, last
120 minutes with activity), a **tokens processed over time** diagram (stacked bars
= input/output tokens per minute, line = cumulative, plus a per-day token
history for the last 14 days), a **sessions** table for today (requests, kind mix,
last model, tokens, cost, last seen — click a row to filter the request feed),
a rolling feed of the latest 40 routing decisions (kind, preset, chosen model,
tokens, cost, latency, failovers), plus cooldown/rank-freshness alerts. Backed by
`GET /v1/spend` (the same JSON if you want to script it; includes `sessions`,
`timeline` and `tokens_daily`).

Session grouping comes from the `x-oc-session` header, set by the bundled
OpenCode plugin (`integrations/opencode/plugins/router-tags`) from `event.sessionID`.
Requests without it group under `(unknown)`.

**Session titles:** session IDs are resolved to their real OpenCode titles via
`opencode api get /api/session` (the local background service; the CLI handles
discovery/auth). The gateway refreshes lazily — max once a minute, in a
background task, whenever `/v1/spend` sees unknown IDs — and caches results in
`state/titles.json`. Titles show up in the sessions table, the request feed,
and the filter chip. A new session gets its title only after OpenCode's aux
`title` call completes, so it appears on a later refresh. Disable with
`ROUTER_TITLES_CMD=` (empty), or point that env var at a custom command.

Note: the per-preset/per-model/session *today* buckets and minute buckets start
accumulating when this feature is first deployed; lifetime `totals` are unaffected.
Restart the gateway after upgrading so `/v1/spend` exposes the new fields.

The per-day token history (`daily_history` in `state/state.json`) is durable —
it is kept for 90 days and is seeded from the shorter-lived daily usage buckets
on startup, so the day rows are populated right away and keep growing.

## Data refresh

`refresh.py` (run daily via the provided timer) first **discovers** each
provider's model list, then merges three free quality/cost sources.

**Discovery** — every *enabled* provider is asked for its `/models` list
(OpenCode uses its separate list endpoint via `models_url`). Results land in
`state/discovered.json`; curated `catalog.yaml` entries always win on id
conflict, and `blocklist:` vetoes specific ids. Discovered models are enabled
automatically — preset ceilings, context/tool floors, balances and cooldowns
decide what actually gets routed. Non-chat ids (embeddings, audio, moderation…)
are filtered by `policy.discovery.exclude_patterns`. Models whose price is
unknown default to `999` so ceilings exclude them.

| Source | Used for | Notes |
|---|---|---|
| [Artificial Analysis](https://artificialanalysis.ai) Data API | intelligence / coding / agentic indices | free key (`AA_API_KEY`), 100 req/day, 1 request per refresh; **attribution required** |
| [opencode.ai/data](https://opencode.ai/data/index.json) | weekly retention + usage share | no key, no quota, daily updates; popularity intentionally low weight |
| [models.dev](https://models.dev/api.json) | prices, context limits, tool support | no key; overrides catalog seeds where matched |

`:free` variants are always priced at 0 and matched only against their exact
models.dev id: they never inherit the paid base model's price, and stale paid
data cannot push them over a `$0` preset ceiling.

Unmatched models keep their `seed_rank` prior and are listed under `unmatched`
in `state/ranks.json` and in `/v1/status`. Fix by adding `aa_slug` / `oc_id` /
`mdev_id` to that model in `catalog.yaml`.

Manual vetoes always win: `rank_override` and `cost_override` per model.

### Reviewing fuzzy matches

Names are matched to benchmark/usage data in tiers: explicit ids → exact →
similarity → model family. Family and ambiguous guesses are recorded in
`state/ranks.json` → `review` (also surfaced in `/v1/status`) with confidence
and alternatives, and printed by `refresh.py`. To pin a match, set `aa_slug` /
`oc_id` in `catalog.yaml` — an explicit id disables fuzzy matching for that
source.

## Balance & availability

| Provider | Mechanism |
|---|---|
| e-Infra | no known API → local daily token budget (`budget.daily_tokens`) |
| OpenCode | local daily request cap (`rate_only.daily_requests`); key required even for free models. Free models also have an undisclosed upstream quota (`Retry-After` on 429); paid models use the per-model prices published by `/models`. |
| OpenRouter | `GET /auth/key` credits check (+ price data from models.dev) |
| DeepSeek | `GET /user/balance` real balance check (`min_balance`) |
| Google AI (Gemini) | no public credit endpoint; upstream 429s trigger cooldown/failover (prices from models.dev) |

API-based checks are cached (`policy.balance_cache_seconds`), fail open, and
exhausted providers are excluded with an explicit reason in the decision log.
Availability is enforced passively from real outcomes (429/5xx/timeout → cooldown).

**Consecutive failures.** Every failed attempt increments the model counter and,
once, the provider counter. After `failures.model_threshold` consecutive model
failures (default 3) or `failures.provider_threshold` provider failures
(default 5), the model/provider is excluded from routing. A success resets both
counters. `failures.*_window_seconds` forgives stale failures (default 1800 s;
`0` means permanent), so a provider can recover after a transient outage or a
fixed key. To re-enable everything immediately without touching usage/balance
accounting:

```bash
curl -s -X POST localhost:4141/v1/reset-failures
```

## Endpoints

| Route | Purpose |
|---|---|
| `POST /v1/chat/completions` | routing entrypoint (OpenAI-compatible, streaming supported) |
| `GET /v1/models` | exposes the presets + approximate cost metadata |
| `GET /v1/status` | providers, balances, usage, **costs**, preset estimates, cooldowns, **failures**, rank freshness, **discovery** snapshot |
| `GET /v1/spend` | live spending snapshot: today, all-time totals, providers, recent decisions |
| `GET /v1/history` | daily totals, project filter, range/metric, provider/preset/model breakdowns, work summary (PostgreSQL) |
| `POST /v1/work/events` | idempotent human/AI/mixed activity event with sanitized operational metadata |
| `GET /dashboard` | live dashboard with all-time cards; `GET /` redirects here |
| `GET /dashboard/history` | long-range/project/work dashboard (PostgreSQL) |
| `POST /v1/reload` | reload `catalog.yaml` + `state/ranks.json` |
| `POST /v1/reset-failures` | clear failure counters, backoff and cooldowns |
| `GET /healthz` | liveness |

Debug bypass: `"model": "direct/<provider>/<upstream>"`, e.g. `direct/e_infra/kimi`.

Streaming streams are normalized: if an upstream keeps sending `delta` chunks
after a `finish_reason`, the gateway withholds the finish reason and emits it in
a final synthetic chunk (with an empty delta) immediately before `data: [DONE]`.
This keeps strict OpenAI-compatible clients from rejecting the post-finish data.

## Integrate with your harness

The gateway speaks the OpenAI **chat completions** API, so anything that can be
pointed at a custom OpenAI-compatible base URL can use it. The `model` field is a
**preset** (`low`, `medium`, `high`, `orchestrator`, `coder`), not an upstream
model id; `router/<preset>` is also authenticated via `settings.apiKey`
(see `.env.example` `ROUTER_API_KEY` or the UI API Keys page).

| Harness | How |
|---|---|
| **OpenCode** | Use the bundled config + plugin in [`integrations/opencode/`](integrations/opencode/) — registers the presets as models and tags aux calls. |
| **Continue / Cline / Roo** | Add an OpenAI-compatible provider with base URL `http://127.0.0.1:4141/v1`, any API key, and select a preset as the model. |
| **Cursor** | Settings → Models → OpenAI API key → *Override Base URL* `http://127.0.0.1:4141/v1`. |
| **Aider** | `aider --openai-api-base http://127.0.0.1:4141/v1 --openai-api-key dummy --model medium` |
| **OpenAI SDK (Python)** | `OpenAI(base_url="http://127.0.0.1:4141/v1", api_key="dummy")`, then `model="medium"`. |
| **curl** | `curl -s http://127.0.0.1:4141/v1/chat/completions -H 'content-type: application/json' -d '{"model":"medium","messages":[{"role":"user","content":"hi"}]}'` |

Optional headers for finer control:

- `x-oc-kind: title | compaction | generate` — force the cheap `low` preset for
  auxiliary calls regardless of the requested tier.
- `x-oc-session: <id>` — group spend per session on the dashboard.
- `x-oc-project: <stable project id>`, `x-oc-project-root: <canonical root>` and
  `x-oc-worktree: <working directory>` — attribute new usage/history records;
  sent automatically by the bundled OpenCode plugin.

## Install as a user service

```bash
mkdir -p ~/.config/systemd/user
cp deploy/llm-auto-router.service deploy/llm-auto-router-refresh.* ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now llm-auto-router.service llm-auto-router-refresh.timer
systemctl --user status llm-auto-router.service
```

The units assume the checkout lives at `~/llm-auto-router` (they use systemd's
`%h` for your home directory); edit `WorkingDirectory`/`ExecStart` if it lives
elsewhere.

## Tests

```bash
.venv/bin/python -m pytest -q     # 76 passed + 5 skipped, scriptable mock upstreams
```

## Caveats

- Tier ceilings and weights are policy seeds — tune with real experience.
- OpenCode free-tier quotas are undisclosed; the daily cap is a conservative guess.
- e-Infra is budget-accounted, not true balance-checked (no known API).
- DeepSeek upstream ids are assumed to alias the current v4 models; verify once the key exists.
- Fuzzy matches (especially e-Infra aliases and `family` guesses) are heuristics — review the
  `review` list after each refresh and pin `aa_slug`/`oc_id` where they are wrong.
- Discovery lists provider ids as-is, including experimental variants and `*-latest`
  aliases; use `blocklist:` if any should never be routed.
- `state/` holds cooldowns, usage, balances and the decision log; safe to delete to reset.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). By submitting a contribution you agree it
is licensed under the same terms as the project (AGPL-3.0-or-later) and that you
accept the [Code of Conduct](CODE_OF_CONDUCT.md).

## Security

See [SECURITY.md](SECURITY.md) for reporting vulnerabilities and for keeping
secrets (`.env`) out of the repository.

## License

Licensed under the **GNU Affero General Public License v3.0 or later**
(`AGPL-3.0-or-later`) — see [LICENSE](LICENSE). This is an **OSI-approved**,
**copyleft** license:

- derivative works must be distributed under the same terms (AGPL-3.0 §5), and
- the copyright, license and attribution notices must be preserved (AGPL-3.0 §4).

In practice that means a fork of this router cannot be shipped as a closed
product: publish the source, keep the license and the author credit, and you
are free to do whatever else you like with it.

> Before publishing, replace `<YOUR NAME>` in `LICENSE` with the copyright holder.

**Network use is covered too.** This project is a network service, so the AGPL's
section 13 applies: if you modify it and let other users interact with it over
a network, you must also offer those users the source of your modified version.
A plain GPL would have left that gap — running a modified fork as a service
without publishing anything.

## Acceptable Use

The license above deliberately contains no field-of-use restrictions, so that
the project stays OSI-approved open source. What follows is therefore a
**statement of the maintainers' expectations — not a license condition**. It
does not limit anyone's legal rights under the AGPL; it is simply what we ask
for, and what we expect from contributors:

- no war, armed conflict, or military activities;
- no production, distribution, or trafficking of illegal drugs;
- no violations of human rights;
- no terrorism or support of terrorist activity.

If you use this project in a way that clashes with the above, we would rather
hear about it than find out later. Please also read the
[Code of Conduct](CODE_OF_CONDUCT.md).
