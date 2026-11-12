"""Model scoring and eligibility, per the routing policy.

request effort (preset) -> cost/context/score filters -> highest-ranked eligible model.

Context management is part of routing: the prompt is estimated from the request
body, an output reserve and safety headroom are added, and models whose effective
context window is smaller are rejected.  Auxiliary/compaction requests use the
same rule, so a large conversation is never compacted by a tiny-context model.

Ranks come from state/ranks.json (Artificial Analysis + opencode.ai/data, via
refresh.py); public benchmark snapshots from state/benchmarks.json enrich the
components.  catalog seed_rank is the prior for unmatched AA data; rank_override
always wins.
"""
from __future__ import annotations

import json
import math
import pathlib
from collections.abc import Mapping
from typing import Any

from catalog import Catalog, Model
from state import State

WEIGHT_IN = 0.75  # blended cost = 0.75*input + 0.25*output ($/1M)
DEFAULT_CONTEXT_HEADROOM = 1.1
DEFAULT_OUTPUT_RESERVE = 4096
MAX_OUTPUT_RESERVE = 32768
JOB_METRIC_MIN_SAMPLES = 5  # job latency/success floors need this many observations


def blended_cost(cost_in: float, cost_out: float) -> float:
    return WEIGHT_IN * cost_in + (1 - WEIGHT_IN) * cost_out


class Ranks:
    def __init__(self, path: pathlib.Path):
        self.path = path / "ranks.json"
        self.data: dict = {"models": {}, "unmatched": [], "fetched_at": None}
        self.benchmarks: dict = {"models": {}, "sources": {}}
        self.load()

    def load(self) -> None:
        if self.path.exists():
            try:
                self.data = json.loads(self.path.read_text())
            except Exception:
                pass
        benchmark_path = self.path.parent / "benchmarks.json"
        if benchmark_path.exists():
            try:
                self.benchmarks = json.loads(benchmark_path.read_text())
            except Exception:
                pass

    def components(self, model_id: str) -> dict:
        """Merged benchmark + Artificial Analysis/OpenCode components."""
        base = dict((self.data.get("models") or {}).get(model_id, {}))
        try:
            from worktracker.benchmarks import components as benchmark_components
            extra = benchmark_components(model_id, self.benchmarks)
        except Exception:
            extra = {}
        return {**extra, **base}


def _components(model: Model, ranks: Ranks) -> dict:
    return ranks.components(model.id)


def _component(model: Model, ranks: Ranks, key: str) -> float | None:
    value = _components(model, ranks).get(key)
    if value is None and key.startswith("aa_"):
        value = model.seed_rank
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def score(model: Model, preset: dict, ranks: Ranks, job: Mapping | None = None) -> float:
    if model.rank_override is not None and not job:
        return float(model.rank_override)
    weights = dict(preset.get("weights") or {})
    if job and job.get("weights"):
        weights.update({str(k): float(v) for k, v in job["weights"].items()})
    total = weight_sum = 0.0
    for key, w in weights.items():
        val = _component(model, ranks, key)
        if val is None:
            continue
        total += float(w) * val
        weight_sum += float(w)
    if weight_sum == 0:
        return float(model.rank_override) if model.rank_override is not None else model.seed_rank
    return total / weight_sum


def effective(model: Model, ranks: Ranks) -> tuple[float, float, int, bool]:
    """(cost_in, cost_out, context, tools) with ranks.json data overriding catalog.

    `:free` upstreams are always free: models.dev sometimes lists only the paid
    base model, and stale price data must not push free models over a $0 ceiling.
    An explicit cost_override still wins.
    """
    d = ranks.components(model.id).get("data") or {}
    if model.cost_override is not None:
        cost_in = cost_out = float(model.cost_override)
    elif str(getattr(model, "upstream", "")).endswith(":free"):
        cost_in = cost_out = 0.0
    else:
        cost_in = float(d.get("cost_in", model.cost_in))
        cost_out = float(d.get("cost_out", model.cost_out))
    return (
        cost_in,
        cost_out,
        int(d.get("context", model.context)),
        bool(d.get("tools", model.tools)),
    )


def _text_length(value: Any) -> int:
    if isinstance(value, str):
        return len(value)
    if isinstance(value, list):
        total = 0
        for part in value:
            if isinstance(part, Mapping):
                if part.get("type") in {"image_url", "image", "input_image"}:
                    total += 4000  # rough image token cost
                elif isinstance(part.get("text"), str):
                    total += len(part["text"])
                else:
                    total += len(json.dumps(part, default=str))
            else:
                total += _text_length(part)
        return total
    if isinstance(value, Mapping):
        return len(json.dumps(value, default=str))
    return 0


def estimate_prompt_tokens(body: Mapping | None) -> int:
    """Cheap, provider-agnostic prompt size estimate (chars/4 + structure)."""
    if not isinstance(body, Mapping):
        return 0
    chars = 0
    for message in body.get("messages") or []:
        if not isinstance(message, Mapping):
            continue
        chars += _text_length(message.get("content"))
        chars += _text_length(message.get("tool_calls"))
        chars += len(str(message.get("role") or ""))
    chars += _text_length(body.get("tools"))
    chars += _text_length(body.get("system"))
    chars += _text_length(body.get("prompt"))
    chars += len(str(body.get("response_format") or ""))
    return max(1, math.ceil(chars / 4) + 32)


def context_requirement(preset: Mapping, policy: Mapping,
                        body: Mapping | None) -> dict:
    prompt = estimate_prompt_tokens(body)
    headroom = float(preset.get("context_headroom")
                     or policy.get("context_headroom") or DEFAULT_CONTEXT_HEADROOM)
    requested_output = int(body.get("max_completion_tokens")
                           or body.get("max_tokens") or 0) if isinstance(body, Mapping) else 0
    reserve = (requested_output if requested_output > 0
               else int(policy.get("output_reserve_tokens") or DEFAULT_OUTPUT_RESERVE))
    reserve = max(512, min(reserve, MAX_OUTPUT_RESERVE))
    required = int(math.ceil(prompt * max(1.0, headroom)) + reserve)
    required = max(required, int(preset.get("min_context") or 0))
    return {
        "prompt_tokens": prompt,
        "output_reserve": reserve,
        "headroom": headroom,
        "required_context": required,
        "min_context": int(preset.get("min_context") or 0),
    }


def _preset_for_job(preset: dict, job: Mapping | None) -> dict:
    if not job:
        return preset
    merged = dict(preset)
    if job.get("weights"):
        merged["weights"] = {**(preset.get("weights") or {}),
                             **{str(k): float(v) for k, v in job["weights"].items()}}
    if job.get("max_cost") is not None:
        merged["max_cost"] = min(float(preset["max_cost"]), float(job["max_cost"]))
    if job.get("min_context") is not None:
        merged["min_context"] = max(int(preset.get("min_context") or 0),
                                    int(job["min_context"]))
    if job.get("require_tools") is not None:
        merged["require_tools"] = bool(job["require_tools"]) or bool(preset.get("require_tools"))
    return merged


def rank_candidates(catalog: Catalog, preset_name: str, ranks: Ranks, state: State,
                    models: dict[str, Model] | None = None, *,
                    required_context: int | None = None,
                    job: Mapping | None = None,
                    metrics: Mapping[str, Mapping] | None = None):
    """Returns (ordered candidates, rejected list) for decision logging.

    `models` is the candidate universe (curated catalog + discovered); defaults to
    catalog models.  `job` adds per-job score floors, weights and latency/success
    constraints.  `required_context` is the estimated prompt + output reserve.
    """
    preset = _preset_for_job(catalog.presets[preset_name], job)
    failures = catalog.policy.get("failures") or {}
    source = models if models is not None else catalog.models
    floors = {str(k): float(v) for k, v in (job or {}).get("min_scores", {}).items()}
    max_latency = (job or {}).get("max_latency_p95_ms")
    min_success = (job or {}).get("min_success_rate")
    chosen: list[tuple[float, float, Model]] = []
    rejected: list[dict] = []
    for m in source.values():
        reason = None
        prov = catalog.providers[m.provider]
        cost_in, cost_out, context, tools = effective(m, ranks)
        observed = (metrics or {}).get(m.id) or {}
        if m.id in catalog.blocklist:
            reason = "blocklisted"
        elif not m.enabled:
            reason = "model disabled in catalog"
        elif not prov.enabled:
            reason = (f"provider disabled (missing {prov.api_key_env})"
                      if prov.api_key_env and not prov.api_key else "provider disabled in catalog")
        elif blended_cost(cost_in, cost_out) > float(preset["max_cost"]):
            reason = f"over cost ceiling ({blended_cost(cost_in, cost_out):.2f} > {preset['max_cost']})"
        elif required_context and context < int(required_context):
            reason = (f"context too small ({context} < required {int(required_context)}: "
                      "prompt + output reserve)")
        elif context < int(preset.get("min_context", 0)):
            reason = f"context too small ({context} < {preset['min_context']})"
        elif preset.get("require_tools") and not tools:
            reason = "no tool calling"
        if reason is None and floors:
            missing = next((f"{key}={_component(m, ranks, key)} < {value}"
                            for key, value in floors.items()
                            if (_component(m, ranks, key) or 0.0) < value
                            and _component(m, ranks, key) is not None), None)
            if missing:
                reason = f"below job score floor ({missing})"
        # Job latency/success floors need enough samples to mean anything;
        # below the minimum the model is judged on other criteria only.
        enough = int(observed.get("rt_requests") or 0) >= JOB_METRIC_MIN_SAMPLES
        if (reason is None and max_latency is not None and enough
                and observed.get("rt_latency_p95_ms") is not None
                and float(observed["rt_latency_p95_ms"]) > float(max_latency)):
            reason = (f"latency p95 {float(observed['rt_latency_p95_ms']):.0f}ms "
                      f"> {float(max_latency):.0f}ms")
        if (reason is None and min_success is not None and enough
                and observed.get("rt_success_rate") is not None
                and float(observed["rt_success_rate"]) < float(min_success) * 100):
            reason = (f"success rate {float(observed['rt_success_rate']):.1f}% "
                      f"< {float(min_success) * 100:.1f}%")
        if reason is None and (rem := state.cooldown_remaining(m.id)) > 0:
            reason = f"cooling down ({rem:.0f}s left)"
        if (reason is None and failures.get("enabled", True)
                and state.is_model_failed(m.id, failures)):
            reason = "model excluded after consecutive failures"
        if (reason is None and failures.get("enabled", True)
                and state.is_provider_failed(m.provider, failures)):
            reason = f"provider {m.provider} excluded after consecutive failures"
        if reason is None and not (state.balance_any(m.provider) or {}).get("ok", True):
            reason = "balance exhausted"
        if reason:
            rejected.append({"id": m.id, "reason": reason})
        else:
            chosen.append((score(m, preset, ranks, job),
                           blended_cost(cost_in, cost_out), m))
    # highest rank first; ties -> cheaper first
    chosen.sort(key=lambda t: (-t[0], t[1]))
    return [m for _, _, m in chosen], rejected


def largest_context(catalog: Catalog, ranks: Ranks,
                    models: dict[str, Model] | None = None) -> dict:
    source = models if models is not None else catalog.models
    best: tuple[int, str | None] = (0, None)
    for model in source.values():
        context = effective(model, ranks)[2]
        if context > best[0]:
            best = (context, model.id)
    return {"context": best[0], "model": best[1]}


def rank_for_request(catalog: Catalog, preset_name: str, ranks: Ranks, state: State,
                     models: dict[str, Model] | None, body: Mapping | None, *,
                     job: Mapping | None = None,
                     metrics: Mapping[str, Mapping] | None = None,
                     allow_escalation: bool = True) -> dict:
    """Context-aware ranking with preset escalation.

    If the requested preset has no model with enough context, presets are tried
    in ascending cost order (never falling back to a smaller context window).
    """
    requirement = context_requirement(catalog.presets[preset_name], catalog.policy, body)
    candidates, rejected = rank_candidates(
        catalog, preset_name, ranks, state, models,
        required_context=requirement["required_context"], job=job, metrics=metrics)
    result = {
        "preset": preset_name,
        "requested_preset": preset_name,
        "escalated": False,
        "candidates": candidates,
        "rejected": rejected,
        "context": requirement,
    }
    if candidates or not allow_escalation:
        return result
    # Escalation must never downgrade: only presets at least as expensive as
    # the requested one are considered, cheapest first.
    budget = float(catalog.presets[preset_name].get("max_cost") or 0.0)
    order = sorted(
        (name for name in catalog.presets
         if name != preset_name
         and float(catalog.presets[name].get("max_cost") or 0.0) >= budget),
        key=lambda name: float(catalog.presets[name]["max_cost"]))
    for name in order:
        alt, alt_rejected = rank_candidates(
            catalog, name, ranks, state, models,
            required_context=requirement["required_context"], job=job, metrics=metrics)
        if alt:
            result.update(preset=name, escalated=True, candidates=alt, rejected=alt_rejected)
            return result
    return result


def context_error(catalog: Catalog, ranks: Ranks, preset_name: str,
                  requirement: Mapping, models: dict[str, Model] | None = None) -> dict:
    largest = largest_context(catalog, ranks, models)
    return {
        "error": {
            "message": (
                f"Request needs about {requirement['prompt_tokens']} prompt tokens plus a "
                f"{requirement['output_reserve']}-token output reserve "
                f"({requirement['required_context']} tokens required), but no eligible model "
                f"in preset '{preset_name}' fits; largest available context is "
                f"{largest['context']} tokens ({largest['model']})."
            ),
            "type": "context_error",
            "code": "context_too_large",
            "param": "messages",
            "context": dict(requirement),
            "largest_context": largest,
            "fix": (
                "Reduce attachments/history, lower max_tokens, enable compaction with a "
                "larger-context preset, or raise the preset's max_cost so larger-context "
                "models become eligible."
            ),
        }
    }


def preview_job(catalog: Catalog, ranks: Ranks, state: State,
                models: dict[str, Model] | None, job: Mapping,
                metrics: Mapping[str, Mapping] | None = None,
                body: Mapping | None = None) -> dict:
    preset_name = job.get("base_preset") or "medium"
    requirement = context_requirement(catalog.presets[preset_name], catalog.policy, body)
    candidates, rejected = rank_candidates(
        catalog, preset_name, ranks, state, models,
        required_context=requirement["required_context"], job=job, metrics=metrics)
    eligible = []
    for model in candidates[:25]:
        cost_in, cost_out, context, tools = effective(model, ranks)
        observed = (metrics or {}).get(model.id) or {}
        eligible.append({
            "id": model.id, "provider": model.provider,
            "score": round(score(model, catalog.presets[preset_name], ranks, job), 2),
            "cost_blended": round(blended_cost(cost_in, cost_out), 4),
            "cost_in": cost_in, "cost_out": cost_out, "context": context,
            "tools": tools, "metrics": observed,
        })
    return {
        "base_preset": preset_name,
        "context": requirement,
        "eligible": eligible,
        "rejected": rejected[:100],
        "would_route": eligible[0]["id"] if eligible else None,
        "on_no_match": job.get("on_no_match", "error"),
        "ok": bool(eligible) or job.get("on_no_match") != "error",
    }


def preset_estimate(catalog: Catalog, preset_name: str, ranks: Ranks, state: State,
                    models: dict[str, Model] | None = None, *,
                    job: Mapping | None = None,
                    metrics: Mapping[str, Mapping] | None = None) -> dict:
    """Approximate cost of the preset: what the top eligible model costs per 1M tokens."""
    preset = catalog.presets[preset_name]
    candidates, _ = rank_candidates(catalog, preset_name, ranks, state, models,
                                    job=job, metrics=metrics)
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
