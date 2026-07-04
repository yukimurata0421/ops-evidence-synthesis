from __future__ import annotations

import json
from typing import Any

from ops_evidence_synthesis.gcp.bigquery import (
    BigQueryOps,
    _pipeline_event_row_to_dict,
    _pipeline_event_storage_row,
    _pipeline_run_row_to_dict,
    _pipeline_run_storage_row,
    _snapshot_row_to_dict,
    _snapshot_storage_row,
    _stored_review_target_set,
)


class _FakeBigQuery:
    class ScalarQueryParameter:
        def __init__(self, name: str, type_: str, value: Any) -> None:
            self.name = name
            self.type_ = type_
            self.value = value

    class QueryJobConfig:
        def __init__(self, query_parameters: list[Any]) -> None:
            self.query_parameters = query_parameters


def test_fetch_model_runs_bigquery_generation_gate_has_from_clause(monkeypatch) -> None:
    captured: dict[str, str] = {}
    store = BigQueryOps.__new__(BigQueryOps)
    store.bigquery = _FakeBigQuery
    store.project_id = "demo-project"
    store.location = "asia-northeast1"

    def fake_query(sql: str, params: list[Any] | None = None) -> list[Any]:
        captured["sql"] = sql
        return []

    monkeypatch.setattr(store, "_query", fake_query)

    assert store.fetch_model_runs("sha") == []
    assert "FROM (SELECT 1)" in captured["sql"]
    assert "SELECT TIMESTAMP('1970-01-01') AS created_at\n          WHERE" not in captured["sql"]


def test_bigquery_snapshot_pipeline_and_event_rows_round_trip_json_fields() -> None:
    snapshot_row = _snapshot_storage_row(
        {
            "evidence_sha256": "sha",
            "canonical_graph_sha256": "graph",
            "schema_version": "canonical_review_graph.v1",
            "arbitration_version": "arb.v1",
            "input_fingerprint_sha256": "fingerprint",
            "input_fingerprint_json": {"bundle_sha": "sha", "providers": ["gemini"]},
            "finding_title": "restart loop",
            "finding_impact": "notifications delayed",
            "primary_count": 1,
            "validation_count": 2,
            "created_at": "2026-01-01T00:00:00Z",
            "created_by": "public-demo",
            "snapshot_status": "persisted",
            "canonical_review_graph_json": {"nodes": [{"id": "rt-1"}]},
        }
    )
    pipeline_row = _pipeline_run_storage_row(
        {
            "pipeline_run_id": "run-1",
            "evidence_sha256": "sha",
            "operation": "fast_review",
            "status": "completed",
            "provider_total": 2,
            "provider_success": 1,
            "provider_failed": 1,
            "summary": {"provider_statuses": [{"provider": "gemini"}]},
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-01T00:01:00Z",
        }
    )
    event_row = _pipeline_event_storage_row(
        {
            "event_id": "event-1",
            "pipeline_run_id": "run-1",
            "evidence_sha256": "sha",
            "operation": "fast_review",
            "event_type": "step",
            "stage": "provider",
            "status": "provider_error",
            "provider_id": "gemma",
            "metadata": {"retryable": True, "denominator": "excluded"},
            "created_at": "2026-01-01T00:01:00Z",
        }
    )

    snapshot = _snapshot_row_to_dict(snapshot_row)
    pipeline = _pipeline_run_row_to_dict(pipeline_row)
    event = _pipeline_event_row_to_dict(event_row)
    target_set = _stored_review_target_set(
        [
            {"review_target_id": "primary", "class": "primary_candidate"},
            {"review_target_id": "validation", "class": "validation_target"},
        ]
    )

    assert snapshot["input_fingerprint_json"] == {"bundle_sha": "sha", "providers": ["gemini"]}
    assert snapshot["canonical_review_graph_json"] == {"nodes": [{"id": "rt-1"}]}
    assert pipeline["summary"] == {"provider_statuses": [{"provider": "gemini"}]}
    assert pipeline["provider_total"] == 2
    assert event["metadata"] == {"retryable": True, "denominator": "excluded"}
    assert target_set["summary"]["primary_review_targets"] == 1
    assert target_set["summary"]["validation_targets"] == 1
    assert target_set["summary"]["score_note"] == "Score is review priority, not truth probability."


def test_latest_pipeline_run_by_operations_uses_bound_operation_params(monkeypatch) -> None:
    captured: dict[str, Any] = {}
    store = BigQueryOps.__new__(BigQueryOps)
    store.bigquery = _FakeBigQuery
    store.project_id = "demo"
    store.location = "asia-northeast1"
    monkeypatch.setattr(store, "ensure_pipeline_tables", lambda: None)

    def fake_query(sql: str, params: list[Any] | None = None) -> list[dict[str, Any]]:
        captured["sql"] = sql
        captured["params"] = params or []
        return [
            {
                "pipeline_run_id": "run-latest",
                "evidence_sha256": "sha",
                "operation": "fast_review",
                "status": "completed",
                "summary_json": json.dumps({"provider_success": 1}),
                "provider_total": 2,
                "provider_success": 1,
                "provider_failed": 1,
                "created_at": "2026-01-01T00:00:00Z",
                "updated_at": "2026-01-01T00:02:00Z",
            }
        ]

    monkeypatch.setattr(store, "_query", fake_query)

    run = store.latest_pipeline_run_by_operations("sha", ["fast_review", "rescore"])

    assert run is not None
    assert run["pipeline_run_id"] == "run-latest"
    assert run["summary"] == {"provider_success": 1}
    assert "operation IN (@operation_0, @operation_1)" in captured["sql"]
    params = {param.name: param.value for param in captured["params"]}
    assert params == {
        "evidence_sha256": "sha",
        "operation_0": "fast_review",
        "operation_1": "rescore",
    }


def test_stored_review_targets_apply_latest_review_status_and_pending_filter(monkeypatch) -> None:
    captured_sql: list[str] = []
    store = BigQueryOps.__new__(BigQueryOps)
    store.bigquery = _FakeBigQuery
    store.project_id = "demo"
    store.location = "asia-northeast1"

    def fake_query(sql: str, params: list[Any] | None = None) -> list[dict[str, Any]]:
        captured_sql.append(sql)
        query_params = {param.name: param.value for param in params or []}
        if "FROM `demo.ops_synthesis.review_targets`" in sql:
            assert query_params == {"evidence_sha256": "sha", "limit": 5}
            return [
                {
                    "target_json": json.dumps(
                        {
                            "review_target_id": "rt-1",
                            "title": "need more log evidence",
                            "status": "pending",
                        }
                    ),
                    "status": "pending",
                }
            ]
        if "FROM `demo.ops_synthesis.reviews`" in sql:
            assert query_params == {"review_target_id": "rt-1"}
            return [
                {
                    "review_id": "review-1",
                    "review_target_id": "rt-1",
                    "decision": "needs_more_data",
                    "reason": "",
                    "human_note": "collect trace logs",
                    "reviewer": "yukimurata0421",
                    "created_at": "2026-01-01T00:05:00Z",
                    "generated_query_json": json.dumps({"sql": "SELECT 1"}),
                }
            ]
        raise AssertionError(sql)

    monkeypatch.setattr(store, "_query", fake_query)

    targets = store._list_stored_review_targets(
        evidence_sha256="sha",
        limit=5,
        pending_only=True,
    )

    assert targets == [
        {
            "review_target_id": "rt-1",
            "title": "need more log evidence",
            "status": "needs_more_data",
            "latest_review": {
                "review_id": "review-1",
                "review_target_id": "rt-1",
                "decision": "needs_more_data",
                "reason": "",
                "human_note": "collect trace logs",
                "reviewer": "yukimurata0421",
                "created_at": "2026-01-01T00:05:00Z",
                "generated_query": {"sql": "SELECT 1"},
                "status": "needs_more_data",
            },
        }
    ]
    assert "status IN ('pending', 'needs_more_data')" in captured_sql[0]
    assert "LIMIT @limit" in captured_sql[0]
