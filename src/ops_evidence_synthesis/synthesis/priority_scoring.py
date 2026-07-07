from __future__ import annotations

import hashlib
import math
import re
from typing import Any, Iterable


GEMINI_PROVIDER_WEIGHT = 1.65
DEFAULT_PROVIDER_WEIGHT = 1.0

USER_IMPACT_TERMS = (
    "user impact",
    "user-impact",
    "user_visible",
    "viewer",
    "customer",
    "delivery",
    "delivered",
    "notification_not_delivered",
    "stream",
    "youtube",
    "audio",
    "latency",
    "error rate",
    "http_5xx",
    "outage",
)

ACTIONABLE_TERMS = (
    "restart",
    "recovery",
    "watchdog",
    "exit",
    "exception",
    "timeout",
    "failure",
    "failed",
    "error",
    "config",
    "deployment",
    "dependency",
    "certificate",
    "auth",
    "delivery",
    "notified",
)

HEALTHY_OR_STATUS_TERMS = (
    "none identified",
    "healthy and idle",
    "healthy state",
    "likely healthy",
    "normal operation",
    "no issue detected",
    "no observed failure",
    "no evidence of service failure",
    "none, service is healthy",
    "status confirmation",
    "not an incident",
    "baseline health",
    "successful runs",
    "successful operation",
    "logs show successful operation",
    "entirely consistent with normal operation",
    "not impacting notification delivery",
    "not experiencing the suspected failure modes",
)

GENERIC_UNITS = {"", "general", "general_review", "generic", "generic_runtime", "unknown", "none", "null"}


def score_review_priority(
    *,
    prior_score: float = 0.0,
    promotion_score: float = 0.0,
    provider_positions: list[dict[str, Any]] | None = None,
    claimed_provider_ids: Iterable[Any] | None = None,
    total_provider_count: int = 0,
    evidence_ref_count: int = 0,
    evidence_family_count: int = 0,
    source_candidate_count: int = 1,
    target_class: str = "",
    canonical_review_unit: str = "",
    title: str = "",
    suspected_issue: str = "",
    operational_mechanism: str = "",
    why_it_matters: str = "",
    why_not_promoted: str = "",
    evidence_summary: Iterable[Any] | None = None,
    counter_evidence_summary: Iterable[Any] | None = None,
    missing_evidence: Iterable[Any] | None = None,
    blocked_reasons: Iterable[Any] | None = None,
    caveats: Iterable[Any] | None = None,
) -> dict[str, Any]:
    """Return a human-review priority score and an explainable breakdown.

    The score ranks review urgency. It is not a truth probability and it is not
    an automatic promotion signal.
    """

    positions = [row for row in provider_positions or [] if isinstance(row, dict)]
    if positions:
        all_provider_ids = [str(row.get("provider_id") or "").strip() for row in positions if str(row.get("provider_id") or "").strip()]
        claimed_ids = [
            str(row.get("provider_id") or "").strip()
            for row in positions
            if str(row.get("stance") or "").casefold() == "claimed" and str(row.get("provider_id") or "").strip()
        ]
    else:
        claimed_ids = [str(provider or "").strip() for provider in claimed_provider_ids or [] if str(provider or "").strip()]
        all_provider_ids = list(claimed_ids)

    weighted_support = _weighted_support_ratio(
        claimed_ids=claimed_ids,
        all_provider_ids=all_provider_ids,
        total_provider_count=total_provider_count,
    )
    gemini_claimed = any(_is_gemini_provider(provider) for provider in claimed_ids)
    gemini_seen = any(_is_gemini_provider(provider) for provider in all_provider_ids) or bool(total_provider_count)
    gemini_signal = 1.0 if gemini_claimed else 0.0

    refs = max(0, int(evidence_ref_count or 0))
    families = max(0, int(evidence_family_count or 0))
    source_candidates = max(1, int(source_candidate_count or 1))
    evidence_volume = _log_signal(refs, normalizer=80)
    evidence_diversity = min(1.0, families / 3.0) if families else (0.35 if refs else 0.0)
    source_breadth = _log_signal(source_candidates, normalizer=10)
    actionability = _actionability_signal(
        canonical_review_unit=canonical_review_unit,
        title=title,
        suspected_issue=suspected_issue,
        operational_mechanism=operational_mechanism,
        why_it_matters=why_it_matters,
        why_not_promoted=why_not_promoted,
        evidence_summary=evidence_summary or [],
        counter_evidence_summary=counter_evidence_summary or [],
        target_class=target_class,
    )
    penalties = _priority_penalties(
        canonical_review_unit=canonical_review_unit,
        title=title,
        suspected_issue=suspected_issue,
        operational_mechanism=operational_mechanism,
        why_not_promoted=why_not_promoted,
        evidence_summary=evidence_summary or [],
        counter_evidence_summary=counter_evidence_summary or [],
        missing_evidence=missing_evidence or [],
        blocked_reasons=blocked_reasons or [],
        caveats=caveats or [],
        gemini_seen=gemini_seen,
        gemini_claimed=gemini_claimed,
    )

    class_bonus = 0.045 if str(target_class or "").casefold() == "primary_candidate" else 0.0
    prior = _clamp(max(float(prior_score or 0.0), float(promotion_score or 0.0)), 0.0, 1.0)
    deterministic_tie_break = _deterministic_tie_break(
        canonical_review_unit,
        title,
        ",".join(sorted(claimed_ids)),
        str(refs),
    )
    computed = (
        0.28
        + 0.31 * weighted_support
        + 0.06 * gemini_signal
        + 0.08 * evidence_volume
        + 0.05 * evidence_diversity
        + 0.06 * source_breadth
        + 0.13 * actionability
        + class_bonus
        - penalties["total_penalty"]
    )
    score = 0.15 * prior + 0.85 * computed + deterministic_tie_break

    if penalties["healthy_status_penalty"]:
        score = min(score, 0.72 + deterministic_tie_break)
    if penalties["generic_unit_penalty"]:
        score = min(score, 0.80 + deterministic_tie_break)
    if gemini_seen and not gemini_claimed and len(claimed_ids) < max(3, int(total_provider_count or len(all_provider_ids) or 0)):
        score = min(score, 0.78 + deterministic_tie_break)

    score = _clamp(score, 0.05, 0.94)
    return {
        "score": round(score, 4),
        "breakdown": {
            "schema_version": "review_priority_score.v2",
            "score_note": "Priority is review urgency, not truth probability.",
            "prior_score": round(prior, 4),
            "weighted_provider_support": round(weighted_support, 4),
            "gemini_weight": GEMINI_PROVIDER_WEIGHT,
            "gemini_claimed": gemini_claimed,
            "gemini_signal": round(gemini_signal, 4),
            "claimed_provider_count": len(set(claimed_ids)),
            "total_provider_count": int(total_provider_count or len(set(all_provider_ids)) or len(set(claimed_ids))),
            "evidence_volume_signal": round(evidence_volume, 4),
            "evidence_diversity_signal": round(evidence_diversity, 4),
            "source_candidate_signal": round(source_breadth, 4),
            "actionability_signal": round(actionability, 4),
            "class_bonus": round(class_bonus, 4),
            "deterministic_tie_break": round(deterministic_tie_break, 4),
            "penalties": penalties,
            "formula": (
                "0.15*prior + 0.85*(weighted provider support, Gemini signal, evidence, "
                "source breadth, actionability, class bonus, penalties) + deterministic tie-break"
            ),
        },
    }


def _weighted_support_ratio(*, claimed_ids: list[str], all_provider_ids: list[str], total_provider_count: int) -> float:
    claimed_unique = sorted(set(provider for provider in claimed_ids if provider))
    all_unique = sorted(set(provider for provider in all_provider_ids if provider))
    total = max(int(total_provider_count or 0), len(all_unique), len(claimed_unique))
    if not all_unique:
        all_unique = list(claimed_unique)
    denominator = sum(_provider_weight(provider) for provider in all_unique)
    missing_provider_count = max(0, total - len(all_unique))
    denominator += missing_provider_count * DEFAULT_PROVIDER_WEIGHT
    if denominator <= 0:
        return 0.0
    numerator = sum(_provider_weight(provider) for provider in claimed_unique)
    return _clamp(numerator / denominator, 0.0, 1.0)


def _provider_weight(provider_id: str) -> float:
    return GEMINI_PROVIDER_WEIGHT if _is_gemini_provider(provider_id) else DEFAULT_PROVIDER_WEIGHT


def _is_gemini_provider(provider_id: str) -> bool:
    return "gemini" in str(provider_id or "").casefold()


def _actionability_signal(
    *,
    canonical_review_unit: str,
    title: str,
    suspected_issue: str,
    operational_mechanism: str,
    why_it_matters: str,
    why_not_promoted: str,
    evidence_summary: Iterable[Any],
    counter_evidence_summary: Iterable[Any],
    target_class: str,
) -> float:
    text = " ".join(
        [
            canonical_review_unit,
            title,
            suspected_issue,
            operational_mechanism,
            why_it_matters,
            why_not_promoted,
            *(str(item) for item in evidence_summary),
            *(str(item) for item in counter_evidence_summary),
            target_class,
        ]
    ).casefold()
    signal = 0.35
    if any(term in text for term in ACTIONABLE_TERMS):
        signal += 0.25
    if any(term in text for term in USER_IMPACT_TERMS):
        signal += 0.25
    if str(target_class or "").casefold() == "primary_candidate":
        signal += 0.15
    if _normalized_unit(canonical_review_unit) in GENERIC_UNITS:
        signal -= 0.15
    if any(term in text for term in HEALTHY_OR_STATUS_TERMS):
        signal -= 0.25
    return _clamp(signal, 0.0, 1.0)


def _priority_penalties(
    *,
    canonical_review_unit: str,
    title: str,
    suspected_issue: str,
    operational_mechanism: str,
    why_not_promoted: str,
    evidence_summary: Iterable[Any],
    counter_evidence_summary: Iterable[Any],
    missing_evidence: Iterable[Any],
    blocked_reasons: Iterable[Any],
    caveats: Iterable[Any],
    gemini_seen: bool,
    gemini_claimed: bool,
) -> dict[str, Any]:
    missing = [str(item or "").strip() for item in missing_evidence if str(item or "").strip()]
    blockers = [str(item or "").strip() for item in blocked_reasons if str(item or "").strip()]
    caveat_values = [str(item or "").strip() for item in caveats if str(item or "").strip()]
    summary_values = [str(item or "").strip() for item in evidence_summary if str(item or "").strip()]
    counter_values = [str(item or "").strip() for item in counter_evidence_summary if str(item or "").strip()]
    text = " ".join(
        [
            canonical_review_unit,
            title,
            suspected_issue,
            operational_mechanism,
            why_not_promoted,
            *summary_values,
            *counter_values,
            *missing,
            *blockers,
            *caveat_values,
        ]
    ).casefold()
    generic_penalty = 0.055 if _normalized_unit(canonical_review_unit) in GENERIC_UNITS else 0.0
    healthy_penalty = 0.09 if any(term in text for term in HEALTHY_OR_STATUS_TERMS) else 0.0
    missing_penalty = min(0.07, 0.0125 * len(missing))
    blocker_penalty = 0.0
    if "user_impact_unverified" in text or "user impact" in text:
        blocker_penalty += 0.025
    if "support_without_evidence_id" in text or "context_only" in text:
        blocker_penalty += 0.05
    if "severity_only" in text:
        blocker_penalty += 0.05
    caveat_penalty = min(0.04, 0.0125 * len(caveat_values))
    gemini_silent_penalty = 0.035 if gemini_seen and not gemini_claimed else 0.0
    total = generic_penalty + healthy_penalty + missing_penalty + blocker_penalty + caveat_penalty + gemini_silent_penalty
    return {
        "generic_unit_penalty": round(generic_penalty, 4),
        "healthy_status_penalty": round(healthy_penalty, 4),
        "missing_evidence_penalty": round(missing_penalty, 4),
        "blocker_penalty": round(blocker_penalty, 4),
        "caveat_penalty": round(caveat_penalty, 4),
        "gemini_silent_penalty": round(gemini_silent_penalty, 4),
        "total_penalty": round(total, 4),
    }


def _log_signal(value: int, *, normalizer: int) -> float:
    if value <= 0:
        return 0.0
    return _clamp(math.log1p(value) / math.log1p(max(1, normalizer)), 0.0, 1.0)


def _deterministic_tie_break(*parts: str) -> float:
    seed = "|".join(str(part or "") for part in parts)
    digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()
    return (int(digest[:6], 16) % 17) / 10000.0


def _normalized_unit(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(value or "").casefold()).strip("_")


def _clamp(value: float, lower: float, upper: float) -> float:
    return min(upper, max(lower, float(value)))
