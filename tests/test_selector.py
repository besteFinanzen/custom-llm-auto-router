"""Pure selector logic: ceilings, floors, cooldowns, balances, overrides, scoring."""
from __future__ import annotations

import json
import pathlib

import pytest

from catalog import Catalog
from selector import Ranks, effective, rank_candidates, score
from state import State

RAW = {
    "policy": {},
    "providers": {
        "p1": {"base_url": "http://x/v1", "api_key_env": None},
        "p2": {"base_url": "http://y/v1", "api_key_env": None},
    },
    "presets": {
        "low": {"max_cost": 0.0, "min_context": 0, "require_tools": False,
                "weights": {"aa_intelligence": 1}},
        "high": {"max_cost": 5.0, "min_context": 0, "require_tools": False,
                 "weights": {"aa_intelligence": 1}},
    },
    "models": [
        {"id": "p1/free", "provider": "p1", "upstream": "free", "cost_in": 0, "cost_out": 0,
         "seed_rank": 40, "context": 100000, "tools": True},
        {"id": "p1/paid", "provider": "p1", "upstream": "paid", "cost_in": 3, "cost_out": 3,
         "seed_rank": 92, "context": 100000, "tools": True},
        {"id": "p2/small", "provider": "p2", "upstream": "small", "cost_in": 0, "cost_out": 0,
         "seed_rank": 70, "context": 8000, "tools": False},
    ],
}


@pytest.fixture
def cat():
    return Catalog(RAW)


@pytest.fixture
def st(tmp_path):
    return State(tmp_path)


@pytest.fixture
def rk(tmp_path):
    return Ranks(tmp_path)


def test_cost_ceiling(cat, st, rk):
    cands, rej = rank_candidates(cat, "low", rk, st)
    ids = [m.id for m in cands]
    assert "p1/free" in ids and "p1/paid" not in ids
    assert any(r["id"] == "p1/paid" and "cost ceiling" in r["reason"] for r in rej)


def test_min_context_floor(cat, st, rk):
    cat.presets["low"]["min_context"] = 50000
    cands, rej = rank_candidates(cat, "low", rk, st)
    assert [m.id for m in cands] == ["p1/free"]
    assert any(r["id"] == "p2/small" and "context too small" in r["reason"] for r in rej)


def test_require_tools(cat, st, rk):
    cat.presets["low"]["require_tools"] = True
    cands, _ = rank_candidates(cat, "low", rk, st)
    assert [m.id for m in cands] == ["p1/free"]


def test_cooldown_excluded(cat, st, rk):
    st.data["cooldowns"]["p1/free"] = 9e12  # far future
    cands, rej = rank_candidates(cat, "low", rk, st)
    assert "p1/free" not in [m.id for m in cands]
    assert any("cooling down" in r["reason"] for r in rej)


def test_balance_exhausted_excluded(cat, st, rk):
    st.data["balances"]["p1"] = {"ts": 0, "ok": False}
    # note: selector reads cached balance flags; gateway refreshes them beforehand
    cands, rej = rank_candidates(cat, "low", rk, st)
    assert "p1/free" not in [m.id for m in cands]
    assert any("balance exhausted" in r["reason"] for r in rej)


def test_rank_sorting_and_override(cat, st, rk):
    cands, _ = rank_candidates(cat, "high", rk, st)
    assert [m.id for m in cands][:3] == ["p1/paid", "p2/small", "p1/free"]  # seed 92 > 70 > 40
    cat.models["p1/free"].rank_override = 99
    cands, _ = rank_candidates(cat, "low", rk, st)
    assert cands[0].id == "p1/free"


def test_ranks_json_overrides_seed(cat, st, rk, tmp_path):
    (tmp_path / "ranks.json").write_text(json.dumps({"models": {
        "p1/free": {"aa_intelligence": 10},
        "p1/paid": {"aa_intelligence": 5},
    }}))
    rk.load()
    cands, _ = rank_candidates(cat, "high", rk, st)
    # ranks.json beats seeds: paid(5) < free(10) although seeds were 92/40;
    # p2/small has no ranks data -> seed 70 still applies
    assert [m.id for m in cands][:3] == ["p2/small", "p1/free", "p1/paid"]


def test_weight_renormalization(cat, st, rk):
    preset = {"weights": {"aa_intelligence": 0.5, "oc_retention": 0.5}}
    cat.models["p1/free"].seed_rank = 80
    s = score(cat.models["p1/free"], preset, rk)  # only aa_ available -> falls back to seed
    assert s == pytest.approx(80)


def test_no_aa_data_uses_seed(cat, st, rk):
    preset = {"weights": {"oc_retention": 1}}
    s = score(cat.models["p1/free"], preset, rk)  # no components at all -> seed prior
    assert s == 40


def test_failed_model_excluded(cat, st, rk):
    cat.policy["failures"] = {"enabled": True, "model_threshold": 1,
                              "provider_threshold": 99,
                              "model_window_seconds": 0, "provider_window_seconds": 0}
    st.data["failures"]["models"]["p1/free"] = {
        "count": 1, "first_failure": 9e12, "last_failure": 9e12, "status": 403}
    cands, rej = rank_candidates(cat, "low", rk, st)
    assert "p1/free" not in [m.id for m in cands]
    assert any(r["id"] == "p1/free" and "consecutive failures" in r["reason"]
               for r in rej)


def test_failed_provider_excluded(cat, st, rk):
    cat.policy["failures"] = {"enabled": True, "model_threshold": 99,
                              "provider_threshold": 1,
                              "model_window_seconds": 0, "provider_window_seconds": 0}
    st.data["failures"]["providers"]["p1"] = {
        "count": 1, "first_failure": 9e12, "last_failure": 9e12, "status": 403}
    cands, rej = rank_candidates(cat, "low", rk, st)
    assert "p1/free" not in [m.id for m in cands]
    assert any(r["id"] == "p1/free" and "provider p1 excluded" in r["reason"]
               for r in rej)


def test_free_variant_always_costs_zero(cat, st, rk, tmp_path):
    """A stale/paid models.dev price must not push a ':free' model over a $0 cap."""
    m = cat.models["p1/free"]
    m.upstream = "vendor/model:free"
    (tmp_path / "ranks.json").write_text(json.dumps({"models": {
        m.id: {"data": {"cost_in": 7.0, "cost_out": 7.0, "context": 100000}},
    }}))
    rk.load()
    assert effective(m, rk)[:2] == (0.0, 0.0)
    cands, _ = rank_candidates(cat, "low", rk, st)
    assert m.id in [c.id for c in cands]
