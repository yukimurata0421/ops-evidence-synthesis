from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from ops_evidence_synthesis.local_first import build_bundle_from_sanitized, sanitize_input, verify_sanitized_output
from ops_evidence_synthesis.profile_discovery import discover_profile
from ops_evidence_synthesis.source_context import (
    analyze_source_context,
    sanitize_source,
    validate_source_analysis_bundle_for_upload,
    validate_source_context_bundle_for_upload,
)
from ops_evidence_synthesis.synthesis.multi_ai import run_multi_ai


ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = ROOT / "sample_projects" / "profile_discovery_sample"


def _source_context_and_analysis(tmp_path: Path) -> tuple[dict[str, object], dict[str, object], Path, Path]:
    source_out = tmp_path / "source_context"
    sanitize_source(
        PROJECT_ROOT,
        service="unknown-sample",
        environment="prod",
        output_dir=source_out,
    )
    source_context = json.loads((source_out / "source_context_bundle.json").read_text(encoding="utf-8"))
    analysis_out = tmp_path / "source_analysis"
    analyze_source_context(
        source_out / "source_context_bundle.json",
        provider="local",
        output_dir=analysis_out,
    )
    source_analysis = json.loads((analysis_out / "source_analysis_bundle.json").read_text(encoding="utf-8"))
    return source_context, source_analysis, source_out, analysis_out


def _evidence_bundle(tmp_path: Path) -> dict[str, object]:
    out = tmp_path / "local_first"
    sanitize_input(ROOT / "sample_logs" / "redaction_fixture.jsonl", out)
    return build_bundle_from_sanitized(
        out / "sanitized_events.jsonl",
        service="unknown-sample",
        environment="prod",
        start="2026-06-16T00:00:00Z",
        end="2026-06-16T18:00:00Z",
        profile_name="generic",
        out_path=out / "evidence_bundle.json",
    )


def test_sanitize_source_generates_safe_source_context_bundle(tmp_path: Path) -> None:
    source_context, _source_analysis, source_out, _analysis_out = _source_context_and_analysis(tmp_path)
    serialized = json.dumps(source_context, ensure_ascii=False, sort_keys=True)
    raw_source = (PROJECT_ROOT / "src" / "watchdog_restart_main.py").read_text(encoding="utf-8")

    assert (source_out / "source_context_bundle.json").exists()
    assert (source_out / "source_context_report.md").exists()
    assert (source_out / "redaction_report.json").exists()
    assert source_context["schema_version"] == "source_context_bundle.v1"
    assert source_context["bundle_type"] == "sanitized_source_context_bundle"
    assert source_context["raw_source_policy"] == "not_uploaded"
    assert source_context["raw_env_policy"] == "not_uploaded"
    assert source_context["sanitization_policy"]["raw_source_uploaded"] is False
    assert source_context["sanitization_policy"]["raw_env_values_uploaded"] is False
    assert raw_source not in serialized
    assert "fake-gmail-token-for-tests-only" not in serialized
    assert "fake-credentials" not in serialized
    assert "discord.example.invalid/webhook/fake" not in serialized
    assert "GOOGLE_APPLICATION_CREDENTIALS" not in serialized
    assert "GMAIL_TOKEN" not in serialized
    assert validate_source_context_bundle_for_upload(source_context)["passed"] is True
    assert verify_sanitized_output(source_out)["passed"] is True

    env_rows = source_context["env_key_summaries"]
    assert env_rows
    assert all("raw_value" not in row for row in env_rows)
    assert all(row["raw_value_uploaded"] is False for row in env_rows)
    assert any(row["secret_like"] is True and row["key_name"] == "" for row in env_rows)
    assert source_context["systemd_units"][0]["exec_start_template"]
    assert "<USER_HOME>" in source_context["systemd_units"][0]["exec_start_template"]
    assert source_context["version_context"]["file_mtime_summary"]["file_count"] >= 1
    assert source_context["version_context"]["deployed_version_confirmed"] is False
    assert "Source context may not match" in source_context["version_context"]["caveat"]


def test_analyze_source_generates_safe_source_analysis_bundle(tmp_path: Path) -> None:
    source_context, source_analysis, _source_out, analysis_out = _source_context_and_analysis(tmp_path)
    assert (analysis_out / "source_analysis_bundle.json").exists()
    assert (analysis_out / "source_analysis_report.md").exists()
    assert source_analysis["schema_version"] == "source_analysis_bundle.v1"
    assert source_analysis["bundle_type"] == "sanitized_source_analysis_bundle"
    assert source_analysis["source_context_sha256"] == source_context["source_context_sha256"]
    assert source_analysis["component_candidates"]
    assert source_analysis["metric_semantics_candidates"]
    assert all(row["human_review_required"] is True for row in source_analysis["component_candidates"])
    assert all(row["human_review_required"] is True for row in source_analysis["metric_semantics_candidates"])
    assert source_analysis["collector_mapping_candidates"]
    assert all(row["safety_level"] == "read_only" for row in source_analysis["collector_mapping_candidates"])
    assert validate_source_analysis_bundle_for_upload(source_analysis)["passed"] is True
    assert verify_sanitized_output(analysis_out)["passed"] is True


def test_source_first_cli_discover_profile_and_multi_ai_context(tmp_path: Path) -> None:
    source_out = tmp_path / "cli_source"
    analysis_out = tmp_path / "cli_analysis"
    subprocess.run(
        [
            sys.executable,
            "-m",
            "ops_evidence_synthesis.cli",
            "sanitize-source",
            "--project-root",
            str(PROJECT_ROOT),
            "--service",
            "unknown-sample",
            "--environment",
            "prod",
            "--out",
            str(source_out),
        ],
        cwd=ROOT,
        check=True,
    )
    subprocess.run(
        [
            sys.executable,
            "-m",
            "ops_evidence_synthesis.cli",
            "analyze-source",
            "--source-context",
            str(source_out / "source_context_bundle.json"),
            "--provider",
            "local",
            "--out",
            str(analysis_out),
        ],
        cwd=ROOT,
        check=True,
    )
    evidence = _evidence_bundle(tmp_path)
    discovery_out = tmp_path / "source_first_discovery"
    discover_profile(
        None,
        source_context_path=source_out / "source_context_bundle.json",
        source_analysis_path=analysis_out / "source_analysis_bundle.json",
        evidence_bundle_path=tmp_path / "local_first" / "evidence_bundle.json",
        service="unknown-sample",
        environment="prod",
        output_dir=discovery_out,
    )
    discovery = json.loads((discovery_out / "profile_discovery_bundle.json").read_text(encoding="utf-8"))
    assert discovery["discovery_policy"]["mode"] == "source_first_sanitized_context"
    assert discovery["source"]["source_context_sha256"]
    assert discovery["source"]["source_analysis_sha256"]
    assert discovery["source_context_summary"]["context_is_not_incident_evidence"] is True
    assert discovery["component_candidates"]
    assert discovery["metric_semantics_candidates"]
    assert verify_sanitized_output(discovery_out)["passed"] is True

    source_context = json.loads((source_out / "source_context_bundle.json").read_text(encoding="utf-8"))
    source_analysis = json.loads((analysis_out / "source_analysis_bundle.json").read_text(encoding="utf-8"))
    result = run_multi_ai(
        evidence,
        {},
        providers=["local-gemini"],
        mode="local",
        output_dir=tmp_path / "multi_ai",
        source_context=source_context,
        source_analysis=source_analysis,
    )
    assert result["context_inputs"]["source_context_included"] is True
    assert result["context_inputs"]["source_analysis_included"] is True
    assert result["context_inputs"]["context_is_not_incident_evidence"] is True
    assert result["multi_ai_synthesis"]["source_context_policy"]["source_context_is_incident_evidence"] is False
    assert result["multi_ai_synthesis"]["source_context_policy"]["support_claims_must_cite_evidence_id"] is True
    assert verify_sanitized_output(tmp_path / "multi_ai")["passed"] is True


def test_source_context_alone_does_not_support_runtime_claim(tmp_path: Path) -> None:
    source_context, source_analysis, _source_out, _analysis_out = _source_context_and_analysis(tmp_path)
    bundle = {
        "schema_version": "evidence_bundle.v1",
        "bundle_type": "sanitized_evidence_bundle",
        "evidence_sha256": "empty-evidence",
        "source": {"service": "unknown-sample", "environment": "prod"},
        "time_window": {"start": "2026-06-16T00:00:00Z", "end": "2026-06-16T18:00:00Z"},
        "local_first_summary": {"raw_logs_uploaded": False},
        "evidence_items": [],
        "signals": [],
    }
    result = run_multi_ai(
        bundle,
        {},
        providers=["local-gemini"],
        mode="local",
        source_context=source_context,
        source_analysis=source_analysis,
    )
    synthesis = result["multi_ai_synthesis"]
    assert synthesis["claim_groups"]
    assert synthesis["claim_groups"][0]["unsupported"] is True
    assert synthesis["auto_archived"][0]["reason"] == "unsupported_support_without_evidence_id"


def test_source_context_api_uploads_accept_valid_bundles(tmp_path: Path) -> None:
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    source_context, source_analysis, _source_out, _analysis_out = _source_context_and_analysis(tmp_path)
    from fastapi.testclient import TestClient
    from ops_evidence_synthesis.api import app

    with TestClient(app) as client:
        context_response = client.post("/source-context/upload", json={"source_context_bundle": source_context})
        assert context_response.status_code == 200, context_response.text
        context_payload = context_response.json()
        assert context_payload["status"] == "accepted"
        assert context_payload["server_validation"]["source_context_sha256_verified"] is True
        assert context_payload["context_is_not_incident_evidence"] is True

        analysis_response = client.post("/source-analysis/upload", json={"source_analysis_bundle": source_analysis})
        assert analysis_response.status_code == 200, analysis_response.text
        analysis_payload = analysis_response.json()
        assert analysis_payload["status"] == "accepted"
        assert analysis_payload["server_validation"]["analysis_sha256_verified"] is True
        assert analysis_payload["context_is_not_incident_evidence"] is True

        discovery_response = client.post(
            "/profile-discovery/upload",
            json={
                "source_context_bundle": source_context,
                "source_analysis_bundle": source_analysis,
                "service": "unknown-sample",
                "environment": "prod",
            },
        )
        assert discovery_response.status_code == 200, discovery_response.text
        assert discovery_response.json()["source_context"]["accepted"] is True

        evidence = _evidence_bundle(tmp_path)
        ai_response = client.post(
            "/ai/multi-run",
            json={
                "evidence_bundle": evidence,
                "source_context_bundle": source_context,
                "source_analysis_bundle": source_analysis,
                "providers": ["local-gemini"],
                "mode": "local",
            },
        )
        assert ai_response.status_code == 200, ai_response.text
        assert ai_response.json()["context_inputs"]["source_context_included"] is True
