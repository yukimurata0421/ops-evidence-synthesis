from __future__ import annotations

import json
import os
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

from ops_evidence_synthesis.ai.base import ModelResponse
from ops_evidence_synthesis.ai.prompts import alternative_hypothesis_prompt


DEFAULT_GPT_OSS_MODEL = "gpt-oss-20b-maas"
DEFAULT_GPT_OSS_LOCATION = "us-central1"
DEFAULT_GPT_OSS_MAX_OUTPUT_TOKENS = 8192
DEFAULT_MISTRAL_MODEL = "mistral-small-2503"
DEFAULT_MISTRAL_LOCATION = "us-central1"
DEFAULT_QWEN_MODEL = "qwen/qwen3-coder-480b-a35b-instruct-maas"
DEFAULT_QWEN_LOCATION = "global"
DEFAULT_GLM_MODEL = "zai-org/glm-5-maas"
DEFAULT_GLM_LOCATION = "global"
DEFAULT_GEMMA_MODEL = "gemma-4-26b-a4b-it-maas"
DEFAULT_GEMMA_LOCATION = "global"
DEFAULT_GROK_MODEL = "grok-4.20-reasoning"
DEFAULT_GROK_LOCATION = "global"
DEFAULT_LLAMA_MODEL = "llama-4-maverick-17b-128e-instruct-maas"
DEFAULT_LLAMA_LOCATION = "us-east5"
DEFAULT_VERTEX_API_VERSION = "v1"


@dataclass(frozen=True, slots=True)
class VertexOpenAICompatProvider:
    provider: str = "openai-gpt-oss-on-vertex"
    model_name: str = DEFAULT_GPT_OSS_MODEL
    prompt_name: str = "alternative-hypothesis"
    project_id: str = ""
    location: str = DEFAULT_GPT_OSS_LOCATION
    temperature: float = 0.0
    max_output_tokens: int = DEFAULT_GPT_OSS_MAX_OUTPUT_TOKENS
    timeout_seconds: int = 240
    max_evidence_items: int = 140
    max_logs: int = 0
    max_normalized_events: int = 0
    max_text_chars: int = 480
    api_version: str = DEFAULT_VERTEX_API_VERSION

    @classmethod
    def from_env(cls) -> "VertexOpenAICompatProvider":
        return cls(
            model_name=os.environ.get("OES_GPT_OSS_MODEL", DEFAULT_GPT_OSS_MODEL),
            project_id=(
                os.environ.get("OES_GPT_OSS_PROJECT")
                or os.environ.get("OES_VERTEX_PROJECT")
                or os.environ.get("GOOGLE_CLOUD_PROJECT")
                or os.environ.get("GCP_PROJECT")
                or ""
            ),
            location=os.environ.get("OES_GPT_OSS_LOCATION", DEFAULT_GPT_OSS_LOCATION),
            temperature=float(os.environ.get("OES_GPT_OSS_TEMPERATURE", "0")),
            max_output_tokens=int(os.environ.get("OES_GPT_OSS_MAX_OUTPUT_TOKENS", str(DEFAULT_GPT_OSS_MAX_OUTPUT_TOKENS))),
            timeout_seconds=int(os.environ.get("OES_GPT_OSS_TIMEOUT_SECONDS", "240")),
            max_evidence_items=int(os.environ.get("OES_GPT_OSS_MAX_EVIDENCE_ITEMS", "140")),
            max_logs=int(os.environ.get("OES_GPT_OSS_MAX_LOGS", "0")),
            max_normalized_events=int(os.environ.get("OES_GPT_OSS_MAX_NORMALIZED_EVENTS", "0")),
            max_text_chars=int(os.environ.get("OES_GPT_OSS_MAX_TEXT_CHARS", "480")),
        )

    def run(self, bundle: dict[str, Any]) -> ModelResponse:
        if not self.project_id:
            raise RuntimeError("OES_GPT_OSS_PROJECT, OES_VERTEX_PROJECT, or GOOGLE_CLOUD_PROJECT is required")

        started = time.perf_counter()
        prompt = alternative_hypothesis_prompt(
            bundle,
            max_evidence_items=self.max_evidence_items,
            max_logs=self.max_logs,
            max_normalized_events=self.max_normalized_events,
            max_text_chars=self.max_text_chars,
        )
        body = {
            "model": self._request_model_name(),
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": self.max_output_tokens,
            "temperature": self.temperature,
            "stream": False,
        }
        response_payload = _post_json(self._chat_completions_url(), body, timeout_seconds=self.timeout_seconds)
        try:
            raw_text = _extract_chat_completion_text(response_payload, model_label="Vertex OpenAI-compatible")
        except RuntimeError as exc:
            if "response content was empty" not in str(exc) or self.max_output_tokens >= 8192:
                raise
            body["max_tokens"] = 8192
            response_payload = _post_json(self._chat_completions_url(), body, timeout_seconds=self.timeout_seconds)
            raw_text = _extract_chat_completion_text(response_payload, model_label="Vertex OpenAI-compatible")
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        usage = response_payload.get("usage") or {}
        return ModelResponse(
            provider=self.provider,
            model_name=self.model_name,
            prompt_name=self.prompt_name,
            temperature=self.temperature,
            raw_output=raw_text,
            latency_ms=max(1, elapsed_ms),
            input_tokens=int(usage.get("prompt_tokens") or max(1, len(prompt) // 4)),
            output_tokens=int(usage.get("completion_tokens") or max(1, len(raw_text) // 4)),
            requested_model_name=self.model_name,
            resolved_model_name=str(response_payload.get("model") or ""),
            provider_response_model_id=str(response_payload.get("model") or ""),
        )

    def _chat_completions_url(self) -> str:
        location = self.location.strip()
        endpoint = _endpoint_for_location(location)
        return (
            f"https://{endpoint}/{self.api_version}/projects/{self.project_id}/"
            f"locations/{location}/endpoints/openapi/chat/completions"
        )

    def _request_model_name(self) -> str:
        if "/" in self.model_name:
            return self.model_name
        if self.model_name.startswith("gpt-oss-"):
            return f"openai/{self.model_name}"
        return self.model_name


@dataclass(frozen=True, slots=True)
class VertexMistralProvider:
    provider: str = "mistral-agent-platform"
    model_name: str = DEFAULT_MISTRAL_MODEL
    prompt_name: str = "alternative-hypothesis"
    project_id: str = ""
    location: str = DEFAULT_MISTRAL_LOCATION
    temperature: float = 0.0
    max_output_tokens: int = 4096
    timeout_seconds: int = 90
    max_evidence_items: int = 140
    max_logs: int = 0
    max_normalized_events: int = 0
    max_text_chars: int = 480
    api_version: str = DEFAULT_VERTEX_API_VERSION

    @classmethod
    def from_env(cls) -> "VertexMistralProvider":
        return cls(
            model_name=os.environ.get("OES_MISTRAL_MODEL", DEFAULT_MISTRAL_MODEL),
            project_id=(
                os.environ.get("OES_MISTRAL_PROJECT")
                or os.environ.get("OES_VERTEX_PROJECT")
                or os.environ.get("GOOGLE_CLOUD_PROJECT")
                or os.environ.get("GCP_PROJECT")
                or ""
            ),
            location=os.environ.get("OES_MISTRAL_LOCATION", DEFAULT_MISTRAL_LOCATION),
            temperature=float(os.environ.get("OES_MISTRAL_TEMPERATURE", "0")),
            max_output_tokens=int(os.environ.get("OES_MISTRAL_MAX_OUTPUT_TOKENS", "4096")),
            timeout_seconds=int(os.environ.get("OES_MISTRAL_TIMEOUT_SECONDS", "90")),
            max_evidence_items=int(os.environ.get("OES_MISTRAL_MAX_EVIDENCE_ITEMS", "140")),
            max_logs=int(os.environ.get("OES_MISTRAL_MAX_LOGS", "0")),
            max_normalized_events=int(os.environ.get("OES_MISTRAL_MAX_NORMALIZED_EVENTS", "0")),
            max_text_chars=int(os.environ.get("OES_MISTRAL_MAX_TEXT_CHARS", "480")),
        )

    def run(self, bundle: dict[str, Any]) -> ModelResponse:
        if not self.project_id:
            raise RuntimeError("OES_MISTRAL_PROJECT, OES_VERTEX_PROJECT, or GOOGLE_CLOUD_PROJECT is required")

        started = time.perf_counter()
        prompt = alternative_hypothesis_prompt(
            bundle,
            max_evidence_items=self.max_evidence_items,
            max_logs=self.max_logs,
            max_normalized_events=self.max_normalized_events,
            max_text_chars=self.max_text_chars,
        )
        body = {
            "model": self.model_name,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": self.max_output_tokens,
            "temperature": self.temperature,
            "stream": False,
        }
        response_payload = _post_json(self._raw_predict_url(), body, timeout_seconds=self.timeout_seconds)
        raw_text = _extract_chat_completion_text(response_payload, model_label="Vertex Mistral")
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        usage = response_payload.get("usage") or {}
        return ModelResponse(
            provider=self.provider,
            model_name=self.model_name,
            prompt_name=self.prompt_name,
            temperature=self.temperature,
            raw_output=raw_text,
            latency_ms=max(1, elapsed_ms),
            input_tokens=int(usage.get("prompt_tokens") or max(1, len(prompt) // 4)),
            output_tokens=int(usage.get("completion_tokens") or max(1, len(raw_text) // 4)),
            requested_model_name=self.model_name,
            resolved_model_name=str(response_payload.get("model") or ""),
            provider_response_model_id=str(response_payload.get("model") or ""),
        )

    def _raw_predict_url(self) -> str:
        location = self.location.strip()
        endpoint = _endpoint_for_location(location)
        model = (
            f"projects/{self.project_id}/locations/{location}/"
            f"publishers/mistralai/models/{self.model_name}"
        )
        return f"https://{endpoint}/{self.api_version}/{model}:rawPredict"


@dataclass(frozen=True, slots=True)
class VertexOpenModelProvider:
    provider: str
    model_name: str
    default_publisher: str
    prompt_name: str = "alternative-hypothesis"
    project_id: str = ""
    location: str = "global"
    temperature: float = 0.0
    max_output_tokens: int = 8192
    timeout_seconds: int = 240
    max_evidence_items: int = 140
    max_logs: int = 0
    max_normalized_events: int = 0
    max_text_chars: int = 480
    api_version: str = DEFAULT_VERTEX_API_VERSION

    @classmethod
    def from_qwen_env(cls) -> "VertexOpenModelProvider":
        return cls(
            provider="qwen-agent-platform",
            model_name=os.environ.get("OES_QWEN_MODEL", DEFAULT_QWEN_MODEL),
            default_publisher="qwen",
            project_id=_open_model_project("QWEN"),
            location=os.environ.get("OES_QWEN_LOCATION", DEFAULT_QWEN_LOCATION),
            temperature=float(os.environ.get("OES_QWEN_TEMPERATURE", "0")),
            max_output_tokens=int(os.environ.get("OES_QWEN_MAX_OUTPUT_TOKENS", "8192")),
            timeout_seconds=int(os.environ.get("OES_QWEN_TIMEOUT_SECONDS", "240")),
            max_evidence_items=int(os.environ.get("OES_QWEN_MAX_EVIDENCE_ITEMS", "140")),
            max_logs=int(os.environ.get("OES_QWEN_MAX_LOGS", "0")),
            max_normalized_events=int(os.environ.get("OES_QWEN_MAX_NORMALIZED_EVENTS", "0")),
            max_text_chars=int(os.environ.get("OES_QWEN_MAX_TEXT_CHARS", "480")),
        )

    @classmethod
    def from_glm_env(cls) -> "VertexOpenModelProvider":
        return cls(
            provider="glm-agent-platform",
            model_name=os.environ.get("OES_GLM_MODEL", DEFAULT_GLM_MODEL),
            default_publisher="zai-org",
            project_id=_open_model_project("GLM"),
            location=os.environ.get("OES_GLM_LOCATION", DEFAULT_GLM_LOCATION),
            temperature=float(os.environ.get("OES_GLM_TEMPERATURE", "0")),
            max_output_tokens=int(os.environ.get("OES_GLM_MAX_OUTPUT_TOKENS", "8192")),
            timeout_seconds=int(os.environ.get("OES_GLM_TIMEOUT_SECONDS", "240")),
            max_evidence_items=int(os.environ.get("OES_GLM_MAX_EVIDENCE_ITEMS", "140")),
            max_logs=int(os.environ.get("OES_GLM_MAX_LOGS", "0")),
            max_normalized_events=int(os.environ.get("OES_GLM_MAX_NORMALIZED_EVENTS", "0")),
            max_text_chars=int(os.environ.get("OES_GLM_MAX_TEXT_CHARS", "480")),
        )

    @classmethod
    def from_gemma_env(cls) -> "VertexOpenModelProvider":
        return cls(
            provider="gemma-agent-platform",
            model_name=os.environ.get("OES_GEMMA_MODEL", DEFAULT_GEMMA_MODEL),
            default_publisher="google",
            project_id=_open_model_project("GEMMA"),
            location=os.environ.get("OES_GEMMA_LOCATION", DEFAULT_GEMMA_LOCATION),
            temperature=float(os.environ.get("OES_GEMMA_TEMPERATURE", "0")),
            max_output_tokens=int(os.environ.get("OES_GEMMA_MAX_OUTPUT_TOKENS", "8192")),
            timeout_seconds=int(os.environ.get("OES_GEMMA_TIMEOUT_SECONDS", "240")),
            max_evidence_items=int(os.environ.get("OES_GEMMA_MAX_EVIDENCE_ITEMS", "140")),
            max_logs=int(os.environ.get("OES_GEMMA_MAX_LOGS", "0")),
            max_normalized_events=int(os.environ.get("OES_GEMMA_MAX_NORMALIZED_EVENTS", "0")),
            max_text_chars=int(os.environ.get("OES_GEMMA_MAX_TEXT_CHARS", "480")),
        )

    @classmethod
    def from_llama_env(cls) -> "VertexOpenModelProvider":
        return cls(
            provider="llama-agent-platform",
            model_name=os.environ.get("OES_LLAMA_MODEL", DEFAULT_LLAMA_MODEL),
            default_publisher="meta",
            project_id=_open_model_project("LLAMA"),
            location=os.environ.get("OES_LLAMA_LOCATION", DEFAULT_LLAMA_LOCATION),
            temperature=float(os.environ.get("OES_LLAMA_TEMPERATURE", "0")),
            max_output_tokens=int(os.environ.get("OES_LLAMA_MAX_OUTPUT_TOKENS", "8192")),
            timeout_seconds=int(os.environ.get("OES_LLAMA_TIMEOUT_SECONDS", "240")),
            max_evidence_items=int(os.environ.get("OES_LLAMA_MAX_EVIDENCE_ITEMS", "140")),
            max_logs=int(os.environ.get("OES_LLAMA_MAX_LOGS", "0")),
            max_normalized_events=int(os.environ.get("OES_LLAMA_MAX_NORMALIZED_EVENTS", "0")),
            max_text_chars=int(os.environ.get("OES_LLAMA_MAX_TEXT_CHARS", "480")),
        )

    @classmethod
    def from_grok_env(cls) -> "VertexOpenModelProvider":
        return cls(
            provider="grok-agent-platform",
            model_name=os.environ.get("OES_GROK_MODEL", DEFAULT_GROK_MODEL),
            default_publisher="xai",
            project_id=_open_model_project("GROK"),
            location=os.environ.get("OES_GROK_LOCATION", DEFAULT_GROK_LOCATION),
            temperature=float(os.environ.get("OES_GROK_TEMPERATURE", "0")),
            max_output_tokens=int(os.environ.get("OES_GROK_MAX_OUTPUT_TOKENS", "8192")),
            timeout_seconds=int(os.environ.get("OES_GROK_TIMEOUT_SECONDS", "300")),
            max_evidence_items=int(os.environ.get("OES_GROK_MAX_EVIDENCE_ITEMS", "140")),
            max_logs=int(os.environ.get("OES_GROK_MAX_LOGS", "0")),
            max_normalized_events=int(os.environ.get("OES_GROK_MAX_NORMALIZED_EVENTS", "0")),
            max_text_chars=int(os.environ.get("OES_GROK_MAX_TEXT_CHARS", "480")),
        )

    def run(self, bundle: dict[str, Any]) -> ModelResponse:
        if not self.project_id:
            raise RuntimeError("OES_VERTEX_PROJECT, GOOGLE_CLOUD_PROJECT, or provider-specific project is required")

        started = time.perf_counter()
        prompt = alternative_hypothesis_prompt(
            bundle,
            max_evidence_items=self.max_evidence_items,
            max_logs=self.max_logs,
            max_normalized_events=self.max_normalized_events,
            max_text_chars=self.max_text_chars,
        )
        body = {
            "model": self._request_model_name(),
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": self.max_output_tokens,
            "temperature": self.temperature,
            "stream": False,
        }
        response_payload = _post_json(self._chat_completions_url(), body, timeout_seconds=self.timeout_seconds)
        try:
            raw_text = _extract_chat_completion_text(response_payload, model_label="Vertex open model")
        except RuntimeError as exc:
            if "response content was empty" not in str(exc) or self.max_output_tokens >= 8192:
                raise
            body["max_tokens"] = 8192
            response_payload = _post_json(self._chat_completions_url(), body, timeout_seconds=self.timeout_seconds)
            raw_text = _extract_chat_completion_text(response_payload, model_label="Vertex open model")
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        usage = response_payload.get("usage") or {}
        return ModelResponse(
            provider=self.provider,
            model_name=self.model_name,
            prompt_name=self.prompt_name,
            temperature=self.temperature,
            raw_output=raw_text,
            latency_ms=max(1, elapsed_ms),
            input_tokens=int(usage.get("prompt_tokens") or max(1, len(prompt) // 4)),
            output_tokens=int(usage.get("completion_tokens") or max(1, len(raw_text) // 4)),
            requested_model_name=self.model_name,
            resolved_model_name=str(response_payload.get("model") or ""),
            provider_response_model_id=str(response_payload.get("model") or ""),
        )

    def _chat_completions_url(self) -> str:
        location = self.location.strip()
        endpoint = _endpoint_for_location(location)
        return (
            f"https://{endpoint}/{self.api_version}/projects/{self.project_id}/"
            f"locations/{location}/endpoints/openapi/chat/completions"
        )

    def _request_model_name(self) -> str:
        model = self.model_name.strip()
        if "/" in model:
            return model
        if not self.default_publisher:
            return model
        return f"{self.default_publisher}/{model}"


def _open_model_project(prefix: str) -> str:
    return (
        os.environ.get(f"OES_{prefix}_PROJECT")
        or os.environ.get("OES_VERTEX_PROJECT")
        or os.environ.get("GOOGLE_CLOUD_PROJECT")
        or os.environ.get("GCP_PROJECT")
        or ""
    )


def _post_json(url: str, body: dict[str, Any], *, timeout_seconds: int) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {_access_token()}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            return dict(json.loads(response.read().decode("utf-8")))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Vertex MaaS returned HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Vertex MaaS request failed: {exc}") from exc


def _extract_chat_completion_text(payload: dict[str, Any], *, model_label: str) -> str:
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        raise RuntimeError(f"{model_label} response did not include choices")
    choice = choices[0]
    if not isinstance(choice, dict):
        raise RuntimeError(f"{model_label} response choice was invalid")
    message = choice.get("message") or {}
    text = ""
    if isinstance(message, dict):
        content = message.get("content")
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            text = "".join(
                str(part.get("text") or "")
                for part in content
                if isinstance(part, dict)
            )
    if not text and isinstance(choice.get("text"), str):
        text = str(choice["text"])
    text = _extract_jsonish_text(text)
    if not text:
        raise RuntimeError(f"{model_label} response content was empty")
    return text


def _extract_jsonish_text(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`").strip()
        if text.startswith("json"):
            text = text[4:].strip()
    if not text.startswith("{"):
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            text = text[start : end + 1].strip()
    return text


def _access_token() -> str:
    token = os.environ.get("GOOGLE_OAUTH_ACCESS_TOKEN")
    if token:
        return token

    metadata_token = _metadata_access_token()
    if metadata_token:
        return metadata_token

    gcloud_token = _gcloud_access_token()
    if gcloud_token:
        return gcloud_token

    raise RuntimeError("Unable to obtain Google Cloud access token")


def _metadata_access_token() -> str:
    request = urllib.request.Request(
        "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token",
        headers={"Metadata-Flavor": "Google"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=2) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except Exception:
        return ""
    return str(payload.get("access_token") or "")


def _gcloud_access_token() -> str:
    try:
        completed = subprocess.run(
            ["gcloud", "auth", "print-access-token"],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except Exception:
        return ""
    return completed.stdout.strip()


def _endpoint_for_location(location: str) -> str:
    if location == "global":
        return "aiplatform.googleapis.com"
    if location in {"us", "eu"}:
        return f"aiplatform.{location}.rep.googleapis.com"
    return f"{location}-aiplatform.googleapis.com"
