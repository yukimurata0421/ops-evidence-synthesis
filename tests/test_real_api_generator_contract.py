from __future__ import annotations

import importlib.util
from pathlib import Path

from ops_evidence_synthesis.canonical import sha256_json


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
        log_count=50000,
        full_items=1520,
        model_items=140,
        model_occurrences=496,
        coverage=0.104487,
    )

    assert "Single-prompt projection coverage is occurrence-weighted, not raw-row coverage" in text
    assert "50,000 rows and 1,520 grouped Evidence Items" in text
    assert "140 high-signal Evidence Items" in text
    assert "496 repeated occurrences" in text
    assert "long tail" in text
    assert "not all copied into the bounded single-prompt projection" in text


def test_projection_coverage_interpretation_for_dense_corpus_avoids_low_coverage_claim() -> None:
    module = _generator_module()

    text = module._projection_coverage_interpretation(
        service="stream_v3_runtime",
        log_count=45000,
        full_items=1012,
        model_items=140,
        model_occurrences=107160,
        coverage=0.991928,
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
        log_count=50000,
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


def test_public_target_title_regenerates_generic_title_after_canonical_unit_changes() -> None:
    module = _generator_module()

    title = module._public_target_title(
        {"title": "Review target requires validation: observability_contract"},
        canonical_review_unit="transport_sender",
    )

    assert title == "Review target requires validation: transport_sender"


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


def test_public_targets_keep_provider_error_out_of_silent_and_convergence_denominator() -> None:
    module = _generator_module()

    targets = module._targets(
        {
            "review_targets": [
                {
                    "target_id": "runtime-restart",
                    "title": "Runtime restart needs validation",
                    "class": "validation_target",
                    "subsystem": "runtime_recovery",
                    "canonical_review_unit": "runtime_recovery",
                    "providers": ["gemini-enterprise-agent-platform", "mistral-agent-platform"],
                    "review_priority_score": 0.86,
                    "evidence_refs": ["PATTERN-001", "METRIC-001"],
                    "suspected_issue": "watchdog restart failures may affect notification delivery",
                    "operational_mechanism": "runtime restart loop and delivery failure",
                    "why_it_matters": "customer notification delivery can be delayed",
                    "missing_evidence": ["user impact metric"],
                    "source_candidate_count": 2,
                }
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
                "provider_id": "mistral-agent-platform",
                "status": "provider_error",
                "schema_valid": False,
                "failure_reason": "HTTP 429 resource exhausted",
                "raw_output_sha256": "b" * 64,
            },
            {
                "provider_id": "qwen-agent-platform",
                "status": "ok",
                "schema_valid": True,
                "raw_output_sha256": "c" * 64,
            },
        ],
        log_count=50000,
        evidence_lookup={
            "PATTERN-001": {
                "message_template": "watchdog restart failure",
                "first_seen": "2026-06-01T01:00:00Z",
                "last_seen": "2026-06-01T01:10:00Z",
            },
            "METRIC-001": {
                "message_template": "notification delivery latency spike",
                "first_seen": "2026-06-01T01:05:00Z",
                "last_seen": "2026-06-01T01:15:00Z",
            },
        },
        window_start="2026-06-01T00:00:00Z",
        window_end="2026-06-02T00:00:00Z",
    )

    assert len(targets) == 1
    target = targets[0]
    positions = {row["provider_id"]: row for row in target["provider_positions"]}
    assert positions["gemini-enterprise-agent-platform"]["stance"] == "support"
    assert positions["mistral-agent-platform"]["stance"] == "provider_error"
    assert "Excluded from convergence denominator" in positions["mistral-agent-platform"]["one_line"]
    assert positions["qwen-agent-platform"]["stance"] == "silent"
    assert target["provider_count"] == 1
    assert target["agreement"]["convergence_score"] == 0.5
    assert target["agreement"]["summary"].startswith("1/2 schema-valid providers")


def test_provider_failure_statuses_are_not_silent_and_do_not_expand_denominator() -> None:
    module = _generator_module()

    targets = module._targets(
        {
            "review_targets": [
                {
                    "target_id": "delivery-impact",
                    "title": "Delivery impact needs validation",
                    "class": "validation_target",
                    "subsystem": "user_experience",
                    "canonical_review_unit": "user_experience",
                    "providers": [
                        "gemini-enterprise-agent-platform",
                        "openai-gpt-oss-on-vertex",
                        "mistral-agent-platform",
                        "gemma-agent-platform",
                    ],
                    "review_priority_score": 0.84,
                    "evidence_refs": ["PATTERN-001", "METRIC-001", "OPS-001"],
                    "suspected_issue": "notification delivery may be blocked",
                    "operational_mechanism": "provider support plus runtime evidence points at delivery impact",
                    "why_it_matters": "human impact must be validated before incident promotion",
                    "missing_evidence": ["recipient acknowledgement"],
                    "source_candidate_count": 3,
                }
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
                "provider_id": "openai-gpt-oss-on-vertex",
                "status": "ok",
                "schema_valid": True,
                "raw_output_sha256": "b" * 64,
            },
            {
                "provider_id": "qwen-agent-platform",
                "status": "ok",
                "schema_valid": True,
                "raw_output_sha256": "c" * 64,
            },
            {
                "provider_id": "mistral-agent-platform",
                "status": "ok",
                "schema_valid": False,
                "failure_reason": "schema_invalid: missing claims",
                "raw_output_sha256": "d" * 64,
            },
            {
                "provider_id": "gemma-agent-platform",
                "status": "retry_exhausted",
                "schema_valid": False,
                "failure_reason": "retry_exhausted after 3 chunk attempts",
                "raw_output_sha256": "e" * 64,
            },
        ],
        log_count=2000,
        evidence_lookup={
            "PATTERN-001": {"message_template": "delivery failed"},
            "METRIC-001": {"message_template": "delivery latency spike"},
            "OPS-001": {"message_template": "operator observed blocked notification"},
        },
        window_start="2026-06-01T00:00:00Z",
        window_end="2026-06-02T00:00:00Z",
    )

    assert len(targets) == 1
    target = targets[0]
    positions = {row["provider_id"]: row for row in target["provider_positions"]}
    assert positions["gemini-enterprise-agent-platform"]["stance"] == "support"
    assert positions["openai-gpt-oss-on-vertex"]["stance"] == "support"
    assert positions["qwen-agent-platform"]["stance"] == "silent"
    assert positions["mistral-agent-platform"]["stance"] == "provider_error"
    assert positions["gemma-agent-platform"]["stance"] == "provider_error"
    assert "schema_invalid" in positions["mistral-agent-platform"]["one_line"]
    assert "retry_exhausted" in positions["gemma-agent-platform"]["one_line"]
    assert target["provider_count"] == 2
    assert target["agreement"]["convergence_score"] == 0.6666666667
    assert target["agreement"]["summary"].startswith("2/3 schema-valid providers")


def test_canonical_public_target_merge_is_order_independent_and_aggregates_chunk_refs() -> None:
    module = _generator_module()
    provider_statuses = [
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
        {
            "provider_id": "gemma-agent-platform",
            "status": "ok",
            "schema_valid": True,
            "raw_output_sha256": "c" * 64,
        },
    ]
    duplicate_targets = [
        {
            "target_id": "runtime-chunk-b",
            "title": "Runtime restart needs validation",
            "class": "validation_target",
            "subsystem": "runtime_recovery",
            "canonical_review_unit": "runtime_recovery",
            "providers": ["qwen-agent-platform"],
            "review_priority_score": 0.73,
            "evidence_refs": ["PATTERN-002", "METRIC-002"],
            "source_chunk_id": "chunk-runtime-002",
            "suspected_issue": "restart loop may hide service health",
            "operational_mechanism": "a later chunk surfaced the same runtime recovery unit",
            "source_candidate_count": 2,
        },
        {
            "target_id": "runtime-chunk-a",
            "title": "Runtime restart needs validation",
            "class": "validation_target",
            "subsystem": "runtime_recovery",
            "canonical_review_unit": "runtime_recovery",
            "providers": ["gemini-enterprise-agent-platform", "gemma-agent-platform"],
            "review_priority_score": 0.82,
            "evidence_refs": ["PATTERN-001", "METRIC-001"],
            "source_chunk_id": "chunk-runtime-001",
            "suspected_issue": "restart loop may hide service health",
            "operational_mechanism": "an earlier chunk surfaced the runtime recovery unit",
            "source_candidate_count": 3,
        },
    ]

    first_order = module._targets(
        {"review_targets": duplicate_targets},
        provider_statuses=provider_statuses,
        log_count=45000,
        evidence_lookup={},
        window_start="2026-06-01T00:00:00Z",
        window_end="2026-06-02T00:00:00Z",
    )
    reverse_order = module._targets(
        {"review_targets": list(reversed(duplicate_targets))},
        provider_statuses=provider_statuses,
        log_count=45000,
        evidence_lookup={},
        window_start="2026-06-01T00:00:00Z",
        window_end="2026-06-02T00:00:00Z",
    )

    assert sha256_json(first_order) == sha256_json(reverse_order)
    assert len(first_order) == 1
    target = first_order[0]
    assert target["target_id"] == "runtime-chunk-a"
    assert target["evidence_refs"] == ["METRIC-001", "METRIC-002", "PATTERN-001", "PATTERN-002"]
    assert target["source_chunks"] == ["chunk-runtime-001", "chunk-runtime-002"]
    assert target["raw"]["source_chunk_ids"] == ["chunk-runtime-001", "chunk-runtime-002"]
    assert target["provider_count"] == 3
    assert target["agreement"]["convergence_score"] == 1.0
    positions = {row["provider_id"]: row["stance"] for row in target["provider_positions"]}
    assert positions == {
        "gemini-enterprise-agent-platform": "support",
        "qwen-agent-platform": "support",
        "gemma-agent-platform": "support",
    }


def test_public_agreement_counts_support_not_participation_and_keeps_counter_signal() -> None:
    module = _generator_module()

    targets = module._targets(
        {
            "review_targets": [
                {
                    "target_id": "mixed-stances",
                    "class": "validation_target",
                    "canonical_review_unit": "runtime_recovery",
                    "subsystem": "runtime_recovery",
                    "providers": ["provider-a", "provider-b", "provider-c"],
                    "participating_providers": ["provider-a", "provider-b", "provider-c"],
                    "supporting_providers": ["provider-a"],
                    "support_provider_count": 1,
                    "countering_providers": ["provider-b"],
                    "counter_provider_count": 1,
                    "evidence_refs": ["PATTERN-001"],
                }
            ]
        },
        provider_statuses=[
            {"provider_id": provider, "status": "ok", "schema_valid": True}
            for provider in ("provider-a", "provider-b", "provider-c")
        ],
        log_count=100,
        evidence_lookup={"PATTERN-001": {"message_template": "restart failure"}},
        window_start="2026-06-01T00:00:00Z",
        window_end="2026-06-02T00:00:00Z",
    )

    target = targets[0]
    assert target["support_provider_count"] == 1
    assert target["counter_provider_count"] == 1
    assert target["participating_provider_count"] == 3
    assert target["agreement"]["convergence_score"] == 0.3333333333
    assert target["agreement"]["verdict"] == "single_source"
    assert {row["provider_id"]: row["stance"] for row in target["provider_positions"]} == {
        "provider-a": "support",
        "provider-b": "counter",
        "provider-c": "caveat_or_validation",
    }


def test_derivation_provenance_hashes_provider_and_chunk_outputs() -> None:
    module = _generator_module()

    provenance = module._derivation_provenance(
        {
            "model_runs": [
                {
                    "provider_id": "provider-a",
                    "model_name": "model-a",
                    "raw_output_sha256": "a" * 64,
                    "parsed_json_sha256": "b" * 64,
                    "chunk_results": [
                        {"raw_output_sha256": "c" * 64},
                        {"raw_output_sha256": "d" * 64},
                    ],
                }
            ]
        },
        source_artifact_sha256="e" * 64,
        source_artifact_uri="gs://private/run/multi_ai_run.json",
        tested_implementation_commit_sha="f" * 40,
        artifact_generation_commit_sha="1" * 40,
    )

    assert provenance["derivation_mode"] == "deterministic_resynthesis_without_provider_api_calls"
    assert provenance["source_artifact_sha256"] == "e" * 64
    assert provenance["provider_output_sha256s"] == {"provider-a": "a" * 64}
    assert provenance["provider_chunk_output_sha256s"]["provider-a"] == ["c" * 64, "d" * 64]
    assert provenance["tested_implementation_commit_sha"] == "f" * 40
    assert provenance["artifact_generation_commit_sha"] == "1" * 40
    assert provenance["derived_with_commit_sha"] == "1" * 40


def test_public_payload_hash_covers_derivation_provenance() -> None:
    module = _generator_module()
    source = (ROOT / "scripts" / "generate_precomputed_review_from_multi_run.py").read_text(encoding="utf-8")

    assert '"provenance": payload["provenance"]' in source


def test_absence_only_audio_energy_target_stays_validation_not_primary_candidate() -> None:
    module = _generator_module()

    targets = module._targets(
        {
            "review_targets": [
                {
                    "target_id": "audio-energy-absence",
                    "title": "Audio energy absence needs validation",
                    "class": "primary_candidate",
                    "subsystem": "audio_energy",
                    "canonical_review_unit": "audio_energy",
                    "providers": [
                        "gemini-enterprise-agent-platform",
                        "openai-gpt-oss-on-vertex",
                        "qwen-agent-platform",
                    ],
                    "review_priority_score": 0.9,
                    "evidence_refs": ["PATTERN-001", "METRIC-001", "LOG-001", "TRACE-001"],
                    "source_candidate_count": 4,
                    "suspected_issue": "No audio energy measurement logs were provided.",
                    "operational_mechanism": "absence of measurement is an observation gap, not a confirmed cause",
                    "why_it_matters": "stream impact requires positive liveness or user-impact evidence",
                    "counter_evidence_summary": ["No audio energy samples were available in the corpus."],
                    "missing_evidence": [
                        "positive audio_energy measurements",
                        "user impact evidence",
                    ],
                }
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
                "provider_id": "openai-gpt-oss-on-vertex",
                "status": "ok",
                "schema_valid": True,
                "raw_output_sha256": "b" * 64,
            },
            {
                "provider_id": "qwen-agent-platform",
                "status": "ok",
                "schema_valid": True,
                "raw_output_sha256": "c" * 64,
            },
        ],
        log_count=50000,
        evidence_lookup={
            "PATTERN-001": {"message_template": "audio_energy field was absent"},
            "METRIC-001": {"message_template": "audio energy metric missing"},
            "LOG-001": {"message_template": "capture pipeline did not emit audio samples"},
            "TRACE-001": {"message_template": "no direct user playback confirmation"},
        },
        window_start="2026-06-01T00:00:00Z",
        window_end="2026-06-02T00:00:00Z",
    )

    assert len(targets) == 1
    target = targets[0]
    assert target["class"] == "validation_target"
    assert target["original_class"] == "primary_candidate"
    assert target["classification"]["adjustment"] == "demoted_primary_candidate_evidence_thin"
    assert target["promotion"]["state"] == "validation"
    assert "human_review_required" in target["promotion"]["blocked_reason"]


def test_primary_candidates_remain_review_targets_not_accepted_causes() -> None:
    module = _generator_module()
    primary_target = {
        "target_id": "primary-runtime",
        "class": "primary_candidate",
        "provider_count": 5,
        "review_priority_score": 0.91,
        "evidence_refs": ["PATTERN-001", "METRIC-001"],
    }

    counts = module._public_review_counts([primary_target], graph_summary={"auto_archived_count": 0})
    summary = module._review_graph_summary(
        {
            "canonical_review_graph": {
                "summary": {},
                "agreement_dimensions": {
                    "technical_baseline_agreement": {"established": True},
                    "incident_baseline_agreement": {"established": True},
                    "provider_detection_overlap": {"value": "5/5"},
                    "review_unit_convergence": {"value": "1/1"},
                },
            }
        },
        targets=[primary_target],
        provider_count=5,
        log_count=44944,
    )

    assert counts["primary_targets"] == 1
    assert counts["validation_targets"] == 0
    assert summary["primary_promoted_count"] == 1
    assert summary["incident_gate_signal"] == "signal_present"
    assert summary["incident_gate_scope"] == "graph_level_signal_not_target_promotion"
    assert summary["incident_baseline_established_count"] == 0
    assert "promotion remains human-gated" in summary["target_promotion_policy"]
    assert "technical support only" in summary["note"]


def test_verified_impact_target_keeps_causal_validation_without_reopening_impact() -> None:
    module = _generator_module()

    blocked_reason = module._blocked_reason(
        {
            "class": "validation_target",
            "has_user_impact_evidence": True,
            "promotion_blocked_reasons": [],
        },
        provider_count=3,
    )

    assert blocked_reason == "incident_baseline_open; causal_alignment_unverified"
    explanation = module._promotion_explanation(
        state="validation",
        provider_count=3,
        valid_count=5,
        has_user_impact=True,
    )
    assert "direct user impact is established" in explanation
    assert "until user impact" not in explanation

    policy = module._target_promotion_policy(
        [{"has_user_impact_evidence": True}, {"has_user_impact_evidence": True}]
    )
    assert "causal and operational evidence gates" in policy
    assert "until impact" not in policy
