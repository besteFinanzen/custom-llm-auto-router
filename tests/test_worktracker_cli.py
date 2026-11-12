"""Unit tests for ``python -m worktracker`` maintenance commands."""
from __future__ import annotations

import subprocess
import sys

import pytest

from worktracker import cli


class FakeStore:
    def __init__(self, *, tables: int = 15, backfill: int = 0,
                 projects: list[dict] | None = None,
                 commits: list[dict] | None = None) -> None:
        self.tables = tables
        self.backfill = backfill
        self.projects = projects or []
        self.commit_rows = commits or []
        self.recomputed: list[tuple[str, str]] = []
        self.closed = False

    def _fetchone(self, sql, params=()):
        return {"tables": self.tables}

    def sync_projects_from_usage(self) -> int:
        return self.backfill

    def list_projects(self, days: int = 0) -> list[dict]:
        return self.projects

    def commits(self, key: str, days: int = 0, limit: int = 200) -> list[dict]:
        return self.commit_rows

    def recompute_commit(self, key: str, sha: str) -> dict:
        self.recomputed.append((key, sha))
        return {}

    def status(self) -> dict:
        return {"enabled": True, "usage_events": 3}

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def fake(monkeypatch):
    store = FakeStore(tables=15, backfill=2,
                      projects=[{"key": "path:/a"}, {"key": "path:/b"}],
                      commits=[{"sha": "abc"}, {"sha": "def"}])
    monkeypatch.setattr(cli, "_connect", lambda: store)
    return store


def test_migrate_reports_schema(fake, capsys):
    assert cli.cmd_migrate(cli.build_parser().parse_args(["migrate"])) == 0
    assert "15 tracker tables" in capsys.readouterr().out
    assert fake.closed is True


def test_backfill_reports_projects(fake, capsys):
    assert cli.cmd_backfill(cli.build_parser().parse_args(["backfill"])) == 0
    assert "backfilled 2 project row(s)" in capsys.readouterr().out


def test_recompute_iterates_projects_and_commits(fake, capsys):
    args = cli.build_parser().parse_args(["recompute", "--days", "7", "--limit", "5"])
    assert cli.cmd_recompute(args) == 0
    assert fake.recomputed == [("path:/a", "abc"), ("path:/a", "def"),
                               ("path:/b", "abc"), ("path:/b", "def")]
    assert "4 commit(s) across 2 project(s)" in capsys.readouterr().out


def test_status_prints_json(fake, capsys):
    assert cli.cmd_status(cli.build_parser().parse_args(["status"])) == 0
    assert '"usage_events": 3' in capsys.readouterr().out


def test_main_dispatches_to_handler(fake):
    assert cli.main(["status"]) == 0


def test_connect_requires_database_url(monkeypatch):
    class NoUrl:
        database_url = ""

        @classmethod
        def from_env(cls):
            return cls()

    monkeypatch.setattr(cli, "Settings", NoUrl)
    with pytest.raises(SystemExit, match="TRACKER_DATABASE_URL"):
        cli._connect()


def test_parser_requires_a_command():
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args([])


def test_module_entrypoint_runs_help():
    result = subprocess.run([sys.executable, "-m", "worktracker", "--help"],
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0
    assert "migrate" in result.stdout
