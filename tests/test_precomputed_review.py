from __future__ import annotations

from pathlib import Path

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


ROOT = Path(__file__).resolve().parents[1]
PUBLIC_SAMPLE_SHA = "1be4a21441fec7d2a4eafa95508badbe4a892bd61f3d9e08541893fba97c6731"
PUBLIC_FLAGSHIP_SHA = "c43cb9ccb916abdb73e71e05b4f643f6419eb74de6324094be25400557f6ed1e"


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
        source_note="generated from public sample fixture with deterministic local providers",
    )

    assert result.evidence_sha256 == PUBLIC_SAMPLE_SHA
    assert payload["summary"]["log_count"] == 20
    assert payload["summary"]["providers"] == {
        "success": 3,
        "total": 4,
        "pipeline_status": "completed",
    }
    assert payload["summary"]["review"]["primary_targets"] == 0
    assert payload["summary"]["review"]["validation_targets"] == 5
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
        source_note="generated from committed public-safe amazon-notify fixture with deterministic local providers",
    )

    assert result.evidence_sha256 == PUBLIC_FLAGSHIP_SHA
    assert payload["summary"]["log_count"] == 6506
    assert payload["summary"]["providers"] == {
        "success": 3,
        "total": 4,
        "pipeline_status": "completed",
    }
    assert payload["summary"]["review"]["primary_targets"] == 0
    assert payload["summary"]["review"]["validation_targets"] == 1
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
    )
    return result, payload
