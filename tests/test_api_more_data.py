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
from ops_evidence_synthesis.synthesis.more_data import analyze_more_data_queries, filter_more_data_requests


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
