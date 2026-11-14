"""Unit tests for merging the Worktracker registry into ``/v1/history``."""
from __future__ import annotations

import pytest

from gateway import _sync_history_projects


def _row(project_id: str, *, in_: int = 0, out: int = 0, tokens: int = 0,
         reqs: int = 0, cost: float = 0.0, last_ts: str | None = None,
         root: str | None = None) -> dict:
    return {
        "id": project_id, "root": root, "in": in_, "out": out, "tokens": tokens,
        "reqs": reqs, "cost": cost, "last_ts": last_ts,
    }


def test_sync_history_projects_merges_aliases_and_registry_metadata():
    data = {
        "projects": [
            _row("legacy-a", root="/repo/a", in_=10, out=5, tokens=15, reqs=2,
                 cost=0.5, last_ts="2026-01-02T00:00:00+00:00"),
            _row("path:/repo/a", root="/repo/a", in_=1, out=1, tokens=2, reqs=1,
                 cost=0.1, last_ts="2026-01-03T00:00:00+00:00"),
            _row("solo", reqs=1),
        ],
    }
    registry = [
        {"key": "path:/repo/a", "display_name": "Repo A", "color": "#336699",
         "root_path": "/repo/a", "archived": False,
         "aliases": ["legacy-a", "/repo/a"]},
        {"key": "path:/repo/b", "display_name": "Repo B", "color": None,
         "root_path": "/repo/b", "archived": True, "aliases": []},
    ]

    out = _sync_history_projects(data, registry)

    assert out is data
    projects = {row["id"]: row for row in out["projects"]}
    assert set(projects) == {"path:/repo/a", "path:/repo/b", "solo"}
    merged = projects["path:/repo/a"]
    assert merged["in"] == 11
    assert merged["out"] == 6
    assert merged["tokens"] == 17
    assert merged["reqs"] == 3
    assert merged["cost"] == pytest.approx(0.6)
    assert merged["last_ts"] == "2026-01-03T00:00:00+00:00"
    assert merged["name"] == "Repo A"
    assert merged["color"] == "#336699"
    assert merged["root"] == "/repo/a"

    # Registry-only projects appear with zero usage, so a tracker-created
    # project is visible on the dashboard before its first request.
    empty = projects["path:/repo/b"]
    assert empty["reqs"] == 0 and empty["cost"] == 0
    assert empty["root"] == "/repo/b"
    assert empty["name"] == "Repo B"
    assert empty["archived"] is True

    # Unregistered usage rows are preserved, and sorting is cost, requests, name.
    assert projects["solo"].get("name") is None
    assert [row["id"] for row in out["projects"]] == [
        "path:/repo/a", "solo", "path:/repo/b"]


def test_sync_history_projects_is_idempotent():
    registry = [{"key": "path:/repo/a", "display_name": "Repo A", "color": None,
                 "root_path": "/repo/a", "archived": False, "aliases": ["/repo/a"]}]
    first = _sync_history_projects(
        {"projects": [_row("/repo/a", in_=3, out=2, tokens=5, reqs=1, cost=0.25)]},
        registry)
    second = _sync_history_projects(
        {"projects": [dict(row) for row in first["projects"]]}, registry)
    assert second["projects"] == first["projects"]
