from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ops_evidence_synthesis.ai.base import ModelResponse
from ops_evidence_synthesis.ingest import ingest_jsonl, ingest_log_files
from ops_evidence_synthesis.models import IncidentWindow
from ops_evidence_synthesis.storage.sqlite_store import SQLiteStore
from ops_evidence_synthesis.synthesis.pipeline import run_pipeline


ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True, slots=True)
class DummyProposalProvider:
    provider: str = "dummy-ai"
    model_name: str = "dummy-proposal-model"
    prompt_name: str = "proposal-check"
    temperature: float = 0.0

    def run(self, bundle: dict[str, Any]) -> ModelResponse:
        evidence_ref = next(iter(bundle["evidence_refs"]))
        payload = {
            "schema_version": "claim-result/v1",
            "agent_role": "proposal_generator",
            "summary": "Dummy proposal output for plumbing verification.",
            "claims": [
                {
                    "claim_type": "support",
                    "claim_text": "RTMPS transport instability should be reviewed before changing the stream runtime.",
                    "evidence_refs": [evidence_ref],
                    "counter_evidence_refs": [],
                    "caveats": ["Dummy check only."],
                    "missing_evidence": ["ffmpeg stderr around the event window."],
                    "temporary_action": "Switch encoder output to a safe fallback profile for 10 minutes.",
                    "permanent_action": "Persist RTMPS retransmit counters in the evidence lake.",
                    "required_authority": "incident commander",
                }
            ],
            "propositions": [
                {
                    "question": "Should humans review RTMPS transport before changing runtime settings?",
                    "linked_claim_hints": ["RTMPS transport instability"],
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


def test_dummy_ai_proposal_reaches_proposal_output(tmp_path: Path) -> None:
    jsonl = tmp_path / "stream_events.jsonl"
    jsonl.write_text(
            (
                '{"ts_utc":"2026-06-15T09:49:47Z","kind":"tcp_send_sample",'
                '"message":"ffmpeg tcp send sample","stream_service":"adsb-streamnew-youtube-stream.service",'
                '"environment":"stream_v3","mbps":4.74,"notsent":622,"unacked":5,"lastsnd_ms":0}\n'
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
        providers=[DummyProposalProvider()],
    )

    proposals = store.list_proposals(evidence_sha256=result.evidence_sha256)

    assert result.model_run_count == 1
    assert result.parsed_result_count == 1
    assert result.claim_count == 1
    more_data_query = store.build_more_data_query(proposals[0]["proposition_id"])
    assert more_data_query["evidence_sha256"] == result.evidence_sha256
    assert more_data_query["queries"]
    assert any(query["preview_count"] >= 1 for query in more_data_query["queries"])
    assert more_data_query["preview_rows"]
    assert result.proposition_count == 1
    assert proposals
    assert proposals[0]["question"] == "Should humans review RTMPS transport or ffmpeg send-path instability?"
    assert proposals[0]["suggested_actions"][0]["temporary_action"] == (
        "Switch encoder output to a safe fallback profile for 10 minutes."
    )
    assert proposals[0]["suggested_actions"][0]["permanent_action"] == (
        "Persist RTMPS retransmit counters in the evidence lake."
    )
    assert proposals[0]["suggested_actions"][0]["required_authority"] == "incident commander"
    assert proposals[0]["suggested_actions"][0]["evidence_refs_valid"] is True

    targets = store.list_review_targets(evidence_sha256=result.evidence_sha256)
    assert targets["summary"]["review_targets"] == 1
    assert store.count_table("review_targets") == 1
    target = targets["targets"][0]
    assert target["core_claim"]
    assert target["score_breakdown"]["score_note"] == "Score is review priority, not truth probability."
    response = store.record_review_target(
        target["review_target_id"],
        "accepted",
        "test-reviewer",
        "confirmed in test",
        reason="confirmed_candidate",
    )
    assert response["status"] == "confirmed_candidate"
    reviewed = store.list_proposals(
        evidence_sha256=result.evidence_sha256,
        pending_only=False,
        include_hidden=True,
    )
    assert reviewed[0]["review_status"] == "confirmed_candidate"


def test_list_review_targets_does_not_persist_projection_without_opt_in(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "oes.sqlite3")
    assert ingest_jsonl(ROOT / "data/sample_logs.jsonl", store) == 20
    result = run_pipeline(
        store,
        IncidentWindow(
            service="payment-api",
            environment="prod",
            incident_start="2026-06-12T10:00:00Z",
            incident_end="2026-06-12T10:20:00Z",
            lookback_minutes=45,
        ),
    )
    before = store.count_table("review_targets")

    targets = store.list_review_targets(evidence_sha256=result.evidence_sha256)

    assert targets["targets"]
    assert store.count_table("review_targets") == before


def test_more_data_result_is_recorded_on_review_target_history(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "oes.sqlite3")
    assert ingest_jsonl(ROOT / "data/sample_logs.jsonl", store) == 20
    result = run_pipeline(
        store,
        IncidentWindow(
            service="payment-api",
            environment="prod",
            incident_start="2026-06-12T10:00:00Z",
            incident_end="2026-06-12T10:20:00Z",
            lookback_minutes=45,
        ),
    )
    target = store.list_review_targets(evidence_sha256=result.evidence_sha256)["targets"][0]

    recorded = store.record_more_data_result(
        target["review_target_id"],
        "child-sha",
        {"evidence_delta": {"added_evidence_ref_count": 2}},
    )
    refreshed = store.get_review_target(target["review_target_id"])

    assert recorded["status"] == "more_data_collected"
    assert refreshed["status"] == "more_data_collected"
    assert refreshed["latest_review"]["generated_query"]["child_evidence_sha256"] == "child-sha"
