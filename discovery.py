"""Provider model discovery: GET {base}/models for every enabled provider.

Writes state/discovered.json. Discovery only records membership + safe defaults;
refresh.py enriches prices/limits and matches ranks. Catalog entries always win
on id conflict; `blocklist` in catalog.yaml vetoes discovered ids.

Safety default: providers without models.dev coverage get `default_cost_*` from
their catalog `discovery` block (e.g. openrouter unknown price -> 999 $/1M,
which no preset ceiling admits, so unknown-price models never route by accident).
"""
from __future__ import annotations

import datetime
import json
import pathlib
import tempfile

import httpx

from catalog import Catalog, Model

DEFAULT_EXCLUDES = ["embed", "whisper", "tts", "moderation", "rerank",
                    "dall-e", "stable-diffusion", "flux", "audio"]


def fetch_model_ids(client: httpx.Client, prov) -> list[str]:
    url = prov.models_url or f"{prov.base_url}/models"
    r = client.get(url, headers=prov.auth_headers(), timeout=25)
    r.raise_for_status()
    payload = r.json()
    items = payload.get("data") if isinstance(payload, dict) else payload
    ids = []
    for it in items or []:
        mid = it.get("id") if isinstance(it, dict) else it
        if isinstance(mid, str) and mid:
            ids.append(mid)
    return sorted(set(ids))


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
            ids = fetch_model_ids(client, prov)
        except Exception as exc:
            providers_out[pid] = {"ok": False, "error": str(exc)[:200],
                                  "kept_previous": True}
            continue
        # drop this provider's previous entries, then re-add fresh ones
        discovered = {mid: m for mid, m in discovered.items()
                      if isinstance(m, dict) and m.get("provider") != pid}
        dconf = prov.discovery or {}
        added = 0
        for mid in ids:
            low = mid.lower()
            if any(x in low for x in excludes):
                continue
            rid = f"{pid}/{mid}"
            if rid in catalog.models:  # curated entry wins
                continue
            discovered[rid] = {
                "id": rid, "provider": pid, "upstream": mid,
                "cost_in": float(dconf.get("default_cost_in", 999)),
                "cost_out": float(dconf.get("default_cost_out", 999)),
                "context": int(dconf.get("default_context", 32768)),
                "output": int(dconf.get("default_output", 8192)),
                "tools": bool(dconf.get("default_tools", True)),
                "seed_rank": seed,
                "source": "discovered",
            }
            added += 1
        providers_out[pid] = {"ok": True, "listed": len(ids), "used": added,
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
