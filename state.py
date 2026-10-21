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
                     "totals": {}, "by_preset": {}, "by_model": {}, "daily": {}}
        if self.file.exists():
            try:
                loaded = json.loads(self.file.read_text())
                self.data.update(loaded)
                for key in ("totals", "by_preset", "by_model"):
                    self.data.setdefault(key, {})
            except Exception:
                backup = self.path / f"state.json.broken-{int(time.time())}"
                self.file.replace(backup)

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

    # ---- usage accounting ------------------------------------------------
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
            agg = self.data["by_preset"].setdefault(preset, {"reqs": 0, "cost": 0.0})
            agg["reqs"] += 1
            agg["cost"] = round(agg.get("cost", 0.0) + cost_usd, 6)
        if model:
            agg = self.data["by_model"].setdefault(model, {"reqs": 0, "cost": 0.0})
            agg["reqs"] += 1
            agg["cost"] = round(agg.get("cost", 0.0) + cost_usd, 6)
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
        b = daily["buckets"].setdefault(minute, {"cost": 0.0, "reqs": 0})
        b["cost"] = round(b["cost"] + cost_usd, 6)
        b["reqs"] += 1
        if len(daily["buckets"]) > 720:  # cap at 12h of minutes
            for k in sorted(daily["buckets"])[: len(daily["buckets"]) - 720]:
                daily["buckets"].pop(k, None)
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
