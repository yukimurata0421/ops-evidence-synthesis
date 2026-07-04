from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from scripts import check_precomputed_review_url as precomputed_smoke
from scripts import cloud_run_smoke


def _write_json(path: Path, payload: dict[str, Any]) -> Path:
    path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    return path


def test_check_precomputed_review_url_validates_public_pages_and_missing_routes(
    monkeypatch,
    capsys,
) -> None:
    hits: list[str] = []
    missing_hits: list[str] = []
    all_needles = "\n".join(
        [
            *precomputed_smoke.PUBLIC_INDEX_NEEDLES,
            *precomputed_smoke.ROOT_NEEDLES,
            *precomputed_smoke.DETAIL_NEEDLES,
            *precomputed_smoke.API_VIEW_NEEDLES,
            *precomputed_smoke.VISUAL_GRAPH_NEEDLES,
            *precomputed_smoke.REPORT_NEEDLES,
            *precomputed_smoke.REVIEW_TARGET_NEEDLES,
            *precomputed_smoke.REVIEW_GRAPH_NEEDLES,
            *precomputed_smoke.RESCORE_DEMO_NEEDLES,
            "gemini-enterprise-agent-platform",
            "extra expected sentence",
            "precomputed_public",
        ]
    )

    def fake_get(url: str, *, timeout_seconds: float) -> tuple[int, float, str]:
        hits.append(url)
        assert timeout_seconds == 4.0
        return 200, 0.01, all_needles

    def fake_get_allowing_error(url: str, *, timeout_seconds: float) -> tuple[int, float, str]:
        missing_hits.append(url)
        assert timeout_seconds == 4.0
        return 404, 0.01, "not found"

    monkeypatch.setattr(precomputed_smoke, "_get", fake_get)
    monkeypatch.setattr(precomputed_smoke, "_get_allowing_error", fake_get_allowing_error)
    monkeypatch.setattr(precomputed_smoke.time, "time", lambda: 12345)

    result = precomputed_smoke.main(
        [
            "--base-url",
            "https://example.invalid/",
            "--evidence-sha",
            "e" * 64,
            "--missing-evidence-sha",
            "f" * 64,
            "--timeout-seconds",
            "4",
            "--expect-provider",
            "gemini-enterprise-agent-platform",
            "--expect-text",
            "extra expected sentence",
        ]
    )

    assert result == 0
    assert any("/ui/full-review-page?evidence_sha256=" in url for url in hits)
    assert any("/ui/rescore-demo?id=amazon-notify-more-data-rescore" in url for url in hits)
    assert any("/health?_" in url for url in hits)
    assert any("/docs?_" in url for url in missing_hits)
    assert any("/ui/summary?evidence_sha256=" in url for url in missing_hits)
    assert "precomputed review smoke: passed" in capsys.readouterr().out


def test_check_precomputed_review_url_rejects_public_forbidden_text(monkeypatch, capsys) -> None:
    def fake_get(_url: str, *, timeout_seconds: float) -> tuple[int, float, str]:
        return 200, 0.01, "\n".join([*precomputed_smoke.PUBLIC_INDEX_NEEDLES, "local-fail"])

    monkeypatch.setattr(precomputed_smoke, "_get", fake_get)

    result = precomputed_smoke.main(
        [
            "--base-url",
            "https://example.invalid",
            "--evidence-sha",
            "e" * 64,
        ]
    )

    assert result == 2
    assert "public demo forbidden text" in capsys.readouterr().out


def test_check_precomputed_review_url_can_allow_recorded_schema_invalid_provider(monkeypatch) -> None:
    all_needles = "\n".join(
        [
            *precomputed_smoke.PUBLIC_INDEX_NEEDLES,
            *precomputed_smoke.ROOT_NEEDLES,
            *precomputed_smoke.DETAIL_NEEDLES,
            *precomputed_smoke.API_VIEW_NEEDLES,
            *precomputed_smoke.VISUAL_GRAPH_NEEDLES,
            *precomputed_smoke.REPORT_NEEDLES,
            *precomputed_smoke.REVIEW_TARGET_NEEDLES,
            *precomputed_smoke.REVIEW_GRAPH_NEEDLES,
            *precomputed_smoke.RESCORE_DEMO_NEEDLES,
            "precomputed_public",
            "schema_valid=false",
        ]
    )

    monkeypatch.setattr(precomputed_smoke, "_get", lambda _url, *, timeout_seconds: (200, 0.01, all_needles))
    monkeypatch.setattr(
        precomputed_smoke,
        "_get_allowing_error",
        lambda _url, *, timeout_seconds: (404, 0.01, "not found"),
    )

    assert (
        precomputed_smoke.main(
            [
                "--base-url",
                "https://example.invalid",
                "--evidence-sha",
                "e" * 64,
                "--allow-non-valid-provider",
            ]
        )
        == 0
    )


def test_cloud_run_smoke_main_sends_write_token_and_validates_workflow(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    evidence_bundle = {"evidence_sha256": "e" * 64, "service": "demo", "environment": "prod"}
    profile = {"profile_id": "approved-demo", "explicit_profile": True}
    evidence_path = _write_json(tmp_path / "bundle.json", evidence_bundle)
    discovery_path = _write_json(tmp_path / "discovery.json", {"schema_version": "profile_discovery_bundle.v1"})
    draft_path = _write_json(tmp_path / "draft.json", {"schema_version": "profile_draft.v1"})
    profile_path = _write_json(tmp_path / "profile.json", profile)
    requests: list[tuple[str, str, dict[str, str] | None]] = []

    def fake_request(
        method: str,
        url: str,
        payload: dict[str, object] | None = None,
        *,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, dict[str, object]]:
        requests.append((method, url, headers))
        if url.endswith("/"):
            return 200, {}
        if url.endswith("/bundles/upload"):
            return 200, {"server_validation": {"passed": True}}
        if url.endswith("/profile-discovery/upload"):
            return 200, {"server_validation": {"passed": True}}
        if url.endswith("/profile-drafts/approve"):
            return 200, {"approved_profile": {"explicit_profile": True}}
        if url.endswith("/evidence-requests/plan"):
            return 200, {
                "plan": {
                    "execution_policy": {"planner_executes_commands": False},
                    "plan_valid": True,
                    "planner_quality_warnings": [],
                    "incident_window": {"start": "2026-01-01T00:00:00Z"},
                    "operator_display_timezone": "UTC",
                },
                "collection_instructions_markdown": "collect safely",
            }
        if url.endswith("/ai/multi-run"):
            return 200, {
                "model_runs": [{"status": "ok"}, {"status": "ok"}, {"status": "ok"}],
                "multi_ai_synthesis": {
                    "score_note": "Score is review priority, not truth probability.",
                    "disagreement_themes": [],
                    "finding_summary": {},
                },
                "canonical_review_graph": {},
                "canonical_graph_status": "persisted",
                "canonical_graph_sha256": "g" * 64,
                "input_fingerprint_sha256": "i" * 64,
            }
        if url.endswith("/review/arbitrate"):
            return 200, {"canonical_graph_status": "persisted"}
        if "/review/graph?evidence_sha256=" in url:
            return 200, {"canonical_graph_status": "persisted", "canonical_review_graph": {}}
        raise AssertionError(url)

    monkeypatch.setattr(cloud_run_smoke, "_request", fake_request)
    monkeypatch.setattr(
        cloud_run_smoke,
        "_request_text",
        lambda method, url, payload=None: (
            200,
            "Multi-AI runs Disagreement Themes Planner quality warnings Canonical Review Graph "
            "Canonical graph SHA Input fingerprint Arbitration version",
        ),
    )

    result = cloud_run_smoke.main(
        [
            "--base-url",
            "https://example.invalid",
            "--evidence-bundle",
            str(evidence_path),
            "--profile-discovery-bundle",
            str(discovery_path),
            "--profile-draft",
            str(draft_path),
            "--approved-profile",
            str(profile_path),
            "--write-token",
            "secret-token",
        ]
    )

    assert result == 0
    mutating_headers = [headers for method, _url, headers in requests if method == "POST"]
    assert mutating_headers
    assert all(headers == {"X-OES-Write-Token": "secret-token"} for headers in mutating_headers)
    assert "Cloud Run smoke: passed" in capsys.readouterr().out


def test_cloud_run_smoke_redacts_large_error_bodies() -> None:
    body = "Authorization: bearer secret\n" + ("x" * 1400)

    redacted = cloud_run_smoke._redacted_body(body)

    assert "Authorization:<redacted>" in redacted
    assert "bearer secret" not in redacted
    assert redacted.endswith("...[truncated]")
