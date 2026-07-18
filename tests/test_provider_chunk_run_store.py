from __future__ import annotations

import pytest

from ops_evidence_synthesis.storage.provider_chunk_runs import (
    POSTGRES_PROVIDER_CHUNK_ATTEMPTS_INSERT,
    POSTGRES_PROVIDER_CHUNK_RUNS_SCHEMA,
    POSTGRES_PROVIDER_CHUNK_RUNS_CLAIM_RETRYABLE,
    POSTGRES_PROVIDER_CHUNK_RUNS_UPSERT,
    PostgresProviderChunkRunStore,
    _postgres_dsn_from_env,
    _postgres_record_values,
    build_provider_chunk_run_store_from_env,
)
from ops_evidence_synthesis.synthesis.multi_ai import _provider_execution_contract_sha256


class FakeJsonb:
    def __init__(self, value):
        self.value = value


def test_chunk_run_store_defaults_to_jsonl_only(monkeypatch) -> None:
    monkeypatch.delenv("OES_CHUNK_RUN_STORE", raising=False)
    monkeypatch.delenv("OES_PROVIDER_CHUNK_RUN_STORE", raising=False)
    monkeypatch.delenv("OES_CHUNK_RUN_POSTGRES_DSN", raising=False)
    monkeypatch.delenv("OES_POSTGRES_DSN", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)

    assert build_provider_chunk_run_store_from_env() is None


def test_postgres_chunk_run_store_requires_dsn(monkeypatch) -> None:
    monkeypatch.setenv("OES_CHUNK_RUN_STORE", "postgres")
    monkeypatch.delenv("OES_CHUNK_RUN_POSTGRES_DSN", raising=False)
    monkeypatch.delenv("OES_POSTGRES_DSN", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)

    with pytest.raises(RuntimeError, match="no DSN"):
        build_provider_chunk_run_store_from_env()


def test_postgres_chunk_run_store_uses_configured_dsn(monkeypatch) -> None:
    monkeypatch.setenv("OES_CHUNK_RUN_STORE", "postgres")
    monkeypatch.setenv("OES_CHUNK_RUN_POSTGRES_DSN", "postgresql://user:pass@localhost:5432/oes")

    store = build_provider_chunk_run_store_from_env()

    assert isinstance(store, PostgresProviderChunkRunStore)
    assert store.dsn == "postgresql://user:pass@localhost:5432/oes"


def test_postgres_dsn_can_use_cloud_sql_socket_env(monkeypatch) -> None:
    monkeypatch.delenv("OES_CHUNK_RUN_POSTGRES_DSN", raising=False)
    monkeypatch.delenv("OES_POSTGRES_DSN", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("OES_CLOUD_SQL_CONNECTION_NAME", "project:asia-northeast1:oes-postgres")
    monkeypatch.setenv("OES_POSTGRES_DB", "ops_evidence")
    monkeypatch.setenv("OES_POSTGRES_USER", "oes_user")
    monkeypatch.setenv("OES_POSTGRES_PASSWORD", "secret value")

    dsn = _postgres_dsn_from_env()

    assert dsn.startswith("postgresql://oes_user:secret%20value@/ops_evidence?host=")
    assert "%2Fcloudsql%2Fproject%3Aasia-northeast1%3Aoes-postgres" in dsn


def test_postgres_record_values_keep_jsonb_payloads_and_retry_time() -> None:
    values = _postgres_record_values(
        {
            "evidence_sha256": "sha",
            "run_id": "run-1",
            "provider_id": "gemini-enterprise-agent-platform",
            "model_name": "gemini",
            "chunk_id": "chunk-001",
            "prompt_sha256": "prompt-sha",
            "prompt_cache_key": "cache-key",
            "status": "rate_limited",
            "provider_status": "failed",
            "schema_valid": False,
            "attempt_count": 2,
            "max_attempts": 3,
            "retried": True,
            "retryable": True,
            "last_error_type": "rate_limited",
            "last_error_message": "HTTP 429; Retry-After: 17",
            "retry_after_sec": 17,
            "artifact": {"status": "failed"},
            "parsed_payload": {"claims": []},
            "repair_rules": ["strip_code_fence_wrapper:1"],
            "semantic_keys": ["pattern:dispatcher:event"],
            "coverage_classes": ["pattern"],
        },
        jsonb=FakeJsonb,
    )

    assert values["status"] == "rate_limited"
    assert values["run_id"] == "run-1"
    assert values["execution_contract_sha256"] == "cache-key"
    assert values["attempt_no"] == 2
    assert values["next_retry_at"]
    assert values["artifact_json"].value == {"status": "failed"}
    assert values["parsed_payload_json"].value == {"claims": []}
    assert values["attempt_json"].value["status"] == "rate_limited"
    assert values["attempt_json"].value["execution_contract_sha256"] == "cache-key"
    assert values["attempt_json"].value["raw_output_sha256"] == ""
    assert values["repair_rules_json"].value == ["strip_code_fence_wrapper:1"]
    assert values["semantic_keys_json"].value == ["pattern:dispatcher:event"]


def test_postgres_schema_upsert_and_queue_are_resume_safe() -> None:
    assert "UNIQUE (provider_id, execution_contract_sha256)" in POSTGRES_PROVIDER_CHUNK_RUNS_SCHEMA
    assert "DROP CONSTRAINT IF EXISTS provider_chunk_runs_provider_id_prompt_sha256_key" in (
        POSTGRES_PROVIDER_CHUNK_RUNS_SCHEMA
    )
    assert "CREATE TABLE IF NOT EXISTS provider_chunk_attempts" in POSTGRES_PROVIDER_CHUNK_RUNS_SCHEMA
    assert "provider_chunk_attempts_lookup" in POSTGRES_PROVIDER_CHUNK_RUNS_SCHEMA
    assert "INSERT INTO provider_chunk_attempts" in POSTGRES_PROVIDER_CHUNK_ATTEMPTS_INSERT
    assert "ON CONFLICT (provider_id, execution_contract_sha256)" in POSTGRES_PROVIDER_CHUNK_RUNS_UPSERT
    assert "WHERE provider_chunk_runs.status <> 'ok'" in POSTGRES_PROVIDER_CHUNK_RUNS_UPSERT
    assert "OR EXCLUDED.status = 'ok'" in POSTGRES_PROVIDER_CHUNK_RUNS_UPSERT
    assert "FOR UPDATE SKIP LOCKED" in POSTGRES_PROVIDER_CHUNK_RUNS_CLAIM_RETRYABLE
    assert "status <> 'ok'" in POSTGRES_PROVIDER_CHUNK_RUNS_CLAIM_RETRYABLE
    assert "locked_at < now() - interval '15 minutes'" in POSTGRES_PROVIDER_CHUNK_RUNS_CLAIM_RETRYABLE


def test_provider_execution_contract_changes_when_model_generation_changes() -> None:
    prompt_sha256 = "same-normalized-prompt"

    first = _provider_execution_contract_sha256("provider-a", "model-v1", prompt_sha256)
    second = _provider_execution_contract_sha256("provider-a", "model-v2", prompt_sha256)

    assert first != second
    assert first == _provider_execution_contract_sha256("provider-a", "model-v1", prompt_sha256)


def test_postgres_retryable_claim_contract_uses_skip_locked_and_marks_single_worker_claim() -> None:
    sql = " ".join(POSTGRES_PROVIDER_CHUNK_RUNS_CLAIM_RETRYABLE.split())

    assert "WITH picked AS" in sql
    assert "FOR UPDATE SKIP LOCKED" in sql
    assert "UPDATE provider_chunk_runs AS runs" in sql
    assert "worker_id = %(worker_id)s" in sql
    assert "locked_at = now()" in sql
    assert "provider_id = %(provider_id)s" in sql
    assert "status <> 'retry_exhausted'" in sql
    assert "status <> 'safety_filter'" in sql
    assert "status <> 'context_length'" in sql
    assert "next_retry_at IS NULL OR next_retry_at <= now()" in sql
    assert "locked_at IS NULL OR locked_at < now() - interval '15 minutes'" in sql
    assert "FROM picked WHERE runs.id = picked.id" in sql
    assert "RETURNING runs.record_json" in sql
