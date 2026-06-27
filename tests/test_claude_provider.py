from __future__ import annotations

from ops_evidence_synthesis.ai.claude import VertexClaudeProvider


def test_claude_global_endpoint_uses_aiplatform_host() -> None:
    provider = VertexClaudeProvider(project_id="ops-evidence-synthesis", location="global")

    assert provider._raw_predict_url() == (
        "https://aiplatform.googleapis.com/v1/projects/ops-evidence-synthesis/"
        "locations/global/publishers/anthropic/models/claude-haiku-4-5:rawPredict"
    )


def test_claude_regional_endpoint_uses_regional_host() -> None:
    provider = VertexClaudeProvider(
        project_id="ops-evidence-synthesis",
        location="asia-northeast1",
        model_name="claude-3-5-haiku",
    )

    assert provider._raw_predict_url() == (
        "https://asia-northeast1-aiplatform.googleapis.com/v1/projects/ops-evidence-synthesis/"
        "locations/asia-northeast1/publishers/anthropic/models/claude-3-5-haiku:rawPredict"
    )


def test_claude_multi_region_endpoint_uses_rep_host() -> None:
    provider = VertexClaudeProvider(project_id="ops-evidence-synthesis", location="us")

    assert provider._raw_predict_url() == (
        "https://aiplatform.us.rep.googleapis.com/v1/projects/ops-evidence-synthesis/"
        "locations/us/publishers/anthropic/models/claude-haiku-4-5:rawPredict"
    )


def test_claude_extracts_json_text_from_content() -> None:
    provider = VertexClaudeProvider(project_id="ops-evidence-synthesis")

    assert provider._extract_text(
        {
            "content": [
                {
                    "type": "text",
                    "text": '```json\n{"schema_version":"claim-result/v1","claims":[]}\n```',
                }
            ]
        }
    ) == '{"schema_version":"claim-result/v1","claims":[]}'


def test_claude_extracts_embedded_json() -> None:
    provider = VertexClaudeProvider(project_id="ops-evidence-synthesis")

    assert provider._extract_text(
        {
            "content": [
                {
                    "type": "text",
                    "text": 'Here is the JSON:\n{"schema_version":"claim-result/v1","claims":[]}\nDone.',
                }
            ]
        }
    ) == '{"schema_version":"claim-result/v1","claims":[]}'
