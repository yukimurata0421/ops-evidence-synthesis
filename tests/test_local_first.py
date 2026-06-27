from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

from ops_evidence_synthesis.ai.prompts import compact_bundle_for_model, root_cause_prompt
from ops_evidence_synthesis.evidence_rules import ai_evidence_rules
from ops_evidence_synthesis.local_first import (
    build_bundle_from_sanitized,
    inspect_input,
    sanitize_input,
    verify_sanitized_output,
)


ROOT = Path(__file__).resolve().parents[1]


def _write_log(path: Path) -> None:
    path.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "timestamp": "2026-06-16T01:00:00Z",
                        "service": "sample",
                        "environment": "prod",
                        "severity": "ERROR",
                        "component": "worker",
                        "message": (
                            "can't open file /home/example/project/foo.py: No such file or directory "
                            "Authorization: Bearer raw-secret-token-1234567890 "
                            "api_key=sk-test-rawsecret1234567890 user_id=u-12345 "
                            "email=alice@example.com client_ip=203.0.113.10"
                        ),
                    },
                    sort_keys=False,
                ),
                json.dumps(
                    {
                        "timestamp": "2026-06-16T01:01:00Z",
                        "service": "sample",
                        "environment": "prod",
                        "severity": "ERROR",
                        "component": "systemd",
                        "message": "sample.service: Main process exited, code=exited, status=203/EXEC; Failed with result 'exit-code'.",
                    },
                    sort_keys=False,
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )


def _read_jsonl(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _build_bundle(output_dir: Path, *, profile_name: str = "generic") -> dict[str, object]:
    return build_bundle_from_sanitized(
        output_dir / "sanitized_events.jsonl",
        service="unknown-sample",
        environment="prod",
        start="2026-06-16T00:00:00Z",
        end="2026-06-16T18:00:00Z",
        profile_name=profile_name,
        out_path=output_dir / "evidence_bundle.json",
    )


def test_sanitize_redacts_and_normalizes_events(tmp_path: Path) -> None:
    raw = tmp_path / "raw.jsonl"
    _write_log(raw)

    result = sanitize_input(raw, tmp_path / "out")
    events = _read_jsonl(Path(result["sanitized_events"]))
    report = json.loads(Path(result["redaction_report"]).read_text(encoding="utf-8"))
    manifest = json.loads(Path(result["manifest"]).read_text(encoding="utf-8"))
    serialized_events = json.dumps(events, sort_keys=True)
    serialized_report = json.dumps(report, sort_keys=True)

    assert "raw-secret-token" not in serialized_events
    assert "sk-test-rawsecret" not in serialized_events
    assert "alice@example.com" not in serialized_events
    assert "203.0.113.10" not in serialized_events
    assert "<REDACTED_SECRET>" in events[0]["message_sanitized"]
    assert re.search(r"<EMAIL_HASH:[0-9a-f]{12}>", str(events[0]["message_sanitized"]))
    assert re.search(r"<IP_HASH:[0-9a-f]{12}>", str(events[0]["message_sanitized"]))
    assert events[0]["event_type"] in {"missing_file", "missing_command"}
    assert events[1]["event_type"] == "service_start_failure"
    assert "raw-secret-token" not in serialized_report
    assert "sk-test-rawsecret" not in serialized_report
    assert manifest["raw_log_policy"] == "not_uploaded"

    required = {
        "event_id",
        "service",
        "environment",
        "severity_text",
        "message_sanitized",
        "attributes",
        "sanitizer_version",
    }
    assert required <= set(events[0])
    assert events[0].get("timestamp") or events[0].get("observed_timestamp")


def test_inspect_summarizes_without_writing(tmp_path: Path) -> None:
    raw = tmp_path / "raw.jsonl"
    _write_log(raw)

    summary = inspect_input(raw)

    assert summary["detected_format"] == "jsonl"
    assert summary["sensitive_candidates_count"] >= 4
    assert "timestamp" in summary["timestamp_field_candidates"]
    assert summary["suggested_system_type"] in {"systemd_service", "generic"}


def test_evidence_bundle_sha_is_stable_and_ignores_json_key_order(tmp_path: Path) -> None:
    raw_one = tmp_path / "one.jsonl"
    raw_two = tmp_path / "two.jsonl"
    event_a = {
        "timestamp": "2026-06-16T01:00:00Z",
        "service": "sample",
        "environment": "prod",
        "severity": "ERROR",
        "component": "worker",
        "message": "No such file or directory user_id=u-12345",
    }
    event_b = {
        "message": event_a["message"],
        "component": event_a["component"],
        "severity": event_a["severity"],
        "environment": event_a["environment"],
        "service": event_a["service"],
        "timestamp": event_a["timestamp"],
    }
    raw_one.write_text(json.dumps(event_a, sort_keys=False) + "\n", encoding="utf-8")
    raw_two.write_text(json.dumps(event_b, sort_keys=False) + "\n", encoding="utf-8")
    sanitize_input(raw_one, tmp_path / "out1")
    sanitize_input(raw_two, tmp_path / "out2")

    first = build_bundle_from_sanitized(
        tmp_path / "out1" / "sanitized_events.jsonl",
        service="sample",
        environment="prod",
        start="2026-06-16T00:00:00Z",
        end="2026-06-16T18:00:00Z",
        profile_name="generic",
        out_path=tmp_path / "bundle1.json",
    )
    second = build_bundle_from_sanitized(
        tmp_path / "out1" / "sanitized_events.jsonl",
        service="sample",
        environment="prod",
        start="2026-06-16T00:00:00Z",
        end="2026-06-16T18:00:00Z",
        profile_name="generic",
        out_path=tmp_path / "bundle2.json",
    )
    reordered = build_bundle_from_sanitized(
        tmp_path / "out2" / "sanitized_events.jsonl",
        service="sample",
        environment="prod",
        start="2026-06-16T00:00:00Z",
        end="2026-06-16T18:00:00Z",
        profile_name="generic",
        out_path=tmp_path / "bundle3.json",
    )

    assert first["evidence_sha256"] == second["evidence_sha256"]
    assert first["evidence_sha256"] == reordered["evidence_sha256"]
    assert first["canonicalization_version"] == "canonical_json.v1"
    spaced = tmp_path / "spaced.jsonl"
    spaced.write_text(' {  "message" : "No such file or directory user_id=u-12345" , "component" : "worker" , "severity" : "ERROR" , "environment" : "prod" , "service" : "sample" , "timestamp" : "2026-06-16T01:00:00Z" } \n', encoding="utf-8")
    sanitize_input(spaced, tmp_path / "out3")
    whitespace = build_bundle_from_sanitized(
        tmp_path / "out3" / "sanitized_events.jsonl",
        service="sample",
        environment="prod",
        start="2026-06-16T00:00:00Z",
        end="2026-06-16T18:00:00Z",
        profile_name="generic",
        out_path=tmp_path / "bundle4.json",
    )
    assert first["evidence_sha256"] == whitespace["evidence_sha256"]


def test_profile_modes_and_analysis_policy(tmp_path: Path) -> None:
    raw = tmp_path / "raw.jsonl"
    _write_log(raw)
    sanitize_input(raw, tmp_path / "out")
    sanitized = tmp_path / "out" / "sanitized_events.jsonl"

    unknown = build_bundle_from_sanitized(
        sanitized,
        service="sample",
        environment="prod",
        start="2026-06-16T00:00:00Z",
        end="2026-06-16T18:00:00Z",
        profile_name="does-not-exist",
        out_path=tmp_path / "unknown.json",
    )
    generic = build_bundle_from_sanitized(
        sanitized,
        service="sample",
        environment="prod",
        start="2026-06-16T00:00:00Z",
        end="2026-06-16T18:00:00Z",
        profile_name="generic",
        out_path=tmp_path / "generic.json",
    )
    explicit = build_bundle_from_sanitized(
        sanitized,
        service="sample",
        environment="prod",
        start="2026-06-16T00:00:00Z",
        end="2026-06-16T18:00:00Z",
        profile_name="stream_v3_monitoring",
        out_path=tmp_path / "explicit.json",
    )

    assert unknown["source"]["profile_confidence"] == "unknown"
    assert unknown["required_profile_questions"]
    assert unknown["analysis_policy"] == {
        "profile_mode": "unknown",
        "explicit_profile": False,
        "allow_primary_candidate": False,
        "prefer_generic_signals": True,
        "require_profile_questions": True,
    }
    assert generic["source"]["profile_confidence"] == "inferred"
    assert generic["analysis_policy"]["profile_mode"] == "inferred"
    assert generic["analysis_policy"]["explicit_profile"] is False
    assert generic["analysis_policy"]["allow_primary_candidate"] is False
    assert "Which logs indicate user impact rather than diagnostic noise?" in generic["required_profile_questions"]
    assert generic["system_profile"]
    assert generic["metric_semantics"]
    assert generic["evidence_items"][0]["evidence_id"] == "PATTERN-001"
    assert generic["signals"][0]["signal_id"] == "SIG-001"
    assert generic["prompt_rules"] == ai_evidence_rules()
    assert explicit["source"]["profile_confidence"] == "explicit"
    assert explicit["analysis_policy"] == {
        "profile_mode": "explicit",
        "explicit_profile": True,
        "allow_primary_candidate": True,
        "prefer_generic_signals": False,
        "require_profile_questions": False,
    }
    assert explicit["required_profile_questions"] == []


def test_generic_without_inference_stays_unknown(tmp_path: Path) -> None:
    raw = tmp_path / "unknown.jsonl"
    raw.write_text(
        json.dumps(
            {
                "timestamp": "2026-06-16T01:00:00Z",
                "service": "sample",
                "environment": "prod",
                "severity": "INFO",
                "component": "worker",
                "message": "plain diagnostic line without recognizable system type",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    sanitize_input(raw, tmp_path / "out")
    bundle = build_bundle_from_sanitized(
        tmp_path / "out" / "sanitized_events.jsonl",
        service="sample",
        environment="prod",
        start="2026-06-16T00:00:00Z",
        end="2026-06-16T18:00:00Z",
        profile_name="generic",
        out_path=tmp_path / "bundle.json",
    )

    assert bundle["source"]["profile_confidence"] == "unknown"
    assert bundle["analysis_policy"]["profile_mode"] == "unknown"
    assert bundle["analysis_policy"]["allow_primary_candidate"] is False


def test_profile_context_is_redacted_before_bundle_verify(tmp_path: Path) -> None:
    raw = tmp_path / "raw.jsonl"
    _write_log(raw)
    output = tmp_path / "out"
    sanitize_input(raw, output)

    bundle = build_bundle_from_sanitized(
        output / "sanitized_events.jsonl",
        service="stream_v3_monitoring",
        environment="arena_server",
        start="2026-06-16T00:00:00Z",
        end="2026-06-16T18:00:00Z",
        profile_name="stream_v3_monitoring",
        out_path=output / "evidence_bundle.json",
    )
    serialized = json.dumps(bundle, sort_keys=True)

    assert "127.0.0.1" not in serialized
    assert "http://127" not in serialized
    assert "<URL_HASH:" in serialized
    verification = verify_sanitized_output(output)
    assert verification["passed"], verification


def test_redaction_fixture_sanitizes_verifies_and_hashes_stably(tmp_path: Path) -> None:
    raw = ROOT / "sample_logs" / "redaction_fixture.jsonl"
    first_out = tmp_path / "first"
    second_out = tmp_path / "second"

    sanitize_input(raw, first_out)
    first_bundle = _build_bundle(first_out)
    sanitize_input(raw, second_out)
    second_bundle = _build_bundle(second_out)

    for output in (first_out, second_out):
        combined = "\n".join(
            (output / name).read_text(encoding="utf-8")
            for name in (
                "sanitized_events.jsonl",
                "manifest.json",
                "redaction_report.json",
                "rejected_lines.jsonl",
                "evidence_bundle.json",
            )
        )
        assert "Authorization:" not in combined
        assert "Bearer " not in combined
        assert "Cookie:" not in combined
        assert "password=" not in combined
        assert "access_token" not in combined
        assert "refresh_token" not in combined
        assert "fakeBearerToken" not in combined
        assert "fake-password" not in combined
        assert "ops@example.test" not in combined
        assert "203.0.113.99" not in combined
        assert "-----BEGIN PRIVATE KEY-----" not in combined
        verification = verify_sanitized_output(output)
        assert verification["passed"], verification

    assert first_bundle["evidence_sha256"] == second_bundle["evidence_sha256"]
    assert first_bundle["schema_version"] == "evidence_bundle.v1"
    assert first_bundle["bundle_type"] == "sanitized_evidence_bundle"
    assert first_bundle["raw_log_policy"] == "not_uploaded"
    assert first_bundle["source"]["service"] == "unknown-sample"
    assert first_bundle["source"]["profile_confidence"] == "inferred"
    assert first_bundle["local_first_summary"]["raw_logs_uploaded"] is False
    assert first_bundle["local_first_summary"]["raw_log_policy"] == "not_uploaded"
    assert first_bundle["local_first_summary"]["evidence_sha256"] == first_bundle["evidence_sha256"]
    assert first_bundle["display_summary"]["primary_badges"] == [
        "raw_log_policy:not_uploaded",
        "verify_sanitized:passed",
        "profile_confidence:inferred",
    ]
    assert first_bundle["analysis_policy"]["profile_mode"] == "inferred"
    assert first_bundle["analysis_policy"]["explicit_profile"] is False
    assert first_bundle["analysis_policy"]["allow_primary_candidate"] is False
    assert first_bundle["prompt_rules"] == ai_evidence_rules()
    assert {signal["signal_type"] for signal in first_bundle["signals"]} >= {
        "missing_command",
        "connection_reset",
        "monitoring_gap",
    }
    compact = compact_bundle_for_model(first_bundle)
    assert compact["prompt_rules"] == ai_evidence_rules()
    assert compact["source"]["service"] == "unknown-sample"
    prompt = root_cause_prompt(first_bundle)
    assert "If profile_confidence is unknown or inferred" in prompt
    assert "Score is review priority, not truth probability." in prompt


def test_verify_sanitized_cli_passes_and_fails_without_printing_secret(tmp_path: Path) -> None:
    raw = ROOT / "sample_logs" / "redaction_fixture.jsonl"
    output = tmp_path / "out"
    sanitize_input(raw, output)
    _build_bundle(output)

    env = {**os.environ, "PYTHONPATH": str(ROOT / "src")}
    passed = subprocess.run(
        [sys.executable, "-m", "ops_evidence_synthesis.cli", "verify-sanitized", str(output)],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )
    assert passed.returncode == 0
    assert "Sanitized output verification: passed" in passed.stdout

    leaked_secret = "leaked-super-secret-token-123456"
    (output / "evidence_bundle.json").write_text(
        json.dumps({"raw_log_policy": "not_uploaded", "leak": f"Authorization: Bearer {leaked_secret}"}) + "\n",
        encoding="utf-8",
    )
    failed = subprocess.run(
        [sys.executable, "-m", "ops_evidence_synthesis.cli", "verify-sanitized", str(output)],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )
    assert failed.returncode != 0
    assert "Sanitized output verification: failed" in failed.stdout
    assert "evidence_bundle.json: secret_like pattern remained" in failed.stdout
    assert leaked_secret not in failed.stdout


def test_legacy_analyze_jsonl_still_runs(tmp_path: Path) -> None:
    env = {**os.environ, "PYTHONPATH": str(ROOT / "src")}
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "ops_evidence_synthesis.cli",
            "--db",
            str(tmp_path / "legacy.sqlite3"),
            "analyze-jsonl",
            "--input",
            str(ROOT / "data" / "sample_logs.jsonl"),
            "--service",
            "payment-api",
            "--environment",
            "prod",
            "--start",
            "2026-06-12T10:00:00Z",
            "--end",
            "2026-06-12T10:20:00Z",
        ],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )

    assert result.returncode == 0, result.stderr
    assert "ingested_logs=20" in result.stdout
    assert "evidence_sha256=" in result.stdout


def test_run_case_selects_log_files_and_skips_mp3(tmp_path: Path) -> None:
    input_dir = tmp_path / "input"
    input_dir.mkdir()
    (input_dir / "sample_logs.jsonl").write_text(
        (ROOT / "data" / "sample_logs.jsonl").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    (input_dir / "recording.mp3").write_bytes(b"not a log")
    env = {**os.environ, "PYTHONPATH": str(ROOT / "src")}

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "ops_evidence_synthesis.cli",
            "--db",
            str(tmp_path / "product.sqlite3"),
            "run-case",
            "--input",
            str(input_dir),
            "--service",
            "payment-api",
            "--environment",
            "prod",
            "--start",
            "2026-06-12T10:00:00Z",
            "--end",
            "2026-06-12T10:20:00Z",
            "--provider",
            "local",
            "--review-base-url",
            "http://127.0.0.1:8084",
        ],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )

    assert result.returncode == 0, result.stderr
    assert "selected_input_files=1" in result.stdout
    assert "skipped_input_files=1" in result.stdout
    assert "ingested_logs=20" in result.stdout
    assert "review_url=http://127.0.0.1:8084/?evidence_sha256=" in result.stdout
    assert "serve_command=ops-evidence --db" in result.stdout
