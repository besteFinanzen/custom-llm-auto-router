"""Provider model discovery: GET {base}/models for every enabled provider.

Writes state/discovered.json. Discovery records provider-published prices,
limits and capabilities when available; refresh.py enriches the rest and matches
ranks. Catalog entries always win on id conflict; `blocklist` in catalog.yaml
vetoes discovered ids.

Safety default: providers without models.dev coverage get `default_cost_*` from
their catalog `discovery` block (e.g. openrouter unknown price -> 999 $/1M,
which no preset ceiling admits, so unknown-price models never route by accident).
"""
from __future__ import annotations

import datetime
import json
import math
import pathlib
import tempfile

import httpx

from catalog import Catalog, Model

DEFAULT_EXCLUDES = ["embed", "whisper", "tts", "moderation", "rerank",
                    "dall-e", "stable-diffusion", "flux", "audio"]


def _dig(obj, *path):
    for key in path:
        if not isinstance(obj, dict):
            return None
        obj = obj.get(key)
    return obj


def _number(value) -> float | None:
    """Parse a non-negative finite number, ignoring booleans."""
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number >= 0 else None


def _metadata(item: dict, dconf: dict) -> dict:
    """Extract pricing/limits/capabilities from an OpenAI-style model record.

    Providers expose this differently. Missing or invalid prices deliberately
    fall back to the provider's `default_cost_*` safety value, not zero.
    """
    cost = item.get("cost") or {}
    cost_in = _number(_dig(cost, "text", "input", "price"))
    cost_out = _number(_dig(cost, "text", "output", "price"))
    if cost_in is None:
        raw = _dig(cost, "input")
        cost_in = _number(raw.get("price") if isinstance(raw, dict) else raw)
    if cost_out is None:
        raw = _dig(cost, "output")
        cost_out = _number(raw.get("price") if isinstance(raw, dict) else raw)

    # OpenRouter-style /models reports USD per token, not per million.
    if cost_in is None or cost_out is None:
        prompt = _number(_dig(item, "pricing", "prompt"))
        completion = _number(_dig(item, "pricing", "completion"))
        if cost_in is None and prompt is not None:
            cost_in = prompt * 1_000_000
        if cost_out is None and completion is not None:
            cost_out = completion * 1_000_000

    context = _number(_dig(item, "limit", "context"))
    if context is None:
        context = _number(item.get("context_length") or item.get("context_window")
                          or item.get("max_context_length"))
    output = _number(_dig(item, "limit", "output"))
    if output is None:
        output = _number(item.get("max_output_tokens") or item.get("max_completion_tokens"))

    tools = _dig(item, "capabilities", "tools", "supported")
    if not isinstance(tools, bool):
        tools = item.get("tool_call") if isinstance(item.get("tool_call"), bool) else item.get("tools")
    if not isinstance(tools, bool):
        tools = None

    modalities = _dig(item, "modalities", "input")
    image = None
    if isinstance(modalities, list):
        image = any(str(m).lower() == "image" or str(m).lower().startswith("image/")
                    for m in modalities)

    return {
        "cost_in": cost_in if cost_in is not None else float(dconf.get("default_cost_in", 999)),
        "cost_out": cost_out if cost_out is not None else float(dconf.get("default_cost_out", 999)),
        "context": int(context) if context is not None else int(dconf.get("default_context", 32768)),
        "output": int(output) if output is not None else int(dconf.get("default_output", 8192)),
        "tools": tools if tools is not None else bool(dconf.get("default_tools", True)),
        "image": image if image is not None else bool(dconf.get("default_image", False)),
    }


def fetch_model_items(client: httpx.Client, prov) -> list[dict]:
    """Fetch the provider's model records, preserving metadata when supplied."""
    url = prov.models_url or f"{prov.base_url}/models"
    r = client.get(url, headers=prov.auth_headers(), timeout=25)
    r.raise_for_status()
    payload = r.json()
    items = payload.get("data") if isinstance(payload, dict) else payload
    out = []
    for it in items or []:
        if isinstance(it, dict) and isinstance(it.get("id"), str) and it["id"]:
            out.append(it)
        elif isinstance(it, str) and it:
            out.append({"id": it})
    return out


def fetch_model_ids(client: httpx.Client, prov) -> list[str]:
    return sorted({item["id"] for item in fetch_model_items(client, prov)})


def sync_discovered(catalog: Catalog, state_dir: pathlib.Path,
                    client: httpx.Client) -> dict:
    """Fetch /models for enabled providers; merge per provider (failures keep old)."""
    pol = (catalog.policy.get("discovery") or {})
    excludes = [p.lower() for p in pol.get("exclude_patterns", DEFAULT_EXCLUDES)]
    seed = float(pol.get("seed_rank", 30))
    path = state_dir / "discovered.json"
    old = {"models": {}, "providers": {}}
    if path.exists():
        try:
            old = json.loads(path.read_text())
        except Exception:
            pass
    old_models = old.get("models") or {}
    providers_out = dict(old.get("providers") or {})

    discovered: dict[str, dict] = dict(old_models)  # start from previous snapshot

    for pid, prov in catalog.providers.items():
        if not prov.enabled:
            continue
        try:
            items = fetch_model_items(client, prov)
        except Exception as exc:
            providers_out[pid] = {"ok": False, "error": str(exc)[:200],
                                  "kept_previous": True}
            continue
        # drop this provider's previous entries, then re-add fresh ones
        discovered = {mid: m for mid, m in discovered.items()
                      if isinstance(m, dict) and m.get("provider") != pid}
        dconf = prov.discovery or {}
        by_id = {item["id"]: item for item in items}
        added = 0
        for mid in sorted(by_id):
            low = mid.lower()
            if any(x in low for x in excludes):
                continue
            rid = f"{pid}/{mid}"
            if rid in catalog.models:  # curated entry wins
                continue
            discovered[rid] = {
                "id": rid, "provider": pid, "upstream": mid,
                **_metadata(by_id[mid], dconf),
                "seed_rank": seed,
                "source": "discovered",
            }
            added += 1
        providers_out[pid] = {"ok": True, "listed": len(by_id), "used": added,
                              "fetched_at": datetime.datetime.now(datetime.timezone.utc).isoformat()}

    out = {"version": 1,
           "fetched_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
           "models": discovered, "providers": providers_out}
    fd, tmp = tempfile.mkstemp(dir=state_dir, prefix=".disc-", suffix=".tmp")
    with open(fd, "w") as f:
        json.dump(out, f)
    pathlib.Path(tmp).replace(path)
    return out


def load_discovered(state_dir: pathlib.Path,
                    catalog: Catalog | None = None) -> dict[str, Model]:
    """Load discovered models.

    When `catalog` is provided, drop entries that cannot be routed anyway:
    disabled providers, and duplicates of a curated model (same provider +
    upstream, e.g. curated `e-infra/kimi` vs discovered `e_infra/kimi`). Without
    this, duplicate entries can burn every failover attempt on one provider.
    """
    path = state_dir / "discovered.json"
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
    except Exception:
        return {}
    models = {mid: Model(cfg) for mid, cfg in (data.get("models") or {}).items()}
    if catalog is None:
        return models
    curated = {(m.provider, m.upstream) for m in catalog.models.values()}
    out: dict[str, Model] = {}
    for mid, m in models.items():
        prov = catalog.providers.get(m.provider)
        if prov is None or not prov.enabled:
            continue
        if (m.provider, m.upstream) in curated:
            continue
        out[mid] = m
    return out
