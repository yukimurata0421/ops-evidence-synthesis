from __future__ import annotations

from typing import Any

from ops_evidence_synthesis.canonical import pretty_json
from ops_evidence_synthesis.evidence_rules import (
    ai_evidence_rules,
    ai_evidence_rules_text,
    evidence_request_planner_rules,
    profile_discovery_rules,
)
from ops_evidence_synthesis.synthesis.subsystems import OPS_SUBSYSTEMS


CLAIM_TYPES = (
    "support",
    "counter_evidence",
    "caveat",
    "validation_target",
    "next_data_needed",
    "insufficient_evidence",
)
FINDING_STATUSES = (
    "supported",
    "contradicted",
    "insufficient_evidence",
    "no_finding",
)
GENERIC_SUBSYSTEMS = (*OPS_SUBSYSTEMS, "general")
_SEVERITY_RANK = {
    "EMERGENCY": 70,
    "ALERT": 60,
    "CRITICAL": 50,
    "ERROR": 40,
    "WARN": 30,
    "WARNING": 30,
    "NOTICE": 25,
    "INFO": 20,
    "DEBUG": 10,
}


def root_cause_prompt(bundle: dict[str, Any]) -> str:
    return _prompt(
        bundle,
        agent_role="hypothesis_generator",
        role_instruction=(
            "Your role is to synthesize human-reviewable operational hypotheses "
            "from sanitized JSONL evidence. Focus on the strongest incident or "
            "reliability candidate, then identify missing evidence and validation targets."
        ),
    )


def alternative_hypothesis_prompt(bundle: dict[str, Any]) -> str:
    return _prompt(
        bundle,
        agent_role="alternative_hypothesis_generator",
        role_instruction=(
            "Your role is alternative hypothesis generation, not final judgement. "
            "Generate only hypotheses that are grounded in the evidence bundle. "
            "Prefer support/counter/caveat structure over broad narrative."
        ),
    )


def profile_discovery_prompt_rules() -> list[str]:
    return ai_evidence_rules() + profile_discovery_rules()


def evidence_request_planner_prompt_rules() -> list[str]:
    return ai_evidence_rules() + evidence_request_planner_rules()


def evidence_requirement_prompt(payload: dict[str, Any]) -> str:
    context = payload.get("requirement_context") if isinstance(payload.get("requirement_context"), dict) else payload
    return (
        "Return only valid JSON. Do not wrap the JSON in Markdown. "
        "Use exactly this top-level object shape: "
        '{"schema_version":"evidence_requirements.v1","requirements":[{'
        '"requirement_id":"REQ-...",'
        '"review_target_id":"...",'
        '"canonical_review_unit":"...",'
        '"blocked_reason":"...",'
        '"question_to_close":"...",'
        '"required_evidence":[{'
        '"evidence_type":"user_impact_signal|runtime_evidence|causal_alignment|instrumentation_consistency|external_dependency_status|evidence_identity|independent_corroboration|critical_outcome",'
        '"source_kind":"metric_or_log|runtime_state|source_context_metadata|human_answer|instrumentation_gap",'
        '"existing_signal_refs":[],'
        '"allowed_signal_names":[],'
        '"acceptance_criteria":"...",'
        '"rejection_criteria":"...",'
        '"collection_mode":"manual_read_only|profile_collector|source_first_metadata|not_collectible",'
        '"maps_to_request_type":"user_impact_signal_query|process_state_query|instrumentation_consistency_query|external_dependency_status_query|log_completeness_query|throughput_signal_query|freshness_signal_query|deployment_correlation_query"'
        '}],'
        '"do_not_request":[],'
        '"fallback_if_unavailable":"..."'
        '}]}. '
        "You are not the incident judge. Do not promote a primary incident and do not assert root cause truth. "
        "Translate deterministic promotion-blocked reasons into evidence requirements that a human can collect. "
        "Use only review_target_id values, evidence refs, and signal names listed in the input context. "
        "If a useful metric or log source is not listed, mark it as source_kind instrumentation_gap and do not invent a concrete metric name. "
        "Always include both acceptance_criteria and rejection_criteria so a child Evidence Bundle can close or weaken the gate. "
        "Human answers are context only; they are not runtime support evidence. "
        "Do not request raw secrets, raw env values, raw Authorization headers, raw Cookie values, token values, private key bodies, or credential files. "
        f"{ai_evidence_rules_text()} "
        f"Requirement context:\n{pretty_json(context)}"
    )


def _prompt(bundle: dict[str, Any], *, agent_role: str, role_instruction: str) -> str:
    if bundle.get("llm_task") == "evidence_requirement_planner":
        return evidence_requirement_prompt(bundle)
    subsystem_values = "|".join(GENERIC_SUBSYSTEMS)
    claim_values = "|".join(CLAIM_TYPES)
    finding_status_values = "|".join(FINDING_STATUSES)
    return (
        "Return only valid JSON. Do not wrap the JSON in Markdown. "
        "Use exactly this top-level object shape: "
        '{"schema_version":"claim-result/v1",'
        f'"agent_role":"{agent_role}",'
        f'"finding_status":"{finding_status_values}",'
        '"summary":"...",'
        f'"claims":[{{"claim_type":"{claim_values}",'
        f'"finding_status":"{finding_status_values}",'
        '"claim_text":"...",'
        f'"subsystem":"{subsystem_values}",'
        '"evidence_identity":{"program":"known|unknown","source":"known|unknown",'
        '"failure_signature":"known|unknown","time_window":"known|unknown"},'
        '"evidence_refs":[],'
        '"counter_evidence_refs":[],'
        '"caveats":[],'
        '"missing_evidence":[],'
        '"temporary_action":"...",'
        '"permanent_action":"...",'
        '"required_authority":"..."}],'
        '"propositions":[{"question":"...","subsystem":"...",'
        '"linked_claim_hints":[]}]}. '
        f"{role_instruction} "
        "Array fields must contain primitive strings only: evidence_refs, counter_evidence_refs, "
        "caveats, missing_evidence, and linked_claim_hints must never contain objects, nested claims, "
        "or dictionaries. If you want to express a caveat, put one concise sentence in the caveats "
        "string array or create a separate top-level claim with claim_type caveat. "
        "This system is not stream_v3-specific: the input is arbitrary sanitized JSONL "
        "normalized into an evidence bundle. Choose the closest generic subsystem. "
        "Use job_configuration when evidence shows a configured command, scheduled job, "
        "supervisor unit, or expected artifact is missing. Use runtime_recovery for "
        "restart loops or watchdog behavior without missing command evidence. Use "
        "observability_contract for log, metric, freshness, or health signal gaps. "
        "Use downstream_dependency for external service dependency behavior. "
        "Use general only when no listed subsystem fits. "
        "Treat agreement as a baseline and disagreement as a validation target, not as majority truth. "
        "System profile, operational contract, log source profile, metric semantics, component map, "
        "known benign noise, and action constraints are interpretation context only; they are not evidence. "
        "Do not cite context as support or counter-evidence. "
        f"{ai_evidence_rules_text()} "
        "Use only evidence_refs present in the bundle; do not invent evidence ids. "
        "Evidence signals and candidate targets are deterministic routing hints derived before AI synthesis; "
        "they are not primary evidence. When using a signal or candidate target, cite its evidence_ids, "
        "not the signal_id or target_id. "
        "Respect candidate target routing: primary candidates are incident hypotheses, validation targets are "
        "supporting, contradicting, or instrumentation checks, and monitor-only items should not become review proposals. "
        "If the evidence does not identify the responsible program/component, source, exact failure signature, "
        "or time window, set finding_status to insufficient_evidence for that claim, set the unknown "
        "evidence_identity fields to unknown, and do not create a supported review proposition from it. "
        "If the bundle contains no operational finding, use finding_status no_finding and explain the missing "
        "data through insufficient_evidence or next_data_needed claims. "
        "Assign exactly one subsystem per claim and do not mix unrelated subsystems in a single proposition. "
        "If a hypothesis needs validation, include missing_evidence and next_data_needed claims. "
        "Avoid 'No immediate action required' unless the evidence is explicitly normal, stable, or improved. "
        "For each support, counter_evidence, caveat, or validation_target claim, fill temporary_action, "
        "permanent_action, and required_authority with concrete human-reviewable recommendations. "
        "For next_data_needed claims, set temporary_action, permanent_action, and required_authority to empty strings; "
        "do not propose collection commands there. "
        "Do not reveal raw secrets or identifiers; preserve only sanitized evidence references.\n\n"
        f"Evidence bundle:\n{pretty_json(compact_bundle_for_model(bundle))}"
    )


def compact_bundle_for_model(
    bundle: dict[str, Any],
    *,
    max_evidence_items: int = 140,
    max_logs: int = 0,
    max_normalized_events: int = 0,
    max_text_chars: int = 480,
) -> dict[str, Any]:
    """Reduce an audit bundle to the evidence surface needed for model synthesis."""
    raw_evidence_items = [row for row in bundle.get("evidence_items") or [] if isinstance(row, dict)]
    selected_evidence_items = _top_evidence_items(raw_evidence_items, limit=max_evidence_items)
    log_patterns = [
        _compact_log_pattern(row, max_text_chars=max_text_chars)
        for row in _top_evidence_items(bundle.get("log_patterns") or [], limit=max_evidence_items)
    ]
    evidence_items = [
        _compact_evidence_item(row, max_text_chars=max_text_chars)
        for row in selected_evidence_items
    ]
    metric_windows = [_compact_metric_window(row) for row in bundle.get("metric_windows") or []]
    logs = [
        _compact_log(row, max_text_chars=max_text_chars)
        for row in _top_logs(bundle.get("logs") or [], limit=max_logs)
    ]
    operational_evidence = [
        _compact_operational_evidence(row, max_text_chars=max_text_chars)
        for row in bundle.get("operational_evidence") or []
        if isinstance(row, dict)
    ]
    evidence_signals = [
        _compact_signal(row, max_text_chars=max_text_chars)
        for row in bundle.get("evidence_signals") or []
        if isinstance(row, dict)
    ]
    local_first_signals = [
        _compact_local_first_signal(row)
        for row in bundle.get("signals") or []
        if isinstance(row, dict)
    ]
    candidate_targets = [
        _compact_candidate_target(row, max_text_chars=max_text_chars)
        for row in bundle.get("candidate_targets") or []
        if isinstance(row, dict)
    ]
    normalized_events = [
        _compact_normalized_event(row, max_text_chars=max_text_chars)
        for row in _top_logs(bundle.get("normalized_events") or [], limit=max_normalized_events)
    ]
    evidence_ids = _evidence_ids(evidence_items, log_patterns, metric_windows, logs, operational_evidence, normalized_events)
    evidence_refs = _compact_evidence_refs(
        bundle.get("evidence_refs") or {},
        evidence_ids=evidence_ids,
        max_text_chars=max_text_chars,
    )
    profile_context = _compact_profile_context(bundle, metric_windows=metric_windows, max_text_chars=max_text_chars)
    return {
        "schema_version": bundle.get("schema_version"),
        "bundle_type": bundle.get("bundle_type"),
        "evidence_sha256": bundle.get("evidence_sha256"),
        "source": bundle.get("source") or {},
        "service": bundle.get("service") or (bundle.get("source") or {}).get("service"),
        "environment": bundle.get("environment") or (bundle.get("source") or {}).get("environment"),
        "window_start": bundle.get("window_start") or (bundle.get("time_window") or {}).get("start"),
        "window_end": bundle.get("window_end") or (bundle.get("time_window") or {}).get("end"),
        "time_window": bundle.get("time_window") or {},
        "raw_log_policy": bundle.get("raw_log_policy"),
        "local_first_summary": bundle.get("local_first_summary") or {},
        "display_summary": bundle.get("display_summary") or {},
        "lookback_window_start": bundle.get("lookback_window_start"),
        "lookback_minutes": bundle.get("lookback_minutes"),
        **profile_context,
        "incident": bundle.get("incident") or {},
        "compression_note": (
            "Model input is compacted from the full audit bundle. Individual sanitized log lines and normalized events "
            "remain in storage by default; this prompt keeps a bounded high-signal sample of SQL-grouped log patterns, "
            "occurrence counts, first/last seen timestamps, baseline counts, metrics, operational evidence, and "
            "review-routing hints. Corpus-level counts describe the omitted low-signal patterns. Profile context is "
            "included only to interpret evidence, not to prove claims."
        ),
        "evidence_corpus_summary": _evidence_corpus_summary(raw_evidence_items, selected_evidence_items),
        "source_counts": {
            "full_evidence_refs": len(bundle.get("evidence_refs") or {}),
            "full_evidence_items": len(bundle.get("evidence_items") or []),
            "full_log_patterns": len(bundle.get("log_patterns") or []),
            "full_metric_windows": len(bundle.get("metric_windows") or []),
            "full_logs": len(bundle.get("logs") or []),
            "full_operational_evidence": len(bundle.get("operational_evidence") or []),
            "full_evidence_signals": len(bundle.get("evidence_signals") or []),
            "full_local_first_signals": len(bundle.get("signals") or []),
            "full_candidate_targets": len(bundle.get("candidate_targets") or []),
            "full_normalized_events": len(bundle.get("normalized_events") or []),
            "model_evidence_refs": len(evidence_refs),
            "model_evidence_items": len(evidence_items),
            "model_log_patterns": len(log_patterns),
            "model_metric_windows": len(metric_windows),
            "model_logs": len(logs),
            "model_operational_evidence": len(operational_evidence),
            "model_evidence_signals": len(evidence_signals),
            "model_local_first_signals": len(local_first_signals),
            "model_candidate_targets": len(candidate_targets),
            "model_normalized_events": len(normalized_events),
        },
        "review_graph_seed": bundle.get("review_graph_seed") or {},
        "evidence_items": evidence_items,
        "signals": local_first_signals,
        "evidence_signals": evidence_signals,
        "candidate_targets": candidate_targets,
        "log_patterns": log_patterns,
        "metric_windows": metric_windows,
        "operational_evidence": operational_evidence,
        "logs": logs,
        "normalized_events": normalized_events,
        "evidence_refs": evidence_refs,
    }


def _evidence_corpus_summary(raw_items: list[dict[str, Any]], selected_items: list[dict[str, Any]]) -> dict[str, Any]:
    total_item_count = len(raw_items)
    selected_ids = {str(item.get("evidence_id") or "") for item in selected_items}
    total_occurrences = sum(_safe_int(item.get("count"), default=1) for item in raw_items)
    selected_occurrences = sum(_safe_int(item.get("count"), default=1) for item in selected_items)
    severity_counts: dict[str, int] = {}
    selected_severity_counts: dict[str, int] = {}
    type_counts: dict[str, int] = {}
    selected_type_counts: dict[str, int] = {}
    for item in raw_items:
        severity = str(item.get("severity_text") or item.get("severity") or "unknown").lower()
        item_type = str(item.get("type") or item.get("event_type") or "unknown")
        severity_counts[severity] = severity_counts.get(severity, 0) + 1
        type_counts[item_type] = type_counts.get(item_type, 0) + 1
        if str(item.get("evidence_id") or "") in selected_ids:
            selected_severity_counts[severity] = selected_severity_counts.get(severity, 0) + 1
            selected_type_counts[item_type] = selected_type_counts.get(item_type, 0) + 1
    omitted_count = max(0, total_item_count - len(selected_items))
    return _drop_empty(
        {
            "full_evidence_item_count": total_item_count,
            "model_evidence_item_count": len(selected_items),
            "omitted_evidence_item_count": omitted_count,
            "full_occurrence_count": total_occurrences,
            "model_occurrence_count": selected_occurrences,
            "occurrence_coverage_ratio": round(selected_occurrences / total_occurrences, 6) if total_occurrences else 0,
            "severity_counts": severity_counts,
            "model_severity_counts": selected_severity_counts,
            "type_counts": type_counts,
            "model_type_counts": selected_type_counts,
            "selection_policy": (
                "Keep highest-severity, highest-count, and operationally interesting sanitized log patterns; "
                "omit low-signal tail patterns from provider prompts while preserving corpus counts."
            ),
        }
    )


def _compact_profile_context(
    bundle: dict[str, Any],
    *,
    metric_windows: list[dict[str, Any]],
    max_text_chars: int,
) -> dict[str, Any]:
    metric_names = {str(row.get("metric_name") or "") for row in metric_windows if row.get("metric_name")}
    return _drop_empty(
        {
        "profile": bundle.get("profile") or {},
        "profile_confidence": bundle.get("profile_confidence") or (bundle.get("source") or {}).get("profile_confidence"),
        "required_profile_questions": bundle.get("required_profile_questions") or [],
        "analysis_policy": bundle.get("analysis_policy") or {},
        "prompt_rules": bundle.get("prompt_rules") or bundle.get("ai_prompt_rules") or ai_evidence_rules(),
        "system_profile": _truncate_nested(bundle.get("system_profile") or {}, max_text_chars),
            "operational_contract": _truncate_nested(bundle.get("operational_contract") or {}, max_text_chars),
            "log_sources": _truncate_nested(bundle.get("log_sources") or [], max_text_chars),
            "metric_semantics": _compact_metric_semantics(bundle.get("metric_semantics") or {}, metric_names=metric_names),
            "component_map": _truncate_nested(bundle.get("component_map") or {}, max_text_chars),
            "known_benign_noise": _truncate_nested(bundle.get("known_benign_noise") or [], max_text_chars),
            "action_constraints": _truncate_nested(bundle.get("action_constraints") or [], max_text_chars),
            "review_policy": bundle.get("review_policy") or {},
            "runtime_ownership": _truncate_nested(bundle.get("runtime_ownership") or {}, max_text_chars),
            "primary_positive_evidence": _truncate_nested(bundle.get("primary_positive_evidence") or {}, max_text_chars),
            "failure_absence_evidence": _truncate_nested(bundle.get("failure_absence_evidence") or {}, max_text_chars),
            "classification_overrides": _truncate_nested(bundle.get("classification_overrides") or [], max_text_chars),
            "support_evidence_requirements": _truncate_nested(bundle.get("support_evidence_requirements") or {}, max_text_chars),
            "context_note": _truncate(bundle.get("context_note"), max_text_chars),
        }
    )


def _compact_log_pattern(row: dict[str, Any], *, max_text_chars: int) -> dict[str, Any]:
    return _drop_empty(
        {
            "pattern_id": row.get("pattern_id"),
            "error_type": row.get("error_type"),
            "count": row.get("count"),
            "baseline_count": row.get("baseline_count"),
            "severity_hint": row.get("severity_hint"),
            "first_seen": row.get("first_seen"),
            "last_seen": row.get("last_seen"),
            "message_template": _truncate(row.get("message_template"), max_text_chars),
            "example_log_sha256": row.get("example_log_sha256"),
            "aggregation_source": row.get("aggregation_source"),
        }
    )


def _compact_evidence_item(row: dict[str, Any], *, max_text_chars: int) -> dict[str, Any]:
    return _drop_empty(
        {
            "evidence_id": row.get("evidence_id"),
            "type": row.get("type"),
            "event_type": row.get("event_type"),
            "severity_text": row.get("severity_text"),
            "count": row.get("count"),
            "first_seen": row.get("first_seen"),
            "last_seen": row.get("last_seen"),
            "message_template": _truncate(row.get("message_template"), max_text_chars),
            "example_sanitized": _truncate(row.get("example_sanitized"), max_text_chars),
            "component": row.get("component"),
            "source": row.get("source"),
        }
    )


def _compact_metric_window(row: dict[str, Any]) -> dict[str, Any]:
    return _drop_empty(
        {
            "metric_window_id": row.get("metric_window_id"),
            "metric_name": row.get("metric_name"),
            "baseline_value": row.get("baseline_value"),
            "current_value": row.get("current_value"),
            "delta": row.get("delta"),
            "delta_pct": row.get("delta_pct"),
            "severity_hint": row.get("severity_hint"),
            "window_start": row.get("window_start"),
            "window_end": row.get("window_end"),
        }
    )


def _compact_log(row: dict[str, Any], *, max_text_chars: int) -> dict[str, Any]:
    return _drop_empty(
        {
            "evidence_id": row.get("evidence_id"),
            "timestamp": row.get("timestamp"),
            "severity": row.get("severity"),
            "error_type": row.get("error_type"),
            "message_sanitized": _truncate(row.get("message_sanitized"), max_text_chars),
            "message_template": _truncate(row.get("message_template"), max_text_chars),
            "raw_log_sha256": row.get("raw_log_sha256"),
            "resource_type": row.get("resource_type"),
        }
    )


def _compact_normalized_event(row: dict[str, Any], *, max_text_chars: int) -> dict[str, Any]:
    return _drop_empty(
        {
            "event_id": row.get("event_id"),
            "timestamp": row.get("timestamp"),
            "service": row.get("service"),
            "environment": row.get("environment"),
            "component": row.get("component"),
            "severity": row.get("severity"),
            "event_type": row.get("event_type"),
            "message_sanitized": _truncate(row.get("message_sanitized"), max_text_chars),
        }
    )


def _compact_operational_evidence(row: dict[str, Any], *, max_text_chars: int) -> dict[str, Any]:
    return _drop_empty(
        {
            "evidence_id": row.get("evidence_id"),
            "request_id": row.get("request_id"),
            "profile_request_id": row.get("profile_request_id"),
            "subsystem": row.get("subsystem"),
            "request_type": row.get("request_type"),
            "summary": _truncate(row.get("summary"), max_text_chars),
            "incident_count": row.get("incident_count"),
            "baseline_count": row.get("baseline_count"),
            "baseline_daily_average": row.get("baseline_daily_average"),
            "sample_count": row.get("sample_count") or len(row.get("samples") or []),
            "observations": _truncate_nested(row.get("observations") or [], max_text_chars),
        }
    )


def _compact_signal(row: dict[str, Any], *, max_text_chars: int) -> dict[str, Any]:
    return _drop_empty(
        {
            "signal_id": row.get("signal_id"),
            "signal_type": row.get("signal_type"),
            "core_target_type": row.get("core_target_type"),
            "routing_hint": row.get("routing_hint"),
            "subsystem": row.get("subsystem"),
            "evidence_ids": row.get("evidence_ids") or [],
            "summary": _truncate(row.get("summary"), max_text_chars),
            "metric_name": row.get("metric_name"),
            "severity": row.get("severity"),
            "score_hint": row.get("score_hint"),
            "attributes": _truncate_nested(row.get("attributes") or {}, max_text_chars),
        }
    )


def _compact_local_first_signal(row: dict[str, Any]) -> dict[str, Any]:
    return _drop_empty(
        {
            "signal_id": row.get("signal_id"),
            "signal_type": row.get("signal_type"),
            "core_target_type": row.get("core_target_type"),
            "evidence_refs": row.get("evidence_refs") or [],
            "component": row.get("component"),
            "count": row.get("count"),
            "confidence": row.get("confidence"),
        }
    )


def _compact_candidate_target(row: dict[str, Any], *, max_text_chars: int) -> dict[str, Any]:
    return _drop_empty(
        {
            "target_id": row.get("target_id"),
            "core_target_type": row.get("core_target_type"),
            "title": row.get("title"),
            "subsystem": row.get("subsystem"),
            "review_mode": row.get("review_mode"),
            "relationship": row.get("relationship"),
            "parent_target_id": row.get("parent_target_id"),
            "signal_ids": row.get("signal_ids") or [],
            "support_evidence_refs": row.get("support_evidence_refs") or [],
            "missing_evidence": _truncate_nested(row.get("missing_evidence") or [], max_text_chars),
            "evidence_request_types": row.get("evidence_request_types") or [],
            "core_claim": _truncate(row.get("core_claim"), max_text_chars),
            "routing_reason": _truncate_nested(row.get("routing_reason") or [], max_text_chars),
            "score_hint": row.get("score_hint"),
        }
    )


def _compact_evidence_refs(
    refs: dict[str, Any],
    *,
    evidence_ids: set[str],
    max_text_chars: int,
) -> dict[str, Any]:
    compact: dict[str, Any] = {}
    for evidence_id in sorted(evidence_ids):
        value = refs.get(evidence_id)
        if not isinstance(value, dict):
            continue
        compact[evidence_id] = _drop_empty(
            {
                "type": value.get("type"),
                "summary": _truncate(value.get("summary"), max_text_chars),
                "timestamp": value.get("timestamp"),
                "metric_name": value.get("metric_name"),
                "baseline_value": value.get("baseline_value"),
                "current_value": value.get("current_value"),
                "delta": value.get("delta"),
                "delta_pct": value.get("delta_pct"),
                "count": value.get("count"),
                "baseline_count": value.get("baseline_count"),
                "first_seen": value.get("first_seen"),
                "last_seen": value.get("last_seen"),
                "severity_hint": value.get("severity_hint"),
                "aggregation_source": value.get("aggregation_source"),
                "subsystem": value.get("subsystem"),
                "source": value.get("source"),
                "request_id": value.get("request_id"),
                "profile_request_id": value.get("profile_request_id"),
                "request_need": value.get("request_need"),
                "request_description": value.get("request_description"),
            }
        )
    return compact


def _top_logs(rows: list[dict[str, Any]], *, limit: int) -> list[dict[str, Any]]:
    if limit <= 0:
        return []
    return sorted(
        rows,
        key=lambda row: (
            -_SEVERITY_RANK.get(str(row.get("severity") or "").upper(), 0),
            str(row.get("timestamp") or ""),
            str(row.get("evidence_id") or row.get("event_id") or ""),
        ),
    )[:limit]


def _top_evidence_items(rows: list[dict[str, Any]], *, limit: int) -> list[dict[str, Any]]:
    typed_rows = [row for row in rows if isinstance(row, dict)]
    if limit <= 0 or len(typed_rows) <= limit:
        return typed_rows
    return sorted(
        typed_rows,
        key=lambda row: (
            -_SEVERITY_RANK.get(str(row.get("severity_text") or row.get("severity") or "").upper(), 0),
            -_safe_int(row.get("count"), default=1),
            -_interesting_evidence_score(row),
            str(row.get("first_seen") or row.get("timestamp") or ""),
            str(row.get("evidence_id") or ""),
        ),
    )[:limit]


def _interesting_evidence_score(row: dict[str, Any]) -> int:
    text = " ".join(
        str(row.get(key) or "")
        for key in ("event_type", "message_template", "example_sanitized", "component", "source")
    ).casefold()
    score = 0
    groups = (
        ("failure", "failed", "error", "exception", "retry", "alert", "critical"),
        ("token", "auth", "ready", "checkpoint"),
        ("run_result", "run_once", "processed", "matched", "notified", "non_target"),
        ("systemd", "watchdog", "service", "deactivated", "heartbeat"),
        ("pubsub", "streamingpull", "idle"),
    )
    for index, terms in enumerate(groups, start=1):
        if any(term in text for term in terms):
            score += 10 - index
    return score


def _safe_int(value: Any, *, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _evidence_ids(*groups: list[dict[str, Any]]) -> set[str]:
    ids: set[str] = set()
    for group in groups:
        for row in group:
            for key in ("evidence_id", "pattern_id", "metric_window_id", "event_id"):
                value = str(row.get(key) or "").strip()
                if value:
                    ids.add(value)
    return ids


def _compact_metric_semantics(semantics: dict[str, Any], *, metric_names: set[str]) -> dict[str, Any]:
    if not isinstance(semantics, dict):
        return {}
    names = [name for name in sorted(metric_names) if name in semantics]
    if not names and len(semantics) <= 30:
        names = sorted(str(name) for name in semantics)
    return {
        name: semantics[name]
        for name in names[:30]
        if isinstance(semantics.get(name), dict)
    }


def _truncate_nested(value: Any, max_chars: int) -> Any:
    if isinstance(value, dict):
        return {key: _truncate_nested(row, max_chars) for key, row in value.items()}
    if isinstance(value, list):
        return [_truncate_nested(row, max_chars) for row in value[:30]]
    if isinstance(value, str):
        return _truncate(value, max_chars)
    return value


def _truncate(value: Any, max_chars: int) -> str:
    text = str(value or "")
    if len(text) <= max_chars:
        return text
    return text[: max(0, max_chars - 3)] + "..."


def _drop_empty(row: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in row.items() if value not in (None, "", [], {})}


def claim_result_response_schema() -> dict[str, Any]:
    string_array = {"type": "ARRAY", "items": {"type": "STRING"}}
    claim = {
        "type": "OBJECT",
        "properties": {
            "claim_type": {"type": "STRING", "enum": list(CLAIM_TYPES)},
            "claim_text": {"type": "STRING"},
            "subsystem": {"type": "STRING", "enum": list(GENERIC_SUBSYSTEMS)},
            "evidence_refs": string_array,
            "counter_evidence_refs": string_array,
            "caveats": string_array,
            "missing_evidence": string_array,
            "temporary_action": {
                "type": "STRING",
                "description": "Concrete reversible mitigation or investigation action for humans to consider.",
            },
            "permanent_action": {
                "type": "STRING",
                "description": "Concrete durable fix, instrumentation, guardrail, or runbook update.",
            },
            "required_authority": {
                "type": "STRING",
                "description": "Human role that should approve or perform the action.",
            },
        },
        "required": [
            "claim_type",
            "claim_text",
            "evidence_refs",
            "subsystem",
            "counter_evidence_refs",
            "caveats",
            "missing_evidence",
            "temporary_action",
            "permanent_action",
            "required_authority",
        ],
        "propertyOrdering": [
            "claim_type",
            "claim_text",
            "subsystem",
            "evidence_refs",
            "counter_evidence_refs",
            "caveats",
            "missing_evidence",
            "temporary_action",
            "permanent_action",
            "required_authority",
        ],
    }
    proposition = {
        "type": "OBJECT",
        "properties": {
            "question": {"type": "STRING"},
            "subsystem": {"type": "STRING"},
            "linked_claim_hints": string_array,
        },
        "required": ["question", "subsystem", "linked_claim_hints"],
        "propertyOrdering": ["question", "subsystem", "linked_claim_hints"],
    }
    return {
        "type": "OBJECT",
        "properties": {
            "schema_version": {"type": "STRING"},
            "agent_role": {"type": "STRING"},
            "summary": {"type": "STRING"},
            "claims": {"type": "ARRAY", "items": claim},
            "propositions": {"type": "ARRAY", "items": proposition},
        },
        "required": ["schema_version", "agent_role", "summary", "claims", "propositions"],
        "propertyOrdering": ["schema_version", "agent_role", "summary", "claims", "propositions"],
    }


def evidence_requirements_response_schema() -> dict[str, Any]:
    string_array = {"type": "ARRAY", "items": {"type": "STRING"}}
    required_evidence = {
        "type": "OBJECT",
        "properties": {
            "evidence_type": {"type": "STRING"},
            "source_kind": {"type": "STRING"},
            "existing_signal_refs": string_array,
            "allowed_signal_names": string_array,
            "acceptance_criteria": {"type": "STRING"},
            "rejection_criteria": {"type": "STRING"},
            "collection_mode": {"type": "STRING"},
            "maps_to_request_type": {"type": "STRING"},
        },
        "required": [
            "evidence_type",
            "source_kind",
            "existing_signal_refs",
            "allowed_signal_names",
            "acceptance_criteria",
            "rejection_criteria",
            "collection_mode",
            "maps_to_request_type",
        ],
        "propertyOrdering": [
            "evidence_type",
            "source_kind",
            "existing_signal_refs",
            "allowed_signal_names",
            "acceptance_criteria",
            "rejection_criteria",
            "collection_mode",
            "maps_to_request_type",
        ],
    }
    requirement = {
        "type": "OBJECT",
        "properties": {
            "requirement_id": {"type": "STRING"},
            "review_target_id": {"type": "STRING"},
            "canonical_review_unit": {"type": "STRING"},
            "blocked_reason": {"type": "STRING"},
            "question_to_close": {"type": "STRING"},
            "required_evidence": {"type": "ARRAY", "items": required_evidence},
            "do_not_request": string_array,
            "fallback_if_unavailable": {"type": "STRING"},
        },
        "required": [
            "requirement_id",
            "review_target_id",
            "canonical_review_unit",
            "blocked_reason",
            "question_to_close",
            "required_evidence",
            "do_not_request",
            "fallback_if_unavailable",
        ],
        "propertyOrdering": [
            "requirement_id",
            "review_target_id",
            "canonical_review_unit",
            "blocked_reason",
            "question_to_close",
            "required_evidence",
            "do_not_request",
            "fallback_if_unavailable",
        ],
    }
    return {
        "type": "OBJECT",
        "properties": {
            "schema_version": {"type": "STRING"},
            "requirements": {"type": "ARRAY", "items": requirement},
        },
        "required": ["schema_version", "requirements"],
        "propertyOrdering": ["schema_version", "requirements"],
    }
