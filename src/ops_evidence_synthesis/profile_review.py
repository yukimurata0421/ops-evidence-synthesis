from __future__ import annotations

import json
from copy import deepcopy
from typing import Any

from ops_evidence_synthesis.ai.base import ModelProvider
from ops_evidence_synthesis.ai.runtime import run_provider_with_retries, safety_preflight_for_model_input
from ops_evidence_synthesis.canonical import sha256_json, sha256_text
from ops_evidence_synthesis.local_first import scan_sanitized_text
from ops_evidence_synthesis.profile_gate import focused_profile_to_approved_profile
from ops_evidence_synthesis.timeutils import utc_now


PROFILE_REVIEW_PATCH_SCHEMA_VERSION = "operational_profile_review_patch.v1"
APPROVED_OPERATIONAL_PROFILE_SCHEMA_VERSION = "approved_operational_profile.v1"

HEALTHY_DIRECTIONS = {"increase", "decrease", "stable", "nonzero", "zero", "unknown"}
METRIC_BEHAVIORS = {"healthy", "suspicious", "neutral", "unknown"}

_PATCH_KEYS = {
    "schema_version",
    "system_summary_overrides",
    "metric_semantics_overrides",
    "component_role_overrides",
    "log_source_overrides",
    "confirmed_user_outcomes",
    "ignored_component_ids",
    "approved_collectors",
    "unresolved_questions",
}
_SYSTEM_SUMMARY_KEYS = {"primary_purpose", "logged_subject", "operational_boundary"}
_METRIC_KEYS = {
    "metric_name",
    "meaning",
    "healthy_direction",
    "zero_behavior",
    "increase_behavior",
    "decrease_behavior",
    "reason",
    "provenance",
}
_COMPONENT_KEYS = {"component_id", "role", "reason", "provenance"}
_LOG_KEYS = {"source", "meaning", "reason", "provenance"}
_QUESTION_KEYS = {"question", "reason"}


class ProfileReviewError(ValueError):
    pass


def build_profile_review_model_input(
    focused_profile: dict[str, Any],
    human_review: dict[str, Any],
) -> dict[str, Any]:
    _require_focused_profile(focused_profile)
    _require_human_review(human_review, require_approval=False)
    return {
        "schema_version": "profile_review_normalization_input.v1",
        "llm_task": "profile_review_normalization",
        "focused_profile": _focused_profile_for_normalization(focused_profile),
        "human_review": _human_review_for_normalization(human_review),
        "normalization_policy": {
            "candidate_patch_only": True,
            "human_final_approval_required": True,
            "unknown_when_ambiguous": True,
            "existing_identifiers_only": True,
            "source_context_is_incident_evidence": False,
            "raw_source_sent_to_provider": False,
            "raw_logs_sent_to_provider": False,
            "allowed_patch_keys": sorted(_PATCH_KEYS),
            "healthy_direction_values": sorted(HEALTHY_DIRECTIONS),
            "metric_behavior_values": sorted(METRIC_BEHAVIORS),
        },
    }


def normalize_profile_review_with_provider(
    focused_profile: dict[str, Any],
    human_review: dict[str, Any],
    provider: ModelProvider,
) -> dict[str, Any]:
    model_input = build_profile_review_model_input(focused_profile, human_review)
    preflight = safety_preflight_for_model_input(model_input, filename="profile_review_normalization_input.json")
    if not preflight.passed:
        raise ProfileReviewError("profile review normalization input failed safety preflight")

    run = run_provider_with_retries(provider, model_input)
    if run.response.status != "ok":
        raise ProfileReviewError(f"profile review normalization failed: {run.failure_reason or run.response.status}")

    from ops_evidence_synthesis.synthesis.output_ingest import parse_model_output

    parsed = parse_model_output(run.response.raw_output)
    if parsed.parsed is None:
        raise ProfileReviewError("profile review normalization returned invalid JSON")
    patch = normalize_profile_review_patch(parsed.parsed)
    errors = validate_profile_review_patch(patch, focused_profile)
    if errors:
        raise ProfileReviewError("profile review patch validation failed: " + "; ".join(errors))

    output_scan = scan_sanitized_text(
        "profile_review_patch.json",
        json.dumps(patch, ensure_ascii=False, sort_keys=True),
    )
    if output_scan.get("findings"):
        raise ProfileReviewError("profile review patch failed safety validation")

    metadata = {
        "schema_version": "profile_review_normalization.v1",
        "provider_id": str(getattr(provider, "provider", "")),
        "model_name": str(getattr(provider, "model_name", "")),
        "prompt_name": str(getattr(provider, "prompt_name", "")),
        "model_input_sha256": sha256_json(model_input),
        "raw_output_sha256": sha256_text(run.response.raw_output),
        "parsed_output_sha256": sha256_json(parsed.parsed),
        "accepted_candidate_patch_sha256": sha256_json(patch),
        "parse_status": parsed.parse_status,
        "repair_applied": parsed.repair_applied,
        "repair_rules": list(parsed.repair_rules),
        **run.retry_metadata(),
    }
    return {
        "status": "candidate_patch_ready",
        "patch": patch,
        "normalization": metadata,
        "validation": {"passed": True, "errors": []},
        "change_summary": profile_review_change_summary(patch),
    }


def deterministic_profile_review_patch(
    focused_profile: dict[str, Any],
    human_review: dict[str, Any],
) -> dict[str, Any]:
    """Fail-safe local fallback: preserve answers as unresolved instead of inventing semantics."""
    _require_focused_profile(focused_profile)
    _require_human_review(human_review, require_approval=False)
    unresolved = []
    for row in human_review.get("answers") or []:
        if not isinstance(row, dict):
            continue
        question = _text(row.get("question"), 500)
        answer = _text(row.get("answer"), 2000)
        if question and answer:
            unresolved.append({"question": question, "reason": f"Human answer requires structured review: {answer}"})
    return normalize_profile_review_patch(
        {
            "schema_version": PROFILE_REVIEW_PATCH_SCHEMA_VERSION,
            "system_summary_overrides": {},
            "metric_semantics_overrides": [],
            "component_role_overrides": [],
            "log_source_overrides": [],
            "confirmed_user_outcomes": [],
            "ignored_component_ids": [],
            "approved_collectors": [],
            "unresolved_questions": unresolved,
        }
    )


def normalize_profile_review_patch(value: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ProfileReviewError("profile review patch must be an object")
    unexpected = sorted(set(value) - _PATCH_KEYS)
    if unexpected:
        raise ProfileReviewError("profile review patch contains unsupported fields: " + ", ".join(unexpected))

    summary = value.get("system_summary_overrides") if isinstance(value.get("system_summary_overrides"), dict) else {}
    summary_unexpected = sorted(set(summary) - _SYSTEM_SUMMARY_KEYS)
    if summary_unexpected:
        raise ProfileReviewError("system summary patch contains unsupported fields: " + ", ".join(summary_unexpected))

    return {
        "schema_version": PROFILE_REVIEW_PATCH_SCHEMA_VERSION,
        "system_summary_overrides": {
            key: _text(summary.get(key), 2000)
            for key in sorted(_SYSTEM_SUMMARY_KEYS)
            if _text(summary.get(key), 2000)
        },
        "metric_semantics_overrides": [
            _normalize_row(row, _METRIC_KEYS, "metric semantics")
            for row in _dict_rows(value.get("metric_semantics_overrides"), limit=80)
        ],
        "component_role_overrides": [
            _normalize_row(row, _COMPONENT_KEYS, "component role")
            for row in _dict_rows(value.get("component_role_overrides"), limit=80)
        ],
        "log_source_overrides": [
            _normalize_row(row, _LOG_KEYS, "log source")
            for row in _dict_rows(value.get("log_source_overrides"), limit=80)
        ],
        "confirmed_user_outcomes": _strings(value.get("confirmed_user_outcomes"), limit=30, max_chars=1000),
        "ignored_component_ids": _strings(value.get("ignored_component_ids"), limit=80, max_chars=300),
        "approved_collectors": _strings(value.get("approved_collectors"), limit=80, max_chars=300),
        "unresolved_questions": [
            _normalize_row(row, _QUESTION_KEYS, "unresolved question")
            for row in _dict_rows(value.get("unresolved_questions"), limit=40)
        ],
    }


def validate_profile_review_patch(
    patch: dict[str, Any],
    focused_profile: dict[str, Any],
) -> list[str]:
    _require_focused_profile(focused_profile)
    errors: list[str] = []
    if patch.get("schema_version") != PROFILE_REVIEW_PATCH_SCHEMA_VERSION:
        errors.append(f"schema_version must be {PROFILE_REVIEW_PATCH_SCHEMA_VERSION}")

    contract = focused_profile.get("observability_contract") if isinstance(focused_profile.get("observability_contract"), dict) else {}
    metric_names = {
        str(row.get("metric_name") or "")
        for row in contract.get("metrics") or []
        if isinstance(row, dict) and str(row.get("metric_name") or "")
    }
    metric_names.update(
        str(row.get("name") or "")
        for row in contract.get("heartbeats") or []
        if isinstance(row, dict) and str(row.get("name") or "")
    )
    component_ids = {
        str(row.get("component_id") or row.get("name") or "")
        for row in focused_profile.get("runtime_components") or []
        if isinstance(row, dict) and str(row.get("component_id") or row.get("name") or "")
    }
    log_sources = {
        str(row.get("source") or "")
        for row in contract.get("logs") or []
        if isinstance(row, dict) and str(row.get("source") or "")
    }
    collector_names = {
        str(row.get("collector") or "")
        for row in focused_profile.get("read_only_collectors") or []
        if isinstance(row, dict) and str(row.get("collector") or "")
    }

    for row in patch.get("metric_semantics_overrides") or []:
        name = str(row.get("metric_name") or "")
        if not name or name not in metric_names:
            errors.append(f"unknown metric_name: {name or '<empty>'}")
        direction = str(row.get("healthy_direction") or "unknown")
        if direction not in HEALTHY_DIRECTIONS:
            errors.append(f"unsupported healthy_direction for {name}: {direction}")
        for key in ("zero_behavior", "increase_behavior", "decrease_behavior"):
            behavior = str(row.get(key) or "unknown")
            if behavior not in METRIC_BEHAVIORS:
                errors.append(f"unsupported {key} for {name}: {behavior}")
    for row in patch.get("component_role_overrides") or []:
        component_id = str(row.get("component_id") or "")
        if not component_id or component_id not in component_ids:
            errors.append(f"unknown component_id: {component_id or '<empty>'}")
    for component_id in patch.get("ignored_component_ids") or []:
        if component_id not in component_ids:
            errors.append(f"unknown ignored component_id: {component_id}")
    for row in patch.get("log_source_overrides") or []:
        source = str(row.get("source") or "")
        if not source or source not in log_sources:
            errors.append(f"unknown log source: {source or '<empty>'}")
    for collector in patch.get("approved_collectors") or []:
        if collector not in collector_names:
            errors.append(f"unknown collector: {collector}")
    return sorted(set(errors))


def build_approved_operational_profile(
    *,
    focused_profile: dict[str, Any],
    human_review: dict[str, Any],
    accepted_patch: dict[str, Any],
    normalization: dict[str, Any] | None = None,
    profile_id: str = "",
    approved_at: str = "",
) -> dict[str, Any]:
    _require_focused_profile(focused_profile)
    _require_human_review(human_review, require_approval=True)
    patch = normalize_profile_review_patch(accepted_patch)
    errors = validate_profile_review_patch(patch, focused_profile)
    if errors:
        raise ProfileReviewError("profile review patch validation failed: " + "; ".join(errors))

    reviewer = _text(human_review.get("reviewer"), 300)
    normalization = normalization if isinstance(normalization, dict) else {}
    normalization_provider_id = str(normalization.get("provider_id") or "").strip() or "deterministic"
    review_provenance = f"human_approved:{normalization_provider_id}"
    effective_profile_id = profile_id or str(focused_profile.get("system_label") or "approved-operational-profile")
    approved = focused_profile_to_approved_profile(
        profile_id=effective_profile_id,
        focused_profile=focused_profile,
    )
    approved["schema_version"] = APPROVED_OPERATIONAL_PROFILE_SCHEMA_VERSION
    approved["status"] = "approved"
    approved["explicit_profile"] = True

    system_profile = approved.setdefault("system_profile", {})
    for key, value in (patch.get("system_summary_overrides") or {}).items():
        destination = "purpose" if key == "primary_purpose" else key
        system_profile[destination] = value
    system_profile["critical_user_outcomes"] = list(patch.get("confirmed_user_outcomes") or [])
    approved["confirmed_user_outcomes"] = list(patch.get("confirmed_user_outcomes") or [])

    metric_semantics = approved.setdefault("metric_semantics", {})
    for row in patch.get("metric_semantics_overrides") or []:
        name = str(row.get("metric_name") or "")
        current = metric_semantics.setdefault(name, {})
        for key in (
            "meaning",
            "healthy_direction",
            "zero_behavior",
            "increase_behavior",
            "decrease_behavior",
        ):
            if row.get(key) not in (None, ""):
                current[key] = row[key]
        current["review_reason"] = str(row.get("reason") or "")
        current["review_provenance"] = review_provenance

    component_map = approved.setdefault("component_map", {})
    for row in patch.get("component_role_overrides") or []:
        component_id = str(row.get("component_id") or "")
        current = component_map.setdefault(component_id, {})
        current["role"] = str(row.get("role") or current.get("role") or "")
        current["review_reason"] = str(row.get("reason") or "")
        current["review_provenance"] = review_provenance
    ignored = set(patch.get("ignored_component_ids") or [])
    for component_id in ignored:
        component_map.pop(component_id, None)

    contract = focused_profile.get("observability_contract") if isinstance(focused_profile.get("observability_contract"), dict) else {}
    logs = [deepcopy(row) for row in contract.get("logs") or [] if isinstance(row, dict)]
    log_overrides = {str(row.get("source") or ""): row for row in patch.get("log_source_overrides") or []}
    for row in logs:
        override = log_overrides.get(str(row.get("source") or ""))
        if override:
            row["meaning"] = override.get("meaning") or row.get("meaning") or ""
            row["review_reason"] = override.get("reason") or ""
    approved["log_sources"] = logs
    approved["approved_collectors"] = list(patch.get("approved_collectors") or [])
    approved["human_questions"] = [
        str(row.get("question") or "")
        for row in patch.get("unresolved_questions") or []
        if str(row.get("question") or "")
    ]
    approved["required_profile_questions"] = list(approved["human_questions"])
    approved["review_policy"] = {
        **(approved.get("review_policy") if isinstance(approved.get("review_policy"), dict) else {}),
        "context_is_not_evidence": True,
        "require_evidence_id_for_support": True,
        "confirmed_outcomes_required_for_promotion": True,
        "source_access_after_approval": "disabled",
    }
    approved["human_review"] = {
        "schema_version": str(human_review.get("schema_version") or "code_profile_human_review_form.v1"),
        "decision": "approved",
        "reviewer": reviewer,
        "approved_at": approved_at or utc_now(),
        "profile_matches_deployment": True,
        "deployment_period_confirmed": True,
        "log_scope_confirmed": True,
        "approval_note": _text(human_review.get("approval_note"), 2000),
        "answers_sha256": sha256_json(human_review.get("answers") or []),
    }
    approved["approval_provenance"] = {
        "focused_profile_sha256": sha256_json(focused_profile),
        "human_review_sha256": sha256_json(human_review),
        "accepted_patch_sha256": sha256_json(patch),
        "normalization_model_input_sha256": str(normalization.get("model_input_sha256") or ""),
        "normalization_output_sha256": str(
            normalization.get("parsed_output_sha256")
            or normalization.get("accepted_candidate_patch_sha256")
            or ""
        ),
        "source_discovery_sha256": str(focused_profile.get("source_discovery_sha256") or ""),
        "source_context_sha256": str(focused_profile.get("source_context_sha256") or ""),
        "source_analysis_sha256": str(focused_profile.get("source_analysis_sha256") or ""),
        "normalization_provider_id": normalization_provider_id,
        "normalization_model_name": str(normalization.get("model_name") or ""),
    }
    approved.pop("approved_profile_sha256", None)
    approved["approved_profile_sha256"] = sha256_json(approved)

    scan = scan_sanitized_text(
        "approved_operational_profile.json",
        json.dumps(approved, ensure_ascii=False, sort_keys=True),
    )
    if scan.get("findings"):
        raise ProfileReviewError("approved operational profile failed safety validation")
    return approved


def validate_approved_operational_profile(
    profile: dict[str, Any],
    *,
    focused_profile: dict[str, Any] | None = None,
) -> list[str]:
    errors: list[str] = []
    if not isinstance(profile, dict):
        return ["approved operational profile must be an object"]
    if profile.get("schema_version") != APPROVED_OPERATIONAL_PROFILE_SCHEMA_VERSION:
        errors.append(f"schema_version must be {APPROVED_OPERATIONAL_PROFILE_SCHEMA_VERSION}")
    if profile.get("status") != "approved" or profile.get("explicit_profile") is not True:
        errors.append("profile must be explicit and approved")
    claimed = str(profile.get("approved_profile_sha256") or "")
    material = deepcopy(profile)
    material.pop("approved_profile_sha256", None)
    if not claimed or claimed != sha256_json(material):
        errors.append("approved_profile_sha256 mismatch")
    review = profile.get("human_review") if isinstance(profile.get("human_review"), dict) else {}
    if review.get("decision") != "approved":
        errors.append("human_review.decision must be approved")
    if not str(review.get("reviewer") or "").strip():
        errors.append("human_review.reviewer is required")
    for key in ("profile_matches_deployment", "deployment_period_confirmed", "log_scope_confirmed"):
        if review.get(key) is not True:
            errors.append(f"human_review.{key} must be true")
    review_policy = profile.get("review_policy") if isinstance(profile.get("review_policy"), dict) else {}
    if review_policy.get("source_access_after_approval") != "disabled":
        errors.append("review_policy.source_access_after_approval must be disabled")
    if review_policy.get("context_is_not_evidence") is not True:
        errors.append("review_policy.context_is_not_evidence must be true")
    if review_policy.get("require_evidence_id_for_support") is not True:
        errors.append("review_policy.require_evidence_id_for_support must be true")
    if focused_profile is not None:
        provenance = profile.get("approval_provenance") if isinstance(profile.get("approval_provenance"), dict) else {}
        if provenance.get("focused_profile_sha256") != sha256_json(focused_profile):
            errors.append("focused profile binding mismatch")
        for key in ("source_context_sha256", "source_analysis_sha256", "source_discovery_sha256"):
            expected = str(focused_profile.get(key) or "")
            if expected and provenance.get(key) != expected:
                errors.append(f"{key} binding mismatch")
    scan = scan_sanitized_text(
        "approved_operational_profile.json",
        json.dumps(profile, ensure_ascii=False, sort_keys=True),
    )
    if scan.get("findings"):
        errors.append("approved operational profile contains unsafe content")
    return sorted(set(errors))


def profile_review_change_summary(patch: dict[str, Any]) -> dict[str, int]:
    return {
        "system_summary_fields": len(patch.get("system_summary_overrides") or {}),
        "metric_semantics": len(patch.get("metric_semantics_overrides") or []),
        "component_roles": len(patch.get("component_role_overrides") or []),
        "log_sources": len(patch.get("log_source_overrides") or []),
        "confirmed_user_outcomes": len(patch.get("confirmed_user_outcomes") or []),
        "ignored_components": len(patch.get("ignored_component_ids") or []),
        "approved_collectors": len(patch.get("approved_collectors") or []),
        "unresolved_questions": len(patch.get("unresolved_questions") or []),
    }


def _require_focused_profile(profile: dict[str, Any]) -> None:
    if not isinstance(profile, dict) or profile.get("schema_version") != "focused_operational_profile.v1":
        raise ProfileReviewError("focused_operational_profile.v1 is required")


def _require_human_review(review: dict[str, Any], *, require_approval: bool) -> None:
    if not isinstance(review, dict):
        raise ProfileReviewError("human_review object is required")
    if not isinstance(review.get("answers"), list):
        raise ProfileReviewError("human_review.answers must be an array")
    if require_approval:
        if str(review.get("decision") or "") != "approved":
            raise ProfileReviewError("human review decision must be approved")
        for key in ("profile_matches_deployment", "deployment_period_confirmed", "log_scope_confirmed"):
            if review.get(key) is not True:
                raise ProfileReviewError(f"{key} must be confirmed before approval")
        if not _text(review.get("reviewer"), 300):
            raise ProfileReviewError("reviewer is required before approval")


def _focused_profile_for_normalization(profile: dict[str, Any]) -> dict[str, Any]:
    contract = profile.get("observability_contract") if isinstance(profile.get("observability_contract"), dict) else {}
    return {
        "schema_version": profile.get("schema_version"),
        "system_label": profile.get("system_label"),
        "system_summary": profile.get("system_summary") or {},
        "runtime_components": list(profile.get("runtime_components") or [])[:80],
        "observability_contract": {
            "logs": list(contract.get("logs") or [])[:80],
            "metrics": list(contract.get("metrics") or [])[:80],
            "heartbeats": list(contract.get("heartbeats") or [])[:40],
        },
        "read_only_collectors": list(profile.get("read_only_collectors") or [])[:80],
        "human_review_required": list(profile.get("human_review_required") or [])[:40],
        "source_discovery_sha256": profile.get("source_discovery_sha256") or "",
        "source_context_sha256": profile.get("source_context_sha256") or "",
        "source_analysis_sha256": profile.get("source_analysis_sha256") or "",
        "profile_limits": profile.get("profile_limits") or {},
    }


def _human_review_for_normalization(review: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": str(review.get("schema_version") or "code_profile_human_review_form.v1"),
        "reviewer": _text(review.get("reviewer"), 300),
        "decision": _text(review.get("decision"), 100),
        "profile_matches_deployment": bool(review.get("profile_matches_deployment")),
        "deployment_period_confirmed": bool(review.get("deployment_period_confirmed")),
        "log_scope_confirmed": bool(review.get("log_scope_confirmed")),
        "answers": [
            {
                "question": _text(row.get("question"), 500),
                "answer": _text(row.get("answer"), 2000),
            }
            for row in review.get("answers") or []
            if isinstance(row, dict)
        ][:40],
        "approval_note": _text(review.get("approval_note"), 2000),
    }


def _normalize_row(row: dict[str, Any], allowed: set[str], label: str) -> dict[str, Any]:
    unexpected = sorted(set(row) - allowed)
    if unexpected:
        raise ProfileReviewError(f"{label} patch contains unsupported fields: " + ", ".join(unexpected))
    return {
        key: _text(value, 2000)
        for key, value in row.items()
        if key in allowed and _text(value, 2000)
    }


def _dict_rows(value: Any, *, limit: int) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [row for row in value if isinstance(row, dict)][:limit]


def _strings(value: Any, *, limit: int, max_chars: int) -> list[str]:
    if not isinstance(value, list):
        return []
    output: list[str] = []
    for item in value:
        text = _text(item, max_chars)
        if text and text not in output:
            output.append(text)
        if len(output) >= limit:
            break
    return output


def _text(value: Any, max_chars: int) -> str:
    return str(value or "").strip()[:max_chars]
