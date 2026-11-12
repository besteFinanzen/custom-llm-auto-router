"""Unit tests for budget evaluation, caching and the remote split-mode client."""
from __future__ import annotations

import httpx

from worktracker.budgets import BudgetGate, RemoteBudgetClient, payload
from worktracker.config import Settings


class FakeStore:
    def __init__(self, result=None, error=None):
        self.result = result or {
            "project_key": "path:/repo/demo", "branch": None, "blocked": False,
            "blocked_by": [], "warnings": [], "budgets": [],
        }
        self.error = error
        self.calls = 0

    def budget_check(self, project_key, branch=None):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.result


def settings(**kwargs):
    return Settings(database_url="postgresql://unused", **kwargs)


def test_payload_describes_the_blocked_budgets():
    body = payload({"blocked_by": [{"metric": "cost_usd", "used": 12.5, "limit": 10.0,
                                    "period": "monthly"}],
                    "warnings": [{"metric": "cost_usd"}]})
    error = body["error"]
    assert error["type"] == "budget_exceeded" and error["code"] == "budget_exceeded"
    assert "cost_usd 12.5/10 (monthly)" in error["message"]
    assert error["budgets"][0]["period"] == "monthly"
    assert error["warnings"][0]["metric"] == "cost_usd"
    assert "Worktracker" in error["fix"]


def test_payload_handles_empty_result():
    error = payload({})["error"]
    assert error["budgets"] == [] and error["warnings"] == []
    assert error["message"].endswith(": ")


def test_gate_caches_then_serves_and_invalidates():
    store = FakeStore()
    clock = [1000.0]
    gate = BudgetGate(store, settings(budget_cache_s=5), clock=lambda: clock[0])
    first = gate.check("path:/repo/demo")
    assert first["blocked"] is False and store.calls == 1
    gate.check("path:/repo/demo")
    assert store.calls == 1  # served from cache
    clock[0] += 6
    gate.check("path:/repo/demo")
    assert store.calls == 2  # cache expired
    gate.check("path:/repo/demo", use_cache=False)
    assert store.calls == 3
    gate.check("path:/other")
    assert store.calls == 4
    gate.invalidate("path:/repo/demo")
    gate.check("path:/other")
    assert store.calls == 4  # other project still cached
    gate.invalidate()
    gate.check("path:/other")
    assert store.calls == 5


def test_gate_fails_open_by_default_and_closed_when_configured():
    store = FakeStore(error=RuntimeError("db down"))
    open_gate = BudgetGate(store, settings(budget_fail_closed=False))
    result = open_gate.check("path:/repo/demo")
    assert result["blocked"] is False and result["error"] == "db down"
    closed = BudgetGate(store, settings(budget_fail_closed=True))
    result = closed.check("path:/repo/demo")
    assert result["blocked"] is True
    assert result["blocked_by"][0]["id"] == "fail-closed"
    assert result["blocked_by"][0]["reason"] == "budget backend unavailable"


def test_with_reserve_projects_cost_and_hard_blocks():
    gate = BudgetGate(FakeStore(), settings())
    base = {"blocked": False, "blocked_by": [], "warnings": [], "budgets": [
        {"id": "b1", "metric": "cost", "used": 5.0, "limit": 10.0,
         "period": "monthly", "hard_block": True, "warn_pct": 50}]}
    assert gate._with_reserve(base, 0) is base  # zero reserve is a no-op
    blocked = gate._with_reserve(base, 6.0)
    assert blocked["blocked"] is True
    assert blocked["blocked_by"][0]["reason"] == "reserved estimate for this request"
    assert blocked["blocked_by"][0]["used"] == 11.0
    assert blocked["warnings"] == []


def test_with_reserve_warns_and_ignores_non_cost_budgets():
    gate = BudgetGate(FakeStore(), settings())
    base = {"blocked": False, "blocked_by": [], "warnings": [], "budgets": [
        {"id": "tok", "metric": "tokens", "used": 1, "limit": 10, "period": "daily"},
        {"id": "cost", "metric": "cost_usd", "used": 4.0, "limit": 10.0,
         "period": "monthly", "hard_block": False, "warn_pct": 50}]}
    result = gate._with_reserve(base, 2.0)
    assert result["blocked"] is False
    assert [w["id"] for w in result["warnings"]] == ["cost"]
    assert result["warnings"][0]["pct"] == 60.0
    # an existing warning for the same budget is not duplicated
    again = gate._with_reserve(result, 2.0)
    assert len(again["warnings"]) == 1


def test_with_reserve_keeps_existing_blocks_untouched():
    gate = BudgetGate(FakeStore(), settings())
    base = {"blocked": True, "blocked_by": [{"id": "x"}], "warnings": [], "budgets": [
        {"id": "cost", "metric": "cost", "used": 1, "limit": 10, "warn_pct": 10}]}
    result = gate._with_reserve(base, 5.0)
    assert result["blocked_by"] == [{"id": "x"}]
    assert result["warnings"] == []


class FakeResponse:
    def __init__(self, data=None, error=None):
        self._data = data
        self._error = error

    def raise_for_status(self):
        if self._error is not None:
            raise self._error

    def json(self):
        return self._data


class FakeClient:
    response: FakeResponse = FakeResponse()

    def __init__(self, *args, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get(self, url, params=None, headers=None):
        FakeClient.last = {"url": url, "params": params, "headers": headers}
        return FakeClient.response


def test_remote_client_returns_remote_result(monkeypatch):
    monkeypatch.setattr(httpx, "Client", FakeClient)
    FakeClient.response = FakeResponse(
        {"blocked": False, "blocked_by": [], "warnings": [], "budgets": []})
    client = RemoteBudgetClient("http://tracker/", token="secret")
    result = client.check("path:/repo/demo", "main", reserve_usd=0.25)
    assert result["blocked"] is False
    assert FakeClient.last["url"] == "http://tracker/worktracker/api/budget/check"
    assert FakeClient.last["params"]["branch"] == "main"
    assert FakeClient.last["params"]["reserve_usd"] == 0.25
    assert FakeClient.last["headers"]["authorization"] == "Bearer secret"


def test_remote_client_fail_open_and_closed(monkeypatch):
    monkeypatch.setattr(httpx, "Client", FakeClient)
    FakeClient.response = FakeResponse(error=RuntimeError("connection refused"))
    open_client = RemoteBudgetClient("http://tracker", fail_closed=False)
    result = open_client.check("path:/repo/demo")
    assert result["blocked"] is False and "connection refused" in result["error"]
    closed = RemoteBudgetClient("http://tracker", fail_closed=True)
    result = closed.check("path:/repo/demo")
    assert result["blocked"] is True
    assert result["blocked_by"][0]["id"] == "fail-closed"
