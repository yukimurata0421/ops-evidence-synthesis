from __future__ import annotations

import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _generator_module():
    path = ROOT / "scripts" / "generate_precomputed_review_from_multi_run.py"
    spec = importlib.util.spec_from_file_location("generate_precomputed_review_from_multi_run", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_projection_coverage_interpretation_explains_long_tail_low_coverage() -> None:
    module = _generator_module()

    text = module._projection_coverage_interpretation(
        service="stream_v3_monitoring",
        log_count=4747,
        full_items=1520,
        model_items=140,
        model_occurrences=496,
        coverage=0.104487,
    )

    assert "Single-prompt projection coverage is occurrence-weighted, not raw-row coverage" in text
    assert "4,747 rows and 1,520 grouped Evidence Items" in text
    assert "140 high-signal Evidence Items" in text
    assert "496 repeated occurrences" in text
    assert "long tail" in text
    assert "not all copied into the bounded single-prompt projection" in text


def test_projection_coverage_interpretation_for_dense_corpus_avoids_low_coverage_claim() -> None:
    module = _generator_module()

    text = module._projection_coverage_interpretation(
        service="stream_v3_runtime",
        log_count=11399,
        full_items=654,
        model_items=140,
        model_occurrences=10771,
        coverage=0.944907,
    )

    assert "Single-prompt projection coverage is occurrence-weighted, not raw-row coverage" in text
    assert "Remaining Evidence Items stay SHA-fixed" in text
    assert "long tail" not in text


def test_projection_interpretation_prefers_provider_full_corpus_coverage_when_reported() -> None:
    module = _generator_module()

    coverage = {
        "schema_version": "provider_full_corpus_coverage.v1",
        "mode": "full_evidence_item_chunking",
        "full_evidence_item_count": 1520,
        "analyzed_evidence_item_count": 1520,
        "coverage_ratio": 1.0,
        "max_chunk_count": 11,
        "all_schema_valid_providers_covered_full_corpus": True,
    }
    text = module._projection_coverage_interpretation(
        service="stream_v3_monitoring",
        log_count=4747,
        full_items=1520,
        model_items=140,
        model_occurrences=496,
        coverage=0.104487,
        full_corpus_coverage=coverage,
    )

    assert "Single-prompt projection coverage" in text
    assert "covered 1,520/1,520 grouped Evidence Items" in text
    assert "through chunked provider calls" in text
    assert "low-frequency Evidence Items are analyzed" in text


def test_provider_full_corpus_coverage_uses_weakest_schema_valid_provider() -> None:
    module = _generator_module()

    coverage = module._provider_full_corpus_coverage(
        {
            "model_runs": [
                {
                    "provider_id": "provider-a",
                    "status": "ok",
                    "schema_valid": True,
                    "full_corpus_coverage": {
                        "full_evidence_item_count": 5,
                        "analyzed_evidence_item_count": 5,
                        "coverage_ratio": 1.0,
                        "chunk_count": 3,
                        "chunk_manifest_entry_count": 3,
                        "chunk_manifest_sha256": "a" * 64,
                        "unassigned_evidence_item_count": 0,
                    },
                },
                {
                    "provider_id": "provider-b",
                    "status": "ok",
                    "schema_valid": True,
                    "full_corpus_coverage": {
                        "full_evidence_item_count": 5,
                        "analyzed_evidence_item_count": 4,
                        "coverage_ratio": 0.8,
                        "chunk_count": 2,
                        "chunk_manifest_entry_count": 2,
                        "chunk_manifest_sha256": "b" * 64,
                        "unassigned_evidence_item_count": 1,
                    },
                },
            ]
        },
        full_items=5,
    )

    assert coverage["mode"] == "full_evidence_item_chunking"
    assert coverage["schema_valid_provider_count"] == 2
    assert coverage["reported_provider_count"] == 2
    assert coverage["full_evidence_item_count"] == 5
    assert coverage["analyzed_evidence_item_count"] == 4
    assert coverage["coverage_ratio"] == 0.8
    assert coverage["max_chunk_count"] == 3
    assert coverage["max_chunk_manifest_entry_count"] == 3
    assert coverage["chunk_manifest_sha256s"] == ["a" * 64, "b" * 64]
    assert coverage["unassigned_evidence_item_count"] == 1
    assert coverage["all_provider_chunk_manifests_present"] is True
    assert coverage["all_schema_valid_providers_covered_full_corpus"] is False


def test_provider_full_corpus_coverage_requires_every_schema_valid_provider_manifest() -> None:
    module = _generator_module()

    coverage = module._provider_full_corpus_coverage(
        {
            "model_runs": [
                {
                    "provider_id": "provider-a",
                    "status": "ok",
                    "schema_valid": True,
                    "full_corpus_coverage": {
                        "full_evidence_item_count": 4,
                        "analyzed_evidence_item_count": 4,
                        "coverage_ratio": 1.0,
                        "chunk_count": 2,
                        "chunk_manifest_entry_count": 2,
                        "chunk_manifest_sha256": "a" * 64,
                        "unassigned_evidence_item_count": 0,
                    },
                },
                {
                    "provider_id": "provider-b",
                    "status": "ok",
                    "schema_valid": True,
                    "full_corpus_coverage": {
                        "full_evidence_item_count": 4,
                        "analyzed_evidence_item_count": 4,
                        "coverage_ratio": 1.0,
                        "chunk_count": 2,
                        "chunk_manifest_entry_count": 2,
                        "chunk_manifest_sha256": "",
                        "unassigned_evidence_item_count": 0,
                    },
                },
            ]
        },
        full_items=4,
    )

    assert coverage["reported_provider_count"] == 2
    assert coverage["chunk_manifest_sha256s"] == ["a" * 64]
    assert coverage["all_provider_chunk_manifests_present"] is False
    assert coverage["all_schema_valid_providers_covered_full_corpus"] is True


def test_evidence_item_accounting_explains_pattern_group_delta() -> None:
    module = _generator_module()

    accounting = module._evidence_item_accounting(
        full_items=8519,
        db_corpus_coverage={"pattern_count": 8513},
    )

    assert accounting["total_evidence_items"] == 8519
    assert accounting["db_pattern_groups"] == 8513
    assert accounting["derived_metric_or_operational_items"] == 6
    assert "deterministic metric" in accounting["explanation"]
    assert "8,513 DB pattern group(s) plus 6" in module._evidence_item_accounting_observation(accounting)


def test_determinism_scope_separates_recorded_outputs_from_merge() -> None:
    module = _generator_module()

    scope = module._determinism_scope()

    assert scope["provider_outputs"] == "recorded_and_hashed_not_recreated_byte_for_byte"
    assert scope["chunk_merge"] == "deterministic_sort_dedup_over_recorded_chunk_outputs"
    assert scope["local_fixture"] == "byte_equal_regeneration_for_deterministic_local_provider_ci"


def test_public_targets_drop_low_information_general_duplicates_and_fix_title() -> None:
    module = _generator_module()

    targets = module._targets(
        {
            "review_targets": [
                {
                    "target_id": "specific-low",
                    "title": "Review target requires validation: general",
                    "class": "validation_target",
                    "subsystem": "general",
                    "canonical_review_unit": "capture_pipeline",
                    "providers": ["gemini-enterprise-agent-platform"],
                    "review_priority_score": 0.61,
                    "evidence_refs": ["PATTERN-001"],
                    "suspected_issue": "capture pipeline failure",
                    "operational_mechanism": "collector failure can hide runtime observations",
                },
                {
                    "target_id": "general-healthy",
                    "title": "Review target requires validation: general",
                    "class": "validation_target",
                    "subsystem": "general",
                    "canonical_review_unit": "general",
                    "providers": ["gemini-enterprise-agent-platform", "qwen-agent-platform"],
                    "review_priority_score": 0.86,
                    "evidence_refs": ["PATTERN-002"],
                    "suspected_issue": "No direct evidence of service failures.",
                    "operational_mechanism": "checkpoint advancement indicates normal operation",
                },
                {
                    "target_id": "specific-high",
                    "title": "Review target requires validation: general",
                    "class": "validation_target",
                    "subsystem": "general",
                    "canonical_review_unit": "capture_pipeline",
                    "providers": ["gemini-enterprise-agent-platform", "qwen-agent-platform"],
                    "review_priority_score": 0.82,
                    "evidence_refs": ["PATTERN-003", "PATTERN-004"],
                    "suspected_issue": "capture pipeline failure",
                    "operational_mechanism": "collector failure can hide runtime observations",
                },
            ]
        },
        provider_statuses=[
            {
                "provider_id": "gemini-enterprise-agent-platform",
                "status": "ok",
                "schema_valid": True,
                "raw_output_sha256": "a" * 64,
            },
            {
                "provider_id": "qwen-agent-platform",
                "status": "ok",
                "schema_valid": True,
                "raw_output_sha256": "b" * 64,
            },
        ],
        log_count=100,
        evidence_lookup={},
        window_start="2026-06-01T00:00:00Z",
        window_end="2026-06-02T00:00:00Z",
    )

    assert [target["target_id"] for target in targets] == ["specific-high"]
    assert targets[0]["title"] == "Review target requires validation: capture_pipeline"
    assert targets[0]["canonical_review_unit"] == "capture_pipeline"
    assert targets[0]["provider_count"] == 2


def test_public_targets_infer_review_unit_for_generic_targets() -> None:
    module = _generator_module()

    targets = module._targets(
        {
            "review_targets": [
                {
                    "target_id": "external-general",
                    "title": "Review target requires validation: general",
                    "class": "validation_target",
                    "subsystem": "general",
                    "canonical_review_unit": "general",
                    "recommended_request_type": "external_dependency_status_query",
                    "providers": ["gemini-enterprise-agent-platform", "qwen-agent-platform"],
                    "review_priority_score": 0.72,
                    "evidence_refs": ["PATTERN-001"],
                    "missing_evidence": ["downstream dependency logs"],
                },
                {
                    "target_id": "instrumentation-general",
                    "title": "Review target requires validation: general",
                    "class": "validation_target",
                    "subsystem": "general",
                    "canonical_review_unit": "general",
                    "recommended_request_type": "instrumentation_consistency_query",
                    "providers": ["gemini-enterprise-agent-platform", "qwen-agent-platform"],
                    "review_priority_score": 0.71,
                    "evidence_refs": ["PATTERN-002"],
                    "missing_evidence": ["metric semantics for error_count"],
                },
                {
                    "target_id": "processing-general",
                    "title": "Review target requires validation: general",
                    "class": "validation_target",
                    "subsystem": "general",
                    "canonical_review_unit": "general",
                    "providers": ["gemini-enterprise-agent-platform", "qwen-agent-platform"],
                    "review_priority_score": 0.7,
                    "evidence_refs": ["PATTERN-003"],
                    "missing_evidence": ["scheduler history"],
                },
            ]
        },
        provider_statuses=[
            {
                "provider_id": "gemini-enterprise-agent-platform",
                "status": "ok",
                "schema_valid": True,
                "raw_output_sha256": "a" * 64,
            },
            {
                "provider_id": "qwen-agent-platform",
                "status": "ok",
                "schema_valid": True,
                "raw_output_sha256": "b" * 64,
            },
        ],
        log_count=100,
        evidence_lookup={
            "PATTERN-001": {"message_template": "HTTP webhook delivery failed"},
            "PATTERN-002": {"message_template": "metric error_count lacks semantic definition"},
            "PATTERN-003": {"message_template": "RUN_RESULT processed matched notified checkpoint advanced"},
        },
        window_start="2026-06-01T00:00:00Z",
        window_end="2026-06-02T00:00:00Z",
    )

    units = {target["target_id"]: target["canonical_review_unit"] for target in targets}
    assert units == {
        "external-general": "downstream_dependency",
        "instrumentation-general": "observability_contract",
        "processing-general": "background_processing",
    }
    assert "Review target requires validation: general" not in [target["title"] for target in targets]
