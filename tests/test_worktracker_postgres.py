"""Real PostgreSQL integration tests for the Worktracker store and HTTP API.

Set ``TEST_ROUTER_DATABASE_URL`` to a disposable database to run these tests;
without it they skip, keeping the default suite hermetic. Every test truncates
the tracker tables first, so point it at a scratch database only.
"""
from __future__ import annotations

import datetime as dt

import httpx
import pytest

from tests.conftest import run
from worktracker.app import create_tracker_app
from worktracker.config import Settings
from worktracker.store import TrackerStore

PROJECT = "path:/repo/demo"
PARENT = "p" * 40
COMMIT = "c" * 40


@pytest.fixture
def postgres_url():
    import os

    url = os.environ.get("TEST_ROUTER_DATABASE_URL")
    if not url:
        pytest.skip("set TEST_ROUTER_DATABASE_URL to a disposable PostgreSQL database")
    yield url


def _settings(url: str) -> Settings:
    return Settings(
        database_url=url, timezone="Europe/Berlin", admin_token="admin",
        editor_key="editor", hook_token="hook", read_token="read",
        redact_paths=False, presence_ttl_s=300)


@pytest.fixture
def store(postgres_url):
    db = TrackerStore(postgres_url, _settings(postgres_url))
    db.clear()
    yield db
    db.clear()
    db.close()


def _usage(event_id: str, **fields) -> dict:
    base = {
        "id": event_id, "ts": dt.datetime.now(dt.timezone.utc).isoformat(),
        "session": "sess-1", "project_id": PROJECT, "project_root": "/repo/demo",
        "branch": "main", "device": "laptop", "tool": "opencode", "kind": "primary",
        "provider": "fake", "model": "fake/model", "tokens_in": 100, "tokens_out": 50,
        "cost": 1.0, "latency_ms": 1000, "ttft_ms": 200, "outcome": "success",
    }
    base.update(fields)
    return base


def test_usage_is_immutable_and_statistics_keep_historical_cost(store):
    # Anchor events to the tracker's local "today": a UTC timestamp one hour ago
    # can already belong to yesterday in Europe/Berlin, which would make the
    # days=1 calendar-day scope (and today_summary) legitimately exclude it.
    today = dt.datetime.now(store.settings.tz).date()
    ts = dt.datetime.combine(today, dt.time.min, tzinfo=store.settings.tz)
    store.record_usage_event(_usage("11111111-1111-1111-1111-111111111111",
                                    ts=ts.isoformat(), cost=5.0, latency_ms=1000,
                                    ttft_ms=200))
    # Replaying the same event id must not rewrite the price or the outcome.
    store.record_usage_event(_usage("11111111-1111-1111-1111-111111111111",
                                    ts=ts.isoformat(), cost=99.0, latency_ms=99999,
                                    outcome="failure"))
    store.record_usage_event(_usage("22222222-2222-2222-2222-222222222222",
                                    ts=ts.isoformat(), cost=1.0, latency_ms=3000,
                                    ttft_ms=600,
                                    outcome="failure", tokens_in=20, tokens_out=10))

    detail = store.model_detail("fake/model", days=30)
    assert detail["totals"]["requests"] == 2
    assert detail["totals"]["tokens"] == 180
    assert detail["totals"]["cost"] == pytest.approx(6.0)
    assert detail["totals"]["latency_p50_ms"] == pytest.approx(2000.0)
    assert detail["totals"]["latency_p95_ms"] == pytest.approx(2900.0)
    assert detail["totals"]["ttft_p50_ms"] == pytest.approx(400.0)
    assert detail["totals"]["success_rate"] == pytest.approx(50.0)

    per_provider = store.provider_models(days=30)
    assert per_provider["total_cost"] == pytest.approx(6.0)
    assert per_provider["providers"][0]["provider"] == "fake"
    assert per_provider["providers"][0]["models"][0]["share"] == pytest.approx(100.0)

    for latency, success in ((800, True), (1200, False), (1000, True)):
        store.record_model_stat("fake", "fake/model", success, latency_ms=latency,
                                ttft_ms=latency // 2)
    observed = store.observed_metrics()["fake/model"]
    assert observed["rt_requests"] == 3
    assert observed["rt_success_rate"] == pytest.approx(66.67, abs=0.01)
    assert observed["rt_failure_rate"] == pytest.approx(33.33, abs=0.01)
    assert observed["rt_latency_p50_ms"] == pytest.approx(1000.0)
    assert store.provider_models(days=30)["providers"][0]["models"][0]["observed"]

    sessions = store.sessions(days=1, project_key=PROJECT)
    assert sessions[0]["session_id"] == "sess-1"
    assert sessions[0]["cost"] == pytest.approx(6.0)
    assert {row["name"] for row in store.breakdown("device", days=1)} == {"laptop"}
    assert store.today_summary()["requests"] == 2


def test_commit_attribution_evidence_and_recompute(store):
    now = dt.datetime.now(dt.timezone.utc)
    store.record_git_snapshot({
        "ts": (now - dt.timedelta(minutes=50)).isoformat(), "project": PROJECT,
        "repo_root": "/repo/demo", "branch": "main", "head": PARENT, "dirty": True,
        "session": "sess-1", "device": "laptop", "tool": "opencode",
        "files": [{"path": "a.py"}], "metadata": {"prompt": "never stored"},
    })
    store.record_tool_event({
        "ts": (now - dt.timedelta(minutes=40)).isoformat(), "project": PROJECT,
        "branch": "main", "session": "sess-1", "device": "laptop",
        "tool_name": "edit", "files": [{"path": "a.py"}],
    })
    store.record_usage_event(_usage(
        "33333333-3333-3333-3333-333333333333",
        ts=(now - dt.timedelta(minutes=30)).isoformat(), cost=0.5, latency_ms=1200))

    commit = store.record_commit({
        "project": PROJECT, "sha": COMMIT, "branch": "main", "parents": [PARENT],
        "committed_at": now.isoformat(), "subject": "add feature", "source": "hook",
        "files": [{"path": "a.py", "additions": 10, "deletions": 2}],
    })
    assert commit["attributed_events"] == 1

    listed = store.commits(PROJECT, days=0)
    assert len(listed) == 1
    assert listed[0]["ai"] == {"requests": 1, "tokens": 150, "cost": pytest.approx(0.5)}

    detail = store.commit_detail(PROJECT, COMMIT)
    assert detail["usage_count"] == 1
    row = detail["usage"][0]
    assert set(row["attribution"].split("+")) >= {
        "time_window", "snapshot_parent", "snapshot_files", "tool_files", "branch_match"}
    assert row["confidence"] == pytest.approx(1.0)
    assert row["evidence"]["snapshot_head"] == PARENT
    assert row["evidence"]["tool"] == "edit"
    assert row["evidence"]["tool_file_overlap"] == ["a.py"]
    assert detail["files"][0]["path"] == "a.py"

    assert store.recompute_commit(PROJECT, COMMIT) == {"ok": True, "attributed_events": 1}
    assert store.commit_detail(PROJECT, COMMIT)["usage_count"] == 1
    with pytest.raises(KeyError):
        store.recompute_commit(PROJECT, "0" * 40)

    assert store.uncommitted(PROJECT)["branches"][0]["requests"] == 0
    store.record_usage_event(_usage(
        "44444444-4444-4444-4444-444444444444",
        ts=(now + dt.timedelta(minutes=5)).isoformat(), cost=2.0))
    uncommitted = store.uncommitted(PROJECT)["branches"][0]
    assert uncommitted["requests"] == 1 and uncommitted["cost"] == pytest.approx(2.0)
    assert store.project_files(PROJECT) [0]["path"] == "a.py"


def test_budget_periods_warn_block_and_branch_scope(store):
    store.resolve_project(PROJECT)
    store.record_usage_event(_usage("55555555-5555-5555-5555-555555555555",
                                    branch="main", cost=60.0))
    store.record_usage_event(_usage("66666666-6666-6666-6666-666666666666",
                                    branch="dev", cost=50.0))

    main_budget = store.upsert_budget({
        "project_key": PROJECT, "branch": "main", "metric": "cost",
        "period": "monthly", "limit": 70, "warn_pct": 80, "hard_block": True})
    store.upsert_budget({"project_key": PROJECT, "branch": "dev", "metric": "cost",
                         "period": "monthly", "limit": 40, "warn_pct": 60,
                         "hard_block": True})

    main = store.budget_check(PROJECT, "main")
    assert main["blocked"] is False
    assert [item["id"] for item in main["warnings"]] == [main_budget["id"]]
    dev = store.budget_check(PROJECT, "dev")
    assert dev["blocked"] is True
    assert dev["blocked_by"][0]["metric"] == "cost"
    assert dev["blocked_by"][0]["used"] == pytest.approx(50.0)
    assert store.budget_check(PROJECT, None)["budgets"] == []

    with pytest.raises(ValueError, match="period"):
        store.upsert_budget({"project_key": PROJECT, "period": "hourly", "limit": 1})
    assert store.delete_budget(main_budget["id"]) is True
    assert store.delete_budget(main_budget["id"]) is False


def test_heartbeats_presence_jobs_and_time_breakdown(store):
    now = dt.datetime.now(dt.timezone.utc)
    store.record_heartbeat({
        "ts": now.isoformat(), "project": PROJECT, "session": "sess-1",
        "machine_name": "laptop", "tool": "vscode", "duration_s": 3600,
        "entity": "a.py", "is_write": True,
        "metadata": {"prompt": "never stored", "safe": "kept"},
    })
    store.record_usage_event(_usage("77777777-7777-7777-7777-777777777777",
                                    ts=(now + dt.timedelta(minutes=10)).isoformat(),
                                    latency_ms=600000, cost=3.0))

    heat = store.heatmap(days=1, project_key=PROJECT)
    assert len(heat["cells"]) == 168
    assert sum(cell["human_s"] for cell in heat["cells"]) == pytest.approx(3600.0)
    assert sum(cell["ai_s"] for cell in heat["cells"]) == pytest.approx(600.0)
    assert heat["max_active_s"] > 0
    assert store.calendar(days=1, project_key=PROJECT)["cells"][-1]["active_s"] == 3600
    assert len(store.day_cells(now.date().isoformat())["cells"]) == 144

    presence = store.presence()
    assert presence[0]["session_id"] == "sess-1"
    assert presence[0]["today"]["cost"] == pytest.approx(3.0)
    assert presence[0]["project_key"] == PROJECT
    assert "prompt" not in store.heartbeats(days=1)[0]["metadata"]

    time = store.time_breakdown(days=1)
    assert time["human_total_s"] == pytest.approx(3600.0)
    assert time["ai_total_s"] == pytest.approx(600.0)

    saved = store.upsert_job({"name": "review", "base_preset": "high",
                              "max_cost": 2.5, "min_success_rate": 0.9,
                              "weights": {"aa_intelligence": 2}})
    assert saved["max_cost"] == pytest.approx(2.5)
    assert store.get_job("review")["min_success_rate"] == pytest.approx(0.9)
    assert [job["name"] for job in store.list_jobs()] == ["review"]
    assert store.delete_job("review") is True
    assert store.delete_job("review") is False

    run_id = store.start_refresh("benchmarks")
    store.finish_refresh(run_id, "ok", {"models": 3})
    assert store.refresh_status()["last_success"]["counts"] == {"models": 3}
    store.record_rejected({"session": "sess-1", "project_key": PROJECT,
                           "missing": ["device"], "path": "/v1/chat/completions"})
    assert store.rejected()[0]["missing"] == ["device"]


@pytest.fixture
def api(postgres_url, tmp_path):
    settings = _settings(postgres_url)
    db = TrackerStore(postgres_url, settings)
    db.clear()
    app = create_tracker_app(settings, db, config_dir=tmp_path, state_dir=tmp_path)
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                               base_url="http://tracker")
    yield db, client
    run(client.aclose())
    db.clear()
    db.close()


def test_slash_safe_project_and_commit_routes_over_http(api):
    store, client = api
    headers = {"authorization": "Bearer admin"}
    now = dt.datetime.now(dt.timezone.utc)
    store.record_usage_event(_usage("88888888-8888-8888-8888-888888888888",
                                    project_id=PROJECT, cost=4.0))
    store.record_commit({"project": PROJECT, "sha": COMMIT, "branch": "main",
                         "committed_at": now.isoformat(), "subject": "work",
                         "files": [{"path": "a.py"}]})

    assert run(client.get("/worktracker/api/projects",
                          headers=headers)).status_code == 200
    detail = run(client.get("/worktracker/api/project",
                            params={"key": PROJECT}, headers=headers))
    assert detail.status_code == 200
    assert detail.json()["project"]["key"] == PROJECT
    assert detail.json()["uncommitted"]["branches"][0]["requests"] == 1

    commits = run(client.get("/worktracker/api/project/commits",
                             params={"key": PROJECT}, headers=headers))
    assert commits.status_code == 200 and len(commits.json()["commits"]) == 1
    commit = run(client.get("/worktracker/api/commit",
                            params={"key": PROJECT, "sha": COMMIT}, headers=headers))
    assert commit.status_code == 200 and commit.json()["sha"] == COMMIT
    missing = run(client.get("/worktracker/api/commit",
                             params={"key": PROJECT, "sha": "0" * 40}, headers=headers))
    assert missing.status_code == 404
    recompute = run(client.post("/worktracker/api/commit/recompute",
                                params={"key": PROJECT, "sha": COMMIT}, headers=headers))
    assert recompute.status_code == 200 and recompute.json()["ok"] is True

    aliases = run(client.get("/worktracker/api/project/aliases",
                             params={"key": PROJECT}, headers=headers))
    assert aliases.status_code == 200
    renamed = run(client.patch("/worktracker/api/project", params={"key": PROJECT},
                               json={"display_name": "Demo"}, headers=headers))
    assert renamed.status_code == 200
    assert renamed.json()["project"]["display_name"] == "Demo"

    assert run(client.get("/worktracker/api/overview?days=7",
                          headers=headers)).status_code == 200
    assert run(client.get("/worktracker/api/stats?days=7",
                          headers=headers)).status_code == 200
    assert run(client.get("/worktracker/api/providers?days=7",
                          headers=headers)).status_code == 200
    assert run(client.get("/worktracker/api/heatmap?days=7",
                          headers=headers)).status_code == 200
    assert run(client.get("/worktracker/api/refresh",
                          headers=headers)).status_code == 200
    assert run(client.get("/worktracker/api/config",
                          headers=headers)).status_code == 200

    assert run(client.get("/worktracker/api/project")).status_code == 401
    assert run(client.get("/worktracker/api/project",
                          params={"key": PROJECT})).status_code == 401


def test_wakatime_and_ingestion_endpoints_over_http(api):
    store, client = api
    headers = {"authorization": "Bearer editor"}
    heartbeat = {
        "time": dt.datetime.now(dt.timezone.utc).timestamp(), "project": PROJECT,
        "entity": "a.py", "is_write": True, "machine_name": "laptop",
        "user_agent": "wakatime/1.0", "duration_s": 300,
    }
    single = run(client.post("/api/v1/heartbeats", json=heartbeat, headers=headers))
    assert single.status_code == 200 and single.json()["data"]["id"]
    bulk = run(client.post("/api/v1/heartbeats.bulk", json=[heartbeat], headers=headers))
    assert bulk.status_code == 200 and len(bulk.json()["responses"]) == 1
    assert run(client.get("/api/v1/users/current", headers=headers)).status_code == 200
    bar = run(client.get("/api/v1/users/current/status_bar/today", headers=headers))
    assert bar.status_code == 200 and bar.json()["data"]["grand_total"]["total_seconds"] == 300
    summary = run(client.get("/api/v1/users/current/summaries",
                             params={"start": dt.date.today().isoformat()},
                             headers=headers))
    assert summary.status_code == 200

    hook = {"authorization": "Bearer hook"}
    snapshot = run(client.post("/worktracker/api/snapshots", json={
        "ts": dt.datetime.now(dt.timezone.utc).isoformat(), "project": PROJECT,
        "branch": "main", "head": PARENT, "files": [{"path": "a.py"}]}, headers=hook))
    assert snapshot.status_code == 200 and snapshot.json()["id"]
    tool = run(client.post("/worktracker/api/tool-events", json={
        "project": PROJECT, "branch": "main", "tool_name": "edit",
        "files": [{"path": "a.py"}]}, headers=hook))
    assert tool.status_code == 200
    commit = run(client.post("/worktracker/api/commits", json={
        "project": PROJECT, "sha": COMMIT, "branch": "main",
        "committed_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "files": [{"path": "a.py"}]}, headers=hook))
    assert commit.status_code == 200
    usage = run(client.post("/worktracker/api/usage", json=_usage(
        "99999999-9999-9999-9999-999999999999", cost=0.25), headers=hook))
    assert usage.status_code == 200 and usage.json()["accepted"] == 1
    assert run(client.post("/worktracker/api/usage", json=[{"id": {"nested": True}},
                                                           _usage("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")],
                           headers=hook)).json()["rejected"]
    assert store.status()["usage_events"] >= 2
    assert run(client.get("/healthz")).json()["database"] is True


class Client:
    """Small synchronous request adapter backed by the app's ASGI transport."""

    def __init__(self, app):
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                        base_url="http://tracker")

    @property
    def _transport(self):
        return self.client._transport

    def get(self, path, **kwargs):
        return run(self.client.get(path, **kwargs))

    def post(self, path, **kwargs):
        return run(self.client.post(path, **kwargs))

    def patch(self, path, **kwargs):
        return run(self.client.patch(path, **kwargs))

    def put(self, path, **kwargs):
        return run(self.client.put(path, **kwargs))

    def delete(self, path, **kwargs):
        return run(self.client.delete(path, **kwargs))


def test_api_keys_store_create_list_resolve_revoke(store):
    key, key_id = store.create_api_key("test key", "router", created_by="test")
    assert key.startswith("wt_")
    # prefix is the 8 hex chars after "wt_"; secret may contain underscores
    prefix = key.split("_", 2)[1]
    assert len(prefix) == 8
    # list does not expose hash or secret
    listed = store.list_api_keys()
    assert len(listed) == 1
    assert str(listed[0]["id"]) == key_id
    assert listed[0]["name"] == "test key"
    assert listed[0]["scope"] == "router"
    assert listed[0]["prefix"] == prefix
    assert "key_hash" not in listed[0]
    # resolve by full key
    resolved = store.resolve_api_key(key)
    assert resolved is not None
    assert str(resolved["id"]) == key_id
    assert resolved["scope"] == "router"
    # resolve by wrong key -> None
    assert store.resolve_api_key("wt_" + "0" * 16 + "_wrong") is None
    # non-wt_ tokens -> None
    assert store.resolve_api_key("admin") is None
    # revoke
    assert store.revoke_api_key(key_id) is True
    assert store.resolve_api_key(key) is None  # revoked key no longer resolves
    assert store.revoke_api_key(key_id) is False  # already revoked


def test_api_keys_store_expiry_and_touch(store):
    import datetime as dt
    key, key_id = store.create_api_key("expiring", "read", created_by="test",
                                       expires_at=dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=1))
    # resolves before expiry
    assert store.resolve_api_key(key) is not None
    # touch updates last_used_at (throttled: second call within 60s is a no-op)
    store.touch_api_key(key_id)
    second = store.list_api_keys()[0]["last_used_at"]
    assert second is not None
    # expired key (manually set far past)
    store._conn.execute(
        "UPDATE tracker_api_keys SET expires_at = now() - interval '1 day' WHERE id = %s",
        (key_id,))
    assert store.resolve_api_key(key) is None
