from ops_evidence_synthesis.synthesis.evidence_reconciliation import (
    contradicted_absence_claims,
    filter_contradicted_absence_claims,
    observed_evidence_facts,
    reconcile_missing_evidence,
)


EVIDENCE_ITEMS = [
    {
        "evidence_id": "PATTERN-003",
        "event_type": "http_5xx",
        "message_template": "checkout failed HTTP 500 database timeout metric=checkout_500_count",
    },
    {
        "evidence_id": "PATTERN-004",
        "event_type": "unknown",
        "message_template": "database connection pool exhausted metric=db_pool_exhausted_count",
    },
    {
        "evidence_id": "PATTERN-008",
        "event_type": "info",
        "message_template": "service restarted after deployment",
    },
]


def test_observed_facts_are_derived_from_sanitized_evidence_items() -> None:
    facts = observed_evidence_facts(EVIDENCE_ITEMS)
    assert {"http_error", "checkout_failure", "pool_exhaustion", "restart"} <= facts
    assert {"checkout_500_signal", "db_pool_exhausted_signal"} <= facts


def test_chunk_local_absence_claims_are_removed_when_full_evidence_contradicts_them() -> None:
    values = [
        "No direct evidence of HTTP 500s or checkout failures is present in the bundle.",
        "No evidence of complete pool exhaustion is present in the corpus.",
        "No database-side metrics are available.",
    ]
    assert filter_contradicted_absence_claims(values, evidence_items=EVIDENCE_ITEMS) == [
        "No database-side metrics are available."
    ]


def test_correlated_absence_claim_is_retained_as_a_real_validation_gap() -> None:
    values = ["No correlated error logs, restart signals, or throughput drops were observed."]
    assert filter_contradicted_absence_claims(values, evidence_items=EVIDENCE_ITEMS) == values


def test_satisfied_log_request_is_reduced_to_the_still_missing_metric() -> None:
    values = ["HTTP 500 error logs or metrics", "Database server logs"]
    assert reconcile_missing_evidence(values, evidence_items=EVIDENCE_ITEMS) == [
        "HTTP 5xx time-series metrics tied to this review unit.",
        "Database server logs",
    ]


def test_existing_runtime_errors_and_checkout_count_are_not_requested_again() -> None:
    values = ["Application error logs", "HTTP 500 metric counts", "Deployment manifest diff"]
    assert reconcile_missing_evidence(values, evidence_items=EVIDENCE_ITEMS) == [
        "Deployment manifest diff"
    ]


def test_full_corpus_absence_claim_is_contradicted_but_chunk_scoped_claim_is_retained() -> None:
    values = [
        "Absence of error signals in the sanitized logs.",
        "No error signals are cited in this chunk.",
    ]
    assert contradicted_absence_claims(values, evidence_items=EVIDENCE_ITEMS) == [values[0]]
