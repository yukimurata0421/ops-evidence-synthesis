from __future__ import annotations

from ops_evidence_synthesis.agents.adk_investigator import build_adk_tool_contract_trace


def test_trace_includes_chunk_and_merge_full_corpus_step() -> None:
    payload = {
        "evidence_sha256": "a" * 64,
        "summary": {"log_count": 100, "raw_log_policy": "not_uploaded"},
        "generation": {"provider_mode": "real_api"},
        "provider_statuses": [
            {
                "provider_id": "gemini-enterprise-agent-platform",
                "status": "ok",
                "schema_valid": True,
                "chunk_failure_count": 0,
            }
        ],
        "analysis_context": {
            "provider_full_corpus_evidence_items": 12,
            "provider_full_corpus_analyzed_evidence_items": 12,
            "provider_full_corpus_coverage_ratio": 1.0,
            "provider_full_corpus_chunk_count": 3,
            "provider_full_corpus_chunk_manifest_count": 3,
            "provider_full_corpus_unassigned_evidence_items": 0,
        },
        "review_graph_summary": {"targets_total": 0},
        "targets": [],
    }

    trace = build_adk_tool_contract_trace(payload)
    steps = {row["step"]: row for row in trace}

    assert "chunk_and_merge_full_corpus" in steps
    assert steps["chunk_and_merge_full_corpus"]["status"] == "completed"
    output = steps["chunk_and_merge_full_corpus"]["output"]
    assert output["analyzed_evidence_items"] == 12
    assert output["chunk_count"] == 3
    assert output["determinism_scope"]["merge"] == "deterministic_sort_dedup_over_recorded_chunk_outputs"
