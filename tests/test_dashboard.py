import pytest

from tests.conftest import build_client, run, write_catalog


@pytest.fixture
def spend_client(tmp_path, stub, monkeypatch):
    monkeypatch.setenv("FAKE_KEY", "k1")
    monkeypatch.setenv("FAKE2_KEY", "k2")
    write_catalog(tmp_path, stub.base_url)
    c = build_client(tmp_path)
    yield c
    run(c.aclose())


def test_spend_snapshot_aggregates(spend_client):
    r = run(spend_client.post("/v1/chat/completions",
                              json={"model": "high", "messages": [{"role": "user", "content": "hi"}]}))
    assert r.status_code == 200
    d = run(spend_client.get("/v1/spend")).json()

    assert d["today"]["reqs"] == 1
    assert d["today"]["cost"] > 0
    assert d["today"]["in"] > 0
    assert d["presets_today"]["high"]["reqs"] == 1
    assert d["models_today"][r.headers["x-router-model"]]["reqs"] == 1
    assert d["recent"] and d["recent"][0]["chosen"] == r.headers["x-router-model"]
    assert d["recent"][0]["cost_usd"] > 0
    assert d["providers"]["fake"]["today"]["cost"] > 0
    assert d["uptime_s"] >= 0
    assert "discovery" in d and "cooldowns" in d


def test_spend_sessions_and_timeline(spend_client):
    for sid in ("sess-a", "sess-b", "sess-a"):
        r = run(spend_client.post("/v1/chat/completions",
                                  headers={"x-oc-session": sid},
                                  json={"model": "high", "messages": [{"role": "user", "content": "hi"}]}))
        assert r.status_code == 200
    d = run(spend_client.get("/v1/spend")).json()
    by = {s["session"]: s for s in d["sessions"]}
    assert by["sess-a"]["reqs"] == 2
    assert by["sess-b"]["reqs"] == 1
    assert by["sess-a"]["last_model"]
    assert by["sess-a"]["kinds"].get("primary") == 2
    assert d["timeline"] and d["timeline"][0]["reqs"] >= 1
    assert all(len(p["t"]) == 5 and ":" in p["t"] for p in d["timeline"])
    assert all(r.get("session") in ("sess-a", "sess-b") for r in d["recent"])


def test_spend_recent_is_newest_first(spend_client):
    for _ in range(2):
        r = run(spend_client.post("/v1/chat/completions",
                                  json={"model": "high", "messages": [{"role": "user", "content": "hi"}]}))
        assert r.status_code == 200
    d = run(spend_client.get("/v1/spend")).json()
    ts = [rec["ts"] for rec in d["recent"]]
    assert ts == sorted(ts, reverse=True)


def test_dashboard_html(spend_client):
    r = run(spend_client.get("/dashboard"))
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert "llm-auto-router" in r.text
    assert "/v1/spend" in r.text
    assert 'id="chart"' in r.text and 'id="sessions"' in r.text
    assert "x-oc-session" in r.text


def test_root_redirects_to_dashboard(spend_client):
    r = run(spend_client.get("/", follow_redirects=False))
    assert r.status_code in (302, 307)
    assert r.headers["location"] == "/dashboard"
