from __future__ import annotations

from ops_evidence_synthesis.synthesis.target_classification import target_reads_as_normal_observation


def test_health_absence_projection_is_auditable_no_finding() -> None:
    target = {
        "suspected_issue": "Despite network anomalies, the stream may have remained healthy.",
        "operational_mechanism": "Absence of unhealthy state logs suggests no confirmed outage.",
    }

    assert target_reads_as_normal_observation(target) is True


def test_negative_finding_can_override_problem_framed_title() -> None:
    target = {
        "suspected_issue": "Memory-induced instability",
        "operational_mechanism": "Memory status was ok during the window.",
        "target_explanation": {
            "why_it_matters": "This negative finding rules out memory pressure.",
            "provider_explanations": [
                {"claim_type": "caveat", "claim_text": "Memory pressure was not observed."},
                {"claim_type": "insufficient_evidence", "claim_text": "No failure evidence."},
            ],
        },
    }

    assert target_reads_as_normal_observation(target) is True


def test_supported_problem_is_not_hidden_by_impact_caveat() -> None:
    target = {
        "suspected_issue": "Potential external dependency timeout failure",
        "why_not_promoted": "No confirmed outage or user impact yet.",
        "target_explanation": {
            "provider_explanations": [
                {"claim_type": "support", "claim_text": "Timeout errors were observed."},
            ],
        },
    }

    assert target_reads_as_normal_observation(target) is False
