from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from typing import Any, Callable, Iterable

from ops_evidence_synthesis.ai.base import ModelProvider, ModelResponse
from ops_evidence_synthesis.ai.claude import VertexClaudeProvider
from ops_evidence_synthesis.ai.heuristic import HeuristicProvider
from ops_evidence_synthesis.ai.maas import VertexMistralProvider, VertexOpenAICompatProvider
from ops_evidence_synthesis.ai.vertex import VertexGeminiProvider


@dataclass(frozen=True, slots=True)
class ProviderInfo:
    provider_id: str
    display_name: str
    enabled: bool
    status: str
    requires_network: bool
    requires_api_key: bool
    model_name: str
    supports_json_schema: bool
    default_timeout_seconds: int


@dataclass(frozen=True, slots=True)
class ProviderSpec:
    provider_id: str
    display_name: str
    aliases: tuple[str, ...]
    model_name: str
    requires_network: bool
    requires_api_key: bool
    supports_json_schema: bool
    default_timeout_seconds: int
    factory: Callable[[], ModelProvider]
    configured: Callable[[], bool]

    def info(self) -> ProviderInfo:
        configured = self.configured()
        disabled = _provider_disabled(self)
        return ProviderInfo(
            provider_id=self.provider_id,
            display_name=self.display_name,
            enabled=configured and not disabled,
            status="disabled_by_policy" if disabled else ("configured" if configured else "skipped_not_configured"),
            requires_network=self.requires_network,
            requires_api_key=self.requires_api_key,
            model_name=self.model_name,
            supports_json_schema=self.supports_json_schema,
            default_timeout_seconds=self.default_timeout_seconds,
        )


@dataclass(frozen=True, slots=True)
class SkippedProvider:
    provider: str
    model_name: str
    prompt_name: str = "multi-ai-skip"
    temperature: float = 0.0
    reason: str = "skipped_not_configured"

    def run(self, bundle: dict[str, Any]) -> ModelResponse:
        raw_output = json.dumps(
            {
                "schema_version": "provider-skip/v1",
                "status": "skipped_not_configured",
                "reason": self.reason,
                "message": "Provider was not configured; no network request was made.",
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        return ModelResponse(
            provider=self.provider,
            model_name=self.model_name,
            prompt_name=self.prompt_name,
            temperature=self.temperature,
            raw_output=raw_output,
            latency_ms=0,
            input_tokens=0,
            output_tokens=0,
            status="skipped_not_configured",
        )


@dataclass(frozen=True, slots=True)
class FailingLocalProvider:
    provider: str = "local-fail"
    model_name: str = "local-fail-simulated"
    prompt_name: str = "simulated-provider-failure"
    temperature: float = 0.0

    def run(self, bundle: dict[str, Any]) -> ModelResponse:
        raise RuntimeError("simulated local provider failure")


def provider_registry() -> list[ProviderSpec]:
    return [
        ProviderSpec(
            provider_id="local-gemini",
            display_name="Local Gemini deterministic",
            aliases=("local-gemini", "fake-gemini"),
            model_name="gemini-simulated-root",
            requires_network=False,
            requires_api_key=False,
            supports_json_schema=True,
            default_timeout_seconds=5,
            factory=lambda: HeuristicProvider("local-gemini", "gemini-simulated-root", "root-cause"),
            configured=lambda: True,
        ),
        ProviderSpec(
            provider_id="local-gpt-oss",
            display_name="Local gpt-oss deterministic",
            aliases=("local-gpt-oss", "fake-gpt-oss"),
            model_name="gpt-oss-simulated-root",
            requires_network=False,
            requires_api_key=False,
            supports_json_schema=True,
            default_timeout_seconds=5,
            factory=lambda: HeuristicProvider("local-gpt-oss", "gpt-oss-simulated-root", "root-cause"),
            configured=lambda: True,
        ),
        ProviderSpec(
            provider_id="local-mistral",
            display_name="Local Mistral deterministic",
            aliases=("local-mistral", "fake-mistral"),
            model_name="mistral-simulated-contrast",
            requires_network=False,
            requires_api_key=False,
            supports_json_schema=True,
            default_timeout_seconds=5,
            factory=lambda: HeuristicProvider("local-mistral", "mistral-simulated-contrast", "contrast"),
            configured=lambda: True,
        ),
        ProviderSpec(
            provider_id="local-claude",
            display_name="Local Claude deterministic",
            aliases=("local-claude", "fake-claude"),
            model_name="claude-simulated-verifier",
            requires_network=False,
            requires_api_key=False,
            supports_json_schema=True,
            default_timeout_seconds=5,
            factory=lambda: HeuristicProvider("local-claude", "claude-simulated-verifier", "verifier"),
            configured=lambda: True,
        ),
        ProviderSpec(
            provider_id="local-fail",
            display_name="Local simulated failure",
            aliases=("local-fail", "fake-fail"),
            model_name="local-fail-simulated",
            requires_network=False,
            requires_api_key=False,
            supports_json_schema=True,
            default_timeout_seconds=5,
            factory=lambda: FailingLocalProvider(),
            configured=lambda: True,
        ),
        ProviderSpec(
            provider_id="gemini-enterprise-agent-platform",
            display_name="Vertex Gemini",
            aliases=("gemini", "vertex-gemini", "gemini-enterprise-agent-platform"),
            model_name=os.environ.get("OES_GEMINI_MODEL", "gemini-3.1-flash-lite"),
            requires_network=True,
            requires_api_key=False,
            supports_json_schema=True,
            default_timeout_seconds=int(os.environ.get("OES_GEMINI_TIMEOUT_SECONDS", "60")),
            factory=VertexGeminiProvider.from_env,
            configured=_vertex_ai_configured,
        ),
        ProviderSpec(
            provider_id="openai-gpt-oss-on-vertex",
            display_name="gpt-oss on Vertex MaaS",
            aliases=("gpt-oss", "vertex-gpt-oss", "gpt-oss-on-vertex", "openai-gpt-oss-on-vertex"),
            model_name=os.environ.get("OES_GPT_OSS_MODEL", "gpt-oss-20b-maas"),
            requires_network=True,
            requires_api_key=False,
            supports_json_schema=True,
            default_timeout_seconds=int(os.environ.get("OES_GPT_OSS_TIMEOUT_SECONDS", "240")),
            factory=VertexOpenAICompatProvider.from_env,
            configured=_gpt_oss_configured,
        ),
        ProviderSpec(
            provider_id="mistral-agent-platform",
            display_name="Mistral on Vertex MaaS",
            aliases=("mistral", "vertex-mistral", "mistral-agent-platform"),
            model_name=os.environ.get("OES_MISTRAL_MODEL", "mistral-small-2503"),
            requires_network=True,
            requires_api_key=False,
            supports_json_schema=True,
            default_timeout_seconds=int(os.environ.get("OES_MISTRAL_TIMEOUT_SECONDS", "90")),
            factory=VertexMistralProvider.from_env,
            configured=_mistral_configured,
        ),
        ProviderSpec(
            provider_id="claude-agent-platform",
            display_name="Claude on Vertex",
            aliases=("claude", "vertex-claude", "claude-agent-platform"),
            model_name=os.environ.get("OES_CLAUDE_MODEL", "claude-haiku-4-5"),
            requires_network=True,
            requires_api_key=False,
            supports_json_schema=True,
            default_timeout_seconds=int(os.environ.get("OES_CLAUDE_TIMEOUT_SECONDS", "90")),
            factory=VertexClaudeProvider.from_env,
            configured=_claude_configured,
        ),
    ]


def provider_infos() -> list[dict[str, Any]]:
    return [asdict(spec.info()) for spec in provider_registry()]


def build_multi_ai_providers(
    names: Iterable[str] | None,
    *,
    mode: str = "real_or_skip",
) -> list[ModelProvider]:
    normalized = normalize_provider_names(names)
    if not normalized:
        normalized = ["local-gemini", "local-gpt-oss", "local-mistral"]
    specs = {alias: spec for spec in provider_registry() for alias in (spec.provider_id, *spec.aliases)}
    providers: list[ModelProvider] = []
    for name in normalized:
        spec = specs.get(name)
        if spec is None:
            supported = ", ".join(sorted(specs))
            raise ValueError(f"unsupported provider '{name}'. Supported providers: {supported}")
        if _provider_disabled(spec):
            providers.append(SkippedProvider(spec.provider_id, spec.model_name, reason="disabled_by_policy"))
        elif spec.requires_network and not _real_ai_enabled(mode):
            providers.append(SkippedProvider(spec.provider_id, spec.model_name))
        elif spec.requires_network and not spec.configured():
            providers.append(SkippedProvider(spec.provider_id, spec.model_name))
        else:
            providers.append(spec.factory())
    return providers


def normalize_provider_names(names: Iterable[str] | None) -> list[str]:
    values: list[str] = []
    for raw in names or []:
        for item in str(raw).split(","):
            name = item.strip().casefold().replace("_", "-")
            if name:
                values.append(name)
    return values


def _real_ai_enabled(mode: str) -> bool:
    if mode in {"local", "deterministic", "fake"}:
        return False
    return os.environ.get("OES_ENABLE_REAL_AI", "").strip() == "1"


def _provider_disabled(spec: ProviderSpec) -> bool:
    disabled_names = {
        item.strip().casefold().replace("_", "-")
        for item in os.environ.get("OES_DISABLED_PROVIDERS", "").split(",")
        if item.strip()
    }
    names = {spec.provider_id, *spec.aliases}
    if disabled_names.intersection(names):
        return True
    env_key = "OES_" + spec.provider_id.replace("-", "_").upper() + "_DISABLED"
    return os.environ.get(env_key, "").strip().casefold() in {"1", "true", "yes", "on"}


def _vertex_project_present() -> bool:
    return bool(
        os.environ.get("OES_VERTEX_PROJECT")
        or os.environ.get("OES_GCP_PROJECT")
        or os.environ.get("GOOGLE_CLOUD_PROJECT")
    )


def _vertex_ai_configured() -> bool:
    return _vertex_project_present()


def _gpt_oss_configured() -> bool:
    return bool(os.environ.get("OES_GPT_OSS_PROJECT") or _vertex_project_present())


def _mistral_configured() -> bool:
    return bool(os.environ.get("OES_MISTRAL_PROJECT") or _vertex_project_present())


def _claude_configured() -> bool:
    return bool(os.environ.get("OES_CLAUDE_PROJECT") or _vertex_project_present())
