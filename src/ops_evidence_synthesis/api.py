from __future__ import annotations

import logging
import os
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import replace
from typing import Any

from ops_evidence_synthesis.ai.base import ModelProvider
from ops_evidence_synthesis.ai.claude import VertexClaudeProvider
from ops_evidence_synthesis.ai.heuristic import HeuristicProvider
from ops_evidence_synthesis.ai.maas import VertexMistralProvider, VertexOpenAICompatProvider, VertexOpenModelProvider
from ops_evidence_synthesis.ai.vertex import VertexGeminiProvider
from ops_evidence_synthesis.gcp.bigquery import BigQueryOps
from ops_evidence_synthesis.observability import configure_logging, log_event
from ops_evidence_synthesis.routes.api_routes import (
    bundle_with_more_data as _bundle_with_more_data,
    child_evidence_chain as _child_evidence_chain,
    configure_api_routes,
    more_data_evidence_delta as _more_data_evidence_delta,
    more_data_refresh_summary as _more_data_refresh_summary,
    more_data_request_statuses as _more_data_request_statuses,
    provider_error_message as _provider_error_message,
    public_precomputed_read_guard as _public_precomputed_read_guard,
    router as api_router,
    write_guard_response as _write_guard_response,
)
from ops_evidence_synthesis.storage.sqlite_store import DEFAULT_DB_PATH, SQLiteStore
from ops_evidence_synthesis.synthesis.pipeline import run_demo
from ops_evidence_synthesis.web.review_page import (
    canonical_review_graph_cards as _canonical_review_graph_cards,
    configure_review_page_store,
    evidence_request_planner_panel as _evidence_request_planner_panel,
    pipeline_progress_panel as _pipeline_progress_panel,
)

try:
    from fastapi import FastAPI, Request
except Exception as exc:  # pragma: no cover
    raise RuntimeError("Install ops-evidence-synthesis[api] to run the API") from exc


LOGGER = logging.getLogger("ops_evidence_synthesis.api")


def _store() -> Any:
    if os.environ.get("OES_STORE", "sqlite").casefold() == "bigquery":
        return BigQueryOps(
            os.environ.get("OES_GCP_PROJECT")
            or os.environ.get("OES_VERTEX_PROJECT")
            or os.environ.get("GOOGLE_CLOUD_PROJECT")
            or "ops-evidence-synthesis",
            location=os.environ.get("OES_BIGQUERY_LOCATION", "asia-northeast1"),
        )
    return SQLiteStore(os.environ.get("OES_DB_PATH", str(DEFAULT_DB_PATH)))


def _gemini_provider() -> ModelProvider:
    provider_name = os.environ.get("OES_GEMINI_PROVIDER", "local").casefold()
    if provider_name in {"vertex", "agent-platform", "gemini-enterprise-agent-platform"}:
        return VertexGeminiProvider.from_env()
    return HeuristicProvider("gemini-local", "gemini-simulated-root", "root-cause")


def _profile_draft_provider() -> ModelProvider:
    model_name = os.environ.get("OES_PROFILE_DRAFT_GEMINI_MODEL", "gemini-3.1-pro-preview")
    return VertexGeminiProvider.from_env(
        prompt_name="profile-draft",
        model_name=model_name,
        max_output_tokens=int(os.environ.get("OES_PROFILE_DRAFT_GEMINI_MAX_OUTPUT_TOKENS", "8192")),
        timeout_seconds=int(os.environ.get("OES_PROFILE_DRAFT_GEMINI_TIMEOUT_SECONDS", "180")),
    )


def _evidence_requirement_provider() -> ModelProvider:
    provider_name = os.environ.get("OES_EVIDENCE_REQUIREMENTS_PROVIDER", "gemini").casefold()
    if provider_name in {"local", "heuristic"}:
        return HeuristicProvider(
            "evidence-requirements-local",
            "evidence-requirements-simulated",
            "evidence-requirements",
        )
    if provider_name in {"gemini", "gemini-flash-lite", "gemini-3.1-flash-lite"}:
        provider = _gemini_provider()
        if isinstance(provider, VertexGeminiProvider):
            return replace(
                provider,
                prompt_name="evidence-requirements",
                max_output_tokens=min(provider.max_output_tokens, 4096),
            )
    return HeuristicProvider(
        "evidence-requirements-local",
        "evidence-requirements-simulated",
        "evidence-requirements",
    )


def _claude_provider() -> ModelProvider:
    provider_name = os.environ.get("OES_CLAUDE_PROVIDER", "vertex").casefold()
    if provider_name in {"vertex", "agent-platform", "claude-agent-platform"}:
        return VertexClaudeProvider.from_env()
    return HeuristicProvider("claude-local", "claude-simulated-root", "root-cause")


def _gpt_oss_provider() -> ModelProvider:
    provider_name = os.environ.get("OES_GPT_OSS_PROVIDER", "vertex").casefold()
    if provider_name in {"vertex", "agent-platform", "openai-gpt-oss-on-vertex"}:
        return VertexOpenAICompatProvider.from_env()
    return HeuristicProvider("gpt-oss-local", "gpt-oss-simulated-root", "alternative-hypothesis")


def _mistral_provider() -> ModelProvider:
    provider_name = os.environ.get("OES_MISTRAL_PROVIDER", "vertex").casefold()
    if provider_name in {"vertex", "agent-platform", "mistral-agent-platform"}:
        return VertexMistralProvider.from_env()
    return HeuristicProvider("mistral-local", "mistral-simulated-root", "alternative-hypothesis")


def _qwen_provider() -> ModelProvider:
    provider_name = os.environ.get("OES_QWEN_PROVIDER", "vertex").casefold()
    if provider_name in {"vertex", "agent-platform", "qwen-agent-platform"}:
        return VertexOpenModelProvider.from_qwen_env()
    return HeuristicProvider("qwen-local", "qwen-simulated-root", "alternative-hypothesis")


def _glm_provider() -> ModelProvider:
    provider_name = os.environ.get("OES_GLM_PROVIDER", "vertex").casefold()
    if provider_name in {"vertex", "agent-platform", "glm-agent-platform"}:
        return VertexOpenModelProvider.from_glm_env()
    return HeuristicProvider("glm-local", "glm-simulated-root", "alternative-hypothesis")


def _startup() -> None:
    configure_logging()
    store = _store()
    store.init_schema()
    if (
        os.environ.get("OES_STORE", "sqlite").casefold() != "bigquery"
        and os.environ.get("OES_SEED_DEMO") == "1"
        and store.count_table("evidence_bundles") == 0
    ):
        run_demo(
            db_path=os.environ.get("OES_DB_PATH", str(DEFAULT_DB_PATH)),
            sample_path=os.environ.get("OES_SAMPLE_PATH", "data/sample_logs.jsonl"),
        )


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    _startup()
    yield


configure_review_page_store(_store)
configure_api_routes(
    store_factory=_store,
    gemini_provider_factory=_gemini_provider,
    profile_draft_provider_factory=_profile_draft_provider,
    evidence_requirement_provider_factory=_evidence_requirement_provider,
    claude_provider_factory=_claude_provider,
    gpt_oss_provider_factory=_gpt_oss_provider,
    mistral_provider_factory=_mistral_provider,
    qwen_provider_factory=_qwen_provider,
    glm_provider_factory=_glm_provider,
)

app = FastAPI(
    title="Ops Evidence Synthesis",
    version="0.1.0",
    description="Evidence bundle, multi-agent synthesis, and review queue API.",
    lifespan=_lifespan,
)
app.include_router(api_router)


@app.middleware("http")
async def _request_observability(request: Request, call_next):
    request_id = request.headers.get("x-request-id") or f"req-{uuid.uuid4().hex[:16]}"
    started = time.perf_counter()
    blocked_response = await _write_guard_response(request, request_id)
    if blocked_response is not None:
        return blocked_response
    public_read_response = _public_precomputed_read_guard(request, request_id)
    if public_read_response is not None:
        return public_read_response
    response = await call_next(request)
    response.headers["X-Request-ID"] = request_id
    if request.url.path == "/" or str(response.headers.get("content-type") or "").startswith("text/html"):
        response.headers["Cache-Control"] = "no-store, max-age=0, must-revalidate"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    log_event(
        LOGGER,
        "api_request",
        request_id=request_id,
        method=request.method,
        path=request.url.path,
        status_code=response.status_code,
        latency_ms=int((time.perf_counter() - started) * 1000),
    )
    return response
