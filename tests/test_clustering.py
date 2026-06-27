from __future__ import annotations

from ops_evidence_synthesis.synthesis.clustering import build_proposition_clusters
from ops_evidence_synthesis.synthesis.review_quality import shape_review_queue


def test_clusters_record_parent_child_validation_relationship() -> None:
    shaped = shape_review_queue(
        [
            _proposal(
                "prop-primary",
                "rtmps_ffmpeg",
                "stream_transport_count dropped to 0 from baseline 18.",
                "stream_transport_count",
                baseline=18.0,
                current=0.0,
                score=0.94,
            ),
            _proposal(
                "prop-validation",
                "network_transport",
                "No connection resets were observed, but RTMPS reconnects need validation.",
                "connection_reset_count",
                baseline=0.0,
                current=0.0,
                score=0.6967,
                claim_type="validation_target",
                next_data=["RTMPS reconnect timestamps around resets"],
            ),
        ],
        include_hidden=True,
    )

    clusters = build_proposition_clusters(shaped)
    by_prop = {cluster.representative_proposition_id: cluster for cluster in clusters}

    primary_cluster_id = by_prop["prop-primary"].cluster_id
    child_json = by_prop["prop-validation"].cluster_json
    assert child_json["parent_cluster_id"] == primary_cluster_id
    assert child_json["relationship"] == "validation_target_for_primary_incident"
    assert child_json["core_target_type"] == "network_error_signal"


def _proposal(
    proposition_id: str,
    subsystem: str,
    support_summary: str,
    metric: str,
    *,
    baseline: float,
    current: float,
    score: float,
    claim_type: str = "support",
    next_data: list[str] | None = None,
) -> dict[str, object]:
    return {
        "proposition_id": proposition_id,
        "evidence_sha256": "e" * 64,
        "window_start": "2026-06-15T22:00:00Z",
        "window_end": "2026-06-16T00:00:00Z",
        "service": "adsb-streamnew-youtube-stream.service",
        "environment": "stream_v3",
        "subsystem": subsystem,
        "question": "Should humans review stream evidence?",
        "support_summary": support_summary,
        "counter_summary": "",
        "validation_targets": [],
        "next_data_needed": next_data or [],
        "priority": "high",
        "review_status": "pending",
        "review_priority_score": score,
        "structured_evidence": {
            "support": [
                {
                    "evidence_id": f"METRIC-{proposition_id}",
                    "summary": f"{metric}={current}",
                    "current_value": current,
                    "baseline_value": baseline,
                }
            ],
            "counter_evidence": [],
            "caveats": [],
            "next_data_needed": next_data or [],
        },
        "suggested_actions": [
            {
                "claim_id": f"claim-{proposition_id}",
                "provider": "gemini-enterprise-agent-platform",
                "model_name": "gemini-2.5-flash",
                "claim_type": claim_type,
                "temporary_action": "Collect and validate the missing evidence.",
                "permanent_action": "",
                "required_authority": "SRE",
                "evidence_refs": [f"METRIC-{proposition_id}"],
                "caveats": [],
                "missing_evidence": next_data or [],
                "evidence_refs_valid": True,
            }
        ],
        "evidence_refs": [f"METRIC-{proposition_id}"],
    }
