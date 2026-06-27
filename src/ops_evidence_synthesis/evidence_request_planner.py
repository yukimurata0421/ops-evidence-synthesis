from __future__ import annotations

import json
from collections import OrderedDict
from pathlib import Path
from typing import Any, Iterable

from ops_evidence_synthesis.ai.base import ModelProvider
from ops_evidence_synthesis.ai.runtime import run_provider_with_retries, safety_preflight_for_model_input
from ops_evidence_synthesis.canonical import canonical_json, pretty_json, sha256_json
from ops_evidence_synthesis.evidence_rules import ai_evidence_rules, evidence_request_planner_rules
from ops_evidence_synthesis.local_first import scan_sanitized_text
from ops_evidence_synthesis.synthesis.output_ingest import repair_json_text
from ops_evidence_synthesis.synthesis.review_arbitration import request_types_from_canonical_graph


PLAN_SCHEMA_VERSION = "evidence_request_plan.v1"
PLAN_TYPE = "manual_read_only_collection_plan"
ANSWERS_SCHEMA_VERSION = "planner_answers.v1"
PLANNER_VERSION = "evidence_request_planner.v1"
EVIDENCE_REQUIREMENTS_SCHEMA_VERSION = "evidence_requirements.v1"

REQUEST_PRIORITIES = {
    "installed_artifact_query": "P1",
    "process_state_query": "P1",
    "scheduler_history_query": "P1",
    "throughput_signal_query": "P1",
    "instrumentation_consistency_query": "P1",
    "log_completeness_query": "P1",
    "deployment_correlation_query": "P2",
    "freshness_signal_query": "P2",
    "user_impact_signal_query": "P2",
    "external_dependency_status_query": "P2",
}

BLOCKED_SOURCES = [
    ".env raw values",
    "credential files",
    "private key bodies",
    "raw Authorization headers",
    "raw Cookie values",
    "token values",
    "password values",
]

DO_NOT_REQUEST = [
    "raw secrets",
    "raw env values",
    "raw Authorization headers",
    "raw Cookie values",
    "token values",
    "private key bodies",
    "credential files",
    "unsanitized logs",
]

PROMOTION_REASON_REQUIREMENTS = {
    "user_impact_unverified": {
        "evidence_type": "user_impact_signal",
        "source_kind": "metric_or_log",
        "maps_to_request_type": "user_impact_signal_query",
        "question": "Did the technical failure produce user-visible or critical-path impact during the incident window?",
        "acceptance": "A user-visible delivery, ingest, playback, notification, or request failure overlaps the technical failure window.",
        "rejection": "No user-visible or critical-path degradation overlaps the technical failure window.",
    },
    "impact_disagreement": {
        "evidence_type": "critical_outcome",
        "source_kind": "metric_or_log",
        "maps_to_request_type": "user_impact_signal_query",
        "question": "Which impact signal resolves the disagreement about user-visible impact?",
        "acceptance": "A critical outcome signal changes in the same window as the technical failure.",
        "rejection": "Critical outcome signals remain healthy or move in a different window.",
    },
    "no_user_impact_evidence": {
        "evidence_type": "user_impact_signal",
        "source_kind": "metric_or_log",
        "maps_to_request_type": "user_impact_signal_query",
        "question": "Is there a collected signal close enough to user impact to support incident promotion?",
        "acceptance": "A collected user-impact proxy overlaps the technical failure window.",
        "rejection": "No user-impact proxy exists or the available proxy is healthy.",
    },
    "single_metric_only": {
        "evidence_type": "independent_corroboration",
        "source_kind": "metric_or_log",
        "maps_to_request_type": "instrumentation_consistency_query",
        "question": "Does an independent log, metric, or runtime-state source corroborate the single metric?",
        "acceptance": "At least one independent runtime source agrees with the metric during the same window.",
        "rejection": "Independent runtime sources are absent, healthy, or contradict the metric.",
    },
    "no_baseline_agreement_or_causal_alignment": {
        "evidence_type": "causal_alignment",
        "source_kind": "metric_or_log",
        "maps_to_request_type": "instrumentation_consistency_query",
        "question": "Does event ordering support the proposed causal alignment?",
        "acceptance": "Process state, logs, and metric buckets show a coherent order from failure to impact.",
        "rejection": "Ordering is missing, ambiguous, or contradicts the proposed causal chain.",
    },
    "cause_disagreement": {
        "evidence_type": "causal_alignment",
        "source_kind": "metric_or_log",
        "maps_to_request_type": "external_dependency_status_query",
        "question": "Which local or external source resolves the cause disagreement?",
        "acceptance": "Runtime evidence distinguishes local failure from dependency or instrumentation failure.",
        "rejection": "Runtime evidence remains ambiguous or points to a competing cause.",
    },
    "support_without_evidence_id": {
        "evidence_type": "evidence_identity",
        "source_kind": "runtime_state",
        "maps_to_request_type": "log_completeness_query",
        "question": "Which sanitized Evidence Item with evidence_id supports the claim?",
        "acceptance": "A sanitized Evidence Item with evidence_id directly supports the runtime claim.",
        "rejection": "The claim cannot be tied to a sanitized Evidence Item.",
    },
    "support_is_context_not_runtime_evidence": {
        "evidence_type": "runtime_evidence",
        "source_kind": "runtime_state",
        "maps_to_request_type": "process_state_query",
        "question": "What runtime evidence proves this context-only hypothesis occurred during the incident window?",
        "acceptance": "Runtime state, logs, or metrics cite the occurrence during the incident window.",
        "rejection": "Only source/profile/human context exists; no runtime occurrence evidence is collected.",
    },
    "core_missing_evidence": {
        "evidence_type": "runtime_evidence",
        "source_kind": "metric_or_log",
        "maps_to_request_type": "instrumentation_consistency_query",
        "question": "What core runtime evidence is missing for this review target?",
        "acceptance": "The missing core evidence is collected with evidence_id and overlaps the review window.",
        "rejection": "The core evidence source is unavailable or does not overlap the review window.",
    },
}

BASE_GRANULARITY = {
    "current": "evidence_bundle_patterns",
    "required": "event_level_journal + 1m_metric_buckets",
    "reason": "Need ordering between process state, log events, metric changes, and deployment changes.",
}

SEVERITY_ONLY_SIGNALS = {"info", "warning", "debug"}
SIGNAL_ENRICHMENT_RULES = {
    "info": ("severity_only", False, False),
    "warning": ("severity_only_or_diagnostic", False, False),
    "debug": ("severity_only", False, False),
    "http_5xx": ("external_or_user_visible_error_candidate", True, True),
    "stream_not_live": ("user_visible_outcome", True, True),
    "watch_url_unavailable": ("user_visible_outcome", True, True),
    "notification_not_delivered": ("user_visible_outcome", True, True),
    "audio_energy_missing": ("user_impact_proxy", True, True),
    "capture_freshness_stale": ("freshness_or_user_impact_proxy", True, True),
    "request_error_rate": ("user_visible_error_rate", True, True),
    "external_dependency_unhealthy": ("external_dependency_status", True, True),
}

STREAM_V3_DOMAIN_REQUESTS = {
    "user_impact_signal_query": "audio_energy_gap_query",
    "freshness_signal_query": "capture_freshness_drift_query",
    "external_dependency_status_query": "youtube_ingest_status_query",
    "instrumentation_consistency_query": "rtmps_reconnect_consistency_query",
    "process_state_query": "ffmpeg_process_state_query",
    "deployment_correlation_query": "deployment_correlation_query",
}

AMAZON_NOTIFY_DOMAIN_REQUESTS = {
    "external_dependency_status_query": "gmail_watch_status_query",
    "process_state_query": "pubsub_listener_state_query",
    "scheduler_history_query": "restart_loop_journal_query",
    "installed_artifact_query": "execstart_artifact_metadata_query",
    "instrumentation_consistency_query": "systemd_unit_definition_query",
}
SOURCE_ANALYSIS_REQUEST_ALIASES = {
    "metric_semantics_query": "instrumentation_consistency_query",
    "collector_mapping_query": "instrumentation_consistency_query",
}
DOMAIN_REQUEST_TYPES = {
    "youtube_ingest_status_query",
    "audio_energy_gap_query",
    "capture_freshness_drift_query",
    "rtmps_reconnect_consistency_query",
    "ffmpeg_process_state_query",
    "gmail_watch_status_query",
    "pubsub_listener_state_query",
    "discord_webhook_delivery_query",
    "systemd_unit_definition_query",
    "execstart_artifact_metadata_query",
    "restart_loop_journal_query",
}
PROFILE_REQUEST_ALIASES = {
    "capture_freshness_drift_query": ("capture_freshness_query",),
    "rtmps_reconnect_consistency_query": ("rtmps_send_path_query", "throughput_signal_query"),
}
STREAM_V3_FALLBACK_METRICS = {
    "youtube_ingest_status_query": [
        "stream_v3_youtube_ingest_connected",
        "stream_v3_youtube_local_ok",
        "stream_v3_youtube_public_ok",
        "stream_v3_youtube_api_ok",
        "stream_v3_youtube_oauth_ok",
        "stream_v3_youtube_stream_active",
        "stream_v3_same_url_live",
    ],
    "audio_energy_gap_query": [
        "stream_v3_audio_evidence_available",
        "stream_v3_audio_ok",
        "stream_v3_audio_fault_count",
        "stream_v3_audio_evidence_age_seconds",
        "stream_v3_pulse_source_present",
    ],
    "capture_freshness_drift_query": [
        "stream_v3_runtime_heartbeat_age_seconds",
        "stream_v3_upload_latest_age_seconds",
        "stream_v3_stream_watchdog_runtime_snapshot_age_seconds",
        "stream_v3_adsb_rendering_ok",
        "stream_v3_adsb_evidence_available",
        "stream_v3_adsb_evidence_age_seconds",
        "stream_v3_video_frame_unhealthy",
    ],
    "rtmps_reconnect_consistency_query": [
        "stream_v3_network_ffmpeg_socket_connected",
        "stream_v3_upload_latest_mbps",
        "stream_v3_upload_p95_mbps",
        "stream_v3_upload_latest_age_seconds",
        "stream_v3_rtmps_ssl_tls_count",
    ],
}
BLOCKED_COLLECTION_METRIC_NAMES = {
    "audio_energy_missing",
    "capture_freshness_seconds",
    "rtmps_reconnect_count",
    "youtube_ingest_connected",
    "youtube_stream_health",
}


def plan_evidence_requests(
    bundle_path: str | Path,
    profile_path: str | Path,
    *,
    output_dir: str | Path,
    answers_path: str | Path | None = None,
    source_analysis_path: str | Path | None = None,
    canonical_review_graph_path: str | Path | None = None,
) -> dict[str, Any]:
    bundle = _load_json(bundle_path)
    profile = _load_json(profile_path)
    answers = _load_json(answers_path) if answers_path else {}
    source_analysis = _load_json(source_analysis_path) if source_analysis_path else {}
    canonical_review_graph = _load_json(canonical_review_graph_path) if canonical_review_graph_path else {}
    plan = build_evidence_request_plan(
        bundle,
        profile,
        planner_answers=answers,
        source_analysis=source_analysis,
        canonical_review_graph=canonical_review_graph,
        generated_from={
            "evidence_bundle": str(bundle_path),
            "approved_profile": str(profile_path),
            "planner_answers": str(answers_path) if answers_path else "",
            "source_analysis": str(source_analysis_path) if source_analysis_path else "",
            "canonical_review_graph": str(canonical_review_graph_path) if canonical_review_graph_path else "",
        },
    )
    markdown = render_collection_instructions(plan)
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    plan_path = output / "evidence_request_plan.json"
    instructions_path = output / "collection_instructions.md"
    sample_answers_path = output / "planner_answers.sample.json"
    plan_path.write_text(pretty_json(plan) + "\n", encoding="utf-8")
    instructions_path.write_text(markdown + "\n", encoding="utf-8")
    sample_answers_path.write_text(pretty_json(sample_planner_answers(plan)) + "\n", encoding="utf-8")
    if answers_path:
        (output / "planner_answers.json").write_text(pretty_json(answers) + "\n", encoding="utf-8")
    return {
        "evidence_request_plan": str(plan_path),
        "collection_instructions": str(instructions_path),
        "planner_answers_sample": str(sample_answers_path),
        "request_count": len(plan["requests"]),
        "human_question_count": len(plan["human_questions"]),
        "plan_id": plan["plan_id"],
        "plan_valid": plan["plan_valid"],
        "planner_quality_warning_count": len(plan.get("planner_quality_warnings") or []),
        "canonical_review_graph_used": bool(canonical_review_graph),
    }


def build_evidence_request_plan(
    bundle: dict[str, Any],
    approved_profile: dict[str, Any],
    *,
    planner_answers: dict[str, Any] | None = None,
    generated_from: dict[str, str] | None = None,
    source_analysis: dict[str, Any] | None = None,
    canonical_review_graph: dict[str, Any] | None = None,
    evidence_requirement_provider: ModelProvider | None = None,
) -> dict[str, Any]:
    _raise_if_unsafe_input(bundle, "evidence_bundle")
    _raise_if_unsafe_input(approved_profile, "approved_profile")
    if planner_answers:
        _raise_if_unsafe_input(planner_answers, "planner_answers")
    if source_analysis:
        _raise_if_unsafe_input(source_analysis, "source_analysis")
    if canonical_review_graph:
        _raise_if_unsafe_input(canonical_review_graph, "canonical_review_graph")

    raw_answers = _raw_answers(planner_answers)
    answers = _answers(planner_answers)
    source_analysis = source_analysis or {}
    canonical_review_graph = canonical_review_graph or {}
    generated_from_context = dict(generated_from or {})
    if canonical_review_graph:
        generated_from_context.setdefault("canonical_review_graph", str(canonical_review_graph.get("snapshot_status") or canonical_review_graph.get("canonical_graph_status") or "api_payload"))
        generated_from_context["canonical_graph_sha256"] = str(canonical_review_graph.get("canonical_graph_sha256") or "")
        generated_from_context["input_fingerprint_sha256"] = str(canonical_review_graph.get("input_fingerprint_sha256") or "")
    else:
        generated_from_context.setdefault("canonical_review_graph", "legacy_fallback")
    parent_sha = str(bundle.get("evidence_sha256") or "")
    profile_id = str(approved_profile.get("profile_id") or "unknown")
    profile_confidence = _profile_confidence(bundle, approved_profile)
    time_window = _time_window(bundle, answers)
    operator_display_timezone = _operator_display_timezone(answers, time_window)
    units = _confirmed_units(bundle, approved_profile, answers)
    components = _component_names(bundle, approved_profile, units)
    granularity = _granularity(answers)
    signals = _signals(bundle)
    signal_enrichment = enrich_signals(bundle)
    request_types = _request_types(
        signals,
        approved_profile,
        source_analysis=source_analysis,
        profile_confidence=profile_confidence,
        canonical_review_graph=canonical_review_graph,
    )
    requests = _build_requests(
        request_types,
        bundle=bundle,
        profile=approved_profile,
        source_analysis=source_analysis,
        profile_confidence=profile_confidence,
        time_window=time_window,
        units=units,
        components=components,
        granularity=granularity,
        answers=answers,
    )
    evidence_requirements_payload = build_evidence_requirements(
        bundle,
        approved_profile,
        canonical_review_graph=canonical_review_graph,
        source_analysis=source_analysis,
        provider=evidence_requirement_provider,
    )
    evidence_requirements = evidence_requirements_payload["requirements"]
    plan_id = "PLAN-" + sha256_json(
        {
            "parent_evidence_sha256": parent_sha,
            "profile_id": profile_id,
            "request_types": request_types,
            "answers": answers,
            "evidence_requirements_sha256": sha256_json(evidence_requirements),
        }
    )[:12].upper()
    plan = {
        "schema_version": PLAN_SCHEMA_VERSION,
        "plan_type": PLAN_TYPE,
        "planner_version": PLANNER_VERSION,
        "plan_id": plan_id,
        "parent_evidence_sha256": parent_sha,
        "profile_id": profile_id,
        "profile_confidence": profile_confidence,
        "incident_window": time_window,
        "operator_display_timezone": operator_display_timezone,
        "generated_from": generated_from_context,
        "canonical_review_graph_summary": (canonical_review_graph.get("summary") if isinstance(canonical_review_graph.get("summary"), dict) else {}),
        "canonical_review_graph_used": bool(canonical_review_graph),
        "execution_policy": execution_policy(),
        "context_policy": context_policy(),
        "signal_enrichment": signal_enrichment,
        "human_questions": human_questions(bundle, approved_profile, answers),
        "evidence_requirements": evidence_requirements,
        "evidence_requirements_metadata": evidence_requirements_payload["metadata"],
        "requests": requests,
        "blocked_sources": BLOCKED_SOURCES,
        "display_summary": {
            "title": "Evidence Request Planner",
            "subtitle": "Manual read-only collection plan. Planner does not execute commands.",
            "primary_badges": [
                "planner_executes_commands:false",
                "raw_output_policy:local_only",
                "sanitize_before_upload:true",
                "verify_sanitized_required:true",
            ],
        },
        "prompt_rules": ai_evidence_rules() + evidence_request_planner_rules(),
    }
    warnings = planner_quality_warnings(
        plan,
        bundle=bundle,
        profile=approved_profile,
        source_analysis=source_analysis,
        answers=answers,
        raw_answers=raw_answers,
    )
    if not canonical_review_graph:
        warnings.append({
            "warning_type": "canonical_graph_legacy_fallback",
            "severity": "warning",
            "message": "Planner did not receive a current canonical_review_graph snapshot and used legacy request generation inputs.",
            "suggested_fix": "Run Review Target Arbitration and pass canonical_review_graph or persist a current snapshot.",
        })
    plan["planner_quality_warnings"] = warnings
    plan["plan_valid"] = not any(str(row.get("severity")) == "error" for row in warnings)
    return plan


def execution_policy() -> dict[str, Any]:
    return {
        "planner_executes_commands": False,
        "read_only_only": True,
        "raw_output_policy": "local_only",
        "raw_grep_output_upload_allowed": False,
        "raw_env_values_allowed": False,
        "raw_credentials_allowed": False,
        "private_key_body_allowed": False,
        "authorization_header_value_allowed": False,
        "cookie_value_allowed": False,
        "sanitize_before_upload": True,
        "verify_sanitized_required": True,
    }


def context_policy() -> dict[str, bool]:
    return {
        "human_answers_are_context_not_evidence": True,
        "support_claims_must_cite_evidence_id": True,
    }


def human_questions(
    bundle: dict[str, Any],
    profile: dict[str, Any],
    answers: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    answers = answers or {}
    window = _time_window(bundle, answers)
    operator_display_timezone = _operator_display_timezone(answers, window)
    units = _confirmed_units(bundle, profile, answers)
    components = _component_names(bundle, profile, units)
    user_impact_options = _signal_names(bundle)
    return [
        {
            "question_id": "Q-001",
            "answer_key": "incident_window",
            "label": "Confirm incident window",
            "input_type": "datetime_range",
            "required": True,
            "default": window,
            "help": "The planner uses this window to generate log and metric collection instructions.",
            "affects": ["journalctl --since/--until", "metric bucket range", "deployment correlation window"],
        },
        {
            "question_id": "Q-002",
            "answer_key": "operator_display_timezone",
            "label": "Confirm operator display timezone",
            "input_type": "single_select",
            "required": True,
            "default": operator_display_timezone,
            "options": ["UTC", "JST", "other"],
            "help": "Collection uses incident_window.timezone; this only controls operator-facing display.",
            "affects": ["UI timestamp display", "operator-facing reports"],
        },
        {
            "question_id": "Q-003",
            "answer_key": "log_retention_days",
            "label": "How many days of logs are retained?",
            "input_type": "integer",
            "required": True,
            "default": int(answers.get("log_retention_days") or 7),
            "help": "Retention controls whether the requested window is collectible or should be marked unavailable.",
            "affects": ["journal query feasibility", "missing evidence classification"],
        },
        {
            "question_id": "Q-004",
            "answer_key": "available_granularity",
            "label": "Which data granularities exist?",
            "input_type": "multi_select",
            "required": True,
            "default": answers.get("available_granularity") or ["event_level_journal", "one_minute_metric_buckets"],
            "options": ["event_level_journal", "one_minute_metric_buckets", "fifteen_minute_aggregates"],
            "help": "The planner chooses event ordering or aggregate checks based on available granularity.",
            "affects": ["required granularity", "request priority"],
        },
        {
            "question_id": "Q-005",
            "answer_key": "confirmed_units",
            "label": "Confirm systemd unit / container / process names",
            "input_type": "path_list",
            "required": True,
            "default": units,
            "help": "Only confirmed names should be substituted into command templates.",
            "affects": ["systemctl templates", "journalctl templates", "process state queries"],
        },
        {
            "question_id": "Q-006",
            "answer_key": "component_criticality",
            "label": "Is each component critical path or diagnostic only?",
            "input_type": "component_map_select",
            "required": True,
            "items": [
                {
                    "component": component,
                    "default": _component_criticality_default(answers, component),
                    "options": ["critical_path", "diagnostic_only", "unknown"],
                }
                for component in components
            ],
            "help": "Criticality changes review priority, not truth status.",
            "affects": ["request priority", "user impact interpretation"],
        },
        {
            "question_id": "Q-007",
            "answer_key": "user_impact_signals",
            "label": "Which signals are closest to user impact?",
            "input_type": "multi_select",
            "required": False,
            "default": [item for item in answers.get("user_impact_signals") or [] if item in user_impact_options],
            "options": user_impact_options,
            "help": "Human-selected user impact signals are context only, not support evidence.",
            "affects": ["user_impact_signal_query", "request priority"],
        },
        _config_metadata_question(answers),
        {
            "question_id": "Q-009",
            "answer_key": "metadata_only_sources",
            "label": "Which sources only need metadata?",
            "input_type": "multi_select",
            "required": False,
            "default": answers.get("metadata_only_sources") or ["systemd_unit", "file_metadata", "env_key_summary"],
            "options": ["systemd_unit", "file_metadata", "env_key_summary", "dependency_manifest", "deployment_marker"],
            "help": "Metadata-only collection avoids uploading raw body content where metadata is enough.",
            "affects": ["installed artifact query", "config metadata query"],
        },
        {
            "question_id": "Q-010",
            "answer_key": "excluded_paths",
            "label": "Paths to exclude completely",
            "input_type": "path_list",
            "required": False,
            "default": answers.get("excluded_paths") or ["credentials/", "*.pem", "*.key"],
            "help": "Excluded paths must not be collected. Raw credential files are never required.",
            "affects": ["file metadata collection", "artifact discovery"],
        },
    ]


def _config_metadata_question(answers: dict[str, Any]) -> dict[str, Any]:
    return {
        "question_id": "Q-008",
        "answer_key": "allow_config_metadata_only",
        "label": "Allow metadata-only config/env discovery?",
        "input_type": "boolean",
        "required": True,
        "default": bool(answers.get("allow_config_metadata_only", True)),
        "help": (
            "Raw env values and credentials are never collected or uploaded. This only allows key names "
            "or key hashes, value types, presence flags, and secret_like flags."
        ),
        "policy": {
            "raw_env_values_allowed": False,
            "credential_values_allowed": False,
            "allowed_extractions": ["key_name_or_hash", "value_type", "present", "secret_like"],
            "prohibited_extractions": [
                "raw_env_value",
                "credential_value",
                "private_key_body",
                "token_value",
                "cookie_value",
                "authorization_header_value",
            ],
        },
    }


def sample_planner_answers(plan: dict[str, Any]) -> dict[str, Any]:
    questions = {row["answer_key"]: row for row in plan.get("human_questions") or []}
    window = questions.get("incident_window", {}).get("default") or {}
    units = questions.get("confirmed_units", {}).get("default") or []
    components = questions.get("component_criticality", {}).get("items") or []
    return {
        "schema_version": ANSWERS_SCHEMA_VERSION,
        "plan_id": plan.get("plan_id") or "PLAN-001",
        "answered_by": "api-user",
        "answered_at": "",
        "answers": {
            "incident_window": window,
            "operator_display_timezone": plan.get("operator_display_timezone") or window.get("timezone") or "UTC",
            "log_retention_days": questions.get("log_retention_days", {}).get("default", 7),
            "available_granularity": questions.get("available_granularity", {}).get("default", []),
            "confirmed_units": units,
            "component_criticality": {
                str(item.get("component")): str(item.get("default") or "unknown") for item in components
            },
            "user_impact_signals": [],
            "allow_config_metadata_only": True,
            "metadata_only_sources": ["systemd_unit", "file_metadata", "env_key_summary"],
            "excluded_paths": ["credentials/", "*.pem", "*.key"],
        },
    }


def build_evidence_requirements(
    bundle: dict[str, Any],
    profile: dict[str, Any],
    *,
    canonical_review_graph: dict[str, Any] | None = None,
    source_analysis: dict[str, Any] | None = None,
    provider: ModelProvider | None = None,
) -> dict[str, Any]:
    graph = canonical_review_graph if isinstance(canonical_review_graph, dict) else {}
    source_analysis = source_analysis if isinstance(source_analysis, dict) else {}
    context = _evidence_requirement_context(bundle, profile, graph, source_analysis)
    deterministic = _deterministic_evidence_requirements(context)
    metadata: dict[str, Any] = {
        "schema_version": "evidence_requirements_metadata.v1",
        "generation_mode": "deterministic_gate",
        "llm_status": "not_requested",
        "requirement_count": len(deterministic),
        "deterministic_requirement_count": len(deterministic),
        "validation_warnings": [],
        "allowed_signal_count": len(context["allowed_signal_names"]),
        "allowed_evidence_ref_count": len(context["allowed_evidence_refs"]),
    }
    if provider is None:
        return {"requirements": deterministic, "metadata": metadata}

    generated = _generate_evidence_requirements_with_provider(provider, context)
    metadata.update(generated["metadata"])
    if generated["requirements"]:
        merged = _merge_requirement_targets(generated["requirements"], deterministic)
        metadata["generation_mode"] = "llm_with_deterministic_gate"
        metadata["requirement_count"] = len(merged)
        return {"requirements": merged, "metadata": metadata}
    metadata["generation_mode"] = "deterministic_gate_fallback"
    metadata["requirement_count"] = len(deterministic)
    return {"requirements": deterministic, "metadata": metadata}


def _evidence_requirement_context(
    bundle: dict[str, Any],
    profile: dict[str, Any],
    graph: dict[str, Any],
    source_analysis: dict[str, Any],
) -> dict[str, Any]:
    targets = [
        _requirement_target_context(target)
        for target in graph.get("validation_targets") or []
        if isinstance(target, dict)
    ]
    targets = [target for target in targets if target.get("promotion_blocked_reasons")]
    return {
        "schema_version": "evidence_requirement_context.v1",
        "evidence_sha256": str(bundle.get("evidence_sha256") or graph.get("evidence_sha256") or ""),
        "allowed_evidence_refs": _allowed_evidence_refs(bundle, graph),
        "allowed_signal_names": _available_signal_names(bundle, profile, source_analysis),
        "allowed_request_types": sorted(REQUEST_PRIORITIES),
        "blocked_sources": DO_NOT_REQUEST,
        "validation_targets": targets[:12],
        "agreement_dimensions": {
            key: graph.get("agreement_dimensions", {}).get(key)
            for key in (
                "provider_detection_overlap",
                "review_unit_convergence",
                "technical_baseline_agreement",
                "incident_baseline_agreement",
                "baseline_agreement",
                "cause_agreement",
                "impact_agreement",
            )
            if isinstance(graph.get("agreement_dimensions"), dict)
        },
        "instructions": {
            "llm_role": "Translate deterministic promotion-blocked reasons into evidence requirements.",
            "not_allowed": "Do not judge primary incident truth and do not invent metric or log names.",
        },
    }


def _requirement_target_context(target: dict[str, Any]) -> dict[str, Any]:
    return {
        "target_id": str(target.get("target_id") or target.get("review_target_id") or ""),
        "title": str(target.get("title") or ""),
        "canonical_review_unit": str(target.get("canonical_review_unit") or ""),
        "class": str(target.get("class") or ""),
        "review_priority_score": float(target.get("review_priority_score") or 0.0),
        "promotion_score": float(target.get("promotion_score") or target.get("review_priority_score") or 0.0),
        "baseline_support_score": float(target.get("baseline_support_score") or 0.0),
        "rollup_provider_ratio": float(target.get("rollup_provider_ratio") or 0.0),
        "recommended_request_type": str(target.get("recommended_request_type") or ""),
        "promotion_blocked_reasons": [
            str(reason)
            for reason in target.get("promotion_blocked_reasons") or []
            if str(reason).strip()
        ],
        "evidence_refs": [str(ref) for ref in target.get("evidence_refs") or [] if str(ref).strip()],
        "missing_evidence": [str(item) for item in target.get("missing_evidence") or [] if str(item).strip()],
        "rollup": target.get("rollup") if isinstance(target.get("rollup"), dict) else {},
    }


def _deterministic_evidence_requirements(context: dict[str, Any]) -> list[dict[str, Any]]:
    requirements: list[dict[str, Any]] = []
    allowed_signals = list(context.get("allowed_signal_names") or [])
    for target_index, target in enumerate(context.get("validation_targets") or [], start=1):
        if not isinstance(target, dict):
            continue
        reasons = [str(item) for item in target.get("promotion_blocked_reasons") or [] if str(item)]
        actionable_reasons = [reason for reason in reasons if reason in PROMOTION_REASON_REQUIREMENTS] or reasons[:1] or ["missing_evidence"]
        for reason_index, reason in enumerate(actionable_reasons[:3], start=1):
            spec = PROMOTION_REASON_REQUIREMENTS.get(reason) or {
                "evidence_type": "runtime_evidence",
                "source_kind": "metric_or_log",
                "maps_to_request_type": str(target.get("recommended_request_type") or "instrumentation_consistency_query"),
                "question": "What additional runtime evidence would close this promotion gate?",
                "acceptance": "Additional runtime evidence supports the gate during the incident window.",
                "rejection": "Additional runtime evidence is unavailable, healthy, or contradicts the gate.",
            }
            request_type = str(target.get("recommended_request_type") or spec["maps_to_request_type"])
            requirements.append(
                {
                    "schema_version": EVIDENCE_REQUIREMENTS_SCHEMA_VERSION,
                    "requirement_id": f"ER-{target_index:03d}-{reason_index:02d}",
                    "generation_source": "deterministic_gate",
                    "review_target_id": str(target.get("target_id") or ""),
                    "canonical_review_unit": str(target.get("canonical_review_unit") or ""),
                    "blocked_reason": reason,
                    "question_to_close": str(spec["question"]),
                    "required_evidence": [
                        {
                            "evidence_type": str(spec["evidence_type"]),
                            "source_kind": str(spec["source_kind"] if allowed_signals else "instrumentation_gap"),
                            "existing_signal_refs": list(target.get("evidence_refs") or [])[:6],
                            "allowed_signal_names": allowed_signals[:8],
                            "acceptance_criteria": str(spec["acceptance"]),
                            "rejection_criteria": str(spec["rejection"]),
                            "collection_mode": "manual_read_only",
                            "maps_to_request_type": request_type if request_type in REQUEST_PRIORITIES else str(spec["maps_to_request_type"]),
                            "instrumentation_gap": not bool(allowed_signals),
                        }
                    ],
                    "do_not_request": list(DO_NOT_REQUEST),
                    "fallback_if_unavailable": "Record the source as unavailable in planner answers and keep this target as validation-only.",
                }
            )
    return requirements


def _generate_evidence_requirements_with_provider(
    provider: ModelProvider,
    context: dict[str, Any],
) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        "llm_status": "not_started",
        "llm_provider": getattr(provider, "provider", ""),
        "llm_model_name": getattr(provider, "model_name", ""),
        "llm_prompt_name": getattr(provider, "prompt_name", ""),
        "validation_warnings": [],
    }
    model_input = {
        "llm_task": "evidence_requirement_planner",
        "schema_version": "evidence_requirement_model_input.v1",
        "requirement_context": context,
    }
    preflight = safety_preflight_for_model_input(model_input, filename="evidence_requirement_model_input.json")
    if not preflight.passed:
        metadata.update(
            {
                "llm_status": "blocked_by_safety_preflight",
                "failure_reason": preflight.failure_reason,
                "finding_types": list(preflight.finding_types),
            }
        )
        return {"requirements": [], "metadata": metadata}
    try:
        run = run_provider_with_retries(provider, model_input)
        metadata.update(run.retry_metadata())
        metadata["llm_status"] = run.response.status
        if run.response.status != "ok":
            return {"requirements": [], "metadata": metadata}
        parsed = _parse_requirement_model_output(run.response.raw_output)
        normalized = _normalize_model_requirements(parsed, context)
        metadata["validation_warnings"] = normalized["warnings"]
        metadata["llm_status"] = "ok" if normalized["requirements"] else "invalid_output"
        metadata["llm_raw_output_sha256"] = sha256_json({"raw_output": run.response.raw_output})
        return {"requirements": normalized["requirements"], "metadata": metadata}
    except Exception as exc:
        metadata.update(
            {
                "llm_status": "failed",
                "failure_reason": str(exc.__class__.__name__),
            }
        )
        return {"requirements": [], "metadata": metadata}


def _parse_requirement_model_output(raw_output: str) -> dict[str, Any]:
    repaired = repair_json_text(raw_output)
    payload = json.loads(repaired.text)
    if not isinstance(payload, dict):
        raise ValueError("evidence requirement model output must be an object")
    return payload


def _normalize_model_requirements(payload: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    allowed_target_ids = {
        str(target.get("target_id") or "")
        for target in context.get("validation_targets") or []
        if isinstance(target, dict)
    }
    target_by_id = {
        str(target.get("target_id") or ""): target
        for target in context.get("validation_targets") or []
        if isinstance(target, dict)
    }
    allowed_refs = set(context.get("allowed_evidence_refs") or [])
    allowed_signals = set(context.get("allowed_signal_names") or [])
    requirements: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    if payload.get("schema_version") != EVIDENCE_REQUIREMENTS_SCHEMA_VERSION:
        warnings.append(_requirement_warning("schema_version_mismatch", "Model output used an unexpected schema_version."))
    for index, row in enumerate(payload.get("requirements") or [], start=1):
        if not isinstance(row, dict):
            warnings.append(_requirement_warning("invalid_requirement", f"Requirement #{index} was not an object."))
            continue
        target_id = str(row.get("review_target_id") or "")
        if target_id not in allowed_target_ids:
            warnings.append(_requirement_warning("unknown_review_target_id", f"Requirement #{index} referenced an unknown review target."))
            continue
        target = target_by_id.get(target_id) or {}
        evidence_rows: list[dict[str, Any]] = []
        for evidence in row.get("required_evidence") or []:
            if not isinstance(evidence, dict):
                continue
            refs = [str(ref) for ref in evidence.get("existing_signal_refs") or [] if str(ref) in allowed_refs]
            requested_signals = [str(item) for item in evidence.get("allowed_signal_names") or [] if str(item)]
            accepted_signals = [name for name in requested_signals if name in allowed_signals]
            unknown_signals = [name for name in requested_signals if name not in allowed_signals]
            source_kind = str(evidence.get("source_kind") or "metric_or_log")
            if unknown_signals and not accepted_signals:
                source_kind = "instrumentation_gap"
            request_type = str(evidence.get("maps_to_request_type") or target.get("recommended_request_type") or "")
            if request_type not in REQUEST_PRIORITIES:
                request_type = str(target.get("recommended_request_type") or "instrumentation_consistency_query")
            evidence_rows.append(
                {
                    "evidence_type": str(evidence.get("evidence_type") or "runtime_evidence"),
                    "source_kind": source_kind,
                    "existing_signal_refs": refs,
                    "allowed_signal_names": accepted_signals,
                    "instrumentation_gap": source_kind == "instrumentation_gap",
                    "instrumentation_gap_names": unknown_signals,
                    "acceptance_criteria": str(evidence.get("acceptance_criteria") or "Collected evidence supports the gate during the incident window."),
                    "rejection_criteria": str(evidence.get("rejection_criteria") or "Collected evidence is absent, healthy, or contradictory."),
                    "collection_mode": str(evidence.get("collection_mode") or "manual_read_only"),
                    "maps_to_request_type": request_type,
                }
            )
        if not evidence_rows:
            warnings.append(_requirement_warning("empty_required_evidence", f"Requirement #{index} had no usable required_evidence."))
            continue
        requirements.append(
            {
                "schema_version": EVIDENCE_REQUIREMENTS_SCHEMA_VERSION,
                "requirement_id": str(row.get("requirement_id") or f"LLM-ER-{index:03d}"),
                "generation_source": "llm",
                "review_target_id": target_id,
                "canonical_review_unit": str(row.get("canonical_review_unit") or target.get("canonical_review_unit") or ""),
                "blocked_reason": str(row.get("blocked_reason") or (target.get("promotion_blocked_reasons") or ["missing_evidence"])[0]),
                "question_to_close": str(row.get("question_to_close") or "What evidence closes this promotion gate?"),
                "required_evidence": evidence_rows,
                "do_not_request": _dedupe([*DO_NOT_REQUEST, *(str(item) for item in row.get("do_not_request") or [])]),
                "fallback_if_unavailable": str(row.get("fallback_if_unavailable") or "Record unavailable evidence and keep the target as validation-only."),
            }
        )
    return {"requirements": requirements, "warnings": warnings}


def _merge_requirement_targets(
    generated: list[dict[str, Any]],
    deterministic: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    covered = {str(row.get("review_target_id") or "") for row in generated}
    merged = list(generated)
    for row in deterministic:
        if str(row.get("review_target_id") or "") not in covered:
            merged.append(row)
    return merged


def _requirement_warning(warning_type: str, message: str) -> dict[str, str]:
    return {"warning_type": warning_type, "message": message}


def _allowed_evidence_refs(bundle: dict[str, Any], graph: dict[str, Any]) -> list[str]:
    refs = [str(ref) for ref in (bundle.get("evidence_refs") or {}).keys() if str(ref)]
    for target in [
        *(graph.get("primary_targets") or []),
        *(graph.get("validation_targets") or []),
        *(graph.get("monitor_only") or []),
    ]:
        if isinstance(target, dict):
            refs.extend(str(ref) for ref in target.get("evidence_refs") or [] if str(ref))
    return _dedupe(refs)


def _available_signal_names(bundle: dict[str, Any], profile: dict[str, Any], source_analysis: dict[str, Any]) -> list[str]:
    names: list[str] = []
    names.extend(str(row.get("signal_type") or "") for row in _signals(bundle) if isinstance(row, dict))
    for row in bundle.get("metric_windows") or []:
        if isinstance(row, dict):
            names.append(str(row.get("metric_name") or ""))
    metric_semantics = profile.get("metric_semantics") if isinstance(profile.get("metric_semantics"), dict) else {}
    names.extend(str(name) for name in metric_semantics.keys())
    for candidate in source_analysis.get("collector_mapping_candidates") or []:
        if not isinstance(candidate, dict):
            continue
        params = candidate.get("params") if isinstance(candidate.get("params"), dict) else {}
        names.extend(_string_values(params.get("metric_names")))
        names.extend(_string_values(candidate.get("metric_names")))
    return _dedupe(name for name in names if name)


def render_collection_instructions(plan: dict[str, Any]) -> str:
    lines = [
        "# Evidence Request Plan",
        "",
        "## Plain-language summary",
        "- The current evidence was not enough to close the review automatically.",
        "- You do not need to run every item in this plan. Pick only requests that match data you actually have access to.",
        "- If a requested metric, dashboard, log source, or state file does not exist, record it as unavailable; do not invent a substitute signal.",
        "- The useful next step is to collect one small, read-only evidence export for the highest-priority request, sanitize it, build a child Evidence Bundle, and upload that child bundle for re-analysis.",
        "- If you only want to review the current result, you can ignore this plan and make a human review decision instead.",
        "",
        "## Immediate next step",
        "1. Choose one request below that your environment can actually answer.",
        "2. Replace placeholders such as `<START>`, `<END>`, `<UNIT>`, `<PROJECT_ROOT>`, and `<PROMETHEUS_JOB>` with real values.",
        "3. Replace `query_metrics ...` with your real metrics/export tool. It is not a command installed by this project.",
        "4. Save the raw export locally; do not upload raw output.",
        "5. Run sanitize, verify, build a child Evidence Bundle, then upload only that child bundle.",
        "",
        "## How to use this file",
        "- This is a collection checklist, not a shell script.",
        "- Do not paste the whole file into a terminal.",
        "- Commands are read-only templates. Replace placeholders such as `<START>`, `<END>`, `<UNIT>`, `<PROJECT_ROOT>`, and `<PROMETHEUS_JOB>` before running anything.",
        "- Templates that start with `query_metrics` are abstract metric queries, not a bundled command. Replace them with your metrics backend, dashboard export, BigQuery query, Prometheus query, or Cloud Monitoring export.",
        "- Run only commands that exist in your environment and that you have reviewed.",
        "",
        "## Safety Policy",
        "- Planner does not execute commands.",
        "- Raw outputs stay local.",
        "- Raw env values and credentials must not be collected.",
        "- Raw Authorization header values, Cookie values, token values, private key bodies, and credential files are blocked.",
        "- Sanitize before upload.",
        "- `ops-evidence verify-sanitized` is required.",
        "",
        "## Human Questions",
    ]
    for question in plan.get("human_questions") or []:
        lines.extend(
            [
                f"- **{question.get('question_id')} {question.get('label')}**",
                f"  - answer_key: `{question.get('answer_key')}`",
                f"  - input_type: `{question.get('input_type')}`",
                f"  - required: `{str(question.get('required')).lower()}`",
                f"  - help: {question.get('help')}",
            ]
        )
    if plan.get("evidence_requirements"):
        metadata = plan.get("evidence_requirements_metadata") if isinstance(plan.get("evidence_requirements_metadata"), dict) else {}
        lines.extend(
            [
                "",
                "## Evidence Requirements to Close Promotion Gates",
                f"- Generation mode: `{metadata.get('generation_mode') or 'unknown'}`",
                f"- Requirement count: `{len(plan.get('evidence_requirements') or [])}`",
                "- These requirements explain what evidence would strengthen or weaken a validation target.",
                "- They do not promote a primary incident by themselves.",
            ]
        )
        for requirement in plan.get("evidence_requirements") or []:
            if not isinstance(requirement, dict):
                continue
            lines.extend(
                [
                    "",
                    f"### {requirement.get('requirement_id')} {requirement.get('canonical_review_unit') or requirement.get('review_target_id')}",
                    f"- Review target: `{requirement.get('review_target_id')}`",
                    f"- Blocked reason: `{requirement.get('blocked_reason')}`",
                    f"- Question to close: {requirement.get('question_to_close')}",
                ]
            )
            for evidence in requirement.get("required_evidence") or []:
                if not isinstance(evidence, dict):
                    continue
                lines.extend(
                    [
                        f"  - Evidence type: `{evidence.get('evidence_type')}`",
                        f"    - source_kind: `{evidence.get('source_kind')}`",
                        f"    - maps_to_request_type: `{evidence.get('maps_to_request_type')}`",
                        f"    - allowed_signal_names: {', '.join(evidence.get('allowed_signal_names') or []) or 'none listed'}",
                        f"    - existing_signal_refs: {', '.join(evidence.get('existing_signal_refs') or []) or 'none'}",
                        f"    - acceptance_criteria: {evidence.get('acceptance_criteria')}",
                        f"    - rejection_criteria: {evidence.get('rejection_criteria')}",
                    ]
                )
                gap_names = evidence.get("instrumentation_gap_names") or []
                if gap_names:
                    lines.append(f"    - instrumentation_gap_names: {', '.join(gap_names)}")
            lines.append(f"- Fallback if unavailable: {requirement.get('fallback_if_unavailable')}")
    by_priority: OrderedDict[str, list[dict[str, Any]]] = OrderedDict()
    for request in plan.get("requests") or []:
        by_priority.setdefault(str(request.get("priority") or "P2"), []).append(request)
    for priority, requests in by_priority.items():
        lines.extend(["", f"## {priority} Requests"])
        for request in requests:
            lines.extend(
                [
                    f"### {request.get('request_id')} {request.get('request_type')}",
                    f"- What is needed: {', '.join(request.get('needed_data') or [])}",
                    f"- Why needed: {request.get('why_needed')}",
                    f"- Required granularity: {request.get('granularity', {}).get('required')}",
                    f"- Granularity reason: {request.get('granularity', {}).get('reason')}",
                    "- Suggested read-only command templates:",
                ]
            )
            for step in request.get("collection_steps") or []:
                command = str(step.get("command_template") or "")
                lines.append(f"  - `{command}`")
                lines.append(f"    - read_only: `{str(step.get('read_only')).lower()}`")
                lines.append(f"    - executes_now: `{str(step.get('executes_now')).lower()}`")
                note = _command_template_note(command)
                if note:
                    lines.append(f"    - note: {note}")
            lines.append("- Sanitization steps:")
            for step in request.get("post_collection_steps") or []:
                lines.append(f"  - `{step}`")
    lines.extend(
        [
            "",
            "## After collection",
            "1. Save raw output locally.",
            "2. Run `ops-evidence sanitize <raw_collection_dir> --out <sanitized_output_dir>`.",
            "3. Run `ops-evidence verify-sanitized <sanitized_output_dir>`.",
            "4. Build a child Evidence Bundle.",
            "5. Upload only the child Evidence Bundle.",
            "",
            "Human answers are operational context, not support evidence. Support claims must still cite `evidence_id`.",
        ]
    )
    return "\n".join(lines)


def _command_template_note(command: str) -> str:
    if not command:
        return ""
    if command.startswith("query_metrics "):
        return "`query_metrics` is a placeholder for your metrics backend; it is not installed by this project."
    if "<" in command and ">" in command:
        return "Template contains placeholders and is not ready to paste as-is."
    return ""


def validate_plan_payload_inputs(
    evidence_bundle: dict[str, Any],
    approved_profile: dict[str, Any],
    planner_answers: dict[str, Any] | None = None,
) -> dict[str, Any]:
    findings = []
    for name, payload in (
        ("evidence_bundle", evidence_bundle),
        ("approved_profile", approved_profile),
        ("planner_answers", planner_answers or {}),
    ):
        scan = scan_sanitized_text(f"{name}.json", canonical_json(payload))
        findings.extend(scan["findings"])
    return {"passed": not findings, "findings": findings}


def _build_requests(
    request_types: list[str],
    *,
    bundle: dict[str, Any],
    profile: dict[str, Any],
    source_analysis: dict[str, Any],
    profile_confidence: str,
    time_window: dict[str, Any],
    units: list[str],
    components: list[str],
    granularity: dict[str, str],
    answers: dict[str, Any],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for index, generic_request_type in enumerate(request_types, start=1):
        mapping = _domain_request_mapping(
            generic_request_type,
            profile=profile,
            source_analysis=source_analysis,
            profile_confidence=profile_confidence,
        )
        request_type = mapping.get("domain_request_type") or generic_request_type
        request = {
            "request_id": f"REQ-{index:03d}",
            "priority": _priority(generic_request_type, answers),
            "request_type": request_type,
            "generic_request_type": generic_request_type,
            "question": _request_question(request_type, generic_request_type),
            "why_needed": _why_needed(generic_request_type, bundle),
            "needed_data": _needed_data(request_type, generic_request_type),
            "time_window": time_window,
            "granularity": granularity if generic_request_type in _granular_request_types() else {
                **BASE_GRANULARITY,
                "required": "metadata + event excerpts",
            },
            "collection_steps": _collection_steps(
                request_type,
                generic_request_type,
                units,
                components,
                answers,
                profile=profile,
            ),
            "post_collection_steps": _post_collection_steps(),
            "human_inputs_required": _human_inputs_required(generic_request_type),
            "blocked_sources": BLOCKED_SOURCES,
        }
        if mapping:
            request.update(mapping)
        rows.append(request)
    return rows


def _request_types(
    signals: list[dict[str, Any]],
    profile: dict[str, Any],
    *,
    source_analysis: dict[str, Any] | None = None,
    profile_confidence: str = "unknown",
    canonical_review_graph: dict[str, Any] | None = None,
) -> list[str]:
    requested: list[str] = []

    def add(*values: str) -> None:
        for value in values:
            if value not in requested:
                requested.append(value)

    for request_type in request_types_from_canonical_graph(canonical_review_graph or {}):
        add(request_type)

    for signal in signals:
        signal_type = str(signal.get("signal_type") or "")
        target = str(signal.get("core_target_type") or "")
        enriched = enrich_signal(signal_type, signal)
        if signal_type in {"missing_command", "missing_file", "config_error"} or target == "job_configuration_mismatch":
            add("installed_artifact_query", "process_state_query", "scheduler_history_query", "deployment_correlation_query")
        if signal_type in {"restart_loop", "process_exit", "service_start_failure"} or target in {"restart_loop", "service_start_failure"}:
            add("process_state_query", "scheduler_history_query")
        if signal_type in {"throughput_disappearance"} or target == "throughput_disappearance":
            add("throughput_signal_query", "process_state_query", "instrumentation_consistency_query")
        if signal_type in {"monitoring_gap", "instrumentation_mismatch"} or target in {"monitoring_gap", "instrumentation_mismatch"}:
            add("instrumentation_consistency_query", "log_completeness_query")
        if target in {"freshness_signal_gap", "user_impact_signal_gap"} or enriched.get("can_generate_request"):
            add("freshness_signal_query", "user_impact_signal_query", "external_dependency_status_query")
        if target in {"external_dependency_failure", "network_error_signal"}:
            add("external_dependency_status_query")
    collector_mappings = profile.get("collector_mappings") if isinstance(profile.get("collector_mappings"), dict) else {}
    for request_type in collector_mappings:
        if request_type in REQUEST_PRIORITIES:
            add(request_type)
    if profile_confidence == "explicit" and source_analysis:
        for candidate in source_analysis.get("collector_mapping_candidates") or []:
            if not isinstance(candidate, dict):
                continue
            request_type = str(candidate.get("request_type") or candidate.get("generic_request_type") or "")
            request_type = SOURCE_ANALYSIS_REQUEST_ALIASES.get(request_type, request_type)
            if request_type in REQUEST_PRIORITIES:
                add(request_type)
    if not requested:
        add("log_completeness_query", "instrumentation_consistency_query")
    return requested


def _collection_steps(
    request_type: str,
    generic_request_type: str,
    units: list[str],
    components: list[str],
    answers: dict[str, Any],
    *,
    profile: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    unit_hint = ",".join(units) if units else "<UNIT>"
    component_hint = ",".join(components) if components else "<COMPONENT>"
    common_limits = {"max_lines": 1000, "max_output_bytes": 1048576, "timeout_seconds": 10}
    source_first_steps = [
        ("source_context_metadata", "ops-evidence sanitize-source --project-root <PROJECT_ROOT> --service <service> --environment <env> --out <source_context_dir>"),
        ("source_analysis_metadata", "ops-evidence analyze-source --source-context <source_context_bundle> --provider local --out <source_analysis_dir>"),
    ]
    profile_metrics = _profile_metric_names(
        profile or {},
        request_type=request_type,
        generic_request_type=generic_request_type,
    )
    youtube_metrics = _metrics_for_stream_v3_request("youtube_ingest_status_query", profile_metrics)
    audio_metrics = _metrics_for_stream_v3_request("audio_energy_gap_query", profile_metrics)
    freshness_metrics = _metrics_for_stream_v3_request("capture_freshness_drift_query", profile_metrics)
    rtmps_metrics = _metrics_for_stream_v3_request("rtmps_reconnect_consistency_query", profile_metrics)
    templates = {
        "process_state_query": [
            ("local_systemd_metadata", "systemctl show <UNIT> --property=ActiveState,SubState,ExecMainStatus,NRestarts"),
            ("local_journal_excerpt", "journalctl -u <UNIT> --since <START> --until <END> --no-pager -o json"),
        ],
        "ffmpeg_process_state_query": [
            ("local_systemd_metadata", "systemctl show <UNIT> --property=ActiveState,SubState,ExecMainStatus,NRestarts"),
            ("local_process_metadata", "ps -eo pid,etime,comm --no-headers"),
        ],
        "pubsub_listener_state_query": [
            ("local_systemd_metadata", "systemctl show <UNIT> --property=ActiveState,SubState,ExecMainStatus,NRestarts"),
        ],
        "installed_artifact_query": [
            ("local_systemd_unit_metadata", "systemctl cat <UNIT> --no-pager"),
            ("local_artifact_metadata", "stat --printf '%n %F %a %s %Y\n' <ARTIFACT_PATH>"),
        ],
        "execstart_artifact_metadata_query": source_first_steps + [
            ("local_artifact_metadata", "stat --printf '%F %a %s %Y\n' <ARTIFACT_PATH>"),
        ],
        "systemd_unit_definition_query": source_first_steps,
        "scheduler_history_query": [
            ("local_systemd_timer_metadata", "systemctl list-timers --all --no-pager --plain"),
            ("local_journal_scheduler_excerpt", "journalctl -u <UNIT> --since <START> --until <END> --no-pager -o json"),
        ],
        "restart_loop_journal_query": [
            ("local_journal_scheduler_excerpt", "journalctl -u <UNIT> --since <START> --until <END> --no-pager -o json"),
        ],
        "deployment_correlation_query": [
            ("local_git_metadata", "git log --since <START> --until <END> --oneline --decorate --max-count 50"),
            ("local_file_mtime_metadata", "find <PROJECT_ROOT> -maxdepth 3 -type f -printf '%T@ %p\n' | sort -nr | head -100"),
        ],
        "external_dependency_status_query": source_first_steps,
        "youtube_ingest_status_query": _prometheus_metric_steps("local_dependency_status_metric_export", youtube_metrics),
        "gmail_watch_status_query": [
            ("local_dependency_status_metadata", "query_metrics --metric gmail_watch_status --start <START> --end <END> --bucket 1m"),
        ],
        "discord_webhook_delivery_query": [
            ("local_dependency_status_metadata", "query_metrics --metric discord_webhook_delivery --start <START> --end <END> --bucket 1m"),
        ],
        "throughput_signal_query": [
            ("local_metric_bucket_export", "query_metrics --metric <METRIC_NAME> --start <START> --end <END> --bucket 1m"),
        ],
        "freshness_signal_query": [
            ("local_freshness_metadata", "find <STATE_DIR> -type f -printf '%p %T@ %s\n' | sort -nr | head -100"),
        ],
        "capture_freshness_drift_query": [
            ("local_freshness_metadata", "find <STATE_DIR> -type f -printf '%p %T@ %s\n' | sort -nr | head -100"),
            *_prometheus_metric_steps("local_freshness_metric_export", freshness_metrics),
        ],
        "user_impact_signal_query": [
            ("local_user_impact_metric_export", "query_metrics --metric <USER_IMPACT_METRIC> --start <START> --end <END> --bucket 1m"),
        ],
        "audio_energy_gap_query": _prometheus_metric_steps("local_user_impact_metric_export", audio_metrics),
        "rtmps_reconnect_consistency_query": _prometheus_metric_steps("local_metric_bucket_export", rtmps_metrics) + source_first_steps,
        "instrumentation_consistency_query": source_first_steps + [
            ("local_sanitized_count_check", "wc -l <SANITIZED_EVENTS_JSONL>"),
        ],
        "log_completeness_query": [
            ("local_log_presence_metadata", "find <LOG_ROOT> -type f -newermt <START> ! -newermt <END> -printf '%p %s %T@\n'"),
            ("local_sanitized_event_count", "ops-evidence inspect <RAW_COLLECTION_DIR>"),
        ],
    }
    output = []
    for collector, template in templates.get(request_type, templates.get(generic_request_type, [])):
        step = {
            "collector": collector,
            "command_template": template,
            "read_only": True,
            "executes_now": False,
            "limits": common_limits if "journal" in collector else {},
            "source_first_preferred": template.startswith("ops-evidence sanitize-source") or template.startswith("ops-evidence analyze-source"),
            "substitution_hints": {
                "units": units,
                "unit_hint": unit_hint,
                "component_hint": component_hint,
                "metadata_only": bool(answers.get("allow_config_metadata_only", True)),
            },
        }
        if "grep -R" in template or " rg " in f" {template} ":
            step["unsafe_if_raw_output_uploaded"] = True
            step["warning"] = "Do not upload raw grep output. If local grep is used, sanitize and verify the result before creating a child Evidence Bundle."
        metric_names = _metric_names_from_promql_template(template)
        if metric_names:
            step["substitution_hints"]["metric_names"] = metric_names
        output.append(step)
    return output


def _metrics_for_stream_v3_request(request_type: str, profile_metrics: list[str]) -> list[str]:
    allowed = [name for name in profile_metrics if name not in BLOCKED_COLLECTION_METRIC_NAMES]
    return _dedupe([*allowed, *STREAM_V3_FALLBACK_METRICS.get(request_type, [])])


def _prometheus_metric_steps(collector: str, metric_names: list[str]) -> list[tuple[str, str]]:
    names = [name for name in _dedupe(metric_names) if _safe_prometheus_metric_name(name)]
    if not names:
        return []
    regex = "|".join(names)
    promql = f'{{__name__=~"{regex}",job="<PROMETHEUS_JOB>"}}'
    return [(collector, f"query_metrics --promql '{promql}' --start <START> --end <END> --bucket 1m")]


def _profile_metric_names(
    profile: dict[str, Any],
    *,
    request_type: str,
    generic_request_type: str,
) -> list[str]:
    request_ids = _request_metric_lookup_ids(request_type, generic_request_type)
    metrics: list[str] = []
    for row in profile.get("operational_evidence_specs") or []:
        if not isinstance(row, dict):
            continue
        row_ids = {
            str(row.get("request_id") or ""),
            str(row.get("profile_request_id") or ""),
            str(row.get("need") or ""),
        }
        if request_ids & row_ids:
            metrics.extend(_string_values(row.get("metric_names")))
    evidence_requests = profile.get("evidence_requests") if isinstance(profile.get("evidence_requests"), dict) else {}
    for key, row in evidence_requests.items():
        if not isinstance(row, dict):
            continue
        row_ids = {str(key), str(row.get("request_id") or ""), str(row.get("profile_request_id") or "")}
        if request_ids & row_ids:
            metrics.extend(_string_values(row.get("metric_names")))
    return _dedupe(metrics)


def _request_metric_lookup_ids(request_type: str, generic_request_type: str) -> set[str]:
    values = {request_type, generic_request_type}
    values.update(PROFILE_REQUEST_ALIASES.get(request_type, ()))
    values.update(PROFILE_REQUEST_ALIASES.get(generic_request_type, ()))
    return {value for value in values if value}


def _string_values(values: Any) -> list[str]:
    if not isinstance(values, list):
        return []
    return [str(value) for value in values if str(value)]


def _dedupe(values: Iterable[str]) -> list[str]:
    output: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value and value not in seen:
            output.append(value)
            seen.add(value)
    return output


def _safe_prometheus_metric_name(value: str) -> bool:
    return bool(value) and all(ch.isalnum() or ch in "_:" for ch in value)


def _metric_names_from_promql_template(template: str) -> list[str]:
    marker = '__name__=~"'
    if marker not in template:
        return []
    metric_regex = template.split(marker, 1)[1].split('"', 1)[0]
    return [value for value in metric_regex.split("|") if value]


def _post_collection_steps() -> list[str]:
    return [
        "ops-evidence sanitize <raw_collection_dir> --out <sanitized_output_dir>",
        "ops-evidence verify-sanitized <sanitized_output_dir>",
        "ops-evidence build-bundle <sanitized_events.jsonl> --service <service> --environment <env> --start <start> --end <end> --profile <approved_profile> --parent-evidence-sha256 <parent_sha> --out <child_evidence_bundle.json>",
    ]


def _request_question(request_type: str, generic_request_type: str | None = None) -> str:
    return {
        "process_state_query": "Was the process or unit actually stopped, restarting, or healthy during the window?",
        "ffmpeg_process_state_query": "Was the ffmpeg streaming process healthy or restarting during the incident window?",
        "pubsub_listener_state_query": "Was the Pub/Sub listener healthy and receiving work during the window?",
        "installed_artifact_query": "Did the configured command or artifact exist with expected permissions and mtime?",
        "execstart_artifact_metadata_query": "Did the approved ExecStart target resolve to the expected deployed artifact metadata?",
        "systemd_unit_definition_query": "Does the sanitized systemd unit metadata match the approved profile mapping?",
        "scheduler_history_query": "Did the scheduler or timer run as expected during the window?",
        "restart_loop_journal_query": "Did the journal show restart-loop behavior during the incident window?",
        "deployment_correlation_query": "Did deployment or config metadata change near the incident window?",
        "external_dependency_status_query": "Was an external dependency unavailable or only a local signal missing?",
        "youtube_ingest_status_query": "Was YouTube ingest unhealthy, or was only local instrumentation missing?",
        "gmail_watch_status_query": "Was the Gmail watch/listener state healthy during the incident window?",
        "discord_webhook_delivery_query": "Did Discord webhook delivery fail or only local notification instrumentation fail?",
        "throughput_signal_query": "Did throughput disappear, or did only the metric disappear?",
        "freshness_signal_query": "Did freshness drift beyond the acceptable window?",
        "capture_freshness_drift_query": "Did capture freshness drift beyond the approved stream_v3 window?",
        "user_impact_signal_query": "Which collected signal is closest to actual user impact?",
        "audio_energy_gap_query": "Did audio energy disappear in a way that could affect viewers?",
        "rtmps_reconnect_consistency_query": "Do RTMPS reconnect metrics, logs, and source instrumentation describe the same state?",
        "instrumentation_consistency_query": "Do logs, metrics, and instrumentation agree about the same state?",
        "log_completeness_query": "Are the expected logs present and complete after sanitization?",
    }.get(request_type or generic_request_type or "", "What additional read-only evidence is needed?")


def _why_needed(request_type: str, bundle: dict[str, Any]) -> str:
    signal_text = ", ".join(sorted({str(row.get("signal_type")) for row in _signals(bundle) if row.get("signal_type")}))
    reasons = {
        "installed_artifact_query": "Missing command evidence needs artifact metadata to distinguish bad config from missing deployment.",
        "process_state_query": "Log patterns alone cannot prove current process state or restart count.",
        "scheduler_history_query": "Scheduler evidence is needed to separate a one-off failed start from repeated job failure.",
        "deployment_correlation_query": "Deployment/config timing can explain when the mismatch was introduced.",
        "external_dependency_status_query": "Dependency status separates external outage from local instrumentation gaps.",
        "throughput_signal_query": "Metric drops require bucket-level data to distinguish true disappearance from aggregation loss.",
        "freshness_signal_query": "Freshness gaps need timestamp drift around the incident window.",
        "user_impact_signal_query": "User impact must be tied to a signal closer to the user outcome.",
        "instrumentation_consistency_query": "Instrumentation mismatch needs source and aggregation checks before diagnosis.",
        "log_completeness_query": "Missing logs can be monitoring gaps rather than system recovery.",
    }
    base = reasons.get(request_type, "Additional evidence is needed before making a supported claim.")
    return f"{base} Current signals: {signal_text or 'unknown'}."


def _needed_data(request_type: str, generic_request_type: str | None = None) -> list[str]:
    values = {
        "installed_artifact_query": ["systemd unit definition", "ExecStart template", "target artifact exists / mode / mtime", "unit file mtime"],
        "execstart_artifact_metadata_query": ["sanitized source context", "ExecStart template", "artifact metadata", "deployment marker"],
        "systemd_unit_definition_query": ["sanitized source context", "systemd unit metadata", "collector mapping confirmation"],
        "process_state_query": ["systemd ActiveState/SubState", "ExecMainStatus", "NRestarts", "journal lines around process exit"],
        "ffmpeg_process_state_query": ["ffmpeg process state", "streaming unit state", "restart count", "journal status"],
        "pubsub_listener_state_query": ["listener unit state", "subscription lag metric", "journal status"],
        "scheduler_history_query": ["timer status", "last trigger time", "journal lines around scheduled run", "run result"],
        "restart_loop_journal_query": ["journal restart excerpts", "timer status", "NRestarts"],
        "deployment_correlation_query": ["git/deploy timestamp", "unit file mtime", "artifact mtime", "config metadata timestamp"],
        "external_dependency_status_query": ["dependency metadata", "health endpoint status if configured", "connection error timing"],
        "youtube_ingest_status_query": ["YouTube ingest health", "public/live status", "stream active flag"],
        "gmail_watch_status_query": ["Gmail watch status", "listener health", "subscription freshness"],
        "discord_webhook_delivery_query": ["webhook delivery status", "sanitized notification failure counts", "external status metadata"],
        "throughput_signal_query": ["metric buckets around drop", "process state", "log/metric consistency", "event ordering"],
        "freshness_signal_query": ["freshness timestamp drift", "state file mtime", "collector run timestamp"],
        "capture_freshness_drift_query": ["capture freshness timestamp", "state file mtime", "collector run timestamp"],
        "user_impact_signal_query": ["user-impact metric buckets", "delivery/ingest/watch status", "external dependency status"],
        "audio_energy_gap_query": ["audio energy metric buckets", "stream state", "viewer-visible delivery status"],
        "rtmps_reconnect_consistency_query": ["RTMPS reconnect metric", "transport log pattern", "source instrumentation mapping"],
        "instrumentation_consistency_query": ["metric aggregation source", "bucket granularity", "sanitized event count", "missing label checks"],
        "log_completeness_query": ["raw event count after sanitize", "expected log files metadata", "retention window", "missing log source check"],
    }
    return values.get(request_type) or values.get(generic_request_type or "") or ["read-only evidence rows"]


def _human_inputs_required(request_type: str) -> list[str]:
    required = ["Confirm the incident time window and timezone."]
    if request_type in {"process_state_query", "scheduler_history_query", "installed_artifact_query"}:
        required.append("Confirm the correct systemd unit, container, or process name.")
    if request_type in {"throughput_signal_query", "freshness_signal_query", "user_impact_signal_query"}:
        required.append("Confirm whether 1m metric buckets are available.")
    if request_type in {"installed_artifact_query", "deployment_correlation_query", "instrumentation_consistency_query"}:
        required.append("Confirm metadata-only config/env extraction is allowed.")
    return required


def _priority(request_type: str, answers: dict[str, Any]) -> str:
    if request_type == "user_impact_signal_query" and answers.get("user_impact_signals"):
        return "P1"
    return REQUEST_PRIORITIES.get(request_type, "P2")


def _granular_request_types() -> set[str]:
    return {
        "throughput_signal_query",
        "freshness_signal_query",
        "user_impact_signal_query",
        "instrumentation_consistency_query",
        "log_completeness_query",
        "process_state_query",
    }


def _profile_confidence(bundle: dict[str, Any], profile: dict[str, Any]) -> str:
    approval = profile.get("profile_discovery_approval") if isinstance(profile.get("profile_discovery_approval"), dict) else {}
    review_policy = profile.get("review_policy") if isinstance(profile.get("review_policy"), dict) else {}
    if profile.get("profile_id") and (approval.get("explicit_profile") is True or review_policy.get("profile_draft_approved") is True or profile.get("explicit_profile") is True):
        return "explicit"
    source = bundle.get("source") if isinstance(bundle.get("source"), dict) else {}
    return str(source.get("profile_confidence") or "unknown")


def _time_window(bundle: dict[str, Any], answers: dict[str, Any]) -> dict[str, Any]:
    answer_window = answers.get("incident_window") if isinstance(answers.get("incident_window"), dict) else {}
    bundle_window = bundle.get("time_window") if isinstance(bundle.get("time_window"), dict) else {}
    return {
        "start": str(answer_window.get("start") or bundle_window.get("start") or ""),
        "end": str(answer_window.get("end") or bundle_window.get("end") or ""),
        "timezone": str(answer_window.get("timezone") or "UTC"),
    }


def _operator_display_timezone(answers: dict[str, Any], time_window: dict[str, Any]) -> str:
    return str(answers.get("operator_display_timezone") or time_window.get("timezone") or "UTC")


def _granularity(answers: dict[str, Any]) -> dict[str, str]:
    available = answers.get("available_granularity") if isinstance(answers.get("available_granularity"), list) else []
    required = "event_level_journal + 1m_metric_buckets"
    if "one_minute_metric_buckets" not in available and "fifteen_minute_aggregates" in available:
        required = "event_level_journal + 15m_aggregate_with_gap_notes"
    return {
        "current": ", ".join(available) if available else BASE_GRANULARITY["current"],
        "required": required,
        "reason": BASE_GRANULARITY["reason"],
    }


def _confirmed_units(bundle: dict[str, Any], profile: dict[str, Any], answers: dict[str, Any]) -> list[str]:
    answer_units = answers.get("confirmed_units")
    if isinstance(answer_units, list) and answer_units:
        return sorted({str(item) for item in answer_units if item})
    units = set()
    for signal in _signals(bundle):
        component = str(signal.get("component") or "")
        if component.endswith(".service"):
            units.add(component)
    for item in bundle.get("evidence_items") or []:
        if isinstance(item, dict):
            component = str(item.get("component") or "")
            if component.endswith(".service"):
                units.add(component)
    collector_mappings = profile.get("collector_mappings") if isinstance(profile.get("collector_mappings"), dict) else {}
    for mapping in collector_mappings.values():
        if isinstance(mapping, dict):
            params = mapping.get("params") if isinstance(mapping.get("params"), dict) else {}
            for unit in params.get("units") or []:
                units.add(str(unit))
    return sorted(units)


def _component_names(bundle: dict[str, Any], profile: dict[str, Any], units: list[str]) -> list[str]:
    components = set(units)
    component_map = profile.get("component_map") if isinstance(profile.get("component_map"), dict) else {}
    components.update(str(value.get("name") or key) if isinstance(value, dict) else str(key) for key, value in component_map.items())
    for signal in _signals(bundle):
        if signal.get("component"):
            components.add(str(signal["component"]))
    return sorted(item for item in components if item)


def _signals(bundle: dict[str, Any]) -> list[dict[str, Any]]:
    return [row for row in bundle.get("signals") or [] if isinstance(row, dict)]


def enrich_signal(signal: str, row: dict[str, Any] | None = None) -> dict[str, Any]:
    name = str(signal or "").strip()
    key = name.casefold()
    signal_class, user_impact, can_generate = SIGNAL_ENRICHMENT_RULES.get(
        key,
        ("diagnostic_or_domain_signal", False, False),
    )
    if row:
        target = str(row.get("core_target_type") or "").casefold()
        if target in {"user_impact_signal_gap", "external_dependency_failure", "network_error_signal"} and key not in SEVERITY_ONLY_SIGNALS:
            user_impact = True
            can_generate = True
    return {
        "signal": name,
        "signal_class": signal_class,
        "can_be_user_impact_signal": bool(user_impact),
        "can_generate_request": bool(can_generate),
    }


def enrich_signals(bundle: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for signal in _signals(bundle):
        name = str(signal.get("signal_type") or "")
        if not name or name in seen:
            continue
        seen.add(name)
        enriched = enrich_signal(name, signal)
        enriched["source_signal_id"] = str(signal.get("signal_id") or "")
        enriched["source_component"] = str(signal.get("component") or "")
        rows.append(enriched)
    return rows


def _signal_names(bundle: dict[str, Any]) -> list[str]:
    return sorted(
        row["signal"]
        for row in enrich_signals(bundle)
        if row.get("can_be_user_impact_signal") is True
    )


def _component_criticality_default(answers: dict[str, Any], component: str) -> str:
    values = answers.get("component_criticality")
    if isinstance(values, dict):
        value = str(values.get(component) or "unknown")
        if value in {"critical_path", "diagnostic_only", "unknown"}:
            return value
    return "unknown"


def _raw_answers(payload: dict[str, Any] | None) -> dict[str, Any]:
    if not payload:
        return {}
    answers = payload.get("answers") if isinstance(payload.get("answers"), dict) else payload
    return dict(answers) if isinstance(answers, dict) else {}


def _answers(payload: dict[str, Any] | None) -> dict[str, Any]:
    answers = _raw_answers(payload)
    if "operator_display_timezone" not in answers and "timezone" in answers:
        answers["operator_display_timezone"] = answers["timezone"]
    return answers


def _domain_request_mapping(
    generic_request_type: str,
    *,
    profile: dict[str, Any],
    source_analysis: dict[str, Any],
    profile_confidence: str,
) -> dict[str, Any]:
    if profile_confidence != "explicit":
        return {}
    source_mapping = _source_analysis_domain_mapping(generic_request_type, source_analysis)
    if source_mapping:
        return source_mapping
    mapping_source = "approved_profile.collector_mappings"
    text = canonical_json(profile).casefold() + "\n" + canonical_json(source_analysis or {}).casefold()
    domain_map: dict[str, str] = {}
    if any(token in text for token in ("stream_v3", "stream-v3", "rtmps", "ffmpeg", "youtube", "audio_energy", "capture_freshness")):
        domain_map = STREAM_V3_DOMAIN_REQUESTS
        mapping_source = "approved_profile.collector_mappings+source_analysis.collector_mapping_candidates"
    elif any(token in text for token in ("amazon", "gmail", "discord", "pubsub", "notify", "webhook")):
        domain_map = AMAZON_NOTIFY_DOMAIN_REQUESTS
        mapping_source = "approved_profile.collector_mappings+source_analysis.collector_mapping_candidates"
    domain_request_type = domain_map.get(generic_request_type, "")
    if not domain_request_type or domain_request_type == generic_request_type:
        return {}
    return _domain_mapping_payload(generic_request_type, domain_request_type, mapping_source)


def _source_analysis_domain_mapping(generic_request_type: str, source_analysis: dict[str, Any]) -> dict[str, Any]:
    if not source_analysis:
        return {}
    candidates = [row for row in source_analysis.get("collector_mapping_candidates") or [] if isinstance(row, dict)]
    matching: list[dict[str, Any]] = []
    for row in candidates:
        request_type = str(row.get("generic_request_type") or row.get("request_type") or "")
        request_type = SOURCE_ANALYSIS_REQUEST_ALIASES.get(request_type, request_type)
        explicit_domain = str(
            row.get("domain_request_type")
            or row.get("mapped_request_type")
            or row.get("recommended_request_type")
            or ""
        )
        if explicit_domain in DOMAIN_REQUEST_TYPES and request_type in {"", generic_request_type}:
            return _domain_mapping_payload(
                generic_request_type,
                explicit_domain,
                "source_analysis.collector_mapping_candidates",
                matched_source_analysis=True,
            )
        if request_type == generic_request_type:
            matching.append(row)
    text = canonical_json({"matching": matching, "all": candidates}).casefold()
    domain_request_type = _infer_domain_request_from_text(generic_request_type, text)
    if not domain_request_type:
        text = canonical_json(source_analysis).casefold()
        domain_request_type = _infer_domain_request_from_text(generic_request_type, text)
    if not domain_request_type:
        return {}
    return _domain_mapping_payload(
        generic_request_type,
        domain_request_type,
        "source_analysis.collector_mapping_candidates",
        matched_source_analysis=bool(matching),
    )


def _infer_domain_request_from_text(generic_request_type: str, text: str) -> str:
    if generic_request_type == "external_dependency_status_query":
        if "youtube" in text or "ingest" in text:
            return "youtube_ingest_status_query"
        if "gmail" in text or "watch" in text:
            return "gmail_watch_status_query"
        if "discord" in text or "webhook" in text or "notification" in text:
            return "discord_webhook_delivery_query"
    if generic_request_type == "user_impact_signal_query":
        if "audio" in text or "audio_energy" in text:
            return "audio_energy_gap_query"
        if "stream" in text or "youtube" in text or "ingest" in text:
            return "youtube_ingest_status_query"
        if "notification" in text or "discord" in text or "webhook" in text:
            return "discord_webhook_delivery_query"
    if generic_request_type == "freshness_signal_query":
        if "capture" in text or "freshness" in text:
            return "capture_freshness_drift_query"
    if generic_request_type == "process_state_query":
        if "ffmpeg" in text or "rtmps" in text:
            return "ffmpeg_process_state_query"
        if "pubsub" in text or "listener" in text or "subscription" in text:
            return "pubsub_listener_state_query"
    if generic_request_type == "scheduler_history_query" and any(token in text for token in ("restart", "loop", "watchdog")):
        return "restart_loop_journal_query"
    if generic_request_type == "installed_artifact_query" and any(token in text for token in ("execstart", "systemd", ".service", "artifact")):
        return "execstart_artifact_metadata_query"
    if generic_request_type == "instrumentation_consistency_query":
        if "rtmps" in text or "reconnect" in text:
            return "rtmps_reconnect_consistency_query"
        if "systemd" in text or ".service" in text or "execstart" in text:
            return "systemd_unit_definition_query"
    return ""


def _domain_mapping_payload(
    generic_request_type: str,
    domain_request_type: str,
    mapped_by: str,
    *,
    matched_source_analysis: bool = False,
) -> dict[str, Any]:
    if not domain_request_type or domain_request_type == generic_request_type:
        return {}
    payload: dict[str, Any] = {
        "domain_mapping_applied": True,
        "domain_request_type": domain_request_type,
        "generic_request_type": generic_request_type,
        "mapped_by": mapped_by,
    }
    if matched_source_analysis:
        payload["matched_source_analysis"] = True
    return payload


def planner_quality_warnings(
    plan: dict[str, Any],
    *,
    bundle: dict[str, Any],
    profile: dict[str, Any],
    source_analysis: dict[str, Any],
    answers: dict[str, Any],
    raw_answers: dict[str, Any],
) -> list[dict[str, Any]]:
    warnings: list[dict[str, Any]] = []
    incident_tz = str((answers.get("incident_window") if isinstance(answers.get("incident_window"), dict) else {}).get("timezone") or plan.get("incident_window", {}).get("timezone") or "UTC")
    legacy_tz = raw_answers.get("timezone")
    if legacy_tz and str(legacy_tz) != incident_tz:
        warnings.append(_planner_warning(
            "timezone_conflict",
            "warning",
            "Legacy timezone differs from incident_window.timezone and is treated as operator_display_timezone.",
            "Use incident_window.timezone for collection and operator_display_timezone for UI/report display.",
        ))
    for question in plan.get("human_questions") or []:
        if question.get("input_type") == "single_select" and isinstance(question.get("default"), (dict, list)):
            warnings.append(_planner_warning(
                "single_select_default_type_mismatch",
                "warning",
                "single_select questions must not use object/list defaults.",
                "Use component_map_select for per-component values or a scalar default for single_select.",
            ))
    q7 = next((row for row in plan.get("human_questions") or [] if row.get("answer_key") == "user_impact_signals"), {})
    severity_options = sorted({str(item) for item in q7.get("options") or [] if str(item).casefold() in SEVERITY_ONLY_SIGNALS})
    if severity_options:
        warnings.append(_planner_warning(
            "user_impact_option_is_severity",
            "warning",
            f"{', '.join(severity_options)} are severity labels, not user impact signals.",
            "Filter severity-only signals from user_impact_signals.",
        ))
    profile_confidence = str(plan.get("profile_confidence") or "")
    requests = [row for row in plan.get("requests") or [] if isinstance(row, dict)]
    if profile_confidence == "explicit" and requests and not any(row.get("domain_mapping_applied") for row in requests):
        warnings.append(_planner_warning(
            "generic_plan_despite_explicit_profile",
            "warning",
            "The profile is explicit but planner generated only generic requests.",
            "Check approved_profile.collector_mappings and source_analysis.collector_mapping_candidates.",
        ))
    raw_search = False
    unresolved = False
    for request in requests:
        for step in request.get("collection_steps") or []:
            template = str(step.get("command_template") or "")
            if "grep -R" in template or " rg " in f" {template} ":
                raw_search = True
            if "<" in template and ">" in template:
                unresolved = True
    if raw_search:
        warnings.append(_planner_warning(
            "raw_search_template_present",
            "warning",
            "Raw recursive search templates require strict local-only handling.",
            "Prefer sanitize-source -> analyze-source. Do not upload raw grep output.",
        ))
    if unresolved:
        warnings.append(_planner_warning(
            "unresolved_command_placeholder",
            "info",
            "Command templates contain placeholders for human substitution.",
            "Confirm all placeholders locally before collection; planner does not execute commands.",
        ))
    if _plan_uses_context_as_evidence(plan):
        warnings.append(_planner_warning(
            "context_used_as_evidence",
            "error",
            "Context fields appear in support evidence positions.",
            "Use Source Context only for mapping/interpretation and cite Evidence Items for runtime support.",
        ))
    if _plan_has_support_claim_without_evidence_id(plan):
        warnings.append(_planner_warning(
            "support_claim_without_evidence_id",
            "error",
            "A runtime support claim is missing evidence_id.",
            "Attach Evidence Items with evidence_id or mark the claim as missing evidence.",
        ))
    confirmed_units = next((row for row in plan.get("human_questions") or [] if row.get("answer_key") == "confirmed_units"), {})
    needs_units = any((row.get("generic_request_type") or row.get("request_type")) in {"process_state_query", "scheduler_history_query", "installed_artifact_query"} for row in requests)
    if needs_units and confirmed_units.get("required") is True and not confirmed_units.get("default"):
        warnings.append(_planner_warning(
            "empty_required_confirmed_units",
            "warning",
            "Unit/process confirmation is required but no default unit was available.",
            "Confirm units manually before executing any local collection template.",
        ))
    return _dedupe_warnings(warnings)


def _planner_warning(warning_type: str, severity: str, message: str, suggested_fix: str) -> dict[str, str]:
    return {
        "warning_type": warning_type,
        "severity": severity,
        "message": message,
        "suggested_fix": suggested_fix,
    }


def _dedupe_warnings(warnings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    rows: list[dict[str, Any]] = []
    for warning in warnings:
        key = str(warning.get("warning_type")) + ":" + str(warning.get("message"))
        if key in seen:
            continue
        seen.add(key)
        rows.append(warning)
    return rows


def _plan_uses_context_as_evidence(plan: dict[str, Any]) -> bool:
    text = canonical_json(plan).casefold()
    return "source_context" in text and "support_evidence" in text


def _plan_has_support_claim_without_evidence_id(plan: dict[str, Any]) -> bool:
    for request in plan.get("requests") or []:
        if not isinstance(request, dict):
            continue
        for claim in request.get("support_claims") or []:
            if isinstance(claim, dict) and not claim.get("evidence_id"):
                return True
    return False


def _load_json(path: str | Path | None) -> dict[str, Any]:
    if not path:
        return {}
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    return payload if isinstance(payload, dict) else {}


def _raise_if_unsafe_input(payload: dict[str, Any], label: str) -> None:
    scan = scan_sanitized_text(f"{label}.json", canonical_json(payload))
    if scan["findings"]:
        raise ValueError(f"unsafe {label} contains raw secret or PII pattern")
