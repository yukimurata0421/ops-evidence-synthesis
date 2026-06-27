from __future__ import annotations

import pytest

pytest.importorskip("fastapi")

from ops_evidence_synthesis.api import (
    _bundle_with_more_data,
    _child_evidence_chain,
    _more_data_evidence_delta,
    _more_data_refresh_summary,
    _more_data_request_statuses,
)
from ops_evidence_synthesis.pipeline_progress import finish_pipeline_run, record_pipeline_event, start_pipeline_run
from ops_evidence_synthesis.storage.sqlite_store import SQLiteStore
from ops_evidence_synthesis.synthesis.more_data import analyze_more_data_queries, filter_more_data_requests
from ops_evidence_synthesis.synthesis.review_arbitration import resolve_canonical_review_graph_snapshot


def test_more_data_refresh_summary_reports_child_bundle_chain() -> None:
    query = {
        "queries": [
            {
                "request_id": "process_state_query",
                "profile_request_id": "ffmpeg_state_query",
                "request_type": "process_state",
                "need": "process_state",
                "preview_count": 12,
                "sql": "select 1",
            },
            {
                "request_id": "deployment_correlation_query",
                "request_type": "deployment_correlation",
                "need": "deployment_correlation",
                "preview_count": 0,
                "sql": "select 2",
            },
        ]
    }

    statuses = _more_data_request_statuses(query)
    summary = _more_data_refresh_summary(
        query,
        request_statuses=statuses,
        run_models=True,
        pipeline_result={"model_run_count": 1, "claim_count": 3, "review_target_count": 1},
        evidence_delta={"added_evidence_ref_count": 2},
    )
    chain = _child_evidence_chain(
        {"evidence_sha256": "parent"},
        {"evidence_sha256": "child", "profile": {"profile_id": "stream_v3"}},
        review_target_id="rt-1",
        proposition_id="prop-1",
        refresh_summary=summary,
    )

    assert statuses[0]["status"] == "preview_ready"
    assert statuses[0]["rows"] == 12
    assert statuses[1]["status"] == "unavailable"
    assert summary["review_target_status_transition"] == "needs_more_data -> evidence_collected"
    assert summary["new_evidence_types"] == ["process_state"]
    assert summary["evidence_delta"]["added_evidence_ref_count"] == 2
    assert summary["model_rerun"]["completed"] is True
    assert chain["parent_evidence_sha256"] == "parent"
    assert chain["generated_child_evidence_sha256"] == "child"
    assert chain["status"] == "needs_more_data -> evidence_collected"
    assert chain["evidence_delta"]["added_evidence_ref_count"] == 2


def test_more_data_request_filter_and_artifact_analysis_enter_child_bundle() -> None:
    requests = [
        {"request_id": "job_definition_query", "request_type": "job_definition", "need": "job_definition"},
        {"request_id": "installed_artifact_query", "request_type": "installed_artifact", "need": "installed_artifact"},
    ]
    assert [row["request_id"] for row in filter_more_data_requests(requests, ["job_definition"])] == [
        "job_definition_query"
    ]

    queries = [
        {
            **requests[0],
            "preview_count": 1,
            "preview_rows": [
                {
                    "timestamp": "2026-06-16T00:00:00Z",
                    "service": "amazon-notify",
                    "severity": "INFO",
                    "message_sanitized": "amazon-notify.service ExecStart=/usr/bin/python3 /opt/amazon-notify/watchdog_restart_main.py",
                    "message_template": "unit definition",
                    "error_type": "",
                    "raw_log_sha256": "a",
                }
            ],
        },
        {
            **requests[1],
            "preview_count": 1,
            "preview_rows": [
                {
                    "timestamp": "2026-06-16T00:01:00Z",
                    "service": "amazon-notify",
                    "severity": "ERROR",
                    "message_sanitized": "python3: can't open file '/opt/amazon-notify/watchdog_restart_main.py': [Errno 2] No such file or directory",
                    "message_template": "missing artifact",
                    "error_type": "job_configuration_mismatch",
                    "raw_log_sha256": "b",
                }
            ],
        },
    ]
    analysis = analyze_more_data_queries(queries)
    bundle = _bundle_with_more_data(
        {
            "schema_version": "ops-evidence-bundle/v1",
            "evidence_sha256": "parent",
            "service": "amazon-notify",
            "environment": "amazon-notify",
            "evidence_refs": {},
            "logs": [],
            "operational_evidence": [],
        },
        {"proposition_id": "prop-1", "subsystem": "job_configuration", "evidence_sha256": "parent"},
        {
            "sql": "select 1",
            "subsystem": "job_configuration",
            "queries": queries,
            "request_analysis": analysis,
            "next_evidence_requests": requests,
        },
        review_target={"review_target_id": "rt-1"},
    )
    evidence_delta = bundle["more_data"]["evidence_delta"]

    analysis_refs = {
        evidence_id: value
        for evidence_id, value in bundle["evidence_refs"].items()
        if value.get("type") == "more_data_analysis"
    }
    assert any(row["request_type"] == "artifact_comparison" for row in analysis)
    assert any("Configured job definition paths match" in row["summary"] for row in bundle["operational_evidence"])
    assert any(value["request_id"] == "job_definition_artifact_comparison" for value in analysis_refs.values())
    assert evidence_delta["added_log_count"] == 2
    assert evidence_delta["added_analysis_count"] >= 2
    assert evidence_delta["added_evidence_ref_count"] >= 4
    assert "job_definition" in evidence_delta["collected_request_types"]
    assert bundle["lineage"]["relationship"] == "more_data_child"
    assert bundle["lineage"]["source_review_target_id"] == "rt-1"
    assert bundle["review_target_history"][0]["event"] == "more_data_child_bundle_created"
    assert _more_data_evidence_delta(
        {"evidence_refs": {}},
        bundle,
        {"queries": queries},
    )["added_evidence_ref_count"] == len(bundle["evidence_refs"])


def test_more_data_child_bundle_rescores_parent_graph_and_promotion(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "more_data_rescore.sqlite3")
    parent = {
        "schema_version": "ops-evidence-bundle/v1",
        "evidence_sha256": "parent-sha",
        "service": "amazon-notify",
        "environment": "prod",
        "evidence_refs": {
            "LOG-1": {
                "evidence_id": "LOG-1",
                "type": "runtime_log",
                "summary": "amazon-notify.service restart loop observed",
            }
        },
        "logs": [],
        "operational_evidence": [],
        "signals": [{"signal_type": "restart_loop", "core_target_type": "job_configuration_mismatch"}],
    }
    store.insert_bundle(parent)
    before_synthesis = {
        "schema_version": "multi_ai_synthesis.v1",
        "evidence_sha256": "parent-sha",
        "provider_count": 3,
        "successful_provider_count": 3,
        "agreement_groups": [],
        "disagreement_groups": [
            {
                "group_id": "rt-runtime",
                "core_target_type": "job_configuration_mismatch",
                "subsystem": "runtime_recovery",
                "providers": ["gemini"],
                "provider_count": 1,
                "evidence_refs": ["LOG-1"],
                "missing_evidence": ["critical outcome metric"],
            }
        ],
        "validation_targets": [
            {
                "group_id": "rt-runtime",
                "title": "Restart loop requires validation",
                "core_target_type": "job_configuration_mismatch",
                "subsystem": "runtime_recovery",
                "providers": ["gemini"],
                "provider_count": 1,
                "evidence_refs": ["LOG-1"],
                "review_priority_score": 0.69,
                "missing_evidence": ["critical outcome metric"],
            }
        ],
    }
    before = resolve_canonical_review_graph_snapshot(
        store,
        parent,
        multi_ai_synthesis=before_synthesis,
        persist_if_missing=True,
        created_by="pytest-before-more-data",
    )
    before_graph = before["canonical_review_graph"]
    before_target = before_graph["validation_targets"][0]
    assert before["canonical_graph_status"] == "persisted"
    assert before_graph["summary"]["primary_count"] == 0
    assert "user_impact_unverified" in before_target["promotion_blocked_reasons"]

    query = {
        "sql": "select user impact rows",
        "subsystem": "runtime_recovery",
        "queries": [
            {
                "request_id": "user_impact_signal_query",
                "request_type": "user_impact_signal",
                "need": "user impact",
                "preview_count": 2,
                "sql": "select notification delivery failures",
                "preview_rows": [
                    {
                        "timestamp": "2026-06-26T23:00:00Z",
                        "service": "amazon-notify",
                        "severity": "ERROR",
                        "message_sanitized": "notification_not_delivered count=47 during restart loop",
                        "message_template": "notification_not_delivered",
                        "error_type": "notification_not_delivered",
                        "raw_log_sha256": "safe-impact-1",
                    },
                    {
                        "timestamp": "2026-06-26T23:01:00Z",
                        "service": "amazon-notify",
                        "severity": "ERROR",
                        "message_sanitized": "notification_not_delivered count=49 after service restart",
                        "message_template": "notification_not_delivered",
                        "error_type": "notification_not_delivered",
                        "raw_log_sha256": "safe-impact-2",
                    },
                ],
            }
        ],
    }
    query["request_analysis"] = analyze_more_data_queries(query["queries"])
    child = _bundle_with_more_data(
        parent,
        {"proposition_id": "prop-runtime", "subsystem": "runtime_recovery", "evidence_sha256": "parent-sha"},
        query,
        review_target=before_target,
    )
    store.insert_bundle(child)
    more_data_refs = [
        evidence_id
        for evidence_id, ref in child["evidence_refs"].items()
        if isinstance(ref, dict) and str(ref.get("type") or "").startswith("more_data")
    ]
    refreshed_parent = {**child, "evidence_sha256": "parent-sha", "child_evidence_sha256": child["evidence_sha256"]}
    after_synthesis = {
        "schema_version": "multi_ai_synthesis.v1",
        "evidence_sha256": "parent-sha",
        "provider_count": 3,
        "successful_provider_count": 3,
        "claim_groups": [
            {
                "group_id": "rt-runtime",
                "core_target_type": "job_configuration_mismatch",
                "subsystem": "runtime_recovery",
                "providers": ["gemini", "gpt-oss", "mistral"],
                "provider_count": 3,
                "evidence_refs": ["LOG-1", *more_data_refs],
            }
        ],
        "agreement_groups": [
            {
                "group_id": "rt-runtime",
                "title": "Notifier restart loop has user-visible delivery impact",
                "core_target_type": "job_configuration_mismatch",
                "subsystem": "runtime_recovery",
                "providers": ["gemini", "gpt-oss", "mistral"],
                "provider_count": 3,
                "evidence_refs": ["LOG-1", *more_data_refs],
                "impact_summary": "notification_not_delivered user impact is visible during the restart loop.",
                "recommended_validation": "process_state",
            }
        ],
        "primary_candidates": [
            {
                "group_id": "rt-runtime",
                "title": "Notifier restart loop has user-visible delivery impact",
                "core_target_type": "job_configuration_mismatch",
                "subsystem": "runtime_recovery",
                "providers": ["gemini", "gpt-oss", "mistral"],
                "provider_count": 3,
                "evidence_refs": ["LOG-1", *more_data_refs],
                "review_priority_score": 0.84,
                "impact_summary": "notification_not_delivered user impact is visible during the restart loop.",
            }
        ],
    }

    refresh_summary = _more_data_refresh_summary(
        query,
        request_statuses=_more_data_request_statuses(query),
        run_models=True,
        pipeline_result={"model_run_count": 3, "claim_count": 1, "review_target_count": 1},
        evidence_delta=child["more_data"]["evidence_delta"],
    )
    run_id = start_pipeline_run(
        store,
        evidence_sha256="parent-sha",
        operation="more_data_refresh",
        summary={"review_target_id": before_target["review_target_id"]},
    )
    record_pipeline_event(
        store,
        pipeline_run_id=run_id,
        evidence_sha256="parent-sha",
        operation="more_data_refresh",
        step_key="more_data_requested",
        status="completed",
        message="Human-gated target requested user-impact evidence.",
        metadata={"review_target_id": before_target["review_target_id"]},
    )
    record_pipeline_event(
        store,
        pipeline_run_id=run_id,
        evidence_sha256="parent-sha",
        operation="more_data_refresh",
        step_key="child_bundle_created",
        status="completed",
        message="Child Evidence Bundle added user-impact rows.",
        metadata={"child_evidence_sha256": child["evidence_sha256"]},
    )
    store.record_more_data_result(before_target["review_target_id"], child["evidence_sha256"], refresh_summary)
    after = resolve_canonical_review_graph_snapshot(
        store,
        refreshed_parent,
        multi_ai_synthesis=after_synthesis,
        persist_if_stale=True,
        created_by="pytest-more-data-rescore",
    )
    after_graph = after["canonical_review_graph"]
    record_pipeline_event(
        store,
        pipeline_run_id=run_id,
        evidence_sha256="parent-sha",
        operation="more_data_refresh",
        step_key="model_rerun_completed",
        status="completed",
        message="Parent canonical graph was re-scored after child evidence.",
        metadata={"canonical_graph_sha256": after["canonical_graph_sha256"]},
    )
    finish_pipeline_run(
        store,
        pipeline_run_id=run_id,
        evidence_sha256="parent-sha",
        operation="more_data_refresh",
        status="succeeded",
        message="More-data loop completed.",
        metadata={"child_evidence_sha256": child["evidence_sha256"]},
    )

    assert child["lineage"]["relationship"] == "more_data_child"
    assert child["parent_evidence_sha256"] == "parent-sha"
    assert after["canonical_graph_status"] == "persisted"
    assert after["previous_snapshot"]["canonical_graph_sha256"] == before["canonical_graph_sha256"]
    assert after["canonical_graph_sha256"] != before["canonical_graph_sha256"]
    assert after_graph["summary"]["primary_count"] == 1
    after_target = after_graph["primary_targets"][0]
    assert after_target["promotion_score"] >= 0.75
    assert after_target["review_priority_score"] > before_target["review_priority_score"]
    assert "user_impact_unverified" not in after_target["promotion_blocked_reasons"]
    decision = after_graph["promotion_decisions"][0]
    assert decision["final_class"] == "primary_candidate"
    events = store.list_pipeline_events(run_id)
    assert [event["step_key"] for event in events][-4:] == [
        "more_data_requested",
        "child_bundle_created",
        "model_rerun_completed",
        "completed",
    ]
