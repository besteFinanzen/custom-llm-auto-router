"""HTTP contracts for every tracker management, WakaTime and auth endpoint."""
from __future__ import annotations

import base64
import dataclasses
import datetime as dt

import pytest
from fastapi import FastAPI

from worktracker.api import (
    _bad,
    _format_duration,
    _ingest_heartbeats,
    _int,
    router,
    waka,
)
from worktracker.api import (
    page as ui_page,
)
from worktracker.app import create_tracker_app

pytest_plugins = ["tests.test_worktracker_postgres"]
pytestmark = pytest.mark.postgres


@pytest.fixture
def usage():
    from tests.test_worktracker_postgres import _usage
    return _usage("dddddddd-dddd-dddd-dddd-dddddddddddd")


@pytest.fixture
def app(store, tmp_path):
    application = create_tracker_app(store.settings, store, config_dir=tmp_path,
                                    state_dir=tmp_path)
    return application


@pytest.fixture
def api(app):
    from tests.test_worktracker_postgres import Client
    return Client(app)


def no_token_api(store, tmp_path):
    settings = dataclasses.replace(
        store.settings, admin_token="", editor_key="", hook_token="", read_token="")
    return create_tracker_app(settings, store, config_dir=tmp_path, state_dir=tmp_path)


def test_tokens_authentication_and_health(app, api, store, tmp_path):
    from tests.test_worktracker_postgres import Client
    editor = {"authorization": "Bearer editor"}
    read = {"authorization": "Bearer read"}
    hook = {"authorization": "Bearer hook"}
    basic = {"authorization": "Basic " + base64.b64encode(b"admin:ignored").decode()}
    assert api.get("/worktracker/api/whoami", headers=read).json()["scope"] == "read"
    assert api.get("/worktracker/api/whoami", headers=basic).json()["scope"] == "admin"
    assert api.get("/worktracker/api/whoami", headers={"authorization": "Basic garbage"}).status_code == 401
    assert api.get("/worktracker/api/whoami", params={"api_key": "read"}).status_code == 200
    assert api.get("/worktracker/api/whoami", params={"token": "read"}).status_code == 200
    assert api.get("/worktracker/api/whoami", headers={"x-api-key": "read"}).status_code == 200
    assert api.get("/worktracker/api/whoami", headers={"x-api-key": "wrong"}).status_code == 401
    assert api.get("/worktracker/api/whoami").status_code == 401
    assert api.post("/worktracker/api/heartbeats", headers=read, json={}).status_code == 403
    assert api.get("/worktracker/api/projects", headers=hook).status_code == 200
    assert api.get("/worktracker/api/jobs", headers=editor).status_code == 200
    assert api.put("/worktracker/api/jobs/review", headers=editor, json={}).status_code == 403
    assert api.post("/worktracker/api/usage", headers=read, json={}).status_code == 403
    assert api.get("/worktracker/api/config", headers=editor).json()["auth_enabled"] is True
    response = api.get("/healthz")
    assert response.status_code == 200 and response.json()["database"] is True
    assert api.get("/").headers["location"] == "/worktracker"
    assert api.get("/worktracker").status_code == 200
    admin = Client(no_token_api(store, tmp_path))
    assert admin.get("/worktracker/api/whoami").json()["scope"] == "open"
    assert admin.get("/worktracker/api/config").json()["auth_enabled"] is False
    assert str(_bad(ValueError("bad value")).detail) == "bad value"
    assert _bad(KeyError("missing")).status_code == 404


def test_missing_store_and_missing_ui_manifest_errors(monkeypatch):
    application = FastAPI()
    application.include_router(router, prefix="/worktracker/api")
    application.include_router(waka, prefix="/api/v1")
    application.include_router(ui_page)
    from tests.test_worktracker_postgres import Client
    client = Client(application)
    assert client.get("/worktracker/api/projects").status_code == 503
    original = __import__("worktracker.api", fromlist=["UI_PATH"]).UI_PATH
    try:
        __import__("worktracker.api", fromlist=["UI_PATH"]).UI_PATH = original.with_name("missing.html")
        assert client.get("/worktracker").status_code == 404
    finally:
        __import__("worktracker.api", fromlist=["UI_PATH"]).UI_PATH = original


def test_api_utilities_and_wakatime_ingest(app, api, store):
    assert _int("not-a-number", 5, 1, 10) == 5
    assert _int(100, 5, 1, 10) == 10
    assert _int(0, 5, 1, 10) == 1
    assert (_format_duration(0), _format_duration(61), _format_duration(3601)) == (
        "0 secs", "1 min 1 sec", "1 hr 0 mins")
    with pytest.raises(ValueError, match="heartbeat must be an object"):
        _ingest_heartbeats(store, ["bad"])
    headers = {"authorization": "Bearer editor"}
    assert api.post("/worktracker/api/heartbeats", headers=headers, content="not-json").status_code == 400
    hb = {"entity": "src/a.py", "project": "path:/repo/demo", "time": dt.datetime.now(dt.timezone.utc).timestamp(),
          "user_agent": "vscode/1.0", "machine_name": "workstation"}
    assert api.post("/worktracker/api/heartbeats", headers=headers, json=hb).status_code == 200
    assert api.post("/api/v1/heartbeats", headers=headers, json=hb).json()["data"]["id"]
    assert api.post("/api/v1/heartbeats.bulk", headers=headers, json=hb).status_code == 400
    assert api.post("/api/v1/heartbeats.bulk", headers=headers, json=[hb, hb]).status_code == 200
    # wakatime-cli (official VS Code extension) uses the /users/current spelling.
    official = "/api/v1/users/current/heartbeats"
    assert api.post(official, headers=headers, json=hb).json()["data"]["id"]
    assert api.post(official + ".bulk", headers=headers, json=[hb, hb]).status_code == 200
    assert api.post(official + ".bulk", headers=headers, json=hb).status_code == 400
    assert api.get("/api/v1/users/current", headers=headers).json()["data"]["plan"] == "self-hosted"
    assert api.get("/api/v1/users/current/status_bar/today", headers=headers).status_code == 200
    assert api.get("/api/v1/users/current/durations", headers=headers).json()["data"]
    assert api.get("/api/v1/projects", headers=headers).json()["data"]
    malformed = api.get("/api/v1/users/current/summaries?start=garbage", headers=headers)
    assert malformed.status_code == 400
    with pytest.raises(ValueError):
        api.get("/api/v1/users/current/durations?date=garbage", headers=headers)
    date = dt.datetime.now(store.settings.tz).date()
    summaries = api.get("/api/v1/users/current/summaries", headers=headers,
                            params={"start": str(date), "end": str(date)})
    assert summaries.status_code == 200 and len(summaries.json()["data"]) == 1


def test_public_masked_and_legacy_project_routes(api, store):
    h = {"authorization": "Bearer admin"}
    result = api.post("/worktracker/api/projects", headers=h,
                          json={"key": "simple", "root": "/root/simple", "name": "Simple"})
    assert result.status_code == 200
    assert api.post("/worktracker/api/projects", headers=h, content="[]").status_code == 400
    assert api.post("/worktracker/api/projects", headers=h,
                        json={"key": "colored", "display_name": "Color", "color": "#ff0000"}).status_code == 200
    assert api.post("/worktracker/api/projects/merge", headers=h, json={}).status_code == 400
    assert api.get("/worktracker/api/projects?days=5", headers=h).status_code == 200
    assert api.get("/worktracker/api/projects/simple?days=5", headers=h).status_code == 200
    assert api.get("/worktracker/api/projects/simple/aliases", headers=h).status_code == 200
    assert api.post("/worktracker/api/projectaliases", headers=h).status_code == 404
    assert api.post("/worktracker/api/project/aliases", headers=h, json={"key": "simple"}).status_code == 400
    assert api.post("/worktracker/api/project/aliases?key=simple", headers=h,
                        json={"kind": "root", "value": "/tmp/simple-two"}).json()["kind"] == "root"
    assert api.post("/worktracker/api/projects/legacy/aliases", headers=h,
                        json={"kind": "key", "value": "legacy"}).json()["project_key"] == "legacy"
    assert api.patch("/worktracker/api/projects/simple", headers=h,
                         json={"display_name": "S2", "color": "#123456"}).status_code == 200
    assert api.patch("/worktracker/api/project?key=simple", headers=h,
                         json={"display_name": "S3", "color": None}).status_code == 200
    assert api.patch("/worktracker/api/project?key=missing", headers=h,
                         json={"display_name": "Missing"}).status_code in (400, 404, 500)
    assert api.get("/worktracker/api/project", headers=h).status_code == 400
    assert api.get("/worktracker/api/project?key=does-not-exist", headers=h).status_code == 404
    assert api.get("/worktracker/api/project/commits?key=simple&days=0", headers=h).status_code == 200
    assert api.get("/worktracker/api/commit?key=simple&sha=unknown", headers=h).status_code == 404
    assert api.get("/worktracker/api/commits?days=0&project_key=simple", headers=h).status_code == 200


def test_budget_jobs_refresh_and_management_endpoints(api, store, monkeypatch):
    h = {"authorization": "Bearer admin"}
    budget = {"project_key": "path:/repo/demo", "branch": "main", "metric": "cost_usd",
              "limit_usd": 10, "period": "monthly", "warn_pct": 50, "hard_block": True}
    response = api.post("/worktracker/api/budgets", headers=h, json=budget)
    assert response.status_code == 200
    identifier = response.json()["budget"]["id"]
    assert api.get("/worktracker/api/budgets?project_key=path:/repo/demo", headers=h).status_code == 200
    assert api.get("/worktracker/api/budget/check?project_key=path:/repo/demo", headers=h).status_code == 200
    assert api.delete("/worktracker/api/budgets/not-a-uuid", headers=h).status_code in (400, 404, 500)
    assert api.delete(f"/worktracker/api/budgets/{identifier}", headers=h).status_code == 200
    assert api.delete(f"/worktracker/api/budgets/{identifier}", headers=h).status_code == 404
    profile = {"base_preset": "medium", "description": "review",
               "min_scores": {"aa_intelligence": 20}, "min_context": 8192}
    assert api.put("/worktracker/api/jobs/review", headers=h, json=profile).status_code == 200
    assert api.get("/worktracker/api/jobs", headers=h).status_code == 200
    assert api.get("/worktracker/api/jobs/export", headers=h).status_code == 200
    assert api.post("/worktracker/api/jobs/import", headers=h,
                        json={"version": 1, "jobs": {}}).json()["imported"] == 0
    assert api.post("/worktracker/api/jobs/preview", headers=h,
                        json={"name": "review", "min_scores": {"aa": 500}}).status_code == 400
    assert api.post("/worktracker/api/jobs/preview", headers=h,
                        json={"name": "review", "job": profile}).status_code == 503
    exported = api.get("/worktracker/api/jobs/export", headers=h).json()
    assert api.post("/worktracker/api/jobs/import", headers=h,
                        json=exported).status_code == 200
    assert api.delete("/worktracker/api/jobs/missing", headers=h).status_code == 404
    assert api.delete("/worktracker/api/jobs/review", headers=h).json()["deleted"] is True
    assert api.post("/worktracker/api/refresh", headers=h).status_code == 200
    status = api.get("/worktracker/api/refresh", headers=h).json()
    assert isinstance(status["runs"], list) and "benchmarks" in status
    assert api.get("/worktracker/api/rejected?limit=99999", headers=h).status_code == 200
    bad = dict(budget, metric="bogus")
    assert api.post("/worktracker/api/budgets", headers=h, json=bad).status_code == 400
    assert api.put("/worktracker/api/jobs/invalid", headers=h,
                       json={"min_scores": {"aa": "not-a-number"}}).status_code == 400
    class Gate:
        def __init__(self):
            self.invalidated = False

        def invalidate(self):
            self.invalidated = True

        def check(self, project_key, branch):
            return {"blocked_by": []}

    new_app = create_tracker_app(store.settings, store)
    new_app.state.wt_budget_gate = Gate()
    from tests.test_worktracker_postgres import Client
    client = Client(new_app)
    assert client.post("/worktracker/api/budgets", headers=h, json=budget).status_code == 200
    assert new_app.state.wt_budget_gate.invalidated is True
    check = client.get("/worktracker/api/budget/check?project_key=path:/repo/demo", headers=h)
    assert check.status_code == 200 and check.json()["blocked_by"] == []


def test_stats_heatmap_activity_and_sources_routes(api, store, usage):
    h = {"authorization": "Bearer read"}
    for endpoint, params in (
        ("/overview", {"days": 30}), ("/models", {"days": 30}), ("/providers", {"days": 30}),
        ("/stats", {"days": 30}), ("/sessions", {"days": 30}), ("/presence", {}),
        ("/calendar", {"days": 20}), ("/heatmap", {"days": 20}),
        ("/day", {"date": dt.datetime.now(store.settings.tz).date().isoformat()}),
        ("/model", {"model": "fake/model"}),
    ):
        assert api.get("/worktracker/api" + endpoint, headers=h, params=params).status_code == 200, endpoint
    assert api.get("/worktracker/api/model?model=%20", headers=h).status_code == 400
    assert api.get("/worktracker/api/day?date=bad", headers=h).status_code == 400
    assert api.get("/worktracker/api/stats?project_key=path:/repo/demo", headers=h).status_code == 200


def test_presence_snapshot_tools_usage_and_rejected_routes(api, store, usage):
    h = {"authorization": "Bearer hook"}
    p = {"project": "path:/repo/demo", "session": "http-one", "device": "laptop",
         "tool": "opencode", "status": "active"}
    assert api.post("/worktracker/api/presence", headers=h, json=p).status_code == 200
    snapshot = {"project": p["project"], "session": "s", "root": "/repo/demo",
                "git_head": "a" * 40, "branch": "main"}
    result = api.post("/worktracker/api/snapshots", headers=h, json=snapshot)
    assert result.status_code == 200
    assert api.post("/worktracker/api/tool-events", headers=h,
                        json={"project": p["project"], "session": "s", "tool": "read",
                              "files": ["a.py"]}).status_code == 200
    assert api.post("/worktracker/api/commits", headers=h, json=[]).status_code == 400
    assert api.post("/worktracker/api/commits", headers=h,
                        json={"sha": "a" * 40, "project": p["project"],
                              "files": [{"path": "a.py", "additions": 2}]}).status_code == 200
    assert api.post("/worktracker/api/usage", headers=h, json=usage).json()["accepted"] == 1
    assert api.post("/worktracker/api/usage", headers=h,
                        json=[usage, "not-mapping", {"id": usage["id"]}]).json()["rejected"]
    gate = type("Gate", (), {"invalidated": False,
                             "invalidate": lambda s: setattr(s, "invalidated", True)})()
    store_app = api._transport.app
    store_app.state.wt_budget_gate = gate
    assert api.post("/worktracker/api/commits", headers=h,
                        json={"sha": "b" * 40, "project": p["project"]}).status_code == 200
    assert gate.invalidated is True
    assert api.post("/worktracker/api/snapshots", headers=h, json=[]).status_code == 400
    assert api.post("/worktracker/api/presence", headers=h, json=[]).status_code == 400
    assert api.post("/worktracker/api/tool-events", headers=h, json=[]).status_code == 400


def test_live_event_stream_disconnects_cleanly(api, store, monkeypatch):
    import httpx

    from tests.conftest import run
    from worktracker import api as api_module
    monkeypatch.setattr(api_module, "EVENT_POLL_SECONDS", 0)
    monkeypatch.setattr(api_module, "EVENT_MAX_CHUNKS", 3)

    async def once():
        events = []
        client = httpx.AsyncClient(transport=api._transport, base_url="http://wb.test",
                                   headers={"authorization": "Bearer read"})
        async with client, client.stream("GET", "/worktracker/api/events") as response:
            assert response.status_code == 200
            async for line in response.aiter_lines():
                if line.startswith("event: presence"):
                    events.append(line)
        return events

    events = run(once())
    assert events and events[0] == "event: presence"


def test_api_keys_create_list_revoke(api, store):
    h = {"authorization": "Bearer admin"}
    # create a router key
    result = api.post("/worktracker/api/keys", headers=h,
                      json={"name": "OpenCode laptop", "scope": "router"})
    assert result.status_code == 200, result.text
    body = result.json()
    assert "key" in body and "prefix" in body and "id" in body
    assert body["key"].startswith("wt_")
    assert body["scope"] == "router"
    # list keys
    listed = api.get("/worktracker/api/keys", headers=h)
    assert listed.status_code == 200
    keys = listed.json()["keys"]
    assert len(keys) == 1
    assert keys[0]["name"] == "OpenCode laptop"
    assert keys[0]["scope"] == "router"
    assert keys[0]["prefix"] == body["prefix"]
    assert "key_hash" not in keys[0]  # hash never exposed
    # create a read key with expiry
    result2 = api.post("/worktracker/api/keys", headers=h,
                       json={"name": "Read-only", "scope": "read", "expires_in_days": 90})
    assert result2.status_code == 200
    listed2 = api.get("/worktracker/api/keys", headers=h)
    assert len(listed2.json()["keys"]) == 2
    # revoke the first key
    revoked = api.delete(f"/worktracker/api/keys/{body['id']}", headers=h)
    assert revoked.status_code == 200
    listed3 = api.get("/worktracker/api/keys", headers=h)
    remaining = listed3.json()["keys"]
    assert len(remaining) == 2  # revoked key still listed, marked revoked
    revoked_key = next(k for k in remaining if k["id"] == body["id"])
    assert revoked_key["revoked_at"] is not None
    active_key = next(k for k in remaining if k["id"] != body["id"])
    assert active_key["name"] == "Read-only"
    assert active_key["revoked_at"] is None
    # revoke again -> 404
    assert api.delete(f"/worktracker/api/keys/{body['id']}", headers=h).status_code == 404


def test_api_keys_scope_enforcement(api, store):
    admin = {"authorization": "Bearer admin"}
    editor = {"authorization": "Bearer editor"}
    read = {"authorization": "Bearer read"}
    hook = {"authorization": "Bearer hook"}
    # non-admin scopes cannot manage keys
    for headers in (editor, read, hook):
        assert api.get("/worktracker/api/keys", headers=headers).status_code == 403
        assert api.post("/worktracker/api/keys", headers=headers,
                        json={"name": "x", "scope": "read"}).status_code == 403
        assert api.delete("/worktracker/api/keys/00000000-0000-0000-0000-000000000000",
                          headers=headers).status_code == 403
    # admin can create keys of any scope
    for scope in ("router", "read", "editor", "hook", "admin"):
        result = api.post("/worktracker/api/keys", headers=admin,
                          json={"name": scope + " key", "scope": scope})
        assert result.status_code == 200, f"scope {scope}: {result.text}"


def test_api_keys_invalid_scope(api, store):
    h = {"authorization": "Bearer admin"}
    assert api.post("/worktracker/api/keys", headers=h,
                    json={"name": "bad", "scope": "superuser"}).status_code == 400
    assert api.post("/worktracker/api/keys", headers=h,
                    json={"name": "no-scope"}).status_code == 400


def test_api_keys_open_mode_rejected(store, tmp_path):
    from tests.test_worktracker_postgres import Client
    settings = dataclasses.replace(
        store.settings,
        admin_token="", editor_key="", hook_token="", read_token="")
    app = create_tracker_app(settings, store, config_dir=tmp_path, state_dir=tmp_path)
    client = Client(app)
    # no token configured -> key management refused with 409
    assert client.get("/worktracker/api/keys").status_code == 409
    assert client.post("/worktracker/api/keys",
                       json={"name": "x", "scope": "read"}).status_code == 409
    assert client.delete("/worktracker/api/keys/00000000-0000-0000-0000-000000000000").status_code == 409
