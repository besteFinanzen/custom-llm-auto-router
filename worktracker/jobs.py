"""Job profiles: YAML defaults + database overrides.

A job describes the requirements of a class of work (``commit``, ``review``,
``orchestrator``, ...).  Orchestrators select it with ``x_oc.job`` in the body
or the ``x-oc-job`` header; the router then filters and scores the catalog
against the job's score floors, cost/context limits and observed latency.
"""
from __future__ import annotations

import pathlib
from collections.abc import Mapping, Sequence
from typing import Any

import yaml

from .attribution import extract

FIELDS = ("name", "base_preset", "description", "min_scores", "weights",
          "min_context", "max_cost", "require_tools", "max_latency_p95_ms",
          "min_success_rate", "on_no_match", "enabled")
ON_NO_MATCH = {"error", "fallback", "cheapest"}


def load_defaults(config_dir: str | pathlib.Path) -> dict[str, dict]:
    path = pathlib.Path(config_dir) / "jobs.yaml"
    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text()) or {}
    jobs = data.get("jobs") if isinstance(data, dict) else None
    return {str(k): dict(v or {}) for k, v in (jobs or {}).items()}


def validate(payload: Mapping, presets: Mapping[str, Any] | set[str]) -> dict:
    name = str(payload.get("name") or "").strip()
    if not name:
        raise ValueError("job name is required")
    base = str(payload.get("base_preset") or "medium").strip()
    known = set(presets.keys()) if isinstance(presets, Mapping) else set(presets)
    if known and base not in known:
        raise ValueError(f"base_preset '{base}' is not a configured preset ({', '.join(sorted(known))})")
    min_scores = payload.get("min_scores") or {}
    weights = payload.get("weights") or {}
    if not isinstance(min_scores, Mapping):
        raise ValueError("min_scores must be an object of component -> minimum score")
    if not isinstance(weights, Mapping):
        raise ValueError("weights must be an object of component -> non-negative weight")
    clean_scores: dict[str, float] = {}
    for key, value in min_scores.items():
        try:
            score = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"min_scores.{key} must be a number") from exc
        if not 0 <= score <= 100:
            raise ValueError(f"min_scores.{key} must be between 0 and 100")
        clean_scores[str(key)] = score
    clean_weights: dict[str, float] = {}
    for key, value in weights.items():
        try:
            weight = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"weights.{key} must be a number") from exc
        if weight < 0:
            raise ValueError(f"weights.{key} must be >= 0")
        clean_weights[str(key)] = weight
    on_no_match = str(payload.get("on_no_match") or "error")
    if on_no_match not in ON_NO_MATCH:
        raise ValueError("on_no_match must be error, fallback or cheapest")
    max_cost = payload.get("max_cost")
    if max_cost is not None and float(max_cost) < 0:
        raise ValueError("max_cost must be >= 0")
    max_latency = payload.get("max_latency_p95_ms")
    if max_latency is not None and float(max_latency) <= 0:
        raise ValueError("max_latency_p95_ms must be > 0")
    success = payload.get("min_success_rate")
    if success is not None and not 0 <= float(success) <= 1:
        raise ValueError("min_success_rate must be between 0 and 1 (a ratio)")
    min_context = payload.get("min_context")
    if min_context is not None and int(min_context) < 0:
        raise ValueError("min_context must be >= 0")
    return {
        "name": name, "base_preset": base,
        "description": str(payload.get("description") or "")[:1000],
        "min_scores": clean_scores, "weights": clean_weights,
        "min_context": int(min_context) if min_context is not None else None,
        "max_cost": float(max_cost) if max_cost is not None else None,
        "require_tools": (bool(payload["require_tools"])
                          if payload.get("require_tools") is not None else None),
        "max_latency_p95_ms": (int(max_latency) if max_latency is not None else None),
        "min_success_rate": float(success) if success is not None else None,
        "on_no_match": on_no_match,
        "enabled": bool(payload.get("enabled", True)),
    }


def merge(defaults: Mapping[str, Mapping], overrides: Sequence[Mapping]) -> dict[str, dict]:
    out: dict[str, dict] = {str(k): {**dict(v), "name": str(k), "source": "yaml"}
                            for k, v in defaults.items()}
    for row in overrides:
        name = str(row.get("name") or "").strip()
        if not name:
            continue
        base = out.get(name, {})
        out[name] = {**base, **{k: v for k, v in dict(row).items() if v is not None},
                     "name": name, "source": "db"}
    return out


def requested_job(headers: Mapping[str, str], body: Any) -> str | None:
    meta = extract(headers, body)
    value = meta.get("job")
    return str(value).strip() or None if value else None


def export(profiles: Mapping[str, Mapping]) -> dict:
    return {"version": 1, "jobs": {
        name: {key: value for key, value in profile.items()
               if key not in {"source", "updated_at"} and key in FIELDS}
        for name, profile in profiles.items()}}


def import_profiles(payload: Mapping, presets: Mapping[str, Any] | set[str]
                    ) -> list[dict]:
    jobs = payload.get("jobs") if isinstance(payload, Mapping) else None
    if not isinstance(jobs, Mapping):
        raise ValueError("import payload must contain a jobs object")
    out = []
    for name, raw in jobs.items():
        out.append(validate({**dict(raw or {}), "name": name}, presets))
    return out
