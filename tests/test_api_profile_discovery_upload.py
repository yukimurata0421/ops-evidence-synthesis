from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient

from ops_evidence_synthesis.api import app
from ops_evidence_synthesis.local_first import build_bundle_from_sanitized, sanitize_input
from ops_evidence_synthesis.profile_discovery import build_profile_discovery_bundle


ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = ROOT / "sample_projects" / "profile_discovery_sample"


def _discovery_bundle(tmp_path: Path) -> dict[str, object]:
    out = tmp_path / "local_first"
    sanitize_input(ROOT / "sample_logs" / "secret_heavy.jsonl", out)
    evidence = build_bundle_from_sanitized(
        out / "sanitized_events.jsonl",
        service="unknown-sample",
        environment="prod",
        start="2026-06-16T00:00:00Z",
        end="2026-06-16T18:00:00Z",
        profile_name="generic",
        out_path=out / "evidence_bundle.json",
    )
    return build_profile_discovery_bundle(
        PROJECT_ROOT,
        evidence_bundle_path=out / "evidence_bundle.json",
        service="unknown-sample",
        environment="prod",
    )


def test_profile_discovery_upload_accepts_valid_bundle_and_returns_local_draft(tmp_path: Path) -> None:
    bundle = _discovery_bundle(tmp_path)
    with TestClient(app) as client:
        response = client.post("/profile-discovery/upload", json={"profile_discovery_bundle": bundle})
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["status"] == "accepted"
        assert payload["server_validation"]["passed"] is True
        assert payload["server_validation"]["discovery_sha256_verified"] is True
        assert payload["discovery_sha256"] == bundle["discovery_sha256"]
        assert payload["profile_draft"]["approved"] is False
        assert payload["profile_draft"]["explicit_profile"] is False
        assert payload["profile_draft"]["human_review_required"] is True


def test_profile_discovery_upload_rejects_secret_without_echoing_value(tmp_path: Path) -> None:
    bundle = _discovery_bundle(tmp_path)
    unsafe = copy.deepcopy(bundle)
    leaked = "raw-profile-discovery-token-123456"
    unsafe["observed_entities"][0]["name"] = f"Authorization: Bearer {leaked}"
    with TestClient(app) as client:
        response = client.post("/profile-discovery/upload", json=unsafe)
        assert response.status_code == 400
        assert "unsafe_content" in response.text
        assert leaked not in response.text


def test_profile_draft_approval_endpoint_returns_explicit_profile(tmp_path: Path) -> None:
    bundle = _discovery_bundle(tmp_path)
    with TestClient(app) as client:
        uploaded = client.post("/profile-discovery/upload", json={"profile_discovery_bundle": bundle})
        assert uploaded.status_code == 200, uploaded.text
        draft = uploaded.json()["profile_draft"]
        response = client.post(
            "/profile-drafts/approve",
            json={
                "profile_draft": draft,
                "profile_id": "unknown-sample-approved",
                "approved_by": "local-reviewer",
                "note": "approved via API smoke",
            },
        )
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["status"] == "approved"
        assert payload["approved"] is True
        assert payload["explicit_profile"] is True
        assert payload["approved_profile"]["profile_id"] == "unknown_sample_approved"
