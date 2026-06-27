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
PUBLIC_DEMO_SHA = "1be4a21441fec7d2a4eafa95508badbe4a892bd61f3d9e08541893fba97c6731"


def test_public_precomputed_review_fixture_is_regenerated_from_pipeline(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "public-demo.sqlite3")
    store.init_schema()
    ingest_jsonl("data/sample_logs.jsonl", store)

    result = run_pipeline(
        store,
        IncidentWindow(
            service="payment-api",
            environment="prod",
            incident_start="2026-06-12T10:00:00Z",
            incident_end="2026-06-12T10:20:00Z",
            lookback_minutes=45,
        ),
        providers=build_multi_ai_providers(PUBLIC_DEMO_PROVIDERS, mode="local"),
    )
    payload = build_precomputed_review_summary(
        store,
        result.evidence_sha256,
        updated_at="2026-06-12T10:20:00Z",
        source_note="generated from public sample fixture with deterministic local providers",
    )

    assert result.evidence_sha256 == PUBLIC_DEMO_SHA
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
        / f"{PUBLIC_DEMO_SHA}.json"
    ).read_text(encoding="utf-8")
    assert stable_precomputed_review_json(payload) == expected
