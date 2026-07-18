from __future__ import annotations

from ops_evidence_synthesis.ai import maas
from ops_evidence_synthesis.ai.provider_registry import build_multi_ai_providers
from ops_evidence_synthesis.ai.maas import (
    VertexMistralProvider,
    VertexOpenAICompatProvider,
    VertexOpenModelProvider,
    _extract_chat_completion_text,
)
from ops_evidence_synthesis.ai.vertex import VertexGeminiProvider
from ops_evidence_synthesis.synthesis import multi_ai


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
            "model": "openai/gpt-oss-20b-maas@20260718",
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
    assert response.requested_model_name == "gpt-oss-20b-maas"
    assert response.resolved_model_name == "openai/gpt-oss-20b-maas@20260718"
    assert response.provider_response_model_id == "openai/gpt-oss-20b-maas@20260718"


def test_mistral_raw_predict_url_uses_mistralai_publisher() -> None:
    provider = VertexMistralProvider(
        project_id="ops-evidence-synthesis",
        location="us-central1",
    )

    assert provider._raw_predict_url() == (
        "https://us-central1-aiplatform.googleapis.com/v1/projects/ops-evidence-synthesis/"
        "locations/us-central1/publishers/mistralai/models/mistral-small-2503:rawPredict"
    )


def test_qwen_chat_completions_uses_global_openapi_endpoint() -> None:
    provider = VertexOpenModelProvider(
        provider="qwen-agent-platform",
        model_name="qwen/qwen3-coder-480b-a35b-instruct-maas",
        default_publisher="qwen",
        project_id="ops-evidence-synthesis",
        location="global",
    )

    assert provider._chat_completions_url() == (
        "https://aiplatform.googleapis.com/v1/projects/ops-evidence-synthesis/"
        "locations/global/endpoints/openapi/chat/completions"
    )
    assert provider._request_model_name() == "qwen/qwen3-coder-480b-a35b-instruct-maas"


def test_open_model_request_name_adds_publisher_when_missing() -> None:
    qwen_provider = VertexOpenModelProvider(
        provider="qwen-agent-platform",
        model_name="qwen3-coder-480b-a35b-instruct-maas",
        default_publisher="qwen",
        project_id="ops-evidence-synthesis",
    )
    glm_provider = VertexOpenModelProvider(
        provider="glm-agent-platform",
        model_name="glm-5-maas",
        default_publisher="zai-org",
        project_id="ops-evidence-synthesis",
    )

    assert qwen_provider._request_model_name() == "qwen/qwen3-coder-480b-a35b-instruct-maas"
    assert glm_provider._request_model_name() == "zai-org/glm-5-maas"


def test_llama_open_model_provider_uses_meta_defaults() -> None:
    provider = VertexOpenModelProvider.from_llama_env()

    assert provider.provider == "llama-agent-platform"
    assert provider.default_publisher == "meta"
    assert provider.model_name == "llama-4-maverick-17b-128e-instruct-maas"
    assert provider.location == "us-east5"
    assert provider._request_model_name() == "meta/llama-4-maverick-17b-128e-instruct-maas"


def test_gemma_open_model_provider_uses_google_defaults() -> None:
    provider = VertexOpenModelProvider.from_gemma_env()

    assert provider.provider == "gemma-agent-platform"
    assert provider.default_publisher == "google"
    assert provider.model_name == "gemma-4-26b-a4b-it-maas"
    assert provider.location == "global"
    assert provider._request_model_name() == "google/gemma-4-26b-a4b-it-maas"


def test_real_provider_defaults_lock_model_location_and_chunk_budgets(monkeypatch) -> None:
    for name in (
        "OES_GEMINI_MODEL",
        "OES_VERTEX_LOCATION",
        "OES_GEMINI_MAX_OUTPUT_TOKENS",
        "OES_GPT_OSS_MODEL",
        "OES_GPT_OSS_LOCATION",
        "OES_GPT_OSS_MAX_OUTPUT_TOKENS",
        "OES_MISTRAL_MODEL",
        "OES_MISTRAL_LOCATION",
        "OES_MISTRAL_MAX_OUTPUT_TOKENS",
        "OES_QWEN_MODEL",
        "OES_QWEN_LOCATION",
        "OES_QWEN_MAX_OUTPUT_TOKENS",
        "OES_GEMMA_MODEL",
        "OES_GEMMA_LOCATION",
        "OES_GEMMA_MAX_OUTPUT_TOKENS",
        "OES_MULTI_AI_CHUNK_TARGET_TOKENS",
        "OES_MULTI_AI_CHUNK_TARGET_TOKENS_GEMINI_ENTERPRISE_AGENT_PLATFORM",
        "OES_MULTI_AI_CHUNK_TARGET_TOKENS_OPENAI_GPT_OSS_ON_VERTEX",
        "OES_MULTI_AI_CHUNK_TARGET_TOKENS_MISTRAL_AGENT_PLATFORM",
        "OES_MULTI_AI_CHUNK_TARGET_TOKENS_QWEN_AGENT_PLATFORM",
        "OES_MULTI_AI_CHUNK_TARGET_TOKENS_GEMMA_AGENT_PLATFORM",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("OES_ENABLE_REAL_AI", "1")
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "demo-project")

    gemini = VertexGeminiProvider.from_env()
    gpt_oss = VertexOpenAICompatProvider.from_env()
    mistral = VertexMistralProvider.from_env()
    qwen = VertexOpenModelProvider.from_qwen_env()
    gemma = VertexOpenModelProvider.from_gemma_env()
    providers = build_multi_ai_providers(["gemini", "gpt-oss", "mistral", "qwen", "gemma"], mode="real")

    assert [provider.provider for provider in providers] == [
        "gemini-enterprise-agent-platform",
        "openai-gpt-oss-on-vertex",
        "mistral-agent-platform",
        "qwen-agent-platform",
        "gemma-agent-platform",
    ]
    assert gemini.model_name == "gemini-3.1-pro-preview"
    assert gemini.location == "global"
    assert gemini.max_output_tokens == 8192
    assert gpt_oss.model_name == "gpt-oss-20b-maas"
    assert gpt_oss.location == "us-central1"
    assert gpt_oss.max_output_tokens == 8192
    assert mistral.model_name == "mistral-small-2503"
    assert mistral.location == "us-central1"
    assert mistral.max_output_tokens == 4096
    assert qwen.model_name == "qwen/qwen3-coder-480b-a35b-instruct-maas"
    assert qwen.location == "global"
    assert qwen.max_output_tokens == 8192
    assert gemma.model_name == "gemma-4-26b-a4b-it-maas"
    assert gemma.location == "global"
    assert gemma.max_output_tokens == 8192
    assert multi_ai._chunk_target_tokens("gemini-enterprise-agent-platform") == 80_000
    assert multi_ai._chunk_target_tokens("openai-gpt-oss-on-vertex") == 64_000
    assert multi_ai._chunk_target_tokens("mistral-agent-platform") == 120_000
    assert multi_ai._chunk_target_tokens("qwen-agent-platform") == 80_000
    assert multi_ai._chunk_target_tokens("gemma-agent-platform") == 80_000
    assert multi_ai._evidence_chunk_size("mistral-agent-platform") == 500
    assert multi_ai._evidence_chunk_size("gemma-agent-platform") == 140


def test_grok_open_model_provider_uses_managed_model_name_without_publisher() -> None:
    provider = VertexOpenModelProvider.from_grok_env()

    assert provider.provider == "grok-agent-platform"
    assert provider.default_publisher == "xai"
    assert provider.model_name == "grok-4.20-reasoning"
    assert provider.location == "global"
    assert provider._request_model_name() == "xai/grok-4.20-reasoning"


def test_open_model_retries_empty_content_with_larger_output_budget(monkeypatch) -> None:
    calls: list[int] = []

    def fake_post_json(url: str, body: dict[str, object], *, timeout_seconds: int) -> dict[str, object]:
        del url, timeout_seconds
        calls.append(int(body["max_tokens"]))
        if len(calls) == 1:
            return {"choices": [{"message": {"content": None, "reasoning_content": "thinking"}}], "usage": {}}
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
    provider = VertexOpenModelProvider(
        provider="glm-agent-platform",
        model_name="zai-org/glm-5-maas",
        default_publisher="zai-org",
        project_id="ops-evidence-synthesis",
        max_output_tokens=512,
    )

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

    assert calls == [512, 8192]
    assert '"schema_version":"claim-result/v1"' in response.raw_output


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
