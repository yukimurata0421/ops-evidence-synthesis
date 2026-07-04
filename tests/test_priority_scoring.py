from __future__ import annotations

from ops_evidence_synthesis.synthesis.priority_scoring import score_review_priority


def test_gemini_claim_has_more_weight_than_non_gemini_claims() -> None:
    common = {
        "prior_score": 0.62,
        "evidence_ref_count": 12,
        "evidence_family_count": 2,
        "source_candidate_count": 3,
        "target_class": "validation_target",
        "canonical_review_unit": "runtime_recovery",
        "title": "Runtime recovery needs validation",
        "suspected_issue": "Restart loop may be affecting delivery",
        "operational_mechanism": "watchdog restart and runtime recovery",
        "why_it_matters": "stream delivery can be affected",
        "missing_evidence": ["user impact evidence"],
        "blocked_reasons": ["user_impact_unverified"],
    }

    with_gemini = score_review_priority(
        **common,
        provider_positions=[
            {"provider_id": "gemini-enterprise-agent-platform", "stance": "claimed"},
            {"provider_id": "glm-agent-platform", "stance": "claimed"},
            {"provider_id": "mistral-agent-platform", "stance": "silent"},
            {"provider_id": "openai-gpt-oss-on-vertex", "stance": "claimed"},
            {"provider_id": "qwen-agent-platform", "stance": "silent"},
        ],
    )
    without_gemini = score_review_priority(
        **common,
        provider_positions=[
            {"provider_id": "gemini-enterprise-agent-platform", "stance": "silent"},
            {"provider_id": "glm-agent-platform", "stance": "claimed"},
            {"provider_id": "mistral-agent-platform", "stance": "claimed"},
            {"provider_id": "openai-gpt-oss-on-vertex", "stance": "claimed"},
            {"provider_id": "qwen-agent-platform", "stance": "silent"},
        ],
    )

    assert with_gemini["score"] > without_gemini["score"]
    assert with_gemini["breakdown"]["gemini_claimed"] is True
    assert without_gemini["breakdown"]["gemini_claimed"] is False
    assert without_gemini["breakdown"]["penalties"]["gemini_silent_penalty"] > 0


def test_actionable_runtime_target_scores_above_healthy_status_target() -> None:
    provider_positions = [
        {"provider_id": "gemini-enterprise-agent-platform", "stance": "claimed"},
        {"provider_id": "glm-agent-platform", "stance": "claimed"},
        {"provider_id": "mistral-agent-platform", "stance": "claimed"},
        {"provider_id": "openai-gpt-oss-on-vertex", "stance": "claimed"},
        {"provider_id": "qwen-agent-platform", "stance": "claimed"},
    ]

    actionable = score_review_priority(
        prior_score=0.86,
        provider_positions=provider_positions,
        evidence_ref_count=20,
        evidence_family_count=2,
        source_candidate_count=4,
        target_class="primary_candidate",
        canonical_review_unit="runtime_recovery",
        title="Watchdog restart loop affects stream delivery",
        suspected_issue="Restart loop may be affecting user-visible stream delivery",
        operational_mechanism="watchdog restart and runtime recovery",
        why_it_matters="stream delivery can be interrupted",
        missing_evidence=[],
        blocked_reasons=[],
    )
    healthy = score_review_priority(
        prior_score=0.86,
        provider_positions=provider_positions,
        evidence_ref_count=80,
        evidence_family_count=1,
        source_candidate_count=10,
        target_class="validation_target",
        canonical_review_unit="service_liveness",
        title="Service is healthy and idle",
        suspected_issue="None, service is healthy and idle.",
        operational_mechanism="successful runs",
        why_it_matters="baseline health confirmation",
        missing_evidence=["user impact evidence"],
        blocked_reasons=["user_impact_unverified"],
    )

    assert actionable["score"] > healthy["score"]
    assert healthy["score"] <= 0.73
    assert healthy["breakdown"]["penalties"]["healthy_status_penalty"] > 0


def test_priority_score_varies_by_support_evidence_and_blockers_instead_of_sticking_to_prior() -> None:
    provider_positions = [
        {"provider_id": "gemini-enterprise-agent-platform", "stance": "claimed"},
        {"provider_id": "gemma-agent-platform", "stance": "claimed"},
        {"provider_id": "mistral-agent-platform", "stance": "claimed"},
        {"provider_id": "openai-gpt-oss-on-vertex", "stance": "claimed"},
        {"provider_id": "qwen-agent-platform", "stance": "claimed"},
    ]
    without_gemini = [
        {**row, "stance": "silent" if "gemini" in row["provider_id"] else "claimed"}
        for row in provider_positions
    ]
    common = {
        "prior_score": 0.86,
        "canonical_review_unit": "runtime_recovery",
        "title": "Restart loop affects notification delivery",
        "suspected_issue": "Restart failures can block notification delivery.",
        "operational_mechanism": "watchdog restart loop and delivery failure",
        "why_it_matters": "customer notification delivery impact",
        "target_class": "primary_candidate",
    }

    dense = score_review_priority(
        **common,
        provider_positions=provider_positions,
        evidence_ref_count=30,
        evidence_family_count=3,
        source_candidate_count=5,
        missing_evidence=[],
        blocked_reasons=[],
    )
    no_gemini = score_review_priority(
        **common,
        provider_positions=without_gemini,
        evidence_ref_count=30,
        evidence_family_count=3,
        source_candidate_count=5,
        missing_evidence=[],
        blocked_reasons=[],
    )
    missing_impact = score_review_priority(
        **common,
        provider_positions=provider_positions,
        evidence_ref_count=2,
        evidence_family_count=1,
        source_candidate_count=1,
        missing_evidence=["user impact evidence", "metric time series"],
        blocked_reasons=["user_impact_unverified"],
    )
    healthy_status = score_review_priority(
        **{
            **common,
            "canonical_review_unit": "service_liveness",
            "title": "Service is healthy and idle",
            "suspected_issue": "None, service is healthy and idle.",
            "operational_mechanism": "successful runs",
            "why_it_matters": "baseline health confirmation",
            "target_class": "validation_target",
        },
        provider_positions=provider_positions,
        evidence_ref_count=30,
        evidence_family_count=3,
        source_candidate_count=5,
        missing_evidence=[],
        blocked_reasons=[],
    )

    scores = {dense["score"], no_gemini["score"], missing_impact["score"], healthy_status["score"]}
    assert len(scores) == 4
    assert 0.86 not in scores
    assert dense["score"] > missing_impact["score"] > healthy_status["score"]
    assert dense["score"] > no_gemini["score"]
    assert no_gemini["breakdown"]["penalties"]["gemini_silent_penalty"] > 0
    assert missing_impact["breakdown"]["penalties"]["missing_evidence_penalty"] > 0
    assert missing_impact["breakdown"]["penalties"]["blocker_penalty"] > 0
