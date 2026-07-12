from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import tempfile
import threading
import time
import uuid
from copy import deepcopy
from dataclasses import asdict
from datetime import timedelta
from pathlib import Path
from typing import Any, Callable

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response

from ops_evidence_synthesis.ai.base import ModelProvider
from ops_evidence_synthesis.ai.provider_registry import provider_infos
from ops_evidence_synthesis.ai.runtime import safe_provider_error_message
from ops_evidence_synthesis.bundle import EvidenceBundleBuilder
from ops_evidence_synthesis.canonical import sha256_json
from ops_evidence_synthesis.collectors.remote import (
    DEFAULT_ALLOWED_PATH_ROOTS,
    RemoteCollectorConfig,
    collect_remote_evidence,
    collector_targets_from_more_data,
)
from ops_evidence_synthesis.evidence_request_planner import (
    build_evidence_request_plan,
    render_collection_instructions,
    sample_planner_answers,
    validate_plan_payload_inputs,
)
from ops_evidence_synthesis.ingest import ingest_log_files, sanitize_logs
from ops_evidence_synthesis.local_first import validate_evidence_bundle_for_upload
from ops_evidence_synthesis.models import IncidentWindow, RawLog, SanitizedLog
from ops_evidence_synthesis.observability import log_event
from ops_evidence_synthesis.precomputed_review import stable_precomputed_review_json
from ops_evidence_synthesis.pipeline_progress import (
    analysis_pipeline_status_from_store,
    finish_pipeline_run,
    pipeline_status_from_store,
    record_pipeline_event,
    start_pipeline_run,
)
from ops_evidence_synthesis.profile_discovery import (
    approved_profile_from_draft,
    build_profile_discovery_bundle,
    build_profile_draft,
    build_profile_draft_with_provider,
    validate_profile_discovery_bundle_for_upload,
)
from ops_evidence_synthesis.profile_review import (
    ProfileReviewError,
    build_approved_operational_profile,
    build_profile_review_interpretation_preview,
    normalize_profile_review_with_provider,
    normalize_profile_review_patch,
    validate_approved_operational_profile,
)
from ops_evidence_synthesis.profiles import profile_context_for_bundle
from ops_evidence_synthesis.source_context import (
    validate_source_analysis_bundle_for_upload,
    validate_source_context_bundle_for_upload,
)
from ops_evidence_synthesis.storage.sqlite_store import DEFAULT_DB_PATH, SQLiteStore
from ops_evidence_synthesis.synthesis.clustering import persist_proposition_clusters
from ops_evidence_synthesis.synthesis.comparison import compare_providers
from ops_evidence_synthesis.synthesis.more_data import analyze_more_data_queries
from ops_evidence_synthesis.synthesis.multi_ai import run_multi_ai
from ops_evidence_synthesis.synthesis.pipeline import (
    run_model_stage,
    run_pipeline,
    run_route_stage,
    run_score_stage,
    run_synthesis_for_bundle,
)
from ops_evidence_synthesis.synthesis.review_arbitration import resolve_canonical_review_graph_snapshot
from ops_evidence_synthesis.synthesis.router import RoutingResult
from ops_evidence_synthesis.timeutils import format_timestamp, parse_timestamp, utc_now
from ops_evidence_synthesis.web.precomputed_review import (
    canonical_precomputed_review_sha as _canonical_precomputed_review_sha,
    fast_detail_target_card as _fast_detail_target_card,
    fast_review_shell as _fast_review_shell,
    precomputed_review_graph_response as _precomputed_review_graph_response,
    precomputed_review_payload as _precomputed_review_payload,
    precomputed_review_target_set as _precomputed_review_target_set,
    precomputed_summary as _precomputed_summary,
    public_precomputed_landing_page as _public_precomputed_landing_page,
    remember_precomputed_review_payload as _remember_precomputed_review_payload,
    render_precomputed_api_page as _render_precomputed_api_page,
    render_precomputed_graph_page as _render_precomputed_graph_page,
    render_precomputed_markdown_report as _render_precomputed_markdown_report,
    render_precomputed_review_detail_page as _render_precomputed_review_detail_page,
    render_rescore_demo_page as _render_rescore_demo_page,
    rescore_demo_payload as _rescore_demo_payload,
    short_sha as _short_sha,
    url_quote as _url_quote,
)
from ops_evidence_synthesis.web.review_page import (
    bundle_lineage_summary as _bundle_lineage_summary,
    html_escape as _html,
    latest_canonical_graph_response as _latest_canonical_graph_response,
    model_run_artifacts_for_ui as _model_run_artifacts_for_ui,
    multi_ai_synthesis_for_ui as _multi_ai_synthesis_for_ui,
    pipeline_progress_panel as _pipeline_progress_panel,
    render_review_targets_page as _review_targets_page,
    review_summary_for_ui as _review_summary_for_ui,
    target_set_from_canonical_graph as _target_set_from_canonical_graph,
)

LOGGER = logging.getLogger("ops_evidence_synthesis.api.routes")
router = APIRouter()
_TARGET_SET_CACHE: dict[tuple[str, str, int, bool], tuple[float, dict[str, Any]]] = {}
_FAST_GCP_REVIEW_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_FAST_GCP_REVIEW_LOCK = threading.Lock()
_FAST_GCP_REVIEW_STATUS_CACHE: dict[str, dict[str, Any]] = {}
_FAST_GCP_REVIEW_STATUS_LOCK = threading.Lock()
_FAST_GCP_REVIEW_QUOTA_CACHE: dict[str, dict[str, Any]] = {}
_FAST_GCP_REVIEW_DISABLE_CACHE: tuple[float, dict[str, Any] | None] | None = None
_FAST_GCP_REVIEW_OWNER_COOKIE = "oes_fast_gcp_review_owner"
_FAST_GCP_REVIEW_LOGIC_REVISION = "source-approved-evidence-v2"
_PUBLIC_RATE_LIMIT_COUNTERS: dict[tuple[str, str, int], int] = {}
_PUBLIC_RATE_LIMIT_LOCK = threading.Lock()

_STORE_FACTORY: Callable[[], Any] | None = None
_GEMINI_PROVIDER_FACTORY: Callable[[], ModelProvider] | None = None
_PROFILE_DRAFT_PROVIDER_FACTORY: Callable[[], ModelProvider] | None = None
_PROFILE_REVIEW_PROVIDER_FACTORY: Callable[[], ModelProvider] | None = None
_EVIDENCE_REQUIREMENT_PROVIDER_FACTORY: Callable[[], ModelProvider] | None = None
_CLAUDE_PROVIDER_FACTORY: Callable[[], ModelProvider] | None = None
_GPT_OSS_PROVIDER_FACTORY: Callable[[], ModelProvider] | None = None
_MISTRAL_PROVIDER_FACTORY: Callable[[], ModelProvider] | None = None
_QWEN_PROVIDER_FACTORY: Callable[[], ModelProvider] | None = None
_GLM_PROVIDER_FACTORY: Callable[[], ModelProvider] | None = None
_LLAMA_PROVIDER_FACTORY: Callable[[], ModelProvider] | None = None


def configure_api_routes(
    *,
    store_factory: Callable[[], Any],
    gemini_provider_factory: Callable[[], ModelProvider],
    profile_draft_provider_factory: Callable[[], ModelProvider],
    profile_review_provider_factory: Callable[[], ModelProvider],
    evidence_requirement_provider_factory: Callable[[], ModelProvider],
    claude_provider_factory: Callable[[], ModelProvider],
    gpt_oss_provider_factory: Callable[[], ModelProvider],
    mistral_provider_factory: Callable[[], ModelProvider],
    qwen_provider_factory: Callable[[], ModelProvider],
    glm_provider_factory: Callable[[], ModelProvider],
    llama_provider_factory: Callable[[], ModelProvider],
) -> None:
    global _STORE_FACTORY
    global _GEMINI_PROVIDER_FACTORY
    global _PROFILE_DRAFT_PROVIDER_FACTORY
    global _PROFILE_REVIEW_PROVIDER_FACTORY
    global _EVIDENCE_REQUIREMENT_PROVIDER_FACTORY
    global _CLAUDE_PROVIDER_FACTORY
    global _GPT_OSS_PROVIDER_FACTORY
    global _MISTRAL_PROVIDER_FACTORY
    global _QWEN_PROVIDER_FACTORY
    global _GLM_PROVIDER_FACTORY
    global _LLAMA_PROVIDER_FACTORY
    _STORE_FACTORY = store_factory
    _GEMINI_PROVIDER_FACTORY = gemini_provider_factory
    _PROFILE_DRAFT_PROVIDER_FACTORY = profile_draft_provider_factory
    _PROFILE_REVIEW_PROVIDER_FACTORY = profile_review_provider_factory
    _EVIDENCE_REQUIREMENT_PROVIDER_FACTORY = evidence_requirement_provider_factory
    _CLAUDE_PROVIDER_FACTORY = claude_provider_factory
    _GPT_OSS_PROVIDER_FACTORY = gpt_oss_provider_factory
    _MISTRAL_PROVIDER_FACTORY = mistral_provider_factory
    _QWEN_PROVIDER_FACTORY = qwen_provider_factory
    _GLM_PROVIDER_FACTORY = glm_provider_factory
    _LLAMA_PROVIDER_FACTORY = llama_provider_factory


def _store() -> Any:
    if _STORE_FACTORY is None:
        raise RuntimeError("API route store factory is not configured")
    return _STORE_FACTORY()


def _gemini_provider() -> ModelProvider:
    if _GEMINI_PROVIDER_FACTORY is None:
        raise RuntimeError("Gemini provider factory is not configured")
    return _GEMINI_PROVIDER_FACTORY()


def _profile_draft_provider() -> ModelProvider:
    if _PROFILE_DRAFT_PROVIDER_FACTORY is None:
        raise RuntimeError("profile draft provider factory is not configured")
    return _PROFILE_DRAFT_PROVIDER_FACTORY()


def _profile_review_provider() -> ModelProvider:
    if _PROFILE_REVIEW_PROVIDER_FACTORY is None:
        raise RuntimeError("profile review provider is not configured")
    return _PROFILE_REVIEW_PROVIDER_FACTORY()


def _evidence_requirement_provider() -> ModelProvider:
    if _EVIDENCE_REQUIREMENT_PROVIDER_FACTORY is None:
        raise RuntimeError("evidence requirement provider factory is not configured")
    return _EVIDENCE_REQUIREMENT_PROVIDER_FACTORY()


def _claude_provider() -> ModelProvider:
    if _CLAUDE_PROVIDER_FACTORY is None:
        raise RuntimeError("Claude provider factory is not configured")
    return _CLAUDE_PROVIDER_FACTORY()


def _gpt_oss_provider() -> ModelProvider:
    if _GPT_OSS_PROVIDER_FACTORY is None:
        raise RuntimeError("GPT-OSS provider factory is not configured")
    return _GPT_OSS_PROVIDER_FACTORY()


def _mistral_provider() -> ModelProvider:
    if _MISTRAL_PROVIDER_FACTORY is None:
        raise RuntimeError("Mistral provider factory is not configured")
    return _MISTRAL_PROVIDER_FACTORY()


def _qwen_provider() -> ModelProvider:
    if _QWEN_PROVIDER_FACTORY is None:
        raise RuntimeError("Qwen provider factory is not configured")
    return _QWEN_PROVIDER_FACTORY()


def _glm_provider() -> ModelProvider:
    if _GLM_PROVIDER_FACTORY is None:
        raise RuntimeError("GLM provider factory is not configured")
    return _GLM_PROVIDER_FACTORY()


def _llama_provider() -> ModelProvider:
    if _LLAMA_PROVIDER_FACTORY is None:
        raise RuntimeError("Llama provider factory is not configured")
    return _LLAMA_PROVIDER_FACTORY()


def _store_label() -> str:
    if os.environ.get("OES_STORE", "sqlite").casefold() == "bigquery":
        project = (
            os.environ.get("OES_GCP_PROJECT")
            or os.environ.get("OES_VERTEX_PROJECT")
            or os.environ.get("GOOGLE_CLOUD_PROJECT")
            or "ops-evidence-synthesis"
        )
        location = os.environ.get("OES_BIGQUERY_LOCATION", "asia-northeast1")
        return f"BigQuery: {project} / {location}"
    return os.environ.get("OES_DB_PATH", str(DEFAULT_DB_PATH))


def _target_cache_ttl_seconds() -> int:
    return int(os.environ.get("OES_REVIEW_TARGET_CACHE_SECONDS", "300"))


def _fast_initial_ui_enabled() -> bool:
    return os.environ.get("OES_UI_FAST_INITIAL", "1").casefold() not in {"0", "false", "no", "off"}


def _precomputed_only_ui_enabled() -> bool:
    return os.environ.get("OES_UI_PRECOMPUTED_ONLY", "0").casefold() in {"1", "true", "yes", "on"}


def _public_runtime_guard_enabled() -> bool:
    return os.environ.get("OES_PUBLIC_RUNTIME_GUARD", "0").strip().casefold() in {"1", "true", "yes", "on"}


def _public_write_token_configured() -> bool:
    return bool(os.environ.get("OES_API_WRITE_TOKEN", "").strip())


def _validate_public_runtime_config() -> None:
    if not _public_runtime_guard_enabled():
        return
    if not _precomputed_only_ui_enabled():
        return
    missing: list[str] = []
    if not _public_write_token_configured():
        missing.append("OES_API_WRITE_TOKEN")
    if _public_fast_gcp_review_enabled() and _fast_gcp_review_provider_mode() != "local":
        if not _fast_gcp_review_quota_backend_available():
            missing.append("OES_FAST_GCP_REVIEW_GCS_PREFIX or OES_FAST_GCP_REVIEW_QUOTA_GCS_PREFIX")
        if not _fast_gcp_review_disable_state_configured():
            missing.append("OES_PUBLIC_FAST_GCP_REVIEW_DISABLE_GCS_URI")
    if os.environ.get("OES_PUBLIC_RUNTIME_REQUIRE_RATE_LIMIT", "1").strip().casefold() in {"1", "true", "yes", "on"}:
        if not _public_rate_limit_enabled():
            missing.append("OES_PUBLIC_RATE_LIMIT_ENABLED")
    if missing:
        raise RuntimeError("public runtime guard failed: missing " + ", ".join(sorted(set(missing))))


def _require_precomputed_review_for_public_read(
    evidence_sha256: str | None,
    *,
    require_evidence_sha: bool = False,
) -> dict[str, Any] | None:
    if require_evidence_sha and _precomputed_only_ui_enabled() and not evidence_sha256:
        raise HTTPException(status_code=404, detail="precomputed evidence_sha256 is required")
    if not evidence_sha256 or not _precomputed_only_ui_enabled():
        return None
    precomputed = _precomputed_review_payload(evidence_sha256)
    if not precomputed:
        raise HTTPException(status_code=404, detail="precomputed review not found")
    return precomputed


PUBLIC_PRECOMPUTED_READ_PATHS = {
    "/",
    "/favicon.ico",
    "/health",
    "/public/fast-gcp-review/owner-session",
    "/public/fast-gcp-review/status",
    "/ui/fast-gcp-review",
    "/ui/api",
    "/ui/full-review-page",
    "/ui/report.md",
    "/ui/review-graph",
    "/ui/rescore-demo",
    "/ui/summary",
    "/review-targets",
    "/review/graph",
}


def _public_fast_gcp_review_enabled() -> bool:
    return os.environ.get("OES_PUBLIC_FAST_GCP_REVIEW_ENABLED", "1").strip().casefold() not in {
        "0",
        "false",
        "no",
        "off",
    }


def _public_fast_gcp_review_write_allowed(request: Request) -> bool:
    if not _public_fast_gcp_review_enabled():
        return False
    path = request.url.path.rstrip("/") or "/"
    return request.method.upper() == "POST" and path in {
        "/public/fast-gcp-review",
        "/public/fast-gcp-review/owner-session",
        "/public/rescore-demo/run",
    }


def _public_budget_guard_path(request: Request) -> bool:
    return (
        request.method.upper() == "POST"
        and (request.url.path.rstrip("/") or "/") == "/internal/budget-guard/fast-gcp-review"
    )


def _budget_guard_token() -> str:
    return os.environ.get("OES_BUDGET_GUARD_TOKEN", "").strip()


def _budget_guard_token_matches(request: Request) -> bool:
    expected = _budget_guard_token()
    if not expected:
        return False
    supplied = (
        request.headers.get("x-oes-budget-guard-token")
        or request.query_params.get("token")
        or ""
    ).strip()
    return bool(supplied) and hmac.compare_digest(expected, supplied)


def _public_rate_limit_enabled() -> bool:
    return os.environ.get("OES_PUBLIC_RATE_LIMIT_ENABLED", "0").strip().casefold() in {"1", "true", "yes", "on"}


def _public_rate_limit_response(request: Request, request_id: str) -> JSONResponse | None:
    if not _public_rate_limit_enabled() or not _precomputed_only_ui_enabled():
        return None
    if _public_budget_guard_path(request):
        return None
    path = request.url.path.rstrip("/") or "/"
    method = request.method.upper()
    action_paths = {"/public/fast-gcp-review", "/public/rescore-demo/run", "/public/fast-gcp-review/owner-session"}
    if method == "POST" and path in action_paths:
        bucket = "public-action"
        window_seconds = _int_env("OES_PUBLIC_ACTION_RATE_LIMIT_WINDOW_SECONDS", 60)
        limit = _int_env("OES_PUBLIC_ACTION_RATE_LIMIT_MAX_REQUESTS", 8)
    else:
        bucket = "public-read"
        window_seconds = _int_env("OES_PUBLIC_RATE_LIMIT_WINDOW_SECONDS", 60)
        limit = _int_env("OES_PUBLIC_RATE_LIMIT_MAX_REQUESTS", 120)
    if limit <= 0 or window_seconds <= 0:
        return None
    now = int(time.time())
    window = now // window_seconds
    client_key = _public_rate_limit_client_key(request)
    counter_key = (bucket, client_key, window)
    retry_after = max(1, window_seconds - (now % window_seconds))
    with _PUBLIC_RATE_LIMIT_LOCK:
        _purge_public_rate_limit_counters(window)
        count = _PUBLIC_RATE_LIMIT_COUNTERS.get(counter_key, 0) + 1
        _PUBLIC_RATE_LIMIT_COUNTERS[counter_key] = count
    if count <= limit:
        return None
    log_event(
        LOGGER,
        "public_rate_limited",
        request_id=request_id,
        method=method,
        path=path,
        bucket=bucket,
        limit=limit,
        window_seconds=window_seconds,
    )
    return JSONResponse(
        status_code=429,
        content={
            "detail": {
                "message": "public demo rate limit exceeded",
                "reason_code": "public_rate_limited",
                "bucket": bucket,
                "limit": limit,
                "window_seconds": window_seconds,
            }
        },
        headers={"X-Request-ID": request_id, "Retry-After": str(retry_after)},
    )


def _public_rate_limit_client_key(request: Request) -> str:
    remote = (request.headers.get("cf-connecting-ip") or "").strip()
    if not remote:
        remote = (request.headers.get("x-forwarded-for") or "").split(",", 1)[0].strip()
    if not remote:
        remote = request.headers.get("x-real-ip", "").strip()
    if not remote and request.client is not None:
        remote = request.client.host or ""
    salt = (
        os.environ.get("OES_PUBLIC_RATE_LIMIT_SALT", "").strip()
        or os.environ.get("GOOGLE_CLOUD_PROJECT", "").strip()
        or "ops-evidence-public-rate-limit"
    )
    digest = hashlib.sha256(f"{salt}:{remote or 'unknown'}".encode("utf-8")).hexdigest()[:24]
    return f"client-{digest}"


def _purge_public_rate_limit_counters(current_window: int) -> None:
    stale = [key for key in _PUBLIC_RATE_LIMIT_COUNTERS if key[2] < current_window - 1]
    for key in stale:
        _PUBLIC_RATE_LIMIT_COUNTERS.pop(key, None)


def _public_precomputed_read_guard(request: Request, request_id: str) -> JSONResponse | None:
    if not _precomputed_only_ui_enabled():
        return None
    if request.method.upper() not in {"GET", "HEAD"}:
        return None
    path = request.url.path.rstrip("/") or "/"
    if path in PUBLIC_PRECOMPUTED_READ_PATHS or path.startswith("/reviews/") or path.startswith("/code-profiles/"):
        return None
    return JSONResponse(
        status_code=404,
        content={"detail": "public demo exposes only precomputed review endpoints"},
        headers={"X-Request-ID": request_id},
    )


def _ui_detail_timeout_ms() -> int:
    return int(os.environ.get("OES_UI_DETAIL_TIMEOUT_MS", "9500"))


def _human_count(value: int) -> str:
    return f"{int(value):,}"


def _clear_target_cache() -> None:
    _TARGET_SET_CACHE.clear()


def _list_review_targets_cached(
    *,
    limit: int = 5,
    evidence_sha256: str | None = None,
    pending_only: bool = True,
) -> dict[str, Any]:
    ttl = _target_cache_ttl_seconds()
    key = (_store_label(), evidence_sha256 or "", limit, pending_only)
    if ttl > 0:
        cached = _TARGET_SET_CACHE.get(key)
        if cached and time.monotonic() - cached[0] < ttl:
            return deepcopy(cached[1])
    target_set = _store().list_review_targets(
        limit=limit,
        evidence_sha256=evidence_sha256,
        pending_only=pending_only,
    )
    if ttl > 0:
        _TARGET_SET_CACHE[key] = (time.monotonic(), deepcopy(target_set))
    return target_set


def _canonical_review_targets_from_snapshot(
    *,
    limit: int,
    evidence_sha256: str | None,
    pending_only: bool,
) -> dict[str, Any]:
    if not evidence_sha256:
        return {}
    store = _store()
    snapshot_response = _latest_canonical_graph_response(store, evidence_sha256)
    bundle = store.get_bundle(evidence_sha256) if hasattr(store, "get_bundle") else None
    model_artifacts = _model_run_artifacts_for_ui(evidence_sha256)
    if isinstance(bundle, dict) and model_artifacts:
        snapshot_response = resolve_canonical_review_graph_snapshot(
            store,
            bundle,
            model_runs=model_artifacts,
            multi_ai_synthesis=_multi_ai_synthesis_for_ui(evidence_sha256, bundle),
            persist_if_missing=False,
            persist_if_stale=False,
            created_by="api-readonly",
        )
    graph = snapshot_response.get("canonical_review_graph") if isinstance(snapshot_response, dict) else {}
    if not isinstance(graph, dict) or graph.get("schema_version") != "canonical_review_graph.v1":
        return {}
    target_set = _target_set_from_canonical_graph(graph, {})
    targets = [target for target in target_set.get("targets") or [] if isinstance(target, dict)]
    if pending_only:
        targets = [
            target
            for target in targets
            if str(target.get("status") or "pending") in {"pending", "needs_more_data"}
        ]
    target_set["targets"] = targets[: max(0, int(limit))]
    target_set["canonical_graph_status"] = str(snapshot_response.get("canonical_graph_status") or "persisted")
    target_set["canonical_graph_sha256"] = str(
        snapshot_response.get("canonical_graph_sha256") or graph.get("canonical_graph_sha256") or ""
    )
    target_set["input_fingerprint_sha256"] = str(
        snapshot_response.get("input_fingerprint_sha256") or graph.get("input_fingerprint_sha256") or ""
    )
    target_set["source"] = "canonical_review_graph"
    return target_set


@router.head("/")
def index_head() -> Response:
    return Response(status_code=200)


@router.get("/favicon.ico", include_in_schema=False)
def favicon() -> Response:
    svg = (
        "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'>"
        "<rect width='64' height='64' rx='14' fill='#202944'/>"
        "<text x='32' y='39' text-anchor='middle' "
        "font-family='Arial, sans-serif' font-size='22' font-weight='700' fill='#f7f2e7'>OE</text>"
        "</svg>"
    )
    return Response(content=svg, media_type="image/svg+xml")


@router.get("/health")
def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "mode": "precomputed_public" if _precomputed_only_ui_enabled() else "api",
    }


@router.get("/", response_class=HTMLResponse)
def index(evidence_sha256: str | None = None, full: bool = False) -> str:
    if _precomputed_only_ui_enabled() and not evidence_sha256:
        return _public_precomputed_landing_page()
    precomputed = _require_precomputed_review_for_public_read(evidence_sha256)
    if evidence_sha256 and precomputed is not None:
        evidence_sha256 = _canonical_precomputed_review_sha(evidence_sha256)
        if full:
            return _render_precomputed_review_detail_page(evidence_sha256, precomputed)
        if _fast_initial_ui_enabled():
            return _fast_review_shell(evidence_sha256, precomputed=precomputed)
    if evidence_sha256 and _fast_initial_ui_enabled() and not full:
        precomputed = _precomputed_review_payload(evidence_sha256)
        display_sha256 = _canonical_precomputed_review_sha(evidence_sha256) if precomputed else evidence_sha256
        return _fast_review_shell(display_sha256, precomputed=precomputed)
    return _render_full_review_page(evidence_sha256)


@router.get("/ui/full-review-page", response_class=HTMLResponse)
def full_review_page(evidence_sha256: str | None = None, full: bool = False) -> str:
    precomputed = _require_precomputed_review_for_public_read(evidence_sha256, require_evidence_sha=True)
    if evidence_sha256 and precomputed is not None:
        evidence_sha256 = _canonical_precomputed_review_sha(evidence_sha256)
        return _render_precomputed_review_detail_page(evidence_sha256, precomputed)
    if evidence_sha256 and _fast_initial_ui_enabled() and not full:
        precomputed = _precomputed_review_payload(evidence_sha256)
        display_sha256 = _canonical_precomputed_review_sha(evidence_sha256) if precomputed else evidence_sha256
        return _render_fast_review_detail_page(display_sha256, precomputed=precomputed)
    return _render_full_review_page(evidence_sha256)


@router.get("/reviews/{evidence_sha256}", response_class=HTMLResponse)
@router.get("/reviews/{evidence_sha256}/", response_class=HTMLResponse)
def static_review_page(evidence_sha256: str) -> str:
    precomputed = _require_precomputed_review_for_public_read(evidence_sha256, require_evidence_sha=True)
    if precomputed is not None:
        evidence_sha256 = _canonical_precomputed_review_sha(evidence_sha256)
        return _render_precomputed_review_detail_page(evidence_sha256, precomputed)
    return _render_full_review_page(evidence_sha256)


@router.get("/reviews/{evidence_sha256}/report.md", response_class=PlainTextResponse)
def static_review_markdown_report(evidence_sha256: str) -> str:
    precomputed = _require_precomputed_review_for_public_read(evidence_sha256, require_evidence_sha=True)
    if precomputed is not None:
        evidence_sha256 = _canonical_precomputed_review_sha(evidence_sha256)
        return _render_precomputed_markdown_report(evidence_sha256, precomputed)
    raise HTTPException(status_code=404, detail="precomputed review not found")


@router.get("/code-profiles/{profile_id}", response_class=HTMLResponse)
@router.get("/code-profiles/{profile_id}/", response_class=HTMLResponse)
def code_profile_review_page(profile_id: str) -> str:
    page = _code_profile_review_artifact(profile_id, "index.html")
    if page is None:
        raise HTTPException(status_code=404, detail="code profile review not found")
    return page


@router.get("/code-profiles/{profile_id}/report.md", response_class=PlainTextResponse)
def code_profile_markdown_report(profile_id: str) -> str:
    report = _code_profile_review_artifact(profile_id, "report.md")
    if report is None:
        raise HTTPException(status_code=404, detail="code profile report not found")
    return report


def _code_profile_review_artifact(profile_id: str, filename: str) -> str | None:
    clean_id = _clean_code_profile_id(profile_id)
    if not clean_id:
        return None
    if filename not in {"index.html", "report.md", "payload.json"}:
        return None
    for directory in _code_profile_review_dirs():
        path = directory / clean_id / filename
        try:
            return path.read_text(encoding="utf-8")
        except FileNotFoundError:
            continue
        except Exception:
            continue
    for uri in _code_profile_review_gcs_uris(clean_id, filename):
        try:
            from ops_evidence_synthesis.gcp.storage import read_text

            return read_text(uri)
        except Exception:
            continue
    return None


def _clean_code_profile_id(profile_id: str) -> str:
    value = str(profile_id or "").strip()
    allowed = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ._-"
    if not 8 <= len(value) <= 128:
        return ""
    if any(char not in allowed for char in value):
        return ""
    return value


def _code_profile_review_dirs() -> list[Path]:
    configured = [
        Path(item)
        for item in os.environ.get("OES_CODE_PROFILE_REVIEW_DIRS", "").split(os.pathsep)
        if item.strip()
    ]
    single = os.environ.get("OES_CODE_PROFILE_REVIEW_DIR", "").strip()
    if single:
        configured.insert(0, Path(single))
    return configured


def _code_profile_review_gcs_uris(profile_id: str, filename: str) -> list[str]:
    prefixes = _code_profile_review_gcs_prefixes()
    return [f"{prefix.rstrip('/')}/{profile_id}/{filename}" for prefix in prefixes if prefix.startswith("gs://")]


def _code_profile_review_gcs_prefixes() -> list[str]:
    prefixes: list[str] = []
    single = os.environ.get("OES_CODE_PROFILE_REVIEW_GCS_PREFIX", "").strip()
    if single:
        prefixes.append(single)
    for item in os.environ.get("OES_CODE_PROFILE_REVIEW_GCS_PREFIXES", "").split(","):
        item = item.strip()
        if item:
            prefixes.append(item)
    if prefixes:
        return prefixes
    for precomputed_prefix in _precomputed_review_gcs_prefixes_from_env():
        derived = _derive_code_profile_review_gcs_prefix(precomputed_prefix)
        if derived:
            prefixes.append(derived)
    return prefixes


def _precomputed_review_gcs_prefixes_from_env() -> list[str]:
    prefixes: list[str] = []
    single = os.environ.get("OES_PRECOMPUTED_REVIEW_GCS_PREFIX", "").strip()
    if single:
        prefixes.append(single)
    for item in os.environ.get("OES_PRECOMPUTED_REVIEW_GCS_PREFIXES", "").split(","):
        item = item.strip()
        if item:
            prefixes.append(item)
    return prefixes


def _derive_code_profile_review_gcs_prefix(precomputed_prefix: str) -> str:
    clean = str(precomputed_prefix or "").strip().rstrip("/")
    if not clean.startswith("gs://") or "/" not in clean[5:]:
        return ""
    parent, _slash, _name = clean.rpartition("/")
    if not parent.startswith("gs://"):
        return ""
    return f"{parent}/code-profile-pages"


@router.get("/ui/summary")
def ui_summary(evidence_sha256: str) -> dict[str, Any]:
    if not evidence_sha256:
        raise HTTPException(status_code=400, detail="evidence_sha256 is required")
    precomputed = _require_precomputed_review_for_public_read(evidence_sha256)
    if precomputed is not None:
        evidence_sha256 = _canonical_precomputed_review_sha(evidence_sha256)
    return _review_summary_for_ui(evidence_sha256)


@router.get("/ui/api", response_class=HTMLResponse)
def public_api_view(evidence_sha256: str | None = None) -> str:
    precomputed = _require_precomputed_review_for_public_read(evidence_sha256, require_evidence_sha=True)
    if precomputed is not None and evidence_sha256:
        evidence_sha256 = _canonical_precomputed_review_sha(evidence_sha256)
        return _render_precomputed_api_page(evidence_sha256, precomputed)
    raise HTTPException(status_code=404, detail="precomputed review not found")


@router.get("/ui/review-graph", response_class=HTMLResponse)
def public_review_graph_view(evidence_sha256: str | None = None) -> str:
    precomputed = _require_precomputed_review_for_public_read(evidence_sha256, require_evidence_sha=True)
    if precomputed is not None and evidence_sha256:
        evidence_sha256 = _canonical_precomputed_review_sha(evidence_sha256)
        return _render_precomputed_graph_page(evidence_sha256, precomputed)
    raise HTTPException(status_code=404, detail="precomputed review not found")


@router.get("/ui/report.md", response_class=PlainTextResponse)
def public_markdown_report(evidence_sha256: str | None = None) -> str:
    precomputed = _require_precomputed_review_for_public_read(evidence_sha256, require_evidence_sha=True)
    if precomputed is not None and evidence_sha256:
        evidence_sha256 = _canonical_precomputed_review_sha(evidence_sha256)
        return _render_precomputed_markdown_report(evidence_sha256, precomputed)
    raise HTTPException(status_code=404, detail="precomputed review not found")


@router.get("/ui/rescore-demo", response_class=HTMLResponse)
def public_rescore_demo_view(id: str = "amazon-notify-more-data-rescore") -> str:
    html = _render_rescore_demo_page(id)
    if not html:
        raise HTTPException(status_code=404, detail="rescore demo not found")
    return html


@router.post("/public/rescore-demo/run")
def public_rescore_demo_run(request: Request, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    if not _public_rescore_demo_run_enabled():
        raise HTTPException(status_code=404, detail="public rescore demo run is disabled")
    payload = payload if isinstance(payload, dict) else {}
    demo_id = str(payload.get("demo_id") or _fast_gcp_rescore_demo_id()).strip()
    run_id = _public_rescore_run_id_from_payload(payload)
    live_model = bool(payload.get("live_model"))
    if live_model and not _fast_gcp_review_owner_access(request, payload):
        raise HTTPException(
            status_code=403,
            detail={
                "message": "owner access is required for live model rescore",
                "reason_code": "owner_access_required",
            },
        )
    if live_model:
        _raise_if_public_fast_gcp_review_disabled(run_id=run_id, cross_check=False)
    return _run_public_fixed_rescore_demo(demo_id=demo_id, run_id=run_id, live_model=live_model)


@router.get("/ui/fast-gcp-review", response_class=HTMLResponse)
def public_fast_gcp_review_view() -> str:
    if not _public_fast_gcp_review_enabled():
        raise HTTPException(status_code=404, detail="fast GCP review is disabled")
    _raise_if_public_fast_gcp_review_disabled()
    return _render_fast_gcp_review_view()


@router.get("/public/fast-gcp-review/status")
def public_fast_gcp_review_status(run_id: str) -> dict[str, Any]:
    if not _public_fast_gcp_review_enabled():
        raise HTTPException(status_code=404, detail="fast GCP review is disabled")
    safe_run_id = _validate_fast_gcp_review_run_id(run_id)
    return _read_fast_gcp_review_status(safe_run_id) or _empty_fast_gcp_review_status(safe_run_id)


@router.post("/public/fast-gcp-review/owner-session")
def public_fast_gcp_review_owner_session(payload: dict[str, Any] | None = None) -> JSONResponse:
    if not _public_fast_gcp_review_enabled():
        raise HTTPException(status_code=404, detail="fast GCP review is disabled")
    payload = payload if isinstance(payload, dict) else {}
    if not _fast_gcp_review_owner_token_matches(str(payload.get("owner_token") or "")):
        raise HTTPException(status_code=403, detail="owner token invalid")
    response = JSONResponse(
        {
            "schema_version": "public_fast_gcp_review_owner_session.v1",
            "status": "ok",
            "owner_access": True,
        }
    )
    _set_fast_gcp_review_owner_cookie(response)
    return response


@router.get("/public/fast-gcp-review/owner-session")
def public_fast_gcp_review_owner_session_status(request: Request) -> dict[str, Any]:
    return {
        "schema_version": "public_fast_gcp_review_owner_session.v1",
        "owner_access": _fast_gcp_review_owner_access(request),
    }


@router.post("/internal/budget-guard/fast-gcp-review")
def budget_guard_fast_gcp_review(request: Request, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    if not _budget_guard_token():
        raise HTTPException(
            status_code=503,
            detail={
                "message": "budget guard token is not configured",
                "reason_code": "budget_guard_token_unavailable",
            },
        )
    if not _budget_guard_token_matches(request):
        raise HTTPException(status_code=403, detail="budget guard token required")
    notification = _budget_guard_notification_from_payload(payload if isinstance(payload, dict) else {})
    decision = _budget_guard_decision(notification)
    if not decision["disable"]:
        return {
            "schema_version": "budget_guard_fast_gcp_review.v1",
            "status": "ignored",
            "decision": decision,
            "notification": notification,
        }
    state = {
        "schema_version": "public_fast_gcp_review_disable_state.v1",
        "disabled": True,
        "disabled_at": utc_now(),
        "reason_code": "budget_threshold_exceeded",
        "message": "Public Fast GCP Review is disabled because the billing budget threshold was exceeded.",
        "budget": decision,
        "notification": notification,
    }
    _write_fast_gcp_review_disable_state(state)
    return {
        "schema_version": "budget_guard_fast_gcp_review.v1",
        "status": "disabled",
        "decision": decision,
        "disable_state": state,
    }


@router.post("/public/fast-gcp-review")
def public_fast_gcp_review(request: Request, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    if not _public_fast_gcp_review_enabled():
        raise HTTPException(status_code=404, detail="fast GCP review is disabled")
    payload = payload if isinstance(payload, dict) else {}
    cross_check = _fast_gcp_cross_check_requested(payload)
    run_id = _fast_gcp_review_run_id_from_payload(payload, cross_check=cross_check)
    _raise_if_public_fast_gcp_review_disabled(run_id=run_id, cross_check=cross_check)
    force = bool(payload.get("force"))
    cache_key = _fast_gcp_review_cache_key(cross_check=cross_check)
    ttl = _fast_gcp_review_cache_seconds()
    _write_fast_gcp_review_status(
        _fast_gcp_review_status_payload(
            run_id=run_id,
            cross_check=cross_check,
            status="running",
            current_step="queued",
            progress_percent=2,
            message="Fast GCP Review request accepted.",
            cache={
                "status": "force_live_api_requested" if force else "checking_recent_public_fast_review_cache",
                "ttl_seconds": ttl,
            },
        )
    )
    with _FAST_GCP_REVIEW_LOCK:
        cached = _FAST_GCP_REVIEW_CACHE.get(cache_key)
        if not force and ttl > 0 and cached and time.monotonic() - cached[0] < ttl:
            cached_payload = deepcopy(cached[1])
            cached_from_run_id = str(cached_payload.get("run_id") or "")
            cached_payload["run_id"] = run_id
            if isinstance(cached_payload.get("urls"), dict):
                cached_payload["urls"]["status"] = _fast_gcp_review_status_url(run_id)
            cached_payload["cache"] = {
                "status": "served_from_recent_public_fast_review_cache",
                "ttl_seconds": ttl,
                "live_api_called": False,
                "cached_from_run_id": cached_from_run_id,
            }
            _write_fast_gcp_review_status(
                _fast_gcp_review_status_payload(
                    run_id=run_id,
                    cross_check=cross_check,
                    status="succeeded",
                    current_step="completed",
                    progress_percent=100,
                    message="Served from recent public Fast GCP Review cache.",
                    cache=cached_payload.get("cache") if isinstance(cached_payload.get("cache"), dict) else {},
                    quota={
                        "status": "cache_hit_no_live_quota_consumed",
                        "live_api_allowed": True,
                    },
                    providers=cached_payload.get("providers") if isinstance(cached_payload.get("providers"), dict) else {},
                    review=cached_payload.get("review") if isinstance(cached_payload.get("review"), dict) else {},
                    urls=cached_payload.get("urls") if isinstance(cached_payload.get("urls"), dict) else {},
                )
            )
            cached_payload["quota"] = {
                "status": "cache_hit_no_live_quota_consumed",
                "live_api_allowed": True,
            }
            return cached_payload
        try:
            quota = _consume_fast_gcp_review_quota(request, payload=payload, run_id=run_id, cross_check=cross_check)
        except HTTPException as exc:
            detail = exc.detail if isinstance(exc.detail, dict) else {}
            message = str(detail.get("message") or exc.detail or "Public Fast GCP Review quota was exceeded.")
            _write_fast_gcp_review_status(
                _fast_gcp_review_status_payload(
                    run_id=run_id,
                    cross_check=cross_check,
                    status="failed",
                    current_step="quota_blocked",
                    progress_percent=100,
                    message=message,
                    reason_code=str(detail.get("reason_code") or "quota_blocked"),
                    quota=detail.get("quota") if isinstance(detail.get("quota"), dict) else {},
                )
            )
            raise
        _write_fast_gcp_review_status(
            _fast_gcp_review_status_payload(
                run_id=run_id,
                cross_check=cross_check,
                status="running",
                current_step="quota_accepted",
                progress_percent=6,
                message="Live model quota accepted for this fixed public demo run.",
                quota=quota,
                cache={
                    "status": "force_live_api_requested" if force else "cache_miss_live_quota_consumed",
                    "ttl_seconds": ttl,
                },
            )
        )
        try:
            result = _run_public_fast_gcp_review(cross_check=cross_check, run_id=run_id)
            result["quota"] = quota
            _write_fast_gcp_review_status(
                _fast_gcp_review_status_payload(
                    run_id=run_id,
                    cross_check=cross_check,
                    status="succeeded",
                    current_step="completed",
                    progress_percent=100,
                    message=f"Completed. Public quota status: {quota.get('status', 'unknown')}.",
                    providers=result.get("providers") if isinstance(result.get("providers"), dict) else {},
                    review=result.get("review") if isinstance(result.get("review"), dict) else {},
                    urls=result.get("urls") if isinstance(result.get("urls"), dict) else {},
                    cache=result.get("cache") if isinstance(result.get("cache"), dict) else {},
                    quota=quota,
                    extra={
                        "timing": result.get("timing") if isinstance(result.get("timing"), dict) else {},
                        "gcp_runtime": result.get("gcp_runtime") if isinstance(result.get("gcp_runtime"), dict) else {},
                    },
                )
            )
            if ttl > 0:
                _FAST_GCP_REVIEW_CACHE[cache_key] = (time.monotonic(), deepcopy(result))
            return result
        except Exception as exc:
            message = safe_provider_error_message(str(exc), max_chars=220)
            _write_fast_gcp_review_status(
                _fast_gcp_review_status_payload(
                    run_id=run_id,
                    cross_check=cross_check,
                    status="failed",
                    current_step="failed",
                    progress_percent=100,
                    message=message or "Fast GCP Review failed.",
                    reason_code="fast_gcp_review_failed",
                )
            )
            raise


def _render_fast_gcp_review_view() -> str:
    model = _fast_gcp_review_model_name()
    sample_rows = _fast_gcp_review_sample_rows()
    cross_check_rows = _fast_gcp_cross_check_sample_rows()
    full_review_sha = "b99da97cab19f026b5475cdaa6100fdd6ebb6d96466a43e6b62a44b99ac414ec"
    rescore_demo_url = _fast_gcp_rescore_demo_url()
    system_preview_html = _render_fast_gcp_system_code_preview()
    cache_note = "Live review calls the model API and consumes public demo quota; stored review URLs remain available after completion."
    cache_note_js = json.dumps(cache_note)
    return f"""<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Fast GCP Review - Ops Evidence Synthesis</title>
    <style>
      :root {{
        color-scheme: light;
        --bg: #eef2f7;
        --surface: #fff;
        --ink: #0f1b2d;
        --muted: #526173;
        --line: #d9e1ec;
        --accent: #2a6fdb;
        --green: #12836b;
        --mono: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      }}
      * {{ box-sizing: border-box; }}
      body {{ margin: 0; background: var(--bg); color: var(--ink); font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }}
      main {{ width: min(calc(100% - 40px), 1120px); margin: 0 auto; padding: 42px 0; }}
      .panel {{ border: 1px solid var(--line); border-radius: 8px; background: var(--surface); padding: 24px; box-shadow: 0 18px 48px rgba(15,27,45,.08); }}
      .top {{ display: flex; justify-content: space-between; gap: 16px; align-items: start; margin-bottom: 22px; }}
      h1 {{ margin: 0 0 10px; font-size: clamp(36px, 6vw, 64px); line-height: .96; letter-spacing: 0; }}
      p {{ margin: 0; color: var(--muted); line-height: 1.55; }}
      .badge {{ display: inline-flex; border: 1px solid #d3e4fb; border-radius: 999px; padding: 7px 10px; background: #e7f0fc; color: var(--accent); font: 800 12px/1 var(--mono); }}
      .grid {{ display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 10px; margin: 22px 0; }}
      .metric {{ border: 1px solid var(--line); border-radius: 8px; padding: 13px; background: #f8fafc; }}
      .metric span {{ display: block; color: var(--muted); font-size: 12px; font-weight: 800; }}
      .metric b {{ display: block; margin-top: 7px; font-size: 18px; overflow-wrap: anywhere; }}
      .source-preview {{ border-top: 1px solid var(--line); border-bottom: 1px solid var(--line); margin: 20px 0 0; padding: 18px 0; }}
      .summary-gate {{ display: flex; justify-content: space-between; align-items: center; gap: 16px; border-top: 1px solid var(--line); margin-top: 20px; padding-top: 18px; }}
      .summary-gate-text {{ min-width: 0; }}
      .summary-status {{ margin-top: 6px; font-size: 14px; }}
      .section-kicker {{ color: var(--accent); font: 850 12px/1 var(--mono); text-transform: uppercase; }}
      h2 {{ margin: 8px 0 6px; font-size: 24px; letter-spacing: 0; }}
      .preview-note {{ margin-top: 6px; max-width: 820px; }}
      .preview-grid {{ display: grid; grid-template-columns: 1.05fr 1fr 1fr; gap: 18px; margin-top: 16px; }}
      .preview-column {{ min-width: 0; }}
      .preview-column h3 {{ margin: 0 0 8px; font-size: 14px; }}
      .preview-list {{ display: grid; gap: 8px; margin: 0; padding: 0; list-style: none; }}
      .preview-list li {{ border-left: 3px solid #c7dbf6; padding: 0 0 0 10px; color: var(--muted); line-height: 1.45; }}
      .preview-list b {{ color: var(--ink); }}
      .chip-row {{ display: flex; flex-wrap: wrap; gap: 6px; margin-top: 12px; }}
      .chip {{ display: inline-flex; max-width: 100%; border: 1px solid var(--line); border-radius: 999px; background: #f8fafc; color: var(--ink); padding: 6px 8px; font: 750 12px/1 var(--mono); overflow-wrap: anywhere; }}
      .hash-line {{ margin-top: 12px; color: var(--muted); font: 12px/1.5 var(--mono); overflow-wrap: anywhere; }}
      .loop-panel {{ border: 1px solid #d3e4fb; border-radius: 8px; background: #f4f8fe; margin-top: 18px; padding: 16px; }}
      .loop-panel strong {{ display: block; margin-bottom: 5px; color: var(--ink); }}
      button, a.button {{ display: inline-flex; align-items: center; justify-content: center; border: 1px solid var(--accent); border-radius: 8px; background: var(--accent); color: #fff; padding: 11px 14px; font-weight: 850; text-decoration: none; cursor: pointer; }}
      button:disabled {{ opacity: .55; cursor: not-allowed; }}
      a.secondary {{ background: #fff; color: var(--ink); border-color: var(--line); }}
      .actions {{ display: flex; flex-wrap: wrap; gap: 10px; margin-top: 18px; }}
      pre {{ margin: 18px 0 0; white-space: pre-wrap; overflow-wrap: anywhere; border: 1px solid var(--line); border-radius: 8px; background: #0f1b2d; color: #e6edf7; padding: 14px; font: 12px/1.5 var(--mono); }}
      .status {{ margin-top: 16px; color: var(--muted); font-weight: 750; }}
      .status-progress {{ margin-top: 10px; }}
      .status-progress div {{ width: 0%; animation: none; transform: none; transition: width .25s ease; }}
      .status-detail {{ margin-top: 10px; border: 1px solid var(--line); border-radius: 8px; background: #f8fafc; padding: 12px; color: var(--muted); font: 12px/1.5 var(--mono); overflow-wrap: anywhere; }}
      .status-detail b {{ color: var(--ink); }}
      .ok {{ color: var(--green); }}
      [hidden] {{ display: none !important; }}
      @media (max-width: 760px) {{ .top, .grid, .summary-gate {{ grid-template-columns: 1fr; display: grid; }} }}
    </style>
  </head>
  <body>
    <main>
      <section class="panel">
        <div class="top">
          <div>
            <span class="badge">Fast GCP Review</span>
            <h1>Run a fixed sanitized sample on GCP.</h1>
            <p>This public action uses a bundled amazon-notify sanitized sample only. It calls Vertex Gemini Flash Lite from Cloud Run and builds the review with the current source-approved evidence logic. The cross-check path runs Gemini Flash Lite and Gemma 4 over a smaller fixed prefix of the same fixture so the live comparison stays bounded. It does not accept arbitrary logs or URLs.</p>
          </div>
          <a class="button secondary" href="/">Back</a>
        </div>
        <div class="grid">
          <div class="metric"><span>Sample</span><b>amazon-notify</b></div>
          <div class="metric"><span>Rows</span><b>{_human_count(sample_rows)}</b></div>
          <div class="metric"><span>Cross-check rows</span><b>{_human_count(cross_check_rows)}</b></div>
          <div class="metric"><span>Primary model</span><b>{_html(model)}</b></div>
          <div class="metric"><span>Logic</span><b>{_html(_FAST_GCP_REVIEW_LOGIC_REVISION)}</b></div>
        </div>
        <div class="summary-gate">
          <div class="summary-gate-text">
            <span class="section-kicker">Code summary</span>
            <h2>Load the sanitized system summary before running the live review.</h2>
            <p class="summary-status" id="summary-status">The public demo reads a precomputed sanitized source summary. Raw source and raw logs are not accepted from this page.</p>
          </div>
          <button id="load-code-summary" type="button">Load Sanitized Code Summary</button>
        </div>
        {system_preview_html}
        <div class="actions">
          <button id="run-fast-review" type="button" disabled>Run Live Fast Review</button>
          <button id="run-cross-check" type="button" disabled>Run Live Cross-check</button>
          <a class="button secondary" href="{_html(rescore_demo_url)}">Watch More Data Rescore</a>
          <a class="button secondary" href="/ui/full-review-page?evidence_sha256={full_review_sha}">Open Full Forensic Review</a>
        </div>
        <div class="loop-panel">
          <strong>After the fast review, inspect the evidence loop.</strong>
          <p>The fixed 2,000-row run usually produces validation targets. The More Data Rescore demo shows how added evidence can change review priority while the final incident cause remains human-gated.</p>
          <p><strong>Quota guarded.</strong> Live clicks consume guarded public demo quota and can be stopped by the budget guard.</p>
        </div>
        <p class="status" id="status">Load the sanitized code summary first. {_html(cache_note)}</p>
        <div class="progress status-progress" role="progressbar" aria-valuemin="0" aria-valuemax="100" aria-valuenow="0" aria-label="Fast GCP Review progress">
          <div id="status-progress-bar"></div>
        </div>
        <div class="status-detail" id="status-detail" hidden></div>
        <div class="actions" id="result-links" hidden></div>
        <pre id="result" hidden></pre>
      </section>
    </main>
    <script>
      const button = document.getElementById("run-fast-review");
      const crossButton = document.getElementById("run-cross-check");
      const summaryButton = document.getElementById("load-code-summary");
      const sourcePreviewPanel = document.getElementById("source-preview-panel");
      const summaryStatus = document.getElementById("summary-status");
      const statusEl = document.getElementById("status");
      const resultEl = document.getElementById("result");
      const linksEl = document.getElementById("result-links");
      const progressBar = document.getElementById("status-progress-bar");
      const statusDetail = document.getElementById("status-detail");
      const cacheNote = {cache_note_js};
      let statusPoller = null;
      let activeRunStartedAt = 0;
      let codeSummaryLoaded = false;
      let ownerAccess = false;

      const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (ch) => ({{
        "&": "&amp;",
        "<": "&lt;",
        ">": "&gt;",
        '"': "&quot;",
        "'": "&#39;"
      }}[ch]));

      function createRunId(crossCheck) {{
        const prefix = crossCheck ? "fast-cross-check-lite" : "fast-gcp-review";
        if (globalThis.crypto && typeof globalThis.crypto.randomUUID === "function") {{
          return `${{prefix}}-${{globalThis.crypto.randomUUID().replace(/-/g, "").slice(0, 12)}}`;
        }}
        return `${{prefix}}-${{Date.now().toString(36)}}${{Math.random().toString(16).slice(2, 10)}}`;
      }}

      function providerText(status) {{
        const providers = status?.providers || {{}};
        return `${{Number(providers.success || 0)}} ok / ${{Number(providers.total || 0)}} total`;
      }}

      function renderRunStatus(status) {{
        if (!status || typeof status !== "object") return;
        const progress = Math.max(0, Math.min(100, Math.round(Number(status.progress_percent || 0))));
        const step = String(status.current_step || status.status || "running").replace(/[_-]+/g, " ");
        const elapsed = activeRunStartedAt ? Math.max(0, Math.round((Date.now() - activeRunStartedAt) / 1000)) : 0;
        if (progressBar) {{
          progressBar.style.width = `${{progress}}%`;
          progressBar.parentElement?.setAttribute("aria-valuenow", String(progress));
        }}
        if (statusEl) {{
          statusEl.textContent = `${{step}} - ${{progress}}%${{elapsed ? ` - ${{elapsed}}s elapsed` : ""}}`;
        }}
        if (statusDetail) {{
          const message = status.message || "";
          const reason = status.reason_code ? `<br><b>Reason</b> ${{esc(status.reason_code)}}` : "";
          const quota = status.quota || {{}};
          const quotaRemaining = quota.daily_remaining !== undefined ? `, daily remaining ${{quota.daily_remaining}}` : "";
          const quotaLine = quota.status ? `<br><b>Quota</b> ${{esc(quota.status)}}${{esc(quotaRemaining)}}` : "";
          statusDetail.innerHTML = `<b>Run</b> ${{esc(status.run_id || "")}}<br><b>Providers</b> ${{esc(providerText(status))}}<br><b>Message</b> ${{esc(message)}}${{reason}}${{quotaLine}}`;
          statusDetail.hidden = false;
        }}
      }}

      async function pollRunStatus(runId) {{
        if (!runId) return;
        try {{
          const response = await fetch(`/public/fast-gcp-review/status?run_id=${{encodeURIComponent(runId)}}`, {{
            headers: {{ "accept": "application/json" }}
          }});
          if (!response.ok) return;
          const status = await response.json();
          renderRunStatus(status);
          if (["succeeded", "failed"].includes(String(status.status || ""))) {{
            stopStatusPolling();
          }}
        }} catch (error) {{}}
      }}

      function startStatusPolling(runId) {{
        stopStatusPolling();
        activeRunStartedAt = Date.now();
        renderRunStatus({{
          run_id: runId,
          status: "running",
          current_step: "queued",
          progress_percent: 1,
          message: "Fast GCP Review request queued.",
          providers: {{ total: 0, success: 0 }}
        }});
        pollRunStatus(runId);
        statusPoller = window.setInterval(() => pollRunStatus(runId), 1500);
      }}

      function stopStatusPolling() {{
        if (statusPoller) {{
          window.clearInterval(statusPoller);
          statusPoller = null;
        }}
      }}

      function readyMessage() {{
        return ownerAccess ? "Owner demo mode active; public live quota will not be consumed for this browser session." : cacheNote;
      }}

      async function activateOwnerSessionFromHash() {{
        const hash = String(window.location.hash || "").replace(/^#/, "");
        if (!hash) return;
        const params = new URLSearchParams(hash);
        const token = params.get("owner_token") || params.get("owner-token");
        if (!token) return;
        window.history.replaceState(null, "", window.location.pathname + window.location.search);
        try {{
          const response = await fetch("/public/fast-gcp-review/owner-session", {{
            method: "POST",
            headers: {{ "content-type": "application/json", "accept": "application/json" }},
            body: JSON.stringify({{ owner_token: token }})
          }});
          const payload = await response.json();
          if (!response.ok || !payload.owner_access) return;
          ownerAccess = true;
          if (summaryStatus) summaryStatus.textContent = "Owner demo mode active. Load the sanitized summary, then run the live review.";
          if (statusEl) statusEl.textContent = "Owner demo mode active. Public live quota will not be consumed for this browser session.";
        }} catch (error) {{}}
      }}

      function loadCodeSummary() {{
        codeSummaryLoaded = true;
        if (sourcePreviewPanel) sourcePreviewPanel.hidden = false;
        if (summaryButton) summaryButton.disabled = true;
        if (summaryStatus) summaryStatus.textContent = "Sanitized code summary loaded. Live review actions are now available.";
        button.disabled = false;
        crossButton.disabled = false;
        statusEl.textContent = "Ready. " + readyMessage();
      }}

      async function runFastReview(crossCheck) {{
        if (!codeSummaryLoaded) {{
          loadCodeSummary();
        }}
        const runId = createRunId(crossCheck);
        button.disabled = true;
        crossButton.disabled = true;
        startStatusPolling(runId);
        resultEl.hidden = true;
        linksEl.hidden = true;
        linksEl.innerHTML = "";
        try {{
          const response = await fetch("/public/fast-gcp-review", {{
            method: "POST",
            headers: {{ "content-type": "application/json" }},
            body: JSON.stringify({{ cross_check: crossCheck, run_id: runId, force: true }})
          }});
          const payload = await response.json();
          if (!response.ok) {{
            const detail = payload.detail;
            const message = detail && typeof detail === "object" ? (detail.message || JSON.stringify(detail)) : (detail || JSON.stringify(payload));
            throw new Error(message);
          }}
          stopStatusPolling();
          renderRunStatus({{
            run_id: runId,
            status: "succeeded",
            current_step: "completed",
            progress_percent: 100,
            message: `Completed. Wall time ${{payload.timing.wall_seconds}}s.`,
            providers: payload.providers || {{}}
          }});
          statusEl.innerHTML = "<span class='ok'>Completed.</span> Wall time " + payload.timing.wall_seconds + "s, providers " + payload.providers.success + "/" + payload.providers.total + ".";
          const links = [
            ["Detail", payload.urls.detail],
            ["API", payload.urls.api],
            ["Graph", payload.urls.graph],
            ["Status", payload.urls.status],
            ["More Data Rescore", payload.urls.rescore],
            ["Full Forensic Review", "/ui/full-review-page?evidence_sha256={full_review_sha}"]
          ];
          linksEl.innerHTML = links.map(([label, url]) => "<a class='button secondary' href='" + url + "'>" + label + "</a>").join("");
          linksEl.hidden = false;
          resultEl.textContent = JSON.stringify(payload, null, 2);
          resultEl.hidden = false;
        }} catch (error) {{
          stopStatusPolling();
          if (progressBar) progressBar.style.width = "100%";
          statusEl.textContent = "Fast review failed: " + error.message;
        }} finally {{
          button.disabled = !codeSummaryLoaded;
          crossButton.disabled = !codeSummaryLoaded;
        }}
      }}
      activateOwnerSessionFromHash();
      summaryButton.addEventListener("click", loadCodeSummary);
      button.addEventListener("click", () => runFastReview(false));
      crossButton.addEventListener("click", () => runFastReview(true));
    </script>
  </body>
</html>"""


def _render_fast_gcp_system_code_preview() -> str:
    source_context, source_analysis, profile_draft, approved_profile = _fast_gcp_profile_context()
    preview = _fast_gcp_system_code_preview(
        source_context=source_context,
        source_analysis=source_analysis,
        profile_draft=profile_draft,
        approved_profile=approved_profile,
    )
    components = preview["components"][:3]
    metrics = preview["metric_semantics"][:3]
    collectors = preview["collector_mappings"][:3]
    component_html = _preview_list_html(
        [
            (
                f"<b>{_html(item['name'])}</b> - {_html(item['subsystem'])} / {_html(item['role'])}; "
                f"confidence {_html(item['confidence'])}"
            )
            for item in components
        ],
        "No component candidates available.",
    )
    metric_html = _preview_list_html(
        [
            (
                f"<b>{_html(item['name'])}</b> - zero={_html(item['zero_behavior'])}, "
                f"increase={_html(item['increase_behavior'])}; {_html(item['semantic_type'])}"
            )
            for item in metrics
        ],
        "No metric semantics available.",
    )
    collector_html = _preview_list_html(
        [
            (
                f"<b>{_html(item['request_type'])}</b> - {_html(', '.join(item['collectors']))}; "
                f"{_html(item['safety_level'])}"
            )
            for item in collectors
        ],
        "No collector mappings available.",
    )
    chips = [
        f"project:{preview['project_type']}",
        f"profile:{preview['profile_id']}",
        f"confidence:{preview['overall_confidence']}",
        f"raw_source:{preview['raw_source_policy']}",
        f"raw_env:{preview['raw_env_policy']}",
    ]
    chip_html = "".join(f"<span class=\"chip\">{_html(chip)}</span>" for chip in chips)
    languages = ", ".join(preview["detected_languages"]) or "not detected"
    entrypoints = ", ".join(preview["entrypoint_candidates"]) or "not detected"
    return f"""
        <section class="source-preview" id="source-preview-panel" hidden aria-label="Sanitized system code preview">
          <span class="section-kicker">Sanitized system code preview</span>
          <h2>What the fixed sample knows about the system before the live model call</h2>
          <p class="preview-note">This preview is generated from sanitized source-context artifacts and the approved profile. It is context, not incident evidence; the live review still requires cited log Evidence IDs before any target can be promoted.</p>
          <div class="chip-row">{chip_html}</div>
          <div class="hash-line">source_context {_html(_short_sha(preview['source_context_sha256']))} / source_analysis {_html(_short_sha(preview['source_analysis_sha256']))} / languages {_html(languages)} / entrypoints {_html(entrypoints)}</div>
          <div class="preview-grid">
            <div class="preview-column">
              <h3>Component map candidates</h3>
              {component_html}
            </div>
            <div class="preview-column">
              <h3>Metric semantics candidates</h3>
              {metric_html}
            </div>
            <div class="preview-column">
              <h3>Read-only collection hints</h3>
              {collector_html}
            </div>
          </div>
        </section>"""


def _preview_list_html(items: list[str], empty_message: str) -> str:
    if not items:
        return f"<ul class=\"preview-list\"><li>{_html(empty_message)}</li></ul>"
    return "<ul class=\"preview-list\">" + "".join(f"<li>{item}</li>" for item in items) + "</ul>"


def _fast_gcp_system_code_preview(
    *,
    source_context: dict[str, Any],
    source_analysis: dict[str, Any],
    profile_draft: dict[str, Any],
    approved_profile: dict[str, Any],
) -> dict[str, Any]:
    project_summary = source_context.get("project_summary") if isinstance(source_context.get("project_summary"), dict) else {}
    system_profile = (
        approved_profile.get("system_profile") if isinstance(approved_profile.get("system_profile"), dict) else {}
    )
    confidence_summary = (
        profile_draft.get("confidence_summary") if isinstance(profile_draft.get("confidence_summary"), dict) else {}
    )
    components: list[dict[str, Any]] = []
    for item in source_analysis.get("component_candidates") or []:
        if not isinstance(item, dict):
            continue
        components.append(
            {
                "name": str(item.get("name") or ""),
                "role": str(item.get("suggested_role") or "unknown"),
                "subsystem": str(item.get("suggested_subsystem") or "unknown"),
                "confidence": _format_confidence(item.get("confidence")),
                "source_refs": [str(ref) for ref in item.get("matched_source_refs") or [] if ref],
            }
        )
    metrics: list[dict[str, Any]] = []
    for item in source_analysis.get("metric_semantics_candidates") or []:
        if not isinstance(item, dict):
            continue
        semantics = item.get("suggested_semantics") if isinstance(item.get("suggested_semantics"), dict) else {}
        metrics.append(
            {
                "name": str(item.get("metric_name") or ""),
                "semantic_type": str(semantics.get("semantic_type") or "unknown"),
                "zero_behavior": str(semantics.get("zero_behavior") or "unknown"),
                "increase_behavior": str(semantics.get("increase_behavior") or "unknown"),
                "decrease_behavior": str(semantics.get("decrease_behavior") or "unknown"),
                "confidence": _format_confidence(item.get("confidence")),
                "source_refs": [str(ref) for ref in item.get("source_refs") or [] if ref],
            }
        )
    collectors: list[dict[str, Any]] = []
    for item in source_analysis.get("collector_mapping_candidates") or []:
        if not isinstance(item, dict):
            continue
        collectors.append(
            {
                "request_type": str(item.get("request_type") or "unknown"),
                "collectors": [str(ref) for ref in item.get("candidate_collectors") or [] if ref],
                "safety_level": str(item.get("safety_level") or "unknown"),
            }
        )
    return {
        "schema_version": "fast_gcp_system_code_preview.v1",
        "source_context_sha256": str(source_context.get("source_context_sha256") or ""),
        "source_analysis_sha256": str(source_analysis.get("analysis_sha256") or ""),
        "profile_id": str(approved_profile.get("profile_id") or "unapproved_profile_context"),
        "project_type": str(system_profile.get("system_type") or project_summary.get("detected_project_type") or "unknown"),
        "raw_source_policy": str(
            source_context.get("raw_source_policy") or source_analysis.get("raw_source_policy") or "not_uploaded"
        ),
        "raw_env_policy": str(
            source_context.get("raw_env_policy") or source_analysis.get("raw_env_policy") or "not_uploaded"
        ),
        "detected_languages": [str(item) for item in project_summary.get("detected_languages") or [] if item],
        "entrypoint_candidates": [str(item) for item in project_summary.get("entrypoint_candidates") or [] if item],
        "service_unit_candidates": [str(item) for item in project_summary.get("service_unit_candidates") or [] if item],
        "metric_name_candidates": [str(item) for item in project_summary.get("metric_name_candidates") or [] if item],
        "components": components,
        "metric_semantics": metrics,
        "collector_mappings": collectors,
        "overall_confidence": _format_confidence(confidence_summary.get("overall_confidence")),
        "context_boundary": "sanitized_source_context_only_not_incident_evidence",
    }


def _format_confidence(value: object) -> str:
    try:
        return f"{float(value):.2f}"
    except (TypeError, ValueError):
        return "n/a"


def _public_rescore_demo_run_enabled() -> bool:
    return os.environ.get("OES_PUBLIC_RESCORE_DEMO_RUN_ENABLED", "1").strip().casefold() not in {
        "0",
        "false",
        "no",
        "off",
    }


def _public_rescore_run_id_from_payload(payload: dict[str, Any]) -> str:
    raw = payload.get("run_id")
    if raw is None or not str(raw).strip():
        return f"fixed-rescore-{uuid.uuid4().hex[:12]}"
    return _validate_fast_gcp_review_run_id(str(raw))


def _run_public_fixed_rescore_demo(*, demo_id: str, run_id: str, live_model: bool = False) -> dict[str, Any]:
    started = time.perf_counter()
    demo_payload = _rescore_demo_payload(demo_id)
    if not demo_payload:
        raise HTTPException(status_code=404, detail="rescore demo not found")
    if str(demo_payload.get("demo_id") or "") != "amazon-notify-more-data-rescore":
        raise HTTPException(status_code=404, detail="unsupported public rescore demo")
    store = SQLiteStore(Path(tempfile.mkdtemp(prefix="oes-fixed-rescore-")) / "rescore.sqlite3")
    store.init_schema()
    parent = _fixed_rescore_parent_bundle()
    store.insert_bundle(parent)
    before_synthesis = _fixed_rescore_before_synthesis()
    before = resolve_canonical_review_graph_snapshot(
        store,
        parent,
        multi_ai_synthesis=before_synthesis,
        persist_if_missing=True,
        created_by="public-fixed-rescore-before",
    )
    before_graph = before["canonical_review_graph"]
    before_target = (before_graph.get("validation_targets") or [{}])[0]
    query = _fixed_rescore_more_data_query()
    query["request_analysis"] = analyze_more_data_queries(query["queries"])
    proposal = {
        "proposition_id": "prop-runtime",
        "subsystem": "runtime_recovery",
        "evidence_sha256": parent["evidence_sha256"],
    }
    child = _bundle_with_more_data(parent, proposal, query, review_target=before_target)
    store.insert_bundle(child)
    more_data_refs = [
        evidence_id
        for evidence_id, ref in child["evidence_refs"].items()
        if isinstance(ref, dict) and str(ref.get("type") or "").startswith("more_data")
    ]
    refreshed_parent = {
        **child,
        "evidence_sha256": parent["evidence_sha256"],
        "child_evidence_sha256": child["evidence_sha256"],
    }
    request_statuses = _more_data_request_statuses(query)
    refresh_summary = _more_data_refresh_summary(
        query,
        request_statuses=request_statuses,
        run_models=True,
        pipeline_result={"model_run_count": 5, "claim_count": 1, "review_target_count": 1},
        evidence_delta=child["more_data"]["evidence_delta"],
    )
    pipeline_run_id = start_pipeline_run(
        store,
        evidence_sha256=parent["evidence_sha256"],
        operation="public_fixed_rescore",
        summary={"review_target_id": before_target.get("review_target_id"), "run_id": run_id},
    )
    record_pipeline_event(
        store,
        pipeline_run_id=pipeline_run_id,
        evidence_sha256=parent["evidence_sha256"],
        operation="public_fixed_rescore",
        step_key="more_data_requested",
        status="completed",
        message="Fixed public rescore requested user-impact evidence.",
        metadata={"review_target_id": before_target.get("review_target_id"), "run_id": run_id},
    )
    record_pipeline_event(
        store,
        pipeline_run_id=pipeline_run_id,
        evidence_sha256=parent["evidence_sha256"],
        operation="public_fixed_rescore",
        step_key="child_bundle_created",
        status="completed",
        message="Child Evidence Bundle added fixed user-impact rows.",
        metadata={"child_evidence_sha256": child["evidence_sha256"]},
    )
    _record_more_data_result_if_supported(
        store,
        str(before_target.get("review_target_id") or ""),
        child["evidence_sha256"],
        refresh_summary,
    )
    model_result: dict[str, Any] = {}
    if live_model:
        model_result = _run_public_live_rescore_model(
            store=store,
            bundle=refreshed_parent,
            run_id=run_id,
        )
        after_graph = model_result.get("canonical_review_graph") if isinstance(model_result.get("canonical_review_graph"), dict) else {}
        after = {
            "canonical_graph_status": model_result.get("canonical_graph_status") or "",
            "canonical_graph_sha256": model_result.get("canonical_graph_sha256") or "",
            "previous_snapshot": before.get("snapshot") or {},
            "canonical_review_graph": after_graph,
        }
    else:
        after = resolve_canonical_review_graph_snapshot(
            store,
            refreshed_parent,
            multi_ai_synthesis=_fixed_rescore_after_synthesis(more_data_refs),
            persist_if_stale=True,
            created_by="public-fixed-rescore-after",
        )
    after_graph = after["canonical_review_graph"]
    record_pipeline_event(
        store,
        pipeline_run_id=pipeline_run_id,
        evidence_sha256=parent["evidence_sha256"],
        operation="public_fixed_rescore",
        step_key="graph_rescored",
        status="completed",
        message="Canonical graph was re-scored after fixed child evidence.",
        metadata={"canonical_graph_sha256": after["canonical_graph_sha256"]},
    )
    finish_pipeline_run(
        store,
        pipeline_run_id=pipeline_run_id,
        evidence_sha256=parent["evidence_sha256"],
        operation="public_fixed_rescore",
        status="succeeded",
        message="Fixed public rescore completed.",
        metadata={"child_evidence_sha256": child["evidence_sha256"], "run_id": run_id},
    )
    events = store.list_pipeline_events(pipeline_run_id)
    before_primary = len(before_graph.get("primary_targets") or [])
    before_validation = len(before_graph.get("validation_targets") or [])
    after_primary = len(after_graph.get("primary_targets") or [])
    after_validation = len(after_graph.get("validation_targets") or [])
    result = {
        "schema_version": "public_fixed_rescore_result.v1",
        "status": "ok",
        "run_id": run_id,
        "demo_id": demo_id,
        "mode": "live_model_rescore_owner_only" if live_model else "fixed_sanitized_child_bundle_no_model_api",
        "model_api_called": bool(live_model and _fast_gcp_review_provider_mode() != "local"),
        "owner_access_required": bool(live_model),
        "arbitrary_input_accepted": False,
        "input": {
            "sample": "amazon-notify",
            "raw_log_policy": "not_uploaded",
            "source": "bundled_public_rescore_fixture",
        },
        "before": {
            "canonical_graph_sha256": before.get("canonical_graph_sha256") or "",
            "primary_count": before_primary,
            "validation_count": before_validation,
            "target_title": str(before_target.get("title") or ""),
            "promotion_blocked_reasons": list(before_target.get("promotion_blocked_reasons") or []),
            "review_priority_score": float(before_target.get("review_priority_score") or 0),
        },
        "child": {
            "evidence_sha256": child["evidence_sha256"],
            "parent_evidence_sha256": child.get("parent_evidence_sha256") or "",
            "relationship": str((child.get("lineage") or {}).get("relationship") or ""),
            "added_log_count": int((child.get("lineage") or {}).get("added_log_count") or 0),
            "added_evidence_ref_count": int((child.get("more_data") or {}).get("evidence_delta", {}).get("added_evidence_ref_count") or 0),
            "request_statuses": request_statuses,
        },
        "after": {
            "canonical_graph_sha256": after.get("canonical_graph_sha256") or "",
            "previous_canonical_graph_sha256": str((after.get("previous_snapshot") or {}).get("canonical_graph_sha256") or ""),
            "primary_count": after_primary,
            "validation_count": after_validation,
            "target_title": str(((after_graph.get("primary_targets") or [{}])[0]).get("title") or ""),
            "review_priority_score": float(((after_graph.get("primary_targets") or [{}])[0]).get("review_priority_score") or 0),
        },
        "transition": {
            "status": refresh_summary["review_target_status_transition"],
            "primary_count_delta": after_primary - before_primary,
            "validation_count_delta": after_validation - before_validation,
        },
        "providers": _public_live_rescore_provider_summary(model_result) if live_model else {
            "requested": [],
            "total": 0,
            "success": 0,
            "schema_valid": 0,
            "statuses": [],
        },
        "pipeline": {
            "pipeline_run_id": pipeline_run_id,
            "steps": [str(event.get("step_key") or "") for event in events],
        },
        "timing": {"wall_seconds": round(time.perf_counter() - started, 3)},
        "urls": {
            "demo": f"/ui/rescore-demo?id={_url_quote(demo_id)}",
            "source_review": str(demo_payload.get("source_review_url") or ""),
        },
    }
    try:
        store.db_path.unlink(missing_ok=True)
        store.db_path.parent.rmdir()
    except Exception:
        pass
    return result


def _run_public_live_rescore_model(*, store: Any, bundle: dict[str, Any], run_id: str) -> dict[str, Any]:
    source_context, source_analysis, _profile_draft, approved_profile = _fast_gcp_profile_context()
    provider_mode = "local" if _fast_gcp_review_provider_mode() == "local" else "real_or_skip"
    providers = _fast_gcp_review_provider_names(cross_check=False)
    result = run_multi_ai(
        bundle,
        approved_profile,
        providers=providers,
        mode=provider_mode,
        store=store,
        source_context=source_context,
        source_analysis=source_analysis,
    )
    provider_success = sum(
        1
        for row in result.get("model_runs") or []
        if isinstance(row, dict) and row.get("status") == "ok" and row.get("schema_valid") is True
    )
    if provider_success < 1:
        raise HTTPException(
            status_code=503,
            detail={
                "message": "live rescore provider did not produce a schema-valid result",
                "reason_code": "live_rescore_provider_failed",
                "run_id": run_id,
                "providers": providers,
                "model_runs": result.get("model_runs") or [],
            },
        )
    return result


def _public_live_rescore_provider_summary(model_result: dict[str, Any]) -> dict[str, Any]:
    rows = [row for row in model_result.get("model_runs") or [] if isinstance(row, dict)]
    statuses = [
        {
            "provider_id": str(row.get("provider") or row.get("provider_id") or ""),
            "model_name": str(row.get("model") or row.get("model_name") or ""),
            "status": str(row.get("status") or ""),
            "schema_valid": bool(row.get("schema_valid")),
            "latency_ms": int(row.get("latency_ms") or 0),
            "input_tokens": int(row.get("input_tokens") or 0),
            "output_tokens": int(row.get("output_tokens") or 0),
        }
        for row in rows
    ]
    success = sum(1 for row in rows if row.get("status") == "ok" and row.get("schema_valid") is True)
    return {
        "requested": _fast_gcp_review_provider_names(cross_check=False),
        "total": len(rows),
        "success": success,
        "schema_valid": success,
        "statuses": statuses,
    }


def _fixed_rescore_parent_bundle() -> dict[str, Any]:
    return {
        "schema_version": "ops-evidence-bundle/v1",
        "evidence_sha256": "parent-sha",
        "service": "amazon-notify",
        "environment": "prod",
        "evidence_refs": {
            "LOG-1": {
                "evidence_id": "LOG-1",
                "type": "runtime_log",
                "summary": "amazon-notify.service restart loop observed",
            }
        },
        "logs": [],
        "operational_evidence": [],
        "signals": [{"signal_type": "restart_loop", "core_target_type": "job_configuration_mismatch"}],
    }


def _fixed_rescore_before_synthesis() -> dict[str, Any]:
    return {
        "schema_version": "multi_ai_synthesis.v1",
        "evidence_sha256": "parent-sha",
        "provider_count": 3,
        "successful_provider_count": 3,
        "agreement_groups": [],
        "disagreement_groups": [
            {
                "group_id": "rt-runtime",
                "core_target_type": "job_configuration_mismatch",
                "subsystem": "runtime_recovery",
                "providers": ["gemini"],
                "provider_count": 1,
                "evidence_refs": ["LOG-1"],
                "missing_evidence": ["critical outcome metric"],
            }
        ],
        "validation_targets": [
            {
                "group_id": "rt-runtime",
                "title": "Restart loop requires validation",
                "core_target_type": "job_configuration_mismatch",
                "subsystem": "runtime_recovery",
                "providers": ["gemini"],
                "provider_count": 1,
                "evidence_refs": ["LOG-1"],
                "review_priority_score": 0.69,
                "missing_evidence": ["critical outcome metric"],
            }
        ],
    }


def _fixed_rescore_more_data_query() -> dict[str, Any]:
    return {
        "sql": "select user impact rows",
        "subsystem": "runtime_recovery",
        "queries": [
            {
                "request_id": "user_impact_signal_query",
                "request_type": "user_impact_signal",
                "need": "user impact",
                "preview_count": 2,
                "sql": "select notification delivery failures",
                "preview_rows": [
                    {
                        "timestamp": "2026-06-26T23:00:00Z",
                        "service": "amazon-notify",
                        "severity": "ERROR",
                        "message_sanitized": "notification_not_delivered count=47 during restart loop",
                        "message_template": "notification_not_delivered",
                        "error_type": "notification_not_delivered",
                        "raw_log_sha256": "safe-impact-1",
                    },
                    {
                        "timestamp": "2026-06-26T23:01:00Z",
                        "service": "amazon-notify",
                        "severity": "ERROR",
                        "message_sanitized": "notification_not_delivered count=49 after service restart",
                        "message_template": "notification_not_delivered",
                        "error_type": "notification_not_delivered",
                        "raw_log_sha256": "safe-impact-2",
                    },
                ],
            }
        ],
    }


def _fixed_rescore_after_synthesis(more_data_refs: list[str]) -> dict[str, Any]:
    return {
        "schema_version": "multi_ai_synthesis.v1",
        "evidence_sha256": "parent-sha",
        "provider_count": 3,
        "successful_provider_count": 3,
        "claim_groups": [
            {
                "group_id": "rt-runtime",
                "core_target_type": "job_configuration_mismatch",
                "subsystem": "runtime_recovery",
                "providers": ["gemini", "gpt-oss", "mistral"],
                "provider_count": 3,
                "evidence_refs": ["LOG-1", *more_data_refs],
            }
        ],
        "agreement_groups": [
            {
                "group_id": "rt-runtime",
                "title": "Notifier restart loop has user-visible delivery impact",
                "core_target_type": "job_configuration_mismatch",
                "subsystem": "runtime_recovery",
                "providers": ["gemini", "gpt-oss", "mistral"],
                "provider_count": 3,
                "evidence_refs": ["LOG-1", *more_data_refs],
                "impact_summary": "notification_not_delivered user impact is visible during the restart loop.",
                "recommended_validation": "process_state",
            }
        ],
        "primary_candidates": [
            {
                "group_id": "rt-runtime",
                "title": "Notifier restart loop has user-visible delivery impact",
                "core_target_type": "job_configuration_mismatch",
                "subsystem": "runtime_recovery",
                "providers": ["gemini", "gpt-oss", "mistral"],
                "provider_count": 3,
                "evidence_refs": ["LOG-1", *more_data_refs],
                "review_priority_score": 0.84,
                "impact_summary": "notification_not_delivered user impact is visible during the restart loop.",
            }
        ],
    }


def _fast_gcp_review_cache_seconds() -> int:
    return _int_env("OES_PUBLIC_FAST_GCP_REVIEW_CACHE_SECONDS", 3600)


def _fast_gcp_review_disable_cache_seconds() -> int:
    return _int_env("OES_PUBLIC_FAST_GCP_REVIEW_DISABLE_CACHE_SECONDS", 30)


def _fast_gcp_review_disable_file() -> Path | None:
    configured = os.environ.get("OES_PUBLIC_FAST_GCP_REVIEW_DISABLE_FILE", "").strip()
    return Path(configured) if configured else None


def _fast_gcp_review_disable_gcs_uri() -> str:
    return os.environ.get("OES_PUBLIC_FAST_GCP_REVIEW_DISABLE_GCS_URI", "").strip()


def _fast_gcp_review_disable_state_configured() -> bool:
    return _fast_gcp_review_disable_file() is not None or _fast_gcp_review_disable_gcs_uri().startswith("gs://")


def _public_fast_gcp_review_disabled_state() -> dict[str, Any]:
    state = _read_fast_gcp_review_disable_state()
    if not isinstance(state, dict) or not state.get("disabled"):
        return {}
    return state


def _raise_if_public_fast_gcp_review_disabled(*, run_id: str = "", cross_check: bool = False) -> None:
    state = _public_fast_gcp_review_disabled_state()
    if not state:
        return
    detail = {
        "message": str(state.get("message") or "Public Fast GCP Review is disabled by budget guard."),
        "reason_code": str(state.get("reason_code") or "budget_guard_disabled"),
        "disabled_at": str(state.get("disabled_at") or ""),
        "budget": state.get("budget") if isinstance(state.get("budget"), dict) else {},
    }
    if run_id:
        try:
            _write_fast_gcp_review_status(
                _fast_gcp_review_status_payload(
                    run_id=run_id,
                    cross_check=cross_check,
                    status="failed",
                    current_step="budget_guard_blocked",
                    progress_percent=100,
                    message=detail["message"],
                    reason_code=detail["reason_code"],
                    quota={"live_api_allowed": False, "status": "budget_guard_disabled"},
                )
            )
        except Exception:
            LOGGER.exception("failed to persist budget guard status")
    raise HTTPException(status_code=503, detail=detail)


def _read_fast_gcp_review_disable_state() -> dict[str, Any]:
    global _FAST_GCP_REVIEW_DISABLE_CACHE
    ttl = max(0, _fast_gcp_review_disable_cache_seconds())
    now = time.monotonic()
    cached = _FAST_GCP_REVIEW_DISABLE_CACHE
    if ttl > 0 and cached is not None and now - cached[0] < ttl:
        return deepcopy(cached[1]) if isinstance(cached[1], dict) else {}
    state = _read_fast_gcp_review_disable_state_uncached()
    _FAST_GCP_REVIEW_DISABLE_CACHE = (now, deepcopy(state) if state else None)
    return state


def _read_fast_gcp_review_disable_state_uncached() -> dict[str, Any]:
    disable_file = _fast_gcp_review_disable_file()
    if disable_file is not None:
        try:
            payload = json.loads(disable_file.read_text(encoding="utf-8"))
        except FileNotFoundError:
            payload = None
        except Exception:
            payload = None
        if isinstance(payload, dict):
            return payload
    disable_uri = _fast_gcp_review_disable_gcs_uri()
    if disable_uri.startswith("gs://"):
        try:
            from ops_evidence_synthesis.gcp.storage import read_text

            payload = json.loads(read_text(disable_uri))
        except Exception as exc:
            if not _is_missing_gcs_object_error(exc):
                LOGGER.warning("failed to read Fast GCP Review disable state", exc_info=True)
            payload = None
        if isinstance(payload, dict):
            return payload
    return {}


def _write_fast_gcp_review_disable_state(state: dict[str, Any]) -> dict[str, Any]:
    global _FAST_GCP_REVIEW_DISABLE_CACHE
    text = json.dumps(state, ensure_ascii=True, sort_keys=True) + "\n"
    wrote = False
    disable_file = _fast_gcp_review_disable_file()
    if disable_file is not None:
        disable_file.parent.mkdir(parents=True, exist_ok=True)
        disable_file.write_text(text, encoding="utf-8")
        wrote = True
    disable_uri = _fast_gcp_review_disable_gcs_uri()
    if disable_uri.startswith("gs://"):
        from ops_evidence_synthesis.gcp.storage import write_text

        write_text(disable_uri, text, content_type="application/json")
        wrote = True
    if not wrote:
        raise HTTPException(
            status_code=503,
            detail={
                "message": "Fast GCP Review disable state storage is not configured.",
                "reason_code": "disable_state_storage_unavailable",
            },
        )
    _FAST_GCP_REVIEW_DISABLE_CACHE = (time.monotonic(), deepcopy(state))
    return state


def _budget_guard_notification_from_payload(payload: dict[str, Any]) -> dict[str, Any]:
    message = payload.get("message") if isinstance(payload.get("message"), dict) else None
    if message and isinstance(message.get("data"), str):
        try:
            decoded = base64.b64decode(str(message["data"])).decode("utf-8")
            parsed = json.loads(decoded)
        except Exception as exc:
            raise HTTPException(status_code=400, detail="invalid Pub/Sub budget notification") from exc
        if isinstance(parsed, dict):
            return parsed
    if payload:
        return payload
    raise HTTPException(status_code=400, detail="budget notification payload is required")


def _budget_guard_decision(notification: dict[str, Any]) -> dict[str, Any]:
    threshold = _float_env("OES_BUDGET_GUARD_DISABLE_THRESHOLD", 0.9)
    cost = _float_value(notification.get("costAmount"))
    budget = _float_value(notification.get("budgetAmount"))
    actual_ratio = cost / budget if budget > 0 else 0.0
    alert_threshold = _float_value(notification.get("alertThresholdExceeded"))
    forecast_threshold = _float_value(notification.get("forecastThresholdExceeded"))
    trigger_ratio = max(actual_ratio, alert_threshold, forecast_threshold)
    return {
        "disable": trigger_ratio >= threshold,
        "disable_threshold": threshold,
        "trigger_ratio": trigger_ratio,
        "actual_ratio": actual_ratio,
        "alert_threshold_exceeded": alert_threshold,
        "forecast_threshold_exceeded": forecast_threshold,
        "cost_amount": cost,
        "budget_amount": budget,
        "currency_code": str(notification.get("currencyCode") or ""),
        "budget_display_name": str(notification.get("budgetDisplayName") or ""),
        "cost_interval_start": str(notification.get("costIntervalStart") or ""),
    }


def _float_value(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _fast_gcp_review_daily_limit() -> int:
    return _int_env("OES_PUBLIC_FAST_GCP_REVIEW_DAILY_LIMIT", 12)


def _fast_gcp_review_client_daily_limit() -> int:
    return _int_env("OES_PUBLIC_FAST_GCP_REVIEW_CLIENT_DAILY_LIMIT", 2)


def _fast_gcp_review_owner_token() -> str:
    return os.environ.get("OES_PUBLIC_FAST_GCP_REVIEW_OWNER_TOKEN", "").strip()


def _fast_gcp_review_owner_token_matches(candidate: str) -> bool:
    expected = _fast_gcp_review_owner_token()
    supplied = str(candidate or "").strip()
    if not expected or not supplied:
        return False
    return hmac.compare_digest(expected, supplied)


def _fast_gcp_review_owner_access(request: Request, payload: dict[str, Any] | None = None) -> bool:
    if not _fast_gcp_review_owner_token():
        return False
    candidates = [
        request.cookies.get(_FAST_GCP_REVIEW_OWNER_COOKIE, ""),
        request.headers.get("x-oes-fast-gcp-review-owner-token", ""),
    ]
    if isinstance(payload, dict):
        candidates.append(str(payload.get("owner_token") or ""))
    return any(_fast_gcp_review_owner_token_matches(str(candidate or "")) for candidate in candidates)


def _set_fast_gcp_review_owner_cookie(response: Response) -> None:
    token = _fast_gcp_review_owner_token()
    if not token:
        return
    response.set_cookie(
        _FAST_GCP_REVIEW_OWNER_COOKIE,
        token,
        max_age=_int_env("OES_PUBLIC_FAST_GCP_REVIEW_OWNER_COOKIE_SECONDS", 14 * 24 * 60 * 60),
        secure=True,
        httponly=True,
        samesite="lax",
    )


def _fast_gcp_review_quota_dir() -> Path | None:
    configured = os.environ.get("OES_FAST_GCP_REVIEW_QUOTA_DIR", "").strip()
    if configured:
        return Path(configured)
    output_dir = os.environ.get("OES_FAST_GCP_REVIEW_OUTPUT_DIR", "").strip()
    if output_dir:
        return Path(output_dir) / "public-fast-gcp-review-quota"
    return None


def _fast_gcp_review_quota_gcs_prefix() -> str:
    configured = os.environ.get("OES_FAST_GCP_REVIEW_QUOTA_GCS_PREFIX", "").strip()
    if configured:
        return configured.rstrip("/")
    base = (
        os.environ.get("OES_FAST_GCP_REVIEW_GCS_PREFIX", "").strip()
        or os.environ.get("OES_PRECOMPUTED_REVIEW_GCS_PREFIX", "").strip()
    )
    if base.startswith("gs://"):
        return f"{base.rstrip('/')}/public-fast-gcp-review-quota"
    return ""


def _fast_gcp_review_quota_backend_available() -> bool:
    return _fast_gcp_review_quota_dir() is not None or _fast_gcp_review_quota_gcs_prefix().startswith("gs://")


def _fast_gcp_review_quota_date() -> str:
    return time.strftime("%Y-%m-%d", time.gmtime())


def _fast_gcp_review_client_quota_key(request: Request) -> str:
    forwarded_for = (request.headers.get("x-forwarded-for") or "").split(",", 1)[0].strip()
    remote = forwarded_for or request.headers.get("x-real-ip", "").strip()
    if not remote and request.client is not None:
        remote = request.client.host or ""
    salt = (
        os.environ.get("OES_PUBLIC_FAST_GCP_REVIEW_QUOTA_SALT", "").strip()
        or os.environ.get("GOOGLE_CLOUD_PROJECT", "").strip()
        or "ops-evidence-public-demo"
    )
    digest = hashlib.sha256(f"{salt}:{remote or 'unknown'}".encode("utf-8")).hexdigest()[:24]
    return f"client-{digest}"


def _fast_gcp_review_quota_object_uri(date_key: str, quota_key: str) -> str:
    prefix = _fast_gcp_review_quota_gcs_prefix()
    if not prefix.startswith("gs://"):
        return ""
    return f"{prefix.rstrip('/')}/{date_key}/{quota_key}.json"


def _fast_gcp_review_quota_cache_key(date_key: str, quota_key: str) -> str:
    return f"{date_key}/{quota_key}"


def _empty_fast_gcp_review_quota_record(date_key: str, quota_key: str, *, limit: int) -> dict[str, Any]:
    return {
        "schema_version": "public_fast_gcp_review_quota.v1",
        "date": date_key,
        "quota_key": quota_key,
        "count": 0,
        "limit": limit,
        "updated_at": "",
    }


def _read_fast_gcp_review_quota_record(date_key: str, quota_key: str, *, limit: int) -> dict[str, Any]:
    quota_dir = _fast_gcp_review_quota_dir()
    if quota_dir is not None:
        path = quota_dir / date_key / f"{quota_key}.json"
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            payload = None
        except Exception:
            payload = None
        if isinstance(payload, dict):
            return payload
    quota_uri = _fast_gcp_review_quota_object_uri(date_key, quota_key)
    if quota_uri:
        try:
            from ops_evidence_synthesis.gcp.storage import read_text

            payload = json.loads(read_text(quota_uri))
        except Exception as exc:
            if not _is_missing_gcs_object_error(exc):
                raise RuntimeError(f"failed to read Fast GCP Review quota state: {quota_uri}") from exc
            payload = None
        if isinstance(payload, dict):
            return payload
    cached = _FAST_GCP_REVIEW_QUOTA_CACHE.get(_fast_gcp_review_quota_cache_key(date_key, quota_key))
    if isinstance(cached, dict):
        return deepcopy(cached)
    return _empty_fast_gcp_review_quota_record(date_key, quota_key, limit=limit)


def _write_fast_gcp_review_quota_record(record: dict[str, Any]) -> None:
    date_key = str(record.get("date") or "")
    quota_key = str(record.get("quota_key") or "")
    if not date_key or not quota_key:
        raise ValueError("quota date and key are required")
    text = json.dumps(record, ensure_ascii=True, sort_keys=True) + "\n"
    quota_dir = _fast_gcp_review_quota_dir()
    if quota_dir is not None:
        path = quota_dir / date_key / f"{quota_key}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    quota_uri = _fast_gcp_review_quota_object_uri(date_key, quota_key)
    if quota_uri:
        from ops_evidence_synthesis.gcp.storage import write_text

        write_text(quota_uri, text, content_type="application/json")
    _FAST_GCP_REVIEW_QUOTA_CACHE[_fast_gcp_review_quota_cache_key(date_key, quota_key)] = deepcopy(record)


def _consume_fast_gcp_review_quota(
    request: Request,
    *,
    payload: dict[str, Any] | None = None,
    run_id: str,
    cross_check: bool,
) -> dict[str, Any]:
    if _fast_gcp_review_owner_access(request, payload):
        return {
            "status": "owner_quota_bypass",
            "live_api_allowed": True,
            "owner_access": True,
        }
    if _truthy_env("OES_PUBLIC_FAST_GCP_REVIEW_QUOTA_DISABLED"):
        return {"status": "disabled", "live_api_allowed": True}
    daily_limit = _fast_gcp_review_daily_limit()
    client_limit = _fast_gcp_review_client_daily_limit()
    date_key = _fast_gcp_review_quota_date()
    client_key = _fast_gcp_review_client_quota_key(request)
    if daily_limit <= 0 or client_limit <= 0:
        _raise_fast_gcp_review_quota_error(
            "Public Fast GCP Review live runs are temporarily disabled.",
            reason_code="quota_disabled",
            quota={"date": date_key, "daily_limit": daily_limit, "client_daily_limit": client_limit},
        )
    if not _fast_gcp_review_quota_backend_available() and _fast_gcp_review_provider_mode() != "local":
        raise HTTPException(
            status_code=503,
            detail={
                "message": "Public Fast GCP Review quota storage is unavailable.",
                "reason_code": "quota_storage_unavailable",
                "quota": {"date": date_key, "live_api_allowed": False},
            },
        )
    total = _read_fast_gcp_review_quota_record(date_key, "total", limit=daily_limit)
    client = _read_fast_gcp_review_quota_record(date_key, client_key, limit=client_limit)
    total_count = int(total.get("count") or 0)
    client_count = int(client.get("count") or 0)
    if total_count >= daily_limit:
        _raise_fast_gcp_review_quota_error(
            "Public Fast GCP Review daily live-run quota has been reached.",
            reason_code="daily_quota_exceeded",
            quota={
                "date": date_key,
                "daily_count": total_count,
                "daily_limit": daily_limit,
                "client_daily_count": client_count,
                "client_daily_limit": client_limit,
            },
        )
    if client_count >= client_limit:
        _raise_fast_gcp_review_quota_error(
            "Public Fast GCP Review client daily live-run quota has been reached.",
            reason_code="client_quota_exceeded",
            quota={
                "date": date_key,
                "daily_count": total_count,
                "daily_limit": daily_limit,
                "client_daily_count": client_count,
                "client_daily_limit": client_limit,
            },
        )
    now = utc_now()
    total.update(
        {
            "schema_version": "public_fast_gcp_review_quota.v1",
            "date": date_key,
            "quota_key": "total",
            "count": total_count + 1,
            "limit": daily_limit,
            "updated_at": now,
            "last_run_id": run_id,
            "last_variant": _fast_gcp_review_variant(cross_check),
        }
    )
    client.update(
        {
            "schema_version": "public_fast_gcp_review_quota.v1",
            "date": date_key,
            "quota_key": client_key,
            "count": client_count + 1,
            "limit": client_limit,
            "updated_at": now,
            "last_run_id": run_id,
            "last_variant": _fast_gcp_review_variant(cross_check),
        }
    )
    try:
        _write_fast_gcp_review_quota_record(total)
        _write_fast_gcp_review_quota_record(client)
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail={
                "message": "Public Fast GCP Review quota storage write failed.",
                "reason_code": "quota_storage_write_failed",
                "quota": {"date": date_key, "live_api_allowed": False},
            },
        ) from exc
    return {
        "status": "accepted",
        "date": date_key,
        "daily_count": total_count + 1,
        "daily_limit": daily_limit,
        "daily_remaining": max(0, daily_limit - total_count - 1),
        "client_daily_count": client_count + 1,
        "client_daily_limit": client_limit,
        "client_daily_remaining": max(0, client_limit - client_count - 1),
        "live_api_allowed": True,
    }


def _raise_fast_gcp_review_quota_error(message: str, *, reason_code: str, quota: dict[str, Any]) -> None:
    quota = dict(quota)
    quota["live_api_allowed"] = False
    raise HTTPException(
        status_code=429,
        detail={
            "message": message,
            "reason_code": reason_code,
            "quota": quota,
        },
    )


def _is_missing_gcs_object_error(exc: Exception) -> bool:
    name = type(exc).__name__
    text = str(exc)
    return name == "NotFound" or "NotFound" in name or "404" in text


def _fast_gcp_review_sample_rows() -> int:
    return max(20, int(os.environ.get("OES_FAST_GCP_REVIEW_SAMPLE_ROWS", "2000")))


def _fast_gcp_cross_check_sample_rows() -> int:
    return max(20, int(os.environ.get("OES_FAST_GCP_CROSS_CHECK_SAMPLE_ROWS", "200")))


def _fast_gcp_effective_sample_rows(*, cross_check: bool) -> int:
    return _fast_gcp_cross_check_sample_rows() if cross_check else _fast_gcp_review_sample_rows()


def _fast_gcp_review_model_name() -> str:
    if _fast_gcp_review_provider_mode() == "local":
        return "local-gemini"
    return os.environ.get("OES_FAST_GCP_GEMINI_MODEL", "gemini-3.1-flash-lite")


def _fast_gcp_review_provider_mode() -> str:
    return os.environ.get("OES_FAST_GCP_REVIEW_PROVIDER_MODE", "real").strip().casefold()


def _fast_gcp_review_provider_name() -> str:
    return "local-gemini" if _fast_gcp_review_provider_mode() == "local" else "gemini-fast-lite"


def _fast_gcp_rescore_demo_id() -> str:
    return os.environ.get("OES_FAST_GCP_REVIEW_RESCORE_DEMO_ID", "amazon-notify-more-data-rescore").strip() or "amazon-notify-more-data-rescore"


def _fast_gcp_rescore_demo_url() -> str:
    return f"/ui/rescore-demo?id={_url_quote(_fast_gcp_rescore_demo_id())}"


def _fast_gcp_review_run_id_from_payload(payload: dict[str, Any], *, cross_check: bool) -> str:
    raw = payload.get("run_id")
    if raw is None or not str(raw).strip():
        return f"{_fast_gcp_review_variant(cross_check).replace('_', '-')}-{uuid.uuid4().hex[:12]}"
    return _validate_fast_gcp_review_run_id(str(raw))


def _validate_fast_gcp_review_run_id(run_id: str) -> str:
    text = str(run_id or "").strip()
    allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-")
    if not text or len(text) > 96 or any(ch not in allowed for ch in text):
        raise HTTPException(status_code=400, detail="run_id must be 1-96 chars using letters, numbers, dot, underscore, or dash")
    return text


def _fast_gcp_review_status_url(run_id: str) -> str:
    return f"/public/fast-gcp-review/status?run_id={_url_quote(run_id)}"


def _fast_gcp_review_status_dir() -> Path | None:
    configured = os.environ.get("OES_FAST_GCP_REVIEW_STATUS_DIR", "").strip()
    if configured:
        return Path(configured)
    output_dir = os.environ.get("OES_FAST_GCP_REVIEW_OUTPUT_DIR", "").strip()
    if output_dir:
        return Path(output_dir) / "public-fast-gcp-review-runs"
    return None


def _fast_gcp_review_status_gcs_prefix() -> str:
    configured = os.environ.get("OES_FAST_GCP_REVIEW_STATUS_GCS_PREFIX", "").strip()
    if configured:
        return configured.rstrip("/")
    base = (
        os.environ.get("OES_FAST_GCP_REVIEW_GCS_PREFIX", "").strip()
        or os.environ.get("OES_PRECOMPUTED_REVIEW_GCS_PREFIX", "").strip()
    )
    if base.startswith("gs://"):
        return f"{base.rstrip('/')}/public-fast-gcp-review-runs"
    return ""


def _fast_gcp_review_status_object_uri(run_id: str) -> str:
    prefix = _fast_gcp_review_status_gcs_prefix()
    return f"{prefix.rstrip('/')}/{run_id}/status.json" if prefix.startswith("gs://") else ""


def _fast_gcp_review_status_payload(
    *,
    run_id: str,
    cross_check: bool,
    status: str,
    current_step: str,
    progress_percent: int,
    message: str,
    providers: dict[str, Any] | None = None,
    review: dict[str, Any] | None = None,
    urls: dict[str, Any] | None = None,
    cache: dict[str, Any] | None = None,
    quota: dict[str, Any] | None = None,
    reason_code: str = "",
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload = {
        "schema_version": "public_fast_gcp_review_status.v1",
        "run_id": run_id,
        "variant": _fast_gcp_review_variant(cross_check),
        "status": status,
        "current_step": current_step,
        "progress_percent": max(0, min(100, int(progress_percent))),
        "message": message,
        "reason_code": reason_code,
        "input": {
            "sample": "amazon-notify",
            "sample_rows": _fast_gcp_effective_sample_rows(cross_check=cross_check),
            "logic_revision": _FAST_GCP_REVIEW_LOGIC_REVISION,
            "raw_log_policy": "not_uploaded",
            "arbitrary_input_accepted": False,
        },
        "providers": providers or {
            "total": len(_fast_gcp_review_provider_names(cross_check=cross_check)),
            "success": 0,
            "schema_valid": 0,
            "requested": _fast_gcp_review_provider_names(cross_check=cross_check),
            "statuses": [],
        },
        "review": review or {},
        "urls": urls or {"status": _fast_gcp_review_status_url(run_id)},
        "cache": cache or {},
        "quota": quota or {},
        "gcs": {
            "status_uri": _fast_gcp_review_status_object_uri(run_id),
            "enabled": bool(_fast_gcp_review_status_object_uri(run_id)),
        },
        "updated_at": utc_now(),
    }
    payload["urls"].setdefault("status", _fast_gcp_review_status_url(run_id))
    if extra:
        payload.update(extra)
    return payload


def _empty_fast_gcp_review_status(run_id: str) -> dict[str, Any]:
    return {
        "schema_version": "public_fast_gcp_review_status.v1",
        "run_id": run_id,
        "status": "not_started",
        "current_step": "not_started",
        "progress_percent": 0,
        "message": "No Fast GCP Review status has been recorded for this run_id.",
        "urls": {"status": _fast_gcp_review_status_url(run_id)},
        "updated_at": "",
    }


def _write_fast_gcp_review_status(status: dict[str, Any]) -> None:
    run_id = _validate_fast_gcp_review_run_id(str(status.get("run_id") or ""))
    text = json.dumps(status, ensure_ascii=True, sort_keys=True) + "\n"
    status_dir = _fast_gcp_review_status_dir()
    if status_dir is not None:
        path = status_dir / run_id / "status.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    status_uri = _fast_gcp_review_status_object_uri(run_id)
    if status_uri:
        from ops_evidence_synthesis.gcp.storage import write_text

        write_text(status_uri, text, content_type="application/json")
    with _FAST_GCP_REVIEW_STATUS_LOCK:
        _FAST_GCP_REVIEW_STATUS_CACHE[run_id] = deepcopy(status)


def _read_fast_gcp_review_status(run_id: str) -> dict[str, Any] | None:
    safe_run_id = _validate_fast_gcp_review_run_id(run_id)
    status_dir = _fast_gcp_review_status_dir()
    if status_dir is not None:
        path = status_dir / safe_run_id / "status.json"
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            payload = None
        except Exception:
            payload = None
        if isinstance(payload, dict):
            return payload
    status_uri = _fast_gcp_review_status_object_uri(safe_run_id)
    if status_uri:
        try:
            from ops_evidence_synthesis.gcp.storage import read_text

            payload = json.loads(read_text(status_uri))
        except Exception:
            payload = None
        if isinstance(payload, dict):
            return payload
    with _FAST_GCP_REVIEW_STATUS_LOCK:
        cached = _FAST_GCP_REVIEW_STATUS_CACHE.get(safe_run_id)
        return deepcopy(cached) if isinstance(cached, dict) else None


def _fast_gcp_cross_check_requested(payload: dict[str, Any]) -> bool:
    raw = payload.get("cross_check")
    if isinstance(raw, str):
        return raw.strip().casefold() in {"1", "true", "yes", "on", "cross_check", "cross-check"}
    return bool(raw)


def _fast_gcp_review_provider_names(*, cross_check: bool) -> list[str]:
    if not cross_check:
        return [_fast_gcp_review_provider_name()]
    if _fast_gcp_review_provider_mode() == "local":
        return ["local-gemini", "local-gpt-oss"]
    return ["gemini-fast-lite", "gemma"]


def _fast_gcp_review_model_names(*, cross_check: bool) -> list[str]:
    if not cross_check:
        return [_fast_gcp_review_model_name()]
    if _fast_gcp_review_provider_mode() == "local":
        return ["local-gemini", "local-gpt-oss"]
    return [
        _fast_gcp_review_model_name(),
        os.environ.get("OES_GEMMA_MODEL", "gemma-4-26b-a4b-it-maas"),
    ]


def _fast_gcp_review_variant(cross_check: bool) -> str:
    return "fast_cross_check_lite" if cross_check else "fast_gcp_review"


def _fast_gcp_review_cache_key(*, cross_check: bool) -> str:
    return ":".join(
        [
            "amazon-notify",
            _FAST_GCP_REVIEW_LOGIC_REVISION,
            _fast_gcp_review_variant(cross_check),
            str(_fast_gcp_effective_sample_rows(cross_check=cross_check)),
            _fast_gcp_review_provider_mode(),
            ",".join(_fast_gcp_review_provider_names(cross_check=cross_check)),
            ",".join(_fast_gcp_review_model_names(cross_check=cross_check)),
        ]
    )


def _fast_gcp_public_provider_mode(*, cross_check: bool) -> str:
    if _fast_gcp_review_provider_mode() == "local":
        return "fast_gcp_local_test"
    if cross_check:
        return "fast_gcp_vertex_gemini_flash_lite_gemma4_cross_check"
    return "fast_gcp_vertex_gemini_flash_lite"


def _run_public_fast_gcp_review(*, cross_check: bool = False, run_id: str | None = None) -> dict[str, Any]:
    run_started = time.perf_counter()
    run_id = _validate_fast_gcp_review_run_id(
        run_id or f"{_fast_gcp_review_variant(cross_check).replace('_', '-')}-{uuid.uuid4().hex[:12]}"
    )
    with tempfile.TemporaryDirectory(prefix="oes-fast-gcp-") as tmp:
        temp_dir = Path(tmp)
        store = SQLiteStore(temp_dir / "fast_review.sqlite3")
        store.init_schema()
        sanitized_rows = _load_fixed_fast_review_sample(
            limit=_fast_gcp_effective_sample_rows(cross_check=cross_check)
        )
        if not sanitized_rows:
            raise HTTPException(status_code=500, detail="fixed fast review sample is empty")
        _write_fast_gcp_review_status(
            _fast_gcp_review_status_payload(
                run_id=run_id,
                cross_check=cross_check,
                status="running",
                current_step="sample_loaded",
                progress_percent=12,
                message=f"{len(sanitized_rows)} fixed sanitized sample row(s) loaded.",
            )
        )
        store.insert_sanitized_logs(sanitized_rows)
        start = min(row.timestamp for row in sanitized_rows)
        end = format_timestamp(parse_timestamp(max(row.timestamp for row in sanitized_rows)) + timedelta(seconds=1))
        bundle = EvidenceBundleBuilder(store).build(
            IncidentWindow(
                service="amazon-notify",
                environment="prod",
                incident_start=start,
                incident_end=end,
                lookback_minutes=int(os.environ.get("OES_FAST_GCP_REVIEW_LOOKBACK_MINUTES", "15")),
            )
        )
        _write_fast_gcp_review_status(
            _fast_gcp_review_status_payload(
                run_id=run_id,
                cross_check=cross_check,
                status="running",
                current_step="bundle_built",
                progress_percent=24,
                message="Sanitized Evidence Bundle built from the fixed sample.",
                extra={
                    "input": {
                        "sample": "amazon-notify",
                        "sample_rows": len(sanitized_rows),
                        "logic_revision": _FAST_GCP_REVIEW_LOGIC_REVISION,
                        "raw_log_policy": "not_uploaded",
                        "arbitrary_input_accepted": False,
                        "evidence_sha256": str(bundle.get("evidence_sha256") or ""),
                        "window_start": str(bundle.get("window_start") or start),
                        "window_end": str(bundle.get("window_end") or end),
                    }
                },
            )
        )
        source_context, source_analysis, profile_draft, approved_profile = _fast_gcp_profile_context()
        provider_mode = "local" if _fast_gcp_review_provider_mode() == "local" else "real_or_skip"
        providers = _fast_gcp_review_provider_names(cross_check=cross_check)
        _write_fast_gcp_review_status(
            _fast_gcp_review_status_payload(
                run_id=run_id,
                cross_check=cross_check,
                status="running",
                current_step="model_call_started",
                progress_percent=42,
                message="Provider call started; waiting for schema-valid model output.",
                providers={
                    "total": len(providers),
                    "success": 0,
                    "schema_valid": 0,
                    "requested": providers,
                    "statuses": [],
                },
                extra={
                    "gcp_runtime": {
                        "service": os.environ.get("K_SERVICE", ""),
                        "revision": os.environ.get("K_REVISION", ""),
                        "configuration": os.environ.get("K_CONFIGURATION", ""),
                        "project": os.environ.get("OES_VERTEX_PROJECT")
                        or os.environ.get("GOOGLE_CLOUD_PROJECT")
                        or os.environ.get("GCP_PROJECT")
                        or "",
                        "location": os.environ.get("OES_FAST_GCP_VERTEX_LOCATION")
                        or os.environ.get("OES_VERTEX_LOCATION", "global"),
                    }
                },
            )
        )
        api_response = run_multi_ai(
            bundle,
            approved_profile,
            providers=providers,
            mode=provider_mode,
            store=store,
            source_context=source_context,
            source_analysis=source_analysis,
        )
        provider_success = sum(
            1
            for row in api_response.get("model_runs") or []
            if isinstance(row, dict) and row.get("status") == "ok" and row.get("schema_valid") is True
        )
        if provider_success < 1:
            raise HTTPException(
                status_code=503,
                detail={
                    "message": "fast GCP review provider did not produce a schema-valid result",
                    "provider_mode": provider_mode,
                    "providers": providers,
                    "model_runs": api_response.get("model_runs") or [],
                },
            )
        provider_statuses = [
            {
                "provider_id": str(row.get("provider") or row.get("provider_id") or ""),
                "model_name": str(row.get("model") or row.get("model_name") or ""),
                "status": str(row.get("status") or ""),
                "schema_valid": bool(row.get("schema_valid")),
                "latency_ms": int(row.get("latency_ms") or 0),
            }
            for row in api_response.get("model_runs") or []
            if isinstance(row, dict)
        ]
        provider_status = {
            "total": len(provider_statuses),
            "success": provider_success,
            "schema_valid": provider_success,
            "requested": providers,
            "statuses": provider_statuses,
        }
        _write_fast_gcp_review_status(
            _fast_gcp_review_status_payload(
                run_id=run_id,
                cross_check=cross_check,
                status="running",
                current_step="provider_completed",
                progress_percent=72,
                message=f"{provider_success} provider result(s) returned schema-valid output.",
                providers=provider_status,
            )
        )
        public_payload = _fast_gcp_public_payload(
            api_response=api_response,
            bundle=bundle,
            source_context=source_context,
            source_analysis=source_analysis,
            profile_draft=profile_draft,
            approved_profile=approved_profile,
            run_id=run_id,
            cross_check=cross_check,
            sample_rows=len(sanitized_rows),
        )
        _write_fast_gcp_review_status(
            _fast_gcp_review_status_payload(
                run_id=run_id,
                cross_check=cross_check,
                status="running",
                current_step="review_payload_built",
                progress_percent=86,
                message="Public review payload built from sanitized artifacts.",
                providers=provider_status,
            )
        )
        _persist_fast_gcp_public_payload(public_payload)
        _write_fast_gcp_review_status(
            _fast_gcp_review_status_payload(
                run_id=run_id,
                cross_check=cross_check,
                status="running",
                current_step="payload_persisted",
                progress_percent=94,
                message="Public review payload persisted.",
                providers=provider_status,
            )
        )
    wall_seconds = round(time.perf_counter() - run_started, 3)
    evidence_sha = str(public_payload.get("evidence_sha256") or "")
    public_review_id = _fast_gcp_public_review_id_from_payload(public_payload) or evidence_sha
    provider_statuses = public_payload.get("provider_statuses") if isinstance(public_payload.get("provider_statuses"), list) else []
    first_provider = provider_statuses[0] if provider_statuses and isinstance(provider_statuses[0], dict) else {}
    provider_success = sum(
        1
        for row in provider_statuses
        if isinstance(row, dict) and row.get("status") == "ok" and row.get("schema_valid") is True
    )
    provider_latency_sum = sum(int(row.get("latency_ms") or 0) for row in provider_statuses if isinstance(row, dict))
    result = {
        "schema_version": "public_fast_gcp_review_result.v1",
        "status": "ok",
        "run_id": run_id,
        "variant": _fast_gcp_review_variant(cross_check),
        "cache": {
            "status": "live_api_result",
            "ttl_seconds": _fast_gcp_review_cache_seconds(),
            "live_api_called": _fast_gcp_review_provider_mode() != "local",
        },
        "input": {
            "sample": "amazon-notify",
            "sample_rows": len(sanitized_rows),
            "logic_revision": _FAST_GCP_REVIEW_LOGIC_REVISION,
            "raw_log_policy": "not_uploaded",
            "arbitrary_input_accepted": False,
            "window_start": str(public_payload.get("analysis_context", {}).get("window_start") or ""),
            "window_end": str(public_payload.get("analysis_context", {}).get("window_end") or ""),
        },
        "gcp_runtime": {
            "service": os.environ.get("K_SERVICE", ""),
            "revision": os.environ.get("K_REVISION", ""),
            "configuration": os.environ.get("K_CONFIGURATION", ""),
            "project": os.environ.get("OES_VERTEX_PROJECT")
            or os.environ.get("GOOGLE_CLOUD_PROJECT")
            or os.environ.get("GCP_PROJECT")
            or "",
            "location": os.environ.get("OES_FAST_GCP_VERTEX_LOCATION") or os.environ.get("OES_VERTEX_LOCATION", "global"),
        },
        "provider": {
            "provider_id": str(first_provider.get("provider_id") or _fast_gcp_review_provider_name()),
            "model_name": str(first_provider.get("model_name") or _fast_gcp_review_model_name()),
            "status": str(first_provider.get("status") or ""),
            "schema_valid": bool(first_provider.get("schema_valid")),
            "latency_ms": int(first_provider.get("latency_ms") or 0),
            "input_tokens": int(first_provider.get("input_tokens") or 0),
            "output_tokens": int(first_provider.get("output_tokens") or 0),
            "raw_output_sha256": str(first_provider.get("raw_output_sha256") or ""),
        },
        "providers": {
            "total": len(provider_statuses),
            "success": provider_success,
            "schema_valid": provider_success,
            "requested": _fast_gcp_review_provider_names(cross_check=cross_check),
            "statuses": [
                {
                    "provider_id": str(row.get("provider_id") or ""),
                    "model_name": str(row.get("model_name") or ""),
                    "status": str(row.get("status") or ""),
                    "schema_valid": bool(row.get("schema_valid")),
                    "latency_ms": int(row.get("latency_ms") or 0),
                    "input_tokens": int(row.get("input_tokens") or 0),
                    "output_tokens": int(row.get("output_tokens") or 0),
                    "raw_output_sha256": str(row.get("raw_output_sha256") or ""),
                }
                for row in provider_statuses
                if isinstance(row, dict)
            ],
        },
        "timing": {
            "wall_seconds": wall_seconds,
            "provider_latency_ms": int(first_provider.get("latency_ms") or 0),
            "provider_latency_sum_ms": provider_latency_sum,
        },
        "review": {
            "evidence_sha256": evidence_sha,
            "public_review_id": public_review_id,
            "payload_sha256": str((public_payload.get("generation") or {}).get("payload_sha256") or ""),
            "canonical_graph_sha256": str(public_payload.get("summary", {}).get("canonical_graph_sha256") or ""),
            "primary_targets": int(public_payload.get("summary", {}).get("review", {}).get("primary_targets") or 0),
            "validation_targets": int(public_payload.get("summary", {}).get("review", {}).get("validation_targets") or 0),
        },
        "rescore_demo": {
            "demo_id": _fast_gcp_rescore_demo_id(),
            "url": _fast_gcp_rescore_demo_url(),
            "purpose": "shows missing evidence changing review priority while incident acceptance remains human-gated",
        },
        "system_preview": _fast_gcp_system_code_preview(
            source_context=source_context,
            source_analysis=source_analysis,
            profile_draft=profile_draft,
            approved_profile=approved_profile,
        ),
        "urls": {
            "detail": f"/ui/full-review-page?evidence_sha256={_url_quote(public_review_id)}",
            "api": f"/ui/api?evidence_sha256={_url_quote(public_review_id)}",
            "graph": f"/ui/review-graph?evidence_sha256={_url_quote(public_review_id)}",
            "report": f"/ui/report.md?evidence_sha256={_url_quote(public_review_id)}",
            "rescore": _fast_gcp_rescore_demo_url(),
            "status": _fast_gcp_review_status_url(run_id),
        },
    }
    _write_fast_gcp_review_status(
        _fast_gcp_review_status_payload(
            run_id=run_id,
            cross_check=cross_check,
            status="succeeded",
            current_step="completed",
            progress_percent=100,
            message=f"Completed in {wall_seconds}s with {provider_success}/{len(provider_statuses)} provider result(s).",
            providers=result["providers"],
            review=result["review"],
            urls=result["urls"],
            cache=result["cache"],
            extra={"timing": result["timing"], "gcp_runtime": result["gcp_runtime"]},
        )
    )
    return result


def _load_fixed_fast_review_sample(*, limit: int | None = None) -> list[SanitizedLog]:
    sample_path = Path(os.environ.get("OES_FAST_GCP_REVIEW_SAMPLE_PATH", "data/amazon_notify_flagship_logs.jsonl"))
    raw_rows: list[RawLog] = []
    limit = int(limit or _fast_gcp_review_sample_rows())
    with sample_path.open("r", encoding="utf-8", errors="replace") as handle:
        for index, line in enumerate(handle, start=1):
            if index > limit:
                break
            stripped = line.strip()
            if not stripped:
                continue
            payload = json.loads(stripped)
            if not isinstance(payload, dict):
                continue
            raw_rows.append(RawLog.from_mapping(payload))
    return sanitize_logs(raw_rows, service="amazon-notify", environment="prod")


def _fast_gcp_profile_context() -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]:
    profile_dir = Path(
        os.environ.get("OES_FAST_GCP_REVIEW_PROFILE_DIR", "data/public_profile_contexts/amazon_notify_sample")
    )
    source_context = _load_optional_json(profile_dir / "source_context_bundle.json")
    source_analysis = _load_optional_json(profile_dir / "source_analysis_bundle.json")
    profile_draft = _load_optional_json(profile_dir / "profile_draft.json")
    approved_profile = _load_optional_json(profile_dir / "approved_profile.json")
    return source_context, source_analysis, profile_draft, approved_profile


def _load_optional_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    if not isinstance(payload, dict):
        return {}
    return payload


def _fast_gcp_public_payload(
    *,
    api_response: dict[str, Any],
    bundle: dict[str, Any],
    source_context: dict[str, Any],
    source_analysis: dict[str, Any],
    profile_draft: dict[str, Any],
    approved_profile: dict[str, Any],
    run_id: str,
    cross_check: bool = False,
    sample_rows: int,
) -> dict[str, Any]:
    from scripts.generate_precomputed_review_from_multi_run import build_payload

    payload = build_payload(
        api_response,
        bundle,
        source_context=source_context,
        source_analysis=source_analysis,
        profile_draft=profile_draft,
        approved_profile=approved_profile,
        api_revision="public-fast-gcp-review-v2",
        profile_id=str(approved_profile.get("profile_id") or "amazon_notify_fast_sample_approved"),
        updated_at=utc_now(),
        source_note=(
            "generated by the public Fast GCP Review path from a fixed sanitized amazon-notify sample; "
            "arbitrary logs and URLs are not accepted"
        ),
        provider_mode=_fast_gcp_public_provider_mode(cross_check=cross_check),
        model_projection_policy=(
            "Fast GCP Review uses a deliberately small fixed sanitized sample so judges can see a live Cloud Run "
            "to Vertex model call quickly. Full 44k-50k reviews remain the recorded forensic artifacts."
        ),
        log_observations=[
            (
                "Fast GCP Review is the short live execution path. It proves the deployed service can build a "
                "sanitized Evidence Bundle, call the configured model, persist a public review payload, and return a URL."
            )
        ],
        min_window_hours=0,
    )
    payload.setdefault("generation", {})["fast_gcp_review"] = {
        "schema_version": "public_fast_gcp_review_generation.v2",
        "logic_revision": _FAST_GCP_REVIEW_LOGIC_REVISION,
        "run_id": run_id,
        "variant": _fast_gcp_review_variant(cross_check),
        "sample": "amazon-notify",
        "sample_rows": sample_rows,
        "providers": _fast_gcp_review_provider_names(cross_check=cross_check),
        "model_names": _fast_gcp_review_model_names(cross_check=cross_check),
        "provider": _fast_gcp_review_provider_name(),
        "model_name": _fast_gcp_review_model_name(),
        "public_input_policy": "fixed_sample_only",
    }
    payload["generation"]["fast_gcp_review"]["public_review_id"] = _fast_gcp_public_review_id(
        payload,
        run_id=run_id,
        cross_check=cross_check,
    )
    return payload


def _fast_gcp_public_review_id(payload: dict[str, Any], *, run_id: str, cross_check: bool) -> str:
    generation = payload.get("generation") if isinstance(payload.get("generation"), dict) else {}
    summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
    provider_statuses = payload.get("provider_statuses") if isinstance(payload.get("provider_statuses"), list) else []
    return sha256_json(
        {
            "schema_version": "public_fast_gcp_review_id.v2",
            "logic_revision": _FAST_GCP_REVIEW_LOGIC_REVISION,
            "run_id": run_id,
            "variant": _fast_gcp_review_variant(cross_check),
            "evidence_sha256": str(payload.get("evidence_sha256") or ""),
            "canonical_graph_sha256": str(summary.get("canonical_graph_sha256") or ""),
            "provider_mode": str(generation.get("provider_mode") or ""),
            "providers": [
                {
                    "provider_id": str(row.get("provider_id") or ""),
                    "model_name": str(row.get("model_name") or ""),
                    "status": str(row.get("status") or ""),
                    "schema_valid": bool(row.get("schema_valid")),
                    "raw_output_sha256": str(row.get("raw_output_sha256") or ""),
                }
                for row in provider_statuses
                if isinstance(row, dict)
            ],
        }
    )


def _fast_gcp_public_review_id_from_payload(payload: dict[str, Any]) -> str:
    generation = payload.get("generation") if isinstance(payload.get("generation"), dict) else {}
    fast_review = generation.get("fast_gcp_review") if isinstance(generation.get("fast_gcp_review"), dict) else {}
    return str(fast_review.get("public_review_id") or "")


def _persist_fast_gcp_public_payload(payload: dict[str, Any]) -> None:
    evidence_sha = str(payload.get("evidence_sha256") or "")
    if not evidence_sha:
        raise RuntimeError("fast GCP payload is missing evidence_sha256")
    storage_id = _fast_gcp_public_review_id_from_payload(payload) or evidence_sha
    text = stable_precomputed_review_json(payload)
    output_dir = os.environ.get("OES_FAST_GCP_REVIEW_OUTPUT_DIR", "").strip()
    if output_dir:
        path = Path(output_dir) / f"{storage_id}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    gcs_prefix = (
        os.environ.get("OES_FAST_GCP_REVIEW_GCS_PREFIX", "").strip()
        or os.environ.get("OES_PRECOMPUTED_REVIEW_GCS_PREFIX", "").strip()
    )
    if gcs_prefix.startswith("gs://"):
        from ops_evidence_synthesis.gcp.storage import write_text

        write_text(f"{gcs_prefix.rstrip('/')}/{storage_id}.json", text, content_type="application/json")
    _remember_precomputed_review_payload(payload)



def _render_full_review_page(evidence_sha256: str | None = None) -> str:
    selected_evidence_sha256 = evidence_sha256 or None
    target_set = (
        _list_review_targets_cached(limit=5, evidence_sha256=selected_evidence_sha256)
        if selected_evidence_sha256
        else {"summary": {}, "targets": []}
    )
    bundle = _store().get_bundle(selected_evidence_sha256) if selected_evidence_sha256 else None
    return _review_targets_page(target_set, evidence_sha256=selected_evidence_sha256, bundle=bundle)


def _render_fast_review_detail_page(evidence_sha256: str, *, precomputed: dict[str, Any] | None = None) -> str:
    precomputed = precomputed if precomputed is not None else _precomputed_review_payload(evidence_sha256)
    if precomputed:
        return _render_precomputed_review_detail_page(evidence_sha256, precomputed)
    store = _store()
    snapshot_response = _latest_canonical_graph_response(store, evidence_sha256)
    graph = snapshot_response.get("canonical_review_graph") if isinstance(snapshot_response, dict) else {}
    graph = graph if isinstance(graph, dict) else {}
    graph_summary = graph.get("summary") if isinstance(graph.get("summary"), dict) else {}
    finding = graph.get("finding") if isinstance(graph.get("finding"), dict) else {}
    display_summary = graph.get("display_summary") if isinstance(graph.get("display_summary"), dict) else {}
    if graph:
        target_set = _target_set_from_canonical_graph(graph, {})
    else:
        try:
            target_set = _list_review_targets_cached(limit=20, evidence_sha256=evidence_sha256, pending_only=False)
        except Exception as exc:
            target_set = {
                "summary": {},
                "targets": [],
                "warning": f"review targets could not be loaded: {safe_provider_error_message(str(exc), max_chars=180)}",
            }
    target_summary = target_set.get("summary") if isinstance(target_set.get("summary"), dict) else {}
    targets = [target for target in target_set.get("targets") or [] if isinstance(target, dict)]
    short_sha = _short_sha(evidence_sha256)
    graph_sha = str(snapshot_response.get("canonical_graph_sha256") or graph.get("canonical_graph_sha256") or "")
    finding_title = str(finding.get("title") or display_summary.get("title") or "No persisted finding yet")
    finding_impact = str(finding.get("impact") or display_summary.get("impact") or "Run analysis to create a persisted review result.")
    primary_count = int(target_summary.get("primary_review_targets") or graph_summary.get("primary_count") or 0)
    validation_count = int(target_summary.get("validation_targets") or graph_summary.get("validation_count") or 0)
    raw_log_count = int(target_summary.get("sanitized_log_count") or 0)
    target_cards = "\n".join(_fast_detail_target_card(target, index=index + 1) for index, target in enumerate(targets))
    pipeline_status = analysis_pipeline_status_from_store(store, evidence_sha256=evidence_sha256)
    pipeline_panel = _pipeline_progress_panel(pipeline_status)
    warning = str(target_set.get("warning") or "")
    warning_html = f'<section class="notice warn">{_html(warning)}</section>' if warning else ""
    complete_url = f"/?evidence_sha256={_url_quote(evidence_sha256)}"
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Ops Evidence Review</title>
  <style>
    :root {{
      color-scheme: light;
      --ink: #17202a;
      --muted: #5c6878;
      --line: #d9e0ea;
      --bg: #f7f8fb;
      --panel: #ffffff;
      --accent: #166d6b;
      --accent-2: #2f5f9e;
      --ok: #237a45;
      --warn: #a15c00;
      --danger: #b42318;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      background: var(--bg);
      color: var(--ink);
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      letter-spacing: 0;
    }}
    header {{
      display: flex;
      justify-content: space-between;
      gap: 14px;
      padding: 18px 24px;
      border-bottom: 1px solid var(--line);
      background: var(--panel);
    }}
    main {{
      display: grid;
      gap: 14px;
      max-width: 1180px;
      margin: 0 auto;
      padding: 18px 24px 40px;
    }}
    h1, h2, p {{ margin: 0; }}
    h1 {{ font-size: 20px; }}
    h2 {{ font-size: 18px; line-height: 1.3; overflow-wrap: anywhere; }}
    p {{ color: var(--muted); line-height: 1.45; }}
    code {{ font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, "Liberation Mono", monospace; overflow-wrap: anywhere; }}
    .meta {{ color: var(--muted); font-size: 12px; text-align: right; }}
    .panel {{
      display: grid;
      gap: 12px;
      padding: 16px;
      border: 1px solid var(--line);
      border-left: 5px solid var(--accent);
      border-radius: 8px;
      background: var(--panel);
      min-width: 0;
    }}
    .panel.secondary {{ border-left-color: var(--accent-2); }}
    .metrics {{
      display: grid;
      grid-template-columns: minmax(0, 1.45fr) repeat(3, minmax(92px, 0.45fr)) minmax(150px, 0.7fr);
      gap: 10px;
    }}
    .metric, .target {{
      border: 1px solid var(--line);
      border-radius: 6px;
      background: #fbfcfe;
      padding: 10px;
      min-width: 0;
    }}
    label {{
      display: block;
      margin-bottom: 5px;
      color: var(--muted);
      font-size: 12px;
      font-weight: 800;
      text-transform: uppercase;
    }}
    strong {{
      display: block;
      font-size: 20px;
      line-height: 1.25;
      overflow-wrap: anywhere;
    }}
    .targets {{ display: grid; gap: 10px; }}
    .target-head {{
      display: grid;
      grid-template-columns: minmax(0, 1fr) auto;
      gap: 12px;
      align-items: start;
    }}
    .score {{
      min-width: 84px;
      text-align: right;
      font-size: 22px;
      font-weight: 800;
      color: var(--accent);
    }}
    .score span {{ display: block; color: var(--muted); font-size: 11px; font-weight: 800; text-transform: uppercase; }}
    .pill-row {{ display: flex; flex-wrap: wrap; gap: 6px; margin-top: 8px; }}
    .pill {{
      border: 1px solid var(--line);
      border-radius: 999px;
      padding: 4px 7px;
      background: #fff;
      color: var(--muted);
      font-size: 12px;
      font-weight: 700;
    }}
    .target-grid {{
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 10px;
      margin-top: 10px;
    }}
    .field {{
      border-top: 1px solid var(--line);
      padding-top: 8px;
      min-width: 0;
    }}
    .field.full {{ grid-column: 1 / -1; }}
    .notice {{
      border: 1px solid var(--line);
      border-left: 5px solid var(--accent-2);
      border-radius: 8px;
      background: var(--panel);
      padding: 14px;
      color: var(--muted);
    }}
    .notice.warn {{ border-left-color: var(--warn); }}
    .pipeline-panel {{
      border: 1px solid var(--line);
      border-left: 5px solid var(--accent-2);
      border-radius: 8px;
      background: var(--panel);
      padding: 14px 16px;
      display: grid;
      gap: 10px;
      min-width: 0;
      overflow: hidden;
    }}
    .pipeline-header {{
      display: flex;
      justify-content: space-between;
      gap: 12px;
      align-items: flex-start;
      min-width: 0;
    }}
    .pipeline-header label {{
      display: block;
      color: var(--muted);
      font-size: 12px;
      font-weight: 800;
      text-transform: uppercase;
      margin-bottom: 4px;
    }}
    .pipeline-header strong {{
      display: block;
      font-size: 18px;
      overflow-wrap: anywhere;
      word-break: break-word;
    }}
    .pipeline-header span {{
      display: block;
      margin-top: 4px;
      color: var(--muted);
      font-size: 13px;
      overflow-wrap: anywhere;
    }}
    .pipeline-status {{
      min-width: 120px;
      text-align: right;
    }}
    .pipeline-status strong {{
      font-size: 14px;
      color: var(--accent-2);
      text-transform: uppercase;
    }}
    .pipeline-status[data-pipeline-status="succeeded"] strong {{ color: var(--ok); }}
    .pipeline-status[data-pipeline-status="failed"] strong,
    .pipeline-status[data-pipeline-status="blocked"] strong {{ color: var(--danger); }}
    .pipeline-status[data-pipeline-status="needs_input"] strong {{ color: var(--warn); }}
    .pipeline-blocking-reason {{
      margin: 0;
      color: var(--danger);
      font-size: 13px;
      overflow-wrap: anywhere;
    }}
    .pipeline-state-summary,
    .pipeline-reason-codes,
    .pipeline-frontier {{
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      min-width: 0;
      color: var(--muted);
      font-size: 12px;
    }}
    .pipeline-state-chip,
    .pipeline-reason-chip,
    .pipeline-canonical-state,
    .pipeline-frontier span {{
      display: inline-flex;
      align-items: center;
      width: fit-content;
      max-width: 100%;
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 3px 7px;
      background: #fff;
      overflow-wrap: anywhere;
    }}
    .pipeline-state-chip[data-current-state="true"] {{
      border-color: #93b2eb;
      background: #f4f7ff;
      color: var(--accent-2);
    }}
    .pipeline-reason-chip {{
      border-color: #f2bbb5;
      background: #fff5f4;
      color: var(--danger);
    }}
    .pipeline-canonical-state {{
      margin: 0 0 5px 6px;
      background: #f8fafc;
    }}
    .pipeline-meter {{
      height: 8px;
      border-radius: 999px;
      background: #e8edf4;
      overflow: hidden;
    }}
    .pipeline-meter div {{
      height: 100%;
      width: 0%;
      border-radius: inherit;
      background: var(--accent-2);
      transition: width 180ms ease;
    }}
    .pipeline-steps {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
      gap: 8px;
      margin: 0;
      padding: 0;
      list-style: none;
    }}
    .pipeline-steps li {{
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #fbfcfe;
      padding: 9px;
      min-width: 0;
      overflow-wrap: anywhere;
    }}
    .pipeline-steps li[data-step-status="completed"],
    .pipeline-steps li[data-step-status="succeeded"],
    .pipeline-steps li[data-step-status="skipped"] {{
      border-color: #b8dfcc;
      background: #f1fbf5;
    }}
    .pipeline-steps li[data-step-status="failed"],
    .pipeline-steps li[data-step-status="blocked"] {{
      border-color: #f2bbb5;
      background: #fff5f4;
    }}
    .pipeline-steps li[data-step-status="running"] {{
      border-color: #afc4ef;
      background: #f4f7ff;
    }}
    .pipeline-step-state {{
      display: inline-block;
      margin-bottom: 5px;
      color: var(--muted);
      font-size: 11px;
      font-weight: 800;
      text-transform: uppercase;
    }}
    .pipeline-steps strong {{
      display: block;
      font-size: 13px;
      line-height: 1.25;
    }}
    .pipeline-steps small {{
      display: block;
      margin-top: 5px;
      color: var(--muted);
      font-size: 12px;
      line-height: 1.35;
    }}
    .pipeline-events {{
      border-top: 1px solid var(--line);
      padding-top: 8px;
    }}
    .pipeline-events summary {{
      cursor: pointer;
      color: var(--muted);
      font-size: 12px;
      font-weight: 800;
      text-transform: uppercase;
    }}
    .pipeline-events ul {{
      display: grid;
      gap: 6px;
      margin: 8px 0 0;
      padding-left: 18px;
    }}
    .pipeline-events li {{
      font-size: 12px;
      overflow-wrap: anywhere;
    }}
    .pipeline-events span {{
      margin: 0 6px;
      color: var(--muted);
      font-weight: 700;
    }}
    .actions {{ display: flex; flex-wrap: wrap; gap: 10px; }}
    a.button {{
      display: inline-flex;
      align-items: center;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: #fff;
      color: var(--ink);
      padding: 8px 10px;
      font-weight: 800;
      text-decoration: none;
    }}
    @media (max-width: 780px) {{
      header {{ display: grid; }}
      .meta {{ text-align: left; }}
      main {{ padding: 14px; }}
      .metrics, .target-grid, .target-head {{ grid-template-columns: 1fr; }}
      .score {{ text-align: left; }}
    }}
  </style>
</head>
<body>
  <header>
    <h1>Ops Evidence Review</h1>
    <div class="meta">Evidence <code>{_html(short_sha)}</code></div>
  </header>
  <main>
    <section class="panel">
      <label>Persisted Review Result</label>
      <h2>{_html(finding_title)}</h2>
      <p>{_html(finding_impact)}</p>
      <div class="metrics">
        <div class="metric"><label>Canonical graph</label><strong>{_html(_short_sha(graph_sha) if graph_sha else "not persisted")}</strong></div>
        <div class="metric"><label>Primary</label><strong>{primary_count}</strong></div>
        <div class="metric"><label>Validation</label><strong>{validation_count}</strong></div>
        <div class="metric"><label>Targets</label><strong>{len(targets)}</strong></div>
        <div class="metric"><label>Logs</label><strong>{_html(_human_count(raw_log_count) if raw_log_count else "unknown")}</strong></div>
      </div>
    </section>
    {warning_html}
    {pipeline_panel}
    <section class="panel secondary">
      <label>Review Targets</label>
      <div class="targets">
        {target_cards or '<section class="notice">No review targets are persisted for this evidence.</section>'}
      </div>
      <div class="actions">
        <a class="button" href="{_html(complete_url)}">Back to summary</a>
      </div>
    </section>
  </main>
</body>
</html>"""


@router.post("/logs/jsonl")
def ingest_logs(payload: dict[str, Any]) -> dict[str, Any]:
    if not _server_path_ingest_enabled():
        raise HTTPException(
            status_code=403,
            detail="server path ingest is disabled; set OES_SERVER_PATH_INGEST_ENABLED=1 in a trusted environment",
        )
    path = payload.get("path")
    paths = payload.get("paths")
    if isinstance(path, str) and path:
        inputs = [path]
    elif isinstance(paths, list) and all(isinstance(item, str) for item in paths):
        inputs = paths
    else:
        raise HTTPException(status_code=400, detail="path or paths is required")
    return {"ingested_logs": ingest_log_files(inputs, _store())}


@router.post("/incidents")
def run_incident(payload: dict[str, Any]) -> dict[str, Any]:
    incident = _incident_from_payload(payload)
    result = run_pipeline(_store(), incident)
    return asdict(result)


@router.post("/bundles")
def create_bundle(payload: dict[str, Any]) -> dict[str, Any]:
    incident = _incident_from_payload(payload)
    bundle = EvidenceBundleBuilder(_store()).build(incident)
    return {
        "evidence_sha256": bundle["evidence_sha256"],
        "service": bundle["service"],
        "environment": bundle["environment"],
        "window_start": bundle["window_start"],
        "window_end": bundle["window_end"],
    }


@router.post("/bundles/upload")
def upload_evidence_bundle(payload: dict[str, Any]) -> dict[str, Any]:
    bundle = _upload_bundle_from_payload(payload)
    store = _store()
    pipeline_run_id = start_pipeline_run(
        store,
        evidence_sha256=str(bundle.get("evidence_sha256") or ""),
        operation="bundle_upload",
        summary={"bundle_type": bundle.get("bundle_type") or ""},
    )
    record_pipeline_event(
        store,
        pipeline_run_id=pipeline_run_id,
        evidence_sha256=str(bundle.get("evidence_sha256") or ""),
        operation="bundle_upload",
        step_key="bundle_received",
        status="completed",
        message="Sanitized Evidence Bundle received.",
    )
    validation = validate_evidence_bundle_for_upload(bundle)
    if not validation["passed"]:
        record_pipeline_event(
            store,
            pipeline_run_id=pipeline_run_id,
            evidence_sha256=str(bundle.get("evidence_sha256") or ""),
            operation="bundle_upload",
            step_key="bundle_validated",
            status="failed",
            message="Evidence Bundle validation failed.",
            metadata={
                "reason_code": "schema_invalid",
                "error_count": len(validation.get("errors") or []),
                "finding_count": len(validation.get("findings") or []),
            },
        )
        finish_pipeline_run(
            store,
            pipeline_run_id=pipeline_run_id,
            evidence_sha256=str(bundle.get("evidence_sha256") or ""),
            operation="bundle_upload",
            status="failed",
            message="Evidence Bundle validation failed.",
            metadata={"reason_code": "schema_invalid"},
        )
        raise HTTPException(
            status_code=400,
            detail={
                "message": "evidence bundle validation failed",
                "errors": validation["errors"],
                "findings": validation["findings"],
            },
        )
    record_pipeline_event(
        store,
        pipeline_run_id=pipeline_run_id,
        evidence_sha256=str(bundle.get("evidence_sha256") or ""),
        operation="bundle_upload",
        step_key="bundle_validated",
        status="completed",
        message="Server-side validation passed.",
    )
    store.insert_bundle(bundle)
    record_pipeline_event(
        store,
        pipeline_run_id=pipeline_run_id,
        evidence_sha256=str(bundle.get("evidence_sha256") or ""),
        operation="bundle_upload",
        step_key="bundle_persisted",
        status="completed",
        message="Evidence Bundle persisted.",
    )
    finish_pipeline_run(
        store,
        pipeline_run_id=pipeline_run_id,
        evidence_sha256=str(bundle.get("evidence_sha256") or ""),
        operation="bundle_upload",
        status="succeeded",
        message="Upload completed.",
    )
    _clear_target_cache()
    summary = bundle.get("local_first_summary") if isinstance(bundle.get("local_first_summary"), dict) else {}
    policy = bundle.get("analysis_policy") if isinstance(bundle.get("analysis_policy"), dict) else {}
    source = bundle.get("source") if isinstance(bundle.get("source"), dict) else {}
    return {
        "status": "accepted",
        "evidence_sha256": bundle["evidence_sha256"],
        "bundle_type": bundle.get("bundle_type"),
        "raw_log_policy": bundle.get("raw_log_policy"),
        "server_validation": {
            "passed": True,
            "evidence_sha256_verified": True,
            "secret_like_patterns": 0,
            "raw_pii_patterns": 0,
        },
        "local_first_summary": summary,
        "analysis_policy": policy,
        "source": source,
        "lineage": _bundle_lineage_summary(bundle),
        "signals": bundle.get("signals") or [],
        "required_profile_questions": bundle.get("required_profile_questions") or [],
        "review_graph_url": f"/?evidence_sha256={bundle['evidence_sha256']}",
        "pipeline_run_id": pipeline_run_id,
        "pipeline_status": pipeline_status_from_store(store, evidence_sha256=bundle["evidence_sha256"], pipeline_run_id=pipeline_run_id),
    }


@router.post("/profile-discovery/upload")
def upload_profile_discovery_bundle(payload: dict[str, Any]) -> dict[str, Any]:
    source_context = _optional_source_context_from_payload(payload)
    source_analysis = _optional_source_analysis_from_payload(payload)
    bundle = _optional_profile_discovery_from_payload(payload)
    if bundle is None and source_context:
        evidence_bundle = payload.get("evidence_bundle") if isinstance(payload.get("evidence_bundle"), dict) else {}
        try:
            bundle = build_profile_discovery_bundle(
                None,
                evidence_bundle_path=None,
                evidence_bundle=evidence_bundle,
                service=str(payload.get("service") or (source_context.get("source") or {}).get("service") or "unknown-service"),
                environment=str(payload.get("environment") or (source_context.get("source") or {}).get("environment") or "unknown"),
                source_context_bundle=source_context,
                source_analysis_bundle=source_analysis,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    if bundle is None:
        bundle = _upload_profile_discovery_from_payload(payload)
    validation = validate_profile_discovery_bundle_for_upload(bundle)
    if not validation["passed"]:
        raise HTTPException(
            status_code=400,
            detail={
                "message": "profile discovery bundle validation failed",
                "errors": validation["errors"],
                "findings": validation["findings"],
            },
        )
    generate_with_ai = bool(payload.get("generate_profile_draft_with_ai") or payload.get("use_ai_profile_draft"))
    draft = (
        build_profile_draft_with_provider(bundle, _profile_draft_provider())
        if generate_with_ai
        else build_profile_draft(bundle)
    )
    return {
        "status": "accepted",
        "discovery_sha256": bundle["discovery_sha256"],
        "bundle_type": bundle.get("bundle_type"),
        "raw_config_policy": bundle.get("raw_config_policy"),
        "raw_logs_policy": bundle.get("raw_logs_policy"),
        "server_validation": {
            "passed": True,
            "discovery_sha256_verified": True,
            "secret_like_patterns": 0,
            "raw_pii_patterns": 0,
        },
        "local_first_summary": bundle.get("local_first_summary") or {},
        "display_summary": bundle.get("display_summary") or {},
        "component_candidates": bundle.get("component_candidates") or [],
        "metric_semantics_candidates": bundle.get("metric_semantics_candidates") or [],
        "collector_mapping_candidates": bundle.get("collector_mapping_candidates") or [],
        "required_profile_questions": bundle.get("required_profile_questions") or [],
        "profile_draft": draft,
        "profile_draft_generation": draft.get("profile_generation") or {"generation_mode": "deterministic_local"},
        "source_context": {
            "accepted": bool(source_context),
            "source_context_sha256": source_context.get("source_context_sha256") if source_context else "",
            "context_is_not_incident_evidence": True,
        },
        "source_analysis": {
            "accepted": bool(source_analysis),
            "analysis_sha256": source_analysis.get("analysis_sha256") if source_analysis else "",
            "context_is_not_incident_evidence": True,
        },
    }


@router.post("/source-context/upload")
def upload_source_context_bundle(payload: dict[str, Any]) -> dict[str, Any]:
    bundle = _upload_source_context_from_payload(payload)
    validation = validate_source_context_bundle_for_upload(bundle)
    if not validation["passed"]:
        raise HTTPException(
            status_code=400,
            detail={
                "message": "source context bundle validation failed",
                "errors": validation["errors"],
                "findings": validation["findings"],
            },
        )
    return {
        "status": "accepted",
        "source_context_sha256": bundle["source_context_sha256"],
        "bundle_type": bundle.get("bundle_type"),
        "raw_source_policy": bundle.get("raw_source_policy"),
        "raw_env_policy": bundle.get("raw_env_policy"),
        "server_validation": {
            "passed": True,
            "source_context_sha256_verified": True,
            "secret_like_patterns": 0,
            "raw_pii_patterns": 0,
        },
        "display_summary": bundle.get("display_summary") or {},
        "context_is_not_incident_evidence": True,
    }


@router.post("/source-analysis/upload")
def upload_source_analysis_bundle(payload: dict[str, Any]) -> dict[str, Any]:
    bundle = _upload_source_analysis_from_payload(payload)
    validation = validate_source_analysis_bundle_for_upload(bundle)
    if not validation["passed"]:
        raise HTTPException(
            status_code=400,
            detail={
                "message": "source analysis bundle validation failed",
                "errors": validation["errors"],
                "findings": validation["findings"],
            },
        )
    return {
        "status": "accepted",
        "analysis_sha256": bundle["analysis_sha256"],
        "source_context_sha256": bundle.get("source_context_sha256") or "",
        "bundle_type": bundle.get("bundle_type"),
        "raw_source_policy": bundle.get("raw_source_policy"),
        "raw_env_policy": bundle.get("raw_env_policy"),
        "server_validation": {
            "passed": True,
            "analysis_sha256_verified": True,
            "secret_like_patterns": 0,
            "raw_pii_patterns": 0,
        },
        "display_summary": bundle.get("display_summary") or {},
        "context_is_not_incident_evidence": True,
    }


@router.post("/profile-drafts/approve")
def approve_profile_draft_api(payload: dict[str, Any]) -> dict[str, Any]:
    draft = payload.get("profile_draft") if isinstance(payload.get("profile_draft"), dict) else payload.get("draft")
    if not isinstance(draft, dict):
        raise HTTPException(status_code=400, detail="profile_draft object is required")
    profile_id = str(payload.get("profile_id") or "").strip()
    approved_by = str(payload.get("approved_by") or "").strip()
    note = str(payload.get("note") or "")
    if not profile_id:
        raise HTTPException(status_code=400, detail="profile_id is required")
    if not approved_by:
        raise HTTPException(status_code=400, detail="approved_by is required")
    try:
        profile = approved_profile_from_draft(
            draft,
            profile_id=profile_id,
            approved_by=approved_by,
            note=note,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    validation_text = json.dumps(profile, ensure_ascii=False, sort_keys=True)
    from ops_evidence_synthesis.local_first import scan_sanitized_text

    scan = scan_sanitized_text("approved_profile.yaml", validation_text)
    if scan["findings"]:
        raise HTTPException(
            status_code=400,
            detail={
                "message": "approved profile validation failed",
                "findings": scan["findings"],
            },
        )
    return {
        "status": "approved",
        "profile_id": profile["profile_id"],
        "approved": True,
        "explicit_profile": True,
        "approved_profile": profile,
    }


@router.post("/profile-reviews/normalize")
def normalize_profile_review_api(payload: dict[str, Any]) -> dict[str, Any]:
    focused_profile = payload.get("focused_profile")
    human_review = payload.get("human_review")
    if not isinstance(focused_profile, dict):
        raise HTTPException(status_code=400, detail="focused_profile object is required")
    if not isinstance(human_review, dict):
        raise HTTPException(status_code=400, detail="human_review object is required")
    try:
        result = normalize_profile_review_with_provider(
            focused_profile,
            human_review,
            _profile_review_provider(),
        )
    except ProfileReviewError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return result


@router.post("/profile-reviews/approve")
def approve_profile_review_api(payload: dict[str, Any]) -> dict[str, Any]:
    focused_profile = payload.get("focused_profile")
    human_review = payload.get("human_review")
    accepted_patch = payload.get("accepted_patch")
    normalization = payload.get("normalization")
    if not isinstance(focused_profile, dict):
        raise HTTPException(status_code=400, detail="focused_profile object is required")
    if not isinstance(human_review, dict):
        raise HTTPException(status_code=400, detail="human_review object is required")
    if not isinstance(accepted_patch, dict):
        raise HTTPException(status_code=400, detail="accepted_patch object is required")
    if normalization is not None and not isinstance(normalization, dict):
        raise HTTPException(status_code=400, detail="normalization must be an object or null")
    try:
        normalized_patch = normalize_profile_review_patch(accepted_patch)
        reviewed_patch_sha256 = str(payload.get("reviewed_patch_sha256") or "")
        if payload.get("interpretation_review_confirmed") is not True:
            raise ProfileReviewError("Gemini interpretation must be reviewed before final approval")
        if reviewed_patch_sha256 != sha256_json(normalized_patch):
            raise ProfileReviewError("edited patch changed after interpretation review")
        reviewed_human_review = deepcopy(human_review)
        reviewed_human_review["interpretation_review_confirmed"] = True
        reviewed_human_review["reviewed_patch_sha256"] = reviewed_patch_sha256
        profile = build_approved_operational_profile(
            focused_profile=focused_profile,
            human_review=reviewed_human_review,
            accepted_patch=normalized_patch,
            normalization=normalization if isinstance(normalization, dict) else {},
            profile_id=str(payload.get("profile_id") or focused_profile.get("system_label") or ""),
        )
    except ProfileReviewError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    errors = validate_approved_operational_profile(profile, focused_profile=focused_profile)
    if errors:
        raise HTTPException(
            status_code=400,
            detail={"message": "approved operational profile validation failed", "errors": errors},
        )
    return {
        "status": "approved",
        "profile_id": profile.get("profile_id") or "",
        "approved_profile_sha256": profile["approved_profile_sha256"],
        "approved_profile": profile,
    }


@router.post("/profile-reviews/preview")
def preview_profile_review_api(payload: dict[str, Any]) -> dict[str, Any]:
    focused_profile = payload.get("focused_profile")
    human_review = payload.get("human_review")
    accepted_patch = payload.get("accepted_patch")
    normalization = payload.get("normalization")
    if not isinstance(focused_profile, dict):
        raise HTTPException(status_code=400, detail="focused_profile object is required")
    if not isinstance(human_review, dict):
        raise HTTPException(status_code=400, detail="human_review object is required")
    if not isinstance(accepted_patch, dict):
        raise HTTPException(status_code=400, detail="accepted_patch object is required")
    if normalization is not None and not isinstance(normalization, dict):
        raise HTTPException(status_code=400, detail="normalization must be an object or null")
    try:
        return build_profile_review_interpretation_preview(
            focused_profile=focused_profile,
            human_review=human_review,
            accepted_patch=accepted_patch,
            normalization=normalization if isinstance(normalization, dict) else {},
            profile_id=str(payload.get("profile_id") or focused_profile.get("system_label") or ""),
        )
    except ProfileReviewError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/evidence-requests/plan")
def plan_evidence_requests_api(payload: dict[str, Any]) -> dict[str, Any]:
    evidence_bundle = payload.get("evidence_bundle")
    approved_profile = payload.get("approved_profile")
    planner_answers = payload.get("planner_answers")
    source_analysis = payload.get("source_analysis_bundle") if isinstance(payload.get("source_analysis_bundle"), dict) else payload.get("source_analysis")
    canonical_review_graph = payload.get("canonical_review_graph") if isinstance(payload.get("canonical_review_graph"), dict) else None
    generate_requirements_with_ai = bool(payload.get("generate_evidence_requirements_with_ai") or payload.get("use_llm_evidence_requirements"))
    if not isinstance(evidence_bundle, dict):
        raise HTTPException(status_code=400, detail="evidence_bundle object is required")
    if not isinstance(approved_profile, dict):
        raise HTTPException(status_code=400, detail="approved_profile object is required")
    if planner_answers is not None and not isinstance(planner_answers, dict):
        raise HTTPException(status_code=400, detail="planner_answers must be an object or null")
    if source_analysis is not None and not isinstance(source_analysis, dict):
        raise HTTPException(status_code=400, detail="source_analysis must be an object or null")
    if canonical_review_graph is not None and not isinstance(canonical_review_graph, dict):
        raise HTTPException(status_code=400, detail="canonical_review_graph must be an object or null")
    canonical_graph_source = "api_payload" if isinstance(canonical_review_graph, dict) and canonical_review_graph else "legacy_fallback"
    if canonical_review_graph is None:
        latest_snapshot = None
        evidence_sha = str(evidence_bundle.get("evidence_sha256") or "")
        store = _store()
        if evidence_sha and hasattr(store, "get_latest_canonical_review_graph_snapshot"):
            latest_snapshot = store.get_latest_canonical_review_graph_snapshot(evidence_sha)
        if isinstance(latest_snapshot, dict) and isinstance(latest_snapshot.get("canonical_review_graph_json"), dict):
            canonical_review_graph = latest_snapshot.get("canonical_review_graph_json")
            canonical_graph_source = "persisted"
    if isinstance(source_analysis, dict) and source_analysis:
        source_validation = validate_source_analysis_bundle_for_upload(source_analysis)
        if not source_validation["passed"]:
            raise HTTPException(status_code=400, detail={"message": "source_analysis validation failed", "findings": source_validation["findings"]})
    validation = validate_plan_payload_inputs(evidence_bundle, approved_profile, planner_answers)
    if not validation["passed"]:
        raise HTTPException(
            status_code=400,
            detail={
                "message": "evidence request planner input validation failed",
                "findings": validation["findings"],
            },
        )
    store = _store()
    evidence_sha = str(evidence_bundle.get("evidence_sha256") or "")
    pipeline_run_id = start_pipeline_run(
        store,
        evidence_sha256=evidence_sha,
        operation="evidence_request_plan",
        summary={
            "canonical_graph_source": canonical_graph_source,
            "planner_answers_supplied": planner_answers is not None,
            "generate_evidence_requirements_with_ai": generate_requirements_with_ai,
        },
    )
    record_pipeline_event(
        store,
        pipeline_run_id=pipeline_run_id,
        evidence_sha256=evidence_sha,
        operation="evidence_request_plan",
        step_key="planner_input_validated",
        status="completed",
        message="Planner input validation passed.",
        metadata={"finding_count": 0},
    )
    record_pipeline_event(
        store,
        pipeline_run_id=pipeline_run_id,
        evidence_sha256=evidence_sha,
        operation="evidence_request_plan",
        step_key="canonical_graph_loaded",
        status="completed" if isinstance(canonical_review_graph, dict) and canonical_review_graph else "skipped",
        message=f"Canonical graph source: {canonical_graph_source}.",
        metadata={"canonical_graph_source": canonical_graph_source},
    )
    record_pipeline_event(
        store,
        pipeline_run_id=pipeline_run_id,
        evidence_sha256=evidence_sha,
        operation="evidence_request_plan",
        step_key="planner_answers_received",
        status="completed" if planner_answers is not None else "needs_input",
        message="Planner answers received." if planner_answers is not None else "Planner can generate a draft, but human-question answers are still missing.",
        metadata={
            "planner_answers_supplied": planner_answers is not None,
            "reason_code": "" if planner_answers is not None else "no_planner_answers",
        },
    )
    requirement_provider = _evidence_requirement_provider() if generate_requirements_with_ai else None
    record_pipeline_event(
        store,
        pipeline_run_id=pipeline_run_id,
        evidence_sha256=evidence_sha,
        operation="evidence_request_plan",
        step_key="evidence_requirements_provider_selected",
        status="completed" if requirement_provider is not None else "skipped",
        message=(
            f"Evidence requirement provider selected: {getattr(requirement_provider, 'provider', '')}."
            if requirement_provider is not None
            else "Evidence requirement provider not requested."
        ),
        metadata={
            "generate_evidence_requirements_with_ai": generate_requirements_with_ai,
            "provider_id": getattr(requirement_provider, "provider", "") if requirement_provider is not None else "",
        },
    )
    try:
        plan = build_evidence_request_plan(
            evidence_bundle,
            approved_profile,
            planner_answers=planner_answers,
            source_analysis=source_analysis if isinstance(source_analysis, dict) else None,
            canonical_review_graph=canonical_review_graph if isinstance(canonical_review_graph, dict) else None,
            evidence_requirement_provider=requirement_provider,
            generated_from={
                "evidence_bundle": "api_payload",
                "approved_profile": "api_payload",
                "planner_answers": "api_payload" if planner_answers else "",
                "source_analysis": "api_payload" if isinstance(source_analysis, dict) and source_analysis else "",
                "canonical_review_graph": canonical_graph_source,
                "canonical_graph_sha256": str((canonical_review_graph or {}).get("canonical_graph_sha256") or "") if isinstance(canonical_review_graph, dict) else "",
                "input_fingerprint_sha256": str((canonical_review_graph or {}).get("input_fingerprint_sha256") or "") if isinstance(canonical_review_graph, dict) else "",
            },
        )
        record_pipeline_event(
            store,
            pipeline_run_id=pipeline_run_id,
            evidence_sha256=evidence_sha,
            operation="evidence_request_plan",
            step_key="plan_generated",
            status="completed",
            message="Evidence request plan generated.",
            metadata={
                "request_count": len(plan.get("requests") or []),
                "human_question_count": len(plan.get("human_questions") or []),
                "evidence_requirement_count": len(plan.get("evidence_requirements") or []),
                "evidence_requirements_generation_mode": (plan.get("evidence_requirements_metadata") or {}).get("generation_mode"),
                "evidence_requirements_llm_status": (plan.get("evidence_requirements_metadata") or {}).get("llm_status"),
                "planner_answers_supplied": planner_answers is not None,
            },
        )
        instructions = render_collection_instructions(plan)
        record_pipeline_event(
            store,
            pipeline_run_id=pipeline_run_id,
            evidence_sha256=evidence_sha,
            operation="evidence_request_plan",
            step_key="instructions_rendered",
            status="completed",
            message="Collection instructions rendered.",
            metadata={"planner_answers_supplied": planner_answers is not None},
        )
        finish_pipeline_run(
            store,
            pipeline_run_id=pipeline_run_id,
            evidence_sha256=evidence_sha,
            operation="evidence_request_plan",
            status="succeeded" if planner_answers is not None else "needs_input",
            message="Evidence request plan generated." if planner_answers is not None else "Human-question answers are still required.",
            metadata={
                "reason_code": "" if planner_answers is not None else "human_input_required",
                "request_count": len(plan.get("requests") or []),
                "human_question_count": len(plan.get("human_questions") or []),
                "evidence_requirement_count": len(plan.get("evidence_requirements") or []),
            },
        )
    except ValueError as exc:
        finish_pipeline_run(
            store,
            pipeline_run_id=pipeline_run_id,
            evidence_sha256=evidence_sha,
            operation="evidence_request_plan",
            status="failed",
            message=str(exc),
        )
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "plan": plan,
        "collection_instructions_markdown": instructions,
        "pipeline_run_id": pipeline_run_id,
        "pipeline_status": pipeline_status_from_store(store, evidence_sha256=evidence_sha, pipeline_run_id=pipeline_run_id),
    }


@router.get("/review/graph")
def get_review_graph_api(evidence_sha256: str, recompute: bool = False) -> dict[str, Any]:
    precomputed = _require_precomputed_review_for_public_read(evidence_sha256)
    if precomputed is not None:
        evidence_sha256 = _canonical_precomputed_review_sha(evidence_sha256)
        return _precomputed_review_graph_response(precomputed, evidence_sha256=evidence_sha256)
    store = _store()
    if not recompute:
        snapshot_response = _latest_canonical_graph_response(store, evidence_sha256)
        if snapshot_response:
            return snapshot_response
        return {"canonical_graph_status": "not_found", "canonical_review_graph": {}}
    bundle = store.get_bundle(evidence_sha256) if hasattr(store, "get_bundle") else None
    if not isinstance(bundle, dict):
        snapshot = (
            store.get_latest_canonical_review_graph_snapshot(evidence_sha256)
            if hasattr(store, "get_latest_canonical_review_graph_snapshot")
            else None
        )
        if isinstance(snapshot, dict):
            return {
                "canonical_graph_status": "persisted",
                "canonical_review_graph": snapshot.get("canonical_review_graph_json") or {},
                "canonical_graph_sha256": snapshot.get("canonical_graph_sha256") or "",
                "input_fingerprint_sha256": snapshot.get("input_fingerprint_sha256") or "",
                "snapshot_created_at": snapshot.get("created_at") or "",
                "snapshot": snapshot,
            }
        return {"canonical_graph_status": "not_found", "canonical_review_graph": {}}
    target_set = _list_review_targets_cached(limit=100, evidence_sha256=evidence_sha256)
    synthesis = _multi_ai_synthesis_for_ui(evidence_sha256, bundle)
    resolution = resolve_canonical_review_graph_snapshot(
        store,
        bundle,
        model_runs=_model_run_artifacts_for_ui(evidence_sha256),
        multi_ai_synthesis=synthesis,
        legacy_review_targets=list(target_set.get("targets") or []),
        legacy_summary=dict(target_set.get("summary") or {}),
        persist_if_missing=False,
        persist_if_stale=False,
        created_by="api-readonly",
    )
    return resolution


@router.post("/review/graph/refresh")
def refresh_review_graph_api(payload: dict[str, Any]) -> dict[str, Any]:
    evidence_sha256 = str(payload.get("evidence_sha256") or "")
    if not evidence_sha256:
        raise HTTPException(status_code=400, detail="evidence_sha256 is required")
    store = _store()
    bundle = store.get_bundle(evidence_sha256) if hasattr(store, "get_bundle") else None
    if not isinstance(bundle, dict):
        raise HTTPException(status_code=404, detail="bundle not found")
    target_set = _list_review_targets_cached(limit=100, evidence_sha256=evidence_sha256)
    synthesis = _multi_ai_synthesis_for_ui(evidence_sha256, bundle)
    resolution = resolve_canonical_review_graph_snapshot(
        store,
        bundle,
        model_runs=_model_run_artifacts_for_ui(evidence_sha256),
        multi_ai_synthesis=synthesis,
        legacy_review_targets=list(target_set.get("targets") or []),
        legacy_summary=dict(target_set.get("summary") or {}),
        persist_if_missing=True,
        persist_if_stale=True,
        created_by=str(payload.get("created_by") or "refresh"),
    )
    _clear_target_cache()
    return resolution


@router.post("/review/arbitrate")
def arbitrate_review_api(payload: dict[str, Any]) -> dict[str, Any]:
    evidence_bundle = payload.get("evidence_bundle") if isinstance(payload.get("evidence_bundle"), dict) else payload.get("bundle")
    if not isinstance(evidence_bundle, dict):
        raise HTTPException(status_code=400, detail="evidence_bundle object is required")
    if evidence_bundle.get("bundle_type") == "sanitized_evidence_bundle":
        validation = validate_evidence_bundle_for_upload(evidence_bundle)
        if not validation["passed"]:
            raise HTTPException(status_code=400, detail={"message": "evidence bundle validation failed", "findings": validation["findings"]})
    synthesis = payload.get("multi_ai_synthesis") if isinstance(payload.get("multi_ai_synthesis"), dict) else {}
    profile = payload.get("approved_profile") if isinstance(payload.get("approved_profile"), dict) else payload.get("profile") if isinstance(payload.get("profile"), dict) else {}
    source_context = _optional_source_context_from_payload(payload)
    source_analysis = _optional_source_analysis_from_payload(payload)
    model_runs = [row for row in payload.get("model_runs") or [] if isinstance(row, dict)] if isinstance(payload.get("model_runs"), list) else []
    legacy_targets = [row for row in payload.get("legacy_review_targets") or [] if isinstance(row, dict)] if isinstance(payload.get("legacy_review_targets"), list) else []
    legacy_summary = payload.get("legacy_summary") if isinstance(payload.get("legacy_summary"), dict) else {}
    resolution = resolve_canonical_review_graph_snapshot(
        _store(),
        evidence_bundle,
        model_runs=model_runs,
        multi_ai_synthesis=synthesis,
        approved_profile=profile if isinstance(profile, dict) else {},
        source_context=source_context or None,
        source_analysis=source_analysis or None,
        planner_answers=payload.get("planner_answers") if isinstance(payload.get("planner_answers"), dict) else None,
        legacy_review_targets=legacy_targets,
        legacy_summary=legacy_summary,
        persist_if_missing=bool(payload.get("persist")),
        persist_if_stale=bool(payload.get("persist_if_stale")),
        created_by="api",
    )
    return resolution


@router.post("/ai/multi-run")
def run_multi_ai_api(payload: dict[str, Any]) -> dict[str, Any]:
    bundle = payload.get("evidence_bundle") if isinstance(payload.get("evidence_bundle"), dict) else None
    if bundle is None and isinstance(payload.get("bundle"), dict):
        bundle = payload.get("bundle")
    if bundle is None and isinstance(payload.get("evidence_sha256"), str):
        bundle = _store().get_bundle(str(payload.get("evidence_sha256")))
    if not isinstance(bundle, dict):
        raise HTTPException(status_code=400, detail="evidence_bundle object or evidence_sha256 is required")
    if bundle.get("bundle_type") == "sanitized_evidence_bundle":
        validation = validate_evidence_bundle_for_upload(bundle)
        if not validation["passed"]:
            raise HTTPException(
                status_code=400,
                detail={
                    "message": "evidence bundle validation failed",
                    "errors": validation["errors"],
                    "findings": validation["findings"],
                },
            )
    approved_profile = (
        payload.get("approved_profile")
        if isinstance(payload.get("approved_profile"), dict)
        else payload.get("profile")
        if isinstance(payload.get("profile"), dict)
        else {}
    )
    provider_names = payload.get("providers")
    if isinstance(provider_names, list):
        providers = [str(item) for item in provider_names]
    elif isinstance(provider_names, str):
        providers = [provider_names]
    else:
        providers = ["local-gemini", "local-gpt-oss", "local-mistral"]
    mode = str(payload.get("mode") or "real_or_skip")
    source_context = _optional_source_context_from_payload(payload)
    source_analysis = _optional_source_analysis_from_payload(payload)
    store = _store()
    store.insert_bundle(bundle)
    try:
        result = run_multi_ai(
            bundle,
            approved_profile if isinstance(approved_profile, dict) else {},
            providers=providers,
            mode=mode,
            store=store,
            source_context=source_context or None,
            source_analysis=source_analysis or None,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    _clear_target_cache()
    result["pipeline_status"] = pipeline_status_from_store(
        store,
        evidence_sha256=str(result.get("evidence_sha256") or bundle.get("evidence_sha256") or ""),
        pipeline_run_id=str(result.get("pipeline_run_id") or ""),
    )
    return result


@router.post("/bundle/create")
def create_bundle_worker(payload: dict[str, Any]) -> dict[str, Any]:
    return create_bundle(payload)


@router.post("/run/local-agents")
def run_local_agents(payload: dict[str, Any]) -> dict[str, Any]:
    bundle = _bundle_from_payload(payload)
    store = _store()
    parsed_results = run_model_stage(store, bundle)
    return {
        "evidence_sha256": bundle["evidence_sha256"],
        "parsed_result_count": len(parsed_results),
        "schema_valid_count": sum(1 for result in parsed_results if result.schema_valid),
        "pipeline_status": pipeline_status_from_store(store, evidence_sha256=bundle["evidence_sha256"]),
    }


@router.post("/run/gemini")
def run_gemini_worker(payload: dict[str, Any]) -> dict[str, Any]:
    bundle = _bundle_from_payload(payload)
    provider = _gemini_provider()
    store = _store()
    parsed_results = run_model_stage(
        store,
        bundle,
        [provider],
    )
    return {
        "evidence_sha256": bundle["evidence_sha256"],
        "provider": provider.provider,
        "model_name": provider.model_name,
        "parsed_result_count": len(parsed_results),
        "schema_valid_count": sum(1 for result in parsed_results if result.schema_valid),
        "pipeline_status": pipeline_status_from_store(store, evidence_sha256=bundle["evidence_sha256"]),
    }


@router.post("/run/claude")
def run_claude_worker(payload: dict[str, Any]) -> dict[str, Any]:
    bundle = _bundle_from_payload(payload)
    provider = _claude_provider()
    store = _store()
    parsed_results = run_model_stage(
        store,
        bundle,
        [provider],
    )
    return {
        "evidence_sha256": bundle["evidence_sha256"],
        "provider": provider.provider,
        "model_name": provider.model_name,
        "parsed_result_count": len(parsed_results),
        "schema_valid_count": sum(1 for result in parsed_results if result.schema_valid),
        "pipeline_status": pipeline_status_from_store(store, evidence_sha256=bundle["evidence_sha256"]),
    }


@router.post("/run/gpt-oss")
def run_gpt_oss_worker(payload: dict[str, Any]) -> dict[str, Any]:
    bundle = _bundle_from_payload(payload)
    provider = _gpt_oss_provider()
    store = _store()
    parsed_results = run_model_stage(
        store,
        bundle,
        [provider],
    )
    return {
        "evidence_sha256": bundle["evidence_sha256"],
        "provider": provider.provider,
        "model_name": provider.model_name,
        "parsed_result_count": len(parsed_results),
        "schema_valid_count": sum(1 for result in parsed_results if result.schema_valid),
        "pipeline_status": pipeline_status_from_store(store, evidence_sha256=bundle["evidence_sha256"]),
    }


@router.post("/run/mistral")
def run_mistral_worker(payload: dict[str, Any]) -> dict[str, Any]:
    bundle = _bundle_from_payload(payload)
    provider = _mistral_provider()
    store = _store()
    parsed_results = run_model_stage(
        store,
        bundle,
        [provider],
    )
    return {
        "evidence_sha256": bundle["evidence_sha256"],
        "provider": provider.provider,
        "model_name": provider.model_name,
        "parsed_result_count": len(parsed_results),
        "schema_valid_count": sum(1 for result in parsed_results if result.schema_valid),
        "pipeline_status": pipeline_status_from_store(store, evidence_sha256=bundle["evidence_sha256"]),
    }


@router.post("/run/llama")
def run_llama_worker(payload: dict[str, Any]) -> dict[str, Any]:
    bundle = _bundle_from_payload(payload)
    provider = _llama_provider()
    store = _store()
    parsed_results = run_model_stage(
        store,
        bundle,
        [provider],
    )
    return {
        "evidence_sha256": bundle["evidence_sha256"],
        "provider": provider.provider,
        "model_name": provider.model_name,
        "parsed_result_count": len(parsed_results),
        "schema_valid_count": sum(1 for result in parsed_results if result.schema_valid),
        "pipeline_status": pipeline_status_from_store(store, evidence_sha256=bundle["evidence_sha256"]),
    }


@router.post("/run/alternatives")
def run_alternatives_worker(payload: dict[str, Any]) -> dict[str, Any]:
    bundle = _bundle_from_payload(payload)
    providers = _configured_alternative_providers()
    store = _store()
    parsed_results = run_model_stage(
        store,
        bundle,
        providers,
    )
    return {
        "evidence_sha256": bundle["evidence_sha256"],
        "providers": [
            {"provider": provider.provider, "model_name": provider.model_name}
            for provider in providers
        ],
        "parsed_result_count": len(parsed_results),
        "schema_valid_count": sum(1 for result in parsed_results if result.schema_valid),
        "pipeline_status": pipeline_status_from_store(store, evidence_sha256=bundle["evidence_sha256"]),
    }


@router.post("/run/external")
def run_external_worker(payload: dict[str, Any]) -> dict[str, Any]:
    bundle = _bundle_from_payload(payload)
    store = _store()
    parsed_results = run_model_stage(
        store,
        bundle,
        [HeuristicProvider("external-local", "contrast-simulated", "contrast")],
    )
    return {
        "evidence_sha256": bundle["evidence_sha256"],
        "parsed_result_count": len(parsed_results),
        "schema_valid_count": sum(1 for result in parsed_results if result.schema_valid),
        "pipeline_status": pipeline_status_from_store(store, evidence_sha256=bundle["evidence_sha256"]),
    }


@router.post("/validate")
def validate_stage(payload: dict[str, Any]) -> dict[str, Any]:
    bundle = _bundle_from_payload(payload)
    parsed_results = _store().fetch_parsed_results(bundle["evidence_sha256"])
    return {
        "evidence_sha256": bundle["evidence_sha256"],
        "parsed_result_count": len(parsed_results),
        "schema_valid_count": sum(1 for result in parsed_results if result.schema_valid),
        "schema_errors": {
            result.result_id: list(result.schema_errors)
            for result in parsed_results
            if result.schema_errors
        },
    }


@router.post("/route")
def route_stage(payload: dict[str, Any]) -> dict[str, Any]:
    bundle = _bundle_from_payload(payload)
    parsed_results = _store().fetch_parsed_results(bundle["evidence_sha256"])
    routing = run_route_stage(_store(), bundle, parsed_results)
    return {
        "evidence_sha256": bundle["evidence_sha256"],
        "claim_count": len(routing.claims),
        "proposition_count": len(routing.propositions),
    }


@router.post("/claim-router")
def claim_router_stage(payload: dict[str, Any]) -> dict[str, Any]:
    return route_stage(payload)


@router.post("/score")
def score_stage(payload: dict[str, Any]) -> dict[str, Any]:
    store = _store()
    bundle = _bundle_from_payload(payload)
    parsed_results = store.fetch_parsed_results(bundle["evidence_sha256"])
    routing = RoutingResult(
        claims=tuple(store.fetch_claims(bundle["evidence_sha256"])),
        propositions=tuple(store.fetch_propositions(bundle["evidence_sha256"])),
    )
    scores = run_score_stage(store, routing, parsed_results)
    clusters = persist_proposition_clusters(store, bundle["evidence_sha256"])
    target_set = (
        store.list_review_targets(limit=5, evidence_sha256=bundle["evidence_sha256"], persist=True)
        if hasattr(store, "list_review_targets")
        else {"summary": {}}
    )
    target_summary = dict(target_set.get("summary") or {})
    return {
        "evidence_sha256": bundle["evidence_sha256"],
        "score_count": len(scores),
        "cluster_count": len(clusters),
        "review_target_count": int(target_summary.get("review_targets") or 0),
        "primary_review_target_count": int(target_summary.get("primary_review_targets") or 0),
        "validation_target_count": int(target_summary.get("validation_targets") or 0),
        "monitor_only_count": int(target_summary.get("monitor_only") or 0),
        "auto_archived_count": int(target_summary.get("auto_archived") or 0),
        "review_queue_count": len(
            store.list_review_queue(
                limit=max(1000, len(routing.propositions)),
                evidence_sha256=bundle["evidence_sha256"],
            )
        ),
    }


@router.post("/clusters/build")
def build_clusters_stage(payload: dict[str, Any]) -> dict[str, Any]:
    store = _store()
    bundle = _bundle_from_payload(payload)
    clusters = persist_proposition_clusters(store, bundle["evidence_sha256"])
    return {
        "evidence_sha256": bundle["evidence_sha256"],
        "cluster_count": len(clusters),
    }


@router.get("/clusters")
def list_clusters(
    limit: int = 50,
    evidence_sha256: str | None = None,
    include_hidden: bool = False,
) -> list[dict[str, Any]]:
    store = _store()
    if not hasattr(store, "list_proposition_clusters"):
        return []
    return store.list_proposition_clusters(
        evidence_sha256=evidence_sha256,
        limit=limit,
        include_hidden=include_hidden,
    )


@router.post("/compare")
def compare_stage(payload: dict[str, Any]) -> dict[str, Any]:
    store = _store()
    bundle = _bundle_from_payload(payload)
    baseline_provider = str(payload.get("baseline_provider") or "gemini-enterprise-agent-platform")
    candidate_providers = payload.get("candidate_providers")
    if not isinstance(candidate_providers, list) or not candidate_providers:
        candidate_providers = _candidate_providers_from_runs(store, bundle["evidence_sha256"], baseline_provider)
    comparisons = []
    for candidate_provider in candidate_providers:
        candidate = str(candidate_provider)
        if not candidate or candidate == baseline_provider:
            continue
        comparison = compare_providers(
            store,
            bundle["evidence_sha256"],
            baseline_provider=baseline_provider,
            candidate_provider=candidate,
        )
        if hasattr(store, "insert_model_comparison"):
            store.insert_model_comparison(comparison)
        comparisons.append(comparison)
    return {
        "evidence_sha256": bundle["evidence_sha256"],
        "baseline_provider": baseline_provider,
        "comparison_count": len(comparisons),
        "comparisons": comparisons,
    }


@router.get("/bundles/{evidence_sha256}")
def get_bundle(evidence_sha256: str) -> dict[str, Any]:
    bundle = _store().get_bundle(evidence_sha256)
    if bundle is None:
        raise HTTPException(status_code=404, detail="bundle not found")
    return bundle


@router.get("/pipeline-status")
def get_pipeline_status_api(
    evidence_sha256: str | None = None,
    pipeline_run_id: str | None = None,
) -> dict[str, Any]:
    evidence_sha = str(evidence_sha256 or "")
    run_id = str(pipeline_run_id or "")
    if not evidence_sha and not run_id:
        raise HTTPException(status_code=400, detail="evidence_sha256 or pipeline_run_id is required")
    return pipeline_status_from_store(_store(), evidence_sha256=evidence_sha, pipeline_run_id=run_id)


@router.get("/reviews")
def list_reviews(limit: int = 50, evidence_sha256: str | None = None) -> list[dict[str, Any]]:
    return _store().list_review_queue(limit=limit, evidence_sha256=evidence_sha256)


@router.get("/review-targets")
def list_review_targets(
    limit: int = 5,
    evidence_sha256: str | None = None,
    include_reviewed: bool = False,
) -> dict[str, Any]:
    precomputed = _require_precomputed_review_for_public_read(evidence_sha256, require_evidence_sha=True)
    if precomputed is not None and evidence_sha256:
        evidence_sha256 = _canonical_precomputed_review_sha(evidence_sha256)
        return _precomputed_review_target_set(
            precomputed,
            evidence_sha256=evidence_sha256,
            limit=limit,
            pending_only=not include_reviewed,
        )
    canonical_target_set = _canonical_review_targets_from_snapshot(
        limit=limit,
        evidence_sha256=evidence_sha256,
        pending_only=not include_reviewed,
    )
    if canonical_target_set:
        return canonical_target_set
    return _list_review_targets_cached(
        limit=limit,
        evidence_sha256=evidence_sha256,
        pending_only=not include_reviewed,
    )


@router.get("/review-targets/{review_target_id}")
def get_review_target(review_target_id: str) -> dict[str, Any]:
    target = _store().get_review_target(review_target_id)
    if target is None:
        raise HTTPException(status_code=404, detail="review target not found")
    return target


@router.get("/proposals")
def list_proposals(
    limit: int = 50,
    evidence_sha256: str | None = None,
    include_reviewed: bool = False,
    include_hidden: bool = False,
) -> list[dict[str, Any]]:
    return _store().list_proposals(
        limit=limit,
        evidence_sha256=evidence_sha256,
        pending_only=not include_reviewed,
        include_hidden=include_hidden,
    )


@router.get("/comparisons")
def list_comparisons(
    limit: int = 20,
    evidence_sha256: str | None = None,
) -> list[dict[str, Any]]:
    store = _store()
    if not hasattr(store, "list_model_comparisons"):
        return []
    return store.list_model_comparisons(evidence_sha256=evidence_sha256, limit=limit)


@router.get("/providers")
def list_providers(include_internal: bool = False) -> list[dict[str, Any]]:
    rows = _provider_rows()
    if _public_provider_details_allowed(include_internal):
        return rows
    return [_redact_provider_row(row) for row in rows]


def _provider_rows() -> list[dict[str, Any]]:
    store = _store()
    latest_runs = {}
    if hasattr(store, "list_latest_model_runs"):
        latest_runs = {
            str(run.get("provider")): run
            for run in store.list_latest_model_runs(limit=20)
        }
    result = []
    for info in provider_infos():
        provider_id = str(info.get("provider_id") or "")
        latest = latest_runs.get(provider_id, {})
        status = str(latest.get("status") or "unknown")
        raw_output = str(latest.get("raw_output") or "")
        result.append(
            {
                "provider": provider_id,
                "provider_id": provider_id,
                "display_name": info.get("display_name"),
                "model_name": info.get("model_name"),
                "configured": bool(info.get("enabled")),
                "status": info.get("status"),
                "requires_network": bool(info.get("requires_network")),
                "requires_api_key": bool(info.get("requires_api_key")),
                "supports_json_schema": bool(info.get("supports_json_schema")),
                "default_timeout_seconds": info.get("default_timeout_seconds"),
                "latest_status": status,
                "available_from_latest_run": status == "ok",
                "latest_error": _provider_error_message(raw_output) if status in {"error", "failed", "timeout"} else "",
                "latest_run_created_at": str(latest.get("created_at") or ""),
                "latest_latency_ms": latest.get("latency_ms"),
                "latest_input_tokens": latest.get("input_tokens"),
                "latest_output_tokens": latest.get("output_tokens"),
            }
        )
    return result


def _public_provider_details_allowed(include_internal: bool) -> bool:
    return bool(include_internal) and _truthy_env("OES_PUBLIC_PROVIDER_DETAILS")


def _redact_provider_row(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "provider": row.get("provider"),
        "provider_id": row.get("provider_id"),
        "display_name": row.get("display_name"),
        "configured": bool(row.get("configured")),
        "status": row.get("status"),
        "requires_network": bool(row.get("requires_network")),
        "requires_api_key": bool(row.get("requires_api_key")),
        "supports_json_schema": bool(row.get("supports_json_schema")),
        "default_timeout_seconds": row.get("default_timeout_seconds"),
        "latest_status": row.get("latest_status") or "unknown",
        "available_from_latest_run": bool(row.get("available_from_latest_run")),
    }


@router.get("/workflow/provider-policy")
def workflow_provider_policy(include_internal: bool = False) -> dict[str, Any]:
    provider_rows = _provider_rows()
    providers = provider_rows if _public_provider_details_allowed(include_internal) else [
        _redact_provider_row(row) for row in provider_rows
    ]
    by_id = {str(row.get("provider_id") or row.get("provider") or ""): row for row in provider_rows}
    alternative_ids = [
        "openai-gpt-oss-on-vertex",
        "mistral-agent-platform",
        "qwen-agent-platform",
        "glm-agent-platform",
        "claude-agent-platform",
    ]
    alternatives = [by_id.get(provider_id, {"provider_id": provider_id, "configured": False}) for provider_id in alternative_ids]
    enabled_alternatives = [row for row in alternatives if bool(row.get("configured"))]
    gemini = by_id.get("gemini-enterprise-agent-platform", {})
    max_cost = _float_env("OES_WORKFLOW_MAX_ESTIMATED_COST_USD", 0.0)
    skip_alternatives = _truthy_env("OES_WORKFLOW_SKIP_ALTERNATIVES") or not enabled_alternatives
    skip_compare = _truthy_env("OES_WORKFLOW_SKIP_COMPARE") or skip_alternatives or len(enabled_alternatives) < 1
    return {
        "schema_version": "workflow_provider_policy.v1",
        "gemini": {
            "provider_id": "gemini-enterprise-agent-platform",
            "configured": bool(gemini.get("configured")),
            "status": gemini.get("status") or "unknown",
            "latest_status": gemini.get("latest_status") or "unknown",
            "recommended_action": "run",
        },
        "alternatives": {
            "provider_ids": alternative_ids,
            "enabled_provider_ids": [str(row.get("provider_id") or row.get("provider") or "") for row in enabled_alternatives],
            "enabled_count": len(enabled_alternatives),
            "skip": skip_alternatives,
            "reason": "no_configured_alternative_provider" if not enabled_alternatives else "",
        },
        "compare": {
            "skip": skip_compare,
            "reason": "alternatives_skipped" if skip_alternatives else "",
        },
        "cost_policy": {
            "max_estimated_cost_usd": max_cost,
            "enforced": max_cost > 0,
            "pricing_requires_env_rates": True,
        },
        "providers": providers,
    }


def _start_review_decision_pipeline(
    store: Any,
    evidence_sha256: str,
    review_target_id: str,
    decision: str,
    reason: str,
) -> str:
    pipeline_run_id = start_pipeline_run(
        store,
        evidence_sha256=evidence_sha256,
        operation="review_decision",
        summary={"review_target_id": review_target_id, "decision": decision, "reason": reason},
    )
    record_pipeline_event(
        store,
        pipeline_run_id=pipeline_run_id,
        evidence_sha256=evidence_sha256,
        operation="review_decision",
        step_key="decision_received",
        status="completed",
        message="Review decision received.",
        metadata={"review_target_id": review_target_id, "decision": decision, "reason": reason},
    )
    return pipeline_run_id


def _finish_review_decision_pipeline(
    store: Any,
    evidence_sha256: str,
    pipeline_run_id: str,
    review_target_id: str,
    decision: str,
    reason: str,
) -> None:
    record_pipeline_event(
        store,
        pipeline_run_id=pipeline_run_id,
        evidence_sha256=evidence_sha256,
        operation="review_decision",
        step_key="decision_persisted",
        status="completed",
        message="Review decision persisted.",
        metadata={"review_target_id": review_target_id, "decision": decision, "reason": reason},
    )
    finish_pipeline_run(
        store,
        pipeline_run_id=pipeline_run_id,
        evidence_sha256=evidence_sha256,
        operation="review_decision",
        status="succeeded",
        message="Review decision saved.",
    )


@router.post("/review-targets/{review_target_id}/accept")
def accept_review_target(review_target_id: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    body = payload or {}
    reason = str(body.get("reason") or body.get("review_label") or "confirmed_candidate")
    if reason not in {"confirmed_candidate", "known_issue", "watchlist"}:
        raise HTTPException(status_code=400, detail="reason must be confirmed_candidate, known_issue, or watchlist")
    store = _store()
    target = store.get_review_target(review_target_id)
    if target is None:
        raise HTTPException(status_code=404, detail="review target not found")
    evidence_sha = str(target.get("evidence_sha256") or "")
    pipeline_run_id = _start_review_decision_pipeline(store, evidence_sha, review_target_id, "accepted", reason)
    try:
        result = store.record_review_target(
            review_target_id,
            "accepted",
            str(body.get("reviewer") or "api-user"),
            str(body.get("human_note") or body.get("note") or ""),
            reason=reason,
        )
        _finish_review_decision_pipeline(store, evidence_sha, pipeline_run_id, review_target_id, "accepted", reason)
        result["pipeline_run_id"] = pipeline_run_id
        result["pipeline_status"] = pipeline_status_from_store(store, evidence_sha256=evidence_sha, pipeline_run_id=pipeline_run_id)
        _clear_target_cache()
        return result
    except Exception as exc:
        finish_pipeline_run(
            store,
            pipeline_run_id=pipeline_run_id,
            evidence_sha256=evidence_sha,
            operation="review_decision",
            status="failed",
            message=str(exc),
        )
        if isinstance(exc, KeyError):
            raise HTTPException(status_code=404, detail="review target not found") from exc
        raise


@router.post("/review-targets/{review_target_id}/reject")
def reject_review_target(review_target_id: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    body = payload or {}
    reason = str(body.get("reason") or "false_positive")
    if reason not in {"false_positive", "low_value", "duplicate", "not_actionable"}:
        raise HTTPException(status_code=400, detail="reason must be false_positive, low_value, duplicate, or not_actionable")
    store = _store()
    target = store.get_review_target(review_target_id)
    if target is None:
        raise HTTPException(status_code=404, detail="review target not found")
    evidence_sha = str(target.get("evidence_sha256") or "")
    pipeline_run_id = _start_review_decision_pipeline(store, evidence_sha, review_target_id, "rejected", reason)
    try:
        result = store.record_review_target(
            review_target_id,
            "rejected",
            str(body.get("reviewer") or "api-user"),
            str(body.get("human_note") or body.get("note") or ""),
            reason=reason,
        )
        _finish_review_decision_pipeline(store, evidence_sha, pipeline_run_id, review_target_id, "rejected", reason)
        result["pipeline_run_id"] = pipeline_run_id
        result["pipeline_status"] = pipeline_status_from_store(store, evidence_sha256=evidence_sha, pipeline_run_id=pipeline_run_id)
        _clear_target_cache()
        return result
    except Exception as exc:
        finish_pipeline_run(
            store,
            pipeline_run_id=pipeline_run_id,
            evidence_sha256=evidence_sha,
            operation="review_decision",
            status="failed",
            message=str(exc),
        )
        if isinstance(exc, KeyError):
            raise HTTPException(status_code=404, detail="review target not found") from exc
        raise


@router.post("/review-targets/{review_target_id}/needs-more-data")
def needs_more_data_review_target(review_target_id: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    body = payload or {}
    store = _store()
    target = store.get_review_target(review_target_id)
    if target is None:
        raise HTTPException(status_code=404, detail="review target not found")
    evidence_sha = str(target.get("evidence_sha256") or "")
    pipeline_run_id = _start_review_decision_pipeline(store, evidence_sha, review_target_id, "needs_more_data", "needs_more_data")
    try:
        result = store.record_review_target(
            review_target_id,
            "needs_more_data",
            str(body.get("reviewer") or "api-user"),
            str(body.get("human_note") or body.get("note") or "generated additional evidence query"),
            reason="needs_more_data",
        )
        _finish_review_decision_pipeline(store, evidence_sha, pipeline_run_id, review_target_id, "needs_more_data", "needs_more_data")
        result["pipeline_run_id"] = pipeline_run_id
        result["pipeline_status"] = pipeline_status_from_store(store, evidence_sha256=evidence_sha, pipeline_run_id=pipeline_run_id)
        _clear_target_cache()
        return result
    except Exception as exc:
        finish_pipeline_run(
            store,
            pipeline_run_id=pipeline_run_id,
            evidence_sha256=evidence_sha,
            operation="review_decision",
            status="failed",
            message=str(exc),
        )
        if isinstance(exc, KeyError):
            raise HTTPException(status_code=404, detail="review target not found") from exc
        raise


@router.get("/review-targets/{review_target_id}/more-data-query")
def more_data_query_for_review_target(review_target_id: str, request_id: str | None = None) -> dict[str, Any]:
    query = _store().build_more_data_query_for_target(
        review_target_id,
        request_ids=[request_id] if request_id else None,
    )
    if not query:
        raise HTTPException(status_code=404, detail="review target not found")
    return query


@router.post("/review-targets/{review_target_id}/more-data-refresh")
def more_data_refresh_for_review_target(review_target_id: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    body = payload or {}
    store = _store()
    target = store.get_review_target(review_target_id)
    if target is None:
        raise HTTPException(status_code=404, detail="review target not found")
    request_ids = _request_ids_from_payload(body)
    request_payload = store.build_more_data_query_for_target(review_target_id, request_ids=request_ids)
    query = dict(request_payload.get("next_query") or {})
    if not query:
        raise HTTPException(status_code=404, detail="more data query not found")
    proposition_id = str(target.get("representative_proposition_id") or query.get("proposition_id") or "")
    proposal = _find_proposal(store, proposition_id, evidence_sha256=str(target.get("evidence_sha256") or query.get("evidence_sha256") or ""))
    if not proposal:
        raise HTTPException(status_code=404, detail="proposal not found")
    parent = store.get_bundle(str(proposal["evidence_sha256"]))
    if parent is None:
        raise HTTPException(status_code=404, detail="bundle not found")
    parent_evidence_sha = str(parent["evidence_sha256"])
    pipeline_run_id = start_pipeline_run(
        store,
        evidence_sha256=parent_evidence_sha,
        operation="more_data_refresh",
        summary={"review_target_id": review_target_id, "proposition_id": proposition_id},
    )
    try:
        record_pipeline_event(
            store,
            pipeline_run_id=pipeline_run_id,
            evidence_sha256=parent_evidence_sha,
            operation="more_data_refresh",
            step_key="more_data_requested",
            status="completed",
            message="More data refresh requested for review target.",
            metadata={"request_id_count": len(request_ids or [])},
        )
        bundle = _bundle_with_more_data(parent, proposal, query, review_target=target)
        store.insert_bundle(bundle)
        record_pipeline_event(
            store,
            pipeline_run_id=pipeline_run_id,
            evidence_sha256=parent_evidence_sha,
            operation="more_data_refresh",
            step_key="child_bundle_created",
            status="completed",
            message="Child Evidence Bundle created.",
            metadata={"child_evidence_sha256": bundle["evidence_sha256"], "child_bundle_count": 1},
        )
        run_models = bool(body.get("run_models", True))
        provider_names = body.get("providers")
        result = None
        if run_models:
            providers = _providers_for_names(provider_names if isinstance(provider_names, list) else ["gemini", "gpt-oss"])
            result = run_synthesis_for_bundle(store, bundle, providers=providers, parent_pipeline_run_id=pipeline_run_id)
            model_status = "completed"
            model_message = "Model rerun completed for child Evidence Bundle."
        else:
            model_status = "skipped"
            model_message = "Model rerun was skipped by request."
        record_pipeline_event(
            store,
            pipeline_run_id=pipeline_run_id,
            evidence_sha256=parent_evidence_sha,
            operation="more_data_refresh",
            step_key="model_rerun_completed",
            status=model_status,
            message=model_message,
            metadata={"child_evidence_sha256": bundle["evidence_sha256"], "run_models": run_models},
        )
        request_statuses = _more_data_request_statuses(query)
        refresh_summary = _more_data_refresh_summary(
            query,
            request_statuses=request_statuses,
            run_models=run_models,
            pipeline_result=asdict(result) if result else None,
            evidence_delta=(bundle.get("more_data") or {}).get("evidence_delta") or {},
        )
        child_chain = _child_evidence_chain(
            parent,
            bundle,
            review_target_id=review_target_id,
            proposition_id=proposition_id,
            refresh_summary=refresh_summary,
        )
        history_result = _record_more_data_result_if_supported(store, review_target_id, bundle["evidence_sha256"], refresh_summary)
        record_pipeline_event(
            store,
            pipeline_run_id=pipeline_run_id,
            evidence_sha256=parent_evidence_sha,
            operation="more_data_refresh",
            step_key="review_history_updated",
            status="completed",
            message="More data result linked back to the review target.",
            metadata={"history_recorded": bool(history_result)},
        )
        finish_pipeline_run(
            store,
            pipeline_run_id=pipeline_run_id,
            evidence_sha256=parent_evidence_sha,
            operation="more_data_refresh",
            status="succeeded",
            message="More data refresh completed.",
            metadata={"child_evidence_sha256": bundle["evidence_sha256"], "child_bundle_count": 1},
        )
    except Exception as exc:
        finish_pipeline_run(
            store,
            pipeline_run_id=pipeline_run_id,
            evidence_sha256=parent_evidence_sha,
            operation="more_data_refresh",
            status="failed",
            message=str(exc),
        )
        raise
    return {
        "parent_evidence_sha256": parent["evidence_sha256"],
        "evidence_sha256": bundle["evidence_sha256"],
        "review_target_id": review_target_id,
        "more_data_preview_count": _more_data_preview_count(query),
        "run_models": run_models,
        "pipeline_result": asdict(result) if result else None,
        "refresh_summary": refresh_summary,
        "request_statuses": request_statuses,
        "child_evidence_chain": child_chain,
        "review_target_history_result": history_result,
        "more_data_request": request_payload,
        "generated_query": query,
        "pipeline_run_id": pipeline_run_id,
        "pipeline_status": pipeline_status_from_store(store, evidence_sha256=parent_evidence_sha, pipeline_run_id=pipeline_run_id),
    }


@router.post("/review-targets/{review_target_id}/remote-collect")
def remote_collect_for_review_target(review_target_id: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    if not _remote_collector_enabled():
        raise HTTPException(
            status_code=403,
            detail="remote collector is disabled; set OES_REMOTE_COLLECTOR_ENABLED=1 in a trusted environment or use the CLI",
        )
    body = payload or {}
    store = _store()
    target = store.get_review_target(review_target_id)
    if target is None:
        raise HTTPException(status_code=404, detail="review target not found")
    request_ids = _request_ids_from_payload(body)
    request_payload = store.build_more_data_query_for_target(review_target_id, request_ids=request_ids)
    base_query = dict(request_payload.get("next_query") or {})
    if not base_query:
        raise HTTPException(status_code=404, detail="more data query not found")
    proposition_id = str(target.get("representative_proposition_id") or base_query.get("proposition_id") or "")
    proposal = _find_proposal(store, proposition_id, evidence_sha256=str(target.get("evidence_sha256") or base_query.get("evidence_sha256") or ""))
    if not proposal:
        raise HTTPException(status_code=404, detail="proposal not found")
    parent = store.get_bundle(str(proposal["evidence_sha256"]))
    if parent is None:
        raise HTTPException(status_code=404, detail="bundle not found")
    parent_evidence_sha = str(parent["evidence_sha256"])
    pipeline_run_id = start_pipeline_run(
        store,
        evidence_sha256=parent_evidence_sha,
        operation="remote_collect",
        summary={"review_target_id": review_target_id, "proposition_id": proposition_id},
    )
    record_pipeline_event(
        store,
        pipeline_run_id=pipeline_run_id,
        evidence_sha256=parent_evidence_sha,
        operation="remote_collect",
        step_key="collector_requested",
        status="completed",
        message="Remote collector requested for review target.",
        metadata={"request_id_count": len(request_ids or [])},
    )
    try:
        collector_query, inserted_logs, collector_summary = _run_remote_collector_for_query(
            store,
            parent=parent,
            target=target,
            query=base_query,
            body=body,
            request_ids=request_ids,
        )
    except Exception as exc:
        record_pipeline_event(
            store,
            pipeline_run_id=pipeline_run_id,
            evidence_sha256=parent_evidence_sha,
            operation="remote_collect",
            step_key="collector_completed",
            status="failed",
            message=str(exc),
        )
        finish_pipeline_run(
            store,
            pipeline_run_id=pipeline_run_id,
            evidence_sha256=parent_evidence_sha,
            operation="remote_collect",
            status="failed",
            message=str(exc),
        )
        if isinstance(exc, ValueError):
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        raise
    try:
        record_pipeline_event(
            store,
            pipeline_run_id=pipeline_run_id,
            evidence_sha256=parent_evidence_sha,
            operation="remote_collect",
            step_key="collector_completed",
            status="completed",
            message="Remote collector completed.",
            metadata={"inserted_logs": inserted_logs, "event_count": int(collector_summary.get("event_count") or 0)},
        )
        bundle = _bundle_with_more_data(parent, proposal, collector_query, review_target=target)
        store.insert_bundle(bundle)
        record_pipeline_event(
            store,
            pipeline_run_id=pipeline_run_id,
            evidence_sha256=parent_evidence_sha,
            operation="remote_collect",
            step_key="child_bundle_created",
            status="completed",
            message="Child Evidence Bundle created from collected evidence.",
            metadata={"child_evidence_sha256": bundle["evidence_sha256"], "child_bundle_count": 1},
        )
        run_models = bool(body.get("run_models", True))
        provider_names = body.get("providers")
        result = None
        if run_models:
            providers = _providers_for_names(provider_names if isinstance(provider_names, list) else ["gemini"])
            result = run_synthesis_for_bundle(store, bundle, providers=providers, parent_pipeline_run_id=pipeline_run_id)
            model_status = "completed"
            model_message = "Model rerun completed for child Evidence Bundle."
        else:
            model_status = "skipped"
            model_message = "Model rerun was skipped by request."
        record_pipeline_event(
            store,
            pipeline_run_id=pipeline_run_id,
            evidence_sha256=parent_evidence_sha,
            operation="remote_collect",
            step_key="model_rerun_completed",
            status=model_status,
            message=model_message,
            metadata={"child_evidence_sha256": bundle["evidence_sha256"], "run_models": run_models},
        )
        request_statuses = _more_data_request_statuses(collector_query)
        refresh_summary = _more_data_refresh_summary(
            collector_query,
            request_statuses=request_statuses,
            run_models=run_models,
            pipeline_result=asdict(result) if result else None,
            evidence_delta=(bundle.get("more_data") or {}).get("evidence_delta") or {},
        )
        child_chain = _child_evidence_chain(
            parent,
            bundle,
            review_target_id=review_target_id,
            proposition_id=proposition_id,
            refresh_summary=refresh_summary,
        )
        history_result = _record_more_data_result_if_supported(store, review_target_id, bundle["evidence_sha256"], refresh_summary)
        record_pipeline_event(
            store,
            pipeline_run_id=pipeline_run_id,
            evidence_sha256=parent_evidence_sha,
            operation="remote_collect",
            step_key="review_history_updated",
            status="completed",
            message="Collected evidence linked back to the review target.",
            metadata={"history_recorded": bool(history_result)},
        )
        finish_pipeline_run(
            store,
            pipeline_run_id=pipeline_run_id,
            evidence_sha256=parent_evidence_sha,
            operation="remote_collect",
            status="succeeded",
            message="Remote collection refresh completed.",
            metadata={"child_evidence_sha256": bundle["evidence_sha256"], "child_bundle_count": 1},
        )
    except Exception as exc:
        finish_pipeline_run(
            store,
            pipeline_run_id=pipeline_run_id,
            evidence_sha256=parent_evidence_sha,
            operation="remote_collect",
            status="failed",
            message=str(exc),
        )
        raise
    return {
        "parent_evidence_sha256": parent["evidence_sha256"],
        "evidence_sha256": bundle["evidence_sha256"],
        "review_target_id": review_target_id,
        "remote_collector": collector_summary,
        "inserted_collector_logs": inserted_logs,
        "more_data_preview_count": _more_data_preview_count(collector_query),
        "run_models": run_models,
        "pipeline_result": asdict(result) if result else None,
        "refresh_summary": refresh_summary,
        "request_statuses": request_statuses,
        "child_evidence_chain": child_chain,
        "review_target_history_result": history_result,
        "more_data_request": request_payload,
        "generated_query": collector_query,
        "pipeline_run_id": pipeline_run_id,
        "pipeline_status": pipeline_status_from_store(store, evidence_sha256=parent_evidence_sha, pipeline_run_id=pipeline_run_id),
    }


@router.post("/reviews/{proposition_id}/accept")
def accept_review(proposition_id: str, status: str = "confirmed_candidate") -> dict[str, str]:
    if status not in {"confirmed_candidate", "known_issue", "watchlist"}:
        raise HTTPException(status_code=400, detail="status must be confirmed_candidate, known_issue, or watchlist")
    review_id = _store().record_review(
        proposition_id,
        "accepted",
        "api-user",
        "",
        decision_detail=status,
        resulting_status=status,
    )
    _clear_target_cache()
    return {
        "review_id": review_id,
        "status": status,
    }


@router.post("/reviews/{proposition_id}/reject")
def reject_review(proposition_id: str, reason: str = "false_positive") -> dict[str, str]:
    if reason not in {"false_positive", "low_value", "duplicate", "not_actionable", "unsupported"}:
        raise HTTPException(status_code=400, detail="reason must be false_positive, low_value, duplicate, not_actionable, or unsupported")
    review_id = _store().record_review(
        proposition_id,
        "rejected",
        "api-user",
        "",
        decision_detail=reason,
        resulting_status=reason,
    )
    _clear_target_cache()
    return {
        "review_id": review_id,
        "status": reason,
    }


@router.post("/reviews/{proposition_id}/needs-more-data")
def needs_more_data(proposition_id: str) -> dict[str, Any]:
    store = _store()
    generated_query = store.build_more_data_query(proposition_id) if hasattr(store, "build_more_data_query") else {}
    review_id = store.record_review(
        proposition_id,
        "needs_more_data",
        "api-user",
        "generated additional evidence query",
        resulting_status="needs_more_data",
        generated_query=generated_query,
    )
    _clear_target_cache()
    return {"review_id": review_id, "status": "needs_more_data", "generated_query": generated_query}


@router.get("/propositions/{proposition_id}/more-data-query")
def more_data_query(proposition_id: str, request_id: str | None = None) -> dict[str, Any]:
    store = _store()
    if not hasattr(store, "build_more_data_query"):
        raise HTTPException(status_code=400, detail="store does not support more data queries")
    query = store.build_more_data_query(proposition_id, request_ids=[request_id] if request_id else None)
    if not query:
        raise HTTPException(status_code=404, detail="proposition not found")
    return query


@router.post("/propositions/{proposition_id}/more-data-refresh")
def more_data_refresh(proposition_id: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    body = payload or {}
    store = _store()
    request_ids = _request_ids_from_payload(body)
    query = store.build_more_data_query(proposition_id, request_ids=request_ids) if hasattr(store, "build_more_data_query") else {}
    if not query:
        raise HTTPException(status_code=404, detail="proposition not found")
    proposal = _find_proposal(store, proposition_id, evidence_sha256=str(query.get("evidence_sha256") or ""))
    if not proposal:
        raise HTTPException(status_code=404, detail="proposal not found")
    parent = store.get_bundle(str(proposal["evidence_sha256"]))
    if parent is None:
        raise HTTPException(status_code=404, detail="bundle not found")
    parent_evidence_sha = str(parent["evidence_sha256"])
    pipeline_run_id = start_pipeline_run(
        store,
        evidence_sha256=parent_evidence_sha,
        operation="more_data_refresh",
        summary={"proposition_id": proposition_id},
    )
    try:
        record_pipeline_event(
            store,
            pipeline_run_id=pipeline_run_id,
            evidence_sha256=parent_evidence_sha,
            operation="more_data_refresh",
            step_key="more_data_requested",
            status="completed",
            message="More data refresh requested for proposition.",
            metadata={"request_id_count": len(request_ids or [])},
        )
        bundle = _bundle_with_more_data(parent, proposal, query)
        store.insert_bundle(bundle)
        record_pipeline_event(
            store,
            pipeline_run_id=pipeline_run_id,
            evidence_sha256=parent_evidence_sha,
            operation="more_data_refresh",
            step_key="child_bundle_created",
            status="completed",
            message="Child Evidence Bundle created.",
            metadata={"child_evidence_sha256": bundle["evidence_sha256"], "child_bundle_count": 1},
        )
        run_models = bool(body.get("run_models", True))
        provider_names = body.get("providers")
        result = None
        if run_models:
            providers = _providers_for_names(provider_names if isinstance(provider_names, list) else ["gemini", "gpt-oss"])
            result = run_synthesis_for_bundle(store, bundle, providers=providers, parent_pipeline_run_id=pipeline_run_id)
            model_status = "completed"
            model_message = "Model rerun completed for child Evidence Bundle."
        else:
            model_status = "skipped"
            model_message = "Model rerun was skipped by request."
        record_pipeline_event(
            store,
            pipeline_run_id=pipeline_run_id,
            evidence_sha256=parent_evidence_sha,
            operation="more_data_refresh",
            step_key="model_rerun_completed",
            status=model_status,
            message=model_message,
            metadata={"child_evidence_sha256": bundle["evidence_sha256"], "run_models": run_models},
        )
        request_statuses = _more_data_request_statuses(query)
        refresh_summary = _more_data_refresh_summary(
            query,
            request_statuses=request_statuses,
            run_models=run_models,
            pipeline_result=asdict(result) if result else None,
            evidence_delta=(bundle.get("more_data") or {}).get("evidence_delta") or {},
        )
        record_pipeline_event(
            store,
            pipeline_run_id=pipeline_run_id,
            evidence_sha256=parent_evidence_sha,
            operation="more_data_refresh",
            step_key="review_history_updated",
            status="skipped",
            message="Legacy proposition refresh has no review-target history row.",
        )
        finish_pipeline_run(
            store,
            pipeline_run_id=pipeline_run_id,
            evidence_sha256=parent_evidence_sha,
            operation="more_data_refresh",
            status="succeeded",
            message="More data refresh completed.",
            metadata={"child_evidence_sha256": bundle["evidence_sha256"], "child_bundle_count": 1},
        )
    except Exception as exc:
        finish_pipeline_run(
            store,
            pipeline_run_id=pipeline_run_id,
            evidence_sha256=parent_evidence_sha,
            operation="more_data_refresh",
            status="failed",
            message=str(exc),
        )
        raise
    return {
        "parent_evidence_sha256": parent["evidence_sha256"],
        "evidence_sha256": bundle["evidence_sha256"],
        "more_data_preview_count": _more_data_preview_count(query),
        "run_models": run_models,
        "pipeline_result": asdict(result) if result else None,
        "refresh_summary": refresh_summary,
        "request_statuses": request_statuses,
        "generated_query": query,
        "pipeline_run_id": pipeline_run_id,
        "pipeline_status": pipeline_status_from_store(store, evidence_sha256=parent_evidence_sha, pipeline_run_id=pipeline_run_id),
    }


def _incident_from_payload(payload: dict[str, Any]) -> IncidentWindow:
    required = ("service", "environment", "incident_start", "incident_end")
    missing = [key for key in required if not payload.get(key)]
    if missing:
        raise HTTPException(status_code=400, detail=f"missing fields: {', '.join(missing)}")
    return IncidentWindow(
        service=str(payload["service"]),
        environment=str(payload["environment"]),
        incident_start=str(payload["incident_start"]),
        incident_end=str(payload["incident_end"]),
        lookback_minutes=int(payload.get("lookback_minutes", 60)),
    )


def _candidate_providers_from_runs(store: Any, evidence_sha256: str, baseline_provider: str) -> list[str]:
    providers = []
    if hasattr(store, "fetch_model_runs"):
        for run in store.fetch_model_runs(evidence_sha256):
            if run.provider != baseline_provider and run.provider not in providers:
                providers.append(run.provider)
    return providers


def _provider_error_message(raw_output: str) -> str:
    try:
        payload = dict(json.loads(raw_output))
    except Exception:
        return safe_provider_error_message(raw_output, max_chars=500)
    return safe_provider_error_message(str(payload.get("message") or payload.get("error") or raw_output), max_chars=500)


def _find_proposal(
    store: Any,
    proposition_id: str,
    *,
    evidence_sha256: str | None = None,
) -> dict[str, Any] | None:
    proposals = store.list_proposals(
        limit=1000,
        evidence_sha256=evidence_sha256 or None,
        pending_only=False,
        include_hidden=True,
    )
    for proposal in proposals:
        if str(proposal.get("proposition_id")) == proposition_id:
            return proposal
    return None


def _request_ids_from_payload(payload: dict[str, Any]) -> list[Any] | None:
    raw = payload.get("request_ids")
    if isinstance(raw, list):
        ids = [item for item in raw if str(item or "").strip()]
    elif raw:
        ids = [raw]
    else:
        request_id = payload.get("request_id")
        ids = [request_id] if request_id else []
    return ids or None


def _run_remote_collector_for_query(
    store: Any,
    *,
    parent: dict[str, Any],
    target: dict[str, Any],
    query: dict[str, Any],
    body: dict[str, Any],
    request_ids: list[Any] | None,
) -> tuple[dict[str, Any], int, dict[str, Any]]:
    targets = collector_targets_from_more_data(
        query,
        units=_body_string_list(body, "units", "unit"),
        paths=_body_string_list(body, "paths", "path"),
        request_ids=request_ids,
    )
    config = RemoteCollectorConfig(
        host=str(body.get("host") or os.environ.get("OES_REMOTE_COLLECTOR_DEFAULT_HOST") or "localhost"),
        service=str(parent.get("service") or target.get("service") or "ops-evidence"),
        environment=str(parent.get("environment") or target.get("environment") or "prod"),
        mode=str(body.get("mode") or os.environ.get("OES_REMOTE_COLLECTOR_MODE") or "auto"),
        ssh_user=str(body.get("ssh_user") or os.environ.get("OES_REMOTE_COLLECTOR_SSH_USER") or ""),
        ssh_key_path=str(body.get("ssh_key_path") or os.environ.get("OES_REMOTE_COLLECTOR_SSH_KEY") or ""),
        timeout_seconds=int(body.get("timeout_seconds") or os.environ.get("OES_REMOTE_COLLECTOR_TIMEOUT_SECONDS") or 12),
        allowed_path_roots=_remote_allowed_roots(),
    )
    search_window = query.get("search_window") if isinstance(query.get("search_window"), dict) else {}
    events = collect_remote_evidence(
        config,
        units=targets["units"],
        paths=targets["paths"],
        request_ids=request_ids,
        since=str(search_window.get("start") or ""),
        until=str(search_window.get("end") or ""),
    )
    sanitized = sanitize_logs(RawLog.from_mapping(event) for event in events)
    inserted_logs = store.insert_sanitized_logs(sanitized) if sanitized and hasattr(store, "insert_sanitized_logs") else 0
    collector_query = _collector_query_from_sanitized(query, sanitized, request_ids=request_ids)
    summary = {
        "host": config.host,
        "mode": config.mode,
        "units": targets["units"],
        "paths": targets["paths"],
        "event_count": len(events),
        "inserted_logs": inserted_logs,
        "enabled": True,
    }
    return collector_query, inserted_logs, summary


def _collector_query_from_sanitized(
    base_query: dict[str, Any],
    logs: list[SanitizedLog],
    *,
    request_ids: list[Any] | None,
) -> dict[str, Any]:
    preview_rows = [_preview_row_from_sanitized_log(log) for log in logs]
    base_requests = [item for item in base_query.get("next_evidence_requests") or [] if isinstance(item, dict)]
    selected_ids = {str(value).strip().casefold().replace("-", "_") for value in request_ids or [] if str(value or "").strip()}
    if selected_ids:
        requests = [
            request
            for request in base_requests
            if str(request.get("request_id") or "").casefold() in selected_ids
            or str(request.get("request_type") or "").casefold() in selected_ids
            or str(request.get("need") or "").casefold() in selected_ids
            or str(request.get("profile_request_id") or "").casefold() in selected_ids
        ]
    else:
        requests = base_requests
    if not requests:
        requests = [
            {
                "request_id": "remote_collect_query",
                "profile_request_id": "remote_collect_query",
                "request_type": "remote_collect",
                "need": "remote_collect",
                "description": "Remote collector evidence",
            }
        ]
    query_rows: list[dict[str, Any]] = []
    for request in requests:
        request_id = str(request.get("request_id") or "remote_collect_query")
        rows = [row for row in preview_rows if _preview_request_id(row) == request_id]
        if not rows:
            rows = [
                row
                for row in preview_rows
                if _preview_request_id(row) in {str(request.get("request_type") or ""), str(request.get("need") or ""), str(request.get("profile_request_id") or "")}
            ]
        query_rows.append(
            {
                **request,
                "sql": "remote_collector",
                "preview_count": len(rows),
                "preview_rows": rows[:80],
            }
        )
    if preview_rows and not any(row.get("preview_count") for row in query_rows):
        query_rows[0]["preview_count"] = len(preview_rows)
        query_rows[0]["preview_rows"] = preview_rows[:80]
    output = {
        **base_query,
        "engine": "remote_collector",
        "sql": "remote_collector",
        "queries": query_rows,
        "preview_count": sum(int(query.get("preview_count") or 0) for query in query_rows),
        "preview_rows": preview_rows[:80],
        "fallback_preview_count": len(preview_rows),
    }
    output["request_analysis"] = analyze_more_data_queries(query_rows)
    return output


def _preview_row_from_sanitized_log(log: SanitizedLog) -> dict[str, Any]:
    return {
        "timestamp": log.timestamp,
        "service": log.service,
        "severity": log.severity,
        "message_sanitized": log.message_sanitized,
        "message_template": log.message_template,
        "error_type": log.error_type,
        "labels_json": dict(log.labels_json or {}),
        "raw_log_sha256": log.raw_log_sha256,
    }


def _preview_request_id(row: dict[str, Any]) -> str:
    labels = row.get("labels_json") if isinstance(row.get("labels_json"), dict) else {}
    return str(labels.get("request_id") or "").strip()





def _remote_collector_enabled() -> bool:
    return os.environ.get("OES_REMOTE_COLLECTOR_ENABLED", "").casefold() in {"1", "true", "yes", "on"}


def _server_path_ingest_enabled() -> bool:
    return os.environ.get("OES_SERVER_PATH_INGEST_ENABLED", "").casefold() in {"1", "true", "yes", "on"}


def _truthy_env(key: str) -> bool:
    return os.environ.get(key, "").strip().casefold() in {"1", "true", "yes", "on"}


def _int_env(key: str, default: int) -> int:
    try:
        return int(os.environ.get(key, str(default)))
    except ValueError:
        return default


def _float_env(key: str, default: float) -> float:
    try:
        return float(os.environ.get(key, str(default)))
    except ValueError:
        return default


async def _write_guard_response(request: Request, request_id: str) -> JSONResponse | None:
    expected = os.environ.get("OES_API_WRITE_TOKEN", "").strip()
    if _public_budget_guard_path(request):
        if _budget_guard_token_matches(request):
            return None
        return JSONResponse(
            status_code=403,
            content={"detail": "budget guard token required"},
            headers={"X-Request-ID": request_id},
        )
    if _public_fast_gcp_review_write_allowed(request):
        return None
    if request.method.upper() in {"GET", "HEAD", "OPTIONS"}:
        return None
    if not expected:
        if _precomputed_only_ui_enabled() or _public_runtime_guard_enabled():
            log_event(
                LOGGER,
                "api_write_guard_unconfigured",
                request_id=request_id,
                method=request.method,
                path=request.url.path,
            )
            return JSONResponse(
                status_code=503,
                content={"detail": "public write guard is not configured"},
                headers={"X-Request-ID": request_id},
            )
        return None
    supplied = (request.headers.get("x-oes-write-token") or request.query_params.get("api_token") or "").strip()
    if not supplied and "application/json" in request.headers.get("content-type", ""):
        body = await request.body()
        supplied = _api_token_from_body(body).strip()
        async def receive() -> dict[str, Any]:
            return {"type": "http.request", "body": body, "more_body": False}
        request._receive = receive  # type: ignore[attr-defined]
    if hmac.compare_digest(str(supplied), str(expected)):
        return None
    log_event(
        LOGGER,
        "api_write_rejected",
        request_id=request_id,
        method=request.method,
        path=request.url.path,
    )
    return JSONResponse(
        status_code=403,
        content={"detail": "write token required"},
        headers={"X-Request-ID": request_id},
    )


def _api_token_from_body(body: bytes) -> str:
    if not body:
        return ""
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return ""
    if isinstance(payload, dict):
        return str(payload.get("api_token") or "")
    return ""


def _remote_allowed_roots() -> tuple[str, ...]:
    raw = os.environ.get("OES_REMOTE_COLLECTOR_ALLOWED_ROOTS", "")
    roots = [item.strip() for item in raw.split(",") if item.strip()]
    return tuple(roots) if roots else DEFAULT_ALLOWED_PATH_ROOTS


def _body_string_list(body: dict[str, Any], plural_key: str, singular_key: str) -> list[str]:
    raw = body.get(plural_key)
    if isinstance(raw, list):
        return [str(item) for item in raw if str(item or "").strip()]
    if raw not in (None, ""):
        return [str(raw)]
    single = body.get(singular_key)
    return [str(single)] if single not in (None, "") else []


def _bundle_with_more_data(
    parent: dict[str, Any],
    proposal: dict[str, Any],
    query: dict[str, Any],
    *,
    review_target: dict[str, Any] | None = None,
) -> dict[str, Any]:
    bundle = deepcopy(parent)
    for key, value in profile_context_for_bundle(bundle).items():
        if key == "profile":
            current_profile = bundle.get("profile") if isinstance(bundle.get("profile"), dict) else {}
            if str(current_profile.get("profile_id") or "generic") == "generic" and str((value or {}).get("profile_id") or "generic") != "generic":
                bundle[key] = value
        elif bundle.get(key) in (None, "", [], {}):
            bundle[key] = value
    preview_items: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for request_query in query.get("queries") or []:
        if not isinstance(request_query, dict):
            continue
        for row in request_query.get("preview_rows") or []:
            if isinstance(row, dict):
                preview_items.append((request_query, row))
    if not preview_items:
        fallback_request = {
            "request_id": "more_data_query",
            "need": "more_data",
            "description": "Fallback subsystem query",
            "sql": query.get("sql") or "",
        }
        preview_items = [(fallback_request, row) for row in list(query.get("preview_rows") or []) if isinstance(row, dict)]
    evidence_refs = dict(bundle.get("evidence_refs") or {})
    logs = list(bundle.get("logs") or [])
    operational_evidence = list(bundle.get("operational_evidence") or [])
    subsystem = str(proposal.get("subsystem") or query.get("subsystem") or "general")
    for index, (request_query, row) in enumerate(preview_items[:80], start=1):
        request_id = str(request_query.get("request_id") or "more_data_query")
        evidence_id = f"MORE-{_evidence_id_part(request_id)}-{index:03d}"
        log = {
            "evidence_id": evidence_id,
            "timestamp": row.get("timestamp") or "",
            "service": row.get("service") or bundle.get("service") or "",
            "environment": bundle.get("environment") or "",
            "severity": row.get("severity") or "",
            "message_sanitized": row.get("message_sanitized") or "",
            "message_template": row.get("message_template") or row.get("message_sanitized") or "",
            "error_type": row.get("error_type") or "",
            "raw_log_sha256": row.get("raw_log_sha256") or "",
            "subsystem": subsystem,
            "source": "more_data_query",
            "request_id": request_id,
            "request_need": request_query.get("need") or "",
        }
        logs.append(log)
        evidence_refs[evidence_id] = {
            "type": "more_data_log",
            "summary": log["message_sanitized"],
            "timestamp": log["timestamp"],
            "subsystem": subsystem,
            "source_proposition_id": proposal.get("proposition_id"),
            "source_review_target_id": (review_target or {}).get("review_target_id"),
            "request_id": request_id,
            "request_need": request_query.get("need") or "",
            "request_description": request_query.get("description") or "",
        }
    for index, analysis in enumerate([item for item in query.get("request_analysis") or [] if isinstance(item, dict)], start=1):
        request_id = str(analysis.get("request_id") or analysis.get("request_type") or "more_data_analysis")
        evidence_id = f"MORE-ANALYSIS-{_evidence_id_part(request_id)}-{index:02d}"
        summary = str(analysis.get("summary") or request_id)
        operational_item = {
            "evidence_id": evidence_id,
            "request_id": request_id,
            "profile_request_id": analysis.get("profile_request_id") or "",
            "request_type": analysis.get("request_type") or "",
            "subsystem": subsystem,
            "summary": summary,
            "incident_count": analysis.get("row_count") or 0,
            "baseline_count": 0,
            "baseline_daily_average": 0.0,
            "observations": list(analysis.get("observations") or []),
            "paths": list(analysis.get("paths") or []),
            "missing_paths": list(analysis.get("missing_paths") or []),
            "matched_paths": list(analysis.get("matched_paths") or []),
            "source": "more_data_analysis",
            "source_proposition_id": proposal.get("proposition_id"),
            "source_review_target_id": (review_target or {}).get("review_target_id"),
        }
        operational_evidence.append(operational_item)
        evidence_refs[evidence_id] = {
            "type": "more_data_analysis",
            "summary": summary,
            "subsystem": subsystem,
            "source": "more_data_analysis",
            "source_proposition_id": proposal.get("proposition_id"),
            "source_review_target_id": (review_target or {}).get("review_target_id"),
            "request_id": request_id,
            "profile_request_id": analysis.get("profile_request_id") or "",
            "request_need": analysis.get("need") or "",
            "request_description": summary,
            "count": analysis.get("row_count") or 0,
        }
    bundle["logs"] = logs
    bundle["operational_evidence"] = operational_evidence
    bundle["evidence_refs"] = evidence_refs
    bundle["parent_evidence_sha256"] = parent["evidence_sha256"]
    bundle["more_data"] = {
        "source_proposition_id": proposal.get("proposition_id"),
        "source_review_target_id": (review_target or {}).get("review_target_id"),
        "subsystem": subsystem,
        "query": query.get("sql") or "",
        "queries": [
            {
                "request_id": request_query.get("request_id"),
                "need": request_query.get("need"),
                "description": request_query.get("description"),
                "preview_count": request_query.get("preview_count"),
                "sql": request_query.get("sql"),
            }
            for request_query in query.get("queries") or []
            if isinstance(request_query, dict)
        ],
        "request_analysis": query.get("request_analysis") or [],
        "next_evidence_requests": query.get("next_evidence_requests") or [],
        "preview_count": len(preview_items),
        "request_statuses": _more_data_request_statuses(query),
    }
    bundle["more_data"]["evidence_delta"] = _more_data_evidence_delta(parent, bundle, query)
    lineage = {
        "relationship": "more_data_child",
        "parent_evidence_sha256": parent["evidence_sha256"],
        "source_proposition_id": proposal.get("proposition_id") or "",
        "source_review_target_id": (review_target or {}).get("review_target_id") or "",
        "source_review_target_question": (review_target or {}).get("question") or proposal.get("question") or "",
        "source_review_target_status": (review_target or {}).get("status") or "",
        "added_log_count": len(logs) - len(parent.get("logs") or []),
        "added_operational_evidence_count": len(operational_evidence) - len(parent.get("operational_evidence") or []),
        "request_ids": _unique_list(
            str(item.get("request_id") or item.get("profile_request_id") or "")
            for item in (query.get("queries") or query.get("request_analysis") or [])
            if isinstance(item, dict)
        ),
    }
    bundle["lineage"] = {**dict(bundle.get("lineage") or {}), **lineage}
    bundle["review_target_history"] = [
        *list(bundle.get("review_target_history") or []),
        {
            "event": "more_data_child_bundle_created",
            "review_target_id": lineage["source_review_target_id"],
            "proposition_id": lineage["source_proposition_id"],
            "parent_evidence_sha256": parent["evidence_sha256"],
        },
    ]
    bundle["created_at"] = utc_now()
    bundle["query_sql_hash"] = sha256_json(
        {
            "parent_evidence_sha256": parent["evidence_sha256"],
            "source_proposition_id": proposal.get("proposition_id"),
            "source_review_target_id": (review_target or {}).get("review_target_id"),
            "query": query.get("sql") or "",
            "queries": bundle["more_data"]["queries"],
            "request_analysis": bundle["more_data"]["request_analysis"],
            "preview_count": len(preview_items),
        }
    )
    hash_payload = {key: value for key, value in bundle.items() if key not in {"created_at", "evidence_sha256"}}
    bundle["evidence_sha256"] = sha256_json(hash_payload)
    return bundle


def _record_more_data_result_if_supported(
    store: Any,
    review_target_id: str,
    child_evidence_sha256: str,
    refresh_summary: dict[str, Any],
) -> dict[str, Any]:
    method = getattr(store, "record_more_data_result", None)
    if not callable(method):
        return {"status": "not_supported"}
    try:
        return dict(method(review_target_id, child_evidence_sha256, refresh_summary))
    except Exception as exc:
        log_event(
            LOGGER,
            "more_data_history_record_failed",
            review_target_id=review_target_id,
            child_evidence_sha256=child_evidence_sha256,
            error_type=exc.__class__.__name__,
        )
        return {"status": "warning", "message": "failed_to_record_review_target_history"}


def _more_data_preview_count(query: dict[str, Any]) -> int:
    if query.get("queries"):
        return sum(int(item.get("preview_count") or 0) for item in query.get("queries") or [] if isinstance(item, dict))
    return len(query.get("preview_rows") or [])


def _more_data_request_statuses(query: dict[str, Any]) -> list[dict[str, Any]]:
    query_rows = [item for item in query.get("queries") or [] if isinstance(item, dict)]
    if not query_rows:
        rows = len(query.get("preview_rows") or [])
        return [
            {
                "request_id": "more_data_query",
                "profile_request_id": "",
                "request_type": "more_data",
                "need": "more_data",
                "status": "preview_ready" if rows else "requested",
                "rows": rows,
                "reason": "" if rows else "no preview rows returned",
            }
        ]
    statuses = []
    for row in query_rows:
        rows = int(row.get("preview_count") or 0)
        if rows > 0:
            status = "preview_ready"
            reason = ""
        elif row.get("sql"):
            status = "unavailable"
            reason = "query returned no preview rows"
        else:
            status = "requested"
            reason = "query not generated"
        statuses.append(
            {
                "request_id": str(row.get("request_id") or ""),
                "profile_request_id": str(row.get("profile_request_id") or ""),
                "request_type": str(row.get("request_type") or ""),
                "need": str(row.get("need") or ""),
                "status": status,
                "rows": rows,
                "reason": reason,
                "target_component": str(row.get("target_component") or ""),
                "preferred_sources": list(row.get("preferred_sources") or []),
            }
        )
    return statuses


def _more_data_refresh_summary(
    query: dict[str, Any],
    *,
    request_statuses: list[dict[str, Any]],
    run_models: bool,
    pipeline_result: dict[str, Any] | None,
    evidence_delta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    preview_rows = _more_data_preview_count(query)
    ready = [status for status in request_statuses if status.get("status") == "preview_ready"]
    unavailable = [status for status in request_statuses if status.get("status") == "unavailable"]
    new_types = _unique_list(
        str(status.get("request_type") or status.get("need") or status.get("request_id") or "")
        for status in ready
    )
    transition = "needs_more_data -> evidence_collected" if preview_rows else "needs_more_data -> evidence_requested"
    if unavailable and not ready:
        transition = "needs_more_data -> evidence_unavailable"
    request_analysis = [item for item in query.get("request_analysis") or [] if isinstance(item, dict)]
    return {
        "added_preview_rows": preview_rows,
        "new_evidence_types": new_types,
        "request_analysis_count": len(request_analysis),
        "artifact_comparison_count": len(
            [item for item in request_analysis if str(item.get("request_type") or "") == "artifact_comparison"]
        ),
        "request_count": len(request_statuses),
        "preview_ready_count": len(ready),
        "unavailable_count": len(unavailable),
        "review_target_status_transition": transition,
        "model_rerun": {
            "requested": bool(run_models),
            "completed": bool(pipeline_result),
            "provider_count": int((pipeline_result or {}).get("model_run_count") or 0),
            "claim_count": int((pipeline_result or {}).get("claim_count") or 0),
            "review_target_count": int((pipeline_result or {}).get("review_target_count") or 0),
        },
        "evidence_delta": evidence_delta or {},
    }


def _child_evidence_chain(
    parent: dict[str, Any],
    child: dict[str, Any],
    *,
    review_target_id: str,
    proposition_id: str,
    refresh_summary: dict[str, Any],
) -> dict[str, Any]:
    return {
        "parent_evidence_sha256": str(parent.get("evidence_sha256") or ""),
        "evidence_request_review_target_id": review_target_id,
        "source_proposition_id": proposition_id,
        "generated_child_evidence_sha256": str(child.get("evidence_sha256") or ""),
        "status": str(refresh_summary.get("review_target_status_transition") or ""),
        "added_preview_rows": int(refresh_summary.get("added_preview_rows") or 0),
        "evidence_delta": dict(refresh_summary.get("evidence_delta") or {}),
        "child_bundle_profile": child.get("profile") or {"profile_id": child.get("profile_id") or child.get("environment") or "generic"},
    }


def _more_data_evidence_delta(parent: dict[str, Any], child: dict[str, Any], query: dict[str, Any]) -> dict[str, Any]:
    parent_refs = set((parent.get("evidence_refs") or {}).keys())
    child_refs = set((child.get("evidence_refs") or {}).keys())
    added_refs = sorted(child_refs - parent_refs)
    child_logs = [item for item in child.get("logs") or [] if isinstance(item, dict)]
    added_logs = [item for item in child_logs if str(item.get("source") or "") == "more_data_query"]
    child_ops = [item for item in child.get("operational_evidence") or [] if isinstance(item, dict)]
    added_analysis = [item for item in child_ops if str(item.get("source") or "") == "more_data_analysis"]
    statuses = _more_data_request_statuses(query)
    ready = [item for item in statuses if item.get("status") == "preview_ready"]
    unavailable = [item for item in statuses if item.get("status") == "unavailable"]
    return {
        "parent_evidence_ref_count": len(parent_refs),
        "child_evidence_ref_count": len(child_refs),
        "added_evidence_ref_count": len(added_refs),
        "added_evidence_refs": added_refs[:50],
        "added_log_count": len(added_logs),
        "added_analysis_count": len(added_analysis),
        "preview_row_count": _more_data_preview_count(query),
        "request_count": len(statuses),
        "preview_ready_count": len(ready),
        "unavailable_count": len(unavailable),
        "collected_request_types": _unique_list(
            str(item.get("request_type") or item.get("need") or item.get("request_id") or "")
            for item in ready
        ),
        "unavailable_request_ids": _unique_list(str(item.get("request_id") or "") for item in unavailable),
    }


def _evidence_id_part(value: str) -> str:
    import re

    text = re.sub(r"[^A-Za-z0-9]+", "-", value).strip("-").upper()
    return (text or "QUERY")[:28]


def _providers_for_names(names: list[Any]) -> list[ModelProvider]:
    providers: list[ModelProvider] = []
    for name in names:
        key = str(name).casefold().replace("_", "-")
        if key in {
            "gemini",
            "gemini-flash",
            "gemini-flash-lite",
            "gemini-2.5-flash",
            "gemini-3.1-flash-lite",
        }:
            providers.append(_gemini_provider())
        elif key in {"claude", "haiku", "claude-haiku"}:
            providers.append(_claude_provider())
        elif key in {"gpt-oss", "gpt-oss-20b", "gpt-oss-20b-maas"}:
            providers.append(_gpt_oss_provider())
        elif key in {"mistral", "mistral-small", "mistral-small-2503"}:
            providers.append(_mistral_provider())
        elif key in {
            "qwen",
            "qwen3-coder",
            "qwen3-coder-480b-a35b-instruct-maas",
            "qwen-agent-platform",
        }:
            providers.append(_qwen_provider())
        elif key in {"glm", "glm-5", "glm-5-maas", "glm-agent-platform"}:
            providers.append(_glm_provider())
        elif key in {
            "llama",
            "meta-llama",
            "llama-4-maverick",
            "llama-4-maverick-17b-128e-instruct-maas",
            "llama-agent-platform",
        }:
            providers.append(_llama_provider())
    if not providers:
        providers.append(_gemini_provider())
    return providers


def _unique_list(values: Any) -> list[str]:
    output: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if not text or text in output:
            continue
        output.append(text)
    return output


def _unique_text(values: Any) -> list[str]:
    output: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if text and text not in output:
            output.append(text)
    return output


def _configured_alternative_providers() -> list[ModelProvider]:
    names = [
        item.strip()
        for item in os.environ.get("OES_ALTERNATIVE_PROVIDERS", "gpt-oss,llama").split(",")
        if item.strip()
    ]
    return _providers_for_names(names)


def _bundle_from_payload(payload: dict[str, Any]) -> dict[str, Any]:
    evidence_sha256 = payload.get("evidence_sha256")
    if not isinstance(evidence_sha256, str) or not evidence_sha256:
        raise HTTPException(status_code=400, detail="evidence_sha256 is required")
    bundle = _store().get_bundle(evidence_sha256)
    if bundle is None:
        raise HTTPException(status_code=404, detail="bundle not found")
    return bundle


def _upload_bundle_from_payload(payload: dict[str, Any]) -> dict[str, Any]:
    candidate = payload.get("bundle") if isinstance(payload.get("bundle"), dict) else payload
    if not isinstance(candidate, dict):
        raise HTTPException(status_code=400, detail="bundle object is required")
    return dict(candidate)


def _optional_profile_discovery_from_payload(payload: dict[str, Any]) -> dict[str, Any] | None:
    candidate = payload.get("profile_discovery_bundle") if isinstance(payload.get("profile_discovery_bundle"), dict) else None
    if candidate is None and isinstance(payload.get("bundle"), dict):
        maybe = payload.get("bundle")
        if maybe.get("schema_version") == "profile_discovery_bundle.v1":
            candidate = maybe
    if candidate is None and payload.get("schema_version") == "profile_discovery_bundle.v1":
        candidate = payload
    return dict(candidate) if isinstance(candidate, dict) else None


def _upload_profile_discovery_from_payload(payload: dict[str, Any]) -> dict[str, Any]:
    candidate = _optional_profile_discovery_from_payload(payload)
    if not isinstance(candidate, dict):
        raise HTTPException(status_code=400, detail="profile discovery bundle object is required")
    return dict(candidate)


def _optional_source_context_from_payload(payload: dict[str, Any]) -> dict[str, Any]:
    candidate = payload.get("source_context_bundle") if isinstance(payload.get("source_context_bundle"), dict) else None
    if candidate is None and isinstance(payload.get("source_context"), dict):
        candidate = payload.get("source_context")
    if candidate is None and isinstance(payload.get("bundle"), dict):
        maybe = payload.get("bundle")
        if maybe.get("schema_version") == "source_context_bundle.v1":
            candidate = maybe
    if candidate is None and payload.get("schema_version") == "source_context_bundle.v1":
        candidate = payload
    return dict(candidate) if isinstance(candidate, dict) else {}


def _upload_source_context_from_payload(payload: dict[str, Any]) -> dict[str, Any]:
    candidate = _optional_source_context_from_payload(payload)
    if not candidate:
        raise HTTPException(status_code=400, detail="source_context_bundle object is required")
    return candidate


def _optional_source_analysis_from_payload(payload: dict[str, Any]) -> dict[str, Any]:
    candidate = payload.get("source_analysis_bundle") if isinstance(payload.get("source_analysis_bundle"), dict) else None
    if candidate is None and isinstance(payload.get("source_analysis"), dict):
        candidate = payload.get("source_analysis")
    if candidate is None and isinstance(payload.get("bundle"), dict):
        maybe = payload.get("bundle")
        if maybe.get("schema_version") == "source_analysis_bundle.v1":
            candidate = maybe
    if candidate is None and payload.get("schema_version") == "source_analysis_bundle.v1":
        candidate = payload
    return dict(candidate) if isinstance(candidate, dict) else {}


def _upload_source_analysis_from_payload(payload: dict[str, Any]) -> dict[str, Any]:
    candidate = _optional_source_analysis_from_payload(payload)
    if not candidate:
        raise HTTPException(status_code=400, detail="source_analysis_bundle object is required")
    return candidate

provider_error_message = _provider_error_message
bundle_with_more_data = _bundle_with_more_data
child_evidence_chain = _child_evidence_chain
more_data_evidence_delta = _more_data_evidence_delta
more_data_refresh_summary = _more_data_refresh_summary
more_data_request_statuses = _more_data_request_statuses
write_guard_response = _write_guard_response
public_precomputed_read_guard = _public_precomputed_read_guard
public_rate_limit_response = _public_rate_limit_response
validate_public_runtime_config = _validate_public_runtime_config

__all__ = [
    "bundle_with_more_data",
    "child_evidence_chain",
    "configure_api_routes",
    "more_data_evidence_delta",
    "more_data_refresh_summary",
    "more_data_request_statuses",
    "provider_error_message",
    "public_precomputed_read_guard",
    "public_rate_limit_response",
    "router",
    "validate_public_runtime_config",
    "write_guard_response",
]
