import json

import pytest

from tests.conftest import build_client, run, write_catalog  # noqa: F401
from titles import TitleCache, parse_sessions, title_cmd


def test_parse_sessions_wrapped():
    payload = json.dumps({"data": [
        {"id": "ses_1", "title": "Fix the router"},
        {"id": "ses_2", "title": "Chat fun"},
        {"id": "ses_3"},                  # no title yet -> skipped
        {"title": "orphan"},              # no id -> skipped
    ]})
    assert parse_sessions(payload) == {"ses_1": "Fix the router", "ses_2": "Chat fun"}


def test_parse_sessions_bare_list_and_garbage():
    assert parse_sessions('[{"id":"ses_a","title":"T"}]') == {"ses_a": "T"}
    assert parse_sessions("not json") == {}
    assert parse_sessions('{"data": null}') == {}
    assert parse_sessions('42') == {}


def test_title_cmd_disable_and_default(monkeypatch):
    monkeypatch.setenv("ROUTER_TITLES_CMD", "")
    assert title_cmd() is None
    monkeypatch.setenv("ROUTER_TITLES_CMD", "definitely-not-a-binary x")
    assert title_cmd() is None
    monkeypatch.setenv("ROUTER_TITLES_CMD", "python3 -V")
    assert title_cmd() == ["python3", "-V"]


def test_title_cache_lookup_and_roundtrip(tmp_path):
    p = tmp_path / "titles.json"
    p.write_text(json.dumps({"titles": {"ses_1": "Hello"}}))
    cache = TitleCache(p)
    assert cache.lookup("ses_1") == "Hello"
    assert cache.lookup("ses_missing") is None
    assert cache.lookup(None) is None
    assert cache.lookup("(unknown)") is None


def test_title_cache_refresh_subprocess(tmp_path, monkeypatch):
    payload = '{"data":[{"id":"ses_new","title":"Fresh title"}]}'
    (tmp_path / "payload.json").write_text(payload)
    monkeypatch.setenv("ROUTER_TITLES_CMD", f"cat {tmp_path / 'payload.json'}")
    cache = TitleCache(tmp_path / "titles.json")
    run(cache._refresh())
    assert cache.titles == {"ses_new": "Fresh title"}
    on_disk = json.loads((tmp_path / "titles.json").read_text())
    assert on_disk["titles"]["ses_new"] == "Fresh title"


def test_refresh_failure_keeps_cache(tmp_path, monkeypatch):
    p = tmp_path / "titles.json"
    p.write_text(json.dumps({"titles": {"ses_old": "Old"}}))
    monkeypatch.setenv("ROUTER_TITLES_CMD", "python3 -c 'import sys; sys.exit(1)'")
    cache = TitleCache(p)
    run(cache._refresh())
    assert cache.titles == {"ses_old": "Old"}


@pytest.fixture
def titled_client(tmp_path, stub, monkeypatch):
    monkeypatch.setenv("FAKE_KEY", "k1")
    monkeypatch.setenv("FAKE2_KEY", "k2")
    write_catalog(tmp_path, stub.base_url)
    (tmp_path / "state").mkdir(exist_ok=True)
    (tmp_path / "state" / "titles.json").write_text(
        json.dumps({"titles": {"ses_x": "My router session"}}))
    c = build_client(tmp_path)
    yield c
    run(c.aclose())


def test_spend_sessions_carry_titles(titled_client):
    r = run(titled_client.post("/v1/chat/completions",
                               headers={"x-oc-session": "ses_x"},
                               json={"model": "high", "messages": [{"role": "user", "content": "hi"}]}))
    assert r.status_code == 200
    run(titled_client.post("/v1/chat/completions",
                           headers={"x-oc-session": "ses_untitled"},
                           json={"model": "high", "messages": [{"role": "user", "content": "hi"}]}))
    d = run(titled_client.get("/v1/spend")).json()
    by = {s["session"]: s for s in d["sessions"]}
    assert by["ses_x"]["title"] == "My router session"
    assert by["ses_untitled"]["title"] is None
    assert all("title" in s for s in d["sessions"])
