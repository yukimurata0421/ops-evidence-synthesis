from __future__ import annotations

from ops_evidence_synthesis.models import ParsedResultRecord
from ops_evidence_synthesis.synthesis.router import route_claims


def test_router_keeps_unsupported_claims_visible() -> None:
    bundle = {
        "evidence_sha256": "a" * 64,
        "evidence_refs": {"LOG-001": {"type": "log", "summary": "known"}},
    }
    result = ParsedResultRecord(
        result_id="result-1",
        run_id="run-1",
        evidence_sha256="a" * 64,
        provider="test-provider",
        parsed_json={
            "schema_version": "claim-result/v1",
            "summary": "test",
            "claims": [
                {
                    "claim_type": "support",
                    "claim_text": "database connection pool saturation",
                    "evidence_refs": ["LOG-999"],
                    "counter_evidence_refs": [],
                    "caveats": [],
                    "missing_evidence": [],
                    "temporary_action": "",
                    "permanent_action": "",
                    "required_authority": "",
                }
            ],
        },
        parsed_json_sha256="b" * 64,
        schema_valid=True,
        schema_errors=(),
        created_at="2026-06-12T10:00:00Z",
    )

    routed = route_claims(bundle, [result])

    assert len(routed.claims) == 1
    assert routed.claims[0].evidence_refs_valid is False
    assert routed.propositions[0].priority == "high"


def test_router_uses_subsystem_routing_without_stream_v3_environment() -> None:
    bundle = {
        "evidence_sha256": "a" * 64,
        "environment": "prod",
        "evidence_refs": {
            "LOG-001": {
                "type": "log",
                "summary": "ffmpeg RTMPS send-path stalled",
                "subsystem": "rtmps_ffmpeg",
            }
        },
    }
    result = _result(
        "RTMPS transport disappeared and ffmpeg send-path stalled.",
        ["LOG-001"],
    )

    routed = route_claims(bundle, [result])

    assert routed.claims[0].subsystem == "rtmps_ffmpeg"
    assert routed.propositions[0].subsystem == "rtmps_ffmpeg"
    assert routed.propositions[0].question == "Should humans review RTMPS transport or ffmpeg send-path instability?"


def test_router_service_health_question_is_generic() -> None:
    bundle = {
        "evidence_sha256": "a" * 64,
        "environment": "stream_v3",
        "evidence_refs": {
            "LOG-001": {
                "type": "log",
                "summary": "subsystems_status snapshot showed service health failure",
            }
        },
    }
    result = _result(
        "subsystems_status snapshot showed service health failure.",
        ["LOG-001"],
    )

    routed = route_claims(bundle, [result])

    assert routed.propositions[0].question == "Should humans review service health, restart, or recovery failure?"
    assert "stream_v3 service health" not in routed.propositions[0].question


def test_router_preserves_insufficient_evidence_status_and_identity() -> None:
    bundle = {
        "evidence_sha256": "a" * 64,
        "environment": "prod",
        "evidence_refs": {},
    }
    result = ParsedResultRecord(
        result_id="result-1",
        run_id="run-1",
        evidence_sha256="a" * 64,
        provider="test-provider",
        parsed_json={
            "schema_version": "claim-result/v1",
            "finding_status": "insufficient_evidence",
            "summary": "test",
            "claims": [
                {
                    "claim_type": "insufficient_evidence",
                    "finding_status": "insufficient_evidence",
                    "claim_text": "The evidence says an error occurred, but the program and failure signature are unknown.",
                    "subsystem": "general",
                    "evidence_identity": {
                        "program": "unknown",
                        "source": "known",
                        "failure_signature": "unknown",
                        "time_window": "known",
                    },
                    "evidence_refs": [],
                    "counter_evidence_refs": [],
                    "caveats": [],
                    "missing_evidence": ["program name", "exact error message"],
                    "temporary_action": "",
                    "permanent_action": "",
                    "required_authority": "",
                }
            ],
        },
        parsed_json_sha256="b" * 64,
        schema_valid=True,
        schema_errors=(),
        created_at="2026-06-12T10:00:00Z",
    )

    routed = route_claims(bundle, [result])

    assert routed.claims[0].finding_status == "insufficient_evidence"
    assert routed.claims[0].evidence_identity["program"] == "unknown"
    assert routed.propositions[0].structured_evidence["support"] == []
    assert routed.propositions[0].structured_evidence["insufficient_evidence"][0]["claim_id"] == routed.claims[0].claim_id


def test_router_normalizes_concrete_evidence_identity_values_to_known() -> None:
    bundle = {
        "evidence_sha256": "a" * 64,
        "environment": "prod",
        "evidence_refs": {"EV-1": {}},
    }
    result = ParsedResultRecord(
        result_id="result-1",
        run_id="run-1",
        evidence_sha256="a" * 64,
        provider="test-provider",
        parsed_json={
            "schema_version": "claim-result/v1",
            "finding_status": "supported",
            "summary": "test",
            "claims": [
                {
                    "claim_type": "support",
                    "finding_status": "supported",
                    "claim_text": "The systemd journal identifies stream.sh as the emitting program.",
                    "subsystem": "service_liveness",
                    "evidence_identity": {
                        "program": "stream.sh",
                        "source": "systemd_journal",
                        "failure_signature": "known",
                        "time_window": "known",
                    },
                    "evidence_refs": ["EV-1"],
                }
            ],
        },
        parsed_json_sha256="b" * 64,
        schema_valid=True,
        schema_errors=(),
        created_at="2026-06-12T10:00:00Z",
    )

    routed = route_claims(bundle, [result])

    assert routed.claims[0].evidence_identity["program"] == "known"
    assert routed.claims[0].evidence_identity["source"] == "known"


def test_router_normalizes_model_subsystem_aliases() -> None:
    bundle = {
        "evidence_sha256": "a" * 64,
        "environment": "prod",
        "evidence_refs": {"EV-1": {}},
    }
    result = ParsedResultRecord(
        result_id="result-1",
        run_id="run-1",
        evidence_sha256="a" * 64,
        provider="test-provider",
        parsed_json={
            "schema_version": "claim-result/v1",
            "finding_status": "supported",
            "summary": "test",
            "claims": [
                {
                    "claim_type": "support",
                    "finding_status": "supported",
                    "claim_text": "Transport evidence needs review.",
                    "subsystem": "rtms_ffmpeg",
                    "evidence_refs": ["EV-1"],
                }
            ],
        },
        parsed_json_sha256="b" * 64,
        schema_valid=True,
        schema_errors=(),
        created_at="2026-06-12T10:00:00Z",
    )

    routed = route_claims(bundle, [result])

    assert routed.claims[0].subsystem == "rtmps_ffmpeg"


def _result(claim_text: str, evidence_refs: list[str]) -> ParsedResultRecord:
    return ParsedResultRecord(
        result_id="result-1",
        run_id="run-1",
        evidence_sha256="a" * 64,
        provider="test-provider",
        parsed_json={
            "schema_version": "claim-result/v1",
            "summary": "test",
            "claims": [
                {
                    "claim_type": "support",
                    "claim_text": claim_text,
                    "evidence_refs": evidence_refs,
                    "counter_evidence_refs": [],
                    "caveats": [],
                    "missing_evidence": [],
                    "temporary_action": "",
                    "permanent_action": "",
                    "required_authority": "",
                }
            ],
        },
        parsed_json_sha256="b" * 64,
        schema_valid=True,
        schema_errors=(),
        created_at="2026-06-12T10:00:00Z",
    )
