"""Approximate preset costs and actual per-request cost accounting."""
from __future__ import annotations

import datetime
import json

from tests.conftest import CATALOG, build_client, run, write_catalog
from tests.test_gateway import BODY, stream


def test_models_expose_approximate_cost(client):
    r = run(client.get("/v1/models"))
    assert r.status_code == 200
    by_id = {m["id"]: m for m in r.json()["data"]}
    high = by_id["high"]["router"]
    assert high["top_model"] == "fake/strong"
    assert high["approx_cost_per_1m"] == 4.0
    assert high["max_cost"] == 10.0
    low = by_id["low"]["router"]
    assert low["top_model"] == "fake2/cheap"
    assert low["approx_cost_per_1m"] == 0.0


def test_actual_cost_logged_and_aggregated(client, tmp_path):
    r = run(client.post("/v1/chat/completions", json=BODY))
    assert r.status_code == 200
    assert r.headers["x-router-model"] == "fake/strong"
    cost = float(r.headers["x-router-cost-usd"])
    assert cost > 0

    rec = json.loads((tmp_path / "state" / "decisions.jsonl").read_text().strip().splitlines()[-1])
    assert rec["chosen"] == "fake/strong"
    assert rec["cost_usd"] == cost
    assert rec["price"] == {"in": 4.0, "out": 4.0}

    state = json.loads((tmp_path / "state" / "state.json").read_text())
    assert state["totals"]["fake"]["reqs"] == 1
    assert state["totals"]["fake"]["cost"] == cost
    assert state["by_preset"]["high"]["reqs"] == 1
    assert state["by_preset"]["high"]["cost"] == cost
    assert state["by_model"]["fake/strong"]["cost"] == cost

    status = run(client.get("/v1/status")).json()
    assert status["costs"]["totals"]["fake"]["cost"] == cost
    assert status["costs"]["by_preset"]["high"]["reqs"] == 1
    assert status["preset_estimates"]["high"]["approx_cost_per_1m"] == 4.0


def test_streaming_cost_logged(client, stub, tmp_path):
    stub.httpd.state["behavior"]["strong"] = "sse"
    status, raw = stream(client, {**BODY, "stream": True})
    assert status == 200 and b"data: [DONE]" in raw
    rec = json.loads((tmp_path / "state" / "decisions.jsonl").read_text().strip().splitlines()[-1])
    assert rec["stream"] is True
    assert rec["cost_usd"] > 0
    assert rec["usage"]["estimated"] is True
    state = json.loads((tmp_path / "state" / "state.json").read_text())
    assert state["totals"]["fake"]["reqs"] == 1


def test_daily_cost_cap(tmp_path, stub, monkeypatch):
    monkeypatch.setenv("FAKE_KEY", "k1")
    monkeypatch.setenv("FAKE2_KEY", "k2")
    (tmp_path / "catalog.yaml").write_text(
        CATALOG.replace("daily_tokens: 1000", "daily_tokens: 1000, daily_cost_usd: 0.01")
               .replace("PLACEHOLDER", stub.base_url))
    (tmp_path / ".env").write_text("")
    today = datetime.date.today().isoformat()
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "state.json").write_text(json.dumps({
        "cooldowns": {},
        "usage": {today: {"fake2": {"in": 0, "out": 0, "reqs": 1, "cost": 0.02}}},
        "balances": {},
    }))
    c = build_client(tmp_path)
    try:
        r = run(c.post("/v1/chat/completions", json={**BODY, "model": "low"}))
    finally:
        run(c.aclose())
    assert r.status_code == 503
    assert r.json()["error"]["type"] == "no_candidates"
