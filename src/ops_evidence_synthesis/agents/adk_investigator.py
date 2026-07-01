from __future__ import annotations

from collections.abc import Iterable
import os
from typing import Any

from ops_evidence_synthesis.canonical import sha256_json


ADK_AGENT_NAME = "ops_evidence_investigator"
DEFAULT_ADK_MODEL = "gemini-3.1-pro-preview"
ADK_TRACE_SCHEMA_VERSION = "adk_tool_trace.v1"

INVESTIGATION_AGENT_INSTRUCTION = """
You are the Ops Evidence Synthesis investigation orchestrator.
Use tools to inspect only sanitized evidence summaries, provider status metadata,
review target projections, source-context hashes, and human-review gates.
Do not ask for raw logs, raw source, credentials, secrets, restarts, rollbacks,
or destructive operations. Source Context and Source Analysis are interpretation
context only; runtime support must cite Evidence Item IDs. If incident impact or
causality is not established, stop at a human-review gate and request more
evidence instead of asserting truth.
""".strip()


def build_investigation_agent(model_name: str = DEFAULT_ADK_MODEL) -> Any:
    """Build the ADK Agent object used by Agent Runtime deployments."""
    try:
        from google.adk.agents import Agent
    except Exception as exc:  # pragma: no cover - exercised only with optional deps absent
        raise RuntimeError("Install the 'agent' extra to build the ADK Agent") from exc

    return Agent(
        model=model_name,
        name=ADK_AGENT_NAME,
        instruction=INVESTIGATION_AGENT_INSTRUCTION,
        tools=[
            freeze_evidence_bundle,
            attach_sanitized_source_context,
            run_cross_check_providers,
            chunk_and_merge_full_corpus,
            validate_citations,
            compute_review_targets,
            arbitrate_review_gate,
            request_more_evidence,
            draft_system_profile,
            deliver_read_only_review,
        ],
    )


def build_investigation_app(
    model_name: str = DEFAULT_ADK_MODEL,
    *,
    project_id: str = "",
    location: str = "",
) -> Any:
    """Build a Vertex AI Agent Runtime compatible AdkApp."""
    try:
        import vertexai
        from vertexai.agent_engines import AdkApp
    except Exception as exc:  # pragma: no cover - exercised only with optional deps absent
        raise RuntimeError("Install the 'agent' extra to build the Agent Runtime AdkApp") from exc
    project = (
        project_id
        or os.environ.get("OES_VERTEX_PROJECT")
        or os.environ.get("GOOGLE_CLOUD_PROJECT")
        or os.environ.get("GCP_PROJECT")
        or ""
    )
    if not project:
        raise RuntimeError("OES_VERTEX_PROJECT or GOOGLE_CLOUD_PROJECT is required to build AdkApp")
    vertexai.init(project=project, location=location or os.environ.get("OES_VERTEX_LOCATION", "global"))
    return AdkApp(agent=build_investigation_agent(model_name=model_name))


def adk_dependency_status() -> dict[str, Any]:
    """Return whether the optional local ADK and Agent Runtime dependencies import."""
    status: dict[str, Any] = {
        "schema_version": "adk_dependency_status.v1",
        "google_adk": {"available": False, "error": ""},
        "vertexai_agent_engines": {"available": False, "error": ""},
    }
    try:
        import google.adk  # type: ignore  # noqa: F401

        status["google_adk"]["available"] = True
    except Exception as exc:
        status["google_adk"]["error"] = f"{type(exc).__name__}: {exc}"
    try:
        from vertexai.agent_engines import AdkApp  # type: ignore  # noqa: F401

        status["vertexai_agent_engines"]["available"] = True
    except Exception as exc:
        status["vertexai_agent_engines"]["error"] = f"{type(exc).__name__}: {exc}"
    status["available"] = bool(
        status["google_adk"]["available"] and status["vertexai_agent_engines"]["available"]
    )
    return status


def freeze_evidence_bundle(
    evidence_sha256: str,
    log_count: int,
    raw_log_policy: str = "not_uploaded",
) -> dict[str, Any]:
    """Confirm that sanitized evidence is fixed before AI review.

    Args:
        evidence_sha256: SHA256 of the sanitized Evidence Bundle.
        log_count: Number of sanitized log rows represented by the bundle.
        raw_log_policy: Policy describing whether raw logs were uploaded.

    Returns:
        Tool result summarizing the immutable evidence boundary.
    """
    short_sha = str(evidence_sha256 or "")[:12]
    return {
        "status": "completed",
        "artifact": "evidence_bundle",
        "evidence_sha256": str(evidence_sha256 or ""),
        "log_count": int(log_count or 0),
        "raw_log_policy": str(raw_log_policy or "not_uploaded"),
        "summary": (
            f"Evidence Bundle {short_sha} fixed {int(log_count or 0):,} sanitized rows; "
            f"raw_log_policy={raw_log_policy or 'not_uploaded'}."
        ),
    }


def attach_sanitized_source_context(
    source_context_attached: bool,
    source_analysis_attached: bool,
    source_context_sha256: str = "",
    source_analysis_sha256: str = "",
) -> dict[str, Any]:
    """Attach sanitized source context as interpretation context, not evidence."""
    attached = bool(source_context_attached or source_analysis_attached)
    summary = (
        "Sanitized Source Context and Source Analysis are available as non-evidence interpretation context."
        if attached
        else "No sanitized Source Context was attached to this review payload."
    )
    return {
        "status": "completed" if attached else "skipped",
        "artifact": "source_context",
        "source_context_attached": bool(source_context_attached),
        "source_analysis_attached": bool(source_analysis_attached),
        "source_context_sha256": str(source_context_sha256 or ""),
        "source_analysis_sha256": str(source_analysis_sha256 or ""),
        "context_is_not_incident_evidence": True,
        "summary": summary,
    }


def run_cross_check_providers(provider_statuses: list[dict[str, Any]]) -> dict[str, Any]:
    """Summarize provider execution and schema validation results."""
    rows = [row for row in provider_statuses if isinstance(row, dict)]
    valid_rows = [
        row
        for row in rows
        if str(row.get("status") or "") == "ok" and bool(row.get("schema_valid"))
    ]
    provider_ids = [str(row.get("provider_id") or "") for row in rows if row.get("provider_id")]
    gemini_ids = [provider_id for provider_id in provider_ids if "gemini" in provider_id]
    return {
        "status": "completed",
        "artifact": "model_runs",
        "provider_count": len(rows),
        "schema_valid_provider_count": len(valid_rows),
        "provider_ids": provider_ids,
        "gemini_reference_present": bool(gemini_ids),
        "summary": (
            f"{len(valid_rows)}/{len(rows)} provider outputs were schema-valid; "
            f"Gemini reference/arbiter present={bool(gemini_ids)}. "
            "A silent provider position is preserved as evidence for review, not treated as failure."
        ),
    }


def chunk_and_merge_full_corpus(
    analysis_context: dict[str, Any],
    provider_statuses: list[dict[str, Any]],
) -> dict[str, Any]:
    """Summarize chunked full-corpus provider execution and deterministic merge scope."""
    context = analysis_context if isinstance(analysis_context, dict) else {}
    providers = [row for row in provider_statuses if isinstance(row, dict)]
    full_items = int(context.get("provider_full_corpus_evidence_items") or context.get("evidence_item_count") or 0)
    analyzed_items = int(context.get("provider_full_corpus_analyzed_evidence_items") or 0)
    chunk_count = int(context.get("provider_full_corpus_chunk_count") or 0)
    manifest_count = int(context.get("provider_full_corpus_chunk_manifest_count") or 0)
    unassigned = int(context.get("provider_full_corpus_unassigned_evidence_items") or 0)
    failed_chunks = sum(int(row.get("chunk_failure_count") or 0) for row in providers)
    coverage_ratio = float(context.get("provider_full_corpus_coverage_ratio") or 0.0)
    status = "completed" if chunk_count and analyzed_items else "skipped"
    return {
        "status": status,
        "artifact": "chunked_provider_merge",
        "full_evidence_items": full_items,
        "analyzed_evidence_items": analyzed_items,
        "coverage_ratio": coverage_ratio,
        "chunk_count": chunk_count,
        "chunk_manifest_count": manifest_count,
        "unassigned_evidence_items": unassigned,
        "failed_chunk_count": failed_chunks,
        "determinism_scope": {
            "provider_outputs": "recorded_and_hashed",
            "merge": "deterministic_sort_dedup_over_recorded_chunk_outputs",
            "fixture_regeneration": "deterministic_local_provider_ci",
        },
        "summary": (
            f"Provider prompts covered {analyzed_items:,}/{full_items:,} Evidence Item(s) "
            f"through up to {chunk_count:,} chunk(s); recorded chunk outputs are sorted and "
            "deduplicated before canonical merge."
            if status == "completed"
            else "No full-corpus chunk manifest was attached to this payload."
        ),
    }


def validate_citations(targets: list[dict[str, Any]]) -> dict[str, Any]:
    """Validate that review targets remain tied to sanitized evidence refs."""
    rows = [row for row in targets if isinstance(row, dict)]
    cited_refs = sorted(
        {
            str(ref)
            for row in rows
            for ref in row.get("evidence_refs") or []
            if str(ref or "").strip()
        }
    )
    chunk_tracked_total = sum(
        max(_safe_int(row.get("evidence_ref_total_count")), len(row.get("evidence_refs") or []))
        for row in rows
    )
    missing_count = sum(len(row.get("missing_evidence") or []) for row in rows)
    uncited_targets = [
        str(row.get("review_target_id") or row.get("target_id") or "")
        for row in rows
        if not row.get("evidence_refs")
    ]
    return {
        "status": "completed",
        "artifact": "citation_validation",
        "target_count": len(rows),
        "cited_evidence_ref_count": len(cited_refs),
        "directly_cited_evidence_ref_count": len(cited_refs),
        "chunk_tracked_evidence_ref_total_count": chunk_tracked_total,
        "missing_evidence_count": missing_count,
        "uncited_target_ids": [target_id for target_id in uncited_targets if target_id][:20],
        "summary": (
            f"{len(rows)} review target(s) directly show {len(cited_refs)} unique cited Evidence Item(s); "
            f"chunk manifests track {chunk_tracked_total} target evidence association(s); "
            f"{missing_count} missing-evidence item(s) remain."
        ),
    }


def _safe_int(value: object) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def compute_review_targets(
    review_graph_summary: dict[str, Any],
    targets: list[dict[str, Any]],
) -> dict[str, Any]:
    """Compute review target counts and convergence from the canonical graph projection."""
    rows = [row for row in targets if isinstance(row, dict)]
    summary = review_graph_summary if isinstance(review_graph_summary, dict) else {}
    primary_count = int(summary.get("primary_promoted_count") or summary.get("primary_count") or 0)
    validation_count = int(
        summary.get("validation_count")
        or summary.get("targets_total", len(rows)) - primary_count
        or 0
    )
    convergence_count = int(summary.get("convergence_count") or 0)
    return {
        "status": "completed",
        "artifact": "canonical_review_graph",
        "targets_total": int(summary.get("targets_total") or len(rows)),
        "primary_count": primary_count,
        "validation_count": max(0, validation_count),
        "convergence_count": convergence_count,
        "summary": (
            f"Canonical Review Graph projected {int(summary.get('targets_total') or len(rows))} target(s), "
            f"{primary_count} primary candidate(s), and {convergence_count} convergence group(s)."
        ),
    }


def arbitrate_review_gate(
    review_graph_summary: dict[str, Any],
    targets: list[dict[str, Any]],
) -> dict[str, Any]:
    """Decide whether the agent can stop or must request human review."""
    rows = [row for row in targets if isinstance(row, dict)]
    summary = review_graph_summary if isinstance(review_graph_summary, dict) else {}
    incident_baseline = str(summary.get("incident_baseline") or "open")
    blocked = [
        str((row.get("promotion") or {}).get("blocked_reason") or "")
        for row in rows
        if isinstance(row.get("promotion"), dict)
        and str((row.get("promotion") or {}).get("blocked_reason") or "")
    ]
    requires_human_review = incident_baseline != "established" or bool(blocked)
    return {
        "status": "human_gate" if requires_human_review else "completed",
        "artifact": "review_arbitration",
        "incident_baseline": incident_baseline,
        "blocked_reasons": sorted(set(blocked))[:20],
        "requires_human_review": requires_human_review,
        "summary": (
            "Final incident causality remains human-gated; the agent stops with review targets."
            if requires_human_review
            else "Incident promotion gate is closed by the provided graph summary."
        ),
    }


def request_more_evidence(targets: list[dict[str, Any]]) -> dict[str, Any]:
    """Request additional read-only evidence for unresolved review targets."""
    rows = [row for row in targets if isinstance(row, dict)]
    request_types = sorted(
        {
            str(row.get("recommended_request_type") or "")
            for row in rows
            if str(row.get("recommended_request_type") or "").strip()
        }
    )
    missing = [
        str(item)
        for row in rows
        for item in row.get("missing_evidence") or []
        if str(item or "").strip()
    ]
    return {
        "status": "completed" if request_types or missing else "skipped",
        "artifact": "evidence_request_plan",
        "request_types": request_types,
        "missing_evidence_count": len(missing),
        "summary": (
            f"Generated read-only follow-up plan with {len(request_types)} request type(s) "
            f"and {len(missing)} missing-evidence item(s), grouped for top-N human review."
        ),
    }


def draft_system_profile(profile_generation: dict[str, Any] | None = None) -> dict[str, Any]:
    """Summarize the optional system-profile draft or approved-profile gate."""
    generation = profile_generation if isinstance(profile_generation, dict) else {}
    mode = str(generation.get("generation_mode") or "not_run")
    approved = bool(generation.get("approved"))
    explicit = bool(generation.get("explicit_profile"))
    profile_id = str(generation.get("profile_id") or "")
    has_profile_context = mode != "not_run" or approved or explicit or bool(profile_id)
    llm_status = str(generation.get("llm_status") or ("persisted" if has_profile_context else "not_run"))
    required_decisions = [
        str(item)
        for item in generation.get("required_human_decisions") or []
        if str(item or "").strip()
    ]
    human_questions = [
        str(item)
        for item in generation.get("human_questions") or []
        if str(item or "").strip()
    ]
    confidence_action = str(generation.get("confidence_action") or "")
    confidence_summary = generation.get("confidence_summary") if isinstance(generation.get("confidence_summary"), dict) else {}
    if explicit:
        summary = (
            "Approved profile context is attached before review; it remains interpretation context "
            "and runtime claims still require Evidence Item IDs."
        )
    elif has_profile_context:
        summary = (
            "A system profile draft or profile context is attached from sanitized discovery and "
            "stops at a human-review gate before incident judgement."
        )
    else:
        summary = "No system profile draft or approved profile context is embedded in this review payload."
    return {
        "status": "human_gate" if has_profile_context else "skipped",
        "artifact": "profile_draft",
        "generation_mode": mode,
        "llm_status": llm_status,
        "approved": approved,
        "explicit_profile": explicit,
        "profile_id": profile_id,
        "component_count": int(generation.get("component_count") or 0),
        "metric_semantics_count": int(generation.get("metric_semantics_count") or 0),
        "collector_mapping_count": int(generation.get("collector_mapping_count") or 0),
        "profile_status": str(generation.get("profile_status") or ""),
        "confidence_summary": dict(confidence_summary),
        "confidence_action": confidence_action,
        "confirmed_user_outcomes": list(generation.get("confirmed_user_outcomes") or []),
        "provisional_user_outcomes": list(generation.get("provisional_user_outcomes") or []),
        "human_questions": human_questions[:8],
        "profile_to_review_links": list(generation.get("profile_to_review_links") or [])[:6],
        "required_human_decisions": required_decisions[:8],
        "summary": summary,
    }


def deliver_read_only_review(
    evidence_sha256: str,
    provider_mode: str,
    public_url: str = "",
) -> dict[str, Any]:
    """Record delivery of a read-only review artifact without live model work on GET."""
    short_sha = str(evidence_sha256 or "")[:12]
    return {
        "status": "completed",
        "artifact": "precomputed_review_summary",
        "evidence_sha256": str(evidence_sha256 or ""),
        "provider_mode": str(provider_mode or ""),
        "public_url": str(public_url or ""),
        "summary": (
            f"Read-only review payload for {short_sha} is served from precomputed JSON; "
            "page load does not invoke live model work."
        ),
    }


def build_adk_tool_contract_trace(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Build the deterministic ADK tool trace used by public review payloads."""
    summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
    generation = payload.get("generation") if isinstance(payload.get("generation"), dict) else {}
    analysis_context = payload.get("analysis_context") if isinstance(payload.get("analysis_context"), dict) else {}
    source_obs = " ".join(str(item) for item in analysis_context.get("source_observations") or [])
    evidence_sha256 = str(payload.get("evidence_sha256") or "")
    log_count = int(summary.get("log_count") or analysis_context.get("db_ingested_log_count") or 0)
    raw_log_policy = str(summary.get("raw_log_policy") or generation.get("raw_log_policy") or "not_uploaded")
    source_context_sha = _extract_hash(source_obs, "source_context_sha256=")
    source_analysis_sha = _extract_hash(source_obs, "analysis_sha256=")
    source_context_attached = bool(source_context_sha or "source context" in source_obs.casefold())
    source_analysis_attached = bool(source_analysis_sha or "source analysis" in source_obs.casefold())
    provider_statuses = [row for row in payload.get("provider_statuses") or [] if isinstance(row, dict)]
    targets = [row for row in payload.get("targets") or [] if isinstance(row, dict)]
    review_graph_summary = payload.get("review_graph_summary") if isinstance(payload.get("review_graph_summary"), dict) else {}
    profile_generation = (
        payload.get("profile_draft_generation")
        if isinstance(payload.get("profile_draft_generation"), dict)
        else payload.get("profile_generation")
    )
    if not isinstance(profile_generation, dict):
        profile_context = payload.get("profile_context") if isinstance(payload.get("profile_context"), dict) else {}
        profile_id = str(analysis_context.get("profile_id") or profile_context.get("profile_id") or "")
        if profile_id:
            confidence_summary = (
                profile_context.get("confidence_summary")
                if isinstance(profile_context.get("confidence_summary"), dict)
                else {}
            )
            profile_generation = {
                "generation_mode": "approved_profile_context",
                "llm_status": "persisted",
                "approved": True,
                "explicit_profile": True,
                "profile_id": profile_id,
                "component_count": int(profile_context.get("component_count") or 0),
                "metric_semantics_count": int(profile_context.get("metric_semantics_count") or 0),
                "collector_mapping_count": int(profile_context.get("collector_mapping_count") or 0),
                "profile_status": str(profile_context.get("profile_status") or ""),
                "confidence_summary": dict(confidence_summary),
                "confidence_action": str(profile_context.get("confidence_action") or ""),
                "confirmed_user_outcomes": list(profile_context.get("confirmed_user_outcomes") or []),
                "provisional_user_outcomes": list(profile_context.get("provisional_user_outcomes") or []),
                "human_questions": list(profile_context.get("human_questions") or []),
                "profile_to_review_links": list(profile_context.get("profile_to_review_links") or []),
                "required_human_decisions": list(profile_context.get("required_human_decisions") or []),
            }
    tool_results = [
        (
            "freeze_evidence_bundle",
            "Freeze Evidence Bundle",
            freeze_evidence_bundle(evidence_sha256, log_count, raw_log_policy),
        ),
        (
            "attach_sanitized_source_context",
            "Attach Sanitized Source Context",
            attach_sanitized_source_context(
                source_context_attached=source_context_attached,
                source_analysis_attached=source_analysis_attached,
                source_context_sha256=source_context_sha,
                source_analysis_sha256=source_analysis_sha,
            ),
        ),
        (
            "run_cross_check_providers",
            "Run Cross-Check Providers",
            run_cross_check_providers(provider_statuses),
        ),
        (
            "chunk_and_merge_full_corpus",
            "Chunk And Merge Full Corpus",
            chunk_and_merge_full_corpus(analysis_context, provider_statuses),
        ),
        (
            "validate_citations",
            "Validate Citations",
            validate_citations(targets),
        ),
        (
            "compute_review_targets",
            "Compute Review Targets",
            compute_review_targets(review_graph_summary, targets),
        ),
        (
            "arbitrate_review_gate",
            "Arbitrate Human Gate",
            arbitrate_review_gate(review_graph_summary, targets),
        ),
        (
            "request_more_evidence",
            "Request More Evidence",
            request_more_evidence(targets),
        ),
        (
            "draft_system_profile",
            "Draft System Profile",
            draft_system_profile(profile_generation if isinstance(profile_generation, dict) else None),
        ),
        (
            "deliver_read_only_review",
            "Deliver Read-Only Review",
            deliver_read_only_review(
                evidence_sha256=evidence_sha256,
                provider_mode=str(generation.get("provider_mode") or ""),
                public_url=str(payload.get("public_url") or ""),
            ),
        ),
    ]
    trace = [_trace_step(tool_name, title, result) for tool_name, title, result in tool_results]
    trace_hash = sha256_json(
        {
            "schema_version": ADK_TRACE_SCHEMA_VERSION,
            "tools": [
                {
                    "tool": step["tool"],
                    "status": step["status"],
                    "output": step["output"],
                }
                for step in trace
            ],
        }
    )
    for step in trace:
        step["trace_sha256"] = trace_hash
    return trace


def trace_from_adk_events(events: Iterable[Any]) -> list[dict[str, Any]]:
    """Convert live ADK event dictionaries into the public agent_trace shape."""
    steps: list[dict[str, Any]] = []
    for event in events:
        row = _to_plain(event)
        if not isinstance(row, dict):
            continue
        content = row.get("content") if isinstance(row.get("content"), dict) else {}
        for part in content.get("parts") or []:
            if not isinstance(part, dict):
                continue
            function_call = part.get("function_call")
            function_response = part.get("function_response")
            if isinstance(function_call, dict):
                name = str(function_call.get("name") or "function_call")
                steps.append(
                    {
                        "schema_version": ADK_TRACE_SCHEMA_VERSION,
                        "trace_source": "adk_event_stream",
                        "adk_agent_name": ADK_AGENT_NAME,
                        "adk_event_type": "function_call",
                        "step": name,
                        "tool": name,
                        "title": _title_from_tool(name),
                        "status": "called",
                        "artifact": f"adk:function_call:{name}",
                        "summary": f"ADK requested tool {name}.",
                        "input": _compact_value(function_call.get("args") or {}),
                    }
                )
            if isinstance(function_response, dict):
                name = str(function_response.get("name") or "function_response")
                response = function_response.get("response")
                steps.append(
                    {
                        "schema_version": ADK_TRACE_SCHEMA_VERSION,
                        "trace_source": "adk_event_stream",
                        "adk_agent_name": ADK_AGENT_NAME,
                        "adk_event_type": "function_response",
                        "step": name,
                        "tool": name,
                        "title": _title_from_tool(name),
                        "status": str((response or {}).get("status") or "completed")
                        if isinstance(response, dict)
                        else "completed",
                        "artifact": f"adk:function_response:{name}",
                        "summary": str((response or {}).get("summary") or f"ADK received response from {name}.")
                        if isinstance(response, dict)
                        else f"ADK received response from {name}.",
                        "output": _compact_value(response),
                    }
                )
    return steps


def _trace_step(tool_name: str, title: str, result: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": ADK_TRACE_SCHEMA_VERSION,
        "trace_source": "adk_tool_contract",
        "adk_agent_name": ADK_AGENT_NAME,
        "adk_event_type": "function_call_and_response",
        "step": tool_name,
        "tool": tool_name,
        "title": title,
        "status": str(result.get("status") or "completed"),
        "artifact": f"adk:tool:{tool_name}",
        "summary": str(result.get("summary") or ""),
        "output": _compact_value(result),
    }


def _extract_hash(text: str, marker: str) -> str:
    if marker not in text:
        return ""
    value = text.split(marker, 1)[1].split()[0].strip(".,;")
    return value if len(value) >= 12 else ""


def _title_from_tool(name: str) -> str:
    return " ".join(part.capitalize() for part in str(name or "").split("_"))


def _compact_value(value: Any, *, max_items: int = 20, max_text_chars: int = 240) -> Any:
    plain = _to_plain(value)
    if isinstance(plain, dict):
        return {
            str(key): _compact_value(row, max_items=max_items, max_text_chars=max_text_chars)
            for key, row in list(plain.items())[:max_items]
        }
    if isinstance(plain, list):
        return [_compact_value(row, max_items=max_items, max_text_chars=max_text_chars) for row in plain[:max_items]]
    if isinstance(plain, str) and len(plain) > max_text_chars:
        return plain[: max(0, max_text_chars - 3)] + "..."
    return plain


def _to_plain(value: Any) -> Any:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, dict):
        return {key: _to_plain(row) for key, row in value.items()}
    if isinstance(value, list):
        return [_to_plain(row) for row in value]
    if hasattr(value, "model_dump"):
        return _to_plain(value.model_dump(mode="json"))
    if hasattr(value, "to_dict"):
        return _to_plain(value.to_dict())
    if hasattr(value, "__dict__"):
        return {
            key: _to_plain(row)
            for key, row in vars(value).items()
            if not key.startswith("_")
        }
    return str(value)
