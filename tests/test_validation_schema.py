from __future__ import annotations

from ops_evidence_synthesis.synthesis.validation import validate_claim_result


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
