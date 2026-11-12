"""Focused tests for attribution, context-aware routing, jobs and budget gates."""
from __future__ import annotations

import copy

import httpx
import pytest

from catalog import Catalog
from selector import Ranks, context_error, context_requirement, rank_for_request
from state import State
from tests.conftest import build_client, run, write_catalog
from tests.test_selector import RAW
from worktracker import attribution, jobs
from worktracker.budgets import BudgetGate, RemoteBudgetClient, payload
from worktracker.config import Settings
from worktracker.store import (
    _clean_metadata,
    _intersection_length,
    _merge_intervals,
    normalize_remote,
)


@pytest.fixture
def routing(tmp_path, monkeypatch):
    """Independent copy of the small selector fixture, including its models."""
    monkeypatch.setenv("P1_KEY", "test")
    monkeypatch.setenv("P2_KEY", "test")
    raw = copy.deepcopy(RAW)
    raw["policy"] = {"context_headroom": 1.1, "output_reserve_tokens": 1000}
    for provider in raw["providers"].values():
        provider["api_key_env"] = "P1_KEY"
    return Catalog(raw), Ranks(tmp_path), State(tmp_path)


def test_missing_attribution_lists_every_field_and_fix():
    missing = attribution.missing({})
    assert [item["field"] for item in missing] == [
        "kind", "session", "project", "project_root", "session_directory",
        "device", "tool", "branch",
    ]
    assert all(item["fix"] for item in missing)
    error = attribution.error_payload(missing)["error"]
    assert error["code"] == "missing_attribution"
    assert all(f"x-oc-{item['field'].replace('_', '-')}" in error["message"]
               for item in error["missing"])


def test_attribution_headers_body_fallback_and_explicit_non_git():
    body = {"x_oc": {"session": "s1", "project": "demo", "project_root": "/work/demo",
                     "session_directory": "/work/demo", "device": "laptop", "tool": "opencode"}}
    meta = attribution.extract({"x-oc-no-git": "1", "x-oc-kind": "title"}, body)
    assert attribution.missing(meta) == []
    rec = {"kind": "primary"}
    attribution.apply(rec, meta)
    assert rec["kind"] == "title"
    assert rec["session_directory"] == "/work/demo"
    assert "git_head" not in rec
    assert rec["metadata"]["no_git"] is True

    meta = attribution.extract({"x-oc-session": "header-s"}, {"x_oc": {"session": "body-s"}})
    assert meta["session"] == "header-s"


def test_metadata_sanitizes_nested_prompt_content():
    clean = _clean_metadata({
        "prompt": "secret", "safe": {"response_text": "secret", "duration": 4},
        "nested": [{"messages": ["secret"], "ok": "yes"}],
    })
    assert clean == {"safe": {"duration": 4},
                     "nested": [{"ok": "yes"}]}
    assert normalize_remote("git@GitHub.com:Org/Repo.git") == "github.com/Org/Repo"
    assert normalize_remote("https://GITHUB.com/org/repo.git") == "github.com/org/repo"


def test_context_reserves_requested_output_and_escalates(routing):
    cat, ranks, state = routing
    cat.models["p1/free"].context = 700
    cat.models["p2/small"].context = 700
    cat.models["p1/paid"].context = 3000
    body = {"messages": [{"role": "user", "content": "a" * 4000}], "max_tokens": 700}
    requirement = context_requirement(cat.presets["low"], cat.policy, body)
    assert requirement["prompt_tokens"] == 1033
    assert requirement["output_reserve"] == 700
    assert requirement["required_context"] == 1837

    result = rank_for_request(cat, "low", ranks, state, cat.models, body)
    assert result["escalated"] is True
    assert result["preset"] == "high"
    assert [m.id for m in result["candidates"]] == ["p1/paid"]


def test_context_error_when_even_largest_model_cannot_fit(routing):
    cat, ranks, state = routing
    for model in cat.models.values():
        model.context = 600
    body = {"messages": [{"content": "a" * 4000}]}
    result = rank_for_request(cat, "low", ranks, state, cat.models, body)
    assert not result["candidates"]
    error = context_error(cat, ranks, "low", result["context"], cat.models)["error"]
    assert error["code"] == "context_too_large"
    assert error["largest_context"] == {"context": 600, "model": "p1/free"}
    assert "output reserve" in error["message"]


def test_job_validation_latency_success_and_fallback(routing):
    cat, ranks, state = routing
    profile = jobs.validate({
        "name": "review", "base_preset": "high", "max_latency_p95_ms": 500,
        "min_success_rate": 0.9, "on_no_match": "fallback",
    }, cat.presets)
    assert profile["on_no_match"] == "fallback"
    metrics = {"p1/free": {"rt_latency_p95_ms": 501, "rt_success_rate": 99, "rt_requests": 9},
               "p1/paid": {"rt_latency_p95_ms": 400, "rt_success_rate": 80, "rt_requests": 9},
               "p2/small": {"rt_latency_p95_ms": 800, "rt_success_rate": 99, "rt_requests": 9}}
    result = rank_for_request(
        cat, "high", ranks, state, cat.models, {"messages": []}, job=profile, metrics=metrics)
    assert not result["candidates"]
    assert any("latency p95" in item["reason"] for item in result["rejected"])
    assert any("success rate" in item["reason"] for item in result["rejected"])

    # Below the sample-size floor the same numbers must not exclude models.
    thin = {key: {**item, "rt_requests": 2} for key, item in metrics.items()}
    relaxed = rank_for_request(
        cat, "high", ranks, state, cat.models, {"messages": []}, job=profile, metrics=thin)
    assert relaxed["candidates"]

    exported = jobs.export({"review": {**profile, "source": "db", "updated_at": "now"}})
    assert jobs.import_profiles(exported, cat.presets) == [{**profile}]
    with pytest.raises(ValueError, match="min_success_rate"):
        jobs.validate({"name": "bad", "base_preset": "high", "min_success_rate": 90}, cat.presets)
    with pytest.raises(ValueError, match="base_preset"):
        jobs.validate({"name": "bad", "base_preset": "missing"}, cat.presets)


def test_interval_union_and_intersection_do_not_double_count():
    merged = _merge_intervals([(0, 20), (5, 30), (15, 25), (40, 50)])
    assert merged == [(0, 30), (40, 50)]
    assert _intersection_length(merged, [(10, 45)]) == 25


class FakeBudgetStore:
    def __init__(self, result):
        self.result = result
        self.calls = 0

    def budget_check(self, project_key, branch):
        self.calls += 1
        return self.result


def test_budget_cache_and_blocking_payload():
    store = FakeBudgetStore({
        "blocked": True, "warnings": [],
        "blocked_by": [{"metric": "cost", "used": 12, "limit": 10, "period": "daily"}],
    })
    gate = BudgetGate(store, Settings(database_url="postgres://test", budget_cache_s=60))
    assert gate.check("path:/demo", "main")["blocked"] is True
    assert gate.check("path:/demo", "main") == store.result
    assert store.calls == 1
    gate.invalidate()
    gate.check("path:/demo", "main")
    assert store.calls == 2
    assert payload(store.result)["error"]["type"] == "budget_exceeded"
    assert "cost 12/10" in payload(store.result)["error"]["message"]


def test_remote_budget_fail_open_or_closed(monkeypatch):
    calls = []

    def offline(*args, **kwargs):
        calls.append(kwargs)
        return httpx.Client(transport=httpx.MockTransport(
            lambda request: httpx.Response(503, json={"error": "offline"})))

    monkeypatch.setattr(httpx, "Client", offline)
    client = RemoteBudgetClient("http://tracker", "secret", fail_closed=False)
    assert client.check("demo", "main")["blocked"] is False
    assert calls[0]["timeout"] == 3.0
    client = RemoteBudgetClient("http://tracker", "secret", fail_closed=True)
    result = client.check("demo", "main")
    assert result["blocked"] is True
    assert result["blocked_by"][0]["id"] == "fail-closed"
    assert result["error"]


def test_gateway_enforces_attribution_before_upstream(tmp_path, stub, monkeypatch):
    monkeypatch.setenv("FAKE_KEY", "k1")
    monkeypatch.setenv("FAKE2_KEY", "k2")
    monkeypatch.setenv("TRACKER_ATTRIBUTION_MODE", "enforce")
    write_catalog(tmp_path, stub.base_url)
    client = build_client(tmp_path)
    try:
        response = run(client.post("/v1/chat/completions", json={
            "model": "high", "messages": [], "x_oc": {"session": "s1"},
        }))
        assert response.status_code == 400
        assert {item["field"] for item in response.json()["error"]["missing"]} == {
            "kind", "project", "project_root", "session_directory", "device", "tool", "branch",
        }
        assert stub.httpd.state["received"] == []
    finally:
        run(client.aclose())


def complete_headers():
    return {
        "x-oc-session": "s1", "x-oc-project": "path:/work/demo",
        "x-oc-project-root": "/work/demo", "x-oc-session-directory": "/work/demo",
        "x-oc-device": "laptop", "x-oc-tool": "opencode",
        "x-oc-branch": "main", "x-oc-git-head": "a" * 40,
        "x-oc-kind": "primary",
    }


class RecordingStore:
    def __init__(self):
        self.events = []
        self.stats = []
        self.rejected_requests = []

    def record_rejected(self, event):
        self.rejected_requests.append(event)
        return len(self.rejected_requests)

    def record_usage_event(self, event):
        self.events.append(event)

    def record_model_stat(self, *args):
        self.stats.append(args)

    def list_jobs(self):
        return []


def test_gateway_warns_and_persists_ttft_historical_cost(client):
    response = run(client.post("/v1/chat/completions", json={
        "model": "high", "messages": [{"role": "user", "content": "hello"}],
    }, headers=complete_headers()))
    assert response.status_code == 200
    rt = client._transport.app.state.wt_runtime
    store = RecordingStore()
    rt.worktracker = store
    response = run(client.post("/v1/chat/completions", json={
        "model": "high", "messages": [{"role": "user", "content": "hello"}],
    }, headers=complete_headers()))
    assert response.status_code == 200
    event = store.events[-1]
    assert event["session_directory"] == "/work/demo"
    assert event["git_head"] == "a" * 40
    assert event["ttft_ms"] == event["latency_ms"]
    assert event["cost"] == pytest.approx(0.000072)
    assert store.stats[-1][0:3] == ("fake", "fake/strong", True)
    assert "messages" not in event and "prompt" not in event


def test_gateway_budget_block_returns_429_without_calling_upstream(client):
    rt = client._transport.app.state.wt_runtime
    rt.budget = BudgetGate(FakeBudgetStore({
        "blocked": True, "warnings": [],
        "blocked_by": [{"metric": "requests", "used": 5, "limit": 5, "period": "total"}],
    }), Settings(database_url="postgres://test"))
    response = run(client.post("/v1/chat/completions", json={
        "model": "high", "messages": [],
    }, headers=complete_headers()))
    assert response.status_code == 429
    assert response.headers["retry-after"] == "60"
    assert response.json()["error"]["code"] == "budget_exceeded"
    assert rt.state.data["totals"] == {}
