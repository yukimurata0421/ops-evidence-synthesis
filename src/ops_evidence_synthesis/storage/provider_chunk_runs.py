from __future__ import annotations

import importlib
import os
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol
from urllib.parse import quote


class ProviderChunkRunStore(Protocol):
    def init_schema(self) -> None:
        ...

    def success_record(self, provider_id: str, prompt_sha256: str) -> dict[str, Any] | None:
        ...

    def latest_record(self, provider_id: str, prompt_sha256: str) -> dict[str, Any] | None:
        ...

    def upsert_record(self, record: dict[str, Any]) -> None:
        ...

    def claim_retryable_records(
        self,
        *,
        worker_id: str,
        provider_id: str = "",
        limit: int = 1,
    ) -> list[dict[str, Any]]:
        ...


def build_provider_chunk_run_store_from_env() -> ProviderChunkRunStore | None:
    store = (
        os.environ.get("OES_CHUNK_RUN_STORE")
        or os.environ.get("OES_PROVIDER_CHUNK_RUN_STORE")
        or ""
    ).strip().casefold()
    dsn = _postgres_dsn_from_env()
    if store in {"", "jsonl", "file", "disabled", "none"}:
        return None
    if store == "auto" and not dsn:
        return None
    if store in {"postgres", "postgresql", "pg", "auto"}:
        if not dsn:
            raise RuntimeError(
                "PostgreSQL chunk run store requested but no DSN was configured. "
                "Set OES_CHUNK_RUN_POSTGRES_DSN, OES_POSTGRES_DSN, or DATABASE_URL."
            )
        return PostgresProviderChunkRunStore(dsn)
    raise RuntimeError(f"unsupported OES_CHUNK_RUN_STORE: {store}")


def _postgres_dsn_from_env() -> str:
    explicit = (
        os.environ.get("OES_CHUNK_RUN_POSTGRES_DSN")
        or os.environ.get("OES_POSTGRES_DSN")
        or os.environ.get("DATABASE_URL")
        or ""
    ).strip()
    if explicit:
        return explicit

    connection_name = os.environ.get("OES_CLOUD_SQL_CONNECTION_NAME", "").strip()
    database = os.environ.get("OES_POSTGRES_DB", "").strip()
    user = os.environ.get("OES_POSTGRES_USER", "").strip()
    password = _postgres_password_from_env()
    if connection_name and database and user:
        socket_dir = os.environ.get("OES_CLOUD_SQL_SOCKET_DIR", "/cloudsql").rstrip("/")
        host = quote(f"{socket_dir}/{connection_name}", safe="")
        password_part = f":{quote(password, safe='')}" if password else ""
        return f"postgresql://{quote(user, safe='')}{password_part}@/{quote(database, safe='')}?host={host}"
    return ""


def _postgres_password_from_env() -> str:
    value = os.environ.get("OES_POSTGRES_PASSWORD", "")
    if value:
        return value
    password_file = os.environ.get("OES_POSTGRES_PASSWORD_FILE", "").strip()
    if not password_file:
        return ""
    try:
        return open(password_file, encoding="utf-8").read().strip()
    except OSError:
        return ""


class PostgresProviderChunkRunStore:
    def __init__(self, dsn: str) -> None:
        self.dsn = dsn

    def init_schema(self) -> None:
        psycopg = self._psycopg()
        with psycopg.connect(self.dsn) as conn:
            with conn.cursor() as cur:
                cur.execute(POSTGRES_PROVIDER_CHUNK_RUNS_SCHEMA)
            conn.commit()

    def success_record(self, provider_id: str, prompt_sha256: str) -> dict[str, Any] | None:
        psycopg = self._psycopg()
        with psycopg.connect(self.dsn) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT record_json
                    FROM provider_chunk_runs
                    WHERE provider_id = %s
                      AND prompt_sha256 = %s
                      AND status = 'ok'
                      AND schema_valid IS TRUE
                    LIMIT 1
                    """,
                    (provider_id, prompt_sha256),
                )
                row = cur.fetchone()
        if not row:
            return None
        record = row[0]
        return dict(record) if isinstance(record, dict) else None

    def latest_record(self, provider_id: str, prompt_sha256: str) -> dict[str, Any] | None:
        psycopg = self._psycopg()
        with psycopg.connect(self.dsn) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT record_json
                    FROM provider_chunk_runs
                    WHERE provider_id = %s
                      AND prompt_sha256 = %s
                    LIMIT 1
                    """,
                    (provider_id, prompt_sha256),
                )
                row = cur.fetchone()
        if not row:
            return None
        record = row[0]
        return dict(record) if isinstance(record, dict) else None

    def upsert_record(self, record: dict[str, Any]) -> None:
        psycopg = self._psycopg()
        jsonb = self._jsonb_adapter()
        values = _postgres_record_values(record, jsonb=jsonb)
        with psycopg.connect(self.dsn) as conn:
            with conn.cursor() as cur:
                cur.execute(POSTGRES_PROVIDER_CHUNK_RUNS_UPSERT, values)
                cur.execute(POSTGRES_PROVIDER_CHUNK_ATTEMPTS_INSERT, values)
            conn.commit()

    def claim_retryable_records(
        self,
        *,
        worker_id: str,
        provider_id: str = "",
        limit: int = 1,
    ) -> list[dict[str, Any]]:
        psycopg = self._psycopg()
        with psycopg.connect(self.dsn) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    POSTGRES_PROVIDER_CHUNK_RUNS_CLAIM_RETRYABLE,
                    {
                        "provider_id": str(provider_id or ""),
                        "worker_id": str(worker_id or "worker"),
                        "limit": max(1, min(int(limit or 1), 500)),
                    },
                )
                rows = cur.fetchall()
            conn.commit()
        records: list[dict[str, Any]] = []
        for row in rows:
            record = row[0]
            if isinstance(record, dict):
                records.append(dict(record))
        return records

    @staticmethod
    def _psycopg() -> Any:
        try:
            return importlib.import_module("psycopg")
        except ImportError as exc:  # pragma: no cover - depends on optional extra
            raise RuntimeError(
                "PostgreSQL chunk run store requires the optional postgres dependencies. "
                'Install with: pip install -e ".[postgres]"'
            ) from exc

    @staticmethod
    def _jsonb_adapter() -> Any:
        try:
            module = importlib.import_module("psycopg.types.json")
        except ImportError as exc:  # pragma: no cover - depends on optional extra
            raise RuntimeError(
                "PostgreSQL chunk run store requires psycopg JSONB support."
            ) from exc
        return module.Jsonb


def _postgres_record_values(record: dict[str, Any], *, jsonb: Any) -> dict[str, Any]:
    status = str(record.get("status") or "")
    retry_after = int(record.get("retry_after_sec") or 0)
    return {
        "run_id": str(record.get("run_id") or ""),
        "evidence_sha256": str(record.get("evidence_sha256") or ""),
        "provider_id": str(record.get("provider_id") or ""),
        "model_name": str(record.get("model_name") or ""),
        "chunk_id": str(record.get("chunk_id") or ""),
        "chunk_index": int(record.get("chunk_index") or 0),
        "chunk_count": int(record.get("chunk_count") or 0),
        "chunk_type": str(record.get("chunk_type") or ""),
        "prompt_sha256": str(record.get("prompt_sha256") or ""),
        "prompt_cache_key": str(record.get("prompt_cache_key") or ""),
        "status": status,
        "provider_status": str(record.get("provider_status") or ""),
        "schema_valid": bool(record.get("schema_valid")),
        "attempt_count": int(record.get("attempt_count") or 0),
        "attempt_no": max(1, int(record.get("attempt_count") or 0)),
        "max_attempts": int(record.get("max_attempts") or 0),
        "retried": bool(record.get("retried")),
        "retryable": bool(record.get("retryable")),
        "last_error_type": str(record.get("last_error_type") or ""),
        "last_error_message": str(record.get("last_error_message") or ""),
        "retry_after_sec": retry_after,
        "next_retry_at": _next_retry_at_sql(status=status, retry_after_sec=retry_after),
        "input_tokens": int(record.get("input_tokens") or 0),
        "output_tokens": int(record.get("output_tokens") or 0),
        "latency_ms": int(record.get("latency_ms") or 0),
        "raw_output_sha256": str(record.get("raw_output_sha256") or ""),
        "parsed_output_sha256": str(record.get("parsed_output_sha256") or ""),
        "parse_status": str(record.get("parse_status") or ""),
        "repair_applied": bool(record.get("repair_applied")),
        "repair_rules_json": jsonb(record.get("repair_rules") or []),
        "semantic_keys_json": jsonb(record.get("semantic_keys") or []),
        "coverage_classes_json": jsonb(record.get("coverage_classes") or []),
        "source_log_count": int(record.get("source_log_count") or 0),
        "evidence_item_count": int(record.get("evidence_item_count") or 0),
        "estimated_input_tokens": int(record.get("estimated_input_tokens") or 0),
        "token_budget": int(record.get("token_budget") or 0),
        "artifact_json": jsonb(record.get("artifact") or {}),
        "parsed_payload_json": jsonb(record.get("parsed_payload") or {}),
        "record_json": jsonb(record),
        "attempt_json": jsonb(_attempt_json(record)),
        "started_at": _timestamp_or_none(record.get("started_at")),
        "finished_at": _timestamp_or_none(record.get("finished_at")),
    }


def _attempt_json(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": "provider_chunk_attempt.v1",
        "run_id": str(record.get("run_id") or ""),
        "evidence_sha256": str(record.get("evidence_sha256") or ""),
        "provider_id": str(record.get("provider_id") or ""),
        "model_name": str(record.get("model_name") or ""),
        "chunk_id": str(record.get("chunk_id") or ""),
        "chunk_index": int(record.get("chunk_index") or 0),
        "chunk_count": int(record.get("chunk_count") or 0),
        "prompt_sha256": str(record.get("prompt_sha256") or ""),
        "status": str(record.get("status") or ""),
        "provider_status": str(record.get("provider_status") or ""),
        "schema_valid": bool(record.get("schema_valid")),
        "attempt_count": int(record.get("attempt_count") or 0),
        "last_error_type": str(record.get("last_error_type") or ""),
        "last_error_message": str(record.get("last_error_message") or ""),
        "retry_after_sec": int(record.get("retry_after_sec") or 0),
        "latency_ms": int(record.get("latency_ms") or 0),
        "input_tokens": int(record.get("input_tokens") or 0),
        "output_tokens": int(record.get("output_tokens") or 0),
        "raw_output_sha256": str(record.get("raw_output_sha256") or ""),
        "parsed_output_sha256": str(record.get("parsed_output_sha256") or ""),
        "started_at": str(record.get("started_at") or ""),
        "finished_at": str(record.get("finished_at") or ""),
    }


def _timestamp_or_none(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _next_retry_at_sql(*, status: str, retry_after_sec: int) -> str | None:
    if status == "ok":
        return None
    if retry_after_sec <= 0:
        return None
    retry_at = datetime.now(UTC) + timedelta(seconds=min(retry_after_sec, 86400))
    return retry_at.isoformat()


POSTGRES_PROVIDER_CHUNK_RUNS_SCHEMA = """
CREATE TABLE IF NOT EXISTS provider_chunk_runs (
  id bigserial PRIMARY KEY,
  evidence_sha256 text NOT NULL,
  provider_id text NOT NULL,
  model_name text NOT NULL DEFAULT '',
  chunk_id text NOT NULL,
  chunk_index integer NOT NULL DEFAULT 0,
  chunk_count integer NOT NULL DEFAULT 0,
  chunk_type text NOT NULL DEFAULT '',
  prompt_sha256 text NOT NULL,
  prompt_cache_key text NOT NULL DEFAULT '',
  status text NOT NULL,
  provider_status text NOT NULL DEFAULT '',
  schema_valid boolean NOT NULL DEFAULT false,
  attempt_count integer NOT NULL DEFAULT 0,
  max_attempts integer NOT NULL DEFAULT 0,
  retried boolean NOT NULL DEFAULT false,
  retryable boolean NOT NULL DEFAULT false,
  last_error_type text NOT NULL DEFAULT '',
  last_error_message text NOT NULL DEFAULT '',
  retry_after_sec integer NOT NULL DEFAULT 0,
  next_retry_at timestamptz,
  locked_at timestamptz,
  worker_id text NOT NULL DEFAULT '',
  input_tokens integer NOT NULL DEFAULT 0,
  output_tokens integer NOT NULL DEFAULT 0,
  latency_ms integer NOT NULL DEFAULT 0,
  raw_output_sha256 text NOT NULL DEFAULT '',
  parsed_output_sha256 text NOT NULL DEFAULT '',
  parse_status text NOT NULL DEFAULT '',
  repair_applied boolean NOT NULL DEFAULT false,
  repair_rules_json jsonb NOT NULL DEFAULT '[]'::jsonb,
  semantic_keys_json jsonb NOT NULL DEFAULT '[]'::jsonb,
  coverage_classes_json jsonb NOT NULL DEFAULT '[]'::jsonb,
  source_log_count integer NOT NULL DEFAULT 0,
  evidence_item_count integer NOT NULL DEFAULT 0,
  estimated_input_tokens integer NOT NULL DEFAULT 0,
  token_budget integer NOT NULL DEFAULT 0,
  artifact_json jsonb NOT NULL DEFAULT '{}'::jsonb,
  parsed_payload_json jsonb NOT NULL DEFAULT '{}'::jsonb,
  record_json jsonb NOT NULL DEFAULT '{}'::jsonb,
  started_at timestamptz,
  finished_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (provider_id, prompt_sha256)
);

CREATE INDEX IF NOT EXISTS provider_chunk_runs_retry
  ON provider_chunk_runs(provider_id, status, next_retry_at);

CREATE INDEX IF NOT EXISTS provider_chunk_runs_evidence
  ON provider_chunk_runs(evidence_sha256, provider_id, chunk_index);

CREATE TABLE IF NOT EXISTS provider_chunk_attempts (
  attempt_id bigserial PRIMARY KEY,
  run_id text NOT NULL,
  evidence_sha256 text NOT NULL,
  provider_id text NOT NULL,
  model_name text NOT NULL DEFAULT '',
  chunk_id text NOT NULL,
  chunk_index integer NOT NULL DEFAULT 0,
  chunk_count integer NOT NULL DEFAULT 0,
  prompt_sha256 text NOT NULL,
  attempt_no integer NOT NULL DEFAULT 1,
  status text NOT NULL,
  provider_status text NOT NULL DEFAULT '',
  schema_valid boolean NOT NULL DEFAULT false,
  error_type text NOT NULL DEFAULT '',
  error_message text NOT NULL DEFAULT '',
  retry_after_sec integer NOT NULL DEFAULT 0,
  input_tokens integer NOT NULL DEFAULT 0,
  output_tokens integer NOT NULL DEFAULT 0,
  latency_ms integer NOT NULL DEFAULT 0,
  raw_output_sha256 text NOT NULL DEFAULT '',
  parsed_output_sha256 text NOT NULL DEFAULT '',
  attempt_json jsonb NOT NULL DEFAULT '{}'::jsonb,
  record_json jsonb NOT NULL DEFAULT '{}'::jsonb,
  started_at timestamptz,
  finished_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS provider_chunk_attempts_lookup
  ON provider_chunk_attempts(evidence_sha256, provider_id, chunk_id, created_at);
"""


POSTGRES_PROVIDER_CHUNK_RUNS_UPSERT = """
INSERT INTO provider_chunk_runs (
  evidence_sha256,
  provider_id,
  model_name,
  chunk_id,
  chunk_index,
  chunk_count,
  chunk_type,
  prompt_sha256,
  prompt_cache_key,
  status,
  provider_status,
  schema_valid,
  attempt_count,
  max_attempts,
  retried,
  retryable,
  last_error_type,
  last_error_message,
  retry_after_sec,
  next_retry_at,
  input_tokens,
  output_tokens,
  latency_ms,
  raw_output_sha256,
  parsed_output_sha256,
  parse_status,
  repair_applied,
  repair_rules_json,
  semantic_keys_json,
  coverage_classes_json,
  source_log_count,
  evidence_item_count,
  estimated_input_tokens,
  token_budget,
  artifact_json,
  parsed_payload_json,
  record_json,
  started_at,
  finished_at
) VALUES (
  %(evidence_sha256)s,
  %(provider_id)s,
  %(model_name)s,
  %(chunk_id)s,
  %(chunk_index)s,
  %(chunk_count)s,
  %(chunk_type)s,
  %(prompt_sha256)s,
  %(prompt_cache_key)s,
  %(status)s,
  %(provider_status)s,
  %(schema_valid)s,
  %(attempt_count)s,
  %(max_attempts)s,
  %(retried)s,
  %(retryable)s,
  %(last_error_type)s,
  %(last_error_message)s,
  %(retry_after_sec)s,
  %(next_retry_at)s::timestamptz,
  %(input_tokens)s,
  %(output_tokens)s,
  %(latency_ms)s,
  %(raw_output_sha256)s,
  %(parsed_output_sha256)s,
  %(parse_status)s,
  %(repair_applied)s,
  %(repair_rules_json)s,
  %(semantic_keys_json)s,
  %(coverage_classes_json)s,
  %(source_log_count)s,
  %(evidence_item_count)s,
  %(estimated_input_tokens)s,
  %(token_budget)s,
  %(artifact_json)s,
  %(parsed_payload_json)s,
  %(record_json)s,
  %(started_at)s,
  %(finished_at)s
)
ON CONFLICT (provider_id, prompt_sha256) DO UPDATE SET
  evidence_sha256 = EXCLUDED.evidence_sha256,
  model_name = EXCLUDED.model_name,
  chunk_id = EXCLUDED.chunk_id,
  chunk_index = EXCLUDED.chunk_index,
  chunk_count = EXCLUDED.chunk_count,
  chunk_type = EXCLUDED.chunk_type,
  prompt_cache_key = EXCLUDED.prompt_cache_key,
  status = EXCLUDED.status,
  provider_status = EXCLUDED.provider_status,
  schema_valid = EXCLUDED.schema_valid,
  attempt_count = EXCLUDED.attempt_count,
  max_attempts = EXCLUDED.max_attempts,
  retried = EXCLUDED.retried,
  retryable = EXCLUDED.retryable,
  last_error_type = EXCLUDED.last_error_type,
  last_error_message = EXCLUDED.last_error_message,
  retry_after_sec = EXCLUDED.retry_after_sec,
  next_retry_at = EXCLUDED.next_retry_at,
  input_tokens = EXCLUDED.input_tokens,
  output_tokens = EXCLUDED.output_tokens,
  latency_ms = EXCLUDED.latency_ms,
  raw_output_sha256 = EXCLUDED.raw_output_sha256,
  parsed_output_sha256 = EXCLUDED.parsed_output_sha256,
  parse_status = EXCLUDED.parse_status,
  repair_applied = EXCLUDED.repair_applied,
  repair_rules_json = EXCLUDED.repair_rules_json,
  semantic_keys_json = EXCLUDED.semantic_keys_json,
  coverage_classes_json = EXCLUDED.coverage_classes_json,
  source_log_count = EXCLUDED.source_log_count,
  evidence_item_count = EXCLUDED.evidence_item_count,
  estimated_input_tokens = EXCLUDED.estimated_input_tokens,
  token_budget = EXCLUDED.token_budget,
  artifact_json = EXCLUDED.artifact_json,
  parsed_payload_json = EXCLUDED.parsed_payload_json,
  record_json = EXCLUDED.record_json,
  started_at = EXCLUDED.started_at,
  finished_at = EXCLUDED.finished_at,
  updated_at = now()
WHERE provider_chunk_runs.status <> 'ok'
   OR EXCLUDED.status = 'ok';
"""


POSTGRES_PROVIDER_CHUNK_ATTEMPTS_INSERT = """
INSERT INTO provider_chunk_attempts (
  run_id,
  evidence_sha256,
  provider_id,
  model_name,
  chunk_id,
  chunk_index,
  chunk_count,
  prompt_sha256,
  attempt_no,
  status,
  provider_status,
  schema_valid,
  error_type,
  error_message,
  retry_after_sec,
  input_tokens,
  output_tokens,
  latency_ms,
  raw_output_sha256,
  parsed_output_sha256,
  attempt_json,
  record_json,
  started_at,
  finished_at
) VALUES (
  %(run_id)s,
  %(evidence_sha256)s,
  %(provider_id)s,
  %(model_name)s,
  %(chunk_id)s,
  %(chunk_index)s,
  %(chunk_count)s,
  %(prompt_sha256)s,
  %(attempt_no)s,
  %(status)s,
  %(provider_status)s,
  %(schema_valid)s,
  %(last_error_type)s,
  %(last_error_message)s,
  %(retry_after_sec)s,
  %(input_tokens)s,
  %(output_tokens)s,
  %(latency_ms)s,
  %(raw_output_sha256)s,
  %(parsed_output_sha256)s,
  %(attempt_json)s,
  %(record_json)s,
  %(started_at)s,
  %(finished_at)s
);
"""


POSTGRES_PROVIDER_CHUNK_RUNS_CLAIM_RETRYABLE = """
WITH picked AS (
  SELECT id
  FROM provider_chunk_runs
  WHERE status <> 'ok'
    AND status <> 'retry_exhausted'
    AND status <> 'safety_filter'
    AND status <> 'context_length'
    AND (%(provider_id)s = '' OR provider_id = %(provider_id)s)
    AND (next_retry_at IS NULL OR next_retry_at <= now())
    AND (locked_at IS NULL OR locked_at < now() - interval '15 minutes')
  ORDER BY next_retry_at NULLS FIRST, updated_at, id
  LIMIT %(limit)s
  FOR UPDATE SKIP LOCKED
)
UPDATE provider_chunk_runs AS runs
SET locked_at = now(),
    worker_id = %(worker_id)s,
    updated_at = now()
FROM picked
WHERE runs.id = picked.id
RETURNING runs.record_json;
"""
