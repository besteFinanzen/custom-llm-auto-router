"""Benchmark parsers, atomic refresh snapshots and scheduler behaviour."""
from __future__ import annotations

import asyncio
import datetime as dt
import inspect
import textwrap

import pytest

from worktracker import benchmarks, refresh_scheduler


def test_normalize_score_and_all_parsers() -> None:
    assert benchmarks.normalize(" OpenAI/GPT-4.1 (Mini) ") == "gpt41"
    assert benchmarks.normalize(None) == ""
    assert benchmarks._score("0.75") == 75.0
    assert benchmarks._score(101) == 100
    assert benchmarks._score(-1) == 0
    assert benchmarks._score("n/a") is None
    aider = """
- model: openai/gpt-4.1
  pass_rate_2: 0.75
- model: claude-sonnet-4
  pass_rate: 65.4
- model: unscored
- not-a-record
"""
    assert benchmarks.parse("aider", aider) == {
        "openai/gpt-4.1": 75.0, "claude-sonnet-4": 65.4}
    swe = '{"verified": [{"name": "GPT 4.1", "resolved": 0.42}, '\
        '"bad", {"name":"ignored"}, {"name":"GPT 4.1", "score":60}]}'
    assert benchmarks.parse("swebench", swe) == {"GPT 4.1": 60.0}
    boards = '{"leaderboards":{"verified":{"results": [{"model":"claude","score":40}]}}}'
    assert benchmarks.parse("swebench", boards) == {"claude": 40.0}
    simple = '[{"model":"direct","resolved":30}]'
    assert benchmarks.parse("swebench", simple) == {"direct": 30.0}
    terminal = textwrap.dedent("""
        - agent: codex-gpt-5
          score: 0.4
        - model: other-model
          accuracy: 82
        - agent: codex-gpt-5
          accuracy: 30
    """)
    assert benchmarks.parse("terminalbench", terminal) == {
        "codex-gpt-5": 40.0, "other-model": 82.0}
    bfcl = 'Model,Overall,Avg Latency Mean (s),Price\n"GPT, 4",0.7,1.2,3\nother,81,2,4\n'
    assert benchmarks.parse("bfcl", bfcl) == {"GPT, 4": 70.0, "other": 81.0}
    live = "model,average\nGPT-4.1,0.75\nmissing,not-a-number\n"
    assert benchmarks.parse("livebench", live) == {"GPT-4.1": 75.0}
    assert benchmarks.parse_csv("gpt-4.1,0.75\n") == {}
    assert benchmarks.parse_csv("model,metric,score\na,foo,n/a\nb,overall,50\n") == {
        "b": 50.0}
    with pytest.raises(ValueError, match="unknown benchmark"):
        benchmarks.parse("made-up", "{}")


def test_parse_formats_resist_empty_or_invalid_content() -> None:
    assert benchmarks.parse_aider("nothing: true") == {}
    assert benchmarks.parse_aider("- model: only-a-name\n") == {}
    assert benchmarks.parse_swebench("[]") == {}
    assert benchmarks.parse_swebench('{"leaderboards": {"Verified": '
                                     '[{"name":"a","resolved":25}]}}') == {"a": 25.0}
    assert benchmarks.parse_terminalbench("a: 1") == {}
    assert benchmarks.parse_csv("") == {}
    assert benchmarks.parse_csv("model,metric,score\na,overall,\n") == {}


def test_load_missing_corrupt_and_partial_snapshots(tmp_path) -> None:
    assert benchmarks.load(tmp_path / "missing") == {
        "fetched_at": None, "sources": {}, "models": {}}
    target = tmp_path / "benchmarks.json"
    target.write_text("not-json")
    assert benchmarks.load(tmp_path)["models"] == {}
    target.write_text('{"fetched_at":"today"}')
    assert benchmarks.load(tmp_path) == {
        "fetched_at": "today", "sources": {}, "models": {}}


class Client:
    def __init__(self, pages: dict[str, str | Exception]):
        self.pages = pages
        self.closed = False

    def get(self, url: str):
        result = self.pages[url]
        if isinstance(result, Exception):
            raise result
        return result

    def close(self) -> None:
        self.closed = True


class Response:
    def __init__(self, text: str, ok: bool = True):
        self.text = text
        self.ok = ok

    def raise_for_status(self) -> None:
        if not self.ok:
            raise RuntimeError("upstream returned 503")


def test_refresh_preserves_old_component_and_does_not_leave_temp_files(tmp_path) -> None:
    targets = benchmarks.refresh(tmp_path, tmp_path, client=Client({
        "one": Response("- model: gpt\n  pass_rate: 60\n")}),
        sources={"polyglot": {"url": "one", "format": "aider"}})
    assert targets["sources"]["polyglot"] == {"status": "ok", "models": 1}
    targets = benchmarks.refresh(tmp_path, tmp_path, client=Client({
        "fail": RuntimeError("network down")}),
        sources={"polyglot": {"url": "fail", "format": "aider"}})
    assert targets["sources"]["polyglot"]["status"] == "error"
    assert targets["models"]["gpt"]["polyglot"] == 60.0
    assert benchmarks.load(tmp_path) == targets
    assert not list(tmp_path.glob(".benchmarks.*"))
    assert set(targets["models"]["gpt"]) == {"polyglot"}
    assert targets["sources"]["polyglot"]["error"] == "network down"
    assert benchmarks.refresh(tmp_path, tmp_path, client=Client({
        "bad": Response("- model: gpt\n  pass_rate: 99\n", ok=False)}),
        sources={"other": {"url": "bad", "format": "aider"}})["sources"]["other"][
            "status"] == "error"


def test_refresh_component_matching_exact_normalised_and_suffix() -> None:
    data = {"models": {"OpenAI/GPT-4.1": {"aider": 80}, "claude-4-opus": {"swe": 50}}}
    source = {"a": "/not-used"}
    assert benchmarks.components("OpenAI/GPT-4.1", data) == {"aider": 80}
    assert benchmarks.components("gpt-4.1", data) == {"aider": 80}
    assert benchmarks.components("vendor/claude-4-opus", data) == {"swe": 50}
    assert benchmarks.components("4-opus", data) == {"swe": 50}
    assert benchmarks.components("", data) == {}
    assert benchmarks.components("unknown", {"models": None}) == {}
    assert source["a"] == "/not-used"


def test_refresh_models_success_errors_and_correction(tmp_path) -> None:
    assert refresh_scheduler.refresh_models(tmp_path, tmp_path)["status"] == "error"
    (tmp_path / "refresh.py").write_text(
        "import argparse\np=argparse.ArgumentParser();p.add_argument('--config-dir');"
        "p.add_argument('--state-dir');p.parse_args();print('models refreshed')\n")
    result = refresh_scheduler.refresh_models(tmp_path, tmp_path)
    assert result == {"status": "ok", "returncode": 0,
                      "stdout": "models refreshed\n", "stderr": ""}
    (tmp_path / "refresh.py").write_text("raise RuntimeError('bad pipeline')\n")
    fail = refresh_scheduler.refresh_models(tmp_path, tmp_path)
    assert fail["status"] == "error" and fail["returncode"] == 1
    assert "bad pipeline" in fail["stderr"]


class Store:
    def __init__(self, last_success=None):
        self.started = []
        self.finished = []
        self.last_success = last_success

    def start_refresh(self, source: str) -> int:
        self.started.append(source)
        return len(self.started)

    def finish_refresh(self, run: int, status: str, data, error=None) -> None:
        self.finished.append((run, status, data, error))

    def refresh_status(self) -> dict:
        return {"last_success": self.last_success}


def test_refresh_all_pipeline_and_benchmark_exception_is_recorded(tmp_path, monkeypatch) -> None:
    config = tmp_path / "config"
    config.mkdir()
    store = Store()
    monkeypatch.setattr(refresh_scheduler, "refresh_models",
                        lambda c, s: {"status": "ok"})
    assert refresh_scheduler.refresh_all(config, tmp_path, include_models=False)["ok"] is True
    assert store.started == []
    monkeypatch.setattr(refresh_scheduler.benchmarks, "refresh",
                        lambda *a, **k: {"sources": {"x": {"status": "ok"}}, "models": {"a": {}}})
    result = refresh_scheduler.refresh_all(config, tmp_path, store)
    assert result["ok"] is True
    assert store.started == ["models", "benchmarks"]
    assert store.finished[0][1] == "ok" and store.finished[1][1] == "ok"
    monkeypatch.setattr(refresh_scheduler.benchmarks, "refresh",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("exploded")))
    assert refresh_scheduler.refresh_all(config, tmp_path, store)["ok"] is False
    assert store.finished[-1][1:]  # failed benchmark run is durable, not silently lost
    assert store.finished[-1][3] == "exploded"


def test_due_missing_store_errors_naive_and_invalid_timestamps() -> None:
    now = dt.datetime.now(dt.timezone.utc)
    assert refresh_scheduler._due(None, 3600) is True
    assert refresh_scheduler._due(Store(), 3600) is True
    assert refresh_scheduler._due(Store({"finished_at": now.isoformat()}), 3600) is False
    old = (now - dt.timedelta(hours=2)).isoformat()
    assert refresh_scheduler._due(Store({"finished_at": old}), 3600) is True
    assert refresh_scheduler._due(Store({"finished_at": now.replace(tzinfo=None).isoformat()}),
                                 3600) is False
    assert refresh_scheduler._due(Store({"finished_at": "not-a-time"}), 3600) is True
    assert refresh_scheduler._due(Store({"finished_at": None}), 3600) is True


class BrokenStatus(Store):
    def refresh_status(self):
        raise RuntimeError("db unavailable")


def test_due_database_error_is_safe() -> None:
    assert refresh_scheduler._due(BrokenStatus(), 60) is True


def test_scheduler_runs_once_when_due_and_survives_failure(monkeypatch) -> None:
    calls = []

    async def to_thread(func, *args):
        func(*args)

    async def sleep(_seconds):
        return None

    monkeypatch.setattr(refresh_scheduler.asyncio, "to_thread", to_thread)
    monkeypatch.setattr(refresh_scheduler.asyncio, "sleep", sleep)
    monkeypatch.setattr(refresh_scheduler, "_due", lambda *a: True)
    monkeypatch.setattr(refresh_scheduler, "refresh_all", lambda *a: calls.append(a) or {"ok": True})
    source = inspect.getsource(refresh_scheduler.scheduler_loop)
    prefix = source.split("    interval_s = ", 1)[1].replace("while True:", "if True:")
    body = """    return "done"
"""
    ns = {"asyncio": refresh_scheduler.asyncio, "_due": lambda *a: True,
          "refresh_all": lambda *a: calls.append(a)}
    exec("async def once(store, config_dir, state_dir, interval_hours=24, initial_delay=0):\n"
         "    interval_s = " + prefix + body, ns)
    assert asyncio.run(ns["once"](None, ".", ".")) == "done"
    assert len(calls) == 1
    ns["_due"] = lambda *a: (_ for _ in ()).throw(RuntimeError("scheduler failure"))
    assert asyncio.run(ns["once"](None, ".", ".")) == "done"


def test_start_scheduler_toggle_and_invalid_interval(tmp_path, monkeypatch) -> None:
    class App:
        class State:
            wt_store = Store()
            wt_config_dir = tmp_path
            wt_state_dir = tmp_path
        state = State()
    monkeypatch.setenv("TRACKER_REFRESH_ENABLED", "false")
    assert refresh_scheduler.start_scheduler(App()) is None
    monkeypatch.setenv("TRACKER_REFRESH_HOURS", "not-a-number")
    monkeypatch.setenv("TRACKER_REFRESH_ENABLED", "1")
    monkeypatch.setenv("TRACKER_REFRESH_INITIAL_DELAY", "999")
    seen = {}

    def fake_create_task(coro):
        seen["locals"] = dict(coro.cr_frame.f_locals)
        coro.close()
        return object()

    monkeypatch.setattr(asyncio, "create_task", fake_create_task)
    assert refresh_scheduler.start_scheduler(App()) is not None
    assert seen["locals"]["initial_delay"] == 999.0


def test_start_scheduler_parses_custom_interval(monkeypatch, tmp_path) -> None:
    seen = {}

    def fake_create_task(coro):
        seen["locals"] = dict(coro.cr_frame.f_locals)
        coro.close()
        return None

    class App:
        class State:
            wt_store = None
            wt_config_dir = tmp_path
            wt_state_dir = tmp_path
        state = State()

    monkeypatch.setattr(asyncio, "create_task", fake_create_task)
    monkeypatch.setenv("TRACKER_REFRESH_ENABLED", "yes")
    monkeypatch.setenv("TRACKER_REFRESH_HOURS", "0.025")
    monkeypatch.setenv("TRACKER_REFRESH_INITIAL_DELAY", "4")
    assert refresh_scheduler.start_scheduler(App()) is None
    frame = seen["locals"]
    assert frame["interval_hours"] == 0.025
    assert frame["initial_delay"] == 4.0
