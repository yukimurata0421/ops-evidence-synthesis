from __future__ import annotations

import os

import pytest

from ops_evidence_synthesis.ai.vertex import VertexGeminiProvider
from ops_evidence_synthesis.synthesis.output_ingest import parse_model_output
from ops_evidence_synthesis.synthesis.validation import validate_claim_result


@pytest.mark.skipif(
    os.environ.get("OES_RUN_REAL_API_SMOKE") != "1",
    reason="manual real API smoke; set OES_RUN_REAL_API_SMOKE=1 to run",
)
def test_manual_gemini_flash_lite_returns_schema_valid_claim_payload() -> None:
    provider = VertexGeminiProvider.from_env(
        model_name=os.environ.get("OES_FAST_GCP_GEMINI_MODEL", "gemini-3.1-flash-lite"),
        max_output_tokens=1024,
        timeout_seconds=60,
    )
    response = provider.run(
        {
            "schema_version": "ops-evidence-bundle/v1",
            "evidence_sha256": "e" * 64,
            "service": "manual-smoke",
            "environment": "prod",
            "profile": {"profile_id": "manual_smoke"},
            "metric_windows": [],
            "log_patterns": [
                {
                    "pattern_id": "PATTERN-001",
                    "message_template": "worker failed to start because config file was missing",
                    "count": 3,
                    "severity": "ERROR",
                }
            ],
            "operational_evidence": [],
            "evidence_refs": {
                "PATTERN-001": {
                    "summary": "worker failed to start because config file was missing",
                }
            },
        }
    )

    parsed = parse_model_output(response.raw_output)
    assert parsed.parsed is not None, parsed.parse_errors
    valid, errors = validate_claim_result(parsed.parsed)
    assert valid, errors
    assert parsed.parsed["claims"]
