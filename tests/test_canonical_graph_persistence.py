from __future__ import annotations

from fastapi.testclient import TestClient

from ops_evidence_synthesis.evidence_request_planner import build_evidence_request_plan
from ops_evidence_synthesis.storage.sqlite_store import SQLiteStore
from ops_evidence_synthesis.synthesis.review_arbitration import (
    REVIEW_ARBITRATION_VERSION,
    arbitrate_review_targets,
    build_canonical_review_graph_snapshot,
    compute_canonical_graph_sha256,
    compute_input_fingerprint,
    resolve_canonical_review_graph_snapshot,
)


def _bundle() -> dict[str, object]:
    return {
        "evidence_sha256": "sha-persist",
        "service": "stream_v3",
        "environment": "prod",
        "evidence_refs": {"METRIC-1": {"evidence_id": "METRIC-1"}},
        "signals": [{"signal_type": "http_5xx", "core_target_type": "network_error_signal"}],
        "time_window": {"start": "2026-06-15T06:00:00Z", "end": "2026-06-15T10:00:00Z"},
    }


def _profile() -> dict[str, object]:
    return {
        "profile_id": "stream-v3-approved",
        "profile_discovery_approval": {"approved": True, "explicit_profile": True},
        "review_policy": {"profile_draft_approved": True},
    }


def _model_runs() -> list[dict[str, object]]:
    return [
        {
            "run_id": "run-b",
            "provider_id": "mistral",
            "model_name": "mistral",
            "status": "ok",
            "schema_valid": True,
            "raw_output_sha256": "out-b",
        },
        {
            "run_id": "run-a",
            "provider_id": "gemini",
            "model_name": "gemini",
            "status": "ok",
            "schema_valid": True,
            "raw_output_sha256": "out-a",
        },
    ]


def _synthesis(component: str = "capture") -> dict[str, object]:
    return {
        "schema_version": "multi_ai_synthesis.v1",
        "evidence_sha256": "sha-persist",
        "provider_count": 2,
        "successful_provider_count": 2,
        "claim_groups": [{"group_id": "cg-1", "providers": ["gemini", "mistral"], "core_target_type": "general_review"}],
        "agreement_groups": [],
        "disagreement_groups": [
            {
                "group_id": "cg-1",
                "core_target_type": "general_review",
                "subsystem": component,
                "providers": ["gemini"],
                "provider_count": 1,
                "evidence_refs": ["METRIC-1"],
                "missing_evidence": ["user impact metric"],
            }
        ],
        "validation_targets": [
            {
                "group_id": "cg-1",
                "title": f"Error spike needs review: {component}",
                "core_target_type": "general_review",
                "subsystem": component,
                "providers": ["gemini"],
                "provider_count": 1,
                "evidence_refs": ["METRIC-1"],
                "missing_evidence": ["user impact metric"],
            }
        ],
        "disagreement_themes": [
            {
                "theme": "Metric/log instrumentation mismatch",
                "group_count": 1,
                "recommended_validation": "instrumentation_consistency_query",
                "group_ids": ["cg-1"],
            }
        ],
    }


def _graph(component: str = "capture") -> dict[str, object]:
    return arbitrate_review_targets(
        _bundle(),
        model_runs=_model_runs(),
        multi_ai_synthesis=_synthesis(component),
        approved_profile=_profile(),
    )


def test_input_fingerprint_is_deterministic_and_model_run_order_independent() -> None:
    one = compute_input_fingerprint(_bundle(), model_runs=_model_runs(), multi_ai_synthesis=_synthesis(), approved_profile=_profile())
    two = compute_input_fingerprint(_bundle(), model_runs=list(reversed(_model_runs())), multi_ai_synthesis=_synthesis(), approved_profile=_profile())
    assert one["input_fingerprint_sha256"] == two["input_fingerprint_sha256"]
    assert one["input_fingerprint_json"]["model_run_ids"] == ["run-a", "run-b"]
    assert one["input_fingerprint_json"]["model_output_sha256s"] == ["out-a", "out-b"]


def test_arbitration_version_changes_fingerprint() -> None:
    one = compute_input_fingerprint(_bundle(), model_runs=_model_runs(), multi_ai_synthesis=_synthesis(), arbitration_version=REVIEW_ARBITRATION_VERSION)
    two = compute_input_fingerprint(_bundle(), model_runs=_model_runs(), multi_ai_synthesis=_synthesis(), arbitration_version="review_arbitration.next")
    assert one["input_fingerprint_sha256"] != two["input_fingerprint_sha256"]


def test_canonical_graph_sha_ignores_nondeterministic_fields() -> None:
    graph = _graph()
    one = compute_canonical_graph_sha256({**graph, "created_at": "2026-01-01T00:00:00Z"})
    two = compute_canonical_graph_sha256({**graph, "created_at": "2026-01-02T00:00:00Z", "snapshot_status": "stale"})
    assert one == two


def test_snapshot_schema_is_generated() -> None:
    snapshot = build_canonical_review_graph_snapshot(_graph(), created_by="pytest")
    assert snapshot["schema_version"] == "canonical_review_graph.v1"
    assert snapshot["arbitration_version"] == REVIEW_ARBITRATION_VERSION
    assert snapshot["canonical_graph_sha256"]
    assert snapshot["input_fingerprint_sha256"]
    assert snapshot["snapshot_status"] == "persisted"
    assert isinstance(snapshot["canonical_review_graph_json"], dict)


def test_sqlite_save_get_latest_and_duplicate_avoidance(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "graphs.sqlite3")
    snapshot = build_canonical_review_graph_snapshot(_graph(), created_by="pytest")
    first = store.save_canonical_review_graph_snapshot(snapshot)
    second = store.save_canonical_review_graph_snapshot(snapshot)
    assert first["canonical_graph_sha256"] == second["canonical_graph_sha256"]
    assert len(store.list_canonical_review_graph_snapshots("sha-persist")) == 1
    latest = store.get_latest_canonical_review_graph_snapshot("sha-persist")
    assert latest is not None
    assert latest["canonical_graph_sha256"] == snapshot["canonical_graph_sha256"]


def test_resolve_status_computed_persisted_and_stale(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "status.sqlite3")
    computed = resolve_canonical_review_graph_snapshot(
        store,
        _bundle(),
        model_runs=_model_runs(),
        multi_ai_synthesis=_synthesis(),
        approved_profile=_profile(),
    )
    assert computed["canonical_graph_status"] == "computed_on_request"

    persisted = resolve_canonical_review_graph_snapshot(
        store,
        _bundle(),
        model_runs=_model_runs(),
        multi_ai_synthesis=_synthesis(),
        approved_profile=_profile(),
        persist_if_missing=True,
        created_by="pytest",
    )
    assert persisted["canonical_graph_status"] == "persisted"

    matched = resolve_canonical_review_graph_snapshot(
        store,
        _bundle(),
        model_runs=_model_runs(),
        multi_ai_synthesis=_synthesis(),
        approved_profile=_profile(),
    )
    assert matched["canonical_graph_status"] == "persisted"

    stale = resolve_canonical_review_graph_snapshot(
        store,
        _bundle(),
        model_runs=_model_runs(),
        multi_ai_synthesis=_synthesis("different_component"),
        approved_profile=_profile(),
    )
    assert stale["canonical_graph_status"] == "stale"
    assert stale["canonical_review_graph"]["stale_reason"] == "input_fingerprint_changed"
    assert stale["previous_snapshot"]["input_fingerprint_sha256"] == persisted["input_fingerprint_sha256"]


def test_resolve_refresh_rebuilds_projection_for_existing_snapshot(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "projection.sqlite3")
    persisted = resolve_canonical_review_graph_snapshot(
        store,
        _bundle(),
        model_runs=_model_runs(),
        multi_ai_synthesis=_synthesis(),
        approved_profile=_profile(),
        persist_if_missing=True,
        created_by="pytest",
    )
    assert persisted["canonical_graph_status"] == "persisted"

    store.replace_review_targets_for_evidence("sha-persist", [])
    assert store.list_review_targets(evidence_sha256="sha-persist", pending_only=False)["summary"]["review_targets"] == 0

    refreshed = resolve_canonical_review_graph_snapshot(
        store,
        _bundle(),
        model_runs=_model_runs(),
        multi_ai_synthesis=_synthesis(),
        approved_profile=_profile(),
        persist_if_missing=True,
        persist_if_stale=True,
        created_by="pytest-refresh",
    )

    assert refreshed["canonical_graph_status"] == "persisted"
    assert refreshed["projection_persistence"]["refreshed"] is True
    targets = store.list_review_targets(evidence_sha256="sha-persist", pending_only=False)
    assert targets["summary"]["review_targets"] == len(refreshed["canonical_review_graph"]["review_targets"])


def test_planner_generated_from_includes_canonical_graph() -> None:
    graph = _graph()
    plan = build_evidence_request_plan(_bundle(), _profile(), canonical_review_graph=graph)
    assert plan["canonical_review_graph_used"] is True
    assert plan["generated_from"]["canonical_graph_sha256"] == graph["canonical_graph_sha256"]
    assert plan["generated_from"]["input_fingerprint_sha256"] == graph["input_fingerprint_sha256"]


def test_review_arbitrate_api_persists_snapshot(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("OES_STORE", "sqlite")
    monkeypatch.setenv("OES_DB_PATH", str(tmp_path / "api.sqlite3"))
    from ops_evidence_synthesis.api import app

    with TestClient(app) as client:
        response = client.post(
            "/review/arbitrate",
            json={
                "evidence_bundle": _bundle(),
                "approved_profile": _profile(),
                "multi_ai_synthesis": _synthesis(),
                "model_runs": _model_runs(),
                "persist": True,
            },
        )
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["canonical_graph_status"] == "persisted"
        assert payload["canonical_graph_sha256"]
        assert payload["input_fingerprint_sha256"]

        targets = client.get(f"/review-targets?evidence_sha256={_bundle()['evidence_sha256']}")
        assert targets.status_code == 200, targets.text
        assert targets.json()["summary"]["review_targets"] == payload["canonical_review_graph"]["summary"]["validation_count"]

        summary = client.get(f"/ui/summary?evidence_sha256={_bundle()['evidence_sha256']}")
        assert summary.status_code == 200, summary.text
        assert summary.json()["canonical_graph_status"] == "persisted"
        assert summary.json()["review"]["validation_targets"] == payload["canonical_review_graph"]["summary"]["validation_count"]

        detail = client.get(f"/ui/full-review-page?evidence_sha256={_bundle()['evidence_sha256']}")
        assert detail.status_code == 200, detail.text
        assert "Ops Evidence Review" in detail.text
        assert "Review Targets" in detail.text
        assert "Error spike needs review" in detail.text


def test_review_graph_get_is_read_only_when_no_snapshot(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("OES_STORE", "sqlite")
    monkeypatch.setenv("OES_DB_PATH", str(tmp_path / "api.sqlite3"))
    from ops_evidence_synthesis.api import app

    store = SQLiteStore(tmp_path / "api.sqlite3")
    store.insert_bundle(_bundle())

    with TestClient(app) as client:
        response = client.get(f"/review/graph?evidence_sha256={_bundle()['evidence_sha256']}")
        assert response.status_code == 200, response.text
        assert response.json()["canonical_graph_status"] == "not_found"

    assert store.list_canonical_review_graph_snapshots("sha-persist") == []


def test_full_ui_does_not_create_additional_snapshot(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("OES_STORE", "sqlite")
    monkeypatch.setenv("OES_DB_PATH", str(tmp_path / "api.sqlite3"))
    from ops_evidence_synthesis.api import app

    store = SQLiteStore(tmp_path / "api.sqlite3")
    snapshot = build_canonical_review_graph_snapshot(_graph(), created_by="pytest")
    store.insert_bundle(_bundle())
    store.save_canonical_review_graph_snapshot(snapshot)
    before = len(store.list_canonical_review_graph_snapshots("sha-persist"))

    with TestClient(app) as client:
        html = client.get(f"/?evidence_sha256={_bundle()['evidence_sha256']}&full=1")
        assert html.status_code == 200, html.text
        assert "Canonical graph loaded from persisted arbitration snapshot" in html.text

    after = len(store.list_canonical_review_graph_snapshots("sha-persist"))
    assert after == before


def test_review_graph_refresh_post_persists_snapshot_and_projection(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("OES_STORE", "sqlite")
    monkeypatch.setenv("OES_DB_PATH", str(tmp_path / "api.sqlite3"))
    from ops_evidence_synthesis.api import app

    store = SQLiteStore(tmp_path / "api.sqlite3")
    store.insert_bundle(_bundle())

    with TestClient(app) as client:
        response = client.post(
            "/review/graph/refresh",
            json={
                "evidence_sha256": _bundle()["evidence_sha256"],
                "created_by": "pytest-refresh",
            },
        )
        assert response.status_code == 200, response.text
        assert response.json()["canonical_graph_status"] == "persisted"

        targets = client.get(f"/review-targets?evidence_sha256={_bundle()['evidence_sha256']}")
        assert targets.status_code == 200, targets.text
        assert targets.json()["summary"]["review_targets"] == len(response.json()["canonical_review_graph"]["review_targets"])

        detail = client.get(f"/ui/full-review-page?evidence_sha256={_bundle()['evidence_sha256']}")
        assert detail.status_code == 200, detail.text
        assert "Ops Evidence Review" in detail.text
        assert "Review Targets" in detail.text
        assert "Back to summary" in detail.text


def test_multi_run_api_and_ui_include_graph_status(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("OES_STORE", "sqlite")
    monkeypatch.setenv("OES_DB_PATH", str(tmp_path / "api.sqlite3"))
    from ops_evidence_synthesis.api import app

    with TestClient(app) as client:
        response = client.post(
            "/ai/multi-run",
            json={
                "evidence_bundle": _bundle(),
                "approved_profile": _profile(),
                "providers": ["local-gemini", "local-gpt-oss", "local-mistral"],
                "mode": "local",
            },
        )
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["canonical_graph_status"] == "persisted"
        assert payload["canonical_graph_sha256"]
        assert payload["input_fingerprint_sha256"]

        graph_response = client.get(f"/review/graph?evidence_sha256={_bundle()['evidence_sha256']}")
        assert graph_response.status_code == 200, graph_response.text
        assert graph_response.json()["canonical_graph_status"] in {"persisted", "stale", "computed_on_request"}

        html = client.get(f"/?evidence_sha256={_bundle()['evidence_sha256']}&full=1").text
        assert "Canonical graph SHA" in html
        assert "Input fingerprint" in html
        assert "Status:" in html
