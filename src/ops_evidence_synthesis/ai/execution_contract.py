from __future__ import annotations

import inspect
import re
from functools import lru_cache
from types import ModuleType
from typing import Any

from ops_evidence_synthesis.ai.base import ModelProvider
from ops_evidence_synthesis.ai.prompts import (
    alternative_hypothesis_prompt,
    compact_bundle_for_model,
    root_cause_prompt,
)
from ops_evidence_synthesis.ai.runtime import safety_preflight_for_model_input
from ops_evidence_synthesis.canonical import pretty_json, sha256_json, sha256_text
from ops_evidence_synthesis.local_first import scan_sanitized_text


EXECUTION_CONTRACT_SCHEMA_VERSION = "provider_execution_contract.v2"
RESPONSE_SCHEMA_VERSION = "claim-result/v1"
PROMPT_RENDERER_VERSION = "multi_ai_claim_prompt.v1"
GENERATION_POLICY_VERSION = "provider_generation_policy.v1"
SAFETY_POLICY_VERSION = "multi_ai_safety_preflight.v1"
NO_TOOL_CONTRACT = "none"

_MUTABLE_MODEL_ALIAS_RE = re.compile(r"(?:^|[-_/])(latest|default)$", re.IGNORECASE)
_PROMPT_PROJECTION_FIELDS = (
    "max_evidence_items",
    "max_logs",
    "max_normalized_events",
    "max_text_chars",
)
_GENERATION_FIELDS = (
    "temperature",
    "top_p",
    "max_output_tokens",
    "max_tokens",
    "seed",
    "stop_sequences",
    "thinking_level",
    "response_mime_type",
    "response_schema_version",
    "anthropic_version",
)


def build_provider_execution_contract(
    provider: ModelProvider,
    bundle: dict[str, Any],
) -> dict[str, Any]:
    """Build the complete request-side identity used for provider result reuse."""
    model_input, rendered_prompt, projection = _rendered_provider_input(provider, bundle)
    model = _model_contract(provider)
    prompt_contract = _prompt_contract(
        provider,
        model_input=model_input,
        rendered_prompt=rendered_prompt,
        projection=projection,
    )
    adapter_source_sha256 = _provider_adapter_source_sha256(provider)
    safety_policy_sha256 = sha256_json(
        {
            "preflight_source_sha256": _source_sha256(
                inspect.getmodule(safety_preflight_for_model_input)
                or safety_preflight_for_model_input
            ),
            "scanner_source_sha256": _source_sha256(
                inspect.getmodule(scan_sanitized_text) or scan_sanitized_text
            ),
        }
    )
    reuse_policy = _reuse_policy(provider, model=model, adapter_source_sha256=adapter_source_sha256)
    return {
        "schema_version": EXECUTION_CONTRACT_SCHEMA_VERSION,
        "provider": {
            "provider_id": str(getattr(provider, "provider", "") or ""),
            "adapter_id": f"{provider.__class__.__module__}.{provider.__class__.__qualname__}",
            "adapter_version": str(
                getattr(provider, "provider_adapter_version", "")
                or getattr(provider, "adapter_version", "")
                or "source-sha256"
            ),
            "adapter_source_sha256": adapter_source_sha256,
        },
        "model": model,
        "input": {
            "model_input_sha256": sha256_json(model_input),
        },
        "prompt_contract": prompt_contract,
        "generation_config": _generation_config(provider),
        "request_protocol": _request_protocol(provider),
        "safety_policy": {
            "version": str(
                getattr(provider, "safety_policy_version", "") or SAFETY_POLICY_VERSION
            ),
            "policy_sha256": safety_policy_sha256,
        },
        "tool_contract": {
            "version": str(getattr(provider, "tool_contract_version", "") or NO_TOOL_CONTRACT),
            "contract_sha256": str(getattr(provider, "tool_contract_sha256", "") or ""),
        },
        "generation_policy": {
            "version": str(
                getattr(provider, "generation_policy_version", "") or GENERATION_POLICY_VERSION
            ),
        },
        "reuse_policy": reuse_policy,
    }


def provider_execution_contract_sha256(contract: dict[str, Any]) -> str:
    if str(contract.get("schema_version") or "") != EXECUTION_CONTRACT_SCHEMA_VERSION:
        raise ValueError(f"unsupported provider execution contract: {contract.get('schema_version')!r}")
    return sha256_json(contract)


def execution_contract_allows_cross_run_reuse(contract: dict[str, Any]) -> bool:
    policy = contract.get("reuse_policy") if isinstance(contract.get("reuse_policy"), dict) else {}
    return str(policy.get("cross_run") or "").casefold() == "allowed"


def is_mutable_model_alias(model_name: str) -> bool:
    normalized = str(model_name or "").strip()
    if not normalized:
        return True
    if normalized.casefold() == "flash":
        return True
    return _MUTABLE_MODEL_ALIAS_RE.search(normalized) is not None


def response_model_observation(response: Any) -> dict[str, str]:
    return {
        "requested_model_name": str(getattr(response, "requested_model_name", "") or ""),
        "resolved_model_name": str(getattr(response, "resolved_model_name", "") or ""),
        "resolved_model_revision": str(getattr(response, "resolved_model_revision", "") or ""),
        "provider_response_model_id": str(getattr(response, "provider_response_model_id", "") or ""),
    }


def _rendered_provider_input(
    provider: ModelProvider,
    bundle: dict[str, Any],
) -> tuple[dict[str, Any], str, dict[str, int]]:
    projection = _prompt_projection(provider)
    model_input = compact_bundle_for_model(bundle, **projection)
    prompt_name = str(getattr(provider, "prompt_name", "") or "")
    if prompt_name == "alternative-hypothesis":
        rendered_prompt = alternative_hypothesis_prompt(bundle, **projection)
    else:
        rendered_prompt = root_cause_prompt(bundle)
    return model_input, rendered_prompt, projection


def _prompt_projection(provider: ModelProvider) -> dict[str, int]:
    defaults = {
        "max_evidence_items": 140,
        "max_logs": 0,
        "max_normalized_events": 0,
        "max_text_chars": 480,
    }
    for field_name in _PROMPT_PROJECTION_FIELDS:
        value = getattr(provider, field_name, None)
        if value is not None:
            defaults[field_name] = int(value)
    return defaults


def _prompt_contract(
    provider: ModelProvider,
    *,
    model_input: dict[str, Any],
    rendered_prompt: str,
    projection: dict[str, int],
) -> dict[str, Any]:
    serialized_input = pretty_json(model_input)
    suffix = f"Evidence bundle:\n{serialized_input}"
    if rendered_prompt.endswith(suffix):
        prompt_template = f"{rendered_prompt[:-len(suffix)]}Evidence bundle:\n<MODEL_INPUT>"
    else:
        prompt_template = rendered_prompt
    descriptor = {
        "contract_version": str(
            getattr(provider, "prompt_contract_version", "") or PROMPT_RENDERER_VERSION
        ),
        "renderer_version": str(
            getattr(provider, "prompt_renderer_version", "") or PROMPT_RENDERER_VERSION
        ),
        "prompt_name": str(getattr(provider, "prompt_name", "") or ""),
        "prompt_template_sha256": sha256_text(prompt_template),
        "input_projection": projection,
        "response_schema_version": str(
            getattr(provider, "response_schema_version", "") or RESPONSE_SCHEMA_VERSION
        ),
    }
    return {
        **descriptor,
        "prompt_contract_sha256": sha256_json(descriptor),
        "rendered_prompt_sha256": sha256_text(rendered_prompt),
    }


def _model_contract(provider: ModelProvider) -> dict[str, Any]:
    requested_model_name = str(getattr(provider, "model_name", "") or "")
    request_model_id = requested_model_name
    request_model_name = getattr(provider, "_request_model_name", None)
    if callable(request_model_name):
        try:
            request_model_id = str(request_model_name() or requested_model_name)
        except (TypeError, ValueError):
            request_model_id = requested_model_name
    resolved_model_name = str(getattr(provider, "resolved_model_name", "") or "")
    resolved_model_revision = str(
        getattr(provider, "resolved_model_revision", "")
        or getattr(provider, "model_revision", "")
        or ""
    )
    explicit_mutable = getattr(provider, "mutable_model_alias", None)
    mutable_alias = (
        bool(explicit_mutable)
        if explicit_mutable is not None
        else is_mutable_model_alias(requested_model_name)
    )
    return {
        "requested_model_name": requested_model_name,
        "request_model_id": request_model_id,
        "resolved_model_name": resolved_model_name,
        "resolved_model_revision": resolved_model_revision,
        "mutable_alias": mutable_alias,
    }


def _generation_config(provider: ModelProvider) -> dict[str, Any]:
    generation_config = getattr(provider, "_generation_config", None)
    if callable(generation_config):
        try:
            value = generation_config()
        except (RuntimeError, TypeError, ValueError):
            value = None
        if isinstance(value, dict):
            return value
    output: dict[str, Any] = {}
    for field_name in _GENERATION_FIELDS:
        value = getattr(provider, field_name, None)
        if value is not None:
            output[field_name] = value
    return output


def _request_protocol(provider: ModelProvider) -> dict[str, str]:
    protocol = {
        key: str(getattr(provider, key, "") or "")
        for key in ("api_version", "location")
        if getattr(provider, key, None) is not None
    }
    endpoint_template = str(getattr(provider, "endpoint_template", "") or "")
    if endpoint_template:
        protocol["endpoint_template_sha256"] = sha256_text(endpoint_template)
    project_id = str(getattr(provider, "project_id", "") or "")
    if project_id:
        protocol["project_scope_sha256"] = sha256_text(project_id)
    return protocol


def _reuse_policy(
    provider: ModelProvider,
    *,
    model: dict[str, Any],
    adapter_source_sha256: str,
) -> dict[str, str]:
    explicit = str(getattr(provider, "cache_reuse_policy", "") or "").strip().casefold()
    if explicit in {"allowed", "cross_run", "cross-run"}:
        cross_run = "allowed"
        reason = "explicit_provider_policy"
    elif explicit in {"disabled", "within_run", "within-run", "none"}:
        cross_run = "disabled"
        reason = "explicit_provider_policy"
    elif bool(model.get("mutable_alias")) and not str(model.get("resolved_model_revision") or ""):
        cross_run = "disabled"
        reason = "mutable_model_alias_without_resolved_revision"
    elif not adapter_source_sha256:
        cross_run = "disabled"
        reason = "provider_adapter_version_unavailable"
    else:
        cross_run = "allowed"
        reason = "versioned_request_contract"
    return {
        "within_run": "allowed",
        "cross_run": cross_run,
        "reason": reason,
    }


def _provider_adapter_source_sha256(provider: ModelProvider) -> str:
    module = inspect.getmodule(provider.__class__)
    source_hash = _source_sha256(module) if isinstance(module, ModuleType) else ""
    return source_hash or _source_sha256(provider.__class__)


@lru_cache(maxsize=None)
def _source_sha256(target: Any) -> str:
    try:
        return sha256_text(inspect.getsource(target))
    except (OSError, TypeError):
        return ""
