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
