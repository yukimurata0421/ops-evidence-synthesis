from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any

from ops_evidence_synthesis.canonical import sha256_json
from ops_evidence_synthesis.profiles import (
    evidence_requests_for_target_type,
    load_profile,
    metric_semantics,
    profile_id_for_item,
    profile_context_for_bundle,
    profile_label,
    target_definition,
    target_type_for_metric,
    target_type_for_subsystem,
    target_type_for_text,
    title_for_target_type,
)
from ops_evidence_synthesis.synthesis.review_quality import ZERO_IS_BAD, ZERO_IS_GOOD


REVIEW_TARGET_LIMIT = 5


def build_review_target_set(
    proposals: list[dict[str, Any]],
    *,
    limit: int = REVIEW_TARGET_LIMIT,
) -> dict[str, Any]:
    review_items = [item for item in proposals if item.get("review_visibility") == "review"]
    monitor_items = [item for item in proposals if item.get("review_visibility") == "monitor_only"]
    archived_items = [
        item
        for item in proposals
        if item.get("review_visibility") not in {"review", "monitor_only"}
    ]
    insufficient_items = [
        item
        for item in proposals
        if int(item.get("insufficient_evidence_count") or 0) > 0
        or "insufficient_evidence" in str(item.get("hidden_reason") or "")
        or "no_finding" in str(item.get("hidden_reason") or "")
    ]
    review_items.sort(
        key=lambda item: (
            -float(item.get("review_priority_score") or 0.0),
            str(item.get("cluster_id") or ""),
            str(item.get("proposition_id") or ""),
        )
    )
    targets = [build_review_target(item, proposals) for item in review_items[:limit]]
    _attach_review_target_relationships(targets)
    graph_summary = _review_graph_summary(targets)
    return {
        "summary": {
            "raw_propositions": len(proposals),
            "clusters": len({str(item.get("cluster_id") or item.get("proposition_id")) for item in proposals}),
            "claim_groups": len({str(item.get("cluster_id") or item.get("proposition_id")) for item in proposals}),
            "review_targets": len(targets),
            "primary_review_targets": graph_summary["primary_review_targets"],
            "validation_targets": graph_summary["validation_targets"],
            "review_graph": graph_summary["review_graph"],
            "monitor_only": len(monitor_items),
            "auto_archived": len(archived_items),
            "insufficient_evidence": len(insufficient_items),
            "score_note": "Score is review priority, not truth probability.",
            "score_note_ja": "Score is review priority, not truth probability.",
        },
        "targets": targets,
        "monitor_only": [_compact_hidden_item(item) for item in monitor_items],
        "auto_archived": [_compact_hidden_item(item) for item in archived_items],
    }


def _review_graph_summary(targets: list[dict[str, Any]]) -> dict[str, Any]:
    primary_targets = [
        target
        for target in targets
        if not target.get("parent_review_target_id")
        and str(target.get("review_mode") or "") == "incident_candidate"
    ]
    if not primary_targets and targets:
        primary_targets = [
            target
            for target in targets
            if not target.get("parent_review_target_id")
        ][:1]
    primary_ids = {str(target.get("review_target_id") or "") for target in primary_targets}
    validation_targets = [
        target
        for target in targets
        if str(target.get("review_target_id") or "") not in primary_ids
    ]
    graph_nodes = []
    for primary in primary_targets:
        related_ids = {
            str(row.get("review_target_id") or "")
            for row in primary.get("related_review_targets") or []
        }
        children = [
            {
                "review_target_id": child.get("review_target_id"),
                "title": child.get("title"),
                "core_target_type": child.get("core_target_type") or child.get("review_target_type"),
                "domain_label": child.get("domain_label"),
                "subsystem": child.get("subsystem"),
                "review_priority_score": child.get("review_priority_score"),
                "relationship": child.get("relationship") or "validation_target_for_primary_incident",
            }
            for child in validation_targets
            if str(child.get("parent_review_target_id") or "") == str(primary.get("review_target_id") or "")
            or str(child.get("review_target_id") or "") in related_ids
        ]
        graph_nodes.append(
            {
                "primary_review_target_id": primary.get("review_target_id"),
                "title": primary.get("title"),
                "core_target_type": primary.get("core_target_type") or primary.get("review_target_type"),
                "domain_label": primary.get("domain_label"),
                "subsystem": primary.get("subsystem"),
                "review_priority_score": primary.get("review_priority_score"),
                "validation_targets": children,
            }
        )
    orphan_validation = [
        {
            "review_target_id": target.get("review_target_id"),
            "title": target.get("title"),
            "core_target_type": target.get("core_target_type") or target.get("review_target_type"),
            "domain_label": target.get("domain_label"),
            "subsystem": target.get("subsystem"),
            "review_priority_score": target.get("review_priority_score"),
        }
        for target in validation_targets
        if not target.get("parent_review_target_id")
    ]
    return {
        "primary_review_targets": len(primary_targets),
        "validation_targets": len(validation_targets),
        "review_graph": {
            "primary_candidate_count": len(primary_targets),
            "validation_target_count": len(validation_targets),
            "nodes": graph_nodes,
            "orphan_validation_targets": orphan_validation,
        },
    }


def _attach_review_target_relationships(targets: list[dict[str, Any]]) -> None:
    if len(targets) < 2:
        return
    primary = _primary_incident_target(targets)
    if primary is None:
        return
    related = []
    for target in targets:
        if target is primary:
            continue
        if not _is_validation_target_child(target):
            continue
        child_summary = {
            "review_target_id": target.get("review_target_id"),
            "cluster_id": target.get("cluster_id"),
            "title": target.get("title"),
            "subsystem": target.get("subsystem"),
            "relationship": "validation_target_for_primary_incident",
        }
        related.append(child_summary)
        target["parent_review_target_id"] = primary.get("review_target_id")
        target["relationship"] = "validation_target_for_primary_incident"
        target.setdefault("why_survived", []).append(
            f"validation target for {primary.get('title')}"
        )
        drawer = dict(target.get("drawer") or {})
        synthesis = dict(drawer.get("synthesis") or {})
        synthesis["parent_review_target_id"] = primary.get("review_target_id")
        synthesis["parent_title"] = primary.get("title")
        synthesis["relationship"] = "validation_target_for_primary_incident"
        synthesis["why_unified"] = _unique(
            [
                *(synthesis.get("why_unified") or []),
                f"validation target for primary incident {primary.get('review_target_id')}",
            ]
        )
        drawer["synthesis"] = synthesis
        target["drawer"] = drawer
    if related:
        primary["related_review_targets"] = related
        drawer = dict(primary.get("drawer") or {})
        synthesis = dict(drawer.get("synthesis") or {})
        synthesis["related_review_targets"] = related
        drawer["synthesis"] = synthesis
        primary["drawer"] = drawer


def _primary_incident_target(targets: list[dict[str, Any]]) -> dict[str, Any] | None:
    primary_target_types = {"throughput_disappearance", "heartbeat_missing", "job_configuration_mismatch"}
    incident_candidates = [
        target
        for target in targets
        if str(target.get("review_mode") or "") == "incident_candidate"
        or str(target.get("core_target_type") or target.get("review_target_type") or "") in primary_target_types
    ]
    candidates = incident_candidates or targets
    return sorted(
        candidates,
        key=lambda target: -float(target.get("review_priority_score") or 0.0),
    )[0]


def _is_validation_target_child(target: dict[str, Any]) -> bool:
    if str(target.get("review_mode") or "") == "validation_target":
        return True
    return str(target.get("core_target_type") or target.get("review_target_type") or "") in {
        "external_dependency_failure",
        "network_error_signal",
        "user_impact_signal_gap",
        "freshness_signal_gap",
        "state_mismatch",
        "monitoring_gap",
        "instrumentation_mismatch",
        "observability_contract_mismatch",
        "service_start_failure",
    }


def build_review_target(item: dict[str, Any], all_items: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    members = _cluster_members(item, all_items or [item])
    member_ids = _unique(
        str(member_id)
        for member in members
        for member_id in (member.get("cluster_member_ids") or [member.get("proposition_id")])
        if member_id
    )
    supporting_providers = _providers_from_members(members)
    all_providers = _providers_from_members(all_items or members)
    model_names = _unique(
        str(model_name)
        for member in members
        for model_name in (member.get("model_names") or _split_csv(member.get("model_name")))
        if model_name
    )
    structured = dict(item.get("structured_evidence") or {})
    profile_id = profile_id_for_item(item)
    loaded_profile = load_profile(profile_id)
    item_source_system = str((item.get("profile") or {}).get("source_system") or "")
    profile_source_system = str(loaded_profile.get("source_system") or "")
    profile = {
        "profile_id": profile_id,
        "profile_label": profile_label(profile_id),
        "source_system": str(
            (profile_source_system if profile_id != "generic" else "")
            or item_source_system
            or profile_source_system
            or item.get("environment")
            or profile_id
        ),
    }
    support = list(structured.get("support") or [])
    counter = list(structured.get("counter_evidence") or [])
    caveats = _unique(
        [
            *(str(value) for value in structured.get("caveats") or []),
            *(str(value) for value in item.get("validation_targets") or []),
        ]
    )
    missing = _unique(
        [
            *(str(value) for value in structured.get("next_data_needed") or []),
            *(str(value) for value in item.get("next_data_needed") or []),
        ]
    )
    target_id = "rt-" + sha256_json(
        {
            "evidence_sha256": item.get("evidence_sha256"),
            "cluster_id": item.get("cluster_id"),
            "members": member_ids,
        }
    )[:20]
    score = round(float(item.get("review_priority_score") or 0.0), 4)
    review_target_type = _review_target_type(item, support=support, counter=counter)
    domain_label = title_for_target_type(review_target_type, profile_id, default="")
    title = domain_label or _title_for(item, support=support, counter=counter)
    proposal = _proposal_for(item, missing, support=support, counter=counter)
    resolution_status = _resolution_status_for_profile(profile_id)
    reviewed_caveats = _review_caveats(item, counter, caveats, support=support, missing=missing)
    next_evidence_requests = _next_evidence_requests_for(
        item,
        missing=missing,
        support=support,
        counter=counter,
    )
    next_cli_command = _next_cli_command(item, next_evidence_requests)
    score_breakdown = _score_breakdown(
        item,
        members=members,
        all_providers=all_providers,
        support=support,
        counter=counter,
        missing=missing,
    )
    model_agreement = _model_agreement(
        item,
        members,
        supporting_providers,
        model_names,
        all_providers=all_providers,
        support=support,
        counter=counter,
        missing=missing,
    )
    why_survived = _why_survived(
        item,
        members=members,
        support=support,
        counter=counter,
        missing=missing,
        supporting_providers=supporting_providers,
    )
    evidence_count = len(
        _unique(
            str(evidence.get("evidence_id") or "")
            for evidence in [*support, *counter]
            if evidence.get("evidence_id")
        )
    )
    return {
        "review_target_id": target_id,
        "cluster_id": str(item.get("cluster_id") or ""),
        "evidence_sha256": str(item.get("evidence_sha256") or ""),
        "title": title,
        "review_target_type": review_target_type,
        "core_target_type": review_target_type,
        "domain_label": title,
        "profile": profile,
        "subsystem": str(item.get("subsystem") or "general"),
        "review_priority": _priority_label(score),
        "review_priority_score": score,
        "review_mode": str(item.get("review_mode") or ""),
        "finding_status_counts": dict(item.get("finding_status_counts") or {}),
        "identity_unknown_keys": list(item.get("identity_unknown_keys") or []),
        "support_count": len(support),
        "counter_count": len(counter),
        "missing_evidence_count": len(missing),
        "evidence_count": evidence_count,
        "core_claim": _core_claim(item, support=support, counter=counter, missing=missing),
        "why_survived": why_survived,
        "support_summary": _support_summary_for(item, support=support, counter=counter, missing=missing),
        "counter_or_caveat_summary": _counter_or_caveat_summary(item, counter, reviewed_caveats),
        "proposal": proposal,
        "model_agreement": model_agreement,
        "score_breakdown": score_breakdown,
        "resolution_status": resolution_status,
        "actions": _actions_for(
            item,
            missing,
            next_evidence_requests=next_evidence_requests,
            next_cli_command=next_cli_command,
        ),
        "status": str(item.get("review_status") or "pending"),
        "raw_proposition_ids": member_ids,
        "representative_proposition_id": str(item.get("proposition_id") or ""),
        "drawer": {
            "evidence_sha256": str(item.get("evidence_sha256") or ""),
            "cluster_id": str(item.get("cluster_id") or ""),
            "time_window": {
                "window_start": str(item.get("window_start") or ""),
                "window_end": str(item.get("window_end") or ""),
            },
            "incident_window": {
                "service": str(item.get("service") or ""),
                "environment": str(item.get("environment") or ""),
                "window_start": str(item.get("window_start") or ""),
                "window_end": str(item.get("window_end") or ""),
            },
            "baseline_window": _baseline_window(item),
            "support_evidence": support,
            "counter_evidence": counter,
            "caveats": reviewed_caveats,
            "missing_evidence": missing,
            "insufficient_evidence": list(structured.get("insufficient_evidence") or []),
            "finding_status_counts": dict(item.get("finding_status_counts") or {}),
            "identity_unknown_keys": list(item.get("identity_unknown_keys") or []),
            "next_evidence_requests": next_evidence_requests,
            "next_cli_command": next_cli_command,
            "resolution_status": resolution_status,
            "model_outputs": _model_outputs_from_members(members),
            "parsed_json": [],
            "raw_proposition_ids": member_ids,
            "synthesis": {
                "cluster_id": str(item.get("cluster_id") or ""),
                "cluster_signature": str(item.get("cluster_signature") or ""),
                "core_target_type": review_target_type,
                "domain_label": title,
                "profile": profile,
                "cluster_size": int(item.get("cluster_size") or len(member_ids) or 1),
                "why_unified": _why_unified(item, members, supporting_providers),
            },
            "zero_semantics": _zero_semantics(support, counter, profile_id=profile_id),
        },
    }


def attach_review_target_artifacts(
    target: dict[str, Any],
    *,
    bundle: dict[str, Any] | None = None,
    model_runs: list[Any] | None = None,
    parsed_results: list[Any] | None = None,
    claims_by_id: dict[str, Any] | None = None,
) -> dict[str, Any]:
    enriched = dict(target)
    drawer = dict(enriched.get("drawer") or {})
    if bundle:
        baseline = bundle.get("baseline") if isinstance(bundle.get("baseline"), dict) else {}
        drawer["baseline_window"] = {
            "start": str(baseline.get("start") or bundle.get("lookback_window_start") or ""),
            "end": str(baseline.get("end") or bundle.get("window_start") or ""),
        }
        drawer["system_context"] = _system_context_for_bundle(bundle)
        drawer["evidence_refs_read"] = _evidence_refs_read(target, bundle)
    if model_runs:
        runs_by_provider = {
            str(getattr(run, "provider", "")): run
            for run in model_runs
        }
        outputs = []
        for provider in _unique(
            output.get("provider")
            for output in drawer.get("model_outputs") or []
            if output.get("provider")
        ):
            run = runs_by_provider.get(provider)
            if run is None:
                continue
            outputs.append(
                {
                    "provider": provider,
                    "model_name": str(getattr(run, "model_name", "")),
                    "prompt_sha256": str(getattr(run, "prompt_sha256", "")),
                    "model_input_sha256": str(getattr(run, "model_input_sha256", "")),
                    "raw_output_sha256": str(getattr(run, "raw_output_sha256", "")),
                    "status": str(getattr(run, "status", "")),
                    "latency_ms": int(getattr(run, "latency_ms", 0) or 0),
                    "raw_output_preview": str(getattr(run, "raw_output", ""))[:2000],
                }
            )
        if outputs:
            drawer["model_outputs"] = outputs
    if parsed_results:
        wanted_providers = {
            str(output.get("provider"))
            for output in drawer.get("model_outputs") or []
            if output.get("provider")
        }
        drawer["parsed_json"] = [
            {
                "result_id": str(getattr(result, "result_id", "")),
                "run_id": str(getattr(result, "run_id", "")),
                "provider": str(getattr(result, "provider", "")),
                "schema_valid": bool(getattr(result, "schema_valid", False)),
                "parsed_json_sha256": str(getattr(result, "parsed_json_sha256", "")),
                "parsed_json": getattr(result, "parsed_json", {}),
            }
            for result in parsed_results
            if not wanted_providers or str(getattr(result, "provider", "")) in wanted_providers
        ]
    if claims_by_id:
        drawer["claims"] = [
            _claim_to_json(claims_by_id[claim_id])
            for claim_id in _claim_ids_from_target(target)
            if claim_id in claims_by_id
        ]
    enriched["drawer"] = drawer
    return enriched


def _system_context_for_bundle(bundle: dict[str, Any]) -> dict[str, Any]:
    profile_context = profile_context_for_bundle(bundle)
    profile = dict(profile_context.get("profile") or {})
    system_profile = dict(profile_context.get("system_profile") or bundle.get("system_profile") or {})
    critical_outcomes = (
        system_profile.get("critical_outcomes")
        or system_profile.get("critical_user_outcomes")
        or []
    )
    return _drop_empty(
        {
            "system_name": bundle.get("service") or profile.get("source_system") or profile.get("profile_id") or "",
            "system_type": system_profile.get("system_type") or "",
            "purpose": system_profile.get("purpose") or "",
            "critical_outcomes": critical_outcomes,
            "profile": profile,
            "operational_contract": bundle.get("operational_contract") or profile_context.get("operational_contract") or {},
            "log_sources": bundle.get("log_sources") or profile_context.get("log_sources") or [],
            "metric_semantics": bundle.get("metric_semantics") or profile_context.get("metric_semantics") or {},
            "component_map": bundle.get("component_map") or profile_context.get("component_map") or {},
            "known_benign_noise": bundle.get("known_benign_noise") or profile_context.get("known_benign_noise") or [],
            "action_constraints": bundle.get("action_constraints") or profile_context.get("action_constraints") or [],
            "review_policy": bundle.get("review_policy") or profile_context.get("review_policy") or {},
            "context_note": bundle.get("context_note") or profile_context.get("context_note") or "",
        }
    )


def _resolution_status_for_profile(profile_id: str) -> dict[str, Any]:
    resolution = load_profile(profile_id).get("resolution_status") or {}
    if not isinstance(resolution, dict):
        return {}
    verification = [str(item) for item in resolution.get("verification") or [] if str(item).strip()]
    return _drop_empty(
        {
            "status": str(resolution.get("status") or ""),
            "fix": str(resolution.get("fix") or ""),
            "verification": verification,
        }
    )


def more_data_request_for_target(target: dict[str, Any], generated_query: dict[str, Any] | None = None) -> dict[str, Any]:
    drawer = dict(target.get("drawer") or {})
    missing = list(drawer.get("missing_evidence") or [])
    subsystem = str(target.get("subsystem") or "")
    required_metric = _required_metrics_for(subsystem, missing, drawer)
    next_evidence_requests = list(drawer.get("next_evidence_requests") or [])
    required_log_source = _unique(
        str(source)
        for request in next_evidence_requests
        if isinstance(request, dict)
        for source in request.get("preferred_sources") or []
        if source
    ) or _required_log_sources_for(subsystem, missing)
    next_cli_command = str(drawer.get("next_cli_command") or "")
    return {
        "review_target_id": target.get("review_target_id"),
        "representative_proposition_id": target.get("representative_proposition_id"),
        "next_data_needed": missing,
        "next_evidence_requests": next_evidence_requests,
        "required_metric": required_metric,
        "required_log_source": required_log_source,
        "next_cli_command": next_cli_command,
        "next_query": generated_query or {},
    }


def _cluster_members(item: dict[str, Any], all_items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    cluster_id = str(item.get("cluster_id") or "")
    if not cluster_id:
        return [item]
    members = [member for member in all_items if str(member.get("cluster_id") or "") == cluster_id]
    return members or [item]


def _review_target_type(
    item: dict[str, Any],
    *,
    support: list[dict[str, Any]],
    counter: list[dict[str, Any]],
) -> str:
    profile_id = profile_id_for_item(item)
    signature = str(item.get("cluster_signature") or "")
    identity_types = _identity_target_types(item)
    if len(identity_types) == 1:
        return identity_types[0]
    evidence_types = _unique(
        str(evidence.get("core_target_type") or "")
        for evidence in [*support, *counter]
        if evidence.get("core_target_type")
    )
    if len(evidence_types) == 1:
        return evidence_types[0]
    for evidence in [*support, *counter]:
        metric = _metric_name(str(evidence.get("summary") or ""))
        target_type = target_type_for_metric(metric, profile_id)
        if target_type:
            return target_type
    subsystem_target_type = target_type_for_subsystem(str(item.get("subsystem") or ""), profile_id)
    if subsystem_target_type and not support and not counter:
        return subsystem_target_type
    if signature and target_definition(signature, profile_id):
        return signature
    text = _item_text(item, support, counter)
    return target_type_for_text(text, profile_id) or signature or "monitoring_gap"


def _identity_target_types(item: dict[str, Any]) -> list[str]:
    structured = dict(item.get("structured_evidence") or {})
    return _unique(
        str(row.get("core_target_type") or "")
        for row in structured.get("evidence_identity") or []
        if isinstance(row, dict) and row.get("core_target_type")
    )


def _title_for(
    item: dict[str, Any],
    *,
    support: list[dict[str, Any]],
    counter: list[dict[str, Any]],
) -> str:
    profile_id = profile_id_for_item(item)
    signature = str(item.get("cluster_signature") or "")
    profile_title = title_for_target_type(signature, profile_id, default="")
    if profile_title:
        return profile_title
    subsystem = str(item.get("subsystem") or "")
    text = _item_text(item, support, counter)
    if signature == "transport_disappearance" or "stream_transport_count" in text:
        return "Stream transport disappeared"
    if subsystem == "youtube_health":
        return "YouTube ingest status needs validation"
    if signature == "connection_reset":
        return "Network reset counter-evidence needs validation"
    if signature == "chromium_capture_errors" and not support and not counter:
        return "Chromium capture evidence gap needs validation"
    if signature.startswith("health_contract_mismatch") or ("watchdog_ok" in text and "dead" in text):
        return "Health signal contradicts dead service state"
    if signature == "rtmps_ffmpeg_instability" or any(term in text for term in ("ffmpeg", "rtmps")):
        return "RTMPS or ffmpeg instability needs review"
    if signature == "runtime_recovery_health":
        return "Runtime recovery evidence needs review"
    if signature == "chromium_capture_errors":
        return "Chromium capture instability needs review"
    if signature == "connection_reset":
        return "Network reset evidence needs review"
    if str(item.get("subsystem") or "") == "audio_energy":
        return "Audio energy gap needs review"
    question = str(item.get("question") or "").strip()
    return question[:96] if question else "Incident evidence needs review"


def _core_claim(
    item: dict[str, Any],
    *,
    support: list[dict[str, Any]],
    counter: list[dict[str, Any]],
    missing: list[str],
) -> str:
    zero_bad = _zero_bad_drop(support, counter)
    if zero_bad:
        metric, baseline, current = zero_bad
        return f"{metric} changed from baseline {baseline:g} to {current:g}."
    zero_good = _zero_good_signal(support, counter)
    if str(item.get("review_mode") or "") == "validation_target" and zero_good:
        metric, baseline, current = zero_good
        claim_missing = _claim_missing_items(missing)
        return (
            f"{metric} is {current:g} versus baseline {baseline:g}; this is counter-evidence, not proof. "
            f"Validate {', '.join(claim_missing[:3])}."
        )
    if str(item.get("review_mode") or "") == "validation_target" and not support and not counter:
        claim_missing = _claim_missing_items(missing)
        return (
            "No structured support evidence survived synthesis; this card remains because the missing evidence is concrete "
            f"and actionable: {', '.join(claim_missing[:3])}."
        )
    for value in (item.get("support_summary"), item.get("counter_summary"), item.get("question")):
        text = str(value or "").strip()
        if text:
            return text[:900]
    for evidence in [*support, *counter]:
        summary = str(evidence.get("summary") or "").strip()
        if summary:
            return summary[:900]
    return ""


def _support_summary_for(
    item: dict[str, Any],
    *,
    support: list[dict[str, Any]],
    counter: list[dict[str, Any]],
    missing: list[str],
) -> str:
    text = _summary_text(item.get("support_summary"), support)
    if text:
        return text
    if str(item.get("review_mode") or "") == "validation_target":
        claim_missing = _claim_missing_items(missing)
        return (
            "No direct structured support evidence was attached; keep this as a validation target only because the next "
            f"checks are specific: {', '.join(claim_missing[:3])}."
        )
    return _summary_text(item.get("counter_summary"), counter)


def _claim_missing_items(missing: list[str]) -> list[str]:
    filtered = []
    for value in missing:
        text = str(value or "").strip()
        lowered = text.casefold()
        if not text:
            continue
        if lowered.startswith("metric-") and lowered.endswith(" validation"):
            continue
        if lowered.startswith("further data is needed"):
            continue
        filtered.append(text)
    return filtered or missing


def _summary_text(value: Any, evidence_items: list[dict[str, Any]]) -> str:
    text = str(value or "").strip()
    if text:
        return text
    return "; ".join(
        _unique(str(item.get("summary") or "").strip() for item in evidence_items if item.get("summary"))
    )


def _counter_or_caveat_summary(
    item: dict[str, Any],
    counter: list[dict[str, Any]],
    caveats: list[str],
) -> str:
    del item, counter
    return "; ".join(_unique(caveats[:4]))


def _review_caveats(
    item: dict[str, Any],
    counter: list[dict[str, Any]],
    caveats: list[str],
    *,
    support: list[dict[str, Any]],
    missing: list[str],
) -> list[str]:
    raw_parts = []
    counter_text = _summary_text(item.get("counter_summary"), counter)
    if counter_text:
        raw_parts.append(counter_text)
    raw_parts.extend(caveats)
    filtered = [
        text
        for text in raw_parts
        if not _is_support_like_caveat(text, support=support, counter=counter)
    ]
    zero_bad = _zero_bad_drop(support, counter)
    if zero_bad:
        metric, _, _ = zero_bad
        profile_id = profile_id_for_item(item)
        target_type = _review_target_type(item, support=support, counter=counter)
        checks = _unique(
            str(request.get("description") or request.get("need") or "")
            for request in evidence_requests_for_target_type(target_type, profile_id)
        )
        check_text = ", ".join(checks[:5]) if checks else "process state, dependency status, user-impact signal, freshness signal, and log completeness"
        filtered.append(
            f"{metric}=0 may be real signal loss or a metric/log collection gap; verify with {check_text}."
        )
        filtered.append(
            "Zero error or warning counts do not rule this out; a critical signal can be absent while the process or collector stays silent."
        )
    elif str(item.get("review_mode") or "") == "validation_target":
        zero_good = _zero_good_signal(support, counter)
        if zero_good:
            metric, _, _ = zero_good
            filtered.append(
                f"{metric}=0 is reassuring counter-evidence, but it does not verify RTMPS ingest, YouTube watch status, audio energy, or capture freshness."
            )
        if not support and not counter:
            filtered.append(
                "This is an evidence-gap review target; reject it if the requested source logs do not exist or are outside the incident window."
            )
    missing_text = " ".join(missing).casefold()
    subsystem = str(item.get("subsystem") or "")
    if _review_target_type(item, support=support, counter=counter) in {
        "throughput_disappearance",
        "external_dependency_failure",
        "network_error_signal",
        "freshness_signal_gap",
        "user_impact_signal_gap",
    }:
        filtered.append("External dependency status, user-impact signals, freshness signals, and log completeness are not yet fully verified.")
    elif any(term in missing_text for term in ("freshness", "impact", "completeness")):
        filtered.append("Freshness, user-impact, and log-completeness signals are not yet fully verified for this evidence gap.")
    return _unique(filtered)[:6]


def _is_support_like_caveat(
    text: str,
    *,
    support: list[dict[str, Any]],
    counter: list[dict[str, Any]],
) -> bool:
    value = str(text or "").strip()
    if not value:
        return False
    lowered = value.casefold()
    support_text = " ".join(str(evidence.get("summary") or "") for evidence in support).casefold()
    counter_text = " ".join(str(evidence.get("summary") or "") for evidence in counter).casefold()
    metric_names = [
        metric
        for metric in (_metric_name(str(evidence.get("summary") or "")) for evidence in [*support, *counter])
        if metric
    ]
    if value.casefold() in support_text and value.casefold() not in counter_text:
        return True
    metric_terms = [metric.casefold() for metric in metric_names]
    metric_terms.extend(metric.casefold().replace("_", " ") for metric in metric_names)
    if any(metric in lowered for metric in metric_terms) and any(
        phrase in lowered
        for phrase in (
            "may indicate an issue",
            "indicate an issue",
            "shows a significant drop",
            "significant drop",
            "dropped to",
        )
    ):
        return True
    return False


def _proposal_for(
    item: dict[str, Any],
    missing: list[str],
    *,
    support: list[dict[str, Any]],
    counter: list[dict[str, Any]],
) -> str:
    signature = str(item.get("cluster_signature") or "")
    profile_id = profile_id_for_item(item)
    requests = evidence_requests_for_target_type(signature, profile_id)
    if not requests:
        inferred_type = _review_target_type(item, support=support, counter=counter)
        requests = evidence_requests_for_target_type(inferred_type, profile_id)
    if requests:
        return _prioritized_request_plan(requests)
    action_texts = []
    for action in item.get("suggested_actions") or []:
        text = str(action.get("temporary_action") or action.get("permanent_action") or "").strip()
        if text and "no immediate action" not in text.casefold():
            action_texts.append(text)
    if action_texts:
        return "; ".join(_unique(action_texts))[:1000]
    if missing:
        return "Correlate " + ", ".join(missing[:5]) + "."
    return "Review the linked evidence and decide whether this should become a confirmed candidate."


def _prioritized_request_plan(requests: list[dict[str, Any]]) -> str:
    descriptions = _unique(str(request.get("description") or request.get("need") or "") for request in requests)
    if not descriptions:
        return "Review the linked evidence and decide whether this should become a confirmed candidate."
    lines = ["Recommended next checks:", "P1:"]
    for index, description in enumerate(descriptions[:2], start=1):
        lines.append(f"  {index}. {description}")
    if len(descriptions) > 2:
        lines.append("P2:")
        for offset, description in enumerate(descriptions[2:4], start=3):
            lines.append(f"  {offset}. {description}")
    if len(descriptions) > 4:
        lines.append("P3:")
        lines.append(f"  5. {descriptions[4]}")
    return "\n".join(lines)


def _score_breakdown(
    item: dict[str, Any],
    *,
    members: list[dict[str, Any]],
    all_providers: list[str],
    support: list[dict[str, Any]],
    counter: list[dict[str, Any]],
    missing: list[str],
) -> dict[str, Any]:
    score = round(float(item.get("review_priority_score") or 0.0), 4)
    evidence_strength = _clamp(
        float(item.get("evidence_ref_score") or 0.0)
        or min(1.0, (len(support) + 0.5 * len(counter)) / 3.0)
    )
    actionability = _clamp(
        float(item.get("actionability_score") or 0.0)
        or (0.85 if missing or item.get("suggested_actions") else 0.35)
    )
    user_impact = _user_impact_risk(item, support=support, counter=counter)
    providers = _providers_from_members(members)
    total_provider_count = max(len(all_providers), len(providers), 1)
    detection_agreement = _clamp(len(providers) / total_provider_count if providers else 0.0)
    evidence_diversity = _evidence_diversity(
        members=members,
        support=support,
        counter=counter,
        provider_count=total_provider_count,
    )
    missing_penalty = _clamp(min(0.3, len(missing) * 0.025))
    duplicate_penalty = _clamp(min(0.2, max(0, int(item.get("cluster_size") or 1) - 1) * 0.04))
    raw = dict(item.get("score_breakdown") or {})
    review_history = dict(raw.get("review_history") or {})
    history_adjustment = float(review_history.get("score_delta") or 0.0)
    return {
        "review_priority_score": score,
        "breakdown": {
            "evidence_strength": round(evidence_strength, 4),
            "actionability": round(actionability, 4),
            "user_impact_risk": round(user_impact, 4),
            "model_detection_agreement": round(detection_agreement, 4),
            "evidence_diversity": round(evidence_diversity, 4),
            "model_agreement": round(detection_agreement, 4),
            "history_adjustment": round(history_adjustment, 4),
            "missing_evidence_penalty": round(missing_penalty, 4),
            "duplicate_penalty": round(duplicate_penalty, 4),
        },
        "score_note": "Score is review priority, not truth probability.",
        "score_note_ja": "Score is review priority, not truth probability.",
        "raw_scoring": raw,
    }


def _user_impact_risk(
    item: dict[str, Any],
    *,
    support: list[dict[str, Any]],
    counter: list[dict[str, Any]],
) -> float:
    if _zero_bad_drop(support, counter):
        return 0.9
    target_type = _review_target_type(item, support=support, counter=counter)
    if target_type in {"throughput_disappearance", "external_dependency_failure", "user_impact_signal_gap", "freshness_signal_gap"}:
        return 0.82
    if target_type in {"restart_loop", "state_mismatch", "observability_contract_mismatch"}:
        return 0.72
    priority = str(item.get("priority") or "").casefold()
    return {"high": 0.78, "medium": 0.55, "low": 0.35}.get(priority, 0.5)


def _model_agreement(
    item: dict[str, Any],
    members: list[dict[str, Any]],
    providers: list[str],
    model_names: list[str],
    *,
    all_providers: list[str],
    support: list[dict[str, Any]],
    counter: list[dict[str, Any]],
    missing: list[str],
) -> dict[str, Any]:
    provider_rows: dict[str, dict[str, Any]] = {}
    for member in members:
        for action in member.get("suggested_actions") or []:
            provider = str(action.get("provider") or member.get("model_provider") or "").strip()
            if not provider:
                continue
            row = provider_rows.setdefault(
                provider,
                {
                    "provider": provider,
                    "model_name": str(action.get("model_name") or ""),
                    "stance": "detected",
                    "summary": "",
                    "claim_types": [],
                },
            )
            if action.get("model_name") and not row["model_name"]:
                row["model_name"] = str(action.get("model_name"))
            if action.get("claim_type"):
                row["claim_types"].append(str(action.get("claim_type")))
            summary = str(action.get("temporary_action") or action.get("permanent_action") or "").strip()
            if summary and not row["summary"]:
                row["summary"] = summary
    for provider in providers:
        provider_rows.setdefault(
            provider,
            {
                "provider": provider,
                "model_name": _model_name_for_provider(provider, model_names),
                "stance": "detected",
                "summary": str(item.get("support_summary") or item.get("question") or "")[:400],
                "claim_types": [],
            },
        )
    disagreements = _disagreements(members)
    total_provider_count = max(len(all_providers), len(provider_rows), len(providers), 1)
    detected_provider_count = len(provider_rows)
    detection_agreement = _clamp(detected_provider_count / total_provider_count if detected_provider_count else 0.0)
    evidence_ref_count = len(_evidence_refs_from_members(members, support=support, counter=counter))
    metric_count = len(_metric_names_from_evidence(support, counter))
    evidence_diversity = _evidence_diversity(
        members=members,
        support=support,
        counter=counter,
        provider_count=total_provider_count,
    )
    diversity_unit_count = metric_count or evidence_ref_count
    diversity_unit = "metric" if metric_count else "evidence ref"
    diversity_label = (
        f"{diversity_unit_count} {diversity_unit}{'' if diversity_unit_count == 1 else 's'}"
        if diversity_unit_count
        else "no evidence refs"
    )
    evidence_gap = _evidence_gap_summary(missing)
    return {
        "summary": (
            f"Model detection agreement: {detected_provider_count}/{total_provider_count}; "
            f"Evidence diversity: {diversity_label}."
            if provider_rows
            else "No model agreement metadata recorded."
        ),
        "detected_provider_count": detected_provider_count,
        "total_provider_count": total_provider_count,
        "model_detection_agreement": round(detection_agreement, 4),
        "evidence_diversity": round(evidence_diversity, 4),
        "evidence_diversity_label": diversity_label,
        "evidence_gap": evidence_gap,
        "providers": list(provider_rows.values()),
        "synthesis": "Synthesis merges agreement into a baseline review target and treats disagreement as validation work, not majority vote.",
        "disagreement": disagreements,
    }


def _why_survived(
    item: dict[str, Any],
    *,
    members: list[dict[str, Any]],
    support: list[dict[str, Any]],
    counter: list[dict[str, Any]],
    missing: list[str],
    supporting_providers: list[str],
) -> list[str]:
    reasons: list[str] = []
    zero_bad = _zero_bad_drop(support, counter)
    if zero_bad:
        reasons.append("zero_is_bad metric dropped to zero")
    if str(item.get("review_mode") or "") == "validation_target":
        if _zero_good_signal(support, counter):
            reasons.append("zero_is_good signal is counter-evidence that constrains the incident hypothesis")
        if not support and not counter:
            reasons.append("structured support is missing but the next evidence request is concrete")
        reasons.append("kept as a validation target, not as a confirmed incident")
    if len(supporting_providers) >= 2:
        reasons.append("detected by multiple models")
    subsystem = str(item.get("subsystem") or "")
    if subsystem and subsystem != "general":
        reasons.append(f"linked to {subsystem} subsystem")
    if missing:
        reasons.append("actionable next check exists")
    if counter or item.get("counter_summary"):
        reasons.append("counter evidence or caveat needs human judgment")
    if int(item.get("cluster_size") or len(members)) > 1:
        reasons.append("duplicates were compressed into one cluster")
    if not reasons:
        reasons.append("highest remaining review-priority cluster after monitor-only and duplicate filtering")
    return reasons


def _actions_for(
    item: dict[str, Any],
    missing: list[str],
    *,
    next_evidence_requests: list[dict[str, Any]],
    next_cli_command: str,
) -> dict[str, Any]:
    return {
        "accept": {
            "decision": "accepted",
            "default_label": "confirmed_candidate",
            "effects": [
                "promote to confirmed_candidate",
                "save as known_issue or watchlist when selected",
                "raise priority for future evidence in the same cluster",
            ],
        },
        "reject": {
            "decision": "rejected",
            "reasons": ["false_positive", "low_value", "duplicate", "not_actionable"],
        },
        "more_data": {
            "decision": "needs_more_data",
            "next_data_needed": missing,
            "next_evidence_requests": next_evidence_requests,
            "next_cli_command": next_cli_command,
            "next_query_available": bool(item.get("proposition_id")),
        },
    }


def _next_evidence_requests_for(
    item: dict[str, Any],
    *,
    missing: list[str],
    support: list[dict[str, Any]],
    counter: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    signature = str(item.get("cluster_signature") or "")
    profile_id = profile_id_for_item(item)
    requests = evidence_requests_for_target_type(signature, profile_id)
    if not requests:
        requests = evidence_requests_for_target_type(
            _review_target_type(item, support=support, counter=counter),
            profile_id,
        )
    if requests:
        return requests
    requests = []
    for index, need in enumerate(missing[:5], start=1):
        request_id = (
            need.casefold()
            .replace("/", " ")
            .replace("-", " ")
            .replace("_", " ")
            .split()
        )
        requests.append(
            {
                "request_id": "_".join(request_id[:5]) + "_query" if request_id else f"more_data_{index}_query",
                "need": "_".join(request_id[:4]) if request_id else f"more_data_{index}",
                "description": need,
            }
        )
    return requests


def _next_cli_command(item: dict[str, Any], requests: list[dict[str, Any]]) -> str:
    if not requests:
        return ""
    evidence_sha256 = str(item.get("evidence_sha256") or "")
    subsystem = str(item.get("subsystem") or "general")
    needs = ",".join(_unique(request.get("need") for request in requests if request.get("need")))
    command = f"ops-evidence collect-more --target {subsystem} --need {needs}"
    if evidence_sha256:
        command = f"ops-evidence collect-more --evidence-sha256 {evidence_sha256} --target {subsystem} --need {needs}"
    return command


def _is_transport_review(
    item: dict[str, Any],
    *,
    support: list[dict[str, Any]],
    counter: list[dict[str, Any]],
    missing: list[str],
) -> bool:
    text = " ".join(
        [
            str(item.get("subsystem") or ""),
            str(item.get("cluster_signature") or ""),
            str(item.get("question") or ""),
            str(item.get("support_summary") or ""),
            str(item.get("counter_summary") or ""),
            *(str(evidence.get("summary") or "") for evidence in [*support, *counter]),
            *missing,
        ]
    ).casefold()
    return any(term in text for term in ("stream_transport_count", "rtmps", "ffmpeg", "transport_disappearance"))


def _evidence_diversity(
    *,
    members: list[dict[str, Any]],
    support: list[dict[str, Any]],
    counter: list[dict[str, Any]],
    provider_count: int,
) -> float:
    refs = _evidence_refs_from_members(members, support=support, counter=counter)
    metric_count = len(_metric_names_from_evidence(support, counter))
    diversity_count = metric_count or len(refs)
    if provider_count <= 0:
        return 0.0
    return _clamp(diversity_count / provider_count)


def _evidence_refs_from_members(
    members: list[dict[str, Any]],
    *,
    support: list[dict[str, Any]],
    counter: list[dict[str, Any]],
) -> list[str]:
    refs: list[str] = []
    refs.extend(str(evidence.get("evidence_id") or "") for evidence in [*support, *counter])
    for member in members:
        refs.extend(str(ref) for ref in member.get("evidence_refs") or [])
        for action in member.get("suggested_actions") or []:
            refs.extend(str(ref) for ref in action.get("evidence_refs") or [])
    return _unique(refs)


def _metric_names_from_evidence(
    support: list[dict[str, Any]],
    counter: list[dict[str, Any]],
) -> list[str]:
    return _unique(
        metric
        for metric in (_metric_name(str(evidence.get("summary") or "")) for evidence in [*support, *counter])
        if metric
    )


def _evidence_gap_summary(missing: list[str]) -> str:
    text = " ".join(missing).casefold()
    gaps = []
    if "ffmpeg" in text:
        gaps.append("ffmpeg")
    if "rtmps" in text:
        gaps.append("RTMPS")
    if "youtube" in text or "ingest" in text:
        gaps.append("YouTube ingest")
    if "audio" in text:
        gaps.append("audio_energy")
    if "capture" in text or "freshness" in text:
        gaps.append("capture_freshness")
    return " / ".join(_unique(gaps)) + " not yet verified" if gaps else ""


def _model_outputs_from_members(members: list[dict[str, Any]]) -> list[dict[str, Any]]:
    outputs: dict[str, dict[str, Any]] = {}
    for member in members:
        for action in member.get("suggested_actions") or []:
            provider = str(action.get("provider") or "").strip()
            if not provider:
                continue
            row = outputs.setdefault(
                provider,
                {
                    "provider": provider,
                    "model_name": str(action.get("model_name") or ""),
                    "claim_types": [],
                    "claim_ids": [],
                    "evidence_refs": [],
                    "summary": "",
                },
            )
            if action.get("claim_type"):
                row["claim_types"].append(str(action.get("claim_type")))
            if action.get("claim_id"):
                row["claim_ids"].append(str(action.get("claim_id")))
            row["evidence_refs"].extend(str(ref) for ref in action.get("evidence_refs") or [])
            if not row["summary"]:
                row["summary"] = str(action.get("temporary_action") or action.get("permanent_action") or "")
    for row in outputs.values():
        row["claim_types"] = _unique(row["claim_types"])
        row["claim_ids"] = _unique(row["claim_ids"])
        row["evidence_refs"] = _unique(row["evidence_refs"])
    return list(outputs.values())


def _baseline_window(item: dict[str, Any]) -> dict[str, str]:
    baseline = item.get("baseline_window")
    if isinstance(baseline, dict):
        return {"start": str(baseline.get("start") or ""), "end": str(baseline.get("end") or "")}
    return {"start": "", "end": str(item.get("window_start") or "")}


def _zero_semantics(support: list[dict[str, Any]], counter: list[dict[str, Any]], *, profile_id: str) -> dict[str, Any]:
    metrics = []
    for evidence in [*support, *counter]:
        metric = _metric_name(str(evidence.get("summary") or ""))
        if not metric:
            continue
        semantics = metric_semantics(metric, profile_id)
        zero_behavior = str(semantics.get("zero_behavior") or "unknown")
        if zero_behavior == "suspicious":
            meaning = "zero_is_bad"
        elif zero_behavior == "healthy":
            meaning = "zero_is_good"
        else:
            meaning = zero_behavior
        metrics.append(
            {
                "metric": metric,
                "meaning": meaning,
                "semantic_type": semantics.get("semantic_type"),
                "core_target_type": semantics.get("core_target_type"),
                "baseline_value": evidence.get("baseline_value"),
                "current_value": evidence.get("current_value"),
            }
        )
    return {
        "metrics": metrics,
        "profile_id": profile_id,
        "zero_behavior_values": ["healthy", "suspicious", "neutral", "unknown"],
    }


def _why_unified(item: dict[str, Any], members: list[dict[str, Any]], providers: list[str]) -> list[str]:
    reasons = [
        f"cluster_signature={item.get('cluster_signature') or ''}",
        f"subsystem={item.get('subsystem') or 'general'}",
    ]
    if len(members) > 1:
        reasons.append(f"{len(members)} proposition rows share the same cluster id")
    if providers:
        reasons.append("providers=" + ", ".join(providers))
    return reasons


def _evidence_refs_read(target: dict[str, Any], bundle: dict[str, Any]) -> list[dict[str, Any]]:
    evidence_refs = bundle.get("evidence_refs") if isinstance(bundle.get("evidence_refs"), dict) else {}
    wanted = set()
    drawer = dict(target.get("drawer") or {})
    for key in ("support_evidence", "counter_evidence"):
        for item in drawer.get(key) or []:
            if item.get("evidence_id"):
                wanted.add(str(item["evidence_id"]))
    output = []
    for evidence_id in sorted(wanted):
        details = evidence_refs.get(evidence_id) if isinstance(evidence_refs, dict) else None
        output.append({"evidence_id": evidence_id, "details": details or {}})
    return output


def _claim_ids_from_target(target: dict[str, Any]) -> list[str]:
    claim_ids = []
    drawer = dict(target.get("drawer") or {})
    for key in ("support_evidence", "counter_evidence"):
        for evidence in drawer.get(key) or []:
            if evidence.get("claim_id"):
                claim_ids.append(str(evidence["claim_id"]))
    for output in drawer.get("model_outputs") or []:
        claim_ids.extend(str(claim_id) for claim_id in output.get("claim_ids") or [])
    return _unique(claim_ids)


def _claim_to_json(claim: Any) -> dict[str, Any]:
    return {
        "claim_id": str(getattr(claim, "claim_id", "")),
        "provider": str(getattr(claim, "provider", "")),
        "claim_type": str(getattr(claim, "claim_type", "")),
        "claim_text": str(getattr(claim, "claim_text", "")),
        "evidence_refs": list(getattr(claim, "evidence_refs", ()) or ()),
        "counter_evidence_refs": list(getattr(claim, "counter_evidence_refs", ()) or ()),
        "caveats": list(getattr(claim, "caveats", ()) or ()),
        "missing_evidence": list(getattr(claim, "missing_evidence", ()) or ()),
        "temporary_action": str(getattr(claim, "temporary_action", "")),
        "permanent_action": str(getattr(claim, "permanent_action", "")),
        "required_authority": str(getattr(claim, "required_authority", "")),
        "evidence_refs_valid": bool(getattr(claim, "evidence_refs_valid", False)),
    }


def _required_metrics_for(subsystem: str, missing: list[str], drawer: dict[str, Any]) -> list[str]:
    metrics = []
    request_types = {
        str(request.get("request_type") or request.get("need") or "")
        for request in drawer.get("next_evidence_requests") or []
        if isinstance(request, dict)
    }
    if "throughput_signal" in request_types:
        metrics.append("throughput_count")
    if "process_state" in request_types:
        metrics.append("process_state")
    if "external_dependency_status" in request_types:
        metrics.append("external_dependency_status")
    if "user_impact_signal" in request_types:
        metrics.append("user_impact_signal")
    if "freshness_signal" in request_types:
        metrics.append("freshness_signal")
    if "state_transition" in request_types:
        metrics.append("state_transition")
    for zero_item in (drawer.get("zero_semantics") or {}).get("metrics") or []:
        if zero_item.get("metric"):
            metrics.append(str(zero_item["metric"]))
    return _unique(metrics)


def _required_log_sources_for(subsystem: str, missing: list[str]) -> list[str]:
    del subsystem
    text = " ".join(missing).casefold()
    sources = []
    if "process" in text:
        sources.extend(["process logs", "supervisor state", "runtime metrics"])
    if "throughput" in text or "traffic" in text or "output" in text:
        sources.extend(["throughput metrics", "traffic logs", "output counters"])
    if "external" in text or "dependency" in text:
        sources.extend(["dependency status logs", "upstream API logs"])
    if "impact" in text or "success" in text or "quality" in text:
        sources.extend(["user-impact metrics", "success-rate metrics"])
    if "freshness" in text or "timestamp" in text:
        sources.extend(["freshness metrics", "collector logs"])
    if "state" in text:
        sources.extend(["state transition logs", "health snapshots"])
    return _unique(sources or ["logs_sanitized"])


def _zero_bad_drop(
    support: list[dict[str, Any]],
    counter: list[dict[str, Any]],
) -> tuple[str, float, float] | None:
    for evidence in [*support, *counter]:
        metric = _metric_name(str(evidence.get("summary") or ""))
        if metric not in ZERO_IS_BAD:
            continue
        baseline = _float(evidence.get("baseline_value"))
        current = _float(evidence.get("current_value"))
        if baseline is not None and baseline > 0 and current == 0:
            return metric, baseline, current
    return None


def _zero_good_signal(
    support: list[dict[str, Any]],
    counter: list[dict[str, Any]],
) -> tuple[str, float, float] | None:
    for evidence in [*support, *counter]:
        metric = _metric_name(str(evidence.get("summary") or ""))
        if metric not in ZERO_IS_GOOD:
            continue
        current = _float(evidence.get("current_value"))
        baseline = _float(evidence.get("baseline_value"))
        if current == 0:
            return metric, baseline if baseline is not None else 0.0, current
    return None


def _metric_name(summary: str) -> str:
    return summary.split("=", 1)[0].strip() if "=" in summary else ""


def _item_text(
    item: dict[str, Any],
    support: list[dict[str, Any]],
    counter: list[dict[str, Any]],
) -> str:
    parts = [
        str(item.get("question") or ""),
        str(item.get("support_summary") or ""),
        str(item.get("counter_summary") or ""),
        str(item.get("cluster_signature") or ""),
    ]
    parts.extend(str(evidence.get("summary") or "") for evidence in [*support, *counter])
    return " ".join(parts).casefold()


def _providers_from_members(members: list[dict[str, Any]]) -> list[str]:
    return _unique(
        str(provider)
        for member in members
        for provider in (member.get("model_providers") or _split_csv(member.get("model_provider")))
        if provider
    )


def _disagreements(members: list[dict[str, Any]]) -> list[str]:
    by_provider: dict[str, set[str]] = defaultdict(set)
    for member in members:
        provider_values = member.get("model_providers") or _split_csv(member.get("model_provider")) or [""]
        signature = str(member.get("cluster_signature") or "")
        summary = str(member.get("support_summary") or member.get("counter_summary") or "")
        for provider in provider_values:
            if provider:
                by_provider[str(provider)].add(signature or summary[:80])
    if len(by_provider) <= 1:
        return []
    signatures = Counter(sig for values in by_provider.values() for sig in values if sig)
    if len(signatures) <= 1:
        return []
    return [
        f"{provider} emphasized {', '.join(sorted(values))}"
        for provider, values in sorted(by_provider.items())
        if values
    ]


def _model_name_for_provider(provider: str, model_names: list[str]) -> str:
    if len(model_names) == 1:
        return model_names[0]
    provider_key = provider.casefold()
    for model_name in model_names:
        if provider_key.split("-", 1)[0] in model_name.casefold():
            return model_name
    return ""


def _compact_hidden_item(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "proposition_id": item.get("proposition_id"),
        "cluster_id": item.get("cluster_id"),
        "title": _title_for(
            item,
            support=list((item.get("structured_evidence") or {}).get("support") or []),
            counter=list((item.get("structured_evidence") or {}).get("counter_evidence") or []),
        ),
        "subsystem": item.get("subsystem"),
        "review_visibility": item.get("review_visibility"),
        "hidden_reason": item.get("hidden_reason"),
        "finding_status_counts": item.get("finding_status_counts") or {},
        "identity_unknown_keys": item.get("identity_unknown_keys") or [],
        "insufficient_evidence_count": item.get("insufficient_evidence_count") or 0,
        "review_priority_score": item.get("review_priority_score"),
    }


def _priority_label(score: float) -> str:
    if score >= 0.78:
        return "high"
    if score >= 0.55:
        return "medium"
    return "low"


def _split_csv(value: Any) -> list[str]:
    return [item.strip() for item in str(value or "").split(",") if item.strip()]


def _float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, value))


def _unique(values: Any) -> list[str]:
    output: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if text and text not in output:
            output.append(text)
    return output


def _drop_empty(row: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in row.items() if value not in (None, "", [], {})}
