"""Provider catalog activation and authentication tests."""
from catalog import ROOT, Catalog


def test_google_provider_uses_gemini_api_key(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    disabled = Catalog.load(ROOT / "catalog.yaml").providers["google"]

    assert disabled.enabled is False
    assert disabled.api_key_env == "GEMINI_API_KEY"
    assert disabled.auth_headers() == {}

    monkeypatch.setenv("GEMINI_API_KEY", "test-google-key")
    enabled = Catalog.load(ROOT / "catalog.yaml").providers["google"]

    assert enabled.enabled is True
    assert enabled.base_url == "https://generativelanguage.googleapis.com/v1beta/openai"
    assert enabled.auth_headers() == {"Authorization": "Bearer test-google-key"}
