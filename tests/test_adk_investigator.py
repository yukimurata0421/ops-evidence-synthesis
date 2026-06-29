from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from ops_evidence_synthesis.agents.adk_investigator import (
    ADK_AGENT_NAME,
    build_adk_tool_contract_trace,
    trace_from_adk_events,
)


ROOT = Path(__file__).resolve().parents[1]


def _payload() -> dict:
    return {
        "schema_version": "precomputed_review_summary.v1",
        "evidence_sha256": "a" * 64,
        "generation": {
            "provider_mode": "real_api_vertex",
            "raw_log_policy": "not_uploaded",
        },
        "summary": {
            "log_count": 6506,
            "raw_log_policy": "not_uploaded",
        },
        "analysis_context": {
            "source_observations": [
                "Sanitized source context was attached with source_context_sha256=" + "b" * 64 + ".",
                "Source analysis was attached with analysis_sha256=" + "c" * 64 + ".",
            ],
        },
        "provider_statuses": [
            {"provider_id": "gemini-enterprise-agent-platform", "status": "ok", "schema_valid": True},
            {"provider_id": "qwen-agent-platform", "status": "ok", "schema_valid": True},
            {"provider_id": "glm-agent-platform", "status": "ok", "schema_valid": True},
        ],
        "review_graph_summary": {
            "targets_total": 2,
            "primary_promoted_count": 1,
            "validation_count": 1,
            "convergence_count": 1,
            "incident_baseline": "open",
        },
        "targets": [
            {
                "review_target_id": "target-1",
                "recommended_request_type": "user_impact_signal_query",
                "evidence_refs": ["LOG-001", "METRIC-001"],
                "missing_evidence": ["user impact signal"],
                "promotion": {"blocked_reason": "user_impact_unverified"},
            },
            {
                "review_target_id": "target-2",
                "recommended_request_type": "process_state_query",
                "evidence_refs": ["LOG-002"],
                "missing_evidence": [],
                "promotion": {},
            },
        ],
    }


def test_adk_tool_contract_trace_records_real_gates_without_raw_payload() -> None:
    trace = build_adk_tool_contract_trace(_payload())

    tools = [step["tool"] for step in trace]
    assert tools == [
        "freeze_evidence_bundle",
        "attach_sanitized_source_context",
        "run_cross_check_providers",
        "validate_citations",
        "compute_review_targets",
        "arbitrate_review_gate",
        "request_more_evidence",
        "draft_system_profile",
        "deliver_read_only_review",
    ]
    assert all(step["trace_source"] == "adk_tool_contract" for step in trace)
    assert all(step["adk_agent_name"] == ADK_AGENT_NAME for step in trace)
    provider_step = next(step for step in trace if step["tool"] == "run_cross_check_providers")
    assert provider_step["output"]["schema_valid_provider_count"] == 3
    gate_step = next(step for step in trace if step["tool"] == "arbitrate_review_gate")
    assert gate_step["status"] == "human_gate"
    assert gate_step["output"]["requires_human_review"] is True
    rendered = json.dumps(trace, sort_keys=True)
    assert "Authorization:" not in rendered
    assert "Bearer " not in rendered


def test_trace_from_adk_events_extracts_function_call_and_response() -> None:
    events = [
        {
            "author": ADK_AGENT_NAME,
            "content": {
                "parts": [
                    {
                        "function_call": {
                            "name": "freeze_evidence_bundle",
                            "args": {"evidence_sha256": "a" * 64, "log_count": 10},
                        }
                    }
                ]
            },
        },
        {
            "author": ADK_AGENT_NAME,
            "content": {
                "parts": [
                    {
                        "function_response": {
                            "name": "freeze_evidence_bundle",
                            "response": {"status": "completed", "summary": "Evidence fixed."},
                        }
                    }
                ]
            },
        },
    ]

    trace = trace_from_adk_events(events)

    assert [step["adk_event_type"] for step in trace] == ["function_call", "function_response"]
    assert trace[0]["tool"] == "freeze_evidence_bundle"
    assert trace[1]["summary"] == "Evidence fixed."


def test_adk_trace_cli_exports_trace(tmp_path: Path) -> None:
    payload_path = tmp_path / "payload.json"
    output_path = tmp_path / "trace.json"
    payload_path.write_text(json.dumps(_payload(), sort_keys=True), encoding="utf-8")

    subprocess.run(
        [
            sys.executable,
            "-m",
            "ops_evidence_synthesis.cli",
            "adk-trace",
            "--precomputed-payload",
            str(payload_path),
            "--out",
            str(output_path),
        ],
        cwd=ROOT,
        check=True,
    )

    exported = json.loads(output_path.read_text(encoding="utf-8"))
    assert exported["schema_version"] == "adk_trace_export.v1"
    assert exported["trace"][0]["tool"] == "freeze_evidence_bundle"
