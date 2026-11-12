"""PostgreSQL storage for projects, heartbeats, commits, budgets and stats.

The store is synchronous on purpose (psycopg + a tiny RLock), matching the
existing router store.  FastAPI handlers call it through ``asyncio.to_thread``
so a slow query cannot block the event loop.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import threading
import urllib.parse
import uuid
from collections.abc import Collection, Iterable, Mapping
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from .config import Settings


def require(row: dict | None) -> dict:
    """Narrow a lookup result for the type checker.

    Callers that index the result of a ``SELECT``/``RETURNING`` query have
    already established the row exists (typically by raising ``KeyError`` right
    before); this keeps both the SQL and the intent visible in the code.
    """
    assert row is not None
    return row

INITIAL_COMMIT_WINDOW = dt.timedelta(hours=24)


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def parse_ts(value: Any) -> dt.datetime:
    if isinstance(value, dt.datetime):
        parsed = value
    elif value is None:
        parsed = utcnow()
    elif isinstance(value, (int, float)) or (
            isinstance(value, str) and value.strip().replace(".", "", 1).isdigit()):
        parsed = dt.datetime.fromtimestamp(float(value), dt.timezone.utc)
    else:
        parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed


def _iso(value: Any) -> Any:
    if isinstance(value, (dt.datetime, dt.date)):
        return value.isoformat()
    return value


def _clean_metadata(value: Any, depth: int = 0) -> Any:
    if depth > 3:
        return None
    if isinstance(value, Mapping):
        blocked = ("prompt", "response", "content", "message", "messages", "text")
        return {str(k)[:80]: _clean_metadata(v, depth + 1) for k, v in value.items()
                if not any(word in str(k).lower() for word in blocked)}
    if isinstance(value, (list, tuple)):
        return [_clean_metadata(v, depth + 1) for v in value[:50]]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value[:500] if isinstance(value, str) else value
    return str(value)[:500]


def normalize_remote(remote: str | None) -> str | None:
    """Normalise a git remote for stable project identity."""
    if not remote:
        return None
    raw = remote.strip()
    if not raw:
        return None
    if "://" in raw:
        parsed = urllib.parse.urlsplit(raw)
        host = (parsed.hostname or "").lower()
        path = parsed.path
    else:
        # scp-like: git@github.com:org/repo.git
        host_path = raw.split("@", 1)[-1]
        if ":" in host_path:
            host, path = host_path.split(":", 1)
        else:
            host, path = "", host_path
        host = host.lower()
    path = "/" + path.strip("/")
    if path.endswith(".git"):
        path = path[:-4]
    return f"{host}{path}" if host else path.strip("/")


def _slug(value: str) -> str:
    cleaned = "".join(ch.lower() if ch.isalnum() else "-" for ch in value)
    return "-".join(part for part in cleaned.split("-") if part)[:80] or "project"


def _merge_intervals(intervals: Iterable[tuple[float, float]]) -> list[tuple[float, float]]:
    ordered = sorted((a, b) for a, b in intervals if b > a)
    out: list[tuple[float, float]] = []
    for start, end in ordered:
        if not out or start > out[-1][1]:
            out.append((start, end))
        else:
            out[-1] = (out[-1][0], max(out[-1][1], end))
    return out


def _union_length(intervals: Iterable[tuple[float, float]]) -> float:
    return sum(end - start for start, end in _merge_intervals(intervals))


def _intersection_length(left: list[tuple[float, float]],
                         right: list[tuple[float, float]]) -> float:
    total = 0.0
    i = j = 0
    while i < len(left) and j < len(right):
        start = max(left[i][0], right[j][0])
        end = min(left[i][1], right[j][1])
        if end > start:
            total += end - start
        if left[i][1] < right[j][1]:
            i += 1
        else:
            j += 1
    return total


class TrackerStore:
    def __init__(self, url: str, settings: Settings | None = None):
        if not url:
            raise ValueError("TRACKER_DATABASE_URL / DATABASE_URL is empty")
        self.url = url
        self.settings = settings or Settings.from_env()
        self._lock = threading.RLock()
        self._conn = psycopg.connect(url, connect_timeout=8, autocommit=True,
                                     row_factory=dict_row)
        self._init_schema()
        self.sync_projects_from_usage()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ------------------------------------------------------------------ schema
    def _init_schema(self) -> None:
        statements = [
            """CREATE TABLE IF NOT EXISTS usage_events (
                   id uuid PRIMARY KEY,
                   ts timestamptz NOT NULL,
                   day date NOT NULL,
                   session_id text,
                   project_id text,
                   project_root text,
                   worktree text,
                   kind text NOT NULL DEFAULT 'primary',
                   requested_preset text,
                   preset text,
                   provider text NOT NULL,
                   model text,
                   tokens_in bigint NOT NULL DEFAULT 0,
                   tokens_out bigint NOT NULL DEFAULT 0,
                   cost_usd numeric(20,10) NOT NULL DEFAULT 0,
                   estimated boolean NOT NULL DEFAULT false,
                   latency_ms integer,
                   stream boolean NOT NULL DEFAULT false)""",
            "ALTER TABLE usage_events ADD COLUMN IF NOT EXISTS branch text",
            "ALTER TABLE usage_events ADD COLUMN IF NOT EXISTS session_directory text",
            "ALTER TABLE usage_events ADD COLUMN IF NOT EXISTS device text",
            "ALTER TABLE usage_events ADD COLUMN IF NOT EXISTS tool text",
            "ALTER TABLE usage_events ADD COLUMN IF NOT EXISTS tool_version text",
            "ALTER TABLE usage_events ADD COLUMN IF NOT EXISTS job text",
            "ALTER TABLE usage_events ADD COLUMN IF NOT EXISTS agent text",
            "ALTER TABLE usage_events ADD COLUMN IF NOT EXISTS git_head text",
            "ALTER TABLE usage_events ADD COLUMN IF NOT EXISTS git_snapshot_id text",
            "ALTER TABLE usage_events ADD COLUMN IF NOT EXISTS repo_remote text",
            "ALTER TABLE usage_events ADD COLUMN IF NOT EXISTS ttft_ms integer",
            "ALTER TABLE usage_events ADD COLUMN IF NOT EXISTS outcome text NOT NULL DEFAULT 'success'",
            "ALTER TABLE usage_events ADD COLUMN IF NOT EXISTS metadata jsonb NOT NULL DEFAULT '{}'::jsonb",
            "CREATE INDEX IF NOT EXISTS usage_events_project_ts_idx ON usage_events (project_id, ts)",
            "CREATE INDEX IF NOT EXISTS usage_events_branch_ts_idx ON usage_events (branch, ts)",
            "CREATE INDEX IF NOT EXISTS usage_events_device_ts_idx ON usage_events (device, ts)",
            """CREATE TABLE IF NOT EXISTS tracker_projects (
                   key text PRIMARY KEY,
                   display_name text NOT NULL,
                   repo_remote text,
                   root_path text,
                   color text,
                   archived boolean NOT NULL DEFAULT false,
                   created_at timestamptz NOT NULL DEFAULT now(),
                   updated_at timestamptz NOT NULL DEFAULT now())""",
            """CREATE TABLE IF NOT EXISTS tracker_project_aliases (
                   id bigserial PRIMARY KEY,
                   project_key text NOT NULL REFERENCES tracker_projects(key) ON DELETE CASCADE,
                   kind text NOT NULL,
                   value text NOT NULL,
                   created_at timestamptz NOT NULL DEFAULT now(),
                   UNIQUE (kind, value))""",
            "CREATE INDEX IF NOT EXISTS tracker_alias_project_idx ON tracker_project_aliases (project_key)",
            """CREATE TABLE IF NOT EXISTS tracker_heartbeats (
                   id text PRIMARY KEY,
                   ts timestamptz NOT NULL,
                   day date NOT NULL,
                   session_id text,
                   project_key text,
                   branch text,
                   device text,
                   tool text,
                   editor text,
                   entity text,
                   language text,
                   category text,
                   is_write boolean NOT NULL DEFAULT false,
                   source text NOT NULL DEFAULT 'wakatime',
                   duration_s double precision NOT NULL DEFAULT 0,
                   metadata jsonb NOT NULL DEFAULT '{}'::jsonb)""",
            "CREATE INDEX IF NOT EXISTS tracker_heartbeats_day_idx ON tracker_heartbeats (day)",
            "CREATE INDEX IF NOT EXISTS tracker_heartbeats_project_day_idx ON tracker_heartbeats (project_key, day)",
            """CREATE TABLE IF NOT EXISTS tracker_git_snapshots (
                   id text PRIMARY KEY,
                   ts timestamptz NOT NULL,
                   session_id text,
                   project_key text,
                   repo_root text,
                   branch text,
                   head text,
                   dirty boolean NOT NULL DEFAULT false,
                   status text,
                   files jsonb NOT NULL DEFAULT '[]'::jsonb,
                   diffstat jsonb NOT NULL DEFAULT '{}'::jsonb,
                   device text,
                   tool text,
                   metadata jsonb NOT NULL DEFAULT '{}'::jsonb)""",
            "CREATE INDEX IF NOT EXISTS tracker_snapshots_project_ts_idx ON tracker_git_snapshots (project_key, ts)",
            """CREATE TABLE IF NOT EXISTS tracker_tool_events (
                   id text PRIMARY KEY,
                   ts timestamptz NOT NULL,
                   session_id text,
                   project_key text,
                   branch text,
                   device text,
                   tool text,
                   tool_name text,
                   files jsonb NOT NULL DEFAULT '[]'::jsonb,
                   metadata jsonb NOT NULL DEFAULT '{}'::jsonb)""",
            "CREATE INDEX IF NOT EXISTS tracker_tool_events_project_ts_idx ON tracker_tool_events (project_key, ts)",
            """CREATE TABLE IF NOT EXISTS tracker_commits (
                   id bigserial PRIMARY KEY,
                   project_key text NOT NULL,
                   sha text NOT NULL,
                   branch text,
                   parents jsonb NOT NULL DEFAULT '[]'::jsonb,
                   author_name text,
                   author_email text,
                   committed_at timestamptz NOT NULL,
                   subject text,
                   body text,
                   files jsonb NOT NULL DEFAULT '[]'::jsonb,
                   additions integer NOT NULL DEFAULT 0,
                   deletions integer NOT NULL DEFAULT 0,
                   source text NOT NULL DEFAULT 'hook',
                   observed_at timestamptz NOT NULL DEFAULT now(),
                   UNIQUE (project_key, sha))""",
            "CREATE INDEX IF NOT EXISTS tracker_commits_project_time_idx ON tracker_commits (project_key, committed_at DESC)",
            """CREATE TABLE IF NOT EXISTS tracker_commit_files (
                   id bigserial PRIMARY KEY,
                   commit_id bigint NOT NULL REFERENCES tracker_commits(id) ON DELETE CASCADE,
                   path text NOT NULL,
                   status text,
                   additions integer NOT NULL DEFAULT 0,
                   deletions integer NOT NULL DEFAULT 0,
                   is_binary boolean NOT NULL DEFAULT false)""",
            """CREATE TABLE IF NOT EXISTS tracker_commit_usage (
                   id bigserial PRIMARY KEY,
                   commit_id bigint NOT NULL REFERENCES tracker_commits(id) ON DELETE CASCADE,
                   usage_event_id uuid NOT NULL REFERENCES usage_events(id) ON DELETE CASCADE,
                   kind text NOT NULL DEFAULT 'primary',
                   attribution text NOT NULL DEFAULT 'time_window',
                   weight double precision NOT NULL DEFAULT 1.0,
                   UNIQUE (commit_id, usage_event_id))""",
            "CREATE INDEX IF NOT EXISTS tracker_commit_usage_event_idx ON tracker_commit_usage (usage_event_id)",
            "ALTER TABLE tracker_commit_usage ADD COLUMN IF NOT EXISTS confidence double precision NOT NULL DEFAULT 1.0",
            "ALTER TABLE tracker_commit_usage ADD COLUMN IF NOT EXISTS evidence jsonb NOT NULL DEFAULT '{}'::jsonb",
            """CREATE TABLE IF NOT EXISTS tracker_budgets (
                   id text PRIMARY KEY,
                   project_key text NOT NULL,
                   branch text,
                   metric text NOT NULL DEFAULT 'cost_usd',
                   period text NOT NULL DEFAULT 'monthly',
                   limit_value numeric(20,10) NOT NULL,
                   warn_pct integer NOT NULL DEFAULT 80,
                   hard_block boolean NOT NULL DEFAULT true,
                   starts_at timestamptz,
                   ends_at timestamptz,
                   enabled boolean NOT NULL DEFAULT true,
                   created_at timestamptz NOT NULL DEFAULT now())""",
            "CREATE INDEX IF NOT EXISTS tracker_budgets_project_idx ON tracker_budgets (project_key, enabled)",
            """CREATE TABLE IF NOT EXISTS tracker_presence (
                   id text PRIMARY KEY,
                   session_id text,
                   project_key text,
                   branch text,
                   device text,
                   tool text,
                   editor text,
                   file text,
                   model text,
                   status text NOT NULL DEFAULT 'active',
                   last_seen timestamptz NOT NULL DEFAULT now(),
                   metadata jsonb NOT NULL DEFAULT '{}'::jsonb)""",
            "CREATE INDEX IF NOT EXISTS tracker_presence_seen_idx ON tracker_presence (last_seen DESC)",
            """CREATE TABLE IF NOT EXISTS tracker_job_profiles (
                   name text PRIMARY KEY,
                   base_preset text NOT NULL,
                   min_scores jsonb NOT NULL DEFAULT '{}'::jsonb,
                   weights jsonb NOT NULL DEFAULT '{}'::jsonb,
                   min_context integer,
                   max_cost numeric(20,10),
                   require_tools boolean,
                   max_latency_p95_ms integer,
                   min_success_rate double precision,
                   on_no_match text NOT NULL DEFAULT 'error',
                   enabled boolean NOT NULL DEFAULT true,
                   updated_at timestamptz NOT NULL DEFAULT now())""",
            """CREATE TABLE IF NOT EXISTS tracker_rejected_requests (
                   id bigserial PRIMARY KEY,
                   ts timestamptz NOT NULL DEFAULT now(),
                   session_id text,
                   project_key text,
                   missing jsonb NOT NULL DEFAULT '[]'::jsonb,
                   path text,
                   user_agent text)""",
            """CREATE TABLE IF NOT EXISTS tracker_refresh_runs (
                   id bigserial PRIMARY KEY,
                   source text NOT NULL,
                   started_at timestamptz NOT NULL DEFAULT now(),
                   finished_at timestamptz,
                   status text NOT NULL DEFAULT 'running',
                   counts jsonb NOT NULL DEFAULT '{}'::jsonb,
                   error text)""",
            """CREATE TABLE IF NOT EXISTS tracker_settings (
                   key text PRIMARY KEY,
                   value jsonb NOT NULL DEFAULT '{}'::jsonb)""",
            """CREATE TABLE IF NOT EXISTS router_model_stats (
                   id bigserial PRIMARY KEY,
                   ts timestamptz NOT NULL DEFAULT now(),
                   provider text,
                   model text NOT NULL,
                   success boolean NOT NULL,
                   latency_ms integer,
                   ttft_ms integer,
                   error text)""",
            "CREATE INDEX IF NOT EXISTS router_model_stats_model_ts_idx ON router_model_stats (model, ts DESC)",
        ]
        with self._lock:
            for statement in statements:
                self._conn.execute(statement)

    # ------------------------------------------------------------------ helpers
    def _fetchall(self, sql: str, params: Iterable[Any] = ()) -> list[dict]:
        with self._lock:
            return list(self._conn.execute(sql, tuple(params)).fetchall())

    def _fetchone(self, sql: str, params: Iterable[Any] = ()) -> dict | None:
        with self._lock:
            return self._conn.execute(sql, tuple(params)).fetchone()

    def _execute(self, sql: str, params: Iterable[Any] = ()) -> None:
        with self._lock:
            self._conn.execute(sql, tuple(params))

    def _scope(self, days: int, project_key: str | None = None,
               column: str = "day", project_column: str = "project_id") -> tuple[str, list[Any]]:
        where: list[str] = []
        params: list[Any] = []
        if days > 0:
            where.append(f"{column} >= %s")
            params.append(dt.datetime.now(self.settings.tz).date() - dt.timedelta(days=days - 1))
        if project_key:
            where.append(f"{project_column} = %s")
            params.append(project_key)
        return ((" WHERE " + " AND ".join(where)) if where else ""), params

    # ------------------------------------------------------------------ projects
    def sync_projects_from_usage(self) -> int:
        """Create a project row for every legacy usage project id."""
        rows = self._fetchall(
            "SELECT DISTINCT project_id, project_root FROM usage_events "
            "WHERE project_id IS NOT NULL")
        count = 0
        for row in rows:
            self.resolve_project(row["project_id"], root=row.get("project_root"))
            count += 1
        return count

    def resolve_project(self, key: str | None, root: str | None = None,
                        remote: str | None = None, display_name: str | None = None,
                        color: str | None = None) -> dict:
        remote_norm = normalize_remote(remote)
        if not key:
            key = remote_norm or (f"path:{root}" if root else "(unassigned)")
        key = key.strip()[:512]
        with self._lock:
            alias = self._conn.execute(
                "SELECT project_key FROM tracker_project_aliases WHERE value = %s "
                "ORDER BY CASE kind WHEN 'key' THEN 0 WHEN 'remote' THEN 1 ELSE 2 END LIMIT 1",
                (key,)).fetchone()
            if alias:
                row = self._conn.execute("SELECT * FROM tracker_projects WHERE key = %s",
                                         (alias["project_key"],)).fetchone()
                if row:
                    return require(self._conn.execute(
                        "SELECT * FROM tracker_projects WHERE key = %s",
                        (alias["project_key"],)).fetchone())
            if root:
                # Same checkout reported under another key (e.g. a session
                # started from a subdirectory) adopts the existing project.
                alias = self._conn.execute(
                    "SELECT project_key FROM tracker_project_aliases "
                    "WHERE kind = 'root' AND value = %s LIMIT 1", (root,)).fetchone()
                if alias:
                    row = self._conn.execute(
                        "SELECT * FROM tracker_projects WHERE key = %s",
                        (alias["project_key"],)).fetchone()
                    if row:
                        self._conn.execute(
                            """INSERT INTO tracker_project_aliases (project_key, kind, value)
                               VALUES (%s, 'key', %s) ON CONFLICT (kind, value) DO NOTHING""",
                            (row["key"], key))
                        return require(row)
            if remote_norm:
                alias = self._conn.execute(
                    "SELECT project_key FROM tracker_project_aliases WHERE value = %s LIMIT 1",
                    (remote_norm,)).fetchone()
                if alias:
                    return require(self._conn.execute(
                        "SELECT * FROM tracker_projects WHERE key = %s",
                        (alias["project_key"],)).fetchone())
            name = display_name or (key.rsplit("/", 1)[-1] if "/" in key else key)
            if key.startswith("path:") or not name:
                name = (root or key).rstrip("/").rsplit("/", 1)[-1] or "project"
            row = self._conn.execute(
                """INSERT INTO tracker_projects (key, display_name, repo_remote, root_path, color)
                   VALUES (%s, %s, %s, %s, %s)
                   ON CONFLICT (key) DO UPDATE SET
                     repo_remote = COALESCE(tracker_projects.repo_remote, EXCLUDED.repo_remote),
                     root_path = COALESCE(tracker_projects.root_path, EXCLUDED.root_path),
                     updated_at = now()
                   RETURNING *""",
                (key, name[:200], remote_norm, root, color)).fetchone()
            assert row is not None  # RETURNING always yields the upserted row
            for kind, value in (("key", key), ("root", root), ("remote", remote_norm)):
                if value:
                    self._conn.execute(
                        """INSERT INTO tracker_project_aliases (project_key, kind, value)
                           VALUES (%s, %s, %s) ON CONFLICT (kind, value) DO NOTHING""",
                        (row["key"], kind, value))
            return row

    def get_project(self, key: str) -> dict | None:
        return self._fetchone("SELECT * FROM tracker_projects WHERE key = %s", (key,))  # Optional

    def lookup_project(self, key: str) -> dict | None:
        """Read-only alias-aware project lookup (never inserts)."""
        row = self._fetchone("SELECT * FROM tracker_projects WHERE key = %s", (key,))
        if row:
            return row
        alias = self._fetchone(
            "SELECT project_key FROM tracker_project_aliases WHERE value = %s LIMIT 1", (key,))
        if not alias:
            return None
        return self._fetchone("SELECT * FROM tracker_projects WHERE key = %s",
                              (alias["project_key"],))

    def project_aliases(self, key: str) -> list[dict]:
        return self._fetchall(
            "SELECT id, kind, value, created_at FROM tracker_project_aliases "
            "WHERE project_key = %s ORDER BY kind, value", (key,))

    def add_alias(self, key: str, kind: str, value: str) -> dict:
        self.resolve_project(key)
        self._execute(
            """INSERT INTO tracker_project_aliases (project_key, kind, value)
               VALUES (%s, %s, %s) ON CONFLICT (kind, value) DO UPDATE
               SET project_key = EXCLUDED.project_key""", (key, kind, value))
        return {"ok": True, "project_key": key, "kind": kind, "value": value}

    def rename_project(self, key: str, display_name: str, color: str | None = None) -> dict:
        row = require(self._fetchone(
            """UPDATE tracker_projects SET display_name = %s, color = COALESCE(%s, color),
                      updated_at = now() WHERE key = %s RETURNING *""",
            (display_name[:200], color, key)))
        if not row:
            raise KeyError(key)
        return row

    def merge_projects(self, source: str, target: str) -> dict:
        if source == target:
            return {"ok": True, "source": source, "target": target, "moved": 0}
        self.resolve_project(source)
        self.resolve_project(target)
        moved = 0
        with self._lock, self._conn.transaction():
            for table, column in (
                ("usage_events", "project_id"),
                ("tracker_heartbeats", "project_key"),
                ("tracker_git_snapshots", "project_key"),
                ("tracker_tool_events", "project_key"),
                ("tracker_commits", "project_key"),
                ("tracker_budgets", "project_key"),
                ("tracker_presence", "project_key"),
            ):
                cur = self._conn.execute(
                    f"UPDATE {table} SET {column} = %s WHERE {column} = %s", (target, source))
                moved += cur.rowcount
            self._conn.execute(
                """INSERT INTO tracker_project_aliases (project_key, kind, value)
                   VALUES (%s, 'merged', %s) ON CONFLICT (kind, value) DO NOTHING""",
                (target, source))
            self._conn.execute("DELETE FROM tracker_projects WHERE key = %s", (source,))
        return {"ok": True, "source": source, "target": target, "moved": moved}

    def list_projects(self, days: int = 0) -> list[dict]:
        self.sync_projects_from_usage()
        start = (dt.datetime.now(self.settings.tz).date() - dt.timedelta(days=days - 1)
                 if days > 0 else None)
        projects = self._fetchall("SELECT * FROM tracker_projects ORDER BY display_name")
        out: list[dict] = []
        for p in projects:
            key = p["key"]
            u = require(self._fetchone(
                """SELECT COALESCE(sum(tokens_in),0) AS tokens_in,
                          COALESCE(sum(tokens_out),0) AS tokens_out,
                          count(*) AS requests, COALESCE(sum(cost_usd),0) AS cost,
                          max(ts) AS last_ts
                   FROM usage_events WHERE project_id = %s""" +
                (" AND day >= %s" if start else ""),
                (key, start) if start else (key,)))
            h = require(self._fetchone(
                """SELECT COALESCE(sum(duration_s),0) AS active_s, max(ts) AS last_ts
                   FROM tracker_heartbeats WHERE project_key = %s""" +
                (" AND day >= %s" if start else ""),
                (key, start) if start else (key,)))
            c = require(self._fetchone(
                """SELECT count(*) AS commits, max(committed_at) AS last_ts
                   FROM tracker_commits WHERE project_key = %s""" +
                (" AND committed_at >= %s" if start else ""),
                (key, dt.datetime.combine(start, dt.time.min, tzinfo=self.settings.tz))
                if start else (key,)))
            last = max((x for x in (u["last_ts"], h["last_ts"], c["last_ts"]) if x),
                       default=p["updated_at"])
            out.append({
                **p,
                "tokens_in": int(u["tokens_in"]),
                "tokens_out": int(u["tokens_out"]),
                "tokens": int(u["tokens_in"]) + int(u["tokens_out"]),
                "requests": int(u["requests"]),
                "cost": float(u["cost"]),
                "active_s": float(h["active_s"]),
                "commits": int(c["commits"]),
                "last_activity": _iso(last),
            })
        for item in out:
            for field in ("created_at", "updated_at"):
                item[field] = _iso(item.get(field))
        return out

    # ------------------------------------------------------------------ heartbeats
    def record_heartbeat(self, payload: Mapping) -> str:
        ts = parse_ts(payload.get("time") or payload.get("ts"))
        entity = str(payload.get("entity") or payload.get("file") or "")[:2000] or None
        project_name = payload.get("project") or payload.get("project_id")
        project = self.resolve_project(
            payload.get("project_key") or project_name,
            root=payload.get("project_root"),
            remote=payload.get("repo_remote"),
            display_name=payload.get("project_display_name"))
        branch = payload.get("branch")
        device = str(payload.get("machine_name") or payload.get("device") or "unknown")[:200]
        raw_id = "|".join(str(x) for x in (device, ts.isoformat(), entity or "",
                                           project["key"], branch or ""))
        hb_id = payload.get("id") or hashlib.sha1(raw_id.encode()).hexdigest()
        duration = float(payload.get("duration_s") or self.settings.heartbeat_interval_s)
        category = payload.get("category") or ("coding" if payload.get("is_write") else None)
        self._execute(
            """INSERT INTO tracker_heartbeats
               (id, ts, day, session_id, project_key, branch, device, tool, editor, entity,
                language, category, is_write, source, duration_s, metadata)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT (id) DO NOTHING""",
            (str(hb_id)[:200], ts, ts.astimezone(self.settings.tz).date(),
             payload.get("session") or payload.get("session_id"), project["key"], branch,
             device, payload.get("tool") or "wakatime",
             payload.get("editor") or payload.get("user_agent"),
             entity, payload.get("language"), category, bool(payload.get("is_write")),
             payload.get("source") or "wakatime", duration,
             Jsonb(_clean_metadata(payload.get("metadata") or {}))))
        self.upsert_presence({
            "session": payload.get("session") or payload.get("session_id") or hb_id,
            "project_key": project["key"], "branch": branch, "device": device,
            "tool": payload.get("tool") or "wakatime",
            "editor": payload.get("editor") or "VS Code",
            "file": entity, "status": "active",
            "metadata": {"source": payload.get("source") or "wakatime"},
        })
        return str(hb_id)

    def heartbeats(self, days: int = 7, project_key: str | None = None,
                   limit: int = 500) -> list[dict]:
        clause, params = self._scope(days, project_key, project_column="project_key")
        rows = self._fetchall(
            """SELECT * FROM tracker_heartbeats""" + clause +
            " ORDER BY ts DESC LIMIT %s", params + [min(limit, 5000)])
        for row in rows:
            row["ts"] = _iso(row["ts"])
            row["day"] = _iso(row["day"])
        return rows

    # ------------------------------------------------------------------ snapshots/tools
    def record_git_snapshot(self, payload: Mapping) -> str:
        ts = parse_ts(payload.get("ts"))
        project = self.resolve_project(
            payload.get("project") or payload.get("project_key"),
            root=payload.get("repo_root") or payload.get("project_root"),
            remote=payload.get("repo_remote"))
        raw = json.dumps({
            "project": project["key"], "branch": payload.get("branch"),
            "head": payload.get("head"), "status": payload.get("status"),
        }, sort_keys=True)
        sid = str(payload.get("id") or payload.get("snapshot") or
                  hashlib.sha1(raw.encode()).hexdigest())[:200]
        self._execute(
            """INSERT INTO tracker_git_snapshots
               (id, ts, session_id, project_key, repo_root, branch, head, dirty, status,
                files, diffstat, device, tool, metadata)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT (id) DO UPDATE SET ts = EXCLUDED.ts, files = EXCLUDED.files,
                 diffstat = EXCLUDED.diffstat, dirty = EXCLUDED.dirty""",
            (sid, ts, payload.get("session") or payload.get("session_id"), project["key"],
             payload.get("repo_root") or payload.get("project_root"), payload.get("branch"),
             payload.get("head"), bool(payload.get("dirty")), payload.get("status"),
             Jsonb(payload.get("files") or []), Jsonb(payload.get("diffstat") or {}),
             payload.get("device"), payload.get("tool") or "opencode",
             Jsonb(_clean_metadata(payload.get("metadata") or {}))))
        return sid

    def record_tool_event(self, payload: Mapping) -> str:
        ts = parse_ts(payload.get("ts"))
        project = self.resolve_project(
            payload.get("project") or payload.get("project_key"),
            root=payload.get("project_root"), remote=payload.get("repo_remote"))
        eid = str(payload.get("id") or uuid.uuid4())
        self._execute(
            """INSERT INTO tracker_tool_events
               (id, ts, session_id, project_key, branch, device, tool, tool_name, files, metadata)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT (id) DO NOTHING""",
            (eid, ts, payload.get("session") or payload.get("session_id"), project["key"],
             payload.get("branch"), payload.get("device"),
             payload.get("tool") or "opencode", payload.get("tool_name"),
             Jsonb(payload.get("files") or []),
             Jsonb(_clean_metadata(payload.get("metadata") or {}))))
        return eid

    # ------------------------------------------------------------------ commits
    def record_commit(self, payload: Mapping) -> dict:
        project = self.resolve_project(
            payload.get("project") or payload.get("project_key"),
            root=payload.get("repo_root") or payload.get("project_root"),
            remote=payload.get("repo_remote"))
        sha = str(payload.get("sha") or payload.get("commit") or "").strip()
        if not sha:
            raise ValueError("commit sha is required")
        committed_at = parse_ts(payload.get("committed_at") or payload.get("ts"))
        files = payload.get("files") or []
        additions = int(payload.get("additions") or sum(
            int(f.get("additions") or 0) for f in files if isinstance(f, dict)))
        deletions = int(payload.get("deletions") or sum(
            int(f.get("deletions") or 0) for f in files if isinstance(f, dict)))
        with self._lock, self._conn.transaction():
            row = self._conn.execute(
                """INSERT INTO tracker_commits
                   (project_key, sha, branch, parents, author_name, author_email,
                    committed_at, subject, body, files, additions, deletions, source)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (project_key, sha) DO UPDATE SET
                     branch = COALESCE(EXCLUDED.branch, tracker_commits.branch),
                     parents = EXCLUDED.parents, subject = EXCLUDED.subject,
                     body = EXCLUDED.body, files = EXCLUDED.files,
                     additions = EXCLUDED.additions, deletions = EXCLUDED.deletions,
                     author_name = COALESCE(EXCLUDED.author_name, tracker_commits.author_name),
                     author_email = COALESCE(EXCLUDED.author_email, tracker_commits.author_email),
                     committed_at = COALESCE(EXCLUDED.committed_at, tracker_commits.committed_at),
                     source = tracker_commits.source || '+' || EXCLUDED.source,
                     observed_at = now()
                   RETURNING *""",
                (project["key"], sha, payload.get("branch"),
                 Jsonb(payload.get("parents") or []), payload.get("author_name"),
                 payload.get("author_email"), committed_at, payload.get("subject"),
                 payload.get("body"), Jsonb(files), additions, deletions,
                 str(payload.get("source") or "hook")[:40])).fetchone()
            assert row is not None  # RETURNING always yields the upserted row
            self._conn.execute("DELETE FROM tracker_commit_files WHERE commit_id = %s",
                               (row["id"],))
            for f in files:
                if not isinstance(f, dict):
                    continue
                self._conn.execute(
                    """INSERT INTO tracker_commit_files
                       (commit_id, path, status, additions, deletions, is_binary)
                       VALUES (%s,%s,%s,%s,%s,%s)""",
                    (row["id"], str(f.get("path") or f.get("file") or "")[:2000],
                     f.get("status"), int(f.get("additions") or 0),
                     int(f.get("deletions") or 0), bool(f.get("binary"))))
        attributed = self.attribute_commit(int(row["id"]))
        return {**row, "files": files, "attributed_events": attributed}

    @staticmethod
    def _file_paths(files: Any) -> set[str]:
        paths: set[str] = set()
        for item in files or []:
            if isinstance(item, str):
                paths.add(item)
            elif isinstance(item, Mapping):
                path = item.get("path") or item.get("file") or item.get("name")
                if path:
                    paths.add(str(path))
        return paths

    def attribute_commit(self, commit_id: int) -> int:
        """Re-link one commit to requests using temporal and file evidence.

        Time windows are the baseline. A snapshot whose HEAD is the commit's
        parent and tool events/snapshots touching committed files increase the
        confidence and are retained as auditable evidence. Auxiliary requests
        remain linked (so spend is not lost) and are identified by ``kind``.
        """
        commit = self._fetchone("SELECT * FROM tracker_commits WHERE id = %s", (commit_id,))
        if not commit:
            return 0
        prev = self._fetchone(
            """SELECT committed_at FROM tracker_commits
               WHERE project_key = %s AND branch IS NOT DISTINCT FROM %s
                 AND committed_at < %s AND id <> %s
               ORDER BY committed_at DESC LIMIT 1""",
            (commit["project_key"], commit["branch"], commit["committed_at"], commit_id))
        start = prev["committed_at"] if prev else commit["committed_at"] - INITIAL_COMMIT_WINDOW
        branch_clause = " AND (branch = %s OR branch IS NULL)" if commit["branch"] else ""
        params: list[Any] = [commit["project_key"], start, commit["committed_at"]]
        if commit["branch"]:
            params.append(commit["branch"])
        events = self._fetchall(
            """SELECT id, ts, kind, branch, session_id, model, cost_usd
               FROM usage_events
               WHERE project_id = %s AND ts > %s AND ts <= %s""" + branch_clause +
            " ORDER BY ts", params)
        commit_files = self._file_paths(commit.get("files"))
        snapshots = self._fetchall(
            """SELECT id, ts, head, files FROM tracker_git_snapshots
               WHERE project_key = %s AND ts >= %s AND ts <= %s
               ORDER BY ts DESC LIMIT 100""",
            (commit["project_key"], start, commit["committed_at"]))
        tools = self._fetchall(
            """SELECT id, ts, files, tool_name FROM tracker_tool_events
               WHERE project_key = %s AND ts >= %s AND ts <= %s
               ORDER BY ts""",
            (commit["project_key"], start, commit["committed_at"]))
        parents = set(commit.get("parents") or [])
        parent_snapshot = next((s for s in snapshots if s.get("head") in parents), None)
        snapshot_paths = set().union(*(self._file_paths(s.get("files"))
                                       for s in snapshots)) if snapshots else set()
        snapshot_overlap = sorted(commit_files & snapshot_paths)
        tool_matches = [(t, sorted(commit_files & self._file_paths(t.get("files"))))
                        for t in tools]
        all_tool_paths = set().union(*(self._file_paths(t.get("files"))
                                      for t in tools)) if tools else set()
        with self._lock, self._conn.transaction():
            self._conn.execute("DELETE FROM tracker_commit_usage WHERE commit_id = %s",
                               (commit["id"],))
            for event in events:
                evidence: dict[str, Any] = {
                    "window_start": start.isoformat(),
                    "window_end": commit["committed_at"].isoformat(),
                    "commit_files": len(commit_files),
                }
                confidence = 0.55
                labels = ["time_window"]
                if parent_snapshot:
                    confidence += 0.15
                    labels.append("snapshot_parent")
                    evidence["snapshot_id"] = parent_snapshot["id"]
                    evidence["snapshot_head"] = parent_snapshot["head"]
                if snapshot_overlap:
                    confidence += 0.1
                    labels.append("snapshot_files")
                    evidence["snapshot_file_overlap"] = snapshot_overlap[:50]
                prior = next(((t, paths) for t, paths in reversed(tool_matches)
                              if paths and t["ts"] <= event["ts"]), None)
                if prior:
                    tool, overlap = prior
                    confidence += 0.2
                    labels.append("tool_files")
                    evidence["tool_event_id"] = tool["id"]
                    evidence["tool"] = tool.get("tool_name")
                    evidence["tool_file_overlap"] = overlap[:50]
                elif commit_files & all_tool_paths:
                    # Tool activity happened during this commit window, even if
                    # it was recorded after the individual request.
                    confidence += 0.08
                    labels.append("file_overlap")
                    evidence["file_overlap"] = sorted(commit_files)[:50]
                if commit["branch"] and event.get("branch") == commit["branch"]:
                    confidence += 0.05
                    labels.append("branch_match")
                    evidence["branch_match"] = True
                if event.get("kind") != "primary":
                    labels.append("auxiliary")
                self._conn.execute(
                    """INSERT INTO tracker_commit_usage
                       (commit_id, usage_event_id, kind, attribution, weight,
                        confidence, evidence)
                       VALUES (%s,%s,%s,%s,1.0,%s,%s)
                       ON CONFLICT (commit_id, usage_event_id) DO NOTHING""",
                    (commit["id"], event["id"], event.get("kind") or "primary",
                     "+".join(labels), min(1.0, confidence), Jsonb(evidence)))
        return len(events)

    def commits(self, project_key: str, days: int = 0, limit: int = 200) -> list[dict]:
        where = ["project_key = %s"]
        params: list[Any] = [project_key]
        if days > 0:
            where.append("committed_at >= %s")
            params.append(utcnow() - dt.timedelta(days=days))
        rows = self._fetchall(
            "SELECT * FROM tracker_commits WHERE " + " AND ".join(where) +
            " ORDER BY committed_at DESC LIMIT %s", params + [min(limit, 1000)])
        for row in rows:
            row["committed_at"] = _iso(row["committed_at"])
            row["observed_at"] = _iso(row["observed_at"])
            usage = require(self._fetchone(
                """SELECT count(*) AS requests, COALESCE(sum(u.tokens_in),0) AS tokens_in,
                          COALESCE(sum(u.tokens_out),0) AS tokens_out,
                          COALESCE(sum(u.cost_usd),0) AS cost
                   FROM tracker_commit_usage cu JOIN usage_events u ON u.id = cu.usage_event_id
                   WHERE cu.commit_id = %s""", (row["id"],)))
            row["ai"] = {
                "requests": int(usage["requests"]),
                "tokens": int(usage["tokens_in"]) + int(usage["tokens_out"]),
                "cost": float(usage["cost"]),
            }
        return rows

    def commit_detail(self, project_key: str, sha: str) -> dict | None:
        commit = self._fetchone(
            "SELECT * FROM tracker_commits WHERE project_key = %s AND sha = %s",
            (project_key, sha))
        if not commit:
            return None
        commit["committed_at"] = _iso(commit["committed_at"])
        commit["observed_at"] = _iso(commit["observed_at"])
        usage = self._fetchall(
            """SELECT u.*, cu.kind AS usage_kind, cu.attribution, cu.confidence,
                      cu.evidence
               FROM tracker_commit_usage cu JOIN usage_events u ON u.id = cu.usage_event_id
               WHERE cu.commit_id = %s ORDER BY u.ts""", (commit["id"],))
        for row in usage:
            row["ts"] = _iso(row["ts"])
            row["day"] = _iso(row["day"])
            row["cost_usd"] = float(row["cost_usd"])
        return {**commit, "usage": usage, "usage_count": len(usage)}

    def recompute_commit(self, project_key: str, sha: str) -> dict:
        commit = self._fetchone(
            "SELECT id FROM tracker_commits WHERE project_key = %s AND sha = %s",
            (project_key, sha))
        if not commit:
            raise KeyError(sha)
        return {"ok": True, "attributed_events": self.attribute_commit(commit["id"])}

    def uncommitted(self, project_key: str) -> dict:
        commits = self._fetchall(
            """SELECT branch, max(committed_at) AS last_commit FROM tracker_commits
               WHERE project_key = %s GROUP BY branch""", (project_key,))
        latest = {row["branch"]: row["last_commit"] for row in commits}
        branches = set(latest)
        branches.update(row["branch"] for row in self._fetchall(
            "SELECT DISTINCT branch FROM usage_events WHERE project_id = %s AND branch IS NOT NULL",
            (project_key,)))
        branches.update(row["branch"] for row in self._fetchall(
            "SELECT DISTINCT branch FROM tracker_git_snapshots WHERE project_key = %s AND branch IS NOT NULL",
            (project_key,)))
        out = []
        for branch in sorted(branches):
            start = latest.get(branch) or utcnow() - dt.timedelta(days=30)
            value = require(self._fetchone(
                """SELECT count(*) AS requests, COALESCE(sum(cost_usd),0) AS cost,
                          COALESCE(sum(tokens_in+tokens_out),0) AS tokens
                   FROM usage_events WHERE project_id = %s AND ts > %s AND branch = %s""",
                (project_key, start, branch)))
            out.append({"branch": branch, "since": _iso(start),
                        "requests": int(value["requests"]), "cost": float(value["cost"]),
                        "tokens": int(value["tokens"])})
        # Untagged legacy requests are shown once instead of being counted on
        # every branch. They start at the latest project commit observed.
        unknown_start = max((v for v in latest.values() if v),
                            default=utcnow() - dt.timedelta(days=30))
        value = require(self._fetchone(
            """SELECT count(*) AS requests, COALESCE(sum(cost_usd),0) AS cost,
                      COALESCE(sum(tokens_in+tokens_out),0) AS tokens
               FROM usage_events WHERE project_id = %s AND ts > %s AND branch IS NULL""",
            (project_key, unknown_start)))
        if int(value["requests"]):
            out.append({"branch": None, "since": _iso(unknown_start),
                        "requests": int(value["requests"]), "cost": float(value["cost"]),
                        "tokens": int(value["tokens"])})
        return {"project_key": project_key, "branches": out}

    # ------------------------------------------------------------------ budgets
    def _usage_value(self, project_key: str, branch: str | None, metric: str,
                     start: dt.datetime, end: dt.datetime) -> float:
        metric = {"cost_usd": "cost"}.get(metric, metric)
        column = {"cost": "COALESCE(sum(cost_usd),0)",
                  "tokens": "COALESCE(sum(tokens_in+tokens_out),0)",
                  "requests": "count(*)"}.get(metric)
        if not column:
            raise ValueError("budget metric must be cost, tokens or requests")
        branch_clause = " AND branch = %s" if branch else ""
        params: list[Any] = [project_key, start, end] + ([branch] if branch else [])
        row = require(self._fetchone(
            f"""SELECT {column} AS value FROM usage_events
                WHERE project_id = %s AND ts >= %s AND ts < %s{branch_clause}""", params))
        return float(row["value"] or 0)

    @staticmethod
    def _period_bounds(period: str, now: dt.datetime, tz) -> tuple[dt.datetime, dt.datetime]:
        local = now.astimezone(tz)
        if period == "daily":
            start = local.replace(hour=0, minute=0, second=0, microsecond=0)
            end = start + dt.timedelta(days=1)
        elif period == "weekly":
            start = (local - dt.timedelta(days=local.weekday())).replace(
                hour=0, minute=0, second=0, microsecond=0)
            end = start + dt.timedelta(days=7)
        elif period == "monthly":
            start = local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            if start.month == 12:
                end = start.replace(year=start.year + 1, month=1)
            else:
                end = start.replace(month=start.month + 1)
        elif period == "total":
            start = dt.datetime(1970, 1, 1, tzinfo=dt.timezone.utc)
            end = now + dt.timedelta(seconds=1)
        else:
            raise ValueError("budget period must be daily, weekly, monthly or total")
        return start.astimezone(dt.timezone.utc), end.astimezone(dt.timezone.utc)

    def budget_check(self, project_key: str, branch: str | None = None,
                     now: dt.datetime | None = None) -> dict:
        now = now or utcnow()
        budgets = self._fetchall(
            "SELECT * FROM tracker_budgets WHERE project_key = %s AND enabled = true",
            (project_key,))
        if branch:
            budgets = [b for b in budgets if b["branch"] in (None, branch)]
        else:
            budgets = [b for b in budgets if b["branch"] is None]
        warnings: list[dict] = []
        blocked: list[dict] = []
        evaluated: list[dict] = []
        for b in budgets:
            start, end = self._period_bounds(b["period"], now, self.settings.tz)
            if b.get("starts_at") and b["starts_at"] > start:
                start = b["starts_at"]
            if b.get("ends_at") and b["ends_at"] < end:
                end = b["ends_at"]
            used = self._usage_value(project_key, b["branch"], b["metric"], start, end)
            limit = float(b["limit_value"])
            pct = (used / limit * 100) if limit > 0 else 100.0
            item = {
                "id": b["id"], "project_key": b["project_key"], "branch": b["branch"],
                "metric": b["metric"], "period": b["period"], "limit": limit,
                "used": round(used, 10), "pct": round(pct, 2),
                "warn_pct": b["warn_pct"], "hard_block": b["hard_block"],
                "period_start": _iso(start), "period_end": _iso(end),
            }
            evaluated.append(item)
            if pct >= 100 and b["hard_block"]:
                blocked.append(item)
            elif pct >= b["warn_pct"]:
                warnings.append(item)
        return {
            "project_key": project_key, "branch": branch,
            "blocked": bool(blocked), "blocked_by": blocked,
            "warnings": warnings, "budgets": evaluated,
            "checked_at": _iso(now),
        }

    def list_budgets(self, project_key: str | None = None) -> list[dict]:
        if project_key:
            rows = self._fetchall(
                "SELECT * FROM tracker_budgets WHERE project_key = %s ORDER BY created_at DESC",
                (project_key,))
        else:
            rows = self._fetchall("SELECT * FROM tracker_budgets ORDER BY created_at DESC")
        for row in rows:
            row["limit_value"] = float(row["limit_value"])
            row["created_at"] = _iso(row["created_at"])
            row["starts_at"] = _iso(row.get("starts_at"))
            row["ends_at"] = _iso(row.get("ends_at"))
        return rows

    def upsert_budget(self, payload: Mapping) -> dict:
        bid = str(payload.get("id") or uuid.uuid4())
        metric = str(payload.get("metric") or "cost_usd")
        metric = {"cost": "cost", "cost_usd": "cost", "tokens": "tokens",
                  "requests": "requests"}.get(metric, metric)
        if metric not in {"cost", "tokens", "requests"}:
            raise ValueError("budget metric must be cost, tokens or requests")
        period = str(payload.get("period") or "monthly")
        limit = float(payload.get("limit") or payload.get("limit_value") or 0)
        if limit < 0:
            raise ValueError("budget limit must be >= 0")
        if period not in {"daily", "weekly", "monthly", "total"}:
            raise ValueError("period must be daily, weekly, monthly or total")
        project_key = str(payload.get("project_key") or payload.get("project") or "")
        if not project_key:
            raise ValueError("project_key is required")
        self.resolve_project(project_key)
        row = require(self._fetchone(
            """INSERT INTO tracker_budgets
               (id, project_key, branch, metric, period, limit_value, warn_pct, hard_block,
                starts_at, ends_at, enabled)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT (id) DO UPDATE SET
                 project_key = EXCLUDED.project_key, branch = EXCLUDED.branch,
                 metric = EXCLUDED.metric, period = EXCLUDED.period,
                 limit_value = EXCLUDED.limit_value, warn_pct = EXCLUDED.warn_pct,
                 hard_block = EXCLUDED.hard_block, starts_at = EXCLUDED.starts_at,
                 ends_at = EXCLUDED.ends_at, enabled = EXCLUDED.enabled
               RETURNING *""",
            (bid, project_key, payload.get("branch") or None, metric, period, limit,
             int(payload.get("warn_pct") or 80), bool(payload.get("hard_block", True)),
             payload.get("starts_at"), payload.get("ends_at"),
             bool(payload.get("enabled", True)))))
        row["limit_value"] = float(row["limit_value"])
        row["created_at"] = _iso(row["created_at"])
        row["starts_at"] = _iso(row.get("starts_at"))
        row["ends_at"] = _iso(row.get("ends_at"))
        return row

    def delete_budget(self, budget_id: str) -> bool:
        with self._lock:
            cur = self._conn.execute("DELETE FROM tracker_budgets WHERE id = %s", (budget_id,))
            return cur.rowcount > 0

    # ------------------------------------------------------------------ jobs
    def list_jobs(self) -> list[dict]:
        rows = self._fetchall("SELECT * FROM tracker_job_profiles ORDER BY name")
        for row in rows:
            row["max_cost"] = float(row["max_cost"]) if row.get("max_cost") is not None else None
            row["updated_at"] = _iso(row["updated_at"])
        return rows

    def get_job(self, name: str) -> dict | None:
        row = self._fetchone("SELECT * FROM tracker_job_profiles WHERE name = %s", (name,))
        if row:
            row["max_cost"] = float(row["max_cost"]) if row.get("max_cost") is not None else None
            row["updated_at"] = _iso(row["updated_at"])
        return row

    def upsert_job(self, payload: Mapping) -> dict:
        name = str(payload.get("name") or "").strip()
        if not name:
            raise ValueError("job name is required")
        base = str(payload.get("base_preset") or "medium")
        min_scores = payload.get("min_scores") or {}
        weights = payload.get("weights") or {}
        if not isinstance(min_scores, Mapping) or not isinstance(weights, Mapping):
            raise ValueError("min_scores and weights must be objects")
        row = require(self._fetchone(
            """INSERT INTO tracker_job_profiles
               (name, base_preset, min_scores, weights, min_context, max_cost,
                require_tools, max_latency_p95_ms, min_success_rate, on_no_match, enabled)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT (name) DO UPDATE SET
                 base_preset = EXCLUDED.base_preset, min_scores = EXCLUDED.min_scores,
                 weights = EXCLUDED.weights, min_context = EXCLUDED.min_context,
                 max_cost = EXCLUDED.max_cost, require_tools = EXCLUDED.require_tools,
                 max_latency_p95_ms = EXCLUDED.max_latency_p95_ms,
                 min_success_rate = EXCLUDED.min_success_rate,
                 on_no_match = EXCLUDED.on_no_match, enabled = EXCLUDED.enabled,
                 updated_at = now()
               RETURNING *""",
            (name, base, Jsonb(dict(min_scores)), Jsonb(dict(weights)),
             payload.get("min_context"), payload.get("max_cost"),
             payload.get("require_tools"), payload.get("max_latency_p95_ms"),
             payload.get("min_success_rate"),
             str(payload.get("on_no_match") or "error"), bool(payload.get("enabled", True))
              )))
        row["max_cost"] = float(row["max_cost"]) if row.get("max_cost") is not None else None
        row["updated_at"] = _iso(row["updated_at"])
        return row

    def delete_job(self, name: str) -> bool:
        with self._lock:
            cur = self._conn.execute("DELETE FROM tracker_job_profiles WHERE name = %s", (name,))
            return cur.rowcount > 0

    # ------------------------------------------------------------------ presence
    def upsert_presence(self, payload: Mapping) -> dict:
        session = str(payload.get("session") or payload.get("session_id") or "unknown")
        device = str(payload.get("device") or "unknown")
        tool = str(payload.get("tool") or "unknown")
        pid = hashlib.sha1(f"{session}|{device}|{tool}".encode()).hexdigest()
        project = None
        if payload.get("project_key"):
            project = self.resolve_project(payload["project_key"])["key"]
        row = require(self._fetchone(
            """INSERT INTO tracker_presence
               (id, session_id, project_key, branch, device, tool, editor, file, model,
                status, last_seen, metadata)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,now(),%s)
               ON CONFLICT (id) DO UPDATE SET
                 project_key = COALESCE(EXCLUDED.project_key, tracker_presence.project_key),
                 branch = COALESCE(EXCLUDED.branch, tracker_presence.branch),
                 editor = COALESCE(EXCLUDED.editor, tracker_presence.editor),
                 file = COALESCE(EXCLUDED.file, tracker_presence.file),
                 model = COALESCE(EXCLUDED.model, tracker_presence.model),
                 status = EXCLUDED.status, last_seen = now(),
                 metadata = tracker_presence.metadata || EXCLUDED.metadata
               RETURNING *""",
            (pid, session, project, payload.get("branch"), device, tool,
             payload.get("editor"), payload.get("file"), payload.get("model"),
             payload.get("status") or "active",
             Jsonb(_clean_metadata(payload.get("metadata") or {})))))
        row["last_seen"] = _iso(row["last_seen"])
        return row

    def presence(self) -> list[dict]:
        cutoff = utcnow() - dt.timedelta(seconds=self.settings.presence_ttl_s)
        rows = self._fetchall(
            "SELECT * FROM tracker_presence WHERE last_seen >= %s ORDER BY last_seen DESC",
            (cutoff,))
        today = dt.datetime.now(self.settings.tz).date()
        stats = {row["session_id"]: row for row in self._fetchall(
            """SELECT session_id, count(*) requests,
                      COALESCE(sum(tokens_in+tokens_out),0) tokens,
                      COALESCE(sum(cost_usd),0) cost
               FROM usage_events WHERE day = %s AND session_id IS NOT NULL
               GROUP BY session_id""", (today,))}
        for row in rows:
            row["last_seen"] = _iso(row["last_seen"])
            spend = stats.get(row["session_id"])
            row["today"] = {
                "requests": int(spend["requests"]) if spend else 0,
                "tokens": int(spend["tokens"]) if spend else 0,
                "cost": float(spend["cost"]) if spend else 0.0,
            }
        return rows

    # ------------------------------------------------------------------ rejected / refresh / stats
    def record_rejected(self, payload: Mapping) -> int:
        row = require(self._fetchone(
            """INSERT INTO tracker_rejected_requests
               (session_id, project_key, missing, path, user_agent)
               VALUES (%s,%s,%s,%s,%s) RETURNING id""",
            (payload.get("session"), payload.get("project_key"),
             Jsonb(payload.get("missing") or []), payload.get("path"),
             payload.get("user_agent"))))
        return int(row["id"])

    def rejected(self, limit: int = 100) -> list[dict]:
        rows = self._fetchall(
            "SELECT * FROM tracker_rejected_requests ORDER BY ts DESC LIMIT %s",
            (min(limit, 1000),))
        for row in rows:
            row["ts"] = _iso(row["ts"])
        return rows

    def start_refresh(self, source: str) -> int:
        return int(require(self._fetchone(
            "INSERT INTO tracker_refresh_runs (source) VALUES (%s) RETURNING id",
            (source,)))["id"])

    def finish_refresh(self, run_id: int, status: str, counts: Mapping | None = None,
                       error: str | None = None) -> None:
        self._execute(
            """UPDATE tracker_refresh_runs SET finished_at = now(), status = %s,
                      counts = %s, error = %s WHERE id = %s""",
            (status, Jsonb(dict(counts or {})), error, run_id))

    def refresh_status(self) -> dict:
        rows = self._fetchall(
            "SELECT * FROM tracker_refresh_runs ORDER BY started_at DESC LIMIT 20")
        for row in rows:
            row["started_at"] = _iso(row["started_at"])
            row["finished_at"] = _iso(row["finished_at"])
        return {"runs": rows,
                "last_success": next((r for r in rows if r["status"] == "ok"), None),
                "last_error": next((r for r in rows if r["status"] == "error"), None)}

    def record_model_stat(self, provider: str | None, model: str, success: bool,
                          latency_ms: int | None = None, ttft_ms: int | None = None,
                          error: str | None = None) -> None:
        self._execute(
            """INSERT INTO router_model_stats
               (provider, model, success, latency_ms, ttft_ms, error)
               VALUES (%s,%s,%s,%s,%s,%s)""",
            (provider, model, success, latency_ms, ttft_ms,
             (error or "")[:500] or None))

    def observed_metrics(self, minutes: int = 1440,
                         providers: Collection[str] | None = None) -> dict[str, dict]:
        """Router-observed latency/success per model over the last ``minutes``.

        ``providers`` narrows the aggregate to models of those providers so the
        per-provider panel does not show numbers from unrelated deployments.
        """
        cutoff = utcnow() - dt.timedelta(minutes=minutes)
        sql = ("""SELECT model, count(*) AS requests,
                      avg(CASE WHEN success THEN 100.0 ELSE 0.0 END) AS success_rate,
                      percentile_cont(0.5) WITHIN GROUP (ORDER BY latency_ms) AS p50,
                      percentile_cont(0.95) WITHIN GROUP (ORDER BY latency_ms) AS p95,
                      percentile_cont(0.5) WITHIN GROUP (ORDER BY ttft_ms) AS ttft50,
                      avg(CASE WHEN success THEN 0.0 ELSE 100.0 END) AS failure_rate
               FROM router_model_stats WHERE ts >= %s""")
        params: list[Any] = [cutoff]
        if providers:
            sql += " AND provider = ANY(%s)"
            params.append(list(providers))
        rows = self._fetchall(sql + " GROUP BY model", params)
        out: dict[str, dict] = {}
        for row in rows:
            out[row["model"]] = {
                "rt_latency_p50_ms": float(row["p50"]) if row["p50"] is not None else None,
                "rt_latency_p95_ms": float(row["p95"]) if row["p95"] is not None else None,
                "rt_ttft_p50_ms": float(row["ttft50"]) if row["ttft50"] is not None else None,
                "rt_success_rate": float(row["success_rate"] or 0),
                "rt_failure_rate": float(row["failure_rate"] or 0),
                "rt_requests": int(row["requests"]),
            }
        return out

    # ------------------------------------------------------------------ feeds
    def record_usage_event(self, payload: Mapping) -> str:
        """Insert one LLM usage record (router mirror / external feed)."""
        parsed_ts = parse_ts(payload.get("ts"))
        raw_id = payload.get("id")
        if raw_id is None or raw_id == "":
            event_id = str(uuid.uuid4())
        elif isinstance(raw_id, str):
            try:
                event_id = str(uuid.UUID(raw_id))
            except ValueError:
                event_id = str(uuid.uuid5(uuid.NAMESPACE_URL, raw_id))
        else:
            raise ValueError("usage event id must be a UUID string")
        project = payload.get("project_id") or payload.get("project")
        project_key = None
        if project:
            project_key = self.resolve_project(
                project, root=payload.get("project_root"),
                remote=payload.get("repo_remote"))["key"]
        tokens_in = int(payload.get("tokens_in") or payload.get("in") or 0)
        tokens_out = int(payload.get("tokens_out") or payload.get("out") or 0)
        self._execute(
            """INSERT INTO usage_events
               (id, ts, day, session_id, project_id, project_root, worktree, kind,
                requested_preset, preset, provider, model, tokens_in, tokens_out,
                cost_usd, estimated, latency_ms, stream, branch, session_directory,
                device, tool, tool_version, job, agent, git_head, git_snapshot_id,
                repo_remote, ttft_ms, outcome, metadata)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                       %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT (id) DO NOTHING""",
            (event_id, parsed_ts, parsed_ts.astimezone(self.settings.tz).date(),
             payload.get("session") or payload.get("session_id") or None, project_key,
             payload.get("project_root"), payload.get("worktree") or payload.get("worktree_path"),
             str(payload.get("kind") or "primary"), payload.get("requested_preset"),
             payload.get("preset"), str(payload.get("provider") or "unknown")[:80],
             payload.get("model"), tokens_in, tokens_out,
             float(payload.get("cost_usd") if payload.get("cost_usd") is not None
                   else payload.get("cost") or 0),  # type: ignore[arg-type]
             bool(payload.get("estimated", False)), payload.get("latency_ms"),
             bool(payload.get("stream", False)), payload.get("branch"),
             payload.get("session_directory"), payload.get("device"), payload.get("tool"),
             payload.get("tool_version"), payload.get("job"), payload.get("agent"),
             payload.get("git_head"), payload.get("git_snapshot_id"),
             payload.get("repo_remote"), payload.get("ttft_ms"),
             str(payload.get("outcome") or "success"),
             Jsonb(_clean_metadata(payload.get("metadata") or {}))))
        if project_key:
            self.upsert_presence({
                "session": payload.get("session") or payload.get("session_id") or "router",
                "project_key": project_key, "branch": payload.get("branch"),
                "device": payload.get("device"), "tool": payload.get("tool") or "opencode",
                "model": payload.get("model"), "status": "active",
                "metadata": {"source": "usage"},
            })
        return event_id

    # ------------------------------------------------------------------ stats
    def overview(self, days: int = 30) -> dict:
        clause, params = self._scope(days)
        usage = require(self._fetchone(
            """SELECT COALESCE(sum(tokens_in),0) AS tokens_in,
                      COALESCE(sum(tokens_out),0) AS tokens_out,
                      count(*) AS requests, COALESCE(sum(cost_usd),0) AS cost
               FROM usage_events""" + clause, params))
        hb = require(self._fetchone(
            """SELECT COALESCE(sum(duration_s),0) AS active_s, count(*) AS heartbeats
               FROM tracker_heartbeats""" + clause, params))
        cutoff = utcnow() - dt.timedelta(days=days) if days > 0 else None
        commits = require(self._fetchone(
            "SELECT count(*) AS commits FROM tracker_commits" +
            (" WHERE committed_at >= %s" if cutoff else ""),
            (cutoff,) if cutoff else ()))
        project_count = require(self._fetchone("SELECT count(*) AS n FROM tracker_projects"))["n"]
        active = len(self.presence())
        daily: dict[str, dict] = {}
        for row in self._fetchall(
                """SELECT day, sum(tokens_in) tokens_in, sum(tokens_out) tokens_out,
                          count(*) requests, sum(cost_usd) cost FROM usage_events""" +
                clause + " GROUP BY day", params):
            key = row["day"].isoformat()
            daily.setdefault(key, {"date": key, "tokens": 0, "requests": 0, "cost": 0.0,
                                   "active_s": 0.0})
            daily[key].update(tokens=int(row["tokens_in"]) + int(row["tokens_out"]),
                              requests=int(row["requests"]), cost=float(row["cost"]))
        for row in self._fetchall(
                """SELECT day, sum(duration_s) active_s FROM tracker_heartbeats""" +
                clause + " GROUP BY day", params):
            key = row["day"].isoformat()
            daily.setdefault(key, {"date": key, "tokens": 0, "requests": 0, "cost": 0.0,
                                   "active_s": 0.0})
            daily[key]["active_s"] = float(row["active_s"])
        models = self._fetchall(
            """SELECT model, count(*) requests, sum(tokens_in+tokens_out) tokens,
                      sum(cost_usd) cost FROM usage_events""" + clause +
            " GROUP BY model ORDER BY cost DESC NULLS LAST, requests DESC LIMIT 20", params)
        return {
            "days": days,
            "totals": {
                "requests": int(usage["requests"]),
                "tokens": int(usage["tokens_in"]) + int(usage["tokens_out"]),
                "cost": float(usage["cost"]),
                "active_s": float(hb["active_s"]),
                "heartbeats": int(hb["heartbeats"]),
                "commits": int(commits["commits"]),
                "projects": int(project_count),
                "active_sessions": active,
            },
            "daily": sorted(daily.values(), key=lambda x: x["date"]),
            "calendar": self.calendar(min(days, 365) if days > 0 else 365),
            "models": [{**m, "tokens": int(m["tokens"] or 0), "cost": float(m["cost"] or 0),
                        "requests": int(m["requests"])} for m in models],
            "presence": self.presence(),
        }

    def heatmap(self, days: int = 28, project_key: str | None = None) -> dict:
        horizon = days if days > 0 else 365
        cutoff = utcnow() - dt.timedelta(days=horizon)
        hb_params: list[Any] = [cutoff]
        usage_params: list[Any] = [cutoff]
        hb_extra = usage_extra = ""
        if project_key:
            hb_extra = " AND project_key = %s"
            usage_extra = " AND project_id = %s"
            hb_params.append(project_key)
            usage_params.append(project_key)
        heartbeats = self._fetchall(
            "SELECT ts, duration_s, project_key FROM tracker_heartbeats WHERE ts >= %s" +
            hb_extra, hb_params)
        usage = self._fetchall(
            """SELECT ts, latency_ms, cost_usd, project_id FROM usage_events
               WHERE ts >= %s""" + usage_extra, usage_params)
        human: dict[tuple[int, int], list[tuple[float, float]]] = {}
        ai: dict[tuple[int, int], list[tuple[float, float]]] = {}
        cost: dict[tuple[int, int], float] = {}
        requests: dict[tuple[int, int], int] = {}
        tz = self.settings.tz

        def add_bucket(target, start: dt.datetime, end: dt.datetime, tzinfo):
            local = start.astimezone(tzinfo)
            hour_start = local.replace(minute=0, second=0, microsecond=0)
            while hour_start < end.astimezone(tzinfo):
                bucket_end = hour_start + dt.timedelta(hours=1)
                s = max(start, hour_start)
                e = min(end, bucket_end)
                if e > s:
                    # Bucket by the hour actually being accumulated: an
                    # interval crossing local midnight belongs to two weekdays.
                    target.setdefault((hour_start.weekday(), hour_start.hour), []).append(
                        (s.timestamp(), e.timestamp()))
                hour_start = bucket_end

        for row in heartbeats:
            start = row["ts"]
            duration = float(row["duration_s"] or self.settings.heartbeat_interval_s)
            add_bucket(human, start, start + dt.timedelta(seconds=duration), tz)
        for row in usage:
            start = row["ts"]
            duration = max(1, int(row["latency_ms"] or 0)) / 1000.0
            add_bucket(ai, start, start + dt.timedelta(seconds=duration), tz)
            local = start.astimezone(tz)
            key = (local.weekday(), local.hour)
            cost[key] = cost.get(key, 0.0) + float(row["cost_usd"] or 0)
            requests[key] = requests.get(key, 0) + 1
        cells = []
        for weekday in range(7):
            for hour in range(24):
                h = _merge_intervals(human.get((weekday, hour), []))
                a = _merge_intervals(ai.get((weekday, hour), []))
                human_s = _union_length(h)
                ai_s = _union_length(a)
                mixed_s = _intersection_length(h, a)
                cells.append({
                    "weekday": weekday, "hour": hour,
                    "human_s": round(human_s, 2), "ai_s": round(ai_s, 2),
                    "mixed_s": round(mixed_s, 2),
                    "active_s": round(human_s + ai_s - mixed_s, 2),
                    "cost": round(cost.get((weekday, hour), 0.0), 10),
                    "requests": requests.get((weekday, hour), 0),
                })
        max_active = max((c["active_s"] for c in cells), default=0)
        return {"days": horizon, "project_key": project_key, "cells": cells,
                "max_active_s": max_active}

    def project_detail(self, key: str, days: int = 30) -> dict | None:
        # Read-only: a GET must not create a project row. Unknown keys return
        # None so the API can answer 404.
        project = self.get_project(key) or self.lookup_project(key)
        if not project:
            return None
        clause, params = self._scope(days, key)
        hb_clause, hb_params = self._scope(days, key, project_column="project_key")
        usage = require(self._fetchone(
            """SELECT COALESCE(sum(tokens_in),0) tokens_in, COALESCE(sum(tokens_out),0) tokens_out,
                      count(*) requests, COALESCE(sum(cost_usd),0) cost
               FROM usage_events""" + clause, params))
        hb = require(self._fetchone(
            """SELECT COALESCE(sum(duration_s),0) active_s FROM tracker_heartbeats""" +
            hb_clause, hb_params))
        branches = self._fetchall(
            """SELECT COALESCE(branch,'(unknown)') branch, count(*) requests,
                      sum(tokens_in+tokens_out) tokens, sum(cost_usd) cost,
                      max(ts) last_seen FROM usage_events""" + clause +
            " GROUP BY 1 ORDER BY cost DESC", params)
        models = self._fetchall(
            """SELECT COALESCE(model,'(unknown)') model, count(*) requests,
                      sum(tokens_in+tokens_out) tokens, sum(cost_usd) cost
               FROM usage_events""" + clause + " GROUP BY 1 ORDER BY cost DESC LIMIT 20", params)
        devices = self._fetchall(
            """SELECT COALESCE(device,'(unknown)') device, count(*) requests,
                      sum(cost_usd) cost FROM usage_events""" + clause +
            " GROUP BY 1 ORDER BY cost DESC", params)
        tools = self._fetchall(
            """SELECT COALESCE(tool,'(unknown)') tool, count(*) requests,
                      sum(cost_usd) cost FROM usage_events""" + clause +
            " GROUP BY 1 ORDER BY cost DESC", params)
        sessions = self._fetchall(
            """SELECT session_id, max(ts) last_seen, count(*) requests,
                      sum(cost_usd) cost, max(model) model
               FROM usage_events""" + clause +
            " GROUP BY session_id ORDER BY last_seen DESC LIMIT 50", params)
        return {
            "project": {**project, "created_at": _iso(project.get("created_at")),
                        "updated_at": _iso(project.get("updated_at"))},
            "range": {"days": days},
            "totals": {
                "requests": int(usage["requests"]),
                "tokens": int(usage["tokens_in"]) + int(usage["tokens_out"]),
                "cost": float(usage["cost"]),
                "active_s": float(hb["active_s"]),
                "commits": len(self.commits(key, days, 1000)),
            },
            "branches": branches, "models": models, "devices": devices,
            "tools": tools, "sessions": sessions,
            "commits": self.commits(key, days),
            "uncommitted": self.uncommitted(key),
            "budgets": self.list_budgets(key),
            "heatmap": self.heatmap(days, key),
        }

    def time_breakdown(self, days: int = 30, project_key: str | None = None) -> dict:
        """Human-only, AI-only and mixed active time from interval unions."""
        cutoff = utcnow() - dt.timedelta(days=days) if days > 0 else dt.datetime(1970, 1, 1, tzinfo=dt.timezone.utc)
        hb_sql = "SELECT ts, duration_s FROM tracker_heartbeats WHERE ts >= %s"
        usage_sql = "SELECT ts, latency_ms FROM usage_events WHERE ts >= %s"
        hb_params: list[Any] = [cutoff]
        usage_params: list[Any] = [cutoff]
        if project_key:
            hb_sql += " AND project_key = %s"
            usage_sql += " AND project_id = %s"
            hb_params.append(project_key)
            usage_params.append(project_key)
        human = _merge_intervals(
            (row["ts"].timestamp(),
             (row["ts"] + dt.timedelta(seconds=float(row["duration_s"] or 0))).timestamp())
            for row in self._fetchall(hb_sql, hb_params))
        ai = _merge_intervals(
            (row["ts"].timestamp(),
             (row["ts"] + dt.timedelta(seconds=max(1, int(row["latency_ms"] or 0)) / 1000)).timestamp())
            for row in self._fetchall(usage_sql, usage_params))
        mixed = _intersection_length(human, ai)
        human_total = _union_length(human)
        ai_total = _union_length(ai)
        return {
            "human_s": round(human_total - mixed, 2),
            "ai_s": round(ai_total - mixed, 2),
            "mixed_s": round(mixed, 2),
            "human_total_s": round(human_total, 2),
            "ai_total_s": round(ai_total, 2),
            "active_s": round(human_total + ai_total - mixed, 2),
        }

    def day_cells(self, date: str, project_key: str | None = None) -> dict:
        """One local day in 10-minute cells (144 cells) for the day heatmap."""
        try:
            day = dt.date.fromisoformat(str(date)[:10])
        except ValueError as exc:
            raise ValueError("date must be YYYY-MM-DD") from exc
        start = dt.datetime.combine(day, dt.time.min, tzinfo=self.settings.tz)
        end = start + dt.timedelta(days=1)
        hb_sql = ("SELECT ts, duration_s FROM tracker_heartbeats "
                  "WHERE ts >= %s AND ts < %s")
        usage_sql = ("SELECT ts, latency_ms, cost_usd FROM usage_events "
                     "WHERE ts >= %s AND ts < %s")
        hb_params: list[Any] = [start, end]
        usage_params: list[Any] = [start, end]
        if project_key:
            hb_sql += " AND project_key = %s"
            usage_sql += " AND project_id = %s"
            hb_params.append(project_key)
            usage_params.append(project_key)
        human: dict[int, list[tuple[float, float]]] = {}
        ai: dict[int, list[tuple[float, float]]] = {}
        cost: dict[int, float] = {}
        requests: dict[int, int] = {}
        def split(target: dict[int, list[tuple[float, float]]],
                  item_start: dt.datetime, item_end: dt.datetime) -> None:
            # Clamp to the day and split across every 10-minute cell the
            # interval overlaps, so a long heartbeat never lands in a single
            # cell.
            s0 = max(item_start, start)
            e0 = min(item_end, end)
            if e0 <= s0:
                return
            index = int((s0 - start).total_seconds() // 600)
            while index < 144:
                c_start = start + dt.timedelta(minutes=index * 10)
                c_end = c_start + dt.timedelta(minutes=10)
                if c_start >= e0:
                    break
                cs = max(s0, c_start)
                ce = min(e0, c_end)
                if ce > cs:
                    target.setdefault(index, []).append((cs.timestamp(), ce.timestamp()))
                index += 1

        for row in self._fetchall(hb_sql, hb_params):
            item_start = row["ts"]
            item_end = item_start + dt.timedelta(seconds=float(row["duration_s"] or 0))
            split(human, item_start, item_end)
        for row in self._fetchall(usage_sql, usage_params):
            item_start = row["ts"]
            item_end = item_start + dt.timedelta(
                seconds=max(1, int(row["latency_ms"] or 0)) / 1000)
            split(ai, item_start, item_end)
            index = min(max(int((item_start - start).total_seconds() // 600), 0), 143)
            cost[index] = cost.get(index, 0.0) + float(row["cost_usd"] or 0)
            requests[index] = requests.get(index, 0) + 1
        cells = []
        for index in range(144):
            h = _merge_intervals(human.get(index, []))
            a = _merge_intervals(ai.get(index, []))
            mixed = _intersection_length(h, a)
            cells.append({
                "start": (start + dt.timedelta(minutes=index * 10)).isoformat(),
                # Same semantics as heatmap(): human_s includes time that was
                # also spent with AI, mixed_s reports the overlap.
                "human_s": round(_union_length(h), 2),
                "ai_s": round(_union_length(a), 2),
                "mixed_s": round(mixed, 2),
                "cost": round(cost.get(index, 0.0), 10),
                "requests": requests.get(index, 0),
            })
        return {"date": day.isoformat(), "project_key": project_key, "cells": cells,
                "granularity_s": 600}

    def calendar(self, days: int = 365, project_key: str | None = None) -> dict:
        """Per-day totals for the day/week heatmap and a rolling year view."""
        days = max(1, min(days, 730))
        end = dt.datetime.now(self.settings.tz).date()
        start = end - dt.timedelta(days=days - 1)
        usage_sql = ("""SELECT day, sum(tokens_in+tokens_out) AS tokens, count(*) AS requests,
                               sum(cost_usd) AS cost FROM usage_events WHERE day >= %s""")
        hb_sql = ("SELECT day, sum(duration_s) AS active_s FROM tracker_heartbeats "
                  "WHERE day >= %s")
        usage_params: list[Any] = [start]
        hb_params: list[Any] = [start]
        if project_key:
            usage_sql += " AND project_id = %s"
            hb_sql += " AND project_key = %s"
            usage_params.append(project_key)
            hb_params.append(project_key)
        buckets: dict[str, dict] = {
            (start + dt.timedelta(days=i)).isoformat(): {
                "date": (start + dt.timedelta(days=i)).isoformat(), "tokens": 0,
                "requests": 0, "cost": 0.0, "active_s": 0.0, "commits": 0}
            for i in range(days)}
        for row in self._fetchall(usage_sql + " GROUP BY day", usage_params):
            key = row["day"].isoformat()
            if key in buckets:
                buckets[key].update(tokens=int(row["tokens"] or 0),
                                    requests=int(row["requests"] or 0),
                                    cost=float(row["cost"] or 0))
        for row in self._fetchall(hb_sql + " GROUP BY day", hb_params):
            key = row["day"].isoformat()
            if key in buckets:
                buckets[key]["active_s"] = float(row["active_s"] or 0)
        commit_sql = "SELECT committed_at FROM tracker_commits WHERE committed_at >= %s"
        commit_params: list[Any] = [dt.datetime.combine(start, dt.time.min,
                                                        tzinfo=self.settings.tz)]
        if project_key:
            commit_sql += " AND project_key = %s"
            commit_params.append(project_key)
        for row in self._fetchall(commit_sql, commit_params):
            key = row["committed_at"].astimezone(self.settings.tz).date().isoformat()
            if key in buckets:
                buckets[key]["commits"] += 1
        return {"days": days, "project_key": project_key,
                "cells": list(buckets.values())}

    # ------------------------------------------------------------------ stats
    def timeline(self, days: int = 30, project_key: str | None = None,
                 limit: int = 100) -> list[dict]:
        where = ["1=1"]
        params: list[Any] = []
        if days > 0:
            where.append("c.committed_at >= %s")
            params.append(utcnow() - dt.timedelta(days=days))
        if project_key:
            where.append("c.project_key = %s")
            params.append(project_key)
        rows = self._fetchall(
            f"""SELECT c.*, COALESCE(a.requests,0) ai_requests,
                       COALESCE(a.tokens,0) ai_tokens, COALESCE(a.cost,0) ai_cost
                FROM tracker_commits c
                LEFT JOIN (SELECT cu.commit_id, count(*) requests,
                                  sum(u.tokens_in+u.tokens_out) tokens,
                                  sum(u.cost_usd) cost
                           FROM tracker_commit_usage cu
                           JOIN usage_events u ON u.id = cu.usage_event_id
                           GROUP BY cu.commit_id) a ON a.commit_id = c.id
                WHERE {' AND '.join(where)}
                ORDER BY c.committed_at DESC LIMIT %s""",
            params + [min(limit, 1000)])
        for row in rows:
            row["committed_at"] = _iso(row["committed_at"])
            row["observed_at"] = _iso(row["observed_at"])
            row["ai"] = {"requests": int(row.pop("ai_requests")),
                         "tokens": int(row.pop("ai_tokens")),
                         "cost": float(row.pop("ai_cost"))}
        return rows

    def sessions(self, days: int = 30, project_key: str | None = None,
                 limit: int = 100) -> list[dict]:
        clause, params = self._scope(days, project_key)
        rows = self._fetchall(
            """SELECT session_id, max(ts) AS last_seen, min(ts) AS first_seen,
                      count(*) AS requests, sum(tokens_in+tokens_out) AS tokens,
                      sum(cost_usd) AS cost, max(project_id) AS project,
                      max(branch) AS branch, max(device) AS device,
                      max(tool) AS tool, max(model) AS model, max(job) AS job
               FROM usage_events""" + clause +
            " GROUP BY session_id ORDER BY last_seen DESC LIMIT %s",
            params + [min(limit, 1000)])
        for row in rows:
            row["first_seen"] = _iso(row["first_seen"])
            row["last_seen"] = _iso(row["last_seen"])
            row["tokens"] = int(row["tokens"] or 0)
            row["cost"] = float(row["cost"] or 0)
            row["requests"] = int(row["requests"])
        return rows

    def breakdown(self, dimension: str, days: int = 30,
                  project_key: str | None = None) -> list[dict]:
        columns = {
            "model": "COALESCE(model, '(unknown)')",
            "provider": "COALESCE(provider, '(unknown)')",
            "device": "COALESCE(device, '(unknown)')",
            "tool": "COALESCE(tool, '(unknown)')",
            "job": "COALESCE(job, '(none)')",
            "branch": "COALESCE(branch, '(unknown)')",
            "kind": "COALESCE(kind, 'primary')",
            "project": "COALESCE(project_id, '(unassigned)')",
            "session": "COALESCE(session_id, '(unknown)')",
        }
        column = columns.get(dimension)
        if not column:
            raise ValueError("unknown breakdown dimension")
        clause, params = self._scope(days, project_key)
        rows = self._fetchall(
            f"""SELECT {column} AS name, count(*) AS requests,
                       sum(tokens_in+tokens_out) AS tokens, sum(cost_usd) AS cost
                FROM usage_events""" + clause +
            " GROUP BY 1 ORDER BY cost DESC NULLS LAST, requests DESC", params)
        return [{**row, "requests": int(row["requests"]),
                 "tokens": int(row["tokens"] or 0), "cost": float(row["cost"] or 0)}
                for row in rows]

    def today_summary(self) -> dict:
        today = dt.datetime.now(self.settings.tz).date()
        usage = require(self._fetchone(
            """SELECT count(*) requests, COALESCE(sum(tokens_in+tokens_out),0) tokens,
                      COALESCE(sum(cost_usd),0) cost FROM usage_events WHERE day = %s""",
            (today,)))
        hb = require(self._fetchone(
            "SELECT COALESCE(sum(duration_s),0) active_s FROM tracker_heartbeats WHERE day = %s",
            (today,)))
        return {"date": today.isoformat(), "requests": int(usage["requests"]),
                "tokens": int(usage["tokens"]), "cost": float(usage["cost"]),
                "active_s": float(hb["active_s"])}

    def heartbeats_between(self, start: dt.datetime, end: dt.datetime,
                           project_key: str | None = None,
                           limit: int = 5000) -> list[dict]:
        sql = ("SELECT * FROM tracker_heartbeats WHERE ts >= %s AND ts < %s")
        params: list[Any] = [start, end]
        if project_key:
            sql += " AND project_key = %s"
            params.append(project_key)
        rows = self._fetchall(sql + " ORDER BY ts LIMIT %s", params + [min(limit, 20000)])
        for row in rows:
            row["ts"] = _iso(row["ts"])
            row["day"] = _iso(row["day"])
        return rows

    def project_files(self, project_key: str, days: int = 30,
                      limit: int = 50) -> list[dict]:
        where = ["c.project_key = %s"]
        params: list[Any] = [project_key]
        if days > 0:
            where.append("c.committed_at >= %s")
            params.append(utcnow() - dt.timedelta(days=days))
        rows = self._fetchall(
            """SELECT f.path, count(*) AS touches, sum(f.additions) AS additions,
                      sum(f.deletions) AS deletions, max(c.committed_at) AS last_touched
               FROM tracker_commit_files f JOIN tracker_commits c ON c.id = f.commit_id
               WHERE """ + " AND ".join(where) +
            " GROUP BY f.path ORDER BY touches DESC, additions DESC LIMIT %s",
            params + [min(limit, 500)])
        for row in rows:
            row["last_touched"] = _iso(row["last_touched"])
            row["touches"] = int(row["touches"])
            row["additions"] = int(row["additions"] or 0)
            row["deletions"] = int(row["deletions"] or 0)
        return rows

    # ------------------------------------------------------------------ model stats
    def provider_models(self, days: int = 30,
                        project_key: str | None = None) -> dict:
        """Usage/cost per model, grouped by provider, with each model's share."""
        clause, params = self._scope(days, project_key)
        rows = self._fetchall(
            """SELECT COALESCE(provider, '(unknown)') AS provider,
                      COALESCE(model, '(unknown)') AS model,
                      count(*) AS requests,
                      COALESCE(sum(tokens_in), 0) AS tokens_in,
                      COALESCE(sum(tokens_out), 0) AS tokens_out,
                      COALESCE(sum(cost_usd), 0) AS cost
               FROM usage_events""" + clause +
            " GROUP BY 1, 2 ORDER BY 1, cost DESC, requests DESC", params)
        observed = self.observed_metrics(providers={row["provider"] for row in rows})
        providers: dict[str, dict] = {}
        total_cost = 0.0
        for row in rows:
            entry = providers.setdefault(row["provider"], {
                "provider": row["provider"], "requests": 0, "tokens": 0,
                "tokens_in": 0, "tokens_out": 0, "cost": 0.0, "models": []})
            item = {
                "model": row["model"],
                "requests": int(row["requests"]),
                "tokens_in": int(row["tokens_in"]),
                "tokens_out": int(row["tokens_out"]),
                "tokens": int(row["tokens_in"]) + int(row["tokens_out"]),
                "cost": float(row["cost"]),
                "observed": observed.get(row["model"]),
            }
            entry["models"].append(item)
            entry["requests"] += item["requests"]
            entry["tokens_in"] += item["tokens_in"]
            entry["tokens_out"] += item["tokens_out"]
            entry["tokens"] += item["tokens"]
            entry["cost"] += item["cost"]
            total_cost += item["cost"]
        out = sorted(providers.values(), key=lambda p: p["cost"], reverse=True)
        for provider in out:
            provider["cost"] = round(provider["cost"], 10)
            for item in provider["models"]:
                item["cost"] = round(item["cost"], 10)
                item["share"] = round(item["cost"] / total_cost * 100, 2) if total_cost else 0.0
        return {"days": days, "project_key": project_key,
                "total_cost": round(total_cost, 10), "providers": out}

    def model_detail(self, model: str, days: int = 90,
                     project_key: str | None = None) -> dict:
        """Everything the UI needs for one model: totals, cost history, breakdowns."""
        where = ["model = %s"]
        params: list[Any] = [model]
        if days > 0:
            where.append("day >= %s")
            params.append(dt.datetime.now(self.settings.tz).date()
                          - dt.timedelta(days=days - 1))
        if project_key:
            where.append("project_id = %s")
            params.append(project_key)
        clause = " WHERE " + " AND ".join(where)
        totals = require(self._fetchone(
            """SELECT count(*) AS requests,
                      COALESCE(sum(tokens_in), 0) AS tokens_in,
                      COALESCE(sum(tokens_out), 0) AS tokens_out,
                      COALESCE(sum(cost_usd), 0) AS cost,
                      percentile_cont(0.5) WITHIN GROUP (ORDER BY latency_ms) AS p50,
                      percentile_cont(0.95) WITHIN GROUP (ORDER BY latency_ms) AS p95,
                      percentile_cont(0.5) WITHIN GROUP (ORDER BY ttft_ms) AS ttft_p50,
                      avg(CASE WHEN outcome = 'success' THEN 100.0 ELSE 0.0 END) AS success_rate
               FROM usage_events""" + clause, params))
        daily = self._fetchall(
            """SELECT day, count(*) AS requests, COALESCE(sum(tokens_in), 0) AS tokens_in,
                      COALESCE(sum(tokens_out), 0) AS tokens_out,
                      COALESCE(sum(cost_usd), 0) AS cost
               FROM usage_events""" + clause + " GROUP BY day ORDER BY day", params)

        def group(column: str, limit: int = 20) -> list[dict]:
            rows = self._fetchall(
                f"""SELECT COALESCE({column}, '(unknown)') AS name, count(*) AS requests,
                           COALESCE(sum(tokens_in + tokens_out), 0) AS tokens,
                           COALESCE(sum(cost_usd), 0) AS cost
                    FROM usage_events""" + clause +
                " GROUP BY 1 ORDER BY cost DESC NULLS LAST, requests DESC LIMIT %s",
                params + [limit])
            return [{"name": r["name"], "requests": int(r["requests"]),
                     "tokens": int(r["tokens"]), "cost": float(r["cost"])} for r in rows]

        sessions = self._fetchall(
            """SELECT session_id, count(*) AS requests, max(ts) AS last_seen,
                      COALESCE(sum(cost_usd), 0) AS cost
               FROM usage_events""" + clause +
            " GROUP BY session_id ORDER BY last_seen DESC LIMIT 25", params)
        return {
            "model": model,
            "range": {"days": days, "project_key": project_key},
            "totals": {
                "requests": int(totals["requests"]),
                "tokens_in": int(totals["tokens_in"]),
                "tokens_out": int(totals["tokens_out"]),
                "tokens": int(totals["tokens_in"]) + int(totals["tokens_out"]),
                "cost": float(totals["cost"]),
                "latency_p50_ms": float(totals["p50"]) if totals["p50"] is not None else None,
                "latency_p95_ms": float(totals["p95"]) if totals["p95"] is not None else None,
                "ttft_p50_ms": float(totals["ttft_p50"]) if totals["ttft_p50"] is not None else None,
                "success_rate": float(totals["success_rate"] or 0),
            },
            "daily": [{"date": r["day"].isoformat(), "requests": int(r["requests"]),
                       "tokens_in": int(r["tokens_in"]), "tokens_out": int(r["tokens_out"]),
                       "tokens": int(r["tokens_in"]) + int(r["tokens_out"]),
                       "cost": float(r["cost"])} for r in daily],
            "providers": group("provider"),
            "projects": group("project_id"),
            "devices": group("device"),
            "tools": group("tool"),
            "jobs": group("job"),
            "kinds": group("kind"),
            "sessions": [{"session_id": r["session_id"], "requests": int(r["requests"]),
                          "cost": float(r["cost"]), "last_seen": _iso(r["last_seen"])}
                         for r in sessions],
            "observed": self.observed_metrics().get(model),
        }

    def status(self) -> dict:
        counts = require(self._fetchone(
            """SELECT (SELECT count(*) FROM usage_events) usage_events,
                      (SELECT count(*) FROM tracker_projects) projects,
                      (SELECT count(*) FROM tracker_heartbeats) heartbeats,
                      (SELECT count(*) FROM tracker_commits) commits,
                      (SELECT count(*) FROM tracker_budgets) budgets,
                      (SELECT count(*) FROM tracker_job_profiles) jobs"""))
        return {"enabled": True, **{k: int(v) for k, v in counts.items()}}

    def clear(self) -> None:
        with self._lock, self._conn.transaction():
            self._conn.execute(
                "TRUNCATE usage_events, tracker_projects, tracker_project_aliases, "
                "tracker_heartbeats, tracker_git_snapshots, tracker_tool_events, "
                "tracker_commits, tracker_commit_files, tracker_commit_usage, "
                "tracker_budgets, tracker_presence, tracker_job_profiles, "
                "tracker_rejected_requests, tracker_refresh_runs, router_model_stats "
                "RESTART IDENTITY CASCADE")
