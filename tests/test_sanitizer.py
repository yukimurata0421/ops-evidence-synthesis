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
