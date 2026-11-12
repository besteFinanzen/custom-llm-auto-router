"""Durable PostgreSQL usage and work-event store.

Routing state stays in JSON: it is small and needs synchronous budget checks.
This store owns durable analytics: per-request usage, project attribution,
unlimited daily history, and work/activity events for the coding tracker.
"""
from __future__ import annotations

import datetime as dt
import json
import threading
import uuid
from collections.abc import Mapping
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

MIGRATION_KEY = "json-state-v1"
DIMENSIONS = {
    "provider": "provider",
    "preset": "COALESCE(preset, 'direct')",
    "model": "COALESCE(model, '(unknown)')",
}


def _today() -> dt.date:
    return dt.date.today()


def _date(value: Any) -> dt.date:
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    return dt.date.fromisoformat(str(value)[:10])


def _timestamp(value: Any) -> dt.datetime:
    if isinstance(value, dt.datetime):
        parsed = value
    elif value:
        parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    else:
        parsed = dt.datetime.now(dt.timezone.utc)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed


def _agg(rows: Mapping | tuple | None = None, **values: Any) -> dict:
    base = {"in": 0, "out": 0, "tokens": 0, "reqs": 0, "cost": 0.0}
    if rows:
        if not isinstance(rows, Mapping):
            values_in, values_out, reqs, cost = rows
            rows = {"tokens_in": values_in, "tokens_out": values_out,
                    "reqs": reqs, "cost": cost}
        base["in"] = int(rows.get("tokens_in") or 0)
        base["out"] = int(rows.get("tokens_out") or 0)
        base["reqs"] = int(rows.get("reqs") or 0)
        base["cost"] = round(float(rows.get("cost") or 0.0), 10)
    for key, value in values.items():
        if key in base:
            base[key] += value
    base["tokens"] = base["in"] + base["out"]
    base["cost"] = round(base["cost"], 10)
    return base


def _add(left: dict, right: dict) -> dict:
    for key in ("in", "out", "reqs"):
        left[key] = int(left.get(key, 0)) + int(right.get(key, 0))
    left["cost"] = round(float(left.get("cost", 0.0)) + float(right.get("cost", 0.0)), 10)
    left["tokens"] = left["in"] + left["out"]
    return left


def _clean_metadata(value: Any, depth: int = 0) -> Any:
    """Keep operational metadata, never prompt/response bodies."""
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


class PostgresUsage:
    """Synchronous psycopg store, deliberately serialized for tiny queries."""

    def __init__(self, url: str):
        if not url:
            raise ValueError("PostgreSQL URL is empty")
        self.url = url
        self._lock = threading.RLock()
        self._conn = psycopg.connect(url, connect_timeout=8, autocommit=True)
        self._init_schema()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _init_schema(self) -> None:
        statements = [
            """CREATE TABLE IF NOT EXISTS router_meta (
                   key text PRIMARY KEY, value text NOT NULL)""",
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
                   tokens_in bigint NOT NULL DEFAULT 0 CHECK (tokens_in >= 0),
                   tokens_out bigint NOT NULL DEFAULT 0 CHECK (tokens_out >= 0),
                   cost_usd numeric(20,10) NOT NULL DEFAULT 0,
                   estimated boolean NOT NULL DEFAULT false,
                   latency_ms integer,
                   stream boolean NOT NULL DEFAULT false)""",
            "CREATE INDEX IF NOT EXISTS usage_events_day_idx ON usage_events (day)",
            "CREATE INDEX IF NOT EXISTS usage_events_project_day_idx ON usage_events (project_id, day)",
            "CREATE INDEX IF NOT EXISTS usage_events_model_day_idx ON usage_events (model, day)",
            """CREATE TABLE IF NOT EXISTS legacy_totals (
                   dimension text NOT NULL,
                   key text NOT NULL,
                   tokens_in bigint NOT NULL DEFAULT 0,
                   tokens_out bigint NOT NULL DEFAULT 0,
                   reqs bigint NOT NULL DEFAULT 0,
                   cost_usd numeric(20,10) NOT NULL DEFAULT 0,
                   PRIMARY KEY (dimension, key))""",
            """CREATE TABLE IF NOT EXISTS legacy_daily (
                   day date PRIMARY KEY,
                   tokens_in bigint NOT NULL DEFAULT 0,
                   tokens_out bigint NOT NULL DEFAULT 0,
                   reqs bigint NOT NULL DEFAULT 0,
                   cost_usd numeric(20,10) NOT NULL DEFAULT 0)""",
            """CREATE TABLE IF NOT EXISTS legacy_provider_daily (
                   day date NOT NULL,
                   provider text NOT NULL,
                   tokens_in bigint NOT NULL DEFAULT 0,
                   tokens_out bigint NOT NULL DEFAULT 0,
                   reqs bigint NOT NULL DEFAULT 0,
                   cost_usd numeric(20,10) NOT NULL DEFAULT 0,
                   PRIMARY KEY (day, provider))""",
            """CREATE TABLE IF NOT EXISTS work_events (
                   id uuid PRIMARY KEY,
                   ts timestamptz NOT NULL,
                   day date NOT NULL,
                   session_id text,
                   project_id text,
                   project_root text,
                   worktree text,
                   source text NOT NULL,
                   actor text NOT NULL CHECK (actor IN ('human', 'ai', 'mixed')),
                   kind text NOT NULL,
                   category text,
                   duration_s double precision NOT NULL DEFAULT 0 CHECK (duration_s >= 0),
                   metadata jsonb NOT NULL DEFAULT '{}'::jsonb)""",
            "CREATE INDEX IF NOT EXISTS work_events_day_idx ON work_events (day)",
            "CREATE INDEX IF NOT EXISTS work_events_project_day_idx ON work_events (project_id, day)",
        ]
        with self._lock:
            for statement in statements:
                self._conn.execute(statement)

    # ---- migration -------------------------------------------------------
    def migrate_state(self, data: Mapping) -> bool:
        """Import JSON aggregates once. Returns True when this call migrated."""
        with self._lock:
            done = self._conn.execute(
                "SELECT value FROM router_meta WHERE key = %s", (MIGRATION_KEY,)).fetchone()
            if done:
                return False

        totals = data.get("totals") or {}
        presets = data.get("by_preset") or {}
        models = data.get("by_model") or {}
        daily = data.get("daily_history") or {}
        usage = data.get("usage") or {}
        with self._lock, self._conn.transaction():
            for dimension, values in (("provider", totals), ("preset", presets), ("model", models)):
                for key, value in values.items():
                    if not key:
                        continue
                    self._conn.execute(
                        """INSERT INTO legacy_totals
                           (dimension, key, tokens_in, tokens_out, reqs, cost_usd)
                           VALUES (%s, %s, %s, %s, %s, %s)
                           ON CONFLICT (dimension, key) DO NOTHING""",
                        (dimension, str(key), int(value.get("in") or 0),
                         int(value.get("out") or 0), int(value.get("reqs") or 0),
                         float(value.get("cost") or 0.0)))
            for day, value in daily.items():
                self._conn.execute(
                    """INSERT INTO legacy_daily
                       (day, tokens_in, tokens_out, reqs, cost_usd)
                       VALUES (%s, %s, %s, %s, %s) ON CONFLICT (day) DO NOTHING""",
                    (_date(day), int(value.get("in") or 0), int(value.get("out") or 0),
                     int(value.get("reqs") or 0), float(value.get("cost") or 0.0)))
            for day, providers in usage.items():
                for provider, value in (providers or {}).items():
                    self._conn.execute(
                        """INSERT INTO legacy_provider_daily
                           (day, provider, tokens_in, tokens_out, reqs, cost_usd)
                           VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING""",
                        (_date(day), str(provider), int(value.get("in") or 0),
                         int(value.get("out") or 0), int(value.get("reqs") or 0),
                         float(value.get("cost") or 0.0)))
            self._conn.execute(
                "INSERT INTO router_meta (key, value) VALUES (%s, %s) "
                "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
                (MIGRATION_KEY, dt.datetime.now(dt.timezone.utc).isoformat()))
        return True

    # ---- writes ----------------------------------------------------------
    def record_usage(self, event: Mapping) -> str:
        event_id = str(event.get("id") or uuid.uuid4())
        ts = _timestamp(event.get("ts"))
        with self._lock:
            self._conn.execute(
                """INSERT INTO usage_events
                   (id, ts, day, session_id, project_id, project_root, worktree,
                    kind, requested_preset, preset, provider, model,
                    tokens_in, tokens_out, cost_usd, estimated, latency_ms, stream)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (id) DO NOTHING""",
                (event_id, ts, ts.astimezone().date(), event.get("session"), event.get("project_id"),
                 event.get("project_root"), event.get("worktree"),
                 event.get("kind") or "primary", event.get("requested_preset"),
                 event.get("preset"), event.get("provider"), event.get("model"),
                 int(event.get("in") or 0), int(event.get("out") or 0),
                 float(event.get("cost") or 0.0), bool(event.get("estimated")),
                 event.get("latency_ms"), bool(event.get("stream"))))
        return event_id

    def record_work_event(self, event: Mapping) -> str:
        actor = str(event.get("actor") or "").lower()
        if actor not in ("human", "ai", "mixed"):
            raise ValueError("actor must be human, ai or mixed")
        event_id = str(event.get("id") or uuid.uuid4())
        ts = _timestamp(event.get("ts"))
        duration = float(event.get("duration_s") or 0)
        if duration < 0 or duration > 86400:
            raise ValueError("duration_s must be between 0 and 86400")
        with self._lock:
            self._conn.execute(
                """INSERT INTO work_events
                   (id, ts, day, session_id, project_id, project_root, worktree,
                    source, actor, kind, category, duration_s, metadata)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (id) DO NOTHING""",
                (event_id, ts, ts.astimezone().date(), event.get("session"), event.get("project_id"),
                 event.get("project_root"), event.get("worktree"),
                 str(event.get("source") or "api")[:80], actor,
                 str(event.get("kind") or "activity")[:80],
                 str(event.get("category") or "")[:120] or None, duration,
                 Jsonb(_clean_metadata(event.get("metadata") or {}))))
        return event_id

    # ---- reads -----------------------------------------------------------
    @staticmethod
    def _scope(days: int, project: str | None) -> tuple[str, list[Any]]:
        where, params = [], []
        if days > 0:
            where.append("day >= %s")
            params.append(_today() - dt.timedelta(days=days - 1))
        if project == "(unassigned)":
            where.append("project_id IS NULL AND project_root IS NULL")
        elif project:
            where.append("(project_id = %s OR project_root = %s)")
            params += [project, project]
        return (" WHERE " + " AND ".join(where)) if where else "", params

    def _event_aggregate(self, days: int, project: str | None) -> dict:
        clause, params = self._scope(days, project)
        row = self._conn.execute(
            """SELECT COALESCE(sum(tokens_in),0) AS tokens_in,
                      COALESCE(sum(tokens_out),0) AS tokens_out,
                      count(*) AS reqs, COALESCE(sum(cost_usd),0) AS cost
               FROM usage_events""" + clause, params).fetchone()
        return _agg(row)

    def _legacy_totals(self, dimension: str) -> dict:
        row = self._conn.execute(
            """SELECT COALESCE(sum(tokens_in),0) AS tokens_in,
                      COALESCE(sum(tokens_out),0) AS tokens_out,
                      COALESCE(sum(reqs),0) AS reqs, COALESCE(sum(cost_usd),0) AS cost
               FROM legacy_totals WHERE dimension = %s""", (dimension,)).fetchone()
        return _agg(row)

    def _legacy_range(self, days: int) -> dict:
        cutoff = _today() - dt.timedelta(days=days - 1)
        row = self._conn.execute(
            """SELECT COALESCE(sum(tokens_in),0) AS tokens_in,
                      COALESCE(sum(tokens_out),0) AS tokens_out,
                      COALESCE(sum(reqs),0) AS reqs, COALESCE(sum(cost_usd),0) AS cost
               FROM legacy_daily WHERE day >= %s""", (cutoff,)).fetchone()
        return _agg(row)

    def _summary(self, days: int, project: str | None) -> dict:
        events = self._event_aggregate(days, project)
        if project:
            return events
        legacy = self._legacy_totals("provider") if days == 0 else self._legacy_range(days)
        return _add(legacy, events)

    def _breakdown(self, dimension: str, days: int, project: str | None) -> list[dict]:
        column = DIMENSIONS[dimension]
        clause, params = self._scope(days, project)
        rows = self._conn.execute(
            f"""SELECT {column} AS key,
                       COALESCE(sum(tokens_in),0) AS tokens_in,
                       COALESCE(sum(tokens_out),0) AS tokens_out,
                       count(*) AS reqs, COALESCE(sum(cost_usd),0) AS cost
                FROM usage_events{clause} GROUP BY 1""", params).fetchall()
        out: dict[str, dict] = {}
        for row in rows:
            key = row[0] or "(unknown)"
            out[key] = _agg({"tokens_in": row[1], "tokens_out": row[2],
                             "reqs": row[3], "cost": row[4]})
        if not project and days == 0:
            for row in self._conn.execute(
                    "SELECT key, tokens_in, tokens_out, reqs, cost_usd FROM legacy_totals "
                    "WHERE dimension = %s", (dimension,)).fetchall():
                key = row[0]
                value = _agg({"tokens_in": row[1], "tokens_out": row[2],
                               "reqs": row[3], "cost": row[4]})
                if key in out:
                    _add(out[key], value)
                else:
                    out[key] = value
        elif not project and dimension == "provider":
            cutoff = _today() - dt.timedelta(days=days - 1)
            for row in self._conn.execute(
                    """SELECT provider, COALESCE(sum(tokens_in),0), COALESCE(sum(tokens_out),0),
                              COALESCE(sum(reqs),0), COALESCE(sum(cost_usd),0)
                       FROM legacy_provider_daily WHERE day >= %s GROUP BY provider""",
                    (cutoff,)).fetchall():
                value = _agg({"tokens_in": row[1], "tokens_out": row[2],
                               "reqs": row[3], "cost": row[4]})
                if row[0] in out:
                    _add(out[row[0]], value)
                else:
                    out[row[0]] = value
        return [{"key": key, **value} for key, value in
                sorted(out.items(), key=lambda item: (-item[1]["cost"], -item[1]["reqs"], item[0]))]

    def _daily(self, days: int, project: str | None) -> list[dict]:
        cutoff = _today() - dt.timedelta(days=days - 1) if days > 0 else None
        by_day: dict[dt.date, dict] = {}
        # legacy daily contributions: only when project is None (global)
        if not project:
            query = "SELECT day, tokens_in, tokens_out, reqs, cost_usd FROM legacy_daily"
            params = []
            if cutoff:
                query += " WHERE day >= %s"
                params.append(cutoff)
            for row in self._conn.execute(query, params).fetchall():
                by_day[row[0]] = _agg({"tokens_in": row[1], "tokens_out": row[2],
                                        "reqs": row[3], "cost": row[4]})
        # new usage events contributions: always, but filtered by days and project scope
        clause, params = self._scope(days, project)
        for row in self._conn.execute(
                f"""SELECT day, COALESCE(sum(tokens_in),0), COALESCE(sum(tokens_out),0),
                           count(*), COALESCE(sum(cost_usd),0)
                    FROM usage_events{clause} GROUP BY day""", params).fetchall():
            value = _agg({"tokens_in": row[1], "tokens_out": row[2],
                          "reqs": row[3], "cost": row[4]})
            if row[0] in by_day:
                _add(by_day[row[0]], value)
            else:
                by_day[row[0]] = value
        if not by_day:
            return []
        start = cutoff or min(by_day)
        end = _today()
        out = []
        day = start
        while day <= end:
            value = by_day.get(day) or _agg()
            out.append({"date": day.isoformat(), **value})
            day += dt.timedelta(days=1)
        return out

    def _projects(self, days: int, selected: str | None) -> list[dict]:
        if selected:
            where = " WHERE (project_id = %s OR project_root = %s)"
            params: list[Any] = [selected, selected]
        elif selected == "(unassigned)":
            where = " WHERE project_id IS NULL AND project_root IS NULL"
            params = []
        elif days > 0:
            where = " WHERE day >= %s"
            params = [_today() - dt.timedelta(days=days - 1)]
        else:
            where, params = "", []
        rows = self._conn.execute(
            """SELECT COALESCE(project_id, project_root, '(unassigned)') AS key,
                      max(project_root), COALESCE(sum(tokens_in),0),
                      COALESCE(sum(tokens_out),0), count(*),
                      COALESCE(sum(cost_usd),0), max(ts)
               FROM usage_events""" + where + " GROUP BY 1 ORDER BY 6 DESC, 5 DESC",
            params).fetchall()
        return [{"id": row[0], "root": row[1], "in": int(row[2]), "out": int(row[3]),
                 "tokens": int(row[2]) + int(row[3]), "reqs": int(row[4]),
                 "cost": round(float(row[5]), 10),
                 "last_ts": row[6].isoformat() if row[6] else None}
                for row in rows]

    def _work_summary(self, days: int, project: str | None) -> dict:
        clause, params = self._scope(days, project)
        rows = self._conn.execute(
            "SELECT actor, COALESCE(sum(duration_s),0), count(*) FROM work_events" +
            clause + " GROUP BY actor", params).fetchall()
        by_actor = {row[0]: {"seconds": round(float(row[1]), 2), "events": int(row[2])}
                    for row in rows}
        return {
            "human": by_actor.get("human", {"seconds": 0, "events": 0}),
            "ai": by_actor.get("ai", {"seconds": 0, "events": 0}),
            "mixed": by_actor.get("mixed", {"seconds": 0, "events": 0}),
        }

    def history(self, days: int = 0, metric: str = "tokens",
                project: str | None = None) -> dict:
        if metric not in ("tokens", "input", "output", "cost", "requests"):
            raise ValueError("metric must be tokens, input, output, cost or requests")
        if days < 0 or days > 36500:
            raise ValueError("days must be 0 (all) or a positive number of days")
        with self._lock:
            summary = self._summary(days, project)
            all_time = self._summary(0, project)
            events_all_time = self._event_aggregate(0, project)
            first = self._conn.execute("SELECT min(day) FROM usage_events").fetchone()[0]
            if not project:
                legacy_first = self._conn.execute("SELECT min(day) FROM legacy_daily").fetchone()[0]
                if legacy_first and (not first or legacy_first < first):
                    first = legacy_first
            projects = self._projects(0, None)
            return {
                "days": days,
                "metric": metric,
                "project": project,
                "all_time": all_time,
                "range": summary,
                "tracked": {**events_all_time, "since": first.isoformat() if first else None},
                "daily": self._daily(days, project),
                "providers": self._breakdown("provider", days, project),
                "presets": self._breakdown("preset", days, project),
                "models": self._breakdown("model", days, project),
                "projects": projects,
                "work": self._work_summary(days, project),
                "migrated": bool(self._conn.execute(
                    "SELECT 1 FROM router_meta WHERE key = %s", (MIGRATION_KEY,)).fetchone()),
            }

    def projects(self, days: int = 0) -> list[dict]:
        with self._lock:
            return self._projects(days, None)

    def all_time(self) -> dict:
        with self._lock:
            return self._summary(0, None)

    def status(self) -> dict:
        with self._lock:
            row = self._conn.execute(
                "SELECT (SELECT count(*) FROM usage_events), "
                "(SELECT count(*) FROM work_events), "
                "(SELECT min(day) FROM usage_events)").fetchone()
            migrated = bool(self._conn.execute(
                "SELECT 1 FROM router_meta WHERE key = %s", (MIGRATION_KEY,)).fetchone())
        return {"enabled": True, "events": int(row[0]), "work_events": int(row[1]),
                "since": row[2].isoformat() if row[2] else None, "migrated": migrated}

    def clear(self) -> None:
        """Test helper; intentionally not exposed through HTTP."""
        with self._lock, self._conn.transaction():
            self._conn.execute("TRUNCATE usage_events, work_events, legacy_totals, "
                               "legacy_daily, legacy_provider_daily, router_meta")

    @staticmethod
    def dumps(value: Any) -> str:
        return json.dumps(value, default=str)
