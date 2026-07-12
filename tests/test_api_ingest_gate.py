from __future__ import annotations

import base64
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient

from ops_evidence_synthesis.api import _provider_error_message, app
from ops_evidence_synthesis.routes import api_routes
from ops_evidence_synthesis.web import precomputed_review as web_precomputed


def _clear_fast_gcp_review_state() -> None:
    api_routes._FAST_GCP_REVIEW_CACHE.clear()
    api_routes._FAST_GCP_REVIEW_STATUS_CACHE.clear()
    api_routes._FAST_GCP_REVIEW_QUOTA_CACHE.clear()
    api_routes._FAST_GCP_REVIEW_DISABLE_CACHE = None
    api_routes._PUBLIC_RATE_LIMIT_COUNTERS.clear()


def _clear_fast_gcp_review_storage_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "OES_FAST_GCP_REVIEW_OUTPUT_DIR",
        "OES_FAST_GCP_REVIEW_STATUS_DIR",
        "OES_FAST_GCP_REVIEW_STATUS_GCS_PREFIX",
        "OES_FAST_GCP_REVIEW_QUOTA_DIR",
        "OES_FAST_GCP_REVIEW_QUOTA_GCS_PREFIX",
        "OES_FAST_GCP_REVIEW_GCS_PREFIX",
        "OES_PRECOMPUTED_REVIEW_GCS_PREFIX",
    ):
        monkeypatch.delenv(name, raising=False)


def test_public_fast_gcp_review_default_contract_is_fixed_sample_and_flash_lite(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OES_FAST_GCP_REVIEW_SAMPLE_ROWS", raising=False)
    monkeypatch.delenv("OES_FAST_GCP_CROSS_CHECK_SAMPLE_ROWS", raising=False)
    monkeypatch.delenv("OES_FAST_GCP_REVIEW_PROVIDER_MODE", raising=False)
    monkeypatch.delenv("OES_FAST_GCP_GEMINI_MODEL", raising=False)
    monkeypatch.delenv("OES_GEMMA_MODEL", raising=False)

    assert api_routes._fast_gcp_review_sample_rows() == 2000
    assert api_routes._fast_gcp_cross_check_sample_rows() == 200
    assert api_routes._fast_gcp_effective_sample_rows(cross_check=False) == 2000
    assert api_routes._fast_gcp_effective_sample_rows(cross_check=True) == 200
    assert api_routes._FAST_GCP_REVIEW_LOGIC_REVISION == "source-approved-evidence-v2"
    assert "source-approved-evidence-v2" in api_routes._fast_gcp_review_cache_key(cross_check=False)
    assert api_routes._fast_gcp_review_provider_names(cross_check=False) == ["gemini-fast-lite"]
    assert api_routes._fast_gcp_review_model_names(cross_check=False) == ["gemini-3.1-flash-lite"]
    assert api_routes._fast_gcp_public_provider_mode(cross_check=False) == "fast_gcp_vertex_gemini_flash_lite"
    assert api_routes._fast_gcp_review_provider_names(cross_check=True) == ["gemini-fast-lite", "gemma"]
    assert api_routes._fast_gcp_review_model_names(cross_check=True) == [
        "gemini-3.1-flash-lite",
        "gemma-4-26b-a4b-it-maas",
    ]


def test_public_fast_gcp_review_id_separates_runs_for_same_evidence_sha() -> None:
    payload = {
        "evidence_sha256": "e" * 64,
        "summary": {"canonical_graph_sha256": "g" * 64},
        "generation": {"provider_mode": "fast_gcp_vertex_gemini_flash_lite"},
        "provider_statuses": [
            {
                "provider_id": "gemini-fast-lite",
                "model_name": "gemini-3.1-flash-lite",
                "status": "ok",
                "schema_valid": True,
                "raw_output_sha256": "m" * 64,
            }
        ],
    }

    first = api_routes._fast_gcp_public_review_id(payload, run_id="fast-gcp-review-a", cross_check=False)
    second = api_routes._fast_gcp_public_review_id(payload, run_id="fast-gcp-review-b", cross_check=False)
    cross_check = api_routes._fast_gcp_public_review_id(payload, run_id="fast-gcp-review-a", cross_check=True)

    assert first != payload["evidence_sha256"]
    assert second != payload["evidence_sha256"]
    assert first != second
    assert first != cross_check
    assert len(first) == 64
    assert len(second) == 64


def test_public_fast_gcp_review_persists_same_evidence_sha_as_distinct_public_artifacts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_fast_gcp_review_state()
    _clear_fast_gcp_review_storage_env(monkeypatch)
    web_precomputed._PRECOMPUTED_REVIEW_CACHE.clear()
    output_dir = tmp_path / "precomputed"
    monkeypatch.setenv("OES_FAST_GCP_REVIEW_OUTPUT_DIR", str(output_dir))
    monkeypatch.setenv("OES_PRECOMPUTED_REVIEW_DIR", str(output_dir))
    monkeypatch.setenv("OES_PRECOMPUTED_REVIEW_CACHE_SECONDS", "0")
    evidence_sha = "e" * 64

    def payload(public_review_id: str, run_id: str, raw_output_sha256: str) -> dict[str, object]:
        return {
            "evidence_sha256": evidence_sha,
            "updated_at": "2026-07-04T00:00:00Z",
            "summary": {
                "status": "ok",
                "finding": {"title": run_id, "impact": "fixed sample review"},
                "review": {"primary_targets": 0, "validation_targets": 1},
                "providers": {"success": 1, "total": 1, "pipeline_status": "succeeded"},
                "raw_log_policy": "not_uploaded",
                "log_count": 2000,
                "canonical_graph_sha256": "g" * 64,
                "input_fingerprint_sha256": "i" * 64,
            },
            "generation": {
                "provider_mode": "fast_gcp_vertex_gemini_flash_lite",
                "fast_gcp_review": {
                    "public_review_id": public_review_id,
                    "run_id": run_id,
                    "sample_rows": 2000,
                    "model_names": ["gemini-3.1-flash-lite"],
                },
            },
            "provider_statuses": [
                {
                    "provider_id": "gemini-fast-lite",
                    "model_name": "gemini-3.1-flash-lite",
                    "status": "ok",
                    "schema_valid": True,
                    "raw_output_sha256": raw_output_sha256,
                }
            ],
            "review_graph_summary": {},
            "analysis_context": {},
            "targets": [],
        }

    first_id = "1" * 64
    second_id = "2" * 64
    api_routes._persist_fast_gcp_public_payload(payload(first_id, "fast-run-a", "a" * 64))
    api_routes._persist_fast_gcp_public_payload(payload(second_id, "fast-run-b", "b" * 64))

    first_path = output_dir / f"{first_id}.json"
    second_path = output_dir / f"{second_id}.json"
    assert first_path.exists()
    assert second_path.exists()
    assert not (output_dir / f"{evidence_sha}.json").exists()
    assert first_path.read_text(encoding="utf-8") != second_path.read_text(encoding="utf-8")

    first_lookup = web_precomputed._precomputed_review_payload(first_id)
    second_lookup = web_precomputed._precomputed_review_payload(second_id)
    evidence_lookup = web_precomputed._precomputed_review_payload(evidence_sha)

    assert first_lookup is not None
    assert second_lookup is not None
    assert first_lookup["generation"]["fast_gcp_review"]["run_id"] == "fast-run-a"
    assert second_lookup["generation"]["fast_gcp_review"]["run_id"] == "fast-run-b"
    assert first_lookup["provider_statuses"][0]["raw_output_sha256"] == "a" * 64
    assert second_lookup["provider_statuses"][0]["raw_output_sha256"] == "b" * 64
    assert evidence_lookup is None


def test_public_fast_gcp_review_status_persists_to_gcs_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_fast_gcp_review_state()
    _clear_fast_gcp_review_storage_env(monkeypatch)
    monkeypatch.setenv("OES_FAST_GCP_REVIEW_GCS_PREFIX", "gs://private/public-review")
    written: dict[str, tuple[str, str]] = {}

    from ops_evidence_synthesis.gcp import storage

    monkeypatch.setattr(
        storage,
        "write_text",
        lambda uri, text, *, content_type="text/plain": written.__setitem__(
            str(uri),
            (text, content_type),
        ),
    )
    monkeypatch.setattr(storage, "read_text", lambda uri: written[str(uri)][0])

    status = api_routes._fast_gcp_review_status_payload(
        run_id="fast-gcp-review-gcs",
        cross_check=False,
        status="running",
        current_step="provider_completed",
        progress_percent=72,
        message="provider completed",
        providers={"success": 1, "total": 1, "statuses": []},
    )
    api_routes._write_fast_gcp_review_status(status)
    api_routes._FAST_GCP_REVIEW_STATUS_CACHE.clear()

    status_uri = "gs://private/public-review/public-fast-gcp-review-runs/fast-gcp-review-gcs/status.json"
    read_back = api_routes._read_fast_gcp_review_status("fast-gcp-review-gcs")

    assert written[status_uri][1] == "application/json"
    assert json.loads(written[status_uri][0])["status"] == "running"
    assert read_back is not None
    assert read_back["status"] == "running"
    assert read_back["gcs"]["status_uri"] == status_uri
    assert read_back["urls"]["status"] == "/public/fast-gcp-review/status?run_id=fast-gcp-review-gcs"


def test_public_fast_gcp_review_quota_consumes_and_persists_gcs_records(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_fast_gcp_review_state()
    _clear_fast_gcp_review_storage_env(monkeypatch)
    monkeypatch.setenv("OES_FAST_GCP_REVIEW_PROVIDER_MODE", "real")
    monkeypatch.setenv("OES_FAST_GCP_REVIEW_GCS_PREFIX", "gs://private/public-review")
    monkeypatch.setenv("OES_PUBLIC_FAST_GCP_REVIEW_DAILY_LIMIT", "3")
    monkeypatch.setenv("OES_PUBLIC_FAST_GCP_REVIEW_CLIENT_DAILY_LIMIT", "2")
    monkeypatch.setenv("OES_PUBLIC_FAST_GCP_REVIEW_QUOTA_SALT", "stable-salt")
    monkeypatch.delenv("OES_PUBLIC_FAST_GCP_REVIEW_QUOTA_DISABLED", raising=False)
    monkeypatch.delenv("OES_PUBLIC_FAST_GCP_REVIEW_OWNER_TOKEN", raising=False)
    monkeypatch.setattr(api_routes.time, "strftime", lambda _format, _time_tuple: "2026-07-04")
    written: dict[str, tuple[str, str]] = {}

    class NotFound(Exception):
        pass

    def fake_read_text(uri: str) -> str:
        if uri not in written:
            raise NotFound(uri)
        return written[uri][0]

    from ops_evidence_synthesis.gcp import storage

    monkeypatch.setattr(storage, "read_text", fake_read_text)
    monkeypatch.setattr(
        storage,
        "write_text",
        lambda uri, text, *, content_type="text/plain": written.__setitem__(
            str(uri),
            (text, content_type),
        ),
    )
    request = SimpleNamespace(
        headers={"x-forwarded-for": "203.0.113.10"},
        cookies={},
        client=SimpleNamespace(host="198.51.100.10"),
    )

    quota = api_routes._consume_fast_gcp_review_quota(
        request,
        payload={},
        run_id="fast-gcp-review-gcs",
        cross_check=False,
    )

    total_uri = "gs://private/public-review/public-fast-gcp-review-quota/2026-07-04/total.json"
    client_uris = [uri for uri in written if uri != total_uri]
    assert quota == {
        "status": "accepted",
        "date": "2026-07-04",
        "daily_count": 1,
        "daily_limit": 3,
        "daily_remaining": 2,
        "client_daily_count": 1,
        "client_daily_limit": 2,
        "client_daily_remaining": 1,
        "live_api_allowed": True,
    }
    assert written[total_uri][1] == "application/json"
    assert len(client_uris) == 1
    assert client_uris[0].startswith(
        "gs://private/public-review/public-fast-gcp-review-quota/2026-07-04/client-"
    )
    total_record = json.loads(written[total_uri][0])
    client_record = json.loads(written[client_uris[0]][0])
    assert total_record["count"] == 1
    assert total_record["last_run_id"] == "fast-gcp-review-gcs"
    assert total_record["last_variant"] == "fast_gcp_review"
    assert client_record["count"] == 1
    assert client_record["last_run_id"] == "fast-gcp-review-gcs"


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


def test_public_write_guard_fails_closed_without_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OES_STORE", "sqlite")
    monkeypatch.setenv("OES_DB_PATH", str(tmp_path / "api.sqlite3"))
    monkeypatch.setenv("OES_UI_PRECOMPUTED_ONLY", "1")
    monkeypatch.delenv("OES_API_WRITE_TOKEN", raising=False)

    with TestClient(app) as client:
        blocked = client.post("/bundles", json={})

    assert blocked.status_code == 503
    assert blocked.json()["detail"] == "public write guard is not configured"


def test_public_runtime_guard_requires_write_token_and_rate_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OES_PUBLIC_RUNTIME_GUARD", "1")
    monkeypatch.setenv("OES_UI_PRECOMPUTED_ONLY", "1")
    monkeypatch.setenv("OES_PUBLIC_FAST_GCP_REVIEW_ENABLED", "0")
    monkeypatch.setenv("OES_PUBLIC_RATE_LIMIT_ENABLED", "1")
    monkeypatch.delenv("OES_API_WRITE_TOKEN", raising=False)

    with pytest.raises(RuntimeError, match="OES_API_WRITE_TOKEN"):
        api_routes.validate_public_runtime_config()

    monkeypatch.setenv("OES_API_WRITE_TOKEN", "secret-token")
    api_routes.validate_public_runtime_config()


def test_public_rate_limit_blocks_repeated_public_reads(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_fast_gcp_review_state()
    monkeypatch.setenv("OES_STORE", "sqlite")
    monkeypatch.setenv("OES_DB_PATH", str(tmp_path / "api.sqlite3"))
    monkeypatch.setenv("OES_API_WRITE_TOKEN", "secret-token")
    monkeypatch.setenv("OES_UI_PRECOMPUTED_ONLY", "1")
    monkeypatch.setenv("OES_PUBLIC_RATE_LIMIT_ENABLED", "1")
    monkeypatch.setenv("OES_PUBLIC_RATE_LIMIT_MAX_REQUESTS", "1")
    monkeypatch.setenv("OES_PUBLIC_FAST_GCP_REVIEW_ENABLED", "0")

    with TestClient(app) as client:
        first = client.get("/health")
        second = client.get("/health")

    assert first.status_code == 200, first.text
    assert second.status_code == 429, second.text
    assert second.json()["detail"]["reason_code"] == "public_rate_limited"


def test_public_fast_gcp_review_uses_fixed_sample_without_write_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_fast_gcp_review_state()
    summaries = tmp_path / "summaries"
    statuses = tmp_path / "statuses"
    quotas = tmp_path / "quotas"
    monkeypatch.setenv("OES_STORE", "sqlite")
    monkeypatch.setenv("OES_DB_PATH", str(tmp_path / "api.sqlite3"))
    monkeypatch.setenv("OES_API_WRITE_TOKEN", "secret-token")
    monkeypatch.setenv("OES_UI_PRECOMPUTED_ONLY", "1")
    monkeypatch.setenv("OES_FAST_GCP_REVIEW_PROVIDER_MODE", "local")
    monkeypatch.setenv("OES_FAST_GCP_REVIEW_SAMPLE_ROWS", "20")
    monkeypatch.setenv("OES_PUBLIC_FAST_GCP_REVIEW_CACHE_SECONDS", "0")
    monkeypatch.setenv("OES_PUBLIC_FAST_GCP_REVIEW_DAILY_LIMIT", "10")
    monkeypatch.setenv("OES_PUBLIC_FAST_GCP_REVIEW_CLIENT_DAILY_LIMIT", "10")
    monkeypatch.setenv("OES_FAST_GCP_REVIEW_OUTPUT_DIR", str(summaries))
    monkeypatch.setenv("OES_FAST_GCP_REVIEW_STATUS_DIR", str(statuses))
    monkeypatch.setenv("OES_FAST_GCP_REVIEW_QUOTA_DIR", str(quotas))
    monkeypatch.setenv("OES_PRECOMPUTED_REVIEW_DIR", str(summaries))

    with TestClient(app) as client:
        page = client.get("/ui/fast-gcp-review")
        initial_status = client.get("/public/fast-gcp-review/status?run_id=fast-gcp-review-test")
        result = client.post("/public/fast-gcp-review", json={"run_id": "fast-gcp-review-test"})
        cross_check = client.post(
            "/public/fast-gcp-review",
            json={"cross_check": True, "run_id": "fast-cross-check-test"},
        )
        blocked = client.post("/ai/multi-run", json={})

    assert page.status_code == 200, page.text
    assert "Gemini Flash Lite" in page.text
    assert "Load Sanitized Code Summary" in page.text
    assert 'id="source-preview-panel" hidden' in page.text
    assert "Run Live Fast Review" in page.text
    assert "Run Live Cross-check" in page.text
    assert "force: true" in page.text
    assert "Live review calls the model API and consumes public demo quota" in page.text
    assert "Quota guarded" in page.text
    assert "Repeat clicks can return the public cache" not in page.text
    assert "Cache-friendly" not in page.text
    assert "/public/fast-gcp-review/status" in page.text
    assert "/public/fast-gcp-review/owner-session" in page.text
    assert "source-approved-evidence-v2" in page.text
    assert "Cross-check rows" in page.text
    assert "owner_token" in page.text
    assert "Watch More Data Rescore" in page.text
    assert "Sanitized system code preview" in page.text
    assert "amazon_notify_sample_source_approved" in page.text
    assert "amazon-notify-main-watchdog.service" in page.text
    assert "job_configuration_mismatch_count" in page.text
    assert "watchdog_heartbeat_count" in page.text
    assert "context, not incident evidence" in page.text
    assert initial_status.status_code == 200, initial_status.text
    assert initial_status.json()["status"] == "not_started"
    assert result.status_code == 200, result.text
    payload = result.json()
    assert payload["status"] == "ok"
    assert payload["run_id"] == "fast-gcp-review-test"
    assert payload["input"]["sample"] == "amazon-notify"
    assert payload["input"]["arbitrary_input_accepted"] is False
    assert payload["provider"]["provider_id"] == "local-gemini"
    assert payload["quota"]["status"] == "accepted"
    assert payload["quota"]["client_daily_remaining"] >= 0
    assert payload["timing"]["wall_seconds"] >= 0
    assert payload["system_preview"]["profile_id"] == "amazon_notify_sample_source_approved"
    assert payload["system_preview"]["context_boundary"] == "sanitized_source_context_only_not_incident_evidence"
    assert payload["system_preview"]["components"][0]["name"] == "amazon_notify_main_watchdog"
    assert payload["system_preview"]["metric_semantics"][0]["name"] == "job_configuration_mismatch_count"
    assert payload["urls"]["detail"].startswith("/ui/full-review-page?evidence_sha256=")
    assert payload["urls"]["status"] == "/public/fast-gcp-review/status?run_id=fast-gcp-review-test"
    assert payload["urls"]["rescore"] == "/ui/rescore-demo?id=amazon-notify-more-data-rescore"
    assert payload["rescore_demo"]["demo_id"] == "amazon-notify-more-data-rescore"
    assert payload["review"]["public_review_id"] != payload["review"]["evidence_sha256"]
    assert cross_check.status_code == 200, cross_check.text
    cross_payload = cross_check.json()
    assert cross_payload["run_id"] == "fast-cross-check-test"
    assert cross_payload["variant"] == "fast_cross_check_lite"
    assert cross_payload["quota"]["status"] == "accepted"
    assert cross_payload["providers"]["requested"] == ["local-gemini", "local-gpt-oss"]
    assert cross_payload["providers"]["total"] == 2
    assert cross_payload["providers"]["success"] == 2
    assert cross_payload["timing"]["provider_latency_sum_ms"] >= cross_payload["timing"]["provider_latency_ms"]
    assert cross_payload["urls"]["detail"].startswith("/ui/full-review-page?evidence_sha256=")
    assert cross_payload["urls"]["status"] == "/public/fast-gcp-review/status?run_id=fast-cross-check-test"
    assert cross_payload["urls"]["rescore"] == "/ui/rescore-demo?id=amazon-notify-more-data-rescore"
    assert cross_payload["review"]["public_review_id"] != cross_payload["review"]["evidence_sha256"]
    assert cross_payload["review"]["public_review_id"] != payload["review"]["public_review_id"]
    assert cross_payload["urls"]["detail"] != payload["urls"]["detail"]
    assert (summaries / f"{payload['review']['public_review_id']}.json").exists()
    assert (summaries / f"{cross_payload['review']['public_review_id']}.json").exists()
    assert (statuses / "fast-gcp-review-test" / "status.json").exists()
    assert (statuses / "fast-cross-check-test" / "status.json").exists()
    assert any(path.name == "total.json" for path in quotas.glob("*/*.json"))
    assert blocked.status_code == 403

    with TestClient(app) as client:
        detail = client.get(payload["urls"]["detail"])
        cross_detail = client.get(cross_payload["urls"]["detail"])
        graph = client.get(payload["urls"]["graph"])
        status = client.get(payload["urls"]["status"])
        cross_status = client.get(cross_payload["urls"]["status"])

    assert detail.status_code == 200, detail.text
    assert cross_detail.status_code == 200, cross_detail.text
    assert graph.status_code == 200, graph.text
    assert status.status_code == 200, status.text
    status_payload = status.json()
    assert status_payload["schema_version"] == "public_fast_gcp_review_status.v1"
    assert status_payload["run_id"] == "fast-gcp-review-test"
    assert status_payload["status"] == "succeeded"
    assert status_payload["current_step"] == "completed"
    assert status_payload["progress_percent"] == 100
    assert status_payload["providers"]["success"] == 1
    assert status_payload["review"]["public_review_id"] == payload["review"]["public_review_id"]
    assert cross_status.status_code == 200, cross_status.text
    assert cross_status.json()["providers"]["success"] == 2


def test_budget_guard_disables_public_fast_gcp_review(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_fast_gcp_review_state()
    disable_file = tmp_path / "public-fast-gcp-review-disabled.json"
    summaries = tmp_path / "summaries"
    statuses = tmp_path / "statuses"
    quotas = tmp_path / "quotas"
    notification = {
        "budgetDisplayName": "Ops Evidence Hackathon Budget",
        "costAmount": 2900,
        "budgetAmount": 3000,
        "currencyCode": "JPY",
        "alertThresholdExceeded": 0.9,
    }
    encoded = base64.b64encode(json.dumps(notification).encode("utf-8")).decode("ascii")
    monkeypatch.setenv("OES_STORE", "sqlite")
    monkeypatch.setenv("OES_DB_PATH", str(tmp_path / "api.sqlite3"))
    monkeypatch.setenv("OES_API_WRITE_TOKEN", "secret-token")
    monkeypatch.setenv("OES_BUDGET_GUARD_TOKEN", "budget-token")
    monkeypatch.setenv("OES_PUBLIC_FAST_GCP_REVIEW_DISABLE_FILE", str(disable_file))
    monkeypatch.setenv("OES_PUBLIC_FAST_GCP_REVIEW_DISABLE_CACHE_SECONDS", "0")
    monkeypatch.setenv("OES_FAST_GCP_REVIEW_PROVIDER_MODE", "local")
    monkeypatch.setenv("OES_FAST_GCP_REVIEW_SAMPLE_ROWS", "20")
    monkeypatch.setenv("OES_PUBLIC_FAST_GCP_REVIEW_CACHE_SECONDS", "0")
    monkeypatch.setenv("OES_FAST_GCP_REVIEW_OUTPUT_DIR", str(summaries))
    monkeypatch.setenv("OES_FAST_GCP_REVIEW_STATUS_DIR", str(statuses))
    monkeypatch.setenv("OES_FAST_GCP_REVIEW_QUOTA_DIR", str(quotas))
    monkeypatch.setenv("OES_PRECOMPUTED_REVIEW_DIR", str(summaries))

    with TestClient(app) as client:
        invalid = client.post("/internal/budget-guard/fast-gcp-review", json={})
        guard = client.post(
            "/internal/budget-guard/fast-gcp-review?token=budget-token",
            json={"message": {"data": encoded}},
        )
        blocked = client.post("/public/fast-gcp-review", json={"run_id": "budget-disabled"})
        status = client.get("/public/fast-gcp-review/status?run_id=budget-disabled")

    assert invalid.status_code == 403
    assert guard.status_code == 200, guard.text
    assert guard.json()["status"] == "disabled"
    assert disable_file.exists()
    state = json.loads(disable_file.read_text(encoding="utf-8"))
    assert state["disabled"] is True
    assert state["budget"]["trigger_ratio"] >= 0.9
    assert blocked.status_code == 503, blocked.text
    assert blocked.json()["detail"]["reason_code"] == "budget_threshold_exceeded"
    assert status.status_code == 200
    assert status.json()["current_step"] == "budget_guard_blocked"


def test_public_fast_gcp_review_enforces_live_quota(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_fast_gcp_review_state()
    summaries = tmp_path / "summaries"
    statuses = tmp_path / "statuses"
    quotas = tmp_path / "quotas"
    monkeypatch.setenv("OES_STORE", "sqlite")
    monkeypatch.setenv("OES_DB_PATH", str(tmp_path / "api.sqlite3"))
    monkeypatch.setenv("OES_API_WRITE_TOKEN", "secret-token")
    monkeypatch.setenv("OES_UI_PRECOMPUTED_ONLY", "1")
    monkeypatch.setenv("OES_FAST_GCP_REVIEW_PROVIDER_MODE", "local")
    monkeypatch.setenv("OES_FAST_GCP_REVIEW_SAMPLE_ROWS", "20")
    monkeypatch.setenv("OES_PUBLIC_FAST_GCP_REVIEW_CACHE_SECONDS", "0")
    monkeypatch.setenv("OES_PUBLIC_FAST_GCP_REVIEW_DAILY_LIMIT", "10")
    monkeypatch.setenv("OES_PUBLIC_FAST_GCP_REVIEW_CLIENT_DAILY_LIMIT", "1")
    monkeypatch.setenv("OES_FAST_GCP_REVIEW_OUTPUT_DIR", str(summaries))
    monkeypatch.setenv("OES_FAST_GCP_REVIEW_STATUS_DIR", str(statuses))
    monkeypatch.setenv("OES_FAST_GCP_REVIEW_QUOTA_DIR", str(quotas))
    monkeypatch.setenv("OES_PRECOMPUTED_REVIEW_DIR", str(summaries))

    with TestClient(app) as client:
        first = client.post("/public/fast-gcp-review", json={"run_id": "quota-first"})
        second = client.post("/public/fast-gcp-review", json={"run_id": "quota-second"})
        second_status = client.get("/public/fast-gcp-review/status?run_id=quota-second")

    assert first.status_code == 200, first.text
    assert first.json()["quota"]["client_daily_remaining"] == 0
    assert second.status_code == 429, second.text
    detail = second.json()["detail"]
    assert detail["reason_code"] == "client_quota_exceeded"
    assert detail["quota"]["live_api_allowed"] is False
    assert second_status.status_code == 200, second_status.text
    status_payload = second_status.json()
    assert status_payload["status"] == "failed"
    assert status_payload["current_step"] == "quota_blocked"
    assert status_payload["reason_code"] == "client_quota_exceeded"


def test_public_fast_gcp_review_cache_hit_does_not_consume_live_quota(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_fast_gcp_review_state()
    summaries = tmp_path / "summaries"
    statuses = tmp_path / "statuses"
    quotas = tmp_path / "quotas"
    monkeypatch.setenv("OES_STORE", "sqlite")
    monkeypatch.setenv("OES_DB_PATH", str(tmp_path / "api.sqlite3"))
    monkeypatch.setenv("OES_API_WRITE_TOKEN", "secret-token")
    monkeypatch.setenv("OES_UI_PRECOMPUTED_ONLY", "1")
    monkeypatch.setenv("OES_FAST_GCP_REVIEW_PROVIDER_MODE", "local")
    monkeypatch.setenv("OES_FAST_GCP_REVIEW_SAMPLE_ROWS", "20")
    monkeypatch.setenv("OES_PUBLIC_FAST_GCP_REVIEW_CACHE_SECONDS", "3600")
    monkeypatch.setenv("OES_PUBLIC_FAST_GCP_REVIEW_DAILY_LIMIT", "1")
    monkeypatch.setenv("OES_PUBLIC_FAST_GCP_REVIEW_CLIENT_DAILY_LIMIT", "1")
    monkeypatch.setenv("OES_FAST_GCP_REVIEW_OUTPUT_DIR", str(summaries))
    monkeypatch.setenv("OES_FAST_GCP_REVIEW_STATUS_DIR", str(statuses))
    monkeypatch.setenv("OES_FAST_GCP_REVIEW_QUOTA_DIR", str(quotas))
    monkeypatch.setenv("OES_PRECOMPUTED_REVIEW_DIR", str(summaries))

    with TestClient(app) as client:
        first = client.post("/public/fast-gcp-review", json={"run_id": "cache-first"})
        second = client.post("/public/fast-gcp-review", json={"run_id": "cache-second"})
        second_status = client.get("/public/fast-gcp-review/status?run_id=cache-second")

    assert first.status_code == 200, first.text
    assert first.json()["quota"]["client_daily_remaining"] == 0
    assert second.status_code == 200, second.text
    second_payload = second.json()
    assert second_payload["run_id"] == "cache-second"
    assert second_payload["cache"]["status"] == "served_from_recent_public_fast_review_cache"
    assert second_payload["quota"]["status"] == "cache_hit_no_live_quota_consumed"
    total_files = list(quotas.glob("*/total.json"))
    assert len(total_files) == 1
    assert json.loads(total_files[0].read_text(encoding="utf-8"))["count"] == 1
    assert second_status.status_code == 200, second_status.text
    assert second_status.json()["quota"]["status"] == "cache_hit_no_live_quota_consumed"


def test_public_fast_gcp_review_force_bypasses_recent_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_fast_gcp_review_state()
    summaries = tmp_path / "summaries"
    statuses = tmp_path / "statuses"
    quotas = tmp_path / "quotas"
    monkeypatch.setenv("OES_STORE", "sqlite")
    monkeypatch.setenv("OES_DB_PATH", str(tmp_path / "api.sqlite3"))
    monkeypatch.setenv("OES_API_WRITE_TOKEN", "secret-token")
    monkeypatch.setenv("OES_UI_PRECOMPUTED_ONLY", "1")
    monkeypatch.setenv("OES_FAST_GCP_REVIEW_PROVIDER_MODE", "local")
    monkeypatch.setenv("OES_FAST_GCP_REVIEW_SAMPLE_ROWS", "20")
    monkeypatch.setenv("OES_PUBLIC_FAST_GCP_REVIEW_CACHE_SECONDS", "3600")
    monkeypatch.setenv("OES_PUBLIC_FAST_GCP_REVIEW_DAILY_LIMIT", "2")
    monkeypatch.setenv("OES_PUBLIC_FAST_GCP_REVIEW_CLIENT_DAILY_LIMIT", "2")
    monkeypatch.delenv("OES_PUBLIC_FAST_GCP_REVIEW_ALLOW_FORCE", raising=False)
    monkeypatch.setenv("OES_FAST_GCP_REVIEW_OUTPUT_DIR", str(summaries))
    monkeypatch.setenv("OES_FAST_GCP_REVIEW_STATUS_DIR", str(statuses))
    monkeypatch.setenv("OES_FAST_GCP_REVIEW_QUOTA_DIR", str(quotas))
    monkeypatch.setenv("OES_PRECOMPUTED_REVIEW_DIR", str(summaries))

    with TestClient(app) as client:
        first = client.post("/public/fast-gcp-review", json={"run_id": "force-first"})
        second = client.post("/public/fast-gcp-review", json={"run_id": "force-second", "force": True})
        second_status = client.get("/public/fast-gcp-review/status?run_id=force-second")

    assert first.status_code == 200, first.text
    assert first.json()["quota"]["client_daily_remaining"] == 1
    assert second.status_code == 200, second.text
    second_payload = second.json()
    assert second_payload["run_id"] == "force-second"
    assert second_payload["cache"]["status"] == "live_api_result"
    assert second_payload["quota"]["status"] == "accepted"
    assert second_payload["quota"]["client_daily_remaining"] == 0
    total_files = list(quotas.glob("*/total.json"))
    assert len(total_files) == 1
    assert json.loads(total_files[0].read_text(encoding="utf-8"))["count"] == 2
    assert second_status.status_code == 200, second_status.text
    second_status_payload = second_status.json()
    assert second_status_payload["status"] == "succeeded"
    assert second_status_payload["quota"]["status"] == "accepted"


def test_public_fast_gcp_review_owner_session_bypasses_live_quota(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_fast_gcp_review_state()
    summaries = tmp_path / "summaries"
    statuses = tmp_path / "statuses"
    quotas = tmp_path / "quotas"
    monkeypatch.setenv("OES_STORE", "sqlite")
    monkeypatch.setenv("OES_DB_PATH", str(tmp_path / "api.sqlite3"))
    monkeypatch.setenv("OES_API_WRITE_TOKEN", "secret-token")
    monkeypatch.setenv("OES_UI_PRECOMPUTED_ONLY", "1")
    monkeypatch.setenv("OES_FAST_GCP_REVIEW_PROVIDER_MODE", "local")
    monkeypatch.setenv("OES_FAST_GCP_REVIEW_SAMPLE_ROWS", "20")
    monkeypatch.setenv("OES_PUBLIC_FAST_GCP_REVIEW_CACHE_SECONDS", "0")
    monkeypatch.setenv("OES_PUBLIC_FAST_GCP_REVIEW_DAILY_LIMIT", "1")
    monkeypatch.setenv("OES_PUBLIC_FAST_GCP_REVIEW_CLIENT_DAILY_LIMIT", "1")
    monkeypatch.setenv("OES_PUBLIC_FAST_GCP_REVIEW_OWNER_TOKEN", "owner-secret")
    monkeypatch.setenv("OES_FAST_GCP_REVIEW_OUTPUT_DIR", str(summaries))
    monkeypatch.setenv("OES_FAST_GCP_REVIEW_STATUS_DIR", str(statuses))
    monkeypatch.setenv("OES_FAST_GCP_REVIEW_QUOTA_DIR", str(quotas))
    monkeypatch.setenv("OES_PRECOMPUTED_REVIEW_DIR", str(summaries))

    with TestClient(app, base_url="https://testserver") as client:
        invalid = client.post("/public/fast-gcp-review/owner-session", json={"owner_token": "wrong"})
        session = client.post("/public/fast-gcp-review/owner-session", json={"owner_token": "owner-secret"})
        first = client.post("/public/fast-gcp-review", json={"run_id": "owner-first"})
        second = client.post("/public/fast-gcp-review", json={"run_id": "owner-second"})
        second_status = client.get("/public/fast-gcp-review/status?run_id=owner-second")

    assert invalid.status_code == 403
    assert session.status_code == 200, session.text
    assert session.json()["owner_access"] is True
    assert "httponly" in session.headers["set-cookie"].lower()
    assert first.status_code == 200, first.text
    assert first.json()["quota"]["status"] == "owner_quota_bypass"
    assert second.status_code == 200, second.text
    assert second.json()["quota"]["status"] == "owner_quota_bypass"
    assert not list(quotas.glob("*/*.json"))
    assert second_status.status_code == 200, second_status.text
    assert second_status.json()["quota"]["status"] == "owner_quota_bypass"


def test_public_fixed_rescore_runs_without_write_token_or_model_api(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OES_STORE", "sqlite")
    monkeypatch.setenv("OES_DB_PATH", str(tmp_path / "api.sqlite3"))
    monkeypatch.setenv("OES_API_WRITE_TOKEN", "secret-token")
    monkeypatch.setenv("OES_UI_PRECOMPUTED_ONLY", "1")

    with TestClient(app) as client:
        page = client.get("/ui/rescore-demo?id=amazon-notify-more-data-rescore")
        result = client.post(
            "/public/rescore-demo/run",
            json={"demo_id": "amazon-notify-more-data-rescore", "run_id": "fixed-rescore-test"},
        )
        blocked = client.post("/review/graph/refresh", json={"evidence_sha256": "parent-sha"})

    assert page.status_code == 200, page.text
    assert "Run Fixed Rescore" in page.text
    assert "/public/rescore-demo/run" in page.text
    assert result.status_code == 200, result.text
    payload = result.json()
    assert payload["schema_version"] == "public_fixed_rescore_result.v1"
    assert payload["run_id"] == "fixed-rescore-test"
    assert payload["mode"] == "fixed_sanitized_child_bundle_no_model_api"
    assert payload["model_api_called"] is False
    assert payload["arbitrary_input_accepted"] is False
    assert payload["before"]["primary_count"] == 0
    assert payload["before"]["validation_count"] == 1
    assert payload["child"]["relationship"] == "more_data_child"
    assert payload["child"]["added_log_count"] == 2
    assert payload["after"]["primary_count"] == 1
    assert payload["transition"]["status"] == "needs_more_data -> evidence_collected"
    assert payload["transition"]["primary_count_delta"] == 1
    assert payload["pipeline"]["steps"][-4:] == [
        "more_data_requested",
        "child_bundle_created",
        "graph_rescored",
        "completed",
    ]
    assert blocked.status_code == 403


def test_public_live_rescore_requires_owner_and_runs_owner_model_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_fast_gcp_review_state()
    monkeypatch.setenv("OES_STORE", "sqlite")
    monkeypatch.setenv("OES_DB_PATH", str(tmp_path / "api.sqlite3"))
    monkeypatch.setenv("OES_API_WRITE_TOKEN", "secret-token")
    monkeypatch.setenv("OES_UI_PRECOMPUTED_ONLY", "1")
    monkeypatch.setenv("OES_FAST_GCP_REVIEW_PROVIDER_MODE", "local")
    monkeypatch.setenv("OES_PUBLIC_FAST_GCP_REVIEW_OWNER_TOKEN", "owner-secret")

    with TestClient(app, base_url="https://testserver") as client:
        owner_before = client.get("/public/fast-gcp-review/owner-session")
        blocked = client.post(
            "/public/rescore-demo/run",
            json={"demo_id": "amazon-notify-more-data-rescore", "run_id": "live-rescore-blocked", "live_model": True},
        )
        session = client.post("/public/fast-gcp-review/owner-session", json={"owner_token": "owner-secret"})
        owner_after = client.get("/public/fast-gcp-review/owner-session")
        result = client.post(
            "/public/rescore-demo/run",
            json={"demo_id": "amazon-notify-more-data-rescore", "run_id": "live-rescore-owner", "live_model": True},
        )

    assert owner_before.status_code == 200
    assert owner_before.json()["owner_access"] is False
    assert blocked.status_code == 403, blocked.text
    assert blocked.json()["detail"]["reason_code"] == "owner_access_required"
    assert session.status_code == 200, session.text
    assert owner_after.status_code == 200
    assert owner_after.json()["owner_access"] is True
    assert result.status_code == 200, result.text
    payload = result.json()
    assert payload["run_id"] == "live-rescore-owner"
    assert payload["mode"] == "live_model_rescore_owner_only"
    assert payload["owner_access_required"] is True
    assert payload["model_api_called"] is False
    assert payload["providers"]["requested"] == ["local-gemini"]
    assert payload["providers"]["success"] >= 1
    assert payload["providers"]["total"] >= 1
    assert payload["child"]["relationship"] == "more_data_child"
    assert payload["transition"]["status"] == "needs_more_data -> evidence_collected"


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
