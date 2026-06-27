from __future__ import annotations

from pathlib import Path

from ops_evidence_synthesis.bundle import EvidenceBundleBuilder
from ops_evidence_synthesis.ingest import ingest_jsonl
from ops_evidence_synthesis.models import IncidentWindow
from ops_evidence_synthesis.storage.sqlite_store import SQLiteStore


ROOT = Path(__file__).resolve().parents[1]


def test_bundle_sha_is_stable_for_same_evidence(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "oes.sqlite3")
    ingest_jsonl(ROOT / "data/sample_logs.jsonl", store)
    incident = IncidentWindow(
        service="payment-api",
        environment="prod",
        incident_start="2026-06-12T10:00:00Z",
        incident_end="2026-06-12T10:20:00Z",
        lookback_minutes=45,
    )

    first = EvidenceBundleBuilder(store).build(incident)
    second = EvidenceBundleBuilder(store).build(incident)

    assert first["evidence_sha256"] == second["evidence_sha256"]
    assert first["logs"][0]["evidence_id"] == "LOG-001"
    assert "PATTERN-001" in first["evidence_refs"]
    assert any(pattern["error_type"] == "connection_pool_exhausted" for pattern in first["log_patterns"])
    assert all(pattern["aggregation_source"] == "sqlite_group_by" for pattern in first["log_patterns"])
    assert all("example_log" not in pattern for pattern in first["log_patterns"])
    pattern_ref = first["evidence_refs"]["PATTERN-001"]
    assert pattern_ref["baseline_count"] >= 0
    assert pattern_ref["first_seen"]
    assert pattern_ref["last_seen"]
    assert first["sanitizer_version"]
    assert first["profile"]["profile_id"] == "generic"
    assert first["normalized_events"]
    assert {"timestamp", "source_system", "service", "environment", "severity", "message_sanitized", "labels"} <= set(first["normalized_events"][0])


def test_stream_v3_monitoring_bundle_adds_operational_evidence(tmp_path: Path) -> None:
    jsonl = tmp_path / "arena_server_stream_v3_monitoring.jsonl"
    jsonl.write_text(
        "\n".join(
            [
                (
                    '{"timestamp":"2026-06-16T23:00:00Z","service":"stream_v3_monitoring",'
                    '"environment":"arena_server","severity":"INFO","resource_type":"prometheus_stream_v3_exporter",'
                    '"message":"metric stream_v3_runtime_ffmpeg_present=1.0",'
                    '"labels":{"source_name":"prometheus_stream_v3_exporter","metric_name":"stream_v3_runtime_ffmpeg_present","metric_value":"1.0"}}'
                ),
                (
                    '{"timestamp":"2026-06-16T23:00:01Z","service":"stream_v3_monitoring",'
                    '"environment":"arena_server","severity":"INFO","resource_type":"prometheus_stream_v3_exporter",'
                    '"message":"metric stream_v3_youtube_ingest_connected=1.0",'
                    '"labels":{"source_name":"prometheus_stream_v3_exporter","metric_name":"stream_v3_youtube_ingest_connected","metric_value":"1.0"}}'
                ),
                (
                    '{"timestamp":"2026-06-16T23:00:02Z","service":"stream_v3_monitoring",'
                    '"environment":"arena_server","severity":"INFO","resource_type":"youtube_watchdog_stats",'
                    '"message":"youtube_watchdog status=ok healthy=True local_ok=True public_ok=True api_ok=True oauth_ok=True ingest_connected=True stream_active=True fail_count=0 judgment=ok",'
                    '"labels":{"source_name":"youtube_watchdog_stats","status":"ok","healthy":true,"fail_count":0}}'
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    store = SQLiteStore(tmp_path / "oes.sqlite3")
    ingest_jsonl(jsonl, store)

    bundle = EvidenceBundleBuilder(store).build(
        IncidentWindow(
            service="stream_v3_monitoring",
            environment="arena_server",
            incident_start="2026-06-16T22:50:00Z",
            incident_end="2026-06-16T23:10:00Z",
            lookback_minutes=60,
        )
    )

    assert bundle["profile"]["profile_id"] == "stream_v3_monitoring"
    assert bundle["incident_window"] == {"start": "2026-06-16T22:50:00Z", "end": "2026-06-16T23:10:00Z"}
    assert "OPS-001" in bundle["evidence_refs"]
    assert "OPS-003" in bundle["evidence_refs"]
    ffmpeg = next(item for item in bundle["operational_evidence"] if item["evidence_id"] == "OPS-001")
    youtube = next(item for item in bundle["operational_evidence"] if item["evidence_id"] == "OPS-003")
    assert ffmpeg["observations"][0]["metric_name"] == "stream_v3_runtime_ffmpeg_present"
    assert ffmpeg["observations"][0]["assessment"] == "healthy"
    assert any(row["observed_value"].get("ingest_connected") is True for row in youtube["observations"] if row["kind"] == "latest_status")
