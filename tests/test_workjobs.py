"""Unit tests for job-profile validation, merging and import/export."""
from __future__ import annotations

import pytest

from worktracker import jobs

PRESETS = {"small", "medium", "large"}


def test_validate_requires_a_name():
    with pytest.raises(ValueError, match="job name is required"):
        jobs.validate({"name": "  "}, PRESETS)


def test_validate_rejects_unknown_preset():
    with pytest.raises(ValueError, match="not a configured preset"):
        jobs.validate({"name": "x", "base_preset": "huge"}, PRESETS)


def test_validate_rejects_non_mapping_scores_and_weights():
    with pytest.raises(ValueError, match="min_scores must be an object"):
        jobs.validate({"name": "x", "min_scores": [1, 2]}, PRESETS)
    with pytest.raises(ValueError, match="weights must be an object"):
        jobs.validate({"name": "x", "weights": "heavy"}, PRESETS)


def test_validate_rejects_bad_score_and_weight_values():
    with pytest.raises(ValueError, match=r"min_scores\.a must be a number"):
        jobs.validate({"name": "x", "min_scores": {"a": "high"}}, PRESETS)
    with pytest.raises(ValueError, match=r"min_scores\.a must be between 0 and 100"):
        jobs.validate({"name": "x", "min_scores": {"a": 200}}, PRESETS)
    with pytest.raises(ValueError, match=r"weights\.w must be a number"):
        jobs.validate({"name": "x", "weights": {"w": "lots"}}, PRESETS)
    with pytest.raises(ValueError, match=r"weights\.w must be >= 0"):
        jobs.validate({"name": "x", "weights": {"w": -1}}, PRESETS)


def test_validate_rejects_bad_limits_and_policies():
    with pytest.raises(ValueError, match="on_no_match must be"):
        jobs.validate({"name": "x", "on_no_match": "maybe"}, PRESETS)
    with pytest.raises(ValueError, match="max_cost must be >= 0"):
        jobs.validate({"name": "x", "max_cost": -1}, PRESETS)
    with pytest.raises(ValueError, match="max_latency_p95_ms must be > 0"):
        jobs.validate({"name": "x", "max_latency_p95_ms": 0}, PRESETS)
    with pytest.raises(ValueError, match="min_success_rate must be between 0 and 1"):
        jobs.validate({"name": "x", "min_success_rate": 2}, PRESETS)
    with pytest.raises(ValueError, match="min_context must be >= 0"):
        jobs.validate({"name": "x", "min_context": -5}, PRESETS)


def test_validate_returns_a_clean_profile():
    clean = jobs.validate({
        "name": " review ", "base_preset": "large", "description": "d" * 2000,
        "min_scores": {"aa": 42}, "weights": {"aa": 2},
        "min_context": 8192, "max_cost": 1.5, "require_tools": True,
        "max_latency_p95_ms": 900, "min_success_rate": 0.9, "enabled": False,
    }, PRESETS)
    assert clean["name"] == "review"
    assert clean["base_preset"] == "large"
    assert clean["min_scores"] == {"aa": 42.0}
    assert clean["weights"] == {"aa": 2.0}
    assert len(clean["description"]) == 1000
    assert clean["min_context"] == 8192
    assert clean["max_cost"] == 1.5
    assert clean["require_tools"] is True
    assert clean["max_latency_p95_ms"] == 900
    assert clean["min_success_rate"] == 0.9
    assert clean["enabled"] is False


def test_validate_defaults_when_fields_are_absent():
    clean = jobs.validate({"name": "default"}, PRESETS)
    assert clean["base_preset"] == "medium"
    assert clean["on_no_match"] == "error"
    assert clean["enabled"] is True
    assert clean["min_context"] is None and clean["max_cost"] is None
    assert clean["require_tools"] is None


def test_load_defaults_reads_yaml_and_skips_missing(tmp_path):
    assert jobs.load_defaults(tmp_path) == {}
    (tmp_path / "jobs.yaml").write_text("jobs:\n  review:\n    base_preset: large\n")
    assert jobs.load_defaults(tmp_path) == {"review": {"base_preset": "large"}}
    (tmp_path / "jobs.yaml").write_text("jobs: []\n")
    assert jobs.load_defaults(tmp_path) == {}


def test_merge_overlays_db_rows_over_yaml():
    defaults = {"review": {"base_preset": "small", "min_context": 4096}}
    merged = jobs.merge(defaults, [
        {"name": "review", "min_context": 8192},
        {"name": "  "},  # nameless rows are ignored
        {"name": "commit", "base_preset": "medium"},
    ])
    assert merged["review"]["base_preset"] == "small"  # yaml value survives
    assert merged["review"]["min_context"] == 8192
    assert merged["review"]["source"] == "db"
    assert merged["commit"]["source"] == "db"
    assert merged["review"]["name"] == "review"


def test_export_filters_to_known_fields():
    exported = jobs.export({"review": {
        "name": "review", "base_preset": "medium", "source": "db",
        "updated_at": "now", "bogus": 1}})
    assert exported == {"version": 1, "jobs": {"review": {
        "name": "review", "base_preset": "medium"}}}


def test_import_profiles_validates_each_job():
    result = jobs.import_profiles({"jobs": {"review": {"base_preset": "large"}}}, PRESETS)
    assert result[0]["name"] == "review" and result[0]["base_preset"] == "large"
    with pytest.raises(ValueError, match="import payload must contain a jobs object"):
        jobs.import_profiles({"nope": 1}, PRESETS)
    with pytest.raises(ValueError, match="not a configured preset"):
        jobs.import_profiles({"jobs": {"review": {"base_preset": "huge"}}}, PRESETS)
