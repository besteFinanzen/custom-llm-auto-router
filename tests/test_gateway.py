"""End-to-end gateway behaviour against a scriptable stub upstream."""
from __future__ import annotations

import datetime
import json
import time

from state import State
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


def _data_events(raw: bytes) -> list[dict]:
    out = []
    for line in raw.decode().splitlines():
        if line.startswith("data: ") and line != "data: [DONE]":
            out.append(json.loads(line[6:]))
    return out


def test_streaming_finish_reason_moved_to_end(client, stub):
    stub.httpd.state["behavior"]["strong"] = "sse_after_finish"
    status, raw = stream(client, {**BODY, "stream": True})
    assert status == 200
    assert raw.rstrip().endswith(b"data: [DONE]")

    evs = _data_events(raw)
    finished = [e for e in evs
                if any(c.get("finish_reason") is not None for c in e.get("choices") or [])]
    assert len(finished) == 1
    assert finished[0] is evs[-1]
    assert finished[0]["choices"] == [
        {"index": 0, "delta": {}, "finish_reason": "tool_calls"}]

    # Every chunk before the synthetic finale has a null finish_reason.
    assert all(c.get("finish_reason") is None
               for e in evs[:-1] for c in e.get("choices") or [])

    # Deltas that arrived after the original finish_reason still reach the client.
    deltas = [c.get("delta") or {} for e in evs for c in e.get("choices") or []]
    assert {"content": "late"} in deltas
    assert {"reasoning_content": "thinking"} in deltas
    assert any("tool_calls" in d for d in deltas)
    assert any(e.get("usage") for e in evs)


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


def test_provider_failures_counted_once(client, stub, tmp_path):
    """A failed attempt bumps the provider counter exactly once, not twice."""
    stub.httpd.state["behavior"].update({"strong": "err", "mid": "err", "cheap": "err"})
    r = run(client.post("/v1/chat/completions", json=BODY))
    assert r.status_code == 502
    state = json.loads((tmp_path / "state" / "state.json").read_text())
    assert state["failures"]["models"]["fake/strong"]["count"] == 1
    assert state["failures"]["models"]["fake/mid"]["count"] == 1
    assert state["failures"]["providers"]["fake"]["count"] == 2
    assert state["failures"]["providers"]["fake2"]["count"] == 1


def test_failed_provider_skipped_without_trying(tmp_path, stub, monkeypatch):
    """An excluded provider is rejected up front and does not eat max_failovers."""
    monkeypatch.setenv("FAKE_KEY", "k1")
    monkeypatch.setenv("FAKE2_KEY", "k2")
    write_catalog(tmp_path, stub.base_url, extra_state={
        "failures": {"models": {}, "providers": {
            "fake": {"count": 5, "first_failure": 9e12,
                     "last_failure": 9e12, "status": 403}}},
    })
    c = build_client(tmp_path)
    try:
        r = run(c.post("/v1/chat/completions", json={**BODY, "model": "high"}))
    finally:
        run(c.aclose())
    assert r.status_code == 200
    assert r.json()["choices"][0]["message"]["content"] == "hello from cheap"
    assert [b["model"] for b in stub.httpd.state["received"]] == ["cheap"]
    rec = json.loads((tmp_path / "state" / "decisions.jsonl").read_text().strip().splitlines()[-1])
    assert {x["id"] for x in rec["rejected"]} >= {"fake/strong", "fake/mid"}
    assert rec["tried"] == []


def test_failure_window_forgives_stale_failures(tmp_path):
    st = State(tmp_path / "state3")
    thresholds = {"enabled": True, "model_threshold": 1, "provider_threshold": 99,
                  "model_window_seconds": 10, "provider_window_seconds": 0}
    run(st.record_model_failure("fake/strong", "fake", "forbidden", 403))
    assert st.is_model_failed("fake/strong", thresholds)
    entry = st.data["failures"]["models"]["fake/strong"]
    entry["first_failure"] = time.time() - 30
    entry["last_failure"] = time.time() - 11
    assert not st.is_model_failed("fake/strong", thresholds)
    # stale entry is dropped so the next failure starts a fresh count
    assert "fake/strong" not in st.data["failures"]["models"]


def test_reset_failures_endpoint(tmp_path, stub, monkeypatch):
    monkeypatch.setenv("FAKE_KEY", "k1")
    monkeypatch.setenv("FAKE2_KEY", "k2")
    write_catalog(tmp_path, stub.base_url, extra_state={
        "cooldowns": {"fake/strong": 9e12},
        "failures": {
            "models": {"fake/strong": {"count": 3, "first_failure": 1,
                                       "last_failure": 1, "status": 403}},
            "providers": {"fake": {"count": 5, "first_failure": 1,
                                   "last_failure": 1, "status": 403}},
        },
        "backoff": {"fake/strong": {"until": 9e12, "base": 60,
                                    "mult": 2.0, "ts": 1}},
    })
    c = build_client(tmp_path)
    try:
        r = run(c.post("/v1/reset-failures"))
        assert r.status_code == 200
        assert r.json()["cleared"] == {"models": 1, "providers": 1,
                                       "backoff": 1, "cooldowns": 1}
        status = run(c.get("/v1/status")).json()
        assert status["failures"] == {"models": {}, "providers": {}}
        assert status["cooldowns"] == {}
    finally:
        run(c.aclose())
    state = json.loads((tmp_path / "state" / "state.json").read_text())
    assert state["backoff"] == {}
