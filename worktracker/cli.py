"""Command-line maintenance for the work tracker database.

Run it as a module so it works both from a checkout and inside the container::

    python -m worktracker migrate      # create or upgrade every tracker table
    python -m worktracker backfill     # project rows for legacy usage events
    python -m worktracker recompute    # re-derive commit -> AI attribution
    python -m worktracker status       # connectivity check plus row counts

The connection string comes from ``TRACKER_DATABASE_URL`` (or ``DATABASE_URL`` /
``ROUTER_DATABASE_URL``).  ``migrate`` is idempotent: opening a
:class:`~worktracker.store.TrackerStore` already applies ``CREATE TABLE IF NOT
EXISTS`` / ``ALTER TABLE ... ADD COLUMN IF NOT EXISTS`` statements, so the
command simply connects and reports what is present.
"""
from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from .config import Settings
from .store import TrackerStore


def _connect() -> TrackerStore:
    settings = Settings.from_env()
    if not settings.database_url:
        raise SystemExit(
            "TRACKER_DATABASE_URL (or DATABASE_URL / ROUTER_DATABASE_URL) must be set")
    return TrackerStore(settings.database_url, settings)


def cmd_migrate(_: argparse.Namespace) -> int:
    store = _connect()
    try:
        row = store._fetchone(
            """SELECT count(*) AS tables FROM information_schema.tables
               WHERE table_schema = 'public'
                 AND (table_name LIKE 'tracker\\_%%' OR table_name = 'usage_events')""")
        tables = int((row or {}).get("tables") or 0)
        print(f"schema ready: {tables} tracker tables")
    finally:
        store.close()
    return 0


def cmd_backfill(_: argparse.Namespace) -> int:
    store = _connect()
    try:
        created = store.sync_projects_from_usage()
        print(f"backfilled {created} project row(s) from usage_events")
    finally:
        store.close()
    return 0


def cmd_recompute(args: argparse.Namespace) -> int:
    store = _connect()
    try:
        projects = store.list_projects(0)
        recomputed = 0
        for project in projects:
            key = project["key"]
            for commit in store.commits(key, days=args.days, limit=args.limit):
                store.recompute_commit(key, commit["sha"])
                recomputed += 1
        print(f"recomputed attribution for {recomputed} commit(s) "
              f"across {len(projects)} project(s)")
    finally:
        store.close()
    return 0


def cmd_status(_: argparse.Namespace) -> int:
    store = _connect()
    try:
        print(json.dumps(store.status(), indent=2, sort_keys=True))
    finally:
        store.close()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="worktracker",
                                     description="Work tracker database maintenance")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("migrate", help="create or upgrade the tracker schema")
    sub.add_parser("backfill", help="create project rows for legacy usage events")
    recompute = sub.add_parser("recompute", help="re-derive commit -> AI attribution")
    recompute.add_argument("--days", type=int, default=0,
                           help="only commits from the last N days (0 = all)")
    recompute.add_argument("--limit", type=int, default=200,
                           help="maximum commits per project")
    sub.add_parser("status", help="print connectivity and row counts as JSON")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    handlers: dict[str, Any] = {
        "migrate": cmd_migrate, "backfill": cmd_backfill,
        "recompute": cmd_recompute, "status": cmd_status,
    }
    return int(handlers[args.command](args))


if __name__ == "__main__":
    sys.exit(main())
