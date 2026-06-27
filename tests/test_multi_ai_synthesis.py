from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from fastapi.testclient import TestClient

from ops_evidence_synthesis.local_first import build_bundle_from_sanitized, sanitize_input
from ops_evidence_synthesis.profile_discovery import approve_profile_draft, discover_profile, draft_profile
from ops_evidence_synthesis.synthesis.multi_ai import SCORE_NOTE, finding_impact_from_synthesis, run_multi_ai, synthesize_multi_ai


ROOT = Path(__file__).resolve().parents[1]


def _bundle_and_profile(tmp_path: Path) -> tuple[dict[str, object], dict[str, object]]:
    sanitized_dir = tmp_path / "sanitized"
    sanitize_input(ROOT / "sample_logs" / "secret_heavy.jsonl", sanitized_dir)
    bundle = build_bundle_from_sanitized(
        sanitized_dir / "sanitized_events.jsonl",
        service="demo-payment",
        environment="prod",
        start="2026-06-16T00:00:00Z",
        end="2026-06-16T18:00:00Z",
        profile_name="generic",
        out_path=tmp_path / "evidence_bundle.json",
    )
    discovery = discover_profile(
        ROOT / "sample_projects" / "profile_discovery_sample",
        evidence_bundle_path=tmp_path / "evidence_bundle.json",
        service="demo-payment",
        environment="prod",
        output_dir=tmp_path / "discovery",
    )
    assert discovery["discovery_sha256"]
    draft_profile(
        tmp_path / "discovery" / "profile_discovery_bundle.json",
        provider="local",
        out_path=tmp_path / "profile_draft.json",
    )
    approve_profile_draft(
        tmp_path / "profile_draft.json",
        profile_id="demo-payment-approved",
        approved_by="pytest",
        out_path=tmp_path / "approved_profile.yaml",
    )
    approved = json.loads((tmp_path / "approved_profile.yaml").read_text(encoding="utf-8"))
    return bundle, approved


def test_run_multi_ai_cli_generates_artifacts_with_local_providers(tmp_path: Path) -> None:
    bundle, _profile = _bundle_and_profile(tmp_path)
    out = tmp_path / "multi_ai"
    command = [
        sys.executable,
        "-m",
        "ops_evidence_synthesis.cli",
        "run-multi-ai",
        "--bundle",
        str(tmp_path / "evidence_bundle.json"),
        "--profile",
        str(tmp_path / "approved_profile.yaml"),
        "--providers",
        "local-gemini,local-gpt-oss,local-mistral",
        "--out",
        str(out),
    ]
    completed = subprocess.run(command, cwd=ROOT, text=True, capture_output=True, check=True)
    assert "local-gemini: ok schema_valid=true" in completed.stdout
    model_runs = [json.loads(line) for line in (out / "model_runs.jsonl").read_text(encoding="utf-8").splitlines()]
    synthesis = json.loads((out / "multi_ai_synthesis.json").read_text(encoding="utf-8"))
    review_targets = json.loads((out / "review_targets.json").read_text(encoding="utf-8"))
    canonical_graph = json.loads((out / "canonical_review_graph.json").read_text(encoding="utf-8"))

    assert len(model_runs) == 3
    for run in model_runs:
        assert run["schema_version"] == "model_run.v1"
        assert run["provider_id"]
        assert run["status"] == "ok"
        assert run["schema_valid"] is True
        assert run["raw_output_sha256"]
        assert run["parsed_json_sha256"]
        assert run["retry"]["attempts"] == 1
        assert "estimated_cost_usd" in run["cost_estimate"]
        assert "raw_output" not in run
        assert run["safety_preflight"]["raw_logs_sent_to_providers"] is False

    assert synthesis["schema_version"] == "multi_ai_synthesis.v1"
    assert synthesis["evidence_sha256"] == bundle["evidence_sha256"]
    assert len(synthesis["agreement_groups"]) >= 1
    assert len(synthesis["disagreement_groups"]) >= 1
    assert len(synthesis["disagreement_themes"]) >= 1
    assert len(synthesis["validation_targets"]) >= 1
    assert synthesis["finding_summary"]["finding"]
    assert synthesis["token_usage"]["input_tokens"] >= 0
    assert synthesis["cost_estimate"]["pricing_source"] in {"env", "not_configured"}
    assert synthesis["score_note"] == SCORE_NOTE
    assert review_targets
    assert canonical_graph["schema_version"] == "canonical_review_graph.v1"
    assert "agreement_dimensions" in canonical_graph


def test_support_claim_without_evidence_id_is_unsupported() -> None:
    bundle = {"evidence_sha256": "sha", "evidence_refs": {"LOG-1": {"message": "safe"}}}
    model_runs = [
        {
            "provider_id": "local-gemini",
            "status": "ok",
            "schema_valid": True,
            "parsed_result": {
                "claims": [
                    {
                        "claim_type": "support",
                        "claim_text": "Restart loop is likely.",
                        "evidence_refs": [],
                        "missing_evidence": [],
                    }
                ]
            },
            "safety_preflight": {"passed": True},
        }
    ]
    synthesis = synthesize_multi_ai(bundle, model_runs)
    assert synthesis["claim_groups"][0]["unsupported"] is True
    assert synthesis["auto_archived"][0]["reason"] == "unsupported_support_without_evidence_id"


def test_safety_preflight_blocks_secret_like_model_input(tmp_path: Path) -> None:
    bundle, profile = _bundle_and_profile(tmp_path)
    unsafe = dict(bundle)
    unsafe["evidence_items"] = [
        *list(bundle.get("evidence_items") or []),
        {"evidence_id": "LEAK-1", "example_sanitized": "Authorization: Bearer raw-token-123456789"},
    ]
    result = run_multi_ai(unsafe, profile, providers=["local-gemini"], output_dir=tmp_path / "blocked")
    run = result["model_runs"][0]
    assert run["status"] == "blocked_by_safety_preflight"
    assert run["failure_reason"] == "secret_like_pattern_detected"
    assert run["safety_preflight"]["passed"] is False


def test_external_provider_unconfigured_is_skipped_not_configured(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("OES_ENABLE_REAL_AI", raising=False)
    bundle, profile = _bundle_and_profile(tmp_path)
    result = run_multi_ai(bundle, profile, providers=["gemini"], output_dir=tmp_path / "skip")
    run = result["model_runs"][0]
    assert run["provider_id"] == "gemini-enterprise-agent-platform"
    assert run["status"] == "skipped_not_configured"
    assert result["multi_ai_synthesis"]["skipped_provider_count"] == 1


def test_provider_failure_does_not_block_other_provider_synthesis(tmp_path: Path) -> None:
    bundle, profile = _bundle_and_profile(tmp_path)
    result = run_multi_ai(
        bundle,
        profile,
        providers=["local-gemini", "local-fail", "local-gpt-oss"],
        output_dir=tmp_path / "partial",
    )
    statuses = {run["provider_id"]: run["status"] for run in result["model_runs"]}
    assert statuses["local-fail"] == "failed"
    assert statuses["local-gemini"] == "ok"
    assert statuses["local-gpt-oss"] == "ok"
    assert result["multi_ai_synthesis"]["successful_provider_count"] == 2
    assert result["multi_ai_synthesis"]["failed_provider_count"] == 1
    assert len(result["multi_ai_synthesis"]["agreement_groups"]) >= 1


def test_synthesis_counts_legacy_error_status_as_failed() -> None:
    synthesis = synthesize_multi_ai(
        {"evidence_sha256": "sha", "evidence_refs": {}},
        [
            {
                "provider_id": "legacy-provider",
                "status": "error",
                "schema_valid": False,
                "parsed_result": {"claims": []},
                "safety_preflight": {"passed": True},
            }
        ],
    )

    assert synthesis["failed_provider_count"] == 1


def test_multi_ai_api_and_ui_panel_include_provider_statuses(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("OES_STORE", "sqlite")
    monkeypatch.setenv("OES_DB_PATH", str(tmp_path / "api.sqlite3"))
    bundle, profile = _bundle_and_profile(tmp_path)

    from ops_evidence_synthesis.api import app

    with TestClient(app) as client:
        response = client.post(
            "/ai/multi-run",
            json={
                "evidence_bundle": bundle,
                "approved_profile": profile,
                "providers": ["local-gemini", "local-gpt-oss", "local-mistral"],
                "mode": "local",
            },
        )
        assert response.status_code == 200, response.text
        payload = response.json()
        assert len(payload["model_runs"]) == 3
        assert len(payload["multi_ai_synthesis"]["provider_statuses"]) == 3
        assert "disagreement_themes" in payload["multi_ai_synthesis"]
        assert payload["multi_ai_synthesis"]["score_note"] == SCORE_NOTE

        status = client.get(f"/pipeline-status?pipeline_run_id={payload['pipeline_run_id']}").json()
        assert status["operation"] == "multi_ai"
        assert status["canonical_state"] == "completed"
        scheduled_step = next(step for step in status["steps"] if step["step_key"] == "providers_scheduled")
        assert scheduled_step["status"] == "completed"
        assert {item["state"] for item in status["state_timeline"]} >= {
            "uploaded",
            "validated",
            "safety_passed",
            "providers_scheduled",
            "provider_completed",
            "schema_validated",
            "arbitration_completed",
            "review_targets_persisted",
            "completed",
        }

        html = client.get(f"/?evidence_sha256={bundle['evidence_sha256']}&full=1").text
        assert "Multi-AI runs" in html
        assert "State: completed" in html
        assert "validated" in html
        assert "Disagreement Themes" in html
        assert "Canonical Review Graph" in html
        assert "Provider detection overlap" in html
        assert "Planner quality warnings" in html
        assert "Collection timezone" in html
        assert "Operator display timezone" in html
        assert "component_map_select" in html
        assert "Raw logs were not sent to providers" in html


def test_model_input_policy_states_raw_logs_are_not_sent(tmp_path: Path) -> None:
    bundle, profile = _bundle_and_profile(tmp_path)
    result = run_multi_ai(
        bundle,
        profile,
        providers=["local-gemini", "local-gpt-oss", "local-mistral"],
        output_dir=tmp_path / "policy",
    )
    synthesis = result["multi_ai_synthesis"]
    assert synthesis["safety"]["raw_logs_sent_to_providers"] is False
    assert "Raw logs are never sent to providers" in synthesis["safety"]["policy"]
    assert all(run["safety_preflight"]["raw_logs_sent_to_providers"] is False for run in result["model_runs"])


def test_disagreement_without_agreement_generates_validation_finding() -> None:
    bundle = {"evidence_sha256": "sha", "evidence_refs": {"LOG-1": {"message": "safe"}, "LOG-2": {"message": "safe"}}}
    model_runs = [
        {
            "provider_id": "provider-a",
            "status": "ok",
            "schema_valid": True,
            "parsed_result": {
                "claims": [
                    {
                        "claim_type": "support",
                        "claim_text": "External dependency timeout caused http_5xx.",
                        "core_target_type": "external_dependency_failure",
                        "component": "edge",
                        "evidence_refs": ["LOG-1"],
                        "missing_evidence": ["external dependency status"],
                    }
                ]
            },
            "safety_preflight": {"passed": True},
        },
        {
            "provider_id": "provider-b",
            "status": "ok",
            "schema_valid": True,
            "parsed_result": {
                "claims": [
                    {
                        "claim_type": "support",
                        "claim_text": "Audio delivery user impact is unclear.",
                        "core_target_type": "user_impact_signal_gap",
                        "component": "audio",
                        "evidence_refs": ["LOG-2"],
                        "missing_evidence": ["audio delivery metric"],
                    }
                ]
            },
            "safety_preflight": {"passed": True},
        },
    ]
    synthesis = synthesize_multi_ai(bundle, model_runs)
    assert len(synthesis["agreement_groups"]) == 0
    assert len(synthesis["disagreement_groups"]) == 2
    assert len(synthesis["disagreement_themes"]) >= 2
    finding = finding_impact_from_synthesis(synthesis)
    assert finding["finding"] == "Multi-AI disagreement requires validation"
    assert "No incident baseline agreement was found" in finding["impact"]
    assert synthesis["finding_summary"] == finding
