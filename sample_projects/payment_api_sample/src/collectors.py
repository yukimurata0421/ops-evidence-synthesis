from __future__ import annotations


READ_ONLY_QUERIES = {
    "deployment_correlation_query": [
        "read Cloud Deploy release metadata",
        "read Cloud Run revision traffic",
    ],
    "instrumentation_consistency_query": [
        "read db_pool_exhausted_count",
        "read checkout_500_count",
        "read db_pool_wait_ms",
    ],
    "user_impact_signal_query": [
        "read checkout success rate",
        "read payment authorization latency",
    ],
}


def collector_names() -> list[str]:
    return sorted(READ_ONLY_QUERIES)
