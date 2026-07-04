from __future__ import annotations

import json
from html.parser import HTMLParser
from pathlib import Path

from ops_evidence_synthesis.web.precomputed_review import render_precomputed_review_detail_page
from ops_evidence_synthesis.window_policy import validate_minimum_analysis_window


ROOT = Path(__file__).resolve().parents[1]
MANIFEST_DIR = ROOT / "data" / "public_evidence_manifests"


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


class _TargetHeadingParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.headings: list[str] = []
        self._article_stack: list[bool] = []
        self._capturing_h2 = False
        self._current_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "article":
            classes = dict(attrs).get("class") or ""
            self._article_stack.append("target" in classes.split())
        if tag == "h2" and any(self._article_stack):
            self._capturing_h2 = True
            self._current_parts = []

    def handle_data(self, data: str) -> None:
        if self._capturing_h2:
            self._current_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "h2" and self._capturing_h2:
            heading = " ".join("".join(self._current_parts).split())
            if heading:
                self.headings.append(heading)
            self._capturing_h2 = False
            self._current_parts = []
        if tag == "article" and self._article_stack:
            self._article_stack.pop()


def _detail_target_headings(html: str) -> list[str]:
    parser = _TargetHeadingParser()
    parser.feed(html)
    return parser.headings


def test_public_evidence_manifest_index_points_to_existing_manifests() -> None:
    index = _load_json(MANIFEST_DIR / "index.json")

    assert index["schema_version"] == "public_evidence_manifest_index.v1"
    assert index["manifests"]

    for relative_path in index["manifests"]:
        manifest_path = ROOT / relative_path
        assert manifest_path.is_file(), relative_path
        manifest = _load_json(manifest_path)
        assert manifest["schema_version"] == "public_evidence_manifest.v1"


def test_public_evidence_manifests_match_precomputed_payloads() -> None:
    for manifest_path in sorted(MANIFEST_DIR.glob("*_real_api.json")):
        manifest = _load_json(manifest_path)
        payload_path = ROOT / manifest["precomputed_payload_path"]
        payload = _load_json(payload_path)
        analysis_context = payload["analysis_context"]

        assert manifest["evidence_sha256"] == payload["evidence_sha256"]
        assert manifest["pipeline_run_id"] == analysis_context["pipeline_run_id"]
        assert manifest["api_revision"] == analysis_context["real_api_revision"]
        assert manifest["canonical_graph_sha256"] == payload["summary"]["canonical_graph_sha256"]
        assert manifest["input_fingerprint_sha256"] == payload["summary"]["input_fingerprint_sha256"]
        assert manifest["payload_sha256"] == payload["generation"]["payload_sha256"]

        source_boundary = manifest["source_boundary"]
        expected_source_boundary = {
            "raw_logs_committed": False,
            "raw_logs_uploaded_to_model": False,
            "raw_source_committed": False,
            "raw_source_uploaded_to_model": False,
            "row_level_sanitized_events_committed": False,
            "public_artifact_type": "manifest_and_precomputed_review",
            "raw_log_policy": analysis_context["raw_log_policy"],
            "raw_source_policy": analysis_context["raw_source_policy"],
        }
        assert {
            key: source_boundary.get(key)
            for key in expected_source_boundary
        } == expected_source_boundary
        for optional_key in ("model_projection_policy", "source_context_sha256", "source_analysis_sha256"):
            if optional_key in source_boundary:
                assert source_boundary[optional_key] == analysis_context[optional_key]
        assert manifest["sanitized_corpus"]["service"] == analysis_context["service"]
        assert manifest["sanitized_corpus"]["environment"] == analysis_context["environment"]
        assert manifest["sanitized_corpus"]["sanitized_row_count"] == analysis_context["sanitized_log_count"]
        assert manifest["sanitized_corpus"]["db_ingested_log_count"] == analysis_context["db_ingested_log_count"]
        assert manifest["sanitized_corpus"]["window_start"] == analysis_context["window_start"]
        assert manifest["sanitized_corpus"]["window_end"] == analysis_context["window_end"]
        window = validate_minimum_analysis_window(
            manifest["sanitized_corpus"]["window_start"],
            manifest["sanitized_corpus"]["window_end"],
            context=manifest_path.name,
        )
        assert manifest["sanitized_corpus"].get("analysis_window_hours", window.duration_hours) >= 24
        assert manifest["sanitized_corpus"]["public_row_level_file"] is None

        assert manifest["token_compression"]["evidence_item_count"] == analysis_context["evidence_item_count"]
        assert (
            manifest["token_compression"]["model_projection_evidence_items"]
            == analysis_context["model_projection_evidence_items"]
        )
        assert (
            manifest["token_compression"]["model_projection_occurrence_count"]
            == analysis_context["model_projection_occurrence_count"]
        )
        assert (
            manifest["token_compression"]["model_projection_occurrence_coverage_ratio"]
            == analysis_context["model_projection_occurrence_coverage_ratio"]
        )
        assert manifest["token_compression"]["policy"] == analysis_context["model_projection_policy"]
        assert (
            manifest["token_compression"]["interpretation"]
            == analysis_context["model_projection_interpretation"]
        )

        assert manifest["provider_summary"]["provider_count"] == analysis_context["provider_count"]
        assert (
            manifest["provider_summary"]["schema_valid_provider_count"]
            == analysis_context["schema_valid_provider_count"]
        )
        assert manifest["provider_summary"]["pipeline_status"] == payload["summary"]["providers"]["pipeline_status"]
        assert manifest["provider_summary"]["provider_count"] == payload["summary"]["providers"]["total"]

        manifest_providers = {
            provider["provider_id"]: provider
            for provider in manifest["provider_summary"]["providers"]
        }
        payload_providers = {
            provider["provider_id"]: provider
            for provider in payload["provider_statuses"]
        }
        assert manifest_providers.keys() == payload_providers.keys()
        for provider_id, provider in manifest_providers.items():
            payload_provider = payload_providers[provider_id]
            assert provider["display_name"] == payload_provider["display_name"]
            assert provider["model_name"] == payload_provider["model_name"]
            assert provider["status"] == payload_provider["status"]
            assert provider["schema_valid"] == payload_provider["schema_valid"]
            assert provider["raw_output_sha256"] == payload_provider["raw_output_sha256"]
            assert provider["parsed_json_sha256"] == payload_provider["parsed_json_sha256"]
            assert provider["input_tokens"] == payload_provider["input_tokens"]
            assert provider["output_tokens"] == payload_provider["output_tokens"]

        assert manifest["review_summary"]["primary_targets"] == payload["summary"]["review"]["primary_targets"]
        assert manifest["review_summary"]["validation_targets"] == payload["summary"]["review"]["validation_targets"]
        assert manifest["review_summary"]["monitor_only"] == payload["summary"]["review"]["monitor_only"]
        assert manifest["review_summary"]["auto_archived"] == payload["summary"]["review"]["auto_archived"]
        titles = [target["title"] for target in payload["targets"]]
        assert len(titles) == len(set(titles)), f"duplicate public target titles in {manifest_path.name}"
        assert (
            manifest["review_summary"]["incident_baseline"]
            == payload["review_graph_summary"]["incident_baseline"]
        )
        assert (
            manifest["review_summary"]["incident_gate_signal"]
            == payload["review_graph_summary"]["incident_gate_signal"]
        )
        assert (
            manifest["review_summary"]["target_promotion_policy"]
            == payload["review_graph_summary"]["target_promotion_policy"]
        )
        assert (
            manifest["review_summary"]["technical_baseline"]
            == payload["review_graph_summary"]["technical_baseline"]
        )
        assert (
            manifest["review_summary"]["provider_detection_overlap"]
            == payload["review_graph_summary"]["provider_detection_overlap"]
        )

        profile_gate = manifest["profile_gate"]
        profile_context = payload["profile_context"]
        assert profile_gate["profile_status"] == profile_context["profile_status"]
        assert profile_gate["confidence_action"] == profile_context["confidence_action"]
        assert profile_gate["confidence_summary"] == profile_context["confidence_summary"]
        assert profile_gate["confirmed_user_outcomes"] == profile_context["confirmed_user_outcomes"]
        assert profile_gate["provisional_user_outcomes"] == profile_context["provisional_user_outcomes"]
        assert profile_gate["human_questions"] == profile_context["human_questions"]
        assert profile_gate["profile_to_review_links"] == profile_context["profile_to_review_links"]
        assert profile_gate["context_is_not_incident_evidence"] is True


def test_public_evidence_manifest_detail_target_headings_are_unique() -> None:
    for manifest_path in sorted(MANIFEST_DIR.glob("*_real_api.json")):
        manifest = _load_json(manifest_path)
        payload_path = ROOT / manifest["precomputed_payload_path"]
        payload = _load_json(payload_path)
        target_count = len([target for target in payload.get("targets") or [] if isinstance(target, dict)])

        html = render_precomputed_review_detail_page(payload["evidence_sha256"], payload)
        headings = _detail_target_headings(html)
        duplicate_headings = sorted(
            {
                heading
                for heading in headings
                if headings.count(heading) > 1
            }
        )

        assert len(headings) == target_count, f"detail target heading count mismatch in {manifest_path.name}"
        assert duplicate_headings == [], f"duplicate detail target headings in {manifest_path.name}: {duplicate_headings}"
        if manifest_path.name == "amazon_notify_real_api.json":
            assert "Review target requires validation: general" not in headings


def test_public_evidence_manifests_do_not_publish_local_artifact_paths() -> None:
    forbidden_fragments = (
        "/home/",
        "/mnt/",
        "workspace/",
        "sanitized_events.jsonl",
        ".sqlite",
        ".db",
    )

    for manifest_path in sorted(MANIFEST_DIR.glob("*.json")):
        content = manifest_path.read_text(encoding="utf-8")
        for fragment in forbidden_fragments:
            assert fragment not in content, f"{fragment} leaked in {manifest_path.name}"


def test_stream_v3_manifest_values_match_public_run_documentation() -> None:
    docs = (ROOT / "docs" / "stream-v3-real-api-runs.md").read_text(encoding="utf-8")
    stream_manifests = [
        MANIFEST_DIR / "stream_v3_dell_runtime_real_api.json",
        MANIFEST_DIR / "stream_v3_arena_monitoring_real_api.json",
    ]

    for manifest_path in stream_manifests:
        manifest = _load_json(manifest_path)
        payload = _load_json(ROOT / manifest["precomputed_payload_path"])
        analysis_context = payload["analysis_context"]
        profile_gate = manifest["profile_gate"]
        confidence = profile_gate["confidence_summary"]

        expected_strings = [
            manifest["evidence_sha256"],
            manifest["api_revision"],
            manifest["canonical_graph_sha256"],
            manifest["input_fingerprint_sha256"],
            manifest["payload_sha256"],
            manifest["source_boundary"]["source_context_sha256"],
            manifest["source_boundary"]["source_analysis_sha256"],
            manifest["precomputed_payload_path"],
            f"{manifest['sanitized_corpus']['sanitized_row_count']:,}",
            str(manifest["sanitized_corpus"]["window_start"]),
            str(manifest["sanitized_corpus"]["window_end"]),
            f"{analysis_context['provider_full_corpus_chunk_count']:,}",
            f"{analysis_context['provider_full_corpus_evidence_items']:,}",
            profile_gate["confidence_action"],
            str(confidence["overall_confidence"]),
        ]
        expected_strings.extend(
            provider["model_name"]
            for provider in manifest["provider_summary"]["providers"]
        )

        for expected in expected_strings:
            assert expected in docs, f"{expected} missing from docs/stream-v3-real-api-runs.md"
