from __future__ import annotations

from ops_evidence_synthesis.ai.gemini import GeminiRestProvider


def test_gemini_rest_defaults_to_pro_with_medium_thinking() -> None:
    provider = GeminiRestProvider()

    assert provider.model_name == "gemini-3.1-pro-preview"
    assert provider._generation_config()["thinkingConfig"] == {"thinkingLevel": "medium"}


def test_gemini_rest_does_not_send_thinking_level_to_gemini_2() -> None:
    provider = GeminiRestProvider(model_name="gemini-2.5-flash")

    assert "thinkingConfig" not in provider._generation_config()


def test_gemini_rest_normalizes_explicit_thinking_level() -> None:
    provider = GeminiRestProvider(thinking_level="HIGH")

    assert provider._generation_config()["thinkingConfig"] == {"thinkingLevel": "high"}
