from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from ops_evidence_synthesis.canonical import sha256_json
from ops_evidence_synthesis.gcp import chunked_review_job
from ops_evidence_synthesis.gcp.chunked_review_job import (
    DEFAULT_JOB_PROVIDERS,
    _public_provider_mode,
    _providers_from_env,
    job_config_from_env,
)
from ops_evidence_synthesis.gcp.storage import GcsUri


def test_job_config_from_env_uses_private_gcs_inputs(monkeypatch) -> None:
    monkeypatch.setenv("OES_JOB_INPUT_BUNDLE_URI", "gs://private/input/evidence_bundle.json")
    monkeypatch.setenv("OES_JOB_APPROVED_PROFILE_URI", "gs://private/input/profile.json")
    monkeypatch.setenv("OES_JOB_SOURCE_CONTEXT_URI", "gs://private/input/source_context.json")
    monkeypatch.setenv("OES_JOB_SOURCE_ANALYSIS_URI", "gs://private/input/source_analysis.json")
    monkeypatch.setenv("OES_JOB_OUTPUT_PREFIX_URI", "gs://private/output/runs")
    monkeypatch.setenv("OES_JOB_PRECOMPUTED_OUTPUT_PREFIX_URI", "gs://private/precomputed")
    monkeypatch.setenv("OES_JOB_RUN_ID", "run-001")
    monkeypatch.setenv("OES_JOB_PROVIDER_MODE", "real")
    monkeypatch.setenv("OES_JOB_PROVIDERS", "gemini,gpt-oss,qwen")

    config = job_config_from_env()

    assert str(config.input_bundle_uri) == "gs://private/input/evidence_bundle.json"
    assert str(config.approved_profile_uri) == "gs://private/input/profile.json"
    assert str(config.run_output_prefix) == "gs://private/output/runs/run-001"
    assert str(config.precomputed_output_prefix_uri) == "gs://private/precomputed"
    assert config.provider_mode == "real"
    assert config.providers == ("gemini", "gpt-oss", "qwen")


def test_job_provider_list_defaults_to_five_provider_set() -> None:
    assert DEFAULT_JOB_PROVIDERS == ("gemini", "gpt-oss", "mistral", "qwen", "gemma")
    assert _providers_from_env("") == DEFAULT_JOB_PROVIDERS
    assert _providers_from_env(" gemini, qwen ,, glm ") == ("gemini", "qwen", "glm")


def test_public_provider_mode_tracks_local_job_mode(monkeypatch) -> None:
    monkeypatch.delenv("OES_JOB_PUBLIC_PROVIDER_MODE", raising=False)
    monkeypatch.setenv("OES_JOB_PROVIDER_MODE", "local")

    assert _public_provider_mode() == "deterministic_local_gcs_review"

    monkeypatch.setenv("OES_JOB_PUBLIC_PROVIDER_MODE", "manual-public-mode")
    assert _public_provider_mode() == "manual-public-mode"


def test_approved_profile_disables_source_context_after_approval(monkeypatch) -> None:
    approved_profile = {
        "schema_version": "approved_operational_profile.v1",
        "status": "approved",
        "explicit_profile": True,
        "profile_id": "stream-runtime",
        "human_review": {
            "profile_matches_deployment": True,
            "deployment_period_confirmed": True,
            "log_scope_confirmed": True,
        },
        "review_policy": {"source_access_after_approval": "disabled"},
    }
    approved_profile["approved_profile_sha256"] = sha256_json(approved_profile)
    reads = {
        "gs://private/input/evidence.json": {"evidence_sha256": "e" * 64},
        "gs://private/input/profile.json": approved_profile,
        "gs://private/input/source.json": {"schema_version": "source_context_bundle.v1"},
    }
    monkeypatch.setattr(chunked_review_job, "read_json", lambda uri: reads[str(uri)])
    config = chunked_review_job.ChunkedReviewJobConfig(
        input_bundle_uri=GcsUri.parse("gs://private/input/evidence.json"),
        output_prefix_uri=GcsUri.parse("gs://private/output"),
        run_id="run-source-boundary",
        approved_profile_uri=GcsUri.parse("gs://private/input/profile.json"),
        source_context_uri=GcsUri.parse("gs://private/input/source.json"),
    )

    with pytest.raises(ValueError, match="source context cannot be supplied"):
        chunked_review_job.run_job(config)


def test_chunked_job_rejects_tampered_approved_profile(monkeypatch) -> None:
    approved_profile = {
        "schema_version": "approved_operational_profile.v1",
        "status": "approved",
        "explicit_profile": True,
        "profile_id": "stream-runtime",
        "human_review": {
            "profile_matches_deployment": True,
            "deployment_period_confirmed": True,
            "log_scope_confirmed": True,
        },
        "review_policy": {"source_access_after_approval": "disabled"},
        "approved_profile_sha256": "0" * 64,
    }
    reads = {
        "gs://private/input/evidence.json": {"evidence_sha256": "e" * 64},
        "gs://private/input/profile.json": approved_profile,
    }
    monkeypatch.setattr(chunked_review_job, "read_json", lambda uri: reads[str(uri)])
    config = chunked_review_job.ChunkedReviewJobConfig(
        input_bundle_uri=GcsUri.parse("gs://private/input/evidence.json"),
        output_prefix_uri=GcsUri.parse("gs://private/output"),
        run_id="run-tampered-profile",
        approved_profile_uri=GcsUri.parse("gs://private/input/profile.json"),
    )

    with pytest.raises(ValueError, match="approved_profile_sha256 mismatch"):
        chunked_review_job.run_job(config)


def test_run_job_reads_private_gcs_artifacts_and_writes_job_outputs(monkeypatch) -> None:
    evidence_sha = "e" * 64
    input_bundle = {"evidence_sha256": evidence_sha, "service": "amazon-notify"}
    approved_profile = {"profile_id": "approved-amazon-notify", "explicit_profile": True}
    source_context = {"schema_version": "source_context_bundle.v1"}
    source_analysis = {"schema_version": "source_analysis_bundle.v1"}
    reads = {
        "gs://private/input/evidence_bundle.json": input_bundle,
        "gs://private/input/approved_profile.json": approved_profile,
        "gs://private/input/source_context.json": source_context,
        "gs://private/input/source_analysis.json": source_analysis,
    }
    written_json: dict[str, dict[str, Any]] = {}
    written_text: dict[str, tuple[str, str]] = {}
    uploaded: list[tuple[str, str, str | None]] = []

    def fake_read_json(uri: GcsUri) -> dict[str, Any]:
        return reads[str(uri)]

    def fake_write_json(uri: GcsUri, payload: dict[str, Any]) -> None:
        written_json[str(uri)] = json.loads(json.dumps(payload, sort_keys=True))

    def fake_upload_file(path: Path, uri: GcsUri, *, content_type: str | None = None) -> None:
        uploaded.append((str(uri).split("/artifacts/", 1)[1], str(uri), content_type))

    def fake_run_multi_ai(
        bundle: dict[str, Any],
        profile: dict[str, Any],
        *,
        providers: tuple[str, ...],
        mode: str,
        output_dir: Path,
        source_context: dict[str, Any],
        source_analysis: dict[str, Any],
        pipeline_run_id: str,
    ) -> dict[str, Any]:
        assert bundle == input_bundle
        assert profile == approved_profile
        assert providers == ("gemini-fast-lite", "gemma")
        assert mode == "real_or_skip"
        assert source_context == reads["gs://private/input/source_context.json"]
        assert source_analysis == reads["gs://private/input/source_analysis.json"]
        assert pipeline_run_id == "run-001"
        (output_dir / "provider_artifact.json").write_text('{"ok": true}\n', encoding="utf-8")
        nested = output_dir / "nested"
        nested.mkdir()
        (nested / "provider_trace.jsonl").write_text("{}\n", encoding="utf-8")
        return {
            "evidence_sha256": evidence_sha,
            "canonical_graph_sha256": "g" * 64,
            "model_runs": [
                {
                    "provider": "gemini-fast-lite",
                    "status": "ok",
                    "schema_valid": True,
                },
                {
                    "provider": "gemma",
                    "status": "provider_error",
                    "schema_valid": False,
                },
            ],
            "provider_chunk_runs": [{"provider": "gemini-fast-lite", "chunk_id": "chunk-1"}],
            "review_targets": [{"review_target_id": "rt-1"}],
        }

    def fake_build_precomputed_payload(
        *,
        result: dict[str, Any],
        bundle: dict[str, Any],
        source_context: dict[str, Any],
        source_analysis: dict[str, Any],
        approved_profile: dict[str, Any],
    ) -> dict[str, Any]:
        assert result["evidence_sha256"] == evidence_sha
        assert bundle == input_bundle
        assert source_context == reads["gs://private/input/source_context.json"]
        assert source_analysis == reads["gs://private/input/source_analysis.json"]
        assert approved_profile == reads["gs://private/input/approved_profile.json"]
        return {
            "schema_version": "precomputed_review_summary.v1",
            "evidence_sha256": evidence_sha,
            "summary": {"provider_count": 2},
            "provider_statuses": [],
            "review_graph_summary": {},
            "targets": [],
        }

    monkeypatch.setattr(chunked_review_job, "read_json", fake_read_json)
    monkeypatch.setattr(chunked_review_job, "write_json", fake_write_json)
    monkeypatch.setattr(chunked_review_job, "upload_file", fake_upload_file)
    monkeypatch.setattr(chunked_review_job, "run_multi_ai", fake_run_multi_ai)
    monkeypatch.setattr(chunked_review_job, "_build_precomputed_payload", fake_build_precomputed_payload)
    monkeypatch.setenv("OES_JOB_WRITE_LATEST", "1")

    from ops_evidence_synthesis.gcp import storage

    monkeypatch.setattr(
        storage,
        "write_text",
        lambda uri, text, *, content_type="text/plain": written_text.__setitem__(
            str(uri),
            (text, content_type),
        ),
    )

    config = chunked_review_job.ChunkedReviewJobConfig(
        input_bundle_uri=GcsUri.parse("gs://private/input/evidence_bundle.json"),
        output_prefix_uri=GcsUri.parse("gs://private/output/runs"),
        run_id="run-001",
        approved_profile_uri=GcsUri.parse("gs://private/input/approved_profile.json"),
        source_context_uri=GcsUri.parse("gs://private/input/source_context.json"),
        source_analysis_uri=GcsUri.parse("gs://private/input/source_analysis.json"),
        precomputed_output_prefix_uri=GcsUri.parse("gs://private/precomputed"),
        static_review_output_prefix_uri=GcsUri.parse("gs://private/review-pages"),
        static_review_public_base_url="https://reviews.example.test/reviews",
        provider_mode="real_or_skip",
        providers=("gemini-fast-lite", "gemma"),
    )

    job_result = chunked_review_job.run_job(config)

    assert written_json[f"gs://private/output/runs/run-001/multi_ai_run.json"]["evidence_sha256"] == evidence_sha
    assert written_json["gs://private/output/runs/run-001/job_result.json"] == job_result
    assert written_json["gs://private/output/runs/latest_job_result.json"] == job_result
    expected_fields = {
        "schema_version": "cloud_run_chunked_review_job_result.v1",
        "run_id": "run-001",
        "evidence_sha256": evidence_sha,
        "input_bundle_uri": "gs://private/input/evidence_bundle.json",
        "approved_profile_uri": "gs://private/input/approved_profile.json",
        "source_context_uri": "gs://private/input/source_context.json",
        "source_analysis_uri": "gs://private/input/source_analysis.json",
        "output_prefix_uri": "gs://private/output/runs/run-001",
        "multi_ai_run_uri": "gs://private/output/runs/run-001/multi_ai_run.json",
        "precomputed_review_uri": f"gs://private/precomputed/{evidence_sha}.json",
        "static_review_html_uri": f"gs://private/review-pages/{evidence_sha}/index.html",
        "static_review_report_uri": f"gs://private/review-pages/{evidence_sha}/report.md",
        "static_review_payload_uri": f"gs://private/review-pages/{evidence_sha}/payload.json",
        "static_review_public_url": f"https://reviews.example.test/reviews/{evidence_sha}/",
        "static_review_report_url": f"https://reviews.example.test/reviews/{evidence_sha}/report.md",
        "providers": ["gemini-fast-lite", "gemma"],
        "provider_mode": "real_or_skip",
        "provider_total": 2,
        "schema_valid_provider_count": 1,
        "provider_chunk_run_count": 1,
        "canonical_graph_sha256": "g" * 64,
        "review_target_count": 1,
    }
    for key, value in expected_fields.items():
        assert job_result[key] == value
    assert {
        (relative_path, uri, content_type)
        for relative_path, uri, content_type in uploaded
    } == {
        (
            "provider_artifact.json",
            "gs://private/output/runs/run-001/artifacts/provider_artifact.json",
            "application/json",
        ),
        (
            "nested/provider_trace.jsonl",
            "gs://private/output/runs/run-001/artifacts/nested/provider_trace.jsonl",
            "application/x-ndjson",
        ),
    }
    precomputed_text, precomputed_type = written_text[f"gs://private/precomputed/{evidence_sha}.json"]
    assert precomputed_type == "application/json"
    assert json.loads(precomputed_text)["evidence_sha256"] == evidence_sha
    html_text, html_type = written_text[f"gs://private/review-pages/{evidence_sha}/index.html"]
    assert html_type == "text/html; charset=utf-8"
    assert "Ops Evidence Review" in html_text
    report_text, report_type = written_text[f"gs://private/review-pages/{evidence_sha}/report.md"]
    assert report_type == "text/markdown; charset=utf-8"
    assert "Incident Review Report" in report_text
    payload_text, payload_type = written_text[f"gs://private/review-pages/{evidence_sha}/payload.json"]
    assert payload_type == "application/json"
    assert json.loads(payload_text)["evidence_sha256"] == evidence_sha
