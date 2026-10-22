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


def test_spend_tokens_timeline_and_daily_history(spend_client):
    for _ in range(2):
        r = run(spend_client.post("/v1/chat/completions",
                                  json={"model": "high", "messages": [{"role": "user", "content": "hi"}]}))
        assert r.status_code == 200
    d = run(spend_client.get("/v1/spend")).json()

    # per-minute buckets carry the token split so the dashboard can chart them
    tl = d["timeline"]
    assert tl, "timeline should have at least one minute bucket"
    assert all(p["in"] > 0 for p in tl)
    assert all(p["tok"] for p in tl)
    assert sum(p["out"] for p in tl) > 0
    assert sum(p["in"] for p in tl) == d["today"]["in"]
    assert sum(p["out"] for p in tl) == d["today"]["out"]

    # per-day token history is kept separately (long-lived, not pruned daily)
    daily = d["tokens_daily"]
    assert daily and len(daily[-1]["date"]) == 10
    assert daily[-1]["in"] == d["today"]["in"]
    assert daily[-1]["out"] == d["today"]["out"]
    assert daily[-1]["reqs"] == d["today"]["reqs"]


def test_daily_history_backfilled_from_usage(tmp_path, stub, monkeypatch):
    import datetime

    from state import State

    monkeypatch.setenv("FAKE_KEY", "k1")
    monkeypatch.setenv("FAKE2_KEY", "k2")
    today = datetime.date.today().isoformat()
    write_catalog(tmp_path, stub.base_url, extra_state={
        "usage": {today: {"fake": {"in": 100, "out": 20, "reqs": 3, "cost": 0.5}}},
    })
    st = State(tmp_path / "state")
    hist = st.data["daily_history"]
    assert today in hist
    assert hist[today]["in"] == 100
    assert hist[today]["out"] == 20
    assert hist[today]["reqs"] == 3


def test_dashboard_html(spend_client):
    r = run(spend_client.get("/dashboard"))
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert "llm-auto-router" in r.text
    assert "/v1/spend" in r.text
    assert 'id="chart"' in r.text and 'id="sessions"' in r.text
    assert 'id="token-chart"' in r.text and 'id="token-daily"' in r.text
    assert "tokens processed" in r.text
    assert "x-oc-session" in r.text


def test_root_redirects_to_dashboard(spend_client):
    r = run(spend_client.get("/", follow_redirects=False))
    assert r.status_code in (302, 307)
    assert r.headers["location"] == "/dashboard"
