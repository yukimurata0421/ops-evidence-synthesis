from __future__ import annotations

from copy import deepcopy

from fastapi.testclient import TestClient

from ops_evidence_synthesis.evidence_request_planner import build_evidence_request_plan
from ops_evidence_synthesis.synthesis.review_arbitration import arbitrate_review_targets


def _bundle() -> dict[str, object]:
    return {
        "schema_version": "evidence_bundle.v1",
        "bundle_type": "sanitized_evidence_bundle",
        "raw_log_policy": "not_uploaded",
        "evidence_sha256": "sha",
        "evidence_refs": {
            "METRIC-1": {"evidence_id": "METRIC-1", "summary": "error count spike"},
            "LOG-1": {"evidence_id": "LOG-1", "summary": "safe log excerpt"},
        },
        "source": {"service": "stream_v3", "environment": "prod", "profile_confidence": "explicit"},
        "analysis_policy": {"explicit_profile": True, "allow_primary_candidate": True, "profile_mode": "explicit"},
        "signals": [{"signal_type": "http_5xx", "core_target_type": "network_error_signal", "component": "edge"}],
        "time_window": {"start": "2026-06-15T06:00:00Z", "end": "2026-06-15T10:00:00Z"},
    }


def _profile() -> dict[str, object]:
    return {
        "profile_id": "stream-v3-approved",
        "profile_discovery_approval": {"approved": True, "explicit_profile": True},
        "review_policy": {"profile_draft_approved": True},
        "collector_mappings": {},
    }


def _disagreement_synthesis() -> dict[str, object]:
    return {
        "schema_version": "multi_ai_synthesis.v1",
        "evidence_sha256": "sha",
        "provider_count": 3,
        "successful_provider_count": 3,
        "claim_groups": [
            {
                "group_id": "cg-a",
                "core_target_type": "general_review",
                "subsystem": "chromium_capture",
                "providers": ["gemini", "gpt-oss", "mistral"],
                "provider_count": 3,
                "evidence_refs": ["METRIC-1"],
            }
        ],
        "agreement_groups": [],
        "disagreement_groups": [
            {
                "group_id": "cg-a",
                "core_target_type": "general_review",
                "subsystem": "chromium_capture",
                "providers": ["gemini"],
                "provider_count": 1,
                "evidence_refs": ["METRIC-1"],
                "missing_evidence": ["critical outcome metric"],
            }
        ],
        "validation_targets": [
            {
                "group_id": "cg-a",
                "title": "Error spike needs review",
                "core_target_type": "general_review",
                "subsystem": "chromium_capture",
                "providers": ["gemini"],
                "provider_count": 1,
                "evidence_refs": ["METRIC-1"],
                "missing_evidence": ["critical outcome metric"],
            }
        ],
        "disagreement_themes": [
            {
                "theme": "Metric/log instrumentation mismatch",
                "group_count": 1,
                "recommended_validation": "instrumentation_consistency_query",
                "group_ids": ["cg-a"],
            }
        ],
        "missing_evidence_requests": [{"question": "user impact metric"}],
    }


def _technical_agreement_without_impact_synthesis() -> dict[str, object]:
    return {
        "schema_version": "multi_ai_synthesis.v1",
        "evidence_sha256": "sha",
        "provider_count": 3,
        "successful_provider_count": 3,
        "agreement_groups": [
            {
                "group_id": "cg-config",
                "title": "Configuration mismatch requires review: runtime_recovery",
                "core_target_type": "job_configuration_mismatch",
                "subsystem": "runtime_recovery",
                "providers": ["gemini", "gpt-oss", "mistral"],
                "provider_count": 3,
                "evidence_refs": ["PATTERN-001", "PATTERN-002", "PATTERN-003"],
                "impact_summary": "3 providers aligned on missing command and monitoring gap evidence.",
                "missing_evidence": ["current systemd unit metadata", "dependency latency metrics"],
            }
        ],
        "disagreement_groups": [
            {
                "group_id": "cg-config",
                "core_target_type": "job_configuration_mismatch",
                "subsystem": "runtime_recovery",
                "providers": ["mistral"],
                "provider_count": 1,
                "evidence_refs": ["PATTERN-003"],
                "missing_evidence": ["dependency latency metrics"],
            }
        ],
        "primary_candidates": [
            {
                "group_id": "cg-config",
                "title": "Configuration mismatch requires review: runtime_recovery",
                "core_target_type": "job_configuration_mismatch",
                "subsystem": "runtime_recovery",
                "providers": ["gemini", "gpt-oss", "mistral"],
                "provider_count": 3,
                "evidence_refs": ["PATTERN-001", "PATTERN-002", "PATTERN-003"],
                "review_priority_score": 0.75,
                "impact_summary": "3 providers aligned on missing command and monitoring gap evidence.",
                "missing_evidence": ["current systemd unit metadata", "dependency latency metrics"],
            }
        ],
        "disagreement_themes": [
            {
                "theme": "External dependency vs local instrumentation gap",
                "group_count": 1,
                "recommended_validation": "external_dependency_status_query",
                "group_ids": ["cg-config"],
            }
        ],
    }


def test_agreement_dimensions_separate_detection_overlap_from_baseline() -> None:
    graph = arbitrate_review_targets(_bundle(), multi_ai_synthesis=_disagreement_synthesis())
    dimensions = graph["agreement_dimensions"]
    assert dimensions["provider_detection_overlap"]["value"] == "3/3"
    assert dimensions["baseline_agreement"]["established"] is False
    assert dimensions["technical_baseline_agreement"]["established"] is False
    assert dimensions["incident_baseline_agreement"]["established"] is False
    assert dimensions["cause_agreement"]["value"] == "none"
    assert dimensions["impact_agreement"]["value"] == "none"


def test_technical_baseline_without_impact_is_visible_but_not_primary() -> None:
    graph = arbitrate_review_targets(_bundle(), multi_ai_synthesis=_technical_agreement_without_impact_synthesis())
    dimensions = graph["agreement_dimensions"]
    assert dimensions["provider_detection_overlap"]["value"] == "3/3"
    assert dimensions["technical_baseline_agreement"]["established"] is True
    assert dimensions["incident_baseline_agreement"]["established"] is False
    assert dimensions["baseline_agreement"]["established"] is False
    assert dimensions["impact_agreement"]["value"] == "none"
    assert graph["finding"]["title"] == "Technical support requires impact validation"
    assert "Providers aligned on technical support" in graph["finding"]["impact"]
    assert graph["summary"]["primary_count"] == 0
    assert graph["summary"]["validation_count"] == 1
    decision = graph["promotion_decisions"][0]
    assert decision["final_class"] == "validation_target"
    assert "user_impact_unverified" in decision["reasons"]


def test_approved_outcome_and_matching_http_5xx_evidence_verify_user_impact() -> None:
    bundle = deepcopy(_bundle())
    bundle["evidence_refs"]["PATTERN-500"] = {
        "evidence_id": "PATTERN-500",
        "event_type": "http_5xx",
        "message_template": "checkout failed HTTP 500",
    }
    bundle["evidence_items"] = [bundle["evidence_refs"]["PATTERN-500"]]
    synthesis = _technical_agreement_without_impact_synthesis()
    for section in ("agreement_groups", "disagreement_groups", "primary_candidates"):
        for row in synthesis.get(section) or []:
            row["evidence_refs"] = ["PATTERN-500"]
            row["title"] = "Database pool saturation requires review"
            row["impact_summary"] = "Database pool saturation is the suspected mechanism."
    approved_profile = {
        "confirmed_user_outcomes": ["Checkout HTTP 500 responses are direct user impact."],
    }

    graph = arbitrate_review_targets(
        bundle,
        multi_ai_synthesis=synthesis,
        approved_profile=approved_profile,
    )

    decision = graph["promotion_decisions"][0]
    target = graph["review_targets"][0]
    assert target["has_user_impact_evidence"] is True
    assert "user_impact_unverified" not in decision["reasons"]
    assert "impact_disagreement" not in decision["reasons"]


def test_review_unit_convergence_increases_priority_without_primary_promotion() -> None:
    synthesis = {
        "schema_version": "multi_ai_synthesis.v1",
        "evidence_sha256": "sha",
        "provider_count": 3,
        "successful_provider_count": 3,
        "agreement_groups": [],
        "disagreement_groups": [],
        "validation_targets": [
            {
                "group_id": "cg-runtime-a",
                "title": "Restart loop requires validation: runtime_recovery",
                "core_target_type": "restart_loop",
                "subsystem": "runtime_recovery",
                "providers": ["gemini"],
                "provider_count": 1,
                "evidence_refs": ["METRIC-1"],
                "review_priority_score": 0.62,
                "missing_evidence": ["critical outcome metric"],
            },
            {
                "group_id": "cg-runtime-b",
                "title": "Review target requires validation: runtime_recovery",
                "core_target_type": "general_review",
                "subsystem": "runtime_recovery",
                "providers": ["mistral"],
                "provider_count": 1,
                "evidence_refs": ["LOG-1"],
                "review_priority_score": 0.62,
                "missing_evidence": ["critical outcome metric"],
            },
            {
                "group_id": "cg-runtime-c",
                "title": "Runtime recovery process state needs review",
                "core_target_type": "process_state_query",
                "subsystem": "runtime_recovery",
                "providers": ["gpt-oss"],
                "provider_count": 1,
                "evidence_refs": ["METRIC-2"],
                "review_priority_score": 0.62,
                "missing_evidence": ["critical outcome metric"],
            },
        ],
    }

    graph = arbitrate_review_targets(_bundle(), multi_ai_synthesis=synthesis)

    assert graph["summary"]["primary_count"] == 0
    assert graph["summary"]["validation_count"] == 1
    assert graph["agreement_dimensions"]["technical_baseline_agreement"]["established"] is True
    assert graph["agreement_dimensions"]["incident_baseline_agreement"]["established"] is False
    assert graph["agreement_dimensions"]["review_unit_convergence"]["value"] == "strong"
    target = graph["validation_targets"][0]
    assert target["canonical_review_unit"] == "runtime_recovery"
    assert target["source_candidate_count"] == 3
    assert target["rollup"]["independent_provider_count"] == 3
    assert target["score_breakdown"]["convergence_bonus"] > 0.10
    assert target["review_priority_score"] > target["promotion_score"]
    assert "user_impact_unverified" in target["promotion_blocked_reasons"]
    priority = graph["planner_inputs"]["validation_target_priorities"][0]
    assert priority["canonical_review_unit"] == "runtime_recovery"
    assert priority["baseline_support_score"] >= 0.65


def test_no_finding_validation_target_routes_to_monitor_only() -> None:
    synthesis = {
        "schema_version": "multi_ai_synthesis.v1",
        "evidence_sha256": "sha",
        "provider_count": 1,
        "successful_provider_count": 1,
        "validation_targets": [
            {
                "group_id": "cg-user-experience",
                "title": "User experience observation",
                "core_target_type": "user_outcome_check",
                "subsystem": "user_experience",
                "providers": ["gemini"],
                "provider_count": 1,
                "evidence_refs": ["LOG-1"],
                "target_explanation": {
                    "suspected_issue": "None identified",
                    "why_it_matters": "Confirms the service is likely healthy and not impacting notification delivery.",
                    "why_not_promoted": "The evidence indicates normal operation rather than an incident.",
                    "provider_explanations": [
                        {"provider_id": "gemini", "claim_type": "insufficient_evidence"}
                    ],
                },
            },
            {
                "group_id": "cg-traffic",
                "title": "Traffic anomaly requires validation",
                "core_target_type": "traffic_anomaly",
                "subsystem": "traffic",
                "providers": ["gemini"],
                "provider_count": 1,
                "evidence_refs": ["METRIC-1"],
                "target_explanation": {
                    "suspected_issue": "Potential traffic anomaly or instrumentation change.",
                    "why_not_promoted": "The metric increase is not correlated with error logs or service failures.",
                },
            },
        ],
    }

    graph = arbitrate_review_targets(_bundle(), multi_ai_synthesis=synthesis)

    assert graph["summary"]["primary_count"] == 0
    assert graph["summary"]["validation_count"] == 1
    assert graph["summary"]["monitor_only_count"] == 1
    assert graph["validation_targets"][0]["canonical_review_unit"] == "traffic"
    monitor = graph["monitor_only"][0]
    assert monitor["canonical_review_unit"] == "user_experience"
    assert monitor["class"] == "monitor_only"
    assert monitor["review_priority_score"] <= 0.35
    decision = next(row for row in graph["promotion_decisions"] if row["target_id"] == monitor["target_id"])
    assert decision["final_class"] == "monitor_only"
    assert "normal_operation_observation" in decision["reasons"]
    assert graph["planner_inputs"]["validation_target_ids"] == [graph["validation_targets"][0]["target_id"]]


def test_structural_caveat_routes_to_monitor_only() -> None:
    synthesis = {
        "schema_version": "multi_ai_synthesis.v1",
        "evidence_sha256": "sha",
        "provider_count": 2,
        "successful_provider_count": 2,
        "validation_targets": [
            {
                "group_id": "cg-version-anchor",
                "title": "Review target requires validation: user_experience",
                "core_target_type": "user_outcome_check",
                "subsystem": "user_experience",
                "providers": ["gemini", "gemma"],
                "provider_count": 2,
                "evidence_refs": ["PATTERN-1", "PATTERN-2"],
                "target_explanation": {
                    "suspected_issue": "Potential mismatch between source code context and runtime environment.",
                    "operational_mechanism": (
                        "The source context provides templates, but there is no deployment evidence to "
                        "confirm these specific versions were active during the incident window."
                    ),
                    "why_it_matters": (
                        "Hypotheses based on the provided source code may be invalid if the running code differs."
                    ),
                    "why_not_promoted": "This is a structural caveat rather than a functional finding.",
                    "provider_explanations": [
                        {
                            "provider_id": "gemma",
                            "claim_type": "caveat",
                            "suspected_issue": "Potential mismatch between source code context and deployed runtime.",
                            "why_not_promoted": (
                                "This is a structural limitation of the evidence bundle, not a specific incident finding."
                            ),
                        },
                        {
                            "provider_id": "gemini",
                            "claim_type": "insufficient_evidence",
                            "suspected_issue": "None identified",
                            "why_not_promoted": "The evidence indicates normal operation rather than an incident.",
                        },
                    ],
                },
            }
        ],
    }

    graph = arbitrate_review_targets(_bundle(), multi_ai_synthesis=synthesis)

    assert graph["summary"]["primary_count"] == 0
    assert graph["summary"]["validation_count"] == 0
    assert graph["summary"]["monitor_only_count"] == 1
    monitor = graph["monitor_only"][0]
    assert monitor["canonical_review_unit"] == "user_experience"
    assert monitor["class"] == "monitor_only"
    decision = next(row for row in graph["promotion_decisions"] if row["target_id"] == monitor["target_id"])
    assert decision["final_class"] == "monitor_only"
    assert "non_incident_structural_caveat" in decision["reasons"]
    assert graph["planner_inputs"]["validation_target_ids"] == []


def test_same_provider_duplicates_have_limited_convergence_bonus() -> None:
    multi_provider = {
        "schema_version": "multi_ai_synthesis.v1",
        "evidence_sha256": "sha",
        "provider_count": 2,
        "successful_provider_count": 2,
        "validation_targets": [
            {
                "group_id": "cg-a",
                "title": "Restart loop requires validation: runtime_recovery",
                "core_target_type": "restart_loop",
                "subsystem": "runtime_recovery",
                "providers": ["gemini"],
                "provider_count": 1,
                "evidence_refs": ["METRIC-1"],
                "review_priority_score": 0.62,
            },
            {
                "group_id": "cg-b",
                "title": "Runtime recovery needs validation",
                "core_target_type": "general_review",
                "subsystem": "runtime_recovery",
                "providers": ["mistral"],
                "provider_count": 1,
                "evidence_refs": ["LOG-1"],
                "review_priority_score": 0.62,
            },
        ],
    }
    same_provider = {
        **multi_provider,
        "provider_count": 1,
        "successful_provider_count": 1,
        "validation_targets": [
            {**multi_provider["validation_targets"][0], "providers": ["gemini"]},
            {**multi_provider["validation_targets"][1], "providers": ["gemini"]},
        ],
    }

    multi_graph = arbitrate_review_targets(_bundle(), multi_ai_synthesis=multi_provider)
    same_graph = arbitrate_review_targets(_bundle(), multi_ai_synthesis=same_provider)

    multi_bonus = multi_graph["validation_targets"][0]["score_breakdown"]["convergence_bonus"]
    same_bonus = same_graph["validation_targets"][0]["score_breakdown"]["convergence_bonus"]
    assert multi_bonus > same_bonus
    assert same_graph["agreement_dimensions"]["technical_baseline_agreement"]["established"] is False
    assert same_graph["agreement_dimensions"]["review_unit_convergence"]["value"] == "partial"


def test_no_baseline_and_no_cause_alignment_blocks_primary_promotion() -> None:
    graph = arbitrate_review_targets(
        _bundle(),
        multi_ai_synthesis=_disagreement_synthesis(),
        legacy_review_targets=[
            {
                "review_target_id": "rt-error-spike",
                "title": "Error spike needs review",
                "review_priority_score": 0.87,
                "drawer": {"support_evidence": [{"evidence_id": "METRIC-1"}]},
                "model_agreement": {"detected_provider_count": 3, "total_provider_count": 3},
            }
        ],
        legacy_summary={"primary_review_targets": 1},
    )
    assert graph["summary"]["primary_count"] == 0
    assert graph["summary"]["validation_count"] >= 1
    decision = [
        row
        for row in graph["promotion_decisions"]
        if "rt-error-spike" in set(row.get("source_target_ids") or [row.get("target_id")])
    ][0]
    assert decision["decision"] == "downgraded"
    assert decision["final_class"] == "validation_target"
    assert "no_baseline_agreement_or_causal_alignment" in decision["reasons"]


def test_single_metric_without_user_impact_caps_score() -> None:
    graph = arbitrate_review_targets(
        _bundle(),
        multi_ai_synthesis={"provider_count": 1, "successful_provider_count": 1, "agreement_groups": [], "disagreement_groups": []},
        legacy_review_targets=[
            {
                "review_target_id": "rt-metric-only",
                "title": "Metric spike needs review",
                "review_priority_score": 0.91,
                "drawer": {"support_evidence": [{"evidence_id": "METRIC-1"}]},
            }
        ],
        legacy_summary={"primary_review_targets": 1},
    )
    decision = graph["promotion_decisions"][0]
    assert decision["score_after"] <= 0.70
    assert any(row["reason"] == "single_metric_without_user_impact" for row in decision["score_caps_applied"])
    assert "single_metric_only" in decision["reasons"]


def test_severity_only_signal_is_not_primary() -> None:
    graph = arbitrate_review_targets(
        _bundle(),
        multi_ai_synthesis={},
        legacy_review_targets=[
            {
                "review_target_id": "rt-info",
                "title": "info",
                "core_target_type": "info",
                "review_priority_score": 0.9,
                "drawer": {"support_evidence": [{"evidence_id": "LOG-1"}]},
            }
        ],
        legacy_summary={"primary_review_targets": 1},
    )
    decision = graph["promotion_decisions"][0]
    assert decision["final_class"] == "validation_target"
    assert "severity_only_signal" in decision["reasons"]
    assert decision["score_after"] <= 0.45


def test_source_and_human_context_only_are_not_incident_support() -> None:
    graph = arbitrate_review_targets(
        _bundle(),
        source_context={"source_context_sha256": "source-sha"},
        planner_answers={"answers": {"operator_display_timezone": "JST"}},
    )
    assert graph["summary"]["primary_count"] == 0
    roles = {row["support_role"] for row in graph["monitor_only"]}
    assert "source_context" in roles
    assert "human_context" in roles
    assert all(row["class"] == "monitor_only" for row in graph["monitor_only"])


def test_support_claim_without_evidence_id_is_auto_archived() -> None:
    graph = arbitrate_review_targets(
        _bundle(),
        multi_ai_synthesis={
            "auto_archived": [
                {"group_id": "cg-unsupported", "reason": "unsupported_support_without_evidence_id", "providers": ["gemini"]}
            ]
        },
    )
    assert graph["summary"]["auto_archived_count"] == 1
    assert graph["auto_archived"][0]["support_role"] == "model_interpretation"


def test_finding_impact_come_from_canonical_graph_for_disagreement() -> None:
    graph = arbitrate_review_targets(_bundle(), multi_ai_synthesis=_disagreement_synthesis())
    assert graph["finding"]["title"] == "Multi-AI disagreement requires validation"
    assert "No incident-promotion agreement was found" in graph["finding"]["impact"]


def test_planner_uses_canonical_review_graph_request_types() -> None:
    graph = arbitrate_review_targets(_bundle(), multi_ai_synthesis=_disagreement_synthesis())
    plan = build_evidence_request_plan(_bundle(), _profile(), canonical_review_graph=graph)
    assert plan["canonical_review_graph_used"] is True
    request_types = [row["generic_request_type"] for row in plan["requests"]]
    assert "instrumentation_consistency_query" in request_types


def test_multi_run_api_response_includes_canonical_review_graph(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("OES_STORE", "sqlite")
    monkeypatch.setenv("OES_DB_PATH", str(tmp_path / "api.sqlite3"))
    from ops_evidence_synthesis.api import app

    with TestClient(app) as client:
        api_bundle = dict(_bundle())
        api_bundle.pop("bundle_type", None)
        api_bundle.pop("schema_version", None)
        response = client.post(
            "/ai/multi-run",
            json={
                "evidence_bundle": api_bundle,
                "approved_profile": _profile(),
                "providers": ["local-gemini", "local-gpt-oss", "local-mistral"],
                "mode": "local",
            },
        )
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["canonical_review_graph"]["schema_version"] == "canonical_review_graph.v1"
        assert "agreement_dimensions" in payload["canonical_review_graph"]


def test_review_arbitrate_api_accepts_payload(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("OES_STORE", "sqlite")
    monkeypatch.setenv("OES_DB_PATH", str(tmp_path / "api.sqlite3"))
    from ops_evidence_synthesis.api import app

    with TestClient(app) as client:
        api_bundle = dict(_bundle())
        api_bundle.pop("bundle_type", None)
        api_bundle.pop("schema_version", None)
        response = client.post(
            "/review/arbitrate",
            json={"evidence_bundle": api_bundle, "multi_ai_synthesis": _disagreement_synthesis()},
        )
        assert response.status_code == 200, response.text
        graph = response.json()["canonical_review_graph"]
        assert graph["summary"]["primary_count"] == 0
        assert graph["summary"]["validation_count"] >= 1
