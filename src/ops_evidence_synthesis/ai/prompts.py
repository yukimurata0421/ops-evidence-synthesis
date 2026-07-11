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


def alternative_hypothesis_prompt(
    bundle: dict[str, Any],
    *,
    max_evidence_items: int = 140,
    max_logs: int = 0,
    max_normalized_events: int = 0,
    max_text_chars: int = 480,
) -> str:
    return _prompt(
        bundle,
        agent_role="alternative_hypothesis_generator",
        role_instruction=(
            "Your role is alternative hypothesis generation, not final judgement. "
            "Generate only hypotheses that are grounded in the evidence bundle. "
            "Prefer support/counter/caveat structure over broad narrative."
        ),
        max_evidence_items=max_evidence_items,
        max_logs=max_logs,
        max_normalized_events=max_normalized_events,
        max_text_chars=max_text_chars,
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


def profile_draft_prompt(payload: dict[str, Any]) -> str:
    discovery = payload.get("profile_discovery") if isinstance(payload.get("profile_discovery"), dict) else payload
    return (
        "Return only valid JSON. Do not wrap the JSON in Markdown. "
        "Use exactly this top-level object shape: "
        '{"schema_version":"profile_draft_ai.v1",'
        '"system_type":"...",'
        '"purpose":"...",'
        '"critical_outcomes":["..."],'
        '"components":[{"component_id":"...","name":"...","role":"...",'
        '"subsystem":"...","core_target_types":["..."]}],'
        '"metric_semantics":[{"metric_name":"...","semantic_type":"heartbeat|throughput|error|warning|latency|freshness|resource|configuration_contract|candidate",'
        '"zero_behavior":"healthy|suspicious|neutral|unknown",'
        '"increase_behavior":"healthy|suspicious|neutral|unknown",'
        '"decrease_behavior":"healthy|suspicious|neutral|unknown",'
        '"subsystem":"...","core_target_type":"..."}],'
        '"log_sources":[{"source_id":"...","description":"..."}],'
        '"collector_mappings":[{"request_type":"...","candidate_collectors":["..."],'
        '"safety_level":"read_only","params":{}}],'
        '"known_benign_noise":["..."],'
        '"action_constraints":["..."],'
        '"assumptions":["..."],'
        '"required_human_decisions":["..."]}. '
        "You are creating a Profile Draft from sanitized Source Context, Source Analysis, "
        "and sanitized log-observed entities. Analyze the sanitized code/config-derived "
        "project entities, entrypoints, systemd units, logger mappings, metric candidates, "
        "collector candidates, and observed log entities. Do not infer runtime incident truth. "
        "This draft explains what the system is, which user outcomes matter, which components "
        "exist, what metrics probably mean, which log sources are relevant, and which read-only "
        "collector mappings a human may approve. "
        "The draft is not an explicit profile until human approval. Keep every component, "
        "metric semantic, and collector mapping reviewable. "
        "Use only names, metrics, components, log sources, and collector candidates present in "
        "the sanitized input. Do not invent concrete metric names, paths, endpoints, commands, "
        "credentials, env values, or private identifiers. "
        "Collector mappings must be read-only. Do not propose write actions, restarts, rollbacks, "
        "credential changes, or raw data collection. "
        "Source Context and Source Analysis are context, not incident evidence. Runtime support "
        "claims later must still cite Evidence Items with evidence_id. "
        f"{ai_evidence_rules_text()} "
        "If a critical user outcome is plausible but not proven, include it as an assumption or "
        "required human decision, not as a fact. "
        f"Profile Discovery Bundle:\n{pretty_json(compact_profile_discovery_for_model(discovery))}"
    )


def focused_operational_profile_prompt(payload: dict[str, Any]) -> str:
    return (
        "Return only valid JSON. Do not wrap the JSON in Markdown. "
        "Use exactly this top-level object shape: "
        '{"schema_version":"focused_operational_profile.v1",'
        '"system_label":"...",'
        '"system_summary":{"system_type":"...","primary_purpose":"...",'
        '"logged_subject":"...","operational_boundary":"...","confidence":0.0},'
        '"runtime_components":[{"component_id":"...","name":"...","role":"...",'
        '"evidence_refs":[],"source_context_refs":[],"confidence":0.0}],'
        '"observability_contract":{"logs":[{"source":"...","meaning":"...",'
        '"evidence_refs":[],"source_context_refs":[]}],'
        '"metrics":[{"metric_name":"...","meaning":"...",'
        '"healthy_direction":"increase|decrease|stable|nonzero|zero|unknown",'
        '"evidence_refs":[],"source_context_refs":[]}],'
        '"heartbeats":[{"name":"...","meaning":"...","evidence_refs":[],"source_context_refs":[]}],'
        '"state_files":[{"name":"...","meaning":"...","source_context_refs":[]}]},'
        '"orchestration_flows":[{"flow_name":"...","trigger":"...","steps":["..."],'
        '"owned_by_components":[],"evidence_refs":[],"source_context_refs":[],"confidence":0.0}],'
        '"failure_modes":[{"failure_mode":"...","observable_signals":[],"missing_evidence":[],"confidence":0.0}],'
        '"read_only_collectors":[{"collector":"...","purpose":"...","safety_level":"read_only"}],'
        '"profile_limits":{"source_context_is_incident_evidence":false,'
        '"runtime_claims_require_evidence_id":true,'
        '"approval_required_before_explicit_profile":true,'
        '"raw_source_sent_to_provider":false,"raw_logs_sent_to_provider":false,'
        '"notes":["..."]},'
        '"human_review_required":["..."]}. '
        "Create a focused operational profile, not an incident diagnosis and not a broad inventory. "
        "Answer these operational questions: what system is this, what is being logged or measured, "
        "which components matter at runtime, and what orchestration or watchdog loop is visible. "
        "Prefer 5 to 12 high-value runtime components. Do not exhaustively list every identifier. "
        "Prefer concrete service units, workers, watchdogs, collectors, media/process runners, queues, "
        "publishers, schedulers, and recovery controllers over helper libraries. "
        "Preserve ownership boundaries. Distinguish the component that owns a runtime process from a "
        "monitor, classifier, dashboard, or remote requester that only observes it. If the sanitized "
        "context says a monitoring plane may request, propose, stage, or coordinate recovery but does "
        "not own the runtime process, do not describe that flow as executing the runtime action. Use "
        "request/propose/coordinate/validate wording instead. Only use execute/restart/kill wording "
        "when the same sanitized context shows the component owns that process or action primitive. "
        "Avoid ambiguous phrases such as 'execute recovery plan' or 'execute ExecutionPlan' for "
        "cross-plane monitoring flows. Prefer 'emit an action plan', 'render an action plan', "
        "'validate an action gate', 'request the allowlisted executor', or 'hand off to the runtime owner'. "
        "In monitoring-plane profiles, delivery components such as media runners, encoders, and audio "
        "workers should be described as observed dependencies unless they are actually owned in that scope. "
        "If the context separates delivery/runtime, observability/control, source-chain, and public "
        "snapshot/publication planes, keep those as separate operational boundaries. "
        "Treat dashboard FAIL/CHANGED, stale metrics, report-missing signals, and public snapshot gaps "
        "as observability candidates unless fresh runtime evidence connects them to delivery impact. "
        "The input contains only sanitized artifacts: Profile Discovery, optional sanitized Evidence Bundle, "
        "optional sanitized Source Context, and optional sanitized Source Analysis. Analyze sanitized code/config "
        "context when it is present, but do not treat it as runtime evidence. Runtime claims must cite evidence_id "
        "or pattern_id values from Evidence Bundle surfaces where available. Code/config claims should cite source "
        "item ids, config ids, systemd unit names, analysis candidate ids, or safe names from source context. "
        "If runtime evidence and source analysis disagree, keep the disagreement explicit in failure_modes or "
        "human_review_required. Do not invent concrete metric names, paths, hostnames, endpoints, env values, "
        "credentials, private identifiers, or commands. Read-only collectors only. Do not propose restarts, "
        "write operations, credential rotation, rollback, deletion, or raw data exfiltration. "
        "Keep user-impact or business-impact statements as assumptions unless the runtime evidence proves them. "
        f"{ai_evidence_rules_text()} "
        "Focused profile input:\n"
        f"{pretty_json(compact_focused_profile_input_for_model(payload))}"
    )


def profile_review_normalization_prompt(payload: dict[str, Any]) -> str:
    return (
        "Return only valid JSON. Do not wrap the JSON in Markdown. "
        "Translate the human review answers into a candidate patch for the focused operational profile. "
        "The candidate is not approval; a human will inspect and accept or edit it. "
        "Use exactly this top-level object shape: "
        '{"schema_version":"operational_profile_review_patch.v1",'
        '"system_summary_overrides":{"primary_purpose":"","logged_subject":"","operational_boundary":""},'
        '"metric_semantics_overrides":[{"metric_name":"...","meaning":"...",'
        '"healthy_direction":"increase|decrease|stable|nonzero|zero|unknown",'
        '"zero_behavior":"healthy|suspicious|neutral|unknown",'
        '"increase_behavior":"healthy|suspicious|neutral|unknown",'
        '"decrease_behavior":"healthy|suspicious|neutral|unknown",'
        '"reason":"...","provenance":"human_answer"}],'
        '"component_role_overrides":[{"component_id":"...","role":"...","reason":"...","provenance":"human_answer"}],'
        '"log_source_overrides":[{"source":"...","meaning":"...","reason":"...","provenance":"human_answer"}],'
        '"confirmed_user_outcomes":["..."],'
        '"ignored_component_ids":["..."],'
        '"approved_collectors":["..."],'
        '"unresolved_questions":[{"question":"...","reason":"..."}]}. '
        "Only emit changes directly supported by a human answer. Do not silently accept the Gemini draft. "
        "Use only metric names, component ids, log source names, and collector names present in focused_profile. "
        "Never invent an identifier, path, endpoint, command, credential, environment value, or runtime fact. "
        "If an answer is ambiguous, preserve it in unresolved_questions and leave the corresponding override empty. "
        "If the human says that zero is good, map zero_behavior to healthy. If zero means missing liveness or stopped work, "
        "map zero_behavior to suspicious. Do not infer this direction without an explicit answer. "
        "System purpose and user outcomes are human-approved interpretation context, not incident evidence. "
        "Do not propose write actions, restarts, rollback, deletion, credential changes, or raw data collection. "
        f"Profile review input:\n{pretty_json(payload)}"
    )


def _prompt(
    bundle: dict[str, Any],
    *,
    agent_role: str,
    role_instruction: str,
    max_evidence_items: int = 140,
    max_logs: int = 0,
    max_normalized_events: int = 0,
    max_text_chars: int = 480,
) -> str:
    if bundle.get("llm_task") == "evidence_requirement_planner":
        return evidence_requirement_prompt(bundle)
    if bundle.get("llm_task") == "focused_operational_profile":
        return focused_operational_profile_prompt(bundle)
    if bundle.get("llm_task") == "profile_draft":
        return profile_draft_prompt(bundle)
    if bundle.get("llm_task") == "profile_review_normalization":
        return profile_review_normalization_prompt(bundle)
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
        '"suspected_issue":"...",'
        '"operational_mechanism":"...",'
        '"why_it_matters":"...",'
        '"evidence_summary":["..."],'
        '"counter_evidence_summary":["..."],'
        '"why_not_promoted":"...",'
        '"next_validation_question":"...",'
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
        "Analyze the sanitized operational log evidence in evidence_items, log_patterns, "
        "metric_windows, operational_evidence, and evidence_refs. These are the only runtime "
        "evidence surfaces you may cite. "
        "If sanitized Source Context or Source Analysis is present, use it only to interpret "
        "component names, metric semantics, logger mappings, collector mappings, entrypoints, "
        "deployment/config context, and profile mapping. Do not treat source context as runtime "
        "incident evidence. Runtime support claims must still cite Evidence Items. "
        "Array fields must contain primitive strings only: evidence_refs, counter_evidence_refs, "
        "caveats, missing_evidence, and linked_claim_hints must never contain objects, nested claims, "
        "or dictionaries. If you want to express a caveat, put one concise sentence in the caveats "
        "string array or create a separate top-level claim with claim_type caveat. "
        "This system is not tied to a single source project: the input is arbitrary sanitized JSONL "
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
        "Every Evidence ID named in evidence_summary, counter_evidence_summary, claim_text, caveats, "
        "or missing_evidence must also appear in evidence_refs or counter_evidence_refs as a primitive string; "
        "never cite Evidence IDs only inside prose fields. "
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
        "For each support, counter_evidence, caveat, or validation_target claim, fill these review-explanation fields in English: "
        "suspected_issue is the concrete issue a human should inspect, not just a subsystem label; "
        "operational_mechanism explains which job, worker, scheduler, watchdog, config surface, dependency, or orchestration step could connect the evidence to the suspected issue; "
        "why_it_matters states the reliability or user/operational outcome that could be affected, while marking user impact as unverified unless runtime evidence proves it; "
        "evidence_summary must contain concise bullets that name the cited Evidence IDs and explain what each cited ref shows; "
        "counter_evidence_summary must list contradictory, weak, silent, or missing signals; "
        "When the input is one chunk of a larger Evidence Bundle, absence from the current chunk is not proof of "
        "absence from the full bundle or corpus. Never claim that a signal is absent from the bundle or corpus based "
        "only on the current chunk; describe it as not cited in this chunk and request cross-chunk validation instead. "
        "why_not_promoted must explain why this is not a root-cause finding yet; "
        "next_validation_question must be the single read-only question that would promote, weaken, or close the target. "
        "For each support, counter_evidence, caveat, or validation_target claim, fill temporary_action, "
        "permanent_action, and required_authority with concrete human-reviewable recommendations. "
        "For next_data_needed claims, set temporary_action, permanent_action, and required_authority to empty strings; "
        "do not propose collection commands there. "
        "Do not reveal raw secrets or identifiers; preserve only sanitized evidence references.\n\n"
        "The compacted evidence bundle may be provider-budgeted; use its corpus summary "
        "to understand omitted lower-signal evidence counts. "
        "Approved profile context is sanitized code/config interpretation, not incident evidence: "
        "use confirmed profile fields only for routing and vocabulary, treat provisional_user_outcomes "
        "as human-gated assumptions, and convert unanswered human_questions into missing_evidence "
        "or next_validation_question entries instead of promoting root cause. "
        f"Evidence bundle:\n{pretty_json(compact_bundle_for_model(bundle, max_evidence_items=max_evidence_items, max_logs=max_logs, max_normalized_events=max_normalized_events, max_text_chars=max_text_chars))}"
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
            "remain in storage by default; a single prompt keeps a bounded high-signal sample of SQL-grouped log patterns, "
            "occurrence counts, first/last seen timestamps, baseline counts, metrics, operational evidence, and "
            "review-routing hints. Multi-provider synthesis covers every Evidence Item by chunking the corpus before "
            "provider calls. Corpus-level counts describe any single-prompt projection. Profile context is "
            "included only to interpret evidence, not to prove claims."
        ),
        "evidence_corpus_summary": _evidence_corpus_summary(raw_evidence_items, selected_evidence_items),
        "db_corpus_coverage": _compact_db_corpus_coverage(bundle.get("db_corpus_coverage") or {}),
        "source_counts": {
            "db_corpus_rows": _safe_int((bundle.get("db_corpus_coverage") or {}).get("total_row_count"), default=0),
            "db_corpus_covered_rows": _safe_int((bundle.get("db_corpus_coverage") or {}).get("covered_row_count"), default=0),
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
        "source_context": _compact_source_context(bundle.get("source_context_context") or {}, max_text_chars=max_text_chars),
        "source_analysis": _compact_source_analysis(bundle.get("source_analysis_context") or {}, max_text_chars=max_text_chars),
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


def compact_profile_discovery_for_model(
    discovery: dict[str, Any],
    *,
    max_items: int = 50,
    max_text_chars: int = 480,
) -> dict[str, Any]:
    """Reduce a sanitized Profile Discovery Bundle for profile draft generation."""
    if not isinstance(discovery, dict):
        return {}
    source_context_summary = discovery.get("source_context_summary")
    source_analysis_summary = discovery.get("source_analysis_summary")
    return _drop_empty(
        {
            "schema_version": discovery.get("schema_version"),
            "bundle_type": discovery.get("bundle_type"),
            "discovery_sha256": discovery.get("discovery_sha256"),
            "raw_config_policy": discovery.get("raw_config_policy"),
            "raw_logs_policy": discovery.get("raw_logs_policy"),
            "source": discovery.get("source") or {},
            "discovery_policy": discovery.get("discovery_policy") or {},
            "local_first_summary": discovery.get("local_first_summary") or {},
            "display_summary": discovery.get("display_summary") or {},
            "source_context_summary": _truncate_nested(source_context_summary or {}, max_text_chars),
            "source_analysis_summary": _truncate_nested(source_analysis_summary or {}, max_text_chars),
            "observed_entities": _truncate_nested(list(discovery.get("observed_entities") or [])[:max_items], max_text_chars),
            "project_entities": _truncate_nested(list(discovery.get("project_entities") or [])[:max_items], max_text_chars),
            "entity_links": _truncate_nested(list(discovery.get("entity_links") or [])[:max_items], max_text_chars),
            "component_candidates": _truncate_nested(list(discovery.get("component_candidates") or [])[:max_items], max_text_chars),
            "metric_semantics_candidates": _truncate_nested(list(discovery.get("metric_semantics_candidates") or [])[:max_items], max_text_chars),
            "collector_mapping_candidates": _truncate_nested(list(discovery.get("collector_mapping_candidates") or [])[:max_items], max_text_chars),
            "external_dependency_candidates": _truncate_nested(list(discovery.get("external_dependency_candidates") or [])[:max_items], max_text_chars),
            "required_profile_questions": _truncate_nested(list(discovery.get("required_profile_questions") or [])[:max_items], max_text_chars),
            "prompt_rules": discovery.get("prompt_rules") or profile_discovery_prompt_rules(),
        }
    )


def compact_focused_profile_input_for_model(payload: dict[str, Any]) -> dict[str, Any]:
    """Build high-signal sanitized context for focused operational profiling."""
    if not isinstance(payload, dict):
        return {}
    discovery = payload.get("profile_discovery") if isinstance(payload.get("profile_discovery"), dict) else {}
    evidence_bundle = payload.get("evidence_bundle") if isinstance(payload.get("evidence_bundle"), dict) else {}
    source_context = payload.get("source_context") if isinstance(payload.get("source_context"), dict) else {}
    source_analysis = payload.get("source_analysis") if isinstance(payload.get("source_analysis"), dict) else {}
    return _drop_empty(
        {
            "llm_task": payload.get("llm_task") or "focused_operational_profile",
            "schema_version": payload.get("schema_version") or "focused_operational_profile_model_input.v1",
            "profile_policy": payload.get("focused_profile_policy") or {},
            "profile_discovery": _compact_focused_discovery(discovery),
            "evidence_bundle": compact_bundle_for_model(
                evidence_bundle,
                max_evidence_items=80,
                max_logs=0,
                max_normalized_events=0,
                max_text_chars=360,
            )
            if evidence_bundle
            else {},
            "source_context": _compact_focused_source_context(source_context, max_text_chars=360),
            "source_analysis": _compact_focused_source_analysis(source_analysis, max_text_chars=360),
            "selection_note": (
                "Rows are sanitized and ranked toward operational profile value: runtime entrypoints, "
                "service units, watchdogs, collectors, orchestration, loggers, metrics, heartbeats, "
                "state files, and recovery logic. The model must still distinguish source context from "
                "runtime evidence."
            ),
        }
    )


def _compact_focused_discovery(discovery: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(discovery, dict):
        return {}
    return _drop_empty(
        {
            "schema_version": discovery.get("schema_version"),
            "bundle_type": discovery.get("bundle_type"),
            "discovery_sha256": discovery.get("discovery_sha256"),
            "raw_config_policy": discovery.get("raw_config_policy"),
            "raw_logs_policy": discovery.get("raw_logs_policy"),
            "source": discovery.get("source") or {},
            "discovery_policy": discovery.get("discovery_policy") or {},
            "local_first_summary": discovery.get("local_first_summary") or {},
            "display_summary": discovery.get("display_summary") or {},
            "source_context_summary": _truncate_nested(discovery.get("source_context_summary") or {}, 360),
            "source_analysis_summary": _truncate_nested(discovery.get("source_analysis_summary") or {}, 360),
            "component_candidates": _truncate_nested(
                _focused_rows(discovery.get("component_candidates") or [], limit=80),
                360,
            ),
            "metric_semantics_candidates": _truncate_nested(
                _focused_rows(discovery.get("metric_semantics_candidates") or [], limit=80),
                360,
            ),
            "collector_mapping_candidates": _truncate_nested(
                _focused_rows(discovery.get("collector_mapping_candidates") or [], limit=40),
                360,
            ),
            "observed_entities": _truncate_nested(
                _focused_rows(discovery.get("observed_entities") or [], limit=80),
                360,
            ),
            "project_entities": _truncate_nested(
                _focused_rows(discovery.get("project_entities") or [], limit=80),
                360,
            ),
            "entity_links": _truncate_nested(
                _focused_rows(discovery.get("entity_links") or [], limit=80),
                360,
            ),
            "external_dependency_candidates": _truncate_nested(
                _focused_rows(discovery.get("external_dependency_candidates") or [], limit=30),
                360,
            ),
            "required_profile_questions": _truncate_nested(
                discovery.get("required_profile_questions") or [],
                360,
            ),
            "prompt_rules": discovery.get("prompt_rules") or profile_discovery_prompt_rules(),
        }
    )


def _compact_focused_source_context(context: dict[str, Any], *, max_text_chars: int) -> dict[str, Any]:
    if not isinstance(context, dict):
        return {}
    return _drop_empty(
        {
            "bundle_type": context.get("bundle_type"),
            "source_context_sha256": context.get("source_context_sha256"),
            "context_is_not_incident_evidence": True,
            "raw_source_policy": context.get("raw_source_policy"),
            "raw_env_policy": context.get("raw_env_policy"),
            "project_summary": _truncate_nested(context.get("project_summary") or {}, max_text_chars),
            "source_items": _truncate_nested(_focused_rows(context.get("source_items") or [], limit=80), max_text_chars),
            "config_items": _truncate_nested(_focused_rows(context.get("config_items") or [], limit=50), max_text_chars),
            "env_key_summaries": _truncate_nested(_focused_rows(context.get("env_key_summaries") or [], limit=30), max_text_chars),
            "systemd_units": _truncate_nested(_focused_rows(context.get("systemd_units") or [], limit=40), max_text_chars),
            "version_context": _truncate_nested(context.get("version_context") or {}, max_text_chars),
            "prompt_rules": context.get("prompt_rules") or [],
        }
    )


def _compact_focused_source_analysis(context: dict[str, Any], *, max_text_chars: int) -> dict[str, Any]:
    if not isinstance(context, dict):
        return {}
    return _drop_empty(
        {
            "bundle_type": context.get("bundle_type"),
            "analysis_sha256": context.get("analysis_sha256"),
            "source_context_sha256": context.get("source_context_sha256"),
            "context_is_not_incident_evidence": True,
            "raw_source_policy": context.get("raw_source_policy"),
            "raw_env_policy": context.get("raw_env_policy"),
            "component_candidates": _truncate_nested(_focused_rows(context.get("component_candidates") or [], limit=90), max_text_chars),
            "metric_semantics_candidates": _truncate_nested(_focused_rows(context.get("metric_semantics_candidates") or [], limit=120), max_text_chars),
            "logger_mapping_candidates": _truncate_nested(_focused_rows(context.get("logger_mapping_candidates") or [], limit=60), max_text_chars),
            "instrumentation_candidates": _truncate_nested(_focused_rows(context.get("instrumentation_candidates") or [], limit=60), max_text_chars),
            "collector_mapping_candidates": _truncate_nested(_focused_rows(context.get("collector_mapping_candidates") or [], limit=40), max_text_chars),
            "profile_mapping_hints": _truncate_nested(_focused_rows(context.get("profile_mapping_hints") or [], limit=60), max_text_chars),
            "prompt_rules": context.get("prompt_rules") or [],
        }
    )


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
                "single-prompt projection may omit tail patterns, while multi-provider synthesis uses chunked "
                "full-corpus coverage."
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
    approved_context = bundle.get("approved_profile_context") if isinstance(bundle.get("approved_profile_context"), dict) else {}
    return _drop_empty(
        {
            "profile": bundle.get("profile") or {},
            "approved_profile_context": _truncate_nested(approved_context, max_text_chars),
            "profile_status": approved_context.get("profile_status") or bundle.get("profile_status"),
            "profile_confidence": (
                approved_context.get("confidence_summary")
                or bundle.get("profile_confidence")
                or (bundle.get("source") or {}).get("profile_confidence")
            ),
            "profile_confidence_action": approved_context.get("confidence_action") or bundle.get("profile_confidence_action"),
            "confidence_thresholds": approved_context.get("confidence_thresholds") or {},
            "confirmed_user_outcomes": approved_context.get("confirmed_user_outcomes") or bundle.get("confirmed_user_outcomes") or [],
            "provisional_user_outcomes": approved_context.get("provisional_user_outcomes") or bundle.get("provisional_user_outcomes") or [],
            "human_questions": approved_context.get("human_questions") or bundle.get("human_questions") or [],
            "profile_review_policy": approved_context.get("profile_review_policy") or bundle.get("profile_review_policy") or {},
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


def _compact_source_context(context: dict[str, Any], *, max_text_chars: int) -> dict[str, Any]:
    if not isinstance(context, dict):
        return {}
    return _drop_empty(
        {
            "bundle_type": context.get("bundle_type"),
            "source_context_sha256": context.get("source_context_sha256"),
            "context_is_not_incident_evidence": True,
            "raw_source_policy": context.get("raw_source_policy"),
            "raw_env_policy": context.get("raw_env_policy"),
            "project_summary": _truncate_nested(context.get("project_summary") or {}, max_text_chars),
            "source_items": _truncate_nested(list(context.get("source_items") or [])[:20], max_text_chars),
            "config_items": _truncate_nested(list(context.get("config_items") or [])[:20], max_text_chars),
            "env_key_summaries": _truncate_nested(list(context.get("env_key_summaries") or [])[:20], max_text_chars),
            "systemd_units": _truncate_nested(list(context.get("systemd_units") or [])[:20], max_text_chars),
            "version_context": _truncate_nested(context.get("version_context") or {}, max_text_chars),
            "prompt_rules": context.get("prompt_rules") or [],
        }
    )


def _compact_source_analysis(context: dict[str, Any], *, max_text_chars: int) -> dict[str, Any]:
    if not isinstance(context, dict):
        return {}
    return _drop_empty(
        {
            "bundle_type": context.get("bundle_type"),
            "analysis_sha256": context.get("analysis_sha256"),
            "source_context_sha256": context.get("source_context_sha256"),
            "context_is_not_incident_evidence": True,
            "raw_source_policy": context.get("raw_source_policy"),
            "raw_env_policy": context.get("raw_env_policy"),
            "component_candidates": _truncate_nested(list(context.get("component_candidates") or [])[:30], max_text_chars),
            "metric_semantics_candidates": _truncate_nested(list(context.get("metric_semantics_candidates") or [])[:30], max_text_chars),
            "logger_mapping_candidates": _truncate_nested(list(context.get("logger_mapping_candidates") or [])[:30], max_text_chars),
            "instrumentation_candidates": _truncate_nested(list(context.get("instrumentation_candidates") or [])[:30], max_text_chars),
            "collector_mapping_candidates": _truncate_nested(list(context.get("collector_mapping_candidates") or [])[:30], max_text_chars),
            "profile_mapping_hints": _truncate_nested(list(context.get("profile_mapping_hints") or [])[:30], max_text_chars),
            "prompt_rules": context.get("prompt_rules") or [],
        }
    )


_FOCUSED_PROFILE_KEYWORDS = (
    "ownership",
    "owner",
    "runtime contract",
    "program map",
    "failure taxonomy",
    "runbook",
    "playbook",
    "decision",
    "sli",
    "same-url",
    "same url",
    "false positive",
    "stale",
    "dashboard",
    "delivery plane",
    "observability plane",
    "control plane",
    "public snapshot",
    "action plan",
    "action gate",
    "allowlist",
    "guarded",
    "request",
    "propose",
    "coordinate",
    "validate",
    "systemd",
    ".service",
    "watchdog",
    "orchestrat",
    "recovery",
    "restart",
    "collector",
    "exporter",
    "logger",
    "metric",
    "heartbeat",
    "freshness",
    "state",
    "pid",
    "queue",
    "pubsub",
    "publisher",
    "subscriber",
    "scheduler",
    "worker",
    "daemon",
    "rtmp",
    "rtmps",
    "ffmpeg",
    "stream",
    "audio",
    "service",
    "unit",
    "timer",
    "cron",
    "health",
    "liveness",
    "配信端末",
    "配信専用",
    "観測層",
    "監視層",
    "arena-server",
    "責務",
    "所有",
    "復旧要求",
    "段階的復旧",
    "誤表示",
    "誤報",
    "raw metrics",
    "根拠",
    "証跡",
    "同一url",
    "same url",
)


def _focused_rows(rows: Any, *, limit: int) -> list[dict[str, Any]]:
    if not isinstance(rows, list):
        return []
    indexed = [(index, row) for index, row in enumerate(rows) if isinstance(row, dict)]
    indexed.sort(key=lambda item: (-_focused_row_score(item[1]), item[0]))
    return [row for _index, row in indexed[:limit]]


def _focused_row_score(row: dict[str, Any]) -> float:
    text = pretty_json(_truncate_nested(row, 240)).casefold()
    score = 0.0
    path = str(row.get("relative_path") or row.get("path") or row.get("source_path") or "").casefold()
    if "/docs/" in f"/{path}" or path.startswith("docs/"):
        score += 12.0
    for path_hint in (
        "10_current",
        "20_runbooks",
        "25_decisions",
        "65_programs",
        "80_templates",
        "runtime-contract",
        "failure-taxonomy",
        "ownership",
        "program-map",
    ):
        if path_hint in path:
            score += 10.0
    for keyword in _FOCUSED_PROFILE_KEYWORDS:
        if keyword in text:
            score += 4.0
    for key in ("confidence", "score", "score_hint", "count", "occurrence_count"):
        value = row.get(key)
        try:
            score += min(float(value), 10.0)
        except (TypeError, ValueError):
            continue
    if row.get("human_review_required") is True:
        score += 0.5
    return score


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
            "db_row_coverage": row.get("db_row_coverage"),
        }
    )


def _compact_db_corpus_coverage(coverage: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(coverage, dict):
        return {}
    return _drop_empty(
        {
            "schema_version": coverage.get("schema_version"),
            "source_table": coverage.get("source_table"),
            "strategy": coverage.get("strategy"),
            "total_row_count": coverage.get("total_row_count"),
            "covered_row_count": coverage.get("covered_row_count"),
            "uncovered_row_count": coverage.get("uncovered_row_count"),
            "coverage_ratio": coverage.get("coverage_ratio"),
            "pattern_count": coverage.get("pattern_count"),
            "singleton_pattern_count": coverage.get("singleton_pattern_count"),
            "low_frequency_pattern_count": coverage.get("low_frequency_pattern_count"),
            "row_assignments_sha256": coverage.get("row_assignments_sha256"),
            "severity_counts": coverage.get("severity_counts"),
            "error_type_counts": coverage.get("error_type_counts"),
            "note": (
                "Every sanitized DB row in the incident window is assigned to an Evidence Item before chunking; "
                "row-level assignments stay in the bundle ledger and this prompt carries only the coverage summary."
            ),
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
            "suspected_issue": {"type": "STRING"},
            "operational_mechanism": {"type": "STRING"},
            "why_it_matters": {"type": "STRING"},
            "evidence_summary": string_array,
            "counter_evidence_summary": string_array,
            "why_not_promoted": {"type": "STRING"},
            "next_validation_question": {"type": "STRING"},
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
            "suspected_issue",
            "operational_mechanism",
            "why_it_matters",
            "evidence_summary",
            "counter_evidence_summary",
            "why_not_promoted",
            "next_validation_question",
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
            "suspected_issue",
            "operational_mechanism",
            "why_it_matters",
            "evidence_summary",
            "counter_evidence_summary",
            "why_not_promoted",
            "next_validation_question",
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


def profile_draft_response_schema() -> dict[str, Any]:
    string_array = {"type": "ARRAY", "items": {"type": "STRING"}}
    component = {
        "type": "OBJECT",
        "properties": {
            "component_id": {"type": "STRING"},
            "name": {"type": "STRING"},
            "role": {"type": "STRING"},
            "subsystem": {"type": "STRING"},
            "core_target_types": string_array,
        },
        "required": ["component_id", "name", "role", "subsystem", "core_target_types"],
        "propertyOrdering": ["component_id", "name", "role", "subsystem", "core_target_types"],
    }
    metric = {
        "type": "OBJECT",
        "properties": {
            "metric_name": {"type": "STRING"},
            "semantic_type": {"type": "STRING"},
            "zero_behavior": {"type": "STRING"},
            "increase_behavior": {"type": "STRING"},
            "decrease_behavior": {"type": "STRING"},
            "subsystem": {"type": "STRING"},
            "core_target_type": {"type": "STRING"},
        },
        "required": [
            "metric_name",
            "semantic_type",
            "zero_behavior",
            "increase_behavior",
            "decrease_behavior",
            "subsystem",
            "core_target_type",
        ],
        "propertyOrdering": [
            "metric_name",
            "semantic_type",
            "zero_behavior",
            "increase_behavior",
            "decrease_behavior",
            "subsystem",
            "core_target_type",
        ],
    }
    log_source = {
        "type": "OBJECT",
        "properties": {
            "source_id": {"type": "STRING"},
            "description": {"type": "STRING"},
        },
        "required": ["source_id", "description"],
        "propertyOrdering": ["source_id", "description"],
    }
    collector = {
        "type": "OBJECT",
        "properties": {
            "request_type": {"type": "STRING"},
            "candidate_collectors": string_array,
            "safety_level": {"type": "STRING"},
        },
        "required": ["request_type", "candidate_collectors", "safety_level"],
        "propertyOrdering": ["request_type", "candidate_collectors", "safety_level"],
    }
    return {
        "type": "OBJECT",
        "properties": {
            "schema_version": {"type": "STRING"},
            "system_type": {"type": "STRING"},
            "purpose": {"type": "STRING"},
            "critical_outcomes": string_array,
            "components": {"type": "ARRAY", "items": component},
            "metric_semantics": {"type": "ARRAY", "items": metric},
            "log_sources": {"type": "ARRAY", "items": log_source},
            "collector_mappings": {"type": "ARRAY", "items": collector},
            "known_benign_noise": string_array,
            "action_constraints": string_array,
            "assumptions": string_array,
            "required_human_decisions": string_array,
        },
        "required": [
            "schema_version",
            "system_type",
            "purpose",
            "critical_outcomes",
            "components",
            "metric_semantics",
            "log_sources",
            "collector_mappings",
            "known_benign_noise",
            "action_constraints",
            "assumptions",
            "required_human_decisions",
        ],
        "propertyOrdering": [
            "schema_version",
            "system_type",
            "purpose",
            "critical_outcomes",
            "components",
            "metric_semantics",
            "log_sources",
            "collector_mappings",
            "known_benign_noise",
            "action_constraints",
            "assumptions",
            "required_human_decisions",
        ],
    }


def focused_operational_profile_response_schema() -> dict[str, Any]:
    string_array = {"type": "ARRAY", "items": {"type": "STRING"}}
    ref_object = {
        "type": "OBJECT",
        "properties": {
            "name": {"type": "STRING"},
            "meaning": {"type": "STRING"},
            "evidence_refs": string_array,
            "source_context_refs": string_array,
        },
        "required": ["name", "meaning", "evidence_refs", "source_context_refs"],
        "propertyOrdering": ["name", "meaning", "evidence_refs", "source_context_refs"],
    }
    component = {
        "type": "OBJECT",
        "properties": {
            "component_id": {"type": "STRING"},
            "name": {"type": "STRING"},
            "role": {"type": "STRING"},
            "evidence_refs": string_array,
            "source_context_refs": string_array,
            "confidence": {"type": "NUMBER"},
        },
        "required": ["component_id", "name", "role", "evidence_refs", "source_context_refs", "confidence"],
        "propertyOrdering": ["component_id", "name", "role", "evidence_refs", "source_context_refs", "confidence"],
    }
    metric = {
        "type": "OBJECT",
        "properties": {
            "metric_name": {"type": "STRING"},
            "meaning": {"type": "STRING"},
            "healthy_direction": {"type": "STRING"},
            "evidence_refs": string_array,
            "source_context_refs": string_array,
        },
        "required": ["metric_name", "meaning", "healthy_direction", "evidence_refs", "source_context_refs"],
        "propertyOrdering": ["metric_name", "meaning", "healthy_direction", "evidence_refs", "source_context_refs"],
    }
    log_source = {
        "type": "OBJECT",
        "properties": {
            "source": {"type": "STRING"},
            "meaning": {"type": "STRING"},
            "evidence_refs": string_array,
            "source_context_refs": string_array,
        },
        "required": ["source", "meaning", "evidence_refs", "source_context_refs"],
        "propertyOrdering": ["source", "meaning", "evidence_refs", "source_context_refs"],
    }
    flow = {
        "type": "OBJECT",
        "properties": {
            "flow_name": {"type": "STRING"},
            "trigger": {"type": "STRING"},
            "steps": string_array,
            "owned_by_components": string_array,
            "evidence_refs": string_array,
            "source_context_refs": string_array,
            "confidence": {"type": "NUMBER"},
        },
        "required": [
            "flow_name",
            "trigger",
            "steps",
            "owned_by_components",
            "evidence_refs",
            "source_context_refs",
            "confidence",
        ],
        "propertyOrdering": [
            "flow_name",
            "trigger",
            "steps",
            "owned_by_components",
            "evidence_refs",
            "source_context_refs",
            "confidence",
        ],
    }
    failure_mode = {
        "type": "OBJECT",
        "properties": {
            "failure_mode": {"type": "STRING"},
            "observable_signals": string_array,
            "missing_evidence": string_array,
            "confidence": {"type": "NUMBER"},
        },
        "required": ["failure_mode", "observable_signals", "missing_evidence", "confidence"],
        "propertyOrdering": ["failure_mode", "observable_signals", "missing_evidence", "confidence"],
    }
    collector = {
        "type": "OBJECT",
        "properties": {
            "collector": {"type": "STRING"},
            "purpose": {"type": "STRING"},
            "safety_level": {"type": "STRING"},
        },
        "required": ["collector", "purpose", "safety_level"],
        "propertyOrdering": ["collector", "purpose", "safety_level"],
    }
    return {
        "type": "OBJECT",
        "properties": {
            "schema_version": {"type": "STRING"},
            "system_label": {"type": "STRING"},
            "system_summary": {
                "type": "OBJECT",
                "properties": {
                    "system_type": {"type": "STRING"},
                    "primary_purpose": {"type": "STRING"},
                    "logged_subject": {"type": "STRING"},
                    "operational_boundary": {"type": "STRING"},
                    "confidence": {"type": "NUMBER"},
                },
                "required": ["system_type", "primary_purpose", "logged_subject", "operational_boundary", "confidence"],
                "propertyOrdering": ["system_type", "primary_purpose", "logged_subject", "operational_boundary", "confidence"],
            },
            "runtime_components": {"type": "ARRAY", "items": component},
            "observability_contract": {
                "type": "OBJECT",
                "properties": {
                    "logs": {"type": "ARRAY", "items": log_source},
                    "metrics": {"type": "ARRAY", "items": metric},
                    "heartbeats": {"type": "ARRAY", "items": ref_object},
                    "state_files": {"type": "ARRAY", "items": ref_object},
                },
                "required": ["logs", "metrics", "heartbeats", "state_files"],
                "propertyOrdering": ["logs", "metrics", "heartbeats", "state_files"],
            },
            "orchestration_flows": {"type": "ARRAY", "items": flow},
            "failure_modes": {"type": "ARRAY", "items": failure_mode},
            "read_only_collectors": {"type": "ARRAY", "items": collector},
            "profile_limits": {
                "type": "OBJECT",
                "properties": {
                    "source_context_is_incident_evidence": {"type": "BOOLEAN"},
                    "runtime_claims_require_evidence_id": {"type": "BOOLEAN"},
                    "approval_required_before_explicit_profile": {"type": "BOOLEAN"},
                    "raw_source_sent_to_provider": {"type": "BOOLEAN"},
                    "raw_logs_sent_to_provider": {"type": "BOOLEAN"},
                    "notes": string_array,
                },
                "required": [
                    "source_context_is_incident_evidence",
                    "runtime_claims_require_evidence_id",
                    "approval_required_before_explicit_profile",
                    "raw_source_sent_to_provider",
                    "raw_logs_sent_to_provider",
                    "notes",
                ],
                "propertyOrdering": [
                    "source_context_is_incident_evidence",
                    "runtime_claims_require_evidence_id",
                    "approval_required_before_explicit_profile",
                    "raw_source_sent_to_provider",
                    "raw_logs_sent_to_provider",
                    "notes",
                ],
            },
            "human_review_required": string_array,
        },
        "required": [
            "schema_version",
            "system_label",
            "system_summary",
            "runtime_components",
            "observability_contract",
            "orchestration_flows",
            "failure_modes",
            "read_only_collectors",
            "profile_limits",
            "human_review_required",
        ],
        "propertyOrdering": [
            "schema_version",
            "system_label",
            "system_summary",
            "runtime_components",
            "observability_contract",
            "orchestration_flows",
            "failure_modes",
            "read_only_collectors",
            "profile_limits",
            "human_review_required",
        ],
    }


def profile_review_patch_response_schema() -> dict[str, Any]:
    string_array = {"type": "ARRAY", "items": {"type": "STRING"}}
    metric = {
        "type": "OBJECT",
        "properties": {
            "metric_name": {"type": "STRING"},
            "meaning": {"type": "STRING"},
            "healthy_direction": {"type": "STRING"},
            "zero_behavior": {"type": "STRING"},
            "increase_behavior": {"type": "STRING"},
            "decrease_behavior": {"type": "STRING"},
            "reason": {"type": "STRING"},
            "provenance": {"type": "STRING"},
        },
        "required": [
            "metric_name",
            "meaning",
            "healthy_direction",
            "zero_behavior",
            "increase_behavior",
            "decrease_behavior",
            "reason",
            "provenance",
        ],
        "propertyOrdering": [
            "metric_name",
            "meaning",
            "healthy_direction",
            "zero_behavior",
            "increase_behavior",
            "decrease_behavior",
            "reason",
            "provenance",
        ],
    }
    component = {
        "type": "OBJECT",
        "properties": {
            "component_id": {"type": "STRING"},
            "role": {"type": "STRING"},
            "reason": {"type": "STRING"},
            "provenance": {"type": "STRING"},
        },
        "required": ["component_id", "role", "reason", "provenance"],
        "propertyOrdering": ["component_id", "role", "reason", "provenance"],
    }
    log_source = {
        "type": "OBJECT",
        "properties": {
            "source": {"type": "STRING"},
            "meaning": {"type": "STRING"},
            "reason": {"type": "STRING"},
            "provenance": {"type": "STRING"},
        },
        "required": ["source", "meaning", "reason", "provenance"],
        "propertyOrdering": ["source", "meaning", "reason", "provenance"],
    }
    unresolved = {
        "type": "OBJECT",
        "properties": {"question": {"type": "STRING"}, "reason": {"type": "STRING"}},
        "required": ["question", "reason"],
        "propertyOrdering": ["question", "reason"],
    }
    return {
        "type": "OBJECT",
        "properties": {
            "schema_version": {"type": "STRING"},
            "system_summary_overrides": {
                "type": "OBJECT",
                "properties": {
                    "primary_purpose": {"type": "STRING"},
                    "logged_subject": {"type": "STRING"},
                    "operational_boundary": {"type": "STRING"},
                },
                "required": ["primary_purpose", "logged_subject", "operational_boundary"],
                "propertyOrdering": ["primary_purpose", "logged_subject", "operational_boundary"],
            },
            "metric_semantics_overrides": {"type": "ARRAY", "items": metric},
            "component_role_overrides": {"type": "ARRAY", "items": component},
            "log_source_overrides": {"type": "ARRAY", "items": log_source},
            "confirmed_user_outcomes": string_array,
            "ignored_component_ids": string_array,
            "approved_collectors": string_array,
            "unresolved_questions": {"type": "ARRAY", "items": unresolved},
        },
        "required": [
            "schema_version",
            "system_summary_overrides",
            "metric_semantics_overrides",
            "component_role_overrides",
            "log_source_overrides",
            "confirmed_user_outcomes",
            "ignored_component_ids",
            "approved_collectors",
            "unresolved_questions",
        ],
        "propertyOrdering": [
            "schema_version",
            "system_summary_overrides",
            "metric_semantics_overrides",
            "component_role_overrides",
            "log_source_overrides",
            "confirmed_user_outcomes",
            "ignored_component_ids",
            "approved_collectors",
            "unresolved_questions",
        ],
    }
