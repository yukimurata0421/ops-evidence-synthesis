from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ops_evidence_synthesis.ai.base import ModelResponse
from ops_evidence_synthesis.ingest import ingest_log_files
from ops_evidence_synthesis.models import IncidentWindow
from ops_evidence_synthesis.storage.sqlite_store import SQLiteStore
from ops_evidence_synthesis.synthesis.comparison import compare_providers
from ops_evidence_synthesis.synthesis.pipeline import run_pipeline


@dataclass(frozen=True, slots=True)
class DummyProvider:
    provider: str
    model_name: str
    claim_text: str
    temporary_action: str
    permanent_action: str
    prompt_name: str = "root-cause"
    temperature: float = 0.0

    def run(self, bundle: dict[str, Any]) -> ModelResponse:
        evidence_ref = next(iter(bundle["evidence_refs"]))
        payload = {
            "schema_version": "claim-result/v1",
            "agent_role": "proposal_generator",
            "summary": f"{self.provider} dummy output.",
            "claims": [
                {
                    "claim_type": "support",
                    "claim_text": self.claim_text,
                    "evidence_refs": [evidence_ref],
                    "counter_evidence_refs": [],
                    "caveats": [],
                    "missing_evidence": [],
                    "temporary_action": self.temporary_action,
                    "permanent_action": self.permanent_action,
                    "required_authority": "incident commander",
                }
            ],
            "propositions": [
                {
                    "question": "Should humans review RTMPS transport?",
                    "linked_claim_hints": ["RTMPS transport"],
                }
            ],
        }
        raw_output = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        return ModelResponse(
            provider=self.provider,
            model_name=self.model_name,
            prompt_name=self.prompt_name,
            temperature=self.temperature,
            raw_output=raw_output,
            latency_ms=1,
            input_tokens=10,
            output_tokens=10,
        )


def test_model_comparison_records_baseline_candidate_delta(tmp_path: Path) -> None:
    jsonl = tmp_path / "stream_events.jsonl"
    jsonl.write_text(
        (
            '{"ts_utc":"2026-06-15T09:49:47Z","kind":"tcp_send_sample",'
            '"message":"ffmpeg tcp send sample","stream_service":"adsb-streamnew-youtube-stream.service",'
            '"mbps":4.74,"notsent":622,"unacked":5,"lastsnd_ms":0}\n'
        ),
        encoding="utf-8",
    )
    store = SQLiteStore(tmp_path / "oes.sqlite3")
    ingest_log_files([jsonl], store)

    result = run_pipeline(
        store,
        IncidentWindow(
            service="adsb-streamnew-youtube-stream.service",
            environment="stream_v3",
            incident_start="2026-06-15T09:49:00Z",
            incident_end="2026-06-15T09:52:00Z",
            lookback_minutes=5,
        ),
        providers=[
            DummyProvider(
                provider="gemini-enterprise-agent-platform",
                model_name="gemini-2.5-flash",
                claim_text="RTMPS transport instability should be reviewed first.",
                temporary_action="Pin bitrate while validating RTMPS retries.",
                permanent_action="Persist RTMPS send-path counters.",
            ),
            DummyProvider(
                provider="claude-agent-platform",
                model_name="claude-haiku-4-5",
                claim_text="RTMPS transport and ffmpeg send-path instability should be reviewed first.",
                temporary_action="Move stream output to fallback profile during validation.",
                permanent_action="Add ffmpeg send-path counters to the evidence lake.",
            ),
        ],
    )

    comparison = compare_providers(store, result.evidence_sha256)
    store.insert_model_comparison(comparison)
    saved = store.list_model_comparisons(evidence_sha256=result.evidence_sha256)

    assert comparison["summary"]["shared_target_count"] == 1
    assert comparison["comparisons"][0]["delta_type"] == "shared_target"
    assert comparison["comparisons"][0]["evidence_overlap_score"] == 1.0
    assert comparison["comparisons"][0]["basis"]["baseline_actions"]
    assert comparison["comparisons"][0]["basis"]["candidate_actions"]
    assert saved[0]["comparison_id"] == comparison["comparison_id"]
