"""Runtime configuration for the work tracker."""
from __future__ import annotations

import os
from dataclasses import dataclass
from zoneinfo import ZoneInfo


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class Settings:
    database_url: str
    timezone: str = "Europe/Berlin"
    admin_token: str = ""
    editor_key: str = ""
    hook_token: str = ""
    read_token: str = ""
    attribution_mode: str = "warn"
    idle_timeout_s: int = 15 * 60
    heartbeat_interval_s: int = 2 * 60
    presence_ttl_s: int = 5 * 60
    retention_days: int = 0
    redact_paths: bool = True
    budget_fail_closed: bool = False
    budget_cache_s: int = 5
    public_url: str = ""
    router_api_key: str = ""
    router_api_keys: str = ""

    @classmethod
    def from_env(cls) -> Settings:
        database_url = (
            os.environ.get("TRACKER_DATABASE_URL")
            or os.environ.get("DATABASE_URL")
            or os.environ.get("ROUTER_DATABASE_URL")
            or ""
        ).strip()
        mode = (os.environ.get("TRACKER_ATTRIBUTION_MODE") or "warn").strip().lower()
        if mode not in {"warn", "enforce", "off"}:
            mode = "warn"
        return cls(
            database_url=database_url,
            timezone=os.environ.get("TRACKER_TIMEZONE", "Europe/Berlin"),
            admin_token=os.environ.get("TRACKER_ADMIN_TOKEN", ""),
            editor_key=os.environ.get("TRACKER_EDITOR_KEY", ""),
            hook_token=os.environ.get("TRACKER_HOOK_TOKEN", ""),
            read_token=os.environ.get("TRACKER_READ_TOKEN", ""),
            attribution_mode=mode,
            idle_timeout_s=int(os.environ.get("TRACKER_IDLE_TIMEOUT_SECONDS", 900)),
            heartbeat_interval_s=int(os.environ.get("TRACKER_HEARTBEAT_SECONDS", 120)),
            presence_ttl_s=int(os.environ.get("TRACKER_PRESENCE_TTL_SECONDS", 300)),
            retention_days=int(os.environ.get("TRACKER_RETENTION_DAYS", 0)),
            redact_paths=_bool("TRACKER_REDACT_PATHS", True),
            budget_fail_closed=_bool("TRACKER_BUDGET_FAIL_CLOSED", False),
            budget_cache_s=int(os.environ.get("TRACKER_BUDGET_CACHE_SECONDS", 5)),
            public_url=(os.environ.get("TRACKER_PUBLIC_URL") or "").rstrip("/"),
            router_api_key=os.environ.get("ROUTER_API_KEY", ""),
            router_api_keys=os.environ.get("ROUTER_API_KEYS", ""),
        )

    @property
    def tz(self) -> ZoneInfo:
        try:
            return ZoneInfo(self.timezone)
        except Exception:
            return ZoneInfo("UTC")

    def tokens(self) -> dict[str, str]:
        """token -> scope. Empty values mean the scope is not configured."""
        out: dict[str, str] = {}
        for value, scope in (
            (self.admin_token, "admin"),
            (self.editor_key, "editor"),
            (self.hook_token, "hook"),
            (self.read_token, "read"),
        ):
            if value:
                out[value] = scope
        return out

    def router_tokens(self) -> dict[str, str]:
        """router API key -> "router" scope, from ROUTER_API_KEY / ROUTER_API_KEYS."""
        out: dict[str, str] = {}
        for value in (self.router_api_key, *(self.router_api_keys.split(",") if self.router_api_keys else [])):
            value = value.strip()
            if value:
                out[value] = "router"
        return out

    @property
    def router_auth_enabled(self) -> bool:
        """True if any router key is configured via env vars."""
        return bool(self.router_tokens())
