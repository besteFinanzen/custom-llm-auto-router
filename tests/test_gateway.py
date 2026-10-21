"""End-to-end gateway behaviour against a scriptable stub upstream."""
from __future__ import annotations

import datetime
import json

from tests.conftest import build_client, run, write_catalog

BODY = {"model": "high", "messages": [{"role": "user", "content": "hi"}]}


def stream(client, body):
    async def go():
        async with client.stream("POST", "/v1/chat/completions", json=body) as r:
            raw = b"".join([chunk async for chunk in r.aiter_bytes()])
            return r.status_code, raw
    return run(go())


def test_routes_ranked_model(client, stub):
    r = run(client.post("/v1/chat/completions", json=BODY))
    assert r.status_code == 200
    assert r.json()["choices"][0]["message"]["content"] == "hello from strong"
    assert stub.httpd.state["received"][0]["model"] == "strong"


def test_429_fails_over_and_cools_down(client, stub, tmp_path):
    stub.httpd.state["behavior"]["strong"] = "429"
    r = run(client.post("/v1/chat/completions", json=BODY))
    assert r.status_code == 200
    assert r.json()["choices"][0]["message"]["content"] == "hello from mid"
    log = (tmp_path / "state" / "decisions.jsonl").read_text().strip().splitlines()
    rec = json.loads(log[-1])
    assert rec["chosen"] == "fake/mid"
    assert rec["tried"] and rec["tried"][0]["status"] == 429

    # strong is cooling down: next request goes straight to mid
    r2 = run(client.post("/v1/chat/completions", json=BODY))
    assert r2.json()["choices"][0]["message"]["content"] == "hello from mid"
    assert [b["model"] for b in stub.httpd.state["received"]] == ["strong", "mid", "mid"]


def test_aux_kind_forces_low_preset(client, stub):
    r = run(client.post("/v1/chat/completions", json=BODY, headers={"x-oc-kind": "title"}))
    assert r.status_code == 200
    assert r.json()["choices"][0]["message"]["content"] == "hello from cheap"
    assert stub.httpd.state["received"][0]["model"] == "cheap"


def test_explicit_preset_and_router_prefix(client, stub):
    run(client.post("/v1/chat/completions", json={**BODY, "model": "low"}))
    run(client.post("/v1/chat/completions", json={**BODY, "model": "router/low"}))
    assert [b["model"] for b in stub.httpd.state["received"]] == ["cheap", "cheap"]


def test_unknown_model_404(client):
    r = run(client.post("/v1/chat/completions", json={**BODY, "model": "nope"}))
    assert r.status_code == 404


def test_direct_passthrough(client, stub):
    r = run(client.post("/v1/chat/completions", json={**BODY, "model": "direct/fake/mid"}))
    assert r.status_code == 200
    assert r.json()["choices"][0]["message"]["content"] == "hello from mid"


def test_streaming_passthrough(client, stub):
    stub.httpd.state["behavior"]["strong"] = "sse"
    status, raw = stream(client, {**BODY, "stream": True})
    assert status == 200
    assert b"data: [DONE]" in raw


def test_budget_exhausted_no_candidates(tmp_path, stub, monkeypatch):
    monkeypatch.setenv("FAKE_KEY", "k1")
    monkeypatch.setenv("FAKE2_KEY", "k2")
    today = datetime.date.today().isoformat()
    write_catalog(tmp_path, stub.base_url, extra_state={
        "cooldowns": {},
        "usage": {today: {"fake2": {"in": 5000, "out": 0, "reqs": 5}}},
        "balances": {},
    })
    c = build_client(tmp_path)
    try:
        r = run(c.post("/v1/chat/completions", json={**BODY, "model": "low"}))
    finally:
        run(c.aclose())
    assert r.status_code == 503
    assert r.json()["error"]["type"] == "no_candidates"


def test_all_upstreams_fail(client, stub):
    stub.httpd.state["behavior"].update({"strong": "err", "mid": "err", "cheap": "err"})
    r = run(client.post("/v1/chat/completions", json=BODY))
    assert r.status_code == 502
    assert len(stub.httpd.state["received"]) == 3  # one try each, then give up
