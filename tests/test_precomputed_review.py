from __future__ import annotations

import json
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
from ops_evidence_synthesis.web.precomputed_review import (
    _precomputed_review_graph_response,
    _public_precomputed_landing_page,
    _render_precomputed_graph_page,
    _render_precomputed_review_detail_page,
    render_rescore_demo_page,
)


ROOT = Path(__file__).resolve().parents[1]
PUBLIC_SAMPLE_SHA = "1be4a21441fec7d2a4eafa95508badbe4a892bd61f3d9e08541893fba97c6731"
PUBLIC_FLAGSHIP_SHA = "c43cb9ccb916abdb73e71e05b4f643f6419eb74de6324094be25400557f6ed1e"
REAL_API_QWEN_GLM_SHA = "7e95346cbf15de7f104631b72d784e02665d0cc1488e42a4ccf69b76fe47308d"
STREAM_V3_DELL_REAL_API_SHA = "64fa79977171fe9bad0664d115ff0ffcf4e248cd12a6a938e62d25cba7b12681"
STREAM_V3_ARENA_REAL_API_SHA = "f22b327f601738de5c7011c9424fe7c615ed35ea693f791849a54af8d7271769"
PUBLIC_PROFILE_CONTEXTS = {
    "amazon-notify": ROOT / "data" / "public_profile_contexts" / "amazon_notify_sample",
    "payment-api": ROOT / "data" / "public_profile_contexts" / "payment_api_sample",
}


def test_public_landing_page_lists_real_api_reviews_only(monkeypatch) -> None:
    monkeypatch.delenv("OES_PRECOMPUTED_REVIEW_DIR", raising=False)
    monkeypatch.delenv("OES_PRECOMPUTED_REVIEW_DIRS", raising=False)

    html = _public_precomputed_landing_page()

    assert REAL_API_QWEN_GLM_SHA[:12] in html
    assert STREAM_V3_DELL_REAL_API_SHA[:12] in html
    assert STREAM_V3_ARENA_REAL_API_SHA[:12] in html
    assert PUBLIC_SAMPLE_SHA[:12] not in html
    assert PUBLIC_FLAGSHIP_SHA[:12] not in html
    assert "Multi-AI disagreement requires validation" not in html
    assert "/ui/rescore-demo?id=amazon-notify-more-data-rescore" in html


def test_public_rescore_demo_is_renderable() -> None:
    html = render_rescore_demo_page("amazon-notify-more-data-rescore")

    assert "More data rescore demo" in html
    assert "Gemini-led control plane" in html
    assert "gemini-enterprise-agent-platform" in html
    assert "qwen-agent-platform" in html
    assert "glm-agent-platform" in html
    assert "Provider positions" in html
    assert "needs_more_data -&gt; evidence_collected" in html
    assert "validation_target" in html
    assert "primary_candidate" in html
    assert "user_impact_unverified" in html
    assert "test_more_data_child_bundle_rescores_parent_graph_and_promotion" in html


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
    assert first_target["agreement"]["score_definition"] == "claimed successful providers / all successful providers"
    assert [row["stance"] for row in first_target["provider_positions"]] == ["claimed", "claimed", "silent"]

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
    assert graph["analysis_context"]["model_projection_evidence_items"] == 140
    assert graph["canonical_review_graph"]["analysis_context"]["db_ingested_log_count"] == 6506


def test_real_api_qwen_glm_precomputed_review_payload_is_renderable() -> None:
    payload_path = ROOT / "data" / "precomputed_review_summaries" / f"{REAL_API_QWEN_GLM_SHA}.json"
    payload = json.loads(payload_path.read_text(encoding="utf-8"))

    assert payload["summary"]["log_count"] == 6506
    assert payload["summary"]["providers"] == {
        "success": 5,
        "total": 5,
        "pipeline_status": "succeeded",
    }
    assert payload["summary"]["review"] == {
        "auto_archived": 0,
        "monitor_only": 2,
        "primary_targets": 1,
        "validation_targets": 6,
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
    assert payload["analysis_context"]["model_projection_occurrence_count"] == 4939
    assert payload["analysis_context"]["model_projection_occurrence_coverage_ratio"] == 0.759145
    assert payload["profile_context"]["profile_id"] == "amazon_notify_qwen_glm_full_corpus_approved"
    assert payload["profile_draft_generation"]["llm_status"] == "ok"
    assert payload["analysis_context"]["source_context_sha256"]
    assert payload["analysis_context"]["source_analysis_sha256"]

    detail_html = _render_precomputed_review_detail_page(REAL_API_QWEN_GLM_SHA, payload)
    graph_html = _render_precomputed_graph_page(REAL_API_QWEN_GLM_SHA, payload)
    graph = _precomputed_review_graph_response(payload, evidence_sha256=REAL_API_QWEN_GLM_SHA)

    assert "Five real providers" in detail_html
    assert "qwen-agent-platform" in detail_html
    assert "glm-agent-platform" in detail_html
    assert "4,939" in detail_html
    assert "qwen-agent-platform" in graph_html
    assert graph["canonical_review_graph"]["summary"]["primary_count"] == 1
    assert graph["canonical_review_graph"]["summary"]["validation_count"] == 6
    assert graph["canonical_review_graph"]["review_graph_summary"]["provider_detection_overlap"] == "5/5"
    assert graph["analysis_context"]["model_projection_occurrence_count"] == 4939


def test_stream_v3_real_api_precomputed_payloads_are_renderable() -> None:
    cases = [
        {
            "sha": STREAM_V3_DELL_REAL_API_SHA,
            "title": "Five real providers",
            "service": "stream_v3_runtime",
            "log_count": 8011,
            "providers": {"success": 5, "total": 5, "pipeline_status": "succeeded"},
            "review": {
                "auto_archived": 0,
                "monitor_only": 2,
                "primary_targets": 0,
                "validation_targets": 7,
            },
            "occurrences": 7383,
            "coverage": 0.921608,
            "gemini_valid": True,
        },
        {
            "sha": STREAM_V3_ARENA_REAL_API_SHA,
            "title": "Five real providers",
            "service": "stream_v3_monitoring",
            "log_count": 5055,
            "providers": {"success": 5, "total": 5, "pipeline_status": "succeeded"},
            "review": {
                "auto_archived": 1,
                "monitor_only": 2,
                "primary_targets": 0,
                "validation_targets": 6,
            },
            "occurrences": 496,
            "coverage": 0.098121,
            "gemini_valid": True,
        },
    ]

    for case in cases:
        payload_path = ROOT / "data" / "precomputed_review_summaries" / f"{case['sha']}.json"
        payload = json.loads(payload_path.read_text(encoding="utf-8"))

        assert payload["summary"]["log_count"] == case["log_count"]
        assert payload["summary"]["providers"] == case["providers"]
        assert payload["summary"]["review"] == case["review"]
        assert payload["analysis_context"]["service"] == case["service"]
        assert payload["analysis_context"]["model_projection_evidence_items"] == 140
        assert payload["analysis_context"]["model_projection_occurrence_count"] == case["occurrences"]
        assert payload["analysis_context"]["model_projection_occurrence_coverage_ratio"] == case["coverage"]
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
        assert payload["profile_draft_generation"]["llm_status"] == "ok"
        assert payload["profile_context"]["profile_id"]
        assert payload["analysis_context"]["source_context_sha256"]
        assert payload["analysis_context"]["source_analysis_sha256"]

        provider_rows = {row["provider_id"]: row for row in payload["provider_statuses"]}
        assert provider_rows["qwen-agent-platform"]["schema_valid"] is True
        assert provider_rows["glm-agent-platform"]["schema_valid"] is True
        assert provider_rows["gemini-enterprise-agent-platform"]["schema_valid"] is case["gemini_valid"]

        detail_html = _render_precomputed_review_detail_page(case["sha"], payload)
        graph_html = _render_precomputed_graph_page(case["sha"], payload)
        graph = _precomputed_review_graph_response(payload, evidence_sha256=case["sha"])

        assert case["title"] in detail_html
        assert case["service"] in detail_html
        assert "qwen-agent-platform" in detail_html
        assert "glm-agent-platform" in detail_html
        assert "DB-to-model projection" in detail_html
        assert str(case["occurrences"]) in detail_html.replace(",", "")
        assert "qwen-agent-platform" in graph_html
        assert graph["analysis_context"]["model_projection_occurrence_count"] == case["occurrences"]
        assert graph["canonical_review_graph"]["summary"]["validation_count"] == case["review"]["validation_targets"]


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
    assert target["agreement"]["score_definition"] == "claimed successful providers / all successful providers"
    assert [row["stance"] for row in target["provider_positions"]] == ["claimed", "claimed", "silent"]

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
