from __future__ import annotations

import copy
import json
import shutil
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient

from ops_evidence_synthesis.api import _canonical_review_graph_cards, _evidence_request_planner_panel, _pipeline_progress_panel, app
from ops_evidence_synthesis.local_first import build_bundle_from_sanitized, sanitize_input


ROOT = Path(__file__).resolve().parents[1]
STREAM_V3_DELL_REAL_API_SHA = "345430d258752cefef81bfb587b4c210799d02bfc849e0a7ac5dc4c48fddb1d6"
LEGACY_STREAM_V3_DELL_SHA = "64fa79977171fe9bad0664d115ff0ffcf4e248cd12a6a938e62d25cba7b12681"


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


def test_root_renders_drag_and_drop_evidence_bundle_upload(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OES_STORE", "sqlite")
    monkeypatch.setenv("OES_DB_PATH", str(tmp_path / "api.sqlite3"))

    with TestClient(app) as client:
        page = client.get("/")
        assert page.status_code == 200
        assert "Upload Sanitized Evidence Bundle" in page.text
        assert "Drop evidence_bundle.json here" in page.text
        assert "artifact-drop-zone" in page.text
        assert "uploadEvidenceBundleFile" in page.text
        assert "Raw logs, raw source files" in page.text
        assert "No Evidence Bundle selected" in page.text
        assert "AI proposals" not in page.text
        assert "Review priority" not in page.text
        assert "Stream transport disappeared" not in page.text


def test_fast_review_shell_embeds_precomputed_summary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OES_STORE", "sqlite")
    monkeypatch.setenv("OES_DB_PATH", str(tmp_path / "api.sqlite3"))
    summary_dir = tmp_path / "summaries"
    summary_dir.mkdir()
    monkeypatch.setenv("OES_PRECOMPUTED_REVIEW_DIR", str(summary_dir))
    monkeypatch.setenv("OES_UI_PRECOMPUTED_ONLY", "1")
    evidence_sha = "a" * 64
    (summary_dir / f"{evidence_sha}.json").write_text(
        json.dumps(
            {
                "schema_version": "precomputed_review_summary.v1",
                "evidence_sha256": evidence_sha,
                "updated_at": "2026-06-27T00:00:00Z",
                "summary": {
                    "status": "ok",
                    "finding": {"title": "Saved finding", "impact": "Saved impact"},
                    "review": {"primary_targets": 1, "validation_targets": 2},
                    "providers": {"success": 3, "total": 4},
                    "raw_log_policy": "not_uploaded",
                    "log_count": 6506,
                    "canonical_graph_sha256": "b" * 64,
                },
                "agent_trace": [
                    {
                        "title": "Sanitize local evidence",
                        "summary": "Raw logs were not uploaded.",
                        "status": "completed",
                        "artifact": "sanitized_events.jsonl",
                    }
                ],
                "review_graph_summary": {
                    "targets_total": 1,
                    "convergence_count": 0,
                    "conflict_count": 0,
                    "single_source_count": 1,
                    "primary_promoted_count": 0,
                    "incident_baseline": "open",
                    "technical_baseline": "open",
                    "provider_detection_overlap": "1/2",
                    "auto_archived_count": 0,
                    "summary": "1 visible validation target: 0 converged / 0 conflicting / 1 single-source.",
                    "score_definition": "Convergence score = claimed successful providers / all successful providers.",
                },
                "devops_loop": {
                    "title": "AI workflow is operated as production software",
                    "summary": "Regression and pipeline signals are visible.",
                    "items": [
                        {
                            "label": "Pipeline events",
                            "value": "append-only checkpoints",
                            "detail": "The run can be inspected as a workflow.",
                        }
                    ],
                },
                "targets": [
                    {
                        "title": "Configuration mismatch requires review",
                        "subsystem": "job_configuration",
                        "review_priority_score": 0.66,
                        "recommended_request_type": "instrumentation_consistency_query",
                        "claim": "Validate scheduler history.",
                        "provider_positions": [
                            {
                                "provider_id": "provider-a",
                                "stance": "claimed",
                                "model_run_hash": "run-a",
                                "one_line": "Flagged scheduler drift.",
                            },
                            {
                                "provider_id": "provider-b",
                                "stance": "silent",
                                "model_run_hash": "run-b",
                                "one_line": "Did not surface this target.",
                            },
                        ],
                        "agreement": {
                            "verdict": "single_source",
                            "convergence_score": 0.5,
                            "score_definition": "claimed successful providers / all successful providers",
                            "technical_baseline": "open",
                            "incident_baseline": "open",
                            "summary": "Only one provider claimed the target.",
                        },
                        "promotion": {
                            "state": "validation",
                            "blocked_reason": "user_impact_unverified",
                            "score_cap_applied": False,
                            "score_note": "Priority is not truth probability.",
                        },
                    }
                ],
            },
            ensure_ascii=False,
        )
    )

    with TestClient(app) as client:
        landing = client.get("/")
        page = client.get(f"/?evidence_sha256={evidence_sha}")
        detail = client.get(f"/ui/full-review-page?evidence_sha256={evidence_sha}")
        api_view = client.get(f"/ui/api?evidence_sha256={evidence_sha}")
        graph_view = client.get(f"/ui/review-graph?evidence_sha256={evidence_sha}")
        report_view = client.get(f"/ui/report.md?evidence_sha256={evidence_sha}")
        review_targets = client.get(f"/review-targets?evidence_sha256={evidence_sha}")
        review_graph = client.get(f"/review/graph?evidence_sha256={evidence_sha}")

    assert landing.status_code == 200
    assert "Saved finding" in landing.text
    assert "Upload Sanitized Evidence Bundle" not in landing.text
    assert "Write token" not in landing.text
    assert page.status_code == 200
    assert "Saved finding" in page.text
    assert "Saved impact" in page.text
    assert "3 / 4" in page.text
    assert "not uploaded" in page.text
    assert "6,506 sanitized logs" in page.text
    assert "Agent Trace" in page.text
    assert "Review Graph Arbitration" in page.text
    assert "0 converged / 0 conflicting / 1 single-source" in page.text
    assert "DevOps Improvement Loop" in page.text
    assert "single_source" in page.text
    assert "claimed 1 / silent 1" in page.text
    assert "Convergence score = claimed successful providers / all successful providers." in page.text
    assert "Configuration mismatch requires review" in page.text
    assert "Loading saved result" not in page.text
    assert "Detailed review state is loading" not in page.text
    assert "Open full page directly" not in page.text
    assert ">--<" not in page.text

    assert detail.status_code == 200
    assert "Saved finding" in detail.text
    assert "Provider Frontier" not in detail.text
    assert "Configuration mismatch requires review" in detail.text
    assert "Provider positions" in detail.text
    assert "provider-a" in detail.text
    assert "provider-b" in detail.text
    assert "Promotion gate" in detail.text
    assert "user_impact_unverified" in detail.text
    assert "Definition: claimed successful providers / all successful providers" in detail.text
    assert api_view.status_code == 200
    assert "Read-only API View" in api_view.text
    assert "Summary JSON" in api_view.text
    assert "Review Graph JSON" in api_view.text
    assert graph_view.status_code == 200
    assert "Review Graph" in graph_view.text
    assert "Nodes and edges" in graph_view.text
    assert report_view.status_code == 200
    assert "Incident Review Report" in report_view.text
    assert "This report is review material, not an accepted incident cause." in report_view.text
    assert "Top Review Targets" in report_view.text
    assert "Promotion gate:" in report_view.text
    assert review_targets.status_code == 200
    assert review_targets.json()["summary"]["source"] == "precomputed_review_summary"
    assert review_targets.json()["targets"][0]["evidence_sha256"] == evidence_sha
    assert review_graph.status_code == 200
    assert review_graph.json()["canonical_graph_status"] == "precomputed"
    assert review_graph.json()["graph"]["node_count"] >= 4
    assert review_graph.json()["graph"]["edge_count"] >= 1
    assert review_graph.json()["canonical_review_graph"]["nodes"]
    assert review_graph.json()["canonical_review_graph"]["edges"]
    assert review_graph.json()["canonical_review_graph"]["review_graph_summary"]["targets_total"] == 1


def test_precomputed_only_ui_returns_404_for_missing_review(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OES_STORE", "sqlite")
    monkeypatch.setenv("OES_DB_PATH", str(tmp_path / "api.sqlite3"))
    monkeypatch.setenv("OES_PRECOMPUTED_REVIEW_DIR", str(tmp_path / "summaries"))
    monkeypatch.setenv("OES_UI_PRECOMPUTED_ONLY", "1")
    evidence_sha = "b" * 64

    with TestClient(app) as client:
        landing = client.get("/")
        detail_without_sha = client.get("/ui/full-review-page")
        targets_without_sha = client.get("/review-targets")
        page = client.get(f"/?evidence_sha256={evidence_sha}")
        full_page = client.get(f"/?evidence_sha256={evidence_sha}&full=1")
        detail = client.get(f"/ui/full-review-page?evidence_sha256={evidence_sha}")
        full_detail = client.get(f"/ui/full-review-page?evidence_sha256={evidence_sha}&full=1")
        api_view = client.get(f"/ui/api?evidence_sha256={evidence_sha}")
        graph_view = client.get(f"/ui/review-graph?evidence_sha256={evidence_sha}")
        report_view = client.get(f"/ui/report.md?evidence_sha256={evidence_sha}")
        summary = client.get(f"/ui/summary?evidence_sha256={evidence_sha}")
        review_targets = client.get(f"/review-targets?evidence_sha256={evidence_sha}")
        review_graph = client.get(f"/review/graph?evidence_sha256={evidence_sha}")
        blocked = {
            path: client.get(path).status_code
            for path in [
                "/docs",
                "/redoc",
                "/openapi.json",
                "/reviews",
                "/proposals",
                "/comparisons",
                "/clusters",
                "/providers",
                "/workflow/provider-policy",
                f"/bundles/{evidence_sha}",
                f"/pipeline-status?evidence_sha256={evidence_sha}",
            ]
        }

    assert landing.status_code == 200
    assert "Upload Sanitized Evidence Bundle" not in landing.text
    assert "Write token" not in landing.text
    assert detail_without_sha.status_code == 404
    assert targets_without_sha.status_code == 404
    assert page.status_code == 404
    assert full_page.status_code == 404
    assert detail.status_code == 404
    assert full_detail.status_code == 404
    assert api_view.status_code == 404
    assert graph_view.status_code == 404
    assert report_view.status_code == 404
    assert summary.status_code == 404
    assert review_targets.status_code == 404
    assert review_graph.status_code == 404
    assert blocked == {path: 404 for path in blocked}
    assert "precomputed review not found" in page.text
    assert "No persisted finding yet" not in page.text
    assert "Provider positions were not projected" not in detail.text


def test_precomputed_only_ui_serves_legacy_public_review_links_as_canonical(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    summaries = tmp_path / "summaries"
    summaries.mkdir()
    shutil.copyfile(
        ROOT / "data" / "precomputed_review_summaries" / f"{STREAM_V3_DELL_REAL_API_SHA}.json",
        summaries / f"{STREAM_V3_DELL_REAL_API_SHA}.json",
    )
    monkeypatch.setenv("OES_STORE", "sqlite")
    monkeypatch.setenv("OES_DB_PATH", str(tmp_path / "api.sqlite3"))
    monkeypatch.setenv("OES_PRECOMPUTED_REVIEW_DIR", str(summaries))
    monkeypatch.setenv("OES_UI_PRECOMPUTED_ONLY", "1")
    monkeypatch.setenv("OES_PRECOMPUTED_REVIEW_CACHE_SECONDS", "0")

    with TestClient(app) as client:
        page = client.get(f"/?evidence_sha256={LEGACY_STREAM_V3_DELL_SHA}")
        detail = client.get(f"/ui/full-review-page?evidence_sha256={LEGACY_STREAM_V3_DELL_SHA}")
        api_view = client.get(f"/ui/api?evidence_sha256={LEGACY_STREAM_V3_DELL_SHA}")
        graph_view = client.get(f"/ui/review-graph?evidence_sha256={LEGACY_STREAM_V3_DELL_SHA}")
        report_view = client.get(f"/ui/report.md?evidence_sha256={LEGACY_STREAM_V3_DELL_SHA}")
        summary = client.get(f"/ui/summary?evidence_sha256={LEGACY_STREAM_V3_DELL_SHA}")
        review_targets = client.get(f"/review-targets?evidence_sha256={LEGACY_STREAM_V3_DELL_SHA}")
        review_graph = client.get(f"/review/graph?evidence_sha256={LEGACY_STREAM_V3_DELL_SHA}")

    for response in (page, detail, api_view, graph_view, report_view, summary, review_targets, review_graph):
        assert response.status_code == 200
    for response in (page, detail, api_view, graph_view, report_view):
        assert LEGACY_STREAM_V3_DELL_SHA[:12] not in response.text
        assert STREAM_V3_DELL_REAL_API_SHA[:12] in response.text
    assert f"evidence_sha256={STREAM_V3_DELL_REAL_API_SHA}" in page.text
    assert f"evidence_sha256={STREAM_V3_DELL_REAL_API_SHA}" in detail.text
    assert summary.json()["evidence_sha256"] == STREAM_V3_DELL_REAL_API_SHA
    assert review_targets.json()["targets"][0]["evidence_sha256"] == STREAM_V3_DELL_REAL_API_SHA
    assert review_graph.json()["canonical_graph_status"] == "precomputed"


def test_pipeline_progress_panel_renders_canonical_states_and_reason_codes() -> None:
    html = _pipeline_progress_panel(
        {
            "pipeline_run_id": "pipe-ui",
            "evidence_sha256": "sha-ui",
            "operation": "multi_ai",
            "status": "failed",
            "canonical_state": "provider_failed",
            "current_step": "providers_completed",
            "current_step_label": "Provider runs completed",
            "progress_percent": 50,
            "blocking_reason": "provider_timeout",
            "provider_total": 2,
            "provider_success": 1,
            "provider_failed": 1,
            "provider_skipped": 0,
            "review_target_count": 0,
            "validation_target_count": 0,
            "child_bundle_count": 0,
            "steps": [
                {
                    "step_key": "providers_scheduled",
                    "step_label": "Provider runs scheduled",
                    "status": "completed",
                    "canonical_state": "providers_scheduled",
                    "message": "2 provider run(s) scheduled.",
                },
                {
                    "step_key": "providers_completed",
                    "step_label": "Provider runs completed",
                    "status": "timeout",
                    "canonical_state": "provider_failed",
                    "message": "mistral timeout.",
                    "reason_code": "provider_timeout",
                },
            ],
            "events": [
                {
                    "step_key": "providers_completed",
                    "status": "timeout",
                    "canonical_state": "provider_failed",
                    "reason_code": "provider_timeout",
                    "provider_id": "mistral",
                    "message": "mistral timeout.",
                }
            ],
            "state_timeline": [
                {"state": "providers_scheduled"},
                {"state": "provider_failed", "reason_code": "provider_timeout"},
            ],
            "active_reasons": ["provider_timeout"],
        }
    )

    assert "State: provider_failed" in html
    assert "providers_scheduled" in html
    assert "provider_failed" in html
    assert "Blocking reason: provider_timeout" in html
    assert "provider_timeout" in html


def test_upload_local_first_bundle_validates_persists_and_renders_summary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OES_STORE", "sqlite")
    monkeypatch.setenv("OES_DB_PATH", str(tmp_path / "api.sqlite3"))
    bundle = _redaction_fixture_bundle(tmp_path)

    with TestClient(app) as client:
        uploaded = client.post("/bundles/upload", json={"bundle": bundle})
        assert uploaded.status_code == 200, uploaded.text
        payload = uploaded.json()
        assert payload["status"] == "accepted"
        assert payload["evidence_sha256"] == bundle["evidence_sha256"]
        assert payload["server_validation"]["passed"] is True
        assert payload["analysis_policy"]["profile_mode"] == "inferred"
        assert payload["analysis_policy"]["allow_primary_candidate"] is False

        fetched = client.get(f"/bundles/{bundle['evidence_sha256']}")
        assert fetched.status_code == 200
        fetched_bundle = fetched.json()
        assert fetched_bundle["bundle_type"] == "sanitized_evidence_bundle"
        assert fetched_bundle["source"]["service"] == "unknown-sample"
        assert fetched_bundle["local_first_summary"]["raw_logs_uploaded"] is False

        shell = client.get(f"/?evidence_sha256={bundle['evidence_sha256']}")
        assert shell.status_code == 200
        assert "Report shell ready" in shell.text
        assert "ui/full-review-page" in shell.text
        assert "Evidence Request Planner" not in shell.text

        page = client.get(f"/?evidence_sha256={bundle['evidence_sha256']}&full=1")
        assert page.status_code == 200
        assert "Local-first safety check" in page.text
        assert "Upload Sanitized Evidence Bundle" in page.text
        assert "Raw log policy: not_uploaded" in page.text
        assert "Primary candidate allowed: false" in page.text
        assert "Evidence Request Planner" in page.text
        assert "Planner does not execute commands" in page.text
        assert "allow_config_metadata_only" in page.text
        assert "Collection Instructions" in page.text
        assert "Copy collection notes" in page.text
        assert "Technical JSON" in page.text
        assert "Copy answers JSON" in page.text
        assert "planner-write-token-input" in page.text
        assert "Required for Generate refined plan" in page.text
        assert "Result output" in page.text
        assert "Refined Output: Collection Instructions" in page.text
        assert "Not generated in this browser yet." in page.text
        assert "Collection Instructions were already current." in page.text
        assert "planner-refine-progress" in page.text
        assert "planner-refine-button" in page.text
        assert "revealPlannerCollectionInstructions" in page.text
        assert page.text.index("Collection Instructions") < page.text.index("Technical JSON")
        assert "Bundle Provenance" in page.text
        assert "Evidence Lineage" not in page.text
        assert "Follow-up Collections" not in page.text
        assert page.text.index("Bundle Provenance") < page.text.index("Evidence Request Planner")
        assert "missing_command" in page.text


def test_aggregate_bundle_renders_evidence_request_planner() -> None:
    bundle = {
        "schema_version": "ops-evidence-bundle/v1",
        "evidence_sha256": "sha-aggregate",
        "service": "stream_v3-aggregate",
        "environment": "stream_v3",
        "incident_window": {"start": "2026-06-15T22:00:00Z", "end": "2026-06-16T00:00:00Z"},
        "profile": {"profile_id": "stream_v3"},
        "system_profile": {"system_type": "livestream_pipeline"},
        "component_map": {"rtmps_ffmpeg": "ffmpeg process and RTMPS transport send path."},
        "metric_semantics": {},
        "log_sources": [],
        "operational_evidence": [
            {
                "evidence_id": "OPS-001",
                "request_id": "throughput_signal_query",
                "summary": "RTMPS connection evidence.",
                "subsystem": "rtmps_ffmpeg",
                "incident_count": 1,
                "baseline_count": 10,
                "samples": [],
            }
        ],
    }

    panel = _evidence_request_planner_panel(bundle)

    assert "Evidence Request Planner" in panel
    assert "stream_v3" in panel
    assert "planner-refine-button" in panel
    assert "Collection Instructions" in panel


def test_upload_rejects_raw_or_tampered_bundle_without_echoing_secret(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OES_STORE", "sqlite")
    monkeypatch.setenv("OES_DB_PATH", str(tmp_path / "api.sqlite3"))
    bundle = _redaction_fixture_bundle(tmp_path)
    leaked_secret = "leaked-token-1234567890"

    with TestClient(app) as client:
        raw = client.post(
            "/bundles/upload",
            json={"message": f"Authorization: Bearer {leaked_secret}", "raw_log_policy": "uploaded"},
        )
        assert raw.status_code == 400
        assert leaked_secret not in raw.text
        assert "contract_mismatch" in raw.text

        tampered = copy.deepcopy(bundle)
        tampered["source"]["service"] = "changed-service"
        mismatch = client.post("/bundles/upload", json=tampered)
        assert mismatch.status_code == 400
        assert "evidence_sha256_mismatch" in mismatch.text

        unsafe = copy.deepcopy(bundle)
        unsafe["evidence_items"][0]["example_sanitized"] = f"password={leaked_secret}"
        unsafe["evidence_sha256"] = "not-the-right-sha"
        response = client.post("/bundles/upload", json=unsafe)
        assert response.status_code == 400
        assert "unsafe_content" in response.text
        assert leaked_secret not in response.text


def test_upload_child_bundle_accepts_and_renders_lineage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OES_STORE", "sqlite")
    monkeypatch.setenv("OES_DB_PATH", str(tmp_path / "api.sqlite3"))
    out = tmp_path / "local_first"
    sanitize_input(ROOT / "sample_logs" / "redaction_fixture.jsonl", out)
    parent = build_bundle_from_sanitized(
        out / "sanitized_events.jsonl",
        service="unknown-sample",
        environment="prod",
        start="2026-06-16T00:00:00Z",
        end="2026-06-16T18:00:00Z",
        profile_name="generic",
        out_path=out / "evidence_bundle.json",
    )
    child = build_bundle_from_sanitized(
        out / "sanitized_events.jsonl",
        service="unknown-sample",
        environment="prod",
        start="2026-06-16T00:00:00Z",
        end="2026-06-16T18:00:00Z",
        profile_name="generic",
        parent_evidence_sha256=str(parent["evidence_sha256"]),
        evidence_request_plan_id="PLAN-TEST-LINEAGE",
        collection_mode="manual_read_only_collection",
        out_path=tmp_path / "child" / "child_evidence_bundle.json",
    )

    with TestClient(app) as client:
        assert client.post("/bundles/upload", json={"bundle": parent}).status_code == 200
        uploaded_child = client.post("/bundles/upload", json={"bundle": child})
        assert uploaded_child.status_code == 200, uploaded_child.text
        payload = uploaded_child.json()
        assert payload["server_validation"]["passed"] is True
        assert payload["lineage"]["parent_evidence_sha256"] == parent["evidence_sha256"]
        assert payload["lineage"]["evidence_request_plan_id"] == "PLAN-TEST-LINEAGE"
        assert payload["lineage"]["collection_mode"] == "manual_read_only_collection"

        parent_page = client.get(f"/?evidence_sha256={parent['evidence_sha256']}&full=1")
        assert parent_page.status_code == 200
        assert "Follow-up Collections" in parent_page.text
        assert "Child Evidence Bundle" in parent_page.text
        assert "PLAN-TEST-LINEAGE" in parent_page.text

        child_page = client.get(f"/?evidence_sha256={child['evidence_sha256']}&full=1")
        assert child_page.status_code == 200
        assert "Bundle Provenance" in child_page.text
        assert "Parent Bundle" in child_page.text
        assert str(parent["evidence_sha256"]) in child_page.text


def test_canonical_graph_cards_use_state_specific_baseline_note() -> None:
    html = _canonical_review_graph_cards(
        {
            "schema_version": "canonical_review_graph.v1",
            "canonical_graph_status": "persisted",
            "canonical_graph_sha256": "graph-sha",
            "input_fingerprint_sha256": "input-sha",
            "summary": {"primary_count": 1, "validation_count": 0},
            "agreement_dimensions": {
                "provider_detection_overlap": {"value": "2/3"},
                "technical_baseline_agreement": {"established": False},
                "incident_baseline_agreement": {"established": True},
                "cause_agreement": {"value": "none"},
                "impact_agreement": {"value": "baseline"},
            },
            "primary_targets": [],
            "validation_targets": [],
        }
    )

    assert "Incident promotion is established, but technical support is not established" in html
    assert "Technical support agreement does not promote" not in html


def test_canonical_graph_cards_explain_refreshed_snapshot_metadata() -> None:
    html = _canonical_review_graph_cards(
        {
            "schema_version": "canonical_review_graph.v1",
            "canonical_graph_status": "persisted",
            "canonical_graph_sha256": "graph-sha",
            "input_fingerprint_sha256": "current-input",
            "persisted_input_fingerprint_sha256": "previous-input",
            "persisted_created_at": "2026-06-20T00:00:00Z",
            "stale_reason": "input_fingerprint_changed",
            "summary": {"primary_count": 0, "validation_count": 0},
            "agreement_dimensions": {
                "provider_detection_overlap": {"value": "0/0"},
                "technical_baseline_agreement": {"established": False},
                "incident_baseline_agreement": {"established": False},
            },
            "primary_targets": [],
            "validation_targets": [],
        }
    )

    assert "Canonical graph was refreshed before persistence" in html
    assert "Previous input fingerprint" in html
