from __future__ import annotations

from ops_evidence_synthesis.ai.vertex import VertexGeminiProvider


def test_vertex_global_endpoint_uses_aiplatform_host() -> None:
    provider = VertexGeminiProvider(project_id="ops-evidence-synthesis", location="global")

    assert provider._generate_content_url() == (
        "https://aiplatform.googleapis.com/v1/projects/ops-evidence-synthesis/"
        "locations/global/publishers/google/models/gemini-3.1-flash-lite:generateContent"
    )


def test_vertex_regional_endpoint_uses_regional_host() -> None:
    provider = VertexGeminiProvider(
        project_id="ops-evidence-synthesis",
        location="asia-northeast1",
        model_name="gemini-2.5-flash-lite",
    )

    assert provider._generate_content_url() == (
        "https://asia-northeast1-aiplatform.googleapis.com/v1/projects/ops-evidence-synthesis/"
        "locations/asia-northeast1/publishers/google/models/gemini-2.5-flash-lite:generateContent"
    )


def test_vertex_extracts_json_text_from_candidate() -> None:
    provider = VertexGeminiProvider(project_id="ops-evidence-synthesis")

    assert provider._extract_text(
        {
            "candidates": [
                {
                    "content": {
                        "parts": [
                            {
                                "text": '```json\n{"schema_version":"claim-result/v1","claims":[]}\n```',
                            }
                        ]
                    }
                }
            ]
        }
    ) == '{"schema_version":"claim-result/v1","claims":[]}'


def test_vertex_gemini_3_uses_medium_thinking_by_default() -> None:
    provider = VertexGeminiProvider(project_id="ops-evidence-synthesis")

    assert provider._generation_config()["thinkingConfig"] == {"thinkingLevel": "medium"}


def test_vertex_gemini_2_does_not_use_thinking_level() -> None:
    provider = VertexGeminiProvider(project_id="ops-evidence-synthesis", model_name="gemini-2.5-flash")

    assert "thinkingConfig" not in provider._generation_config()


def test_vertex_thinking_level_from_env(monkeypatch) -> None:
    monkeypatch.setenv("OES_VERTEX_PROJECT", "ops-evidence-synthesis")
    monkeypatch.setenv("OES_GEMINI_THINKING_LEVEL", "HIGH")

    provider = VertexGeminiProvider.from_env()

    assert provider._generation_config()["thinkingConfig"] == {"thinkingLevel": "high"}
