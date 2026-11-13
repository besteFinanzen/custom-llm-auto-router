"""FastAPI routes for the work tracker and the WakaTime-compatible API.

The router app mounts this router when ``APP_MODE`` is ``all``; standalone
tracker deployments mount it from :mod:`worktracker.app`.  All database work
runs in a worker thread, keeping the async routing path responsive.
"""
from __future__ import annotations

import asyncio
import base64
import datetime as dt
import json
import pathlib
from collections.abc import Mapping
from typing import Any, ClassVar

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse

from . import benchmarks
from . import jobs as job_profiles
from .config import Settings
from .store import TrackerStore, _iso, utcnow

UI_PATH = pathlib.Path(__file__).with_name("ui") / "index.html"

# Server-sent events: how many chunks a stream yields before closing and how
# long to wait between them.  Kept as module constants so tests can shrink the
# cadence instead of sleeping for real.
EVENT_POLL_SECONDS = 5.0
EVENT_MAX_CHUNKS = 120

router = APIRouter()
waka = APIRouter()
page = APIRouter()


def _token(request: Request) -> str:
    authorization = request.headers.get("authorization", "")
    if authorization.lower().startswith("basic "):
        try:
            decoded = base64.b64decode(authorization.split(" ", 1)[1]).decode("utf-8", "replace")
            return decoded.split(":", 1)[0]
        except Exception:
            return ""
    if authorization.lower().startswith("bearer "):
        return authorization.split(" ", 1)[1].strip()
    return (request.headers.get("x-api-key")
            or request.query_params.get("api_key")
            or request.query_params.get("token") or "")


class Auth:
    """Scope check with an explicit hierarchy.

    admin > editor > hook > read; ``router`` is inference-only and can never
    read tracker data.  No configured tokens = open mode unless
    ``require_configured`` is set (used by key-management routes).

    ``_ALLOWED[token_scope]`` is the set of required scopes that token scope
    satisfies.  ``admin`` satisfies everything and is short-circuited.
    """

    _ALLOWED: ClassVar[dict[str, set[str]]] = {
        "router": {"router"},
        "hook": {"hook", "read"},
        "read": {"read"},
        "editor": {"editor", "hook", "read"},
    }

    def __init__(self, *scopes: str, require_configured: bool = False):
        self.scopes = set(scopes)
        self.require_configured = require_configured

    def _allowed(self, token_scope: str) -> bool:
        if token_scope == "admin":
            return True
        return bool(self._ALLOWED.get(token_scope, set()) & self.scopes)

    async def __call__(self, request: Request) -> str:
        settings: Settings | None = getattr(request.app.state, "wt_settings", None)
        if settings is None:
            return "open"
        store = getattr(request.app.state, "wt_store", None)
        token = _token(request)

        # 1. Env tokens (bootstrap / root credentials)
        env_scope = settings.tokens().get(token)
        if env_scope:
            if self._allowed(env_scope):
                return env_scope
            raise HTTPException(status_code=403,
                                detail=f"token scope '{env_scope}' cannot perform this action")

        # 2. DB-backed keys (generated from the UI)
        if store is not None:
            key = await _run(store.resolve_api_key, token)
            if key:
                if self._allowed(key["scope"]):
                    await _run(store.touch_api_key, key["id"])
                    return key["scope"]
                raise HTTPException(status_code=403,
                                    detail=f"key scope '{key['scope']}' cannot perform this action")

        # 3. No token matched
        has_env = bool(settings.tokens())
        has_db = await _run(store.has_active_api_keys) if store is not None else False
        if not has_env and not has_db:
            if self.require_configured:
                raise HTTPException(status_code=409,
                                    detail="tracker not configured: set TRACKER_ADMIN_TOKEN first")
            return "open"
        if not has_env and not has_db:
            return "open"
        raise HTTPException(status_code=401, detail="invalid or missing API token")


def _store(request: Request) -> TrackerStore:
    store = getattr(request.app.state, "wt_store", None)
    if store is None:
        raise HTTPException(status_code=503, detail="work tracker database is not configured")
    return store


def _settings(request: Request) -> Settings:
    return getattr(request.app.state, "wt_settings", Settings.from_env())


def _int(value: Any, default: int, low: int, high: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, number))


async def _run(fn, *args, **kwargs):
    return await asyncio.to_thread(fn, *args, **kwargs)


def _bad(exc: Exception) -> HTTPException:
    if isinstance(exc, KeyError):
        return HTTPException(status_code=404, detail=str(exc).strip("'"))
    return HTTPException(status_code=400, detail=str(exc))


async def _json(request: Request) -> Any:
    try:
        return await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="body must be valid JSON") from exc


# ---------------------------------------------------------------- overview / UI
@page.get("/worktracker", response_class=HTMLResponse, include_in_schema=False)
async def worktracker_ui() -> HTMLResponse:
    if not UI_PATH.exists():
        return HTMLResponse("<h1>Worktracker UI missing</h1>", status_code=404)
    return HTMLResponse(UI_PATH.read_text())


@router.get("/overview", dependencies=[Depends(Auth("read"))])
async def overview(request: Request, days: int = 30):
    store = _store(request)
    days = _int(days, 30, 1, 730)
    data = await _run(store.overview, days)
    data["today"] = await _run(store.today_summary)
    data["time"] = await _run(store.time_breakdown, days)
    data["breakdowns"] = {
        "devices": await _run(store.breakdown, "device", days),
        "tools": await _run(store.breakdown, "tool", days),
        "jobs": await _run(store.breakdown, "job", days),
        "kinds": await _run(store.breakdown, "kind", days),
        "providers": await _run(store.breakdown, "provider", days),
    }
    data["timezone"] = _settings(request).timezone
    data["refresh"] = await _run(store.refresh_status)
    return data


@router.get("/config", dependencies=[Depends(Auth("read"))])
async def config(request: Request):
    settings = _settings(request)
    store = getattr(request.app.state, "wt_store", None)
    auth_enabled = bool(settings.tokens())
    router_auth_enabled = settings.router_auth_enabled
    if store is not None:
        has_db_keys = await _run(store.has_active_api_keys)
        auth_enabled = auth_enabled or has_db_keys
        has_router_key = await _run(store.has_router_api_key)
        router_auth_enabled = router_auth_enabled or has_router_key
    return {
        "timezone": settings.timezone,
        "attribution_mode": settings.attribution_mode,
        "idle_timeout_s": settings.idle_timeout_s,
        "heartbeat_interval_s": settings.heartbeat_interval_s,
        "presence_ttl_s": settings.presence_ttl_s,
        "retention_days": settings.retention_days,
        "redact_paths": settings.redact_paths,
        "public_url": settings.public_url,
        "app_mode": getattr(request.app.state, "wt_app_mode", "all"),
        "auth_enabled": auth_enabled,
        "router_auth_enabled": router_auth_enabled,
    }


# ---------------------------------------------------------------- projects
@router.get("/whoami", dependencies=[Depends(Auth("read"))])
async def whoami(request: Request):
    """Small credential-validation endpoint used by the git hook installer."""
    settings = _settings(request)
    store = getattr(request.app.state, "wt_store", None)
    token = _token(request)
    env_scope = settings.tokens().get(token)
    if env_scope:
        return {"ok": True, "scope": env_scope, "source": "env",
                "mode": getattr(request.app.state, "wt_app_mode", "all"),
                "timezone": settings.timezone,
                "can_manage_keys": env_scope == "admin"}
    if store is not None:
        key = await _run(store.resolve_api_key, token)
        if key:
            return {"ok": True, "scope": key["scope"], "source": "key",
                    "name": key["name"], "key_id": key["id"],
                    "mode": getattr(request.app.state, "wt_app_mode", "all"),
                    "timezone": settings.timezone,
                    "can_manage_keys": key["scope"] == "admin"}
    return {"ok": True, "scope": "open", "source": "open",
            "mode": getattr(request.app.state, "wt_app_mode", "all"),
            "timezone": settings.timezone,
            "can_manage_keys": False}


# ---------------------------------------------------------------- API keys
_KEY_SCOPES = {"router", "read", "editor", "hook", "admin"}


@router.get("/keys", dependencies=[Depends(Auth("admin", require_configured=True))])
async def list_api_keys(request: Request):
    """List all API keys (without hashes or secrets)."""
    store = _store(request)
    keys = await _run(store.list_api_keys)
    return {"keys": keys}


@router.post("/keys", dependencies=[Depends(Auth("admin", require_configured=True))])
async def create_api_key(request: Request):
    """Create a new API key.  The full secret is returned exactly once."""
    body = await _json(request)
    if not isinstance(body, Mapping):
        raise HTTPException(400, "body must be an object")
    name = str(body.get("name") or "").strip()
    scope = str(body.get("scope") or "").strip()
    expires_at = body.get("expires_at")
    if not name:
        raise HTTPException(400, "name is required")
    if scope not in _KEY_SCOPES:
        raise HTTPException(400, f"invalid scope: {scope or '(missing)'}")
    if expires_at is not None:
        try:
            expires_at = dt.datetime.fromisoformat(str(expires_at))
        except ValueError as exc:
            raise HTTPException(400, "expires_at must be an ISO-8601 timestamp") from exc
    store = _store(request)
    try:
        full_key, key_id = await _run(
            store.create_api_key, name, scope, "ui", expires_at)
    except Exception as exc:
        raise _bad(exc) from exc
    return {"key": full_key, "id": key_id, "name": name, "scope": scope,
            "prefix": full_key.split("_")[1],
            "expires_at": expires_at.isoformat() if expires_at else None}


@router.delete("/keys/{key_id}",
               dependencies=[Depends(Auth("admin", require_configured=True))])
async def revoke_api_key(request: Request, key_id: str):
    """Soft-revoke an API key (sets ``revoked_at``)."""
    store = _store(request)
    ok = await _run(store.revoke_api_key, key_id)
    if not ok:
        raise HTTPException(404, "key not found or already revoked")
    return {"ok": True, "id": key_id}


# NOTE: project keys are full git remotes or paths ("host/org/repo",
# "path:/src/app"), so they contain slashes. Those endpoints therefore take the
# key as a query parameter and only key-less paths appear in the route table;
# the path-style aliases below resolve keys without slashes.
async def _projects_list(request: Request, days: int = 30):
    store = _store(request)
    return {"projects": await _run(store.list_projects, _int(days, 30, 0, 730))}


async def _projects_create(request: Request):
    body = await _json(request)
    if not isinstance(body, Mapping):
        raise HTTPException(400, "body must be an object")
    store = _store(request)
    try:
        project = await _run(
            store.resolve_project, body.get("key") or body.get("project"),
            body.get("root") or body.get("path"), body.get("remote"),
            body.get("display_name") or body.get("name"), body.get("color"))
        if body.get("display_name") or body.get("name") or body.get("color"):
            project = await _run(store.rename_project, project["key"],
                                 body.get("display_name") or body.get("name")
                                 or project["display_name"], body.get("color"))
    except Exception as exc:
        raise _bad(exc) from exc
    return {"project": project}


async def _projects_merge(request: Request):
    body = await _json(request)
    source, target = body.get("source"), body.get("target")
    if not source or not target:
        raise HTTPException(400, "source and target are required")
    try:
        return await _run(_store(request).merge_projects, source, target)
    except Exception as exc:
        raise _bad(exc) from exc


def _key(request: Request, key: str | None = None) -> str:
    value = (key or request.query_params.get("key") or "").strip()
    if not value:
        raise HTTPException(400, "project key is required (?key=...)")
    return value


async def _project_detail(request: Request, key: str, days: int = 30):
    store = _store(request)
    try:
        data = await _run(store.project_detail, key, _int(days, 30, 1, 730))
    except Exception as exc:
        raise _bad(exc) from exc
    if not data:
        raise HTTPException(404, f"unknown project {key}")
    data["files"] = await _run(store.project_files, key, _int(days, 30, 0, 730))
    data["calendar"] = await _run(store.calendar, _int(days, 30, 1, 730), key)
    return data


async def _project_aliases(request: Request, key: str):
    return {"aliases": await _run(_store(request).project_aliases, key)}


async def _project_add_alias(request: Request, key: str):
    body = await _json(request)
    kind = str(body.get("kind") or "key")
    value = str(body.get("value") or "").strip()
    if not value:
        raise HTTPException(400, "alias value is required")
    try:
        return await _run(_store(request).add_alias, key, kind, value)
    except Exception as exc:
        raise _bad(exc) from exc


async def _project_commits(request: Request, key: str, days: int = 0, limit: int = 200):
    return {"commits": await _run(_store(request).commits, key,
                                  _int(days, 0, 0, 730), _int(limit, 200, 1, 1000))}


async def _commit_detail(request: Request, key: str, sha: str):
    data = await _run(_store(request).commit_detail, key, sha)
    if not data:
        raise HTTPException(404, f"unknown commit {sha} in {key}")
    return data


# --- key-less routes (query parameter `key`) ---------------------------------
@router.get("/projects", dependencies=[Depends(Auth("read"))])
async def list_projects(request: Request, days: int = 30):
    return await _projects_list(request, days)


@router.post("/projects", dependencies=[Depends(Auth("editor"))])
async def create_project(request: Request):
    return await _projects_create(request)


@router.post("/projects/merge", dependencies=[Depends(Auth("editor"))])
async def merge_projects(request: Request):
    return await _projects_merge(request)


@router.get("/project", dependencies=[Depends(Auth("read"))])
async def project_detail(request: Request, days: int = 30, key: str | None = None):
    return await _project_detail(request, _key(request, key), days)


@router.patch("/project", dependencies=[Depends(Auth("editor"))])
async def rename_project(request: Request, key: str | None = None):
    project_key = _key(request, key)
    body = await _json(request)
    try:
        return {"project": await _run(
            _store(request).rename_project, project_key,
            str(body.get("display_name") or body.get("name") or project_key),
            body.get("color"))}
    except Exception as exc:
        raise _bad(exc) from exc


@router.get("/project/aliases", dependencies=[Depends(Auth("read"))])
async def project_aliases(request: Request, key: str | None = None):
    return await _project_aliases(request, _key(request, key))


@router.post("/project/aliases", dependencies=[Depends(Auth("editor"))])
async def add_alias(request: Request, key: str | None = None):
    return await _project_add_alias(request, _key(request, key))


@router.get("/project/commits", dependencies=[Depends(Auth("read"))])
async def project_commits(request: Request, days: int = 0, limit: int = 200,
                          key: str | None = None):
    return await _project_commits(request, _key(request, key), days, limit)


@router.get("/commit", dependencies=[Depends(Auth("read"))])
async def commit_detail(request: Request, sha: str, key: str | None = None):
    return await _commit_detail(request, _key(request, key), sha)


# --- path-style aliases (keys without slashes) -------------------------------
@router.get("/projects/{key}", dependencies=[Depends(Auth("read"))])
async def project_detail_path(key: str, request: Request, days: int = 30):
    return await _project_detail(request, key, days)


@router.patch("/projects/{key}", dependencies=[Depends(Auth("editor"))])
async def rename_project_path(key: str, request: Request):
    return await rename_project(request, key)


@router.get("/projects/{key}/aliases", dependencies=[Depends(Auth("read"))])
async def project_aliases_path(key: str, request: Request):
    return await _project_aliases(request, key)


@router.post("/projects/{key}/aliases", dependencies=[Depends(Auth("editor"))])
async def add_alias_path(key: str, request: Request):
    return await _project_add_alias(request, key)


@router.get("/projects/{key}/commits", dependencies=[Depends(Auth("read"))])
async def project_commits_path(key: str, request: Request, days: int = 0, limit: int = 200):
    return await _project_commits(request, key, days, limit)


@router.get("/projects/{key}/commits/{sha}", dependencies=[Depends(Auth("read"))])
async def commit_detail_path(key: str, sha: str, request: Request):
    return await _commit_detail(request, key, sha)


async def _recompute_commit(request: Request, key: str, sha: str):
    try:
        result = await _run(_store(request).recompute_commit, key, sha)
    except Exception as exc:
        raise _bad(exc) from exc
    gate = getattr(request.app.state, "wt_budget_gate", None)
    if gate:
        gate.invalidate()
    return result


@router.post("/commit/recompute", dependencies=[Depends(Auth("editor"))])
async def recompute_commit(request: Request, sha: str, key: str | None = None):
    return await _recompute_commit(request, _key(request, key), sha)


@router.post("/projects/{key}/commits/{sha}/recompute",
             dependencies=[Depends(Auth("editor"))])
async def recompute_commit_path(key: str, sha: str, request: Request):
    return await _recompute_commit(request, key, sha)


@router.get("/commits", dependencies=[Depends(Auth("read"))])
async def timeline(request: Request, days: int = 30, project_key: str | None = None,
                    limit: int = 100):
    return {"commits": await _run(_store(request).timeline,
                                  _int(days, 30, 0, 730), project_key,
                                  _int(limit, 100, 1, 1000))}


# ---------------------------------------------------------------- activity
@router.get("/sessions", dependencies=[Depends(Auth("read"))])
async def sessions(request: Request, days: int = 30, project_key: str | None = None):
    return {"sessions": await _run(_store(request).sessions,
                                   _int(days, 30, 0, 730), project_key)}


@router.get("/presence", dependencies=[Depends(Auth("read"))])
async def presence(request: Request):
    store = _store(request)
    return {"presence": await _run(store.presence),
            "today": await _run(store.today_summary)}


@router.get("/models", dependencies=[Depends(Auth("read"))])
async def models(request: Request, days: int = 30, project_key: str | None = None):
    store = _store(request)
    days = _int(days, 30, 0, 730)
    return {"models": await _run(store.breakdown, "model", days, project_key),
            "providers": await _run(store.breakdown, "provider", days, project_key),
            "observed": await _run(store.observed_metrics)}


@router.get("/providers", dependencies=[Depends(Auth("read"))])
async def provider_models(request: Request, days: int = 30,
                          project_key: str | None = None):
    """Usage and cost of every model, grouped by provider (for the stats page)."""
    store = _store(request)
    return await _run(store.provider_models, _int(days, 30, 0, 730), project_key)


@router.get("/model", dependencies=[Depends(Auth("read"))])
async def model_detail(request: Request, model: str, days: int = 90,
                       project_key: str | None = None):
    """Detailed statistics and cost history for one model."""
    if not model.strip():
        raise HTTPException(400, "model is required")
    store = _store(request)
    return await _run(store.model_detail, model, _int(days, 90, 1, 730), project_key)


@router.get("/stats", dependencies=[Depends(Auth("read"))])
async def stats(request: Request, days: int = 30, project_key: str | None = None):
    store = _store(request)
    days = _int(days, 30, 0, 730)
    return {
        "range": {"days": days, "project_key": project_key},
        "projects": await _run(store.breakdown, "project", days, project_key),
        "branches": await _run(store.breakdown, "branch", days, project_key),
        "devices": await _run(store.breakdown, "device", days, project_key),
        "tools": await _run(store.breakdown, "tool", days, project_key),
        "jobs": await _run(store.breakdown, "job", days, project_key),
        "kinds": await _run(store.breakdown, "kind", days, project_key),
        "time": await _run(store.time_breakdown, days, project_key),
    }


@router.get("/heatmap", dependencies=[Depends(Auth("read"))])
async def heatmap(request: Request, days: int = 28, project_key: str | None = None):
    return await _run(_store(request).heatmap, _int(days, 28, 1, 730), project_key)


@router.get("/calendar", dependencies=[Depends(Auth("read"))])
async def calendar(request: Request, days: int = 365, project_key: str | None = None):
    return await _run(_store(request).calendar, _int(days, 365, 1, 730), project_key)


@router.get("/day", dependencies=[Depends(Auth("read"))])
async def day(request: Request, date: str | None = None, project_key: str | None = None):
    value = date or dt.datetime.now(_settings(request).tz).date().isoformat()
    try:
        return await _run(_store(request).day_cells, value, project_key)
    except Exception as exc:
        raise _bad(exc) from exc


@router.get("/events", dependencies=[Depends(Auth("read"))])
async def events(request: Request):
    store = _store(request)
    settings = _settings(request)

    async def stream():
        for index in range(EVENT_MAX_CHUNKS):
            if await request.is_disconnected():
                break
            presence_rows = await _run(store.presence)
            today = await _run(store.today_summary)
            chunk = json.dumps({"presence": presence_rows, "today": today,
                                "index": index, "tz": settings.timezone})
            yield f"event: presence\ndata: {chunk}\n\n"
            await asyncio.sleep(EVENT_POLL_SECONDS)

    return StreamingResponse(stream(), media_type="text/event-stream")


# ---------------------------------------------------------------- ingestion
@router.post("/heartbeats", dependencies=[Depends(Auth("editor"))])
async def post_heartbeat(request: Request):
    payload = await _json(request)
    try:
        ids = await _run(_ingest_heartbeats, _store(request), payload)
    except Exception as exc:
        raise _bad(exc) from exc
    return {"responses": ids, "count": len(ids)}


@router.post("/snapshots", dependencies=[Depends(Auth("hook", "editor"))])
async def post_snapshot(request: Request):
    payload = await _json(request)
    try:
        return {"id": await _run(_store(request).record_git_snapshot, payload)}
    except Exception as exc:
        raise _bad(exc) from exc


@router.post("/tool-events", dependencies=[Depends(Auth("hook", "editor"))])
async def post_tool_event(request: Request):
    payload = await _json(request)
    try:
        return {"id": await _run(_store(request).record_tool_event, payload)}
    except Exception as exc:
        raise _bad(exc) from exc


@router.post("/commits", dependencies=[Depends(Auth("hook", "editor"))])
async def post_commit(request: Request):
    payload = await _json(request)
    if not isinstance(payload, Mapping):
        raise HTTPException(400, "body must be an object")
    try:
        commit = await _run(_store(request).record_commit, payload)
    except Exception as exc:
        raise _bad(exc) from exc
    gate = getattr(request.app.state, "wt_budget_gate", None)
    if gate:
        gate.invalidate()
    return {"commit": {**commit, "observed_at": _iso(commit.get("observed_at"))}}


@router.post("/presence", dependencies=[Depends(Auth("editor", "hook"))])
async def post_presence(request: Request):
    payload = await _json(request)
    try:
        return {"presence": await _run(_store(request).upsert_presence, payload)}
    except Exception as exc:
        raise _bad(exc) from exc


@router.post("/usage", dependencies=[Depends(Auth("hook", "editor"))])
async def post_usage(request: Request):
    payload = await _json(request)
    rows = payload if isinstance(payload, list) else [payload]
    store = _store(request)
    ids, rejected = [], []
    for row in rows:
        if not isinstance(row, Mapping):
            rejected.append({"error": "row must be an object"})
            continue
        try:
            ids.append(await _run(store.record_usage_event, row))
        except Exception as exc:
            rejected.append({"id": str(row.get("id") or ""), "error": str(exc)})
    return {"accepted": len(ids), "ids": ids, "rejected": rejected}


# ---------------------------------------------------------------- budgets
@router.get("/budgets", dependencies=[Depends(Auth("read"))])
async def list_budgets(request: Request, project_key: str | None = None):
    return {"budgets": await _run(_store(request).list_budgets, project_key)}


@router.post("/budgets", dependencies=[Depends(Auth("admin"))])
async def upsert_budget(request: Request):
    payload = await _json(request)
    try:
        budget = await _run(_store(request).upsert_budget, payload)
    except Exception as exc:
        raise _bad(exc) from exc
    gate = getattr(request.app.state, "wt_budget_gate", None)
    if gate:
        gate.invalidate()
    return {"budget": budget}


@router.delete("/budgets/{budget_id}", dependencies=[Depends(Auth("admin"))])
async def delete_budget(budget_id: str, request: Request):
    deleted = await _run(_store(request).delete_budget, budget_id)
    gate = getattr(request.app.state, "wt_budget_gate", None)
    if gate:
        gate.invalidate()
    if not deleted:
        raise HTTPException(404, "unknown budget")
    return {"deleted": True}


@router.get("/budget/check", dependencies=[Depends(Auth("read"))])
async def budget_check(request: Request, project_key: str, branch: str | None = None):
    gate = getattr(request.app.state, "wt_budget_gate", None)
    try:
        result = await _run(gate.check if gate else _store(request).budget_check,
                            project_key, branch)
    except Exception as exc:
        raise _bad(exc) from exc
    return result


# ---------------------------------------------------------------- jobs
def _preset_names(request: Request) -> set[str]:
    runtime = getattr(request.app.state, "wt_runtime", None)
    if runtime is not None:
        return set(runtime.catalog.presets.keys())
    return {"low", "medium", "high", "orchestrator", "coder"}


def _job_profiles(request: Request) -> dict:
    config_dir = getattr(request.app.state, "wt_config_dir", pathlib.Path.cwd())
    defaults = job_profiles.load_defaults(config_dir)
    overrides = []
    try:
        overrides = _store(request).list_jobs()
    except Exception:
        pass
    return job_profiles.merge(defaults, overrides)


@router.get("/jobs", dependencies=[Depends(Auth("read"))])
async def list_jobs(request: Request):
    return {"jobs": list(_job_profiles(request).values()),
            "defaults": job_profiles.load_defaults(
                getattr(request.app.state, "wt_config_dir", pathlib.Path.cwd())),
            "presets": sorted(_preset_names(request))}


@router.put("/jobs/{name}", dependencies=[Depends(Auth("admin"))])
async def upsert_job(name: str, request: Request):
    payload = await _json(request)
    try:
        clean = job_profiles.validate({**dict(payload), "name": name},
                                      _preset_names(request))
        saved = await _run(_store(request).upsert_job, clean)
    except Exception as exc:
        raise _bad(exc) from exc
    return {"job": saved}


@router.delete("/jobs/{name}", dependencies=[Depends(Auth("admin"))])
async def delete_job(name: str, request: Request):
    if not await _run(_store(request).delete_job, name):
        raise HTTPException(404, "unknown job profile")
    return {"deleted": True}


@router.post("/jobs/preview", dependencies=[Depends(Auth("read"))])
async def preview_job(request: Request):
    payload = await _json(request)
    runtime = getattr(request.app.state, "wt_runtime", None)
    try:
        clean = job_profiles.validate(payload, _preset_names(request))
    except Exception as exc:
        raise _bad(exc) from exc
    if runtime is None:
        raise HTTPException(503, "job preview needs the router catalog (APP_MODE=all)")
    from selector import preview_job as selector_preview
    metrics = await _run(_store(request).observed_metrics)
    result = await _run(selector_preview, runtime.catalog, runtime.ranks,
                        runtime.state, runtime.models, clean, metrics)
    return {"job": clean, **result}


@router.get("/jobs/export", dependencies=[Depends(Auth("read"))])
async def export_jobs(request: Request):
    return job_profiles.export(_job_profiles(request))


@router.post("/jobs/import", dependencies=[Depends(Auth("admin"))])
async def import_jobs(request: Request):
    payload = await _json(request)
    try:
        cleaned = job_profiles.import_profiles(payload, _preset_names(request))
        saved = [await _run(_store(request).upsert_job, item) for item in cleaned]
    except Exception as exc:
        raise _bad(exc) from exc
    return {"imported": len(saved), "jobs": saved}


# ---------------------------------------------------------------- refresh
@router.get("/refresh", dependencies=[Depends(Auth("read"))])
async def refresh_status(request: Request):
    store = _store(request)
    status = await _run(store.refresh_status)
    return {"runs": status.get("runs") or [],
            "last_success": status.get("last_success"),
            "last_error": status.get("last_error"),
            "benchmarks": await _run(benchmarks.load,
                                     getattr(request.app.state, "wt_state_dir", "state"))}


@router.post("/refresh", dependencies=[Depends(Auth("admin"))])
async def refresh_now(request: Request):
    from .refresh_scheduler import refresh_all

    store = _store(request)
    result = await _run(
        refresh_all, getattr(request.app.state, "wt_config_dir", pathlib.Path.cwd()),
        getattr(request.app.state, "wt_state_dir", pathlib.Path("state")), store)
    return result


@router.get("/rejected", dependencies=[Depends(Auth("read"))])
async def rejected(request: Request, limit: int = 100):
    return {"rejected": await _run(_store(request).rejected, _int(limit, 100, 1, 1000))}


# ---------------------------------------------------------------- WakaTime API
def _format_duration(seconds: float) -> str:
    total = int(max(0, seconds))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours} hr{'s' if hours != 1 else ''} {minutes} min{'s' if minutes != 1 else ''}"
    if minutes:
        return f"{minutes} min{'s' if minutes != 1 else ''} {secs} sec{'s' if secs != 1 else ''}"
    return f"{secs} sec{'s' if secs != 1 else ''}"


def _ingest_heartbeats(store: TrackerStore, payload: Any) -> list[list[Any]]:
    rows = payload if isinstance(payload, list) else [payload]
    out: list[list[Any]] = []
    for row in rows:
        if not isinstance(row, Mapping):
            raise ValueError("heartbeat must be an object")
        copied = dict(row)
        if "time" in copied and "ts" not in copied:
            copied["ts"] = copied["time"]
        copied.setdefault("source", "wakatime")
        copied.setdefault("tool", copied.get("user_agent") or "wakatime")
        copied.setdefault("session", f"{copied.get('machine_name') or 'machine'}:"
                                     f"{copied.get('project') or 'project'}")
        hb_id = store.record_heartbeat(copied)
        out.append([hb_id, 201])
    return out


@waka.get("/users/current", dependencies=[Depends(Auth("read"))])
async def waka_user(request: Request):
    now = utcnow().isoformat()
    return {"data": {
        "id": "worktracker", "username": "worktracker",
        "email": "worktracker@localhost", "display_name": "Worktracker",
        "timezone": _settings(request).timezone, "is_email_public": False,
        "plan": "self-hosted", "created_at": now, "modified_at": now,
        "last_heartbeat_at": now, "last_plugin_name": "worktracker",
        "last_project": None,
    }}


# The official wakatime-cli (used by the VS Code extension) posts to
# /users/current/heartbeats[.bulk]; keep both spellings on the same handlers.
@waka.post("/heartbeats", dependencies=[Depends(Auth("editor"))])
@waka.post("/users/current/heartbeats", dependencies=[Depends(Auth("editor"))])
async def waka_heartbeats(request: Request):
    payload = await _json(request)
    try:
        responses = await _run(_ingest_heartbeats, _store(request), payload)
    except Exception as exc:
        raise _bad(exc) from exc
    return {"responses": responses, "data": {"id": responses[0][0] if responses else None}}


@waka.post("/heartbeats.bulk", dependencies=[Depends(Auth("editor"))])
@waka.post("/users/current/heartbeats.bulk", dependencies=[Depends(Auth("editor"))])
async def waka_heartbeats_bulk(request: Request):
    payload = await _json(request)
    if not isinstance(payload, list):
        raise HTTPException(400, "bulk heartbeats must be a JSON array")
    try:
        responses = await _run(_ingest_heartbeats, _store(request), payload)
    except Exception as exc:
        raise _bad(exc) from exc
    return {"responses": responses}


@waka.get("/users/current/status_bar/today", dependencies=[Depends(Auth("read"))])
async def waka_status_bar(request: Request):
    store = _store(request)
    summary = await _run(store.today_summary)
    rows = await _run(store.heartbeats, 1, None, 5000)
    projects: dict[str, float] = {}
    for row in rows:
        projects[row.get("project_key") or "(unassigned)"] = (
            projects.get(row.get("project_key") or "(unassigned)", 0.0)
            + float(row.get("duration_s") or 0))
    seconds = summary["active_s"]
    return {"data": {
        "grand_total": {"text": _format_duration(seconds), "total_seconds": seconds,
                        "hours": int(seconds // 3600), "minutes": int(seconds % 3600 // 60),
                        "digital": f"{int(seconds // 3600)}:{int(seconds % 3600 // 60):02d}"},
        "categories": [{"name": "Coding", "text": _format_duration(seconds),
                        "total_seconds": seconds}],
        "projects": [{"name": name, "text": _format_duration(value),
                      "total_seconds": value} for name, value in
                     sorted(projects.items(), key=lambda item: -item[1])],
        "languages": [], "editors": [], "operating_systems": [], "machines": [],
    }}


@waka.get("/users/current/summaries", dependencies=[Depends(Auth("read"))])
async def waka_summaries(request: Request, start: str | None = None,
                         end: str | None = None):
    store = _store(request)
    try:
        start_date = dt.date.fromisoformat(start or "") if start else \
            dt.datetime.now(_settings(request).tz).date()
        end_date = dt.date.fromisoformat(end or "") if end else start_date
    except ValueError as exc:
        raise HTTPException(400, "start/end must be YYYY-MM-DD") from exc
    span = max(1, (end_date - start_date).days + 1)
    cells = (await _run(store.calendar, span))["cells"]
    wanted = {start_date.isoformat(), end_date.isoformat()}
    data = [{
        "date": cell["date"],
        "grand_total": {"text": _format_duration(cell["active_s"]),
                        "total_seconds": cell["active_s"]},
        "projects": [], "languages": [], "editors": [], "machines": [],
        "categories": [{"name": "Coding", "text": _format_duration(cell["active_s"]),
                        "total_seconds": cell["active_s"]}],
        "range": {"date": cell["date"]},
    } for cell in cells if cell["date"] in wanted or span <= 31]
    return {"data": data}


@waka.get("/users/current/durations", dependencies=[Depends(Auth("read"))])
async def waka_durations(request: Request, date: str | None = None):
    store = _store(request)
    day = dt.date.fromisoformat(date) if date else \
        dt.datetime.now(_settings(request).tz).date()
    start = dt.datetime.combine(day, dt.time.min, tzinfo=_settings(request).tz)
    rows = await _run(store.heartbeats_between, start,
                      start + dt.timedelta(days=1))
    return {"data": [{
        "time": row["ts"], "duration": row["duration_s"],
        "project": row.get("project_key"), "branch": row.get("branch"),
        "category": row.get("category"), "entity": row.get("entity"),
        "type": "file",
    } for row in rows], "start": start.isoformat(), "end": (start + dt.timedelta(days=1)).isoformat()}


@waka.get("/projects", dependencies=[Depends(Auth("read"))])
async def waka_projects(request: Request):
    projects = await _run(_store(request).list_projects, 0)
    return {"data": [{"id": p["key"], "name": p["display_name"]} for p in projects]}
