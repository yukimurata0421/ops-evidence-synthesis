from __future__ import annotations

from ops_evidence_synthesis.web.precomputed_review import (
    _precomputed_review_graph_response,
    _render_precomputed_review_detail_page,
)


def _payload() -> dict:
    return {
        "evidence_sha256": "e" * 64,
        "updated_at": "2026-06-30T00:00:00Z",
        "summary": {
            "status": "ok",
            "finding": {"title": "Graph contract review", "impact": "Human review remains required."},
            "review": {"primary_targets": 0, "validation_targets": 1, "monitor_only": 0, "auto_archived": 0},
            "providers": {"success": 2, "total": 2, "pipeline_status": "succeeded"},
            "baselines": {"technical": True, "incident": True},
            "raw_log_policy": "not_uploaded",
            "log_count": 42,
            "canonical_graph_status": "persisted",
            "canonical_graph_sha256": "g" * 64,
            "input_fingerprint_sha256": "i" * 64,
        },
        "review_graph_summary": {
            "targets_total": 1,
            "primary_promoted_count": 0,
            "convergence_count": 1,
            "single_source_count": 0,
            "rule_or_context_count": 0,
            "partial_overlap_count": 0,
            "conflict_count": 0,
            "auto_archived_count": 0,
            "incident_baseline": "established",
            "incident_gate_signal": "signal_present",
            "technical_baseline": "established",
            "target_promotion_policy": (
                "Incident gate signal is graph-level support; target promotion remains human-gated."
            ),
            "provider_detection_overlap": "2/2",
            "review_unit_convergence": "1/1",
            "summary": "One target converged but promotion remains gated.",
        },
        "analysis_context": {
            "db_ingested_log_count": 42,
            "model_projection_evidence_items": 10,
            "model_projection_occurrence_count": 30,
            "model_projection_occurrence_coverage_ratio": 0.714286,
            "model_projection_interpretation": "Projection coverage is occurrence-weighted, not raw-row coverage.",
        },
        "provider_statuses": [
            {"provider_id": "gemini-enterprise-agent-platform", "status": "ok", "schema_valid": True},
            {"provider_id": "qwen-agent-platform", "status": "ok", "schema_valid": True},
        ],
        "targets": [
            {
                "review_target_id": "rt-1",
                "target_id": "rt-1",
                "title": "Delivery impact requires validation",
                "class": "validation_target",
                "status": "pending",
                "canonical_review_unit": "delivery_impact",
                "subsystem": "background_processing",
                "agreement": {
                    "verdict": "convergence",
                    "convergence_score": 1.0,
                    "technical_baseline": "established",
                    "incident_baseline": "open",
                    "summary": "2/2 providers projected this target.",
                },
                "promotion": {
                    "state": "validation",
                    "blocked_reason": "user_impact_unverified",
                    "score_note": "Priority is review urgency, not truth probability.",
                },
                "provider_positions": [
                    {"provider_id": "gemini-enterprise-agent-platform", "stance": "claimed"},
                    {"provider_id": "qwen-agent-platform", "stance": "claimed"},
                ],
                "evidence_refs": ["PATTERN-001"],
                "missing_evidence": ["User impact or operational outcome evidence tied to this review unit."],
            }
        ],
    }


def test_precomputed_graph_edges_reference_existing_nodes_and_gate_terms_are_split() -> None:
    payload = _payload()
    response = _precomputed_review_graph_response(payload, evidence_sha256=payload["evidence_sha256"])
    graph = response["graph"]
    nodes = {node["id"]: node for node in graph["nodes"]}
    missing_sources = [edge["source"] for edge in graph["edges"] if edge["source"] not in nodes]
    missing_targets = [edge["target"] for edge in graph["edges"] if edge["target"] not in nodes]

    assert missing_sources == []
    assert missing_targets == []
    assert nodes["baseline:technical"]["label"] == "Technical support"
    assert nodes["baseline:incident"]["label"] == "Incident gate signal"
    assert nodes["baseline:incident"]["state"] == "signal present"
    assert "target promotion remains human-gated" in nodes["baseline:incident"]["detail"]
    assert all(node["label"] != "Incident promotion" for node in graph["nodes"])
    assert response["canonical_review_graph"]["display_summary"]["incident_gate_signal"] == "signal present"


def test_precomputed_detail_page_separates_graph_gate_signal_from_target_promotion() -> None:
    payload = _payload()
    html = _render_precomputed_review_detail_page(payload["evidence_sha256"], payload)

    assert "Incident gate signal" in html
    assert "Target promotion" in html
    assert "per-target human-gated" in html
    assert "Incident gate signal is graph-level support" in html
    assert "<label>Incident promotion</label>" not in html
    assert "Incident promotion: open" in html
