from __future__ import annotations

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
            supported = "local, gemini, claude, gpt-oss, mistral, qwen, glm, gemma, grok, llama"
            raise ValueError(f"unsupported provider '{name}'. Supported providers: {supported}")
    return providers


def _normalize_provider_names(names: Iterable[str] | None) -> list[str]:
    values: list[str] = []
    for raw in names or []:
        for item in str(raw).split(","):
            name = item.strip().casefold()
            if name:
                values.append(name)
    return values
