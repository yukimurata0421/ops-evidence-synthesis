from __future__ import annotations

import json
from dataclasses import asdict

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


def test_sanitizer_property_redacts_generated_sensitive_log_fragments() -> None:
    cases = [
        {
            "user": "alice",
            "email": "alice.ops@example.com",
            "ip": "203.0.113.10",
            "secret": "sk-proj-alpha-secret-0000000001",
            "home": "/home/alice/private-service/config.yaml",
            "windows": "C:\\Users\\alice\\private-service\\runner.ps1",
        },
        {
            "user": "bob",
            "email": "bob.ops@example.net",
            "ip": "198.51.100.23",
            "secret": "ghp_beta_secret_0000000002",
            "home": "/Users/bob/projects/private-service/.env",
            "windows": "C:\\Users\\bob\\projects\\private-service\\job.py",
        },
        {
            "user": "carol",
            "email": "carol.ops@example.org",
            "ip": "192.0.2.44",
            "secret": "xoxb-gamma-secret-0000000003",
            "home": "/home/carol/work/stream_v3/secrets.json",
            "windows": "C:\\Users\\carol\\work\\stream_v3\\secrets.json",
        },
    ]

    for index, case in enumerate(cases, start=1):
        raw = RawLog.from_mapping(
            {
                "timestamp": f"2026-06-15T09:5{index}:57Z",
                "service": "stream-engine",
                "environment": "prod",
                "severity": "ERROR",
                "message": (
                    f"user={case['email']} ip={case['ip']} token={case['secret']} "
                    f"path={case['home']} windows={case['windows']} "
                    "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.payload.signature"
                ),
                "labels": {
                    "operator": case["email"],
                    "source_path": case["home"],
                    "api_key": case["secret"],
                },
            }
        )

        payload_text = json.dumps(asdict(sanitize_log(raw)), sort_keys=True)

        assert case["email"] not in payload_text
        assert case["ip"] not in payload_text
        assert case["secret"] not in payload_text
        assert case["home"] not in payload_text
        assert case["windows"] not in payload_text
        assert "eyJhbGciOiJIUzI1NiJ9.payload.signature" not in payload_text
        assert "<EMAIL>" in payload_text
        assert "<IP>" in payload_text
        assert "<SECRET>" in payload_text
        assert "<LOCAL_PATH>" in payload_text
        assert "<AUTH_HEADER>" in payload_text
