from __future__ import annotations

from ops_evidence_synthesis.profile_gate import (
    build_approved_profile_model_context,
    build_focused_profile_context_summary,
    build_profile_context_summary,
    profile_confidence_action,
)


def test_profile_context_summary_keeps_profile_as_human_gated_context() -> None:
    profile_draft = {
        "schema_version": "profile_draft.v1",
        "profile_generation": {
            "llm_status": "ok",
            "source_discovery_sha256": "d" * 64,
        },
        "profile": {
            "system_type": "notification_workflow",
            "purpose": "Deliver notifications from background jobs.",
            "components": [{"name": "worker"}],
            "critical_user_outcomes": ["Notifications are delivered"],
            "metric_semantics": {
                "notification_delivered_total": {
                    "zero_behavior": "bad_when_expected_traffic_exists",
                    "confidence": 0.78,
                }
            },
        },
        "required_profile_questions": [
            "Which metrics are zero-is-good or zero-is-bad?",
            "Which logs indicate user impact rather than diagnostic noise?",
        ],
        "assumptions": [
            "Customer notification delivery is a critical user outcome.",
            "Critical user outcomes are plausible but not proven.",
            "Human review is required before promotion.",
        ],
    }
    approved_profile = {
        "profile_id": "generic_notification_profile",
        "profile_discovery_approval": {"approved": True, "explicit_profile": True},
        "system_profile": {
            "system_type": "notification_workflow",
            "purpose": "Deliver notifications.",
            "provisional_user_outcomes": ["Background notifications complete successfully"],
        },
        "component_map": {
            "service_health": {"role": "health signal", "confidence": 0.8},
            "background_processing": {"role": "worker loop", "confidence": 0.7},
        },
        "metric_semantics": {
            "processed_total": {"zero_behavior": "bad_when_expected", "confidence": 0.78},
        },
        "collector_mappings": {"systemd_journal": {"confidence": 0.7}},
    }
    review_targets = [
        {
            "canonical_review_unit": "service_health",
            "target_explanation": {"evidence_summary": ["processed_total=0 for 30 minutes"]},
            "promotion": {"blocked_reason": "user_impact_unverified"},
            "missing_evidence": ["User impact or operational outcome evidence tied to this review unit."],
        },
        {
            "canonical_review_unit": "runtime_recovery",
            "promotion": {"blocked_reason": "user_impact_unverified"},
            "missing_evidence": ["User impact or operational outcome evidence tied to this review unit."],
        },
    ]

    summary = build_profile_context_summary(
        profile_id="",
        profile_draft=profile_draft,
        approved_profile=approved_profile,
        source_context_sha="s" * 64,
        source_analysis_sha="a" * 64,
        review_targets=review_targets,
    )

    assert summary["schema_version"] == "profile_context_summary.v2"
    assert summary["profile_id"] == "generic_notification_profile"
    assert summary["profile_status"] == "approved_context_human_gated_outcomes"
    assert summary["context_is_not_incident_evidence"] is True
    assert summary["profile_review_policy"] == {
        "context_is_not_incident_evidence": True,
        "confirmed_outcomes_required_for_promotion": True,
        "provisional_outcomes_create_missing_evidence": True,
        "low_confidence_fields_require_human_review": True,
        "runtime_support_must_cite_evidence_id": True,
    }
    assert summary["confidence_summary"]["overall_confidence"] == 0.765
    assert summary["confidence_action"] == "use_for_subsystem_routing_human_gated"
    assert summary["confirmed_user_outcomes"] == []
    assert "Background notifications complete successfully" in summary["provisional_user_outcomes"]
    assert "Customer notification delivery" in " ".join(summary["provisional_user_outcomes"])
    assert "Critical user outcomes are plausible but not proven" not in summary["provisional_user_outcomes"]
    assert "Which metrics are zero-is-good or zero-is-bad?" in summary["human_questions"]

    zero_link = next(
        row
        for row in summary["profile_to_review_links"]
        if row["question"] == "Which metrics are zero-is-good or zero-is-bad?"
    )
    assert zero_link["review_units"] == ["service_health"]
    impact_link = next(
        row
        for row in summary["profile_to_review_links"]
        if row["question"] == "Which logs indicate user impact rather than diagnostic noise?"
    )
    assert set(impact_link["review_units"]) == {"service_health", "runtime_recovery"}


def test_profile_context_summary_without_context_still_exports_gate_policy() -> None:
    summary = build_profile_context_summary(
        profile_id="",
        profile_draft={},
        approved_profile={},
        review_targets=[],
    )

    assert summary["schema_version"] == "profile_context_summary.v2"
    assert summary["profile_status"] == "not_run"
    assert summary["approved"] is False
    assert summary["explicit_profile"] is False
    assert summary["confidence_action"] == "not_available"
    assert summary["profile_review_policy"]["runtime_support_must_cite_evidence_id"] is True
    assert summary.get("profile_to_review_links", []) == []


def test_focused_profile_context_summary_uses_existing_profile_output() -> None:
    focused_profile = {
        "schema_version": "focused_operational_profile.v1",
        "system_label": "stream_v3_arena_monitoring",
        "source_context_sha256": "c" * 64,
        "source_analysis_sha256": "a" * 64,
        "source_discovery_sha256": "d" * 64,
        "focused_profile_generation": {
            "llm_status": "ok",
            "generation_mode": "gemini_focused_operational_profile",
            "model_name": "gemini-3.1-pro-preview",
        },
        "system_summary": {
            "system_type": "systemd_service",
            "primary_purpose": "Observe stream health and coordinate guarded recovery requests.",
            "logged_subject": "stream health and recovery request logs",
            "operational_boundary": "monitoring plane; runtime owner remains separate",
            "confidence": 0.8,
        },
        "runtime_components": [
            {
                "component_id": "stream_v3_arena_monitor",
                "name": "stream-v3-arena-monitor.service",
                "role": "monitoring control loop",
                "confidence": 0.9,
            }
        ],
        "observability_contract": {
            "metrics": [
                {
                    "metric_name": "stream_v3_youtube_warn_count",
                    "meaning": "YouTube warning count",
                    "healthy_direction": "zero",
                    "confidence": 0.8,
                }
            ],
            "logs": [
                {
                    "source": "systemd_journal",
                    "meaning": "service status logs",
                }
            ],
        },
        "read_only_collectors": [
            {
                "collector": "journalctl",
                "purpose": "read service logs",
                "safety_level": "read_only",
            }
        ],
        "human_review_required": [
            "What is the critical user outcome?",
            "Which metrics are zero-is-good or zero-is-bad?",
        ],
    }
    summary = build_focused_profile_context_summary(
        profile_id="stream_v3_arena_monitoring_focused_approved",
        focused_profile=focused_profile,
        review_targets=[
            {
                "canonical_review_unit": "youtube_health",
                "target_explanation": {"evidence_summary": ["stream_v3_youtube_warn_count=0"]},
                "promotion": {"blocked_reason": "user_impact_unverified"},
                "missing_evidence": ["User impact or operational outcome evidence tied to this review unit."],
            }
        ],
    )

    assert summary["profile_id"] == "stream_v3_arena_monitoring_focused_approved"
    assert summary["profile_status"] == "approved_context_human_gated_outcomes"
    assert summary["generation_mode"] == "profile_draft_and_approved_profile"
    assert summary["llm_status"] == "ok"
    assert summary["draft_schema_version"] == "focused_operational_profile.v1"
    assert summary["source_context_sha256"] == "c" * 64
    assert summary["source_analysis_sha256"] == "a" * 64
    assert summary["source_discovery_sha256"] == "d" * 64
    assert summary["system_type"] == "systemd_service"
    assert summary["component_count"] == 1
    assert summary["metric_semantics_count"] == 1
    assert summary["collector_mapping_count"] == 2
    assert summary["confidence_action"] == "use_for_subsystem_routing_human_gated"
    zero_link = next(
        row
        for row in summary["profile_to_review_links"]
        if row["question"] == "Which metrics are zero-is-good or zero-is-bad?"
    )
    assert zero_link["review_units"] == ["youtube_health"]


def test_profile_confidence_action_thresholds() -> None:
    assert profile_confidence_action(None) == "not_available"
    assert profile_confidence_action(0.75) == "use_for_subsystem_routing_human_gated"
    assert profile_confidence_action(0.749) == "candidate_only_requires_profile_review"
    assert profile_confidence_action(0.6) == "candidate_only_requires_profile_review"
    assert profile_confidence_action(0.599) == "discovery_required_before_routing"


def test_approved_profile_model_context_is_bounded_and_non_evidence() -> None:
    profile = {
        "profile_id": "large_profile",
        "system_profile": {"system_type": "stream_processor"},
        "component_map": {f"component_{index}": {"confidence": 0.8} for index in range(100)},
        "metric_semantics": {f"metric_{index}": {"confidence": 0.7} for index in range(95)},
        "collector_mappings": {"journal": {"confidence": 0.7}},
        "action_constraints": ["Do not auto-remediate."],
        "provisional_user_outcomes": ["Stream stays healthy"],
        "human_questions": ["Confirm critical user outcomes."],
    }

    context = build_approved_profile_model_context(profile)

    assert context["profile_id"] == "large_profile"
    assert context["explicit_profile"] is True
    assert context["context_is_not_evidence"] is True
    assert context["require_evidence_id_for_support"] is True
    assert context["profile_review_policy"]["context_is_not_incident_evidence"] is True
    assert len(context["component_map"]) == 80
    assert len(context["metric_semantics"]) == 80
    assert context["provisional_user_outcomes"] == ["Stream stays healthy"]
    assert "Confirm critical user outcomes." in context["human_questions"]


def test_empty_approved_profile_model_context_is_explicitly_not_run() -> None:
    context = build_approved_profile_model_context({})

    assert context == {
        "explicit_profile": False,
        "context_is_not_evidence": True,
        "profile_status": "not_run",
        "confidence_action": "not_available",
        "profile_review_policy": {
            "context_is_not_incident_evidence": True,
            "runtime_support_must_cite_evidence_id": True,
        },
    }


def test_final_human_approved_profile_does_not_reopen_resolved_questions() -> None:
    profile = {
        "schema_version": "approved_operational_profile.v1",
        "status": "approved",
        "explicit_profile": True,
        "profile_id": "payment-api",
        "human_review": {"decision": "approved", "reviewer": "operator"},
        "confirmed_user_outcomes": ["Checkout HTTP 500 responses are direct user impact."],
        "human_questions": [],
        "required_profile_questions": [],
        "metric_semantics": {
            "checkout_500_count": {
                "zero_behavior": "healthy",
                "increase_behavior": "suspicious",
                "confidence": 0.7,
            }
        },
    }

    summary = build_profile_context_summary(
        profile_id="payment-api",
        profile_draft={},
        approved_profile=profile,
    )

    assert summary["human_questions"] == []
    assert summary["confirmed_user_outcomes"] == ["Checkout HTTP 500 responses are direct user impact."]
