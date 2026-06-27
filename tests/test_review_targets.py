from __future__ import annotations

from ops_evidence_synthesis.synthesis.review_quality import shape_review_queue
from ops_evidence_synthesis.synthesis.review_targets import build_review_target_set, more_data_request_for_target


def test_review_target_set_compresses_to_human_review_cards() -> None:
    proposals = [
        _proposal(
            "prop-transport",
            "rtmps_ffmpeg",
            "stream_transport_count dropped to 0 from baseline 18.",
            metric="stream_transport_count",
            baseline=18.0,
            current=0.0,
            score=0.94,
            provider="gemini-enterprise-agent-platform",
        ),
        _proposal(
            "prop-transport-haiku",
            "rtmps_ffmpeg",
            "stream_transport_count dropped to 0 from baseline 18.",
            metric="stream_transport_count",
            baseline=18.0,
            current=0.0,
            score=0.88,
            provider="claude-haiku",
        ),
        _proposal(
            "prop-no-action",
            "network_transport",
            "connection_reset_count remained stable at 0.0.",
            metric="connection_reset_count",
            baseline=0.0,
            current=0.0,
            score=0.74,
            action="No immediate action required.",
            authority="None",
        ),
    ]
    proposals[0]["structured_evidence"]["caveats"] = [
        "The rtmps_ffmpeg subsystem shows a significant drop in stream transport count, which may indicate an issue."
    ]

    shaped = shape_review_queue(proposals, include_hidden=True)
    target_set = build_review_target_set(shaped, limit=5)

    assert target_set["summary"]["raw_propositions"] == 3
    assert target_set["summary"]["claim_groups"] == 2
    assert target_set["summary"]["review_targets"] == 1
    assert target_set["summary"]["primary_review_targets"] == 1
    assert target_set["summary"]["validation_targets"] == 0
    assert target_set["summary"]["monitor_only"] == 1
    assert target_set["summary"]["auto_archived"] == 1

    target = target_set["targets"][0]
    assert target["title"] == "Stream transport disappeared"
    assert target["support_count"] == 1
    assert target["counter_count"] == 0
    assert target["missing_evidence_count"] == len(target["drawer"]["missing_evidence"])
    assert target["evidence_count"] == 1
    assert target["review_priority_score"] == 0.94
    assert target["score_breakdown"]["score_note"] == "Score is review priority, not truth probability."
    breakdown = target["score_breakdown"]["breakdown"]
    assert breakdown["evidence_strength"] > 0
    assert breakdown["model_detection_agreement"] == 1.0
    assert breakdown["evidence_diversity"] == 0.5
    assert target["model_agreement"]["detected_provider_count"] == 2
    assert target["model_agreement"]["total_provider_count"] == 2
    assert target["model_agreement"]["evidence_diversity_label"] == "1 metric"
    assert "zero_is_bad metric dropped to zero" in target["why_survived"]
    assert "significant drop" not in target["counter_or_caveat_summary"]
    assert "metric/log collection gap" in target["counter_or_caveat_summary"]
    assert "Recommended next checks:" in target["proposal"]
    assert "P1:" in target["proposal"]
    assert "P2:" in target["proposal"]
    assert "P3:" in target["proposal"]
    assert any("RTMPS connection" in item for item in target["actions"]["more_data"]["next_data_needed"])
    assert target["core_target_type"] == "throughput_disappearance"
    assert target["profile"]["profile_id"] == "stream_v3"
    assert target["actions"]["more_data"]["next_evidence_requests"][0]["request_id"] == "process_state_query"
    assert target["actions"]["more_data"]["next_evidence_requests"][0]["profile_request_id"] == "ffmpeg_state_query"
    more_data = more_data_request_for_target(target, {"sql": "select 1"})
    assert more_data["next_evidence_requests"][1]["request_id"] == "throughput_signal_query"
    assert more_data["next_evidence_requests"][1]["profile_request_id"] == "rtmps_reconnect_query"
    assert "ops-evidence collect-more" in more_data["next_cli_command"]
    assert target["drawer"]["zero_semantics"]["metrics"][0]["meaning"] == "zero_is_bad"


def test_review_target_card_describes_validation_target_as_counter_evidence() -> None:
    proposal = _proposal(
        "prop-network-validation",
        "network_transport",
        "No connection resets were observed.",
        metric="connection_reset_count",
        baseline=0.0,
        current=0.0,
        score=0.6967,
        action="Collect logs and network traces for the incident window.",
        authority="Streaming Platform Lead",
    )
    proposal["next_data_needed"] = ["RTMPS reconnect timestamps around resets"]
    proposal["structured_evidence"]["next_data_needed"] = ["RTMPS reconnect timestamps around resets"]
    proposal["suggested_actions"][0]["claim_type"] = "validation_target"

    shaped = shape_review_queue([proposal], include_hidden=True)
    target_set = build_review_target_set(shaped, limit=5)

    target = target_set["targets"][0]
    assert target["title"] == "Network reset counter-evidence needs validation"
    assert target["review_mode"] == "validation_target"
    assert "counter-evidence, not proof" in target["core_claim"]
    assert any("zero_is_good signal is counter-evidence" in reason for reason in target["why_survived"])
    assert "RTMPS" in target["proposal"]
    assert target["drawer"]["next_evidence_requests"][0]["request_id"] == "network_path_query"
    assert target["drawer"]["next_evidence_requests"][0]["profile_request_id"] == "network_reset_by_destination_query"


def test_review_target_set_links_validation_target_to_primary_incident() -> None:
    transport = _proposal(
        "prop-transport-primary",
        "rtmps_ffmpeg",
        "stream_transport_count dropped to 0 from baseline 18.",
        metric="stream_transport_count",
        baseline=18.0,
        current=0.0,
        score=0.94,
        provider="gemini-enterprise-agent-platform",
    )
    validation = _proposal(
        "prop-network-validation",
        "network_transport",
        "No connection resets were observed.",
        metric="connection_reset_count",
        baseline=0.0,
        current=0.0,
        score=0.6967,
        action="Collect logs and network traces for the incident window.",
        authority="Streaming Platform Lead",
    )
    validation["next_data_needed"] = ["RTMPS reconnect timestamps around resets"]
    validation["structured_evidence"]["next_data_needed"] = ["RTMPS reconnect timestamps around resets"]
    validation["suggested_actions"][0]["claim_type"] = "validation_target"

    shaped = shape_review_queue([transport, validation], include_hidden=True)
    target_set = build_review_target_set(shaped, limit=5)
    by_title = {target["title"]: target for target in target_set["targets"]}

    primary = by_title["Stream transport disappeared"]
    child = by_title["Network reset counter-evidence needs validation"]
    assert child["parent_review_target_id"] == primary["review_target_id"]
    assert child["relationship"] == "validation_target_for_primary_incident"
    assert primary["related_review_targets"][0]["review_target_id"] == child["review_target_id"]
    assert target_set["summary"]["primary_review_targets"] == 1
    assert target_set["summary"]["validation_targets"] == 1
    graph = target_set["summary"]["review_graph"]
    assert graph["primary_candidate_count"] == 1
    assert graph["validation_target_count"] == 1
    assert graph["nodes"][0]["primary_review_target_id"] == primary["review_target_id"]
    assert graph["nodes"][0]["validation_targets"][0]["review_target_id"] == child["review_target_id"]


def test_review_target_display_repairs_stale_broad_text_signature_with_profile_subsystem() -> None:
    proposal = {
        "proposition_id": "prop-chromium-stale-signature",
        "evidence_sha256": "e" * 64,
        "cluster_id": "cluster-stale",
        "cluster_signature": "throughput_disappearance",
        "review_visibility": "review",
        "review_mode": "validation_target",
        "environment": "stream_v3",
        "subsystem": "chromium_capture",
        "question": "Should humans review Chromium capture process instability?",
        "support_summary": "",
        "counter_summary": "",
        "validation_targets": ["RTMPS packet capture is missing."],
        "next_data_needed": ["packet_capture_logs"],
        "priority": "medium",
        "review_priority_score": 0.56,
        "structured_evidence": {
            "support": [],
            "counter_evidence": [],
            "caveats": [],
            "next_data_needed": ["packet_capture_logs"],
        },
        "suggested_actions": [],
        "evidence_refs": ["OPS-005"],
    }

    target_set = build_review_target_set([proposal], limit=5)
    target = target_set["targets"][0]

    assert target["core_target_type"] == "freshness_signal_gap"
    assert target["title"] == "Capture freshness gap needs validation"
    assert target["drawer"]["synthesis"]["cluster_signature"] == "throughput_disappearance"


def test_job_configuration_mismatch_proposal_prioritizes_command_and_timer_checks() -> None:
    proposal = {
        "proposition_id": "prop-job-config",
        "evidence_sha256": "e" * 64,
        "cluster_id": "cluster-job-config",
        "cluster_signature": "job_configuration_mismatch",
        "review_visibility": "review",
        "review_mode": "incident_candidate",
        "environment": "generic",
        "subsystem": "job_configuration",
        "question": "Should humans review missing configured job commands or supervisor configuration drift?",
        "support_summary": "systemd failed because the configured command target returned no such file or directory.",
        "counter_summary": "",
        "validation_targets": [],
        "next_data_needed": [
            "current supervisor unit definition and configured command target existence.",
            "timer history and last successful execution timestamp.",
        ],
        "priority": "high",
        "review_status": "pending",
        "review_priority_score": 0.86,
        "evidence_ref_score": 1.0,
        "actionability_score": 1.0,
        "cross_model_agreement": 1.0,
        "structured_evidence": {
            "support": [
                {
                    "evidence_id": "PATTERN-001",
                    "summary": "can't open file /opt/app/deployment/systemd/watchdog.py: no such file or directory",
                }
            ],
            "counter_evidence": [],
            "caveats": [],
            "next_data_needed": [
                "current supervisor unit definition and configured command target existence.",
                "timer history and last successful execution timestamp.",
            ],
        },
        "suggested_actions": [],
        "evidence_refs": ["PATTERN-001"],
    }

    target_set = build_review_target_set([proposal], limit=5)
    target = target_set["targets"][0]

    assert target["core_target_type"] == "job_configuration_mismatch"
    assert target["title"] == "Configured job command is missing"
    assert target["support_count"] == len(target["drawer"]["support_evidence"]) == 1
    assert target["counter_count"] == len(target["drawer"]["counter_evidence"]) == 0
    assert target["missing_evidence_count"] == len(target["drawer"]["missing_evidence"]) == 2
    assert target["evidence_count"] == 1
    assert "configured command target exists" in target["proposal"]
    assert "installed artifacts or package contents" in target["proposal"]
    assert "scheduler or timer history" in target["proposal"]
    assert target["actions"]["more_data"]["next_evidence_requests"][0]["request_id"] == "job_definition_query"


def _proposal(
    proposition_id: str,
    subsystem: str,
    support_summary: str,
    *,
    metric: str,
    baseline: float,
    current: float,
    score: float,
    provider: str = "gemini-enterprise-agent-platform",
    action: str = "Check RTMPS and ffmpeg state.",
    authority: str = "SRE Team Lead",
) -> dict[str, object]:
    return {
        "proposition_id": proposition_id,
        "evidence_sha256": "e" * 64,
        "window_start": "2026-06-15T22:00:00Z",
        "window_end": "2026-06-16T00:00:00Z",
        "service": "adsb-streamnew-youtube-stream.service",
        "environment": "stream_v3",
        "subsystem": subsystem,
        "question": "Should humans review RTMPS transport or ffmpeg send-path instability?",
        "support_summary": support_summary,
        "counter_summary": "",
        "validation_targets": [],
        "next_data_needed": [],
        "priority": "high",
        "review_status": "pending",
        "review_priority_score": score,
        "evidence_ref_score": 1.0,
        "actionability_score": 1.0 if authority != "None" else 0.0,
        "cross_model_agreement": 0.7,
        "structured_evidence": {
            "support": [
                {
                    "evidence_id": "METRIC-001",
                    "summary": f"{metric}={current}",
                    "current_value": current,
                    "baseline_value": baseline,
                }
            ],
            "counter_evidence": [],
            "caveats": [],
            "next_data_needed": [],
        },
        "suggested_actions": [
            {
                "claim_id": f"claim-{proposition_id}",
                "provider": provider,
                "model_name": provider,
                "claim_type": "support",
                "temporary_action": action,
                "permanent_action": "",
                "required_authority": authority,
                "evidence_refs": ["METRIC-001"],
                "caveats": [],
                "missing_evidence": [],
                "evidence_refs_valid": True,
            }
        ],
        "evidence_refs": ["METRIC-001"],
    }
