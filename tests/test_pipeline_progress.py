from __future__ import annotations

from ops_evidence_synthesis.pipeline_progress import (
    analysis_pipeline_status_from_store,
    finish_pipeline_run,
    pipeline_status_from_store,
    record_pipeline_event,
    start_pipeline_run,
)
from ops_evidence_synthesis.storage.sqlite_store import SQLiteStore


REQUESTED_STATES = {
    "uploaded",
    "validated",
    "safety_passed",
    "providers_scheduled",
    "provider_completed",
    "provider_failed",
    "parse_failed",
    "schema_validated",
    "arbitration_completed",
    "review_targets_persisted",
    "planner_generated",
    "waiting_human_answers",
    "refined_plan_generated",
    "child_bundle_required",
    "completed",
    "blocked",
}

REQUESTED_BLOCKING_REASONS = {
    "blocked_by_safety_preflight",
    "provider_not_configured",
    "provider_timeout",
    "parse_failed",
    "schema_invalid",
    "no_claims_extracted",
    "no_planner_answers",
    "human_input_required",
    "child_bundle_missing",
}


def test_sqlite_pipeline_progress_lifecycle(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "pipeline.sqlite3")
    store.init_schema()

    run_id = start_pipeline_run(
        store,
        evidence_sha256="sha-progress",
        operation="bundle_upload",
        summary={"bundle_type": "sanitized_evidence_bundle"},
    )
    record_pipeline_event(
        store,
        pipeline_run_id=run_id,
        evidence_sha256="sha-progress",
        operation="bundle_upload",
        step_key="bundle_received",
        status="completed",
        message="Bundle received.",
    )
    status = pipeline_status_from_store(store, evidence_sha256="sha-progress", pipeline_run_id=run_id)
    assert status["pipeline_run_id"] == run_id
    assert status["status"] == "running"
    assert status["completed_steps"] == 1
    assert status["blocking_reason"] == ""
    assert status["provider_total"] == 0
    assert status["steps"][0]["status"] == "completed"

    record_pipeline_event(
        store,
        pipeline_run_id=run_id,
        evidence_sha256="sha-progress",
        operation="bundle_upload",
        step_key="bundle_validated",
        status="completed",
        message="Validation passed.",
    )
    record_pipeline_event(
        store,
        pipeline_run_id=run_id,
        evidence_sha256="sha-progress",
        operation="bundle_upload",
        step_key="bundle_persisted",
        status="completed",
        message="Bundle persisted.",
    )
    finish_pipeline_run(
        store,
        pipeline_run_id=run_id,
        evidence_sha256="sha-progress",
        operation="bundle_upload",
        status="succeeded",
        message="Upload complete.",
    )

    final = store.get_pipeline_status(evidence_sha256="sha-progress")
    assert final["pipeline_run_id"] == run_id
    assert final["status"] == "succeeded"
    assert final["progress_percent"] == 100
    assert final["completed_steps"] == final["total_steps"] == 3
    assert [step["step_key"] for step in final["steps"][:3]] == [
        "bundle_received",
        "bundle_validated",
        "bundle_persisted",
    ]
    assert final["events"][-1]["step_key"] == "completed"
    assert final["blocking_reason"] == ""
    assert REQUESTED_STATES <= set(final["known_states"])
    assert REQUESTED_BLOCKING_REASONS <= set(final["known_blocking_reasons"])
    assert final["canonical_state"] == "completed"
    assert {item["state"] for item in final["state_timeline"]} >= {"uploaded", "validated", "completed"}


def test_analysis_pipeline_status_prefers_model_run_over_later_planner_run(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "pipeline-analysis-preferred.sqlite3")
    store.init_schema()

    analysis_run_id = start_pipeline_run(
        store,
        evidence_sha256="sha-analysis-preferred",
        operation="multi_ai",
        summary={"provider_count": 2},
    )
    record_pipeline_event(
        store,
        pipeline_run_id=analysis_run_id,
        evidence_sha256="sha-analysis-preferred",
        operation="multi_ai",
        step_key="providers_completed",
        status="completed",
        message="gemini finished.",
        metadata={"provider": "gemini", "status": "ok", "schema_valid": True},
    )
    record_pipeline_event(
        store,
        pipeline_run_id=analysis_run_id,
        evidence_sha256="sha-analysis-preferred",
        operation="multi_ai",
        step_key="providers_completed",
        status="completed",
        message="mistral finished.",
        metadata={"provider": "mistral", "status": "ok", "schema_valid": True},
    )
    finish_pipeline_run(
        store,
        pipeline_run_id=analysis_run_id,
        evidence_sha256="sha-analysis-preferred",
        operation="multi_ai",
        status="succeeded",
        message="Analysis complete.",
        metadata={"provider_total": 2, "provider_success": 2, "review_target_count": 4},
    )

    planner_run_id = start_pipeline_run(
        store,
        evidence_sha256="sha-analysis-preferred",
        operation="evidence_request_plan",
        summary={"request_count": 3},
    )
    finish_pipeline_run(
        store,
        pipeline_run_id=planner_run_id,
        evidence_sha256="sha-analysis-preferred",
        operation="evidence_request_plan",
        status="succeeded",
        message="Planner complete.",
        metadata={"request_count": 3},
    )

    planner = pipeline_status_from_store(
        store,
        evidence_sha256="sha-analysis-preferred",
        pipeline_run_id=planner_run_id,
    )
    analysis = analysis_pipeline_status_from_store(store, evidence_sha256="sha-analysis-preferred")

    assert planner["pipeline_run_id"] == planner_run_id
    assert planner["operation"] == "evidence_request_plan"
    assert planner["provider_total"] == 0
    assert analysis["pipeline_run_id"] == analysis_run_id
    assert analysis["operation"] == "multi_ai"
    assert analysis["provider_total"] == 2
    assert analysis["provider_success"] == 2
    assert analysis["review_target_count"] == 4


def test_sqlite_pipeline_progress_failure_closes_run(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "pipeline-failed.sqlite3")
    store.init_schema()

    run_id = start_pipeline_run(
        store,
        evidence_sha256="sha-failed",
        operation="model_stage",
        summary={"provider_count": 1},
    )
    record_pipeline_event(
        store,
        pipeline_run_id=run_id,
        evidence_sha256="sha-failed",
        operation="model_stage",
        step_key="providers_scheduled",
        status="running",
        message="Provider scheduled.",
    )
    finish_pipeline_run(
        store,
        pipeline_run_id=run_id,
        evidence_sha256="sha-failed",
        operation="model_stage",
        status="failed",
        message="Provider timeout.",
    )

    status = store.get_pipeline_status(evidence_sha256="sha-failed")
    assert status["status"] == "failed"
    assert status["error_message"] == "Provider timeout."
    assert status["blocking_reason"] == "provider_timeout"
    assert status["latest_event"]["step_key"] == "failed"


def test_sqlite_pipeline_progress_tracks_provider_frontier_and_reason(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "pipeline-frontier.sqlite3")
    store.init_schema()

    run_id = start_pipeline_run(
        store,
        evidence_sha256="sha-frontier",
        operation="multi_ai",
        summary={"provider_count": 3},
    )
    record_pipeline_event(
        store,
        pipeline_run_id=run_id,
        evidence_sha256="sha-frontier",
        operation="multi_ai",
        step_key="providers_scheduled",
        status="running",
        message="3 provider run(s) scheduled.",
        metadata={"provider_count": 3},
    )
    record_pipeline_event(
        store,
        pipeline_run_id=run_id,
        evidence_sha256="sha-frontier",
        operation="multi_ai",
        step_key="providers_completed",
        status="completed",
        message="gemini finished.",
        metadata={
            "provider": "gemini",
            "artifact_id": "run-gemini",
                "model_input_sha256": "input-sha",
                "raw_output_sha256": "raw-gemini",
                "status": "ok",
                "schema_valid": True,
            },
        )
    record_pipeline_event(
        store,
        pipeline_run_id=run_id,
        evidence_sha256="sha-frontier",
        operation="multi_ai",
        step_key="providers_completed",
        status="skipped",
        message="claude was not configured.",
        metadata={
            "provider": "claude",
            "artifact_id": "run-claude",
            "status": "skipped_not_configured",
        },
    )
    record_pipeline_event(
        store,
        pipeline_run_id=run_id,
        evidence_sha256="sha-frontier",
        operation="multi_ai",
        step_key="providers_completed",
        status="timeout",
        message="mistral timeout.",
        metadata={
            "provider": "mistral",
            "artifact_id": "run-mistral",
            "status": "timeout",
        },
    )

    status = store.get_pipeline_status(evidence_sha256="sha-frontier")
    assert status["status"] == "failed"
    assert status["blocking_reason"] == "provider_timeout"
    assert status["provider_total"] == 3
    assert status["provider_success"] == 1
    assert status["provider_skipped"] == 1
    assert status["provider_failed"] == 1
    assert status["latest_event"]["provider_id"] == "mistral"
    assert status["latest_event"]["artifact_id"] == "run-mistral"
    assert status["latest_event"]["reason_code"] == "provider_timeout"
    assert status["canonical_state"] == "provider_failed"
    assert "provider_timeout" in status["active_reasons"]
    assert {item["state"] for item in status["state_timeline"]} >= {
        "providers_scheduled",
        "provider_completed",
        "schema_validated",
        "provider_failed",
    }


def test_pipeline_status_does_not_succeed_when_all_providers_failed(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "pipeline-all-failed.sqlite3")
    store.init_schema()

    run_id = start_pipeline_run(
        store,
        evidence_sha256="sha-all-failed",
        operation="multi_ai",
        summary={"provider_count": 1},
    )
    record_pipeline_event(
        store,
        pipeline_run_id=run_id,
        evidence_sha256="sha-all-failed",
        operation="multi_ai",
        step_key="providers_completed",
        status="failed",
        message="claude failed.",
        metadata={
            "provider": "claude",
            "artifact_id": "run-claude",
            "status": "failed",
            "reason_code": "provider_failed",
        },
    )
    finish_pipeline_run(
        store,
        pipeline_run_id=run_id,
        evidence_sha256="sha-all-failed",
        operation="multi_ai",
        status="succeeded",
        message="Multi-provider analysis completed.",
    )

    status = store.get_pipeline_status(evidence_sha256="sha-all-failed", pipeline_run_id=run_id)
    assert status["status"] == "failed"
    assert status["blocking_reason"] == "provider_failed"
    assert status["provider_total"] == 1
    assert status["provider_success"] == 0
    assert status["provider_failed"] == 1


def test_pipeline_status_counts_schema_invalid_provider_output_as_failed(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "pipeline-schema-invalid.sqlite3")
    store.init_schema()

    run_id = start_pipeline_run(
        store,
        evidence_sha256="sha-schema-invalid",
        operation="multi_ai",
        summary={"provider_count": 2},
    )
    record_pipeline_event(
        store,
        pipeline_run_id=run_id,
        evidence_sha256="sha-schema-invalid",
        operation="multi_ai",
        step_key="providers_completed",
        status="completed",
        message="gemini finished.",
        metadata={
            "provider": "gemini",
            "artifact_id": "run-gemini",
            "status": "ok",
            "schema_valid": True,
        },
    )
    record_pipeline_event(
        store,
        pipeline_run_id=run_id,
        evidence_sha256="sha-schema-invalid",
        operation="multi_ai",
        step_key="providers_completed",
        status="completed",
        message="gpt-oss finished with invalid schema.",
        metadata={
            "provider": "gpt-oss",
            "artifact_id": "run-gpt-oss",
            "status": "ok",
            "schema_valid": False,
            "reason_code": "schema_invalid",
        },
    )
    finish_pipeline_run(
        store,
        pipeline_run_id=run_id,
        evidence_sha256="sha-schema-invalid",
        operation="multi_ai",
        status="succeeded",
        message="Multi-provider analysis completed.",
    )

    status = store.get_pipeline_status(evidence_sha256="sha-schema-invalid", pipeline_run_id=run_id)
    assert status["status"] == "succeeded"
    assert status["blocking_reason"] == ""
    assert status["provider_total"] == 2
    assert status["provider_success"] == 1
    assert status["provider_failed"] == 1
    assert "schema_invalid" in status["active_reasons"]
    assert {item["state"] for item in status["state_timeline"]} >= {"provider_failed", "schema_validated"}


def test_pipeline_status_tracks_claim_and_child_bundle_blocking_reasons(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "pipeline-reasons.sqlite3")
    store.init_schema()

    no_claims_run_id = start_pipeline_run(
        store,
        evidence_sha256="sha-no-claims",
        operation="synthesis",
        summary={},
    )
    record_pipeline_event(
        store,
        pipeline_run_id=no_claims_run_id,
        evidence_sha256="sha-no-claims",
        operation="synthesis",
        step_key="claims_routed",
        status="completed",
        message="0 claims and 0 propositions routed.",
        metadata={"claim_count": 0, "proposition_count": 0},
    )
    no_claims = store.get_pipeline_status(evidence_sha256="sha-no-claims", pipeline_run_id=no_claims_run_id)
    assert "no_claims_extracted" in no_claims["active_reasons"]
    assert no_claims["events"][-1]["reason_code"] == "no_claims_extracted"

    child_run_id = start_pipeline_run(
        store,
        evidence_sha256="sha-child",
        operation="more_data_refresh",
        summary={},
    )
    record_pipeline_event(
        store,
        pipeline_run_id=child_run_id,
        evidence_sha256="sha-child",
        operation="more_data_refresh",
        step_key="more_data_requested",
        status="completed",
        message="More data decision recorded.",
    )
    record_pipeline_event(
        store,
        pipeline_run_id=child_run_id,
        evidence_sha256="sha-child",
        operation="more_data_refresh",
        step_key="child_bundle_created",
        status="failed",
        message="Child bundle was missing.",
    )
    child_status = store.get_pipeline_status(evidence_sha256="sha-child", pipeline_run_id=child_run_id)
    assert child_status["canonical_state"] == "child_bundle_required"
    assert child_status["blocking_reason"] == "child_bundle_missing"
    assert "child_bundle_missing" in child_status["active_reasons"]
    assert {item["state"] for item in child_status["state_timeline"]} >= {"child_bundle_required"}


def test_pipeline_status_tracks_planner_waiting_and_refined_states(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "pipeline-planner.sqlite3")
    store.init_schema()

    draft_run_id = start_pipeline_run(
        store,
        evidence_sha256="sha-planner-draft",
        operation="evidence_request_plan",
        summary={"planner_answers_supplied": False},
    )
    record_pipeline_event(
        store,
        pipeline_run_id=draft_run_id,
        evidence_sha256="sha-planner-draft",
        operation="evidence_request_plan",
        step_key="planner_answers_received",
        status="needs_input",
        message="Planner answers missing.",
        metadata={"reason_code": "no_planner_answers"},
    )
    finish_pipeline_run(
        store,
        pipeline_run_id=draft_run_id,
        evidence_sha256="sha-planner-draft",
        operation="evidence_request_plan",
        status="needs_input",
        message="Human-question answers are still required.",
        metadata={"reason_code": "human_input_required"},
    )
    draft = store.get_pipeline_status(evidence_sha256="sha-planner-draft", pipeline_run_id=draft_run_id)
    assert draft["canonical_state"] == "waiting_human_answers"
    assert {"no_planner_answers", "human_input_required"} <= set(draft["active_reasons"])

    refined_run_id = start_pipeline_run(
        store,
        evidence_sha256="sha-planner-refined",
        operation="evidence_request_plan",
        summary={"planner_answers_supplied": True},
    )
    record_pipeline_event(
        store,
        pipeline_run_id=refined_run_id,
        evidence_sha256="sha-planner-refined",
        operation="evidence_request_plan",
        step_key="plan_generated",
        status="completed",
        message="Evidence request plan generated.",
        metadata={"planner_answers_supplied": True},
    )
    refined = store.get_pipeline_status(evidence_sha256="sha-planner-refined", pipeline_run_id=refined_run_id)
    assert {item["state"] for item in refined["state_timeline"]} >= {"refined_plan_generated"}


def test_sqlite_pipeline_progress_tracks_child_bundle_count(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "pipeline-child.sqlite3")
    store.init_schema()

    run_id = start_pipeline_run(
        store,
        evidence_sha256="sha-parent",
        operation="more_data_refresh",
        summary={"review_target_id": "rt-1"},
    )
    record_pipeline_event(
        store,
        pipeline_run_id=run_id,
        evidence_sha256="sha-parent",
        operation="more_data_refresh",
        step_key="child_bundle_created",
        status="completed",
        message="Child bundle created.",
        metadata={"child_evidence_sha256": "sha-child"},
    )

    status = store.get_pipeline_status(evidence_sha256="sha-parent")
    assert status["child_bundle_count"] == 1
    assert status["latest_event"]["output_sha256"] == "sha-child"
