from __future__ import annotations

from ops_evidence_synthesis.gcp.chunked_review_job import (
    DEFAULT_JOB_PROVIDERS,
    _providers_from_env,
    job_config_from_env,
)


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
    assert DEFAULT_JOB_PROVIDERS == ("gemini", "gpt-oss", "mistral", "qwen", "glm")
    assert _providers_from_env("") == DEFAULT_JOB_PROVIDERS
    assert _providers_from_env(" gemini, qwen ,, glm ") == ("gemini", "qwen", "glm")
