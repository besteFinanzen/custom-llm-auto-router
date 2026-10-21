"""Model scoring and eligibility, per the routing policy:

request effort (preset) -> blended-cost ceiling -> highest-ranked eligible model.
Ranks come from state/ranks.json (Artificial Analysis + opencode.ai/data, via
refresh.py); catalog seed_rank is the prior for unmatched models; rank_override wins.
"""
from __future__ import annotations

import json
import pathlib

from catalog import Catalog, Model
from state import State

WEIGHT_IN = 0.75  # blended cost = 0.75*input + 0.25*output ($/1M)


def blended_cost(cost_in: float, cost_out: float) -> float:
    return WEIGHT_IN * cost_in + (1 - WEIGHT_IN) * cost_out


class Ranks:
    def __init__(self, path: pathlib.Path):
        self.path = path / "ranks.json"
        self.data: dict = {"models": {}, "unmatched": [], "fetched_at": None}
        self.load()

    def load(self) -> None:
        if self.path.exists():
            try:
                self.data = json.loads(self.path.read_text())
            except Exception:
                pass

    def components(self, model_id: str) -> dict:
        return (self.data.get("models") or {}).get(model_id, {})


def score(model: Model, preset: dict, ranks: Ranks) -> float:
    if model.rank_override is not None:
        return float(model.rank_override)
    comps = ranks.components(model.id)
    total = weight_sum = 0.0
    for key, w in preset["weights"].items():
        val = comps.get(key)
        if val is None and key.startswith("aa_"):
            val = model.seed_rank  # prior for unmatched benchmark data
        if val is None:
            continue  # unknown component (e.g. oc_*) -> drop weight
        total += w * float(val)
        weight_sum += w
    if weight_sum == 0:
        return model.seed_rank
    return total / weight_sum


def effective(model: Model, ranks: Ranks) -> tuple[float, float, int, bool]:
    """(cost_in, cost_out, context, tools) with ranks.json data overriding catalog."""
    d = ranks.components(model.id).get("data") or {}
    return (
        float(model.cost_override) if model.cost_override is not None else float(d.get("cost_in", model.cost_in)),
        float(model.cost_override) if model.cost_override is not None else float(d.get("cost_out", model.cost_out)),
        int(d.get("context", model.context)),
        bool(d.get("tools", model.tools)),
    )


def rank_candidates(catalog: Catalog, preset_name: str, ranks: Ranks, state: State,
                    models: dict[str, Model] | None = None):
    """Returns (ordered candidates, rejected list) for decision logging.

    `models` is the candidate universe (curated catalog + discovered); defaults to
    catalog models. Blocklisted ids are always rejected.
    """
    preset = catalog.presets[preset_name]
    source = models if models is not None else catalog.models
    chosen: list[tuple[float, float, Model]] = []
    rejected: list[dict] = []
    for m in source.values():
        reason = None
        prov = catalog.providers[m.provider]
        cost_in, cost_out, context, tools = effective(m, ranks)
        if m.id in catalog.blocklist:
            reason = "blocklisted"
        elif not m.enabled:
            reason = "model disabled in catalog"
        elif not prov.enabled:
            reason = f"provider disabled (missing {prov.api_key_env})"
        elif blended_cost(cost_in, cost_out) > float(preset["max_cost"]):
            reason = f"over cost ceiling ({blended_cost(cost_in, cost_out):.2f} > {preset['max_cost']})"
        elif context < int(preset.get("min_context", 0)):
            reason = f"context too small ({context} < {preset['min_context']})"
        elif preset.get("require_tools") and not tools:
            reason = "no tool calling"
        elif (rem := state.cooldown_remaining(m.id)) > 0:
            reason = f"cooling down ({rem:.0f}s left)"
        elif not (state.balance_any(m.provider) or {}).get("ok", True):
            reason = "balance exhausted"
        if reason:
            rejected.append({"id": m.id, "reason": reason})
        else:
            chosen.append((score(m, preset, ranks), blended_cost(cost_in, cost_out), m))
    # highest rank first; ties -> cheaper first
    chosen.sort(key=lambda t: (-t[0], t[1]))
    return [m for _, _, m in chosen], rejected


def preset_estimate(catalog: Catalog, preset_name: str, ranks: Ranks, state: State,
                    models: dict[str, Model] | None = None) -> dict:
    """Approximate cost of the preset: what the top eligible model costs per 1M tokens."""
    preset = catalog.presets[preset_name]
    candidates, _ = rank_candidates(catalog, preset_name, ranks, state, models)
    est = {
        "preset": preset_name,
        "max_cost": preset["max_cost"],           # ceiling ($/1M blended)
        "candidates": len(candidates),
        "top_model": None,
        "approx_cost_per_1m": None,
        "approx_cost_in": None,
        "approx_cost_out": None,
    }
    if candidates:
        cost_in, cost_out, _, _ = effective(candidates[0], ranks)
        est.update(
            top_model=candidates[0].id,
            approx_cost_per_1m=round(blended_cost(cost_in, cost_out), 4),
            approx_cost_in=cost_in,
            approx_cost_out=cost_out,
        )
    return est
