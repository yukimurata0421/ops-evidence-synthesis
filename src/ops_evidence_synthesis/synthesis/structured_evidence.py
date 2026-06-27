from __future__ import annotations

from typing import Any

from ops_evidence_synthesis.models import ClaimRecord
from ops_evidence_synthesis.profiles import metric_semantics, profile_for_bundle


def build_structured_evidence(bundle: dict[str, Any], claims: list[ClaimRecord]) -> dict[str, Any]:
    evidence_refs = bundle.get("evidence_refs") or {}
    support: list[dict[str, Any]] = []
    counter_evidence: list[dict[str, Any]] = []
    caveats: list[str] = []
    next_data_needed: list[str] = []
    insufficient_evidence: list[dict[str, Any]] = []
    finding_statuses: dict[str, int] = {}
    identity_gaps: list[str] = []
    evidence_identity: list[dict[str, Any]] = []
    profile_id = str(profile_for_bundle(bundle).get("profile_id") or "generic")

    for claim in claims:
        finding_statuses[claim.finding_status] = finding_statuses.get(claim.finding_status, 0) + 1
        if claim.evidence_identity:
            evidence_identity.append(
                {
                    "claim_id": claim.claim_id,
                    "provider": claim.provider,
                    **dict(claim.evidence_identity),
                }
            )
            identity_gaps.extend(
                f"{claim.claim_id}:{key}"
                for key, value in claim.evidence_identity.items()
                if str(value).casefold() == "unknown"
            )
        refs = list(claim.evidence_refs)
        counter_refs = list(claim.counter_evidence_refs) or refs
        if claim.finding_status in {"insufficient_evidence", "no_finding"} or claim.claim_type == "insufficient_evidence":
            insufficient_evidence.append(
                {
                    "claim_id": claim.claim_id,
                    "provider": claim.provider,
                    "finding_status": claim.finding_status,
                    "claim_text": claim.claim_text,
                    "missing_evidence": list(claim.missing_evidence),
                    "evidence_identity": dict(claim.evidence_identity),
                }
            )
            caveats.append(claim.claim_text)
            caveats.extend(claim.caveats)
            next_data_needed.extend(claim.missing_evidence)
        elif claim.claim_type == "support":
            support.extend(_evidence_items(evidence_refs, refs, claim))
        elif claim.claim_type == "counter_evidence":
            items = _evidence_items(evidence_refs, counter_refs, claim)
            if any(_is_zero_bad_worsening(item, profile_id=profile_id) for item in items):
                support.extend(items)
            else:
                counter_evidence.extend(items)
        elif claim.claim_type in {"caveat", "validation_target"}:
            caveats.append(claim.claim_text)
            caveats.extend(claim.caveats)
            if claim.missing_evidence:
                next_data_needed.extend(claim.missing_evidence)
        elif claim.claim_type == "next_data_needed":
            next_data_needed.append(claim.claim_text)
            next_data_needed.extend(claim.missing_evidence)
        else:
            caveats.extend(claim.caveats)
            next_data_needed.extend(claim.missing_evidence)

    return {
        "support": _dedupe_items(support),
        "counter_evidence": _dedupe_items(counter_evidence),
        "caveats": _dedupe_strings(caveats),
        "next_data_needed": _dedupe_strings(next_data_needed),
        "insufficient_evidence": _dedupe_insufficient(insufficient_evidence),
        "finding_statuses": finding_statuses,
        "identity_gaps": _dedupe_strings(identity_gaps),
        "evidence_identity": evidence_identity,
    }


def _evidence_items(
    evidence_refs: dict[str, Any],
    refs: list[str],
    claim: ClaimRecord,
) -> list[dict[str, Any]]:
    items = []
    for ref in refs:
        details = evidence_refs.get(ref) or {}
        if not isinstance(details, dict):
            details = {}
        item = {
            "evidence_id": ref,
            "kind": details.get("type") or _kind_from_ref(ref),
            "summary": details.get("summary") or claim.claim_text,
            "claim_id": claim.claim_id,
            "provider": claim.provider,
            "subsystem": details.get("subsystem") or claim.subsystem,
        }
        if claim.evidence_identity.get("core_target_type"):
            item["core_target_type"] = claim.evidence_identity["core_target_type"]
        if claim.evidence_identity.get("review_mode"):
            item["review_mode"] = claim.evidence_identity["review_mode"]
        if claim.evidence_identity.get("target_id"):
            item["target_id"] = claim.evidence_identity["target_id"]
        if "count" in details:
            item["count"] = details["count"]
        if "baseline_count" in details:
            item["baseline_count"] = details["baseline_count"]
        if "current_value" in details:
            item["current_value"] = details["current_value"]
        if "baseline_value" in details:
            item["baseline_value"] = details["baseline_value"]
        if "delta" in details:
            item["delta"] = details["delta"]
        if "delta_pct" in details:
            item["delta_pct"] = details["delta_pct"]
        items.append(item)
    return items


def _kind_from_ref(ref: str) -> str:
    if ref.startswith("PATTERN-"):
        return "log_pattern"
    if ref.startswith("METRIC-"):
        return "health_metric"
    if ref.startswith("LOG-"):
        return "representative_log"
    return "evidence"


def _dedupe_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen = set()
    output = []
    for item in items:
        key = (item.get("evidence_id"), item.get("claim_id"))
        if key in seen:
            continue
        seen.add(key)
        output.append(item)
    return output


def _dedupe_strings(values: list[str]) -> list[str]:
    return list(dict.fromkeys(value for value in values if value))


def _dedupe_insufficient(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen = set()
    output = []
    for item in items:
        key = (item.get("claim_id"), item.get("claim_text"))
        if key in seen:
            continue
        seen.add(key)
        output.append(item)
    return output


def _is_zero_bad_worsening(item: dict[str, Any], *, profile_id: str) -> bool:
    summary = str(item.get("summary") or "")
    metric_name = summary.split("=", 1)[0].strip()
    if str(metric_semantics(metric_name, profile_id).get("zero_behavior") or "") != "suspicious":
        return False
    try:
        current_value = float(item.get("current_value"))
        baseline_value = float(item.get("baseline_value"))
    except (TypeError, ValueError):
        return False
    return current_value == 0 and baseline_value > 0
