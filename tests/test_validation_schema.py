from __future__ import annotations

import pytest

from ops_evidence_synthesis.synthesis.validation import (
    _fallback_validate,
    evidence_ref_errors,
    parse_model_json,
    valid_evidence_refs,
    validate_claim_result,
)


@pytest.mark.parametrize(
    ("raw_output", "expected_payload", "expected_error"),
    [
        ('{"summary":"ok"}', {"summary": "ok"}, ""),
        ('["not", "an", "object"]', None, "top-level model output must be an object"),
        ('{"broken":', None, "invalid JSON: line 1, column 11"),
    ],
)
def test_parse_model_json_reports_object_and_syntax_boundaries(
    raw_output: str,
    expected_payload: dict | None,
    expected_error: str,
) -> None:
    payload, errors = parse_model_json(raw_output)

    assert payload == expected_payload
    if expected_error:
        assert errors and errors[0].startswith(expected_error)
    else:
        assert errors == ()


def test_claim_result_schema_accepts_insufficient_evidence_status() -> None:
    valid, errors = validate_claim_result(
        {
            "schema_version": "claim-result/v1",
            "agent_role": "hypothesis_generator",
            "finding_status": "insufficient_evidence",
            "summary": "The evidence is too ambiguous to create a review finding.",
            "claims": [
                {
                    "claim_type": "insufficient_evidence",
                    "finding_status": "insufficient_evidence",
                    "claim_text": "The program and exact failure signature are not identifiable from the evidence.",
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
            "propositions": [],
        }
    )

    assert valid is True
    assert errors == ()


def test_claim_result_schema_accepts_concrete_evidence_identity_values() -> None:
    valid, errors = validate_claim_result(
        {
            "schema_version": "claim-result/v1",
            "agent_role": "hypothesis_generator",
            "finding_status": "supported",
            "summary": "The evidence source is identifiable.",
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
            "propositions": [],
        }
    )

    assert valid is True
    assert errors == ()


def test_claim_result_schema_accepts_structured_identity_time_window() -> None:
    valid, errors = validate_claim_result(
        {
            "schema_version": "claim-result/v1",
            "agent_role": "hypothesis_generator",
            "finding_status": "supported",
            "summary": "The evidence window is identifiable.",
            "claims": [
                {
                    "claim_type": "support",
                    "finding_status": "supported",
                    "claim_text": "The runtime evidence is scoped to the inspected seven-day window.",
                    "subsystem": "runtime_recovery",
                    "evidence_identity": {
                        "program": "known",
                        "source": "known",
                        "failure_signature": "known",
                        "time_window": {
                            "start": "2026-06-19T19:44:54Z",
                            "end": "2026-06-26T19:44:54Z",
                            "timezone": "UTC",
                        },
                    },
                    "evidence_refs": ["PATTERN-001"],
                }
            ],
            "propositions": [],
        }
    )

    assert valid is True
    assert errors == ()


def test_claim_result_schema_accepts_model_subsystem_aliases() -> None:
    valid, errors = validate_claim_result(
        {
            "schema_version": "claim-result/v1",
            "agent_role": "hypothesis_generator",
            "finding_status": "supported",
            "summary": "The stream transport evidence is identifiable.",
            "claims": [
                {
                    "claim_type": "support",
                    "finding_status": "supported",
                    "claim_text": "The ffmpeg send path has evidence that needs review.",
                    "subsystem": "rtms_ffmpeg",
                    "evidence_refs": ["EV-1"],
                }
            ],
            "propositions": [],
        }
    )

    assert valid is True
    assert errors == ()


def test_claim_result_schema_reports_precise_nested_paths() -> None:
    valid, errors = validate_claim_result(
        {
            "schema_version": "claim-result/v1",
            "finding_status": "certain",
            "summary": "Invalid provider output.",
            "claims": [
                {
                    "claim_type": "root_cause",
                    "finding_status": "certain",
                    "claim_text": "",
                    "evidence_refs": [""],
                    "evidence_identity": {"time_window": 123},
                }
            ],
        }
    )

    assert valid is False
    rendered = "\n".join(errors)
    assert "$.finding_status" in rendered
    assert "$.claims.0.claim_type" in rendered
    assert "$.claims.0.finding_status" in rendered
    assert "$.claims.0.claim_text" in rendered
    assert "$.claims.0.evidence_refs.0" in rendered
    assert "$.claims.0.evidence_identity.time_window" in rendered


def test_fallback_validator_rejects_every_required_claim_boundary() -> None:
    valid, errors = _fallback_validate(
        {
            "schema_version": 1,
            "finding_status": "certain",
            "summary": None,
            "claims": [
                "not-an-object",
                {
                    "claim_type": "root_cause",
                    "finding_status": "certain",
                    "claim_text": "",
                    "evidence_refs": "EV-1",
                    "subsystem": 42,
                    "evidence_identity": "known",
                },
                {
                    "claim_type": "support",
                    "claim_text": "A claim with malformed identity fields.",
                    "evidence_refs": [],
                    "evidence_identity": {
                        "program": 1,
                        "source": [],
                        "failure_signature": {},
                        "time_window": {"start": 1, "end": [], "timezone": {}},
                    },
                },
            ],
        }
    )

    assert valid is False
    assert errors == (
        "$.schema_version: required string",
        "$.summary: required string",
        "$.finding_status: unsupported value",
        "$.claims.0: must be object",
        "$.claims.1.claim_type: unsupported value",
        "$.claims.1.claim_text: required string",
        "$.claims.1.evidence_refs: required array",
        "$.claims.1.subsystem: must be string",
        "$.claims.1.finding_status: unsupported value",
        "$.claims.1.evidence_identity: must be object",
        "$.claims.2.evidence_identity.program: must be string",
        "$.claims.2.evidence_identity.source: must be string",
        "$.claims.2.evidence_identity.failure_signature: must be string",
        "$.claims.2.evidence_identity.time_window: must be string or object",
    )


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"schema_version": "claim-result/v1", "summary": "missing claims"},
        {"schema_version": "claim-result/v1", "summary": "wrong claims", "claims": {}},
    ],
)
def test_fallback_validator_rejects_missing_top_level_contract(payload: dict) -> None:
    valid, errors = _fallback_validate(payload)

    assert valid is False
    assert errors


def test_fallback_validator_accepts_structured_identity_window() -> None:
    valid, errors = _fallback_validate(
        {
            "schema_version": "claim-result/v1",
            "finding_status": "supported",
            "summary": "Valid fallback payload.",
            "claims": [
                {
                    "claim_type": "support",
                    "finding_status": "supported",
                    "claim_text": "Evidence is available.",
                    "subsystem": "runtime_recovery",
                    "evidence_refs": ["EV-1"],
                    "evidence_identity": {
                        "program": "stream.sh",
                        "source": "systemd_journal",
                        "failure_signature": "timeout",
                        "time_window": {
                            "start": "2026-07-01T00:00:00Z",
                            "end": "2026-07-01T00:05:00Z",
                            "timezone": "UTC",
                        },
                    },
                }
            ],
        }
    )

    assert valid is True
    assert errors == ()


def test_evidence_reference_validation_reports_only_unknown_ids() -> None:
    bundle = {"evidence_refs": {"EV-1": {}, "EV-2": {}}}

    assert valid_evidence_refs(bundle, ["EV-1", "EV-2"]) is True
    assert valid_evidence_refs(bundle, ["EV-1", "EV-404"]) is False
    assert evidence_ref_errors(bundle, ["EV-404", "EV-1", "EV-500"]) == (
        "EV-404",
        "EV-500",
    )
