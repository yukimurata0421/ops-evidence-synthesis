from __future__ import annotations

import re
from collections import defaultdict
from typing import Any

from ops_evidence_synthesis.canonical import sha256_json
from ops_evidence_synthesis.profiles import (
    evidence_requests_for_target_type,
    load_profile,
    metric_names_by_zero_behavior,
    metric_semantics,
    profile_id_for_item,
    target_definition,
    target_type_for_metric,
    target_type_for_subsystem,
    target_type_for_text,
)


ZERO_IS_GOOD = metric_names_by_zero_behavior("healthy")
ZERO_IS_BAD = metric_names_by_zero_behavior("suspicious")

OBSERVABILITY_VOLUME_ONLY = {
    "active_hour_count",
    "active_service_count",
    "total_log_count",
}


def shape_review_queue(
    proposals: list[dict[str, Any]],
    *,
    include_hidden: bool = False,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    enriched = [_enrich_proposal(dict(item)) for item in proposals]
    _apply_cluster_hiding(enriched)
    _apply_cross_subsystem_primary_hiding(enriched)
    visible = enriched if include_hidden else [
        item for item in enriched if item["review_visibility"] == "review"
    ]
    visible.sort(
        key=lambda item: (
            -float(item.get("review_priority_score") or 0.0),
            str(item.get("cluster_id") or ""),
            str(item.get("proposition_id") or ""),
        )
    )
    if limit is not None:
        return visible[:limit]
    return visible


def _enrich_proposal(item: dict[str, Any]) -> dict[str, Any]:
    structured = dict(item.get("structured_evidence") or {})
    actions = item.get("suggested_actions") or []
    evidence_refs = item.get("evidence_refs") or []
    item["subsystem"] = item.get("subsystem") or "general"
    profile_id = profile_id_for_item(item)
    support = list(structured.get("support") or [])
    counter = list(structured.get("counter_evidence") or [])
    caveats = list(structured.get("caveats") or [])
    next_data = list(structured.get("next_data_needed") or item.get("next_data_needed") or [])
    insufficient_evidence = list(structured.get("insufficient_evidence") or [])

    providers = _unique(
        action.get("provider")
        for action in actions
        if action.get("provider")
    )
    model_names = _unique(
        action.get("model_name")
        for action in actions
        if action.get("model_name")
    )
    cluster_signature = _claim_signature(item, support=support, counter=counter)
    cluster_id = "cluster-" + sha256_json(
        {
            "evidence_sha256": item.get("evidence_sha256"),
            "window_start": str(item.get("window_start") or ""),
            "window_end": str(item.get("window_end") or ""),
            "subsystem": item.get("subsystem") or "general",
            "claim_signature": cluster_signature,
        }
    )[:16]

    default_next_data = _default_next_data_needed(
        cluster_signature,
        subsystem=str(item.get("subsystem") or ""),
        support=support,
        counter=counter,
        profile_id=profile_id_for_item(item),
    )
    if default_next_data:
        next_data = _unique([*next_data, *default_next_data])
        structured["next_data_needed"] = next_data
        item["structured_evidence"] = structured
        item["next_data_needed"] = next_data

    hidden_reasons: list[str] = []
    caps: list[dict[str, Any]] = []
    raw_score = float(item.get("review_priority_score") or 0.0)
    effective_score = raw_score
    review_history = _review_history_signal(item)

    support_count = len(support)
    counter_count = len(counter)
    evidence_count = len(set(str(ref) for ref in evidence_refs))
    missing_evidence_count = len(next_data)
    finding_status_counts = _finding_status_counts(structured, actions)
    identity_unknown_keys = _identity_unknown_keys(structured, actions, support=support)
    structured_review_modes = _structured_review_modes(structured, support=support, counter=counter)
    insufficient_evidence_only = bool(
        insufficient_evidence
        or finding_status_counts.get("insufficient_evidence")
    ) and support_count == 0
    no_finding_only = bool(finding_status_counts.get("no_finding")) and support_count == 0
    primary_incident_target = _is_primary_incident_target(cluster_signature, profile_id=profile_id)
    primary_incident_mode = primary_incident_target and not (
        cluster_signature == "job_configuration_mismatch"
        and str(item.get("subsystem") or "") != "job_configuration"
    )
    no_immediate_action = _has_no_immediate_action(item, actions)
    no_human_action = _has_no_human_action(actions)
    zero_good_improvement = _has_zero_good_improvement(item, support=support, counter=counter)
    zero_bad_worsening = _has_zero_bad_worsening(item, support=support, counter=counter)
    observability_volume_only = _is_observability_volume_only(
        item,
        support=support,
        counter=counter,
        next_data=next_data,
    )
    validation_review = _is_validation_review_candidate(
        item,
        cluster_signature=cluster_signature,
        support=support,
        counter=counter,
        next_data=next_data,
        zero_bad_worsening=zero_bad_worsening,
        zero_good_improvement=zero_good_improvement,
        observability_volume_only=observability_volume_only,
    )
    if insufficient_evidence_only or no_finding_only:
        validation_review = False
    identity_blocked = bool(identity_unknown_keys) and not zero_bad_worsening

    if no_finding_only:
        effective_score = min(effective_score, 0.25)
        caps.append({"reason": "no_finding", "max_score": 0.25})
        hidden_reasons.append("no_finding")
    elif insufficient_evidence_only:
        effective_score = min(effective_score, 0.35)
        caps.append({"reason": "insufficient_evidence", "max_score": 0.35})
        hidden_reasons.append("insufficient_evidence")
    if identity_unknown_keys and not zero_bad_worsening:
        if any(key in {"program", "source"} for key in identity_unknown_keys):
            effective_score = min(effective_score, 0.45)
            caps.append({"reason": "program_or_source_unknown", "max_score": 0.45})
            hidden_reasons.append("program_or_source_unknown")
        if "failure_signature" in identity_unknown_keys:
            effective_score = min(effective_score, 0.50)
            caps.append({"reason": "failure_signature_unknown", "max_score": 0.50})
            hidden_reasons.append("failure_signature_unknown")
        if "time_window" in identity_unknown_keys:
            effective_score = min(effective_score, 0.50)
            caps.append({"reason": "time_window_unknown", "max_score": 0.50})
            hidden_reasons.append("time_window_unknown")
    if evidence_count == 0:
        effective_score = min(effective_score, 0.65)
        caps.append({"reason": "evidence_id_missing", "max_score": 0.65})
    if support_count == 0 and counter_count == 0:
        effective_score = min(effective_score, 0.55)
        caps.append({"reason": "support_and_counter_empty", "max_score": 0.55})
        hidden_reasons.append("support_and_counter_empty")
    elif support_count == 0:
        effective_score = min(effective_score, 0.55)
        caps.append({"reason": "support_empty", "max_score": 0.55})
    if no_immediate_action and not zero_bad_worsening and not primary_incident_mode:
        effective_score = min(effective_score, 0.40)
        caps.append({"reason": "no_immediate_action", "max_score": 0.40})
        hidden_reasons.append("no_immediate_action")
    if zero_good_improvement and not zero_bad_worsening:
        effective_score = min(effective_score, 0.35)
        caps.append({"reason": "zero_is_good_monitor_only", "max_score": 0.35})
        hidden_reasons.append("monitor_only_zero_is_good")
    if observability_volume_only and not zero_bad_worsening:
        effective_score = min(effective_score, 0.40)
        caps.append({"reason": "observability_volume_only", "max_score": 0.40})
        hidden_reasons.append("monitor_only_observability_volume")
    if missing_evidence_count == 0 and no_human_action and not zero_bad_worsening:
        hidden_reasons.append("no_missing_evidence_and_no_human_action")
    if "proposal only" in _proposal_text(item):
        hidden_reasons.append("proposal_only_no_evidence")

    review_visibility = "review"
    if (zero_good_improvement or no_immediate_action) and not zero_bad_worsening and not primary_incident_mode:
        review_visibility = "monitor_only"
    if observability_volume_only and not zero_bad_worsening:
        review_visibility = "monitor_only"
    if hidden_reasons and review_visibility != "monitor_only":
        review_visibility = "hidden"
    if zero_bad_worsening:
        review_visibility = "review"
        hidden_reasons = [reason for reason in hidden_reasons if reason != "monitor_only_zero_is_good"]
        effective_score = max(effective_score, min(raw_score, 0.90))
    elif validation_review and not identity_blocked:
        review_visibility = "review"
        hidden_reasons = [
            reason
            for reason in hidden_reasons
            if reason
            not in {
                "monitor_only_zero_is_good",
                "no_missing_evidence_and_no_human_action",
            }
        ]
        effective_score = max(effective_score, min(raw_score, _validation_review_score_cap(cluster_signature, profile_id=profile_id)))

    floors: list[dict[str, Any]] = []
    priority_floor = _primary_incident_score_floor(
        cluster_signature if primary_incident_mode else "",
        profile_id=profile_id,
        zero_bad_worsening=zero_bad_worsening,
        validation_review=validation_review,
    )
    if priority_floor is not None and (support_count > 0 or zero_bad_worsening) and not identity_blocked:
        review_visibility = "review"
        hidden_reasons = [
            reason
            for reason in hidden_reasons
            if reason
            not in {
                "no_immediate_action",
                "support_and_counter_empty",
                "no_missing_evidence_and_no_human_action",
                "proposal_only_no_evidence",
            }
        ]
        if effective_score < priority_floor:
            floors.append({"reason": f"{cluster_signature}_primary_incident_floor", "min_score": priority_floor})
            effective_score = priority_floor

    if review_history["effect"] == "boost":
        effective_score = min(1.0, effective_score + float(review_history["score_delta"]))
        hidden_reasons = [
            reason
            for reason in hidden_reasons
            if reason not in {"no_missing_evidence_and_no_human_action", "proposal_only_no_evidence"}
        ]
    elif review_history["effect"] == "needs_more_data":
        effective_score = min(1.0, effective_score + float(review_history["score_delta"]))
        if next_data:
            review_visibility = "review"
            hidden_reasons = [
                reason
                for reason in hidden_reasons
                if reason not in {"no_missing_evidence_and_no_human_action", "support_and_counter_empty"}
            ]
    elif review_history["effect"] == "demote":
        effective_score = min(effective_score, float(review_history["max_score"]))
        caps.append({"reason": str(review_history["reason"]), "max_score": float(review_history["max_score"])})
        hidden_reasons.append(str(review_history["reason"]))
        review_visibility = "monitor_only" if review_history["detail"] in {"low_value", "not_actionable"} else "hidden"

    if no_finding_only:
        effective_score = min(effective_score, 0.25)
        review_visibility = "hidden"
        if "no_finding" not in hidden_reasons:
            hidden_reasons.append("no_finding")
    elif insufficient_evidence_only:
        effective_score = min(effective_score, 0.35)
        review_visibility = "hidden"
        if "insufficient_evidence" not in hidden_reasons:
            hidden_reasons.append("insufficient_evidence")

    if support_count == 0 and not zero_bad_worsening:
        effective_score = min(effective_score, 0.55)
        if not any(cap.get("reason") == "support_empty" for cap in caps):
            caps.append({"reason": "support_empty", "max_score": 0.55})
        if review_history["effect"] not in {"boost", "needs_more_data"}:
            hidden_reasons.append("support_empty" if counter_count > 0 else "support_and_counter_empty")
            review_visibility = "hidden"

    _repair_display_summaries(
        item,
        support=support,
        counter=counter,
        zero_bad_worsening=zero_bad_worsening,
    )

    item["raw_review_priority_score"] = raw_score
    item["review_priority_score"] = round(effective_score, 4)
    item["review_visibility"] = review_visibility
    item["hidden_reason"] = ", ".join(hidden_reasons)
    item["score_breakdown"] = {
        "raw_review_priority_score": raw_score,
        "effective_review_priority_score": round(effective_score, 4),
        "caps": caps,
        "floors": floors,
        "zero_is_good_improvement": zero_good_improvement,
        "zero_is_bad_worsening": zero_bad_worsening,
        "primary_incident_target": primary_incident_mode,
        "observability_volume_only": observability_volume_only,
        "validation_review": validation_review,
        "finding_status_counts": finding_status_counts,
        "identity_unknown_keys": identity_unknown_keys,
        "insufficient_evidence_only": insufficient_evidence_only,
        "no_immediate_action": no_immediate_action,
        "no_human_action": no_human_action,
        "review_history": review_history,
    }
    item["review_history"] = review_history
    item["cluster_id"] = cluster_id
    item["cluster_signature"] = cluster_signature
    item["cluster_size"] = 1
    item["cluster_member_ids"] = [item.get("proposition_id")]
    item["cluster_representative"] = True
    item["evidence_count"] = evidence_count
    item["support_count"] = support_count
    item["counter_count"] = counter_count
    item["missing_evidence_count"] = missing_evidence_count
    item["insufficient_evidence_count"] = len(insufficient_evidence)
    item["finding_status_counts"] = finding_status_counts
    item["identity_unknown_keys"] = identity_unknown_keys
    item["model_providers"] = providers
    item["model_provider"] = ", ".join(providers)
    item["model_names"] = model_names
    item["model_name"] = ", ".join(model_names)
    item["review_mode"] = (
        "incident_candidate"
        if zero_bad_worsening or primary_incident_mode or "incident_candidate" in structured_review_modes
        else "insufficient_evidence"
        if insufficient_evidence_only or no_finding_only
        else "validation_target"
        if validation_review or "validation_target" in structured_review_modes
        else "monitor_or_archive"
    )
    return item


def _review_history_signal(item: dict[str, Any]) -> dict[str, Any]:
    decision = str(item.get("latest_review_decision") or "").casefold()
    detail = str(item.get("latest_review_detail") or item.get("review_status") or "").casefold()
    if decision == "accepted" or detail in {"confirmed_candidate", "known_issue", "watchlist"}:
        return {
            "effect": "boost",
            "decision": decision or "accepted",
            "detail": detail or "confirmed_candidate",
            "score_delta": 0.10 if detail in {"known_issue", "confirmed_candidate"} else 0.06,
            "reason": "human_confirmed_candidate_history",
        }
    if decision == "needs_more_data" or detail == "needs_more_data":
        return {
            "effect": "needs_more_data",
            "decision": decision or "needs_more_data",
            "detail": "needs_more_data",
            "score_delta": 0.03,
            "reason": "human_requested_more_data_history",
        }
    if decision == "rejected" or detail in {"false_positive", "low_value", "duplicate", "not_actionable", "unsupported"}:
        max_score_by_detail = {
            "false_positive": 0.20,
            "duplicate": 0.22,
            "unsupported": 0.25,
            "not_actionable": 0.30,
            "low_value": 0.32,
        }
        max_score = max_score_by_detail.get(detail, 0.25)
        return {
            "effect": "demote",
            "decision": decision or "rejected",
            "detail": detail or "false_positive",
            "score_delta": -max_score,
            "max_score": max_score,
            "reason": f"human_rejected_{detail or 'false_positive'}",
        }
    return {
        "effect": "none",
        "decision": decision,
        "detail": detail,
        "score_delta": 0.0,
        "reason": "",
    }


def _apply_cluster_hiding(items: list[dict[str, Any]]) -> None:
    clusters: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in items:
        clusters[str(item["cluster_id"])].append(item)
    for members in clusters.values():
        if len(members) <= 1:
            continue
        members.sort(
            key=lambda item: (
                -float(item.get("review_priority_score") or 0.0),
                -int(item.get("evidence_count") or 0),
                -int(item.get("support_count") or 0),
                str(item.get("proposition_id") or ""),
            )
        )
        representative = members[0]
        ids = [str(item.get("proposition_id")) for item in members]
        providers = _unique(
            provider
            for item in members
            for provider in item.get("model_providers", [])
        )
        model_names = _unique(
            model_name
            for item in members
            for model_name in item.get("model_names", [])
        )
        for index, item in enumerate(members):
            item["cluster_size"] = len(members)
            item["cluster_member_ids"] = ids
            item["model_providers"] = providers
            item["model_provider"] = ", ".join(providers)
            item["model_names"] = model_names
            item["model_name"] = ", ".join(model_names)
            if item is representative:
                item["cluster_representative"] = True
                continue
            item["cluster_representative"] = False
            item["review_visibility"] = "hidden"
            reason = str(item.get("hidden_reason") or "")
            item["hidden_reason"] = ", ".join(
                _unique([reason, "duplicate_cluster_member"])
            )


def _apply_cross_subsystem_primary_hiding(items: list[dict[str, Any]]) -> None:
    groups: dict[tuple[str, str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for item in items:
        if item.get("review_visibility") != "review":
            continue
        if str(item.get("review_mode") or "") != "incident_candidate":
            continue
        signature = str(item.get("cluster_signature") or "")
        if not _is_primary_incident_target(signature, profile_id=profile_id_for_item(item)):
            continue
        groups[
            (
                str(item.get("evidence_sha256") or ""),
                str(item.get("window_start") or ""),
                str(item.get("window_end") or ""),
                signature,
            )
        ].append(item)
    for members in groups.values():
        if len(members) <= 1:
            continue
        members.sort(
            key=lambda item: (
                0 if "rule-engine" in set(item.get("model_providers") or []) else 1,
                -int(item.get("support_count") or 0),
                -float(item.get("review_priority_score") or 0.0),
                str(item.get("subsystem") or ""),
                str(item.get("proposition_id") or ""),
            )
        )
        representative = members[0]
        duplicate_ids = [str(item.get("proposition_id") or "") for item in members[1:]]
        representative["cross_subsystem_duplicate_ids"] = duplicate_ids
        for item in members[1:]:
            item["review_visibility"] = "hidden"
            item["cluster_representative"] = False
            item["hidden_reason"] = ", ".join(
                _unique([item.get("hidden_reason"), "duplicate_primary_core_target"])
            )


def _claim_signature(item: dict[str, Any], *, support: list[dict[str, Any]], counter: list[dict[str, Any]]) -> str:
    text = _proposal_text(item)
    profile_id = profile_id_for_item(item)
    subsystem = str(item.get("subsystem") or "")
    identity_target_types = _identity_target_types(item)
    if len(identity_target_types) == 1:
        return identity_target_types[0]
    evidence_target_types = _unique(
        evidence.get("core_target_type")
        for evidence in [*support, *counter]
        if evidence.get("core_target_type")
    )
    if len(evidence_target_types) == 1:
        return evidence_target_types[0]
    metric_names = sorted(
        {
            _metric_name_from_summary(str(evidence.get("summary") or ""))
            for evidence in [*support, *counter]
        }
        - {""}
    )
    for metric_name in metric_names:
        target_type = target_type_for_metric(metric_name, profile_id)
        if target_type:
            return target_type
    evidence_text = " ".join(str(evidence.get("summary") or "") for evidence in [*support, *counter]).casefold()
    evidence_target_type = target_type_for_text(evidence_text, profile_id)
    if evidence_target_type:
        return evidence_target_type
    subsystem_target_type = target_type_for_subsystem(subsystem, profile_id)
    if subsystem_target_type and not metric_names:
        return subsystem_target_type
    if subsystem == "runtime_recovery" and (
        "runtime_restart_count" in text
        or "service_health_failure_count" in text
        or {"runtime_restart_count", "service_health_failure_count"} & set(metric_names)
    ):
        return "restart_loop"
    if subsystem == "observability_contract" and (
        "active_service_count" in text
        or "total_log_count" in text
        or {"active_service_count", "total_log_count"} & set(metric_names)
    ):
        return "observability_volume_or_contract"
    if "active_service_count" in text or "total_log_count" in text or {"active_service_count", "total_log_count"} & set(metric_names):
        return "observability_volume_or_contract"
    text_target_type = target_type_for_text(text, profile_id)
    if text_target_type:
        return text_target_type
    if "runtime_restart_count" in text or "service_health_failure_count" in text:
        return "restart_loop"
    if metric_names:
        return "metric:" + ",".join(metric_names)
    words = re.findall(r"[a-z0-9_]{4,}", text)
    return "text:" + "-".join(words[:8])


def _identity_target_types(item: dict[str, Any]) -> list[str]:
    structured = dict(item.get("structured_evidence") or {})
    return _unique(
        row.get("core_target_type")
        for row in structured.get("evidence_identity") or []
        if isinstance(row, dict) and row.get("core_target_type")
    )


def _structured_review_modes(
    structured: dict[str, Any],
    *,
    support: list[dict[str, Any]],
    counter: list[dict[str, Any]],
) -> list[str]:
    rows = [
        row
        for row in structured.get("evidence_identity") or []
        if isinstance(row, dict)
    ]
    rows.extend(row for row in [*support, *counter] if isinstance(row, dict))
    return _unique(
        str(row.get("review_mode") or "")
        for row in rows
        if str(row.get("review_mode") or "") in {"incident_candidate", "validation_target", "monitor_only"}
    )


def _has_zero_good_improvement(
    item: dict[str, Any],
    *,
    support: list[dict[str, Any]],
    counter: list[dict[str, Any]],
) -> bool:
    text = _proposal_text(item)
    if "stable at 0" in text or "stable at zero" in text or "decreased to 0" in text or "decreased to zero" in text:
        if any(_zero_behavior_for_item(item, _metric_name_from_summary(str(evidence.get("summary") or ""))) == "healthy" for evidence in [*support, *counter]):
            return True
    for evidence in [*support, *counter]:
        metric = _metric_name_from_summary(str(evidence.get("summary") or ""))
        if _zero_behavior_for_item(item, metric) == "healthy" and _float(evidence.get("current_value")) == 0:
            baseline = _float(evidence.get("baseline_value"))
            if baseline is None or baseline >= 0:
                return True
    return False


def _has_zero_bad_worsening(
    item: dict[str, Any],
    *,
    support: list[dict[str, Any]],
    counter: list[dict[str, Any]],
) -> bool:
    text = _proposal_text(item)
    for evidence in [*support, *counter]:
        metric = _metric_name_from_summary(str(evidence.get("summary") or ""))
        if _zero_behavior_for_item(item, metric) == "suspicious" and _float(evidence.get("current_value")) == 0:
            baseline = _float(evidence.get("baseline_value"))
            if baseline is not None and baseline > 0:
                return True
    return any(metric in text and ("dropped to 0" in text or "dropped to zero" in text) for metric in ZERO_IS_BAD)


def _is_observability_volume_only(
    item: dict[str, Any],
    *,
    support: list[dict[str, Any]],
    counter: list[dict[str, Any]],
    next_data: list[str],
) -> bool:
    if next_data:
        return False
    metric_names = {
        _metric_name_from_summary(str(evidence.get("summary") or ""))
        for evidence in [*support, *counter]
    } - {""}
    volume_only_metrics = _observability_volume_only_metrics(item)
    if not metric_names or not metric_names <= volume_only_metrics:
        return False
    return True


def _is_validation_review_candidate(
    item: dict[str, Any],
    *,
    cluster_signature: str,
    support: list[dict[str, Any]],
    counter: list[dict[str, Any]],
    next_data: list[str],
    zero_bad_worsening: bool,
    zero_good_improvement: bool,
    observability_volume_only: bool,
) -> bool:
    if zero_bad_worsening or observability_volume_only or not next_data:
        return False
    actions = item.get("suggested_actions") or []
    if _has_no_immediate_action(item, actions):
        return False
    if not support and not counter and not item.get("evidence_refs") and not actions:
        return False
    subsystem = str(item.get("subsystem") or "")
    raw_score = float(item.get("review_priority_score") or 0.0)
    if raw_score < 0.58:
        return False
    text = " ".join(
        [
            _proposal_text(item),
            cluster_signature,
            subsystem,
            *(str(value) for value in next_data),
            *(str(evidence.get("summary") or "") for evidence in [*support, *counter]),
        ]
    ).casefold()
    if cluster_signature in {"restart_loop", "state_mismatch"}:
        return any(term in text for term in ("dead", "watchdog_ok", "stream_service_substate"))
    if subsystem in {"resource_pressure"}:
        return False
    if cluster_signature == "observability_volume_or_contract":
        return False
    if cluster_signature in {
        "throughput_disappearance",
        "heartbeat_missing",
        "freshness_signal_gap",
        "user_impact_signal_gap",
        "network_error_signal",
        "external_dependency_failure",
        "instrumentation_mismatch",
    }:
        return True
    if cluster_signature in {"state_mismatch", "observability_contract_mismatch", "service_start_failure", "monitoring_gap"}:
        return True
    if zero_good_improvement and not evidence_requests_for_target_type(cluster_signature, profile_id_for_item(item)):
        return False
    return _has_validation_action(actions)


def _validation_review_score_cap(cluster_signature: str, *, profile_id: str) -> float:
    definition = target_definition(cluster_signature, profile_id)
    configured = _float(definition.get("validation_score_cap"))
    if configured is not None:
        return configured
    if cluster_signature == "throughput_disappearance":
        return 0.84
    if cluster_signature == "heartbeat_missing":
        return 0.82
    if cluster_signature == "network_error_signal":
        return 0.60
    if cluster_signature in {"freshness_signal_gap", "user_impact_signal_gap"}:
        return 0.56
    if cluster_signature in {"instrumentation_mismatch", "monitoring_gap"}:
        return 0.62
    if cluster_signature in {"state_mismatch", "observability_contract_mismatch"}:
        return 0.66
    if cluster_signature == "service_start_failure":
        return 0.70
    return 0.62


def _is_primary_incident_target(cluster_signature: str, *, profile_id: str) -> bool:
    definition = target_definition(cluster_signature, profile_id)
    if "primary_incident" in definition:
        return bool(definition.get("primary_incident"))
    return cluster_signature in {"throughput_disappearance", "heartbeat_missing", "job_configuration_mismatch"}


def _primary_incident_score_floor(
    cluster_signature: str,
    *,
    profile_id: str,
    zero_bad_worsening: bool,
    validation_review: bool,
) -> float | None:
    definition = target_definition(cluster_signature, profile_id)
    if definition:
        if zero_bad_worsening:
            configured = _float(definition.get("zero_bad_incident_score_floor"))
            if configured is not None:
                return configured
        if validation_review:
            configured = _float(definition.get("validation_incident_score_floor"))
            if configured is not None:
                return configured
        configured = _float(definition.get("incident_score_floor"))
        if configured is not None:
            return configured
    if cluster_signature == "throughput_disappearance":
        if zero_bad_worsening:
            return 0.90
        return 0.84 if validation_review else 0.80
    if cluster_signature == "heartbeat_missing":
        if zero_bad_worsening:
            return 0.88
        return 0.82 if validation_review else 0.78
    if cluster_signature == "job_configuration_mismatch":
        return 0.86 if validation_review else 0.82
    return None


def _has_validation_action(actions: list[dict[str, Any]]) -> bool:
    for action in actions:
        claim_type = str(action.get("claim_type") or "").casefold()
        text = " ".join(
            str(action.get(key) or "")
            for key in ("temporary_action", "permanent_action", "required_authority")
        ).casefold()
        if claim_type in {"validation_target", "next_data_needed", "caveat"}:
            return True
        if any(term in text for term in ("validate", "cross-check", "collect", "verify")):
            return True
    return False


def _finding_status_counts(structured: dict[str, Any], actions: list[dict[str, Any]]) -> dict[str, int]:
    raw_counts = structured.get("finding_statuses")
    if isinstance(raw_counts, dict) and raw_counts:
        return {
            str(status): int(count)
            for status, count in raw_counts.items()
            if str(status).strip()
        }
    counts: dict[str, int] = {}
    for action in actions:
        status = str(action.get("finding_status") or "").strip().casefold()
        if status:
            counts[status] = counts.get(status, 0) + 1
        elif str(action.get("claim_type") or "").strip().casefold() == "insufficient_evidence":
            counts["insufficient_evidence"] = counts.get("insufficient_evidence", 0) + 1
    return counts


def _identity_unknown_keys(
    structured: dict[str, Any],
    actions: list[dict[str, Any]],
    *,
    support: list[dict[str, Any]],
) -> list[str]:
    raw_rows = structured.get("evidence_identity")
    if isinstance(raw_rows, list) and raw_rows:
        rows = [row for row in raw_rows if isinstance(row, dict)]
    else:
        rows = [
            {
                "claim_id": action.get("claim_id"),
                **dict(action.get("evidence_identity") or {}),
            }
            for action in actions
            if isinstance(action.get("evidence_identity"), dict)
        ]
    support_claim_ids = {
        str(item.get("claim_id") or "")
        for item in support
        if item.get("claim_id")
    }
    if support_claim_ids:
        rows = [row for row in rows if str(row.get("claim_id") or "") in support_claim_ids]
    unknown_keys: list[str] = []
    for row in rows:
        for key in ("program", "source", "failure_signature", "time_window"):
            if str(row.get(key) or "").casefold() == "unknown":
                unknown_keys.append(key)
    return _unique(unknown_keys)


def _has_no_immediate_action(item: dict[str, Any], actions: list[dict[str, Any]]) -> bool:
    text = _proposal_text(item)
    if "no immediate action required" in text or "no immediate action needed" in text:
        return True
    return any(
        "no immediate action" in f"{action.get('temporary_action', '')} {action.get('permanent_action', '')}".casefold()
        for action in actions
    )


def _has_no_human_action(actions: list[dict[str, Any]]) -> bool:
    if not actions:
        return True
    for action in actions:
        authority = str(action.get("required_authority") or "").strip().casefold()
        action_text = f"{action.get('temporary_action', '')} {action.get('permanent_action', '')}".casefold()
        if authority not in {"", "none", "n/a"} and "no immediate action" not in action_text:
            return False
    return True


def _repair_display_summaries(
    item: dict[str, Any],
    *,
    support: list[dict[str, Any]],
    counter: list[dict[str, Any]],
    zero_bad_worsening: bool,
) -> None:
    support_summary = str(item.get("support_summary") or "").strip()
    counter_summary = str(item.get("counter_summary") or "").strip()

    if zero_bad_worsening and counter_summary and not counter:
        support_summary = _join_summary_fragments([support_summary, counter_summary])
        counter_summary = ""

    if not support_summary and support:
        support_summary = _join_summary_fragments(
            str(evidence.get("summary") or "")
            for evidence in support
        )
    if not counter_summary and counter:
        counter_summary = _join_summary_fragments(
            str(evidence.get("summary") or "")
            for evidence in counter
        )

    item["support_summary"] = support_summary
    item["counter_summary"] = counter_summary


def _default_next_data_needed(
    cluster_signature: str,
    *,
    subsystem: str,
    support: list[dict[str, Any]],
    counter: list[dict[str, Any]],
    profile_id: str = "generic",
) -> list[str]:
    # The caller's item is not available in this helper signature; infer from
    # metric semantics first, then fall back to generic target requests.
    metrics = {
        _metric_name_from_summary(str(evidence.get("summary") or ""))
        for evidence in [*support, *counter]
    }
    for metric in metrics:
        target_type = target_type_for_metric(metric, profile_id)
        if target_type:
            requests = evidence_requests_for_target_type(target_type, profile_id)
            if requests:
                return [str(request.get("description") or request.get("need")) for request in requests]
    requests = evidence_requests_for_target_type(cluster_signature, profile_id)
    if requests:
        return [str(request.get("description") or request.get("need")) for request in requests]
    return []


def _zero_behavior_for_item(item: dict[str, Any], metric_name: str) -> str:
    if not metric_name:
        return "unknown"
    return str(metric_semantics(metric_name, profile_id_for_item(item)).get("zero_behavior") or "unknown")


def _observability_volume_only_metrics(item: dict[str, Any]) -> set[str]:
    profile = load_profile(profile_id_for_item(item))
    review_policy = profile.get("review_policy") if isinstance(profile.get("review_policy"), dict) else {}
    configured = {
        str(metric)
        for metric in review_policy.get("observability_volume_only_metrics") or []
        if str(metric).strip()
    }
    return configured or OBSERVABILITY_VOLUME_ONLY


def _join_summary_fragments(values: Any) -> str:
    return "; ".join(_unique(str(value).strip() for value in values if str(value).strip()))


def _proposal_text(item: dict[str, Any]) -> str:
    parts: list[str] = []
    for key in ("question", "support_summary", "counter_summary", "subsystem"):
        parts.append(str(item.get(key) or ""))
    for key in ("validation_targets", "next_data_needed"):
        values = item.get(key) or []
        if isinstance(values, list):
            parts.extend(str(value) for value in values)
    for action in item.get("suggested_actions") or []:
        parts.extend(
            str(action.get(key) or "")
            for key in ("temporary_action", "permanent_action", "required_authority", "claim_type")
        )
    return " ".join(parts).casefold()


def _metric_name_from_summary(summary: str) -> str:
    match = re.match(r"\s*([a-zA-Z0-9_]+)\s*=", summary)
    return match.group(1) if match else ""


def _float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _unique(values: Any) -> list[str]:
    output: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if not text or text in output:
            continue
        output.append(text)
    return output
