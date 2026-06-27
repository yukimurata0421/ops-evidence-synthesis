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
from ops_evidence_synthesis.ai.prompts import (
    claim_result_response_schema,
    evidence_requirement_prompt,
    evidence_requirements_response_schema,
    root_cause_prompt,
)


DEFAULT_VERTEX_MODEL = "gemini-3.1-flash-lite"
DEFAULT_VERTEX_LOCATION = "global"
DEFAULT_VERTEX_API_VERSION = "v1"
DEFAULT_VERTEX_THINKING_LEVEL = "medium"
GEMINI_THINKING_LEVELS = {"minimal", "low", "medium", "high"}


@dataclass(frozen=True, slots=True)
class VertexGeminiProvider:
    provider: str = "gemini-enterprise-agent-platform"
    model_name: str = DEFAULT_VERTEX_MODEL
    prompt_name: str = "root-cause"
    project_id: str = ""
    location: str = DEFAULT_VERTEX_LOCATION
    temperature: float = 0.0
    max_output_tokens: int = 8192
    timeout_seconds: int = 60
    api_version: str = DEFAULT_VERTEX_API_VERSION
    thinking_level: str = DEFAULT_VERTEX_THINKING_LEVEL

    @classmethod
    def from_env(cls) -> "VertexGeminiProvider":
        return cls(
            model_name=os.environ.get("OES_GEMINI_MODEL", DEFAULT_VERTEX_MODEL),
            project_id=(
                os.environ.get("OES_VERTEX_PROJECT")
                or os.environ.get("GOOGLE_CLOUD_PROJECT")
                or os.environ.get("GCP_PROJECT")
                or ""
            ),
            location=os.environ.get("OES_VERTEX_LOCATION", DEFAULT_VERTEX_LOCATION),
            temperature=float(os.environ.get("OES_GEMINI_TEMPERATURE", "0")),
            max_output_tokens=int(os.environ.get("OES_GEMINI_MAX_OUTPUT_TOKENS", "8192")),
            timeout_seconds=int(os.environ.get("OES_GEMINI_TIMEOUT_SECONDS", "60")),
            thinking_level=os.environ.get("OES_GEMINI_THINKING_LEVEL", DEFAULT_VERTEX_THINKING_LEVEL),
        )

    def run(self, bundle: dict[str, Any]) -> ModelResponse:
        if not self.project_id:
            raise RuntimeError("OES_VERTEX_PROJECT or GOOGLE_CLOUD_PROJECT is required for Vertex Gemini")

        started = time.perf_counter()
        prompt = self._prompt(bundle)
        body = {
            "contents": [
                {
                    "role": "user",
                    "parts": [{"text": prompt}],
                }
            ],
            "generationConfig": self._generation_config(),
        }
        request = urllib.request.Request(
            self._generate_content_url(),
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self._access_token()}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                response_payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Vertex Gemini returned HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"Vertex Gemini request failed: {exc}") from exc

        raw_text = self._extract_text(response_payload)
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        usage = response_payload.get("usageMetadata") or {}
        return ModelResponse(
            provider=self.provider,
            model_name=self.model_name,
            prompt_name=self.prompt_name,
            temperature=self.temperature,
            raw_output=raw_text,
            latency_ms=max(1, elapsed_ms),
            input_tokens=int(usage.get("promptTokenCount") or max(1, len(prompt) // 4)),
            output_tokens=int(usage.get("candidatesTokenCount") or max(1, len(raw_text) // 4)),
        )

    def _generate_content_url(self) -> str:
        location = self.location.strip()
        endpoint = "aiplatform.googleapis.com" if location == "global" else f"{location}-aiplatform.googleapis.com"
        model = (
            f"projects/{self.project_id}/locations/{location}/"
            f"publishers/google/models/{self.model_name}"
        )
        return f"https://{endpoint}/{self.api_version}/{model}:generateContent"

    def _prompt(self, bundle: dict[str, Any]) -> str:
        if self.prompt_name == "evidence-requirements":
            return evidence_requirement_prompt(bundle)
        return root_cause_prompt(bundle)

    def _generation_config(self) -> dict[str, Any]:
        config: dict[str, Any] = {
            "temperature": self.temperature,
            "maxOutputTokens": self.max_output_tokens,
            "responseMimeType": "application/json",
            "responseSchema": self._response_schema(),
        }
        thinking_config = self._thinking_config()
        if thinking_config:
            config["thinkingConfig"] = thinking_config
        return config

    def _thinking_config(self) -> dict[str, str]:
        if not self.model_name.startswith("gemini-3"):
            return {}
        level = self.thinking_level.strip().casefold()
        if not level:
            return {}
        if level not in GEMINI_THINKING_LEVELS:
            supported = ", ".join(sorted(GEMINI_THINKING_LEVELS))
            raise RuntimeError(f"unsupported OES_GEMINI_THINKING_LEVEL '{self.thinking_level}'; supported: {supported}")
        return {"thinkingLevel": level}

    def _response_schema(self) -> dict[str, Any]:
        if self.prompt_name == "evidence-requirements":
            return evidence_requirements_response_schema()
        return claim_result_response_schema()

    def _access_token(self) -> str:
        token = os.environ.get("GOOGLE_OAUTH_ACCESS_TOKEN")
        if token:
            return token

        metadata_token = self._metadata_access_token()
        if metadata_token:
            return metadata_token

        gcloud_token = self._gcloud_access_token()
        if gcloud_token:
            return gcloud_token

        raise RuntimeError("Unable to obtain Google Cloud access token")

    def _metadata_access_token(self) -> str:
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

    def _gcloud_access_token(self) -> str:
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

    def _extract_text(self, payload: dict[str, Any]) -> str:
        candidates = payload.get("candidates")
        if not isinstance(candidates, list) or not candidates:
            raise RuntimeError("Vertex Gemini response did not include candidates")
        parts = ((candidates[0].get("content") or {}).get("parts") or [])
        texts = [part.get("text", "") for part in parts if isinstance(part, dict)]
        text = "".join(texts).strip()
        if text.startswith("```"):
            text = text.strip("`")
            if text.startswith("json"):
                text = text[4:].strip()
        if not text:
            raise RuntimeError("Vertex Gemini response candidate was empty")
        return text
