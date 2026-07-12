from __future__ import annotations

from scripts.generate_precomputed_review_from_multi_run import (
    _bundle_db_corpus_coverage,
    _coverage_corpus_label,
    _source_observations,
)


def test_local_first_occurrences_are_not_reported_as_zero_db_coverage() -> None:
    coverage = _bundle_db_corpus_coverage(
        {
            "evidence_items": [
                {"type": "log_pattern", "count": 3},
                {"type": "log_pattern", "count": 2},
            ]
        },
        fallback_rows=5,
    )

    assert coverage["strategy"] == "local_first_grouped_occurrence_accounting"
    assert coverage["covered_row_count"] == 5
    assert coverage["uncovered_row_count"] == 0
    assert coverage["coverage_ratio"] == 1.0
    assert _coverage_corpus_label(coverage) == "event rows"


def test_approved_run_reports_source_access_boundary_without_empty_hashes() -> None:
    observations = _source_observations(
        source_context_sha="",
        source_analysis_sha="",
        approved_profile={"review_policy": {"source_access_after_approval": "disabled"}},
    )

    assert "frozen approved profile" in observations[0]
    assert "sha256=." not in " ".join(observations)
