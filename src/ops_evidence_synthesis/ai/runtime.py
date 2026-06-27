from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass
from typing import Any

from ops_evidence_synthesis.ai.base import ModelProvider, ModelResponse
from ops_evidence_synthesis.ai.prompts import compact_bundle_for_model
from ops_evidence_synthesis.canonical import pretty_json, sha256_json
from ops_evidence_synthesis.local_first import scan_sanitized_text


TRANSIENT_ERROR_MARKERS = (
    "429",
    "500",
    "502",
    "503",
    "504",
    "deadline",
    "exhausted",
    "quota",
    "rate limit",
    "temporarily",
    "throttle",
    "timeout",
    "timed out",
    "unavailable",
)


@dataclass(frozen=True, slots=True)
class SafetyPreflightResult:
    passed: bool
    finding_types: tuple[str, ...]
    failure_reason: str
    finding_count: int = 0


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    max_attempts: int
    base_delay_seconds: float
    max_delay_seconds: float


@dataclass(frozen=True, slots=True)
class ProviderRunResult:
    response: ModelResponse
    attempts: int
    max_attempts: int
    retried: bool
    retryable: bool
    failure_reason: str
    exception_type: str = ""

    def retry_metadata(self) -> dict[str, Any]:
        return {
            "attempts": self.attempts,
            "max_attempts": self.max_attempts,
            "retried": self.retried,
            "retryable": self.retryable,
            "failure_reason": self.failure_reason,
            "exception_type": self.exception_type,
        }


def compact_model_input_for_bundle(bundle: dict[str, Any]) -> dict[str, Any]:
    return compact_bundle_for_model(bundle)


def compact_model_input_sha256(bundle: dict[str, Any]) -> str:
    return sha256_json(compact_model_input_for_bundle(bundle))


def safety_preflight_for_bundle(bundle: dict[str, Any]) -> SafetyPreflightResult:
    return safety_preflight_for_model_input(compact_model_input_for_bundle(bundle), filename="legacy_model_input.json")


def safety_preflight_for_model_input(
    model_input: dict[str, Any],
    *,
    filename: str = "model_input.json",
) -> SafetyPreflightResult:
    scan = scan_sanitized_text(filename, _model_safety_scan_text(model_input))
    findings = list(scan.get("findings") or [])
    if not findings:
        return SafetyPreflightResult(True, (), "", 0)
    finding_types = tuple(sorted({str(item.get("type") or "unknown") for item in findings}))
    return SafetyPreflightResult(False, finding_types, "secret_like_pattern_detected", len(findings))


def blocked_provider_response(provider: ModelProvider, preflight: SafetyPreflightResult) -> ModelResponse:
    return ModelResponse(
        provider=provider.provider,
        model_name=provider.model_name,
        prompt_name=provider.prompt_name,
        temperature=provider.temperature,
        raw_output=json.dumps(
            {
                "schema_version": "provider-blocked/v1",
                "error_type": "safety_preflight_blocked",
                "status": "blocked_by_safety_preflight",
                "failure_reason": preflight.failure_reason,
                "finding_types": list(preflight.finding_types),
                "finding_count": preflight.finding_count,
            },
            ensure_ascii=False,
            sort_keys=True,
        ),
        latency_ms=0,
        input_tokens=0,
        output_tokens=0,
        status="blocked_by_safety_preflight",
    )


def run_provider_with_retries(provider: ModelProvider, bundle: dict[str, Any]) -> ProviderRunResult:
    policy = retry_policy_for_provider(provider.provider)
    attempts = 0
    started = time.perf_counter()
    last_retryable = False
    while attempts < policy.max_attempts:
        attempts += 1
        try:
            response = provider.run(bundle)
            if response.status == "timeout" and attempts < policy.max_attempts:
                last_retryable = True
                _sleep_before_retry(policy, attempts)
                continue
            return ProviderRunResult(
                response=response,
                attempts=attempts,
                max_attempts=policy.max_attempts,
                retried=attempts > 1,
                retryable=response.status == "timeout",
                failure_reason="" if response.status == "ok" else str(response.status or "provider_status"),
            )
        except TimeoutError as exc:
            last_retryable = True
            if attempts < policy.max_attempts:
                _sleep_before_retry(policy, attempts)
                continue
            return ProviderRunResult(
                response=_provider_error_response(provider, exc, "timeout", "provider_timeout", attempts, policy, started),
                attempts=attempts,
                max_attempts=policy.max_attempts,
                retried=attempts > 1,
                retryable=True,
                failure_reason="provider_timeout",
                exception_type=exc.__class__.__name__,
            )
        except Exception as exc:
            retryable = is_retryable_provider_error(exc)
            last_retryable = retryable
            if retryable and attempts < policy.max_attempts:
                _sleep_before_retry(policy, attempts)
                continue
            return ProviderRunResult(
                response=_provider_error_response(provider, exc, "failed", "provider_exception", attempts, policy, started),
                attempts=attempts,
                max_attempts=policy.max_attempts,
                retried=attempts > 1,
                retryable=retryable,
                failure_reason="provider_exception",
                exception_type=exc.__class__.__name__,
            )
    return ProviderRunResult(
        response=_provider_error_response(
            provider,
            RuntimeError("provider execution failed"),
            "failed",
            "provider_exception",
            attempts,
            policy,
            started,
        ),
        attempts=attempts,
        max_attempts=policy.max_attempts,
        retried=attempts > 1,
        retryable=last_retryable,
        failure_reason="provider_exception",
        exception_type="RuntimeError",
    )


def retry_policy_for_provider(provider_id: str) -> RetryPolicy:
    provider_key = _env_key_part(provider_id)
    max_attempts = _int_env(f"OES_{provider_key}_MAX_ATTEMPTS", _int_env("OES_MODEL_MAX_ATTEMPTS", 2))
    base_delay = _float_env(
        f"OES_{provider_key}_RETRY_BASE_SECONDS",
        _float_env("OES_MODEL_RETRY_BASE_SECONDS", 0.25),
    )
    max_delay = _float_env(
        f"OES_{provider_key}_RETRY_MAX_SECONDS",
        _float_env("OES_MODEL_RETRY_MAX_SECONDS", 5.0),
    )
    return RetryPolicy(
        max_attempts=max(1, min(max_attempts, 8)),
        base_delay_seconds=max(0.0, min(base_delay, 30.0)),
        max_delay_seconds=max(0.0, min(max_delay, 120.0)),
    )


def is_retryable_provider_error(exc: BaseException) -> bool:
    text = f"{exc.__class__.__name__}: {exc}".casefold()
    return any(marker in text for marker in TRANSIENT_ERROR_MARKERS)


def normalize_model_status(status: str) -> str:
    value = str(status or "").strip()
    if value == "error":
        return "failed"
    return value or "failed"


def cost_estimate_for_response(response: ModelResponse) -> dict[str, Any]:
    return cost_estimate_for_tokens(
        provider_id=response.provider,
        model_name=response.model_name,
        input_tokens=int(response.input_tokens or 0),
        output_tokens=int(response.output_tokens or 0),
    )


def cost_estimate_for_tokens(
    *,
    provider_id: str,
    model_name: str,
    input_tokens: int,
    output_tokens: int,
) -> dict[str, Any]:
    input_rate = _rate_for(provider_id, model_name, "INPUT")
    output_rate = _rate_for(provider_id, model_name, "OUTPUT")
    estimated = (input_tokens / 1_000_000 * input_rate) + (output_tokens / 1_000_000 * output_rate)
    pricing_source = "env" if input_rate or output_rate else "not_configured"
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "input_usd_per_1m_tokens": input_rate,
        "output_usd_per_1m_tokens": output_rate,
        "estimated_cost_usd": round(estimated, 8),
        "pricing_source": pricing_source,
    }


def summarize_model_run_costs(model_runs: list[dict[str, Any]]) -> dict[str, Any]:
    input_tokens = sum(int(run.get("input_tokens") or 0) for run in model_runs)
    output_tokens = sum(int(run.get("output_tokens") or 0) for run in model_runs)
    estimated_cost = 0.0
    configured_count = 0
    for run in model_runs:
        cost = run.get("cost_estimate") if isinstance(run.get("cost_estimate"), dict) else {}
        estimated_cost += float(cost.get("estimated_cost_usd") or 0.0)
        if cost.get("pricing_source") == "env":
            configured_count += 1
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "estimated_cost_usd": round(estimated_cost, 8),
        "priced_run_count": configured_count,
        "pricing_source": "env" if configured_count else "not_configured",
    }


def _provider_error_response(
    provider: ModelProvider,
    exc: BaseException,
    status: str,
    failure_reason: str,
    attempts: int,
    policy: RetryPolicy,
    started: float,
) -> ModelResponse:
    message = _safe_error_message(exc)
    return ModelResponse(
        provider=provider.provider,
        model_name=provider.model_name,
        prompt_name=provider.prompt_name,
        temperature=provider.temperature,
        raw_output=json.dumps(
            {
                "schema_version": "provider-error/v1",
                "error_type": "provider_error",
                "status": status,
                "failure_reason": failure_reason,
                "message": message,
                "exception_type": exc.__class__.__name__,
                "retry_attempts": attempts,
                "max_attempts": policy.max_attempts,
            },
            ensure_ascii=False,
            sort_keys=True,
        ),
        latency_ms=max(1, int((time.perf_counter() - started) * 1000)),
        input_tokens=0,
        output_tokens=0,
        status=status,
    )


def safe_provider_error_message(message: str, *, max_chars: int = 500) -> str:
    text = re.sub(r"https?://\S+", "<URL>", str(message or ""))
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return ""
    text = text[:max(1, max_chars)]
    if scan_sanitized_text("provider_error.txt", text).get("findings"):
        return "provider error redacted by safety policy"
    return text


def _safe_error_message(exc: BaseException) -> str:
    message = safe_provider_error_message(str(exc), max_chars=500)
    if not message:
        return exc.__class__.__name__
    return message


def _model_safety_scan_text(model_input: dict[str, Any]) -> str:
    text = pretty_json(model_input)
    replacements = (
        (r"(?i)\bAuthorization\s*:\s*<AUTH_HEADER>", "sanitized_auth_header_placeholder"),
        (r"(?i)\bBearer\s+<REDACTED_SECRET>", "sanitized_bearer_placeholder"),
        (r"(?i)\bBasic\s+<REDACTED_SECRET>", "sanitized_basic_auth_placeholder"),
        (r"(?i)\bCookie\s*:\s*<COOKIE>", "sanitized_cookie_placeholder"),
        (r"(?i)\bSet-Cookie\s*:\s*<COOKIE>", "sanitized_set_cookie_placeholder"),
        (r"(?i)\bapi_key\s*=\s*<SECRET>", "sanitized_api_key_placeholder"),
        (r"(?i)\baccess_token\s*=\s*<SECRET>", "sanitized_access_token_placeholder"),
        (r"(?i)\brefresh_token\s*=\s*<SECRET>", "sanitized_refresh_token_placeholder"),
        (r"(?i)\bpassword\s*=\s*<SECRET>", "sanitized_password_placeholder"),
        (r"(?i)\bsecret\s*=\s*<SECRET>", "sanitized_secret_placeholder"),
    )
    for pattern, replacement in replacements:
        text = re.sub(pattern, replacement, text)
    return text


def _sleep_before_retry(policy: RetryPolicy, attempt: int) -> None:
    delay = min(policy.max_delay_seconds, policy.base_delay_seconds * (2 ** max(0, attempt - 1)))
    if delay > 0:
        time.sleep(delay)


def _rate_for(provider_id: str, model_name: str, direction: str) -> float:
    keys = (
        f"OES_{_env_key_part(provider_id)}_{direction}_USD_PER_1M_TOKENS",
        f"OES_{_env_key_part(model_name)}_{direction}_USD_PER_1M_TOKENS",
        f"OES_MODEL_{direction}_USD_PER_1M_TOKENS",
    )
    for key in keys:
        if key in os.environ:
            return max(0.0, _float_env(key, 0.0))
    return 0.0


def _env_key_part(value: str) -> str:
    text = re.sub(r"[^A-Za-z0-9]+", "_", str(value or "")).strip("_").upper()
    return text or "MODEL"


def _int_env(key: str, default: int) -> int:
    try:
        return int(os.environ.get(key, str(default)))
    except ValueError:
        return default


def _float_env(key: str, default: float) -> float:
    try:
        return float(os.environ.get(key, str(default)))
    except ValueError:
        return default
