"""Persistent runtime state: cooldowns, usage accounting, balance cache.

JSON file, atomic writes, one async lock. Small data volumes => simplicity over speed.
"""
from __future__ import annotations

import asyncio
import datetime
import json
import pathlib
import tempfile
import time

USAGE_KEEP_DAYS = 3


class State:
    def __init__(self, path: pathlib.Path):
        self.path = pathlib.Path(path)
        self.path.mkdir(parents=True, exist_ok=True)
        self.file = self.path / "state.json"
        self.lock = asyncio.Lock()
        self.data = {"cooldowns": {}, "usage": {}, "balances": {},
                     "totals": {}, "by_preset": {}, "by_model": {}, "daily": {},
                     "daily_history": {},
                     # consecutive failure tracking + exponential backoff (v2 schema)
                     "failures": {"models": {}, "providers": {}},
                     "backoff": {}}
        if self.file.exists():
            try:
                loaded = json.loads(self.file.read_text())
                self.data.update(loaded)
            except Exception:
                backup = self.path / f"state.json.broken-{int(time.time())}"
                self.file.replace(backup)
        for key in ("totals", "by_preset", "by_model", "daily_history"):
            self.data.setdefault(key, {})
        # Ensure failure-tracking schema defaults exist
        failures = self.data.setdefault("failures", {"models": {}, "providers": {}})
        failures.setdefault("models", {})
        failures.setdefault("providers", {})
        self.data.setdefault("backoff", {})
        self._backfill_history()

    def _backfill_history(self) -> None:
        """Seed the long-lived per-day token history from the pruned usage buckets.

        `usage` only keeps a few days; `daily_history` is the durable series that
        feeds the tokens-over-time diagram. Days already recorded are left alone.
        """
        usage = self.data.get("usage") or {}
        for day, providers in usage.items():
            if day in self.data["daily_history"]:
                continue
            agg = {"in": 0, "out": 0, "reqs": 0, "cost": 0.0}
            for u in (providers or {}).values():
                agg["in"] += u.get("in", 0)
                agg["out"] += u.get("out", 0)
                agg["reqs"] += u.get("reqs", 0)
                agg["cost"] = round(agg["cost"] + u.get("cost", 0.0), 6)
            self.data["daily_history"][day] = agg

    async def _save(self) -> None:
        async with self.lock:
            fd, tmp = tempfile.mkstemp(dir=self.path, prefix=".state-", suffix=".tmp")
            with open(fd, "w") as f:
                json.dump(self.data, f)
            pathlib.Path(tmp).replace(self.file)

    # ---- cooldowns -----------------------------------------------------
    def cooldown_remaining(self, model_id: str) -> float:
        until = self.data["cooldowns"].get(model_id, 0)
        return max(0.0, until - time.time())

    async def set_cooldown(self, model_id: str, seconds: float) -> None:
        self.data["cooldowns"][model_id] = time.time() + seconds
        await self._save()

    async def clear_cooldown(self, model_id: str) -> None:
        if model_id in self.data["cooldowns"]:
            self.data["cooldowns"].pop(model_id, None)
            await self._save()

    # ---- failure tracking (consecutive failures + exponential backoff) ----
    def _failures(self) -> dict:
        """Normalized failures store with guaranteed models/providers dicts."""
        failures = self.data.setdefault("failures", {"models": {}, "providers": {}})
        failures.setdefault("models", {})
        failures.setdefault("providers", {})
        return failures

    def get_model_failures(self, model_id: str) -> dict:
        """Get consecutive failure info for a model.

        Returns {
            "count": int,
            "first_failure": float,   # epoch seconds
            "last_failure": float,    # epoch seconds (falls back to first_failure)
            "last_error": str,
            "status": int,
            "provider": str,
        }
        """
        failures = self._failures()
        entry = failures.get("models", {}).get(model_id) or {}
        return {
            "count": int(entry.get("count") or 0),
            "first_failure": float(entry.get("first_failure") or 0.0),
            "last_failure": float(entry.get("last_failure") or entry.get("first_failure") or 0.0),
            "last_error": str(entry.get("last_error") or ""),
            "status": int(entry.get("status") or 0),
            "provider": str(entry.get("provider") or ""),
        }

    def get_provider_failures(self, provider_id: str) -> dict:
        """Get consecutive failure info for a provider."""
        failures = self._failures()
        entry = failures.get("providers", {}).get(provider_id) or {}
        return {
            "count": int(entry.get("count") or 0),
            "first_failure": float(entry.get("first_failure") or 0.0),
            "last_failure": float(entry.get("last_failure") or entry.get("first_failure") or 0.0),
            "last_error": str(entry.get("last_error") or ""),
            "status": int(entry.get("status") or 0),
        }

    def _bump(self, scope: str, key: str, error: str, status: int, provider: str = "") -> dict:
        """Increment failure count for (scope,key); returns the updated entry."""
        failures = self._failures()
        scope_map = failures.get("models" if scope == "models" else "providers")
        scope_map.setdefault(key, {})
        now = time.time()
        entry = scope_map[key]
        entry["count"] = int(entry.get("count") or 0) + 1
        entry.setdefault("first_failure", now)
        entry["last_failure"] = now
        entry["last_error"] = str(error)[:300]
        entry["status"] = status or 0
        if provider:
            entry["provider"] = provider
        return {
            "count": entry["count"],
            "first_failure": entry["first_failure"],
            "last_error": entry["last_error"],
            "status": entry["status"],
            "provider": entry.get("provider"),
        }

    async def record_model_failure(self, model_id: str, provider: str, error: str, status: int = 0) -> dict:
        """Record consecutive failure for a model; also bump the provider-level counter."""
        mf = self._bump("models", model_id, error, status, provider)
        self._bump("providers", provider, error, status)
        await self._save()
        return mf

    async def record_provider_failure(self, provider_id: str, error: str, status: int = 0) -> dict:
        """Record consecutive failure for a provider (any model attempt)."""
        pf = self._bump("providers", provider_id, error, status)
        await self._save()
        return pf

    async def record_success(self, model_id: str, provider: str) -> None:
        """Reset consecutive failure counters for a model (and its provider) after a success."""
        failures = self._failures()
        changed = False
        models_map = failures.get("models", {})
        if model_id in models_map:
            models_map.pop(model_id, None)
            changed = True
        providers_map = failures.get("providers", {})
        if provider in providers_map:
            providers_map.pop(provider, None)
            changed = True
        # Also clear backoff for this model
        backoff = self.data.get("backoff")
        if backoff is not None and model_id in backoff:
            backoff.pop(model_id, None)
            changed = True
        if changed:
            await self._save()

    async def reset_failures(self) -> dict:
        """Clear all failure counters, backoff entries and cooldowns (operator reset).

        Usage/balance accounting is intentionally left untouched.
        """
        failures = self._failures()
        cleared = {
            "models": len(failures["models"]),
            "providers": len(failures["providers"]),
            "backoff": len(self.data.get("backoff") or {}),
            "cooldowns": len(self.data.get("cooldowns") or {}),
        }
        failures["models"].clear()
        failures["providers"].clear()
        self.data.setdefault("backoff", {}).clear()
        self.data.setdefault("cooldowns", {}).clear()
        await self._save()
        return cleared

    def is_model_failed(self, model_id: str, thresholds: dict) -> bool:
        """Check if a model has exceeded its consecutive failure threshold.

        `thresholds` is the failures policy dict (from catalog.yaml). A positive
        `model_window_seconds` forgives failures that stopped more than that long
        ago; the stale entry is dropped so the next failure starts a fresh count.
        """
        policy = thresholds or {}
        if not policy.get("enabled", True):
            return False
        model_threshold = int(policy.get("model_threshold", 3))
        window = float(policy.get("model_window_seconds") or 0)
        entry = self.get_model_failures(model_id)
        if window > 0 and entry["count"] and (time.time() - entry["last_failure"]) > window:
            self._failures()["models"].pop(model_id, None)
            return False
        return entry["count"] >= model_threshold

    def is_provider_failed(self, provider_id: str, thresholds: dict) -> bool:
        """Check if a provider has exceeded its consecutive failure threshold.

        `thresholds` is the failures policy dict (from catalog.yaml). A positive
        `provider_window_seconds` forgives failures that stopped more than that
        long ago; the stale entry is dropped so the next failure starts fresh.
        """
        policy = thresholds or {}
        if not policy.get("enabled", True):
            return False
        provider_threshold = int(policy.get("provider_threshold", 5))
        window = float(policy.get("provider_window_seconds") or 0)
        entry = self.get_provider_failures(provider_id)
        if window > 0 and entry["count"] and (time.time() - entry["last_failure"]) > window:
            self._failures()["providers"].pop(provider_id, None)
            return False
        return entry["count"] >= provider_threshold

    # ---- exponential backoff ------------------------------------------------
    def set_backoff(self, model_id: str, seconds: float, multiplier: float = 2.0, provider: str = "") -> None:
        """Schedule exponential backoff: cooldown = min(seconds * multiplier^n, cap)."""
        backoff = self.data.setdefault("backoff", {})
        backoff[model_id] = {
            "until": time.time() + seconds,
            "base": seconds,
            "mult": multiplier,
            "ts": time.time(),
        }

    def backoff_until(self, model_id: str) -> float:
        """Get the absolute timestamp when backoff/cooldown for this model should end."""
        backoff = self.data.get("backoff", {})
        if model_id in backoff:
            return float(backoff[model_id].get("until") or 0.0)
        return float(self.cooldown_remaining(model_id))

    def clear_backoff(self, model_id: str) -> None:
        """Clear backoff state for a model (e.g., after success)."""
        backoff = self.data.get("backoff")
        if backoff is not None and model_id in backoff:
            backoff.pop(model_id, None)

    # ---- usage accounting -------------------------------------------------
    def _today(self) -> str:
        return datetime.date.today().isoformat()

    def usage_today(self, provider: str) -> dict:
        day = self.data["usage"].get(self._today(), {}).get(provider)
        return day or {"in": 0, "out": 0, "reqs": 0, "cost": 0.0}

    async def add_usage(self, provider: str, tokens_in: int, tokens_out: int,
                        cost_usd: float = 0.0, preset: str | None = None,
                        model: str | None = None, session: str | None = None,
                        kind: str | None = None) -> None:
        usage = self.data["usage"]
        for d in list(usage):  # prune old day buckets
            if d < (datetime.date.today() - datetime.timedelta(days=USAGE_KEEP_DAYS)).isoformat():
                usage.pop(d, None)
        day = usage.setdefault(self._today(), {}).setdefault(provider, {"in": 0, "out": 0, "reqs": 0, "cost": 0.0})
        day["in"] += tokens_in
        day["out"] += tokens_out
        day["reqs"] += 1
        day["cost"] = round(day.get("cost", 0.0) + cost_usd, 6)
        # lifetime aggregates (never pruned)
        tot = self.data["totals"].setdefault(provider, {"in": 0, "out": 0, "reqs": 0, "cost": 0.0})
        tot["in"] += tokens_in
        tot["out"] += tokens_out
        tot["reqs"] += 1
        tot["cost"] = round(tot.get("cost", 0.0) + cost_usd, 6)
        if preset:
            agg = self.data["by_preset"].setdefault(
                preset, {"reqs": 0, "cost": 0.0, "in": 0, "out": 0})
            agg["reqs"] += 1
            agg["cost"] = round(agg.get("cost", 0.0) + cost_usd, 6)
            # These token fields are forward-only; older aggregates predate them.
            agg["in"] = int(agg.get("in") or 0) + tokens_in
            agg["out"] = int(agg.get("out") or 0) + tokens_out
        if model:
            agg = self.data["by_model"].setdefault(
                model, {"reqs": 0, "cost": 0.0, "in": 0, "out": 0})
            agg["reqs"] += 1
            agg["cost"] = round(agg.get("cost", 0.0) + cost_usd, 6)
            agg["in"] = int(agg.get("in") or 0) + tokens_in
            agg["out"] = int(agg.get("out") or 0) + tokens_out
        daily = self.data.get("daily")
        if not daily or daily.get("date") != self._today():
            daily = {"date": self._today(), "by_preset": {}, "by_model": {},
                     "by_session": {}, "buckets": {}}
            self.data["daily"] = daily
        daily.setdefault("by_session", {})
        daily.setdefault("buckets", {})
        for key, bucket in (("by_preset", preset), ("by_model", model)):
            if not bucket:
                continue
            agg = daily[key].setdefault(bucket, {"reqs": 0, "cost": 0.0})
            agg["reqs"] += 1
            agg["cost"] = round(agg.get("cost", 0.0) + cost_usd, 6)
        # per-session aggregation (today) — enables grouping in the dashboard
        s = daily["by_session"].setdefault(
            session or "(unknown)",
            {"reqs": 0, "cost": 0.0, "in": 0, "out": 0, "last_ts": 0.0,
             "last_model": None, "kinds": {}})
        s["reqs"] += 1
        s["cost"] = round(s.get("cost", 0.0) + cost_usd, 6)
        s["in"] += tokens_in
        s["out"] += tokens_out
        s["last_ts"] = time.time()
        if model:
            s["last_model"] = model
        if kind:
            s["kinds"][kind] = s["kinds"].get(kind, 0) + 1
        # per-minute spend buckets — drives the live chart
        minute = datetime.datetime.now().strftime("%H:%M")
        b = daily["buckets"].setdefault(minute, {"cost": 0.0, "reqs": 0, "in": 0, "out": 0})
        b["cost"] = round(b["cost"] + cost_usd, 6)
        b["reqs"] += 1
        b["in"] = b.get("in", 0) + tokens_in
        b["out"] = b.get("out", 0) + tokens_out
        b["tok"] = True
        if len(daily["buckets"]) > 720:  # cap at 12h of minutes
            for k in sorted(daily["buckets"])[: len(daily["buckets"]) - 720]:
                daily["buckets"].pop(k, None)
        # long-lived per-day token history — drives the tokens-over-time diagram
        hist = self.data["daily_history"].setdefault(
            self._today(), {"in": 0, "out": 0, "reqs": 0, "cost": 0.0})
        hist["in"] += tokens_in
        hist["out"] += tokens_out
        hist["reqs"] += 1
        hist["cost"] = round(hist.get("cost", 0.0) + cost_usd, 6)
        # `daily_history` is deliberately unlimited; PostgreSQL stores per-request
        # detail while this compact JSON series remains a local fallback.
        await self._save()

    # ---- balance cache ----------------------------------------------------
    def balance_cached(self, provider: str, max_age_s: float) -> dict | None:
        b = self.data["balances"].get(provider)
        if b and (time.time() - b.get("ts", 0)) < max_age_s:
            return b
        return None

    def balance_any(self, provider: str) -> dict | None:
        return self.data["balances"].get(provider)

    async def set_balance(self, provider: str, ok: bool, detail: dict) -> None:
        self.data["balances"][provider] = {"ts": time.time(), "ok": ok, **detail}
        await self._save()
