from __future__ import annotations

from ops_evidence_synthesis.synthesis.router import route_claims
from ops_evidence_synthesis.synthesis.signals import build_signal_graph


def test_signal_graph_routes_primary_and_metric_log_conflict_separately() -> None:
    bundle = _stream_bundle()

    graph = build_signal_graph(bundle)
    signals = graph["signals"]
    targets = graph["candidate_targets"]

    assert any(signal["signal_type"] == "zero_is_bad_drop" for signal in signals)
    conflict = [signal for signal in signals if signal["signal_type"] == "log_metric_conflict"]
    assert conflict
    assert conflict[0]["core_target_type"] == "instrumentation_mismatch"
    assert conflict[0]["routing_hint"] == "validation_target"

    by_type = {target["core_target_type"]: target for target in targets}
    assert by_type["throughput_disappearance"]["review_mode"] == "incident_candidate"
    assert by_type["instrumentation_mismatch"]["review_mode"] == "validation_target"
    assert by_type["instrumentation_mismatch"]["parent_target_id"] == by_type["throughput_disappearance"]["target_id"]
    assert "METRIC-009" in by_type["instrumentation_mismatch"]["support_evidence_refs"]
    assert "OPS-002" in by_type["instrumentation_mismatch"]["support_evidence_refs"]


def test_signal_graph_uses_profile_text_terms_and_ignores_positive_restart_count() -> None:
    bundle = {
        "schema_version": "ops-evidence-bundle/v1",
        "evidence_sha256": "e" * 64,
        "profile": {"profile_id": "stream_v3_monitoring", "source_system": "stream_v3_monitoring"},
        "service": "stream_v3_monitoring",
        "environment": "arena_server",
        "window_start": "2026-06-16T22:12:45Z",
        "window_end": "2026-06-17T00:12:45Z",
        "metric_windows": [],
        "operational_evidence": [],
        "log_patterns": [
            {
                "pattern_id": "PATTERN-001",
                "error_type": "youtube_health",
                "message_template": (
                    "watchdog entry_type=watchdog_ok stream_service_substate=running "
                    "runtime_status=running runtime_restart_count=<N> youtube_fail_count=<N>"
                ),
                "example_log": "watchdog_ok runtime_status=running runtime_restart_count=0",
                "count": 91,
                "baseline_count": 409,
                "severity_hint": "low",
            },
            {
                "pattern_id": "PATTERN-012",
                "error_type": "stream_transport",
                "message_template": (
                    "status=incident_candidate cause=rtmps_connect_failure_all_families affected=rtmps"
                ),
                "example_log": "status=incident_candidate cause=rtmps_connect_failure_all_families affected=rtmps",
                "count": 1,
                "baseline_count": 0,
                "severity_hint": "high",
            },
            {
                "pattern_id": "PATTERN-021",
                "error_type": "dependency_timeout",
                "message_template": "Pre-FFmpeg min_wait=<N>.0s mode=restart overlay_timeout=<N>.0s require_overlay_ready=<N>",
                "example_log": "Pre-FFmpeg min_wait=1.0s mode=restart overlay_timeout=30.0s require_overlay_ready=1",
                "count": 1,
                "baseline_count": 0,
                "severity_hint": "low",
            },
        ],
    }

    graph = build_signal_graph(bundle)
    signals = graph["signals"]
    restart_signals = [signal for signal in signals if signal["core_target_type"] == "restart_loop"]
    throughput_signals = [signal for signal in signals if signal["core_target_type"] == "throughput_disappearance"]

    assert restart_signals == []
    assert throughput_signals
    assert throughput_signals[0]["routing_hint"] == "primary_candidate"
    assert throughput_signals[0]["subsystem"] == "rtmps_ffmpeg"
    freshness_signals = [signal for signal in signals if signal["core_target_type"] == "freshness_signal_gap"]
    assert freshness_signals
    assert freshness_signals[0]["subsystem"] == "chromium_capture"


def test_router_adds_rule_engine_propositions_from_candidate_targets() -> None:
    bundle = _stream_bundle()
    graph = build_signal_graph(bundle)
    bundle = {
        **bundle,
        "evidence_signals": graph["signals"],
        "candidate_targets": graph["candidate_targets"],
        "review_graph_seed": graph["review_graph_seed"],
    }

    result = route_claims(bundle, [])

    assert {claim.provider for claim in result.claims} == {"rule-engine"}
    by_core = {
        claim.evidence_identity["core_target_type"]: claim
        for claim in result.claims
    }
    assert by_core["throughput_disappearance"].evidence_refs == ("METRIC-001",)
    assert set(by_core["instrumentation_mismatch"].evidence_refs) == {"OPS-002", "METRIC-009"}
    assert len(result.propositions) == 2
    assert any(
        "metric/log consistency" in proposition.question
        for proposition in result.propositions
    )


def _stream_bundle() -> dict[str, object]:
    return {
        "schema_version": "ops-evidence-bundle/v1",
        "evidence_sha256": "e" * 64,
        "profile": {"profile_id": "stream_v3", "source_system": "stream_v3"},
        "service": "stream_v3",
        "environment": "stream_v3",
        "window_start": "2026-06-15T22:00:00Z",
        "window_end": "2026-06-16T00:00:00Z",
        "metric_windows": [
            {
                "metric_window_id": "METRIC-001",
                "metric_name": "stream_transport_count",
                "baseline_value": 18.0,
                "current_value": 0.0,
                "delta": -18.0,
                "severity_hint": "high",
            },
            {
                "metric_window_id": "METRIC-009",
                "metric_name": "rtmps_reconnect_count",
                "baseline_value": 0.0,
                "current_value": 0.0,
                "delta": 0.0,
                "severity_hint": "low",
            },
        ],
        "log_patterns": [],
        "operational_evidence": [
            {
                "evidence_id": "OPS-002",
                "request_id": "throughput_signal_query",
                "profile_request_id": "rtmps_reconnect_query",
                "request_type": "throughput_signal",
                "need": "throughput_signal",
                "summary": "RTMPS connection, reconnect, send-path, and transport evidence.",
                "subsystem": "rtmps_ffmpeg",
                "incident_count": 480,
                "baseline_count": 0,
                "baseline_daily_average": 0.0,
            }
        ],
        "evidence_refs": {
            "METRIC-001": {
                "type": "metric_window",
                "summary": "stream_transport_count 18.0 -> 0.0",
                "metric_name": "stream_transport_count",
                "baseline_value": 18.0,
                "current_value": 0.0,
            },
            "METRIC-009": {
                "type": "metric_window",
                "summary": "rtmps_reconnect_count 0.0 -> 0.0",
                "metric_name": "rtmps_reconnect_count",
                "baseline_value": 0.0,
                "current_value": 0.0,
            },
            "OPS-002": {
                "type": "operational_evidence",
                "summary": "RTMPS reconnect evidence rows were observed.",
                "incident_count": 480,
                "baseline_count": 0,
                "subsystem": "rtmps_ffmpeg",
                "request_id": "throughput_signal_query",
                "profile_request_id": "rtmps_reconnect_query",
            },
        },
    }
