from __future__ import annotations

import re
from typing import Any


NORMAL_OPERATION_REASON = "normal_operation_observation"
STRUCTURAL_CAVEAT_REASON = "non_incident_structural_caveat"
NO_FINDING_STANCE = "no_finding"

_NO_ISSUE_PHRASES = (
    "none identified",
    "no issue",
    "no observed failure",
    "no observed failures",
    "no evidence of service failure",
    "no evidence of service start failures",
    "no evidence of failure",
    "no logs indicate service start failures",
    "no logs indicate failure",
    "no failure evidence",
    "service appears healthy",
    "likely healthy",
    "currently stable",
    "normal operation",
    "successful operation",
    "logs show successful operation",
    "baseline observation of healthy activity",
    "baseline observation of normal operation",
    "baseline operational observation",
    "confirms operational status",
    "confirms normal",
    "functioning as expected",
    "message ingestion is functional",
    "entirely consistent with normal operation",
    "normal operation rather than an incident",
    "not impacting notification delivery",
    "not experiencing the suspected failure modes",
)

_PROBLEM_PHRASES = (
    "potential",
    "anomaly",
    "spike",
    "surge",
    "increase",
    "decrease",
    "failure",
    "failed",
    "error",
    "timeout",
    "restart loop",
    "expired",
    "missed",
    "disappearance",
    "mismatch",
    "missing",
    "not delivered",
    "zero notifications",
    "upstream change",
    "configuration issue",
    "unknown operational state",
    "invalid argument",
    "invalid arguments",
    "root cause",
    "unexpected",
    "outage",
    "degradation",
    "instability",
)

_INSUFFICIENT_OR_NO_FINDING_TYPES = {"insufficient_evidence", "no_finding"}
_NON_INCIDENT_CAVEAT_TYPES = {"caveat", "support", "insufficient_evidence", "no_finding"}
_STRUCTURAL_CAVEAT_PHRASES = (
    "structural caveat",
    "structural limitation",
    "standard caveat",
    "version anchoring is missing",
    "version anchoring is unconfirmed",
    "deployed_version_confirmed field is false",
    "source context may not match",
    "source code context and runtime environment",
    "source code context and deployed runtime",
    "source code/config and the actual deployed version",
    "source code/config and the actual running version",
)
_NON_INCIDENT_CAVEAT_PHRASES = (
    "rather than a functional finding",
    "not a specific incident finding",
    "not a root cause",
    "not identifying a failure",
    "standard caveat for all findings",
)


def target_reads_as_normal_observation(
    target: dict[str, Any],
    *,
    target_explanation: dict[str, Any] | None = None,
) -> bool:
    """Return True when a review target is actually a no-finding observation.

    These rows should remain auditable, but they should not be counted as
    unresolved incident validation targets or provider problem claims.
    """

    explanation = target_explanation if isinstance(target_explanation, dict) else {}
    if not explanation and isinstance(target.get("target_explanation"), dict):
        explanation = target["target_explanation"]

    issue_text = _joined_text(
        [
            target.get("suspected_issue"),
            explanation.get("suspected_issue"),
        ]
    )
    issue_has_problem = _contains_problem_signal(issue_text)
    issue_has_no_finding = _contains_no_issue_signal(issue_text)
    if issue_has_problem and not issue_has_no_finding:
        return False
    if issue_has_no_finding:
        return True

    no_issue_context = _joined_text(
        [
            target.get("why_not_promoted"),
            explanation.get("why_not_promoted"),
            target.get("why_it_matters"),
            explanation.get("why_it_matters"),
            *_string_items(target.get("counter_evidence_summary")),
            *_string_items(explanation.get("counter_evidence_summary")),
            *_provider_explanation_texts(explanation),
        ]
    )
    if _contains_no_issue_signal(no_issue_context) and not issue_has_problem:
        return True

    claim_types = _provider_claim_types(explanation)
    if claim_types and claim_types.issubset(_INSUFFICIENT_OR_NO_FINDING_TYPES):
        combined = _joined_text([issue_text, no_issue_context])
        return _contains_no_issue_signal(combined) and not _contains_problem_signal(issue_text)
    return False


def target_reads_as_non_incident_structural_caveat(
    target: dict[str, Any],
    *,
    target_explanation: dict[str, Any] | None = None,
) -> bool:
    """Return True when a row is an evidence-boundary caveat, not an incident claim."""

    explanation = target_explanation if isinstance(target_explanation, dict) else {}
    if not explanation and isinstance(target.get("target_explanation"), dict):
        explanation = target["target_explanation"]

    combined = _joined_text(
        [
            target.get("suspected_issue"),
            explanation.get("suspected_issue"),
            target.get("operational_mechanism"),
            explanation.get("operational_mechanism"),
            target.get("why_it_matters"),
            explanation.get("why_it_matters"),
            target.get("why_not_promoted"),
            explanation.get("why_not_promoted"),
            *_string_items(target.get("evidence_summary")),
            *_string_items(explanation.get("evidence_summary")),
            *_string_items(target.get("counter_evidence_summary")),
            *_string_items(explanation.get("counter_evidence_summary")),
            *_provider_explanation_texts(explanation),
        ]
    )
    if not _contains_structural_caveat_signal(combined):
        return False
    claim_types = _provider_claim_types(explanation)
    if claim_types and not claim_types.issubset(_NON_INCIDENT_CAVEAT_TYPES):
        return False
    return _contains_non_incident_caveat_signal(combined) or _contains_no_issue_signal(combined)


def normal_observation_reason(target: dict[str, Any], *, target_explanation: dict[str, Any] | None = None) -> str:
    unit = str(target.get("canonical_review_unit") or target.get("subsystem") or "review unit")
    return (
        f"`{unit}` reads as a normal-operation or no-finding observation. "
        "It is retained for audit, but excluded from unresolved incident validation targets."
    )


def structural_caveat_reason(target: dict[str, Any], *, target_explanation: dict[str, Any] | None = None) -> str:
    unit = str(target.get("canonical_review_unit") or target.get("subsystem") or "review unit")
    return (
        f"`{unit}` reads as a source/deployment evidence-boundary caveat rather than an incident finding. "
        "It is retained for audit, but excluded from unresolved incident validation targets."
    )


def _contains_no_issue_signal(text: str) -> bool:
    lowered = str(text or "").casefold()
    normalized = re.sub(r"[^a-z0-9]+", " ", lowered).strip()
    if normalized in {"none", "n a", "na", "no finding", "no findings"}:
        return True
    return any(phrase in lowered for phrase in _NO_ISSUE_PHRASES)


def _contains_structural_caveat_signal(text: str) -> bool:
    lowered = str(text or "").casefold()
    return any(phrase in lowered for phrase in _STRUCTURAL_CAVEAT_PHRASES)


def _contains_non_incident_caveat_signal(text: str) -> bool:
    lowered = str(text or "").casefold()
    return any(phrase in lowered for phrase in _NON_INCIDENT_CAVEAT_PHRASES)


def _contains_problem_signal(text: str) -> bool:
    lowered = str(text or "").casefold()
    cleaned = lowered
    for phrase in _NO_ISSUE_PHRASES:
        cleaned = cleaned.replace(phrase, " ")
    cleaned = cleaned.replace("missing evidence", " ")
    cleaned = cleaned.replace("missing-evidence", " ")
    return any(phrase in cleaned for phrase in _PROBLEM_PHRASES)


def _provider_claim_types(explanation: dict[str, Any]) -> set[str]:
    rows = explanation.get("provider_explanations") if isinstance(explanation, dict) else []
    return {
        str(row.get("claim_type") or "").strip().casefold()
        for row in rows or []
        if isinstance(row, dict) and str(row.get("claim_type") or "").strip()
    }


def _provider_explanation_texts(explanation: dict[str, Any]) -> list[str]:
    rows = explanation.get("provider_explanations") if isinstance(explanation, dict) else []
    values: list[str] = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        for key in (
            "claim_text",
            "suspected_issue",
            "why_it_matters",
            "why_not_promoted",
            "operational_mechanism",
        ):
            value = str(row.get(key) or "").strip()
            if value:
                values.append(value)
    return values


def _string_items(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value if str(item).strip()]
    text = str(value or "").strip()
    return [text] if text else []


def _joined_text(values: list[Any]) -> str:
    return " ".join(str(value or "") for value in values if str(value or "").strip()).casefold()
