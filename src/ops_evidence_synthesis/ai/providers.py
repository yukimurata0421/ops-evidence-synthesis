from __future__ import annotations

import os
from collections.abc import Iterable

from ops_evidence_synthesis.ai.base import ModelProvider
from ops_evidence_synthesis.ai.claude import VertexClaudeProvider
from ops_evidence_synthesis.ai.heuristic import default_local_providers
from ops_evidence_synthesis.ai.maas import VertexMistralProvider, VertexOpenAICompatProvider, VertexOpenModelProvider
from ops_evidence_synthesis.ai.vertex import VertexGeminiProvider


def build_provider_list(names: Iterable[str] | None) -> list[ModelProvider]:
    normalized = _normalize_provider_names(names)
    if not normalized:
        return list(default_local_providers())
    providers: list[ModelProvider] = []
    for name in normalized:
        if name == "local":
            providers.extend(default_local_providers())
        elif name in {"gemini", "vertex-gemini", "gemini-enterprise-agent-platform"}:
            providers.append(VertexGeminiProvider.from_env())
        elif name in {"gemini-fast-lite", "vertex-gemini-fast-lite", "fast-gemini", "fast-gcp-gemini"}:
            providers.append(_fast_gemini_provider())
        elif name in {"claude", "vertex-claude", "claude-agent-platform"}:
            providers.append(VertexClaudeProvider.from_env())
        elif name in {"gpt-oss", "vertex-gpt-oss", "openai-gpt-oss-on-vertex"}:
            providers.append(VertexOpenAICompatProvider.from_env())
        elif name in {"mistral", "vertex-mistral", "mistral-agent-platform"}:
            providers.append(VertexMistralProvider.from_env())
        elif name in {"qwen", "qwen3-coder", "vertex-qwen", "qwen-agent-platform"}:
            providers.append(VertexOpenModelProvider.from_qwen_env())
        elif name in {"glm", "glm-5", "vertex-glm", "glm-agent-platform"}:
            providers.append(VertexOpenModelProvider.from_glm_env())
        elif name in {"gemma", "gemma4", "gemma-4", "vertex-gemma", "gemma-agent-platform"}:
            providers.append(VertexOpenModelProvider.from_gemma_env())
        elif name in {"grok", "vertex-grok", "xai-grok", "grok-agent-platform"}:
            providers.append(VertexOpenModelProvider.from_grok_env())
        elif name in {"llama", "meta-llama", "vertex-llama", "llama-agent-platform"}:
            providers.append(VertexOpenModelProvider.from_llama_env())
        else:
            supported = "local, gemini, gemini-fast-lite, claude, gpt-oss, mistral, qwen, glm, gemma, grok, llama"
            raise ValueError(f"unsupported provider '{name}'. Supported providers: {supported}")
    return providers


def _fast_gemini_provider() -> VertexGeminiProvider:
    return VertexGeminiProvider(
        provider="gemini-fast-lite-agent-platform",
        model_name=os.environ.get("OES_FAST_GCP_GEMINI_MODEL", "gemini-3.1-flash-lite"),
        prompt_name="root-cause",
        project_id=(
            os.environ.get("OES_VERTEX_PROJECT")
            or os.environ.get("GOOGLE_CLOUD_PROJECT")
            or os.environ.get("GCP_PROJECT")
            or ""
        ),
        location=os.environ.get("OES_FAST_GCP_VERTEX_LOCATION") or os.environ.get("OES_VERTEX_LOCATION", "global"),
        temperature=float(os.environ.get("OES_FAST_GCP_GEMINI_TEMPERATURE", "0")),
        max_output_tokens=int(os.environ.get("OES_FAST_GCP_GEMINI_MAX_OUTPUT_TOKENS", "4096")),
        timeout_seconds=int(os.environ.get("OES_FAST_GCP_GEMINI_TIMEOUT_SECONDS", "45")),
        api_version=os.environ.get("OES_FAST_GCP_VERTEX_API_VERSION", "v1"),
        thinking_level=os.environ.get("OES_FAST_GCP_GEMINI_THINKING_LEVEL", "minimal"),
    )


def _normalize_provider_names(names: Iterable[str] | None) -> list[str]:
    values: list[str] = []
    for raw in names or []:
        for item in str(raw).split(","):
            name = item.strip().casefold()
            if name:
                values.append(name)
    return values
