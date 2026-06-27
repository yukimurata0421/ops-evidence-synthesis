from __future__ import annotations

from ops_evidence_synthesis.synthesis.review_quality import shape_review_queue


def test_review_quality_hides_zero_is_good_no_action() -> None:
    proposals = [
        {
            "proposition_id": "prop-good-zero",
            "evidence_sha256": "e" * 64,
            "window_start": "2026-06-15T22:00:00Z",
            "window_end": "2026-06-16T00:00:00Z",
            "subsystem": "network_transport",
            "question": "Should humans review network transport instability?",
            "support_summary": "connection_reset_count remained stable at 0.0.",
            "counter_summary": "",
            "validation_targets": [],
            "next_data_needed": [],
            "review_priority_score": 0.61,
            "structured_evidence": {
                "support": [
                    {
                        "evidence_id": "METRIC-003",
                        "summary": "connection_reset_count=0.0",
                        "current_value": 0.0,
                        "baseline_value": 0.0,
                    }
                ],
                "counter_evidence": [],
                "caveats": [],
                "next_data_needed": [],
            },
            "suggested_actions": [
                {
                    "provider": "gemini-enterprise-agent-platform",
                    "model_name": "gemini-2.5-flash",
                    "temporary_action": "No immediate action required.",
                    "permanent_action": "Continue monitoring.",
                    "required_authority": "None",
                }
            ],
            "evidence_refs": ["METRIC-003"],
        }
    ]

    assert shape_review_queue(proposals) == []
    hidden = shape_review_queue(proposals, include_hidden=True)
    assert hidden[0]["review_visibility"] == "monitor_only"
    assert hidden[0]["review_priority_score"] == 0.35
    assert "monitor_only_zero_is_good" in hidden[0]["hidden_reason"]


def test_review_quality_keeps_zero_is_bad_transport_disappearance() -> None:
    proposals = [
        {
            "proposition_id": "prop-transport-zero",
            "evidence_sha256": "e" * 64,
            "window_start": "2026-06-15T22:00:00Z",
            "window_end": "2026-06-16T00:00:00Z",
            "subsystem": "rtmps_ffmpeg",
            "question": "Should humans review RTMPS transport or ffmpeg send-path instability?",
            "support_summary": "stream_transport_count dropped to 0 from baseline 18.",
            "counter_summary": "",
            "validation_targets": [],
            "next_data_needed": [],
            "review_priority_score": 0.94,
            "structured_evidence": {
                "support": [
                    {
                        "evidence_id": "METRIC-007",
                        "summary": "stream_transport_count=0.0",
                        "current_value": 0.0,
                        "baseline_value": 18.0,
                    }
                ],
                "counter_evidence": [],
                "caveats": [],
                "next_data_needed": [],
            },
            "suggested_actions": [
                {
                    "provider": "gemini-enterprise-agent-platform",
                    "model_name": "gemini-2.5-flash",
                    "temporary_action": "Check RTMPS and ffmpeg state.",
                    "permanent_action": "Alert on transport disappearance.",
                    "required_authority": "SRE Team Lead",
                }
            ],
            "evidence_refs": ["METRIC-007"],
        }
    ]

    shaped = shape_review_queue(proposals)
    assert shaped[0]["review_visibility"] == "review"
    assert shaped[0]["score_breakdown"]["zero_is_bad_worsening"] is True
    assert shaped[0]["cluster_signature"] == "throughput_disappearance"
    assert shaped[0]["missing_evidence_count"] == 5
    assert any("RTMPS connection" in item for item in shaped[0]["next_data_needed"])


def test_review_quality_moves_zero_bad_counter_summary_to_support_display() -> None:
    proposals = [
        {
            "proposition_id": "prop-transport-counter",
            "evidence_sha256": "e" * 64,
            "window_start": "2026-06-15T22:00:00Z",
            "window_end": "2026-06-16T00:00:00Z",
            "subsystem": "rtmps_ffmpeg",
            "question": "Should humans review RTMPS transport or ffmpeg send-path instability?",
            "support_summary": "",
            "counter_summary": "stream_transport_count dropped to 0 from baseline 18.",
            "validation_targets": [],
            "next_data_needed": [],
            "review_priority_score": 0.94,
            "structured_evidence": {
                "support": [
                    {
                        "evidence_id": "METRIC-007",
                        "summary": "stream_transport_count=0.0",
                        "current_value": 0.0,
                        "baseline_value": 18.0,
                    }
                ],
                "counter_evidence": [],
                "caveats": [],
                "next_data_needed": [],
            },
            "suggested_actions": [],
            "evidence_refs": ["METRIC-007"],
        }
    ]

    shaped = shape_review_queue(proposals)
    assert "stream_transport_count dropped to 0" in shaped[0]["support_summary"]
    assert shaped[0]["counter_summary"] == ""


def test_review_quality_hides_duplicate_cluster_member() -> None:
    base = {
        "evidence_sha256": "e" * 64,
        "window_start": "2026-06-15T22:00:00Z",
        "window_end": "2026-06-16T00:00:00Z",
        "subsystem": "youtube_health",
        "question": "watchdog_ok and dead substate coexist",
        "support_summary": "watchdog_ok despite stream_service_substate dead.",
        "counter_summary": "",
        "validation_targets": [],
        "next_data_needed": ["YouTube watch URL status"],
        "structured_evidence": {"support": [], "counter_evidence": [], "caveats": [], "next_data_needed": []},
        "suggested_actions": [],
        "evidence_refs": ["LOG-001"],
    }

    proposals = [
        {**base, "proposition_id": "prop-a", "review_priority_score": 0.8},
        {**base, "proposition_id": "prop-b", "review_priority_score": 0.7},
    ]

    shaped = shape_review_queue(proposals, include_hidden=True)
    assert shaped[0]["cluster_size"] == 2
    assert shaped[0]["cluster_representative"] is True
    assert shaped[1]["review_visibility"] == "hidden"
    assert "duplicate_cluster_member" in shaped[1]["hidden_reason"]


def test_review_quality_hides_proposal_without_structured_evidence() -> None:
    proposals = [
        {
            "proposition_id": "prop-empty",
            "evidence_sha256": "e" * 64,
            "window_start": "2026-06-15T22:00:00Z",
            "window_end": "2026-06-16T00:00:00Z",
            "subsystem": "youtube_health",
            "question": "watchdog_ok and dead substate coexist",
            "support_summary": "",
            "counter_summary": "",
            "validation_targets": [],
            "next_data_needed": ["YouTube watch URL status"],
            "review_priority_score": 0.94,
            "structured_evidence": {"support": [], "counter_evidence": [], "caveats": [], "next_data_needed": []},
            "suggested_actions": [],
            "evidence_refs": [],
        }
    ]

    assert shape_review_queue(proposals) == []
    hidden = shape_review_queue(proposals, include_hidden=True)
    assert hidden[0]["review_priority_score"] == 0.55
    assert "support_and_counter_empty" in hidden[0]["hidden_reason"]


def test_review_quality_hides_counter_only_without_support() -> None:
    proposals = [
        {
            "proposition_id": "prop-counter-only",
            "evidence_sha256": "e" * 64,
            "window_start": "2026-06-15T22:00:00Z",
            "window_end": "2026-06-16T00:00:00Z",
            "subsystem": "resource_pressure",
            "question": "Should humans review resource pressure?",
            "support_summary": "",
            "counter_summary": "Runtime memory pressure evidence exists as counter-evidence only.",
            "validation_targets": [],
            "next_data_needed": [],
            "review_priority_score": 0.94,
            "structured_evidence": {
                "support": [],
                "counter_evidence": [
                    {
                        "evidence_id": "OPS-008",
                        "summary": "Runtime and monitor host memory/resource-pressure evidence.",
                        "kind": "operational_evidence",
                    }
                ],
                "caveats": [],
                "next_data_needed": [],
            },
            "suggested_actions": [],
            "evidence_refs": ["OPS-008"],
        }
    ]

    assert shape_review_queue(proposals) == []
    hidden = shape_review_queue(proposals, include_hidden=True)
    assert hidden[0]["review_priority_score"] == 0.55
    assert "support_empty" in hidden[0]["hidden_reason"]


def test_review_quality_hides_validation_target_with_only_missing_evidence() -> None:
    proposals = [
        {
            "proposition_id": "prop-missing-only",
            "evidence_sha256": "e" * 64,
            "window_start": "2026-06-16T22:12:45Z",
            "window_end": "2026-06-17T00:12:45Z",
            "service": "stream_v3_monitoring",
            "environment": "arena_server",
            "profile": {"profile_id": "stream_v3_monitoring"},
            "subsystem": "youtube_health",
            "question": "Should humans review YouTube ingest status?",
            "support_summary": "",
            "counter_summary": "",
            "validation_targets": [],
            "next_data_needed": ["Correlate local ingest, public watch, API, OAuth, and active stream."],
            "review_priority_score": 0.86,
            "structured_evidence": {
                "support": [],
                "counter_evidence": [],
                "caveats": [],
                "next_data_needed": ["Correlate local ingest, public watch, API, OAuth, and active stream."],
            },
            "suggested_actions": [
                {
                    "provider": "gemini-enterprise-agent-platform",
                    "model_name": "gemini-2.5-flash",
                    "claim_type": "validation_target",
                    "temporary_action": "Collect and validate YouTube ingest evidence.",
                    "permanent_action": "Document validation source coverage.",
                    "required_authority": "SRE",
                }
            ],
            "evidence_refs": [],
        }
    ]

    assert shape_review_queue(proposals) == []
    hidden = shape_review_queue(proposals, include_hidden=True)
    assert hidden[0]["review_visibility"] == "hidden"
    assert "support_and_counter_empty" in hidden[0]["hidden_reason"]


def test_review_quality_preserves_structured_validation_review_mode() -> None:
    proposals = [
        {
            "proposition_id": "prop-restart-validation",
            "evidence_sha256": "e" * 64,
            "window_start": "2026-06-16T22:12:45Z",
            "window_end": "2026-06-17T00:12:45Z",
            "service": "stream_v3_monitoring",
            "environment": "arena_server",
            "profile": {"profile_id": "stream_v3_monitoring"},
            "subsystem": "runtime_recovery",
            "question": "Should humans review restart evidence?",
            "support_summary": "runtime_restart_count increased from baseline 3 to 6.",
            "counter_summary": "",
            "validation_targets": [],
            "next_data_needed": ["Correlate runtime restarts, rollouts, and health changes."],
            "review_priority_score": 0.70,
            "structured_evidence": {
                "support": [
                    {
                        "evidence_id": "METRIC-003",
                        "summary": "runtime_restart_count 3.0 -> 6.0",
                        "kind": "metric_window",
                        "core_target_type": "restart_loop",
                        "review_mode": "validation_target",
                    }
                ],
                "counter_evidence": [],
                "caveats": [],
                "next_data_needed": ["Correlate runtime restarts, rollouts, and health changes."],
                "evidence_identity": [
                    {
                        "core_target_type": "restart_loop",
                        "review_mode": "validation_target",
                    }
                ],
            },
            "suggested_actions": [
                {
                    "provider": "rule-engine",
                    "claim_type": "support",
                    "temporary_action": "Collect restart state evidence.",
                    "permanent_action": "Persist restart validation.",
                    "required_authority": "SRE",
                }
            ],
            "evidence_refs": ["METRIC-003"],
        }
    ]

    shaped = shape_review_queue(proposals)

    assert shaped[0]["review_visibility"] == "review"
    assert shaped[0]["review_mode"] == "validation_target"


def test_review_quality_hides_insufficient_evidence_claims() -> None:
    proposals = [
        {
            "proposition_id": "prop-insufficient",
            "evidence_sha256": "e" * 64,
            "window_start": "2026-06-15T22:00:00Z",
            "window_end": "2026-06-16T00:00:00Z",
            "subsystem": "runtime_recovery",
            "question": "Should humans review an unspecified runtime error?",
            "support_summary": "",
            "counter_summary": "",
            "validation_targets": ["An error occurred but the program and failure signature are unknown."],
            "next_data_needed": ["program name", "exact error message"],
            "review_priority_score": 0.91,
            "structured_evidence": {
                "support": [],
                "counter_evidence": [],
                "caveats": ["An error occurred but the program and failure signature are unknown."],
                "next_data_needed": ["program name", "exact error message"],
                "insufficient_evidence": [
                    {
                        "claim_id": "claim-insufficient",
                        "provider": "test-provider",
                        "finding_status": "insufficient_evidence",
                        "claim_text": "An error occurred but the program and failure signature are unknown.",
                        "missing_evidence": ["program name", "exact error message"],
                        "evidence_identity": {
                            "program": "unknown",
                            "source": "unknown",
                            "failure_signature": "unknown",
                            "time_window": "known",
                        },
                    }
                ],
                "finding_statuses": {"insufficient_evidence": 1},
                "identity_gaps": [
                    "claim-insufficient:program",
                    "claim-insufficient:source",
                    "claim-insufficient:failure_signature",
                ],
                "evidence_identity": [
                    {
                        "claim_id": "claim-insufficient",
                        "program": "unknown",
                        "source": "unknown",
                        "failure_signature": "unknown",
                        "time_window": "known",
                    }
                ],
            },
            "suggested_actions": [],
            "evidence_refs": [],
        }
    ]

    assert shape_review_queue(proposals) == []
    hidden = shape_review_queue(proposals, include_hidden=True)
    assert hidden[0]["review_visibility"] == "hidden"
    assert hidden[0]["review_mode"] == "insufficient_evidence"
    assert hidden[0]["review_priority_score"] == 0.35
    assert hidden[0]["finding_status_counts"] == {"insufficient_evidence": 1}
    assert "insufficient_evidence" in hidden[0]["hidden_reason"]
    assert "program_or_source_unknown" in hidden[0]["hidden_reason"]


def test_review_quality_hides_supported_claim_with_unknown_program_identity() -> None:
    proposals = [
        {
            "proposition_id": "prop-unknown-program",
            "evidence_sha256": "e" * 64,
            "window_start": "2026-06-15T22:00:00Z",
            "window_end": "2026-06-16T00:00:00Z",
            "subsystem": "general",
            "question": "Should humans review an unspecified error?",
            "support_summary": "An error pattern increased, but the emitting program is unknown.",
            "counter_summary": "",
            "validation_targets": [],
            "next_data_needed": ["program name"],
            "review_priority_score": 0.82,
            "structured_evidence": {
                "support": [
                    {
                        "evidence_id": "LOG-UNKNOWN",
                        "claim_id": "claim-unknown",
                        "summary": "error occurred",
                    }
                ],
                "counter_evidence": [],
                "caveats": [],
                "next_data_needed": ["program name"],
                "finding_statuses": {"supported": 1},
                "evidence_identity": [
                    {
                        "claim_id": "claim-unknown",
                        "program": "unknown",
                        "source": "known",
                        "failure_signature": "known",
                        "time_window": "known",
                    }
                ],
            },
            "suggested_actions": [
                {
                    "provider": "test-provider",
                    "model_name": "test-model",
                    "claim_type": "support",
                    "finding_status": "supported",
                    "evidence_identity": {
                        "program": "unknown",
                        "source": "known",
                        "failure_signature": "known",
                        "time_window": "known",
                    },
                    "temporary_action": "Collect the emitting program name before review.",
                    "permanent_action": "Add structured program labels.",
                    "required_authority": "SRE",
                }
            ],
            "evidence_refs": ["LOG-UNKNOWN"],
        }
    ]

    assert shape_review_queue(proposals) == []
    hidden = shape_review_queue(proposals, include_hidden=True)
    assert hidden[0]["review_priority_score"] == 0.45
    assert hidden[0]["identity_unknown_keys"] == ["program"]
    assert "program_or_source_unknown" in hidden[0]["hidden_reason"]


def test_review_quality_moves_no_immediate_action_to_monitor_only() -> None:
    proposals = [
        {
            "proposition_id": "prop-no-action",
            "evidence_sha256": "e" * 64,
            "window_start": "2026-06-15T22:00:00Z",
            "window_end": "2026-06-16T00:00:00Z",
            "subsystem": "general",
            "question": "What incident hypothesis needs human review first?",
            "support_summary": "Warnings decreased and no immediate action is required.",
            "counter_summary": "",
            "validation_targets": [],
            "next_data_needed": [],
            "review_priority_score": 0.82,
            "structured_evidence": {
                "support": [
                    {
                        "evidence_id": "METRIC-009",
                        "summary": "warn_count=0.0",
                        "current_value": 0.0,
                        "baseline_value": 10.0,
                    }
                ],
                "counter_evidence": [],
                "caveats": [],
                "next_data_needed": [],
            },
            "suggested_actions": [
                {
                    "provider": "gemini-enterprise-agent-platform",
                    "model_name": "gemini-2.5-flash",
                    "temporary_action": "No immediate action required.",
                    "permanent_action": "Continue monitoring.",
                    "required_authority": "None",
                }
            ],
            "evidence_refs": ["METRIC-009"],
        }
    ]

    assert shape_review_queue(proposals) == []
    hidden = shape_review_queue(proposals, include_hidden=True)
    assert hidden[0]["review_visibility"] == "monitor_only"
    assert hidden[0]["review_priority_score"] == 0.35
    assert "no_immediate_action" in hidden[0]["hidden_reason"]


def test_review_quality_adds_next_data_for_ffmpeg_log_candidate() -> None:
    proposals = [
        {
            "proposition_id": "prop-ffmpeg-log",
            "evidence_sha256": "e" * 64,
            "window_start": "2026-06-15T22:00:00Z",
            "window_end": "2026-06-16T00:00:00Z",
            "subsystem": "general",
            "question": "Should humans review RTMPS transport or ffmpeg send-path instability?",
            "support_summary": "ffmpeg exited with code 1 and restarted.",
            "counter_summary": "",
            "validation_targets": [],
            "next_data_needed": [],
            "review_priority_score": 0.61,
            "structured_evidence": {
                "support": [
                    {
                        "evidence_id": "LOG-020",
                        "summary": "ffmpeg exited with code 1. Restarting in 5s.",
                    }
                ],
                "counter_evidence": [],
                "caveats": [],
                "next_data_needed": [],
            },
            "suggested_actions": [
                {
                    "provider": "gemini-enterprise-agent-platform",
                    "model_name": "gemini-2.5-flash",
                    "temporary_action": "Review stream-engine_tail.log.",
                    "permanent_action": "Improve ffmpeg error handling.",
                    "required_authority": "Developer/SRE",
                }
            ],
            "evidence_refs": ["LOG-020"],
        }
    ]

    shaped = shape_review_queue(proposals)
    assert shaped[0]["cluster_signature"] == "runtime_instability"
    assert shaped[0]["missing_evidence_count"] == 3


def test_review_quality_moves_observability_volume_only_to_monitor() -> None:
    proposals = [
        {
            "proposition_id": "prop-active-service-count",
            "evidence_sha256": "e" * 64,
            "window_start": "2026-06-15T22:00:00Z",
            "window_end": "2026-06-16T00:00:00Z",
            "subsystem": "resource_pressure",
            "question": "Should humans review resource pressure or timeout behavior?",
            "support_summary": "Active service count increased from 1.71 to 4.0, indicating increased load.",
            "counter_summary": "",
            "validation_targets": [],
            "next_data_needed": [],
            "review_priority_score": 0.6125,
            "structured_evidence": {
                "support": [
                    {
                        "evidence_id": "METRIC-002",
                        "summary": "active_service_count=4.0",
                        "current_value": 4.0,
                        "baseline_value": 1.71,
                    }
                ],
                "counter_evidence": [],
                "caveats": [],
                "next_data_needed": [],
            },
            "suggested_actions": [
                {
                    "provider": "openai-gpt-oss-on-vertex",
                    "model_name": "gpt-oss-20b-maas",
                    "temporary_action": "Review service deployment logs for new instances and traffic patterns.",
                    "permanent_action": "Adjust auto-scaling thresholds if necessary.",
                    "required_authority": "Platform Ops Lead",
                }
            ],
            "evidence_refs": ["METRIC-002"],
        }
    ]

    assert shape_review_queue(proposals) == []
    hidden = shape_review_queue(proposals, include_hidden=True)
    assert hidden[0]["review_visibility"] == "monitor_only"
    assert hidden[0]["review_priority_score"] == 0.4
    assert "monitor_only_observability_volume" in hidden[0]["hidden_reason"]


def test_review_quality_promotes_stream_validation_targets_without_promoting_runtime_good_news() -> None:
    proposals = [
        _validation_proposal(
            "prop-youtube",
            "youtube_health",
            "youtube_health_count=0.0",
            "YouTube health is zero but ingest and watch URL need validation.",
            "YouTube watch URL status",
            raw_score=0.755,
            baseline=0.0,
            current=0.0,
        ),
        _validation_proposal(
            "prop-network",
            "network_transport",
            "connection_reset_count=0.0",
            "No connection resets were observed, but RTMPS reconnects need validation.",
            "RTMPS reconnect timestamps around resets",
            raw_score=0.6967,
            baseline=0.0,
            current=0.0,
        ),
        _validation_proposal(
            "prop-runtime",
            "runtime_recovery",
            "runtime_restart_count=0.0",
            "Runtime restarts decreased to zero.",
            "runtime restart timestamps",
            raw_score=0.87,
            baseline=0.42,
            current=0.0,
        ),
    ]

    shaped = shape_review_queue(proposals, include_hidden=True)
    by_id = {str(item["proposition_id"]): item for item in shaped}

    assert by_id["prop-youtube"]["review_visibility"] == "review"
    assert by_id["prop-youtube"]["review_mode"] == "validation_target"
    assert by_id["prop-youtube"]["review_priority_score"] == 0.62
    assert by_id["prop-network"]["review_visibility"] == "review"
    assert by_id["prop-network"]["review_mode"] == "validation_target"
    assert by_id["prop-network"]["review_priority_score"] == 0.6
    assert by_id["prop-runtime"]["review_visibility"] == "monitor_only"
    assert by_id["prop-runtime"]["review_priority_score"] == 0.35


def test_review_quality_reuses_human_acceptance_as_priority_boost() -> None:
    proposal = _validation_proposal(
        "prop-human-accepted",
        "network_transport",
        "connection_reset_count=0.0",
        "No connection resets were observed, but RTMPS reconnects need validation.",
        "RTMPS reconnect timestamps around resets",
        raw_score=0.60,
        baseline=0.0,
        current=0.0,
    )
    proposal["latest_review_decision"] = "accepted"
    proposal["latest_review_detail"] = "confirmed_candidate"

    shaped = shape_review_queue([proposal], include_hidden=True)

    assert shaped[0]["review_history"]["effect"] == "boost"
    assert shaped[0]["review_priority_score"] == 0.7
    assert shaped[0]["score_breakdown"]["review_history"]["reason"] == "human_confirmed_candidate_history"


def test_review_quality_reuses_human_rejection_as_demote() -> None:
    proposal = _validation_proposal(
        "prop-human-rejected",
        "network_transport",
        "connection_reset_count=0.0",
        "No connection resets were observed, but RTMPS reconnects need validation.",
        "RTMPS reconnect timestamps around resets",
        raw_score=0.70,
        baseline=0.0,
        current=0.0,
    )
    proposal["latest_review_decision"] = "rejected"
    proposal["latest_review_detail"] = "false_positive"

    shaped = shape_review_queue([proposal], include_hidden=True)

    assert shaped[0]["review_history"]["effect"] == "demote"
    assert shaped[0]["review_visibility"] == "hidden"
    assert shaped[0]["review_priority_score"] == 0.2
    assert "human_rejected_false_positive" in shaped[0]["hidden_reason"]


def test_review_quality_keeps_generic_throughput_disappearance_as_primary_incident() -> None:
    proposal = {
        "proposition_id": "prop-generic-throughput-zero",
        "evidence_sha256": "e" * 64,
        "profile": "generic",
        "window_start": "2026-06-15T22:00:00Z",
        "window_end": "2026-06-16T00:00:00Z",
        "subsystem": "traffic",
        "question": "Should humans review a throughput disappearance?",
        "support_summary": "The generic throughput signal is missing for this window.",
        "counter_summary": "No error spike was observed.",
        "validation_targets": [],
        "next_data_needed": [],
        "review_priority_score": 0.61,
        "structured_evidence": {
            "support": [
                {
                    "evidence_id": "METRIC-GENERIC",
                    "summary": "throughput_count=0.0",
                    "current_value": 0.0,
                    "baseline_value": 0.0,
                }
            ],
            "counter_evidence": [],
            "caveats": [],
            "next_data_needed": [],
        },
        "suggested_actions": [
            {
                "provider": "heuristic",
                "model_name": "generic",
                "temporary_action": "No immediate action required.",
                "permanent_action": "Continue monitoring.",
                "required_authority": "None",
            }
        ],
        "evidence_refs": ["METRIC-GENERIC"],
    }

    shaped = shape_review_queue([proposal], include_hidden=True)

    assert shaped[0]["cluster_signature"] == "throughput_disappearance"
    assert shaped[0]["review_visibility"] == "review"
    assert shaped[0]["review_mode"] == "incident_candidate"
    assert shaped[0]["review_priority_score"] == 0.8
    assert shaped[0]["score_breakdown"]["primary_incident_target"] is True
    assert shaped[0]["score_breakdown"]["floors"][0]["reason"] == "throughput_disappearance_primary_incident_floor"


def test_review_quality_prefers_profile_subsystem_mapping_over_broad_text_terms() -> None:
    proposal = {
        "proposition_id": "prop-chromium-capture",
        "evidence_sha256": "e" * 64,
        "environment": "stream_v3",
        "window_start": "2026-06-15T22:00:00Z",
        "window_end": "2026-06-16T00:00:00Z",
        "subsystem": "chromium_capture",
        "question": "Should humans review Chromium capture process instability?",
        "support_summary": "",
        "counter_summary": "",
        "validation_targets": ["Packet capture is missing for RTMPS reconnect validation."],
        "next_data_needed": ["packet_capture_logs"],
        "review_priority_score": 0.71,
        "structured_evidence": {
            "support": [],
            "counter_evidence": [],
            "caveats": [],
            "next_data_needed": ["packet_capture_logs"],
        },
        "suggested_actions": [
            {
                "provider": "openai-gpt-oss-on-vertex",
                "model_name": "gpt-oss-20b-maas",
                "claim_type": "next_data_needed",
                "temporary_action": "Collect and review Chromium capture data for subsystem health.",
                "permanent_action": "Enable continuous packet capture for RTMPS traffic.",
                "required_authority": "Capture Team",
            }
        ],
        "evidence_refs": ["OPS-005"],
    }

    shaped = shape_review_queue([proposal], include_hidden=True)

    assert shaped[0]["cluster_signature"] == "freshness_signal_gap"
    assert shaped[0]["review_mode"] == "validation_target"
    assert shaped[0]["review_priority_score"] == 0.55


def test_review_quality_hides_cross_subsystem_duplicate_primary_targets() -> None:
    rule_primary = {
        "proposition_id": "prop-rule-primary",
        "evidence_sha256": "e" * 64,
        "environment": "stream_v3",
        "window_start": "2026-06-15T22:00:00Z",
        "window_end": "2026-06-16T00:00:00Z",
        "subsystem": "rtmps_ffmpeg",
        "question": "Should humans review stream transport disappeared?",
        "support_summary": "throughput_signal was present in baseline but absent from the incident window.",
        "counter_summary": "",
        "validation_targets": [],
        "next_data_needed": [],
        "review_priority_score": 0.84,
        "structured_evidence": {
            "support": [
                {
                    "evidence_id": "OPS-002",
                    "summary": "stream_transport_count=0.0",
                    "current_value": 0.0,
                    "baseline_value": 18.0,
                    "core_target_type": "throughput_disappearance",
                }
            ],
            "counter_evidence": [],
            "caveats": [],
            "next_data_needed": [],
            "evidence_identity": [
                {
                    "provider": "rule-engine",
                    "core_target_type": "throughput_disappearance",
                    "review_mode": "incident_candidate",
                }
            ],
        },
        "suggested_actions": [
            {
                "provider": "rule-engine",
                "model_name": "",
                "temporary_action": "Review the cited evidence.",
                "permanent_action": "Persist the validation signal.",
                "required_authority": "SRE",
            }
        ],
        "evidence_refs": ["OPS-002"],
    }
    ai_duplicate = {
        **rule_primary,
        "proposition_id": "prop-ai-duplicate",
        "subsystem": "youtube_health",
        "question": "Should humans review YouTube live health or API evidence?",
        "support_summary": "The leading hypothesis is RTMPS transport or ffmpeg send-path instability.",
        "review_priority_score": 0.8425,
        "suggested_actions": [
            {
                "provider": "gemini-local",
                "model_name": "gemini-simulated-verifier",
                "temporary_action": "Keep mitigations reversible.",
                "permanent_action": "Route RTMPS counters.",
                "required_authority": "SRE",
            }
        ],
        "structured_evidence": {
            **rule_primary["structured_evidence"],
            "evidence_identity": [
                {
                    "provider": "gemini-local",
                    "core_target_type": "throughput_disappearance",
                    "review_mode": "incident_candidate",
                }
            ],
        },
    }

    shaped = shape_review_queue([ai_duplicate, rule_primary], include_hidden=True)
    by_id = {item["proposition_id"]: item for item in shaped}

    assert by_id["prop-rule-primary"]["review_visibility"] == "review"
    assert by_id["prop-ai-duplicate"]["review_visibility"] == "hidden"
    assert "duplicate_primary_core_target" in by_id["prop-ai-duplicate"]["hidden_reason"]


def _validation_proposal(
    proposition_id: str,
    subsystem: str,
    metric_summary: str,
    support_summary: str,
    next_data: str,
    *,
    raw_score: float,
    baseline: float,
    current: float,
) -> dict[str, object]:
    return {
        "proposition_id": proposition_id,
        "evidence_sha256": "e" * 64,
        "window_start": "2026-06-15T22:00:00Z",
        "window_end": "2026-06-16T00:00:00Z",
        "subsystem": subsystem,
        "question": "Should humans review validation evidence?",
        "support_summary": support_summary,
        "counter_summary": "",
        "validation_targets": [],
        "next_data_needed": [next_data],
        "review_priority_score": raw_score,
        "structured_evidence": {
            "support": [
                {
                    "evidence_id": "METRIC-VALIDATION",
                    "summary": metric_summary,
                    "current_value": current,
                    "baseline_value": baseline,
                }
            ],
            "counter_evidence": [],
            "caveats": [],
            "next_data_needed": [next_data],
        },
        "suggested_actions": [
            {
                "provider": "openai-gpt-oss-on-vertex",
                "model_name": "gpt-oss-20b-maas",
                "claim_type": "validation_target",
                "temporary_action": "Collect and validate the missing evidence.",
                "permanent_action": "Document validation source coverage.",
                "required_authority": "SRE",
            }
        ],
        "evidence_refs": ["METRIC-VALIDATION"],
    }
