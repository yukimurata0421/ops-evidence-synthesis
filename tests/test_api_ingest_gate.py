from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient

from ops_evidence_synthesis.api import _provider_error_message, app


def test_server_path_ingest_is_disabled_by_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OES_STORE", "sqlite")
    monkeypatch.setenv("OES_DB_PATH", str(tmp_path / "api.sqlite3"))
    monkeypatch.delenv("OES_SERVER_PATH_INGEST_ENABLED", raising=False)

    with TestClient(app) as client:
        response = client.post("/logs/jsonl", json={"path": str(tmp_path / "missing.jsonl")})

    assert response.status_code == 403
    assert "server path ingest is disabled" in response.text


def test_server_path_ingest_can_be_enabled_in_trusted_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OES_STORE", "sqlite")
    monkeypatch.setenv("OES_DB_PATH", str(tmp_path / "api.sqlite3"))
    monkeypatch.setenv("OES_SERVER_PATH_INGEST_ENABLED", "1")
    path = tmp_path / "events.jsonl"
    path.write_text(
        json.dumps(
            {
                "timestamp": "2026-06-19T00:00:00Z",
                "service": "demo",
                "environment": "prod",
                "severity": "INFO",
                "message": "demo event",
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )

    with TestClient(app) as client:
        response = client.post("/logs/jsonl", json={"path": str(path)})

    assert response.status_code == 200, response.text
    assert response.json()["ingested_logs"] == 1


def test_write_token_guard_blocks_mutations_when_configured(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OES_STORE", "sqlite")
    monkeypatch.setenv("OES_DB_PATH", str(tmp_path / "api.sqlite3"))
    monkeypatch.setenv("OES_API_WRITE_TOKEN", "secret-token")

    with TestClient(app) as client:
        blocked = client.post("/bundles", json={})
        allowed = client.get("/workflow/provider-policy")

    assert blocked.status_code == 403
    assert blocked.json()["detail"] == "write token required"
    assert blocked.headers["x-request-id"]
    assert allowed.status_code == 200


def test_public_fast_gcp_review_uses_fixed_sample_without_write_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    summaries = tmp_path / "summaries"
    monkeypatch.setenv("OES_STORE", "sqlite")
    monkeypatch.setenv("OES_DB_PATH", str(tmp_path / "api.sqlite3"))
    monkeypatch.setenv("OES_API_WRITE_TOKEN", "secret-token")
    monkeypatch.setenv("OES_UI_PRECOMPUTED_ONLY", "1")
    monkeypatch.setenv("OES_FAST_GCP_REVIEW_PROVIDER_MODE", "local")
    monkeypatch.setenv("OES_FAST_GCP_REVIEW_SAMPLE_ROWS", "20")
    monkeypatch.setenv("OES_PUBLIC_FAST_GCP_REVIEW_CACHE_SECONDS", "0")
    monkeypatch.setenv("OES_FAST_GCP_REVIEW_OUTPUT_DIR", str(summaries))
    monkeypatch.setenv("OES_PRECOMPUTED_REVIEW_DIR", str(summaries))

    with TestClient(app) as client:
        page = client.get("/ui/fast-gcp-review")
        result = client.post("/public/fast-gcp-review", json={})
        cross_check = client.post("/public/fast-gcp-review", json={"cross_check": True})
        blocked = client.post("/ai/multi-run", json={})

    assert page.status_code == 200, page.text
    assert "Gemini Flash Lite" in page.text
    assert "Run Fast Cross-check Lite" in page.text
    assert "Watch More Data Rescore" in page.text
    assert "Sanitized system code preview" in page.text
    assert "amazon_notify_sample_source_approved" in page.text
    assert "amazon-notify-main-watchdog.service" in page.text
    assert "job_configuration_mismatch_count" in page.text
    assert "watchdog_heartbeat_count" in page.text
    assert "context, not incident evidence" in page.text
    assert result.status_code == 200, result.text
    payload = result.json()
    assert payload["status"] == "ok"
    assert payload["input"]["sample"] == "amazon-notify"
    assert payload["input"]["arbitrary_input_accepted"] is False
    assert payload["provider"]["provider_id"] == "local-gemini"
    assert payload["timing"]["wall_seconds"] >= 0
    assert payload["system_preview"]["profile_id"] == "amazon_notify_sample_source_approved"
    assert payload["system_preview"]["context_boundary"] == "sanitized_source_context_only_not_incident_evidence"
    assert payload["system_preview"]["components"][0]["name"] == "amazon_notify_main_watchdog"
    assert payload["system_preview"]["metric_semantics"][0]["name"] == "job_configuration_mismatch_count"
    assert payload["urls"]["detail"].startswith("/ui/full-review-page?evidence_sha256=")
    assert payload["urls"]["rescore"] == "/ui/rescore-demo?id=amazon-notify-more-data-rescore"
    assert payload["rescore_demo"]["demo_id"] == "amazon-notify-more-data-rescore"
    assert payload["review"]["public_review_id"] != payload["review"]["evidence_sha256"]
    assert cross_check.status_code == 200, cross_check.text
    cross_payload = cross_check.json()
    assert cross_payload["variant"] == "fast_cross_check_lite"
    assert cross_payload["providers"]["requested"] == ["local-gemini", "local-gpt-oss"]
    assert cross_payload["providers"]["total"] == 2
    assert cross_payload["providers"]["success"] == 2
    assert cross_payload["timing"]["provider_latency_sum_ms"] >= cross_payload["timing"]["provider_latency_ms"]
    assert cross_payload["urls"]["detail"].startswith("/ui/full-review-page?evidence_sha256=")
    assert cross_payload["urls"]["rescore"] == "/ui/rescore-demo?id=amazon-notify-more-data-rescore"
    assert cross_payload["review"]["public_review_id"] != cross_payload["review"]["evidence_sha256"]
    assert cross_payload["review"]["public_review_id"] != payload["review"]["public_review_id"]
    assert cross_payload["urls"]["detail"] != payload["urls"]["detail"]
    assert (summaries / f"{payload['review']['public_review_id']}.json").exists()
    assert (summaries / f"{cross_payload['review']['public_review_id']}.json").exists()
    assert blocked.status_code == 403

    with TestClient(app) as client:
        detail = client.get(payload["urls"]["detail"])
        cross_detail = client.get(cross_payload["urls"]["detail"])
        graph = client.get(payload["urls"]["graph"])

    assert detail.status_code == 200, detail.text
    assert cross_detail.status_code == 200, cross_detail.text
    assert graph.status_code == 200, graph.text


def test_write_token_guard_accepts_header_and_body_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OES_STORE", "sqlite")
    monkeypatch.setenv("OES_DB_PATH", str(tmp_path / "api.sqlite3"))
    monkeypatch.setenv("OES_SERVER_PATH_INGEST_ENABLED", "1")
    monkeypatch.setenv("OES_API_WRITE_TOKEN", "secret-token")
    path = tmp_path / "events.jsonl"
    path.write_text(
        json.dumps(
            {
                "timestamp": "2026-06-19T00:00:00Z",
                "service": "demo",
                "environment": "prod",
                "severity": "INFO",
                "message": "demo event",
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )

    with TestClient(app) as client:
        header_response = client.post(
            "/logs/jsonl",
            json={"path": str(path)},
            headers={"X-OES-Write-Token": "secret-token"},
        )
        body_response = client.post(
            "/logs/jsonl",
            json={"path": str(path), "api_token": "secret-token"},
        )

    assert header_response.status_code == 200, header_response.text
    assert body_response.status_code == 200, body_response.text


def test_write_token_guard_tolerates_secret_manager_trailing_newline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OES_STORE", "sqlite")
    monkeypatch.setenv("OES_DB_PATH", str(tmp_path / "api.sqlite3"))
    monkeypatch.setenv("OES_API_WRITE_TOKEN", "secret-token\n")

    with TestClient(app) as client:
        response = client.post(
            "/bundles/upload",
            json={"bundle": {}},
            headers={"X-OES-Write-Token": "secret-token"},
        )

    assert response.status_code == 400
    assert response.json()["detail"]["message"] == "evidence bundle validation failed"


def test_workflow_provider_policy_reports_skip_and_cost_policy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OES_STORE", "sqlite")
    monkeypatch.setenv("OES_DB_PATH", str(tmp_path / "api.sqlite3"))
    monkeypatch.setenv("OES_WORKFLOW_MAX_ESTIMATED_COST_USD", "1.25")
    monkeypatch.setenv("OES_DISABLED_PROVIDERS", "mistral,gpt-oss,claude")

    with TestClient(app) as client:
        response = client.get("/workflow/provider-policy")

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["schema_version"] == "workflow_provider_policy.v1"
    assert payload["alternatives"]["skip"] is True
    assert payload["cost_policy"]["enforced"] is True
    assert payload["cost_policy"]["max_estimated_cost_usd"] == 1.25
    assert all("latest_error" not in row for row in payload["providers"])
    assert all("model_name" not in row for row in payload["providers"])


def test_provider_policy_internal_details_require_explicit_env_flag(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OES_STORE", "sqlite")
    monkeypatch.setenv("OES_DB_PATH", str(tmp_path / "api.sqlite3"))
    monkeypatch.delenv("OES_PUBLIC_PROVIDER_DETAILS", raising=False)

    with TestClient(app) as client:
        public_response = client.get("/workflow/provider-policy?include_internal=true")

    assert public_response.status_code == 200, public_response.text
    public_payload = public_response.json()
    assert all("model_name" not in row for row in public_payload["providers"])

    monkeypatch.setenv("OES_PUBLIC_PROVIDER_DETAILS", "1")
    with TestClient(app) as client:
        internal_response = client.get("/workflow/provider-policy?include_internal=true")

    assert internal_response.status_code == 200, internal_response.text
    internal_payload = internal_response.json()
    assert any("model_name" in row for row in internal_payload["providers"])


def test_provider_error_message_is_safe_for_public_policy() -> None:
    raw_output = json.dumps(
        {
            "message": (
                "quota exceeded; see https://cloud.example.invalid/quota\n"
                "Authorization: Bearer raw-token-123456789"
            )
        },
        sort_keys=True,
    )

    message = _provider_error_message(raw_output)

    assert "https://cloud.example.invalid" not in message
    assert "raw-token-123456789" not in message
