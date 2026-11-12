import asyncio
import json
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import os  # noqa: E402

os.environ.setdefault("ROUTER_TITLES_CMD", "")  # hermetic: no opencode subprocess in tests
os.environ.setdefault("ROUTER_DATABASE_URL", "")  # opt in explicitly for PostgreSQL tests
os.environ.setdefault("DATABASE_URL", "")  # never connect to the developer's default database

from tests.stub import StubServer  # noqa: E402
import gateway  # noqa: F401,E402  # construct the default app before per-test DSNs

# one event loop for the whole session: the gateway's AsyncClient must stay on
# a single loop, and ASGITransport only works inside a loop.
LOOP = asyncio.new_event_loop()


def run(coro):
    return LOOP.run_until_complete(coro)


CATALOG = """
policy:
  aux_kinds: [title, compaction, generate]
  aux_preset: low
  cooldown_seconds: 5
  cooldown_max_seconds: 5
  balance_cache_seconds: 60
  request_timeout_seconds: 5
  max_failovers: 2
providers:
  fake:
    base_url: PLACEHOLDER
    api_key_env: FAKE_KEY
  fake2:
    base_url: PLACEHOLDER
    api_key_env: FAKE2_KEY
    balance: {type: budget, daily_tokens: 1000}
presets:
  low:
    max_cost: 0.0
    min_context: 0
    require_tools: false
    weights: {aa_intelligence: 1}
  high:
    max_cost: 10.0
    min_context: 0
    require_tools: false
    weights: {aa_intelligence: 1}
models:
  - {id: fake/strong, provider: fake,  upstream: strong, cost_in: 4, cost_out: 4, seed_rank: 90, context: 200000, tools: true}
  - {id: fake/mid,    provider: fake,  upstream: mid,    cost_in: 1, cost_out: 1, seed_rank: 50, context: 200000, tools: true}
  - {id: fake2/cheap, provider: fake2, upstream: cheap,  cost_in: 0, cost_out: 0, seed_rank: 30, context: 200000, tools: true}
"""


@pytest.fixture
def stub():
    s = StubServer()
    s.start()
    yield s
    s.stop()


def write_catalog(tmp_path: pathlib.Path, stub_url: str, extra_state: dict | None = None) -> pathlib.Path:
    (tmp_path / "catalog.yaml").write_text(CATALOG.replace("PLACEHOLDER", stub_url))
    (tmp_path / ".env").write_text("")
    if extra_state:
        (tmp_path / "state").mkdir(exist_ok=True)
        (tmp_path / "state" / "state.json").write_text(json.dumps(extra_state))
    return tmp_path


def build_client(tmp_path):
    import httpx
    from gateway import create_app

    app = create_app(tmp_path, tmp_path / "state")
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://router")


@pytest.fixture
def client(tmp_path, stub, monkeypatch):
    monkeypatch.setenv("FAKE_KEY", "k1")
    monkeypatch.setenv("FAKE2_KEY", "k2")
    write_catalog(tmp_path, stub.base_url)
    c = build_client(tmp_path)
    yield c
    run(c.aclose())
