from __future__ import annotations

from ops_evidence_synthesis.ai.providers import build_provider_list
from ops_evidence_synthesis.ai.prompts import (
    claim_result_response_schema,
    compact_bundle_for_model,
    root_cause_prompt,
)
from ops_evidence_synthesis.ai.provider_registry import build_multi_ai_providers, provider_infos
from ops_evidence_synthesis.ai.vertex import VertexGeminiProvider
from ops_evidence_synthesis.profiles import available_profile_ids, load_profile


def test_provider_selection_defaults_to_local_providers() -> None:
    providers = build_provider_list([])

    assert [provider.provider for provider in providers] == [
        "gemini-local",
        "gemini-local",
        "external-local",
    ]


def test_provider_selection_supports_vertex_gemini(monkeypatch) -> None:
    monkeypatch.setenv("OES_VERTEX_PROJECT", "ops-evidence-synthesis")

    providers = build_provider_list(["gemini"])

    assert len(providers) == 1
    assert isinstance(providers[0], VertexGeminiProvider)
    assert providers[0].provider == "gemini-enterprise-agent-platform"
    assert providers[0].project_id == "ops-evidence-synthesis"
    assert providers[0].model_name == "gemini-3.1-flash-lite"
    assert providers[0].thinking_level == "medium"


def test_provider_registry_can_disable_provider_by_policy(monkeypatch) -> None:
    monkeypatch.setenv("OES_ENABLE_REAL_AI", "1")
    monkeypatch.setenv("OES_VERTEX_PROJECT", "ops-evidence-synthesis")
    monkeypatch.setenv("OES_DISABLED_PROVIDERS", "gemini")

    providers = build_multi_ai_providers(["gemini"], mode="real_or_skip")
    infos = {row["provider_id"]: row for row in provider_infos()}
    response = providers[0].run({})

    assert infos["gemini-enterprise-agent-platform"]["status"] == "disabled_by_policy"
    assert response.status == "skipped_not_configured"
    assert "disabled_by_policy" in response.raw_output


def test_root_cause_prompt_and_schema_are_generic_not_stream_v3_only() -> None:
    bundle = {
        "evidence_sha256": "e" * 64,
        "service": "generic-service",
        "environment": "prod",
        "evidence_refs": {},
    }

    prompt = root_cause_prompt(bundle)
    schema = claim_result_response_schema()
    subsystem_enum = schema["properties"]["claims"]["items"]["properties"]["subsystem"]["enum"]

    assert "arbitrary sanitized JSONL" in prompt
    assert "context only; they are not evidence" in prompt
    assert "Do not invent log source names, metric names, state file paths, endpoints, or collector commands." in prompt
    assert "Do not translate missing evidence into ad hoc local commands" in prompt
    assert "For next_data_needed claims, set temporary_action, permanent_action, and required_authority to empty strings" in prompt
    assert "job_configuration" in subsystem_enum
    assert "downstream_dependency" in subsystem_enum
    assert "database_connection_pool" in subsystem_enum
    assert "service_liveness" in subsystem_enum
    assert "traffic" in subsystem_enum


def test_claim_result_schema_allows_profile_subsystems() -> None:
    schema = claim_result_response_schema()
    subsystem_enum = set(schema["properties"]["claims"]["items"]["properties"]["subsystem"]["enum"])
    profile_subsystems: set[str] = set()

    def collect_subsystems(value) -> None:
        if isinstance(value, dict):
            for key, row in value.items():
                if key == "subsystem" and isinstance(row, str):
                    profile_subsystems.add(row)
                collect_subsystems(row)
        elif isinstance(value, list):
            for row in value:
                collect_subsystems(row)

    for profile_id in available_profile_ids():
        profile = load_profile(profile_id)
        component_map = profile.get("component_map") or {}
        if isinstance(component_map, dict):
            profile_subsystems.update(str(key) for key in component_map)
        collect_subsystems(profile)

    assert profile_subsystems <= subsystem_enum


def test_compact_bundle_for_model_keeps_high_signal_refs_and_drops_bulk_payload() -> None:
    full_bundle = {
        "schema_version": "ops-evidence-bundle/v1",
        "evidence_sha256": "e" * 64,
        "service": "generic-service",
        "environment": "prod",
        "system_profile": {"system_type": "notification_workflow"},
        "operational_contract": {"expected_normal": ["configured commands exist"]},
        "metric_semantics": {"error_count": {"zero_behavior": "healthy"}},
        "component_map": {"job_configuration": "systemd unit"},
        "action_constraints": ["Profile context is not evidence."],
        "window_start": "2026-06-16T00:00:00Z",
        "window_end": "2026-06-16T01:00:00Z",
        "evidence_refs": {
            "PATTERN-001": {"type": "log_pattern", "summary": "missing configured command", "count": 10},
            "METRIC-001": {"type": "metric_window", "summary": "error_count=10", "current_value": 10},
            "LOG-001": {
                "type": "log",
                "summary": "error " + ("x" * 1000),
                "timestamp": "2026-06-16T00:00:00Z",
            },
            "LOG-002": {"type": "log", "summary": "info only"},
        },
        "log_patterns": [
            {
                "pattern_id": "PATTERN-001",
                "error_type": "job_configuration_mismatch",
                "count": 10,
                "example_log": "can't open file /opt/app/job.py",
            }
        ],
        "metric_windows": [{"metric_window_id": "METRIC-001", "metric_name": "error_count", "current_value": 10}],
        "logs": [
            {
                "evidence_id": "LOG-001",
                "timestamp": "2026-06-16T00:00:00Z",
                "severity": "ERROR",
                "message_sanitized": "error " + ("x" * 1000),
                "labels_json": {"bulk": "y" * 1000},
            },
            {
                "evidence_id": "LOG-002",
                "timestamp": "2026-06-16T00:01:00Z",
                "severity": "INFO",
                "message_sanitized": "info",
            },
        ],
        "normalized_events": [
            {
                "event_id": "EV-001",
                "timestamp": "2026-06-16T00:00:00Z",
                "severity": "ERROR",
                "message_sanitized": "event " + ("z" * 1000),
            }
        ],
    }

    compact_default = compact_bundle_for_model(full_bundle, max_text_chars=80)

    assert compact_default["source_counts"]["full_logs"] == 2
    assert compact_default["system_profile"]["system_type"] == "notification_workflow"
    assert compact_default["operational_contract"]["expected_normal"] == ["configured commands exist"]
    assert compact_default["component_map"]["job_configuration"] == "systemd unit"
    assert compact_default["source_counts"]["model_logs"] == 0
    assert compact_default["source_counts"]["full_normalized_events"] == 1
    assert compact_default["source_counts"]["model_normalized_events"] == 0
    assert compact_default["logs"] == []
    assert set(compact_default["evidence_refs"]) == {"METRIC-001", "PATTERN-001"}
    assert "example_log" not in compact_default["log_patterns"][0]
    assert "Individual sanitized log lines" in compact_default["compression_note"]

    compact_with_logs = compact_bundle_for_model(full_bundle, max_logs=1, max_text_chars=80)

    assert compact_with_logs["source_counts"]["model_logs"] == 1
    assert [row["evidence_id"] for row in compact_with_logs["logs"]] == ["LOG-001"]
    assert "labels_json" not in compact_with_logs["logs"][0]
    assert set(compact_with_logs["evidence_refs"]) == {"LOG-001", "METRIC-001", "PATTERN-001"}
    assert len(compact_with_logs["evidence_refs"]["LOG-001"]["summary"]) <= 80
