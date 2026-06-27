from __future__ import annotations

import copy
import json
import subprocess
import sys
from pathlib import Path

from ops_evidence_synthesis.canonical import sha256_json
from ops_evidence_synthesis.local_first import build_bundle_from_sanitized, sanitize_input, verify_sanitized_output
from ops_evidence_synthesis.profile_discovery import (
    approve_profile_draft,
    build_profile_discovery_bundle,
    discover_profile,
    draft_profile,
    profile_discovery_hash_payload,
)


ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = ROOT / "sample_projects" / "profile_discovery_sample"


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
