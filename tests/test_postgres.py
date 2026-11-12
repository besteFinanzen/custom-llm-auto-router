"""Integration tests for the real PostgreSQL usage/history store.

Set TEST_ROUTER_DATABASE_URL to a disposable database to run these tests. The
rest of the test suite deliberately leaves tracking disabled so it is hermetic.
"""
from __future__ import annotations

import datetime as dt
import os

import pytest

from tests.conftest import build_client, run, write_catalog
from tracker import PostgresUsage

TODAY = dt.date.today().isoformat()
YESTERDAY = (dt.date.today() - dt.timedelta(days=1)).isoformat()
LEGACY = {
    "totals": {
        "fake": {"in": 100, "out": 20, "reqs": 3, "cost": 0.5},
        "fake2": {"in": 10, "out": 5, "reqs": 1, "cost": 0.1},
    },
    "by_preset": {"high": {"reqs": 3, "cost": 0.5}},
    "by_model": {"fake/strong": {"reqs": 3, "cost": 0.5}},
    "daily_history": {
        TODAY: {"in": 100, "out": 20, "reqs": 3, "cost": 0.5},
        YESTERDAY: {"in": 10, "out": 5, "reqs": 1, "cost": 0.1},
    },
    "usage": {
        TODAY: {"fake": {"in": 100, "out": 20, "reqs": 3, "cost": 0.5}},
        YESTERDAY: {"fake2": {"in": 10, "out": 5, "reqs": 1, "cost": 0.1}},
    },
}


@pytest.fixture
def postgres_url():
    url = os.environ.get("TEST_ROUTER_DATABASE_URL")
    if not url:
        pytest.skip("set TEST_ROUTER_DATABASE_URL to a disposable PostgreSQL database")
    yield url


@pytest.fixture
def store(postgres_url):
    db = PostgresUsage(postgres_url)
    db.clear()
    yield db
    db.clear()
    db.close()


def test_migration_preserves_legacy_and_is_idempotent(store):
    assert store.migrate_state(LEGACY) is True
    assert store.migrate_state(LEGACY) is False
    data = store.history()
    assert data["all_time"] == {
        "in": 110, "out": 25, "tokens": 135, "reqs": 4, "cost": pytest.approx(0.6)
    }
    assert data["providers"][0]["key"] == "fake"
    preset = next(item for item in data["presets"] if item["key"] == "high")
    assert preset["reqs"] == 3
    assert preset["cost"] == pytest.approx(0.5)
    assert len([day for day in data["daily"] if day["reqs"]]) == 2
    assert data["migrated"] is True


def test_events_projects_ranges_and_deduplication(store):
    event = {
        "id": "00000000-0000-0000-0000-000000000001",
        "ts": dt.datetime.now(dt.timezone.utc).isoformat(),
        "session": "sess-a",
        "project_id": "project-a",
        "project_root": "/home/user/project-a",
        "worktree": "/home/user/project-a",
        "kind": "primary",
        "requested_preset": "high",
        "preset": "high",
        "provider": "fake",
        "model": "fake/strong",
        "in": 1000,
        "out": 250,
        "cost": 0.0125,
        "estimated": False,
        "latency_ms": 1500,
        "stream": False,
    }
    store.record_usage(event)
    store.record_usage(event)
    data = store.history(days=7, project="project-a")
    assert data["range"]["reqs"] == 1
    assert data["range"]["tokens"] == 1250
    assert data["range"]["cost"] == pytest.approx(0.0125)
    assert data["all_time"]["reqs"] == 1
    assert data["daily"][-1]["reqs"] == 1
    assert data["projects"] == [{
        "id": "project-a", "root": "/home/user/project-a", "in": 1000,
        "out": 250, "tokens": 1250, "reqs": 1, "cost": 0.0125,
        "last_ts": data["projects"][0]["last_ts"],
    }]
    assert data["presets"][0]["key"] == "high"
    with pytest.raises(ValueError):
        store.history(metric="invalid")


def test_work_events_support_human_and_ai_time(store):
    for actor, seconds in (("human", 120), ("ai", 75), ("ai", 15)):
        store.record_work_event({
            "project_id": "project-a", "project_root": "/project-a",
            "source": "test", "actor": actor, "kind": "coding",
            "category": "implementation", "duration_s": seconds,
            "metadata": {"tool": "write", "prompt": "must not persist"},
        })
    data = store.history(project="project-a")
    assert data["work"]["human"]["seconds"] == 120
    assert data["work"]["ai"] == {"seconds": 90, "events": 2}
    with store._conn.transaction():
        details = store._conn.execute("SELECT metadata FROM work_events LIMIT 1").fetchone()[0]
    assert "prompt" not in details
    with pytest.raises(ValueError):
        store.record_work_event({"actor": "robot"})


def test_unassigned_project_and_selected_breakdown(store):
    store.record_usage({
        "id": "00000000-0000-0000-0000-000000000009", "ts": dt.datetime.now(dt.timezone.utc).isoformat(),
        "kind": "primary", "preset": "low", "provider": "fake", "model": "fake/strong",
        "in": 10, "out": 5, "cost": 0.001, "estimated": False,
    })
    history = store.history(days=7, project="(unassigned)")
    assert history["range"]["reqs"] == 1
    assert history["daily"][-1]["reqs"] == 1
    assert history["projects"] == [{
        "id": "(unassigned)", "root": None, "in": 10, "out": 5, "tokens": 15,
        "reqs": 1, "cost": pytest.approx(0.001), "last_ts": history["projects"][0]["last_ts"],
    }]


def test_gateway_usage_history_and_work_api(tmp_path, stub, monkeypatch, postgres_url):
    monkeypatch.setenv("DATABASE_URL", postgres_url)
    monkeypatch.delenv("ROUTER_DATABASE_URL", raising=False)
    monkeypatch.setenv("FAKE_KEY", "k1")
    monkeypatch.setenv("FAKE2_KEY", "k2")
    write_catalog(tmp_path, stub.base_url, extra_state=LEGACY)
    # Isolate the gateway test from other module-level store instances.
    db = PostgresUsage(postgres_url)
    db.clear()
    db.close()
    client = build_client(tmp_path)
    try:
        response = run(client.post("/v1/chat/completions", headers={
            "x-oc-session": "sess-a",
            "x-oc-project": "project-a",
            "x-oc-project-root": "/home/user/project-a",
            "x-oc-worktree": "/home/user/worktree-a",
            "x-oc-kind": "primary",
        }, json={"model": "high", "messages": [{"role": "user", "content": "hi"}]}))
        assert response.status_code == 200

        spend = run(client.get("/v1/spend")).json()
        assert spend["all_time"]["reqs"] == 5
        totals = [provider["total"] for provider in spend["providers"].values()]
        assert spend["all_time"]["in"] == sum(row["in"] for row in totals)
        assert spend["all_time"]["out"] == sum(row["out"] for row in totals)
        assert spend["all_time"]["cost"] == pytest.approx(sum(row["cost"] for row in totals))

        history = run(client.get("/v1/history", params={
            "days": 7, "metric": "tokens", "project": "project-a",
        })).json()
        assert history["project"] == "project-a"
        assert history["range"] == {
            "in": 11, "out": 7, "tokens": 18,
            "reqs": 1, "cost": pytest.approx(0.000072),
        }
        row = history["providers"][0]
        assert row["key"] == "fake"
        assert history["days"] == 7
        assert history["metric"] == "tokens"
        assert len(history["daily"]) == 7
        assert run(client.get("/v1/history", params={"metric": "nope"})).status_code == 400

        result = run(client.post("/v1/work/events", json={
            "project_id": "project-a", "project_root": "/home/user/project-a",
            "source": "opencode", "actor": "human", "kind": "coding",
            "category": "editing", "duration_s": 120,
            "metadata": {"tool": "edit", "prompt": "private"},
        }))
        assert result.status_code == 200
        history = run(client.get("/v1/history", params={
            "days": 30, "project": "project-a",
        })).json()
        assert history["work"]["human"] == {"seconds": 120, "events": 1}
        assert run(client.get("/dashboard/history")).status_code == 200
    finally:
        run(client.aclose())
        db = PostgresUsage(postgres_url)
        db.clear()
        db.close()
