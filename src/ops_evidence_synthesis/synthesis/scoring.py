from __future__ import annotations

from collections import defaultdict

from ops_evidence_synthesis.canonical import sha256_json
from ops_evidence_synthesis.models import ClaimRecord, ParsedResultRecord, PropositionRecord, ScoreRecord
from ops_evidence_synthesis.timeutils import utc_now


def _clamp(value: float) -> float:
    return round(max(0.0, min(1.0, value)), 4)


def score_propositions(
    propositions: list[PropositionRecord] | tuple[PropositionRecord, ...],
    claims: list[ClaimRecord] | tuple[ClaimRecord, ...],
    parsed_results: list[ParsedResultRecord] | tuple[ParsedResultRecord, ...],
) -> tuple[ScoreRecord, ...]:
    now = utc_now()
    claims_by_id = {claim.claim_id: claim for claim in claims}
    valid_result_count = sum(1 for result in parsed_results if result.schema_valid)
    schema_score = _clamp(valid_result_count / len(parsed_results)) if parsed_results else 0.0

    scores: list[ScoreRecord] = []
    for proposition in propositions:
        linked = [claims_by_id[claim_id] for claim_id in proposition.linked_claim_ids if claim_id in claims_by_id]
        if not linked:
            evidence_ref_score = 0.0
            unsupported_penalty = 1.0
            actionability = 0.0
            agreement = 0.0
            contradiction = 0.0
        else:
            evidence_ref_score = _clamp(sum(1 for claim in linked if claim.evidence_refs_valid) / len(linked))
            unsupported_penalty = _clamp(
                sum(1 for claim in linked if not claim.evidence_refs_valid or not claim.evidence_refs) / len(linked)
            )
            actionability = _clamp(
                sum(1 for claim in linked if claim.temporary_action or claim.permanent_action) / len(linked)
            )
            providers = {claim.provider for claim in linked}
            support_providers = {claim.provider for claim in linked if claim.claim_type == "support"}
            agreement = _clamp(len(support_providers) / len(providers)) if providers else 0.0
            contradiction = _contradiction_score(linked)

        safety_score = _safety_score(linked)
        disagreement_weight = 1.0 - agreement
        priority_by_label = {"high": 1.0, "medium": 0.65, "low": 0.35}.get(proposition.priority, 0.5)
        review_priority = _clamp(
            0.25 * priority_by_label
            + 0.2 * evidence_ref_score
            + 0.15 * actionability
            + 0.15 * contradiction
            + 0.15 * disagreement_weight
            + 0.1 * (1.0 - unsupported_penalty)
        )
        score_id = "score-" + sha256_json(
            {
                "proposition_id": proposition.proposition_id,
                "schema_score": schema_score,
                "evidence_ref_score": evidence_ref_score,
                "review_priority": review_priority,
            }
        )[:16]
        scores.append(
            ScoreRecord(
                score_id=score_id,
                proposition_id=proposition.proposition_id,
                schema_score=schema_score,
                evidence_ref_score=evidence_ref_score,
                unsupported_claim_penalty=unsupported_penalty,
                contradiction_penalty=contradiction,
                cross_model_agreement=agreement,
                actionability_score=actionability,
                safety_score=safety_score,
                review_priority_score=review_priority,
                created_at=now,
            )
        )
    return tuple(scores)


def _contradiction_score(linked: list[ClaimRecord]) -> float:
    by_provider: dict[str, set[str]] = defaultdict(set)
    for claim in linked:
        by_provider[claim.provider].add(claim.claim_type)
    has_support = any("support" in values for values in by_provider.values())
    has_counter = any("counter_evidence" in values for values in by_provider.values())
    has_caveat = any(values & {"caveat", "validation_target", "next_data_needed"} for values in by_provider.values())
    if has_support and has_counter:
        return 0.8
    if has_support and has_caveat:
        return 0.45
    if has_counter:
        return 0.6
    return 0.0


def _safety_score(linked: list[ClaimRecord]) -> float:
    if not linked:
        return 0.0
    risky_terms = ("delete", "drop table", "purge", "disable auth", "ignore alert")
    risky = 0
    for claim in linked:
        action_text = f"{claim.temporary_action} {claim.permanent_action}".casefold()
        if any(term in action_text for term in risky_terms):
            risky += 1
    return _clamp(1.0 - (risky / len(linked)))
