"""Per-provider balance/capacity checks, cached in state (default 1h).

Types:
  none       - always ok
  budget     - no real API: local daily token cap from our own usage accounting
  rate_only  - local daily request cap (providers with undisclosed free quotas)
  openrouter - GET {base}/auth/key -> limit_remaining (credits); fail-open on error
  deepseek   - GET {base}/user/balance -> sum(total_balance) >= min_balance; fail-open on error
"""
from __future__ import annotations

import httpx

from catalog import Provider
from state import State


async def refresh_balance(prov: Provider, state: State, client: httpx.AsyncClient, max_age: float) -> dict:
    cached = state.balance_cached(prov.id, max_age)
    if cached:
        return cached
    bal = prov.balance
    t = bal.get("type", "none")
    try:
        if t == "openrouter":
            r = await client.get(f"{prov.base_url}/auth/key", headers=prov.auth_headers(), timeout=15)
            r.raise_for_status()
            lr = (r.json().get("data") or {}).get("limit_remaining")
            ok = True if lr is None else float(lr) > 0.01
            detail = {"type": t, "limit_remaining": lr}
        elif t == "deepseek":
            r = await client.get(f"{prov.base_url}/user/balance", headers=prov.auth_headers(), timeout=15)
            r.raise_for_status()
            infos = (r.json().get("balance_infos")) or []
            total = sum(float(i.get("total_balance", 0)) for i in infos)
            ok = total >= float(bal.get("min_balance", 0))
            detail = {"type": t, "total_balance": total}
        else:
            return {"type": t, "ok": True}
    except Exception as exc:  # fail-open; availability is enforced by cooldowns
        old = state.balance_any(prov.id)
        if old is not None:
            return old
        return {"type": t, "ok": True, "assumed": True, "error": str(exc)[:200]}
    await state.set_balance(prov.id, ok, detail)
    return {"ok": ok, **detail}


async def balance_ok(prov: Provider, state: State, client: httpx.AsyncClient, max_age: float) -> tuple[bool, dict]:
    bal = prov.balance
    t = bal.get("type", "none")
    if t == "none":
        return True, {"type": t}
    if t == "budget":
        u = state.usage_today(prov.id)
        cap = bal.get("daily_tokens")
        ok = cap is None or (u["in"] + u["out"]) < cap
        cost_cap = bal.get("daily_cost_usd")
        if cost_cap is not None:
            ok = ok and u.get("cost", 0.0) < float(cost_cap)
        return ok, {"type": t, "used": u["in"] + u["out"], "cap": cap,
                    "cost_today": u.get("cost", 0.0), "cost_cap": cost_cap}
    if t == "rate_only":
        u = state.usage_today(prov.id)
        cap = bal.get("daily_requests")
        ok = cap is None or u["reqs"] < cap
        return ok, {"type": t, "used": u["reqs"], "cap": cap}
    d = await refresh_balance(prov, state, client, max_age)
    return bool(d.get("ok", True)), d
