from __future__ import annotations

import re
from typing import Any, Iterable


_ABSENCE_MARKERS = (
    "no ",
    "not present",
    "not observed",
    "does not include",
    "does not contain",
    "were not provided",
    "was not provided",
    "are missing",
    "is missing",
    "contains 0",
    "only successful",
    "absence of",
)


def filter_contradicted_absence_claims(
    values: Iterable[object],
    *,
    evidence_items: Iterable[dict[str, Any]],
) -> list[str]:
    facts = observed_evidence_facts(evidence_items)
    output: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if not text or _absence_claim_is_contradicted(text, facts):
            continue
        if text not in output:
            output.append(text)
    return output


def contradicted_absence_claims(
    values: Iterable[object],
    *,
    evidence_items: Iterable[dict[str, Any]],
) -> list[str]:
    facts = observed_evidence_facts(evidence_items)
    return [
        text
        for value in values
        if (text := str(value or "").strip()) and _absence_claim_is_contradicted(text, facts)
    ]


def reconcile_missing_evidence(
    values: Iterable[object],
    *,
    evidence_items: Iterable[dict[str, Any]],
) -> list[str]:
    facts = observed_evidence_facts(evidence_items)
    output: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if not text:
            continue
        lowered = text.casefold()
        if "http_error" in facts and re.search(r"http\s*(?:500|5xx)\s+error\s+logs?\s+or\s+metrics?", lowered):
            text = "HTTP 5xx time-series metrics tied to this review unit."
        elif "http_error" in facts and (
            re.search(r"http\s*(?:500|5xx)\s+error\s+logs?", lowered)
            or "checkout failure logs" in lowered
        ) and "metric" not in lowered:
            continue
        elif "pool_exhaustion" in facts and "pool exhaustion logs" in lowered and "metric" not in lowered:
            continue
        elif "restart" in facts and "restart events" in lowered and "metric" not in lowered:
            continue
        if text not in output:
            output.append(text)
    return output


def evidence_items_from_bundle(bundle: dict[str, Any]) -> list[dict[str, Any]]:
    items = [item for item in bundle.get("evidence_items") or [] if isinstance(item, dict)]
    if items:
        return items
    refs = bundle.get("evidence_refs") if isinstance(bundle.get("evidence_refs"), dict) else {}
    return [item for item in refs.values() if isinstance(item, dict)]


def observed_evidence_facts(evidence_items: Iterable[dict[str, Any]]) -> set[str]:
    facts: set[str] = set()
    for item in evidence_items:
        event_type = str(item.get("event_type") or item.get("type") or "").casefold()
        text = " ".join(
            str(item.get(key) or "")
            for key in ("message_template", "summary", "example_sanitized", "metric_name")
        ).casefold()
        if event_type in {"http_5xx", "http5xx"} or re.search(r"\bhttp\s*(?:500|5\d\d|5xx)\b", text):
            facts.update({"http_error", "runtime_error"})
        if "checkout fail" in text:
            facts.update({"checkout_failure", "runtime_error"})
        if "pool exhaust" in text:
            facts.add("pool_exhaustion")
        if event_type == "timeout" or "timeout" in text:
            facts.add("timeout")
        if event_type == "restart" or re.search(r"\brestart(?:ed|ing|s)?\b", text):
            facts.add("restart")
        if event_type in {"error", "fatal", "exception"} or re.search(r"\b(?:failed|failure|exception)\b", text):
            facts.add("runtime_error")
        if "checkout_500_count" in text:
            facts.add("checkout_500_signal")
        if "db_pool_exhausted_count" in text:
            facts.add("db_pool_exhausted_signal")
        if "deploy rollout" in text or "deployment" in text or event_type == "deployment_event":
            facts.add("deployment")
        if "checkout completed" in text and ("status=200" in text or "status=<num>" in text):
            facts.add("successful_checkout")
    return facts


def _absence_claim_is_contradicted(text: str, facts: set[str]) -> bool:
    lowered = text.casefold()
    if not any(marker in lowered for marker in _ABSENCE_MARKERS):
        return False
    if "correlated" in lowered:
        return False
    if "this chunk" in lowered or "current chunk" in lowered:
        return False
    checks = (
        ("http_error", bool(re.search(r"http\s*(?:500|5xx)|http error", lowered))),
        ("checkout_failure", "checkout fail" in lowered),
        ("pool_exhaustion", "pool exhaust" in lowered),
        ("restart", bool(re.search(r"\brestart(?:ed|s)?\b", lowered))),
        ("timeout", "timeout" in lowered),
        ("runtime_error", "error logs" in lowered or "failure signals" in lowered),
        (
            "runtime_error",
            any(
                token in lowered
                for token in (
                    "error signals",
                    "error or failure evidence",
                    "failure evidence",
                    "errors or anomalies",
                    "500-status patterns",
                )
            ),
        ),
        ("checkout_500_signal", "checkout_500_count" in lowered),
        ("db_pool_exhausted_signal", "db_pool_exhausted_count" in lowered),
        ("deployment", "deployment logs" in lowered or "version anchors" in lowered),
    )
    return any(observed and fact in facts for fact, observed in checks)
