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
from ops_evidence_synthesis.ai.prompts import root_cause_prompt


DEFAULT_CLAUDE_MODEL = "claude-haiku-4-5"
DEFAULT_CLAUDE_LOCATION = "global"
DEFAULT_VERTEX_API_VERSION = "v1"
DEFAULT_ANTHROPIC_VERSION = "vertex-2023-10-16"


@dataclass(frozen=True, slots=True)
class VertexClaudeProvider:
    provider: str = "claude-agent-platform"
    model_name: str = DEFAULT_CLAUDE_MODEL
    prompt_name: str = "root-cause"
    project_id: str = ""
    location: str = DEFAULT_CLAUDE_LOCATION
    anthropic_version: str = DEFAULT_ANTHROPIC_VERSION
    temperature: float = 0.0
    max_output_tokens: int = 4096
    timeout_seconds: int = 90
    api_version: str = DEFAULT_VERTEX_API_VERSION

    @classmethod
    def from_env(cls) -> "VertexClaudeProvider":
        return cls(
            model_name=os.environ.get("OES_CLAUDE_MODEL", DEFAULT_CLAUDE_MODEL),
            project_id=(
                os.environ.get("OES_CLAUDE_PROJECT")
                or os.environ.get("OES_VERTEX_PROJECT")
                or os.environ.get("GOOGLE_CLOUD_PROJECT")
                or os.environ.get("GCP_PROJECT")
                or ""
            ),
            location=os.environ.get("OES_CLAUDE_LOCATION", DEFAULT_CLAUDE_LOCATION),
            temperature=float(os.environ.get("OES_CLAUDE_TEMPERATURE", "0")),
            max_output_tokens=int(os.environ.get("OES_CLAUDE_MAX_OUTPUT_TOKENS", "4096")),
            timeout_seconds=int(os.environ.get("OES_CLAUDE_TIMEOUT_SECONDS", "90")),
        )

    def run(self, bundle: dict[str, Any]) -> ModelResponse:
        if not self.project_id:
            raise RuntimeError("OES_CLAUDE_PROJECT, OES_VERTEX_PROJECT, or GOOGLE_CLOUD_PROJECT is required")

        started = time.perf_counter()
        prompt = self._prompt(bundle)
        body = {
            "anthropic_version": self.anthropic_version,
            "messages": [
                {
                    "role": "user",
                    "content": prompt,
                }
            ],
            "max_tokens": self.max_output_tokens,
            "temperature": self.temperature,
            "stream": False,
        }
        request = urllib.request.Request(
            self._raw_predict_url(),
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
            raise RuntimeError(f"Vertex Claude returned HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"Vertex Claude request failed: {exc}") from exc

        raw_text = self._extract_text(response_payload)
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        usage = response_payload.get("usage") or {}
        return ModelResponse(
            provider=self.provider,
            model_name=self.model_name,
            prompt_name=self.prompt_name,
            temperature=self.temperature,
            raw_output=raw_text,
            latency_ms=max(1, elapsed_ms),
            input_tokens=int(usage.get("input_tokens") or max(1, len(prompt) // 4)),
            output_tokens=int(usage.get("output_tokens") or max(1, len(raw_text) // 4)),
            requested_model_name=self.model_name,
            resolved_model_name=str(response_payload.get("model") or ""),
            provider_response_model_id=str(response_payload.get("model") or ""),
        )

    def _raw_predict_url(self) -> str:
        location = self.location.strip()
        endpoint = _endpoint_for_location(location)
        model = (
            f"projects/{self.project_id}/locations/{location}/"
            f"publishers/anthropic/models/{self.model_name}"
        )
        return f"https://{endpoint}/{self.api_version}/{model}:rawPredict"

    def _prompt(self, bundle: dict[str, Any]) -> str:
        return root_cause_prompt(bundle)

    def _extract_text(self, payload: dict[str, Any]) -> str:
        content = payload.get("content")
        if not isinstance(content, list) or not content:
            raise RuntimeError("Vertex Claude response did not include content")
        texts = [
            str(block.get("text") or "")
            for block in content
            if isinstance(block, dict) and block.get("type") in {"text", None}
        ]
        text = "".join(texts).strip()
        if text.startswith("```"):
            text = text.strip("`").strip()
            if text.startswith("json"):
                text = text[4:].strip()
        if not text.startswith("{"):
            start = text.find("{")
            end = text.rfind("}")
            if start >= 0 and end > start:
                text = text[start : end + 1].strip()
        if not text:
            raise RuntimeError("Vertex Claude response content was empty")
        return text

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


def _endpoint_for_location(location: str) -> str:
    if location == "global":
        return "aiplatform.googleapis.com"
    if location in {"us", "eu"}:
        return f"aiplatform.{location}.rep.googleapis.com"
    return f"{location}-aiplatform.googleapis.com"
