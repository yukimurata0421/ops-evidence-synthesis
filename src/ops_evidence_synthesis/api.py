from __future__ import annotations

import os
import json
import time
import hmac
import logging
import uuid
from contextlib import asynccontextmanager
from copy import deepcopy
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any
from urllib.parse import quote

from ops_evidence_synthesis.bundle import EvidenceBundleBuilder
from ops_evidence_synthesis.canonical import sha256_json
from ops_evidence_synthesis.ai.base import ModelProvider
from ops_evidence_synthesis.ai.claude import VertexClaudeProvider
from ops_evidence_synthesis.ai.heuristic import HeuristicProvider
from ops_evidence_synthesis.ai.maas import VertexMistralProvider, VertexOpenAICompatProvider
from ops_evidence_synthesis.ai.provider_registry import provider_infos
from ops_evidence_synthesis.ai.runtime import safe_provider_error_message
from ops_evidence_synthesis.ai.vertex import VertexGeminiProvider
from ops_evidence_synthesis.collectors.remote import (
    DEFAULT_ALLOWED_PATH_ROOTS,
    RemoteCollectorConfig,
    collect_remote_evidence,
    collector_targets_from_more_data,
)
from ops_evidence_synthesis.gcp.bigquery import BigQueryOps
from ops_evidence_synthesis.ingest import ingest_log_files, sanitize_logs
from ops_evidence_synthesis.evidence_request_planner import (
    build_evidence_request_plan,
    render_collection_instructions,
    sample_planner_answers,
    validate_plan_payload_inputs,
)
from ops_evidence_synthesis.local_first import validate_evidence_bundle_for_upload
from ops_evidence_synthesis.models import IncidentWindow, RawLog, SanitizedLog
from ops_evidence_synthesis.observability import configure_logging, log_event
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
    validate_profile_discovery_bundle_for_upload,
)
from ops_evidence_synthesis.profiles import profile_context_for_bundle
from ops_evidence_synthesis.storage.sqlite_store import DEFAULT_DB_PATH, SQLiteStore
from ops_evidence_synthesis.source_context import (
    validate_source_analysis_bundle_for_upload,
    validate_source_context_bundle_for_upload,
)
from ops_evidence_synthesis.synthesis.more_data import analyze_more_data_queries
from ops_evidence_synthesis.synthesis.multi_ai import SCORE_NOTE, finding_impact_from_synthesis, run_multi_ai, synthesize_multi_ai
from ops_evidence_synthesis.synthesis.review_arbitration import resolve_canonical_review_graph_snapshot
from ops_evidence_synthesis.synthesis.pipeline import (
    run_model_stage,
    run_demo,
    run_pipeline,
    run_route_stage,
    run_synthesis_for_bundle,
    run_score_stage,
)
from ops_evidence_synthesis.synthesis.clustering import persist_proposition_clusters
from ops_evidence_synthesis.synthesis.comparison import compare_providers
from ops_evidence_synthesis.synthesis.router import RoutingResult
from ops_evidence_synthesis.timeutils import utc_now

try:
    from fastapi import FastAPI, HTTPException, Request
    from fastapi.responses import HTMLResponse, JSONResponse, Response
except Exception as exc:  # pragma: no cover
    raise RuntimeError("Install ops-evidence-synthesis[api] to run the API") from exc


_TARGET_SET_CACHE: dict[tuple[str, str, int, bool], tuple[float, dict[str, Any]]] = {}
_PRECOMPUTED_REVIEW_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
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


def _precomputed_review_cache_ttl_seconds() -> int:
    return int(os.environ.get("OES_PRECOMPUTED_REVIEW_CACHE_SECONDS", "300"))


def _fast_initial_ui_enabled() -> bool:
    return os.environ.get("OES_UI_FAST_INITIAL", "1").casefold() not in {"0", "false", "no", "off"}


def _precomputed_only_ui_enabled() -> bool:
    return os.environ.get("OES_UI_PRECOMPUTED_ONLY", "0").casefold() in {"1", "true", "yes", "on"}


def _ui_detail_timeout_ms() -> int:
    return int(os.environ.get("OES_UI_DETAIL_TIMEOUT_MS", "9500"))


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


def _gemini_provider() -> ModelProvider:
    provider_name = os.environ.get("OES_GEMINI_PROVIDER", "local").casefold()
    if provider_name in {"vertex", "agent-platform", "gemini-enterprise-agent-platform"}:
        return VertexGeminiProvider.from_env()
    return HeuristicProvider("gemini-local", "gemini-simulated-root", "root-cause")


def _evidence_requirement_provider() -> ModelProvider:
    provider_name = os.environ.get("OES_EVIDENCE_REQUIREMENTS_PROVIDER", "gemini").casefold()
    if provider_name in {"local", "heuristic"}:
        return HeuristicProvider("evidence-requirements-local", "evidence-requirements-simulated", "evidence-requirements")
    if provider_name in {"gemini", "gemini-flash-lite", "gemini-3.1-flash-lite"}:
        provider = _gemini_provider()
        if isinstance(provider, VertexGeminiProvider):
            return replace(provider, prompt_name="evidence-requirements", max_output_tokens=min(provider.max_output_tokens, 4096))
        return HeuristicProvider("evidence-requirements-local", "evidence-requirements-simulated", "evidence-requirements")
    return HeuristicProvider("evidence-requirements-local", "evidence-requirements-simulated", "evidence-requirements")


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


app = FastAPI(
    title="Ops Evidence Synthesis",
    version="0.1.0",
    description="Evidence bundle, multi-agent synthesis, and review queue API.",
    lifespan=_lifespan,
)


@app.middleware("http")
async def _request_observability(request: Request, call_next):
    request_id = request.headers.get("x-request-id") or f"req-{uuid.uuid4().hex[:16]}"
    started = time.perf_counter()
    blocked_response = await _write_guard_response(request, request_id)
    if blocked_response is not None:
        return blocked_response
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


@app.head("/")
def index_head() -> Response:
    return Response(status_code=200)


@app.get("/", response_class=HTMLResponse)
def index(evidence_sha256: str | None = None, full: bool = False) -> str:
    if evidence_sha256 and _precomputed_only_ui_enabled():
        precomputed = _precomputed_review_payload(evidence_sha256)
        if not precomputed:
            raise HTTPException(status_code=404, detail="precomputed review not found")
        if full:
            return _render_precomputed_review_detail_page(evidence_sha256, precomputed)
        if _fast_initial_ui_enabled():
            return _fast_review_shell(evidence_sha256, precomputed=precomputed)
    if evidence_sha256 and _fast_initial_ui_enabled() and not full:
        precomputed = _precomputed_review_payload(evidence_sha256)
        return _fast_review_shell(evidence_sha256, precomputed=precomputed)
    return _render_full_review_page(evidence_sha256)


@app.get("/ui/full-review-page", response_class=HTMLResponse)
def full_review_page(evidence_sha256: str | None = None, full: bool = False) -> str:
    if evidence_sha256 and _precomputed_only_ui_enabled():
        precomputed = _precomputed_review_payload(evidence_sha256)
        if not precomputed:
            raise HTTPException(status_code=404, detail="precomputed review not found")
        return _render_precomputed_review_detail_page(evidence_sha256, precomputed)
    if evidence_sha256 and _fast_initial_ui_enabled() and not full:
        precomputed = _precomputed_review_payload(evidence_sha256)
        return _render_fast_review_detail_page(evidence_sha256, precomputed=precomputed)
    return _render_full_review_page(evidence_sha256)


@app.get("/ui/summary")
def ui_summary(evidence_sha256: str) -> dict[str, Any]:
    if not evidence_sha256:
        raise HTTPException(status_code=400, detail="evidence_sha256 is required")
    if _precomputed_only_ui_enabled() and not _precomputed_review_payload(evidence_sha256):
        raise HTTPException(status_code=404, detail="precomputed review not found")
    return _review_summary_for_ui(evidence_sha256)


def _precomputed_review_dirs() -> list[Path]:
    configured = [
        Path(item)
        for item in os.environ.get("OES_PRECOMPUTED_REVIEW_DIRS", "").split(os.pathsep)
        if item.strip()
    ]
    single = os.environ.get("OES_PRECOMPUTED_REVIEW_DIR")
    if single:
        configured.insert(0, Path(single))
    configured.append(Path("data/precomputed_review_summaries"))
    return configured


def _precomputed_review_payload(evidence_sha256: str) -> dict[str, Any] | None:
    evidence_id = str(evidence_sha256 or "").strip()
    if not evidence_id or len(evidence_id) > 128 or any(ch not in "0123456789abcdefABCDEF-" for ch in evidence_id):
        return None
    ttl = _precomputed_review_cache_ttl_seconds()
    cached = _PRECOMPUTED_REVIEW_CACHE.get(evidence_id)
    if ttl > 0 and cached and time.monotonic() - cached[0] < ttl:
        return deepcopy(cached[1])
    for directory in _precomputed_review_dirs():
        path = directory / f"{evidence_id}.json"
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            continue
        except Exception:
            continue
        if not isinstance(payload, dict):
            continue
        if str(payload.get("evidence_sha256") or "") != evidence_id:
            continue
        if ttl > 0:
            _PRECOMPUTED_REVIEW_CACHE[evidence_id] = (time.monotonic(), deepcopy(payload))
        return payload
    return None


def _precomputed_summary(payload: dict[str, Any] | None, evidence_sha256: str) -> dict[str, Any] | None:
    if not isinstance(payload, dict):
        return None
    summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
    if not summary:
        return None
    return {
        "schema_version": "ui_summary.v1",
        "evidence_sha256": evidence_sha256,
        "status": str(summary.get("status") or "ok"),
        "message": str(summary.get("message") or ""),
        "finding": dict(summary.get("finding") or {}),
        "review": dict(summary.get("review") or {}),
        "providers": dict(summary.get("providers") or {}),
        "baselines": dict(summary.get("baselines") or {}),
        "raw_log_policy": str(summary.get("raw_log_policy") or "unknown"),
        "log_count": int(summary.get("log_count") or 0),
        "canonical_graph_status": str(summary.get("canonical_graph_status") or "precomputed"),
        "canonical_graph_sha256": str(summary.get("canonical_graph_sha256") or ""),
        "input_fingerprint_sha256": str(summary.get("input_fingerprint_sha256") or ""),
        "updated_at": str(payload.get("updated_at") or summary.get("updated_at") or ""),
    }


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
      --warn: #a15c00;
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


def _render_precomputed_review_detail_page(evidence_sha256: str, payload: dict[str, Any]) -> str:
    summary = _precomputed_summary(payload, evidence_sha256) or {}
    finding = summary.get("finding") if isinstance(summary.get("finding"), dict) else {}
    review = summary.get("review") if isinstance(summary.get("review"), dict) else {}
    providers = summary.get("providers") if isinstance(summary.get("providers"), dict) else {}
    targets = [target for target in payload.get("targets") or [] if isinstance(target, dict)]
    graph_sha = str(summary.get("canonical_graph_sha256") or "")
    raw_policy = str(summary.get("raw_log_policy") or "unknown")
    log_count = int(summary.get("log_count") or 0)
    target_cards = "\n".join(_fast_detail_target_card(target, index=index + 1) for index, target in enumerate(targets))
    trace_panel = _precomputed_agent_trace_panel(payload)
    provider_panel = _precomputed_provider_panel(payload, providers)
    graph_summary_panel = _precomputed_review_graph_summary_panel(payload)
    devops_loop_panel = _precomputed_devops_loop_panel(payload)
    summary_url = f"/?evidence_sha256={_url_quote(evidence_sha256)}"
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
      --warn: #a15c00;
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
    .metric, .target, .trace-step, .provider-row, .graph-cell {{
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
    .targets, .trace-grid, .provider-grid, .graph-summary-grid {{ display: grid; gap: 10px; }}
    .trace-grid {{ grid-template-columns: repeat(3, minmax(0, 1fr)); }}
    .provider-grid {{ grid-template-columns: repeat(4, minmax(0, 1fr)); }}
    .graph-summary-grid {{ grid-template-columns: repeat(4, minmax(0, 1fr)); }}
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
    .position-list {{ display: grid; gap: 6px; }}
    .position-row {{
      display: grid;
      grid-template-columns: minmax(150px, 0.6fr) 96px minmax(0, 1.4fr) 96px;
      gap: 8px;
      align-items: start;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: #fff;
      padding: 8px;
    }}
    .position-row p {{ color: var(--ink); }}
    .stance {{
      display: inline-flex;
      width: max-content;
      border: 1px solid var(--line);
      border-radius: 999px;
      padding: 3px 7px;
      background: #eef6f5;
      color: var(--accent);
      font-size: 12px;
      font-weight: 800;
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
    @media (max-width: 900px) {{
      .metrics, .trace-grid, .provider-grid, .graph-summary-grid {{ grid-template-columns: 1fr 1fr; }}
    }}
    @media (max-width: 760px) {{
      header {{ display: grid; }}
      .meta {{ text-align: left; }}
      main {{ padding: 14px; }}
      .metrics, .target-grid, .target-head, .trace-grid, .provider-grid, .graph-summary-grid, .position-row {{ grid-template-columns: 1fr; }}
      .score {{ text-align: left; }}
    }}
  </style>
</head>
<body>
  <header>
    <h1>Ops Evidence Review</h1>
    <div class="meta">Evidence <code>{_html(_short_sha(evidence_sha256))}</code></div>
  </header>
  <main>
    <section class="panel">
      <label>Persisted Review Result</label>
      <h2>{_html(str(finding.get("title") or "No persisted finding yet"))}</h2>
      <p>{_html(str(finding.get("impact") or "Run analysis to create a persisted review result."))}</p>
      <div class="metrics">
        <div class="metric"><label>Canonical graph</label><strong>{_html(_short_sha(graph_sha) if graph_sha else "precomputed")}</strong></div>
        <div class="metric"><label>Providers</label><strong>{int(providers.get("success") or 0)} / {int(providers.get("total") or 0)}</strong></div>
        <div class="metric"><label>Primary</label><strong>{int(review.get("primary_targets") or 0)}</strong></div>
        <div class="metric"><label>Validation</label><strong>{int(review.get("validation_targets") or 0)}</strong></div>
        <div class="metric"><label>Raw logs</label><strong>{_html(_display_policy(raw_policy))}</strong><p>{_html(_human_count(log_count) if log_count else "sanitized bundle")}</p></div>
      </div>
    </section>
    {graph_summary_panel}
    {trace_panel}
    {devops_loop_panel}
    {provider_panel}
    <section class="panel secondary">
      <label>Review Targets</label>
      <div class="targets">
        {target_cards or '<section class="target">No review targets are persisted for this evidence.</section>'}
      </div>
      <div class="actions">
        <a class="button" href="{_html(summary_url)}">Back to summary</a>
      </div>
    </section>
  </main>
</body>
</html>"""


def _precomputed_agent_trace_panel(payload: dict[str, Any]) -> str:
    steps = [step for step in payload.get("agent_trace") or [] if isinstance(step, dict)]
    if not steps:
        return ""
    rows = "".join(
        f"""
        <article class="trace-step">
          <label>Step {index}</label>
          <strong>{_html(str(step.get("title") or step.get("step") or ""))}</strong>
          <p>{_html(str(step.get("summary") or ""))}</p>
          <div class="pill-row">
            <span class="pill">{_html(str(step.get("status") or "completed"))}</span>
            <span class="pill">{_html(str(step.get("artifact") or step.get("tool") or ""))}</span>
          </div>
        </article>
        """
        for index, step in enumerate(steps, start=1)
    )
    return f"""
    <section class="panel secondary">
      <label>Agent Trace</label>
      <h2>Guarded autonomous investigation loop</h2>
      <p>The system advances evidence collection and review planning, while final causal judgement and destructive actions stay human-gated.</p>
      <div class="trace-grid">{rows}</div>
    </section>"""


def _precomputed_provider_panel(payload: dict[str, Any], providers_summary: dict[str, Any]) -> str:
    providers = [row for row in payload.get("provider_statuses") or [] if isinstance(row, dict)]
    if not providers:
        return ""
    rows = "".join(
        f"""
        <article class="provider-row">
          <label>{_html(str(row.get("provider_id") or ""))}</label>
          <strong>{_html(str(row.get("status") or "unknown"))}</strong>
          <p>schema_valid={_html(str(bool(row.get("schema_valid"))).lower())}</p>
          <p><code>{_html(str(row.get("raw_output_sha256") or "")[:12])}</code></p>
        </article>
        """
        for row in providers
    )
    return f"""
    <section class="panel">
      <label>Provider Frontier</label>
      <h2>{int(providers_summary.get("success") or 0)} successful / {int(providers_summary.get("total") or 0)} total</h2>
      <p>Provider disagreement is preserved as validation work, not collapsed into majority truth.</p>
      <div class="provider-grid">{rows}</div>
    </section>"""


def _precomputed_review_graph_summary_panel(payload: dict[str, Any]) -> str:
    summary = payload.get("review_graph_summary")
    if not isinstance(summary, dict):
        return ""
    cells = [
        ("Converged", str(int(summary.get("convergence_count") or 0))),
        ("Conflicting", str(int(summary.get("conflict_count") or 0))),
        ("Single-source", str(int(summary.get("single_source_count") or 0))),
        ("Primary promoted", str(int(summary.get("primary_promoted_count") or 0))),
        ("Incident baseline", str(summary.get("incident_baseline") or "open")),
        ("Technical baseline", str(summary.get("technical_baseline") or "open")),
        ("Detection overlap", str(summary.get("provider_detection_overlap") or "unknown")),
        ("Auto-archived", str(int(summary.get("auto_archived_count") or 0))),
    ]
    cell_html = "".join(
        f"""
        <article class="graph-cell">
          <label>{_html(label)}</label>
          <strong>{_html(value)}</strong>
        </article>
        """
        for label, value in cells
    )
    note = str(summary.get("note") or "")
    note_html = f"<p>{_html(note)}</p>" if note else ""
    score_definition = str(summary.get("score_definition") or "")
    score_definition_html = f"<p>{_html(score_definition)}</p>" if score_definition else ""
    return f"""
    <section class="panel secondary">
      <label>Review Graph Arbitration</label>
      <h2>{_html(str(summary.get("summary") or "Provider agreement was evaluated before promotion."))}</h2>
      {note_html}
      {score_definition_html}
      <div class="graph-summary-grid">{cell_html}</div>
    </section>"""


def _precomputed_devops_loop_panel(payload: dict[str, Any]) -> str:
    loop = payload.get("devops_loop")
    if not isinstance(loop, dict):
        return ""
    items = [item for item in loop.get("items") or [] if isinstance(item, dict)]
    if not items:
        return ""
    rows = "".join(
        f"""
        <article class="graph-cell">
          <label>{_html(str(item.get("label") or "loop signal"))}</label>
          <strong>{_html(str(item.get("value") or ""))}</strong>
          <p>{_html(str(item.get("detail") or ""))}</p>
        </article>
        """
        for item in items
    )
    return f"""
    <section class="panel">
      <label>DevOps Improvement Loop</label>
      <h2>{_html(str(loop.get("title") or "AI workflow is operated as production software"))}</h2>
      <p>{_html(str(loop.get("summary") or "Pipeline events, regression cases, and tests make the agent loop observable and improvable."))}</p>
      <div class="graph-summary-grid">{rows}</div>
    </section>"""


def _provider_position_counts(target: dict[str, Any]) -> dict[str, int]:
    counts: dict[str, int] = {}
    positions = target.get("provider_positions")
    if not isinstance(positions, list):
        return counts
    for row in positions:
        if not isinstance(row, dict):
            continue
        stance = str(row.get("stance") or "silent").strip() or "silent"
        counts[stance] = counts.get(stance, 0) + 1
    return counts


def _provider_position_summary(target: dict[str, Any]) -> str:
    counts = _provider_position_counts(target)
    if not counts:
        provider_count = int(target.get("provider_count") or 0)
        return f"claimed {provider_count}" if provider_count else "not projected"
    ordered = [
        f"{name} {counts[name]}"
        for name in ("claimed", "contradicted", "silent")
        if counts.get(name)
    ]
    remaining = [
        f"{name} {value}"
        for name, value in sorted(counts.items())
        if name not in {"claimed", "contradicted", "silent"}
    ]
    return " / ".join(ordered + remaining)


def _provider_positions_html(target: dict[str, Any]) -> str:
    positions = [row for row in target.get("provider_positions") or [] if isinstance(row, dict)]
    if not positions:
        return "<p>Provider positions were not projected for this persisted target.</p>"
    rows = "".join(
        f"""
        <article class="position-row">
          <strong>{_html(str(row.get("provider_id") or "provider"))}</strong>
          <span class="stance">{_html(str(row.get("stance") or "silent"))}</span>
          <p>{_html(str(row.get("one_line") or "No normalized statement was projected."))}</p>
          <code>{_html(str(row.get("model_run_hash") or "")[:12])}</code>
        </article>
        """
        for row in positions
    )
    return f'<div class="position-list">{rows}</div>'


def _target_agreement_text(target: dict[str, Any]) -> str:
    agreement = target.get("agreement")
    if not isinstance(agreement, dict):
        return "Agreement projection is not available for this persisted target."
    verdict = str(agreement.get("verdict") or "unknown")
    score = agreement.get("convergence_score")
    try:
        score_text = f"{float(score):.3f}"
    except (TypeError, ValueError):
        score_text = "unknown"
    technical = str(agreement.get("technical_baseline") or "open")
    incident = str(agreement.get("incident_baseline") or "open")
    summary = str(agreement.get("summary") or "")
    definition = str(agreement.get("score_definition") or "")
    definition_text = f" Definition: {definition}." if definition else ""
    base = (
        f"Verdict: {verdict}. Convergence score: {score_text}. "
        f"Technical baseline: {technical}. Incident baseline: {incident}.{definition_text}"
    )
    return f"{base} {summary}".strip()


def _target_promotion_text(target: dict[str, Any]) -> str:
    promotion = target.get("promotion")
    if not isinstance(promotion, dict):
        return "Promotion gate details are not available for this persisted target."
    state = str(promotion.get("state") or "validation")
    reason = str(promotion.get("blocked_reason") or "human validation required")
    cap = promotion.get("score_cap_applied")
    cap_text = "score cap applied" if cap else "no score cap applied"
    note = str(promotion.get("score_note") or "")
    text = f"State: {state}. Blocked because: {reason}. {cap_text}."
    return f"{text} {note}".strip()


def _precomputed_target_preview_panel(targets: list[dict[str, Any]]) -> str:
    rows = "".join(
        f"""
        <article class="target-preview">
          <label>{_html(str(target.get("subsystem") or target.get("class") or "review target"))}</label>
          <strong>{_html(str(target.get("title") or "Review target"))}</strong>
          <p>{_html(str(target.get("claim") or target.get("core_claim") or "Evidence-backed validation target."))}</p>
          <div class="pill-row">
            <span class="pill">priority {_html(f"{float(target.get('review_priority_score') or target.get('score') or 0.0):.3f}")}</span>
            <span class="pill">{_html(str((target.get("agreement") or {}).get("verdict") if isinstance(target.get("agreement"), dict) else "agreement pending"))}</span>
            <span class="pill">{_html(_provider_position_summary(target))}</span>
            <span class="pill">{_html(str(target.get("recommended_request_type") or target.get("next_check") or "review"))}</span>
          </div>
        </article>
        """
        for target in targets
    )
    return f"""
    <section class="panel secondary">
      <label>Showcased Review Targets</label>
      <h2>Convergence and human-gated checks before causal judgement</h2>
      <p>The first screen shows both provider convergence and validation work immediately; deeper evidence refs are available in the detailed review.</p>
      <div class="target-preview-grid">{rows}</div>
    </section>"""


def _fast_detail_target_card(target: dict[str, Any], *, index: int) -> str:
    score = float(target.get("review_priority_score") or target.get("priority_score") or 0.0)
    title = str(target.get("title") or target.get("core_claim") or target.get("proposal") or f"Review target {index}")
    target_class = str(target.get("class") or target.get("target_class") or target.get("review_mode") or "review_target")
    status = str(target.get("status") or "pending")
    subsystem = str(target.get("subsystem") or target.get("component") or target.get("canonical_review_unit") or "general")
    evidence_refs = target.get("evidence_refs") if isinstance(target.get("evidence_refs"), list) else []
    missing = target.get("missing_evidence") if isinstance(target.get("missing_evidence"), list) else []
    caveats = target.get("caveats") if isinstance(target.get("caveats"), list) else []
    claim = str(target.get("claim") or target.get("core_claim") or target.get("impact_summary") or target.get("proposal") or "")
    action = str(target.get("recommended_validation") or target.get("recommended_request_type") or target.get("proposal") or "")
    agreement = target.get("agreement") if isinstance(target.get("agreement"), dict) else {}
    agreement_verdict = str(agreement.get("verdict") or "agreement pending")
    provider_summary = _provider_position_summary(target)
    provider_positions = _provider_positions_html(target)
    agreement_text = _target_agreement_text(target)
    promotion_text = _target_promotion_text(target)
    return f"""
<article class="target">
  <div class="target-head">
    <div>
      <label>Target {index}</label>
      <h2>{_html(title)}</h2>
      <div class="pill-row">
        <span class="pill">Class: {_html(target_class)}</span>
        <span class="pill">Status: {_html(status)}</span>
        <span class="pill">Subsystem: {_html(subsystem)}</span>
        <span class="pill">Agreement: {_html(agreement_verdict)}</span>
        <span class="pill">Provider stance: {_html(provider_summary)}</span>
        <span class="pill">Evidence refs: {_html(str(len(evidence_refs)))}</span>
      </div>
    </div>
    <div class="score">{score:.3f}<span>Priority</span></div>
  </div>
  <div class="target-grid">
    <div class="field full"><label>Observed claim</label><p>{_html(claim or title)}</p></div>
    <div class="field full"><label>Provider positions</label>{provider_positions}</div>
    <div class="field full"><label>Agreement and baselines</label><p>{_html(agreement_text)}</p></div>
    <div class="field full"><label>Why not promoted</label><p>{_html(promotion_text)}</p></div>
    <div class="field"><label>Next check</label><p>{_html(action or "Review cited evidence and missing signals.")}</p></div>
    <div class="field"><label>Missing evidence</label><p>{_html("; ".join(str(item) for item in missing[:4]) or "none")}</p></div>
    <div class="field"><label>Evidence refs</label><p>{_html(", ".join(str(item) for item in evidence_refs[:8]) or "none")}</p></div>
    <div class="field"><label>Caveats</label><p>{_html("; ".join(str(item) for item in caveats[:4]) or "none")}</p></div>
  </div>
</article>"""


def _short_sha(value: str) -> str:
    text = str(value or "")
    return text if len(text) <= 24 else f"{text[:12]}...{text[-12:]}"


def _display_policy(value: object) -> str:
    text = str(value or "").strip()
    return text.replace("_", " ").replace("-", " ") if text else "unknown"


def _url_quote(value: str) -> str:
    return quote(str(value or ""), safe="")


def _js_string(value: object) -> str:
    encoded = json.dumps(str(value or ""), ensure_ascii=False)
    return encoded[1:-1]


def _fast_review_shell(evidence_sha256: str, *, precomputed: dict[str, Any] | None = None) -> str:
    precomputed = precomputed if precomputed is not None else _precomputed_review_payload(evidence_sha256)
    summary = _precomputed_summary(precomputed, evidence_sha256) if precomputed else None
    finding = summary.get("finding") if isinstance(summary, dict) and isinstance(summary.get("finding"), dict) else {}
    review = summary.get("review") if isinstance(summary, dict) and isinstance(summary.get("review"), dict) else {}
    providers = summary.get("providers") if isinstance(summary, dict) and isinstance(summary.get("providers"), dict) else {}
    raw_policy = str(summary.get("raw_log_policy") or "pending") if isinstance(summary, dict) else "pending"
    log_count = int(summary.get("log_count") or 0) if isinstance(summary, dict) else 0
    graph_sha = str(summary.get("canonical_graph_sha256") or "") if isinstance(summary, dict) else ""
    updated_at = str(summary.get("updated_at") or "") if isinstance(summary, dict) else ""
    target_previews = [target for target in (precomputed or {}).get("targets", []) if isinstance(target, dict)][:3]
    target_preview_html = _precomputed_target_preview_panel(target_previews) if target_previews else ""
    trace_panel = _precomputed_agent_trace_panel(precomputed or {})
    graph_summary_panel = _precomputed_review_graph_summary_panel(precomputed or {})
    devops_loop_panel = _precomputed_devops_loop_panel(precomputed or {})
    short_sha = _short_sha(evidence_sha256)
    full_url = f"/ui/full-review-page?evidence_sha256={_url_quote(evidence_sha256)}"
    finding_title = str(finding.get("title") or "No persisted finding yet")
    finding_impact = str(finding.get("impact") or "Run analysis to create a persisted review result.")
    provider_text = (
        f"{int(providers.get('success') or 0)} / {int(providers.get('total') or 0)}"
        if providers
        else "pending"
    )
    primary_text = str(int(review.get("primary_targets") or 0)) if review else "pending"
    validation_text = str(int(review.get("validation_targets") or 0)) if review else "pending"
    graph_text = f"Graph {_short_sha(graph_sha)}" if graph_sha else "canonical graph pending"
    raw_log_note = _human_count(log_count) + " sanitized logs" if log_count else "sanitized evidence only"
    initial_state = "Precomputed result ready" if summary else "Report shell ready"
    initial_note = (
        "This first response already contains the persisted review summary."
        if summary
        else "The page is usable while the lightweight summary is fetched."
    )
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Ops Evidence Synthesis</title>
  <style>
    :root {{
      color-scheme: light;
      --ink: #18202a;
      --muted: #647184;
      --line: #d8dee8;
      --bg: #f7f8fb;
      --panel: #ffffff;
      --accent: #166d6b;
      --accent-2: #2f5f9e;
      --warn: #a15c00;
      --danger: #b42318;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background: var(--bg);
      color: var(--ink);
      letter-spacing: 0;
    }}
    header {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 14px;
      padding: 18px 24px;
      border-bottom: 1px solid var(--line);
      background: var(--panel);
    }}
    h1 {{ margin: 0; font-size: 20px; font-weight: 800; }}
    main {{
      display: grid;
      gap: 16px;
      padding: 18px 24px 40px;
      max-width: 1180px;
      margin: 0 auto;
    }}
    .panel {{
      border: 1px solid var(--line);
      border-left: 5px solid var(--accent);
      border-radius: 8px;
      background: var(--panel);
      padding: 16px;
      display: grid;
      gap: 12px;
      min-width: 0;
    }}
    .panel.secondary {{ border-left-color: var(--accent-2); }}
    .panel.warn {{ border-left-color: var(--warn); }}
    .panel.compact {{ border-left-color: #7c6f2b; }}
    .summary-grid {{
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 12px;
    }}
    .result-grid {{
      display: grid;
      grid-template-columns: minmax(0, 1.45fr) repeat(3, minmax(86px, 0.45fr)) minmax(150px, 0.7fr);
      gap: 10px;
      align-items: stretch;
    }}
    .result-cell {{
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 10px;
      min-width: 0;
      background: #fbfcfe;
    }}
    .trace-grid, .target-preview-grid, .graph-summary-grid {{
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 10px;
    }}
    .graph-summary-grid {{ grid-template-columns: repeat(4, minmax(0, 1fr)); }}
    .trace-step, .target-preview, .graph-cell {{
      border: 1px solid var(--line);
      border-radius: 6px;
      background: #fbfcfe;
      padding: 10px;
      min-width: 0;
    }}
    .result-cell strong {{ font-size: 18px; }}
    .result-cell p {{ font-size: 13px; }}
    label {{
      display: block;
      color: var(--muted);
      font-size: 12px;
      font-weight: 800;
      text-transform: uppercase;
      margin-bottom: 5px;
    }}
    strong {{
      display: block;
      font-size: 20px;
      line-height: 1.25;
      overflow-wrap: anywhere;
    }}
    p {{ margin: 0; color: var(--muted); line-height: 1.45; }}
    code {{
      font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, "Liberation Mono", monospace;
      overflow-wrap: anywhere;
    }}
    .meta {{
      color: var(--muted);
      font-size: 12px;
      text-align: right;
      max-width: 560px;
    }}
    .progress {{
      height: 8px;
      border-radius: 999px;
      background: #e8edf4;
      overflow: hidden;
    }}
    .progress div {{
      height: 100%;
      width: 35%;
      border-radius: inherit;
      background: var(--accent-2);
      animation: loading 1.2s ease-in-out infinite alternate;
    }}
    .actions {{ display: flex; flex-wrap: wrap; gap: 10px; }}
    button, a.button {{
      border: 1px solid var(--line);
      border-radius: 6px;
      background: #fff;
      color: var(--ink);
      padding: 8px 10px;
      font: inherit;
      font-weight: 700;
      cursor: pointer;
      text-decoration: none;
    }}
    button.primary, a.button.primary {{
      border-color: var(--accent);
      background: var(--accent);
      color: #fff;
    }}
    .error {{ color: var(--danger); }}
    @keyframes loading {{
      from {{ transform: translateX(-20%); }}
      to {{ transform: translateX(190%); }}
    }}
    @media (max-width: 760px) {{
      header {{ display: grid; align-items: start; }}
      .meta {{ text-align: left; }}
      .summary-grid, .trace-grid, .target-preview-grid, .graph-summary-grid {{ grid-template-columns: 1fr; }}
      .result-grid {{ grid-template-columns: 1fr 1fr; }}
      .result-cell:first-child {{ grid-column: 1 / -1; }}
      main {{ padding: 14px; }}
    }}
  </style>
</head>
<body>
  <header>
    <h1>Ops Evidence Synthesis</h1>
    <div class="meta">Evidence <code>{_html(short_sha)}</code></div>
  </header>
  <main>
    <section class="panel">
      <div class="summary-grid">
        <div>
          <label>Selected Evidence</label>
          <strong>{_html(short_sha)}</strong>
          <p>{_html(updated_at or "Persisted evidence selected")}</p>
        </div>
        <div>
          <label>Initial State</label>
          <strong>{_html(initial_state)}</strong>
          <p>{_html(initial_note)}</p>
        </div>
        <div>
          <label>Delivery Mode</label>
          <strong>Read-only Cloud result</strong>
          <p>Initial GET does not start model runs, collectors, or mutation work.</p>
        </div>
      </div>
    </section>
    <section class="panel compact" id="summary-panel" aria-live="polite">
      <label>Persisted Result Summary</label>
      <div class="result-grid">
        <div class="result-cell">
          <label>Finding</label>
          <strong id="summary-finding">{_html(finding_title)}</strong>
          <p id="summary-impact">{_html(finding_impact)}</p>
        </div>
        <div class="result-cell">
          <label>Providers</label>
          <strong id="summary-providers">{_html(provider_text)}</strong>
          <p>successful model outputs</p>
        </div>
        <div class="result-cell">
          <label>Primary</label>
          <strong id="summary-primary">{_html(primary_text)}</strong>
          <p>promoted targets</p>
        </div>
        <div class="result-cell">
          <label>Validation</label>
          <strong id="summary-validation">{_html(validation_text)}</strong>
          <p>human review targets</p>
        </div>
        <div class="result-cell">
          <label>Raw Logs</label>
          <strong id="summary-raw-policy">{_html(_display_policy(raw_policy))}</strong>
          <p id="summary-graph">{_html(raw_log_note)} / {_html(graph_text)}</p>
        </div>
      </div>
    </section>
    {graph_summary_panel}
    {trace_panel}
    {devops_loop_panel}
    {target_preview_html}
    <section class="panel secondary">
      <label>Detailed Review</label>
      <strong>Review targets and provider status are ready</strong>
      <p>Open the detailed page for the full target list. This route is precomputed and read-only for evaluator self-service.</p>
      <div class="actions">
        <a class="button primary" href="{_html(full_url)}">Open detailed review</a>
      </div>
    </section>
  </main>
  <script>
    const evidenceSha = "{_js_string(evidence_sha256)}";
    const summaryUrl = `/ui/summary?evidence_sha256=${{encodeURIComponent(evidenceSha)}}`;
    const setText = (id, value) => {{
      const node = document.getElementById(id);
      if (node) node.textContent = value ?? "";
    }};
    const displayPolicy = (value) => String(value || "unknown").replace(/[_-]+/g, " ");

    async function loadSummary() {{
      try {{
        const response = await fetch(summaryUrl, {{headers: {{"Accept": "application/json"}}}});
        if (!response.ok) throw new Error(`HTTP ${{response.status}}`);
        const summary = await response.json();
        setText("summary-finding", summary.finding?.title || "No persisted finding yet");
        setText("summary-impact", summary.finding?.impact || summary.message || "Run analysis to create a review summary.");
        setText("summary-providers", `${{Number(summary.providers?.success || 0)}} / ${{Number(summary.providers?.total || 0)}}`);
        setText("summary-primary", String(Number(summary.review?.primary_targets || 0)));
        setText("summary-validation", String(Number(summary.review?.validation_targets || 0)));
        setText("summary-raw-policy", displayPolicy(summary.raw_log_policy));
        const logCount = Number(summary.log_count || 0);
        const logText = logCount ? `${{logCount.toLocaleString()}} sanitized logs` : "sanitized evidence only";
        const graphText = summary.canonical_graph_sha256 ? `Graph ${{summary.canonical_graph_sha256.slice(0, 12)}}...` : "canonical graph not persisted";
        setText("summary-graph", `${{logText}} / ${{graphText}}`);
      }} catch (error) {{
        console.warn("summary refresh failed", error);
      }}
    }}

    loadSummary();
  </script>
</body>
</html>"""


def _review_targets_page(
    target_set: dict[str, Any],
    *,
    evidence_sha256: str | None = None,
    bundle: dict[str, Any] | None = None,
) -> str:
    legacy_summary = dict(target_set.get("summary") or {})
    legacy_targets = list(target_set.get("targets") or [])
    evidence_label = f"<br><code>{_html(evidence_sha256)}</code>" if evidence_sha256 else ""
    show_review = bool(evidence_sha256)
    ui_synthesis = _multi_ai_synthesis_for_ui(evidence_sha256, bundle) if show_review else {}
    canonical_graph = (
        _canonical_review_graph_for_ui(evidence_sha256, bundle, target_set, ui_synthesis)
        if show_review
        else {}
    )
    canonical_target_set = _target_set_from_canonical_graph(canonical_graph, legacy_summary)
    summary = dict(canonical_target_set.get("summary") or legacy_summary)
    targets = list(canonical_target_set.get("targets") or [])
    cards = _canonical_review_graph_cards(canonical_graph) or (
        "<section class='empty'>No review targets</section>"
    )
    data_json = _json_for_script(canonical_target_set)
    raw_count = int(legacy_summary.get("raw_propositions") or 0)
    primary_count = int(summary.get("primary_review_targets") or 0)
    validation_count = int(summary.get("validation_targets") or 0)
    review_input_count = raw_count or max(
        int(summary.get("claim_groups") or summary.get("clusters") or 0),
        primary_count
        + validation_count
        + int(summary.get("monitor_only") or 0)
        + int(summary.get("insufficient_evidence") or 0)
        + int(summary.get("auto_archived") or 0),
    )
    review_input_label = "AI proposals" if raw_count else "Review inputs"
    compression_copy = (
        f"{review_input_count} {review_input_label} summarized into {primary_count} canonical primary targets"
        f" and {validation_count} canonical validation targets"
    )
    finding_banner = _finding_banner(summary, targets, ui_synthesis, canonical_graph=canonical_graph) if show_review else ""
    upload_panel = _artifact_upload_panel(collapsed=show_review, evidence_sha256=evidence_sha256)
    top_panels = f"{finding_banner}{upload_panel}" if show_review else upload_panel
    local_first_panel = _local_first_panel(bundle)
    planner_panel = _evidence_request_planner_panel(bundle, canonical_graph=canonical_graph)
    provenance_panel = _bundle_provenance_panel(bundle)
    follow_up_panel = _follow_up_collections_panel(bundle)
    multi_ai_panel = _multi_ai_panel(evidence_sha256, canonical_graph=canonical_graph) if show_review else ""
    pipeline_status = analysis_pipeline_status_from_store(_store(), evidence_sha256=evidence_sha256 or "") if show_review else {}
    pipeline_panel = _pipeline_progress_panel(pipeline_status) if show_review else ""
    review_body = (
        f"""
    <section class="left-pane">
      <div class="summary">
        <div class="summary-item"><strong>{review_input_count}</strong><span>{_html(review_input_label)}</span></div>
        <div class="summary-item"><strong>{int(summary.get("claim_groups") or summary.get("clusters") or 0)}</strong><span>Claim groups</span></div>
        <div class="summary-item"><strong>{int(summary.get("primary_review_targets") or 0)}</strong><span>Primary targets</span></div>
        <div class="summary-item"><strong>{int(summary.get("validation_targets") or 0)}</strong><span>Validation targets</span></div>
        <div class="summary-item"><strong>{int(summary.get("monitor_only") or 0)}</strong><span>Monitor only</span></div>
        <div class="summary-item"><strong>{int(summary.get("insufficient_evidence") or 0)}</strong><span>Insufficient evidence</span></div>
        <div class="summary-item"><strong>{int(summary.get("auto_archived") or 0)}</strong><span>Auto archived</span></div>
      </div>
      <p class="score-note">{_html(summary.get("score_note") or "Score is review priority, not truth probability.")}</p>
      <p class="compression-copy">{_html(compression_copy)}</p>
      <div class="cards">{cards}</div>
    </section>
    <aside class="drawer" id="drawer">
      <h2>Evidence details</h2>
      <p class="score-note">Select a review target to inspect evidence, model outputs, parsed JSON, and source proposition IDs.</p>
    </aside>"""
        if show_review
        else """
    <section class="left-pane">
      <section class="empty">
        <strong>No Evidence Bundle selected.</strong>
        <p>Drop a sanitized <code>evidence_bundle.json</code> above to start. Existing BigQuery review targets are not shown until a bundle is selected.</p>
      </section>
    </section>"""
    )
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Ops Evidence Synthesis</title>
  <link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 16 16'%3E%3Crect width='16' height='16' rx='3' fill='%23166d6b'/%3E%3Cpath d='M4 8.5 7 11l5-6' fill='none' stroke='white' stroke-width='2'/%3E%3C/svg%3E">
  <style>
    :root {{
      color-scheme: light;
      --ink: #18202a;
      --muted: #647184;
      --line: #d8dee8;
      --bg: #f7f8fb;
      --panel: #ffffff;
      --accent: #166d6b;
      --accent-2: #2f5f9e;
      --warn: #a15c00;
      --danger: #b42318;
      --ok: #157347;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background: var(--bg);
      color: var(--ink);
      letter-spacing: 0;
    }}
    header {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      padding: 18px 24px;
      border-bottom: 1px solid var(--line);
      background: var(--panel);
    }}
    h1 {{
      margin: 0;
      font-size: 20px;
      font-weight: 700;
    }}
    main {{
      display: grid;
      grid-template-columns: minmax(0, 1fr) minmax(380px, 42vw);
      gap: 18px;
      padding: 18px 24px 40px;
    }}
    .left-pane {{
      min-width: 0;
    }}
    .finding-banner {{
      grid-column: 1 / -1;
      border: 1px solid var(--line);
      border-left: 5px solid var(--accent);
      background: var(--panel);
      border-radius: 8px;
      padding: 16px;
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 14px;
    }}
    .finding-item label {{
      display: block;
      color: var(--muted);
      font-size: 12px;
      font-weight: 800;
      text-transform: uppercase;
      margin-bottom: 5px;
    }}
    .finding-item strong {{
      display: block;
      font-size: 20px;
      line-height: 1.25;
    }}
    .finding-item span {{
      display: block;
      margin-top: 5px;
      color: var(--muted);
      font-size: 13px;
    }}
    .upload-panel {{
      grid-column: 1 / -1;
      border: 1px solid var(--line);
      border-left: 5px solid var(--accent);
      border-radius: 8px;
      background: var(--panel);
      padding: 16px;
      display: grid;
      gap: 12px;
    }}
    .upload-panel h2 {{
      margin: 0;
      font-size: 18px;
    }}
    .upload-panel-compact {{
      display: block;
      padding: 0;
      border-left-width: 1px;
    }}
    .upload-panel-compact summary {{
      padding: 12px 16px;
      cursor: pointer;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      font-weight: 700;
    }}
    .upload-panel-compact .upload-panel-body {{
      display: grid;
      gap: 12px;
      padding: 0 16px 16px;
    }}
    .drop-zone {{
      border: 2px dashed var(--line);
      border-radius: 8px;
      background: #fbfcfe;
      padding: 22px;
      display: grid;
      gap: 8px;
      text-align: center;
      cursor: pointer;
    }}
    .drop-zone.dragover {{
      border-color: var(--accent);
      background: #eef9f7;
    }}
    .drop-zone strong {{
      font-size: 16px;
    }}
    .upload-status {{
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #fbfcfe;
      padding: 10px;
      min-height: 42px;
    }}
    .pipeline-panel {{
      grid-column: 1 / -1;
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
    .pipeline-status[data-pipeline-status="succeeded"] strong {{
      color: var(--ok);
    }}
    .pipeline-status[data-pipeline-status="failed"] strong,
    .pipeline-status[data-pipeline-status="blocked"] strong {{
      color: var(--danger);
    }}
    .pipeline-status[data-pipeline-status="needs_input"] strong {{
      color: var(--warn);
    }}
    .pipeline-frontier {{
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      margin-top: 10px;
      color: var(--muted);
      font-size: 12px;
    }}
    .pipeline-frontier span {{
      border: 1px solid var(--border);
      border-radius: 6px;
      padding: 4px 8px;
      background: #fff;
      overflow-wrap: anywhere;
    }}
    .pipeline-blocking-reason {{
      margin: 8px 0 0;
      color: var(--danger);
      font-size: 13px;
      overflow-wrap: anywhere;
    }}
    .pipeline-state-summary,
    .pipeline-reason-codes {{
      display: flex;
      flex-wrap: wrap;
      gap: 6px;
      min-width: 0;
    }}
    .pipeline-state-chip,
    .pipeline-reason-chip,
    .pipeline-canonical-state {{
      display: inline-flex;
      align-items: center;
      width: fit-content;
      max-width: 100%;
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 3px 7px;
      background: #fff;
      color: var(--muted);
      font-size: 11px;
      font-weight: 800;
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
    .planner-panel, .provenance-panel, .follow-up-panel {{
      grid-column: 1;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--panel);
      padding: 16px;
      display: grid;
      gap: 14px;
      min-width: 0;
      max-width: 100%;
      overflow: hidden;
    }}
    .planner-panel details {{
      display: grid;
      gap: 14px;
      min-width: 0;
      max-width: 100%;
    }}
    .planner-panel summary {{
      cursor: pointer;
      font-weight: 700;
      color: var(--ink);
      list-style-position: inside;
    }}
    .planner-panel summary span {{
      margin-right: 10px;
    }}
    .planner-grid {{
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 12px;
      min-width: 0;
    }}
    .planner-question, .planner-request, .provenance-item, .follow-up-item {{
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #fbfcfe;
      padding: 10px;
      min-width: 0;
      max-width: 100%;
      overflow-wrap: anywhere;
    }}
    .planner-question input,
    .planner-question select,
    .planner-question textarea {{
      box-sizing: border-box;
      width: 100%;
      max-width: 100%;
      min-width: 0;
    }}
    .planner-question label {{
      max-width: 100%;
      overflow-wrap: anywhere;
      word-break: break-word;
    }}
    .planner-policy-list {{
      overflow-wrap: anywhere;
    }}
    .planner-question h3, .planner-request h3, .provenance-item h3, .follow-up-item h3 {{
      margin: 0 0 6px;
      font-size: 13px;
    }}
    .planner-question input, .planner-question select, .planner-question textarea {{
      width: 100%;
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 6px;
      margin-top: 4px;
    }}
    .planner-question textarea {{
      min-height: 68px;
    }}
    .planner-policy-list {{
      margin: 6px 0 0;
      padding-left: 18px;
      color: var(--muted);
      font-size: 12px;
    }}
    .summary {{
      display: grid;
      grid-template-columns: repeat(6, minmax(92px, 1fr));
      gap: 8px;
      margin-bottom: 12px;
    }}
    .summary-item {{
      border: 1px solid var(--line);
      background: var(--panel);
      border-radius: 8px;
      padding: 10px;
      min-height: 64px;
    }}
    .summary-item strong {{
      display: block;
      font-size: 22px;
      line-height: 1;
      font-variant-numeric: tabular-nums;
    }}
    .summary-item span {{
      display: block;
      margin-top: 6px;
      color: var(--muted);
      font-size: 12px;
    }}
    .score-note {{
      margin: 0 0 12px;
      color: var(--muted);
      font-size: 13px;
    }}
    .compression-copy {{
      margin: 0 0 10px;
      color: var(--ink);
      font-size: 14px;
      font-weight: 700;
    }}
    .cards {{
      display: grid;
      gap: 12px;
    }}
    .review-graph {{
      display: grid;
      gap: 14px;
    }}
    .graph-group {{
      display: grid;
      gap: 8px;
    }}
    .graph-overview {{
      border: 1px solid var(--line);
      border-left: 4px solid var(--accent);
      border-radius: 8px;
      background: var(--panel);
      padding: 12px;
      display: grid;
      gap: 10px;
    }}
    .primary-node {{
      display: flex;
      justify-content: space-between;
      gap: 10px;
      align-items: flex-start;
    }}
    .graph-label {{
      margin: 0 0 6px;
      color: var(--muted);
      font-size: 12px;
      font-weight: 800;
      text-transform: uppercase;
    }}
    .card {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 14px;
      min-width: 0;
      overflow-wrap: anywhere;
    }}
    .card.primary-card {{
      border-left: 4px solid var(--accent);
    }}
    .validation-tree {{
      margin-left: 18px;
      display: grid;
      gap: 8px;
      border-left: 2px solid var(--line);
      padding-left: 12px;
    }}
    .validation-node {{
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--panel);
      padding: 10px;
      min-width: 0;
      overflow-wrap: anywhere;
    }}
    .validation-node-head {{
      display: flex;
      justify-content: space-between;
      gap: 10px;
      align-items: flex-start;
      min-width: 0;
    }}
    .validation-node-head > div:first-child {{
      min-width: 0;
      flex: 1 1 auto;
    }}
    .validation-title {{
      margin: 0;
      font-size: 14px;
      font-weight: 800;
      overflow-wrap: anywhere;
      word-break: break-word;
    }}
    .graph-overview .validation-node {{
      padding: 8px;
    }}
    .node-score {{
      color: var(--accent-2);
      font-weight: 800;
      font-variant-numeric: tabular-nums;
      white-space: nowrap;
      flex: 0 0 auto;
    }}
    .card-head {{
      display: flex;
      justify-content: space-between;
      gap: 12px;
      align-items: flex-start;
      min-width: 0;
    }}
    .card-head > div:first-child {{
      min-width: 0;
      flex: 1 1 auto;
    }}
    .title {{
      margin: 0;
      font-size: 17px;
      line-height: 1.25;
      overflow-wrap: anywhere;
      word-break: break-word;
    }}
    .pill-row {{
      display: flex;
      flex-wrap: wrap;
      gap: 6px;
      margin: 8px 0 0;
    }}
    .pill {{
      border: 1px solid var(--line);
      border-radius: 999px;
      padding: 3px 8px;
      color: var(--muted);
      background: #f8fafc;
      font-size: 12px;
    }}
    .priority-high {{ color: var(--danger); border-color: #f2bbb5; background: #fff5f4; }}
    .priority-medium {{ color: var(--warn); border-color: #efcf9c; background: #fff8ed; }}
    .priority-low {{ color: var(--muted); }}
    .score {{
      min-width: 76px;
      flex: 0 0 auto;
      text-align: right;
      color: var(--accent);
      font-weight: 800;
      font-variant-numeric: tabular-nums;
    }}
    .score span {{
      display: block;
      color: var(--muted);
      font-size: 11px;
      font-weight: 600;
    }}
    .grid {{
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 10px;
      margin-top: 12px;
    }}
    .field {{
      min-width: 0;
    }}
    .field.full {{
      grid-column: 1 / -1;
    }}
    .field label {{
      display: block;
      color: var(--muted);
      font-size: 11px;
      font-weight: 700;
      text-transform: uppercase;
      margin-bottom: 3px;
    }}
    .field p {{
      margin: 0;
      font-size: 13px;
      overflow-wrap: anywhere;
    }}
    .proposal-text {{
      margin: 0;
      font-size: 13px;
      white-space: pre-wrap;
      overflow-wrap: anywhere;
    }}
    .why {{
      margin: 0;
      padding-left: 18px;
      font-size: 13px;
    }}
    .breakdown {{
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 6px;
    }}
    .metric {{
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 7px;
      background: #fbfcfe;
      min-height: 54px;
    }}
    .metric strong {{
      display: block;
      font-size: 14px;
      font-variant-numeric: tabular-nums;
    }}
    .metric span {{
      color: var(--muted);
      font-size: 11px;
    }}
    .priority-explain {{
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #fbfcfe;
      padding: 10px;
      margin-top: 12px;
    }}
    .priority-explain strong {{
      display: block;
      margin-bottom: 6px;
      font-size: 13px;
    }}
    .priority-explain ul, .evidence-summary ul, .verification-list {{
      margin: 0;
      padding-left: 18px;
      font-size: 13px;
    }}
    .priority-explain li, .evidence-summary li, .verification-list li {{
      margin: 2px 0;
    }}
    .resolution-box {{
      border: 1px solid #b8dfcc;
      border-radius: 8px;
      background: #f1fbf5;
      padding: 10px;
      display: grid;
      gap: 8px;
    }}
    .resolution-box strong {{
      color: var(--ok);
    }}
    .evidence-summary {{
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #fbfcfe;
      padding: 10px;
    }}
    details.raw-json {{
      border-top: 1px solid var(--line);
      padding-top: 10px;
      margin-top: 10px;
    }}
    details.raw-json summary {{
      cursor: pointer;
      color: var(--muted);
      font-size: 13px;
      font-weight: 800;
      text-transform: uppercase;
    }}
    details.planner-technical-json {{
      grid-column: 1 / -1;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #fbfcfe;
      padding: 10px;
      margin-top: 12px;
    }}
    details.planner-technical-json summary {{
      cursor: pointer;
      color: var(--muted);
      font-size: 12px;
      font-weight: 800;
      text-transform: uppercase;
    }}
    .planner-progress {{
      grid-column: 1 / -1;
      width: min(520px, 100%);
      margin-top: 8px;
    }}
    .planner-progress-row {{
      display: flex;
      justify-content: space-between;
      gap: 12px;
      color: var(--muted);
      font-size: 12px;
      font-weight: 700;
    }}
    .planner-progress-track {{
      height: 7px;
      margin-top: 5px;
      border-radius: 999px;
      background: #e8edf4;
      overflow: hidden;
    }}
    .planner-progress-bar {{
      width: 0%;
      height: 100%;
      border-radius: inherit;
      background: var(--accent);
      transition: width 180ms ease;
    }}
    .planner-auth {{
      grid-column: 1 / -1;
      display: grid;
      grid-template-columns: minmax(120px, 160px) minmax(180px, 1fr) auto auto minmax(120px, auto);
      gap: 8px;
      align-items: center;
      padding: 10px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #fbfcfe;
      min-width: 0;
    }}
    .planner-auth label {{
      color: var(--muted);
      font-size: 11px;
      font-weight: 800;
      text-transform: uppercase;
    }}
    .planner-auth input {{
      box-sizing: border-box;
      width: 100%;
      min-width: 0;
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 8px;
      font: inherit;
    }}
    .planner-auth input.token-missing {{
      border-color: #e07b71;
      box-shadow: 0 0 0 3px rgba(176, 58, 46, 0.12);
    }}
    .planner-auth .score-note {{
      min-width: 0;
      overflow-wrap: anywhere;
    }}
    .planner-result-panel {{
      grid-column: 1 / -1;
      display: grid;
      gap: 8px;
      padding: 10px;
      border: 1px solid var(--line);
      border-left: 4px solid var(--accent);
      border-radius: 8px;
      background: #fbfcfe;
      min-width: 0;
    }}
    .planner-result-panel strong {{
      font-size: 14px;
    }}
    .planner-result-panel p {{
      margin: 0;
      color: var(--muted);
      font-size: 13px;
    }}
    .planner-result-panel[data-state="error"] {{
      border-left-color: var(--danger);
      background: #fff8f7;
    }}
    .planner-result-panel[data-state="success"] {{
      border-left-color: var(--accent);
      background: #f1fbf5;
    }}
    .planner-result-panel .actions {{
      margin-top: 0;
    }}
    .planner-output-stamp {{
      margin: 0 0 6px 0;
      padding: 7px 9px;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: #f6f8fb;
      color: var(--muted);
      font-size: 12px;
      overflow-wrap: anywhere;
    }}
    .planner-output-stamp[data-state="changed"] {{
      border-color: #8bc5a7;
      background: #f1fbf5;
      color: var(--text);
    }}
    .planner-output-stamp[data-state="unchanged"] {{
      border-color: #d5bd74;
      background: #fff8e8;
      color: var(--text);
    }}
    .write-token-backdrop {{
      position: fixed;
      inset: 0;
      z-index: 1000;
      display: flex;
      align-items: center;
      justify-content: center;
      padding: 18px;
      background: rgba(15, 23, 42, 0.38);
    }}
    .write-token-dialog {{
      width: min(420px, 100%);
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--panel);
      box-shadow: 0 18px 50px rgba(15, 23, 42, 0.24);
      padding: 16px;
      display: grid;
      gap: 10px;
    }}
    .write-token-dialog h2 {{
      margin: 0;
      font-size: 17px;
    }}
    .write-token-dialog p {{
      margin: 0;
      color: var(--muted);
      font-size: 13px;
    }}
    .write-token-dialog input {{
      box-sizing: border-box;
      width: 100%;
      min-width: 0;
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 8px;
      font: inherit;
    }}
    .write-token-dialog .token-error {{
      color: var(--danger);
      min-height: 16px;
    }}
    .write-token-actions {{
      display: flex;
      justify-content: flex-end;
      gap: 8px;
      margin-top: 4px;
    }}
    .actions {{
      display: flex;
      gap: 8px;
      flex-wrap: wrap;
      margin-top: 12px;
    }}
    button, select, input, textarea {{
      font: inherit;
    }}
    button {{
      border: 1px solid var(--line);
      background: #fff;
      color: var(--ink);
      border-radius: 6px;
      min-height: 34px;
      padding: 6px 10px;
      cursor: pointer;
      font-size: 13px;
    }}
    button:hover {{ border-color: var(--accent); color: var(--accent); }}
    button.primary {{ background: var(--accent); color: #fff; border-color: var(--accent); }}
    button.danger {{ border-color: #f2bbb5; color: var(--danger); }}
    button:disabled {{
      cursor: progress;
      opacity: 0.65;
    }}
    .drawer {{
      position: sticky;
      top: 12px;
      align-self: start;
      min-width: 0;
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 14px;
      max-height: calc(100vh - 24px);
      overflow: auto;
      overflow-wrap: anywhere;
    }}
    .drawer h2 {{
      margin: 0 0 8px;
      font-size: 17px;
      overflow-wrap: anywhere;
      word-break: break-word;
    }}
    .drawer-section {{
      border-top: 1px solid var(--line);
      padding-top: 10px;
      margin-top: 10px;
    }}
    .drawer-section h3 {{
      margin: 0 0 6px;
      font-size: 13px;
      color: var(--muted);
      text-transform: uppercase;
    }}
    pre {{
      white-space: pre-wrap;
      word-break: break-word;
      background: #f6f8fb;
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 8px;
      font-size: 12px;
      max-height: 240px;
      overflow: auto;
    }}
    pre.planner-result-highlight {{
      border-color: #8ccfb8;
      background: #f1fbf5;
      box-shadow: 0 0 0 3px rgba(22, 109, 107, 0.16);
      transition: background 160ms ease, border-color 160ms ease, box-shadow 160ms ease;
    }}
    .decision {{
      display: grid;
      grid-template-columns: minmax(120px, 180px) minmax(0, 1fr);
      gap: 8px;
      margin-top: 10px;
    }}
    .decision textarea {{
      grid-column: 1 / -1;
      min-height: 54px;
      border-radius: 6px;
      border: 1px solid var(--line);
      padding: 8px;
    }}
    .empty {{ background: var(--panel); border: 1px solid var(--line); border-radius: 8px; padding: 24px; color: var(--muted); }}
    code {{
      font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      font-size: 12px;
      overflow-wrap: anywhere;
      word-break: break-word;
    }}
    @media (max-width: 980px) {{
      header {{ align-items: flex-start; flex-direction: column; gap: 6px; }}
      main {{ display: block; padding: 12px; }}
      .finding-banner {{ grid-template-columns: 1fr; margin-bottom: 12px; }}
      .pipeline-panel {{ margin-bottom: 12px; }}
      .pipeline-header {{ display: grid; grid-template-columns: 1fr; }}
      .pipeline-status {{ text-align: left; }}
      .planner-grid {{ grid-template-columns: 1fr; }}
      .planner-auth {{ grid-template-columns: 1fr; }}
      .summary {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
      .grid, .breakdown, .decision {{ grid-template-columns: 1fr; }}
      .field.full, .decision textarea {{ grid-column: auto; }}
      .drawer {{ position: static; margin-top: 12px; max-height: none; }}
    }}
  </style>
</head>
<body>
  <header>
    <h1>Ops Evidence Synthesis</h1>
    <div><code>{_html("Evidence " + _short_sha(evidence_sha256 or ""))}</code>{"" if evidence_sha256 else evidence_label}</div>
  </header>
  <main>
    {top_panels}
    {pipeline_panel}
    {multi_ai_panel}
    {local_first_panel}
    {review_body}
    {provenance_panel}
    {follow_up_panel}
    {planner_panel}
  </main>
  <script type="application/json" id="target-data">{data_json}</script>
  <script>
	    const targetSet = JSON.parse(document.getElementById("target-data").textContent);
	    const targets = new Map((targetSet.targets || []).map((target) => [target.review_target_id, target]));
	    const nativeFetch = window.fetch.bind(window);
	    const writeTokenStorageKey = "oes.write_token";

	    function storedWriteToken() {{
	      try {{
	        return window.localStorage.getItem(writeTokenStorageKey) || "";
	      }} catch (error) {{
	        return "";
	      }}
	    }}

	    function saveWriteToken(token) {{
	      try {{
	        window.localStorage.setItem(writeTokenStorageKey, token);
	      }} catch (error) {{}}
	    }}

	    function clearWriteToken() {{
	      try {{
	        window.localStorage.removeItem(writeTokenStorageKey);
	      }} catch (error) {{}}
	    }}

	    function writeHeaders(headers, tokenOverride = null) {{
	      const output = new Headers(headers || {{}});
	      const token = String(tokenOverride ?? storedWriteToken()).trim();
	      if (token) output.set("X-OES-Write-Token", token);
	      return output;
	    }}

	    function syntheticWriteTokenResponse() {{
	      return new Response(JSON.stringify({{detail: "write token required"}}), {{
	        status: 403,
	        headers: {{"Content-Type": "application/json"}},
	      }});
	    }}

	    function showWriteTokenDialog(message) {{
	      return new Promise((resolve) => {{
	        const previous = document.getElementById("write-token-backdrop");
	        if (previous) previous.remove();
	        const backdrop = document.createElement("div");
	        backdrop.id = "write-token-backdrop";
	        backdrop.className = "write-token-backdrop";
	        backdrop.innerHTML = `
	          <form id="write-token-dialog" class="write-token-dialog" aria-modal="true">
	            <h2>Write token required</h2>
	            <p id="write-token-message"></p>
	            <input id="write-token-input" name="write-token" type="password" autocomplete="off" spellcheck="false" aria-label="Write token">
	            <p id="write-token-error" class="token-error" role="alert"></p>
	            <div class="write-token-actions">
	              <button type="button" id="write-token-cancel">Cancel</button>
	              <button type="submit" class="primary">Continue</button>
	            </div>
	          </form>`;
	        document.body.appendChild(backdrop);
	        const form = document.getElementById("write-token-dialog");
	        const input = document.getElementById("write-token-input");
	        const error = document.getElementById("write-token-error");
	        const messageNode = document.getElementById("write-token-message");
	        const cancel = document.getElementById("write-token-cancel");
	        if (messageNode) messageNode.textContent = message || "Enter the current write token to continue.";
	        function close(value) {{
	          backdrop.remove();
	          resolve(value || "");
	        }}
	        form?.addEventListener("submit", (event) => {{
	          event.preventDefault();
	          const token = String(input?.value || "").trim();
	          if (!token) {{
	            if (error) error.textContent = "Write token is required.";
	            input?.focus();
	            return;
	          }}
	          saveWriteToken(token);
	          close(token);
	        }});
	        cancel?.addEventListener("click", () => close(""));
	        backdrop.addEventListener("keydown", (event) => {{
	          if (event.key === "Escape") close("");
	        }});
	        window.setTimeout(() => input?.focus(), 0);
	      }});
	    }}

	    async function ensureWriteToken(message) {{
	      const token = storedWriteToken().trim();
	      if (token) return token;
	      return showWriteTokenDialog(message);
	    }}

	    function plannerWriteTokenInput() {{
	      return document.getElementById("planner-write-token-input");
	    }}

	    function plannerHasWriteToken() {{
	      const typed = String(plannerWriteTokenInput()?.value || "").trim();
	      return Boolean(typed || storedWriteToken().trim());
	    }}

	    function updatePlannerGenerateEnabled() {{
	      const button = document.getElementById("planner-refine-button");
	      if (!button) return;
	      const enabled = plannerHasWriteToken();
	      button.disabled = !enabled;
	      button.setAttribute("aria-disabled", String(!enabled));
	      button.title = enabled ? "Generate refined plan" : "Enter write token before generating";
	    }}

	    function setPlannerResult(message, state = "idle") {{
	      const panel = document.getElementById("planner-result-panel");
	      const messageNode = document.getElementById("planner-result-message");
	      if (panel) panel.dataset.state = state;
	      if (messageNode) messageNode.textContent = message || "";
	    }}

	    function setPlannerOutputStamp(message, state = "idle") {{
	      const stamp = document.getElementById("planner-output-stamp");
	      if (!stamp) return;
	      stamp.textContent = message || "";
	      stamp.dataset.state = state;
	    }}

	    function plannerSubmittedFieldCount(plannerAnswers) {{
	      const answers = plannerAnswers && typeof plannerAnswers === "object" && plannerAnswers.answers && typeof plannerAnswers.answers === "object"
	        ? plannerAnswers.answers
	        : {{}};
	      return Object.keys(answers).length;
	    }}

	    function openPlannerPanel() {{
	      const details = document.querySelector("#evidence-request-planner > details");
	      if (details) details.open = true;
	    }}

	    function setPlannerTokenStatus(message, isError = false) {{
	      const status = document.getElementById("planner-write-token-status");
	      if (!status) return;
	      status.textContent = message || "";
	      status.style.color = isError ? "var(--danger)" : "var(--muted)";
	    }}

	    function savePlannerWriteToken() {{
	      const input = plannerWriteTokenInput();
	      const token = String(input?.value || "").trim();
	      if (!token) {{
	        input?.classList.add("token-missing");
	        input?.focus();
	        setPlannerTokenStatus("Token is required.", true);
	        return "";
	      }}
	      input?.classList.remove("token-missing");
	      saveWriteToken(token);
	      setPlannerTokenStatus("Token saved in this browser.");
	      updatePlannerGenerateEnabled();
	      return token;
	    }}

	    function clearPlannerWriteToken() {{
	      const input = plannerWriteTokenInput();
	      if (input) {{
	        input.value = "";
	        input.classList.remove("token-missing");
	      }}
	      clearWriteToken();
	      setPlannerTokenStatus("Token cleared.");
	      updatePlannerGenerateEnabled();
	    }}

	    function requirePlannerWriteToken() {{
	      const input = plannerWriteTokenInput();
	      const typed = String(input?.value || "").trim();
	      if (typed) return savePlannerWriteToken();
	      const stored = storedWriteToken().trim();
	      if (stored) {{
	        setPlannerTokenStatus("Using saved token.");
	        return stored;
	      }}
	      input?.classList.add("token-missing");
	      input?.scrollIntoView({{behavior: "smooth", block: "center"}});
	      input?.focus();
	      setPlannerTokenStatus("Enter write token before generating.", true);
	      updatePlannerGenerateEnabled();
	      return "";
	    }}

	    window.fetch = async function(url, options = {{}}) {{
	      const method = String(options?.method || "GET").toUpperCase();
	      const writeMethod = !["GET", "HEAD", "OPTIONS"].includes(method);
	      let token = "";
	      if (writeMethod) {{
	        token = await ensureWriteToken("Enter the write token to continue this write action.");
	        if (!token) return syntheticWriteTokenResponse();
	      }}
	      const requestOptions = writeMethod ? {{...options, headers: writeHeaders(options.headers, token)}} : options;
	      const response = await nativeFetch(url, requestOptions);
	      if (writeMethod && [401, 403].includes(response.status)) {{
	        clearWriteToken();
	        const retryToken = await showWriteTokenDialog("Write token was rejected. Enter the current write token.");
	        if (retryToken) return nativeFetch(url, {{...options, headers: writeHeaders(options.headers, retryToken)}});
	      }}
	      return response;
	    }};

	    function esc(value) {{
      return String(value ?? "").replace(/[&<>"']/g, (char) => ({{
        "&": "&amp;",
        "<": "&lt;",
        ">": "&gt;",
        "\\"": "&quot;",
        "'": "&#39;"
      }}[char]));
    }}

    function pretty(value) {{
      return esc(JSON.stringify(value ?? null, null, 2));
    }}

    function list(items) {{
      const values = Array.isArray(items) ? items : [];
      if (!values.length) return "<p class='score-note'>No records.</p>";
      return "<ul class='why'>" + values.map((item) => `<li>${{esc(item)}}</li>`).join("") + "</ul>";
    }}

    function rawDetails(title, value) {{
      return `<details class="raw-json"><summary>${{esc(title)}}</summary><pre>${{pretty(value)}}</pre></details>`;
    }}

    function pipelineStatusClass(status) {{
      return String(status || "unknown").replace(/[^a-zA-Z0-9_-]/g, "_") || "unknown";
    }}

    function pipelineStepHtml(step) {{
      const status = String(step?.status || "pending");
      const canonicalState = String(step?.canonical_state || "");
      const stateChip = canonicalState ? `<span class="pipeline-canonical-state">${{esc(canonicalState)}}</span>` : "";
      return `
        <li data-step-status="${{esc(status)}}">
          <span class="pipeline-step-state">${{esc(status)}}</span>${{stateChip}}
          <strong>${{esc(step?.step_label || step?.step_key || "")}}</strong>
          <small>${{esc(step?.message || "")}}</small>
        </li>`;
    }}

    function pipelineEventHtml(event) {{
      const reason = event?.reason_code ? ` / ${{esc(event.reason_code)}}` : "";
      const provider = event?.provider_id ? ` / ${{esc(event.provider_id)}}` : "";
      const canonicalState = event?.canonical_state ? ` / ${{esc(event.canonical_state)}}` : "";
      return `
        <li>
          <code>${{esc(event?.step_key || "")}}</code>
          <span>${{esc(event?.status || "")}}${{canonicalState}}${{reason}}${{provider}}</span>
          <small>${{esc(event?.message || "")}}</small>
        </li>`;
    }}

    function pipelineStateTimelineHtml(status) {{
      const current = String(status?.canonical_state || "");
      const timeline = Array.isArray(status?.state_timeline) ? status.state_timeline : [];
      const states = [];
      for (const item of timeline) {{
        const state = String(item?.state || "");
        if (state && !states.includes(state)) states.push(state);
      }}
      if (current && !states.includes(current)) states.push(current);
      return states.map((state) => `<span class="pipeline-state-chip" data-current-state="${{state === current ? "true" : "false"}}">${{esc(state)}}</span>`).join("");
    }}

    function pipelineReasonCodesHtml(status) {{
      const reasons = Array.isArray(status?.active_reasons) ? status.active_reasons : [];
      return reasons.map((reason) => `<span class="pipeline-reason-chip">${{esc(reason)}}</span>`).join("");
    }}

    function providerFrontierText(status) {{
      const total = Number(status?.provider_total || 0);
      const success = Number(status?.provider_success || 0);
      const failed = Number(status?.provider_failed || 0);
      const skipped = Number(status?.provider_skipped || 0);
      return `Providers: ${{success}} ok / ${{failed}} failed / ${{skipped}} skipped / ${{total}} total`;
    }}

    function reviewFrontierText(status) {{
      const reviewTargets = Number(status?.review_target_count || 0);
      const validationTargets = Number(status?.validation_target_count || 0);
      const childBundles = Number(status?.child_bundle_count || 0);
      return `Review: ${{reviewTargets}} targets / ${{validationTargets}} validation / ${{childBundles}} child bundles`;
    }}

    function renderPipelineStatus(status) {{
      if (!status || typeof status !== "object") return;
      const panel = document.getElementById("pipeline-progress-panel");
      if (!panel) return;
      const runId = String(status.pipeline_run_id || "");
      const evidenceSha = String(status.evidence_sha256 || panel.dataset.evidenceSha256 || "");
      const runStatus = String(status.status || "not_started");
      const progress = Math.max(0, Math.min(100, Math.round(Number(status.progress_percent) || 0)));
      panel.dataset.pipelineRunId = runId;
      panel.dataset.evidenceSha256 = evidenceSha;
      const operation = panel.querySelector("[data-pipeline-operation]");
      const current = panel.querySelector("[data-pipeline-current]");
      const statusBox = panel.querySelector(".pipeline-status");
      const statusText = panel.querySelector("[data-pipeline-status-text]");
      const progressText = panel.querySelector("[data-pipeline-progress-text]");
      const progressBar = panel.querySelector("[data-pipeline-progress-bar]");
      const meter = panel.querySelector(".pipeline-meter");
      const blockingReason = panel.querySelector("[data-pipeline-blocking-reason]");
      const currentState = panel.querySelector("[data-pipeline-current-state]");
      const stateTimeline = panel.querySelector("[data-pipeline-state-timeline]");
      const reasonCodes = panel.querySelector("[data-pipeline-reason-codes]");
      const providerFrontier = panel.querySelector("[data-pipeline-provider-frontier]");
      const reviewFrontier = panel.querySelector("[data-pipeline-review-frontier]");
      const stepsNode = panel.querySelector("[data-pipeline-steps]");
      const eventsNode = panel.querySelector("[data-pipeline-events]");
      if (operation) operation.textContent = status.operation || "No run recorded";
      if (current) current.textContent = status.current_step_label || status.current_step || "No step recorded";
      if (statusBox) statusBox.dataset.pipelineStatus = pipelineStatusClass(runStatus);
      if (statusText) statusText.textContent = runStatus;
      if (progressText) progressText.textContent = `${{progress}}%`;
      if (progressBar) progressBar.style.width = `${{progress}}%`;
      if (meter) meter.setAttribute("aria-valuenow", String(progress));
      if (blockingReason) {{
        const reason = String(status.blocking_reason || "");
        blockingReason.textContent = reason ? `Blocking reason: ${{reason}}` : "";
        blockingReason.hidden = !reason;
      }}
      if (currentState) {{
        const canonicalState = String(status.canonical_state || "");
        currentState.textContent = canonicalState ? `State: ${{canonicalState}}` : "State: not_started";
      }}
      if (stateTimeline) {{
        const html = pipelineStateTimelineHtml(status);
        stateTimeline.innerHTML = html;
        stateTimeline.hidden = !html;
      }}
      if (reasonCodes) {{
        const html = pipelineReasonCodesHtml(status);
        reasonCodes.innerHTML = html;
        reasonCodes.hidden = !html;
      }}
      if (providerFrontier) providerFrontier.textContent = providerFrontierText(status);
      if (reviewFrontier) reviewFrontier.textContent = reviewFrontierText(status);
      const steps = Array.isArray(status.steps) ? status.steps : [];
      if (stepsNode) {{
        stepsNode.innerHTML = steps.length
          ? steps.map(pipelineStepHtml).join("")
          : "<li data-step-status='pending'><span class='pipeline-step-state'>not_started</span><strong>No pipeline run recorded yet</strong><small>Run upload, analysis, plan generation, review, or more-data refresh to create progress history.</small></li>";
      }}
      const events = Array.isArray(status.events) ? status.events.slice(-6) : [];
      if (eventsNode) {{
        eventsNode.innerHTML = events.length
          ? events.map(pipelineEventHtml).join("")
          : "<li><span>not_started</span><small>No server-side progress events recorded.</small></li>";
      }}
    }}

    async function refreshPipelineStatus() {{
      const panel = document.getElementById("pipeline-progress-panel");
      if (!panel) return;
      const params = new URLSearchParams();
      const runId = String(panel.dataset.pipelineRunId || "");
      const evidenceSha = String(panel.dataset.evidenceSha256 || "");
      if (runId) params.set("pipeline_run_id", runId);
      else if (evidenceSha) params.set("evidence_sha256", evidenceSha);
      else return;
      try {{
        const response = await nativeFetch(`/pipeline-status?${{params.toString()}}`);
        if (!response.ok) return;
        renderPipelineStatus(await response.json());
      }} catch (error) {{}}
    }}

    function setupPipelineStatusPolling() {{
      const panel = document.getElementById("pipeline-progress-panel");
      if (!panel) return;
      refreshPipelineStatus();
      window.setInterval(refreshPipelineStatus, 5000);
    }}

    function uploadStatus(html) {{
      const node = document.getElementById("artifact-upload-status");
      if (node) node.innerHTML = html;
    }}

    async function uploadEvidenceBundleFile(file) {{
      if (!file) return;
      uploadStatus(`<strong>Reading ${{esc(file.name)}}...</strong>`);
      let payload;
      try {{
        payload = JSON.parse(await file.text());
      }} catch (error) {{
        uploadStatus(`<strong style="color: var(--danger);">Invalid JSON.</strong><p class="score-note">Choose a sanitized evidence_bundle.json file.</p>`);
        return;
      }}
      if (payload?.schema_version !== "evidence_bundle.v1" || payload?.bundle_type !== "sanitized_evidence_bundle") {{
        uploadStatus(`<strong style="color: var(--danger);">Unsupported artifact.</strong><p class="score-note">This uploader accepts sanitized Evidence Bundles only. Raw logs are not accepted.</p>`);
        return;
      }}
      uploadStatus(`<strong>Uploading sanitized Evidence Bundle...</strong><p class="score-note">Server-side validation is running. Raw logs are not uploaded.</p>`);
      const response = await fetch("/bundles/upload", {{
        method: "POST",
        headers: {{"Content-Type": "application/json"}},
        body: JSON.stringify({{bundle: payload}}),
      }});
      const result = await response.json().catch(() => ({{}}));
      if (!response.ok) {{
        uploadStatus(`<strong style="color: var(--danger);">Upload rejected.</strong><pre>${{pretty(result)}}</pre>`);
        return;
      }}
      renderPipelineStatus(result.pipeline_status);
      const url = result.review_graph_url || `/?evidence_sha256=${{encodeURIComponent(result.evidence_sha256 || "")}}`;
      uploadStatus(`
        <strong style="color: var(--ok);">Upload accepted. Server-side validation passed.</strong>
        <p>Evidence SHA256: <code>${{esc(result.evidence_sha256 || "")}}</code></p>
        <p>Raw log policy: <code>${{esc(result.raw_log_policy || "")}}</code></p>
        <p><a href="${{esc(url)}}">Open Review Graph</a></p>
      `);
    }}

    function setupArtifactUploader() {{
      const zone = document.getElementById("artifact-drop-zone");
      const input = document.getElementById("artifact-file-input");
      if (!zone || !input) return;
      zone.addEventListener("click", () => input.click());
      input.addEventListener("change", () => uploadEvidenceBundleFile(input.files?.[0]));
      for (const eventName of ["dragenter", "dragover"]) {{
        zone.addEventListener(eventName, (event) => {{
          event.preventDefault();
          zone.classList.add("dragover");
        }});
      }}
      for (const eventName of ["dragleave", "drop"]) {{
        zone.addEventListener(eventName, (event) => {{
          event.preventDefault();
          zone.classList.remove("dragover");
        }});
      }}
      zone.addEventListener("drop", (event) => uploadEvidenceBundleFile(event.dataTransfer?.files?.[0]));
    }}

    function evidenceSummary(target, drawer) {{
      const profileId = target.profile?.profile_id || drawer.synthesis?.profile?.profile_id || "generic";
      const coreType = target.core_target_type || target.review_target_type || "";
      const support = Array.isArray(drawer.support_evidence) ? drawer.support_evidence : [];
      const lines = [];
      if (profileId === "amazon_notify" && coreType === "job_configuration_mismatch") {{
        lines.push("Missing configured command target detected");
        lines.push("Service start failure observed");
      }} else if (profileId === "amazon_notify" && coreType === "service_start_failure") {{
        lines.push("Service start failure observed");
        lines.push("Configured command path needs verification");
      }} else if (support.length) {{
        lines.push(...support.slice(0, 2).map((item) => item.summary || item.evidence_id || "Support evidence recorded"));
      }} else {{
        lines.push(target.core_claim || "Evidence summary is recorded in the support evidence details");
      }}
      lines.push(`Profile: ${{profileId}}`);
      lines.push(`Type: ${{coreType || "unknown"}}`);
      return `<div class="evidence-summary"><h3>Evidence Summary</h3><ul>${{lines.map((line) => `<li>${{esc(line)}}</li>`).join("")}}</ul></div>`;
    }}

    function evidenceList(items) {{
      const rows = Array.isArray(items) ? items : [];
      const lines = rows.slice(0, 5).map((item) => item.summary || item.evidence_id || JSON.stringify(item));
      if (rows.length > 5) lines.push(`${{rows.length - 5}} additional evidence records are available in raw JSON`);
      return list(lines);
    }}

    function systemContextSummary(context) {{
      const critical = Array.isArray(context.critical_outcomes) ? context.critical_outcomes : [];
      return `
        <div class="grid">
          <div class="field"><label>System</label><p>${{esc(context.system_name || context.profile?.source_system || "")}}</p></div>
          <div class="field"><label>Type</label><p>${{esc(context.system_type || "")}}</p></div>
          <div class="field full"><label>Purpose</label><p>${{esc(context.purpose || "")}}</p></div>
          <div class="field full"><label>Critical outcomes</label>${{list(critical)}}</div>
        </div>
      `;
    }}

    function resolutionPanel(status) {{
      const resolution = status || {{}};
      if (!resolution.status && !resolution.fix && !(resolution.verification || []).length) return "";
      const verification = Array.isArray(resolution.verification) ? resolution.verification : [];
      return `
        <div class="drawer-section">
          <h3>Resolution / Verification</h3>
          <div class="resolution-box">
            <div><label>Status</label><p><strong>${{esc(resolution.status || "")}}</strong></p></div>
            <div><label>Fix</label><p>${{esc(resolution.fix || "")}}</p></div>
            <div><label>Verification</label><ul class="verification-list">${{verification.map((item) => `<li>${{esc(item)}}</li>`).join("")}}</ul></div>
          </div>
        </div>
      `;
    }}

    function statusTable(statuses) {{
      const rows = Array.isArray(statuses) ? statuses : [];
      if (!rows.length) return "<p class='score-note'>No request status records.</p>";
      return `<table style="width:100%; border-collapse: collapse; font-size: 12px;">
        <thead><tr><th align="left">Request</th><th align="left">Profile request</th><th align="left">Status</th><th align="right">Rows</th></tr></thead>
        <tbody>${{rows.map((row) => `
          <tr>
            <td><code>${{esc(row.request_id || "")}}</code></td>
            <td><code>${{esc(row.profile_request_id || "")}}</code></td>
            <td>${{esc(row.status || "")}}</td>
            <td align="right">${{esc(row.rows ?? 0)}}</td>
          </tr>`).join("")}}</tbody>
      </table>`;
    }}

    function analysisTable(items) {{
      const rows = Array.isArray(items) ? items : [];
      if (!rows.length) return "<p class='score-note'>No request analysis records.</p>";
      return `<table style="width:100%; border-collapse: collapse; font-size: 12px;">
        <thead><tr><th align="left">Request</th><th align="left">Type</th><th align="left">Summary</th><th align="right">Rows</th></tr></thead>
        <tbody>${{rows.map((row) => `
          <tr>
            <td><code>${{esc(row.request_id || "")}}</code></td>
            <td>${{esc(row.request_type || "")}}</td>
            <td>${{esc(row.summary || "")}}</td>
            <td align="right">${{esc(row.row_count ?? 0)}}</td>
          </tr>`).join("")}}</tbody>
      </table>`;
    }}

    function requestButtons(requests, id) {{
      const rows = Array.isArray(requests) ? requests : [];
      if (!rows.length) return "<p class='score-note'>No profile-linked evidence requests.</p>";
        return `<div class="actions request-actions">${{rows.map((row) => {{
        const requestId = String(row.request_id || "");
        const label = row.profile_request_id ? `${{requestId}} / ${{row.profile_request_id}}` : requestId;
        return `<button onclick="runMoreDataRequest('${{esc(id)}}', '${{esc(requestId)}}', false)">${{esc(label)}}</button>
                <button onclick="runMoreDataRequest('${{esc(id)}}', '${{esc(requestId)}}', true)">+ Gemini</button>
                <button onclick="remoteCollectRequest('${{esc(id)}}', '${{esc(requestId)}}', false)">Host collect</button>
                <button onclick="remoteCollectRequest('${{esc(id)}}', '${{esc(requestId)}}', true)">Host collect + Gemini</button>`;
      }}).join("")}}</div>`;
    }}

    function refreshSummaryHtml(decision, refreshResult) {{
      const summary = refreshResult.refresh_summary || {{}};
      const chain = refreshResult.child_evidence_chain || {{}};
      const model = refreshResult.pipeline_result || null;
      return `
        <h3>More data result</h3>
        <div class="field full"><label>Status transition</label><p>${{esc(summary.review_target_status_transition || "needs_more_data -> evidence_requested")}}</p></div>
        <div class="field full"><label>Child Evidence Bundle</label><pre>${{pretty(chain)}}</pre></div>
        <div class="grid">
          <div class="field"><label>Preview rows</label><p>${{esc(summary.added_preview_rows ?? refreshResult.more_data_preview_count ?? 0)}}</p></div>
          <div class="field"><label>New evidence types</label><p>${{esc((summary.new_evidence_types || []).join(", ") || "none")}}</p></div>
          <div class="field"><label>Request analysis</label><p>${{esc(summary.request_analysis_count ?? 0)}}</p></div>
          <div class="field"><label>Artifact comparisons</label><p>${{esc(summary.artifact_comparison_count ?? 0)}}</p></div>
          <div class="field"><label>Model rerun</label><p>${{esc(model ? "completed" : "not run")}}</p></div>
          <div class="field"><label>Generated bundle</label><p><code>${{esc(refreshResult.evidence_sha256 || "")}}</code></p></div>
        </div>
        <div class="drawer-section"><h3>Evidence request status</h3>${{statusTable(refreshResult.request_statuses || [])}}</div>
        <div class="drawer-section"><h3>Request analysis</h3>${{analysisTable(refreshResult.generated_query?.request_analysis || [])}}</div>
        <div class="drawer-section"><h3>Decision record</h3><pre>${{pretty(decision)}}</pre></div>
      `;
    }}

    function showTarget(id) {{
      const target = targets.get(id);
      if (!target) return;
      const drawer = target.drawer || {{}};
      const agreement = target.model_agreement || {{}};
      const providers = agreement.providers || [];
      const moreDataAction = (target.actions || {{}}).more_data || {{}};
      const nextEvidenceRequests = drawer.next_evidence_requests || moreDataAction.next_evidence_requests || [];
      const nextCliCommand = drawer.next_cli_command || moreDataAction.next_cli_command || "";
      const systemContext = drawer.system_context || {{}};
      const resolutionStatus = target.resolution_status || drawer.resolution_status || {{}};
      document.getElementById("drawer").innerHTML = `
        <h2>${{esc(target.title)}}</h2>
        <div class="pill-row">
          <span class="pill">${{esc(target.subsystem)}}</span>
          <span class="pill">Profile: ${{esc(target.profile?.profile_id || "generic")}}</span>
          <span class="pill">Type: ${{esc(target.core_target_type || target.review_target_type || "")}}</span>
          <span class="pill">${{esc(target.cluster_id)}}</span>
          <span class="pill">${{esc(target.status || "pending")}}</span>
        </div>
        <div class="drawer-section">
          <h3>Review decision</h3>
          <div class="decision">
            <select id="decision-reason">
              <option value="confirmed_candidate">Confirmed candidate</option>
              <option value="known_issue">Known issue</option>
              <option value="watchlist">Watchlist</option>
              <option value="false_positive">False positive</option>
              <option value="low_value">Low value</option>
              <option value="duplicate">Duplicate</option>
              <option value="not_actionable">Not actionable</option>
            </select>
            <input id="reviewer" value="api-user" aria-label="reviewer">
            <textarea id="human-note" placeholder="Review note"></textarea>
          </div>
          <div class="actions">
            <button class="primary" onclick="reviewTarget('${{esc(id)}}', 'accept')">Accept</button>
            <button class="danger" onclick="reviewTarget('${{esc(id)}}', 'reject')">Reject</button>
            <button onclick="reviewTarget('${{esc(id)}}', 'needs-more-data', false)">More data</button>
            <button onclick="reviewTarget('${{esc(id)}}', 'needs-more-data', true)">More data + Gemini</button>
          </div>
        </div>
        <div class="drawer-section">${{evidenceSummary(target, drawer)}}</div>
        ${{resolutionPanel(resolutionStatus)}}
        <div class="drawer-section"><h3>Evidence ID</h3><p><code>${{esc(drawer.evidence_sha256 || "")}}</code></p></div>
        <div class="drawer-section"><h3>System Context</h3>${{systemContextSummary(systemContext)}}${{rawDetails("Show raw JSON", systemContext)}}</div>
        <div class="drawer-section"><h3>Profile mapping</h3><p class="score-note">Profile: ${{esc(target.profile?.profile_id || drawer.synthesis?.profile?.profile_id || "generic")}} / Type: ${{esc(target.core_target_type || drawer.synthesis?.core_target_type || "")}}</p>${{rawDetails("Show raw JSON", {{ profile: target.profile || drawer.synthesis?.profile || null, core_target_type: target.core_target_type || drawer.synthesis?.core_target_type || null, domain_label: target.domain_label || drawer.synthesis?.domain_label || null }})}}</div>
        <div class="drawer-section"><h3>Time window</h3><p class="score-note">${{esc(drawer.incident_window?.window_start || drawer.time_window?.window_start || "")}} -> ${{esc(drawer.incident_window?.window_end || drawer.time_window?.window_end || "")}}</p>${{rawDetails("Show raw JSON", {{ incident_window: drawer.incident_window, baseline_window: drawer.baseline_window, time_window: drawer.time_window }})}}</div>
        <div class="drawer-section"><h3>Support evidence</h3>${{evidenceList(drawer.support_evidence || [])}}${{rawDetails("Show raw JSON", drawer.support_evidence || [])}}</div>
        <div class="drawer-section"><h3>Counter evidence</h3>${{evidenceList(drawer.counter_evidence || [])}}${{rawDetails("Show raw JSON", drawer.counter_evidence || [])}}</div>
        <div class="drawer-section"><h3>Caveats</h3>${{list(drawer.caveats || [])}}</div>
        <div class="drawer-section"><h3>Missing evidence</h3>${{list(drawer.missing_evidence || [])}}</div>
        <div class="drawer-section"><h3>Finding status</h3>${{rawDetails("Show raw JSON", {{ finding_status_counts: target.finding_status_counts || drawer.finding_status_counts || {{}}, identity_unknown_keys: target.identity_unknown_keys || drawer.identity_unknown_keys || [], insufficient_evidence: drawer.insufficient_evidence || [] }})}}</div>
        <div class="drawer-section">
          <h3>More data requests</h3>
          <div class="decision"><input id="collector-host" value="localhost" aria-label="collector host" placeholder="Collector host"></div>
          ${{requestButtons(nextEvidenceRequests, id)}}
        </div>
        <div class="drawer-section"><h3>Generated More data requests</h3><p class="score-note">${{esc(nextEvidenceRequests.length)}} requests recorded.</p>${{rawDetails("Show raw JSON", {{ next_evidence_requests: nextEvidenceRequests, next_cli_command: nextCliCommand, next_query_available: moreDataAction.next_query_available || false }})}}</div>
        <div class="drawer-section"><h3>Model agreement</h3><p>Outputs from multiple models are merged into the same review target; disagreements remain validation targets.</p><p class="score-note">${{esc(providers.length)}} provider outputs recorded.</p>${{rawDetails("Show raw JSON", providers)}}</div>
        <div class="drawer-section"><h3>Disagreements</h3>${{list(agreement.disagreement || [])}}</div>
        <div class="drawer-section"><h3>Review history</h3>${{rawDetails("Show raw JSON", {{ latest_review: target.latest_review || null, review_history: target.score_breakdown?.raw_scoring?.review_history || null }})}}</div>
        <div class="drawer-section"><h3>Related review targets</h3>${{rawDetails("Show raw JSON", {{ parent_review_target_id: target.parent_review_target_id || null, relationship: target.relationship || null, related_review_targets: target.related_review_targets || drawer.synthesis?.related_review_targets || [] }})}}</div>
        <div class="drawer-section" id="more-data-result"><h3>More data result</h3><p class="score-note">Click More data or a request button to save the decision and generate a child Evidence Bundle.</p></div>
        <div class="drawer-section"><h3>Source proposition IDs</h3>${{list(drawer.raw_proposition_ids || target.raw_proposition_ids || [])}}</div>
        <div class="drawer-section"><h3>Model outputs</h3>${{rawDetails("Show raw JSON", drawer.model_outputs || [])}}</div>
        <div class="drawer-section"><h3>Parsed JSON</h3>${{rawDetails("Show raw JSON", drawer.parsed_json || [])}}</div>
        <div class="drawer-section"><h3>Zero-value semantics</h3>${{rawDetails("Show raw JSON", drawer.zero_semantics || {{}})}}</div>
      `;
    }}

    function runMoreDataRequest(id, requestId, runModels = false) {{
      return reviewTarget(id, "needs-more-data", runModels, requestId ? [requestId] : []);
    }}

    async function remoteCollectRequest(id, requestId, runModels = false) {{
      const host = document.getElementById("collector-host")?.value || "localhost";
      const response = await fetch(`/review-targets/${{encodeURIComponent(id)}}/remote-collect`, {{
        method: "POST",
        headers: {{"Content-Type": "application/json"}},
        body: JSON.stringify({{host, run_models: Boolean(runModels), providers: ["gemini"], request_ids: requestId ? [requestId] : []}}),
      }});
      const result = await response.json().catch(() => ({{}}));
      const resultNode = document.getElementById("more-data-result");
      if (resultNode) {{
        if (!response.ok) {{
          resultNode.innerHTML = `<h3>Remote collect result</h3><pre>${{pretty(result)}}</pre>`;
        }} else {{
          renderPipelineStatus(result.pipeline_status);
          resultNode.innerHTML = refreshSummaryHtml({{decision: "remote_collect", request_id: requestId}}, result);
        }}
      }}
    }}

    async function reviewTarget(id, action, runModels = false, requestIds = []) {{
      const reason = document.getElementById("decision-reason")?.value || "";
      const reviewer = document.getElementById("reviewer")?.value || "api-user";
      const human_note = document.getElementById("human-note")?.value || "";
      const endpoint = action === "needs-more-data" ? "needs-more-data" : action;
      const selectedRequestIds = Array.isArray(requestIds) ? requestIds.filter(Boolean) : [];
      const body = {{
        reason,
        reviewer,
        human_note,
      }};
      if (selectedRequestIds.length) {{
        body.request_ids = selectedRequestIds;
      }}
      if (action === "accept" && !["confirmed_candidate", "known_issue", "watchlist"].includes(body.reason)) {{
        body.reason = "confirmed_candidate";
      }}
      if (action === "reject" && !["false_positive", "low_value", "duplicate", "not_actionable"].includes(body.reason)) {{
        body.reason = "false_positive";
      }}
      const response = await fetch(`/review-targets/${{encodeURIComponent(id)}}/${{endpoint}}`, {{
        method: "POST",
        headers: {{"Content-Type": "application/json"}},
        body: JSON.stringify(body),
      }});
      const result = await response.json().catch(() => ({{}}));
      if (action === "needs-more-data") {{
        const refresh = await fetch(`/review-targets/${{encodeURIComponent(id)}}/more-data-refresh`, {{
          method: "POST",
          headers: {{"Content-Type": "application/json"}},
          body: JSON.stringify({{run_models: Boolean(runModels), providers: ["gemini"], request_ids: selectedRequestIds}}),
        }});
        const refreshResult = await refresh.json().catch(() => ({{}}));
        renderPipelineStatus(refreshResult.pipeline_status);
        const resultNode = document.getElementById("more-data-result");
        if (resultNode) {{
          resultNode.innerHTML = refreshSummaryHtml(result, refreshResult);
        }}
        return;
      }}
      location.reload();
    }}

    function plannerJson(id) {{
      const node = document.getElementById(id);
      if (!node) return null;
      try {{
        return JSON.parse(node.textContent || "null");
      }} catch (error) {{
        return null;
      }}
    }}

    function plannerLines(value) {{
      return String(value || "").split(/\\r?\\n/).map((item) => item.trim()).filter(Boolean);
    }}

    function collectPlannerAnswers() {{
      const plan = plannerJson("planner-plan-json") || {{}};
      const answers = {{}};
      document.querySelectorAll("[data-planner-question]").forEach((node) => {{
        const key = node.getAttribute("data-answer-key") || "";
        const type = node.getAttribute("data-input-type") || "";
        if (!key) return;
        if (type === "datetime_range") {{
          answers[key] = {{
            start: node.querySelector("[data-planner-field='start']")?.value || "",
            end: node.querySelector("[data-planner-field='end']")?.value || "",
            timezone: node.querySelector("[data-planner-field='timezone']")?.value || "UTC",
          }};
        }} else if (type === "multi_select") {{
          answers[key] = Array.from(node.querySelectorAll("input[type='checkbox'][data-planner-value]:checked")).map((input) => input.value);
        }} else if (type === "boolean") {{
          answers[key] = Boolean(node.querySelector("input[type='checkbox'][data-planner-key]")?.checked);
        }} else if (type === "integer") {{
          const raw = node.querySelector("input[type='number'][data-planner-key]")?.value || "0";
          answers[key] = Number.parseInt(raw, 10) || 0;
        }} else if (type === "path_list") {{
          answers[key] = plannerLines(node.querySelector("textarea[data-planner-key]")?.value || "");
        }} else if (type === "component_map_select") {{
          const value = {{}};
          node.querySelectorAll("select[data-planner-component]").forEach((select) => {{
            const component = select.getAttribute("data-planner-component") || "";
            if (component) value[component] = select.value || "unknown";
          }});
          answers[key] = value;
        }} else if (type === "json") {{
          try {{
            answers[key] = JSON.parse(node.querySelector("textarea[data-planner-key]")?.value || "{{}}");
          }} catch (error) {{
            answers[key] = {{}};
          }}
        }} else {{
          answers[key] = node.querySelector("[data-planner-key]")?.value || "";
        }}
      }});
      const payload = {{
        schema_version: "planner_answers.v1",
        plan_id: plan.plan_id || "PLAN-UI",
        answered_by: "api-user",
        answered_at: new Date().toISOString(),
        answers,
      }};
      const preview = document.getElementById("planner-answers-json");
      if (preview) preview.textContent = JSON.stringify(payload, null, 2);
      return payload;
    }}

    function setPlannerProgress(percent, label) {{
      const value = Math.max(0, Math.min(100, Number(percent) || 0));
      const bar = document.getElementById("planner-refine-progress-bar");
      const step = document.getElementById("planner-refine-progress-step");
      const valueNode = document.getElementById("planner-refine-progress-value");
      const progress = document.getElementById("planner-refine-progress");
      if (bar) bar.style.width = `${{value}}%`;
      if (step) step.textContent = label || "";
      if (valueNode) valueNode.textContent = `${{Math.round(value)}}%`;
      if (progress) {{
        progress.setAttribute("aria-valuenow", String(Math.round(value)));
        progress.setAttribute("aria-label", label || "Planner progress");
      }}
    }}

	    function setPlannerRefineBusy(isBusy) {{
	      const button = document.getElementById("planner-refine-button");
	      if (!button) return;
	      button.disabled = Boolean(isBusy);
	      button.textContent = isBusy ? "Generating..." : "Generate refined plan";
	      if (!isBusy) updatePlannerGenerateEnabled();
	    }}

    function plannerUiTick() {{
      return new Promise((resolve) => window.setTimeout(resolve, 0));
    }}

	    function revealPlannerCollectionInstructions() {{
	      const markdownNode = document.getElementById("planner-collection-markdown");
	      const stamp = document.getElementById("planner-output-stamp");
	      if (!markdownNode) return;
      markdownNode.classList.add("planner-result-highlight");
      if (stamp) stamp.classList.add("planner-result-highlight");
      markdownNode.setAttribute("tabindex", "-1");
      const reduceMotion = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
      (stamp || markdownNode).scrollIntoView({{behavior: reduceMotion ? "auto" : "smooth", block: "start"}});
      try {{
        markdownNode.focus({{preventScroll: true}});
      }} catch (error) {{
        markdownNode.focus();
      }}
	      window.setTimeout(() => {{
	        markdownNode.classList.remove("planner-result-highlight");
	        if (stamp) stamp.classList.remove("planner-result-highlight");
	      }}, 3200);
	    }}

	    function revealPlannerTechnicalJson() {{
	      const details = document.getElementById("planner-technical-json");
	      if (!details) return;
	      details.open = true;
	      details.scrollIntoView({{behavior: "smooth", block: "start"}});
	    }}

    async function generateRefinedPlan() {{
      openPlannerPanel();
      const bundle = plannerJson("planner-bundle-json");
      const profile = plannerJson("planner-approved-profile-json");
      const sourceAnalysis = plannerJson("planner-source-analysis-json");
      const canonicalReviewGraph = plannerJson("planner-canonical-review-graph-json");
      const status = document.getElementById("planner-refine-status");
      const planNode = document.getElementById("planner-refined-plan-json");
      const markdownNode = document.getElementById("planner-collection-markdown");
      const previousMarkdown = markdownNode ? (markdownNode.textContent || "") : "";
	      if (!bundle || !profile) {{
	        if (status) status.textContent = "Planner input is missing.";
	        setPlannerProgress(0, "Planner input is missing");
	        return;
	      }}
	      const writeToken = requirePlannerWriteToken();
		      if (!writeToken) {{
		        if (status) status.textContent = "Write token is required before generating a refined plan.";
		        setPlannerProgress(0, "Write token required");
		        setPlannerResult("Enter a write token above to enable Generate refined plan.", "error");
		        return;
		      }}
		      setPlannerRefineBusy(true);
		      setPlannerProgress(8, "Reading form answers");
	      if (status) status.textContent = "Reading form answers...";
	      setPlannerResult("Generating refined plan. Output will appear below.", "idle");
	      await plannerUiTick();
      const slowTimer = window.setTimeout(() => {{
        setPlannerProgress(78, "Waiting for API response");
        if (status) status.textContent = "Still generating. Waiting for the API response...";
      }}, 7000);
      try {{
        const planner_answers = collectPlannerAnswers();
        setPlannerProgress(22, "Preparing request");
        if (status) status.textContent = "Preparing request...";
        await plannerUiTick();
        const requestPayload = {{
          evidence_bundle: bundle,
          approved_profile: profile,
          planner_answers,
          generate_evidence_requirements_with_ai: true,
        }};
        if (sourceAnalysis && Object.keys(sourceAnalysis).length) requestPayload.source_analysis = sourceAnalysis;
        if (canonicalReviewGraph && Object.keys(canonicalReviewGraph).length) requestPayload.canonical_review_graph = canonicalReviewGraph;
        setPlannerProgress(42, "Sending request");
        if (status) status.textContent = "Sending request...";
        await plannerUiTick();
        const response = await fetch("/evidence-requests/plan", {{
          method: "POST",
          headers: {{"Content-Type": "application/json"}},
          body: JSON.stringify(requestPayload),
        }});
        setPlannerProgress(84, "Processing response");
        if (status) status.textContent = "Processing response...";
        const result = await response.json().catch(() => ({{}}));
        if (!response.ok) {{
          const detail = typeof result.detail === "string" ? result.detail : (result.detail?.message || result.message || "");
	          setPlannerProgress(100, "Failed");
	          if (status) status.textContent = detail ? `Refine failed: ${{detail}}` : "Refine failed.";
	          setPlannerResult(detail ? `Refine failed: ${{detail}}` : "Refine failed.", "error");
	          if (planNode) planNode.textContent = JSON.stringify(result, null, 2);
	          return;
	        }}
	        renderPipelineStatus(result.pipeline_status);
	        const collectionMarkdown = result.collection_instructions_markdown || "";
	        const outputChanged = collectionMarkdown !== previousMarkdown;
	        const generatedAt = new Date().toLocaleString();
	        const plan = result.plan || {{}};
	        const requestCount = Array.isArray(plan.requests) ? plan.requests.length : 0;
	        const questionCount = Array.isArray(plan.human_questions) ? plan.human_questions.length : 0;
	        const fieldCount = plannerSubmittedFieldCount(planner_answers);
	        if (planNode) planNode.textContent = JSON.stringify(plan, null, 2);
	        if (markdownNode) {{
	          markdownNode.textContent = collectionMarkdown;
	          markdownNode.dataset.generatedAt = generatedAt;
	          markdownNode.dataset.outputChanged = String(outputChanged);
	        }}
	        setPlannerProgress(100, "Complete");
	        if (status) status.textContent = outputChanged
	          ? "Refined plan generated. Collection Instructions changed below."
	          : "Refined plan generated. Collection Instructions were already current.";
	        const changeMessage = outputChanged
	          ? "Collection Instructions changed."
	          : "No Collection Instructions text changed for the current answers.";
	        setPlannerResult(
	          `Refined plan generated at ${{generatedAt}}. ${{changeMessage}} API returned ${{requestCount}} requests and ${{questionCount}} questions from ${{fieldCount}} submitted form fields.`,
	          "success"
	        );
	        setPlannerOutputStamp(
	          `${{outputChanged ? "Updated" : "Generated with no text changes"}} at ${{generatedAt}} from the current human-question values. ${{requestCount}} requests returned.`,
	          outputChanged ? "changed" : "unchanged"
	        );
	        revealPlannerCollectionInstructions();
	      }} catch (error) {{
	        setPlannerProgress(100, "Failed");
	        if (status) status.textContent = "Refine failed: request could not be completed.";
	        setPlannerResult("Refine failed: request could not be completed.", "error");
	      }} finally {{
        window.clearTimeout(slowTimer);
        setPlannerRefineBusy(false);
      }}
    }}

    async function copyElementText(elementId, statusId) {{
      const node = document.getElementById(elementId);
      const status = document.getElementById(statusId);
      const text = node ? (node.textContent || "") : "";
      if (!text.trim()) {{
        if (status) status.textContent = "Nothing to copy.";
        return;
      }}
      try {{
        if (navigator.clipboard && navigator.clipboard.writeText) {{
          await navigator.clipboard.writeText(text);
        }} else {{
          const textarea = document.createElement("textarea");
          textarea.value = text;
          textarea.setAttribute("readonly", "readonly");
          textarea.style.position = "fixed";
          textarea.style.left = "-9999px";
          document.body.appendChild(textarea);
          textarea.select();
          document.execCommand("copy");
          document.body.removeChild(textarea);
        }}
        if (status) status.textContent = "Copied.";
      }} catch (error) {{
        if (status) status.textContent = "Copy failed.";
      }}
    }}

    const plannerPanel = document.getElementById("evidence-request-planner");
    if (plannerPanel) {{
      const tokenInput = plannerWriteTokenInput();
      if (tokenInput) {{
        tokenInput.addEventListener("input", () => {{
          tokenInput.classList.remove("token-missing");
          if (String(tokenInput.value || "").trim()) {{
            setPlannerTokenStatus("Token entered. Generate refined plan is enabled.");
          }} else if (storedWriteToken().trim()) {{
            setPlannerTokenStatus("Saved token available. Generate refined plan is enabled.");
          }} else {{
            setPlannerTokenStatus("Enter write token to enable Generate refined plan.");
          }}
          updatePlannerGenerateEnabled();
        }});
      }}
      if (storedWriteToken().trim()) {{
        setPlannerTokenStatus("Saved token available. Generate refined plan is enabled.");
      }} else {{
        setPlannerTokenStatus("Enter write token to enable Generate refined plan.");
      }}
      collectPlannerAnswers();
      updatePlannerGenerateEnabled();
    }}
    setupArtifactUploader();
    setupPipelineStatusPolling();
    if ((targetSet.targets || []).length) showTarget(targetSet.targets[0].review_target_id);
  </script>
</body>
</html>"""


@app.post("/logs/jsonl")
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


@app.post("/incidents")
def run_incident(payload: dict[str, Any]) -> dict[str, Any]:
    incident = _incident_from_payload(payload)
    result = run_pipeline(_store(), incident)
    return asdict(result)


@app.post("/bundles")
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


@app.post("/bundles/upload")
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


@app.post("/profile-discovery/upload")
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
    draft = build_profile_draft(bundle)
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


@app.post("/source-context/upload")
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


@app.post("/source-analysis/upload")
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


@app.post("/profile-drafts/approve")
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


@app.post("/evidence-requests/plan")
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


@app.get("/review/graph")
def get_review_graph_api(evidence_sha256: str, recompute: bool = False) -> dict[str, Any]:
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


@app.post("/review/graph/refresh")
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


@app.post("/review/arbitrate")
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


@app.post("/ai/multi-run")
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


@app.post("/bundle/create")
def create_bundle_worker(payload: dict[str, Any]) -> dict[str, Any]:
    return create_bundle(payload)


@app.post("/run/local-agents")
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


@app.post("/run/gemini")
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


@app.post("/run/claude")
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


@app.post("/run/gpt-oss")
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


@app.post("/run/mistral")
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


@app.post("/run/alternatives")
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


@app.post("/run/external")
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


@app.post("/validate")
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


@app.post("/route")
def route_stage(payload: dict[str, Any]) -> dict[str, Any]:
    bundle = _bundle_from_payload(payload)
    parsed_results = _store().fetch_parsed_results(bundle["evidence_sha256"])
    routing = run_route_stage(_store(), bundle, parsed_results)
    return {
        "evidence_sha256": bundle["evidence_sha256"],
        "claim_count": len(routing.claims),
        "proposition_count": len(routing.propositions),
    }


@app.post("/claim-router")
def claim_router_stage(payload: dict[str, Any]) -> dict[str, Any]:
    return route_stage(payload)


@app.post("/score")
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


@app.post("/clusters/build")
def build_clusters_stage(payload: dict[str, Any]) -> dict[str, Any]:
    store = _store()
    bundle = _bundle_from_payload(payload)
    clusters = persist_proposition_clusters(store, bundle["evidence_sha256"])
    return {
        "evidence_sha256": bundle["evidence_sha256"],
        "cluster_count": len(clusters),
    }


@app.get("/clusters")
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


@app.post("/compare")
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


@app.get("/bundles/{evidence_sha256}")
def get_bundle(evidence_sha256: str) -> dict[str, Any]:
    bundle = _store().get_bundle(evidence_sha256)
    if bundle is None:
        raise HTTPException(status_code=404, detail="bundle not found")
    return bundle


@app.get("/pipeline-status")
def get_pipeline_status_api(
    evidence_sha256: str | None = None,
    pipeline_run_id: str | None = None,
) -> dict[str, Any]:
    evidence_sha = str(evidence_sha256 or "")
    run_id = str(pipeline_run_id or "")
    if not evidence_sha and not run_id:
        raise HTTPException(status_code=400, detail="evidence_sha256 or pipeline_run_id is required")
    return pipeline_status_from_store(_store(), evidence_sha256=evidence_sha, pipeline_run_id=run_id)


@app.get("/reviews")
def list_reviews(limit: int = 50, evidence_sha256: str | None = None) -> list[dict[str, Any]]:
    return _store().list_review_queue(limit=limit, evidence_sha256=evidence_sha256)


@app.get("/review-targets")
def list_review_targets(
    limit: int = 5,
    evidence_sha256: str | None = None,
    include_reviewed: bool = False,
) -> dict[str, Any]:
    return _list_review_targets_cached(
        limit=limit,
        evidence_sha256=evidence_sha256,
        pending_only=not include_reviewed,
    )


@app.get("/review-targets/{review_target_id}")
def get_review_target(review_target_id: str) -> dict[str, Any]:
    target = _store().get_review_target(review_target_id)
    if target is None:
        raise HTTPException(status_code=404, detail="review target not found")
    return target


@app.get("/proposals")
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


@app.get("/comparisons")
def list_comparisons(
    limit: int = 20,
    evidence_sha256: str | None = None,
) -> list[dict[str, Any]]:
    store = _store()
    if not hasattr(store, "list_model_comparisons"):
        return []
    return store.list_model_comparisons(evidence_sha256=evidence_sha256, limit=limit)


@app.get("/providers")
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


@app.get("/workflow/provider-policy")
def workflow_provider_policy(include_internal: bool = False) -> dict[str, Any]:
    provider_rows = _provider_rows()
    providers = provider_rows if _public_provider_details_allowed(include_internal) else [
        _redact_provider_row(row) for row in provider_rows
    ]
    by_id = {str(row.get("provider_id") or row.get("provider") or ""): row for row in provider_rows}
    alternative_ids = [
        "openai-gpt-oss-on-vertex",
        "mistral-agent-platform",
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


@app.post("/review-targets/{review_target_id}/accept")
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


@app.post("/review-targets/{review_target_id}/reject")
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


@app.post("/review-targets/{review_target_id}/needs-more-data")
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


@app.get("/review-targets/{review_target_id}/more-data-query")
def more_data_query_for_review_target(review_target_id: str, request_id: str | None = None) -> dict[str, Any]:
    query = _store().build_more_data_query_for_target(
        review_target_id,
        request_ids=[request_id] if request_id else None,
    )
    if not query:
        raise HTTPException(status_code=404, detail="review target not found")
    return query


@app.post("/review-targets/{review_target_id}/more-data-refresh")
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


@app.post("/review-targets/{review_target_id}/remote-collect")
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


@app.post("/reviews/{proposition_id}/accept")
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


@app.post("/reviews/{proposition_id}/reject")
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


@app.post("/reviews/{proposition_id}/needs-more-data")
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


@app.get("/propositions/{proposition_id}/more-data-query")
def more_data_query(proposition_id: str, request_id: str | None = None) -> dict[str, Any]:
    store = _store()
    if not hasattr(store, "build_more_data_query"):
        raise HTTPException(status_code=400, detail="store does not support more data queries")
    query = store.build_more_data_query(proposition_id, request_ids=[request_id] if request_id else None)
    if not query:
        raise HTTPException(status_code=404, detail="proposition not found")
    return query


@app.post("/propositions/{proposition_id}/more-data-refresh")
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



def _canonical_review_graph_for_ui(
    evidence_sha256: str | None,
    bundle: dict[str, Any] | None,
    target_set: dict[str, Any],
    synthesis: dict[str, Any] | None,
) -> dict[str, Any]:
    base_bundle = bundle if isinstance(bundle, dict) else {"evidence_sha256": evidence_sha256 or ""}
    try:
        store = _store()
        snapshot_response = _latest_canonical_graph_response(store, evidence_sha256 or "")
        if snapshot_response:
            return dict(snapshot_response.get("canonical_review_graph") or {})
        model_artifacts = _model_run_artifacts_for_ui(evidence_sha256)
        resolution = resolve_canonical_review_graph_snapshot(
            store,
            base_bundle,
            model_runs=model_artifacts,
            multi_ai_synthesis=synthesis if isinstance(synthesis, dict) else {},
            legacy_review_targets=list(target_set.get("targets") or []),
            legacy_summary=dict(target_set.get("summary") or {}),
            persist_if_missing=False,
            persist_if_stale=False,
            created_by="ui-readonly",
        )
        return dict(resolution.get("canonical_review_graph") or {})
    except Exception:
        return {}


def _latest_canonical_graph_response(store: Any, evidence_sha256: str) -> dict[str, Any]:
    if not evidence_sha256 or not hasattr(store, "get_latest_canonical_review_graph_snapshot"):
        return {}
    snapshot = store.get_latest_canonical_review_graph_snapshot(evidence_sha256)
    if not isinstance(snapshot, dict):
        return {}
    graph = snapshot.get("canonical_review_graph_json") if isinstance(snapshot.get("canonical_review_graph_json"), dict) else {}
    if not graph:
        return {}
    graph = dict(graph)
    graph["canonical_graph_status"] = "persisted"
    graph["snapshot_status"] = "persisted"
    graph["canonical_graph_sha256"] = str(snapshot.get("canonical_graph_sha256") or graph.get("canonical_graph_sha256") or "")
    graph["input_fingerprint_sha256"] = str(snapshot.get("input_fingerprint_sha256") or graph.get("input_fingerprint_sha256") or "")
    graph["snapshot_created_at"] = str(snapshot.get("created_at") or graph.get("snapshot_created_at") or "")
    return {
        "canonical_graph_status": "persisted",
        "canonical_graph_sha256": graph["canonical_graph_sha256"],
        "input_fingerprint_sha256": graph["input_fingerprint_sha256"],
        "canonical_review_graph": graph,
        "snapshot": snapshot,
        "snapshot_created_at": graph["snapshot_created_at"],
    }


def _review_summary_for_ui(evidence_sha256: str) -> dict[str, Any]:
    precomputed = _precomputed_summary(_precomputed_review_payload(evidence_sha256), evidence_sha256)
    if precomputed:
        return precomputed
    store = _store()
    bundle = store.get_bundle(evidence_sha256) if hasattr(store, "get_bundle") else None
    snapshot_response = _latest_canonical_graph_response(store, evidence_sha256)
    graph = snapshot_response.get("canonical_review_graph") if isinstance(snapshot_response, dict) else {}
    graph = graph if isinstance(graph, dict) else {}
    finding = graph.get("finding") if isinstance(graph.get("finding"), dict) else {}
    display_summary = graph.get("display_summary") if isinstance(graph.get("display_summary"), dict) else {}
    summary = graph.get("summary") if isinstance(graph.get("summary"), dict) else {}
    dimensions = graph.get("agreement_dimensions") if isinstance(graph.get("agreement_dimensions"), dict) else {}
    pipeline_status = analysis_pipeline_status_from_store(store, evidence_sha256=evidence_sha256)
    raw_policy = ""
    if isinstance(bundle, dict):
        raw_policy = str(bundle.get("raw_log_policy") or bundle.get("raw_output_policy") or "")
    provider_success = int(pipeline_status.get("provider_success") or 0)
    provider_total = int(pipeline_status.get("provider_total") or 0)
    if not provider_total and hasattr(store, "fetch_model_runs"):
        runs = store.fetch_model_runs(evidence_sha256)
        provider_total = len(runs)
        provider_success = sum(
            1
            for run in runs
            if str((run.get("status") if isinstance(run, dict) else getattr(run, "status", "")) or "") == "ok"
        )
    return {
        "schema_version": "ui_summary.v1",
        "evidence_sha256": evidence_sha256,
        "status": "ok" if graph else "not_found",
        "message": "" if graph else "No persisted canonical review graph is available yet.",
        "finding": {
            "title": str(finding.get("title") or display_summary.get("title") or ""),
            "impact": str(finding.get("impact") or display_summary.get("impact") or ""),
        },
        "review": {
            "primary_targets": int(summary.get("primary_count") or 0),
            "validation_targets": int(summary.get("validation_count") or 0),
            "monitor_only": int(summary.get("monitor_only_count") or 0),
            "auto_archived": int(summary.get("auto_archived_count") or 0),
        },
        "providers": {
            "success": provider_success,
            "total": provider_total,
            "pipeline_status": str(pipeline_status.get("status") or "not_started"),
        },
        "baselines": {
            "technical": bool((dimensions.get("technical_baseline_agreement") or {}).get("established")),
            "incident": bool((dimensions.get("incident_baseline_agreement") or dimensions.get("baseline_agreement") or {}).get("established")),
        },
        "raw_log_policy": raw_policy or "unknown",
        "canonical_graph_status": str(snapshot_response.get("canonical_graph_status") or "not_found"),
        "canonical_graph_sha256": str(snapshot_response.get("canonical_graph_sha256") or ""),
        "input_fingerprint_sha256": str(snapshot_response.get("input_fingerprint_sha256") or ""),
    }


def _model_run_artifacts_for_ui(evidence_sha256: str | None) -> list[dict[str, Any]]:
    if not evidence_sha256:
        return []
    store = _store()
    if not hasattr(store, "fetch_model_runs") or not hasattr(store, "fetch_parsed_results"):
        return []
    runs = store.fetch_model_runs(evidence_sha256)
    if not runs:
        return []
    parsed = store.fetch_parsed_results(evidence_sha256)
    return _multi_ai_artifacts_from_records(runs, parsed)


def _target_set_from_canonical_graph(graph: dict[str, Any], legacy_summary: dict[str, Any] | None = None) -> dict[str, Any]:
    if not isinstance(graph, dict) or graph.get("schema_version") != "canonical_review_graph.v1":
        return {"summary": legacy_summary or {}, "targets": []}
    legacy_summary = legacy_summary or {}
    summary = graph.get("summary") if isinstance(graph.get("summary"), dict) else {}
    targets = [
        row
        for row in [*(graph.get("primary_targets") or []), *(graph.get("validation_targets") or [])]
        if isinstance(row, dict)
    ]
    return {
        "schema_version": "canonical_review_target_set.v1",
        "summary": {
            "raw_propositions": int(legacy_summary.get("raw_propositions") or 0),
            "clusters": int(legacy_summary.get("clusters") or legacy_summary.get("claim_groups") or 0),
            "claim_groups": int(legacy_summary.get("claim_groups") or legacy_summary.get("clusters") or 0),
            "review_targets": len(targets),
            "primary_review_targets": int(summary.get("primary_count") or 0),
            "validation_targets": int(summary.get("validation_count") or 0),
            "monitor_only": int(summary.get("monitor_only_count") or 0),
            "auto_archived": int(summary.get("auto_archived_count") or 0),
            "insufficient_evidence": int(legacy_summary.get("insufficient_evidence") or 0),
            "score_note": graph.get("score_note") or SCORE_NOTE,
        },
        "targets": targets,
        "canonical_review_graph": graph,
    }


def _finding_banner(
    summary: dict[str, Any],
    targets: list[dict[str, Any]],
    synthesis: dict[str, Any] | None = None,
    *,
    canonical_graph: dict[str, Any] | None = None,
) -> str:
    synthesis = synthesis or {}
    canonical_graph = canonical_graph or {}
    if canonical_graph.get("schema_version") == "canonical_review_graph.v1":
        finding_row = canonical_graph.get("finding") if isinstance(canonical_graph.get("finding"), dict) else {}
        finding = str(finding_row.get("title") or "Evidence requires profile or additional context")
        impact = str(finding_row.get("impact") or "No sufficiently supported review target was promoted.")
        log_count = int(summary.get("sanitized_log_count") or summary.get("log_count") or 0)
        result = f"Analyzed {_human_count(log_count)} log lines" if log_count else "Analyzed evidence bundle"
        return f"""
    <section class="finding-banner">
      <div class="finding-item"><label>Finding</label><strong>{_html(finding)}</strong></div>
      <div class="finding-item"><label>Impact</label><strong>{_html(impact)}</strong></div>
      <div class="finding-item"><label>Evidence</label><strong>{_html(result)}</strong></div>
    </section>"""
    if synthesis:
        finding_summary = synthesis.get("finding_summary") if isinstance(synthesis.get("finding_summary"), dict) else finding_impact_from_synthesis(synthesis)
        finding = str(finding_summary.get("finding") or "Evidence requires profile or additional context")
        impact = str(finding_summary.get("impact") or "No sufficiently supported review target was promoted.")
        log_count = int(summary.get("sanitized_log_count") or summary.get("log_count") or 0)
        result = f"Analyzed {_human_count(log_count)} log lines" if log_count else "Analyzed evidence bundle"
        return f"""
    <section class="finding-banner">
      <div class="finding-item"><label>Finding</label><strong>{_html(finding)}</strong></div>
      <div class="finding-item"><label>Impact</label><strong>{_html(impact)}</strong></div>
      <div class="finding-item"><label>Evidence</label><strong>{_html(result)}</strong></div>
    </section>"""
    first = targets[0] if targets else {}
    profile_ids = {
        str((target.get("profile") or {}).get("profile_id") or "")
        for target in targets
        if isinstance(target.get("profile"), dict)
    }
    core_types = {
        str(target.get("core_target_type") or target.get("review_target_type") or "")
        for target in targets
    }
    log_count = int(summary.get("sanitized_log_count") or summary.get("log_count") or 0)
    if "amazon_notify" in profile_ids and (
        "job_configuration_mismatch" in core_types or "service_start_failure" in core_types
    ):
        finding = "systemd unit references a missing command target"
        impact = "watchdog service repeatedly fails to start"
        result = f"Detected configuration mismatch from {_human_count(log_count)} real log lines" if log_count else "Detected configuration mismatch from real logs"
        return f"""
    <section class="finding-banner">
      <div class="finding-item"><label>Finding</label><strong>{_html(finding)}</strong></div>
      <div class="finding-item"><label>Impact</label><strong>{_html(impact)}</strong></div>
      <div class="finding-item"><label>Evidence</label><strong>{_html(result)}</strong><span>{_html(_human_count(log_count) + " log lines" if log_count else "real logs")}</span></div>
    </section>"""
    title = str(first.get("title") or "Review target detected")
    impact = str(first.get("core_claim") or first.get("support_summary") or "Evidence requires human review.")
    result = (
        f"Analyzed {_human_count(log_count)} log lines"
        if log_count
        else "Analyzed evidence bundle"
    )
    return f"""
    <section class="finding-banner">
      <div class="finding-item"><label>Finding</label><strong>{_html(title)}</strong></div>
      <div class="finding-item"><label>Impact</label><strong>{_html(impact)}</strong></div>
      <div class="finding-item"><label>Evidence</label><strong>{_html(result)}</strong></div>
    </section>"""


def _artifact_upload_panel(*, collapsed: bool = False, evidence_sha256: str | None = None) -> str:
    body = """
      <h2>Upload Sanitized Evidence Bundle</h2>
      <p class="score-note">
        Drag and drop a local-first <code>evidence_bundle.json</code>. Raw logs, raw source files,
        and raw env values are not accepted by this screen.
      </p>
      <div class="drop-zone" id="artifact-drop-zone" role="button" tabindex="0" aria-label="Upload sanitized evidence bundle">
        <strong>Drop evidence_bundle.json here</strong>
        <span>or click to choose a file</span>
        <span class="score-note">Accepted schema: <code>evidence_bundle.v1</code> / <code>sanitized_evidence_bundle</code></span>
      </div>
      <input id="artifact-file-input" type="file" accept=".json,application/json" style="display:none">
      <div class="upload-status" id="artifact-upload-status">
        <span class="score-note">Upload runs server-side contract validation and secret/PII scanning before saving to BigQuery.</span>
      </div>"""
    if collapsed:
        evidence_text = f"<code>{_html(evidence_sha256 or '')}</code>" if evidence_sha256 else ""
        return f"""
    <details class="upload-panel upload-panel-compact" id="artifact-upload">
      <summary><span>Upload another sanitized Evidence Bundle</span>{evidence_text}</summary>
      <div class="upload-panel-body">{body}</div>
    </details>"""
    return """
    <section class="upload-panel" id="artifact-upload">
""" + body + """
    </section>"""


def _local_first_panel(bundle: dict[str, Any] | None) -> str:
    if not isinstance(bundle, dict) or bundle.get("bundle_type") != "sanitized_evidence_bundle":
        return ""
    summary = bundle.get("local_first_summary") if isinstance(bundle.get("local_first_summary"), dict) else {}
    policy = bundle.get("analysis_policy") if isinstance(bundle.get("analysis_policy"), dict) else {}
    source = bundle.get("source") if isinstance(bundle.get("source"), dict) else {}
    signals = [
        str(signal.get("signal_type") or "")
        for signal in bundle.get("signals") or []
        if isinstance(signal, dict) and signal.get("signal_type")
    ]
    questions = [str(item) for item in bundle.get("required_profile_questions") or [] if str(item).strip()]
    profile_mode = str(policy.get("profile_mode") or source.get("profile_confidence") or "unknown")
    primary_allowed = bool(policy.get("allow_primary_candidate"))
    signal_text = ", ".join(signals[:8]) or "none"
    question_items = "".join(f"<li>{_html(question)}</li>" for question in questions[:5])
    profile_note = (
        "This bundle includes an explicit system profile and can proceed to primary candidate review."
        if primary_allowed
        else (
            "This bundle does not include an explicit system profile. "
            "Generic operational signals and required profile questions are shown before incident diagnosis."
        )
    )
    return f"""
    <section class="finding-banner">
      <div class="finding-item">
        <label>Local-first safety check</label>
        <strong>Server-side bundle validation ready</strong>
        <span>Raw logs uploaded: {_html(str(bool(summary.get("raw_logs_uploaded"))).lower())}</span>
        <span>Raw log policy: {_html(summary.get("raw_log_policy") or bundle.get("raw_log_policy") or "")}</span>
        <span>Evidence SHA256: <code>{_html(bundle.get("evidence_sha256") or "")}</code></span>
      </div>
      <div class="finding-item">
        <label>Profile mode</label>
        <strong>{_html(profile_mode)}</strong>
        <span>Explicit profile: {_html(str(bool(policy.get("explicit_profile"))).lower())}</span>
        <span>Primary candidate allowed: {_html(str(primary_allowed).lower())}</span>
        <span>Detected format: {_html(summary.get("detected_format") or source.get("detected_format") or "")}</span>
      </div>
      <div class="finding-item">
        <label>Generic signals</label>
        <strong>{_html(signal_text)}</strong>
        <span>{_html(profile_note)}</span>
        <ul>{question_items}</ul>
      </div>
    </section>"""


def _pipeline_progress_panel(status: dict[str, Any] | None) -> str:
    status = status if isinstance(status, dict) else {}
    evidence_sha = str(status.get("evidence_sha256") or "")
    pipeline_run_id = str(status.get("pipeline_run_id") or "")
    operation = str(status.get("operation") or "No run recorded")
    run_status = str(status.get("status") or "not_started")
    canonical_state = str(status.get("canonical_state") or "")
    progress = int(max(0, min(100, round(float(status.get("progress_percent") or 0)))))
    current = str(status.get("current_step_label") or status.get("current_step") or "No step recorded")
    blocking_reason = str(status.get("blocking_reason") or "")
    provider_frontier = (
        f"Providers: {int(status.get('provider_success') or 0)} ok / "
        f"{int(status.get('provider_failed') or 0)} failed / "
        f"{int(status.get('provider_skipped') or 0)} skipped / "
        f"{int(status.get('provider_total') or 0)} total"
    )
    review_frontier = (
        f"Review: {int(status.get('review_target_count') or 0)} targets / "
        f"{int(status.get('validation_target_count') or 0)} validation / "
        f"{int(status.get('child_bundle_count') or 0)} child bundles"
    )
    status_class = "".join(ch for ch in run_status if ch.isalnum() or ch in {"-", "_"}) or "unknown"
    steps = [step for step in status.get("steps") or [] if isinstance(step, dict)]
    events = [event for event in status.get("events") or [] if isinstance(event, dict)]
    state_timeline = [item for item in status.get("state_timeline") or [] if isinstance(item, dict)]
    active_reasons = [str(reason) for reason in status.get("active_reasons") or [] if str(reason)]
    state_timeline_html = _pipeline_state_timeline_html(state_timeline, canonical_state)
    reason_codes_html = _pipeline_reason_codes_html(active_reasons)
    if steps:
        step_items = "".join(
            f"""
            <li data-step-status="{_html(str(step.get("status") or "pending"))}">
              <span class="pipeline-step-state">{_html(str(step.get("status") or "pending"))}</span>{_pipeline_canonical_state_chip(str(step.get("canonical_state") or ""))}
              <strong>{_html(str(step.get("step_label") or step.get("step_key") or ""))}</strong>
              <small>{_html(str(step.get("message") or ""))}</small>
            </li>
            """
            for step in steps
        )
    else:
        step_items = "<li data-step-status='pending'><span class='pipeline-step-state'>not_started</span><strong>No pipeline run recorded yet</strong><small>Run upload, analysis, plan generation, review, or more-data refresh to create progress history.</small></li>"
    recent_events = events[-6:]
    event_items = "".join(
        f"""
        <li>
          <code>{_html(str(event.get("step_key") or ""))}</code>
          <span>{_html(_pipeline_event_status_text(event))}</span>
          <small>{_html(str(event.get("message") or ""))}</small>
        </li>
        """
        for event in recent_events
    )
    if not event_items:
        event_items = "<li><span>not_started</span><small>No server-side progress events recorded.</small></li>"
    return f"""
    <section class="pipeline-panel" id="pipeline-progress-panel" data-evidence-sha256="{_html(evidence_sha)}" data-pipeline-run-id="{_html(pipeline_run_id)}">
      <div class="pipeline-header">
        <div>
          <label>Pipeline Progress</label>
          <strong data-pipeline-operation>{_html(operation)}</strong>
          <span data-pipeline-current>{_html(current)}</span>
          <span data-pipeline-current-state>{_html("State: " + canonical_state if canonical_state else "State: not_started")}</span>
        </div>
        <div class="pipeline-status" data-pipeline-status="{_html(status_class)}">
          <strong data-pipeline-status-text>{_html(run_status)}</strong>
          <span data-pipeline-progress-text>{progress}%</span>
        </div>
      </div>
      <p class="pipeline-blocking-reason" data-pipeline-blocking-reason{" hidden" if not blocking_reason else ""}>{_html("Blocking reason: " + blocking_reason if blocking_reason else "")}</p>
      <div class="pipeline-state-summary" data-pipeline-state-timeline{" hidden" if not state_timeline_html else ""}>{state_timeline_html}</div>
      <div class="pipeline-reason-codes" data-pipeline-reason-codes{" hidden" if not reason_codes_html else ""}>{reason_codes_html}</div>
      <div class="pipeline-frontier">
        <span data-pipeline-provider-frontier>{_html(provider_frontier)}</span>
        <span data-pipeline-review-frontier>{_html(review_frontier)}</span>
      </div>
      <div class="pipeline-meter" role="progressbar" aria-valuemin="0" aria-valuemax="100" aria-valuenow="{progress}" aria-label="Pipeline progress">
        <div data-pipeline-progress-bar style="width: {progress}%"></div>
      </div>
      <ol class="pipeline-steps" data-pipeline-steps>{step_items}</ol>
      <details class="pipeline-events">
        <summary>Recent pipeline events</summary>
        <ul data-pipeline-events>{event_items}</ul>
      </details>
    </section>"""


def _pipeline_event_status_text(event: dict[str, Any]) -> str:
    parts = [str(event.get("status") or "")]
    if event.get("canonical_state"):
        parts.append(str(event.get("canonical_state") or ""))
    if event.get("reason_code"):
        parts.append(str(event.get("reason_code") or ""))
    if event.get("provider_id"):
        parts.append(str(event.get("provider_id") or ""))
    return " / ".join(part for part in parts if part)


def _pipeline_state_timeline_html(state_timeline: list[dict[str, Any]], current_state: str) -> str:
    states: list[str] = []
    for item in state_timeline:
        state = str(item.get("state") or "")
        if state and state not in states:
            states.append(state)
    if current_state and current_state not in states:
        states.append(current_state)
    return "".join(
        f'<span class="pipeline-state-chip" data-current-state="{_html(str(state == current_state).lower())}">{_html(state)}</span>'
        for state in states
    )


def _pipeline_reason_codes_html(active_reasons: list[str]) -> str:
    unique: list[str] = []
    for reason in active_reasons:
        if reason and reason not in unique:
            unique.append(reason)
    return "".join(f'<span class="pipeline-reason-chip">{_html(reason)}</span>' for reason in unique)


def _pipeline_canonical_state_chip(state: str) -> str:
    return f'<span class="pipeline-canonical-state">{_html(state)}</span>' if state else ""




def _multi_ai_synthesis_for_ui(evidence_sha256: str | None, bundle: dict[str, Any] | None = None) -> dict[str, Any]:
    if not evidence_sha256:
        return {}
    store = _store()
    if not hasattr(store, "fetch_model_runs") or not hasattr(store, "fetch_parsed_results"):
        return {}
    ui_bundle = bundle if isinstance(bundle, dict) else None
    if ui_bundle is None and hasattr(store, "get_bundle"):
        maybe_bundle = store.get_bundle(evidence_sha256)
        ui_bundle = maybe_bundle if isinstance(maybe_bundle, dict) else None
    if ui_bundle is None:
        ui_bundle = {"evidence_sha256": evidence_sha256}
    runs = store.fetch_model_runs(evidence_sha256)
    if not runs:
        return {}
    parsed = store.fetch_parsed_results(evidence_sha256)
    artifacts = _multi_ai_artifacts_from_records(runs, parsed)
    return synthesize_multi_ai(ui_bundle, artifacts)

def _multi_ai_panel(evidence_sha256: str | None, *, canonical_graph: dict[str, Any] | None = None) -> str:
    if not evidence_sha256:
        return ""
    store = _store()
    if not hasattr(store, "fetch_model_runs") or not hasattr(store, "fetch_parsed_results"):
        return ""
    runs = store.fetch_model_runs(evidence_sha256)
    if not runs:
        return """
    <section class="finding-banner multi-ai-panel">
      <div class="finding-item">
        <label>Multi-AI runs</label>
        <strong>No model runs recorded yet</strong>
        <span>Run <code>ops-evidence run-multi-ai</code> or <code>POST /ai/multi-run</code>.</span>
      </div>
      <div class="finding-item">
        <label>Safety</label>
        <strong>Raw logs are not sent to providers</strong>
        <span>Only sanitized Evidence Bundles are valid model input.</span>
      </div>
    </section>"""
    synthesis = _multi_ai_synthesis_for_ui(evidence_sha256)
    canonical_graph = canonical_graph or {}
    dimensions = canonical_graph.get("agreement_dimensions") if isinstance(canonical_graph.get("agreement_dimensions"), dict) else {}
    provider_overlap = (dimensions.get("provider_detection_overlap") or {}).get("value") or "0/0"
    technical_baseline = dimensions.get("technical_baseline_agreement") if isinstance(dimensions.get("technical_baseline_agreement"), dict) else {}
    incident_baseline = (
        dimensions.get("incident_baseline_agreement")
        if isinstance(dimensions.get("incident_baseline_agreement"), dict)
        else dimensions.get("baseline_agreement") if isinstance(dimensions.get("baseline_agreement"), dict) else {}
    )
    technical_baseline_text = "established" if technical_baseline.get("established") else "not established"
    incident_baseline_text = "established" if incident_baseline.get("established") else "not established"
    cause_text = (dimensions.get("cause_agreement") or {}).get("value") or "none"
    impact_text = (dimensions.get("impact_agreement") or {}).get("value") or "none"
    review_unit_convergence = (dimensions.get("review_unit_convergence") or {}).get("value") or "none"
    provider_rows = "".join(
        "<li>"
        f"<strong>{_html(str(row.get('provider_id') or ''))}</strong> "
        f"{_html(str(row.get('status') or 'unknown'))} / "
        f"schema_valid={_html(str(bool(row.get('schema_valid'))).lower())} "
        f"<code>{_html(str(row.get('raw_output_sha256') or '')[:12])}</code>"
        "</li>"
        for row in synthesis.get("provider_statuses") or []
    )
    agreement_count = len(synthesis.get("agreement_groups") or [])
    disagreement_count = len(synthesis.get("disagreement_groups") or [])
    validation_count = len(synthesis.get("validation_targets") or [])
    themes = [row for row in synthesis.get("disagreement_themes") or [] if isinstance(row, dict)]
    theme_rows = "".join(
        "<li>"
        f"<strong>{_html(str(row.get('theme') or ''))}</strong> "
        f"{int(row.get('group_count') or 0)} groups / "
        f"validation: <code>{_html(str(row.get('recommended_validation') or ''))}</code>"
        "</li>"
        for row in themes[:5]
    )
    disagreement_note = (
        "No incident baseline agreement was found. The system did not promote a primary incident candidate. "
        "Disputed claims were routed to validation targets for human review."
        if agreement_count == 0 and disagreement_count > 0
        else f"{validation_count} validation targets remain for human review."
    )
    return f"""
    <section class="finding-banner multi-ai-panel">
      <div class="finding-item">
        <label>Multi-AI runs</label>
        <strong>{int(synthesis.get("successful_provider_count") or 0)} successful / {int(synthesis.get("provider_count") or 0)} total</strong>
        <ul>{provider_rows}</ul>
      </div>
      <div class="finding-item">
        <label>Agreement Dimensions</label>
        <strong>Provider detection overlap: {_html(provider_overlap)}</strong>
        <span>Technical baseline: {_html(technical_baseline_text)}</span>
        <span>Incident baseline: {_html(incident_baseline_text)}</span>
        <span>Review-unit convergence: {_html(review_unit_convergence)}</span>
        <span>Cause agreement: {_html(cause_text)}</span>
        <span>Impact agreement: {_html(impact_text)}</span>
        <span>Technical agreement is not treated as incident truth or primary promotion.</span>
      </div>
      <div class="finding-item">
        <label>Disagreement Themes</label>
        <strong>{len(themes)} themes</strong>
        <ul>{theme_rows or "<li>No disagreement themes</li>"}</ul>
      </div>
      <div class="finding-item">
        <label>Disagreement</label>
        <strong>{disagreement_count} disagreement groups</strong>
        <span>{_html(disagreement_note)}</span>
      </div>
      <div class="finding-item">
        <label>Safety</label>
        <strong>Raw logs were not sent to providers</strong>
        <span>{_html(str((synthesis.get("safety") or {}).get("policy") or ""))}</span>
        <span>{_html(SCORE_NOTE)}</span>
      </div>
    </section>"""


def _multi_ai_artifacts_from_records(runs: list[Any], parsed_results: list[Any]) -> list[dict[str, Any]]:
    parsed_by_run = {str(result.run_id): result for result in parsed_results}
    artifacts: list[dict[str, Any]] = []
    for run in runs:
        parsed = parsed_by_run.get(str(run.run_id))
        claims = []
        proposed = []
        missing: list[str] = []
        caveats: list[str] = []
        parsed_sha = ""
        schema_valid = False
        schema_errors: list[str] = []
        if parsed is not None:
            payload = parsed.parsed_json
            claims = [claim for claim in payload.get("claims") or [] if isinstance(claim, dict)]
            proposed = [row for row in payload.get("propositions") or [] if isinstance(row, dict)]
            missing = _unique_text(
                item for claim in claims for item in claim.get("missing_evidence") or [] if str(item).strip()
            )
            caveats = _unique_text(item for claim in claims for item in claim.get("caveats") or [] if str(item).strip())
            parsed_sha = parsed.parsed_json_sha256
            schema_valid = bool(parsed.schema_valid)
            schema_errors = [str(item) for item in parsed.schema_errors]
        artifacts.append(
            {
                "schema_version": "model_run.v1",
                "run_id": run.run_id,
                "evidence_sha256": run.evidence_sha256,
                "provider_id": run.provider,
                "display_name": run.provider,
                "model_name": run.model_name,
                "status": run.status,
                "latency_ms": run.latency_ms,
                "input_tokens": run.input_tokens,
                "output_tokens": run.output_tokens,
                "raw_output_sha256": run.raw_output_sha256,
                "parsed_json_sha256": parsed_sha,
                "schema_valid": schema_valid,
                "schema_errors": schema_errors,
                "failure_reason": "" if run.status == "ok" and schema_valid else str(run.status),
                "parsed_result": {
                    "claims": claims,
                    "missing_evidence": missing,
                    "caveats": caveats,
                    "proposed_review_targets": proposed,
                },
                "safety_preflight": {
                    "passed": run.status != "blocked_by_safety_preflight",
                    "raw_logs_sent_to_providers": False,
                },
            }
        )
    return artifacts


def _evidence_request_planner_panel(bundle: dict[str, Any] | None, *, canonical_graph: dict[str, Any] | None = None) -> str:
    if not _supports_evidence_request_planner(bundle):
        return ""
    source = bundle.get("source") if isinstance(bundle.get("source"), dict) else {}
    policy = bundle.get("analysis_policy") if isinstance(bundle.get("analysis_policy"), dict) else {}
    profile_mode = str(policy.get("profile_mode") or source.get("profile_confidence") or "unknown")
    profile = _planner_ui_profile(bundle)
    canonical_graph = canonical_graph or {}
    try:
        plan = build_evidence_request_plan(
            bundle,
            profile,
            canonical_review_graph=canonical_graph,
            generated_from={
                "evidence_bundle": "ui_selected_bundle",
                "approved_profile": "ui_minimal_profile_context",
                "canonical_review_graph": "ui_canonical_review_graph" if canonical_graph else "",
            },
        )
    except ValueError:
        return ""
    instructions = render_collection_instructions(plan)
    sample_answers = sample_planner_answers(plan)
    questions = "".join(_planner_question_card(question) for question in plan.get("human_questions") or [])
    requirements = _planner_requirement_cards(plan)
    requests = _planner_request_cards(plan)
    warnings = [row for row in plan.get("planner_quality_warnings") or [] if isinstance(row, dict)]
    warning_items = "".join(
        f"<li><code>{_html(str(row.get('warning_type') or ''))}</code>: {_html(str(row.get('message') or ''))}</li>"
        for row in warnings
    )
    warning_panel = f"""
        <div class="planner-question">
          <h3>Planner quality warnings</h3>
          <p>Plan valid: {_html(str(bool(plan.get('plan_valid'))).lower())}</p>
          <ul class="planner-policy-list">{warning_items or '<li>No planner quality warnings.</li>'}</ul>
        </div>
    """
    collection_timezone = ((plan.get("incident_window") or {}).get("timezone") if isinstance(plan.get("incident_window"), dict) else "") or "UTC"
    operator_timezone = str(plan.get("operator_display_timezone") or collection_timezone)
    timezone_panel = f"""
        <div class="planner-question">
          <h3>Timezone handling</h3>
          <p>Collection timezone: <code>{_html(collection_timezone)}</code></p>
          <p>Operator display timezone: <code>{_html(operator_timezone)}</code></p>
        </div>
    """
    source_analysis_context: dict[str, Any] = {}
    return f"""
    <section class="planner-panel" id="evidence-request-planner">
      <details>
        <summary><span>Evidence Request Planner</span><code>{_html(plan.get("plan_id") or "")}</code></summary>
        <p class="score-note">Use this only when a review target needs additional sanitized evidence. It is a planning aid, not part of the current review result.</p>
        <div class="planner-grid">
          <div class="planner-question">
            <h3>Safety Policy</h3>
            <ul class="planner-policy-list">
              <li>Planner does not execute commands.</li>
              <li>Raw outputs stay local.</li>
              <li>Raw env values and credentials are not collected.</li>
              <li>sanitize / verify-sanitized required before upload.</li>
              <li>Human answers are context, not evidence.</li>
            </ul>
          </div>
          <div class="planner-question">
            <h3>Plan identity</h3>
            <p><code>{_html(plan.get("plan_id") or "")}</code></p>
            <p>Profile mode: {_html(profile_mode)}</p>
            <p>Canonical graph used: {_html(str(bool(plan.get('canonical_review_graph_used'))).lower())}</p>
            <p>API: <code>POST /evidence-requests/plan</code></p>
          </div>
          <div class="planner-question">
            <h3>Execution boundary</h3>
            <p>Generated command templates are not executed by this UI.</p>
            <p>Only sanitized and verified child Evidence Bundles should be uploaded.</p>
          </div>
          {timezone_panel}
          {warning_panel}
        </div>
	        <h2>Human Questions</h2>
	        <div class="planner-grid">{questions}</div>
	        <h2>Evidence Requirements</h2>
	        <div class="planner-grid">{requirements}</div>
	        <h2>Generated Plan</h2>
	        <div class="planner-grid">{requests}</div>
	        <div class="planner-auth">
	          <label for="planner-write-token-input">Write token</label>
	          <input id="planner-write-token-input" type="password" autocomplete="off" spellcheck="false" placeholder="Required for Generate refined plan">
	          <button type="button" onclick="savePlannerWriteToken()">Save token</button>
	          <button type="button" onclick="clearPlannerWriteToken()">Clear</button>
	          <span id="planner-write-token-status" class="score-note">Required for write actions.</span>
	        </div>
	        <div class="actions">
	          <button id="planner-refine-button" class="primary" type="button" onclick="generateRefinedPlan()" disabled>Generate refined plan</button>
	          <button type="button" onclick="copyElementText('planner-collection-markdown', 'planner-copy-status')">Copy collection notes</button>
	          <span id="planner-refine-status" class="score-note">Ready.</span>
	          <span id="planner-copy-status" class="score-note"></span>
	        </div>
        <div id="planner-refine-progress" class="planner-progress" role="progressbar" aria-valuemin="0" aria-valuemax="100" aria-valuenow="0" aria-label="Planner ready">
          <div class="planner-progress-row">
            <span id="planner-refine-progress-step">Ready</span>
            <span id="planner-refine-progress-value">0%</span>
          </div>
          <div class="planner-progress-track">
	            <div id="planner-refine-progress-bar" class="planner-progress-bar"></div>
	          </div>
	        </div>
	        <div id="planner-result-panel" class="planner-result-panel" data-state="idle">
	          <strong>Result output</strong>
	          <p id="planner-result-message">After generation, output appears in Refined Output: Collection Instructions below this panel.</p>
	          <div class="actions">
	            <button type="button" onclick="revealPlannerCollectionInstructions()">View output below</button>
	            <button type="button" onclick="revealPlannerTechnicalJson()">View technical JSON</button>
	          </div>
	        </div>
	        <div class="grid">
	          <div class="field full">
	            <label>Refined Output: Collection Instructions</label>
	            <p id="planner-output-stamp" class="planner-output-stamp" data-state="idle">Not generated in this browser yet.</p>
	            <pre id="planner-collection-markdown">{_html(instructions)}</pre>
	          </div>
	        </div>
	        <details class="planner-technical-json" id="planner-technical-json">
          <summary>Technical JSON</summary>
          <div class="actions">
            <button type="button" onclick="copyElementText('planner-answers-json', 'planner-copy-status')">Copy answers JSON</button>
            <button type="button" onclick="copyElementText('planner-refined-plan-json', 'planner-copy-status')">Copy refined plan JSON</button>
          </div>
          <div class="grid">
            <div class="field full">
              <label>planner_answers.json</label>
              <pre id="planner-answers-json">{_html(json.dumps(sample_answers, ensure_ascii=False, sort_keys=True, indent=2))}</pre>
            </div>
            <div class="field full">
              <label>Refined plan JSON</label>
              <pre id="planner-refined-plan-json">{_html(json.dumps(plan, ensure_ascii=False, sort_keys=True, indent=2))}</pre>
            </div>
          </div>
        </details>
      </details>
      <script type="application/json" id="planner-bundle-json">{_json_for_script(bundle)}</script>
      <script type="application/json" id="planner-approved-profile-json">{_json_for_script(profile)}</script>
      <script type="application/json" id="planner-source-analysis-json">{_json_for_script(source_analysis_context)}</script>
      <script type="application/json" id="planner-canonical-review-graph-json">{_json_for_script(canonical_graph)}</script>
      <script type="application/json" id="planner-plan-json">{_json_for_script(plan)}</script>
    </section>"""


def _planner_ui_profile(bundle: dict[str, Any]) -> dict[str, Any]:
    source = bundle.get("source") if isinstance(bundle.get("source"), dict) else {}
    policy = bundle.get("analysis_policy") if isinstance(bundle.get("analysis_policy"), dict) else {}
    profile = bundle.get("profile") if isinstance(bundle.get("profile"), dict) else {}
    explicit = bool(policy.get("explicit_profile"))
    profile_id = str(profile.get("profile_id") or source.get("profile_name") or source.get("service") or "ui_profile")
    component_map: dict[str, dict[str, str]] = {}
    bundle_components = bundle.get("component_map") if isinstance(bundle.get("component_map"), dict) else {}
    for name, description in bundle_components.items():
        component = str(name or "").strip()
        if component and component not in component_map:
            component_map[component] = {
                "name": component,
                "subsystem": component,
                "description": str(description or ""),
            }
    for signal in bundle.get("signals") or []:
        if not isinstance(signal, dict):
            continue
        component = str(signal.get("component") or "").strip()
        if component and component not in component_map:
            component_map[component] = {"name": component, "subsystem": "unknown"}
    return {
        "profile_id": profile_id,
        "profile_discovery_approval": {"approved": explicit, "explicit_profile": explicit},
        "component_map": component_map,
        "collector_mappings": {},
        "metric_semantics": bundle.get("metric_semantics") if isinstance(bundle.get("metric_semantics"), dict) else {},
        "log_sources": bundle.get("log_sources") if isinstance(bundle.get("log_sources"), list) else [],
    }


def _supports_evidence_request_planner(bundle: dict[str, Any] | None) -> bool:
    if not isinstance(bundle, dict):
        return False
    if bundle.get("bundle_type") == "sanitized_evidence_bundle":
        return True
    return str(bundle.get("schema_version") or "") == "ops-evidence-bundle/v1" and bool(
        bundle.get("system_profile") or bundle.get("component_map") or bundle.get("operational_evidence")
    )


def _planner_question_card(question: dict[str, Any]) -> str:
    key = str(question.get("answer_key") or "")
    input_type = str(question.get("input_type") or "text")
    ui_type = _planner_ui_input_type(question)
    policy_html = ""
    if key == "allow_config_metadata_only":
        allowed = question.get("policy", {}).get("allowed_extractions", []) if isinstance(question.get("policy"), dict) else []
        prohibited = question.get("policy", {}).get("prohibited_extractions", []) if isinstance(question.get("policy"), dict) else []
        policy_html = f"""
          <p><strong>Raw env values and credentials will never be collected or uploaded.</strong></p>
          <p>This only allows metadata extraction: {_html(", ".join(str(item) for item in allowed))}</p>
          <p>Prohibited: {_html(", ".join(str(item) for item in prohibited))}</p>
        """
    return f"""
    <div class="planner-question" data-planner-question="true" data-answer-key="{_html(key)}" data-input-type="{_html(ui_type)}">
      <h3>{_html(question.get("question_id") or "")} {_html(question.get("label") or key)}</h3>
      <p class="score-note">input_type: <code>{_html(input_type)}</code> / required: {_html(str(bool(question.get("required"))).lower())}</p>
      <p>{_html(question.get("help") or "")}</p>
      {_planner_question_input(question, ui_type)}
      {policy_html}
    </div>"""


def _planner_ui_input_type(question: dict[str, Any]) -> str:
    input_type = str(question.get("input_type") or "text")
    if input_type == "single_select" and isinstance(question.get("default"), dict):
        return "json"
    return input_type


def _planner_question_input(question: dict[str, Any], ui_type: str) -> str:
    key = str(question.get("answer_key") or "")
    default = question.get("default")
    options = [str(item) for item in question.get("options") or []]
    if ui_type == "datetime_range":
        value = default if isinstance(default, dict) else {}
        return (
            f'<input data-planner-field="start" value="{_html(value.get("start") or "")}" aria-label="{_html(key)} start">'
            f'<input data-planner-field="end" value="{_html(value.get("end") or "")}" aria-label="{_html(key)} end">'
            f'<input data-planner-field="timezone" value="{_html(value.get("timezone") or "UTC")}" aria-label="{_html(key)} timezone">'
        )
    if ui_type == "single_select":
        selected = str(default or "")
        choices = options or ([selected] if selected else [])
        return (
            f'<select data-planner-key="{_html(key)}">'
            + "".join(
                f'<option value="{_html(option)}"{" selected" if option == selected else ""}>{_html(option)}</option>'
                for option in choices
            )
            + "</select>"
        )
    if ui_type == "component_map_select":
        items = [item for item in question.get("items") or [] if isinstance(item, dict)]
        rows = []
        for item in items:
            component = str(item.get("component") or "")
            selected = str(item.get("default") or "unknown")
            choices = [str(option) for option in item.get("options") or ["critical_path", "diagnostic_only", "unknown"]]
            rows.append(
                '<label style="display:block; font-size:12px; text-transform:none; margin-bottom:6px;">'
                f'<span>{_html(component)}</span>'
                f'<select data-planner-component="{_html(component)}">'
                + "".join(
                    f'<option value="{_html(option)}"{" selected" if option == selected else ""}>{_html(option)}</option>'
                    for option in choices
                )
                + '</select></label>'
            )
        return "".join(rows) or '<p class="score-note">No components detected.</p>'
    if ui_type == "multi_select":
        selected = {str(item) for item in default} if isinstance(default, list) else set()
        choices = options or sorted(selected)
        return "".join(
            '<label style="display:block; font-size:12px; text-transform:none;">'
            f'<input type="checkbox" data-planner-value="true" data-planner-key="{_html(key)}" '
            f'value="{_html(option)}"{" checked" if option in selected else ""}> {_html(option)}</label>'
            for option in choices
        )
    if ui_type == "boolean":
        return (
            '<label style="display:block; font-size:12px; text-transform:none;">'
            f'<input type="checkbox" data-planner-key="{_html(key)}"{" checked" if bool(default) else ""}> enabled</label>'
        )
    if ui_type == "integer":
        return f'<input type="number" data-planner-key="{_html(key)}" value="{_html(default if default is not None else 0)}">'
    if ui_type == "path_list":
        values = default if isinstance(default, list) else []
        return f'<textarea data-planner-key="{_html(key)}">{_html(chr(10).join(str(item) for item in values))}</textarea>'
    if ui_type == "json":
        return f'<textarea data-planner-key="{_html(key)}">{_html(json.dumps(default or {}, ensure_ascii=False, sort_keys=True, indent=2))}</textarea>'
    return f'<input type="text" data-planner-key="{_html(key)}" value="{_html(default or "")}">'


def _planner_request_cards(plan: dict[str, Any]) -> str:
    cards = []
    for request in (plan.get("requests") or [])[:6]:
        if not isinstance(request, dict):
            continue
        steps = [step for step in request.get("collection_steps") or [] if isinstance(step, dict)]
        commands = [str(step.get("command_template") or "") for step in steps if step.get("command_template")]
        step_warnings = [str(step.get("warning") or "") for step in steps if step.get("warning")]
        mapping = ""
        if request.get("domain_mapping_applied"):
            mapping = (
                f"<p>Domain mapping: <code>{_html(str(request.get('generic_request_type') or ''))}</code> -> "
                f"<code>{_html(str(request.get('domain_request_type') or ''))}</code> via {_html(str(request.get('mapped_by') or ''))}</p>"
            )
        warning_html = "".join(f"<li>{_html(item)}</li>" for item in step_warnings)
        cards.append(
            f"""
            <div class="planner-request">
              <h3>{_html(request.get("priority") or "P2")} {_html(request.get("request_type") or "")}</h3>
              <p><strong>{_html(request.get("question") or "")}</strong></p>
              <p>{_html(request.get("why_needed") or "")}</p>
              <p>Required granularity: {_html((request.get("granularity") or {}).get("required") or "")}</p>
              {mapping}
              <p>Read-only command templates:</p>
              <ul class="planner-policy-list">{"".join(f"<li><code>{_html(command)}</code></li>" for command in commands[:3])}</ul>
              {f'<ul class="planner-policy-list">{warning_html}</ul>' if warning_html else ''}
              <p>Post collection: sanitize -> verify-sanitized -> build child Evidence Bundle.</p>
            </div>
            """
        )
    return "".join(cards) or "<p class='score-note'>No requests generated.</p>"


def _planner_requirement_cards(plan: dict[str, Any]) -> str:
    cards = []
    metadata = plan.get("evidence_requirements_metadata") if isinstance(plan.get("evidence_requirements_metadata"), dict) else {}
    for requirement in (plan.get("evidence_requirements") or [])[:6]:
        if not isinstance(requirement, dict):
            continue
        evidence_rows = [row for row in requirement.get("required_evidence") or [] if isinstance(row, dict)]
        evidence_html = "".join(
            "<li>"
            f"<code>{_html(str(row.get('evidence_type') or 'runtime_evidence'))}</code> "
            f"via <code>{_html(str(row.get('maps_to_request_type') or ''))}</code><br>"
            f"Accept: {_html(str(row.get('acceptance_criteria') or ''))}<br>"
            f"Reject: {_html(str(row.get('rejection_criteria') or ''))}"
            "</li>"
            for row in evidence_rows[:3]
        )
        cards.append(
            f"""
            <div class="planner-request">
              <h3>{_html(requirement.get("requirement_id") or "")} {_html(requirement.get("canonical_review_unit") or "")}</h3>
              <p><strong>{_html(requirement.get("question_to_close") or "")}</strong></p>
              <p>Blocked reason: <code>{_html(requirement.get("blocked_reason") or "")}</code></p>
              <p>Review target: <code>{_html(requirement.get("review_target_id") or "")}</code></p>
              <ul class="planner-policy-list">{evidence_html or '<li>No required evidence rows.</li>'}</ul>
              <p class="score-note">Fallback: {_html(requirement.get("fallback_if_unavailable") or "")}</p>
            </div>
            """
        )
    if not cards:
        return "<p class='score-note'>No promotion-gate evidence requirements were generated.</p>"
    summary = (
        f"<p class='score-note'>Generation: {_html(str(metadata.get('generation_mode') or 'unknown'))}; "
        f"LLM status: {_html(str(metadata.get('llm_status') or 'not_requested'))}</p>"
    )
    return summary + "".join(cards)


def _bundle_provenance_panel(bundle: dict[str, Any] | None) -> str:
    if not isinstance(bundle, dict) or bundle.get("bundle_type") != "sanitized_evidence_bundle":
        return ""
    sha = str(bundle.get("evidence_sha256") or "")
    parent_sha = str(bundle.get("parent_evidence_sha256") or "")
    plan_id = str(bundle.get("evidence_request_plan_id") or "")
    collection_mode = str(bundle.get("collection_mode") or "")
    raw_output_policy = str(bundle.get("raw_output_policy") or "not_uploaded")
    source = bundle.get("source") if isinstance(bundle.get("source"), dict) else {}
    time_window = bundle.get("time_window") if isinstance(bundle.get("time_window"), dict) else {}
    source_name = str(source.get("service") or source.get("source_system") or "unknown")
    environment = str(source.get("environment") or "unknown")
    profile_name = str(source.get("profile_name") or "unknown")
    window_start = str(time_window.get("start") or "")
    window_end = str(time_window.get("end") or "")
    relationship = "Follow-up child bundle" if parent_sha else "Root evidence bundle"
    parent_panel = (
        f"""
        <div class="provenance-item">
          <h3>Parent Bundle</h3>
          <p>SHA: <code>{_html(parent_sha)}</code></p>
          <p>Evidence Request Plan: <code>{_html(plan_id or "none")}</code></p>
        </div>
        """
        if parent_sha
        else ""
    )
    return f"""
    <section class="provenance-panel" id="bundle-provenance">
      <h2>Bundle Provenance</h2>
      <p class="score-note">This section describes how the selected Evidence Bundle was produced. Follow-up child bundles are listed separately.</p>
      <div class="planner-grid">
        <div class="provenance-item">
          <h3>Selected Bundle</h3>
          <p>SHA: <code>{_html(sha)}</code></p>
          <p>Relationship: {_html(relationship)}</p>
          <p>Parent SHA: <code>{_html(parent_sha or "none")}</code></p>
          <p>Evidence Request Plan: <code>{_html(plan_id or "none")}</code></p>
        </div>
        <div class="provenance-item">
          <h3>Source Scope</h3>
          <p>Service: {_html(source_name)}</p>
          <p>Environment: {_html(environment)}</p>
          <p>Profile: {_html(profile_name)}</p>
          <p>Window: <code>{_html(window_start or "unknown")}</code> -> <code>{_html(window_end or "unknown")}</code></p>
        </div>
        <div class="provenance-item">
          <h3>Collection Policy</h3>
          <p>collection_mode: {_html(collection_mode or "uploaded_sanitized_bundle")}</p>
          <p>raw_output_policy: {_html(raw_output_policy)}</p>
          <p>sanitize_before_upload: {_html(str(bool(bundle.get("sanitize_before_upload"))).lower())}</p>
          <p>verify_sanitized_required: {_html(str(bool(bundle.get("verify_sanitized_required"))).lower())}</p>
        </div>
        {parent_panel}
      </div>
    </section>"""


def _follow_up_collections_panel(bundle: dict[str, Any] | None) -> str:
    if not isinstance(bundle, dict) or bundle.get("bundle_type") != "sanitized_evidence_bundle":
        return ""
    sha = str(bundle.get("evidence_sha256") or "")
    children = _child_bundle_summaries(sha)
    if not children:
        return ""
    child_rows = "".join(
        f"""
        <div class="follow-up-item">
          <h3>Child Evidence Bundle</h3>
          <p>SHA: <code>{_html(child.get("evidence_sha256") or "")}</code></p>
          <p>Plan: <code>{_html(child.get("evidence_request_plan_id") or "")}</code></p>
          <p>collection_mode: {_html(child.get("collection_mode") or "")}</p>
          <p>status: {_html(child.get("status") or "uploaded / validated")}</p>
        </div>
        """
        for child in children
    )
    return f"""
    <section class="follow-up-panel" id="follow-up-collections">
      <h2>Follow-up Collections</h2>
      <p class="score-note">These are sanitized child bundles collected after a More data decision or an evidence request plan.</p>
      <div class="planner-grid">{child_rows}</div>
    </section>"""


def _evidence_lineage_panel(bundle: dict[str, Any] | None) -> str:
    return _bundle_provenance_panel(bundle) + _follow_up_collections_panel(bundle)


def _bundle_lineage_summary(bundle: dict[str, Any]) -> dict[str, Any]:
    return {
        "parent_evidence_sha256": str(bundle.get("parent_evidence_sha256") or ""),
        "evidence_request_plan_id": str(bundle.get("evidence_request_plan_id") or ""),
        "collection_mode": str(bundle.get("collection_mode") or ""),
        "raw_output_policy": str(bundle.get("raw_output_policy") or ""),
        "sanitize_before_upload": bool(bundle.get("sanitize_before_upload")),
        "verify_sanitized_required": bool(bundle.get("verify_sanitized_required")),
        "child_bundle": bool(bundle.get("parent_evidence_sha256")),
    }


def _child_bundle_summaries(parent_sha: str) -> list[dict[str, Any]]:
    if not parent_sha:
        return []
    store = _store()
    method = getattr(store, "list_child_bundles", None)
    if method is None:
        return []
    try:
        return list(method(parent_sha, limit=12))
    except Exception:
        return []


def _human_count(value: int) -> str:
    return f"{int(value):,}"


def _remote_collector_enabled() -> bool:
    return os.environ.get("OES_REMOTE_COLLECTOR_ENABLED", "").casefold() in {"1", "true", "yes", "on"}


def _server_path_ingest_enabled() -> bool:
    return os.environ.get("OES_SERVER_PATH_INGEST_ENABLED", "").casefold() in {"1", "true", "yes", "on"}


def _truthy_env(key: str) -> bool:
    return os.environ.get(key, "").strip().casefold() in {"1", "true", "yes", "on"}


def _float_env(key: str, default: float) -> float:
    try:
        return float(os.environ.get(key, str(default)))
    except ValueError:
        return default


async def _write_guard_response(request: Request, request_id: str) -> JSONResponse | None:
    expected = os.environ.get("OES_API_WRITE_TOKEN", "").strip()
    if not expected or request.method.upper() in {"GET", "HEAD", "OPTIONS"}:
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
        for item in os.environ.get("OES_ALTERNATIVE_PROVIDERS", "gpt-oss,mistral").split(",")
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



def _canonical_review_graph_cards(graph: dict[str, Any]) -> str:
    if not isinstance(graph, dict) or graph.get("schema_version") != "canonical_review_graph.v1":
        return ""
    summary = graph.get("summary") if isinstance(graph.get("summary"), dict) else {}
    dimensions = graph.get("agreement_dimensions") if isinstance(graph.get("agreement_dimensions"), dict) else {}
    baseline = dimensions.get("baseline_agreement") if isinstance(dimensions.get("baseline_agreement"), dict) else {}
    technical_baseline = dimensions.get("technical_baseline_agreement") if isinstance(dimensions.get("technical_baseline_agreement"), dict) else {}
    incident_baseline = (
        dimensions.get("incident_baseline_agreement")
        if isinstance(dimensions.get("incident_baseline_agreement"), dict)
        else baseline
    )
    provider_overlap = (dimensions.get("provider_detection_overlap") or {}).get("value") or "0/0"
    review_unit_convergence = dimensions.get("review_unit_convergence") if isinstance(dimensions.get("review_unit_convergence"), dict) else {}
    graph_status = str(graph.get("canonical_graph_status") or graph.get("snapshot_status") or "computed_on_request")
    graph_sha = str(graph.get("canonical_graph_sha256") or "")
    fingerprint_sha = str(graph.get("current_input_fingerprint_sha256") or graph.get("input_fingerprint_sha256") or "")
    previous_fingerprint_sha = str(graph.get("persisted_input_fingerprint_sha256") or "")
    stale_reason = str(graph.get("stale_reason") or "")
    snapshot_created_at = str(graph.get("snapshot_created_at") or graph.get("persisted_created_at") or "")
    snapshot_created_label = (
        "Previous snapshot created at"
        if graph_status == "persisted" and stale_reason and graph.get("persisted_created_at") and not graph.get("snapshot_created_at")
        else "Snapshot created at"
    )
    if graph_status == "stale":
        stale_note = "<p class='score-note'><strong>Canonical graph is stale.</strong> The stored snapshot was generated from a different input fingerprint. The UI is showing a recomputed graph for the current inputs.</p>"
    elif graph_status == "persisted" and stale_reason:
        stale_note = (
            "<p class='score-note'><strong>Canonical graph was refreshed before persistence.</strong> "
            f"Previous snapshot reason: <code>{_html(stale_reason)}</code>. "
            f"Previous input fingerprint: <code>{_html(previous_fingerprint_sha or 'unknown')}</code>.</p>"
        )
    elif graph_status == "persisted":
        stale_note = "<p class='score-note'>Canonical graph loaded from persisted arbitration snapshot.</p>"
    else:
        stale_note = "<p class='score-note'>Canonical graph computed on request. Snapshot persistence is not available or no snapshot exists.</p>"
    technical_established = bool(technical_baseline.get("established"))
    incident_established = bool(incident_baseline.get("established"))
    if technical_established and incident_established:
        baseline_note = "Technical and incident baselines are established. Score is review priority, not truth probability."
    elif technical_established:
        baseline_note = "Technical baseline is established, but incident impact is not verified; keep impact validation separate from technical consensus."
    elif incident_established:
        baseline_note = "Incident baseline is established, but technical baseline is not established; review cause and instrumentation disagreements before treating this as technical consensus."
    else:
        baseline_note = "No baseline agreement is established; disputed claims remain validation targets."
    cause = (dimensions.get("cause_agreement") or {}).get("value") or "none"
    impact = (dimensions.get("impact_agreement") or {}).get("value") or "none"
    primary = [row for row in graph.get("primary_targets") or [] if isinstance(row, dict)]
    validation = [row for row in graph.get("validation_targets") or [] if isinstance(row, dict)]
    monitor = [row for row in graph.get("monitor_only") or [] if isinstance(row, dict)]
    archived = [row for row in graph.get("auto_archived") or [] if isinstance(row, dict)]
    decisions = [row for row in graph.get("promotion_decisions") or [] if isinstance(row, dict)]
    warnings = [row for row in graph.get("arbitration_warnings") or [] if isinstance(row, dict)]
    theme_rows = "".join(
        "<li>"
        f"<strong>{_html(str(row.get('theme') or ''))}</strong> "
        f"{int(row.get('group_count') or 0)} groups / "
        f"validation: <code>{_html(str(row.get('recommended_validation') or ''))}</code>"
        "</li>"
        for row in [item for item in graph.get("disagreement_themes") or [] if isinstance(item, dict)][:8]
    )
    warning_rows = "".join(
        f"<li><code>{_html(str(row.get('warning_type') or ''))}</code>: {_html(str(row.get('message') or ''))}</li>"
        for row in warnings[:8]
    )
    decision_rows = "".join(_promotion_decision_card(row) for row in decisions[:12])
    primary_cards = "".join(_canonical_target_card(row, role_label="Primary target", extra_class="primary-card") for row in primary)
    validation_cards = "".join(_canonical_target_card(row, role_label="Validation target", extra_class="") for row in validation[:20])
    monitor_note = f"<p class='score-note'>Monitor-only: {len(monitor)} / auto archived: {len(archived)}</p>"
    return f"""
<div class="review-graph canonical-review-graph">
  <section class="graph-group">
    <p class="graph-label">Canonical Review Graph</p>
    <article class="graph-overview">
      <div class="primary-node">
        <div>
          <h2 class="title">Review Target Arbitration</h2>
          <div class="pill-row">
            <span class="pill">Provider detection overlap: {_html(provider_overlap)}</span>
            <span class="pill">Technical baseline: {_html('established' if technical_baseline.get('established') else 'not established')}</span>
            <span class="pill">Incident baseline: {_html('established' if incident_baseline.get('established') else 'not established')}</span>
            <span class="pill">Review-unit convergence: {_html(review_unit_convergence.get('value') or 'none')}</span>
            <span class="pill">Cause agreement: {_html(cause)}</span>
            <span class="pill">Impact agreement: {_html(impact)}</span>
            <span class="pill">Status: {_html(graph_status)}</span>
          </div>
        </div>
        <div class="score">{int(summary.get('primary_count') or 0)}<span>Primary targets</span></div>
      </div>
      <p class="score-note">Arbitration version: <code>{_html(str(graph.get('arbitration_version') or graph.get('generated_by') or ''))}</code></p>
      <p class="score-note">Canonical graph SHA: <code>{_html(graph_sha)}</code></p>
      <p class="score-note">Input fingerprint: <code>{_html(fingerprint_sha)}</code></p>
      <p class="score-note">{_html(snapshot_created_label)}: {_html(snapshot_created_at or 'not persisted')}</p>
      {stale_note}
      <p class="score-note">{_html(baseline_note)}</p>
      <div class="summary">
        <div class="summary-item"><strong>{int(summary.get('primary_count') or 0)}</strong><span>Canonical primary</span></div>
        <div class="summary-item"><strong>{int(summary.get('validation_count') or 0)}</strong><span>Validation targets</span></div>
        <div class="summary-item"><strong>{int(summary.get('monitor_only_count') or 0)}</strong><span>Monitor only</span></div>
        <div class="summary-item"><strong>{int(summary.get('auto_archived_count') or 0)}</strong><span>Auto archived</span></div>
      </div>
    </article>
  </section>
  <section class="graph-group">
    <p class="graph-label">Disagreement Themes</p>
    <article class="card"><ul>{theme_rows or '<li>No disagreement themes.</li>'}</ul></article>
  </section>
  <section class="graph-group">
    <p class="graph-label">Primary targets</p>
    {primary_cards or '<section class="empty">No primary candidate was promoted by arbitration.</section>'}
  </section>
  <section class="graph-group">
    <p class="graph-label">Validation targets</p>
    {validation_cards or '<section class="empty">No validation targets.</section>'}
    {monitor_note}
  </section>
  <section class="graph-group">
    <p class="graph-label">Promotion decisions</p>
    <div class="cards">{decision_rows or '<section class="empty">No promotion decisions.</section>'}</div>
    {('<article class="card"><h3>Arbitration warnings</h3><ul>' + warning_rows + '</ul></article>') if warning_rows else ''}
  </section>
</div>"""


def _canonical_target_card(target: dict[str, Any], *, role_label: str, extra_class: str) -> str:
    score = float(target.get("review_priority_score") or 0.0)
    title = str(target.get("title") or "")
    if role_label == "Primary target":
        title = title.replace("Requires validation", "Candidate").replace("requires validation", "candidate")
    reasons = "; ".join(str(item) for item in list(target.get("promotion_blocked_reasons") or [])[:5])
    caps = ", ".join(
        f"{float(row.get('cap') or 0):.2f}:{row.get('reason')}"
        for row in target.get("score_caps_applied") or []
        if isinstance(row, dict)
    )
    rollup = target.get("rollup") if isinstance(target.get("rollup"), dict) else {}
    source_count = int(target.get("source_candidate_count") or rollup.get("source_candidate_count") or 1)
    provider_count = int(rollup.get("independent_provider_count") or target.get("provider_count") or 0)
    evidence_ref_count = int(rollup.get("evidence_ref_count") or len(target.get("evidence_refs") or []))
    breakdown = target.get("score_breakdown") if isinstance(target.get("score_breakdown"), dict) else {}
    convergence_bonus = float(breakdown.get("convergence_bonus") or rollup.get("priority_bonus") or 0.0)
    promotion_score = float(target.get("promotion_score") or target.get("review_priority_score") or 0.0)
    baseline_support_score = float(target.get("baseline_support_score") or rollup.get("baseline_support_score") or 0.0)
    convergence_text = (
        f"{source_count} candidates / {provider_count} providers / {evidence_ref_count} evidence refs; "
        f"+{convergence_bonus:.2f} priority; baseline support {baseline_support_score:.2f}"
    )
    return f"""
<article class="card {_html(extra_class)}" id="{_html(target.get('review_target_id') or target.get('target_id'))}">
  <div class="card-head">
    <div>
      <h2 class="title">[{_html(role_label)}] {_html(title)}</h2>
      <div class="pill-row">
        <span class="pill">Class: {_html(target.get('class') or '')}</span>
        <span class="pill">Support: {_html(target.get('support_role') or '')}</span>
        <span class="pill">Type: {_html(target.get('core_target_type') or '')}</span>
        <span class="pill">Theme: {_html(target.get('linked_disagreement_theme') or '')}</span>
        <span class="pill">Unit: {_html(target.get('canonical_review_unit') or '')}</span>
        <span class="pill">Sources: {_html(str(source_count))}</span>
        <span class="pill">Providers: {_html(str(provider_count))}</span>
      </div>
    </div>
    <div class="score">{score:.3f}<span>Review priority</span></div>
  </div>
  <div class="grid">
    <div class="field full"><label>Impact</label><p>{_html(target.get('impact_summary') or '')}</p></div>
    <div class="field"><label>Promotion gate</label><p>{_html(reasons or 'passed')}</p></div>
    <div class="field"><label>Score caps</label><p>{_html(caps or 'none')}</p></div>
    <div class="field"><label>Convergence</label><p>{_html(convergence_text)}</p></div>
    <div class="field"><label>Promotion score</label><p>{promotion_score:.3f}</p></div>
    <div class="field"><label>Recommended request</label><p><code>{_html(target.get('recommended_request_type') or '')}</code></p></div>
  </div>
  <div class="actions">
    <button onclick="showTarget('{_html(target.get('review_target_id') or target.get('target_id'))}')">Evidence details</button>
    <button class="primary" onclick="showTarget('{_html(target.get('review_target_id') or target.get('target_id'))}')">Review</button>
  </div>
</article>"""


def _promotion_decision_card(decision: dict[str, Any]) -> str:
    reasons = ", ".join(str(item) for item in decision.get("reasons") or [])
    caps = ", ".join(
        f"{float(row.get('cap') or 0):.2f}:{row.get('reason')}"
        for row in decision.get("score_caps_applied") or []
        if isinstance(row, dict)
    )
    review_score = float(decision.get("review_priority_score") or decision.get("score_after") or 0.0)
    convergence_bonus = float(decision.get("convergence_bonus") or 0.0)
    return f"""
<article class="validation-node">
  <div class="validation-node-head">
    <div>
      <h3 class="validation-title">{_html(decision.get('source_target_title') or decision.get('target_id') or '')}</h3>
      <div class="pill-row">
        <span class="pill">{_html(decision.get('original_class') or '')} -> {_html(decision.get('final_class') or '')}</span>
        <span class="pill">{_html(decision.get('decision') or '')}</span>
      </div>
    </div>
    <div class="node-score">{review_score:.3f}</div>
  </div>
  <p class="score-note">Promotion score: {float(decision.get('score_after') or 0.0):.3f}; convergence bonus: +{convergence_bonus:.3f}</p>
  <p class="score-note">Reasons: {_html(reasons or 'none')}</p>
  <p class="score-note">Score caps: {_html(caps or 'none')}</p>
</article>"""


def _review_graph_cards(targets: list[dict[str, Any]]) -> str:
    if not targets:
        return ""
    by_id = {str(target.get("review_target_id") or ""): target for target in targets}
    primary_targets = [
        target
        for target in targets
        if not target.get("parent_review_target_id")
        and str(target.get("review_mode") or "") == "incident_candidate"
    ]
    if not primary_targets:
        primary_targets = [
            target
            for target in targets
            if not target.get("parent_review_target_id")
        ][:1]
    rendered: set[str] = set()
    groups: list[str] = []
    for primary in primary_targets:
        primary_id = str(primary.get("review_target_id") or "")
        rendered.add(primary_id)
        related_ids = [
            str(row.get("review_target_id") or "")
            for row in primary.get("related_review_targets") or []
            if row.get("review_target_id")
        ]
        children = [
            by_id[child_id]
            for child_id in related_ids
            if child_id in by_id and child_id not in rendered
        ]
        children.extend(
            child
            for child in targets
            if str(child.get("parent_review_target_id") or "") == primary_id
            and str(child.get("review_target_id") or "") not in {str(item.get("review_target_id") or "") for item in children}
        )
        for child in children:
            rendered.add(str(child.get("review_target_id") or ""))
        child_nodes = "\n".join(_validation_target_node(child, compact=True) for child in children) or (
            "<section class='empty'>No validation targets linked to this primary target.</section>"
        )
        groups.append(
            f"""
<section class="graph-group">
  <p class="graph-label">Review structure</p>
  {_primary_graph_overview(primary, child_nodes)}
  <p class="graph-label">Primary target details</p>
  {_review_target_card(primary, role_label="Primary target", extra_class="primary-card")}
</section>"""
        )
    orphan_cards = [
        _review_target_card(target, role_label="Review", extra_class="")
        for target in targets
        if str(target.get("review_target_id") or "") not in rendered
    ]
    if orphan_cards:
        groups.append(
            "<section class='graph-group'><p class='graph-label'>Unlinked review targets</p>"
            + "\n".join(orphan_cards)
            + "</section>"
        )
    return "<div class='review-graph'>" + "\n".join(groups) + "</div>"


def _primary_graph_overview(primary: dict[str, Any], child_nodes: str) -> str:
    score = float(primary.get("review_priority_score") or 0.0)
    return f"""
<article class="graph-overview">
  <div class="primary-node">
    <div>
      <h2 class="title">[Primary target] {_html(primary.get('title') or '')}</h2>
      <div class="pill-row">
        <span class="pill">Type: {_html(primary.get('core_target_type') or primary.get('review_target_type') or '')}</span>
        <span class="pill">Profile: {_html((primary.get('profile') or {}).get('profile_id') or 'generic')}</span>
        <span class="pill">{_html(primary.get('subsystem') or 'general')}</span>
      </div>
    </div>
    <div class="score">{score:.3f}<span>Review priority</span></div>
  </div>
  <div class="validation-tree">
    <p class="graph-label">Validation targets</p>
    {child_nodes}
  </div>
</article>"""


def _validation_target_node(target: dict[str, Any], *, compact: bool = False) -> str:
    score = float(target.get("review_priority_score") or 0.0)
    why = "; ".join(str(item) for item in list(target.get("why_survived") or [])[:3])
    detail_html = "" if compact else f"""
  <div class="grid">
    <div class="field"><label>Purpose</label><p>{_html(target.get('core_claim') or '')}</p></div>
    <div class="field"><label>Why retained</label><p>{_html(why)}</p></div>
  </div>"""
    return f"""
<article class="validation-node" id="{_html(target.get('review_target_id'))}">
  <div class="validation-node-head">
    <div>
      <h3 class="validation-title">{_html(target.get('title') or '')}</h3>
      <div class="pill-row">
        <span class="pill">Type: {_html(target.get('core_target_type') or target.get('review_target_type') or '')}</span>
        <span class="pill">{_html(target.get('subsystem') or 'general')}</span>
        <span class="pill">{_html(target.get('relationship') or target.get('review_mode') or 'validation_target')}</span>
      </div>
    </div>
    <div class="node-score">{score:.3f}</div>
  </div>
  {detail_html}
  <div class="actions">
    <button onclick="showTarget('{_html(target.get('review_target_id'))}')">Evidence details</button>
    <button class="primary" onclick="showTarget('{_html(target.get('review_target_id'))}')">Review</button>
  </div>
</article>"""


def _review_target_card(target: dict[str, Any], *, role_label: str = "Review", extra_class: str = "") -> str:
    score = float(target.get("review_priority_score") or 0.0)
    priority = str(target.get("review_priority") or "low")
    breakdown = dict((target.get("score_breakdown") or {}).get("breakdown") or {})
    why_items = "".join(
        f"<li>{_html(_review_reason(reason))}</li>"
        for reason in list(target.get("why_survived") or [])[:5]
    )
    agreement = dict(target.get("model_agreement") or {})
    providers = list(agreement.get("providers") or [])
    provider_text = ", ".join(
        _html(
            " ".join(
                value
                for value in (
                    str(provider.get("provider") or ""),
                    str(provider.get("stance") or ""),
                )
                if value
            )
        )
        for provider in providers
    ) or _html(str(agreement.get("summary") or "No model agreement metadata recorded."))
    evidence_gap = str(agreement.get("evidence_gap") or "")
    model_agreement_html = (
        f"<p>{_html(_model_agreement_summary(agreement))}</p>"
        f"<p class='score-note'>{provider_text}</p>"
        + (f"<p class='score-note'>Evidence gap: {_html(evidence_gap)}</p>" if evidence_gap else "")
    )
    metric_cells = "\n".join(
        _metric_cell(label, breakdown.get(key))
        for label, key in (
            ("Evidence strength", "evidence_strength"),
            ("Actionability", "actionability"),
            ("Impact risk", "user_impact_risk"),
            ("Model detection", "model_detection_agreement"),
            ("Evidence diversity", "evidence_diversity"),
            ("History adjustment", "history_adjustment"),
            ("Missing evidence penalty", "missing_evidence_penalty"),
            ("Duplicate penalty", "duplicate_penalty"),
        )
    )
    priority_explain = _priority_explain_html(target, breakdown, providers)
    return f"""
<article class="card {_html(extra_class)}" id="{_html(target.get('review_target_id'))}">
  <div class="card-head">
    <div>
      <h2 class="title">[{_html(role_label)}] {_html(target.get('title') or '')}</h2>
      <div class="pill-row">
        <span class="pill priority-{_html(priority)}">{_html(_priority_label(priority))}</span>
        <span class="pill">Profile: {_html((target.get('profile') or {}).get('profile_id') or 'generic')}</span>
        <span class="pill">Type: {_html(target.get('core_target_type') or target.get('review_target_type') or '')}</span>
        <span class="pill">{_html(target.get('subsystem') or 'general')}</span>
        <span class="pill">{_html(target.get('cluster_id') or '')}</span>
      </div>
    </div>
    <div class="score">{score:.3f}<span>Review priority</span></div>
  </div>
  <div class="grid">
    <div class="field full"><label>Core claim</label><p>{_html(target.get('core_claim') or '')}</p></div>
    <div class="field"><label>Why retained</label><ul class="why">{why_items}</ul></div>
    <div class="field"><label>Model agreement</label>{model_agreement_html}</div>
    <div class="field"><label>Support evidence</label><p>{_html(target.get('support_summary') or '')}</p></div>
    <div class="field"><label>Counter / Caveat</label><p>{_html(target.get('counter_or_caveat_summary') or '')}</p></div>
    <div class="field full"><label>Proposal</label><div class="proposal-text">{_html(_proposal_text(target.get('proposal') or ''))}</div></div>
    <div class="field full"><label>Score breakdown</label><div class="breakdown">{metric_cells}</div></div>
  </div>
  {priority_explain}
  <div class="actions">
    <button onclick="showTarget('{_html(target.get('review_target_id'))}')">Evidence details</button>
    <button class="primary" onclick="showTarget('{_html(target.get('review_target_id'))}')">Review</button>
  </div>
</article>"""


def _metric_cell(label: str, value: Any) -> str:
    try:
        numeric = float(value)
        display = f"{numeric:.2f}"
    except (TypeError, ValueError):
        display = "0.00"
    return f"<div class='metric'><strong>{_html(display)}</strong><span>{_html(label)}</span></div>"


def _priority_explain_html(target: dict[str, Any], breakdown: dict[str, Any], providers: list[dict[str, Any]]) -> str:
    priority = str(target.get("review_priority") or "low")
    items = [
        ("Evidence strength", breakdown.get("evidence_strength")),
        ("Actionable", breakdown.get("actionability")),
        ("Impact", breakdown.get("user_impact_risk")),
    ]
    rows = []
    for label, value in items:
        try:
            display = f"{float(value):.2f}"
        except (TypeError, ValueError):
            display = "0.00"
        rows.append(f"<li>&#10003; {_html(label)}: {_html(display)}</li>")
    if len(providers) > 1:
        rows.append(f"<li>&#10003; Multiple models detected ({len(providers)} providers)</li>")
    else:
        rows.append("<li>Single-model detection recorded</li>")
    return (
        "<div class='priority-explain'>"
        f"<strong>Why priority is {_html(_priority_label(priority))}:</strong>"
        f"<ul>{''.join(rows)}</ul>"
        "</div>"
    )


def _priority_label(priority: str) -> str:
    return {
        "high": "high",
        "medium": "medium",
        "low": "low",
    }.get(str(priority or "").casefold(), str(priority or ""))


def _model_agreement_summary(agreement: dict[str, Any]) -> str:
    detected = int(agreement.get("detected_provider_count") or 0)
    total = int(agreement.get("total_provider_count") or 0)
    diversity = str(agreement.get("evidence_diversity_label") or "")
    if detected and total:
        return f"Model detection agreement: {detected}/{total}. Evidence diversity: {_diversity_label(diversity)}."
    return "No model agreement metadata recorded."


def _diversity_label(label: str) -> str:
    text = str(label or "").strip()
    if not text:
        return "not recorded"
    return text


def _review_reason(reason: object) -> str:
    text = str(reason or "")
    if text.startswith("linked to ") and text.endswith(" subsystem"):
        subsystem = text.removeprefix("linked to ").removesuffix(" subsystem")
        return f"linked to {subsystem} subsystem"
    if text.startswith("validation target for "):
        return "validation target for " + text.removeprefix("validation target for ")
    return text


def _proposal_text(text: object) -> str:
    return str(text or "")


def _json_for_script(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True).replace("<", "\\u003c").replace("</", "<\\/")


def _review_row(item: dict[str, Any]) -> str:
    priority = str(item.get("priority") or "low")
    score = float(item.get("review_priority_score") or 0.0)
    next_data = item.get("next_data_needed") or []
    actions = item.get("suggested_actions") or []
    action_text = ""
    if actions:
        first_action = actions[0]
        action_text = first_action.get("temporary_action") or first_action.get("permanent_action") or ""
        authority = first_action.get("required_authority") or ""
        if authority:
            action_text = f"{action_text} ({authority})" if action_text else authority
    proposal_text = action_text or "; ".join(next_data[:3])
    meta = " / ".join(
        value
        for value in (
            str(item.get("subsystem") or ""),
            str(item.get("cluster_id") or ""),
            str(item.get("model_provider") or ""),
        )
        if value
    )
    counts = (
        f"S:{int(item.get('support_count') or 0)} "
        f"C:{int(item.get('counter_count') or 0)} "
        f"M:{int(item.get('missing_evidence_count') or 0)} "
        f"E:{int(item.get('evidence_count') or 0)}"
    )
    return f"""
<tr>
  <td data-label="Target"><strong>{_html(item['service'])}</strong><br>{_html(item['window_start'])}<br>{_html(item['window_end'])}</td>
  <td data-label="Review target">{_html(item['question'])}<br><code>{_html(item['proposition_id'])}</code><br><code>{_html(meta)}</code><br><code>{_html(counts)}</code></td>
  <td data-label="Support">{_html(item.get('support_summary') or '')}</td>
  <td data-label="Counter / Caveat">{_html(item.get('counter_summary') or '')}<br>{_html('; '.join(item.get('validation_targets') or []))}</td>
  <td data-label="Proposal">{_html(proposal_text)}</td>
  <td data-label="Score"><span class="priority-{priority}">{_html(_priority_label(priority))}</span><br><span class="score">{score:.3f}</span></td>
  <td data-label="Actions"><div class="actions">
    <button onclick="review('{_html(item['proposition_id'])}', 'accept')">Accept</button>
    <button onclick="review('{_html(item['proposition_id'])}', 'reject')">Reject</button>
    <button onclick="review('{_html(item['proposition_id'])}', 'needs-more-data')">More data</button>
  </div></td>
</tr>"""


def _html(value: object) -> str:
    import html

    return html.escape(str(value), quote=True)
