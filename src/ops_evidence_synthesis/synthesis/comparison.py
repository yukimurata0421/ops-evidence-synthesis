from __future__ import annotations

from collections import Counter
from typing import Any

from ops_evidence_synthesis.canonical import sha256_json
from ops_evidence_synthesis.models import ClaimRecord
from ops_evidence_synthesis.timeutils import utc_now


DEFAULT_BASELINE_PROVIDER = "gemini-enterprise-agent-platform"
DEFAULT_CANDIDATE_PROVIDER = "claude-agent-platform"


def compare_providers(
    store: Any,
    evidence_sha256: str,
    *,
    baseline_provider: str = DEFAULT_BASELINE_PROVIDER,
    candidate_provider: str = DEFAULT_CANDIDATE_PROVIDER,
) -> dict[str, Any]:
    claims = store.fetch_claims(evidence_sha256)
    propositions = store.fetch_propositions(evidence_sha256)
    proposals = {
        str(item["proposition_id"]): item
        for item in store.list_proposals(
            limit=max(50, len(propositions) + 10),
            evidence_sha256=evidence_sha256,
            pending_only=False,
        )
    }
    claims_by_id = {claim.claim_id: claim for claim in claims}
    comparisons = []
    for proposition in propositions:
        linked = [claims_by_id[claim_id] for claim_id in proposition.linked_claim_ids if claim_id in claims_by_id]
        baseline_claims = [claim for claim in linked if claim.provider == baseline_provider]
        candidate_claims = [claim for claim in linked if claim.provider == candidate_provider]
        proposal = proposals.get(proposition.proposition_id, {})
        comparisons.append(
            _compare_proposition(
                proposition_id=proposition.proposition_id,
                question=proposition.question,
                priority=proposition.priority,
                review_priority_score=float(proposal.get("review_priority_score") or 0.0),
                baseline_claims=baseline_claims,
                candidate_claims=candidate_claims,
            )
        )

    summary = _summary(comparisons, baseline_provider=baseline_provider, candidate_provider=candidate_provider)
    payload = {
        "schema_version": "model-comparison/v1",
        "comparison_id": "cmp-"
        + sha256_json(
            {
                "evidence_sha256": evidence_sha256,
                "baseline_provider": baseline_provider,
                "candidate_provider": candidate_provider,
                "comparisons": comparisons,
            }
        )[:16],
        "evidence_sha256": evidence_sha256,
        "baseline_provider": baseline_provider,
        "candidate_provider": candidate_provider,
        "summary": summary,
        "comparisons": comparisons,
        "created_at": utc_now(),
    }
    return payload


def _compare_proposition(
    *,
    proposition_id: str,
    question: str,
    priority: str,
    review_priority_score: float,
    baseline_claims: list[ClaimRecord],
    candidate_claims: list[ClaimRecord],
) -> dict[str, Any]:
    baseline_refs = _refs(baseline_claims)
    candidate_refs = _refs(candidate_claims)
    shared_refs = sorted(set(baseline_refs) & set(candidate_refs))
    union_refs = sorted(set(baseline_refs) | set(candidate_refs))
    evidence_overlap = round(len(shared_refs) / len(union_refs), 4) if union_refs else 0.0
    baseline_score = _provider_score(baseline_claims)
    candidate_score = _provider_score(candidate_claims)
    agreement_score = _agreement_score(baseline_claims, candidate_claims, evidence_overlap)
    comparison_score = round(
        0.45 * evidence_overlap
        + 0.25 * agreement_score
        + 0.15 * min(baseline_score["actionability_score"], candidate_score["actionability_score"])
        + 0.15 * min(baseline_score["evidence_ref_score"], candidate_score["evidence_ref_score"]),
        4,
    )

    if baseline_claims and candidate_claims:
        delta_type = "shared_target"
    elif baseline_claims:
        delta_type = "baseline_only"
    elif candidate_claims:
        delta_type = "candidate_only"
    else:
        delta_type = "no_model_claims"

    return {
        "proposition_id": proposition_id,
        "question": question,
        "priority": priority,
        "delta_type": delta_type,
        "review_priority_score": round(review_priority_score, 4),
        "comparison_score": comparison_score,
        "agreement_score": agreement_score,
        "evidence_overlap_score": evidence_overlap,
        "baseline_provider_score": baseline_score,
        "candidate_provider_score": candidate_score,
        "candidate_minus_baseline": round(candidate_score["overall_score"] - baseline_score["overall_score"], 4),
        "basis": {
            "baseline_claim_count": len(baseline_claims),
            "candidate_claim_count": len(candidate_claims),
            "baseline_claim_types": dict(Counter(claim.claim_type for claim in baseline_claims)),
            "candidate_claim_types": dict(Counter(claim.claim_type for claim in candidate_claims)),
            "shared_evidence_refs": shared_refs,
            "baseline_only_evidence_refs": sorted(set(baseline_refs) - set(candidate_refs)),
            "candidate_only_evidence_refs": sorted(set(candidate_refs) - set(baseline_refs)),
            "baseline_actions": _actions(baseline_claims),
            "candidate_actions": _actions(candidate_claims),
            "baseline_claims": _claim_summaries(baseline_claims),
            "candidate_claims": _claim_summaries(candidate_claims),
        },
    }


def _provider_score(claims: list[ClaimRecord]) -> dict[str, float]:
    if not claims:
        return {
            "overall_score": 0.0,
            "evidence_ref_score": 0.0,
            "valid_evidence_score": 0.0,
            "actionability_score": 0.0,
            "safety_score": 0.0,
        }
    evidence_ref_score = _clamp(sum(1 for claim in claims if claim.evidence_refs) / len(claims))
    valid_evidence_score = _clamp(sum(1 for claim in claims if claim.evidence_refs_valid) / len(claims))
    actionability_score = _clamp(
        sum(1 for claim in claims if claim.temporary_action or claim.permanent_action) / len(claims)
    )
    safety_score = _safety_score(claims)
    overall = _clamp(
        0.35 * evidence_ref_score
        + 0.25 * valid_evidence_score
        + 0.25 * actionability_score
        + 0.15 * safety_score
    )
    return {
        "overall_score": overall,
        "evidence_ref_score": evidence_ref_score,
        "valid_evidence_score": valid_evidence_score,
        "actionability_score": actionability_score,
        "safety_score": safety_score,
    }


def _agreement_score(
    baseline_claims: list[ClaimRecord],
    candidate_claims: list[ClaimRecord],
    evidence_overlap: float,
) -> float:
    if not baseline_claims or not candidate_claims:
        return 0.0
    baseline_types = {claim.claim_type for claim in baseline_claims}
    candidate_types = {claim.claim_type for claim in candidate_claims}
    type_union = baseline_types | candidate_types
    type_overlap = len(baseline_types & candidate_types) / len(type_union) if type_union else 0.0
    return _clamp(0.65 * evidence_overlap + 0.35 * type_overlap)


def _summary(
    comparisons: list[dict[str, Any]],
    *,
    baseline_provider: str,
    candidate_provider: str,
) -> dict[str, Any]:
    counts = Counter(item["delta_type"] for item in comparisons)
    return {
        "baseline_provider": baseline_provider,
        "candidate_provider": candidate_provider,
        "proposition_count": len(comparisons),
        "shared_target_count": counts.get("shared_target", 0),
        "baseline_only_count": counts.get("baseline_only", 0),
        "candidate_only_count": counts.get("candidate_only", 0),
        "average_comparison_score": _average(item["comparison_score"] for item in comparisons),
        "average_evidence_overlap_score": _average(item["evidence_overlap_score"] for item in comparisons),
        "average_candidate_minus_baseline": _average(item["candidate_minus_baseline"] for item in comparisons),
    }


def _refs(claims: list[ClaimRecord]) -> list[str]:
    return sorted(
        {
            ref
            for claim in claims
            for ref in (*claim.evidence_refs, *claim.counter_evidence_refs)
            if ref
        }
    )


def _actions(claims: list[ClaimRecord]) -> list[dict[str, Any]]:
    return [
        {
            "claim_id": claim.claim_id,
            "claim_type": claim.claim_type,
            "temporary_action": claim.temporary_action,
            "permanent_action": claim.permanent_action,
            "required_authority": claim.required_authority,
            "evidence_refs": list(claim.evidence_refs),
        }
        for claim in claims
        if claim.temporary_action or claim.permanent_action or claim.required_authority
    ]


def _claim_summaries(claims: list[ClaimRecord]) -> list[dict[str, Any]]:
    return [
        {
            "claim_id": claim.claim_id,
            "claim_type": claim.claim_type,
            "claim_text": claim.claim_text,
            "evidence_refs": list(claim.evidence_refs),
            "counter_evidence_refs": list(claim.counter_evidence_refs),
            "evidence_refs_valid": claim.evidence_refs_valid,
        }
        for claim in claims
    ]


def _safety_score(claims: list[ClaimRecord]) -> float:
    risky_terms = ("delete", "drop table", "purge", "disable auth", "ignore alert")
    risky = 0
    for claim in claims:
        action_text = f"{claim.temporary_action} {claim.permanent_action}".casefold()
        if any(term in action_text for term in risky_terms):
            risky += 1
    return _clamp(1.0 - (risky / len(claims)))


def _average(values: Any) -> float:
    items = [float(value) for value in values]
    if not items:
        return 0.0
    return round(sum(items) / len(items), 4)


def _clamp(value: float) -> float:
    return round(max(0.0, min(1.0, value)), 4)
