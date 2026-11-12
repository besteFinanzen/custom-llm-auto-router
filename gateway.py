"""llm-auto-router gateway: OpenAI-compatible proxy with policy routing.

POST /v1/chat/completions  model: "low" | "medium" | "high" | "orchestrator" | "coder"
                           ("router/<preset>" tolerated; "direct/<provider>/<upstream>" bypasses routing)
                           header x-oc-kind: primary|compaction|title|generate
                           (aux kinds are forced to policy.aux_preset)
GET  /v1/models            list exposed virtual models
GET  /v1/status            selector view: providers, balances, cooldowns, usage, ranks
GET  /v1/spend             live spending snapshot (today, all-time, providers, recent)
GET  /v1/history           durable PostgreSQL history (range, projects, breakdowns)
GET  /dashboard            live spending dashboard (HTML)
GET  /dashboard/history    long-range usage and work dashboard (HTML)
POST /v1/work/events       record human/AI work activity (no prompts or responses)
POST /v1/reload            re-read catalog.yaml + state/ranks.json
POST /v1/reset-failures    clear failure counters, backoff and cooldowns
"""
from __future__ import annotations

import asyncio
import datetime
import json
import logging
import os
import pathlib
import time
import uuid
from contextlib import asynccontextmanager
from typing import Any

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response, StreamingResponse

import balance as balance_mod
from catalog import ROOT, Catalog, Model, load_dotenv
from selector import Ranks, effective, preset_estimate, rank_candidates
from sse import normalize_sse_stream
from state import State
from titles import TitleCache
from discovery import load_discovered
from tracker import PostgresUsage

FAILOVER_STATUSES = {401, 403, 408, 409, 429, 500, 502, 503, 504}
EST_CHARS_PER_TOKEN = 4
DECISIONS_MAX_BYTES = 64 * 1024 * 1024
MAX_REJECTED_LOGGED = 40
HOST = os.environ.get("ROUTER_HOST", "127.0.0.1")
PORT = int(os.environ.get("PORT", os.environ.get("ROUTER_PORT", "4141")))
DASHBOARD_URL = f"http://{HOST}:{PORT}/dashboard"


class UpstreamFail(Exception):
    def __init__(self, status: int, detail: str):
        self.status = status
        self.detail = detail[:500]


class Runtime:
    """Swappable app context so /v1/reload can hot-reload config and ranks."""

    def __init__(self, config_dir: pathlib.Path, state_dir: pathlib.Path):
        load_dotenv(config_dir / ".env")
        self.config_dir = config_dir
        self.catalog = Catalog.load(config_dir / "catalog.yaml")
        self.state = State(state_dir)
        self.started_at = time.time()
        self.ranks = Ranks(state_dir)
        self.client = httpx.AsyncClient(timeout=self._timeout())
        self.decisions_file = state_dir / "decisions.jsonl"
        self.titles = TitleCache(state_dir / "titles.json")
        self.discovered: dict[str, Model] = {}
        self.models: dict[str, Model] = {}
        self.tracker_error: str | None = None
        # Standard for managed deployments; retain the original local alias.
        database_url = (os.environ.get("DATABASE_URL") or
                        os.environ.get("ROUTER_DATABASE_URL") or "").strip()
        self.tracker: PostgresUsage | None = None
        if database_url:
            try:
                self.tracker = PostgresUsage(database_url)
                self.tracker.migrate_state(self.state.data)
            except Exception as exc:
                self.tracker_error = f"{type(exc).__name__}: {exc}"
                logging.getLogger("uvicorn.error").error(
                    "PostgreSQL usage store unavailable: %s", self.tracker_error)
                if self.tracker:
                    self.tracker.close()
                self.tracker = None
        self._refresh_models()

    def _refresh_models(self) -> None:
        if (self.catalog.policy.get("discovery") or {}).get("enabled", True):
            self.discovered = load_discovered(self.state.path, self.catalog)
        else:
            self.discovered = {}
        self.models = {**self.discovered, **self.catalog.models}  # curated wins

    def _timeout(self) -> httpx.Timeout:
        return httpx.Timeout(connect=10.0,
                             read=float(self.catalog.policy.get("request_timeout_seconds", 300)),
                             write=60.0, pool=60.0)

    @property
    def policy(self) -> dict:
        return self.catalog.policy

    def reload(self) -> None:
        self.catalog = Catalog.load(self.config_dir / "catalog.yaml")
        self.ranks.load()
        self._refresh_models()

    def log_decision(self, rec: dict) -> None:
        # 500 candidates x every failed request is what turned this log into
        # hundreds of MB; keep a useful head and record the total instead.
        if len(rec.get("rejected") or []) > MAX_REJECTED_LOGGED:
            rec = {**rec, "rejected_total": len(rec["rejected"]),
                   "rejected": rec["rejected"][:MAX_REJECTED_LOGGED]}
        try:
            if self.decisions_file.exists() and self.decisions_file.stat().st_size > DECISIONS_MAX_BYTES:
                self.decisions_file.replace(self.decisions_file.with_suffix(".jsonl.1"))
        except OSError:
            pass
        with self.decisions_file.open("a") as f:
            f.write(json.dumps(rec, default=str) + "\n")

    async def track_usage(self, model: Model, tokens_in: int, tokens_out: int,
                          cost_usd: float, rec: dict, *, estimated: bool,
                          stream: bool) -> None:
        """Update routing aggregates, then persist the durable event.

        A database outage must not take down model routing. Failures are logged
        and surfaced in /v1/status rather than silently discarded.
        """
        await self.state.add_usage(
            model.provider, tokens_in, tokens_out, cost_usd=cost_usd,
            preset=rec.get("preset") or "direct", model=model.id,
            session=rec.get("session"), kind=rec.get("kind"))
        if not self.tracker:
            return
        event = {
            "id": rec.get("request_id") or str(uuid.uuid4()),
            "ts": rec.get("ts"),
            "session": rec.get("session"),
            "project_id": rec.get("project_id"),
            "project_root": rec.get("project_root"),
            "worktree": rec.get("worktree"),
            "kind": rec.get("kind") or "primary",
            "requested_preset": rec.get("preset_requested"),
            "preset": rec.get("preset") or "direct",
            "provider": model.provider,
            "model": model.id,
            "in": tokens_in,
            "out": tokens_out,
            "cost": cost_usd,
            "estimated": estimated,
            "latency_ms": rec.get("latency_ms"),
            "stream": stream,
        }
        try:
            await asyncio.to_thread(self.tracker.record_usage, event)
            self.tracker_error = None
        except Exception as exc:
            self.tracker_error = f"{type(exc).__name__}: {exc}"
            logging.getLogger("uvicorn.error").exception(
                "failed to persist usage event for %s", model.id)


def err(status: int, msg: str, typ: str = "router_error") -> JSONResponse:
    return JSONResponse(status_code=status,
                        content={"error": {"message": msg, "type": typ, "code": str(status)}})


def _json_all_time(rt: Runtime) -> dict:
    """Legacy JSON aggregate fallback when PostgreSQL is not configured."""
    total = {"in": 0, "out": 0, "tokens": 0, "reqs": 0, "cost": 0.0}
    for value in (rt.state.data.get("totals") or {}).values():
        total["in"] += int(value.get("in") or 0)
        total["out"] += int(value.get("out") or 0)
        total["reqs"] += int(value.get("reqs") or 0)
        total["cost"] += float(value.get("cost") or 0.0)
    total["tokens"] = total["in"] + total["out"]
    total["cost"] = round(total["cost"], 10)
    return total


def _discovery_status(rt: Runtime) -> dict:
    out = {"enabled": bool((rt.catalog.policy.get("discovery") or {}).get("enabled", True)),
           "models": len(rt.discovered)}
    path = rt.state.path / "discovered.json"
    if path.exists():
        try:
            data = json.loads(path.read_text())
            out["fetched_at"] = data.get("fetched_at")
            out["providers"] = data.get("providers", {})
        except Exception:
            pass
    return out


class DirectModel(Model):
    """Ad-hoc model for direct/<provider>/<upstream> requests (no routing)."""


async def proxy_call(rt: Runtime, model: Model, body: dict, stream: bool, rec: dict) -> Response:
    prov = rt.catalog.providers[model.provider]
    t0 = time.time()

    # ---- pre-flight: skip models/providers already excluded by failure thresholds ----
    failure_policy = rt.policy.get("failures") or {}
    if failure_policy.get("enabled", True):
        if rt.state.is_model_failed(model.id, failure_policy):
            raise UpstreamFail(503, f"model {model.id} excluded after consecutive failures")
        if rt.state.is_provider_failed(prov.id, failure_policy):
            raise UpstreamFail(503, f"provider {prov.id} excluded after consecutive failures")

    try:
        req = rt.client.build_request("POST", f"{prov.base_url}/chat/completions",
                                      json=body, headers=prov.auth_headers())
        resp = await rt.client.send(req, stream=True)
        if resp.status_code >= 400:
            raw = await resp.aread()
            await resp.aclose()
            if resp.status_code in FAILOVER_STATUSES:
                retry_after = resp.headers.get("retry-after")
                try:
                    cd = min(float(retry_after), float(rt.policy.get("cooldown_max_seconds", 900))) if retry_after \
                        else float(rt.policy.get("cooldown_seconds", 60))
                except ValueError:
                    cd = float(rt.policy.get("cooldown_seconds", 60))

                # ---- record consecutive failure (model counter + provider counter) ----
                err_text = raw.decode("utf-8", "replace")
                await rt.state.record_model_failure(model.id, prov.id, err_text, resp.status_code)

                # ---- apply exponential backoff (multiplier^n) ----
                mult = float(failure_policy.get("backoff_multiplier", 2.0))
                base = float(failure_policy.get("backoff_base_seconds", 60))
                cap = float(failure_policy.get("backoff_max_seconds", 900))
                n = rt.state.get_model_failures(model.id).get("count", 1)
                backoff_cd = min(base * (mult ** max(n - 1, 0)), cap)
                if retry_after:
                    backoff_cd = min(float(retry_after), cap)
                rt.state.set_backoff(model.id, backoff_cd, mult, prov.id)
                await rt.state.set_cooldown(model.id, backoff_cd)
            raise UpstreamFail(resp.status_code, raw.decode("utf-8", "replace"))
    except UpstreamFail:
        raise
    except (httpx.TimeoutException, httpx.TransportError) as exc:
        err_text = f"transport error: {exc}"
        await rt.state.record_model_failure(model.id, prov.id, err_text, 0)
        base = float(failure_policy.get("backoff_base_seconds", 60))
        mult = float(failure_policy.get("backoff_multiplier", 2.0))
        cap = float(failure_policy.get("backoff_max_seconds", 900))
        n = rt.state.get_model_failures(model.id).get("count", 1)
        backoff_cd = min(base * (mult ** max(n - 1, 0)), cap)
        rt.state.set_backoff(model.id, backoff_cd, mult, prov.id)
        await rt.state.set_cooldown(model.id, backoff_cd)
        raise UpstreamFail(0, f"transport error: {exc}")

    cost_in, cost_out, _, _ = effective(model, rt.ranks)

    def cost_of(tin: int, tout: int) -> float:
        return round(tin / 1e6 * cost_in + tout / 1e6 * cost_out, 6)

    est_in = len(json.dumps(body)) // EST_CHARS_PER_TOKEN
    if stream:
        async def gen():
            n = 0
            try:
                # Decode upstream content-encoding (gzip/deflate/br) so the
                # downstream sees plain SSE. Some upstreams (DeepSeek) gzip
                # event streams; forwarding raw bytes without the encoding
                # header makes clients fail with "stream ended without
                # finish_reason".
                async def upstream_bytes():
                    nonlocal n
                    async for chunk in resp.aiter_bytes():
                        n += len(chunk)
                        yield chunk

                # Hold finish_reason back until the stream ends so providers
                # that keep sending deltas after it do not crash strict
                # OpenAI-compatible clients.
                async for chunk in normalize_sse_stream(upstream_bytes()):
                    yield chunk
            finally:
                await resp.aclose()
                tout = max(n // (EST_CHARS_PER_TOKEN * 2), 1)
                cost = cost_of(est_in, tout)
                rec.update(chosen=model.id, latency_ms=int((time.time() - t0) * 1000), stream=True,
                           usage={"in": est_in, "out": tout, "estimated": True},
                           cost_usd=cost, price={"in": cost_in, "out": cost_out})
                await rt.track_usage(model, est_in, tout, cost, rec,
                                     estimated=True, stream=True)
                rt.log_decision(rec)
                await rt.state.record_success(model.id, model.provider)
        return StreamingResponse(gen(), media_type=resp.headers.get("content-type", "text/event-stream"),
                                 headers={"x-router-model": model.id})

    try:
        data = await resp.aread()
    finally:
        await resp.aclose()
    usage: dict[str, Any] = {}
    try:
        usage = (json.loads(data).get("usage")) or {}
    except Exception:
        pass
    tin = int(usage.get("prompt_tokens") or est_in)
    tout = int(usage.get("completion_tokens") or len(data) // (EST_CHARS_PER_TOKEN * 2))
    cost = cost_of(tin, tout)
    rec.update(chosen=model.id, latency_ms=int((time.time() - t0) * 1000), stream=False,
               usage={"in": usage.get("prompt_tokens"), "out": usage.get("completion_tokens")},
               cost_usd=cost, price={"in": cost_in, "out": cost_out})
    await rt.track_usage(model, tin, tout, cost, rec,
                         estimated=not bool(usage.get("prompt_tokens")),
                         stream=False)
    rt.log_decision(rec)
    await rt.state.record_success(model.id, model.provider)
    return Response(content=data, status_code=200,
                    media_type=resp.headers.get("content-type", "application/json"),
                    headers={"x-router-model": model.id,
                             "x-router-cost-usd": f"{cost:.6f}"})


def _recent_decisions(rt: Runtime, limit: int = 40) -> list[dict]:
    """Tail of decisions.jsonl without loading the whole file."""
    path = rt.state.path / "decisions.jsonl"
    if not path.exists():
        return []
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 256_000))
            lines = f.read().splitlines()
    except OSError:
        return []
    out: list[dict] = []
    for raw in reversed(lines):
        try:
            out.append(json.loads(raw))
        except ValueError:
            continue
        if len(out) >= limit:
            break
    return out


def create_app(config_dir: pathlib.Path | None = None,
               state_dir: pathlib.Path | None = None) -> FastAPI:
    config_dir = pathlib.Path(config_dir or os.environ.get("ROUTER_CONFIG_DIR") or ROOT)
    state_dir = pathlib.Path(state_dir or os.environ.get("ROUTER_STATE_DIR") or config_dir / "state")
    rt = Runtime(config_dir, state_dir)
    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        logging.getLogger("uvicorn.error").info("llm-auto-router: dashboard at %s", DASHBOARD_URL)
        yield
        if rt.tracker:
            rt.tracker.close()

    app = FastAPI(title="llm-auto-router", lifespan=lifespan)

    @app.post("/v1/chat/completions")
    async def chat(request: Request):
        body = await request.json()
        requested = str(body.get("model") or "")
        kind = request.headers.get("x-oc-kind", "primary")
        session = (request.headers.get("x-oc-session") or "")[:128] or None
        project_id = (request.headers.get("x-oc-project") or "")[:256] or None
        project_root = (request.headers.get("x-oc-project-root") or "")[:1024] or None
        worktree = (request.headers.get("x-oc-worktree") or "")[:1024] or None
        directory = (request.headers.get("x-oc-directory") or "")[:1024] or None
        rec: dict[str, Any] = {"ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                               "request_id": str(uuid.uuid4()),
                               "kind": kind, "session": session,
                               "project_id": project_id, "project_root": project_root,
                               "worktree": worktree or directory,
                               "requested": requested, "tried": [], "rejected": []}
        stream = bool(body.get("stream"))
        max_age = float(rt.policy.get("balance_cache_seconds", 3600))

        if requested.startswith("direct/"):
            try:
                _, pid, upstream = requested.split("/", 2)
            except ValueError:
                return err(400, f"bad direct model syntax: {requested}")
            prov = rt.catalog.providers.get(pid)
            if not prov:
                return err(404, f"unknown provider {pid}")
            if not prov.enabled:
                return err(503, f"provider {pid} disabled (missing {prov.api_key_env})")
            candidates = [DirectModel({"id": requested, "provider": pid,
                                       "upstream": upstream, "seed_rank": 0})]
        else:
            name = requested.split("/", 1)[1] if requested.startswith("router/") else requested
            if name not in rt.catalog.presets:
                return err(404, f"unknown model '{requested}'. use a preset "
                                f"({', '.join(rt.catalog.presets)}) or direct/<provider>/<upstream>")
            preset_eff = rt.policy.get("aux_preset", "low") if kind in rt.policy.get("aux_kinds", []) else name
            rec.update(preset_requested=name, preset=preset_eff)
            for prov in rt.catalog.providers.values():
                if prov.enabled and prov.balance.get("type") in ("openrouter", "deepseek"):
                    await balance_mod.refresh_balance(prov, rt.state, rt.client, max_age)
            candidates, rec["rejected"] = rank_candidates(rt.catalog, preset_eff, rt.ranks,
                                                          rt.state, rt.models)

        if not candidates:
            rec.update(error="no candidates")
            rt.log_decision(rec)
            return err(503, "no eligible model (all disabled/cooling/over-budget); see "
                            "/v1/status and state/decisions.jsonl", "no_candidates")

        failure_policy = rt.policy.get("failures") or {}
        tried = 0
        limit = 1 + int(rt.policy.get("max_failovers", 3))
        for m in candidates:
            if tried >= limit:
                break
            prov = rt.catalog.providers[m.provider]
            # Already-excluded models are skipped without consuming a failover
            # attempt: otherwise a handful of permanently dead models can hide
            # every healthy candidate behind the max_failovers limit.
            if failure_policy.get("enabled", True) and (
                    rt.state.is_model_failed(m.id, failure_policy)
                    or rt.state.is_provider_failed(m.provider, failure_policy)):
                rec["rejected"].append({"id": m.id,
                                        "reason": "excluded after consecutive failures"})
                continue
            ok, detail = await balance_mod.balance_ok(prov, rt.state, rt.client, max_age)
            if not ok:
                rec["rejected"].append({"id": m.id, "reason": f"balance: {detail}"})
                continue
            tried += 1
            b2 = dict(body)
            b2["model"] = m.upstream
            try:
                return await proxy_call(rt, m, b2, stream, rec)
            except UpstreamFail as f:
                rec["tried"].append({"id": m.id, "status": f.status, "error": f.detail[:200]})
        rec.update(error="all candidates failed")
        rt.log_decision(rec)
        if tried == 0:  # candidates existed but every one failed the balance check
            return err(503, "no eligible model (all candidates over budget); see "
                            "/v1/status and state/decisions.jsonl", "no_candidates")
        last = rec["tried"][-1] if rec["tried"] else {}
        return err(502, f"all candidates failed; last: {last.get('id')} -> {last.get('status')} "
                        f"{last.get('error', '')[:200]}", "upstream_error")

    @app.get("/v1/models")
    async def models():
        now = int(time.time())
        data = []
        for name in rt.catalog.presets:
            data.append({"id": name, "object": "model", "created": now, "owned_by": "router",
                         "router": preset_estimate(rt.catalog, name, rt.ranks, rt.state, rt.models)})
        return {"object": "list", "data": data}

    @app.get("/v1/status")
    async def status():
        return {
            "presets": list(rt.catalog.presets),
            "preset_estimates": {name: preset_estimate(rt.catalog, name, rt.ranks, rt.state, rt.models)
                                 for name in rt.catalog.presets},
            "costs": {
                "today": {pid: rt.state.usage_today(pid).get("cost", 0.0)
                          for pid in rt.catalog.providers},
                "totals": rt.state.data.get("totals", {}),
                "by_preset": rt.state.data.get("by_preset", {}),
                "by_model": rt.state.data.get("by_model", {}),
            },
            "providers": {
                pid: {
                    "enabled": p.enabled,
                    "balance": p.balance,
                    "balance_cached": rt.state.balance_any(pid),
                    "usage_today": rt.state.usage_today(pid),
                } for pid, p in rt.catalog.providers.items()
            },
            "cooldowns": {mid: round(rt.state.cooldown_remaining(mid))
                          for mid in rt.state.data["cooldowns"] if rt.state.cooldown_remaining(mid) > 0},
            "failures": rt.state.data.get("failures", {}),
            "tracking": (rt.tracker.status() if rt.tracker else
                         {"enabled": False, "error": rt.tracker_error or
                          "set DATABASE_URL to enable PostgreSQL history"}),
            "ranks": {"fetched_at": rt.ranks.data.get("fetched_at"),
                      "unmatched": rt.ranks.data.get("unmatched", []),
                      "review": rt.ranks.data.get("review", [])},
            "discovery": _discovery_status(rt),
        }

    @app.get("/v1/spend")
    async def spend():
        agg = {"cost": 0.0, "reqs": 0, "in": 0, "out": 0}
        providers = {}
        for pid, p in rt.catalog.providers.items():
            u = rt.state.usage_today(pid)
            providers[pid] = {
                "enabled": p.enabled,
                "today": u,
                "total": (rt.state.data.get("totals") or {}).get(pid, {}),
                "balance": rt.state.balance_any(pid),
            }
            for k in agg:
                agg[k] += u.get(k, 0)
        agg["cost"] = round(agg["cost"], 6)
        daily = rt.state.data.get("daily") or {}
        sessions = [{"session": k, **v} for k, v in
                    (daily.get("by_session") or {}).items()]
        sessions.sort(key=lambda s: (-(s.get("cost") or 0), -(s.get("reqs") or 0)))
        for s in sessions:
            s["title"] = rt.titles.lookup(s.get("session"))
        unknown = [s["session"] for s in sessions
                   if rt.titles.lookup(s.get("session")) is None
                   and s.get("session") != "(unknown)"]
        rt.titles.ensure_refresh(unknown)
        timeline = [{"t": k, "cost": v.get("cost", 0.0), "reqs": v.get("reqs", 0),
                     "in": v.get("in", 0), "out": v.get("out", 0),
                     "tok": bool(v.get("tok")) or bool(v.get("in")) or bool(v.get("out"))}
                    for k, v in sorted((daily.get("buckets") or {}).items())]
        tokens_daily = [{"date": k, "in": v.get("in", 0), "out": v.get("out", 0),
                         "reqs": v.get("reqs", 0), "cost": v.get("cost", 0.0)}
                        for k, v in sorted((rt.state.data.get("daily_history") or {}).items())]
        if rt.tracker:
            try:
                all_time = await asyncio.to_thread(rt.tracker.all_time)
                tracking = await asyncio.to_thread(rt.tracker.status)
            except Exception as exc:
                all_time = _json_all_time(rt)
                tracking = {"enabled": False, "error": f"{type(exc).__name__}: {exc}"}
        else:
            all_time = _json_all_time(rt)
            tracking = {"enabled": False, "error": rt.tracker_error}
        return {
            "now": time.time(),
            "uptime_s": round(time.time() - rt.started_at),
            "today": agg,
            "all_time": all_time,
            "providers": providers,
            "presets_today": daily.get("by_preset", {}),
            "models_today": daily.get("by_model", {}),
            "sessions": sessions,
            "timeline": timeline,
            "tokens_daily": tokens_daily,
            "presets_total": rt.state.data.get("by_preset", {}),
            "models_total": rt.state.data.get("by_model", {}),
            "cooldowns": {mid: round(rt.state.cooldown_remaining(mid))
                          for mid in rt.state.data["cooldowns"]
                          if rt.state.cooldown_remaining(mid) > 0},
            "recent": _recent_decisions(rt),
            "discovery": _discovery_status(rt),
            "tracking": tracking,
            "ranks": {"fetched_at": rt.ranks.data.get("fetched_at")},
        }

    @app.get("/v1/history")
    async def history(days: int = 0, metric: str = "tokens",
                      project: str | None = None):
        if not rt.tracker:
            return err(503, rt.tracker_error or
                       "PostgreSQL tracking is disabled; set DATABASE_URL",
                       "tracking_unavailable")
        try:
            return await asyncio.to_thread(rt.tracker.history, days, metric, project)
        except ValueError as exc:
            return err(400, str(exc), "bad_history_query")
        except Exception as exc:
            rt.tracker_error = f"{type(exc).__name__}: {exc}"
            return err(503, f"PostgreSQL history unavailable: {rt.tracker_error}",
                       "tracking_unavailable")

    @app.post("/v1/work/events")
    async def work_event(request: Request):
        if not rt.tracker:
            return err(503, rt.tracker_error or
                       "PostgreSQL tracking is disabled; set DATABASE_URL",
                       "tracking_unavailable")
        try:
            payload = await request.json()
        except Exception:
            return err(400, "expected a JSON object", "bad_work_event")
        if not isinstance(payload, dict):
            return err(400, "expected a JSON object", "bad_work_event")
        event = {
            "id": payload.get("id"),
            "ts": payload.get("ts"),
            "session": payload.get("session") or request.headers.get("x-oc-session"),
            "project_id": payload.get("project_id") or request.headers.get("x-oc-project"),
            "project_root": (payload.get("project_root") or
                             request.headers.get("x-oc-project-root")),
            "worktree": payload.get("worktree") or request.headers.get("x-oc-worktree"),
            "source": payload.get("source") or "api",
            "actor": payload.get("actor"),
            "kind": payload.get("kind") or "activity",
            "category": payload.get("category"),
            "duration_s": payload.get("duration_s") or 0,
            "metadata": payload.get("metadata") or {},
        }
        try:
            event_id = await asyncio.to_thread(rt.tracker.record_work_event, event)
        except (ValueError, TypeError) as exc:
            return err(400, str(exc), "bad_work_event")
        except Exception as exc:
            rt.tracker_error = f"{type(exc).__name__}: {exc}"
            return err(503, f"PostgreSQL work store unavailable: {rt.tracker_error}",
                       "tracking_unavailable")
        return {"ok": True, "id": event_id}

    @app.get("/dashboard")
    async def dashboard():
        html = (pathlib.Path(__file__).resolve().parent / "dashboard.html").read_text()
        return Response(html, media_type="text/html")

    @app.get("/dashboard/history")
    async def history_dashboard():
        html = (pathlib.Path(__file__).resolve().parent / "history.html").read_text()
        return Response(html, media_type="text/html")

    @app.get("/")
    async def root():
        return RedirectResponse("/dashboard")

    @app.post("/v1/reload")
    async def reload():
        rt.reload()
        return {"ok": True}

    @app.post("/v1/reset-failures")
    async def reset_failures():
        """Operator escape hatch: re-enable models/providers after a fixed key
        or a transient outage (usage/balance accounting is preserved)."""
        return {"ok": True, "cleared": await rt.state.reset_failures()}

    @app.get("/healthz")
    async def healthz():
        return {"ok": True}

    return app


app = create_app()

if __name__ == "__main__":
    import uvicorn
    print(f"llm-auto-router: dashboard at {DASHBOARD_URL}", flush=True)
    uvicorn.run("gateway:app", host=HOST, port=PORT, log_level="info")
