from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class ModelResponse:
    provider: str
    model_name: str
    prompt_name: str
    temperature: float
    raw_output: str
    latency_ms: int
    input_tokens: int
    output_tokens: int
    status: str = "ok"


class ModelProvider(Protocol):
    provider: str
    model_name: str
    prompt_name: str
    temperature: float

    def run(self, bundle: dict) -> ModelResponse:
        ...
