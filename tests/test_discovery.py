"""Discovery + fuzzy rank composition tests."""
import json

import httpx

from catalog import Catalog, Model
from discovery import load_discovered, sync_discovered
from refresh import compose
from tests.conftest import build_client, run, write_catalog

DISCOVERY_YAML = """
policy:
  discovery: {{enabled: true, seed_rank: 40, exclude_patterns: [embed]}}
providers:
  fake:
    base_url: {url}
    api_key_env: ""
    discovery: {{default_cost_in: 0, default_cost_out: 0, default_context: 64000}}
presets:
  low: {{max_cost: 0.0, min_context: 0, require_tools: false, weights: {{aa_intelligence: 1}}}}
models:
  - {{id: fake/curated, provider: fake, upstream: curated, cost_in: 0, cost_out: 0, seed_rank: 50, context: 200000, tools: true}}
"""

PLAIN_YAML = """
policy: {}
providers:
  fake: {base_url: "http://127.0.0.1:9/v1", api_key_env: ""}
presets:
  low: {max_cost: 0, min_context: 0, require_tools: false, weights: {aa_intelligence: 1}}
models: []
"""


def _write(tmp_path, text):
    p = tmp_path / "catalog.yaml"
    p.write_text(text)
    (tmp_path / ".env").write_text("")
    return p


def test_sync_discovered_filters_and_defaults(tmp_path, stub):
    stub.httpd.state["models"] = ["curated", "fresh", "text-embedding-3-small"]
    (tmp_path / "state").mkdir()
    catalog = Catalog.load(_write(tmp_path, DISCOVERY_YAML.format(url=stub.base_url)))
    with httpx.Client() as c:
        out = sync_discovered(catalog, tmp_path / "state", c)
    assert set(out["models"]) == {"fake/fresh"}  # curated skipped, embedding excluded
    m = out["models"]["fake/fresh"]
    assert m["seed_rank"] == 40
    assert m["cost_in"] == 0 and m["context"] == 64000
    assert load_discovered(tmp_path / "state")["fake/fresh"].provider == "fake"


def _preload_discovered(tmp_path):
    state = tmp_path / "state"
    state.mkdir(exist_ok=True)
    (state / "discovered.json").write_text(json.dumps({"models": {
        "fake/discovered": {"id": "fake/discovered", "provider": "fake", "upstream": "disc",
                            "cost_in": 0, "cost_out": 0, "seed_rank": 99,
                            "context": 200000, "tools": True}}, "providers": {}}))


def test_gateway_routes_to_discovered(tmp_path, stub, monkeypatch):
    monkeypatch.setenv("FAKE_KEY", "k1")
    monkeypatch.setenv("FAKE2_KEY", "k2")
    write_catalog(tmp_path, stub.base_url)
    _preload_discovered(tmp_path)
    c = build_client(tmp_path)
    try:
        r = run(c.post("/v1/chat/completions",
                       json={"model": "low", "messages": [{"role": "user", "content": "hi"}]}))
        assert r.status_code == 200
        assert r.headers["x-router-model"] == "fake/discovered"
        assert stub.httpd.state["received"][-1]["model"] == "disc"
        status = run(c.get("/v1/status")).json()
        assert status["discovery"]["models"] == 1
    finally:
        run(c.aclose())


def test_blocklist_excludes_discovered(tmp_path, stub, monkeypatch):
    monkeypatch.setenv("FAKE_KEY", "k1")
    monkeypatch.setenv("FAKE2_KEY", "k2")
    write_catalog(tmp_path, stub.base_url)
    cat = tmp_path / "catalog.yaml"
    cat.write_text(cat.read_text() + "\nblocklist: [fake/discovered]\n")
    _preload_discovered(tmp_path)
    c = build_client(tmp_path)
    try:
        r = run(c.post("/v1/chat/completions",
                       json={"model": "low", "messages": [{"role": "user", "content": "hi"}]}))
        assert r.status_code == 200
        assert r.headers["x-router-model"] == "fake2/cheap"
    finally:
        run(c.aclose())


def test_compose_matches_family_exact_and_unknown(tmp_path):
    catalog = Catalog.load(_write(tmp_path, PLAIN_YAML))
    models = {
        "e-infra/kimi": Model({"id": "e-infra/kimi", "provider": "fake", "upstream": "kimi"}),
        "openrouter/moonshotai/kimi-k2": Model({"id": "openrouter/moonshotai/kimi-k2",
                                                "provider": "fake", "upstream": "moonshotai/kimi-k2"}),
        "mystery/foo": Model({"id": "mystery/foo", "provider": "fake", "upstream": "foo-bar-9000"}),
    }
    aa = {"data": [
        {"slug": "moonshotai/kimi-k2", "name": "Kimi K2",
         "artificial_analysis_intelligence_index": 50},
        {"slug": "moonshotai/kimi-k2-thinking", "name": "Kimi K2 Thinking",
         "artificial_analysis_intelligence_index": 80},
        {"slug": "deepseek/deepseek-v4-pro", "name": "DeepSeek V4 Pro",
         "artificial_analysis_intelligence_index": 65},
    ]}
    oc = {"weeklyRetention": [{"model": "kimi-k2", "retentionPercent": 40}],
          "leaderboard": {"pastWeek": [{"model": "kimi-k2", "tokens": 1000},
                                       {"model": "deepseek-v4-pro", "tokens": 500}]}}
    out = compose(catalog, models, aa, oc, None, {})

    e = out["models"]["e-infra/kimi"]
    assert e["match"]["aa"]["how"] == "family"
    assert e["match"]["aa"]["slug"] == "moonshotai/kimi-k2-thinking"
    assert e["aa_intelligence"] == 80

    o = out["models"]["openrouter/moonshotai/kimi-k2"]
    assert o["match"]["aa"]["how"] == "exact"
    assert o["aa_intelligence"] == 50
    assert o["oc_retention"] == 40
    assert o["oc_popularity"] == 100

    m = out["models"]["mystery/foo"]
    assert m["match"]["aa"]["how"] == "none"
    assert m["match"]["oc"]["how"] == "none"
    assert "mystery/foo" in out["unmatched"]

    assert any(r["id"] == "e-infra/kimi" and r["how"] == "family" for r in out["review"])
