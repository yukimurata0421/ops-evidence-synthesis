from __future__ import annotations

import os

import pytest

from ops_evidence_synthesis.storage.provider_chunk_runs import (
    POSTGRES_PROVIDER_CHUNK_RUNS_CLAIM_RETRYABLE,
    PostgresProviderChunkRunStore,
)


@pytest.mark.skipif(
    os.environ.get("OES_RUN_POSTGRES_INTEGRATION") != "1",
    reason="manual PostgreSQL integration; set OES_RUN_POSTGRES_INTEGRATION=1 and OES_TEST_POSTGRES_DSN",
)
def test_manual_postgres_claim_skip_locked_does_not_double_claim_retryable_chunks() -> None:
    dsn = os.environ.get("OES_TEST_POSTGRES_DSN", "").strip()
    assert dsn, "OES_TEST_POSTGRES_DSN is required for manual PostgreSQL integration"
    store = PostgresProviderChunkRunStore(dsn)
    store.init_schema()
    provider_id = "gemini-enterprise-agent-platform"
    records = [
        {
            "run_id": "manual-pg-integration",
            "evidence_sha256": "e" * 64,
            "provider_id": provider_id,
            "model_name": "gemini-3.1-pro-preview",
            "chunk_id": f"chunk-{index:03d}",
            "chunk_index": index,
            "chunk_count": 2,
            "prompt_sha256": f"prompt-{index}",
            "status": "provider_error",
            "provider_status": "failed",
            "schema_valid": False,
            "attempt_count": 1,
            "max_attempts": 3,
            "retryable": True,
            "last_error_type": "rate_limited",
            "last_error_message": "HTTP 429",
            "retry_after_sec": 0,
            "artifact": {"status": "provider_error"},
            "parsed_payload": {},
        }
        for index in (1, 2)
    ]
    for record in records:
        store.upsert_record(record)

    psycopg = store._psycopg()
    with psycopg.connect(dsn) as first, psycopg.connect(dsn) as second:
        first.autocommit = False
        second.autocommit = False
        with first.cursor() as first_cur:
            first_cur.execute(
                POSTGRES_PROVIDER_CHUNK_RUNS_CLAIM_RETRYABLE,
                {"provider_id": provider_id, "worker_id": "worker-a", "limit": 1},
            )
            first_claim = [dict(row[0]) for row in first_cur.fetchall()]
            assert len(first_claim) == 1
            with second.cursor() as second_cur:
                second_cur.execute(
                    POSTGRES_PROVIDER_CHUNK_RUNS_CLAIM_RETRYABLE,
                    {"provider_id": provider_id, "worker_id": "worker-b", "limit": 1},
                )
                second_claim = [dict(row[0]) for row in second_cur.fetchall()]
                assert len(second_claim) == 1
        second.commit()
        first.commit()

    claimed_chunks = {first_claim[0]["chunk_id"], second_claim[0]["chunk_id"]}
    assert claimed_chunks == {"chunk-001", "chunk-002"}
    assert store.claim_retryable_records(worker_id="worker-c", provider_id=provider_id, limit=1) == []
