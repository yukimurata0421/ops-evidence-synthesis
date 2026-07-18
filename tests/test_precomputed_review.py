from __future__ import annotations

import json
import re
from pathlib import Path

from ops_evidence_synthesis.canonical import sha256_json
from ops_evidence_synthesis.ai.provider_registry import build_multi_ai_providers
from ops_evidence_synthesis.ingest import ingest_jsonl
from ops_evidence_synthesis.models import IncidentWindow
from ops_evidence_synthesis.precomputed_review import (
    PUBLIC_DEMO_PROVIDERS,
    build_precomputed_review_summary,
    stable_precomputed_review_json,
)
from ops_evidence_synthesis.storage.sqlite_store import SQLiteStore
from ops_evidence_synthesis.synthesis.pipeline import run_pipeline
from ops_evidence_synthesis.web import precomputed_review as web_precomputed
from ops_evidence_synthesis.web.precomputed_review import (
    _canonical_precomputed_review_sha,
    _fast_detail_target_card,
    _precomputed_review_payload,
    _precomputed_review_graph_response,
    _public_precomputed_landing_page,
    _landing_workspace_preview_html,
    _render_precomputed_graph_page,
    _render_precomputed_markdown_report,
    _render_precomputed_review_detail_page,
    _rollup_audit_html,
    _workspace_target_detail_html,
    rescore_demo_payload,
    render_rescore_demo_page,
)
from scripts.generate_precomputed_review_from_multi_run import (
    _merge_duplicate_public_target,
    _provider_summary_title,
    _public_review_counts,
    _public_target_class,
)


ROOT = Path(__file__).resolve().parents[1]
PUBLIC_SAMPLE_SHA = "90df7ce2f75392b1d2e0043f5e45076b223e9001d1eb1fde8bc83beeed0b42af"
PUBLIC_FLAGSHIP_SHA = "ee52464dffb1daebbfc4b2ac8e030d946367cb6ad54c3d4cb93391c69292d809"
PUBLIC_REAL_API_SHA = "b99da97cab19f026b5475cdaa6100fdd6ebb6d96466a43e6b62a44b99ac414ec"
REAL_API_QWEN_GLM_SHA = "7ca07bd8ed4bcb6009b654f17c40576a7b3462c62b2c74011c1623043550ccfb"
STREAM_V3_DELL_REAL_API_SHA = "b7d56da85abe109ab044e05d4fc7b40462615e5b230db2b570f717c83762ab96"
LEGACY_STREAM_V3_DELL_SHA = "64fa79977171fe9bad0664d115ff0ffcf4e248cd12a6a938e62d25cba7b12681"
STREAM_V3_ARENA_REAL_API_SHA = "8d165418fca88f856d8525bbdae804b6b649455450796b2dc44d2134b21abd9a"
PUBLIC_PROFILE_CONTEXTS = {
    "amazon-notify": ROOT / "data" / "public_profile_contexts" / "amazon_notify_sample",
    "payment-api": ROOT / "data" / "public_profile_contexts" / "payment_api_sample",
}


def test_provider_summary_title_distinguishes_local_and_real_modes() -> None:
    assert _provider_summary_title(
        valid_provider_count=5,
        provider_count=5,
        log_count=44191,
        service="stream_v3_runtime",
        provider_mode="deterministic_local_gcs_review",
    ).startswith("Five local deterministic providers")
    assert _provider_summary_title(
        valid_provider_count=5,
        provider_count=5,
        log_count=44944,
        service="amazon-notify",
        provider_mode="real_api_private_gcs_cloud_run_job_postgres_ledger",
    ).startswith("Five real providers")
    assert _provider_summary_title(
        valid_provider_count=5,
        provider_count=5,
        log_count=45000,
        service="stream_v3_runtime",
        provider_mode="recorded_real_api_deterministic_resynthesis",
    ).startswith("Five real providers")


def test_runtime_release_provenance_overlays_publication_and_image_roles(monkeypatch) -> None:
    monkeypatch.setenv("OES_PUBLISHED_REPOSITORY_HEAD_SHA", "a" * 40)
    monkeypatch.setenv("OES_DEPLOYED_IMAGE_DIGEST", "sha256:" + "b" * 64)

    payload = web_precomputed._with_runtime_release_provenance(
        {
            "provenance": {
                "tested_implementation_commit_sha": "c" * 40,
                "artifact_generation_commit_sha": "d" * 40,
            }
        }
    )

    assert payload["provenance"]["tested_implementation_commit_sha"] == "c" * 40
    assert payload["provenance"]["artifact_generation_commit_sha"] == "d" * 40
    assert payload["provenance"]["published_repository_head_sha"] == "a" * 40
    assert payload["provenance"]["deployed_image_digest"] == "sha256:" + "b" * 64


def test_rollup_audit_shows_type_counts_provider_memberships_and_source_candidates() -> None:
    html = _rollup_audit_html(
        {
            "rollup": {
                "source_candidate_count": 2,
                "distinct_target_type_count": 2,
                "source_candidate_type_counts": {"restart_loop": 1, "runtime_exception": 1},
                "provider_candidate_membership_counts": {"provider-a": 1, "provider-b": 1},
                "supporting_provider_counts": {"provider-a": 1},
                "countering_provider_counts": {"provider-b": 1},
                "target_type_divergence_penalty": 0.03,
            },
            "overmerge_review": {
                "warning": "Different target types were merged into one review unit.",
            },
            "source_candidates": [
                {
                    "canonical_target_type": "restart_loop",
                    "provider_ids": ["provider-a"],
                    "supporting_provider_ids": ["provider-a"],
                    "evidence_refs": ["OPS-001"],
                    "claim": "worker restart loop",
                },
                {
                    "canonical_target_type": "runtime_exception",
                    "provider_ids": ["provider-b"],
                    "countering_provider_ids": ["provider-b"],
                    "evidence_refs": ["OPS-002"],
                    "claim": "uncaught worker exception",
                },
            ],
        }
    )

    assert "Canonical rollup review required" in html
    assert "2 source candidate(s), 2 distinct target type(s)" in html
    assert "restart_loop" in html
    assert "runtime_exception" in html
    assert "provider-a" in html
    assert "Source-candidate type counts" in html
    assert "Provider candidate memberships" in html
    assert "Supporting provider counts" in html
    assert "Countering provider counts" in html
    assert "candidate vote(s)" not in html
    assert "OPS-002" in html


def test_public_target_dedupe_preserves_rollup_audit_details() -> None:
    first = {
        "canonical_review_unit": "runtime_recovery",
        "source_candidates": [
            {
                "source_candidate_id": "candidate-a",
                "provider_ids": ["provider-a", "provider-c"],
                "supporting_provider_ids": ["provider-a"],
                "countering_provider_ids": ["provider-c"],
                "canonical_target_type": "restart_loop",
                "evidence_refs": ["OPS-001"],
                "claim": "worker restart loop",
            }
        ],
        "rollup": {
            "source_candidate_count": 1,
            "provider_candidate_membership_counts": {"provider-a": 1, "provider-c": 1},
            "supporting_provider_counts": {"provider-a": 1},
            "countering_provider_counts": {"provider-c": 1},
            "source_candidate_type_counts": {"restart_loop": 1},
            "distinct_target_type_count": 1,
        },
        "provider_positions": [{"provider_id": "provider-a", "stance": "claimed"}],
        "agreement": {},
        "classification": {},
        "promotion": {},
        "review_reason": {"factors": []},
        "raw": {},
    }
    second = {
        **first,
        "source_candidates": [
            {
                "source_candidate_id": "candidate-b",
                "provider_ids": ["provider-b"],
                "supporting_provider_ids": ["provider-b"],
                "canonical_target_type": "runtime_exception",
                "evidence_refs": ["OPS-002"],
                "claim": "uncaught worker exception",
            }
        ],
        "rollup": {
            "source_candidate_count": 1,
            "provider_candidate_membership_counts": {"provider-b": 1},
            "supporting_provider_counts": {"provider-b": 1},
            "countering_provider_counts": {},
            "source_candidate_type_counts": {"runtime_exception": 1},
            "distinct_target_type_count": 1,
        },
        "provider_positions": [{"provider_id": "provider-b", "stance": "claimed"}],
    }

    merged = _merge_duplicate_public_target(first, second)

    assert merged["rollup"]["source_candidate_count"] == 2
    assert merged["rollup"]["source_candidate_type_counts"] == {
        "restart_loop": 1,
        "runtime_exception": 1,
    }
    assert merged["rollup"]["provider_candidate_membership_counts"] == {
        "provider-a": 1,
        "provider-b": 1,
        "provider-c": 1,
    }
    assert merged["rollup"]["supporting_provider_counts"] == {
        "provider-a": 1,
        "provider-b": 1,
    }
    assert merged["rollup"]["countering_provider_counts"] == {"provider-c": 1}
    assert merged["rollup"]["independent_support_provider_count"] == 2
    assert merged["rollup"]["target_type_votes"] == merged["rollup"]["source_candidate_type_counts"]
    assert merged["rollup"]["provider_vote_counts"] == merged["rollup"][
        "provider_candidate_membership_counts"
    ]
    assert merged["rollup"]["target_type_divergence_penalty"] == 0.03
    assert merged["overmerge_review"]["required"] is True
    assert len(merged["source_candidates"]) == 2


def test_fast_gcp_provider_mode_uses_current_platform_label() -> None:
    assert web_precomputed._detail_provider_mode_label(
        {"generation": {"provider_mode": "fast_gcp_vertex_gemini_flash_lite"}}
    ) == "Fast GCP / Agent Platform / Gemini 3.1 Flash-Lite"
    assert web_precomputed._detail_provider_mode_label(
        {
            "generation": {
                "provider_mode": "fast_gcp_vertex_gemini_flash_lite_gemma4_cross_check"
            }
        }
    ) == "Fast GCP / Agent Platform / Gemini 3.1 Flash-Lite + Gemma 4"

def test_public_landing_page_lists_real_api_reviews_only(monkeypatch) -> None:
    monkeypatch.delenv("OES_PRECOMPUTED_REVIEW_DIR", raising=False)
    monkeypatch.delenv("OES_PRECOMPUTED_REVIEW_DIRS", raising=False)

    html = _public_precomputed_landing_page()

    assert PUBLIC_REAL_API_SHA[:12] in html
    assert REAL_API_QWEN_GLM_SHA[:12] in html
    assert STREAM_V3_DELL_REAL_API_SHA[:12] in html
    assert STREAM_V3_ARENA_REAL_API_SHA[:12] in html
    assert "Primary Review" in html
    assert "Cross-Domain Scale Validation" in html
    assert "Scale validation is the curated review set above, not a fourth hidden run." in html
    assert "No scale validation review is available." not in html
    assert "Archived recorded runs" in html
    assert "Rows" in html
    assert "Chunks" in html
    assert "Coverage" in html
    assert "44,944" in html
    assert "45,000" in html
    assert "50,000" in html
    assert PUBLIC_SAMPLE_SHA[:12] not in html
    assert PUBLIC_FLAGSHIP_SHA[:12] not in html
    assert "sanitized source context" in html
    assert "AIが断定する前に、運用証拠を固定する。" in html
    assert "Provider convergence creates review targets, not accepted incident causes" in html
    assert "Watch rescore loop" in html
    assert "ADK-compatible trace included" in html
    assert "provider signal, not a verdict" in html
    assert "0 AUTO-PROMOTED CAUSES" in html
    assert "Replay path for reproducibility, AI path for real evidence." in html
    assert "Public Replay" in html
    assert "More Data Rescore" in html
    assert "Fast GCP Review" in html
    assert "Gemini Flash Lite" in html
    assert "Full Forensic AI Review" in html
    assert "measured review graph generation is about 11 seconds" in html
    assert "Built as the evidence gate before automated action." in html
    assert "Markdown incident report" in html
    assert "Review graph" in html
    assert "Multi-AI disagreement requires validation" not in html
    assert "/ui/rescore-demo?id=amazon-notify-more-data-rescore" in html
    assert f"/ui/report.md?evidence_sha256={STREAM_V3_DELL_REAL_API_SHA}" in html
    assert html.index(STREAM_V3_DELL_REAL_API_SHA[:12]) < html.index(PUBLIC_REAL_API_SHA[:12])
    assert "<b>0</b><span>primary candidates</span>" in html
    assert "transport_sender" in html
    assert "VALIDATION TARGET" in html
    assert "primary - 5/5" not in html


def test_landing_workspace_preview_is_driven_by_selected_review_payload() -> None:
    html = _landing_workspace_preview_html(
        {
            "targets": [
                {
                    "canonical_review_unit": "database_pool_saturation",
                    "class": "primary_candidate",
                    "review_priority_score": 0.91,
                    "operational_mechanism": "Connections wait because the configured pool is exhausted.",
                    "provider_positions": [
                        {"provider_id": "gemini-enterprise-agent-platform", "stance": "claimed"},
                        {"provider_id": "qwen-agent-platform", "stance": "silent"},
                    ],
                    "classification": {"provider_support": "1/2"},
                    "evidence_refs": ["DB-POOL-001", "DB-WAIT-002"],
                    "next_validation_question": "Did request latency rise in the same window?",
                    "why_not_promoted": "User impact remains unverified.",
                }
            ]
        }
    )

    assert "database_pool_saturation" in html
    assert "PRIMARY CANDIDATE" in html
    assert "0.91" in html
    assert "primary candidate - 1/2" in html
    assert "DB-POOL-001" in html
    assert "Did request latency rise in the same window?" in html
    assert "chromium_capture" not in html


def test_public_landing_cards_match_linked_payloads(monkeypatch) -> None:
    monkeypatch.delenv("OES_PRECOMPUTED_REVIEW_DIR", raising=False)
    monkeypatch.delenv("OES_PRECOMPUTED_REVIEW_DIRS", raising=False)

    html = _public_precomputed_landing_page()
    for sha in (PUBLIC_REAL_API_SHA, STREAM_V3_DELL_REAL_API_SHA, STREAM_V3_ARENA_REAL_API_SHA):
        payload = _load_json(ROOT / "data" / "precomputed_review_summaries" / f"{sha}.json")
        summary = payload["summary"]
        providers = summary["providers"]
        review = summary["review"]
        context = payload["analysis_context"]
        card = _landing_card_html(html, sha)

        assert f"<dd>{providers['success']}/{providers['total']}</dd>" in card
        assert f"<dd>{review['primary_targets']}</dd>" in card
        target_count = int(review["primary_targets"]) + int(review["validation_targets"])
        assert f"<dd>{target_count}</dd>" in card
        assert f"<dd>{int(context['provider_full_corpus_chunk_count'])}</dd>" in card
        assert "100.0%" in card
        assert f"/ui/full-review-page?evidence_sha256={sha}" in card
        assert f"/ui/api?evidence_sha256={sha}" in card
        assert f"/ui/review-graph?evidence_sha256={sha}" in card
        assert f"/ui/report.md?evidence_sha256={sha}" in card


def test_public_markdown_report_renders_human_review_boundary() -> None:
    payload = _load_json(ROOT / "data" / "precomputed_review_summaries" / f"{STREAM_V3_DELL_REAL_API_SHA}.json")

    report = _render_precomputed_markdown_report(STREAM_V3_DELL_REAL_API_SHA, payload)

    assert report.startswith("# Incident Review Report:")
    assert "This report is review material, not an accepted incident cause." in report
    assert "## Evidence Boundary" in report
    assert "DB coverage ledger:" in report
    assert "Provider corpus:" in report
    assert "## Provider Statuses" in report
    assert "| Provider | Model | Status | Schema valid | Output hash |" in report
    assert "## Human Review Questions" in report
    assert "Approved profile context: `stream_v3_runtime_source_approved_20260711`" in report
    assert "approved_human_reviewed" in report
    assert "## Review Queries This Report Supports" in report
    assert "List targets that are blocked by missing user-impact evidence." in report
    assert "## Top Review Targets" in report
    assert "Provider stance:" in report
    assert "Promotion gate:" in report
    assert "review urgency, not truth probability" in report
    assert "majority-vote truth" in report


def test_public_rescore_demo_is_renderable() -> None:
    html = render_rescore_demo_page("amazon-notify-more-data-rescore")
    payload = rescore_demo_payload("amazon-notify-more-data-rescore")

    assert "More data rescore demo" in html
    assert "Gemini-led control plane" in html
    assert "gemini-enterprise-agent-platform" in html
    assert "qwen-agent-platform" in html
    assert "glm-agent-platform" in html
    assert "Provider positions" in html
    assert "Run Fixed Rescore" in html
    assert "Run Live Model Rescore" in html
    assert "/public/rescore-demo/run" in html
    assert "/public/fast-gcp-review/owner-session" in html
    assert "does not call model APIs" in html
    assert "needs_more_data -&gt; evidence_collected" in html
    assert "validation_target" in html
    assert "primary_candidate" in html
    assert "user_impact_unverified" in html
    assert "Source trace" in html
    assert "preserved_demo_snapshot" in html
    assert "Before target present in current source review: no" in html
    assert "test_more_data_child_bundle_rescores_parent_graph_and_promotion" in html
    assert payload["demo_id"] == "amazon-notify-more-data-rescore"
    assert payload["before"]["state"] == "validation_target"
    assert payload["more_data_loop"]["status_transition"] == "needs_more_data -> evidence_collected"
    assert payload["more_data_loop"]["added_log_count"] == 2
    assert payload["after"]["state"] == "primary_candidate"
    assert payload["after"]["promotion_decision"]["final_class"] == "primary_candidate"


def test_detail_target_cards_keep_provider_summary_and_provider_list_in_sync() -> None:
    target = {
        "review_target_id": "rt-provider-sync",
        "title": "Runtime restart needs validation",
        "class": "validation_target",
        "status": "pending",
        "subsystem": "runtime_recovery",
        "canonical_review_unit": "runtime_recovery",
        "review_priority_score": 0.81,
        "provider_count": 1,
        "agreement": {
            "verdict": "single_source",
            "convergence_score": 0.5,
            "summary": "1/2 schema-valid providers projected this target.",
        },
        "promotion": {
            "state": "validation",
            "blocked_reason": "user_impact_unverified",
            "score_note": "Priority is review urgency, not truth probability.",
        },
        "provider_positions": [
            {
                "provider_id": "gemini-enterprise-agent-platform",
                "stance": "claimed",
                "one_line": "Projected this canonical review unit.",
            },
            {
                "provider_id": "qwen-agent-platform",
                "stance": "silent",
                "one_line": "Did not surface this normalized review target.",
            },
            {
                "provider_id": "mistral-agent-platform",
                "stance": "provider_error",
                "one_line": "HTTP 429 resource exhausted. Excluded from convergence denominator.",
            },
        ],
        "evidence_refs": ["PATTERN-001", "METRIC-001"],
        "missing_evidence": ["user impact metric"],
        "target_explanation": {
            "suspected_issue": "watchdog restart failures may affect notification delivery",
            "operational_mechanism": "runtime restart loop and delivery failure",
            "why_it_matters": "customer notification delivery can be delayed",
        },
    }

    card_html = _fast_detail_target_card(target, index=1)
    workspace_html = _workspace_target_detail_html(target, index=1)

    assert "Provider stance: support 1 / silent 1 / provider_error 1" in card_html
    assert card_html.count('class="position-row"') == 3
    assert "gemini-enterprise-agent-platform" in card_html
    assert "qwen-agent-platform" in card_html
    assert "mistral-agent-platform" in card_html
    assert "1 support / 1 silent / 1 provider error / 0.50" in workspace_html
    assert workspace_html.count("workspace-provider-card") == 3
    assert "provider_error" in workspace_html


def test_public_precomputed_review_fixture_is_regenerated_from_pipeline(tmp_path: Path) -> None:
    result, payload = _build_public_payload(
        tmp_path,
        input_path="data/sample_logs.jsonl",
        db_name="public-sample.sqlite3",
        service="payment-api",
        start="2026-06-12T10:00:00Z",
        end="2026-06-12T10:20:00Z",
        lookback_minutes=45,
        updated_at="2026-06-12T10:20:00Z",
        target_limit=5,
        source_note="generated from public sample fixture with deterministic local providers and sanitized source profile context",
    )

    assert result.evidence_sha256 == PUBLIC_SAMPLE_SHA
    assert payload["generation"]["provider_mode"] == "deterministic_local"
    assert payload["summary"]["log_count"] == 20
    assert payload["summary"]["providers"] == {
        "success": 3,
        "total": 3,
        "pipeline_status": "completed",
    }
    assert all(row["provider_id"] != "local-fail" for row in payload["provider_statuses"])
    assert payload["summary"]["review"]["primary_targets"] == 0
    assert payload["summary"]["review"]["validation_targets"] == 5
    assert payload["profile_context"]["profile_id"] == "payment_api_sample_source_approved"
    assert payload["profile_draft_generation"]["llm_status"] == "ok"
    assert payload["analysis_context"]["source_context_sha256"]
    assert payload["analysis_context"]["source_analysis_sha256"]
    first_target = payload["targets"][0]
    assert first_target["agreement"]["convergence_score"] == 0.6666666667
    assert first_target["agreement"]["score_definition"] == "supporting schema-valid providers / all schema-valid providers"
    assert [row["stance"] for row in first_target["provider_positions"]] == ["support", "support", "silent"]

    expected = (
        ROOT
        / "data"
        / "precomputed_review_summaries"
        / f"{PUBLIC_SAMPLE_SHA}.json"
    ).read_text(encoding="utf-8")
    assert stable_precomputed_review_json(payload) == expected


def test_precomputed_review_records_provider_mode_override(tmp_path: Path) -> None:
    _, payload = _build_public_payload(
        tmp_path,
        input_path="data/sample_logs.jsonl",
        db_name="public-sample-api-mode.sqlite3",
        service="payment-api",
        start="2026-06-12T10:00:00Z",
        end="2026-06-12T10:20:00Z",
        lookback_minutes=45,
        updated_at="2026-06-12T10:20:00Z",
        target_limit=5,
        source_note="generated from API providers",
        provider_mode="real_api",
    )

    assert payload["generation"]["provider_mode"] == "real_api"


def test_precomputed_detail_page_renders_provider_mode() -> None:
    evidence_sha = "a" * 64
    html = _render_precomputed_review_detail_page(
        evidence_sha,
        {
            "evidence_sha256": evidence_sha,
            "updated_at": "2026-06-28T00:00:00Z",
            "generation": {
                "provider_mode": "real_api",
                "source_note": "generated from API providers",
            },
            "summary": {
                "status": "ok",
                "finding": {"title": "Saved finding", "impact": "Saved impact"},
                "review": {"primary_targets": 0, "validation_targets": 0},
                "providers": {"success": 1, "total": 1, "pipeline_status": "completed"},
                "raw_log_policy": "not_uploaded",
                "log_count": 1,
                "canonical_graph_sha256": "b" * 64,
                "input_fingerprint_sha256": "c" * 64,
            },
            "provider_statuses": [
                {
                    "provider_id": "mistral-agent-platform",
                    "status": "ok",
                    "schema_valid": True,
                    "raw_output_sha256": "d" * 64,
                }
            ],
            "review_graph_summary": {},
            "analysis_context": {
                "db_ingested_log_count": 6506,
                "model_projection_evidence_items": 140,
                "model_projection_occurrence_count": 5041,
                "model_projection_occurrence_coverage_ratio": 0.774823,
                "model_projection_policy": "Top high-signal evidence items were selected from the persisted sanitized corpus.",
                "model_projection_interpretation": "Projection coverage is occurrence-weighted, not raw-row coverage.",
                "log_observations": ["Sanitized log corpus was persisted before model analysis."],
                "source_observations": ["Sanitized source context was attached."],
                "analysis_conclusion": ["Human review remains required."],
            },
            "targets": [],
        },
    )

    assert "Served by the public read-only API" in html
    assert "Analysis mode:" in html
    assert "real_api" in html
    assert "mistral-agent-platform" in html
    assert "DB-to-model projection" in html
    assert "6,506" in html
    assert "140" in html
    assert "5,041" in html
    assert "77.5%" in html
    assert "Projection coverage is occurrence-weighted" in html


def test_precomputed_review_gcs_uri_prefixes(monkeypatch) -> None:
    from ops_evidence_synthesis.web.precomputed_review import _precomputed_review_gcs_uris

    monkeypatch.setenv("OES_PRECOMPUTED_REVIEW_GCS_PREFIX", "gs://private/precomputed")
    monkeypatch.setenv(
        "OES_PRECOMPUTED_REVIEW_GCS_PREFIXES",
        "gs://private/backup, https://example.invalid/not-gcs",
    )

    assert _precomputed_review_gcs_uris("a" * 64) == [
        f"gs://private/precomputed/{'a' * 64}.json",
        f"gs://private/backup/{'a' * 64}.json",
    ]


def test_precomputed_review_lookup_prefers_public_review_id_and_falls_back_to_local_on_gcs_failure(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from ops_evidence_synthesis.gcp import storage

    web_precomputed._PRECOMPUTED_REVIEW_CACHE.clear()
    monkeypatch.setenv("OES_PRECOMPUTED_REVIEW_DIR", str(tmp_path))
    monkeypatch.setenv("OES_PRECOMPUTED_REVIEW_GCS_PREFIX", "gs://private/precomputed")
    monkeypatch.setenv("OES_PRECOMPUTED_REVIEW_CACHE_SECONDS", "0")
    public_review_id = "1" * 64
    evidence_sha = "2" * 64
    fast_payload = {
        "evidence_sha256": evidence_sha,
        "summary": {"finding": {"title": "fast public id"}, "review": {}, "providers": {}},
        "generation": {"fast_gcp_review": {"public_review_id": public_review_id, "run_id": "fast-run-1"}},
        "provider_statuses": [],
        "targets": [],
    }
    local_payload = {
        "evidence_sha256": evidence_sha,
        "summary": {"finding": {"title": "local evidence sha"}, "review": {}, "providers": {}},
        "generation": {},
        "provider_statuses": [],
        "targets": [],
    }
    (tmp_path / f"{public_review_id}.json").write_text(json.dumps(fast_payload), encoding="utf-8")
    (tmp_path / f"{evidence_sha}.json").write_text(json.dumps(local_payload), encoding="utf-8")
    gcs_reads: list[str] = []

    def failing_read_json(uri: str):
        gcs_reads.append(uri)
        raise RuntimeError("simulated GCS outage")

    monkeypatch.setattr(storage, "read_json", failing_read_json)

    assert _precomputed_review_payload(public_review_id)["summary"]["finding"]["title"] == "fast public id"
    assert _precomputed_review_payload(evidence_sha)["summary"]["finding"]["title"] == "local evidence sha"
    assert gcs_reads == []

    (tmp_path / f"{evidence_sha}.json").unlink()
    assert _precomputed_review_payload(evidence_sha) is None
    assert gcs_reads == [f"gs://private/precomputed/{evidence_sha}.json"]


def test_precomputed_review_lookup_can_prefer_gcs_over_local(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from ops_evidence_synthesis.gcp import storage

    web_precomputed._PRECOMPUTED_REVIEW_CACHE.clear()
    monkeypatch.setenv("OES_PRECOMPUTED_REVIEW_DIR", str(tmp_path))
    monkeypatch.setenv("OES_PRECOMPUTED_REVIEW_GCS_PREFIX", "gs://private/precomputed")
    monkeypatch.setenv("OES_PRECOMPUTED_REVIEW_GCS_FIRST", "1")
    monkeypatch.setenv("OES_PRECOMPUTED_REVIEW_CACHE_SECONDS", "0")
    evidence_sha = "3" * 64
    local_payload = {
        "evidence_sha256": evidence_sha,
        "summary": {"finding": {"title": "local stale payload"}, "review": {}, "providers": {}},
        "generation": {},
        "provider_statuses": [],
        "targets": [],
    }
    gcs_payload = {
        "evidence_sha256": evidence_sha,
        "summary": {"finding": {"title": "fresh gcs payload"}, "review": {}, "providers": {}},
        "generation": {},
        "provider_statuses": [],
        "targets": [],
    }
    (tmp_path / f"{evidence_sha}.json").write_text(json.dumps(local_payload), encoding="utf-8")

    monkeypatch.setattr(storage, "read_json", lambda _uri: gcs_payload)

    assert _precomputed_review_payload(evidence_sha)["summary"]["finding"]["title"] == "fresh gcs payload"


def test_legacy_public_stream_v3_hash_resolves_to_canonical_primary(monkeypatch) -> None:
    monkeypatch.delenv("OES_PRECOMPUTED_REVIEW_DIR", raising=False)
    monkeypatch.delenv("OES_PRECOMPUTED_REVIEW_DIRS", raising=False)
    monkeypatch.setenv("OES_PRECOMPUTED_REVIEW_CACHE_SECONDS", "0")

    payload = _precomputed_review_payload(LEGACY_STREAM_V3_DELL_SHA)

    assert _canonical_precomputed_review_sha(LEGACY_STREAM_V3_DELL_SHA) == STREAM_V3_DELL_REAL_API_SHA
    assert payload is not None
    assert payload["evidence_sha256"] == STREAM_V3_DELL_REAL_API_SHA


def test_precomputed_graph_renders_analysis_context() -> None:
    evidence_sha = "a" * 64
    payload = {
        "evidence_sha256": evidence_sha,
        "summary": {
            "status": "ok",
            "finding": {"title": "Saved finding", "impact": "Saved impact"},
            "review": {"primary_targets": 0, "validation_targets": 1},
            "providers": {"success": 1, "total": 1, "pipeline_status": "completed"},
            "raw_log_policy": "not_uploaded",
            "log_count": 6506,
            "canonical_graph_sha256": "b" * 64,
            "input_fingerprint_sha256": "c" * 64,
        },
        "analysis_context": {
            "db_ingested_log_count": 6506,
            "model_projection_evidence_items": 140,
            "model_projection_occurrence_count": 5041,
            "model_projection_occurrence_coverage_ratio": 0.774823,
            "model_projection_interpretation": "Projection coverage is occurrence-weighted, not raw-row coverage.",
        },
        "provider_statuses": [
            {"provider_id": "gemini", "status": "ok", "schema_valid": True},
        ],
        "targets": [
            {
                "target_id": "target-1",
                "title": "Runtime recovery requires human review",
                "agreement": {"summary": "One provider claimed the target."},
                "promotion": {"state": "validation"},
                "provider_positions": [
                    {"provider_id": "gemini", "stance": "claimed"},
                ],
            }
        ],
    }

    html = _render_precomputed_graph_page(evidence_sha, payload)
    graph = _precomputed_review_graph_response(payload, evidence_sha256=evidence_sha)

    assert "DB ingested logs" in html
    assert "6,506" in html
    assert "140" in html
    assert "5,041" in html
    assert "77.5%" in html
    assert "Projection coverage is occurrence-weighted" in html
    assert graph["analysis_context"]["model_projection_evidence_items"] == 140
    assert graph["canonical_review_graph"]["analysis_context"]["db_ingested_log_count"] == 6506
    assert graph["canonical_review_graph"]["display_summary"]["incident_gate_signal"] == "no graph-level signal"


def test_real_api_qwen_glm_precomputed_review_payload_is_renderable() -> None:
    payload_path = ROOT / "data" / "precomputed_review_summaries" / f"{REAL_API_QWEN_GLM_SHA}.json"
    payload = json.loads(payload_path.read_text(encoding="utf-8"))

    assert payload["summary"]["log_count"] == 23400
    assert payload["summary"]["providers"] == {
        "success": 5,
        "total": 5,
        "pipeline_status": "succeeded",
    }
    assert payload["summary"]["review"] == {
        "auto_archived": 1,
        "monitor_only": 2,
        "primary_targets": 0,
        "validation_targets": 5,
    }
    assert payload["generation"]["payload_sha256"] == sha256_json(
        {
            "evidence_sha256": payload["evidence_sha256"],
            "summary": payload["summary"],
            "provider_statuses": payload["provider_statuses"],
            "review_graph_summary": payload["review_graph_summary"],
            "profile_context": payload["profile_context"],
            "targets": payload["targets"],
        }
    )
    providers = {row["provider_id"] for row in payload["provider_statuses"]}
    assert {
        "gemini-enterprise-agent-platform",
        "openai-gpt-oss-on-vertex",
        "mistral-agent-platform",
        "qwen-agent-platform",
        "glm-agent-platform",
    } <= providers
    assert all(row["status"] == "ok" and row["schema_valid"] for row in payload["provider_statuses"])
    assert payload["analysis_context"]["model_projection_evidence_items"] == 140
    assert payload["analysis_context"]["model_projection_occurrence_count"] == 19649
    assert payload["analysis_context"]["model_projection_occurrence_coverage_ratio"] == 0.839701
    assert payload["analysis_context"]["model_projection_interpretation"].startswith(
        "Projection coverage is occurrence-weighted"
    )
    assert payload["profile_context"]["profile_id"] == "amazon_notify_qwen_glm_full_corpus_approved"
    assert payload["profile_draft_generation"]["llm_status"] == "ok"
    assert payload["profile_context"]["schema_version"] == "profile_context_summary.v2"
    assert payload["profile_context"]["profile_status"] == "approved_context_human_gated_outcomes"
    assert payload["profile_context"]["confidence_action"] == "use_for_subsystem_routing_human_gated"
    assert payload["profile_context"]["confirmed_user_outcomes"] == []
    assert "Ensure amazon-notify service processes notifications successfully" in payload["profile_context"]["provisional_user_outcomes"]
    assert "assumed_critical_outcomes" not in json.dumps(payload["profile_context"])
    zero_link = next(
        row
        for row in payload["profile_context"]["profile_to_review_links"]
        if row["question"] == "Which metrics are zero-is-good or zero-is-bad?"
    )
    assert {"runtime_recovery", "service_health", "background_processing"} <= set(zero_link["review_units"])
    assert payload["analysis_context"]["source_context_sha256"]
    assert payload["analysis_context"]["source_analysis_sha256"]
    assert all("review_reason" in target for target in payload["targets"])
    assert payload["targets"][0]["review_reason"]["headline"].startswith(
        "Review target created because provider convergence"
    )
    assert all("target_explanation" in target for target in payload["targets"])
    assert payload["targets"][0]["target_explanation"]["suspected_issue"]
    evidence_summary_text = " ".join(payload["targets"][0]["target_explanation"]["evidence_summary"])
    assert "No such file" in evidence_summary_text or "can't open file" in evidence_summary_text
    assert payload["targets"][0]["missing_evidence"]

    detail_html = _render_precomputed_review_detail_page(REAL_API_QWEN_GLM_SHA, payload)
    graph_html = _render_precomputed_graph_page(REAL_API_QWEN_GLM_SHA, payload)
    graph = _precomputed_review_graph_response(payload, evidence_sha256=REAL_API_QWEN_GLM_SHA)

    assert "Five real providers" in detail_html
    assert "What this target means operationally" in detail_html
    assert "Suspected issue" in detail_html
    assert "Operational mechanism" in detail_html
    assert "Why this target is in review" in detail_html
    assert "Review target created because provider convergence" in detail_html
    assert "No such file" in detail_html or "can't open file" in detail_html
    assert "qwen-agent-platform" in detail_html
    assert "glm-agent-platform" in detail_html
    assert "19,649" in detail_html
    assert "Incident gate signal" in detail_html
    assert "Target promotion" in detail_html
    assert "confidence_action=use_for_subsystem_routing_human_gated" in detail_html
    assert "provisional_user_outcomes_pending_approval" in detail_html
    assert "profile_questions_linked_to_review_units" in detail_html
    assert "qwen-agent-platform" in graph_html
    assert "Incident gate signal" in graph_html
    assert graph["canonical_review_graph"]["summary"]["primary_count"] == 0
    assert graph["canonical_review_graph"]["summary"]["validation_count"] == 5
    assert graph["canonical_review_graph"]["review_graph_summary"]["provider_detection_overlap"] == "5/5"
    assert graph["analysis_context"]["model_projection_occurrence_count"] == 19649
    assert graph["canonical_review_graph"]["display_summary"]["incident_gate_signal"] == "signal present"


def test_public_real_api_guarded_review_matches_fresh_five_provider_payload() -> None:
    payload = _load_json(ROOT / "data" / "precomputed_review_summaries" / f"{PUBLIC_REAL_API_SHA}.json")

    assert payload["summary"]["canonical_graph_sha256"].startswith("8ad416a42a0a")
    assert payload["summary"]["providers"] == {
        "success": 5,
        "total": 5,
        "pipeline_status": "succeeded",
    }
    assert payload["summary"]["review"]["primary_targets"] == 1
    assert payload["summary"]["review"]["validation_targets"] == 10
    assert payload["analysis_context"]["provider_full_corpus_chunk_count"] == 105
    assert payload["analysis_context"]["provider_full_corpus_coverage_ratio"] == 1.0
    assert all(row["status"] == "ok" and row["schema_valid"] for row in payload["provider_statuses"])

    detail_html = _render_precomputed_review_detail_page(PUBLIC_REAL_API_SHA, payload)

    assert "Five real providers analyzed the 44,944-row amazon-notify corpus" in detail_html
    assert "5 / 5" in detail_html
    assert "Chunk And Merge Full Corpus" in detail_html
    assert "real_api_vertex_gemini_3_1_pro_gpt_oss_mistral_qwen_gemma4_chunked_full_corpus" in detail_html
    assert "gemini-3.1-pro-preview" in detail_html
    assert "gemini-3.1-flash-lite" not in detail_html
    assert "gemma-agent-platform" in detail_html
    assert "rate_limited_fail_closed" not in detail_html
    assert "Mistral did not contribute" not in detail_html
    assert "010838ba" not in detail_html


def test_stream_v3_real_api_precomputed_payloads_are_renderable() -> None:
    cases = [
        {
            "sha": STREAM_V3_DELL_REAL_API_SHA,
            "title": "Five real providers",
            "service": "stream_v3_runtime",
            "log_count": 45000,
            "providers": {"success": 5, "total": 5, "pipeline_status": "succeeded"},
            "review": {
                "auto_archived": 2,
                "monitor_only": 6,
                "primary_targets": 0,
                "validation_targets": 6,
            },
            "projection_items": 140,
            "occurrences": 44104,
            "coverage": 0.980089,
            "full_corpus_items": 1036,
            "chunk_count": 10,
            "profile_generation_status": "persisted",
            "confirmed_user_outcomes": [
                "Continuously available public YouTube live stream with fresh ADS-B visual content and audible program audio."
            ],
        },
        {
            "sha": STREAM_V3_ARENA_REAL_API_SHA,
            "title": "Five real providers",
            "service": "stream_v3_monitoring",
            "log_count": 49942,
            "providers": {"success": 5, "total": 5, "pipeline_status": "succeeded"},
            "review": {
                "auto_archived": 2,
                "monitor_only": 3,
                "primary_targets": 0,
                "validation_targets": 2,
            },
            "projection_items": 25,
            "occurrences": 49942,
            "coverage": 1.0,
            "full_corpus_items": 25,
            "chunk_count": 4,
            "profile_generation_status": "persisted",
            "confirmed_user_outcomes": [
                "a continuously available public YouTube live stream with fresh ADS-B visual content and audible program audio"
            ],
        },
    ]

    for case in cases:
        payload_path = ROOT / "data" / "precomputed_review_summaries" / f"{case['sha']}.json"
        payload = json.loads(payload_path.read_text(encoding="utf-8"))

        assert payload["summary"]["log_count"] == case["log_count"]
        assert payload["summary"]["providers"] == case["providers"]
        assert payload["summary"]["review"] == case["review"]
        public_primary_count = sum(1 for target in payload["targets"] if target["class"] == "primary_candidate")
        public_validation_count = sum(1 for target in payload["targets"] if target["class"] != "primary_candidate")
        assert payload["summary"]["review"]["primary_targets"] == public_primary_count
        assert payload["summary"]["review"]["validation_targets"] == public_validation_count
        assert payload["review_graph_summary"]["targets_total"] == len(payload["targets"])
        assert payload["analysis_context"]["service"] == case["service"]
        assert payload["analysis_context"]["model_projection_evidence_items"] == case["projection_items"]
        assert payload["analysis_context"]["model_projection_occurrence_count"] == case["occurrences"]
        assert payload["analysis_context"]["model_projection_occurrence_coverage_ratio"] == case["coverage"]
        assert "occurrence-weighted" in payload["analysis_context"]["model_projection_interpretation"]
        assert payload["analysis_context"]["provider_full_corpus_analyzed_evidence_items"] == case["full_corpus_items"]
        assert payload["analysis_context"]["provider_full_corpus_evidence_items"] == case["full_corpus_items"]
        assert payload["analysis_context"]["provider_full_corpus_unassigned_evidence_items"] == 0
        assert payload["analysis_context"]["provider_full_corpus_chunk_count"] == case["chunk_count"]
        assert payload["analysis_context"]["provider_full_corpus_coverage_ratio"] == 1.0
        assert payload["analysis_context"]["db_corpus_coverage_ratio"] == 1.0
        assert payload["analysis_context"]["db_corpus_direct_prompt_row_count"] == 0
        hashed_payload = {
            "evidence_sha256": payload["evidence_sha256"],
            "summary": payload["summary"],
            "provider_statuses": payload["provider_statuses"],
            "review_graph_summary": payload["review_graph_summary"],
            "profile_context": payload["profile_context"],
            "targets": payload["targets"],
        }
        if payload.get("provenance"):
            hashed_payload["provenance"] = payload["provenance"]
        assert payload["generation"]["payload_sha256"] == sha256_json(hashed_payload)
        assert payload["profile_draft_generation"]["llm_status"] == case["profile_generation_status"]
        assert payload["profile_context"]["profile_id"]
        assert payload["profile_context"]["schema_version"] == "profile_context_summary.v2"
        assert payload["profile_context"]["profile_status"] == "approved_context_human_gated_outcomes"
        assert payload["profile_context"]["confidence_action"] == "approved_human_reviewed"
        assert payload["profile_context"]["confirmed_user_outcomes"] == case["confirmed_user_outcomes"]
        assert payload["profile_context"]["provisional_user_outcomes"] == []
        assert "assumed_critical_outcomes" not in json.dumps(payload["profile_context"])
        assert payload["profile_context"]["profile_to_review_links"] == []
        assert payload["analysis_context"]["source_context_sha256"] == ""
        assert payload["analysis_context"]["source_analysis_sha256"] == ""
        assert "frozen approved profile" in payload["analysis_context"]["source_observations"][0]
        assert all("review_reason" in target for target in payload["targets"])
        assert payload["targets"][0]["review_reason"]["headline"].startswith(
            "Review target created because provider convergence"
        )
        assert all("target_explanation" in target for target in payload["targets"])
        assert payload["targets"][0]["target_explanation"]["suspected_issue"]
        assert payload["targets"][0]["target_explanation"]["evidence_summary"]
        for target in payload["targets"]:
            summaries = target["target_explanation"]["evidence_summary"]
            assert all(
                not re.fullmatch(r"PATTERN-\d+", str(summary).strip())
                for summary in summaries
            )
        if case["sha"] == STREAM_V3_DELL_REAL_API_SHA:
            assert any(
                target["canonical_review_unit"] == "background_processing"
                and "playlist exhaustion" in target["suspected_issue"].lower()
                for target in payload["targets"]
            )
            assert any(
                target["canonical_review_unit"] == "capture_pipeline"
                and "causal link" in target["suspected_issue"].lower()
                for target in payload["targets"]
            )
            assert all(target["canonical_review_unit"] != "database_connection_pool" for target in payload["targets"])

        provider_rows = {row["provider_id"]: row for row in payload["provider_statuses"]}
        assert provider_rows["qwen-agent-platform"]["schema_valid"] is True
        assert provider_rows["gemma-agent-platform"]["schema_valid"] is True
        assert provider_rows["mistral-agent-platform"]["schema_valid"] is True
        assert provider_rows["gemini-enterprise-agent-platform"]["schema_valid"] is True
        if case["sha"] == STREAM_V3_ARENA_REAL_API_SHA:
            assert provider_rows["openai-gpt-oss-on-vertex"]["status"] == "ok"
            assert provider_rows["openai-gpt-oss-on-vertex"]["schema_valid"] is True
            assert provider_rows["openai-gpt-oss-on-vertex"]["model_name"] == "gpt-oss-20b-maas"
            assert {target["canonical_review_unit"] for target in payload["targets"]} == {
                "transport_sender",
                "runtime_recovery",
            }

        detail_html = _render_precomputed_review_detail_page(case["sha"], payload)
        graph_html = _render_precomputed_graph_page(case["sha"], payload)
        graph = _precomputed_review_graph_response(payload, evidence_sha256=case["sha"])

        assert case["title"] in detail_html
        assert case["service"] in detail_html
        assert "What this target means operationally" in detail_html
        assert "Suspected issue" in detail_html
        assert "Why this target is in review" in detail_html
        assert "Review target created because provider convergence" in detail_html
        assert "qwen-agent-platform" in detail_html
        assert "gemma-agent-platform" in detail_html
        assert "DB-to-model projection" in detail_html
        assert "Single-prompt projection coverage is occurrence-weighted" in detail_html
        assert "Chunk And Merge Full Corpus" in detail_html
        assert "Incident gate signal" in detail_html
        assert str(case["occurrences"]) in detail_html.replace(",", "")
        if case["sha"] == STREAM_V3_ARENA_REAL_API_SHA:
            assert "Network timeout when the WAN address observer" in detail_html
            assert "gpt-oss-20b-maas" in detail_html
            assert "provider_error" not in json.dumps(payload["provider_statuses"])
        assert "qwen-agent-platform" in graph_html
        assert "Incident gate signal" in graph_html
        assert graph["analysis_context"]["model_projection_occurrence_count"] == case["occurrences"]
        assert graph["canonical_review_graph"]["summary"]["validation_count"] == case["review"]["validation_targets"]
        assert graph["canonical_review_graph"]["display_summary"]["incident_gate_signal"] == "signal present"


def test_stream_v3_dell_and_arena_profiles_stay_separated() -> None:
    dell = _load_json(ROOT / "data" / "precomputed_review_summaries" / f"{STREAM_V3_DELL_REAL_API_SHA}.json")
    arena = _load_json(ROOT / "data" / "precomputed_review_summaries" / f"{STREAM_V3_ARENA_REAL_API_SHA}.json")

    assert dell["profile_context"]["profile_id"] == "stream_v3_runtime_source_approved_20260711"
    assert arena["profile_context"]["profile_id"] == "stream_v3_monitoring_source_approved_20260711"
    assert dell["analysis_context"]["service"] == "stream_v3_runtime"
    assert arena["analysis_context"]["service"] == "stream_v3_monitoring"
    assert dell["profile_context"]["source_discovery_sha256"] != arena["profile_context"]["source_discovery_sha256"]
    assert dell["profile_context"]["purpose"] != arena["profile_context"]["purpose"]
    assert dell["profile_context"]["provisional_user_outcomes"] == []
    assert arena["profile_context"]["provisional_user_outcomes"] == []
    assert dell["profile_context"]["metric_semantics_count"] == 4
    assert arena["profile_context"]["metric_semantics_count"] == 6


def test_precomputed_detail_page_ui_smoke_includes_provider_targets_missing_evidence_and_public_links() -> None:
    payload = _load_json(ROOT / "data" / "precomputed_review_summaries" / f"{STREAM_V3_DELL_REAL_API_SHA}.json")

    html = _render_precomputed_review_detail_page(STREAM_V3_DELL_REAL_API_SHA, payload)

    assert html.count('class="target"') == len(payload["targets"])
    assert html.count("workspace-provider-card") >= len(payload["targets"]) * len(payload["provider_statuses"])
    assert "Top missing evidence" in html
    assert "Missing evidence" in html
    assert "/ui/rescore-demo?id=amazon-notify-more-data-rescore" not in html
    assert "GitHub" in html
    assert "Architecture" in html
    assert "Demo Script" in html
    assert "blob/main/docs/demo-video-script.md" in html
    assert "qwen-agent-platform" in html
    assert "gemma-agent-platform" in html


def test_observation_validation_target_is_not_labeled_as_suspected_issue() -> None:
    target = {
        "review_target_id": "cog-observation-user-experience",
        "class": "validation_target",
        "canonical_review_unit": "user_experience",
        "review_priority_score": 0.774,
        "provider_positions": [
            {
                "provider_id": "gemini-fast-lite-agent-platform",
                "stance": "claimed",
            }
        ],
        "agreement": {
            "verdict": "single_source",
            "convergence_score": 1.0,
        },
        "target_explanation": {
            "schema_version": "target_explanation.v1",
            "suspected_issue": "None identified",
            "operational_mechanism": (
                "The systemd service and pubsub listener appear to be processing messages "
                "and advancing checkpoints as expected."
            ),
            "why_it_matters": "Confirms the service is likely healthy and not impacting notification delivery.",
            "why_not_promoted": "The evidence indicates normal operation rather than an incident.",
            "next_validation_question": "Are there any specific time windows where notifications were reported as missing?",
            "provider_explanations": [
                {
                    "claim_type": "insufficient_evidence",
                    "provider_id": "gemini-fast-lite-agent-platform",
                }
            ],
        },
        "evidence_refs": ["PATTERN-281", "PATTERN-314", "PATTERN-382"],
        "missing_evidence": ["Logs or metrics indicating specific notification delivery failures."],
    }

    detail_html = _workspace_target_detail_html(target, index=1)
    queue_html = web_precomputed._workspace_queue_item_html(target, index=1)

    assert "Current finding" in detail_html
    assert "<label>Suspected issue</label>" not in detail_html
    assert "None identified" not in detail_html
    assert "The evidence indicates normal operation rather than an incident." in detail_html
    assert "score is queue priority, not incident probability" in detail_html
    assert "<span>priority</span>0.77" in queue_html
    assert "Review priority, not incident probability" in queue_html

    no_finding_target = {
        **target,
        "class": "monitor_only",
        "provider_positions": [
            {
                "provider_id": "gemini-fast-lite-agent-platform",
                "stance": "no_finding",
            }
        ],
        "agreement": {
            "verdict": "normal_observation",
            "convergence_score": 0.0,
        },
    }
    no_finding_detail = _workspace_target_detail_html(no_finding_target, index=2)
    no_finding_queue = web_precomputed._workspace_queue_item_html(no_finding_target, index=2)

    assert "0 support / 0 silent / 1 no finding / 0.00" in no_finding_detail
    assert "1/1 no finding" in no_finding_queue

    anomaly_target = {
        **target,
        "review_target_id": "cog-traffic",
        "canonical_review_unit": "traffic",
        "target_explanation": {
            "suspected_issue": "Potential traffic anomaly or instrumentation change.",
            "operational_mechanism": "The unique_trace_count metric is tracking distinct execution paths.",
            "why_it_matters": "An unexplained increase in trace counts could indicate unexpected system behavior.",
            "why_not_promoted": "The metric increase is not correlated with error logs or service failures.",
        },
    }
    anomaly_html = _workspace_target_detail_html(anomaly_target, index=2)

    assert "<label>Suspected issue</label>" in anomaly_html
    assert "<label>Current finding</label>" not in anomaly_html

    healthy_validation_target = {
        **target,
        "canonical_review_unit": "youtube_health",
        "target_explanation": {
            "suspected_issue": "None; reporting jobs appear healthy.",
            "why_not_promoted": "This is a secondary operational task and not the primary streaming service.",
            "why_it_matters": "Ensures that operational cost reporting is functioning.",
            "next_validation_question": "Is this reporting job expected during the incident window?",
        },
    }
    healthy_html = _workspace_target_detail_html(healthy_validation_target, index=3)

    assert "<label>Current finding</label>" in healthy_html
    assert "None; reporting jobs appear healthy." not in healthy_html
    assert "This is a secondary operational task and not the primary streaming service." in healthy_html

    for issue_text, why_not_promoted in (
        ("None", "No failure signals exist."),
        ("No incident detected.", "No incident evidence exists."),
    ):
        no_incident_target = {
            **target,
            "target_explanation": {
                "suspected_issue": issue_text,
                "why_not_promoted": why_not_promoted,
                "why_it_matters": "Without error signals, no incident hypothesis can be formed.",
            },
        }
        no_incident_html = _workspace_target_detail_html(no_incident_target, index=4)

        assert "<label>Current finding</label>" in no_incident_html
        assert f"<p>{issue_text}</p>" not in no_incident_html
        assert why_not_promoted in no_incident_html


def test_review_workbench_separates_problem_candidates_from_no_issue_observations() -> None:
    no_issue_user_experience = {
        "review_target_id": "cog-user-experience",
        "class": "validation_target",
        "canonical_review_unit": "user_experience",
        "review_priority_score": 0.774,
        "provider_positions": [{"provider_id": "gemini-fast-lite-agent-platform", "stance": "claimed"}],
        "agreement": {"verdict": "single_source", "convergence_score": 1.0},
        "target_explanation": {
            "suspected_issue": "None identified",
            "why_not_promoted": "The evidence indicates normal operation rather than an incident.",
            "next_validation_question": "Are there any specific time windows where notifications were missing?",
        },
    }
    traffic_candidate = {
        "review_target_id": "cog-traffic",
        "class": "validation_target",
        "canonical_review_unit": "traffic",
        "review_priority_score": 0.7377,
        "provider_positions": [{"provider_id": "gemini-fast-lite-agent-platform", "stance": "claimed"}],
        "agreement": {"verdict": "single_source", "convergence_score": 1.0},
        "target_explanation": {
            "suspected_issue": "Potential traffic anomaly or instrumentation change.",
            "why_not_promoted": "The metric increase is not correlated with any error logs or service failures.",
            "next_validation_question": "Does the increase correlate with expected business activity?",
        },
    }
    no_issue_service_health = {
        "review_target_id": "cog-service-health",
        "class": "validation_target",
        "canonical_review_unit": "service_health",
        "review_priority_score": 0.724,
        "provider_positions": [{"provider_id": "gemini-fast-lite-agent-platform", "stance": "claimed"}],
        "agreement": {"verdict": "single_source", "convergence_score": 1.0},
        "target_explanation": {
            "suspected_issue": "None identified; logs show successful operation.",
            "why_not_promoted": "The evidence is entirely consistent with normal operation.",
            "next_validation_question": "Are there any specific missing time windows?",
        },
    }

    html = web_precomputed._detail_issue_triage_html(
        [no_issue_user_experience, traffic_candidate, no_issue_service_health]
    )

    assert "1 problem candidate / 2 no-issue observations" in html
    assert "Problem candidate" in html
    assert "Potential traffic anomaly or instrumentation change." in html
    assert html.count("No issue observed") == 2
    assert "normal operation rather than an incident" in html
    assert "entirely consistent with normal operation" in html


def test_rescore_loop_links_are_only_attached_to_matching_source_evidence() -> None:
    rescore_url = "/ui/rescore-demo?id=amazon-notify-more-data-rescore"

    assert rescore_url not in web_precomputed._public_action_links_html(STREAM_V3_DELL_REAL_API_SHA)
    assert rescore_url not in web_precomputed._public_action_links_html(PUBLIC_REAL_API_SHA)
    assert rescore_url not in web_precomputed._detail_action_links_html(STREAM_V3_ARENA_REAL_API_SHA)
    assert rescore_url in web_precomputed._public_action_links_html(REAL_API_QWEN_GLM_SHA)
    assert rescore_url in web_precomputed._detail_action_links_html(REAL_API_QWEN_GLM_SHA)


def test_review_graph_uses_public_manifest_label_instead_of_profile_id() -> None:
    payload = _load_json(ROOT / "data" / "precomputed_review_summaries" / f"{PUBLIC_REAL_API_SHA}.json")

    label = web_precomputed._review_graph_service_label(payload)
    html = _render_precomputed_graph_page(PUBLIC_REAL_API_SHA, payload)

    assert label == "Guarded review: amazon-notify 44,944 rows, 0 auto-promoted causes"
    assert label in html
    assert "amazon notify e2e 20260701t003045z approved" not in html


def test_public_target_classification_demotes_evidence_thin_primary_candidate() -> None:
    final_class, classification = _public_target_class(
        {"canonical_review_unit": "audio_energy"},
        original_class="primary_candidate",
        provider_count=4,
        valid_count=5,
        evidence_ref_count=2,
        evidence_family_count=2,
        source_candidate_count=1,
        target_explanation={
            "why_not_promoted": "No specific failure signals, error logs, or metric spikes were provided.",
            "counter_evidence_summary": ["No audio energy measurement logs were provided."],
        },
        missing_evidence=["Specific error logs", "Metric time-series data"],
        blocked_reason="primary_candidate_only; incident_baseline_not_auto_accepted; human_review_required",
    )

    assert final_class == "validation_target"
    assert classification["adjustment"] == "demoted_primary_candidate_evidence_thin"
    assert classification["original_class"] == "primary_candidate"


def test_public_target_classification_keeps_evidence_supported_primary_candidate() -> None:
    final_class, classification = _public_target_class(
        {"canonical_review_unit": "runtime_recovery"},
        original_class="primary_candidate",
        provider_count=4,
        valid_count=5,
        evidence_ref_count=6,
        evidence_family_count=3,
        source_candidate_count=4,
        target_explanation={
            "why_not_promoted": "",
            "evidence_summary": ["PATTERN, METRIC, and OPS evidence jointly show the runtime path."],
        },
        missing_evidence=[],
        blocked_reason="primary_candidate_only; incident_baseline_not_auto_accepted; human_review_required",
    )

    assert final_class == "primary_candidate"
    assert classification["adjustment"] == ""


def test_public_target_classification_routes_no_finding_to_monitor_only() -> None:
    no_finding_class, no_finding = _public_target_class(
        {"canonical_review_unit": "user_experience"},
        original_class="validation_target",
        provider_count=1,
        valid_count=1,
        evidence_ref_count=3,
        evidence_family_count=1,
        source_candidate_count=1,
        target_explanation={
            "suspected_issue": "None identified",
            "why_it_matters": "Confirms the service is likely healthy and not impacting notification delivery.",
            "why_not_promoted": "The evidence indicates normal operation rather than an incident.",
            "provider_explanations": [
                {"provider_id": "gemini-fast-lite-agent-platform", "claim_type": "insufficient_evidence"}
            ],
        },
        missing_evidence=["Logs or metrics indicating specific notification delivery failures."],
        blocked_reason="user_impact_unverified; impact_disagreement",
    )
    anomaly_class, anomaly = _public_target_class(
        {"canonical_review_unit": "traffic"},
        original_class="validation_target",
        provider_count=1,
        valid_count=1,
        evidence_ref_count=2,
        evidence_family_count=1,
        source_candidate_count=1,
        target_explanation={
            "suspected_issue": "Potential traffic anomaly or instrumentation change.",
            "why_not_promoted": "The metric increase is not correlated with error logs or service failures.",
        },
        missing_evidence=["Traffic baseline and user-impact metric."],
        blocked_reason="user_impact_unverified; impact_disagreement",
    )

    assert no_finding_class == "monitor_only"
    assert no_finding["adjustment"] == "normal_operation_observation"
    assert anomaly_class == "validation_target"
    assert anomaly["adjustment"] == ""

    healthy_baseline_class, healthy_baseline = _public_target_class(
        {"canonical_review_unit": "user_experience"},
        original_class="validation_target",
        provider_count=1,
        valid_count=2,
        evidence_ref_count=7,
        evidence_family_count=1,
        source_candidate_count=2,
        target_explanation={
            "suspected_issue": "none",
            "evidence_summary": [
                "PATTERN-002 shows successful notification processing with failure_kind=None and auth_status=READY."
            ],
            "counter_evidence_summary": [
                "No evidence of message processing delays or ingestion errors was found."
            ],
            "why_it_matters": "Continuous execution is a prerequisite for successful notification delivery.",
            "why_not_promoted": "This is a baseline observation of healthy activity, not a root cause or an incident.",
            "provider_explanations": [
                {
                    "claim_type": "support",
                    "suspected_issue": "None; message ingestion is functional.",
                    "why_not_promoted": "This confirms operational status rather than identifying a failure.",
                }
            ],
        },
        missing_evidence=["User impact or operational outcome evidence tied to this review unit."],
        blocked_reason="user_impact_unverified",
    )

    assert healthy_baseline_class == "monitor_only"
    assert healthy_baseline["adjustment"] == "normal_operation_observation"

    expected_watchdog_class, expected_watchdog = _public_target_class(
        {"canonical_review_unit": "runtime_recovery"},
        original_class="validation_target",
        provider_count=1,
        valid_count=1,
        evidence_ref_count=6,
        evidence_family_count=1,
        source_candidate_count=1,
        target_explanation={
            "suspected_issue": "None; watchdog is functioning as expected.",
            "evidence_summary": [
                "PATTERN-004, PATTERN-005, and PATTERN-006 show watchdog service cycles completing."
            ],
            "counter_evidence_summary": [
                "No evidence of watchdog failure or restart loops was found."
            ],
            "why_it_matters": "Ensures the main service is monitored for health.",
            "why_not_promoted": "The evidence confirms normal watchdog behavior.",
            "provider_explanations": [
                {
                    "claim_type": "support",
                    "suspected_issue": "None; service appears healthy.",
                    "why_not_promoted": "This is a baseline operational observation.",
                }
            ],
        },
        missing_evidence=["systemd error logs"],
        blocked_reason="no_baseline_agreement_or_causal_alignment; cause_disagreement",
    )

    assert expected_watchdog_class == "monitor_only"
    assert expected_watchdog["adjustment"] == "normal_operation_observation"

    structural_caveat_class, structural_caveat = _public_target_class(
        {"canonical_review_unit": "user_experience"},
        original_class="validation_target",
        provider_count=2,
        valid_count=2,
        evidence_ref_count=52,
        evidence_family_count=1,
        source_candidate_count=3,
        target_explanation={
            "suspected_issue": "Potential mismatch between source code context and runtime environment.",
            "evidence_summary": [
                "The source_context.version_context.deployed_version_confirmed field is false.",
                "Multiple patterns confirm that messages are being accepted by the system.",
            ],
            "counter_evidence_summary": [
                "No evidence of message processing delays or ingestion errors was found."
            ],
            "operational_mechanism": (
                "The source context provides templates, but there is no deployment evidence to confirm "
                "these specific versions were active during the incident window."
            ),
            "why_it_matters": (
                "Hypotheses based on the provided source code may be invalid if the running code differs."
            ),
            "why_not_promoted": "This is a structural caveat rather than a functional finding.",
            "provider_explanations": [
                {
                    "claim_type": "caveat",
                    "suspected_issue": "Potential mismatch between source code context and deployed runtime.",
                    "why_not_promoted": (
                        "This is a structural limitation of the evidence bundle, not a specific incident finding."
                    ),
                },
                {
                    "claim_type": "support",
                    "suspected_issue": "None; message ingestion is functional.",
                    "why_not_promoted": "This confirms operational status rather than identifying a failure.",
                },
            ],
        },
        missing_evidence=["deployment_history"],
        blocked_reason="incident_baseline_open; user_impact_or_business_output_unverified",
    )

    assert structural_caveat_class == "monitor_only"
    assert structural_caveat["adjustment"] == "non_incident_structural_caveat"

    for suspected_issue, why_not_promoted in (
        (
            "Unknown operational state causing a surge in trace generation.",
            "The evidence only shows a derived metric spike without identifying the source program, exact failure signature, or user impact.",
        ),
        (
            "Downstream Gmail watch may have expired, causing missed notifications.",
            "No downstream dependency evidence is present in the current bundle.",
        ),
        (
            "throughput_disappearance",
            "This is a request for data to move from 'no finding' to a potential 'validation target'.",
        ),
    ):
        target_class, classification = _public_target_class(
            {"canonical_review_unit": "amazon_notify"},
            original_class="validation_target",
            provider_count=2,
            valid_count=5,
            evidence_ref_count=4,
            evidence_family_count=2,
            source_candidate_count=1,
            target_explanation={
                "suspected_issue": suspected_issue,
                "why_not_promoted": why_not_promoted,
            },
            missing_evidence=["Corroborating runtime and user-impact evidence."],
            blocked_reason="user_impact_unverified; impact_disagreement",
        )
        assert target_class == "validation_target"
        assert classification["adjustment"] == ""

    counts = _public_review_counts(
        [
            {"class": no_finding_class},
            {"class": anomaly_class},
            {"class": "primary_candidate"},
        ],
        graph_summary={"monitor_only_count": 2, "auto_archived_count": 1},
    )
    assert counts == {
        "primary_targets": 1,
        "validation_targets": 1,
        "monitor_only": 3,
        "auto_archived": 1,
    }


def test_flagship_precomputed_review_fixture_is_regenerated_from_pipeline(tmp_path: Path) -> None:
    result, payload = _build_public_payload(
        tmp_path,
        input_path="data/amazon_notify_flagship_logs.jsonl",
        db_name="amazon-notify-flagship.sqlite3",
        service="amazon-notify",
        start="2026-06-26T22:30:00Z",
        end="2026-06-26T23:32:21Z",
        lookback_minutes=1440,
        updated_at="2026-06-26T23:32:21Z",
        target_limit=6,
        source_note="generated from committed public-safe amazon-notify fixture with deterministic local providers and sanitized source profile context",
    )

    assert result.evidence_sha256 == PUBLIC_FLAGSHIP_SHA
    assert payload["summary"]["log_count"] == 6506
    assert payload["summary"]["providers"] == {
        "success": 3,
        "total": 3,
        "pipeline_status": "completed",
    }
    assert all(row["provider_id"] != "local-fail" for row in payload["provider_statuses"])
    assert payload["summary"]["review"]["primary_targets"] == 0
    assert payload["summary"]["review"]["validation_targets"] == 1
    assert payload["profile_context"]["profile_id"] == "amazon_notify_sample_source_approved"
    assert payload["profile_draft_generation"]["llm_status"] == "ok"
    assert payload["analysis_context"]["source_context_sha256"]
    assert payload["analysis_context"]["source_analysis_sha256"]
    assert payload["review_graph_summary"]["convergence_count"] >= 1
    target = payload["targets"][0]
    assert target["agreement"]["verdict"] == "convergence"
    assert target["agreement"]["convergence_score"] == 0.6666666667
    assert target["agreement"]["score_definition"] == "supporting schema-valid providers / all schema-valid providers"
    assert [row["stance"] for row in target["provider_positions"]] == [
        "support",
        "support",
        "caveat_or_validation",
    ]

    expected = (
        ROOT
        / "data"
        / "precomputed_review_summaries"
        / f"{PUBLIC_FLAGSHIP_SHA}.json"
    ).read_text(encoding="utf-8")
    assert stable_precomputed_review_json(payload) == expected


def _build_public_payload(
    tmp_path: Path,
    *,
    input_path: str,
    db_name: str,
    service: str,
    start: str,
    end: str,
    lookback_minutes: int,
    updated_at: str,
    target_limit: int,
    source_note: str,
    provider_mode: str = "deterministic_local",
):
    store = SQLiteStore(tmp_path / db_name)
    store.init_schema()
    ingest_jsonl(input_path, store)

    result = run_pipeline(
        store,
        IncidentWindow(
            service=service,
            environment="prod",
            incident_start=start,
            incident_end=end,
            lookback_minutes=lookback_minutes,
        ),
        providers=build_multi_ai_providers(PUBLIC_DEMO_PROVIDERS, mode="local"),
    )
    payload = build_precomputed_review_summary(
        store,
        result.evidence_sha256,
        updated_at=updated_at,
        target_limit=target_limit,
        source_note=source_note,
        provider_mode=provider_mode,
        **_public_profile_kwargs(service),
    )
    return result, payload


def _public_profile_kwargs(service: str) -> dict:
    profile_dir = PUBLIC_PROFILE_CONTEXTS.get(service)
    if not profile_dir:
        return {}
    approved_profile = _load_json(profile_dir / "approved_profile.json")
    return {
        "source_context": _load_json(profile_dir / "source_context_bundle.json"),
        "source_analysis": _load_json(profile_dir / "source_analysis_bundle.json"),
        "profile_draft": _load_json(profile_dir / "profile_draft.json"),
        "approved_profile": approved_profile,
        "profile_id": str(approved_profile.get("profile_id") or ""),
    }


def _load_json(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(data, dict), path
    return data


def _landing_card_html(html: str, evidence_sha: str) -> str:
    marker = f"<span class=\"sha\">{evidence_sha[:12]}</span>"
    marker_index = html.index(marker)
    start = html.rfind("<article", 0, marker_index)
    end = html.index("</article>", marker_index) + len("</article>")
    assert start >= 0
    return html[start:end]
