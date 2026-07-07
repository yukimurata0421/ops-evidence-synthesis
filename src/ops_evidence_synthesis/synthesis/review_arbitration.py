from __future__ import annotations

from copy import deepcopy
from typing import Any, Iterable

from ops_evidence_synthesis.canonical import canonical_json, sha256_json
from ops_evidence_synthesis.synthesis.output_ingest import (
    merge_candidate_observations,
    observation_groups_from_graph,
)
from ops_evidence_synthesis.synthesis.priority_scoring import score_review_priority
from ops_evidence_synthesis.synthesis.target_classification import (
    NORMAL_OPERATION_REASON,
    target_reads_as_normal_observation,
)
from ops_evidence_synthesis.timeutils import utc_now
SCORE_NOTE = "Score is review priority, not truth probability."


CANONICAL_REVIEW_GRAPH_SCHEMA_VERSION = "canonical_review_graph.v1"
REVIEW_ARBITRATION_VERSION = "review_arbitration.v6"

RUNTIME_EVIDENCE_PREFIXES = ("EVIDENCE-", "LOG-", "METRIC-", "PATTERN-", "OPS-")
SEVERITY_ONLY_SIGNALS = {"info", "warning", "debug"}
USER_IMPACT_TOKENS = (
    "user impact",
    "user-impact",
    "viewer",
    "customer",
    "delivery",
    "ingest",
    "watch",
    "audio",
    "stream_not_live",
    "watch_url_unavailable",
    "notification_not_delivered",
    "request_error_rate",
    "http_5xx",
    "user_visible",
)
BLOCKING_CAVEAT_TOKENS = (
    "critical=false",
    "critical false",
    "critical_false",
    "user impact unverified",
    "user_impact_unverified",
    "impact unverified",
    "context only",
    "source context only",
    "human answer only",
)

REASON_TO_REQUEST_TYPE = {
    "single_metric_only": "instrumentation_consistency_query",
    "single_metric_without_user_impact": "instrumentation_consistency_query",
    "user_impact_unverified": "user_impact_signal_query",
    "no_user_impact_evidence": "user_impact_signal_query",
    "blocking_caveat_present": "instrumentation_consistency_query",
    "critical_false": "process_state_query",
    "severity_only_signal": "user_impact_signal_query",
    "support_without_evidence_id": "instrumentation_consistency_query",
    "support_is_context_not_runtime_evidence": "instrumentation_consistency_query",
    "no_baseline_agreement_or_causal_alignment": "instrumentation_consistency_query",
    "cause_disagreement": "external_dependency_status_query",
    "impact_disagreement": "user_impact_signal_query",
    "core_missing_evidence": "instrumentation_consistency_query",
}

STATE_MACHINE = {
    "initial_states": ["candidate"],
    "classification_states": ["unsupported", "monitor_only", "validation_target", "primary_candidate"],
    "human_review_states": [
        "accepted",
        "rejected",
        "needs_more_data",
        "evidence_collected",
        "strengthened",
        "weakened",
        "resolved",
        "still_unknown",
    ],
    "child_bundle_loop": [
        "needs_more_data",
        "child_bundle_uploaded",
        "evidence_collected",
        "re_analysis",
        "strengthened|weakened|still_unknown|resolved",
    ],
}


def arbitrate_review_targets(
    evidence_bundle: dict[str, Any],
    *,
    model_runs: list[dict[str, Any]] | None = None,
    multi_ai_synthesis: dict[str, Any] | None = None,
    approved_profile: dict[str, Any] | None = None,
    source_context: dict[str, Any] | None = None,
    source_analysis: dict[str, Any] | None = None,
    planner_answers: dict[str, Any] | None = None,
    legacy_review_targets: list[dict[str, Any]] | None = None,
    legacy_summary: dict[str, Any] | None = None,
) -> dict[str, Any]:
    bundle = evidence_bundle if isinstance(evidence_bundle, dict) else {}
    synthesis = multi_ai_synthesis if isinstance(multi_ai_synthesis, dict) else {}
    runs = [run for run in model_runs or [] if isinstance(run, dict)]
    profile = approved_profile if isinstance(approved_profile, dict) else {}
    source_context = source_context if isinstance(source_context, dict) else {}
    source_analysis = source_analysis if isinstance(source_analysis, dict) else {}
    planner_answers = planner_answers if isinstance(planner_answers, dict) else {}
    legacy_targets = [row for row in legacy_review_targets or [] if isinstance(row, dict)]
    legacy_summary = legacy_summary if isinstance(legacy_summary, dict) else {}

    agreement_dimensions = build_agreement_dimensions(synthesis, runs)
    raw_candidates = _candidate_inputs(
        bundle,
        synthesis=synthesis,
        legacy_targets=legacy_targets,
        legacy_summary=legacy_summary,
        source_context=source_context,
        source_analysis=source_analysis,
        planner_answers=planner_answers,
    )
    candidates, observation_groups = merge_candidate_observations(
        raw_candidates,
        evidence_sha256=str(bundle.get("evidence_sha256") or synthesis.get("evidence_sha256") or ""),
    )
    theme_by_group, request_by_theme = _theme_indexes(synthesis)

    primary_targets: list[dict[str, Any]] = []
    validation_targets: list[dict[str, Any]] = []
    monitor_only: list[dict[str, Any]] = []
    auto_archived: list[dict[str, Any]] = []
    promotion_decisions: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []

    for candidate in candidates:
        target, decision = _arbitrate_candidate(
            candidate,
            bundle=bundle,
            agreement_dimensions=agreement_dimensions,
            theme_by_group=theme_by_group,
            request_by_theme=request_by_theme,
        )
        final_class = str(decision.get("final_class") or "validation_target")
        if final_class == "primary_candidate":
            primary_targets.append(target)
        elif final_class == "monitor_only":
            monitor_only.append(target)
        elif final_class == "auto_archived":
            auto_archived.append(target)
        else:
            validation_targets.append(target)
        promotion_decisions.append(decision)

    for archived in synthesis.get("auto_archived") or []:
        if not isinstance(archived, dict):
            continue
        target = _archived_target_from_synthesis(archived, bundle)
        auto_archived.append(target)
        promotion_decisions.append(
            {
                "target_id": target["target_id"],
                "source_target_title": target["title"],
                "final_class": "auto_archived",
                "original_class": "unsupported",
                "decision": "archived",
                "reasons": [str(archived.get("reason") or "unsupported")],
                "score_before": 0.0,
                "score_after": 0.0,
                "score_caps_applied": [],
            }
        )

    primary_targets.sort(key=lambda row: (-float(row.get("review_priority_score") or 0.0), str(row.get("target_id") or "")))
    validation_targets.sort(key=lambda row: (-float(row.get("review_priority_score") or 0.0), str(row.get("target_id") or "")))
    monitor_only.sort(key=lambda row: str(row.get("target_id") or ""))
    auto_archived.sort(key=lambda row: str(row.get("target_id") or ""))
    _apply_review_unit_convergence(agreement_dimensions, [*primary_targets, *validation_targets])

    disagreement_themes = [row for row in synthesis.get("disagreement_themes") or [] if isinstance(row, dict)]
    planner_inputs = _planner_inputs(
        validation_targets=validation_targets,
        promotion_decisions=promotion_decisions,
        disagreement_themes=disagreement_themes,
        synthesis=synthesis,
    )
    finding = _canonical_finding(primary_targets, validation_targets, monitor_only, agreement_dimensions)
    if _has_legacy_primary_downgrade(promotion_decisions):
        warnings.append(
            {
                "warning_type": "legacy_primary_reclassified",
                "severity": "info",
                "message": "Legacy primary targets were reclassified by Review Target Arbitration.",
            }
        )

    fingerprint = compute_input_fingerprint(
        bundle,
        model_runs=runs,
        multi_ai_synthesis=synthesis,
        approved_profile=profile,
        source_context=source_context,
        source_analysis=source_analysis,
        planner_answers=planner_answers,
    )
    graph = {
        "schema_version": CANONICAL_REVIEW_GRAPH_SCHEMA_VERSION,
        "evidence_sha256": str(bundle.get("evidence_sha256") or synthesis.get("evidence_sha256") or ""),
        "generated_by": REVIEW_ARBITRATION_VERSION,
        "arbitration_version": REVIEW_ARBITRATION_VERSION,
        "input_fingerprint": fingerprint["input_fingerprint_json"],
        "input_fingerprint_sha256": fingerprint["input_fingerprint_sha256"],
        "snapshot_status": "computed_on_request",
        "score_note": SCORE_NOTE,
        "summary": {
            "primary_count": len(primary_targets),
            "validation_count": len(validation_targets),
            "monitor_only_count": len(monitor_only),
            "auto_archived_count": len(auto_archived),
        },
        "finding": finding,
        "agreement_dimensions": agreement_dimensions,
        "canonical_observation_groups": observation_groups,
        "disagreement_themes": disagreement_themes,
        "primary_targets": primary_targets,
        "validation_targets": validation_targets,
        "monitor_only": monitor_only,
        "auto_archived": auto_archived,
        "promotion_decisions": promotion_decisions,
        "arbitration_warnings": warnings,
        "planner_inputs": planner_inputs,
        "support_role_policy": support_role_policy(),
        "score_policy": score_policy(),
        "state_machine": STATE_MACHINE,
        "display_summary": {
            "title": finding["title"],
            "impact": finding["impact"],
            "provider_detection_overlap": (agreement_dimensions.get("provider_detection_overlap") or {}).get("value") or "0/0",
            "technical_baseline_agreement": "established" if (agreement_dimensions.get("technical_baseline_agreement") or {}).get("established") else "not established",
            "incident_baseline_agreement": "established" if (agreement_dimensions.get("incident_baseline_agreement") or agreement_dimensions.get("baseline_agreement") or {}).get("established") else "not established",
            "baseline_agreement": "established" if (agreement_dimensions.get("baseline_agreement") or {}).get("established") else "not established",
            "cause_agreement": (agreement_dimensions.get("cause_agreement") or {}).get("value") or "none",
            "impact_agreement": (agreement_dimensions.get("impact_agreement") or {}).get("value") or "none",
            "score_note": SCORE_NOTE,
        },
    }
    graph["review_targets"] = [*primary_targets, *validation_targets]
    graph["canonical_graph_sha256"] = compute_canonical_graph_sha256(graph)
    return graph


def compute_input_fingerprint(
    evidence_bundle: dict[str, Any],
    *,
    model_runs: list[dict[str, Any]] | None = None,
    multi_ai_synthesis: dict[str, Any] | None = None,
    approved_profile: dict[str, Any] | None = None,
    source_context: dict[str, Any] | None = None,
    source_analysis: dict[str, Any] | None = None,
    planner_answers: dict[str, Any] | None = None,
    arbitration_version: str = REVIEW_ARBITRATION_VERSION,
) -> dict[str, Any]:
    runs = [_plain_mapping(run) for run in model_runs or [] if _plain_mapping(run)]
    model_run_ids = sorted(_non_empty(_mapping_get(run, "run_id") for run in runs))
    model_output_sha256s = sorted(
        _non_empty(
            _mapping_get(run, "raw_output_sha256") or _mapping_get(run, "parsed_json_sha256")
            for run in runs
        )
    )
    model_run_set = [
        {
            "model_name": str(_mapping_get(run, "model_name") or ""),
            "provider_id": str(_mapping_get(run, "provider_id") or _mapping_get(run, "provider") or ""),
            "raw_output_sha256": str(_mapping_get(run, "raw_output_sha256") or ""),
            "run_id": str(_mapping_get(run, "run_id") or ""),
            "schema_valid": bool(_mapping_get(run, "schema_valid")),
            "status": str(_mapping_get(run, "status") or ""),
        }
        for run in sorted(runs, key=lambda row: (str(_mapping_get(row, "run_id") or ""), str(_mapping_get(row, "provider_id") or _mapping_get(row, "provider") or "")))
    ]
    fingerprint = {
        "evidence_sha256": str((evidence_bundle or {}).get("evidence_sha256") or "null"),
        "model_run_set_sha256": sha256_json(model_run_set) if model_run_set else "null",
        "model_run_ids": model_run_ids,
        "model_output_sha256s": model_output_sha256s,
        "profile_sha256": _payload_sha256(approved_profile),
        "source_context_sha256": str((source_context or {}).get("source_context_sha256") or "null"),
        "source_analysis_sha256": str((source_analysis or {}).get("analysis_sha256") or "null"),
        "planner_answers_sha256": _payload_sha256(planner_answers),
        "multi_ai_synthesis_sha256": _payload_sha256(_stable_synthesis_for_fingerprint(multi_ai_synthesis or {})),
        "arbitration_version": arbitration_version,
        "canonical_schema_version": CANONICAL_REVIEW_GRAPH_SCHEMA_VERSION,
    }
    return {
        "input_fingerprint_json": fingerprint,
        "input_fingerprint_sha256": sha256_json(fingerprint),
    }


def compute_canonical_graph_sha256(graph: dict[str, Any]) -> str:
    return sha256_json(_deterministic_graph(graph))


def build_canonical_review_graph_snapshot(
    graph: dict[str, Any],
    *,
    created_by: str = "api",
    snapshot_status: str = "persisted",
) -> dict[str, Any]:
    finding = graph.get("finding") if isinstance(graph.get("finding"), dict) else {}
    summary = graph.get("summary") if isinstance(graph.get("summary"), dict) else {}
    graph_sha = str(graph.get("canonical_graph_sha256") or compute_canonical_graph_sha256(graph))
    input_fingerprint = graph.get("input_fingerprint") if isinstance(graph.get("input_fingerprint"), dict) else {}
    input_fingerprint_sha = str(graph.get("input_fingerprint_sha256") or sha256_json(input_fingerprint or {}))
    return {
        "evidence_sha256": str(graph.get("evidence_sha256") or ""),
        "canonical_graph_sha256": graph_sha,
        "schema_version": str(graph.get("schema_version") or CANONICAL_REVIEW_GRAPH_SCHEMA_VERSION),
        "arbitration_version": str(graph.get("arbitration_version") or graph.get("generated_by") or REVIEW_ARBITRATION_VERSION),
        "input_fingerprint_sha256": input_fingerprint_sha,
        "input_fingerprint_json": input_fingerprint,
        "finding_title": str(finding.get("title") or finding.get("finding") or ""),
        "finding_impact": str(finding.get("impact") or ""),
        "primary_count": int(summary.get("primary_count") or 0),
        "validation_count": int(summary.get("validation_count") or 0),
        "monitor_only_count": int(summary.get("monitor_only_count") or 0),
        "auto_archived_count": int(summary.get("auto_archived_count") or 0),
        "promotion_decision_count": len(graph.get("promotion_decisions") or []),
        "created_at": utc_now(),
        "created_by": str(created_by or "api"),
        "snapshot_status": snapshot_status,
        "canonical_review_graph_json": _graph_with_snapshot_metadata(graph, snapshot_status=snapshot_status),
    }


def resolve_canonical_review_graph_snapshot(
    store: Any | None,
    evidence_bundle: dict[str, Any],
    *,
    model_runs: list[dict[str, Any]] | None = None,
    multi_ai_synthesis: dict[str, Any] | None = None,
    approved_profile: dict[str, Any] | None = None,
    source_context: dict[str, Any] | None = None,
    source_analysis: dict[str, Any] | None = None,
    planner_answers: dict[str, Any] | None = None,
    legacy_review_targets: list[dict[str, Any]] | None = None,
    legacy_summary: dict[str, Any] | None = None,
    persist_if_missing: bool = False,
    persist_if_stale: bool = False,
    created_by: str = "api",
) -> dict[str, Any]:
    current_graph = arbitrate_review_targets(
        evidence_bundle,
        model_runs=model_runs,
        multi_ai_synthesis=multi_ai_synthesis,
        approved_profile=approved_profile,
        source_context=source_context,
        source_analysis=source_analysis,
        planner_answers=planner_answers,
        legacy_review_targets=legacy_review_targets,
        legacy_summary=legacy_summary,
    )
    evidence_sha = str(current_graph.get("evidence_sha256") or (evidence_bundle or {}).get("evidence_sha256") or "")
    latest = _latest_snapshot(store, evidence_sha)
    current_fp = str(current_graph.get("input_fingerprint_sha256") or "")
    response: dict[str, Any]
    if latest and str(latest.get("input_fingerprint_sha256") or "") == current_fp:
        persisted_graph = latest.get("canonical_review_graph_json") if isinstance(latest.get("canonical_review_graph_json"), dict) else current_graph
        persisted_graph = _graph_with_snapshot_metadata(persisted_graph, snapshot_status="persisted")
        response = _graph_response("persisted", persisted_graph, snapshot=latest)
        if persist_if_missing or persist_if_stale:
            projection_result = _refresh_snapshot_projections(store, persisted_graph)
            response["projection_persistence"] = projection_result
            if projection_result.get("warning"):
                response["persistence_warning"] = projection_result["warning"]
        return response
    if latest:
        current_graph = _graph_with_snapshot_metadata(
            current_graph,
            snapshot_status="stale",
            extra={
                "stale_reason": "input_fingerprint_changed",
                "persisted_input_fingerprint_sha256": str(latest.get("input_fingerprint_sha256") or ""),
                "current_input_fingerprint_sha256": current_fp,
                "persisted_created_at": str(latest.get("created_at") or ""),
            },
        )
        if persist_if_stale:
            save_result = _save_snapshot(store, current_graph, created_by=created_by)
            if save_result.get("saved"):
                saved = save_result.get("snapshot") if isinstance(save_result.get("snapshot"), dict) else {}
                graph = _graph_with_snapshot_metadata(current_graph, snapshot_status="persisted")
                response = _graph_response("persisted", graph, snapshot=saved)
                response["previous_snapshot"] = _snapshot_metadata(latest)
                if save_result.get("warning"):
                    response["persistence_warning"] = save_result.get("warning")
                return response
            response = _graph_response("stale", current_graph, snapshot=latest)
            response["persistence_warning"] = save_result.get("warning") or "canonical graph generated but snapshot persistence failed"
            return response
        response = _graph_response("stale", current_graph, snapshot=latest)
        response["previous_snapshot"] = _snapshot_metadata(latest)
        return response

    if persist_if_missing:
        save_result = _save_snapshot(store, current_graph, created_by=created_by)
        if save_result.get("saved"):
            saved = save_result.get("snapshot") if isinstance(save_result.get("snapshot"), dict) else {}
            graph = _graph_with_snapshot_metadata(current_graph, snapshot_status="persisted")
            response = _graph_response("persisted", graph, snapshot=saved)
            if save_result.get("warning"):
                response["persistence_warning"] = save_result.get("warning")
            return response
        response = _graph_response("computed_on_request", current_graph, snapshot={})
        response["persistence_warning"] = save_result.get("warning") or "canonical graph generated but snapshot persistence failed"
        return response
    return _graph_response("computed_on_request", current_graph, snapshot={})


def _graph_response(status: str, graph: dict[str, Any], *, snapshot: dict[str, Any]) -> dict[str, Any]:
    if not snapshot:
        snapshot = build_canonical_review_graph_snapshot(graph, created_by="request", snapshot_status=status)
    return {
        "canonical_graph_status": status,
        "canonical_graph_sha256": str(graph.get("canonical_graph_sha256") or compute_canonical_graph_sha256(graph)),
        "input_fingerprint_sha256": str(graph.get("input_fingerprint_sha256") or ""),
        "canonical_review_graph": graph,
        "snapshot": snapshot,
        "snapshot_created_at": str(snapshot.get("created_at") or ""),
    }


def _save_snapshot(store: Any | None, graph: dict[str, Any], *, created_by: str) -> dict[str, Any]:
    if store is None or not hasattr(store, "save_canonical_review_graph_snapshot"):
        return {"saved": False, "warning": "canonical graph generated but snapshot persistence is not available"}
    snapshot = build_canonical_review_graph_snapshot(graph, created_by=created_by)
    try:
        saved = store.save_canonical_review_graph_snapshot(snapshot)
        group_warning = _save_observation_groups(store, graph)
        target_warning = _save_review_target_projection(store, graph)
    except Exception as exc:
        return {"saved": False, "warning": f"canonical graph generated but snapshot persistence failed: {exc}"}
    response = {"saved": True, "snapshot": saved if isinstance(saved, dict) else snapshot}
    warnings = [warning for warning in (group_warning, target_warning) if warning]
    if warnings:
        response["warning"] = "; ".join(warnings)
    return response


def _refresh_snapshot_projections(store: Any | None, graph: dict[str, Any]) -> dict[str, Any]:
    group_warning = _save_observation_groups(store, graph)
    target_warning = _save_review_target_projection(store, graph)
    warnings = [warning for warning in (group_warning, target_warning) if warning]
    response: dict[str, Any] = {"refreshed": True}
    if warnings:
        response["warning"] = "; ".join(warnings)
    return response


def _save_review_target_projection(store: Any | None, graph: dict[str, Any]) -> str:
    targets = _review_target_projection_rows(graph)
    if not targets:
        return ""
    try:
        evidence_sha = str(graph.get("evidence_sha256") or "")
        if hasattr(store, "replace_review_targets_for_evidence"):
            store.replace_review_targets_for_evidence(evidence_sha, targets)
        elif hasattr(store, "upsert_review_targets"):
            store.upsert_review_targets(targets)
        else:
            return "canonical review targets were not persisted: review target projection is not available"
    except Exception as exc:
        return f"canonical review targets were not persisted: {exc}"
    return ""


def _review_target_projection_rows(graph: dict[str, Any]) -> list[dict[str, Any]]:
    evidence_sha = str(graph.get("evidence_sha256") or "")
    raw_targets = [
        row
        for row in [
            *(graph.get("primary_targets") or []),
            *(graph.get("validation_targets") or []),
        ]
        if isinstance(row, dict)
    ]
    if not raw_targets:
        raw_targets = [row for row in graph.get("review_targets") or [] if isinstance(row, dict)]
    targets: list[dict[str, Any]] = []
    for target in raw_targets:
        output = deepcopy(target)
        review_target_id = str(
            output.get("review_target_id")
            or output.get("target_id")
            or output.get("canonical_observation_group_id")
            or ""
        )
        if not review_target_id:
            continue
        output["review_target_id"] = review_target_id
        output.setdefault("target_id", review_target_id)
        output["evidence_sha256"] = str(output.get("evidence_sha256") or evidence_sha)
        output.setdefault("cluster_id", output.get("canonical_observation_group_id") or review_target_id)
        output.setdefault("subsystem", output.get("component") or output.get("canonical_review_unit") or "general")
        output.setdefault("core_claim", output.get("title") or output.get("impact_summary") or "")
        output.setdefault("proposal", output.get("impact_summary") or output.get("title") or "")
        output.setdefault("status", "pending")
        targets.append(output)
    return targets


def _save_observation_groups(store: Any | None, graph: dict[str, Any]) -> str:
    if store is None or not hasattr(store, "replace_canonical_observation_groups"):
        return ""
    groups = [row for row in graph.get("canonical_observation_groups") or [] if isinstance(row, dict)]
    if not groups:
        groups = observation_groups_from_graph(graph)
    if not groups:
        return ""
    try:
        store.replace_canonical_observation_groups(str(graph.get("evidence_sha256") or ""), groups)
    except Exception as exc:
        return f"canonical observation groups were not persisted: {exc}"
    return ""


def _latest_snapshot(store: Any | None, evidence_sha256: str) -> dict[str, Any] | None:
    if not evidence_sha256 or store is None or not hasattr(store, "get_latest_canonical_review_graph_snapshot"):
        return None
    try:
        snapshot = store.get_latest_canonical_review_graph_snapshot(evidence_sha256)
    except Exception:
        return None
    return snapshot if isinstance(snapshot, dict) else None


def _snapshot_metadata(snapshot: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(snapshot, dict) or not snapshot:
        return {}
    return {
        "evidence_sha256": str(snapshot.get("evidence_sha256") or ""),
        "canonical_graph_sha256": str(snapshot.get("canonical_graph_sha256") or ""),
        "input_fingerprint_sha256": str(snapshot.get("input_fingerprint_sha256") or ""),
        "created_at": str(snapshot.get("created_at") or ""),
        "created_by": str(snapshot.get("created_by") or ""),
        "snapshot_status": str(snapshot.get("snapshot_status") or ""),
    }


def _graph_with_snapshot_metadata(graph: dict[str, Any], *, snapshot_status: str, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    output = deepcopy(graph)
    output["snapshot_status"] = snapshot_status
    output["canonical_graph_status"] = snapshot_status
    output["canonical_graph_sha256"] = str(output.get("canonical_graph_sha256") or compute_canonical_graph_sha256(output))
    if extra:
        output.update(extra)
    return output


def _deterministic_graph(value: Any) -> Any:
    excluded = {
        "created_at",
        "snapshot_status",
        "canonical_graph_status",
        "persisted_at",
        "request_id",
        "canonical_graph_sha256",
        "previous_snapshot",
        "snapshot",
        "snapshot_created_at",
        "persistence_warning",
        "stale_reason",
        "persisted_input_fingerprint_sha256",
        "current_input_fingerprint_sha256",
        "persisted_created_at",
    }
    if isinstance(value, dict):
        return {str(key): _deterministic_graph(item) for key, item in value.items() if str(key) not in excluded}
    if isinstance(value, list):
        return [_deterministic_graph(item) for item in value]
    return value


def _stable_synthesis_for_fingerprint(synthesis: dict[str, Any]) -> dict[str, Any]:
    excluded = {"canonical_review_graph_summary", "canonical_review_graph", "canonical_graph_status"}
    return {str(key): value for key, value in synthesis.items() if str(key) not in excluded}


def _payload_sha256(value: Any) -> str:
    if value is None or value == {} or value == []:
        return "null"
    return sha256_json(value)


def _plain_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if hasattr(value, "__dict__"):
        return dict(value.__dict__)
    return {}


def _mapping_get(value: dict[str, Any], key: str) -> Any:
    return value.get(key) if isinstance(value, dict) else None


def _non_empty(values: Iterable[Any]) -> list[str]:
    return [str(value) for value in values if str(value or "").strip()]


def build_agreement_dimensions(synthesis: dict[str, Any], model_runs: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    runs = [run for run in model_runs or [] if isinstance(run, dict)]
    groups = [row for row in synthesis.get("claim_groups") or [] if isinstance(row, dict)]
    agreement_groups = [row for row in synthesis.get("agreement_groups") or [] if isinstance(row, dict)]
    disagreement_groups = [row for row in synthesis.get("disagreement_groups") or [] if isinstance(row, dict)]
    provider_total = int(synthesis.get("successful_provider_count") or 0) or sum(
        1 for run in runs if run.get("status") == "ok" and run.get("schema_valid") is True
    )
    provider_total = provider_total or int(synthesis.get("provider_count") or 0) or len(runs)
    detected_providers = _unique(
        provider
        for group in groups
        for provider in group.get("providers") or []
        if str(provider).strip()
    )
    if not detected_providers and provider_total and (agreement_groups or disagreement_groups):
        detected_providers = [f"provider-{index}" for index in range(1, provider_total + 1)]
    overlap_count = len(detected_providers)
    if provider_total and overlap_count > provider_total:
        provider_total = overlap_count

    target_types = [str(group.get("core_target_type") or "") for group in agreement_groups if group.get("core_target_type")]
    disagreement_types = [str(group.get("core_target_type") or "") for group in disagreement_groups if group.get("core_target_type")]
    target_type_value = _agreement_value(target_types, disagreement_types)

    max_agreement_providers = max((int(group.get("provider_count") or 0) for group in agreement_groups), default=0)
    evidence_ref_value = "weak"
    if agreement_groups and max_agreement_providers >= max(provider_total, 1) and any(group.get("evidence_refs") for group in agreement_groups):
        evidence_ref_value = "strong"
    elif agreement_groups and any(group.get("evidence_refs") for group in agreement_groups):
        evidence_ref_value = "partial"

    if not agreement_groups:
        cause_value = "none"
    elif disagreement_groups:
        cause_value = "partial"
    else:
        cause_value = "strong"

    impact_text = _joined_text(agreement_groups)
    impact_value = "none"
    if any(token in impact_text for token in USER_IMPACT_TOKENS):
        impact_value = "strong" if max_agreement_providers >= max(provider_total, 1) and not disagreement_groups else "partial"

    next_action_value = _next_action_agreement_value(synthesis, disagreement_groups, agreement_groups)
    technical_baseline_established = bool(
        agreement_groups
        and max_agreement_providers >= max(provider_total, 1)
        and evidence_ref_value in {"partial", "strong"}
        and cause_value in {"partial", "strong"}
    )
    technical_baseline_reason = (
        "Providers aligned on the same technical target with cited evidence."
        if technical_baseline_established
        else "Providers did not align on the same technical target with cited evidence."
    )

    incident_baseline_established = bool(
        agreement_groups
        and cause_value in {"partial", "strong"}
        and evidence_ref_value in {"partial", "strong"}
        and impact_value in {"partial", "strong"}
        and next_action_value in {"partial", "strong"}
    )
    incident_baseline_reason = (
        "Providers aligned on evidence, cause, impact, and next action."
        if incident_baseline_established
        else "Providers did not align on cause, impact, and next action."
    )
    return {
        "provider_detection_overlap": {
            "value": f"{overlap_count}/{provider_total}" if provider_total else "0/0",
            "description": "Providers mentioned related signals; this is not incident baseline agreement.",
            "detected_provider_count": overlap_count,
            "total_provider_count": provider_total,
        },
        "target_type_agreement": {"value": target_type_value},
        "evidence_ref_agreement": {"value": evidence_ref_value},
        "cause_agreement": {"value": cause_value},
        "impact_agreement": {"value": impact_value},
        "next_action_agreement": {"value": next_action_value},
        "technical_baseline_agreement": {
            "established": technical_baseline_established,
            "reason": technical_baseline_reason,
        },
        "incident_baseline_agreement": {
            "established": incident_baseline_established,
            "reason": incident_baseline_reason,
        },
        "baseline_agreement": {
            "established": incident_baseline_established,
            "reason": incident_baseline_reason,
            "alias_for": "incident_baseline_agreement",
        },
    }


def _apply_review_unit_convergence(agreement_dimensions: dict[str, Any], targets: list[dict[str, Any]]) -> None:
    rows = [
        _review_unit_convergence_row(target)
        for target in targets
        if isinstance(target, dict) and str(target.get("canonical_review_unit") or "").strip()
    ]
    rows = [row for row in rows if row["source_candidate_count"] > 1]
    rows.sort(
        key=lambda row: (
            -float(row.get("baseline_support_score") or 0.0),
            -float(row.get("review_priority_score") or 0.0),
            str(row.get("canonical_review_unit") or ""),
        )
    )
    converged = [
        row
        for row in rows
        if int(row.get("source_candidate_count") or 0) >= 2
        and int(row.get("independent_provider_count") or 0) >= 2
        and int(row.get("evidence_ref_count") or 0) >= 2
        and float(row.get("baseline_support_score") or 0.0) >= 0.65
    ]
    if converged:
        value = "strong" if float(converged[0].get("baseline_support_score") or 0.0) >= 0.8 else "partial"
        reason = "Multiple independent candidates converged on the same review unit with cited runtime evidence."
    elif rows:
        value = "partial"
        reason = "Duplicate review units exist, but independent provider or evidence diversity is not strong enough for technical support."
    else:
        value = "none"
        reason = "No duplicate review-unit convergence was found."
    agreement_dimensions["review_unit_convergence"] = {
        "value": value,
        "reason": reason,
        "converged_unit_count": len(converged),
        "candidate_unit_count": len(rows),
        "top_units": rows[:5],
    }
    technical = agreement_dimensions.get("technical_baseline_agreement")
    if converged and isinstance(technical, dict) and not technical.get("established"):
        technical["established"] = True
        technical["reason"] = reason
        technical["source"] = "review_unit_convergence"


def _review_unit_convergence_row(target: dict[str, Any]) -> dict[str, Any]:
    rollup = _rollup_metrics(target)
    return {
        "target_id": str(target.get("target_id") or ""),
        "canonical_review_unit": str(target.get("canonical_review_unit") or ""),
        "title": str(target.get("title") or ""),
        "class": str(target.get("class") or ""),
        "source_candidate_count": int(rollup.get("source_candidate_count") or target.get("source_candidate_count") or 1),
        "independent_provider_count": int(rollup.get("independent_provider_count") or target.get("provider_count") or 0),
        "evidence_ref_count": int(rollup.get("evidence_ref_count") or len(target.get("evidence_refs") or [])),
        "baseline_support_score": float(rollup.get("baseline_support_score") or 0.0),
        "rollup_provider_ratio": float(rollup.get("rollup_provider_ratio") or 0.0),
        "review_priority_score": float(target.get("review_priority_score") or 0.0),
    }


def support_role_policy() -> dict[str, Any]:
    return {
        "runtime_evidence": {"incident_support_allowed": True},
        "source_context": {"incident_support_allowed": False, "allowed_for": ["profile mapping", "metric semantics", "instrumentation interpretation"]},
        "profile_context": {"incident_support_allowed": False},
        "human_context": {"incident_support_allowed": False},
        "model_interpretation": {"incident_support_allowed": False},
        "support_claims_must_cite_evidence_id": True,
    }


def score_policy() -> dict[str, Any]:
    return {
        "support_without_evidence_id": {"max_score": 0.55},
        "context_only": {"max_score": 0.50},
        "single_metric_only": {"max_score": 0.70},
        "severity_only_signal": {"max_score": 0.45},
        "user_impact_unverified": {"max_score": 0.72},
        "blocking_caveat_present": {"max_score": 0.65},
        "baseline_agreement_missing_with_no_cause_alignment": {"primary_promotion": "blocked"},
    }


def request_types_from_canonical_graph(graph: dict[str, Any]) -> list[str]:
    planner_inputs = graph.get("planner_inputs") if isinstance(graph.get("planner_inputs"), dict) else {}
    return _unique(str(item) for item in planner_inputs.get("recommended_request_types") or [] if str(item).strip())


def _candidate_inputs(
    bundle: dict[str, Any],
    *,
    synthesis: dict[str, Any],
    legacy_targets: list[dict[str, Any]],
    legacy_summary: dict[str, Any],
    source_context: dict[str, Any],
    source_analysis: dict[str, Any],
    planner_answers: dict[str, Any],
) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for row in synthesis.get("primary_candidates") or []:
        if isinstance(row, dict):
            candidates.append(_candidate_from_multi_ai(row, original_class="primary_candidate"))
    for row in synthesis.get("validation_targets") or []:
        if isinstance(row, dict):
            candidates.append(_candidate_from_multi_ai(row, original_class="validation_target"))

    legacy_primary_count = int(legacy_summary.get("primary_review_targets") or 0)
    for index, row in enumerate(legacy_targets, start=1):
        original_class = "primary_candidate" if index <= legacy_primary_count else "validation_target"
        review_mode = str(row.get("review_mode") or "")
        if review_mode == "incident_candidate":
            original_class = "primary_candidate"
        elif review_mode == "validation_target" or row.get("parent_review_target_id"):
            original_class = "validation_target"
        candidates.append(_candidate_from_legacy(row, original_class=original_class))

    if source_context:
        candidates.append(_context_candidate("source_context", source_context, bundle))
    if source_analysis:
        candidates.append(_context_candidate("source_analysis", source_analysis, bundle))
    if planner_answers:
        candidates.append(_context_candidate("human_answers", planner_answers, bundle))

    return _dedupe_candidates(candidates)


def _candidate_from_multi_ai(row: dict[str, Any], *, original_class: str) -> dict[str, Any]:
    target_id = str(row.get("review_target_id") or row.get("target_id") or row.get("group_id") or "")
    if not target_id:
        target_id = "mai-" + sha256_json(row)[:16]
    return {
        "target_id": target_id,
        "source": "multi_ai_synthesis",
        "original_class": original_class,
        "title": str(row.get("title") or row.get("core_target_type") or "Review target requires validation"),
        "impact_summary": str(row.get("impact_summary") or ""),
        "core_target_type": str(row.get("core_target_type") or row.get("review_target_type") or "general_review"),
        "subsystem": str(row.get("subsystem") or "general"),
        "component": str(row.get("component") or ""),
        "providers": _unique(row.get("providers") or []),
        "provider_count": int(row.get("provider_count") or len(row.get("providers") or [])),
        "evidence_refs": _unique(row.get("evidence_refs") or []),
        "missing_evidence": _unique(row.get("missing_evidence") or []),
        "caveats": _unique(row.get("caveats") or []),
        "target_explanation": dict(row.get("target_explanation") or {}),
        "suspected_issue": str(row.get("suspected_issue") or ""),
        "operational_mechanism": str(row.get("operational_mechanism") or ""),
        "why_it_matters": str(row.get("why_it_matters") or ""),
        "evidence_summary": _unique(row.get("evidence_summary") or []),
        "counter_evidence_summary": _unique(row.get("counter_evidence_summary") or []),
        "why_not_promoted": str(row.get("why_not_promoted") or ""),
        "next_validation_question": str(row.get("next_validation_question") or ""),
        "score_before": float(row.get("review_priority_score") or row.get("score") or (0.75 if original_class == "primary_candidate" else 0.62)),
        "group_id": str(row.get("group_id") or target_id),
        "raw": row,
    }


def _candidate_from_legacy(row: dict[str, Any], *, original_class: str) -> dict[str, Any]:
    target_id = str(row.get("review_target_id") or row.get("target_id") or row.get("cluster_id") or "")
    if not target_id:
        target_id = "legacy-" + sha256_json(row)[:16]
    drawer = row.get("drawer") if isinstance(row.get("drawer"), dict) else {}
    support = [item for item in drawer.get("support_evidence") or [] if isinstance(item, dict)]
    counter = [item for item in drawer.get("counter_evidence") or [] if isinstance(item, dict)]
    refs = _evidence_refs_from_target(row)
    missing = _unique([*(drawer.get("missing_evidence") or []), *(row.get("next_data_needed") or [])])
    caveats = _unique([*(drawer.get("caveats") or []), row.get("counter_or_caveat_summary") or ""])
    agreement = row.get("model_agreement") if isinstance(row.get("model_agreement"), dict) else {}
    return {
        "target_id": target_id,
        "source": "legacy_review_target",
        "original_class": original_class,
        "title": str(row.get("title") or "Review target requires validation"),
        "impact_summary": str(row.get("impact_summary") or row.get("core_claim") or row.get("support_summary") or ""),
        "core_target_type": str(row.get("core_target_type") or row.get("review_target_type") or "general_review"),
        "subsystem": str(row.get("subsystem") or "general"),
        "component": str(row.get("component") or ""),
        "providers": _unique((provider.get("provider") for provider in agreement.get("providers") or [] if isinstance(provider, dict))),
        "provider_count": int(agreement.get("detected_provider_count") or 0),
        "evidence_refs": refs,
        "support_evidence": support,
        "counter_evidence": counter,
        "missing_evidence": missing,
        "caveats": caveats,
        "target_explanation": dict(row.get("target_explanation") or {}),
        "suspected_issue": str(row.get("suspected_issue") or ""),
        "operational_mechanism": str(row.get("operational_mechanism") or ""),
        "why_it_matters": str(row.get("why_it_matters") or ""),
        "evidence_summary": _unique(row.get("evidence_summary") or []),
        "counter_evidence_summary": _unique(row.get("counter_evidence_summary") or []),
        "why_not_promoted": str(row.get("why_not_promoted") or ""),
        "next_validation_question": str(row.get("next_validation_question") or ""),
        "score_before": float(row.get("review_priority_score") or 0.0),
        "group_id": str(row.get("cluster_id") or target_id),
        "raw": row,
    }


def _context_candidate(kind: str, payload: dict[str, Any], bundle: dict[str, Any]) -> dict[str, Any]:
    role = {
        "source_context": "source_context",
        "source_analysis": "source_context",
        "human_answers": "human_context",
    }.get(kind, "model_interpretation")
    title = {
        "source_context": "Source context is available",
        "source_analysis": "Source analysis is available",
        "human_answers": "Human planner answers are available",
    }.get(kind, "Context is available")
    return {
        "target_id": f"ctx-{kind}-" + sha256_json({"kind": kind, "evidence_sha256": bundle.get("evidence_sha256")})[:10],
        "source": kind,
        "original_class": "context",
        "title": title,
        "impact_summary": "Context may guide mapping or planning but is not incident evidence.",
        "core_target_type": "context_only",
        "subsystem": "context",
        "component": "",
        "providers": [],
        "provider_count": 0,
        "evidence_refs": [],
        "missing_evidence": [],
        "caveats": ["context only"],
        "score_before": 0.0,
        "group_id": kind,
        "support_role_override": role,
        "raw": {"context_sha256": payload.get("source_context_sha256") or payload.get("analysis_sha256") or ""},
    }


def _dedupe_candidates(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    output: list[dict[str, Any]] = []
    for candidate in candidates:
        key = str(candidate.get("target_id") or "")
        if key in seen:
            continue
        seen.add(key)
        output.append(candidate)
    return output


def _arbitrate_candidate(
    candidate: dict[str, Any],
    *,
    bundle: dict[str, Any],
    agreement_dimensions: dict[str, Any],
    theme_by_group: dict[str, str],
    request_by_theme: dict[str, str],
) -> tuple[dict[str, Any], dict[str, Any]]:
    text = _candidate_text(candidate)
    refs = _unique(candidate.get("evidence_refs") or [])
    support_role = str(candidate.get("support_role_override") or _support_role(refs, candidate))
    runtime = support_role == "runtime_evidence"
    evidence_diversity = len(refs)
    has_user_impact = _has_user_impact(candidate, text)
    severity_only = _is_severity_only(candidate, text)
    blocking_caveats = _blocking_caveats(candidate, text)
    missing_core = _core_missing_evidence(candidate)
    score_before = float(candidate.get("score_before") or 0.0)
    if score_before <= 0.0 and str(candidate.get("original_class")) == "primary_candidate":
        score_before = 0.75
    elif score_before <= 0.0:
        score_before = 0.55
    rollup = _rollup_metrics(candidate)
    convergence_bonus = _convergence_priority_bonus(rollup)
    score_with_convergence = min(1.0, score_before + convergence_bonus)

    reasons: list[str] = []
    score_caps: list[dict[str, Any]] = []
    score_after = score_with_convergence
    raw_target_explanation = candidate.get("target_explanation") if isinstance(candidate.get("target_explanation"), dict) else {}
    normal_observation = target_reads_as_normal_observation(
        candidate,
        target_explanation=raw_target_explanation,
    )

    baseline = agreement_dimensions.get("baseline_agreement") if isinstance(agreement_dimensions.get("baseline_agreement"), dict) else {}
    cause = agreement_dimensions.get("cause_agreement") if isinstance(agreement_dimensions.get("cause_agreement"), dict) else {}
    impact = agreement_dimensions.get("impact_agreement") if isinstance(agreement_dimensions.get("impact_agreement"), dict) else {}
    if normal_observation:
        reasons.append(NORMAL_OPERATION_REASON)
        score_after = _cap(score_after, 0.35, NORMAL_OPERATION_REASON, score_caps)
    if baseline.get("established") is False and cause.get("value") == "none":
        reasons.append("no_baseline_agreement_or_causal_alignment")
    if not runtime:
        reasons.append("support_is_context_not_runtime_evidence")
        score_after = _cap(score_after, 0.50, "context_only", score_caps)
    if not refs and str(candidate.get("source")) != "legacy_review_target":
        reasons.append("support_without_evidence_id")
        score_after = _cap(score_after, 0.55, "support_without_evidence_id", score_caps)
    if evidence_diversity <= 1 and not has_user_impact:
        reasons.append("single_metric_only")
        score_after = _cap(score_after, 0.70, "single_metric_without_user_impact", score_caps)
    if severity_only:
        reasons.append("severity_only_signal")
        score_after = _cap(score_after, 0.45, "severity_only_signal", score_caps)
    if not has_user_impact:
        reasons.append("user_impact_unverified")
        score_after = _cap(score_after, 0.72, "user_impact_unverified", score_caps)
    if blocking_caveats:
        reasons.append("blocking_caveat_present")
        reasons.extend(blocking_caveats)
        score_after = _cap(score_after, 0.65, "blocking_caveat_present", score_caps)
    if str(cause.get("value") or "") == "none" and (baseline.get("established") is False):
        reasons.append("cause_disagreement")
    if str(impact.get("value") or "") == "none" and not has_user_impact:
        reasons.append("impact_disagreement")
    if missing_core:
        reasons.append("core_missing_evidence")

    reasons = _unique(reasons)
    original = str(candidate.get("original_class") or "candidate")
    promotion_score = score_after
    total_provider_count = int(
        (agreement_dimensions.get("provider_detection_overlap") or {}).get("total_provider_count") or 0
    )
    priority_result = score_review_priority(
        prior_score=score_with_convergence,
        promotion_score=promotion_score,
        claimed_provider_ids=_unique(candidate.get("providers") or []),
        total_provider_count=total_provider_count,
        evidence_ref_count=evidence_diversity,
        evidence_family_count=int(rollup.get("evidence_family_count") or 0),
        source_candidate_count=int(rollup.get("source_candidate_count") or 1),
        target_class=original,
        canonical_review_unit=str(candidate.get("canonical_review_unit") or candidate.get("subsystem") or ""),
        title=str(candidate.get("title") or ""),
        suspected_issue=str(candidate.get("suspected_issue") or candidate.get("impact_summary") or ""),
        operational_mechanism=str(candidate.get("operational_mechanism") or ""),
        why_it_matters=str(candidate.get("why_it_matters") or ""),
        why_not_promoted=str(candidate.get("why_not_promoted") or raw_target_explanation.get("why_not_promoted") or ""),
        evidence_summary=_string_items(candidate.get("evidence_summary") or raw_target_explanation.get("evidence_summary")),
        counter_evidence_summary=_string_items(
            candidate.get("counter_evidence_summary") or raw_target_explanation.get("counter_evidence_summary")
        ),
        missing_evidence=_unique(candidate.get("missing_evidence") or []),
        blocked_reasons=reasons,
        caveats=_unique(candidate.get("caveats") or []),
    )
    if normal_observation and float(priority_result["score"]) > 0.35:
        priority_result = {
            **priority_result,
            "score": 0.35,
            "breakdown": {
                **priority_result["breakdown"],
                "normal_observation_cap": 0.35,
            },
        }
    review_priority_score = float(priority_result["score"])
    final_class = _final_class(original, runtime=runtime, reasons=reasons, score=promotion_score)
    state = {
        "primary_candidate": "primary_candidate",
        "validation_target": "validation_target",
        "monitor_only": "monitor_only",
        "auto_archived": "unsupported",
    }.get(final_class, "validation_target")
    linked_theme = theme_by_group.get(str(candidate.get("group_id") or "")) or _theme_for_candidate(candidate, text)
    request_type = request_by_theme.get(linked_theme) or _request_type_for_reasons(reasons, linked_theme)
    missing_evidence = _missing_evidence_for_target(candidate, reasons)
    target_explanation = _target_explanation_for_candidate(
        candidate,
        refs=refs,
        reasons=reasons,
        request_type=request_type,
        linked_theme=linked_theme,
    )
    target = {
        "target_id": str(candidate.get("target_id") or ""),
        "review_target_id": str(candidate.get("target_id") or ""),
        "class": final_class,
        "state": state,
        "source": str(candidate.get("source") or ""),
        "source_target_id": str(candidate.get("target_id") or ""),
        "title": str(candidate.get("title") or "Review target requires validation"),
        "impact_summary": str(candidate.get("impact_summary") or "Evidence requires validation."),
        "core_target_type": str(candidate.get("core_target_type") or "general_review"),
        "review_target_type": str(candidate.get("core_target_type") or "general_review"),
        "canonical_group_key": str(candidate.get("canonical_group_key") or ""),
        "canonical_target_type": str(candidate.get("canonical_target_type") or candidate.get("core_target_type") or "general_review"),
        "canonical_subject": str(candidate.get("canonical_subject") or ""),
        "canonical_review_unit": str(candidate.get("canonical_review_unit") or candidate.get("subsystem") or ""),
        "canonical_observation_group_id": str(candidate.get("group_id") or candidate.get("target_id") or ""),
        "source_target_ids": _unique(candidate.get("source_target_ids") or [candidate.get("target_id")]),
        "source_candidate_count": int(candidate.get("source_candidate_count") or 1),
        "subsystem": str(candidate.get("subsystem") or "general"),
        "component": str(candidate.get("component") or ""),
        "review_priority_score": round(review_priority_score, 4),
        "raw_review_priority_score": round(score_before, 4),
        "promotion_score": round(promotion_score, 4),
        "score_with_convergence": round(score_with_convergence, 4),
        "rollup_provider_ratio": float(rollup.get("rollup_provider_ratio") or 0.0),
        "baseline_support_score": float(rollup.get("baseline_support_score") or 0.0),
        "rollup": rollup,
        "score_breakdown": {
            "score_note": SCORE_NOTE,
            "base_score": round(score_before, 4),
            "convergence_bonus": round(convergence_bonus, 4),
            "score_with_convergence": round(score_with_convergence, 4),
            "promotion_score": round(promotion_score, 4),
            "review_priority_score": round(review_priority_score, 4),
            "priority_model": priority_result["breakdown"],
            "rollup": rollup,
        },
        "score_caps_applied": score_caps,
        "support_role": support_role,
        "support_role_policy": "Only runtime_evidence with evidence_id can support runtime incident claims.",
        "evidence_refs": refs,
        "evidence_diversity": evidence_diversity,
        "has_runtime_evidence": runtime,
        "has_user_impact_evidence": has_user_impact,
        "missing_evidence": missing_evidence,
        "caveats": _unique(candidate.get("caveats") or []),
        "target_explanation": target_explanation,
        "suspected_issue": str(target_explanation.get("suspected_issue") or ""),
        "operational_mechanism": str(target_explanation.get("operational_mechanism") or ""),
        "why_it_matters": str(target_explanation.get("why_it_matters") or ""),
        "evidence_summary": list(target_explanation.get("evidence_summary") or []),
        "counter_evidence_summary": list(target_explanation.get("counter_evidence_summary") or []),
        "why_not_promoted": str(target_explanation.get("why_not_promoted") or ""),
        "next_validation_question": str(target_explanation.get("next_validation_question") or ""),
        "providers": _unique(candidate.get("providers") or []),
        "provider_count": int(candidate.get("provider_count") or 0),
        "linked_disagreement_theme": linked_theme,
        "recommended_request_type": request_type,
        "promotion_blocked_reasons": reasons,
        "status": "pending",
        "profile": {"profile_id": "canonical_review_graph"},
        "cluster_id": str(candidate.get("group_id") or ""),
        "drawer": _drawer_for_target(
            candidate,
            bundle,
            refs,
            reasons,
            linked_theme,
            request_type,
            missing_evidence=missing_evidence,
            target_explanation=target_explanation,
        ),
    }
    decision = {
        "target_id": target["target_id"],
        "source_target_ids": list(target.get("source_target_ids") or []),
        "source_target_title": target["title"],
        "final_class": final_class,
        "original_class": original,
        "decision": _decision_label(original, final_class),
        "reasons": reasons,
        "score_before": round(score_before, 4),
        "score_with_convergence": round(score_with_convergence, 4),
        "score_after": round(promotion_score, 4),
        "review_priority_score": round(review_priority_score, 4),
        "convergence_bonus": round(convergence_bonus, 4),
        "priority_model": priority_result["breakdown"],
        "baseline_support_score": float(rollup.get("baseline_support_score") or 0.0),
        "score_caps_applied": score_caps,
    }
    return target, decision


def _missing_evidence_for_target(candidate: dict[str, Any], reasons: list[str]) -> list[str]:
    missing = _unique(candidate.get("missing_evidence") or [])
    if "user_impact_unverified" in reasons:
        missing.append("User impact or operational outcome evidence tied to this review unit.")
    if "cause_disagreement" in reasons or "no_baseline_agreement_or_causal_alignment" in reasons:
        missing.append("Causal alignment evidence connecting the review unit to the incident window.")
    if "support_without_evidence_id" in reasons:
        missing.append("Runtime Evidence Item IDs that support the claim.")
    if "support_is_context_not_runtime_evidence" in reasons:
        missing.append("Runtime log or metric evidence; source context alone cannot support an incident claim.")
    if "core_missing_evidence" in reasons:
        missing.append("Core evidence needed to close the promotion gate.")
    return _unique(missing)


def _target_explanation_for_candidate(
    candidate: dict[str, Any],
    *,
    refs: list[str],
    reasons: list[str],
    request_type: str,
    linked_theme: str,
) -> dict[str, Any]:
    raw = candidate.get("target_explanation") if isinstance(candidate.get("target_explanation"), dict) else {}
    unit = str(candidate.get("canonical_review_unit") or candidate.get("component") or candidate.get("subsystem") or "review unit")
    suspected_issue = (
        _first_non_meta_text(
            candidate.get("suspected_issue"),
            raw.get("suspected_issue"),
            candidate.get("impact_summary"),
            candidate.get("title"),
        )
        or _fallback_suspected_issue(candidate, unit=unit, request_type=request_type)
    )
    operational_mechanism = (
        str(candidate.get("operational_mechanism") or raw.get("operational_mechanism") or "").strip()
        or _fallback_operational_mechanism(candidate, unit=unit)
    )
    why_it_matters = (
        str(candidate.get("why_it_matters") or raw.get("why_it_matters") or "").strip()
        or "The current evidence may affect an operational outcome, but user impact is not proven by this target alone."
    )
    evidence_summary = _unique(
        [
            *_string_items(candidate.get("evidence_summary")),
            *_string_items(raw.get("evidence_summary")),
        ]
    )
    if not evidence_summary:
        evidence_summary = [
            f"{ref}: cited runtime evidence for this review unit; inspect the Evidence Item body before changing incident state."
            for ref in refs[:8]
        ]
    counter_summary = _unique(
        [
            *_string_items(candidate.get("counter_evidence_summary")),
            *_string_items(raw.get("counter_evidence_summary")),
        ]
    )
    if not counter_summary and reasons:
        counter_summary = [f"Promotion blocker: {reason}." for reason in reasons[:4]]
    why_not_promoted = (
        str(candidate.get("why_not_promoted") or raw.get("why_not_promoted") or "").strip()
        or _why_not_promoted_from_reasons(reasons)
    )
    next_validation_question = (
        str(candidate.get("next_validation_question") or raw.get("next_validation_question") or "").strip()
        or _next_validation_question(unit=unit, request_type=request_type, linked_theme=linked_theme)
    )
    provider_explanations = list(raw.get("provider_explanations") or [])
    return {
        "schema_version": "target_explanation.v1",
        "suspected_issue": suspected_issue,
        "operational_mechanism": operational_mechanism,
        "why_it_matters": why_it_matters,
        "evidence_summary": evidence_summary,
        "counter_evidence_summary": counter_summary,
        "why_not_promoted": why_not_promoted,
        "next_validation_question": next_validation_question,
        "provider_explanations": provider_explanations,
    }


def _fallback_operational_mechanism(candidate: dict[str, Any], *, unit: str) -> str:
    target_type = str(candidate.get("core_target_type") or candidate.get("canonical_target_type") or "").casefold()
    subsystem = str(candidate.get("subsystem") or "").casefold()
    text = f"{target_type} {subsystem} {unit}".casefold()
    if "job" in text or "config" in text or "deployment" in text:
        return "Configuration, deployment, or scheduled-job behavior may be shaping the observed runtime signal."
    if "runtime" in text or "restart" in text or "watchdog" in text:
        return "Runtime recovery or watchdog orchestration may be shaping the observed state transitions."
    if "external" in text or "dependency" in text or "youtube" in text:
        return "An external dependency or downstream health signal may be involved, but it needs independent confirmation."
    if "observability" in text or "instrument" in text or "metric" in text:
        return "The instrumentation contract may be incomplete or inconsistent with the runtime behavior."
    return f"The `{unit}` review unit groups provider claims and cited evidence that need a human operational interpretation."


def _fallback_suspected_issue(candidate: dict[str, Any], *, unit: str, request_type: str) -> str:
    text = " ".join(
        str(candidate.get(key) or "")
        for key in ("core_target_type", "canonical_target_type", "subsystem", "component", "title")
    ).casefold()
    if "job" in text or "config" in text or "deployment" in text or "deployment_correlation" in request_type:
        return (
            f"Review whether configuration, deployment timing, or scheduled-job behavior for `{unit}` "
            "correlates with the cited runtime evidence."
        )
    if "runtime" in text or "restart" in text or "watchdog" in text:
        return f"Review whether `{unit}` indicates a runtime recovery or watchdog behavior that needs validation."
    if "external" in text or "dependency" in text or "youtube" in text:
        return f"Review whether `{unit}` reflects an external dependency or downstream health issue."
    if "observability" in text or "instrument" in text or "metric" in text:
        return f"Review whether `{unit}` reflects an instrumentation or observability contract gap."
    return f"Review what operational issue `{unit}` represents before promoting it."


def _first_non_meta_text(*values: object) -> str:
    for value in values:
        text = str(value or "").strip()
        if text and not _is_meta_summary(text):
            return text
    return ""


def _is_meta_summary(text: str) -> bool:
    lowered = text.casefold()
    return any(
        token in lowered
        for token in (
            "providers aligned",
            "schema-valid providers projected",
            "review target requires validation",
            "this is not majority-vote truth",
            "technical review support",
        )
    )


def _why_not_promoted_from_reasons(reasons: list[str]) -> str:
    if "user_impact_unverified" in reasons:
        return "Not promoted because user impact or operational outcome evidence is not attached to this target."
    if "support_is_context_not_runtime_evidence" in reasons:
        return "Not promoted because context can guide interpretation but cannot prove runtime incident support."
    if "support_without_evidence_id" in reasons:
        return "Not promoted because runtime support is missing usable Evidence Item IDs."
    if "cause_disagreement" in reasons or "no_baseline_agreement_or_causal_alignment" in reasons:
        return "Not promoted because causal alignment is not established across the current evidence."
    if reasons:
        return "Not promoted because one or more promotion gates remain open: " + ", ".join(reasons) + "."
    return "Not promoted until a human reviews the cited evidence and confirms the operational outcome."


def _next_validation_question(*, unit: str, request_type: str, linked_theme: str) -> str:
    if request_type:
        return f"Can `{request_type}` confirm whether `{unit}` explains the observed runtime behavior?"
    if linked_theme:
        return f"Does the validation theme `{linked_theme}` close or weaken `{unit}`?"
    return f"What read-only evidence would confirm or reject `{unit}` as an operational issue?"


def _string_items(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value if str(item).strip()]
    text = str(value or "").strip()
    return [text] if text else []


def _rollup_metrics(candidate: dict[str, Any]) -> dict[str, Any]:
    raw = candidate.get("rollup") if isinstance(candidate.get("rollup"), dict) else {}
    if raw:
        return {
            "source_candidate_count": int(raw.get("source_candidate_count") or candidate.get("source_candidate_count") or 1),
            "independent_provider_count": int(raw.get("independent_provider_count") or len(candidate.get("providers") or [])),
            "provider_vote_counts": dict(raw.get("provider_vote_counts") or {}),
            "same_provider_duplicate_count": int(raw.get("same_provider_duplicate_count") or 0),
            "evidence_ref_count": int(raw.get("evidence_ref_count") or len(candidate.get("evidence_refs") or [])),
            "evidence_family_count": int(raw.get("evidence_family_count") or 0),
            "evidence_family_counts": dict(raw.get("evidence_family_counts") or {}),
            "target_type_votes": dict(raw.get("target_type_votes") or {}),
            "distinct_target_type_count": int(raw.get("distinct_target_type_count") or 0),
            "provider_convergence_bonus": float(raw.get("provider_convergence_bonus") or 0.0),
            "evidence_diversity_bonus": float(raw.get("evidence_diversity_bonus") or 0.0),
            "target_type_convergence_bonus": float(raw.get("target_type_convergence_bonus") or 0.0),
            "repeated_independent_claim_bonus": float(raw.get("repeated_independent_claim_bonus") or 0.0),
            "same_provider_duplicate_bonus": float(raw.get("same_provider_duplicate_bonus") or 0.0),
            "priority_bonus": float(raw.get("priority_bonus") or 0.0),
            "rollup_provider_ratio": float(raw.get("rollup_provider_ratio") or 0.0),
            "baseline_support_score": float(raw.get("baseline_support_score") or 0.0),
        }
    providers = _unique(candidate.get("providers") or [])
    refs = _unique(candidate.get("evidence_refs") or [])
    return {
        "source_candidate_count": int(candidate.get("source_candidate_count") or 1),
        "independent_provider_count": len(providers),
        "provider_vote_counts": {provider: 1 for provider in providers},
        "same_provider_duplicate_count": 0,
        "evidence_ref_count": len(refs),
        "evidence_family_count": len({_evidence_family(ref) for ref in refs if _evidence_family(ref)}),
        "evidence_family_counts": {},
        "target_type_votes": {str(candidate.get("core_target_type") or "general_review"): 1},
        "distinct_target_type_count": 1,
        "provider_convergence_bonus": 0.0,
        "evidence_diversity_bonus": 0.0,
        "target_type_convergence_bonus": 0.0,
        "repeated_independent_claim_bonus": 0.0,
        "same_provider_duplicate_bonus": 0.0,
        "priority_bonus": 0.0,
        "rollup_provider_ratio": float(candidate.get("rollup_provider_ratio") or 0.0),
        "baseline_support_score": float(candidate.get("baseline_support_score") or 0.0),
    }


def _convergence_priority_bonus(rollup: dict[str, Any]) -> float:
    return min(0.18, max(0.0, float(rollup.get("priority_bonus") or 0.0)))


def _review_priority_score(
    *,
    promotion_score: float,
    score_with_convergence: float,
    rollup: dict[str, Any],
    reasons: list[str],
    runtime: bool,
) -> float:
    if not runtime or not rollup:
        return promotion_score
    hard_priority_blocks = {
        "support_is_context_not_runtime_evidence",
        "support_without_evidence_id",
        "severity_only_signal",
        "blocking_caveat_present",
    }
    if hard_priority_blocks.intersection(reasons):
        return promotion_score
    if int(rollup.get("source_candidate_count") or 0) <= 1:
        return promotion_score
    return min(0.86, max(promotion_score, score_with_convergence))


def _evidence_family(ref: str) -> str:
    value = str(ref or "").strip().upper()
    if not value:
        return ""
    if "-" in value:
        return value.split("-", 1)[0]
    return "".join(ch for ch in value if ch.isalpha())


def _final_class(original: str, *, runtime: bool, reasons: list[str], score: float) -> str:
    if original == "context":
        return "monitor_only"
    if NORMAL_OPERATION_REASON in reasons:
        return "monitor_only"
    if "support_without_evidence_id" in reasons and not runtime:
        return "auto_archived"
    if original == "monitor_only":
        return "monitor_only"
    if original == "auto_archived":
        return "auto_archived"
    blocking = {
        "no_baseline_agreement_or_causal_alignment",
        "support_is_context_not_runtime_evidence",
        "single_metric_only",
        "severity_only_signal",
        "user_impact_unverified",
        "blocking_caveat_present",
        "cause_disagreement",
        "impact_disagreement",
        "core_missing_evidence",
    }
    if blocking.intersection(reasons):
        return "validation_target"
    if original == "primary_candidate" and runtime and score >= 0.75:
        return "primary_candidate"
    return "validation_target"


def _canonical_finding(
    primary_targets: list[dict[str, Any]],
    validation_targets: list[dict[str, Any]],
    monitor_only: list[dict[str, Any]],
    agreement_dimensions: dict[str, Any],
) -> dict[str, str]:
    if primary_targets:
        top = primary_targets[0]
        return {
            "title": str(top.get("title") or "Primary incident candidate"),
            "impact": str(top.get("impact_summary") or "Primary candidate was promoted by Review Target Arbitration."),
        }
    baseline = agreement_dimensions.get("baseline_agreement") if isinstance(agreement_dimensions.get("baseline_agreement"), dict) else {}
    technical_baseline = (
        agreement_dimensions.get("technical_baseline_agreement")
        if isinstance(agreement_dimensions.get("technical_baseline_agreement"), dict)
        else {}
    )
    if validation_targets and technical_baseline.get("established") and baseline.get("established") is False:
        return {
            "title": "Technical support requires impact validation",
            "impact": (
                "Providers aligned on technical support, but user impact or incident impact is not verified. "
                f"{len(validation_targets)} validation targets remain for human review, and no primary incident candidate was promoted."
            ),
        }
    if validation_targets and baseline.get("established") is False:
        return {
            "title": "Multi-AI disagreement requires validation",
            "impact": (
                f"No incident-promotion agreement was found. {len(validation_targets)} validation targets remain for human review, "
                "and no primary incident candidate was promoted."
            ),
        }
    if validation_targets:
        return {
            "title": "Evidence requires validation",
            "impact": f"{len(validation_targets)} validation targets remain for human review.",
        }
    if monitor_only:
        return {
            "title": "No actionable incident candidate promoted",
            "impact": "Signals were routed to monitor-only or archive.",
        }
    return {
        "title": "Evidence requires profile or additional context",
        "impact": "No sufficiently supported review target was promoted.",
    }


def _planner_inputs(
    *,
    validation_targets: list[dict[str, Any]],
    promotion_decisions: list[dict[str, Any]],
    disagreement_themes: list[dict[str, Any]],
    synthesis: dict[str, Any],
) -> dict[str, Any]:
    request_types: list[str] = []
    missing: list[str] = []
    for theme in disagreement_themes:
        request = str(theme.get("recommended_validation") or "")
        if request:
            request_types.append(request)
    for target in validation_targets:
        if target.get("recommended_request_type"):
            request_types.append(str(target.get("recommended_request_type")))
        missing.extend(str(item) for item in target.get("missing_evidence") or [] if str(item).strip())
    for decision in promotion_decisions:
        for reason in decision.get("reasons") or []:
            request = REASON_TO_REQUEST_TYPE.get(str(reason))
            if request:
                request_types.append(request)
    for request in synthesis.get("missing_evidence_requests") or []:
        if isinstance(request, dict) and request.get("question"):
            missing.append(str(request.get("question")))
    return {
        "recommended_request_types": _unique(request_types),
        "missing_evidence": _unique(missing),
        "validation_target_ids": [str(target.get("target_id") or "") for target in validation_targets],
        "validation_target_priorities": [
            {
                "target_id": str(target.get("target_id") or ""),
                "canonical_review_unit": str(target.get("canonical_review_unit") or ""),
                "review_priority_score": float(target.get("review_priority_score") or 0.0),
                "promotion_score": float(target.get("promotion_score") or target.get("review_priority_score") or 0.0),
                "rollup_provider_ratio": float(target.get("rollup_provider_ratio") or 0.0),
                "baseline_support_score": float(target.get("baseline_support_score") or 0.0),
                "source_candidate_count": int(target.get("source_candidate_count") or 1),
                "recommended_request_type": str(target.get("recommended_request_type") or ""),
            }
            for target in sorted(
                validation_targets,
                key=lambda row: (-float(row.get("review_priority_score") or 0.0), str(row.get("target_id") or "")),
            )
        ],
        "promotion_decision_reasons": _unique(
            reason for decision in promotion_decisions for reason in decision.get("reasons") or []
        ),
    }


def _drawer_for_target(
    candidate: dict[str, Any],
    bundle: dict[str, Any],
    refs: list[str],
    reasons: list[str],
    linked_theme: str,
    request_type: str,
    *,
    missing_evidence: list[str] | None = None,
    target_explanation: dict[str, Any] | None = None,
) -> dict[str, Any]:
    raw = candidate.get("raw") if isinstance(candidate.get("raw"), dict) else {}
    raw_drawer = raw.get("drawer") if isinstance(raw.get("drawer"), dict) else {}
    return {
        "evidence_sha256": str(bundle.get("evidence_sha256") or candidate.get("evidence_sha256") or ""),
        "support_evidence": raw_drawer.get("support_evidence") or candidate.get("support_evidence") or [],
        "counter_evidence": raw_drawer.get("counter_evidence") or candidate.get("counter_evidence") or [],
        "caveats": _unique([*(candidate.get("caveats") or []), *reasons]),
        "missing_evidence": _unique(missing_evidence or candidate.get("missing_evidence") or []),
        "target_explanation": target_explanation or {},
        "next_evidence_requests": [
            {
                "request_id": "CANONICAL-REQ-001",
                "request_type": request_type,
                "reason": "generated_from_canonical_review_graph",
            }
        ] if request_type else [],
        "synthesis": {
            "canonical_review_graph": True,
            "source": candidate.get("source"),
            "original_class": candidate.get("original_class"),
            "promotion_blocked_reasons": reasons,
            "linked_disagreement_theme": linked_theme,
            "recommended_request_type": request_type,
        },
    }


def _archived_target_from_synthesis(row: dict[str, Any], bundle: dict[str, Any]) -> dict[str, Any]:
    target_id = str(row.get("group_id") or "archived-" + sha256_json(row)[:12])
    return {
        "target_id": target_id,
        "review_target_id": target_id,
        "class": "auto_archived",
        "state": "unsupported",
        "source": "multi_ai_synthesis",
        "title": "Unsupported model claim archived",
        "impact_summary": "Support claim did not cite usable Evidence Items with evidence_id.",
        "core_target_type": "unsupported",
        "review_target_type": "unsupported",
        "subsystem": "general",
        "review_priority_score": 0.0,
        "support_role": "model_interpretation",
        "evidence_refs": [],
        "missing_evidence": [],
        "caveats": [str(row.get("reason") or "unsupported")],
        "providers": _unique(row.get("providers") or []),
        "provider_count": len(row.get("providers") or []),
        "status": "archived",
        "profile": {"profile_id": "canonical_review_graph"},
        "cluster_id": target_id,
        "drawer": {"evidence_sha256": str(bundle.get("evidence_sha256") or ""), "caveats": [str(row.get("reason") or "unsupported")]},
    }


def _evidence_refs_from_target(row: dict[str, Any]) -> list[str]:
    refs: list[str] = []
    refs.extend(str(ref) for ref in row.get("evidence_refs") or [] if str(ref).strip())
    drawer = row.get("drawer") if isinstance(row.get("drawer"), dict) else {}
    for key in ("support_evidence", "counter_evidence"):
        for item in drawer.get(key) or []:
            if isinstance(item, dict):
                ref = str(item.get("evidence_id") or item.get("id") or "")
                if ref:
                    refs.append(ref)
                refs.extend(str(ref) for ref in item.get("evidence_refs") or [] if str(ref).strip())
    return _unique(refs)


def _support_role(refs: list[str], candidate: dict[str, Any]) -> str:
    source = str(candidate.get("source") or "")
    if source in {"source_context", "source_analysis"}:
        return "source_context"
    if source == "human_answers":
        return "human_context"
    if refs and any(_is_runtime_evidence_ref(ref) for ref in refs):
        return "runtime_evidence"
    if refs and source == "legacy_review_target":
        return "runtime_evidence"
    return "model_interpretation"


def _is_runtime_evidence_ref(ref: str) -> bool:
    value = str(ref or "").strip().upper()
    return bool(value) and (value.startswith(RUNTIME_EVIDENCE_PREFIXES) or "EVIDENCE" in value)


def _has_user_impact(candidate: dict[str, Any], text: str) -> bool:
    if any(token in text for token in USER_IMPACT_TOKENS):
        return True
    target = str(candidate.get("core_target_type") or "").casefold()
    return target in {"user_impact_signal_gap", "external_dependency_failure", "network_error_signal"}


def _is_severity_only(candidate: dict[str, Any], text: str) -> bool:
    target = str(candidate.get("core_target_type") or "").casefold()
    signal = str(candidate.get("signal_type") or "").casefold()
    title = str(candidate.get("title") or "").strip().casefold()
    return target in SEVERITY_ONLY_SIGNALS or signal in SEVERITY_ONLY_SIGNALS or title in SEVERITY_ONLY_SIGNALS


def _blocking_caveats(candidate: dict[str, Any], text: str) -> list[str]:
    reasons: list[str] = []
    if any(token in text for token in BLOCKING_CAVEAT_TOKENS):
        if any(token in text for token in ("critical=false", "critical false", "critical_false")):
            reasons.append("critical_false")
        if any(token in text for token in ("user impact unverified", "user_impact_unverified", "impact unverified")):
            reasons.append("user_impact_unverified")
        if "context only" in text or "source context only" in text or "human answer only" in text:
            reasons.append("context_only")
    return _unique(reasons)


def _core_missing_evidence(candidate: dict[str, Any]) -> bool:
    missing = " ".join(str(item) for item in candidate.get("missing_evidence") or []).casefold()
    return any(token in missing for token in ("user impact", "cause", "root cause", "evidence_id", "critical", "external dependency"))


def _candidate_text(candidate: dict[str, Any]) -> str:
    return _joined_text(
        [
            candidate.get("title"),
            candidate.get("impact_summary"),
            candidate.get("core_target_type"),
            candidate.get("subsystem"),
            candidate.get("component"),
            *(candidate.get("missing_evidence") or []),
            *(candidate.get("caveats") or []),
        ]
    )


def _cap(score: float, cap: float, reason: str, caps: list[dict[str, Any]]) -> float:
    if score > cap:
        caps.append({"cap": cap, "reason": reason})
        return cap
    return score


def _decision_label(original: str, final_class: str) -> str:
    if final_class == "auto_archived":
        return "archived"
    if original == final_class:
        return "retained"
    if original == "primary_candidate" and final_class == "validation_target":
        return "downgraded"
    if final_class == "primary_candidate":
        return "promoted"
    return "classified"


def _theme_indexes(synthesis: dict[str, Any]) -> tuple[dict[str, str], dict[str, str]]:
    by_group: dict[str, str] = {}
    by_theme: dict[str, str] = {}
    for theme in synthesis.get("disagreement_themes") or []:
        if not isinstance(theme, dict):
            continue
        name = str(theme.get("theme") or "")
        request = str(theme.get("recommended_validation") or "")
        if name:
            by_theme[name] = request
        for group_id in theme.get("group_ids") or []:
            if name:
                by_group[str(group_id)] = name
    return by_group, by_theme


def _theme_for_candidate(candidate: dict[str, Any], text: str) -> str:
    if any(token in text for token in ("external", "dependency", "http_5xx", "timeout")):
        return "External dependency vs local instrumentation gap"
    if any(token in text for token in ("user impact", "delivery", "ingest", "watch", "audio")):
        return "User impact signal is unclear"
    if any(token in text for token in ("freshness", "stale", "timestamp", "drift")):
        return "Freshness drift requires timestamp validation"
    if any(token in text for token in ("metric", "count", "aggregation", "mismatch")):
        return "Metric/log instrumentation mismatch"
    if any(token in text for token in ("deployment", "config", "version")):
        return "Deployment or configuration correlation unclear"
    return "General disagreement requires validation"


def _request_type_for_reasons(reasons: list[str], linked_theme: str) -> str:
    for reason in reasons:
        request = REASON_TO_REQUEST_TYPE.get(reason)
        if request:
            return request
    return {
        "External dependency vs local instrumentation gap": "external_dependency_status_query",
        "User impact signal is unclear": "user_impact_signal_query",
        "Freshness drift requires timestamp validation": "freshness_signal_query",
        "Metric/log instrumentation mismatch": "instrumentation_consistency_query",
        "Deployment or configuration correlation unclear": "deployment_correlation_query",
    }.get(linked_theme, "instrumentation_consistency_query")


def _agreement_value(agreement_types: list[str], disagreement_types: list[str]) -> str:
    if not agreement_types:
        return "none"
    if len(set(agreement_types)) == 1 and not disagreement_types:
        return "strong"
    return "partial"


def _next_action_agreement_value(
    synthesis: dict[str, Any],
    disagreement_groups: list[dict[str, Any]],
    agreement_groups: list[dict[str, Any]],
) -> str:
    if not agreement_groups and disagreement_groups:
        return "partial" if len(synthesis.get("disagreement_themes") or []) <= 2 else "none"
    requests = [str(row.get("recommended_validation") or "") for row in synthesis.get("disagreement_themes") or [] if isinstance(row, dict)]
    if requests and len(set(requests)) == 1:
        return "strong"
    if requests or agreement_groups:
        return "partial"
    return "none"


def _has_legacy_primary_downgrade(decisions: list[dict[str, Any]]) -> bool:
    return any(
        decision.get("original_class") == "primary_candidate"
        and decision.get("final_class") != "primary_candidate"
        for decision in decisions
    )


def _joined_text(value: Any) -> str:
    if isinstance(value, dict):
        value = value.values()
    if isinstance(value, (list, tuple, set)):
        return " ".join(_joined_text(item) for item in value).casefold()
    return str(value or "").casefold()


def _unique(values: Iterable[Any]) -> list[str]:
    output: list[str] = []
    for value in values:
        text = str(value).strip()
        if text and text not in output:
            output.append(text)
    return output
