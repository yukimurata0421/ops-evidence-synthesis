from __future__ import annotations

import copy
import json
import subprocess
import sys
from pathlib import Path

from ops_evidence_synthesis.canonical import sha256_json
from ops_evidence_synthesis.ai.base import ModelResponse
from ops_evidence_synthesis.local_first import build_bundle_from_sanitized, sanitize_input, verify_sanitized_output
from ops_evidence_synthesis.profile_discovery import (
    approve_profile_draft,
    build_focused_profile_with_provider,
    build_profile_discovery_bundle,
    build_profile_draft_with_provider,
    discover_profile,
    draft_focused_profile,
    draft_profile,
    profile_discovery_hash_payload,
)


ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = ROOT / "sample_projects" / "profile_discovery_sample"


class FakeGeminiProfileDraftProvider:
    provider = "gemini-enterprise-agent-platform"
    model_name = "gemini-3.1-pro-preview"
    prompt_name = "profile-draft"
    temperature = 0.0

    def run(self, bundle: dict) -> ModelResponse:
        assert bundle["llm_task"] == "profile_draft"
        assert bundle["profile_draft_policy"]["raw_source_sent_to_provider"] is False
        assert bundle["profile_draft_policy"]["draft_requires_human_approval"] is True
        payload = {
            "schema_version": "profile_draft_ai.v1",
            "system_type": "notification_workflow",
            "purpose": "Monitor sanitized notification workflow evidence and route review work.",
            "critical_outcomes": ["Notification forwarding remains observable."],
            "components": [
                {
                    "component_id": "watchdog",
                    "name": "watchdog",
                    "role": "Monitors scheduler and service liveness.",
                    "subsystem": "service_liveness",
                    "core_target_types": ["heartbeat_missing"],
                }
            ],
            "metric_semantics": [
                {
                    "metric_name": "watchdog_success_count",
                    "semantic_type": "heartbeat",
                    "zero_behavior": "suspicious",
                    "increase_behavior": "healthy",
                    "decrease_behavior": "suspicious",
                    "subsystem": "service_liveness",
                    "core_target_type": "heartbeat_missing",
                }
            ],
            "log_sources": [{"source_id": "application_logs", "description": "Sanitized application logs."}],
            "collector_mappings": [
                {
                    "request_type": "process_state_query",
                    "candidate_collectors": ["systemd status"],
                    "safety_level": "read_only",
                }
            ],
            "known_benign_noise": ["empty polling interval"],
            "action_constraints": ["Do not request credentials."],
            "assumptions": ["Critical outcomes require human confirmation."],
            "required_human_decisions": ["Confirm metric semantics before approval."],
        }
        return ModelResponse(
            provider=self.provider,
            model_name=self.model_name,
            prompt_name=self.prompt_name,
            temperature=self.temperature,
            raw_output=json.dumps(payload, ensure_ascii=False, sort_keys=True),
            latency_ms=1,
            input_tokens=100,
            output_tokens=100,
        )


class FakeGeminiFocusedProfileProvider:
    provider = "gemini-enterprise-agent-platform"
    model_name = "gemini-3.1-pro-preview"
    prompt_name = "focused-operational-profile"
    temperature = 0.0

    def run(self, bundle: dict) -> ModelResponse:
        assert bundle["llm_task"] == "focused_operational_profile"
        assert bundle["focused_profile_policy"]["raw_source_sent_to_provider"] is False
        assert bundle["focused_profile_policy"]["raw_logs_sent_to_provider"] is False
        assert bundle["focused_profile_policy"]["source_context_is_incident_evidence"] is False
        payload = {
            "schema_version": "focused_operational_profile.v1",
            "system_label": "unknown-sample",
            "system_summary": {
                "system_type": "notification_workflow",
                "primary_purpose": "Watch notification workflow liveness and route review work.",
                "logged_subject": "watchdog service logs and heartbeat metrics",
                "operational_boundary": "Source context is profile context, not incident evidence.",
                "confidence": 0.84,
            },
            "runtime_components": [
                {
                    "component_id": "watchdog",
                    "name": "watchdog",
                    "role": "Monitors scheduler and service liveness.",
                    "evidence_refs": ["EVT-001"],
                    "source_context_refs": ["amazon-notify-main-watchdog.service"],
                    "confidence": 0.86,
                }
            ],
            "observability_contract": {
                "logs": [
                    {
                        "source": "application_logs",
                        "meaning": "Sanitized application log stream.",
                        "evidence_refs": ["EVT-001"],
                        "source_context_refs": [],
                    }
                ],
                "metrics": [
                    {
                        "metric_name": "watchdog_success_count",
                        "meaning": "Watchdog successful checks.",
                        "healthy_direction": "increase",
                        "evidence_refs": ["EVT-001"],
                        "source_context_refs": [],
                    }
                ],
                "heartbeats": [
                    {
                        "name": "watchdog_success_count",
                        "meaning": "Liveness heartbeat.",
                        "evidence_refs": ["EVT-001"],
                        "source_context_refs": [],
                    }
                ],
                "state_files": [],
            },
            "orchestration_flows": [
                {
                    "flow_name": "Watchdog Recovery",
                    "trigger": "missing heartbeat or service liveness gap",
                    "steps": ["observe heartbeat", "request read-only verification", "stop for human approval"],
                    "owned_by_components": ["watchdog"],
                    "evidence_refs": ["EVT-001"],
                    "source_context_refs": ["amazon-notify-main-watchdog.service"],
                    "confidence": 0.8,
                }
            ],
            "failure_modes": [
                {
                    "failure_mode": "watchdog liveness gap",
                    "observable_signals": ["watchdog_success_count"],
                    "missing_evidence": ["confirm user impact"],
                    "confidence": 0.7,
                }
            ],
            "read_only_collectors": [
                {
                    "collector": "process_state_query",
                    "purpose": "Check service liveness without changing runtime state.",
                    "safety_level": "read_only",
                }
            ],
            "profile_limits": {
                "source_context_is_incident_evidence": False,
                "runtime_claims_require_evidence_id": True,
                "approval_required_before_explicit_profile": True,
                "raw_source_sent_to_provider": False,
                "raw_logs_sent_to_provider": False,
                "notes": ["sanitized input only"],
            },
            "human_review_required": ["Confirm critical outcome before approval."],
        }
        return ModelResponse(
            provider=self.provider,
            model_name=self.model_name,
            prompt_name=self.prompt_name,
            temperature=self.temperature,
            raw_output=json.dumps(payload, ensure_ascii=False, sort_keys=True),
            latency_ms=1,
            input_tokens=100,
            output_tokens=100,
        )


def _redaction_fixture_bundle(tmp_path: Path) -> dict[str, object]:
    out = tmp_path / "local_first"
    sanitize_input(ROOT / "sample_logs" / "redaction_fixture.jsonl", out)
    return build_bundle_from_sanitized(
        out / "sanitized_events.jsonl",
        service="unknown-sample",
        environment="prod",
        start="2026-06-16T00:00:00Z",
        end="2026-06-16T18:00:00Z",
        profile_name="generic",
        out_path=out / "evidence_bundle.json",
    )


def _bundle_with_script_and_metric(tmp_path: Path) -> dict[str, object]:
    bundle = copy.deepcopy(_redaction_fixture_bundle(tmp_path))
    item = bundle["evidence_items"][0]
    item["example_sanitized"] = (
        f"{item['example_sanitized']} <USER_HOME>/projects/amazon-notify/src/watchdog_restart_main.py "
        "job_configuration_mismatch_count"
    )
    return bundle


def test_discover_profile_generates_sanitized_bundle_and_links_entities(tmp_path: Path) -> None:
    evidence = _bundle_with_script_and_metric(tmp_path)
    evidence_path = tmp_path / "evidence_bundle.json"
    evidence_path.write_text(json.dumps(evidence, sort_keys=True), encoding="utf-8")

    result = discover_profile(
        PROJECT_ROOT,
        evidence_bundle_path=evidence_path,
        service="unknown-sample",
        environment="prod",
        output_dir=tmp_path / "discovery",
    )
    discovery_path = Path(result["profile_discovery_bundle"])
    bundle = json.loads(discovery_path.read_text(encoding="utf-8"))
    serialized = json.dumps(bundle, sort_keys=True)

    assert discovery_path.exists()
    assert bundle["schema_version"] == "profile_discovery_bundle.v1"
    assert bundle["bundle_type"] == "sanitized_profile_discovery_bundle"
    assert bundle["raw_config_policy"] == "not_uploaded"
    assert bundle["raw_logs_policy"] == "not_uploaded"
    assert bundle["local_first_summary"]["raw_configs_uploaded"] is False
    assert bundle["local_first_summary"]["raw_logs_uploaded"] is False
    assert "fake-gmail-token-for-tests-only" not in serialized
    assert "fake-credentials" not in serialized
    assert "DISCORD_WEBHOOK_URL" not in serialized
    assert "GOOGLE_APPLICATION_CREDENTIALS" not in serialized
    assert "Authorization:" not in serialized
    assert "Bearer " not in serialized

    observed_units = [row for row in bundle["observed_entities"] if row["entity_type"] == "systemd_unit"]
    assert any(row["name"] == "amazon-notify-main-watchdog.service" for row in observed_units)
    assert any(
        row["match_type"] == "systemd_unit_exact_match"
        and "unit_name" in row["match_features"]
        for row in bundle["entity_links"]
    )
    assert any(
        row["match_type"] == "exec_start_path_basename_match"
        and "exec_start_basename" in row["match_features"]
        for row in bundle["entity_links"]
    )
    assert bundle["component_candidates"]
    assert all(row["human_review_required"] is True for row in bundle["component_candidates"])
    assert bundle["collector_mapping_candidates"]
    assert all(row["safety_level"] == "read_only" for row in bundle["collector_mapping_candidates"])


def test_metric_candidates_draft_and_verify_sanitized(tmp_path: Path) -> None:
    evidence = _bundle_with_script_and_metric(tmp_path)
    discovery = build_profile_discovery_bundle(
        PROJECT_ROOT,
        evidence_bundle_path=None,
        service="unknown-sample",
        environment="prod",
    )
    with_evidence = build_profile_discovery_bundle(
        PROJECT_ROOT,
        evidence_bundle_path=None,
        service="unknown-sample",
        environment="prod",
    )
    assert discovery["discovery_sha256"] == with_evidence["discovery_sha256"]

    evidence_path = tmp_path / "evidence_bundle.json"
    evidence_path.write_text(json.dumps(evidence, sort_keys=True), encoding="utf-8")
    discover_profile(
        PROJECT_ROOT,
        evidence_bundle_path=evidence_path,
        service="unknown-sample",
        environment="prod",
        output_dir=tmp_path / "discovery",
    )
    discovery_path = tmp_path / "discovery" / "profile_discovery_bundle.json"
    bundle = json.loads(discovery_path.read_text(encoding="utf-8"))
    assert bundle["metric_semantics_candidates"]
    assert all(row["human_review_required"] is True for row in bundle["metric_semantics_candidates"])
    assert verify_sanitized_output(tmp_path / "discovery")["passed"] is True

    draft = draft_profile(
        discovery_path,
        provider="local",
        out_path=tmp_path / "discovery" / "profile_draft.json",
    )
    assert draft["schema_version"] == "profile_draft.v1"
    assert draft["approved"] is False
    assert draft["explicit_profile"] is False
    assert draft["human_review_required"] is True
    assert draft["profile"]["collector_mappings"]
    assert all(
        row["safety_level"] == "read_only"
        for row in draft["profile"]["collector_mappings"].values()
    )
    assert verify_sanitized_output(tmp_path / "discovery")["passed"] is True
    serialized_draft = json.dumps(draft, sort_keys=True)
    assert "fake-gmail-token-for-tests-only" not in serialized_draft
    assert "Authorization:" not in serialized_draft


def test_gemini_profile_draft_uses_sanitized_discovery_and_requires_human_review(tmp_path: Path) -> None:
    evidence = _bundle_with_script_and_metric(tmp_path)
    evidence_path = tmp_path / "evidence_bundle.json"
    evidence_path.write_text(json.dumps(evidence, sort_keys=True), encoding="utf-8")
    discovery = build_profile_discovery_bundle(
        PROJECT_ROOT,
        evidence_bundle_path=evidence_path,
        service="unknown-sample",
        environment="prod",
    )

    draft = build_profile_draft_with_provider(discovery, FakeGeminiProfileDraftProvider())

    assert draft["schema_version"] == "profile_draft.v1"
    assert draft["approved"] is False
    assert draft["explicit_profile"] is False
    assert draft["human_review_required"] is True
    assert draft["profile_generation"]["llm_status"] == "ok"
    assert draft["profile_generation"]["model_name"] == "gemini-3.1-pro-preview"
    assert draft["profile"]["system_type"] == "notification_workflow"
    assert draft["profile"]["component_map"]["watchdog"]["human_review_required"] is True
    assert draft["profile"]["metric_semantics"]["watchdog_success_count"]["zero_behavior"] == "suspicious"
    assert draft["profile"]["collector_mappings"]["process_state_query"]["safety_level"] == "read_only"
    assert any("Gemini analyzed sanitized Profile Discovery context" in item for item in draft["assumptions"])


def test_gemini_focused_profile_uses_sanitized_operational_context(tmp_path: Path) -> None:
    evidence = _bundle_with_script_and_metric(tmp_path)
    evidence_path = tmp_path / "evidence_bundle.json"
    evidence_path.write_text(json.dumps(evidence, sort_keys=True), encoding="utf-8")
    discovery = build_profile_discovery_bundle(
        PROJECT_ROOT,
        evidence_bundle_path=evidence_path,
        service="unknown-sample",
        environment="prod",
    )

    profile = build_focused_profile_with_provider(
        discovery,
        FakeGeminiFocusedProfileProvider(),
        evidence_bundle=evidence,
    )

    assert profile["schema_version"] == "focused_operational_profile.v1"
    assert profile["focused_profile_generation"]["llm_status"] == "ok"
    assert profile["focused_profile_generation"]["fallback_used"] is False
    assert profile["system_summary"]["system_type"] == "notification_workflow"
    assert profile["profile_limits"]["source_context_is_incident_evidence"] is False
    assert profile["profile_limits"]["runtime_claims_require_evidence_id"] is True
    assert profile["profile_limits"]["raw_source_sent_to_provider"] is False
    assert profile["profile_limits"]["raw_logs_sent_to_provider"] is False
    assert profile["read_only_collectors"][0]["safety_level"] == "read_only"
    assert len(profile["runtime_components"]) <= 12
    assert verify_sanitized_output(tmp_path)["passed"] is True


def test_discovery_sha_is_stable_and_ignores_key_order(tmp_path: Path) -> None:
    evidence = _bundle_with_script_and_metric(tmp_path)
    evidence_path = tmp_path / "evidence_bundle.json"
    evidence_path.write_text(json.dumps(evidence, sort_keys=True), encoding="utf-8")
    first = build_profile_discovery_bundle(
        PROJECT_ROOT,
        evidence_bundle_path=evidence_path,
        service="unknown-sample",
        environment="prod",
    )
    second = build_profile_discovery_bundle(
        PROJECT_ROOT,
        evidence_bundle_path=evidence_path,
        service="unknown-sample",
        environment="prod",
    )
    assert first["discovery_sha256"] == second["discovery_sha256"]

    reordered = json.loads(json.dumps(first, sort_keys=True))
    reordered["discovery_sha256"] = sha256_json(profile_discovery_hash_payload(reordered))
    assert first["discovery_sha256"] == reordered["discovery_sha256"]


def test_verify_sanitized_fails_for_secret_in_profile_discovery_output(tmp_path: Path) -> None:
    output = tmp_path / "unsafe"
    output.mkdir()
    (output / "profile_discovery_bundle.json").write_text(
        json.dumps(
            {
                "schema_version": "profile_discovery_bundle.v1",
                "message": "Authorization: Bearer intentionally-unsafe-token-12345",
            }
        ),
        encoding="utf-8",
    )
    result = verify_sanitized_output(output)
    assert result["passed"] is False
    assert result["findings"][0]["type"] == "secret_like"


def test_discover_and_draft_profile_cli(tmp_path: Path) -> None:
    evidence = _redaction_fixture_bundle(tmp_path)
    evidence_path = tmp_path / "evidence_bundle.json"
    evidence_path.write_text(json.dumps(evidence, sort_keys=True), encoding="utf-8")
    discovery_out = tmp_path / "cli_discovery"
    subprocess.run(
        [
            sys.executable,
            "-m",
            "ops_evidence_synthesis.cli",
            "discover-profile",
            "--project-root",
            str(PROJECT_ROOT),
            "--evidence-bundle",
            str(evidence_path),
            "--service",
            "unknown-sample",
            "--environment",
            "prod",
            "--out",
            str(discovery_out),
        ],
        check=True,
        cwd=ROOT,
    )
    assert (discovery_out / "profile_discovery_bundle.json").exists()
    subprocess.run(
        [
            sys.executable,
            "-m",
            "ops_evidence_synthesis.cli",
            "draft-profile",
            "--discovery-bundle",
            str(discovery_out / "profile_discovery_bundle.json"),
            "--provider",
            "local",
            "--out",
            str(discovery_out / "profile_draft.json"),
        ],
        check=True,
        cwd=ROOT,
    )
    draft_focused_profile(
        discovery_out / "profile_discovery_bundle.json",
        provider="local",
        evidence_bundle_path=evidence_path,
        out_path=discovery_out / "focused_operational_profile.json",
    )
    focused = json.loads((discovery_out / "focused_operational_profile.json").read_text(encoding="utf-8"))
    assert focused["schema_version"] == "focused_operational_profile.v1"
    assert focused["profile_limits"]["approval_required_before_explicit_profile"] is True
    assert verify_sanitized_output(discovery_out)["passed"] is True


def test_approve_profile_draft_writes_explicit_profile_and_build_bundle_uses_it(
    tmp_path: Path,
    monkeypatch,
) -> None:
    evidence = _redaction_fixture_bundle(tmp_path)
    evidence_path = tmp_path / "evidence_bundle.json"
    evidence_path.write_text(json.dumps(evidence, sort_keys=True), encoding="utf-8")
    discovery_out = tmp_path / "discovery"
    discover_profile(
        PROJECT_ROOT,
        evidence_bundle_path=evidence_path,
        service="unknown-sample",
        environment="prod",
        output_dir=discovery_out,
    )
    draft = draft_profile(
        discovery_out / "profile_discovery_bundle.json",
        provider="local",
        out_path=discovery_out / "profile_draft.json",
    )
    assert draft["approved"] is False
    profile_dir = tmp_path / "profiles"
    monkeypatch.setenv("OES_PROFILE_DIR", str(profile_dir))
    result = approve_profile_draft(
        discovery_out / "profile_draft.json",
        profile_id="unknown-sample-approved",
        approved_by="local-reviewer",
        note="approved for test",
        out_path=profile_dir / "unknown_sample_approved.yaml",
    )
    assert result["approved"] is True
    assert result["explicit_profile"] is True
    approved_text = (profile_dir / "unknown_sample_approved.yaml").read_text(encoding="utf-8")
    assert "fake-gmail-token-for-tests-only" not in approved_text
    assert "Authorization:" not in approved_text

    explicit = build_bundle_from_sanitized(
        tmp_path / "local_first" / "sanitized_events.jsonl",
        service="unknown-sample",
        environment="prod",
        start="2026-06-16T00:00:00Z",
        end="2026-06-16T18:00:00Z",
        profile_name="unknown-sample-approved",
        out_path=tmp_path / "explicit_bundle.json",
    )
    assert explicit["source"]["profile_confidence"] == "explicit"
    assert explicit["analysis_policy"]["explicit_profile"] is True
    assert explicit["analysis_policy"]["allow_primary_candidate"] is True
