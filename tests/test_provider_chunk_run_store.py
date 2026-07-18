from __future__ import annotations

from dataclasses import dataclass

import pytest

from ops_evidence_synthesis.ai.execution_contract import (
    build_provider_execution_contract,
    execution_contract_allows_cross_run_reuse,
    is_mutable_model_alias,
    provider_execution_contract_sha256,
)
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


class FakeJsonb:
    def __init__(self, value):
        self.value = value


@dataclass(frozen=True, slots=True)
class ContractProvider:
    provider: str = "provider-a"
    model_name: str = "model-v1"
    prompt_name: str = "alternative-hypothesis"
    temperature: float = 0.0
    max_output_tokens: int = 4096
    max_evidence_items: int = 140
    max_logs: int = 0
    max_normalized_events: int = 0
    max_text_chars: int = 480
    timeout_seconds: int = 60
    adapter_version: str = "adapter.v1"
    mutable_model_alias: bool | None = None
    resolved_model_revision: str = ""
    tool_contract_version: str = "none"
    response_schema_version: str = "claim-result/v1"
    prompt_renderer_version: str = "multi_ai_claim_prompt.v1"
    safety_policy_version: str = "multi_ai_safety_preflight.v1"
    generation_policy_version: str = "provider_generation_policy.v1"
    cache_reuse_policy: str = ""


def _contract_bundle() -> dict:
    item = {
        "evidence_id": "EVIDENCE-001",
        "type": "log_pattern",
        "message_template": "worker restarted",
        "count": 2,
    }
    return {
        "schema_version": "evidence_bundle.v1",
        "evidence_sha256": "bundle-sha",
        "service": "worker",
        "environment": "prod",
        "evidence_items": [item],
        "evidence_refs": {"EVIDENCE-001": item},
    }


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
    assert values["execution_contract_version"] == "provider_execution_contract.v1"
    assert values["execution_contract_json"].value == {}
    assert values["attempt_no"] == 2
    assert values["next_retry_at"]
    assert values["artifact_json"].value == {"status": "failed"}
    assert values["parsed_payload_json"].value == {"claims": []}
    assert values["attempt_json"].value["status"] == "rate_limited"
    assert values["attempt_json"].value["execution_contract_sha256"] == "cache-key"
    assert values["attempt_json"].value["raw_output_sha256"] == ""
    assert values["repair_rules_json"].value == ["strip_code_fence_wrapper:1"]
    assert values["semantic_keys_json"].value == ["pattern:dispatcher:event"]


def test_postgres_record_values_persist_v2_execution_audit_fields() -> None:
    contract = build_provider_execution_contract(ContractProvider(), _contract_bundle())
    contract_sha256 = provider_execution_contract_sha256(contract)

    values = _postgres_record_values(
        {
            "provider_id": "provider-a",
            "model_name": "model-v1",
            "execution_contract_sha256": contract_sha256,
            "execution_contract_version": contract["schema_version"],
            "execution_contract": contract,
            "model_input_sha256": contract["input"]["model_input_sha256"],
            "prompt_contract_sha256": contract["prompt_contract"]["prompt_contract_sha256"],
            "requested_model_name": "model-v1",
            "resolved_model_name": "model-v1-20260718",
            "resolved_model_revision": "20260718",
            "provider_response_model_id": "provider/model-v1@20260718",
            "mutable_model_alias": False,
            "cache_reuse_policy": "allowed",
            "status": "ok",
        },
        jsonb=FakeJsonb,
    )

    assert values["execution_contract_version"] == "provider_execution_contract.v2"
    assert values["execution_contract_json"].value == contract
    assert values["model_input_sha256"] == contract["input"]["model_input_sha256"]
    assert values["prompt_contract_sha256"] == contract["prompt_contract"]["prompt_contract_sha256"]
    assert values["provider_response_model_id"] == "provider/model-v1@20260718"
    assert values["cache_reuse_policy"] == "allowed"


def test_postgres_schema_upsert_and_queue_are_resume_safe() -> None:
    assert "UNIQUE (provider_id, execution_contract_sha256)" in POSTGRES_PROVIDER_CHUNK_RUNS_SCHEMA
    assert "DROP CONSTRAINT IF EXISTS provider_chunk_runs_provider_id_prompt_sha256_key" in (
        POSTGRES_PROVIDER_CHUNK_RUNS_SCHEMA
    )
    assert "CREATE TABLE IF NOT EXISTS provider_chunk_attempts" in POSTGRES_PROVIDER_CHUNK_RUNS_SCHEMA
    assert "provider_chunk_attempts_lookup" in POSTGRES_PROVIDER_CHUNK_RUNS_SCHEMA
    assert "INSERT INTO provider_chunk_attempts" in POSTGRES_PROVIDER_CHUNK_ATTEMPTS_INSERT
    assert "ON CONFLICT (provider_id, execution_contract_sha256)" in POSTGRES_PROVIDER_CHUNK_RUNS_UPSERT
    assert "execution_contract_version" in POSTGRES_PROVIDER_CHUNK_RUNS_SCHEMA
    assert "execution_contract_json jsonb" in POSTGRES_PROVIDER_CHUNK_RUNS_SCHEMA
    assert "model_input_sha256" in POSTGRES_PROVIDER_CHUNK_RUNS_SCHEMA
    assert "prompt_contract_sha256" in POSTGRES_PROVIDER_CHUNK_RUNS_SCHEMA
    assert "provider_response_model_id" in POSTGRES_PROVIDER_CHUNK_RUNS_SCHEMA
    assert "cache_reuse_policy" in POSTGRES_PROVIDER_CHUNK_RUNS_SCHEMA
    assert "WHERE provider_chunk_runs.status <> 'ok'" in POSTGRES_PROVIDER_CHUNK_RUNS_UPSERT
    assert "OR EXCLUDED.status = 'ok'" in POSTGRES_PROVIDER_CHUNK_RUNS_UPSERT
    assert "FOR UPDATE SKIP LOCKED" in POSTGRES_PROVIDER_CHUNK_RUNS_CLAIM_RETRYABLE
    assert "status <> 'ok'" in POSTGRES_PROVIDER_CHUNK_RUNS_CLAIM_RETRYABLE
    assert "locked_at < now() - interval '15 minutes'" in POSTGRES_PROVIDER_CHUNK_RUNS_CLAIM_RETRYABLE


def test_provider_execution_contract_changes_for_output_affecting_settings() -> None:
    bundle = _contract_bundle()
    baseline = build_provider_execution_contract(ContractProvider(), bundle)

    variants = [
        ContractProvider(model_name="model-v2"),
        ContractProvider(temperature=0.2),
        ContractProvider(max_output_tokens=8192),
        ContractProvider(max_text_chars=240),
        ContractProvider(adapter_version="adapter.v2"),
        ContractProvider(tool_contract_version="read-only-tools.v1"),
        ContractProvider(response_schema_version="claim-result/v2"),
        ContractProvider(prompt_renderer_version="multi_ai_claim_prompt.v2"),
        ContractProvider(safety_policy_version="multi_ai_safety_preflight.v2"),
        ContractProvider(generation_policy_version="provider_generation_policy.v2"),
    ]

    baseline_hash = provider_execution_contract_sha256(baseline)
    assert all(
        provider_execution_contract_sha256(build_provider_execution_contract(provider, bundle))
        != baseline_hash
        for provider in variants
    )
    assert baseline_hash == provider_execution_contract_sha256(
        build_provider_execution_contract(ContractProvider(), bundle)
    )
    changed_input = _contract_bundle()
    changed_input["evidence_refs"]["EVIDENCE-001"]["message_template"] = "worker stopped"
    assert baseline_hash != provider_execution_contract_sha256(
        build_provider_execution_contract(ContractProvider(), changed_input)
    )


def test_provider_execution_contract_ignores_operational_retry_settings() -> None:
    bundle = _contract_bundle()
    baseline = build_provider_execution_contract(ContractProvider(timeout_seconds=60), bundle)
    different_timeout = build_provider_execution_contract(ContractProvider(timeout_seconds=600), bundle)

    assert provider_execution_contract_sha256(baseline) == provider_execution_contract_sha256(
        different_timeout
    )


def test_mutable_model_alias_disables_cross_run_reuse_until_revision_is_resolved() -> None:
    bundle = _contract_bundle()
    unresolved = build_provider_execution_contract(
        ContractProvider(model_name="model-latest", mutable_model_alias=None),
        bundle,
    )
    resolved = build_provider_execution_contract(
        ContractProvider(
            model_name="model-latest",
            mutable_model_alias=None,
            resolved_model_revision="2026-07-18",
        ),
        bundle,
    )

    assert execution_contract_allows_cross_run_reuse(unresolved) is False
    assert execution_contract_allows_cross_run_reuse(resolved) is True


@pytest.mark.parametrize(
    "model_name",
    [
        "gemini-3.1-pro-preview",
        "model-experimental",
        "model-exp",
        "model-beta",
        "gemini-3.1-flash-lite",
        "model-latest",
        "model-default",
    ],
)
def test_mutable_model_alias_detection_covers_provider_alias_families(model_name: str) -> None:
    assert is_mutable_model_alias(model_name) is True


def test_cross_run_reuse_is_default_deny_without_revision_or_audited_policy() -> None:
    bundle = _contract_bundle()
    unresolved = build_provider_execution_contract(
        ContractProvider(model_name="mistral-small-2503"),
        bundle,
    )
    explicitly_allowed = build_provider_execution_contract(
        ContractProvider(model_name="mistral-small-2503", cache_reuse_policy="allowed"),
        bundle,
    )

    assert execution_contract_allows_cross_run_reuse(unresolved) is False
    assert unresolved["reuse_policy"]["reason"] == "model_revision_or_audited_policy_required"
    assert execution_contract_allows_cross_run_reuse(explicitly_allowed) is True
    assert explicitly_allowed["reuse_policy"]["reason"] == "explicit_provider_policy"


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
