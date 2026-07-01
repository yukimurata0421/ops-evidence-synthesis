from __future__ import annotations

from ops_evidence_synthesis.models import RawLog
from ops_evidence_synthesis.sanitizer import sanitize_log


def test_sanitizer_redacts_sensitive_values() -> None:
    raw = RawLog.from_mapping(
        {
            "timestamp": "2026-06-12T10:00:00Z",
            "service": "payment-api",
            "environment": "prod",
            "severity": "ERROR",
            "message": (
                "user alice@example.com from 203.0.113.9 "
                "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.secret.payload "
                "api_key=sk-test-secret-1234567890 user_id=u-12345"
            ),
            "labels": {"operator": "bob@example.com"},
        }
    )

    clean = sanitize_log(raw)

    assert "alice@example.com" not in clean.message_sanitized
    assert "203.0.113.9" not in clean.message_sanitized
    assert "sk-test-secret" not in clean.message_sanitized
    assert "u-12345" not in clean.message_sanitized
    assert "<EMAIL>" in clean.message_sanitized
    assert "<IP>" in clean.message_sanitized
    assert clean.error_type == "application_error"
    assert clean.labels_json["operator"] == "<EMAIL>"


def test_sanitizer_redacts_rtmps_stream_key() -> None:
    raw = RawLog.from_mapping(
        {
            "timestamp": "2026-06-15T09:53:57Z",
            "service": "stream-engine",
            "environment": "stream_v3",
            "severity": "ERROR",
            "message": "Error closing file rtmps://a.rtmps.youtube.com:443/live2/c0pr-599v-8t6h-m08w-c9hf",
        }
    )

    clean = sanitize_log(raw)

    assert "c0pr-599v-8t6h-m08w-c9hf" not in clean.message_sanitized
    assert "<STREAM_KEY>" in clean.message_sanitized


def test_sanitizer_redacts_user_home_paths_for_db_ingest() -> None:
    raw = RawLog.from_mapping(
        {
            "timestamp": "2026-06-15T09:53:57Z",
            "service": "notify-worker",
            "environment": "prod",
            "severity": "ERROR",
            "message": (
                "systemd notify.service: /home/yuki/projects/private-app/.venv/bin/python: "
                "can't open file '/Users/alice/private-app/deployment/job.py': "
                "windows path C:\\Users\\alice\\private-app\\runner.ps1"
            ),
            "labels": {"source_path": "/home/yuki/projects/private-app/logs/input.jsonl"},
        }
    )

    clean = sanitize_log(raw)

    assert "/home/yuki" not in clean.message_sanitized
    assert "/Users/alice" not in clean.message_sanitized
    assert "C:\\Users\\alice" not in clean.message_sanitized
    assert "/home/yuki" not in clean.labels_json["source_path"]
    assert "<LOCAL_PATH>/python" in clean.message_sanitized
    assert "<LOCAL_PATH>/job.py" in clean.message_sanitized
    assert "<LOCAL_PATH>/runner.ps1" in clean.message_sanitized
    assert clean.labels_json["source_path"] == "<LOCAL_PATH>/input.jsonl"
    assert clean.error_type == "job_configuration_mismatch"
