"""Fuzzy matching unit tests."""
from matching import base_name, best_match, normalize


def test_normalize_variants():
    assert normalize("moonshotai/Kimi-K2.5:free") == "moonshotai/kimi-k2-5"
    assert base_name("deepseek/deepseek-v4.1-flash") == "deepseek-v4-1-flash"
    assert normalize("GLM_5.3 Flash") == "glm-5-3-flash"
    assert normalize("model-preview") == "model"


def test_exact_alias_and_display_name():
    idx = {"moonshotai/kimi-k2": ["moonshotai/kimi-k2", "Kimi K2"]}
    s, conf, how, alts = best_match("kimi-k2", idx)
    assert (s, how, conf) == ("moonshotai/kimi-k2", "exact", 1.0)


def test_family_picks_highest_quality():
    idx = {"kimi-k2": ["kimi-k2"], "kimi-k2-thinking": ["kimi-k2-thinking"]}
    s, conf, how, alts = best_match("kimi", idx, {"kimi-k2": 50, "kimi-k2-thinking": 80})
    assert s == "kimi-k2-thinking"
    assert how == "family"
    assert conf == 0.6


def test_near_miss_rejected():
    idx = {"llama-3-8b": ["llama-3-8b"], "mistral-7b": ["mistral-7b"]}
    s, conf, how, alts = best_match("foo-bar-9000", idx)
    assert s is None and how == "none"


def test_ambiguous_base_picks_quality():
    idx = {"meta/llama-3": ["meta/llama-3"], "x/llama-3": ["x/llama-3"]}
    s, conf, how, alts = best_match("llama-3", idx, {"meta/llama-3": 40, "x/llama-3": 60})
    assert s == "x/llama-3"
    assert how == "base-ambiguous"


def test_free_suffix_and_similar():
    idx = {"deepseek/deepseek-v4-pro": ["deepseek/deepseek-v4-pro", "DeepSeek V4 Pro"]}
    s, conf, how, alts = best_match("deepseek/deepseek-v4-pro:free", idx)
    assert s == "deepseek/deepseek-v4-pro"
    assert how == "exact"
