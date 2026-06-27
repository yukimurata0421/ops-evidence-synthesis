from __future__ import annotations

import copy
import json
import subprocess
import sys
from pathlib import Path

import pytest

from ops_evidence_synthesis.ai.base import ModelResponse
from ops_evidence_synthesis.evidence_request_planner import (
    build_evidence_request_plan,
    plan_evidence_requests,
    planner_quality_warnings,
    render_collection_instructions,
)
from ops_evidence_synthesis.local_first import build_bundle_from_sanitized, sanitize_input, verify_sanitized_output
from ops_evidence_synthesis.profile_discovery import approve_profile_draft, discover_profile, draft_profile


ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = ROOT / "sample_projects" / "profile_discovery_sample"


def _policy_bundle() -> dict[str, object]:
    return {
        "schema_version": "evidence_bundle.v1",
        "bundle_type": "sanitized_evidence_bundle",
        "raw_log_policy": "not_uploaded",
        "evidence_sha256": "parent-sha-test",
        "source": {"service": "sample", "environment": "prod", "profile_confidence": "explicit"},
        "time_window": {"start": "2026-06-16T00:00:00Z", "end": "2026-06-16T18:00:00Z"},
        "signals": [
            {
                "signal_id": "SIG-001",
                "signal_type": "missing_command",
                "core_target_type": "job_configuration_mismatch",
                "component": "amazon-notify-main-watchdog.service",
                "evidence_refs": ["PATTERN-001"],
                "count": 1,
                "confidence": 0.8,
            },
            {
                "signal_id": "SIG-002",
                "signal_type": "restart_loop",
                "core_target_type": "restart_loop",
                "component": "amazon-notify-main-watchdog.service",
                "evidence_refs": ["PATTERN-002"],
                "count": 1,
                "confidence": 0.8,
            },
            {
                "signal_id": "SIG-003",
                "signal_type": "throughput_disappearance",
                "core_target_type": "throughput_disappearance",
                "component": "worker",
                "evidence_refs": ["PATTERN-003"],
                "count": 1,
                "confidence": 0.8,
            },
            {
                "signal_id": "SIG-004",
                "signal_type": "instrumentation_mismatch",
                "core_target_type": "instrumentation_mismatch",
                "component": "observer",
                "evidence_refs": ["PATTERN-004"],
                "count": 1,
                "confidence": 0.8,
            },
            {
                "signal_id": "SIG-005",
                "signal_type": "info",
                "core_target_type": "diagnostic_severity",
                "component": "observer",
                "evidence_refs": ["PATTERN-005"],
                "count": 10,
                "confidence": 0.2,
            },
            {
                "signal_id": "SIG-006",
                "signal_type": "warning",
                "core_target_type": "diagnostic_severity",
                "component": "observer",
                "evidence_refs": ["PATTERN-006"],
                "count": 3,
                "confidence": 0.2,
            },
            {
                "signal_id": "SIG-007",
                "signal_type": "debug",
                "core_target_type": "diagnostic_severity",
                "component": "observer",
                "evidence_refs": ["PATTERN-007"],
                "count": 1,
                "confidence": 0.1,
            },
            {
                "signal_id": "SIG-008",
                "signal_type": "http_5xx",
                "core_target_type": "external_dependency_failure",
                "component": "api-edge",
                "evidence_refs": ["PATTERN-008"],
                "count": 5,
                "confidence": 0.7,
            },
        ],
    }


def _approved_profile() -> dict[str, object]:
    return {
        "profile_id": "sample_approved",
        "profile_discovery_approval": {"approved": True, "explicit_profile": True},
        "component_map": {
            "watchdog": {"name": "amazon-notify-main-watchdog.service", "subsystem": "runtime_recovery"}
        },
        "collector_mappings": {
            "process_state_query": {
                "candidate_collectors": ["local_systemd", "local_journal"],
                "params": {"units": ["amazon-notify-main-watchdog.service"]},
                "safety_level": "read_only",
            }
        },
    }


class RequirementProvider:
    provider = "requirement-test"
    model_name = "requirement-model"
    prompt_name = "evidence-requirements"
    temperature = 0.0

    def run(self, bundle: dict[str, object]) -> ModelResponse:
        assert bundle["llm_task"] == "evidence_requirement_planner"
        context = bundle["requirement_context"]
        target = context["validation_targets"][0]
        raw_output = json.dumps(
            {
                "schema_version": "evidence_requirements.v1",
                "requirements": [
                    {
                        "requirement_id": "LLM-REQ-001",
                        "review_target_id": target["target_id"],
                        "canonical_review_unit": target["canonical_review_unit"],
                        "blocked_reason": "user_impact_unverified",
                        "question_to_close": "Did the runtime failure produce user-visible impact?",
                        "required_evidence": [
                            {
                                "evidence_type": "user_impact_signal",
                                "source_kind": "metric_or_log",
                                "existing_signal_refs": ["PATTERN-001", "UNKNOWN-REF"],
                                "allowed_signal_names": ["http_5xx", "made_up_metric"],
                                "acceptance_criteria": "A listed user-impact signal overlaps the technical failure window.",
                                "rejection_criteria": "Listed user-impact signals remain healthy during the failure window.",
                                "collection_mode": "manual_read_only",
                                "maps_to_request_type": "user_impact_signal_query",
                            }
                        ],
                        "do_not_request": ["raw secrets"],
                        "fallback_if_unavailable": "Keep the target validation-only.",
                    }
                ],
            }
        )
        return ModelResponse(
            provider=self.provider,
            model_name=self.model_name,
            prompt_name=self.prompt_name,
            temperature=self.temperature,
            raw_output=raw_output,
            latency_ms=1,
            input_tokens=1,
            output_tokens=1,
        )


def _canonical_graph() -> dict[str, object]:
    return {
        "schema_version": "canonical_review_graph.v1",
        "evidence_sha256": "parent-sha-test",
        "summary": {"primary_count": 0, "validation_count": 1},
        "agreement_dimensions": {
            "technical_baseline_agreement": {"established": True},
            "incident_baseline_agreement": {"established": False},
        },
        "validation_targets": [
            {
                "target_id": "rt-impact",
                "review_target_id": "rt-impact",
                "title": "Runtime failure requires impact validation",
                "canonical_review_unit": "runtime_recovery",
                "review_priority_score": 0.82,
                "promotion_score": 0.72,
                "baseline_support_score": 0.9,
                "convergence_score": 0.8,
                "recommended_request_type": "user_impact_signal_query",
                "promotion_blocked_reasons": ["user_impact_unverified"],
                "evidence_refs": ["PATTERN-001"],
                "missing_evidence": ["critical outcome metric"],
            }
        ],
        "planner_inputs": {
            "recommended_request_types": ["user_impact_signal_query"],
            "validation_target_ids": ["rt-impact"],
            "promotion_decision_reasons": ["user_impact_unverified"],
        },
    }


def _real_bundle_and_profile(tmp_path: Path) -> tuple[Path, Path]:
    out = tmp_path / "local_first"
    sanitize_input(ROOT / "sample_logs" / "redaction_fixture.jsonl", out)
    build_bundle_from_sanitized(
        out / "sanitized_events.jsonl",
        service="unknown-sample",
        environment="prod",
        start="2026-06-16T00:00:00Z",
        end="2026-06-16T18:00:00Z",
        profile_name="generic",
        out_path=out / "evidence_bundle.json",
    )
    discovery = tmp_path / "discovery"
    discover_profile(
        PROJECT_ROOT,
        evidence_bundle_path=out / "evidence_bundle.json",
        service="unknown-sample",
        environment="prod",
        output_dir=discovery,
    )
    draft_profile(
        discovery / "profile_discovery_bundle.json",
        provider="local",
        out_path=discovery / "profile_draft.json",
    )
    approve_profile_draft(
        discovery / "profile_draft.json",
        profile_id="unknown_sample_approved",
        approved_by="api-user",
        out_path=discovery / "approved_profile.yaml",
    )
    return out / "evidence_bundle.json", discovery / "approved_profile.yaml"


def test_plan_evidence_requests_generates_plan_instructions_and_safe_policy(tmp_path: Path) -> None:
    bundle_path = tmp_path / "bundle.json"
    profile_path = tmp_path / "approved_profile.yaml"
    bundle_path.write_text(json.dumps(_policy_bundle()), encoding="utf-8")
    profile_path.write_text(json.dumps(_approved_profile()), encoding="utf-8")

    result = plan_evidence_requests(bundle_path, profile_path, output_dir=tmp_path / "plan")
    plan = json.loads((tmp_path / "plan" / "evidence_request_plan.json").read_text(encoding="utf-8"))
    instructions = (tmp_path / "plan" / "collection_instructions.md").read_text(encoding="utf-8")

    assert Path(result["evidence_request_plan"]).exists()
    assert Path(result["collection_instructions"]).exists()
    assert (tmp_path / "plan" / "planner_answers.sample.json").exists()
    assert plan["schema_version"] == "evidence_request_plan.v1"
    assert plan["execution_policy"]["planner_executes_commands"] is False
    assert plan["execution_policy"]["raw_env_values_allowed"] is False
    assert plan["execution_policy"]["read_only_only"] is True
    assert plan["context_policy"]["human_answers_are_context_not_evidence"] is True
    assert "Planner does not execute commands" in instructions
    assert "Raw env values and credentials must not be collected" in instructions
    for request in plan["requests"]:
        for step in request["collection_steps"]:
            assert step["read_only"] is True
            assert step["executes_now"] is False


def test_human_questions_are_ui_ready_and_config_question_is_metadata_only() -> None:
    plan = build_evidence_request_plan(_policy_bundle(), _approved_profile())
    questions = plan["human_questions"]
    assert all({"question_id", "answer_key", "label", "input_type", "required", "help"} <= set(row) for row in questions)
    input_types = {row["input_type"] for row in questions}
    assert {"datetime_range", "single_select", "multi_select", "boolean", "integer", "path_list", "component_map_select"} <= input_types
    config_question = next(row for row in questions if row["answer_key"] == "allow_config_metadata_only")
    assert config_question["default"] is True
    assert config_question["policy"]["raw_env_values_allowed"] is False
    assert "key_name_or_hash" in config_question["policy"]["allowed_extractions"]
    assert "raw_env_value" in config_question["policy"]["prohibited_extractions"]


def test_evidence_requirements_explain_promotion_gate_without_ai() -> None:
    plan = build_evidence_request_plan(
        _policy_bundle(),
        _approved_profile(),
        canonical_review_graph=_canonical_graph(),
    )
    requirements = plan["evidence_requirements"]
    assert requirements
    first = requirements[0]
    assert first["blocked_reason"] == "user_impact_unverified"
    assert first["review_target_id"] == "rt-impact"
    assert first["generation_source"] == "deterministic_gate"
    evidence = first["required_evidence"][0]
    assert evidence["maps_to_request_type"] == "user_impact_signal_query"
    assert evidence["acceptance_criteria"]
    assert evidence["rejection_criteria"]
    assert "raw secrets" in first["do_not_request"]
    instructions = render_collection_instructions(plan)
    assert "Evidence Requirements to Close Promotion Gates" in instructions
    assert "They do not promote a primary incident by themselves." in instructions


def test_evidence_requirements_can_be_generated_by_model_and_sanitized() -> None:
    plan = build_evidence_request_plan(
        _policy_bundle(),
        _approved_profile(),
        canonical_review_graph=_canonical_graph(),
        evidence_requirement_provider=RequirementProvider(),
    )
    metadata = plan["evidence_requirements_metadata"]
    assert metadata["generation_mode"] == "llm_with_deterministic_gate"
    assert metadata["llm_status"] == "ok"
    requirement = plan["evidence_requirements"][0]
    assert requirement["generation_source"] == "llm"
    evidence = requirement["required_evidence"][0]
    assert evidence["existing_signal_refs"] == ["PATTERN-001"]
    assert evidence["allowed_signal_names"] == ["http_5xx"]
    assert evidence["source_kind"] == "metric_or_log"
    assert evidence["instrumentation_gap_names"] == ["made_up_metric"]


def test_answers_refine_time_window_timezone_units_and_granularity(tmp_path: Path) -> None:
    answers = {
        "schema_version": "planner_answers.v1",
        "plan_id": "PLAN-001",
        "answered_by": "api-user",
        "answers": {
            "incident_window": {
                "start": "2026-06-15T22:00:00Z",
                "end": "2026-06-16T00:00:00Z",
                "timezone": "JST",
            },
            "available_granularity": ["fifteen_minute_aggregates"],
            "confirmed_units": ["amazon-notify-main-watchdog.service"],
            "allow_config_metadata_only": True,
        },
    }
    plan = build_evidence_request_plan(_policy_bundle(), _approved_profile(), planner_answers=answers)
    assert plan["requests"][0]["time_window"]["start"] == "2026-06-15T22:00:00Z"
    assert plan["requests"][0]["time_window"]["timezone"] == "JST"
    assert plan["operator_display_timezone"] == "JST"
    assert plan["operator_display_timezone"] == "JST"
    process = next(row for row in plan["requests"] if row.get("generic_request_type") == "process_state_query")
    assert process["collection_steps"][0]["substitution_hints"]["units"] == ["amazon-notify-main-watchdog.service"]
    throughput = next(row for row in plan["requests"] if row.get("generic_request_type") == "throughput_signal_query")
    assert "15m_aggregate" in throughput["granularity"]["required"]


def test_signal_rules_generate_required_request_types() -> None:
    plan = build_evidence_request_plan(_policy_bundle(), _approved_profile())
    request_types = {row.get("generic_request_type") or row["request_type"] for row in plan["requests"]}
    assert "installed_artifact_query" in request_types
    assert "process_state_query" in request_types
    assert "scheduler_history_query" in request_types
    assert "deployment_correlation_query" in request_types
    assert "throughput_signal_query" in request_types
    assert "instrumentation_consistency_query" in request_types
    assert "log_completeness_query" in request_types
    assert all(row["blocked_sources"] for row in plan["requests"])
    assert any("credential files" in row["blocked_sources"] for row in plan["requests"])
    assert all(any("sanitize" in step for step in row["post_collection_steps"]) for row in plan["requests"])
    assert all(any("verify-sanitized" in step for step in row["post_collection_steps"]) for row in plan["requests"])
    assert all(any("build-bundle" in step for step in row["post_collection_steps"]) for row in plan["requests"])


def test_plan_verify_sanitized_passes_and_detects_secret_injection(tmp_path: Path) -> None:
    bundle_path = tmp_path / "bundle.json"
    profile_path = tmp_path / "approved_profile.yaml"
    bundle_path.write_text(json.dumps(_policy_bundle()), encoding="utf-8")
    profile_path.write_text(json.dumps(_approved_profile()), encoding="utf-8")
    plan_evidence_requests(bundle_path, profile_path, output_dir=tmp_path / "plan")
    assert verify_sanitized_output(tmp_path / "plan")["passed"] is True

    plan_path = tmp_path / "plan" / "evidence_request_plan.json"
    unsafe = json.loads(plan_path.read_text(encoding="utf-8"))
    unsafe["requests"][0]["question"] = "Authorization: Bearer raw-token-123456789"
    plan_path.write_text(json.dumps(unsafe), encoding="utf-8")
    result = verify_sanitized_output(tmp_path / "plan")
    assert result["passed"] is False
    assert any(row["type"] == "secret_like" for row in result["findings"])


def test_plan_evidence_requests_cli_with_answers(tmp_path: Path) -> None:
    bundle_path, profile_path = _real_bundle_and_profile(tmp_path)
    first_out = tmp_path / "plan"
    subprocess.run(
        [
            sys.executable,
            "-m",
            "ops_evidence_synthesis.cli",
            "plan-evidence-requests",
            "--bundle",
            str(bundle_path),
            "--profile",
            str(profile_path),
            "--out",
            str(first_out),
        ],
        cwd=ROOT,
        check=True,
    )
    sample_answers = first_out / "planner_answers.sample.json"
    assert sample_answers.exists()
    answers = json.loads(sample_answers.read_text(encoding="utf-8"))
    answers["answers"]["incident_window"]["timezone"] = "JST"
    answers["answers"]["operator_display_timezone"] = "JST"
    answers["answers"]["available_granularity"] = ["fifteen_minute_aggregates"]
    answers_path = first_out / "planner_answers.json"
    answers_path.write_text(json.dumps(answers), encoding="utf-8")
    refined = tmp_path / "plan_refined"
    subprocess.run(
        [
            sys.executable,
            "-m",
            "ops_evidence_synthesis.cli",
            "plan-evidence-requests",
            "--bundle",
            str(bundle_path),
            "--profile",
            str(profile_path),
            "--answers",
            str(answers_path),
            "--out",
            str(refined),
        ],
        cwd=ROOT,
        check=True,
    )
    plan = json.loads((refined / "evidence_request_plan.json").read_text(encoding="utf-8"))
    assert plan["requests"][0]["time_window"]["timezone"] == "JST"
    assert plan["operator_display_timezone"] == "JST"
    assert verify_sanitized_output(refined)["passed"] is True


def test_evidence_request_plan_api_returns_plan_and_rejects_secret(tmp_path: Path) -> None:
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient
    from ops_evidence_synthesis.api import app

    with TestClient(app) as client:
        response = client.post(
            "/evidence-requests/plan",
            json={
                "evidence_bundle": _policy_bundle(),
                "approved_profile": _approved_profile(),
                "planner_answers": None,
                "canonical_review_graph": _canonical_graph(),
                "generate_evidence_requirements_with_ai": True,
            },
        )
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["plan"]["schema_version"] == "evidence_request_plan.v1"
        assert payload["plan"]["execution_policy"]["planner_executes_commands"] is False
        assert payload["plan"]["evidence_requirements_metadata"]["llm_status"] == "ok"
        assert payload["plan"]["evidence_requirements"]
        assert any(
            question["answer_key"] == "allow_config_metadata_only"
            for question in payload["plan"]["human_questions"]
        )
        assert "Planner does not execute commands" in payload["collection_instructions_markdown"]

        refined = client.post(
            "/evidence-requests/plan",
            json={
                "evidence_bundle": _policy_bundle(),
                "approved_profile": _approved_profile(),
                "canonical_review_graph": _canonical_graph(),
                "generate_evidence_requirements_with_ai": True,
                "planner_answers": {
                    "schema_version": "planner_answers.v1",
                    "plan_id": payload["plan"]["plan_id"],
                    "answered_by": "api-user",
                    "answers": {
                        "incident_window": {
                            "start": "2026-06-15T22:00:00Z",
                            "end": "2026-06-16T00:00:00Z",
                            "timezone": "JST",
                        },
                        "available_granularity": ["fifteen_minute_aggregates"],
                        "confirmed_units": ["amazon-notify-main-watchdog.service"],
                        "allow_config_metadata_only": True,
                    },
                },
            },
        )
        assert refined.status_code == 200, refined.text
        refined_plan = refined.json()["plan"]
        assert refined_plan["requests"][0]["time_window"]["timezone"] == "JST"
        assert "planner_quality_warnings" in refined_plan
        throughput = next(row for row in refined_plan["requests"] if row.get("generic_request_type") == "throughput_signal_query")
        assert "15m_aggregate" in throughput["granularity"]["required"]

        unsafe = copy.deepcopy(_policy_bundle())
        leaked = "raw-api-token-123456789"
        unsafe["signals"][0]["component"] = f"Authorization: Bearer {leaked}"
        rejected = client.post(
            "/evidence-requests/plan",
            json={"evidence_bundle": unsafe, "approved_profile": _approved_profile()},
        )
        assert rejected.status_code == 400
        assert leaked not in rejected.text


def test_user_impact_signal_options_filter_severity_labels() -> None:
    plan = build_evidence_request_plan(_policy_bundle(), _approved_profile())
    question = next(row for row in plan["human_questions"] if row["answer_key"] == "user_impact_signals")
    assert "http_5xx" in question["options"]
    assert "info" not in question["options"]
    assert "warning" not in question["options"]
    assert "debug" not in question["options"]
    enriched = {row["signal"]: row for row in plan["signal_enrichment"]}
    assert enriched["info"]["can_be_user_impact_signal"] is False
    assert enriched["http_5xx"]["can_be_user_impact_signal"] is True


def test_timezone_conflict_uses_operator_display_timezone() -> None:
    answers = {
        "answers": {
            "incident_window": {
                "start": "2026-06-15T06:00:00Z",
                "end": "2026-06-15T10:00:00Z",
                "timezone": "UTC",
            },
            "timezone": "JST",
        }
    }
    plan = build_evidence_request_plan(_policy_bundle(), _approved_profile(), planner_answers=answers)
    assert plan["incident_window"]["timezone"] == "UTC"
    assert plan["operator_display_timezone"] == "JST"
    assert all(row["time_window"]["timezone"] == "UTC" for row in plan["requests"])
    warnings = {row["warning_type"] for row in plan["planner_quality_warnings"]}
    assert "timezone_conflict" in warnings


def test_component_criticality_uses_component_map_select() -> None:
    plan = build_evidence_request_plan(_policy_bundle(), _approved_profile())
    question = next(row for row in plan["human_questions"] if row["answer_key"] == "component_criticality")
    assert question["input_type"] == "component_map_select"
    assert question["items"]
    assert all(set(item["options"]) == {"critical_path", "diagnostic_only", "unknown"} for item in question["items"])


def test_quality_gate_detects_single_select_object_default() -> None:
    fake_plan = {
        "incident_window": {"timezone": "UTC"},
        "operator_display_timezone": "UTC",
        "profile_confidence": "unknown",
        "human_questions": [
            {"answer_key": "bad", "input_type": "single_select", "default": {"component": "unknown"}},
        ],
        "requests": [],
    }
    warnings = planner_quality_warnings(
        fake_plan,
        bundle=_policy_bundle(),
        profile={},
        source_analysis={},
        answers={},
        raw_answers={},
    )
    assert any(row["warning_type"] == "single_select_default_type_mismatch" for row in warnings)


def test_explicit_profile_applies_domain_mapping() -> None:
    plan = build_evidence_request_plan(
        _policy_bundle(),
        _approved_profile(),
        source_analysis={
            "schema_version": "source_analysis_bundle.v1",
            "bundle_type": "sanitized_source_analysis_bundle",
            "collector_mapping_candidates": [
                {"component": "gmail watch", "read_only": True, "request_type": "external_dependency_status_query"}
            ],
        },
    )
    mapped = [row for row in plan["requests"] if row.get("domain_mapping_applied")]
    assert mapped
    assert any(row.get("generic_request_type") != row.get("request_type") for row in mapped)
    assert "generic_plan_despite_explicit_profile" not in {row["warning_type"] for row in plan["planner_quality_warnings"]}


def test_source_analysis_explicit_domain_request_type_takes_priority() -> None:
    generic_profile = {
        "profile_id": "plain-approved",
        "profile_discovery_approval": {"approved": True, "explicit_profile": True},
        "component_map": {"worker": {"name": "worker", "subsystem": "plain"}},
        "collector_mappings": {},
    }
    bundle = _policy_bundle()
    bundle["signals"] = [
        {
            "signal_id": "SIG-UI-001",
            "signal_type": "stream_not_live",
            "core_target_type": "user_impact_signal_gap",
            "component": "stream-output",
            "evidence_refs": ["PATTERN-001"],
            "count": 1,
            "confidence": 0.8,
        }
    ]
    plan = build_evidence_request_plan(
        bundle,
        generic_profile,
        source_analysis={
            "collector_mapping_candidates": [
                {
                    "request_type": "user_impact_signal_query",
                    "domain_request_type": "audio_energy_gap_query",
                    "params": {"metric_names": ["audio_energy_missing"]},
                    "safety_level": "read_only",
                }
            ]
        },
    )
    mapped = next(row for row in plan["requests"] if row.get("generic_request_type") == "user_impact_signal_query")
    assert mapped["request_type"] == "audio_energy_gap_query"
    assert mapped["mapped_by"] == "source_analysis.collector_mapping_candidates"
    assert mapped["matched_source_analysis"] is True


def test_source_analysis_metric_semantics_alias_generates_instrumentation_request() -> None:
    generic_profile = {
        "profile_id": "plain-approved",
        "profile_discovery_approval": {"approved": True, "explicit_profile": True},
        "component_map": {},
        "collector_mappings": {},
    }
    bundle = _policy_bundle()
    bundle["signals"] = []
    plan = build_evidence_request_plan(
        bundle,
        generic_profile,
        source_analysis={
            "collector_mapping_candidates": [
                {
                    "request_type": "metric_semantics_query",
                    "params": {"metric_names": ["rtmps_reconnect_count"]},
                    "safety_level": "read_only",
                }
            ]
        },
    )
    request = next(row for row in plan["requests"] if row.get("generic_request_type") == "instrumentation_consistency_query")
    assert request["request_type"] == "rtmps_reconnect_consistency_query"
    assert request["domain_mapping_applied"] is True


def test_explicit_profile_generic_only_emits_quality_warning() -> None:
    generic_profile = {
        "profile_id": "plain-approved",
        "profile_discovery_approval": {"approved": True, "explicit_profile": True},
        "component_map": {"worker": {"name": "worker", "subsystem": "plain"}},
        "collector_mappings": {},
    }
    plan = build_evidence_request_plan(_policy_bundle(), generic_profile)
    assert not any(row.get("domain_mapping_applied") for row in plan["requests"])
    assert "generic_plan_despite_explicit_profile" in {row["warning_type"] for row in plan["planner_quality_warnings"]}


def test_raw_grep_template_is_not_emitted_and_source_first_is_preferred() -> None:
    plan = build_evidence_request_plan(_policy_bundle(), _approved_profile())
    templates = [
        str(step.get("command_template") or "")
        for request in plan["requests"]
        for step in request.get("collection_steps") or []
    ]
    assert not any("grep -R" in template for template in templates)
    assert any("ops-evidence sanitize-source" in template for template in templates)


def test_collection_instructions_are_labeled_as_checklist_not_shell_script() -> None:
    plan = build_evidence_request_plan(_policy_bundle(), _approved_profile())
    markdown = render_collection_instructions(plan)

    assert "This is a collection checklist, not a shell script." in markdown
    assert "Do not paste the whole file into a terminal." in markdown
    assert "The current evidence was not enough to close the review automatically." in markdown
    assert "Choose one request below that your environment can actually answer." in markdown
    assert "If a requested metric, dashboard, log source, or state file does not exist, record it as unavailable" in markdown
    assert "`query_metrics` is a placeholder for your metrics backend" in markdown


def test_prompt_rules_forbid_invented_collection_sources() -> None:
    plan = build_evidence_request_plan(_policy_bundle(), _approved_profile())
    prompt_rules = " ".join(str(rule) for rule in plan["prompt_rules"])

    assert "Do not invent log source names, metric names, state file paths, endpoints, or collector commands." in prompt_rules
    assert "Do not translate missing evidence into ad hoc local commands; leave collection templates to the Evidence Request Planner." in prompt_rules
    assert "Collection templates must not include invented metrics, log paths, state files, endpoints, or commands." in prompt_rules


def test_stream_v3_collection_templates_use_profile_metrics_not_unimplemented_names() -> None:
    profile = {
        "profile_id": "stream_v3_minimal",
        "profile_discovery_approval": {"approved": True, "explicit_profile": True},
        "component_map": {
            "youtube_health": {"name": "youtube_health", "subsystem": "youtube"},
            "chromium_capture": {"name": "chromium_capture", "subsystem": "capture"},
            "audio_energy": {"name": "audio_energy", "subsystem": "audio"},
        },
        "operational_evidence_specs": [
            {
                "request_id": "external_dependency_status_query",
                "profile_request_id": "youtube_ingest_status_query",
                "metric_names": ["stream_v3_youtube_ingest_connected"],
            },
            {
                "request_id": "freshness_signal_query",
                "profile_request_id": "capture_freshness_query",
                "metric_names": ["stream_v3_runtime_heartbeat_age_seconds"],
            },
            {
                "request_id": "user_impact_signal_query",
                "profile_request_id": "audio_energy_gap_query",
                "metric_names": ["stream_v3_audio_ok", "stream_v3_audio_fault_count"],
            },
        ],
    }
    bundle = _policy_bundle()
    bundle["source"] = {"service": "stream_v3_arena_monitor", "environment": "prod", "profile_confidence": "explicit"}
    bundle["signals"] = [
        {
            "signal_id": "SIG-EXT-001",
            "signal_type": "external_dependency_unhealthy",
            "core_target_type": "external_dependency_failure",
            "component": "youtube_health",
            "evidence_refs": ["PATTERN-EXT-001"],
            "count": 1,
            "confidence": 0.8,
        },
        {
            "signal_id": "SIG-FRESH-001",
            "signal_type": "capture_freshness_stale",
            "core_target_type": "freshness_signal_gap",
            "component": "chromium_capture",
            "evidence_refs": ["PATTERN-FRESH-001"],
            "count": 1,
            "confidence": 0.8,
        },
        {
            "signal_id": "SIG-AUDIO-001",
            "signal_type": "stream_not_live",
            "core_target_type": "user_impact_signal_gap",
            "component": "audio_energy",
            "evidence_refs": ["PATTERN-AUDIO-001"],
            "count": 1,
            "confidence": 0.8,
        },
    ]
    plan = build_evidence_request_plan(
        bundle,
        profile,
        source_analysis={
            "collector_mapping_candidates": [
                {
                    "request_type": "user_impact_signal_query",
                    "domain_request_type": "audio_energy_gap_query",
                    "params": {"metric_names": ["audio_energy_missing"]},
                    "safety_level": "read_only",
                }
            ]
        },
    )
    markdown = render_collection_instructions(plan)

    assert "audio_energy_missing" not in markdown
    assert "capture_freshness_seconds" not in markdown
    assert "youtube_stream_health" not in markdown
    assert "rtmps_reconnect_count" not in markdown
    assert "`query_metrics --metric youtube_ingest_connected" not in markdown
    assert "stream_v3_youtube_ingest_connected" in markdown
    assert "stream_v3_audio_ok" in markdown
    assert "stream_v3_audio_fault_count" in markdown
    assert "stream_v3_runtime_heartbeat_age_seconds" in markdown
    assert "stream_v3_upload_latest_age_seconds" in markdown
    assert "<PROMETHEUS_JOB>" in markdown


def test_build_bundle_cli_accepts_child_lineage_options_and_profile_path(tmp_path: Path) -> None:
    bundle_path, profile_path = _real_bundle_and_profile(tmp_path)
    parent = json.loads(bundle_path.read_text(encoding="utf-8"))
    child_out = tmp_path / "child" / "child_evidence_bundle.json"
    subprocess.run(
        [
            sys.executable,
            "-m",
            "ops_evidence_synthesis.cli",
            "build-bundle",
            str(bundle_path.parent / "sanitized_events.jsonl"),
            "--service",
            "unknown-sample",
            "--environment",
            "prod",
            "--start",
            "2026-06-16T00:00:00Z",
            "--end",
            "2026-06-16T18:00:00Z",
            "--profile",
            str(profile_path),
            "--parent-evidence-sha256",
            str(parent["evidence_sha256"]),
            "--evidence-request-plan-id",
            "PLAN-CLI-LINEAGE",
            "--collection-mode",
            "manual_read_only_collection",
            "--out",
            str(child_out),
        ],
        cwd=ROOT,
        check=True,
    )
    child = json.loads(child_out.read_text(encoding="utf-8"))
    assert child["parent_evidence_sha256"] == parent["evidence_sha256"]
    assert child["evidence_request_plan_id"] == "PLAN-CLI-LINEAGE"
    assert child["collection_mode"] == "manual_read_only_collection"
    assert child["raw_output_policy"] == "local_only"
    assert child["sanitize_before_upload"] is True
    assert child["verify_sanitized_required"] is True
    assert child["analysis_policy"]["profile_mode"] == "explicit"
    assert verify_sanitized_output(child_out.parent)["passed"] is True


def test_cloud_run_smoke_script_is_available() -> None:
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "cloud_run_smoke.py"), "--help"],
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    assert result.returncode == 0
    assert "--base-url" in result.stdout
