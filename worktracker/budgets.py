"""Budget evaluation, caching and HTTP client for split deployments."""
from __future__ import annotations

import threading
import time
from collections.abc import Callable

from .config import Settings
from .store import TrackerStore, utcnow


def payload(result: dict) -> dict:
    return {
        "error": {
            "message": "AI budget exhausted for this project/branch: "
                       + ", ".join(
                           f"{b['metric']} {b['used']:g}/{b['limit']:g} ({b['period']})"
                           for b in result.get("blocked_by") or []),
            "type": "budget_exceeded",
            "code": "budget_exceeded",
            "budgets": result.get("blocked_by") or [],
            "warnings": result.get("warnings") or [],
            "fix": "Raise or remove the cap in Worktracker > Budgets, or switch branch/project.",
        }
    }


class BudgetGate:
    """Short-lived cache around ``TrackerStore.budget_check``.

    A cache miss or database error fails open unless
    ``TRACKER_BUDGET_FAIL_CLOSED=1`` is set.
    """

    def __init__(self, store: TrackerStore, settings: Settings,
                 clock: Callable[[], float] = time.monotonic):
        self.store = store
        self.settings = settings
        self.clock = clock
        self._lock = threading.Lock()
        self._cache: dict[tuple[str, str | None], tuple[float, dict]] = {}

    def check(self, project_key: str, branch: str | None = None,
              use_cache: bool = True, reserve_usd: float = 0.0) -> dict:
        key = (project_key, branch)
        now = self.clock()
        if use_cache:
            with self._lock:
                cached = self._cache.get(key)
            if cached and cached[0] > now:
                return self._with_reserve(cached[1], reserve_usd)
        try:
            result = self.store.budget_check(project_key, branch)
        except Exception as exc:  # fail open by default, never block on a broken DB
            result = {
                "project_key": project_key, "branch": branch, "blocked": False,
                "blocked_by": [], "warnings": [], "budgets": [],
                "error": str(exc), "checked_at": utcnow().isoformat(),
            }
            if self.settings.budget_fail_closed:
                result["blocked"] = True
                result["blocked_by"] = [{
                    "id": "fail-closed", "metric": "cost", "used": 0, "limit": 0,
                    "period": "total", "reason": "budget backend unavailable",
                }]
        with self._lock:
            self._cache[key] = (now + max(1, self.settings.budget_cache_s), result)
        return self._with_reserve(result, reserve_usd)

    @staticmethod
    def _with_reserve(result: dict, reserve_usd: float) -> dict:
        """Add this request's estimated cost to cost budgets.

        The cache is only refreshed every ``budget_cache_s`` seconds, so two
        concurrent requests could otherwise both pass a cap they would exceed
        together. The reservation is per-request and never cached.
        """
        if reserve_usd <= 0:
            return result
        out = {**result}
        blocked_by: list[dict] = []
        for item in result.get("blocked_by") or []:
            blocked_by.append(item)
        if result.get("blocked"):
            out["blocked_by"] = blocked_by
            return out
        warnings = list(result.get("warnings") or [])
        for item in result.get("budgets") or []:
            if item.get("metric") not in ("cost", "cost_usd") or not item.get("limit"):
                continue
            projected = float(item.get("used") or 0) + reserve_usd
            pct = projected / float(item["limit"]) * 100
            hit = {
                "id": item.get("id"), "metric": "cost", "used": round(projected, 10),
                "limit": float(item["limit"]), "period": item.get("period"),
                "reason": "reserved estimate for this request",
            }
            if pct >= 100 and item.get("hard_block", True):
                blocked_by.append(hit)
            elif pct >= float(item.get("warn_pct") or 0) and not any(
                    w.get("id") == item.get("id") for w in warnings):
                warnings.append({**hit, "pct": pct})
        if blocked_by:
            out["blocked"] = True
            out["blocked_by"] = blocked_by
        out["warnings"] = warnings
        return out

    def invalidate(self, project_key: str | None = None) -> None:
        with self._lock:
            if project_key is None:
                self._cache.clear()
            else:
                for key in [k for k in self._cache if k[0] == project_key]:
                    self._cache.pop(key, None)


class RemoteBudgetClient:
    """HTTP budget check used by ``APP_MODE=router`` split deployments."""

    def __init__(self, base_url: str, token: str = "", timeout: float = 3.0,
                 fail_closed: bool = False):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout
        self.fail_closed = fail_closed

    def check(self, project_key: str, branch: str | None = None,
              use_cache: bool = True, reserve_usd: float = 0.0) -> dict:
        """Same signature as BudgetGate.check; the remote tracker evaluates caps.

        ``use_cache`` is accepted for interface compatibility (the remote side
        owns its own cache) and ``reserve_usd`` applies the same local
        per-request cost projection before the result is trusted.
        """
        import httpx

        headers = {"authorization": f"Bearer {self.token}"} if self.token else {}
        try:
            with httpx.Client(timeout=self.timeout) as client:
                response = client.get(
                    f"{self.base_url}/worktracker/api/budget/check",
                    params={"project_key": project_key, "branch": branch or "",
                            "reserve_usd": round(reserve_usd, 6)},
                    headers=headers)
                response.raise_for_status()
                return BudgetGate._with_reserve(response.json(), reserve_usd)
        except Exception as exc:
            blocked = bool(self.fail_closed)
            return {
                "project_key": project_key, "branch": branch, "blocked": blocked,
                "blocked_by": ([{"id": "fail-closed", "metric": "cost", "used": 0,
                                 "limit": 0, "period": "total"}] if blocked else []),
                "warnings": [], "budgets": [], "error": str(exc),
            }
