from __future__ import annotations

from statistics import mean
from typing import Any


PROFILE_CONFIDENCE_THRESHOLDS = {
    "use_for_subsystem_routing": 0.75,
    "candidate_only_human_gate": 0.60,
}


def build_profile_context_summary(
    *,
    profile_id: str,
    profile_draft: dict[str, Any],
    approved_profile: dict[str, Any],
    source_context_sha: str = "",
    source_analysis_sha: str = "",
    review_targets: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    draft_profile = _as_dict(profile_draft.get("profile"))
    approved_system_profile = _as_dict(approved_profile.get("system_profile"))
    approved_id = str(approved_profile.get("profile_id") or "")
    effective_profile_id = profile_id or approved_id or str(draft_profile.get("profile_id") or "")
    component_map = _as_dict(approved_profile.get("component_map")) or _as_dict(draft_profile.get("component_map"))
    draft_components = _as_list(draft_profile.get("components"))
    metric_semantics = approved_profile.get("metric_semantics") or draft_profile.get("metric_semantics") or {}
    collector_mappings = approved_profile.get("collector_mappings") or draft_profile.get("collector_mappings") or {}
    generation = _as_dict(profile_draft.get("profile_generation"))
    confidence_summary = _confidence_summary(profile_draft, approved_profile, component_map, metric_semantics)
    overall_confidence = _float(confidence_summary.get("overall_confidence"))
    confidence_action = profile_confidence_action(overall_confidence)
    human_questions = profile_human_questions(profile_draft, approved_profile)
    required_decisions = _required_human_decisions(profile_draft)
    confirmed_outcomes = confirmed_user_outcomes(approved_profile, profile_draft)
    provisional_outcomes = provisional_user_outcomes(approved_profile, profile_draft)
    approval = _as_dict(approved_profile.get("profile_discovery_approval"))
    has_context = bool(profile_draft or approved_profile or effective_profile_id or source_context_sha or source_analysis_sha)
    llm_status = str(generation.get("llm_status") or profile_draft.get("llm_status") or ("persisted" if has_context else "not_run"))
    context = {
        "schema_version": "profile_context_summary.v2",
        "profile_id": effective_profile_id,
        "profile_status": _profile_status(
            has_context=has_context,
            approved=bool(approved_profile or approval.get("approved") is True),
            explicit=bool(
                approved_profile
                or effective_profile_id
                or approval.get("explicit_profile") is True
                or profile_draft.get("explicit_profile") is True
            ),
        ),
        "generation_mode": (
            "profile_draft_and_approved_profile"
            if profile_draft and approved_profile
            else "approved_profile_context"
            if approved_profile or effective_profile_id
            else "sanitized_source_context"
            if has_context
            else "not_run"
        ),
        "llm_status": llm_status,
        "approved": bool(approved_profile or approval.get("approved") is True or effective_profile_id),
        "explicit_profile": bool(
            approved_profile
            or effective_profile_id
            or approval.get("explicit_profile") is True
            or profile_draft.get("explicit_profile") is True
        ),
        "draft_schema_version": str(profile_draft.get("schema_version") or ""),
        "source_discovery_sha256": str(
            profile_draft.get("source_discovery_sha256")
            or generation.get("source_discovery_sha256")
            or approval.get("source_discovery_sha256")
            or ""
        ),
        "source_context_sha256": source_context_sha,
        "source_analysis_sha256": source_analysis_sha,
        "system_type": str(
            approved_profile.get("system_type")
            or approved_system_profile.get("system_type")
            or draft_profile.get("system_type")
            or ""
        ),
        "purpose": str(
            approved_profile.get("purpose")
            or approved_system_profile.get("purpose")
            or draft_profile.get("purpose")
            or ""
        ),
        "component_count": len(component_map) if component_map else len(draft_components),
        "metric_semantics_count": len(metric_semantics) if isinstance(metric_semantics, dict | list) else 0,
        "collector_mapping_count": len(collector_mappings) if isinstance(collector_mappings, dict | list) else 0,
        "confidence_summary": confidence_summary,
        "confidence_action": confidence_action,
        "confidence_thresholds": dict(PROFILE_CONFIDENCE_THRESHOLDS),
        "confirmed_user_outcomes": confirmed_outcomes,
        "provisional_user_outcomes": provisional_outcomes,
        "human_questions": human_questions[:8],
        "required_human_decisions": required_decisions[:8],
        "profile_review_policy": {
            "context_is_not_incident_evidence": True,
            "confirmed_outcomes_required_for_promotion": True,
            "provisional_outcomes_create_missing_evidence": True,
            "low_confidence_fields_require_human_review": True,
            "runtime_support_must_cite_evidence_id": True,
        },
        "context_is_not_incident_evidence": True,
        "summary": (
            "Profile context was generated or approved from sanitized discovery; it constrains routing "
            "and questions, but runtime claims still require Evidence Item IDs and human-approved user outcomes."
            if has_context
            else "No profile context was recorded for this payload."
        ),
    }
    links = profile_to_review_links(context, review_targets or [])
    if links:
        context["profile_to_review_links"] = links
    return context


def build_focused_profile_context_summary(
    *,
    profile_id: str,
    focused_profile: dict[str, Any],
    review_targets: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Summarize an existing focused operational profile for the human gate."""
    profile = _as_dict(focused_profile)
    generation = _as_dict(profile.get("focused_profile_generation"))
    draft = {
        "schema_version": str(profile.get("schema_version") or ""),
        "profile_generation": generation,
        "source_discovery_sha256": str(
            profile.get("source_discovery_sha256") or generation.get("source_discovery_sha256") or ""
        ),
        "required_profile_questions": _to_text_list(profile.get("human_review_required")),
        "required_human_decisions": _to_text_list(profile.get("human_review_required")),
    }
    approved = focused_profile_to_approved_profile(
        profile_id=profile_id,
        focused_profile=profile,
    )
    return build_profile_context_summary(
        profile_id=profile_id,
        profile_draft=draft,
        approved_profile=approved,
        source_context_sha=str(profile.get("source_context_sha256") or generation.get("source_context_sha256") or ""),
        source_analysis_sha=str(profile.get("source_analysis_sha256") or generation.get("source_analysis_sha256") or ""),
        review_targets=review_targets,
    )


def focused_profile_to_approved_profile(
    *,
    profile_id: str,
    focused_profile: dict[str, Any],
) -> dict[str, Any]:
    profile = _as_dict(focused_profile)
    summary = _as_dict(profile.get("system_summary"))
    generation = _as_dict(profile.get("focused_profile_generation"))
    system_profile = {
        "system_type": str(summary.get("system_type") or ""),
        "purpose": str(summary.get("primary_purpose") or ""),
        "logged_subject": str(summary.get("logged_subject") or ""),
        "operational_boundary": str(summary.get("operational_boundary") or ""),
        "confidence": _float(summary.get("confidence")),
        "profile_source": "focused_operational_profile",
    }
    component_map = {
        str(row.get("component_id") or row.get("name") or f"component_{index:03d}"): {
            "name": str(row.get("name") or ""),
            "role": str(row.get("role") or ""),
            "confidence": _float(row.get("confidence")),
            "source_context_refs": _to_text_list(row.get("source_context_refs")),
            "evidence_refs": _to_text_list(row.get("evidence_refs")),
        }
        for index, row in enumerate(_as_dict_list(profile.get("runtime_components")), start=1)
    }
    observability = _as_dict(profile.get("observability_contract"))
    metric_semantics = {
        str(row.get("metric_name") or f"metric_{index:03d}"): {
            "meaning": str(row.get("meaning") or ""),
            "healthy_direction": str(row.get("healthy_direction") or "unknown"),
            "confidence": _profile_row_confidence(row, default=_float(summary.get("confidence"))),
            "source_context_refs": _to_text_list(row.get("source_context_refs")),
            "evidence_refs": _to_text_list(row.get("evidence_refs")),
        }
        for index, row in enumerate(_as_dict_list(observability.get("metrics")), start=1)
        if str(row.get("metric_name") or "").strip()
    }
    collector_mappings: dict[str, dict[str, Any]] = {}
    for index, row in enumerate(_as_dict_list(profile.get("read_only_collectors")), start=1):
        name = str(row.get("collector") or f"collector_{index:03d}")
        collector_mappings[name] = {
            "purpose": str(row.get("purpose") or ""),
            "safety_level": str(row.get("safety_level") or "read_only"),
            "confidence": _profile_row_confidence(row, default=_float(summary.get("confidence"))),
        }
    for index, row in enumerate(_as_dict_list(observability.get("logs")), start=1):
        name = str(row.get("source") or f"log_source_{index:03d}")
        collector_mappings.setdefault(
            name,
            {
                "purpose": str(row.get("meaning") or ""),
                "safety_level": "read_only",
                "confidence": _profile_row_confidence(row, default=_float(summary.get("confidence"))),
            },
        )
    confidence_summary = _focused_confidence_summary(
        summary=summary,
        component_map=component_map,
        metric_semantics=metric_semantics,
        collector_mappings=collector_mappings,
    )
    return {
        "profile_id": profile_id or str(profile.get("system_label") or ""),
        "profile_discovery_approval": {
            "approved": True,
            "explicit_profile": True,
            "source_discovery_sha256": str(
                profile.get("source_discovery_sha256") or generation.get("source_discovery_sha256") or ""
            ),
        },
        "system_profile": system_profile,
        "component_map": component_map,
        "metric_semantics": metric_semantics,
        "collector_mappings": collector_mappings,
        "confidence_summary": confidence_summary,
        "human_questions": _to_text_list(profile.get("human_review_required")),
        "required_profile_questions": _to_text_list(profile.get("human_review_required")),
        "action_constraints": _to_text_list(_as_dict(profile.get("profile_limits")).get("notes")),
    }


def build_approved_profile_model_context(profile: dict[str, Any]) -> dict[str, Any]:
    if not profile:
        return {
            "explicit_profile": False,
            "context_is_not_evidence": True,
            "profile_status": "not_run",
            "confidence_action": "not_available",
            "profile_review_policy": {
                "context_is_not_incident_evidence": True,
                "runtime_support_must_cite_evidence_id": True,
            },
        }
    summary = build_profile_context_summary(
        profile_id=str(profile.get("profile_id") or ""),
        profile_draft={},
        approved_profile=profile,
    )
    return {
        "profile_id": str(summary.get("profile_id") or ""),
        "explicit_profile": bool(summary.get("explicit_profile")),
        "profile_status": str(summary.get("profile_status") or ""),
        "context_is_not_evidence": True,
        "require_evidence_id_for_support": True,
        "system_profile": _as_dict(profile.get("system_profile")),
        "metric_semantics": _bounded_mapping(_as_dict(profile.get("metric_semantics")), limit=80),
        "component_map": _bounded_mapping(_as_dict(profile.get("component_map")), limit=80),
        "collector_mappings": _as_dict(profile.get("collector_mappings")),
        "action_constraints": _to_text_list(profile.get("action_constraints")),
        "confidence_summary": summary.get("confidence_summary") or {},
        "confidence_action": summary.get("confidence_action") or "not_available",
        "confidence_thresholds": dict(PROFILE_CONFIDENCE_THRESHOLDS),
        "confirmed_user_outcomes": list(summary.get("confirmed_user_outcomes") or []),
        "provisional_user_outcomes": list(summary.get("provisional_user_outcomes") or []),
        "human_questions": list(summary.get("human_questions") or []),
        "profile_review_policy": summary.get("profile_review_policy") or {},
    }


def profile_confidence_action(overall_confidence: float | None) -> str:
    if overall_confidence is None:
        return "not_available"
    if overall_confidence >= PROFILE_CONFIDENCE_THRESHOLDS["use_for_subsystem_routing"]:
        return "use_for_subsystem_routing_human_gated"
    if overall_confidence >= PROFILE_CONFIDENCE_THRESHOLDS["candidate_only_human_gate"]:
        return "candidate_only_requires_profile_review"
    return "discovery_required_before_routing"


def profile_human_questions(profile_draft: dict[str, Any], approved_profile: dict[str, Any]) -> list[str]:
    questions: list[str] = []
    questions.extend(_to_text_list(profile_draft.get("required_profile_questions")))
    questions.extend(
        item
        for item in _to_text_list(profile_draft.get("required_human_decisions"))
        if _looks_like_question(item)
    )
    questions.extend(_to_text_list(approved_profile.get("human_questions")))
    questions.extend(_to_text_list(approved_profile.get("required_profile_questions")))
    if not any("critical user outcome" in item.lower() for item in questions):
        questions.append("What is the critical user outcome?")
    if not any("zero-is-good" in item.lower() or "zero-is-bad" in item.lower() for item in questions):
        questions.append("Which metrics are zero-is-good or zero-is-bad?")
    if not any("user impact" in item.lower() for item in questions):
        questions.append("Which logs indicate user impact rather than diagnostic noise?")
    return _unique(questions)


def confirmed_user_outcomes(approved_profile: dict[str, Any], profile_draft: dict[str, Any]) -> list[str]:
    outcomes: list[str] = []
    approved_system_profile = _as_dict(approved_profile.get("system_profile"))
    outcomes.extend(_to_text_list(approved_profile.get("confirmed_user_outcomes")))
    outcomes.extend(_to_text_list(approved_system_profile.get("confirmed_user_outcomes")))
    draft_profile = _as_dict(profile_draft.get("profile"))
    outcomes.extend(_to_text_list(draft_profile.get("confirmed_user_outcomes")))
    return _unique(outcomes)


def provisional_user_outcomes(approved_profile: dict[str, Any], profile_draft: dict[str, Any]) -> list[str]:
    outcomes: list[str] = []
    approved_system_profile = _as_dict(approved_profile.get("system_profile"))
    draft_profile = _as_dict(profile_draft.get("profile"))
    for source in (approved_profile, approved_system_profile, draft_profile):
        outcomes.extend(_to_text_list(source.get("provisional_user_outcomes")))
        outcomes.extend(_to_text_list(source.get("critical_user_outcomes")))
        outcomes.extend(_to_text_list(source.get("critical_outcomes")))
    for assumption in _to_text_list(profile_draft.get("assumptions")):
        text = assumption.strip().rstrip(".")
        lower = text.lower()
        if "critical user outcome" not in lower and "user outcome" not in lower:
            continue
        if "not inferred" in lower or "not proven" in lower or "require" in lower:
            continue
        normalized = text
        for prefix in (
            "Critical user outcomes are plausible but not proven",
            "Critical user outcomes are assumed and require human validation",
        ):
            if normalized == prefix:
                normalized = ""
        if normalized.endswith(" is a critical user outcome"):
            normalized = normalized[: -len(" is a critical user outcome")]
        if normalized:
            outcomes.append(normalized)
    confirmed = set(confirmed_user_outcomes(approved_profile, profile_draft))
    return _unique(item for item in outcomes if item not in confirmed)


def profile_to_review_links(
    profile_context: dict[str, Any],
    review_targets: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    links: list[dict[str, Any]] = []
    questions = _to_text_list(profile_context.get("human_questions"))
    target_units = [_target_unit(row) for row in review_targets if isinstance(row, dict)]
    target_units = [unit for unit in target_units if unit]
    for question in questions:
        lower = question.lower()
        if "zero-is-good" in lower or "zero-is-bad" in lower:
            units = [
                _target_unit(row)
                for row in review_targets
                if isinstance(row, dict) and _target_mentions_zero_semantics(row)
            ]
            units = _unique(unit for unit in units if unit)
            if units:
                links.append(
                    {
                        "question": question,
                        "review_units": units[:6],
                        "reason": (
                            "Zero-count metrics can mean healthy idle, disabled routing, or broken processing; "
                            "linked review targets keep that ambiguity human-gated."
                        ),
                    }
                )
        elif "user impact" in lower or "critical user outcome" in lower:
            units = [
                _target_unit(row)
                for row in review_targets
                if isinstance(row, dict)
                and (
                    "user_impact_unverified" in str(row.get("promotion") or "")
                    or "User impact or operational outcome evidence" in str(row.get("missing_evidence") or "")
                )
            ]
            units = _unique(unit for unit in units if unit) or target_units[:6]
            if units:
                links.append(
                    {
                        "question": question,
                        "review_units": units[:6],
                        "reason": (
                            "Targets blocked on user impact cannot be promoted until the profile question "
                            "is answered with operational outcome evidence."
                        ),
                    }
                )
    return links[:6]


def _confidence_summary(
    profile_draft: dict[str, Any],
    approved_profile: dict[str, Any],
    component_map: dict[str, Any],
    metric_semantics: object,
) -> dict[str, Any]:
    summary = _as_dict(profile_draft.get("confidence_summary")) or _as_dict(approved_profile.get("confidence_summary"))
    if summary:
        return {
            "overall_confidence": _float(summary.get("overall_confidence")),
            "component_mapping_confidence": _float(summary.get("component_mapping_confidence")),
            "metric_semantics_confidence": _float(summary.get("metric_semantics_confidence")),
            "collector_mapping_confidence": _float(summary.get("collector_mapping_confidence")),
        }
    component_scores = [
        score
        for score in (_float(_as_dict(value).get("confidence")) for value in component_map.values())
        if score is not None
    ]
    metric_scores: list[float] = []
    if isinstance(metric_semantics, dict):
        metric_scores = [
            score
            for score in (_float(_as_dict(value).get("confidence")) for value in metric_semantics.values())
            if score is not None
        ]
    component_confidence = round(mean(component_scores), 3) if component_scores else None
    metric_confidence = round(mean(metric_scores), 3) if metric_scores else None
    scores = [score for score in (component_confidence, metric_confidence) if score is not None]
    overall = round(mean(scores), 3) if scores else None
    return {
        "overall_confidence": overall,
        "component_mapping_confidence": component_confidence,
        "metric_semantics_confidence": metric_confidence,
        "collector_mapping_confidence": None,
    }


def _required_human_decisions(profile_draft: dict[str, Any]) -> list[str]:
    decisions = _to_text_list(profile_draft.get("required_human_decisions"))
    if decisions:
        return _unique(decisions)
    return [
        "Approve profile context before treating it as an explicit operational profile.",
        "Keep source context separate from runtime evidence.",
    ]


def _profile_status(*, has_context: bool, approved: bool, explicit: bool) -> str:
    if approved and explicit:
        return "approved_context_human_gated_outcomes"
    if explicit:
        return "explicit_context_pending_approval"
    if has_context:
        return "sanitized_context_pending_profile_review"
    return "not_run"


def _target_unit(target: dict[str, Any]) -> str:
    return str(
        target.get("canonical_review_unit")
        or target.get("normalized_review_unit")
        or target.get("review_unit")
        or target.get("subsystem")
        or target.get("title")
        or ""
    )


def _target_mentions_zero_semantics(target: dict[str, Any]) -> bool:
    unit = _target_unit(target).lower()
    if unit in {"service_health", "background_processing"}:
        return True
    text = " ".join(
        [
            str(target.get("suspected_issue") or ""),
            str(target.get("claim") or ""),
            str(target.get("evidence_summary") or ""),
            str(target.get("target_explanation") or ""),
            str(target.get("next_validation_question") or ""),
        ]
    ).lower()
    return any(marker in text for marker in ("=0", "zero", "processed=0", "notified=0", "matched=0"))


def _bounded_mapping(value: dict[str, Any], *, limit: int) -> dict[str, Any]:
    if not value:
        return {}
    return {str(key): item for key, item in list(value.items())[:limit]}


def _looks_like_question(value: str) -> bool:
    text = value.strip()
    return text.endswith("?") or text.lower().startswith(("what ", "which ", "are ", "does ", "do ", "confirm "))


def _as_dict(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _as_list(value: object) -> list[Any]:
    return value if isinstance(value, list) else []


def _as_dict_list(value: object) -> list[dict[str, Any]]:
    return [item for item in _as_list(value) if isinstance(item, dict)]


def _to_text_list(value: object) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, tuple):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


def _unique(values: Any) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        text = str(value).strip()
        if not text or text in seen:
            continue
        seen.add(text)
        out.append(text)
    return out


def _float(value: object) -> float | None:
    try:
        if value in (None, ""):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _profile_row_confidence(row: dict[str, Any], *, default: float | None) -> float | None:
    return _float(row.get("confidence")) if _float(row.get("confidence")) is not None else default


def _focused_confidence_summary(
    *,
    summary: dict[str, Any],
    component_map: dict[str, Any],
    metric_semantics: dict[str, Any],
    collector_mappings: dict[str, Any],
) -> dict[str, Any]:
    component_scores = [
        score
        for score in (_float(_as_dict(value).get("confidence")) for value in component_map.values())
        if score is not None
    ]
    metric_scores = [
        score
        for score in (_float(_as_dict(value).get("confidence")) for value in metric_semantics.values())
        if score is not None
    ]
    collector_scores = [
        score
        for score in (_float(_as_dict(value).get("confidence")) for value in collector_mappings.values())
        if score is not None
    ]
    component_confidence = round(mean(component_scores), 3) if component_scores else _float(summary.get("confidence"))
    metric_confidence = round(mean(metric_scores), 3) if metric_scores else None
    collector_confidence = round(mean(collector_scores), 3) if collector_scores else None
    scores = [score for score in (component_confidence, metric_confidence, collector_confidence) if score is not None]
    return {
        "overall_confidence": round(mean(scores), 3) if scores else _float(summary.get("confidence")),
        "component_mapping_confidence": component_confidence,
        "metric_semantics_confidence": metric_confidence,
        "collector_mapping_confidence": collector_confidence,
    }
