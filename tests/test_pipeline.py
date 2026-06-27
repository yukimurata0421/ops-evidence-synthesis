from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ops_evidence_synthesis.ai.base import ModelResponse
from ops_evidence_synthesis.ingest import ingest_jsonl
from ops_evidence_synthesis.models import IncidentWindow, ModelRunRecord
from ops_evidence_synthesis.storage.sqlite_store import SQLiteStore
from ops_evidence_synthesis.synthesis.pipeline import run_model_stage, run_pipeline


ROOT = Path(__file__).resolve().parents[1]


def test_pipeline_runs_end_to_end(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "oes.sqlite3")
    assert ingest_jsonl(ROOT / "data/sample_logs.jsonl", store) == 20

    result = run_pipeline(
        store,
        IncidentWindow(
            service="payment-api",
            environment="prod",
            incident_start="2026-06-12T10:00:00Z",
            incident_end="2026-06-12T10:20:00Z",
            lookback_minutes=45,
        ),
    )

    assert len(result.evidence_sha256) == 64
    assert result.model_run_count == 3
    assert result.parsed_result_count == 3
    assert result.claim_count >= 4
    assert result.proposition_count >= 2
    assert result.score_count == result.proposition_count
    assert result.cluster_count >= 1
    assert 0 < result.review_queue_count <= result.proposition_count
    assert store.count_table("model_runs") == 3
    assert store.count_table("parsed_results") == 3
    assert store.count_table("claims") == result.claim_count
    assert store.count_table("scores") == result.score_count
    assert store.count_table("proposition_clusters") == result.cluster_count

    queue = store.list_review_queue()
    assert queue
    assert queue[0]["review_priority_score"] >= 0
    all_proposals = store.list_proposals(
        evidence_sha256=result.evidence_sha256,
        pending_only=False,
        include_hidden=True,
    )
    assert any("connection pool" in item["question"] for item in all_proposals)

    empty_result = run_pipeline(
        store,
        IncidentWindow(
            service="payment-api",
            environment="prod",
            incident_start="2026-06-12T11:00:00Z",
            incident_end="2026-06-12T11:10:00Z",
            lookback_minutes=10,
        ),
        providers=[RaisingProvider()],
    )

    assert empty_result.proposition_count == 0
    assert empty_result.review_queue_count == 0
    assert store.list_review_queue(evidence_sha256=empty_result.evidence_sha256) == []


@dataclass(frozen=True, slots=True)
class RaisingProvider:
    provider: str = "failing-ai"
    model_name: str = "failing-model"
    prompt_name: str = "root-cause"
    temperature: float = 0.0

    def run(self, bundle: dict[str, Any]) -> ModelResponse:
        raise RuntimeError("quota exhausted")


def test_pipeline_records_provider_errors_and_continues(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("OES_MODEL_RETRY_BASE_SECONDS", "0")
    store = SQLiteStore(tmp_path / "oes.sqlite3")
    assert ingest_jsonl(ROOT / "data/sample_logs.jsonl", store) == 20

    result = run_pipeline(
        store,
        IncidentWindow(
            service="payment-api",
            environment="prod",
            incident_start="2026-06-12T10:00:00Z",
            incident_end="2026-06-12T10:20:00Z",
            lookback_minutes=45,
        ),
        providers=[RaisingProvider()],
    )
    parsed = store.fetch_parsed_results(result.evidence_sha256)

    assert result.model_run_count == 1
    assert result.parsed_result_count == 1
    assert result.claim_count > 0
    assert result.cluster_count > 0
    assert parsed[0].schema_valid is False
    assert parsed[0].parsed_json["error_type"] == "provider_error"
    model_run = store.fetch_model_runs(result.evidence_sha256)[0]
    assert model_run.status == "failed"
    assert json.loads(model_run.raw_output)["retry_attempts"] == 2
    assert all(claim.provider == "rule-engine" for claim in store.fetch_claims(result.evidence_sha256))


@dataclass(frozen=True, slots=True)
class SlowProvider:
    provider: str
    model_name: str
    prompt_name: str = "root-cause"
    temperature: float = 0.0
    barrier: threading.Barrier | None = None
    events: list[tuple[str, str]] | None = None
    lock: Any | None = None

    def run(self, bundle: dict[str, Any]) -> ModelResponse:
        self._record("entered")
        if self.barrier is not None:
            self.barrier.wait(timeout=1.0)
        self._record("released")
        return ModelResponse(
            provider=self.provider,
            model_name=self.model_name,
            prompt_name=self.prompt_name,
            temperature=self.temperature,
            raw_output=json.dumps(
                {
                    "schema_version": "claim-result/v1",
                    "agent_role": "hypothesis_generator",
                    "finding_status": "no_finding",
                    "summary": "no finding",
                    "claims": [],
                    "propositions": [],
                }
            ),
            latency_ms=1,
            input_tokens=1,
            output_tokens=1,
        )

    def _record(self, action: str) -> None:
        if self.events is None:
            return
        if self.lock is None:
            self.events.append((self.provider, action))
            return
        with self.lock:
            self.events.append((self.provider, action))


def test_model_stage_runs_providers_in_parallel_and_writes_sequentially(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "oes.sqlite3")
    store.init_schema()
    bundle = {
        "evidence_sha256": "e" * 64,
        "evidence_refs": {},
    }
    barrier = threading.Barrier(2)
    events: list[tuple[str, str]] = []
    lock = threading.Lock()
    providers = [
        SlowProvider("slow-a", "model-a", barrier=barrier, events=events, lock=lock),
        SlowProvider("slow-b", "model-b", barrier=barrier, events=events, lock=lock),
    ]

    parsed = run_model_stage(store, bundle, providers)

    actions = [action for _, action in events]
    assert actions.count("entered") == 2
    assert actions.count("released") == 2
    assert max(index for index, action in enumerate(actions) if action == "entered") < min(
        index for index, action in enumerate(actions) if action == "released"
    )
    assert [result.provider for result in parsed] == ["slow-a", "slow-b"]
    model_runs = store.fetch_model_runs(bundle["evidence_sha256"])
    parsed_rows = store.fetch_parsed_results(bundle["evidence_sha256"])
    assert sorted(run.provider for run in model_runs) == ["slow-a", "slow-b"]
    assert {run.provider: run.status for run in model_runs} == {"slow-a": "ok", "slow-b": "ok"}
    assert sorted(result.provider for result in parsed_rows) == ["slow-a", "slow-b"]


@dataclass(frozen=True, slots=True)
class MustNotRunProvider:
    provider: str = "unsafe-ai"
    model_name: str = "unsafe-model"
    prompt_name: str = "root-cause"
    temperature: float = 0.0

    def run(self, bundle: dict[str, Any]) -> ModelResponse:
        raise AssertionError("provider should not be called after safety preflight failure")


def test_model_stage_blocks_unsafe_legacy_model_input(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "oes.sqlite3")
    store.init_schema()
    bundle = {
        "evidence_sha256": "f" * 64,
        "service": "demo",
        "environment": "prod",
        "evidence_items": [
            {
                "evidence_id": "LEAK-1",
                "summary": "Authorization: Bearer raw-token-1234567890",
            }
        ],
        "evidence_refs": {
            "LEAK-1": {
                "type": "log_pattern",
                "summary": "Authorization: Bearer raw-token-1234567890",
            }
        },
    }

    parsed = run_model_stage(store, bundle, [MustNotRunProvider()])
    run = store.fetch_model_runs(bundle["evidence_sha256"])[0]

    assert parsed[0].schema_valid is False
    assert run.status == "blocked_by_safety_preflight"
    assert parsed[0].parsed_json["error_type"] == "safety_preflight_blocked"


def test_fetch_model_runs_uses_latest_bundle_generation(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "oes.sqlite3")
    store.init_schema()
    bundle = {
        "schema_version": "test.v1",
        "evidence_sha256": "a" * 64,
        "service": "demo",
        "environment": "prod",
        "window_start": "2026-06-19T00:00:00Z",
        "window_end": "2026-06-19T01:00:00Z",
        "created_at": "2026-06-19T00:00:00Z",
    }
    store.insert_bundle(bundle)
    store.insert_model_run(
        ModelRunRecord(
            run_id="run-old",
            evidence_sha256=bundle["evidence_sha256"],
            prompt_sha256="p",
            model_input_sha256="m",
            provider="provider-a",
            model_name="model-a",
            temperature=0.0,
            raw_output="{}",
            raw_output_sha256="r",
            latency_ms=1,
            input_tokens=1,
            output_tokens=1,
            status="ok",
            created_at="2026-06-19T00:10:00Z",
        )
    )

    assert [run.run_id for run in store.fetch_model_runs(bundle["evidence_sha256"])] == ["run-old"]

    newer = dict(bundle)
    newer["created_at"] = "2026-06-19T00:20:00Z"
    store.insert_bundle(newer)

    assert store.fetch_model_runs(bundle["evidence_sha256"]) == []
