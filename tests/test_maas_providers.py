from __future__ import annotations

from ops_evidence_synthesis.ai import maas
from ops_evidence_synthesis.ai.maas import (
    VertexMistralProvider,
    VertexOpenAICompatProvider,
    _extract_chat_completion_text,
)


def test_gpt_oss_chat_completions_url_uses_openapi_endpoint() -> None:
    provider = VertexOpenAICompatProvider(
        project_id="ops-evidence-synthesis",
        location="us-central1",
    )

    assert provider._chat_completions_url() == (
        "https://us-central1-aiplatform.googleapis.com/v1/projects/ops-evidence-synthesis/"
        "locations/us-central1/endpoints/openapi/chat/completions"
    )


def test_gpt_oss_global_endpoint_uses_global_host() -> None:
    provider = VertexOpenAICompatProvider(
        project_id="ops-evidence-synthesis",
        location="global",
        model_name="gpt-oss-120b-maas",
    )

    assert provider._chat_completions_url() == (
        "https://aiplatform.googleapis.com/v1/projects/ops-evidence-synthesis/"
        "locations/global/endpoints/openapi/chat/completions"
    )


def test_gpt_oss_request_model_name_adds_openai_publisher() -> None:
    provider = VertexOpenAICompatProvider(
        project_id="ops-evidence-synthesis",
        model_name="gpt-oss-20b-maas",
    )

    assert provider._request_model_name() == "openai/gpt-oss-20b-maas"


def test_gpt_oss_default_allows_reasoning_and_json_output_tokens() -> None:
    provider = VertexOpenAICompatProvider(project_id="ops-evidence-synthesis")

    assert provider.max_output_tokens == 8192


def test_gpt_oss_retries_empty_content_with_larger_output_budget(monkeypatch) -> None:
    calls: list[int] = []

    def fake_post_json(url: str, body: dict[str, object], *, timeout_seconds: int) -> dict[str, object]:
        del url, timeout_seconds
        calls.append(int(body["max_tokens"]))
        if len(calls) == 1:
            return {"choices": [{"message": {"content": ""}}], "usage": {}}
        return {
            "choices": [
                {
                    "message": {
                        "content": (
                            '{"schema_version":"claim-result/v1","agent_role":"alternative_hypothesis_generator",'
                            '"finding_status":"no_finding","summary":"ok","claims":[],"propositions":[]}'
                        )
                    }
                }
            ],
            "usage": {},
        }

    monkeypatch.setattr(maas, "_post_json", fake_post_json)
    provider = VertexOpenAICompatProvider(project_id="ops-evidence-synthesis", max_output_tokens=4096)

    response = provider.run(
        {
            "schema_version": "ops-evidence-bundle/v1",
            "evidence_sha256": "e" * 64,
            "service": "svc",
            "environment": "prod",
            "profile": {"profile_id": "generic"},
            "metric_windows": [],
            "log_patterns": [],
            "operational_evidence": [],
            "evidence_refs": {},
        }
    )

    assert calls == [4096, 8192]
    assert '"schema_version":"claim-result/v1"' in response.raw_output


def test_mistral_raw_predict_url_uses_mistralai_publisher() -> None:
    provider = VertexMistralProvider(
        project_id="ops-evidence-synthesis",
        location="us-central1",
    )

    assert provider._raw_predict_url() == (
        "https://us-central1-aiplatform.googleapis.com/v1/projects/ops-evidence-synthesis/"
        "locations/us-central1/publishers/mistralai/models/mistral-small-2503:rawPredict"
    )


def test_extract_chat_completion_text_from_message() -> None:
    payload = {
        "choices": [
            {
                "message": {
                    "content": '```json\n{"schema_version":"claim-result/v1","claims":[]}\n```',
                }
            }
        ]
    }

    assert _extract_chat_completion_text(payload, model_label="test") == (
        '{"schema_version":"claim-result/v1","claims":[]}'
    )


def test_extract_chat_completion_text_from_embedded_json() -> None:
    payload = {
        "choices": [
            {
                "message": {
                    "content": 'Here is the JSON:\n{"schema_version":"claim-result/v1","claims":[]}\nDone.',
                }
            }
        ]
    }

    assert _extract_chat_completion_text(payload, model_label="test") == (
        '{"schema_version":"claim-result/v1","claims":[]}'
    )
