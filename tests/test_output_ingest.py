from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ops_evidence_synthesis.ai.base import ModelResponse
from ops_evidence_synthesis.storage.sqlite_store import SQLiteStore
from ops_evidence_synthesis.synthesis.output_ingest import merge_candidate_observations, parse_model_output
from ops_evidence_synthesis.synthesis.pipeline import run_model_stage
from ops_evidence_synthesis.synthesis.review_arbitration import resolve_canonical_review_graph_snapshot


def _claim_result() -> dict[str, Any]:
    return {
        "schema_version": "claim-result/v1",
        "finding_status": "no_finding",
        "summary": "no finding",
        "claims": [],
        "propositions": [],
    }


def test_parse_model_output_repairs_code_fence_without_mutating_raw_text() -> None:
    raw = "model response:\n```json\n" + json.dumps(_claim_result()) + "\n```\n"

    parsed = parse_model_output(raw)

    assert parsed.parsed == _claim_result()
    assert parsed.parse_status == "parsed_repaired"
    assert parsed.repair_applied is True
    assert parsed.repaired_output != raw
    assert parsed.repair_rules == ("strip_code_fence_wrapper:1",)


@dataclass(frozen=True, slots=True)
class FencedProvider:
    provider: str = "fenced-provider"
    model_name: str = "fenced-model"
    prompt_name: str = "root-cause"
    temperature: float = 0.0

    def run(self, bundle: dict[str, Any]) -> ModelResponse:
        return ModelResponse(
            provider=self.provider,
            model_name=self.model_name,
            prompt_name=self.prompt_name,
            temperature=self.temperature,
            raw_output="preface\n```json\n" + json.dumps(_claim_result()) + "\n```\ntrailer",
            latency_ms=5,
            input_tokens=3,
            output_tokens=7,
        )


def test_model_stage_persists_repaired_output_artifact(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "repair.sqlite3")
    store.init_schema()
    bundle = {"evidence_sha256": "b" * 64, "evidence_refs": {}}

    parsed_results = run_model_stage(store, bundle, [FencedProvider()])

    assert parsed_results[0].schema_valid is True
    assert parsed_results[0].parsed_json["summary"] == "no finding"
    run = store.fetch_model_runs(bundle["evidence_sha256"])[0]
    assert run.raw_output.startswith("preface")
    artifacts = store.list_model_output_artifacts(bundle["evidence_sha256"])
    assert len(artifacts) == 1
    assert artifacts[0]["run_id"] == run.run_id
    assert artifacts[0]["parse_status"] == "parsed_repaired"
    assert artifacts[0]["repair_applied"] is True
    assert artifacts[0]["original_preserved"] is True


def test_canonical_observation_groups_merge_semantic_duplicates_and_persist(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "groups.sqlite3")
    bundle = {
        "evidence_sha256": "sha-observation",
        "service": "generic",
        "environment": "prod",
        "evidence_refs": {"OPS-001": {"evidence_id": "OPS-001"}},
    }
    duplicate_targets = [
        {
            "review_target_id": "legacy-a",
            "title": "Restart loop requires validation: sender",
            "core_target_type": "restart_loop",
            "subsystem": "transport_sender",
            "evidence_refs": ["OPS-001"],
            "review_priority_score": 0.62,
            "drawer": {"support_evidence": [{"evidence_id": "OPS-001"}]},
        },
        {
            "review_target_id": "legacy-b",
            "title": "Restart loop needs review",
            "core_target_type": "general_review",
            "subsystem": "transport_sender",
            "evidence_refs": ["OPS-001"],
            "review_priority_score": 0.61,
            "drawer": {"support_evidence": [{"evidence_id": "OPS-001"}]},
        },
    ]

    resolution = resolve_canonical_review_graph_snapshot(
        store,
        bundle,
        legacy_review_targets=duplicate_targets,
        legacy_summary={"review_targets": 2, "primary_review_targets": 0},
        persist_if_missing=True,
        created_by="pytest",
    )

    graph = resolution["canonical_review_graph"]
    assert resolution["canonical_graph_status"] == "persisted"
    assert graph["summary"]["validation_count"] == 1
    target = graph["validation_targets"][0]
    assert target["source_candidate_count"] == 2
    assert set(target["source_target_ids"]) == {"legacy-a", "legacy-b"}
    groups = store.list_canonical_observation_groups(bundle["evidence_sha256"])
    assert len(groups) == 1
    assert groups[0]["source_candidate_count"] == 2
    assert groups[0]["canonical_target_type"] == "process_restart_loop"


def test_canonical_observation_groups_roll_up_same_subsystem_even_when_type_differs(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "review-unit.sqlite3")
    bundle = {
        "evidence_sha256": "sha-review-unit",
        "service": "generic",
        "environment": "prod",
        "evidence_refs": {
            "METRIC-010": {"evidence_id": "METRIC-010"},
            "OPS-007": {"evidence_id": "OPS-007"},
        },
    }
    targets = [
        {
            "review_target_id": "runtime-general",
            "title": "Review target requires validation: runtime_recovery",
            "core_target_type": "general_review",
            "subsystem": "runtime_recovery",
            "evidence_refs": ["METRIC-010", "OPS-007"],
            "review_priority_score": 0.62,
            "drawer": {"missing_evidence": ["runtime recovery logs during the incident window"]},
        },
        {
            "review_target_id": "runtime-restart",
            "title": "Restart loop requires validation: runtime_recovery",
            "core_target_type": "restart_loop",
            "subsystem": "runtime_recovery",
            "evidence_refs": ["METRIC-010"],
            "review_priority_score": 0.62,
            "drawer": {"missing_evidence": ["systemd substate logs during the incident window"]},
        },
    ]

    resolution = resolve_canonical_review_graph_snapshot(
        store,
        bundle,
        legacy_review_targets=targets,
        legacy_summary={"review_targets": 2, "primary_review_targets": 0},
        persist_if_missing=True,
        created_by="pytest",
    )

    graph = resolution["canonical_review_graph"]
    assert graph["arbitration_version"] == "review_arbitration.v5"
    assert graph["summary"]["validation_count"] == 1
    target = graph["validation_targets"][0]
    assert target["source_candidate_count"] == 2
    assert set(target["source_target_ids"]) == {"runtime-general", "runtime-restart"}
    assert set(target["evidence_refs"]) == {"METRIC-010", "OPS-007"}
    assert target["canonical_review_unit"] == "runtime_recovery"
    assert target["rollup"]["source_candidate_count"] == 2
    assert target["baseline_support_score"] > 0.0


def test_canonical_observation_groups_roll_up_transport_aliases(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "transport-unit.sqlite3")
    bundle = {
        "evidence_sha256": "sha-transport-unit",
        "service": "generic",
        "environment": "prod",
        "evidence_refs": {
            "METRIC-012": {"evidence_id": "METRIC-012"},
            "OPS-006": {"evidence_id": "OPS-006"},
        },
    }
    targets = [
        {
            "review_target_id": "rtmps-target",
            "title": "Throughput disappearance requires validation: rtmps_ffmpeg",
            "core_target_type": "throughput_disappearance",
            "subsystem": "rtmps_ffmpeg",
            "evidence_refs": ["METRIC-012"],
            "review_priority_score": 0.62,
        },
        {
            "review_target_id": "network-target",
            "title": "Review target requires validation: network_transport",
            "core_target_type": "general_review",
            "subsystem": "network_transport",
            "evidence_refs": ["OPS-006"],
            "review_priority_score": 0.62,
        },
    ]

    resolution = resolve_canonical_review_graph_snapshot(
        store,
        bundle,
        legacy_review_targets=targets,
        legacy_summary={"review_targets": 2, "primary_review_targets": 0},
        persist_if_missing=True,
        created_by="pytest",
    )

    graph = resolution["canonical_review_graph"]
    assert graph["summary"]["validation_count"] == 1
    target = graph["validation_targets"][0]
    assert target["source_candidate_count"] == 2
    assert set(target["source_target_ids"]) == {"rtmps-target", "network-target"}
    assert target["canonical_review_unit"] == "transport_sender"


def test_canonical_observation_groups_split_general_mixed_evidence_families() -> None:
    candidates = [
        {
            "review_target_id": "network-general",
            "title": "Review target requires validation: general",
            "core_target_type": "general_review",
            "subsystem": "general",
            "suspected_issue": "Network connectivity interruptions to the upstream anchor service",
            "operational_mechanism": "tcp anchor observer timeout while probing Cloudflare",
            "evidence_refs": ["PATTERN-005"],
            "providers": ["provider-a"],
            "review_priority_score": 0.62,
        },
        {
            "review_target_id": "exception-general",
            "title": "Review target requires validation: general",
            "core_target_type": "general_review",
            "subsystem": "general",
            "suspected_issue": "Exception occurred during request processing",
            "operational_mechanism": "Traceback from request processing path",
            "evidence_refs": ["PATTERN-098", "PATTERN-099"],
            "providers": ["provider-b"],
            "review_priority_score": 0.61,
        },
        {
            "review_target_id": "memory-general",
            "title": "Review target requires validation: general",
            "core_target_type": "general_review",
            "subsystem": "general",
            "suspected_issue": "Memory pressure causing service instability",
            "operational_mechanism": "stream_v3_memory_critical_count crossed a critical threshold",
            "evidence_refs": ["PATTERN-1504"],
            "providers": ["provider-c"],
            "review_priority_score": 0.6,
        },
    ]

    merged, groups = merge_candidate_observations(candidates, evidence_sha256="a" * 64)

    assert len(merged) == 3
    assert len(groups) == 3
    assert {row["canonical_review_unit"] for row in merged} == {
        "resource_pressure",
        "runtime_exception",
        "transport_sender",
    }
