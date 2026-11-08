"""End-to-end gateway behaviour against a scriptable stub upstream."""
from __future__ import annotations

import datetime
import json

from tests.conftest import build_client, run, write_catalog
from state import State

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


def test_403_fails_over_and_cools_down(client, stub, tmp_path):
    stub.httpd.state["behavior"]["strong"] = "403"
    r = run(client.post("/v1/chat/completions", json=BODY))
    assert r.status_code == 200
    assert r.json()["choices"][0]["message"]["content"] == "hello from mid"
    log = (tmp_path / "state" / "decisions.jsonl").read_text().strip().splitlines()
    rec = json.loads(log[-1])
    assert rec["chosen"] == "fake/mid"
    assert rec["tried"] and rec["tried"][0]["status"] == 403

    # strong is cooling down: next request goes straight to mid
    r2 = run(client.post("/v1/chat/completions", json=BODY))
    assert r2.json()["choices"][0]["message"]["content"] == "hello from mid"
    assert [b["model"] for b in stub.httpd.state["received"]] == ["strong", "mid", "mid"]


def test_consecutive_failures_exclude_model(tmp_path):
    """After model_threshold consecutive failures the model is excluded."""
    from state import State

    st = State(tmp_path / "state2")
    thresholds = {"enabled": True, "model_threshold": 2, "provider_threshold": 99,
                  "model_window_seconds": 0, "provider_window_seconds": 0,
                  "backoff_base_seconds": 60, "backoff_multiplier": 2.0,
                  "backoff_max_seconds": 900}
    assert not st.is_model_failed("fake/strong", thresholds)
    assert not st.is_provider_failed("fake", thresholds)

    run(st.record_model_failure("fake/strong", "fake", "forbidden", 403))
    run(st.record_model_failure("fake/strong", "fake", "forbidden", 403))
    assert st.is_model_failed("fake/strong", thresholds)

    # success resets the counters -> not failed anymore
    run(st.record_success("fake/strong", "fake"))
    assert not st.is_model_failed("fake/strong", thresholds)


def test_success_resets_failure_counter(tmp_path):
    """A successful response resets the consecutive-failure counter (used by the gateway)."""
    from state import State

    st = State(tmp_path / "state2")
    thresholds = {"enabled": True, "model_threshold": 1, "provider_threshold": 1,
                  "model_window_seconds": 0, "provider_window_seconds": 0,
                  "backoff_base_seconds": 60, "backoff_multiplier": 2.0,
                  "backoff_max_seconds": 900}

    run(st.record_model_failure("fake/strong", "fake", "forbidden", 403))
    run(st.record_provider_failure("fake", "forbidden", 403))
    assert st.is_model_failed("fake/strong", thresholds)
    assert st.is_provider_failed("fake", thresholds)

    run(st.record_success("fake/strong", "fake"))
    assert not st.is_model_failed("fake/strong", thresholds)
    assert not st.is_provider_failed("fake", thresholds)


def test_all_upstreams_fail(client, stub):
    stub.httpd.state["behavior"].update({"strong": "err", "mid": "err", "cheap": "err"})
    r = run(client.post("/v1/chat/completions", json=BODY))
    assert r.status_code == 502
    assert len(stub.httpd.state["received"]) == 3  # one try each, then give up
