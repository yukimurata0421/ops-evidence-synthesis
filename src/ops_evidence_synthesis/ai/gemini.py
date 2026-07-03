from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

from ops_evidence_synthesis.ai.base import ModelResponse
from ops_evidence_synthesis.ai.prompts import root_cause_prompt


DEFAULT_GEMINI_MODEL = "gemini-3.1-pro-preview"
DEFAULT_GEMINI_ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
DEFAULT_GEMINI_THINKING_LEVEL = "medium"
GEMINI_THINKING_LEVELS = {"minimal", "low", "medium", "high"}


@dataclass(frozen=True, slots=True)
class GeminiRestProvider:
    provider: str = "gemini"
    model_name: str = DEFAULT_GEMINI_MODEL
    prompt_name: str = "root-cause"
    temperature: float = 0.0
    api_key_env: str = "GEMINI_API_KEY"
    endpoint_template: str = DEFAULT_GEMINI_ENDPOINT
    timeout_seconds: int = 60
    thinking_level: str = DEFAULT_GEMINI_THINKING_LEVEL

    def run(self, bundle: dict[str, Any]) -> ModelResponse:
        api_key = os.environ.get(self.api_key_env)
        if not api_key:
            raise RuntimeError(f"{self.api_key_env} is not set")

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
        url = f"{self.endpoint_template.format(model=self.model_name)}?key={api_key}"
        request = urllib.request.Request(
            url,
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                response_payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Gemini API returned HTTP {exc.code}: {detail}") from exc

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

    def _prompt(self, bundle: dict[str, Any]) -> str:
        return root_cause_prompt(bundle)

    def _generation_config(self) -> dict[str, Any]:
        config: dict[str, Any] = {
            "temperature": self.temperature,
            "responseMimeType": "application/json",
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
            raise RuntimeError(f"unsupported Gemini thinking level '{self.thinking_level}'; supported: {supported}")
        return {"thinkingLevel": level}

    def _extract_text(self, payload: dict[str, Any]) -> str:
        candidates = payload.get("candidates")
        if not isinstance(candidates, list) or not candidates:
            raise RuntimeError("Gemini API response did not include candidates")
        parts = ((candidates[0].get("content") or {}).get("parts") or [])
        texts = [part.get("text", "") for part in parts if isinstance(part, dict)]
        text = "".join(texts).strip()
        if text.startswith("```"):
            text = text.strip("`")
            if text.startswith("json"):
                text = text[4:].strip()
        if not text:
            raise RuntimeError("Gemini API response candidate was empty")
        return text
