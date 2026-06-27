from __future__ import annotations

from pathlib import Path

from ops_evidence_synthesis.bundle import EvidenceBundleBuilder
from ops_evidence_synthesis.ingest import ingest_log_files, load_log_file
from ops_evidence_synthesis.models import IncidentWindow
from ops_evidence_synthesis.storage.sqlite_store import SQLiteStore
from ops_evidence_synthesis.synthesis.pipeline import run_pipeline
from ops_evidence_synthesis.synthesis.subsystems import STREAM_V3_SUBSYSTEMS
from scripts.run_stream_v3_bigquery_aggregate import _compact_labels, _operational_evidence_specs


def test_ingests_stream_v3_jsonl_and_text_logs(tmp_path: Path) -> None:
    jsonl = tmp_path / "fast_recovery_events_tail.jsonl"
    jsonl.write_text(
        "\n".join(
            [
                (
                    '{"ts_utc":"2026-06-15T09:49:47Z","kind":"tcp_send_sample",'
                    '"message":"ffmpeg tcp send sample","stream_service":"adsb-streamnew-youtube-stream.service",'
                    '"mbps":4.74,"notsent":622,"unacked":5,"lastsnd_ms":0,'
                    '"conn":"ESTAB 0 6004 10.42.0.241:51728 192.178.230.134:443"}'
                ),
                (
                    '{"ts_utc":"2026-06-15T09:50:49Z","kind":"tcp_send_sample",'
                    '"message":"ffmpeg tcp send sample","stream_service":"adsb-streamnew-youtube-stream.service",'
                    '"mbps":4.75,"notsent":0,"unacked":0,"lastsnd_ms":73}'
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    text_log = tmp_path / "stream-engine_tail.log"
    text_log.write_text(
        "[stream-v3-runtime] starting PulseAudio on unix:/run/stream-pulse/native\n"
        "[2026-06-15 09:51:00] Overlay ready probe passed\n"
        "2026-06-15 09:52:00,123 [INFO] Track finished rc=0\n",
        encoding="utf-8",
    )
    store = SQLiteStore(tmp_path / "oes.sqlite3")

    assert ingest_log_files([jsonl, text_log], store) == 5

    bundle = EvidenceBundleBuilder(store).build(
        IncidentWindow(
            service="adsb-streamnew-youtube-stream.service",
            environment="stream_v3",
            incident_start="2026-06-15T09:49:00Z",
            incident_end="2026-06-15T09:53:00Z",
            lookback_minutes=5,
        )
    )

    assert bundle["logs"]
    assert any(log["error_type"] == "stream_transport" for log in bundle["logs"])
    assert "10.42.0.241" not in bundle["logs"][0]["message_sanitized"]


def test_non_object_json_lines_are_ingested_as_text(tmp_path: Path) -> None:
    text_bundle = tmp_path / "arena_state_bundle.txt"
    text_bundle.write_text(
        '{\n'
        '  "reasons": [\n'
        '    "swap used above watch floor"\n'
        '  ],\n'
        '  "ok": true\n'
        '}\n',
        encoding="utf-8",
    )

    rows = load_log_file(text_bundle)

    assert len(rows) == 6
    assert any(row.labels.get("json_parse_error") == "json_row_not_object" for row in rows)
    assert any("swap used above watch floor" in row.message for row in rows)


def test_ingest_can_force_case_service_and_environment(tmp_path: Path) -> None:
    jsonl = tmp_path / "fast_recovery_events_tail.jsonl"
    jsonl.write_text(
        (
            '{"ts_utc":"2026-06-15T09:49:47Z","kind":"tcp_send_sample",'
            '"message":"ffmpeg tcp send sample","stream_service":"adsb-streamnew-youtube-stream.service",'
            '"mbps":4.74,"notsent":622,"unacked":5,"lastsnd_ms":0}\n'
        ),
        encoding="utf-8",
    )
    store = SQLiteStore(tmp_path / "oes.sqlite3")

    assert ingest_log_files(
        [jsonl],
        store,
        service="stream_v3_monitoring",
        environment="arena_server",
    ) == 1

    logs = store.fetch_logs(
        "stream_v3_monitoring",
        "arena_server",
        "2026-06-15T09:49:00Z",
        "2026-06-15T09:50:00Z",
    )

    assert len(logs) == 1
    assert logs[0].labels_json["original_service"] == "adsb-streamnew-youtube-stream.service"
    assert logs[0].labels_json["original_environment"] == "prod"


def test_stream_v3_pipeline_reaches_local_agents(tmp_path: Path) -> None:
    jsonl = tmp_path / "fast_recovery_events_tail.jsonl"
    jsonl.write_text(
        (
            '{"ts_utc":"2026-06-15T09:49:47Z","kind":"tcp_send_sample",'
            '"message":"ffmpeg tcp send sample","stream_service":"adsb-streamnew-youtube-stream.service",'
            '"mbps":4.74,"notsent":622,"unacked":5,"lastsnd_ms":0}\n'
            '{"ts_utc":"2026-06-15T09:50:49Z","kind":"watchdog",'
            '"message":"youtube watchdog live evidence degraded","stream_service":"adsb-streamnew-youtube-stream.service",'
            '"status":"degraded","healthy":false,"failure_kind":"youtube_probe"}\n'
        ),
        encoding="utf-8",
    )
    store = SQLiteStore(tmp_path / "oes.sqlite3")
    ingest_log_files([jsonl], store)

    result = run_pipeline(
        store,
        IncidentWindow(
            service="adsb-streamnew-youtube-stream.service",
            environment="stream_v3",
            incident_start="2026-06-15T09:49:00Z",
            incident_end="2026-06-15T09:52:00Z",
            lookback_minutes=5,
        ),
    )

    assert result.model_run_count == 3
    assert result.parsed_result_count == 3
    assert result.claim_count > 0
    assert result.review_queue_count > 0
    queue = store.list_review_queue(evidence_sha256=result.evidence_sha256)
    assert all(item["subsystem"] in STREAM_V3_SUBSYSTEMS for item in queue)
    assert all("structured_evidence" in item for item in queue)


def test_stream_v3_aggregate_defines_operational_evidence_requests() -> None:
    specs = {spec["request_id"]: spec for spec in _operational_evidence_specs()}

    assert specs["process_state_query"]["profile_request_id"] == "ffmpeg_state_query"
    assert specs["throughput_signal_query"]["profile_request_id"] == "rtmps_reconnect_query"
    assert specs["external_dependency_status_query"]["profile_request_id"] == "youtube_ingest_status_query"
    assert specs["user_impact_signal_query"]["profile_request_id"] == "audio_energy_gap_query"
    assert specs["freshness_signal_query"]["profile_request_id"] == "capture_freshness_query"
    assert specs["network_path_query"]["profile_request_id"] == "network_reset_by_destination_query"
    assert specs["state_transition_query"]["profile_request_id"] == "stream_service_substate_query"
    assert specs["process_state_query"]["subsystem"] == "rtmps_ffmpeg"


def test_stream_v3_aggregate_labels_do_not_expose_local_source_paths() -> None:
    labels = _compact_labels(
        {
            "source_path": "/home/example/projects/stream_v3/.state/wan-observer/logs/example.jsonl",
            "source_line": 42,
            "kind": "tcp_send_sample",
        }
    )

    assert labels["source_path"].startswith("sanitized://example.jsonl#")
    assert "/home/" not in labels["source_path"]
    assert labels["source_line"] == 42
    assert labels["kind"] == "tcp_send_sample"
