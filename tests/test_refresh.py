"""Ranks refresh: ':free' models must never inherit paid prices."""
from __future__ import annotations

from catalog import Model
from refresh import compose, mdev_record


def _model(mid="openrouter/free-x", upstream="vendor/model:free", **kw):
    cfg = {"id": mid, "provider": "openrouter", "upstream": upstream,
           "cost_in": 0, "cost_out": 0, "context": 32768, "tools": True}
    cfg.update(kw)
    return Model(cfg)


def test_mdev_record_uses_exact_free_variant():
    m = _model()
    mdev = {"openrouter": {"models": {
        "vendor/model": {"cost": {"input": 5, "output": 10}},
        "vendor/model:free": {"cost": {"input": 0, "output": 0}},
    }}}
    rec = mdev_record(mdev, "openrouter", m)
    assert rec["cost"] == {"input": 0, "output": 0}


def test_mdev_record_supports_google_models():
    m = Model({"id": "google/gemini-test", "provider": "google",
               "upstream": "gemini-test"})
    mdev = {"google": {"models": {
        "gemini-test": {"cost": {"input": 0.3, "output": 2.5},
                        "limit": {"context": 1048576, "output": 65536},
                        "tool_call": True},
    }}}

    rec = mdev_record(mdev, "google", m)

    assert rec["cost"] == {"input": 0.3, "output": 2.5}
    assert rec["limit"] == {"context": 1048576, "output": 65536}
    assert rec["tool_call"] is True


def test_mdev_record_never_falls_back_to_paid_base():
    m = _model()
    mdev = {"openrouter": {"models": {
        "vendor/model": {"cost": {"input": 5, "output": 10}},
    }}}
    assert mdev_record(mdev, "openrouter", m) is None


def test_compose_forces_free_variant_cost_zero():
    m = _model()
    old = {"openrouter/free-x": {"data": {"cost_in": 0.5, "cost_out": 0.5,
                                          "context": 1000}}}
    out = compose(None, {"openrouter/free-x": m}, None, None, None, old)
    data = out["models"]["openrouter/free-x"]["data"]
    assert data["cost_in"] == 0.0
    assert data["cost_out"] == 0.0
    assert data["context"] == 1000  # non-cost metadata is preserved


def test_compose_keeps_paid_prices_for_non_free_models():
    m = _model(mid="openrouter/paid", upstream="vendor/model")
    old = {"openrouter/paid": {"data": {"cost_in": 1.5, "cost_out": 2.5}}}
    out = compose(None, {"openrouter/paid": m}, None, None, None, old)
    data = out["models"]["openrouter/paid"]["data"]
    assert data["cost_in"] == 1.5
    assert data["cost_out"] == 2.5
