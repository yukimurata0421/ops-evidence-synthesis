from __future__ import annotations

import os
import importlib.util
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ops_evidence_synthesis.canonical import canonical_json, sha256_json
from ops_evidence_synthesis.gcp.storage import GcsUri, read_json, upload_file, write_json
from ops_evidence_synthesis.precomputed_review import stable_precomputed_review_json
from ops_evidence_synthesis.synthesis.multi_ai import run_multi_ai
from ops_evidence_synthesis.web.precomputed_review import (
    render_precomputed_markdown_report,
    render_precomputed_review_detail_page,
)


DEFAULT_JOB_PROVIDERS = ("gemini", "gpt-oss", "mistral", "qwen", "gemma")


@dataclass(frozen=True, slots=True)
class ChunkedReviewJobConfig:
    input_bundle_uri: GcsUri
    output_prefix_uri: GcsUri
    run_id: str
    approved_profile_uri: GcsUri | None = None
    source_context_uri: GcsUri | None = None
    source_analysis_uri: GcsUri | None = None
    precomputed_output_prefix_uri: GcsUri | None = None
    static_review_output_prefix_uri: GcsUri | None = None
    static_review_public_base_url: str = ""
    provider_mode: str = "real_or_skip"
    providers: tuple[str, ...] = DEFAULT_JOB_PROVIDERS

    @property
    def run_output_prefix(self) -> GcsUri:
        return self.output_prefix_uri.child(self.run_id)


def main() -> None:
    result = run_from_env()
    print(canonical_json(result))


def run_from_env() -> dict[str, Any]:
    config = job_config_from_env()
    return run_job(config)


def job_config_from_env() -> ChunkedReviewJobConfig:
    input_bundle = _required_gcs_uri("OES_JOB_INPUT_BUNDLE_URI")
    output_prefix = _required_gcs_uri("OES_JOB_OUTPUT_PREFIX_URI")
    run_id = os.environ.get("OES_JOB_RUN_ID", "").strip() or f"run-{uuid.uuid4().hex}"
    return ChunkedReviewJobConfig(
        input_bundle_uri=input_bundle,
        output_prefix_uri=output_prefix,
        run_id=run_id,
        approved_profile_uri=_optional_gcs_uri("OES_JOB_APPROVED_PROFILE_URI"),
        source_context_uri=_optional_gcs_uri("OES_JOB_SOURCE_CONTEXT_URI"),
        source_analysis_uri=_optional_gcs_uri("OES_JOB_SOURCE_ANALYSIS_URI"),
        precomputed_output_prefix_uri=_optional_gcs_uri("OES_JOB_PRECOMPUTED_OUTPUT_PREFIX_URI"),
        static_review_output_prefix_uri=_optional_gcs_uri("OES_JOB_STATIC_REVIEW_OUTPUT_PREFIX_URI"),
        static_review_public_base_url=os.environ.get("OES_JOB_STATIC_REVIEW_PUBLIC_BASE_URL", "").strip(),
        provider_mode=os.environ.get("OES_JOB_PROVIDER_MODE", "real_or_skip").strip() or "real_or_skip",
        providers=_providers_from_env(os.environ.get("OES_JOB_PROVIDERS", "")),
    )


def run_job(config: ChunkedReviewJobConfig) -> dict[str, Any]:
    bundle = read_json(config.input_bundle_uri)
    approved_profile = read_json(config.approved_profile_uri) if config.approved_profile_uri else {}
    source_context = read_json(config.source_context_uri) if config.source_context_uri else {}
    source_analysis = read_json(config.source_analysis_uri) if config.source_analysis_uri else {}
    with tempfile.TemporaryDirectory(prefix="oes-chunked-review-") as temp_name:
        output_dir = Path(temp_name)
        result = run_multi_ai(
            bundle,
            approved_profile,
            providers=config.providers,
            mode=config.provider_mode,
            output_dir=output_dir,
            source_context=source_context,
            source_analysis=source_analysis,
            pipeline_run_id=config.run_id,
        )
        run_prefix = config.run_output_prefix
        multi_ai_uri = run_prefix.child("multi_ai_run.json")
        write_json(multi_ai_uri, result)
        precomputed_uri = _write_precomputed_payload_if_requested(
            config,
            result=result,
            bundle=bundle,
            source_context=source_context,
            source_analysis=source_analysis,
            approved_profile=approved_profile,
        )
        static_review_outputs = _write_static_review_outputs_if_requested(config, payload=precomputed_uri.payload)
        uploaded_artifacts = _upload_output_dir(output_dir, run_prefix.child("artifacts"))
        job_result = _job_result_payload(
            config,
            result=result,
            multi_ai_uri=multi_ai_uri,
            precomputed_uri=precomputed_uri.uri,
            static_review_outputs=static_review_outputs,
            uploaded_artifacts=uploaded_artifacts,
        )
        write_json(run_prefix.child("job_result.json"), job_result)
        if _truthy(os.environ.get("OES_JOB_WRITE_LATEST", "")):
            write_json(config.output_prefix_uri.child("latest_job_result.json"), job_result)
        return job_result


def _job_result_payload(
    config: ChunkedReviewJobConfig,
    *,
    result: dict[str, Any],
    multi_ai_uri: GcsUri,
    precomputed_uri: GcsUri | None,
    static_review_outputs: dict[str, str],
    uploaded_artifacts: list[str],
) -> dict[str, Any]:
    model_runs = [row for row in result.get("model_runs") or [] if isinstance(row, dict)]
    provider_chunk_runs = [row for row in result.get("provider_chunk_runs") or [] if isinstance(row, dict)]
    ok_runs = [
        row
        for row in model_runs
        if str(row.get("status") or "") == "ok" and row.get("schema_valid") is True
    ]
    return {
        "schema_version": "cloud_run_chunked_review_job_result.v1",
        "run_id": config.run_id,
        "evidence_sha256": str(result.get("evidence_sha256") or ""),
        "input_bundle_uri": str(config.input_bundle_uri),
        "approved_profile_uri": str(config.approved_profile_uri) if config.approved_profile_uri else "",
        "source_context_uri": str(config.source_context_uri) if config.source_context_uri else "",
        "source_analysis_uri": str(config.source_analysis_uri) if config.source_analysis_uri else "",
        "output_prefix_uri": str(config.run_output_prefix),
        "multi_ai_run_uri": str(multi_ai_uri),
        "precomputed_review_uri": str(precomputed_uri) if precomputed_uri else "",
        "static_review_html_uri": static_review_outputs.get("static_review_html_uri", ""),
        "static_review_report_uri": static_review_outputs.get("static_review_report_uri", ""),
        "static_review_payload_uri": static_review_outputs.get("static_review_payload_uri", ""),
        "static_review_public_url": static_review_outputs.get("static_review_public_url", ""),
        "static_review_report_url": static_review_outputs.get("static_review_report_url", ""),
        "providers": list(config.providers),
        "provider_mode": config.provider_mode,
        "provider_total": len(model_runs),
        "schema_valid_provider_count": len(ok_runs),
        "provider_chunk_run_count": len(provider_chunk_runs),
        "canonical_graph_sha256": str(result.get("canonical_graph_sha256") or ""),
        "review_target_count": len(result.get("review_targets") or []),
        "uploaded_artifacts": uploaded_artifacts,
        "result_sha256": sha256_json(result),
    }


@dataclass(frozen=True, slots=True)
class PrecomputedReviewOutput:
    uri: GcsUri | None
    payload: dict[str, Any]


def _write_precomputed_payload_if_requested(
    config: ChunkedReviewJobConfig,
    *,
    result: dict[str, Any],
    bundle: dict[str, Any],
    source_context: dict[str, Any],
    source_analysis: dict[str, Any],
    approved_profile: dict[str, Any],
) -> PrecomputedReviewOutput:
    payload = _build_precomputed_payload(
        result=result,
        bundle=bundle,
        source_context=source_context,
        source_analysis=source_analysis,
        approved_profile=approved_profile,
    )
    evidence_sha = str(payload.get("evidence_sha256") or result.get("evidence_sha256") or "")
    if not evidence_sha:
        raise RuntimeError("precomputed payload did not include evidence_sha256")
    if config.precomputed_output_prefix_uri is None:
        return PrecomputedReviewOutput(uri=None, payload=payload)
    uri = config.precomputed_output_prefix_uri.child(f"{evidence_sha}.json")
    from ops_evidence_synthesis.gcp.storage import write_text

    write_text(uri, stable_precomputed_review_json(payload), content_type="application/json")
    return PrecomputedReviewOutput(uri=uri, payload=payload)


def _write_static_review_outputs_if_requested(
    config: ChunkedReviewJobConfig,
    *,
    payload: dict[str, Any],
) -> dict[str, str]:
    if config.static_review_output_prefix_uri is None:
        return {}
    evidence_sha = str(payload.get("evidence_sha256") or "")
    if not evidence_sha:
        raise RuntimeError("static review payload did not include evidence_sha256")
    review_prefix = config.static_review_output_prefix_uri.child(evidence_sha)
    html_uri = review_prefix.child("index.html")
    report_uri = review_prefix.child("report.md")
    payload_uri = review_prefix.child("payload.json")
    html = render_precomputed_review_detail_page(evidence_sha, payload)
    report = render_precomputed_markdown_report(evidence_sha, payload)

    from ops_evidence_synthesis.gcp.storage import write_text

    write_text(html_uri, html, content_type="text/html; charset=utf-8")
    write_text(report_uri, report, content_type="text/markdown; charset=utf-8")
    write_text(payload_uri, stable_precomputed_review_json(payload), content_type="application/json")
    public_url = _static_review_public_url(config.static_review_public_base_url, evidence_sha)
    return {
        "static_review_html_uri": str(html_uri),
        "static_review_report_uri": str(report_uri),
        "static_review_payload_uri": str(payload_uri),
        "static_review_public_url": public_url,
        "static_review_report_url": f"{public_url.rstrip('/')}/report.md" if public_url else "",
    }


def _static_review_public_url(base_url: str, evidence_sha: str) -> str:
    base = str(base_url or "").strip().rstrip("/")
    if not base:
        return ""
    if "{evidence_sha256}" in base:
        return base.format(evidence_sha256=evidence_sha)
    return f"{base}/{evidence_sha}/"


def _build_precomputed_payload(
    *,
    result: dict[str, Any],
    bundle: dict[str, Any],
    source_context: dict[str, Any],
    source_analysis: dict[str, Any],
    approved_profile: dict[str, Any],
) -> dict[str, Any]:
    generator = _load_precomputed_generator()
    return generator.build_payload(
        result,
        bundle,
        source_context=source_context,
        source_analysis=source_analysis,
        profile_draft={},
        approved_profile=approved_profile,
        api_revision=os.environ.get("OES_JOB_API_REVISION", ""),
        profile_id=os.environ.get("OES_JOB_PROFILE_ID", "") or str(approved_profile.get("profile_id") or ""),
        updated_at=os.environ.get("OES_JOB_UPDATED_AT", ""),
        source_note=os.environ.get(
            "OES_JOB_SOURCE_NOTE",
            "generated by private Cloud Run Job from sanitized GCS artifacts",
        ),
        provider_mode=_public_provider_mode(),
        model_projection_policy=os.environ.get("OES_JOB_MODEL_PROJECTION_POLICY", ""),
        log_observations=_csv_env("OES_JOB_LOG_OBSERVATIONS"),
        min_window_hours=int(os.environ.get("OES_JOB_MIN_WINDOW_HOURS", "24")),
    )


def _load_precomputed_generator() -> Any:
    path = _precomputed_generator_path()
    spec = importlib.util.spec_from_file_location("generate_precomputed_review_from_multi_run", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"failed to load precomputed generator from {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _precomputed_generator_path() -> Path:
    configured = os.environ.get("OES_JOB_PRECOMPUTED_GENERATOR_PATH", "").strip()
    candidates = []
    if configured:
        candidates.append(Path(configured))
    candidates.append(Path.cwd() / "scripts" / "generate_precomputed_review_from_multi_run.py")
    candidates.append(Path(__file__).resolve().parents[3] / "scripts" / "generate_precomputed_review_from_multi_run.py")
    for path in candidates:
        if path.exists():
            return path
    raise RuntimeError("precomputed review generator script was not found")


def _public_provider_mode() -> str:
    configured = os.environ.get("OES_JOB_PUBLIC_PROVIDER_MODE", "").strip()
    if configured:
        return configured
    job_mode = os.environ.get("OES_JOB_PROVIDER_MODE", "real_or_skip").strip().casefold()
    if job_mode in {"local", "deterministic", "fake"}:
        return "deterministic_local_gcs_review"
    return "real_api_private_gcs_cloud_run_job_postgres_ledger"


def _upload_output_dir(output_dir: Path, prefix: GcsUri) -> list[str]:
    uploaded: list[str] = []
    for path in sorted(output_dir.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(output_dir).as_posix()
        uri = prefix.child(relative)
        upload_file(path, uri, content_type=_content_type(path))
        uploaded.append(str(uri))
    return uploaded


def _providers_from_env(raw: str) -> tuple[str, ...]:
    values = tuple(part.strip() for part in str(raw or "").split(",") if part.strip())
    return values or DEFAULT_JOB_PROVIDERS


def _required_gcs_uri(name: str) -> GcsUri:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is required")
    return GcsUri.parse(value)


def _optional_gcs_uri(name: str) -> GcsUri | None:
    value = os.environ.get(name, "").strip()
    return GcsUri.parse(value) if value else None


def _content_type(path: Path) -> str:
    if path.suffix == ".json":
        return "application/json"
    if path.suffix == ".jsonl":
        return "application/x-ndjson"
    return "application/octet-stream"


def _csv_env(name: str) -> list[str]:
    return [item.strip() for item in os.environ.get(name, "").split("|") if item.strip()]


def _truthy(value: str) -> bool:
    return str(value or "").strip().casefold() in {"1", "true", "yes", "on"}


if __name__ == "__main__":
    main()
