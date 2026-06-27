from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
from dataclasses import asdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

from ops_evidence_synthesis.canonical import canonical_json, sha256_json
from ops_evidence_synthesis.models import (
    ClaimRecord,
    ModelRunRecord,
    ParsedResultRecord,
    PropositionClusterRecord,
    PropositionRecord,
    SanitizedLog,
    ScoreRecord,
    severity_rank,
)
from ops_evidence_synthesis.profiles import profile_for_bundle
from ops_evidence_synthesis.synthesis.structured_evidence import build_structured_evidence
from ops_evidence_synthesis.synthesis.more_data import (
    analyze_more_data_queries,
    bigquery_text_predicate_for_request,
    filter_more_data_requests,
    normalize_more_data_requests,
)
from ops_evidence_synthesis.synthesis.review_quality import shape_review_queue
from ops_evidence_synthesis.synthesis.review_targets import (
    attach_review_target_artifacts,
    build_review_target_set,
    more_data_request_for_target,
)
from ops_evidence_synthesis.synthesis.subsystems import bigquery_predicate_for_subsystem
from ops_evidence_synthesis.timeutils import utc_now


DEFAULT_BIGQUERY_LOCATION = "asia-northeast1"

_PIPELINE_TABLES_READY: set[tuple[str, str]] = set()
_PIPELINE_TABLES_LOCK = threading.Lock()


def _json_value(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value



def _snapshot_storage_row(snapshot: dict[str, Any]) -> dict[str, Any]:
    return {
        "evidence_sha256": str(snapshot.get("evidence_sha256") or ""),
        "canonical_graph_sha256": str(snapshot.get("canonical_graph_sha256") or ""),
        "schema_version": str(snapshot.get("schema_version") or ""),
        "arbitration_version": str(snapshot.get("arbitration_version") or ""),
        "input_fingerprint_sha256": str(snapshot.get("input_fingerprint_sha256") or ""),
        "input_fingerprint_json": canonical_json(snapshot.get("input_fingerprint_json") or {}),
        "finding_title": str(snapshot.get("finding_title") or ""),
        "finding_impact": str(snapshot.get("finding_impact") or ""),
        "primary_count": int(snapshot.get("primary_count") or 0),
        "validation_count": int(snapshot.get("validation_count") or 0),
        "monitor_only_count": int(snapshot.get("monitor_only_count") or 0),
        "auto_archived_count": int(snapshot.get("auto_archived_count") or 0),
        "promotion_decision_count": int(snapshot.get("promotion_decision_count") or 0),
        "created_at": str(snapshot.get("created_at") or utc_now()),
        "created_by": str(snapshot.get("created_by") or ""),
        "snapshot_status": str(snapshot.get("snapshot_status") or "persisted"),
        "canonical_review_graph_json": canonical_json(snapshot.get("canonical_review_graph_json") or {}),
    }


def _snapshot_row_to_dict(row: Any) -> dict[str, Any]:
    return {
        "evidence_sha256": str(_row_get(row, "evidence_sha256") or ""),
        "canonical_graph_sha256": str(_row_get(row, "canonical_graph_sha256") or ""),
        "schema_version": str(_row_get(row, "schema_version") or ""),
        "arbitration_version": str(_row_get(row, "arbitration_version") or ""),
        "input_fingerprint_sha256": str(_row_get(row, "input_fingerprint_sha256") or ""),
        "input_fingerprint_json": dict(_json_value(_row_get(row, "input_fingerprint_json")) or {}),
        "finding_title": str(_row_get(row, "finding_title") or ""),
        "finding_impact": str(_row_get(row, "finding_impact") or ""),
        "primary_count": int(_row_get(row, "primary_count") or 0),
        "validation_count": int(_row_get(row, "validation_count") or 0),
        "monitor_only_count": int(_row_get(row, "monitor_only_count") or 0),
        "auto_archived_count": int(_row_get(row, "auto_archived_count") or 0),
        "promotion_decision_count": int(_row_get(row, "promotion_decision_count") or 0),
        "created_at": str(_row_get(row, "created_at") or ""),
        "created_by": str(_row_get(row, "created_by") or ""),
        "snapshot_status": str(_row_get(row, "snapshot_status") or ""),
        "canonical_review_graph_json": dict(_json_value(_row_get(row, "canonical_review_graph_json")) or {}),
    }


def _pipeline_run_storage_row(run: dict[str, Any]) -> dict[str, Any]:
    return {
        "pipeline_run_id": str(run.get("pipeline_run_id") or ""),
        "evidence_sha256": str(run.get("evidence_sha256") or ""),
        "parent_pipeline_run_id": str(run.get("parent_pipeline_run_id") or ""),
        "operation": str(run.get("operation") or ""),
        "status": str(run.get("status") or "running"),
        "current_step": str(run.get("current_step") or ""),
        "total_steps": int(run.get("total_steps") or 0),
        "completed_steps": int(run.get("completed_steps") or 0),
        "blocking_reason": str(run.get("blocking_reason") or ""),
        "provider_total": int(run.get("provider_total") or 0),
        "provider_success": int(run.get("provider_success") or 0),
        "provider_failed": int(run.get("provider_failed") or 0),
        "provider_skipped": int(run.get("provider_skipped") or 0),
        "review_target_count": int(run.get("review_target_count") or 0),
        "validation_target_count": int(run.get("validation_target_count") or 0),
        "child_bundle_count": int(run.get("child_bundle_count") or 0),
        "summary_json": canonical_json(run.get("summary") or {}),
        "error_message": str(run.get("error_message") or ""),
        "created_at": str(run.get("created_at") or utc_now()),
        "updated_at": str(run.get("updated_at") or utc_now()),
        "completed_at": str(run.get("completed_at")) if run.get("completed_at") else None,
    }


def _pipeline_run_row_to_dict(row: Any) -> dict[str, Any]:
    return {
        "pipeline_run_id": str(_row_get(row, "pipeline_run_id") or ""),
        "evidence_sha256": str(_row_get(row, "evidence_sha256") or ""),
        "parent_pipeline_run_id": str(_row_get(row, "parent_pipeline_run_id") or ""),
        "operation": str(_row_get(row, "operation") or ""),
        "status": str(_row_get(row, "status") or ""),
        "current_step": str(_row_get(row, "current_step") or ""),
        "total_steps": int(_row_get(row, "total_steps", 0) or 0),
        "completed_steps": int(_row_get(row, "completed_steps", 0) or 0),
        "blocking_reason": str(_row_get(row, "blocking_reason") or ""),
        "provider_total": int(_row_get(row, "provider_total", 0) or 0),
        "provider_success": int(_row_get(row, "provider_success", 0) or 0),
        "provider_failed": int(_row_get(row, "provider_failed", 0) or 0),
        "provider_skipped": int(_row_get(row, "provider_skipped", 0) or 0),
        "review_target_count": int(_row_get(row, "review_target_count", 0) or 0),
        "validation_target_count": int(_row_get(row, "validation_target_count", 0) or 0),
        "child_bundle_count": int(_row_get(row, "child_bundle_count", 0) or 0),
        "summary": dict(_json_value(_row_get(row, "summary_json", {})) or {}),
        "error_message": str(_row_get(row, "error_message") or ""),
        "created_at": str(_row_get(row, "created_at") or ""),
        "updated_at": str(_row_get(row, "updated_at") or ""),
        "completed_at": str(_row_get(row, "completed_at") or ""),
    }


def _pipeline_event_storage_row(event: dict[str, Any]) -> dict[str, Any]:
    return {
        "event_id": str(event.get("event_id") or ""),
        "pipeline_run_id": str(event.get("pipeline_run_id") or ""),
        "evidence_sha256": str(event.get("evidence_sha256") or ""),
        "operation": str(event.get("operation") or ""),
        "event_type": str(event.get("event_type") or ""),
        "stage": str(event.get("stage") or ""),
        "step_key": str(event.get("step_key") or ""),
        "step_label": str(event.get("step_label") or ""),
        "status": str(event.get("status") or ""),
        "provider_id": str(event.get("provider_id") or ""),
        "artifact_id": str(event.get("artifact_id") or ""),
        "input_sha256": str(event.get("input_sha256") or ""),
        "output_sha256": str(event.get("output_sha256") or ""),
        "reason_code": str(event.get("reason_code") or ""),
        "message": str(event.get("message") or ""),
        "ordinal": int(event.get("ordinal") or 0),
        "metadata_json": canonical_json(event.get("metadata") or {}),
        "created_at": str(event.get("created_at") or utc_now()),
    }


def _pipeline_event_row_to_dict(row: Any) -> dict[str, Any]:
    return {
        "event_id": str(_row_get(row, "event_id") or ""),
        "pipeline_run_id": str(_row_get(row, "pipeline_run_id") or ""),
        "evidence_sha256": str(_row_get(row, "evidence_sha256") or ""),
        "operation": str(_row_get(row, "operation") or ""),
        "event_type": str(_row_get(row, "event_type") or ""),
        "stage": str(_row_get(row, "stage") or ""),
        "step_key": str(_row_get(row, "step_key") or ""),
        "step_label": str(_row_get(row, "step_label") or ""),
        "status": str(_row_get(row, "status") or ""),
        "provider_id": str(_row_get(row, "provider_id") or ""),
        "artifact_id": str(_row_get(row, "artifact_id") or ""),
        "input_sha256": str(_row_get(row, "input_sha256") or ""),
        "output_sha256": str(_row_get(row, "output_sha256") or ""),
        "reason_code": str(_row_get(row, "reason_code") or ""),
        "message": str(_row_get(row, "message") or ""),
        "ordinal": int(_row_get(row, "ordinal", 0) or 0),
        "metadata": dict(_json_value(_row_get(row, "metadata_json", {})) or {}),
        "created_at": str(_row_get(row, "created_at") or ""),
    }


def _model_output_artifact_storage_row(artifact: dict[str, Any]) -> dict[str, Any]:
    return {
        "artifact_id": str(artifact.get("artifact_id") or ""),
        "run_id": str(artifact.get("run_id") or ""),
        "evidence_sha256": str(artifact.get("evidence_sha256") or ""),
        "provider": str(artifact.get("provider") or ""),
        "model_name": str(artifact.get("model_name") or ""),
        "raw_output_sha256": str(artifact.get("raw_output_sha256") or ""),
        "repaired_output_sha256": str(artifact.get("repaired_output_sha256") or ""),
        "parsed_json_sha256": str(artifact.get("parsed_json_sha256") or ""),
        "parse_status": str(artifact.get("parse_status") or ""),
        "repair_applied": bool(artifact.get("repair_applied")),
        "repair_rules": [str(item) for item in artifact.get("repair_rules") or []],
        "schema_valid": bool(artifact.get("schema_valid")),
        "schema_errors": [str(item) for item in artifact.get("schema_errors") or []],
        "original_preserved": bool(artifact.get("original_preserved")),
        "artifact_json": canonical_json(artifact),
        "created_at": str(artifact.get("created_at") or utc_now()),
    }


def _model_output_artifact_row_to_dict(row: Any) -> dict[str, Any]:
    artifact = dict(_json_value(_row_get(row, "artifact_json")) or {})
    artifact.update(
        {
            "artifact_id": str(_row_get(row, "artifact_id") or ""),
            "run_id": str(_row_get(row, "run_id") or ""),
            "evidence_sha256": str(_row_get(row, "evidence_sha256") or ""),
            "provider": str(_row_get(row, "provider") or ""),
            "model_name": str(_row_get(row, "model_name") or ""),
            "raw_output_sha256": str(_row_get(row, "raw_output_sha256") or ""),
            "repaired_output_sha256": str(_row_get(row, "repaired_output_sha256") or ""),
            "parsed_json_sha256": str(_row_get(row, "parsed_json_sha256") or ""),
            "parse_status": str(_row_get(row, "parse_status") or ""),
            "repair_applied": bool(_row_get(row, "repair_applied")),
            "repair_rules": list(_row_get(row, "repair_rules", []) or []),
            "schema_valid": bool(_row_get(row, "schema_valid")),
            "schema_errors": list(_row_get(row, "schema_errors", []) or []),
            "original_preserved": bool(_row_get(row, "original_preserved")),
            "created_at": str(_row_get(row, "created_at") or ""),
        }
    )
    return artifact


def _observation_group_storage_row(group: dict[str, Any]) -> dict[str, Any]:
    return {
        "group_id": str(group.get("group_id") or ""),
        "evidence_sha256": str(group.get("evidence_sha256") or ""),
        "canonical_group_key": str(group.get("canonical_group_key") or ""),
        "canonical_target_type": str(group.get("canonical_target_type") or ""),
        "canonical_subject": str(group.get("canonical_subject") or ""),
        "subsystem": str(group.get("subsystem") or "general"),
        "component": str(group.get("component") or ""),
        "source_target_ids": [str(item) for item in group.get("source_target_ids") or []],
        "source_candidate_count": int(group.get("source_candidate_count") or len(group.get("source_target_ids") or []) or 1),
        "providers": [str(item) for item in group.get("providers") or []],
        "provider_count": int(group.get("provider_count") or 0),
        "evidence_refs": [str(item) for item in group.get("evidence_refs") or []],
        "missing_evidence": [str(item) for item in group.get("missing_evidence") or []],
        "caveats": [str(item) for item in group.get("caveats") or []],
        "support_evidence": canonical_json(group.get("support_evidence") or []),
        "counter_evidence": canonical_json(group.get("counter_evidence") or []),
        "review_priority_score": float(group.get("review_priority_score") or 0.0),
        "consensus_class": str(group.get("consensus_class") or ""),
        "group_json": canonical_json(group),
        "created_at": str(group.get("created_at") or utc_now()),
    }


def _observation_group_row_to_dict(row: Any) -> dict[str, Any]:
    group = dict(_json_value(_row_get(row, "group_json")) or {})
    group.update(
        {
            "group_id": str(_row_get(row, "group_id") or ""),
            "evidence_sha256": str(_row_get(row, "evidence_sha256") or ""),
            "canonical_group_key": str(_row_get(row, "canonical_group_key") or ""),
            "canonical_target_type": str(_row_get(row, "canonical_target_type") or ""),
            "canonical_subject": str(_row_get(row, "canonical_subject") or ""),
            "subsystem": str(_row_get(row, "subsystem") or "general"),
            "component": str(_row_get(row, "component") or ""),
            "source_target_ids": list(_row_get(row, "source_target_ids", []) or []),
            "source_candidate_count": int(_row_get(row, "source_candidate_count", 0) or 0),
            "providers": list(_row_get(row, "providers", []) or []),
            "provider_count": int(_row_get(row, "provider_count", 0) or 0),
            "evidence_refs": list(_row_get(row, "evidence_refs", []) or []),
            "missing_evidence": list(_row_get(row, "missing_evidence", []) or []),
            "caveats": list(_row_get(row, "caveats", []) or []),
            "support_evidence": list(_json_value(_row_get(row, "support_evidence")) or []),
            "counter_evidence": list(_json_value(_row_get(row, "counter_evidence")) or []),
            "review_priority_score": float(_row_get(row, "review_priority_score", 0.0) or 0.0),
            "consensus_class": str(_row_get(row, "consensus_class") or ""),
            "created_at": str(_row_get(row, "created_at") or ""),
        }
    )
    return group


def _stored_review_target_set(targets: list[dict[str, Any]]) -> dict[str, Any]:
    primary_count = sum(
        1
        for target in targets
        if str(target.get("class") or target.get("target_class") or target.get("review_mode") or "")
        in {"primary_candidate", "incident_candidate"}
    )
    validation_count = sum(
        1
        for target in targets
        if str(target.get("class") or target.get("target_class") or target.get("review_mode") or "")
        in {"validation_target", "needs_validation"}
    )
    if not validation_count and targets:
        validation_count = max(0, len(targets) - primary_count)
    return {
        "schema_version": "canonical_review_target_set.v1",
        "summary": {
            "raw_propositions": 0,
            "clusters": 0,
            "claim_groups": 0,
            "review_targets": len(targets),
            "primary_review_targets": primary_count,
            "validation_targets": validation_count,
            "monitor_only": sum(1 for target in targets if str(target.get("class") or "") == "monitor_only"),
            "auto_archived": sum(1 for target in targets if str(target.get("class") or "") == "auto_archived"),
            "insufficient_evidence": 0,
            "score_note": "Score is review priority, not truth probability.",
            "score_note_ja": "Score is review priority, not truth probability.",
        },
        "targets": targets,
    }


def _bundle_storage_row(bundle: dict[str, Any]) -> dict[str, str]:
    source = bundle.get("source") if isinstance(bundle.get("source"), dict) else {}
    window = bundle.get("time_window") if isinstance(bundle.get("time_window"), dict) else {}
    evidence_sha256 = str(bundle["evidence_sha256"])
    return {
        "evidence_sha256": evidence_sha256,
        "schema_version": str(bundle.get("schema_version") or ""),
        "service": str(bundle.get("service") or source.get("service") or ""),
        "environment": str(bundle.get("environment") or source.get("environment") or ""),
        "window_start": str(bundle.get("window_start") or window.get("start") or ""),
        "window_end": str(bundle.get("window_end") or window.get("end") or ""),
        "query_sql_hash": str(bundle.get("query_sql_hash") or sha256_json({"evidence_sha256": evidence_sha256})),
        "sanitizer_version": str(bundle.get("sanitizer_version") or "sanitize.v1"),
        "created_at": str(bundle.get("created_at") or utc_now()),
    }


def _row_get(row: Any, key: str, default: Any = None) -> Any:
    try:
        return row[key]
    except Exception:
        return default


def _severity_for_rank(rank: int) -> str:
    if rank >= 70:
        return "EMERGENCY"
    if rank >= 60:
        return "ALERT"
    if rank >= 50:
        return "CRITICAL"
    if rank >= 40:
        return "ERROR"
    if rank >= 30:
        return "WARN"
    if rank >= 25:
        return "NOTICE"
    if rank >= 20:
        return "INFO"
    if rank >= 10:
        return "DEBUG"
    return "INFO"


class BigQueryOps:
    """BigQuery-backed evidence and synthesis store.

    SQLite remains the fast local test store. This class mirrors the same store
    surface used by the pipeline so Cloud Run and one-off GCP runs can persist
    logs, model outputs, parsed JSON, claims, propositions, and scores directly
    into BigQuery.
    """

    def __init__(self, project_id: str, *, location: str = DEFAULT_BIGQUERY_LOCATION) -> None:
        try:
            from google.cloud import bigquery
        except Exception as exc:  # pragma: no cover
            raise RuntimeError("Install ops-evidence-synthesis[gcp] to use BigQueryOps") from exc

        self.bigquery = bigquery
        self.project_id = project_id
        self.location = location
        credentials = self._credentials_from_env()
        self.client = bigquery.Client(project=project_id, location=location, credentials=credentials)

    def _credentials_from_env(self) -> Any:
        token = os.environ.get("GOOGLE_OAUTH_ACCESS_TOKEN")
        if not token and os.environ.get("OES_BIGQUERY_AUTH", "auto").casefold() in {"auto", "gcloud"}:
            token = self._gcloud_access_token()
        if not token:
            return None
        try:
            from google.oauth2.credentials import Credentials
        except Exception as exc:  # pragma: no cover
            raise RuntimeError("google-auth is required for GOOGLE_OAUTH_ACCESS_TOKEN auth") from exc
        return Credentials(token=token, expiry=datetime.utcnow() + timedelta(minutes=50))

    def _gcloud_access_token(self) -> str:
        if not shutil.which("gcloud"):
            return ""
        try:
            completed = subprocess.run(
                ["gcloud", "auth", "print-access-token"],
                check=True,
                capture_output=True,
                text=True,
                timeout=10,
            )
        except Exception:
            return ""
        return completed.stdout.strip()

    def init_schema(self) -> None:
        if os.environ.get("OES_BIGQUERY_APPLY_SCHEMA_ON_STARTUP", "").casefold() in {"1", "true", "yes", "on"}:
            self.apply_schema()

    def apply_schema(self, schema_path: str | Path = "gcp/bigquery/schema.sql") -> None:
        sql = Path(schema_path).read_text(encoding="utf-8").replace("${PROJECT_ID}", self.project_id)
        self.client.query(sql).result()

    def insert_sanitized_logs(self, logs: Iterable[SanitizedLog]) -> int:
        rows = [self._sanitized_log_row(log) for log in logs]
        if not rows:
            return 0
        self._insert_json_rows(
            "ops_evidence_raw",
            "logs_sanitized",
            rows,
            row_ids=[row["raw_log_sha256"] for row in rows],
        )
        return len(rows)

    def load_sanitized_logs_jsonl(self, path: str | Path) -> int:
        target = self._table("ops_evidence_raw", "logs_sanitized")
        job_config = self.bigquery.LoadJobConfig(
            source_format=self.bigquery.SourceFormat.NEWLINE_DELIMITED_JSON,
            write_disposition=self.bigquery.WriteDisposition.WRITE_APPEND,
        )
        source_path = Path(path)
        with source_path.open("rb") as file:
            job = self.client.load_table_from_file(file, target, job_config=job_config)
        job.result()
        return int(job.output_rows or 0)

    def delete_logs(self, *, environment: str, start: str, end: str) -> None:
        sql = f"""
        DELETE FROM `{self._table("ops_evidence_raw", "logs_sanitized")}`
        WHERE environment = @environment
          AND timestamp >= TIMESTAMP(@start)
          AND timestamp < TIMESTAMP(@end)
        """
        self._query(
            sql,
            [
                self.bigquery.ScalarQueryParameter("environment", "STRING", environment),
                self.bigquery.ScalarQueryParameter("start", "STRING", start),
                self.bigquery.ScalarQueryParameter("end", "STRING", end),
            ],
        )

    def fetch_logs(
        self,
        service: str,
        environment: str,
        start: str,
        end: str,
        *,
        min_severity: str | None = None,
    ) -> list[SanitizedLog]:
        params: list[Any] = [
            self.bigquery.ScalarQueryParameter("environment", "STRING", environment),
            self.bigquery.ScalarQueryParameter("start", "STRING", start),
            self.bigquery.ScalarQueryParameter("end", "STRING", end),
        ]
        service_clause = ""
        if service not in {"*", "__all__", f"{environment}-aggregate"}:
            service_clause = "AND service = @service"
            params.append(self.bigquery.ScalarQueryParameter("service", "STRING", service))
        sql = f"""
        SELECT *
        FROM `{self._table("ops_evidence_raw", "logs_sanitized")}`
        WHERE environment = @environment
          AND timestamp >= TIMESTAMP(@start)
          AND timestamp < TIMESTAMP(@end)
          {service_clause}
        QUALIFY ROW_NUMBER() OVER (PARTITION BY raw_log_sha256 ORDER BY timestamp DESC) = 1
        ORDER BY timestamp, raw_log_sha256
        """
        rows = self._query(sql, params)
        min_rank = severity_rank(min_severity) if min_severity else None
        logs: list[SanitizedLog] = []
        for row in rows:
            severity = str(row["severity"])
            if min_rank is not None and severity_rank(severity) < min_rank:
                continue
            logs.append(self._row_to_sanitized_log(row))
        return logs

    def fetch_log_pattern_summaries(
        self,
        service: str,
        environment: str,
        start: str,
        end: str,
        *,
        baseline_start: str,
        baseline_end: str,
        limit: int = 25,
    ) -> list[dict[str, Any]]:
        params: list[Any] = [
            self.bigquery.ScalarQueryParameter("environment", "STRING", environment),
            self.bigquery.ScalarQueryParameter("start", "STRING", start),
            self.bigquery.ScalarQueryParameter("end", "STRING", end),
            self.bigquery.ScalarQueryParameter("baseline_start", "STRING", baseline_start),
            self.bigquery.ScalarQueryParameter("baseline_end", "STRING", baseline_end),
            self.bigquery.ScalarQueryParameter("limit", "INT64", int(limit)),
        ]
        service_clause = ""
        baseline_service_clause = ""
        if service not in {"*", "__all__", f"{environment}-aggregate"}:
            service_clause = "AND service = @service"
            baseline_service_clause = "AND service = @service"
            params.append(self.bigquery.ScalarQueryParameter("service", "STRING", service))
        severity_case = """
            CASE UPPER(severity)
              WHEN 'EMERGENCY' THEN 70
              WHEN 'ALERT' THEN 60
              WHEN 'CRITICAL' THEN 50
              WHEN 'ERROR' THEN 40
              WHEN 'WARN' THEN 30
              WHEN 'WARNING' THEN 30
              WHEN 'NOTICE' THEN 25
              WHEN 'INFO' THEN 20
              WHEN 'DEBUG' THEN 10
              ELSE 0
            END
        """
        sql = f"""
        WITH current_patterns AS (
          SELECT
            message_template,
            error_type,
            COUNT(*) AS count,
            MIN(timestamp) AS first_seen,
            MAX(timestamp) AS last_seen,
            MAX({severity_case}) AS max_severity_rank,
            MIN(raw_log_sha256) AS example_log_sha256
          FROM `{self._table("ops_evidence_raw", "logs_sanitized")}`
          WHERE environment = @environment
            AND timestamp >= TIMESTAMP(@start)
            AND timestamp < TIMESTAMP(@end)
            {service_clause}
          GROUP BY message_template, error_type
        ),
        baseline_patterns AS (
          SELECT
            message_template,
            error_type,
            COUNT(*) AS baseline_count
          FROM `{self._table("ops_evidence_raw", "logs_sanitized")}`
          WHERE environment = @environment
            AND timestamp >= TIMESTAMP(@baseline_start)
            AND timestamp < TIMESTAMP(@baseline_end)
            {baseline_service_clause}
          GROUP BY message_template, error_type
        )
        SELECT
          c.message_template,
          c.error_type,
          c.count,
          FORMAT_TIMESTAMP('%Y-%m-%dT%H:%M:%SZ', c.first_seen) AS first_seen,
          FORMAT_TIMESTAMP('%Y-%m-%dT%H:%M:%SZ', c.last_seen) AS last_seen,
          c.max_severity_rank,
          c.example_log_sha256,
          COALESCE(b.baseline_count, 0) AS baseline_count
        FROM current_patterns c
        LEFT JOIN baseline_patterns b
          ON b.message_template = c.message_template
         AND b.error_type = c.error_type
        ORDER BY c.count DESC, c.message_template, c.error_type
        LIMIT @limit
        """
        rows = self._query(sql, params)
        return [
            {
                "message_template": str(_row_get(row, "message_template", "") or ""),
                "error_type": str(_row_get(row, "error_type", "") or ""),
                "count": int(_row_get(row, "count", 0) or 0),
                "baseline_count": int(_row_get(row, "baseline_count", 0) or 0),
                "first_seen": str(_row_get(row, "first_seen", "") or ""),
                "last_seen": str(_row_get(row, "last_seen", "") or ""),
                "max_severity": _severity_for_rank(int(_row_get(row, "max_severity_rank", 0) or 0)),
                "example_log_sha256": str(_row_get(row, "example_log_sha256", "") or ""),
                "aggregation_source": "bigquery_group_by",
            }
            for row in rows
        ]

    def insert_bundle(self, bundle: dict[str, Any]) -> None:
        storage_row = _bundle_storage_row(bundle)
        row = {
            "evidence_sha256": storage_row["evidence_sha256"],
            "schema_version": storage_row["schema_version"],
            "service": storage_row["service"],
            "environment": storage_row["environment"],
            "window_start": storage_row["window_start"],
            "window_end": storage_row["window_end"],
            "query_sql_hash": storage_row["query_sql_hash"],
            "sanitizer_version": storage_row["sanitizer_version"],
            "bundle_json": json.dumps(bundle, ensure_ascii=False, sort_keys=True),
            "created_at": storage_row["created_at"],
        }
        self._insert_json_rows("ops_synthesis", "evidence_bundles", [row], row_ids=[bundle["evidence_sha256"]])

    def get_bundle(self, evidence_sha256: str) -> dict[str, Any] | None:
        sql = f"""
        SELECT bundle_json
        FROM `{self._table("ops_synthesis", "evidence_bundles")}`
        WHERE evidence_sha256 = @evidence_sha256
        ORDER BY created_at DESC
        LIMIT 1
        """
        rows = list(
            self._query(
                sql,
                [self.bigquery.ScalarQueryParameter("evidence_sha256", "STRING", evidence_sha256)],
            )
            )
        if not rows:
            return None
        return dict(_json_value(rows[0]["bundle_json"]))

    def ensure_pipeline_tables(self) -> None:
        cache_key = (self.project_id, self.location)
        with _PIPELINE_TABLES_LOCK:
            if cache_key in _PIPELINE_TABLES_READY:
                return
            if self._pipeline_tables_have_required_columns():
                _PIPELINE_TABLES_READY.add(cache_key)
                return
        self._query(
            f"""
            CREATE TABLE IF NOT EXISTS `{self._table("ops_synthesis", "pipeline_runs")}` (
              pipeline_run_id STRING NOT NULL,
              evidence_sha256 STRING NOT NULL,
              parent_pipeline_run_id STRING,
              operation STRING NOT NULL,
              status STRING NOT NULL,
              current_step STRING NOT NULL,
              total_steps INT64 NOT NULL,
              completed_steps INT64 NOT NULL,
              blocking_reason STRING,
              provider_total INT64,
              provider_success INT64,
              provider_failed INT64,
              provider_skipped INT64,
              review_target_count INT64,
              validation_target_count INT64,
              child_bundle_count INT64,
              summary_json JSON NOT NULL,
              error_message STRING NOT NULL,
              created_at TIMESTAMP NOT NULL,
              updated_at TIMESTAMP NOT NULL,
              completed_at TIMESTAMP
            )
            PARTITION BY DATE(created_at)
            CLUSTER BY evidence_sha256, pipeline_run_id, status
            """
        )
        self._query(
            f"""
            CREATE TABLE IF NOT EXISTS `{self._table("ops_synthesis", "pipeline_events")}` (
              event_id STRING NOT NULL,
              pipeline_run_id STRING NOT NULL,
              evidence_sha256 STRING NOT NULL,
              operation STRING NOT NULL,
              event_type STRING,
              stage STRING,
              step_key STRING NOT NULL,
              step_label STRING NOT NULL,
              status STRING NOT NULL,
              provider_id STRING,
              artifact_id STRING,
              input_sha256 STRING,
              output_sha256 STRING,
              reason_code STRING,
              message STRING NOT NULL,
              ordinal INT64 NOT NULL,
              metadata_json JSON NOT NULL,
              created_at TIMESTAMP NOT NULL
            )
            PARTITION BY DATE(created_at)
            CLUSTER BY evidence_sha256, pipeline_run_id, step_key
            """
        )
        for column, column_type in (
            ("parent_pipeline_run_id", "STRING"),
            ("blocking_reason", "STRING"),
            ("provider_total", "INT64"),
            ("provider_success", "INT64"),
            ("provider_failed", "INT64"),
            ("provider_skipped", "INT64"),
            ("review_target_count", "INT64"),
            ("validation_target_count", "INT64"),
            ("child_bundle_count", "INT64"),
        ):
            self._query(
                f"ALTER TABLE `{self._table('ops_synthesis', 'pipeline_runs')}` ADD COLUMN IF NOT EXISTS {column} {column_type}"
            )
        for column in ("event_type", "stage", "provider_id", "artifact_id", "input_sha256", "output_sha256", "reason_code"):
            self._query(
                f"ALTER TABLE `{self._table('ops_synthesis', 'pipeline_events')}` ADD COLUMN IF NOT EXISTS {column} STRING"
            )
        with _PIPELINE_TABLES_LOCK:
            _PIPELINE_TABLES_READY.add(cache_key)

    def _pipeline_tables_have_required_columns(self) -> bool:
        required = {
            "pipeline_runs": {
                "pipeline_run_id",
                "evidence_sha256",
                "parent_pipeline_run_id",
                "operation",
                "status",
                "current_step",
                "total_steps",
                "completed_steps",
                "blocking_reason",
                "provider_total",
                "provider_success",
                "provider_failed",
                "provider_skipped",
                "review_target_count",
                "validation_target_count",
                "child_bundle_count",
                "summary_json",
                "error_message",
                "created_at",
                "updated_at",
                "completed_at",
            },
            "pipeline_events": {
                "event_id",
                "pipeline_run_id",
                "evidence_sha256",
                "operation",
                "event_type",
                "stage",
                "step_key",
                "step_label",
                "status",
                "provider_id",
                "artifact_id",
                "input_sha256",
                "output_sha256",
                "reason_code",
                "message",
                "ordinal",
                "metadata_json",
                "created_at",
            },
        }
        try:
            for table, columns in required.items():
                schema = self.client.get_table(self._table("ops_synthesis", table)).schema
                present = {field.name for field in schema}
                if not columns.issubset(present):
                    return False
        except Exception:
            return False
        return True

    def upsert_pipeline_run(self, run: dict[str, Any]) -> None:
        self.ensure_pipeline_tables()
        row = _pipeline_run_storage_row(run)
        if not row["pipeline_run_id"]:
            return
        self._insert_json_rows("ops_synthesis", "pipeline_runs", [row], row_ids=[f"{row['pipeline_run_id']}:{row['updated_at']}"])

    def insert_pipeline_event(self, event: dict[str, Any]) -> None:
        self.ensure_pipeline_tables()
        row = _pipeline_event_storage_row(event)
        if not row["event_id"] or not row["pipeline_run_id"]:
            return
        self._insert_json_rows("ops_synthesis", "pipeline_events", [row], row_ids=[row["event_id"]])

    def get_pipeline_run(self, pipeline_run_id: str) -> dict[str, Any] | None:
        self.ensure_pipeline_tables()
        sql = f"""
        SELECT * EXCEPT(rn)
        FROM (
          SELECT
            *,
            ROW_NUMBER() OVER (PARTITION BY pipeline_run_id ORDER BY updated_at DESC, created_at DESC) AS rn
          FROM `{self._table("ops_synthesis", "pipeline_runs")}`
          WHERE pipeline_run_id = @pipeline_run_id
        )
        WHERE rn = 1
        LIMIT 1
        """
        rows = list(self._query(sql, [self.bigquery.ScalarQueryParameter("pipeline_run_id", "STRING", str(pipeline_run_id or ""))]))
        return _pipeline_run_row_to_dict(rows[0]) if rows else None

    def latest_pipeline_run(self, evidence_sha256: str) -> dict[str, Any] | None:
        self.ensure_pipeline_tables()
        sql = f"""
        SELECT * EXCEPT(rn)
        FROM (
          SELECT
            *,
            ROW_NUMBER() OVER (PARTITION BY pipeline_run_id ORDER BY updated_at DESC, created_at DESC) AS run_rn,
            ROW_NUMBER() OVER (ORDER BY updated_at DESC, created_at DESC, pipeline_run_id DESC) AS rn
          FROM `{self._table("ops_synthesis", "pipeline_runs")}`
          WHERE evidence_sha256 = @evidence_sha256
        )
        WHERE run_rn = 1 AND rn = 1
        LIMIT 1
        """
        rows = list(self._query(sql, [self.bigquery.ScalarQueryParameter("evidence_sha256", "STRING", str(evidence_sha256 or ""))]))
        return _pipeline_run_row_to_dict(rows[0]) if rows else None

    def latest_pipeline_run_by_operations(
        self,
        evidence_sha256: str,
        operations: list[str] | tuple[str, ...],
    ) -> dict[str, Any] | None:
        self.ensure_pipeline_tables()
        operation_values = [str(operation) for operation in operations if str(operation)]
        if not operation_values:
            return self.latest_pipeline_run(evidence_sha256)
        operation_params = ", ".join(f"@operation_{index}" for index, _operation in enumerate(operation_values))
        sql = f"""
        SELECT * EXCEPT(rn)
        FROM (
          SELECT
            *,
            ROW_NUMBER() OVER (PARTITION BY pipeline_run_id ORDER BY updated_at DESC, created_at DESC) AS run_rn,
            ROW_NUMBER() OVER (ORDER BY updated_at DESC, created_at DESC, pipeline_run_id DESC) AS rn
          FROM `{self._table("ops_synthesis", "pipeline_runs")}`
          WHERE evidence_sha256 = @evidence_sha256
            AND operation IN ({operation_params})
        )
        WHERE run_rn = 1 AND rn = 1
        LIMIT 1
        """
        params = [self.bigquery.ScalarQueryParameter("evidence_sha256", "STRING", str(evidence_sha256 or ""))]
        params.extend(
            self.bigquery.ScalarQueryParameter(f"operation_{index}", "STRING", operation)
            for index, operation in enumerate(operation_values)
        )
        rows = list(self._query(sql, params))
        return _pipeline_run_row_to_dict(rows[0]) if rows else None

    def list_pipeline_events(self, pipeline_run_id: str) -> list[dict[str, Any]]:
        self.ensure_pipeline_tables()
        sql = f"""
        SELECT * EXCEPT(rn)
        FROM (
          SELECT
            *,
            ROW_NUMBER() OVER (PARTITION BY event_id ORDER BY created_at DESC) AS rn
          FROM `{self._table("ops_synthesis", "pipeline_events")}`
          WHERE pipeline_run_id = @pipeline_run_id
        )
        WHERE rn = 1
        ORDER BY ordinal, created_at, event_id
        """
        rows = self._query(sql, [self.bigquery.ScalarQueryParameter("pipeline_run_id", "STRING", str(pipeline_run_id or ""))])
        return [_pipeline_event_row_to_dict(row) for row in rows]

    def get_pipeline_status(self, *, evidence_sha256: str = "", pipeline_run_id: str = "") -> dict[str, Any]:
        from ops_evidence_synthesis.pipeline_progress import build_pipeline_status, empty_pipeline_status

        run = self.get_pipeline_run(pipeline_run_id) if pipeline_run_id else self.latest_pipeline_run(evidence_sha256)
        if not run:
            return empty_pipeline_status(evidence_sha256=evidence_sha256, pipeline_run_id=pipeline_run_id)
        events = self.list_pipeline_events(str(run.get("pipeline_run_id") or ""))
        return build_pipeline_status(run, events)

    def ensure_model_output_artifacts_table(self) -> None:
        sql = f"""
        CREATE TABLE IF NOT EXISTS `{self._table("ops_synthesis", "model_output_artifacts")}` (
          artifact_id STRING NOT NULL,
          run_id STRING NOT NULL,
          evidence_sha256 STRING NOT NULL,
          provider STRING NOT NULL,
          model_name STRING NOT NULL,
          raw_output_sha256 STRING NOT NULL,
          repaired_output_sha256 STRING NOT NULL,
          parsed_json_sha256 STRING NOT NULL,
          parse_status STRING NOT NULL,
          repair_applied BOOL NOT NULL,
          repair_rules ARRAY<STRING>,
          schema_valid BOOL NOT NULL,
          schema_errors ARRAY<STRING>,
          original_preserved BOOL NOT NULL,
          artifact_json JSON NOT NULL,
          created_at TIMESTAMP NOT NULL
        )
        PARTITION BY DATE(created_at)
        CLUSTER BY evidence_sha256, provider, parse_status
        """
        self._query(sql)

    def ensure_canonical_review_graphs_table(self) -> None:
        sql = f"""
        CREATE TABLE IF NOT EXISTS `{self._table("ops_synthesis", "canonical_review_graphs")}` (
          evidence_sha256 STRING NOT NULL,
          canonical_graph_sha256 STRING NOT NULL,
          schema_version STRING NOT NULL,
          arbitration_version STRING NOT NULL,
          input_fingerprint_sha256 STRING NOT NULL,
          input_fingerprint_json JSON NOT NULL,
          finding_title STRING,
          finding_impact STRING,
          primary_count INT64 NOT NULL,
          validation_count INT64 NOT NULL,
          monitor_only_count INT64 NOT NULL,
          auto_archived_count INT64 NOT NULL,
          promotion_decision_count INT64 NOT NULL,
          created_at TIMESTAMP NOT NULL,
          created_by STRING NOT NULL,
          snapshot_status STRING NOT NULL,
          canonical_review_graph_json JSON NOT NULL
        )
        PARTITION BY DATE(created_at)
        CLUSTER BY evidence_sha256, input_fingerprint_sha256, canonical_graph_sha256
        """
        self._query(sql)

    def ensure_canonical_observation_groups_table(self) -> None:
        sql = f"""
        CREATE TABLE IF NOT EXISTS `{self._table("ops_synthesis", "canonical_observation_groups")}` (
          group_id STRING NOT NULL,
          evidence_sha256 STRING NOT NULL,
          canonical_group_key STRING NOT NULL,
          canonical_target_type STRING NOT NULL,
          canonical_subject STRING NOT NULL,
          subsystem STRING NOT NULL,
          component STRING,
          source_target_ids ARRAY<STRING>,
          source_candidate_count INT64 NOT NULL,
          providers ARRAY<STRING>,
          provider_count INT64 NOT NULL,
          evidence_refs ARRAY<STRING>,
          missing_evidence ARRAY<STRING>,
          caveats ARRAY<STRING>,
          support_evidence JSON,
          counter_evidence JSON,
          review_priority_score FLOAT64 NOT NULL,
          consensus_class STRING NOT NULL,
          group_json JSON NOT NULL,
          created_at TIMESTAMP NOT NULL
        )
        PARTITION BY DATE(created_at)
        CLUSTER BY evidence_sha256, canonical_group_key, canonical_target_type
        """
        self._query(sql)

    def save_canonical_review_graph_snapshot(self, snapshot: dict[str, Any]) -> dict[str, Any]:
        self.ensure_canonical_review_graphs_table()
        row = _snapshot_storage_row(snapshot)
        existing = self._canonical_review_graph_snapshot_by_identity(
            row["evidence_sha256"],
            row["input_fingerprint_sha256"],
            row["canonical_graph_sha256"],
        )
        if existing is not None:
            return existing
        row_id = sha256_json(
            {
                "evidence_sha256": row["evidence_sha256"],
                "input_fingerprint_sha256": row["input_fingerprint_sha256"],
                "canonical_graph_sha256": row["canonical_graph_sha256"],
            }
        )[:32]
        self._insert_json_rows("ops_synthesis", "canonical_review_graphs", [row], row_ids=[row_id])
        return self._canonical_review_graph_snapshot_by_identity(
            row["evidence_sha256"],
            row["input_fingerprint_sha256"],
            row["canonical_graph_sha256"],
        ) or dict(snapshot)

    def get_latest_canonical_review_graph_snapshot(self, evidence_sha256: str) -> dict[str, Any] | None:
        self.ensure_canonical_review_graphs_table()
        sql = f"""
        SELECT *
        FROM `{self._table("ops_synthesis", "canonical_review_graphs")}`
        WHERE evidence_sha256 = @evidence_sha256
        ORDER BY created_at DESC
        LIMIT 1
        """
        rows = list(
            self._query(sql, [self.bigquery.ScalarQueryParameter("evidence_sha256", "STRING", str(evidence_sha256 or ""))])
        )
        return _snapshot_row_to_dict(rows[0]) if rows else None

    def list_canonical_review_graph_snapshots(self, evidence_sha256: str) -> list[dict[str, Any]]:
        self.ensure_canonical_review_graphs_table()
        sql = f"""
        SELECT *
        FROM `{self._table("ops_synthesis", "canonical_review_graphs")}`
        WHERE evidence_sha256 = @evidence_sha256
        ORDER BY created_at DESC
        """
        rows = self._query(sql, [self.bigquery.ScalarQueryParameter("evidence_sha256", "STRING", str(evidence_sha256 or ""))])
        return [_snapshot_row_to_dict(row) for row in rows]

    def replace_canonical_observation_groups(self, evidence_sha256: str, groups: Iterable[dict[str, Any]]) -> int:
        self.ensure_canonical_observation_groups_table()
        evidence_id = str(evidence_sha256 or "")
        params = [self.bigquery.ScalarQueryParameter("evidence_sha256", "STRING", evidence_id)]
        self._delete_ignoring_streaming_buffer(
            f"""
            DELETE FROM `{self._table("ops_synthesis", "canonical_observation_groups")}`
            WHERE evidence_sha256 = @evidence_sha256
            """,
            params,
        )
        rows = [
            _observation_group_storage_row({**group, "evidence_sha256": str(group.get("evidence_sha256") or evidence_id)})
            for group in groups
            if isinstance(group, dict)
        ]
        if not rows:
            return 0
        row_ids = [
            sha256_json(
                {
                    "group_id": row["group_id"],
                    "evidence_sha256": row["evidence_sha256"],
                    "created_at": row["created_at"],
                }
            )[:32]
            for row in rows
        ]
        self._insert_json_rows("ops_synthesis", "canonical_observation_groups", rows, row_ids=row_ids)
        return len(rows)

    def list_canonical_observation_groups(self, evidence_sha256: str) -> list[dict[str, Any]]:
        self.ensure_canonical_observation_groups_table()
        sql = f"""
        SELECT * EXCEPT(rn)
        FROM (
          SELECT
            *,
            ROW_NUMBER() OVER (PARTITION BY group_id ORDER BY created_at DESC) AS rn
          FROM `{self._table("ops_synthesis", "canonical_observation_groups")}`
          WHERE evidence_sha256 = @evidence_sha256
        )
        WHERE rn = 1
        ORDER BY review_priority_score DESC, group_id
        """
        rows = self._query(sql, [self.bigquery.ScalarQueryParameter("evidence_sha256", "STRING", str(evidence_sha256 or ""))])
        return [_observation_group_row_to_dict(row) for row in rows]

    def _canonical_review_graph_snapshot_by_identity(
        self,
        evidence_sha256: str,
        input_fingerprint_sha256: str,
        canonical_graph_sha256: str,
    ) -> dict[str, Any] | None:
        sql = f"""
        SELECT *
        FROM `{self._table("ops_synthesis", "canonical_review_graphs")}`
        WHERE evidence_sha256 = @evidence_sha256
          AND input_fingerprint_sha256 = @input_fingerprint_sha256
          AND canonical_graph_sha256 = @canonical_graph_sha256
        ORDER BY created_at DESC
        LIMIT 1
        """
        rows = list(
            self._query(
                sql,
                [
                    self.bigquery.ScalarQueryParameter("evidence_sha256", "STRING", evidence_sha256),
                    self.bigquery.ScalarQueryParameter("input_fingerprint_sha256", "STRING", input_fingerprint_sha256),
                    self.bigquery.ScalarQueryParameter("canonical_graph_sha256", "STRING", canonical_graph_sha256),
                ],
            )
        )
        return _snapshot_row_to_dict(rows[0]) if rows else None

    def latest_bundle(self) -> dict[str, Any] | None:
        sql = f"""
        SELECT bundle_json
        FROM `{self._table("ops_synthesis", "evidence_bundles")}`
        ORDER BY created_at DESC
        LIMIT 1
        """
        rows = list(self._query(sql))
        if not rows:
            return None
        return dict(_json_value(rows[0]["bundle_json"]))

    def list_child_bundles(self, parent_evidence_sha256: str, *, limit: int = 20) -> list[dict[str, Any]]:
        parent = str(parent_evidence_sha256 or "")
        if not parent:
            return []
        sql = f"""
        SELECT bundle_json
        FROM `{self._table("ops_synthesis", "evidence_bundles")}`
        ORDER BY created_at DESC
        LIMIT 500
        """
        children: list[dict[str, Any]] = []
        for row in self._query(sql):
            bundle = dict(_json_value(row["bundle_json"]) or {})
            if str(bundle.get("parent_evidence_sha256") or "") != parent:
                continue
            children.append(
                {
                    "evidence_sha256": str(bundle.get("evidence_sha256") or ""),
                    "parent_evidence_sha256": parent,
                    "evidence_request_plan_id": str(bundle.get("evidence_request_plan_id") or ""),
                    "collection_mode": str(bundle.get("collection_mode") or ""),
                    "status": "uploaded / validated",
                    "raw_output_policy": str(bundle.get("raw_output_policy") or ""),
                    "sanitize_before_upload": bool(bundle.get("sanitize_before_upload")),
                    "verify_sanitized_required": bool(bundle.get("verify_sanitized_required")),
                }
            )
            if len(children) >= limit:
                break
        return children

    def delete_synthesis_for_evidence(self, evidence_sha256: str) -> None:
        params = [self.bigquery.ScalarQueryParameter("evidence_sha256", "STRING", evidence_sha256)]
        self._delete_ignoring_streaming_buffer(
            f"""
            DELETE FROM `{self._table("ops_synthesis", "model_comparisons")}`
            WHERE evidence_sha256 = @evidence_sha256
            """,
            params,
        )
        self._delete_ignoring_streaming_buffer(
            f"""
            DELETE FROM `{self._table("ops_synthesis", "scores")}`
            WHERE proposition_id IN (
              SELECT proposition_id
              FROM `{self._table("ops_synthesis", "propositions")}`
              WHERE evidence_sha256 = @evidence_sha256
            )
            """,
            params,
        )
        # Keep review history. BigQuery also rejects DELETEs against rows still
        # in the streaming buffer, and user_reviews is commonly written from the
        # UI immediately before a rebuild.
        for table in (
            "canonical_observation_groups",
            "proposition_clusters",
            "propositions",
            "claims",
            "model_output_artifacts",
            "parsed_results",
            "model_runs",
        ):
            sql = f"DELETE FROM `{self._table('ops_synthesis', table)}` WHERE evidence_sha256 = @evidence_sha256"
            self._delete_ignoring_streaming_buffer(sql, params)

    def insert_model_run(self, run: ModelRunRecord) -> None:
        self._insert_json_rows("ops_synthesis", "model_runs", [asdict(run)], row_ids=[run.run_id])

    def fetch_model_runs(self, evidence_sha256: str) -> list[ModelRunRecord]:
        sql = f"""
        WITH latest_bundle AS (
          SELECT created_at
          FROM `{self._table("ops_synthesis", "evidence_bundles")}`
          WHERE evidence_sha256 = @evidence_sha256
          ORDER BY created_at DESC
          LIMIT 1
        ),
        generation_gate AS (
          SELECT created_at FROM latest_bundle
          UNION ALL
          SELECT TIMESTAMP('1970-01-01') AS created_at
          FROM (SELECT 1)
          WHERE NOT EXISTS (SELECT 1 FROM latest_bundle)
        )
        SELECT r.*
        FROM `{self._table("ops_synthesis", "model_runs")}` r
        CROSS JOIN generation_gate b
        WHERE r.evidence_sha256 = @evidence_sha256
          AND r.created_at >= b.created_at
        QUALIFY ROW_NUMBER() OVER (
          PARTITION BY provider, model_name
          ORDER BY r.created_at DESC, r.run_id DESC
        ) = 1
        ORDER BY r.created_at, r.run_id
        """
        rows = self._query(
            sql,
            [self.bigquery.ScalarQueryParameter("evidence_sha256", "STRING", evidence_sha256)],
        )
        return [
            ModelRunRecord(
                run_id=str(row["run_id"]),
                evidence_sha256=str(row["evidence_sha256"]),
                prompt_sha256=str(row["prompt_sha256"]),
                model_input_sha256=str(row["model_input_sha256"]),
                provider=str(row["provider"]),
                model_name=str(row["model_name"]),
                temperature=float(row["temperature"]),
                raw_output=str(row["raw_output"]),
                raw_output_sha256=str(row["raw_output_sha256"]),
                latency_ms=int(row["latency_ms"] or 0),
                input_tokens=int(row["input_tokens"] or 0),
                output_tokens=int(row["output_tokens"] or 0),
                status=str(row["status"]),
                created_at=str(row["created_at"]),
            )
            for row in rows
        ]

    def list_latest_model_runs(self, limit: int = 20) -> list[dict[str, Any]]:
        sql = f"""
        SELECT * EXCEPT(rn)
        FROM (
          SELECT
            *,
            ROW_NUMBER() OVER (PARTITION BY provider ORDER BY created_at DESC, run_id DESC) AS rn
          FROM `{self._table("ops_synthesis", "model_runs")}`
        )
        WHERE rn = 1
        ORDER BY created_at DESC
        LIMIT @limit
        """
        rows = self._query(sql, [self.bigquery.ScalarQueryParameter("limit", "INT64", limit)])
        return [_stringify_query_row(dict(row)) for row in rows]

    def insert_parsed_result(self, result: ParsedResultRecord) -> None:
        row = asdict(result)
        row["parsed_json"] = json.dumps(result.parsed_json, ensure_ascii=False, sort_keys=True)
        row["schema_errors"] = list(result.schema_errors)
        self._insert_json_rows("ops_synthesis", "parsed_results", [row], row_ids=[result.result_id])

    def insert_model_output_artifact(self, artifact: dict[str, Any]) -> None:
        self.ensure_model_output_artifacts_table()
        row = _model_output_artifact_storage_row(artifact)
        self._insert_json_rows("ops_synthesis", "model_output_artifacts", [row], row_ids=[row["artifact_id"]])

    def list_model_output_artifacts(self, evidence_sha256: str) -> list[dict[str, Any]]:
        self.ensure_model_output_artifacts_table()
        sql = f"""
        SELECT * EXCEPT(rn)
        FROM (
          SELECT
            *,
            ROW_NUMBER() OVER (PARTITION BY artifact_id ORDER BY created_at DESC) AS rn
          FROM `{self._table("ops_synthesis", "model_output_artifacts")}`
          WHERE evidence_sha256 = @evidence_sha256
        )
        WHERE rn = 1
        ORDER BY created_at, artifact_id
        """
        rows = self._query(sql, [self.bigquery.ScalarQueryParameter("evidence_sha256", "STRING", str(evidence_sha256 or ""))])
        return [_model_output_artifact_row_to_dict(row) for row in rows]

    def fetch_parsed_results(self, evidence_sha256: str) -> list[ParsedResultRecord]:
        sql = f"""
        WITH latest_bundle AS (
          SELECT created_at
          FROM `{self._table("ops_synthesis", "evidence_bundles")}`
          WHERE evidence_sha256 = @evidence_sha256
          ORDER BY created_at DESC
          LIMIT 1
        )
        SELECT r.*
        FROM `{self._table("ops_synthesis", "parsed_results")}` r
        CROSS JOIN latest_bundle b
        WHERE r.evidence_sha256 = @evidence_sha256
          AND r.created_at >= b.created_at
        QUALIFY ROW_NUMBER() OVER (PARTITION BY result_id ORDER BY created_at DESC) = 1
        ORDER BY created_at, result_id
        """
        rows = self._query(
            sql,
            [self.bigquery.ScalarQueryParameter("evidence_sha256", "STRING", evidence_sha256)],
        )
        return [
            ParsedResultRecord(
                result_id=str(row["result_id"]),
                run_id=str(row["run_id"]),
                evidence_sha256=str(row["evidence_sha256"]),
                provider=str(row["provider"]),
                parsed_json=dict(_json_value(row["parsed_json"])),
                parsed_json_sha256=str(row["parsed_json_sha256"]),
                schema_valid=bool(row["schema_valid"]),
                schema_errors=tuple(row["schema_errors"] or ()),
                created_at=str(row["created_at"]),
            )
            for row in rows
        ]

    def insert_claims(self, claims: Iterable[ClaimRecord]) -> None:
        rows = []
        row_ids = []
        for claim in claims:
            row = asdict(claim)
            row["evidence_refs"] = list(claim.evidence_refs)
            row["counter_evidence_refs"] = list(claim.counter_evidence_refs)
            row["caveats"] = list(claim.caveats)
            row["missing_evidence"] = list(claim.missing_evidence)
            row["evidence_identity"] = json.dumps(claim.evidence_identity, ensure_ascii=False, sort_keys=True)
            rows.append(row)
            row_ids.append(claim.claim_id)
        self._insert_json_rows("ops_synthesis", "claims", rows, row_ids=row_ids)

    def fetch_claims(self, evidence_sha256: str) -> list[ClaimRecord]:
        sql = f"""
        WITH latest_bundle AS (
          SELECT created_at
          FROM `{self._table("ops_synthesis", "evidence_bundles")}`
          WHERE evidence_sha256 = @evidence_sha256
          ORDER BY created_at DESC
          LIMIT 1
        )
        SELECT c.*
        FROM `{self._table("ops_synthesis", "claims")}` c
        CROSS JOIN latest_bundle b
        WHERE c.evidence_sha256 = @evidence_sha256
          AND c.created_at >= b.created_at
        QUALIFY ROW_NUMBER() OVER (PARTITION BY claim_id ORDER BY created_at DESC) = 1
        ORDER BY created_at, claim_id
        """
        rows = self._query(
            sql,
            [self.bigquery.ScalarQueryParameter("evidence_sha256", "STRING", evidence_sha256)],
        )
        return [
            ClaimRecord(
                claim_id=str(row["claim_id"]),
                evidence_sha256=str(row["evidence_sha256"]),
                result_id=str(row["result_id"]),
                provider=str(row["provider"]),
                claim_type=str(row["claim_type"]),
                claim_text=str(row["claim_text"]),
                evidence_refs=tuple(row["evidence_refs"] or ()),
                counter_evidence_refs=tuple(row["counter_evidence_refs"] or ()),
                caveats=tuple(row["caveats"] or ()),
                missing_evidence=tuple(row["missing_evidence"] or ()),
                temporary_action=str(row["temporary_action"] or ""),
                permanent_action=str(row["permanent_action"] or ""),
                required_authority=str(row["required_authority"] or ""),
                review_status=str(row["review_status"]),
                created_at=str(row["created_at"]),
                evidence_refs_valid=bool(row["evidence_refs_valid"]),
                subsystem=str(row["subsystem"] or "general"),
                finding_status=str(_row_get(row, "finding_status", "supported") or "supported"),
                evidence_identity=dict(_json_value(_row_get(row, "evidence_identity", {})) or {}),
            )
            for row in rows
        ]

    def insert_propositions(self, propositions: Iterable[PropositionRecord]) -> None:
        rows = []
        row_ids = []
        for proposition in propositions:
            row = asdict(proposition)
            row["linked_claim_ids"] = list(proposition.linked_claim_ids)
            row["validation_targets"] = list(proposition.validation_targets)
            row["next_data_needed"] = list(proposition.next_data_needed)
            row["structured_evidence"] = json.dumps(proposition.structured_evidence, ensure_ascii=False, sort_keys=True)
            rows.append(row)
            row_ids.append(proposition.proposition_id)
        self._insert_json_rows("ops_synthesis", "propositions", rows, row_ids=row_ids)

    def fetch_propositions(self, evidence_sha256: str) -> list[PropositionRecord]:
        sql = f"""
        WITH latest_bundle AS (
          SELECT created_at
          FROM `{self._table("ops_synthesis", "evidence_bundles")}`
          WHERE evidence_sha256 = @evidence_sha256
          ORDER BY created_at DESC
          LIMIT 1
        )
        SELECT p.*
        FROM `{self._table("ops_synthesis", "propositions")}` p
        CROSS JOIN latest_bundle b
        WHERE p.evidence_sha256 = @evidence_sha256
          AND p.created_at >= b.created_at
        QUALIFY ROW_NUMBER() OVER (PARTITION BY proposition_id ORDER BY created_at DESC) = 1
        ORDER BY created_at, proposition_id
        """
        rows = self._query(
            sql,
            [self.bigquery.ScalarQueryParameter("evidence_sha256", "STRING", evidence_sha256)],
        )
        return [
            PropositionRecord(
                proposition_id=str(row["proposition_id"]),
                evidence_sha256=str(row["evidence_sha256"]),
                question=str(row["question"]),
                linked_claim_ids=tuple(row["linked_claim_ids"] or ()),
                support_summary=str(row["support_summary"] or ""),
                counter_summary=str(row["counter_summary"] or ""),
                validation_targets=tuple(row["validation_targets"] or ()),
                next_data_needed=tuple(row["next_data_needed"] or ()),
                priority=str(row["priority"]),
                review_status=str(row["review_status"]),
                created_at=str(row["created_at"]),
                subsystem=str(row["subsystem"] or "general"),
                structured_evidence=dict(_json_value(row["structured_evidence"] or {}) or {}),
            )
            for row in rows
        ]

    def insert_scores(self, scores: Iterable[ScoreRecord]) -> None:
        rows = [asdict(score) for score in scores]
        self._insert_json_rows(
            "ops_synthesis",
            "scores",
            rows,
            row_ids=[row["score_id"] for row in rows],
        )

    def insert_proposition_clusters(self, clusters: Iterable[PropositionClusterRecord]) -> None:
        rows = []
        row_ids = []
        for cluster in clusters:
            row = asdict(cluster)
            row["member_proposition_ids"] = list(cluster.member_proposition_ids)
            row["supporting_providers"] = list(cluster.supporting_providers)
            row["model_names"] = list(cluster.model_names)
            row["cluster_json"] = json.dumps(cluster.cluster_json, ensure_ascii=False, sort_keys=True)
            rows.append(row)
            row_ids.append(cluster.cluster_id)
        self._insert_json_rows("ops_synthesis", "proposition_clusters", rows, row_ids=row_ids)

    def list_proposition_clusters(
        self,
        *,
        evidence_sha256: str | None = None,
        limit: int = 50,
        include_hidden: bool = False,
    ) -> list[dict[str, Any]]:
        filters = []
        params: list[Any] = [self.bigquery.ScalarQueryParameter("limit", "INT64", limit)]
        if evidence_sha256:
            filters.append("c.evidence_sha256 = @evidence_sha256")
            params.append(self.bigquery.ScalarQueryParameter("evidence_sha256", "STRING", evidence_sha256))
        else:
            filters.append("b.incident_rn = 1")
        filters.append("c.created_at >= b.created_at")
        if not include_hidden:
            filters.append("c.review_visibility = 'review'")
        where = f"WHERE {' AND '.join(filters)}" if filters else ""
        sql = f"""
        WITH latest_bundles AS (
          SELECT * EXCEPT(evidence_rn)
          FROM (
            SELECT
              *,
              ROW_NUMBER() OVER (PARTITION BY evidence_sha256 ORDER BY created_at DESC) evidence_rn,
              ROW_NUMBER() OVER (
                PARTITION BY service, environment, window_start, window_end
                ORDER BY created_at DESC, evidence_sha256 DESC
              ) incident_rn
            FROM `{self._table("ops_synthesis", "evidence_bundles")}`
          )
          WHERE evidence_rn = 1
        )
        SELECT c.*
        FROM `{self._table("ops_synthesis", "proposition_clusters")}` c
        JOIN latest_bundles b ON b.evidence_sha256 = c.evidence_sha256
        {where}
        QUALIFY ROW_NUMBER() OVER (PARTITION BY c.cluster_id ORDER BY c.created_at DESC) = 1
        ORDER BY c.review_priority_score DESC, c.created_at DESC
        LIMIT @limit
        """
        return [_stringify_query_row(dict(row)) for row in self._query(sql, params)]

    def insert_model_comparison(self, comparison: dict[str, Any]) -> None:
        row = {
            "comparison_id": comparison["comparison_id"],
            "evidence_sha256": comparison["evidence_sha256"],
            "baseline_provider": comparison["baseline_provider"],
            "candidate_provider": comparison["candidate_provider"],
            "comparison_json": json.dumps(comparison, ensure_ascii=False, sort_keys=True),
            "created_at": comparison["created_at"],
        }
        self._insert_json_rows(
            "ops_synthesis",
            "model_comparisons",
            [row],
            row_ids=[comparison["comparison_id"]],
        )

    def list_model_comparisons(
        self,
        *,
        evidence_sha256: str | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        filters = []
        params: list[Any] = [self.bigquery.ScalarQueryParameter("limit", "INT64", limit)]
        if evidence_sha256:
            filters.append("evidence_sha256 = @evidence_sha256")
            params.append(self.bigquery.ScalarQueryParameter("evidence_sha256", "STRING", evidence_sha256))
        where = f"WHERE {' AND '.join(filters)}" if filters else ""
        sql = f"""
        SELECT comparison_json
        FROM `{self._table("ops_synthesis", "model_comparisons")}`
        {where}
        QUALIFY ROW_NUMBER() OVER (PARTITION BY comparison_id ORDER BY created_at DESC) = 1
        ORDER BY created_at DESC
        LIMIT @limit
        """
        return [dict(_json_value(row["comparison_json"])) for row in self._query(sql, params)]

    def list_review_queue(
        self,
        limit: int = 50,
        *,
        evidence_sha256: str | None = None,
    ) -> list[dict[str, Any]]:
        return self.list_proposals(limit=limit, evidence_sha256=evidence_sha256, pending_only=True)

    def list_review_targets(
        self,
        limit: int = 5,
        *,
        evidence_sha256: str | None = None,
        pending_only: bool = True,
        persist: bool = False,
    ) -> dict[str, Any]:
        raw_limit = max(1000, limit * 40)
        proposals = self.list_proposals(
            limit=raw_limit,
            evidence_sha256=evidence_sha256,
            pending_only=pending_only,
            include_hidden=True,
        )
        target_set = build_review_target_set(proposals, limit=limit)
        targets = [self._enrich_review_target(target) for target in target_set["targets"]]
        if not targets and evidence_sha256:
            targets = self._list_stored_review_targets(
                evidence_sha256=evidence_sha256,
                limit=limit,
                pending_only=pending_only,
            )
            target_set = _stored_review_target_set(targets)
        if persist:
            self.upsert_review_targets(targets)
        target_set["targets"] = targets
        target_set["summary"]["sanitized_log_count"] = self._sanitized_log_count_for_summary(
            evidence_sha256=evidence_sha256,
            targets=targets,
        )
        return target_set

    def _list_stored_review_targets(
        self,
        *,
        evidence_sha256: str,
        limit: int,
        pending_only: bool,
    ) -> list[dict[str, Any]]:
        filters = ["evidence_sha256 = @evidence_sha256"]
        params: list[Any] = [
            self.bigquery.ScalarQueryParameter("evidence_sha256", "STRING", str(evidence_sha256 or "")),
            self.bigquery.ScalarQueryParameter("limit", "INT64", int(limit)),
        ]
        if pending_only:
            filters.append("status IN ('pending', 'needs_more_data')")
        where = " AND ".join(filters)
        rows = list(
            self._query(
                f"""
                SELECT target_json, status
                FROM `{self._table("ops_synthesis", "review_targets")}`
                WHERE {where}
                QUALIFY ROW_NUMBER() OVER (PARTITION BY review_target_id ORDER BY updated_at DESC, created_at DESC) = 1
                ORDER BY review_priority_score DESC, updated_at DESC, review_target_id
                LIMIT @limit
                """,
                params,
            )
        )
        targets: list[dict[str, Any]] = []
        for row in rows:
            target = dict(_json_value(row["target_json"]) or {})
            target["status"] = str(row["status"] or target.get("status") or "pending")
            latest = self._latest_review_for_target(str(target.get("review_target_id") or ""))
            if latest:
                target["latest_review"] = latest
                target["status"] = str(latest.get("status") or target["status"])
            targets.append(target)
        return targets

    def replace_review_targets_for_evidence(self, evidence_sha256: str, targets: Iterable[dict[str, Any]]) -> None:
        evidence_id = str(evidence_sha256 or "")
        rows = [
            {**target, "evidence_sha256": str(target.get("evidence_sha256") or evidence_id)}
            for target in targets
            if isinstance(target, dict)
        ]
        if evidence_id:
            self._delete_ignoring_streaming_buffer(
                f"DELETE FROM `{self._table('ops_synthesis', 'review_targets')}` WHERE evidence_sha256 = @evidence_sha256",
                [self.bigquery.ScalarQueryParameter("evidence_sha256", "STRING", evidence_id)],
            )
        self.upsert_review_targets(rows)

    def _sanitized_log_count_for_summary(
        self,
        *,
        evidence_sha256: str | None = None,
        targets: list[dict[str, Any]] | None = None,
    ) -> int:
        evidence_id = evidence_sha256 or next(
            (
                str(target.get("evidence_sha256") or "")
                for target in targets or []
                if str(target.get("evidence_sha256") or "")
            ),
            "",
        )
        bundle = self.get_bundle(evidence_id) if evidence_id else None
        if bundle:
            rows = list(
                self._query(
                    f"""
                    SELECT COUNT(*) AS count
                    FROM `{self._table("ops_evidence_raw", "logs_sanitized")}`
                    WHERE service = @service
                      AND environment = @environment
                    """,
                    [
                        self.bigquery.ScalarQueryParameter("service", "STRING", str(bundle.get("service") or "")),
                        self.bigquery.ScalarQueryParameter("environment", "STRING", str(bundle.get("environment") or "")),
                    ],
                )
            )
            count = int(rows[0]["count"]) if rows else 0
            if count:
                return count
        return self.count_table("logs_sanitized")

    def get_review_target(self, review_target_id: str) -> dict[str, Any] | None:
        target_set = self.list_review_targets(limit=100, pending_only=False)
        for target in target_set["targets"]:
            if str(target.get("review_target_id")) == review_target_id:
                return target
        return self._fetch_stored_review_target(review_target_id)

    def upsert_review_targets(self, targets: Iterable[dict[str, Any]]) -> None:
        rows = []
        row_ids = []
        created_at = utc_now()
        for target in targets:
            support = (target.get("drawer") or {}).get("support_evidence") or []
            counter = (target.get("drawer") or {}).get("counter_evidence") or []
            caveats = (target.get("drawer") or {}).get("caveats") or []
            missing = (target.get("drawer") or {}).get("missing_evidence") or []
            rows.append(
                {
                    "review_target_id": str(target["review_target_id"]),
                    "cluster_id": str(target.get("cluster_id") or ""),
                    "evidence_sha256": str(target.get("evidence_sha256") or ""),
                    "title": str(target.get("title") or ""),
                    "subsystem": str(target.get("subsystem") or "general"),
                    "core_claim": str(target.get("core_claim") or ""),
                    "support_json": json.dumps(support, ensure_ascii=False, sort_keys=True),
                    "counter_json": json.dumps(counter, ensure_ascii=False, sort_keys=True),
                    "caveats_json": json.dumps(caveats, ensure_ascii=False, sort_keys=True),
                    "missing_evidence_json": json.dumps(missing, ensure_ascii=False, sort_keys=True),
                    "proposal": str(target.get("proposal") or ""),
                    "review_priority_score": float(target.get("review_priority_score") or 0.0),
                    "score_breakdown_json": json.dumps(
                        target.get("score_breakdown") or {},
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                    "status": str(target.get("status") or "pending"),
                    "target_json": json.dumps(target, ensure_ascii=False, sort_keys=True),
                    "created_at": created_at,
                    "updated_at": created_at,
                }
            )
            row_ids.append(f"{target['review_target_id']}-{created_at}")
        self._insert_json_rows("ops_synthesis", "review_targets", rows, row_ids=row_ids)

    def list_proposals(
        self,
        limit: int = 50,
        *,
        evidence_sha256: str | None = None,
        pending_only: bool = True,
        include_hidden: bool = False,
    ) -> list[dict[str, Any]]:
        raw_limit = max(limit * 6, limit + 50)
        rows = self._list_proposal_rows(
            limit=raw_limit,
            evidence_sha256=evidence_sha256,
            pending_only=pending_only,
        )
        proposals: list[dict[str, Any]] = []
        claims_cache: dict[str, dict[str, ClaimRecord]] = {}
        models_cache: dict[str, dict[str, str]] = {}
        for item in rows:
            linked_claim_ids = list(item["linked_claim_ids"] or [])
            evidence_id = str(item["evidence_sha256"])
            bundle = dict(_json_value(item.pop("bundle_json", {})) or {})
            profile = profile_for_bundle(bundle)
            item["profile"] = {
                "profile_id": str(profile.get("profile_id") or "generic"),
                "profile_label": str(profile.get("profile_label") or "Generic logs"),
                "source_system": str(profile.get("source_system") or item.get("environment") or ""),
            }
            item["profile_id"] = item["profile"]["profile_id"]
            claims_by_id = claims_cache.setdefault(
                evidence_id,
                {claim.claim_id: claim for claim in self.fetch_claims(evidence_id)},
            )
            model_by_provider = models_cache.setdefault(
                evidence_id,
                {run.provider: run.model_name for run in self.fetch_model_runs(evidence_id)},
            )
            linked_claims = [
                claims_by_id[claim_id]
                for claim_id in linked_claim_ids
                if claim_id in claims_by_id
            ]
            item["structured_evidence"] = build_structured_evidence(bundle, linked_claims)
            item["suggested_actions"] = [
                {
                    "claim_id": claim.claim_id,
                    "provider": claim.provider,
                    "model_name": model_by_provider.get(claim.provider, ""),
                    "claim_type": claim.claim_type,
                    "finding_status": claim.finding_status,
                    "evidence_identity": dict(claim.evidence_identity),
                    "temporary_action": claim.temporary_action,
                    "permanent_action": claim.permanent_action,
                    "required_authority": claim.required_authority,
                    "evidence_refs": list(claim.evidence_refs),
                    "caveats": list(claim.caveats),
                    "missing_evidence": list(claim.missing_evidence),
                    "evidence_refs_valid": claim.evidence_refs_valid,
                }
                for claim in linked_claims
                if (
                    claim.temporary_action
                    or claim.permanent_action
                    or claim.required_authority
                    or claim.claim_type == "insufficient_evidence"
                    or claim.finding_status in {"insufficient_evidence", "no_finding", "contradicted"}
                )
            ]
            item["evidence_refs"] = sorted(
                {
                    ref
                    for claim in linked_claims
                    for ref in (*claim.evidence_refs, *claim.counter_evidence_refs)
                }
            )
            proposals.append(item)
        return shape_review_queue(proposals, include_hidden=include_hidden, limit=limit)

    def record_review(
        self,
        proposition_id: str,
        decision: str,
        reviewer: str,
        note: str,
        *,
        decision_detail: str = "",
        resulting_status: str = "",
        generated_query: dict[str, Any] | None = None,
    ) -> str:
        import uuid

        from ops_evidence_synthesis.timeutils import utc_now

        review_id = f"review-{uuid.uuid4().hex[:16]}"
        created_at = utc_now()
        status = resulting_status or _status_for_review_decision(decision, decision_detail)
        self._insert_json_rows(
            "ops_synthesis",
            "user_reviews",
            [
                {
                    "review_id": review_id,
                    "proposition_id": proposition_id,
                    "decision": decision,
                    "reviewer": reviewer,
                    "note": note,
                    "created_at": created_at,
                    "decision_detail": decision_detail,
                    "resulting_status": status,
                    "generated_query_json": json.dumps(generated_query or {}, ensure_ascii=False, sort_keys=True),
                }
            ],
            row_ids=[review_id],
        )
        return review_id

    def record_review_target(
        self,
        review_target_id: str,
        decision: str,
        reviewer: str,
        human_note: str,
        *,
        reason: str = "",
    ) -> dict[str, Any]:
        import uuid

        target = self.get_review_target(review_target_id)
        if target is None:
            raise KeyError(f"review target not found: {review_target_id}")
        status = _status_for_review_decision(decision, reason)
        generated_query: dict[str, Any] = {}
        if decision == "needs_more_data":
            proposition_id = str(target.get("representative_proposition_id") or "")
            generated_query = (
                self.build_more_data_query(
                    proposition_id,
                    requests=(target.get("drawer") or {}).get("next_evidence_requests") or [],
                )
                if proposition_id
                else {}
            )
        review_id = f"review-{uuid.uuid4().hex[:16]}"
        created_at = utc_now()
        self._insert_json_rows(
            "ops_synthesis",
            "reviews",
            [
                {
                    "review_id": review_id,
                    "review_target_id": review_target_id,
                    "decision": decision,
                    "reason": reason,
                    "human_note": human_note,
                    "reviewer": reviewer,
                    "created_at": created_at,
                    "generated_query_json": json.dumps(generated_query or {}, ensure_ascii=False, sort_keys=True),
                }
            ],
            row_ids=[review_id],
        )
        for proposition_id in target.get("raw_proposition_ids") or []:
            self.record_review(
                str(proposition_id),
                decision,
                reviewer,
                human_note,
                decision_detail=reason,
                resulting_status=status,
                generated_query=generated_query,
            )
        response = {
            "review_id": review_id,
            "review_target_id": review_target_id,
            "decision": decision,
            "reason": reason,
            "status": status,
        }
        if decision == "needs_more_data":
            response["more_data"] = more_data_request_for_target(target, generated_query)
        return response

    def record_more_data_result(
        self,
        review_target_id: str,
        child_evidence_sha256: str,
        summary: dict[str, Any],
        *,
        reviewer: str = "system",
        human_note: str = "More-data evidence collected and rerun completed.",
    ) -> dict[str, Any]:
        import uuid

        target = self.get_review_target(review_target_id)
        if target is None:
            raise KeyError(f"review target not found: {review_target_id}")
        review_id = f"review-{uuid.uuid4().hex[:16]}"
        created_at = utc_now()
        status = "more_data_collected"
        generated_query = {
            "event": "more_data_result",
            "child_evidence_sha256": child_evidence_sha256,
            "refresh_summary": summary,
        }
        self._insert_json_rows(
            "ops_synthesis",
            "reviews",
            [
                {
                    "review_id": review_id,
                    "review_target_id": review_target_id,
                    "decision": status,
                    "reason": "evidence_collected",
                    "human_note": human_note,
                    "reviewer": reviewer,
                    "created_at": created_at,
                    "generated_query_json": json.dumps(generated_query, ensure_ascii=False, sort_keys=True),
                }
            ],
            row_ids=[review_id],
        )
        for proposition_id in target.get("raw_proposition_ids") or []:
            self.record_review(
                str(proposition_id),
                status,
                reviewer,
                human_note,
                decision_detail="evidence_collected",
                resulting_status=status,
                generated_query=generated_query,
            )
        return {
            "review_id": review_id,
            "review_target_id": review_target_id,
            "decision": status,
            "reason": "evidence_collected",
            "status": status,
            "child_evidence_sha256": child_evidence_sha256,
        }

    def build_more_data_query_for_target(
        self,
        review_target_id: str,
        *,
        request_ids: list[Any] | tuple[Any, ...] | None = None,
    ) -> dict[str, Any]:
        target = self.get_review_target(review_target_id)
        if target is None:
            return {}
        proposition_id = str(target.get("representative_proposition_id") or "")
        generated_query = (
            self.build_more_data_query(
                proposition_id,
                requests=(target.get("drawer") or {}).get("next_evidence_requests") or [],
                request_ids=request_ids,
            )
            if proposition_id
            else {}
        )
        return more_data_request_for_target(target, generated_query)

    def _enrich_review_target(self, target: dict[str, Any]) -> dict[str, Any]:
        evidence_sha256 = str(target.get("evidence_sha256") or "")
        bundle = self.get_bundle(evidence_sha256) if evidence_sha256 else None
        model_runs = self.fetch_model_runs(evidence_sha256) if evidence_sha256 else []
        parsed_results = self.fetch_parsed_results(evidence_sha256) if evidence_sha256 else []
        claims = self.fetch_claims(evidence_sha256) if evidence_sha256 else []
        claims_by_id = {claim.claim_id: claim for claim in claims}
        enriched = attach_review_target_artifacts(
            target,
            bundle=bundle,
            model_runs=model_runs,
            parsed_results=parsed_results,
            claims_by_id=claims_by_id,
        )
        latest = self._latest_review_for_target(str(enriched.get("review_target_id") or ""))
        if latest:
            enriched["status"] = str(latest.get("status") or enriched.get("status") or "pending")
            enriched["latest_review"] = latest
        return enriched

    def _latest_review_for_target(self, review_target_id: str) -> dict[str, Any] | None:
        rows = list(
            self._query(
                f"""
                SELECT *
                FROM `{self._table("ops_synthesis", "reviews")}`
                WHERE review_target_id = @review_target_id
                ORDER BY created_at DESC
                LIMIT 1
                """,
                [self.bigquery.ScalarQueryParameter("review_target_id", "STRING", review_target_id)],
            )
        )
        if not rows:
            return None
        row = dict(rows[0])
        return {
            "review_id": str(row["review_id"]),
            "review_target_id": str(row["review_target_id"]),
            "decision": str(row["decision"]),
            "reason": str(row["reason"] or ""),
            "human_note": str(row["human_note"] or ""),
            "reviewer": str(row["reviewer"] or ""),
            "created_at": _stringify_query_row({"created_at": row["created_at"]})["created_at"],
            "generated_query": dict(_json_value(row.get("generated_query_json") or {}) or {}),
            "status": _status_for_review_decision(str(row["decision"]), str(row.get("reason") or "")),
        }

    def _fetch_stored_review_target(self, review_target_id: str) -> dict[str, Any] | None:
        rows = list(
            self._query(
                f"""
                SELECT target_json, status
                FROM `{self._table("ops_synthesis", "review_targets")}`
                WHERE review_target_id = @review_target_id
                ORDER BY updated_at DESC
                LIMIT 1
                """,
                [self.bigquery.ScalarQueryParameter("review_target_id", "STRING", review_target_id)],
            )
        )
        if not rows:
            return None
        row = rows[0]
        target = dict(_json_value(row["target_json"]))
        target["status"] = str(row["status"] or target.get("status") or "pending")
        latest = self._latest_review_for_target(review_target_id)
        if latest:
            target["latest_review"] = latest
            target["status"] = str(latest.get("status") or target["status"])
        return target

    def count_table(self, table_name: str) -> int:
        allowed = {
            "logs_sanitized": ("ops_evidence_raw", "logs_sanitized"),
            "evidence_bundles": ("ops_synthesis", "evidence_bundles"),
            "model_runs": ("ops_synthesis", "model_runs"),
            "pipeline_runs": ("ops_synthesis", "pipeline_runs"),
            "pipeline_events": ("ops_synthesis", "pipeline_events"),
            "parsed_results": ("ops_synthesis", "parsed_results"),
            "model_output_artifacts": ("ops_synthesis", "model_output_artifacts"),
            "claims": ("ops_synthesis", "claims"),
            "propositions": ("ops_synthesis", "propositions"),
            "scores": ("ops_synthesis", "scores"),
            "proposition_clusters": ("ops_synthesis", "proposition_clusters"),
            "review_targets": ("ops_synthesis", "review_targets"),
            "canonical_review_graphs": ("ops_synthesis", "canonical_review_graphs"),
            "canonical_observation_groups": ("ops_synthesis", "canonical_observation_groups"),
            "model_comparisons": ("ops_synthesis", "model_comparisons"),
            "user_reviews": ("ops_synthesis", "user_reviews"),
            "reviews": ("ops_synthesis", "reviews"),
        }
        if table_name not in allowed:
            raise ValueError(f"unsupported table: {table_name}")
        dataset, table = allowed[table_name]
        rows = self._query(f"SELECT COUNT(*) AS count FROM `{self._table(dataset, table)}`")
        return int(list(rows)[0]["count"])

    def insert_json_rows(self, dataset: str, table: str, rows: list[dict[str, Any]]) -> None:
        self._insert_json_rows(dataset, table, rows)

    def _list_proposal_rows(
        self,
        *,
        limit: int,
        evidence_sha256: str | None = None,
        pending_only: bool,
    ) -> list[dict[str, Any]]:
        filters = []
        params: list[Any] = [self.bigquery.ScalarQueryParameter("limit", "INT64", limit)]
        if evidence_sha256:
            filters.append("p.evidence_sha256 = @evidence_sha256")
            params.append(self.bigquery.ScalarQueryParameter("evidence_sha256", "STRING", evidence_sha256))
        else:
            filters.append("b.incident_rn = 1")
        filters.append("p.created_at >= b.created_at")
        if pending_only:
            filters.append("COALESCE(r.resulting_status, p.review_status) IN ('pending', 'needs_more_data')")
        where = f"WHERE {' AND '.join(filters)}" if filters else ""
        sql = f"""
        WITH latest_props AS (
          SELECT * EXCEPT(rn)
          FROM (
            SELECT *, ROW_NUMBER() OVER (PARTITION BY proposition_id ORDER BY created_at DESC) rn
            FROM `{self._table("ops_synthesis", "propositions")}`
          )
          WHERE rn = 1
        ),
        latest_scores AS (
          SELECT * EXCEPT(rn)
          FROM (
            SELECT *, ROW_NUMBER() OVER (PARTITION BY proposition_id ORDER BY created_at DESC) rn
            FROM `{self._table("ops_synthesis", "scores")}`
          )
          WHERE rn = 1
        ),
        latest_bundles AS (
          SELECT * EXCEPT(evidence_rn)
          FROM (
            SELECT
              *,
              ROW_NUMBER() OVER (PARTITION BY evidence_sha256 ORDER BY created_at DESC) evidence_rn,
              ROW_NUMBER() OVER (
                PARTITION BY service, environment, window_start, window_end
                ORDER BY created_at DESC, evidence_sha256 DESC
              ) incident_rn
            FROM `{self._table("ops_synthesis", "evidence_bundles")}`
          )
          WHERE evidence_rn = 1
        ),
        latest_reviews AS (
          SELECT * EXCEPT(rn)
          FROM (
            SELECT *, ROW_NUMBER() OVER (PARTITION BY proposition_id ORDER BY created_at DESC) rn
            FROM `{self._table("ops_synthesis", "user_reviews")}`
          )
          WHERE rn = 1
        )
        SELECT
          p.* EXCEPT(review_status),
          COALESCE(r.resulting_status, p.review_status) AS review_status,
          r.decision AS latest_review_decision,
          r.decision_detail AS latest_review_detail,
          s.schema_score,
          s.evidence_ref_score,
          s.unsupported_claim_penalty,
          s.contradiction_penalty,
          s.cross_model_agreement,
          s.actionability_score,
          s.safety_score,
          s.review_priority_score,
          b.service,
          b.environment,
          CAST(b.window_start AS STRING) AS window_start,
          CAST(b.window_end AS STRING) AS window_end,
          b.bundle_json
        FROM latest_props p
        JOIN latest_bundles b ON b.evidence_sha256 = p.evidence_sha256
        LEFT JOIN latest_scores s ON s.proposition_id = p.proposition_id
        LEFT JOIN latest_reviews r ON r.proposition_id = p.proposition_id
        {where}
        ORDER BY s.review_priority_score DESC, p.created_at DESC
        LIMIT @limit
        """
        return [dict(row) for row in self._query(sql, params)]

    def build_more_data_query(
        self,
        proposition_id: str,
        *,
        limit: int = 200,
        requests: list[dict[str, Any]] | None = None,
        request_ids: list[Any] | tuple[Any, ...] | None = None,
    ) -> dict[str, Any]:
        rows = list(
            self._query(
                f"""
                SELECT
                  p.*,
                  b.bundle_json,
                  b.environment,
                  CAST(b.window_start AS STRING) AS window_start,
                  CAST(b.window_end AS STRING) AS window_end
                FROM `{self._table("ops_synthesis", "propositions")}` p
                JOIN `{self._table("ops_synthesis", "evidence_bundles")}` b
                  ON b.evidence_sha256 = p.evidence_sha256
                WHERE p.proposition_id = @proposition_id
                QUALIFY ROW_NUMBER() OVER (PARTITION BY p.proposition_id ORDER BY p.created_at DESC) = 1
                """,
                [self.bigquery.ScalarQueryParameter("proposition_id", "STRING", proposition_id)],
            )
        )
        if not rows:
            return {}
        row = rows[0]
        bundle = dict(_json_value(row["bundle_json"]))
        subsystem = str(row["subsystem"] or "general")
        baseline = bundle.get("baseline") if isinstance(bundle.get("baseline"), dict) else {}
        search_start = str(
            baseline.get("start")
            or bundle.get("lookback_window_start")
            or bundle.get("window_start")
            or row["window_start"]
        )
        window_end = str(bundle.get("window_end") or row["window_end"])
        subsystem_predicate = bigquery_predicate_for_subsystem(subsystem)
        next_data_needed = list(row["next_data_needed"] or [])
        normalized_requests = normalize_more_data_requests(next_data_needed, requests)
        normalized_requests = filter_more_data_requests(normalized_requests, request_ids)
        sql = f"""
        SELECT timestamp, service, severity, message_sanitized, message_template, error_type, labels_json, raw_log_sha256
        FROM `{self._table("ops_evidence_raw", "logs_sanitized")}`
        WHERE environment = '{_sql_literal(str(bundle["environment"]))}'
          AND timestamp >= TIMESTAMP('{_sql_literal(search_start)}')
          AND timestamp < TIMESTAMP('{_sql_literal(window_end)}')
          AND {subsystem_predicate}
        ORDER BY timestamp DESC
        LIMIT {int(limit)}
        """
        fallback_rows = [_stringify_query_row(dict(item)) for item in self._query(sql)]
        query_rows = []
        flattened_preview_rows = []
        per_request_limit = max(20, min(int(limit), 100))
        for request in normalized_requests:
            request_predicate = bigquery_text_predicate_for_request(request)
            request_sql = f"""
            SELECT timestamp, service, severity, message_sanitized, message_template, error_type, labels_json, raw_log_sha256
            FROM `{self._table("ops_evidence_raw", "logs_sanitized")}`
            WHERE environment = '{_sql_literal(str(bundle["environment"]))}'
              AND timestamp >= TIMESTAMP('{_sql_literal(search_start)}')
              AND timestamp < TIMESTAMP('{_sql_literal(window_end)}')
              AND ({subsystem_predicate})
              AND {request_predicate}
            ORDER BY timestamp DESC
            LIMIT {per_request_limit}
            """
            rows_for_request = [_stringify_query_row(dict(item)) for item in self._query(request_sql)]
            flattened_preview_rows.extend({**row_item, "request_id": request["request_id"]} for row_item in rows_for_request[:20])
            query_rows.append(
                {
                    **request,
                    "sql": request_sql.strip(),
                    "preview_count": len(rows_for_request),
                    "preview_rows": rows_for_request[:20],
                }
            )
        request_analysis = analyze_more_data_queries(query_rows)
        preview_rows = flattened_preview_rows or fallback_rows[:20]
        return {
            "engine": "bigquery",
            "proposition_id": proposition_id,
            "evidence_sha256": str(row["evidence_sha256"]),
            "subsystem": subsystem,
            "next_data_needed": next_data_needed,
            "next_evidence_requests": normalized_requests,
            "search_window": {
                "start": search_start,
                "end": window_end,
                "basis": "baseline_to_incident_end" if baseline.get("start") else "lookback_to_incident_end",
            },
            "sql": sql.strip(),
            "queries": query_rows,
            "request_analysis": request_analysis,
            "preview_count": sum(int(query.get("preview_count") or 0) for query in query_rows) if query_rows else len(fallback_rows),
            "preview_rows": preview_rows[:50],
            "fallback_preview_count": len(fallback_rows),
        }

    def _sanitized_log_row(self, log: SanitizedLog) -> dict[str, Any]:
        return {
            "timestamp": log.timestamp,
            "service": log.service,
            "environment": log.environment,
            "severity": log.severity,
            "trace_id": log.trace_id,
            "span_id": log.span_id,
            "deploy_id": log.deploy_id,
            "version": log.version,
            "message_sanitized": log.message_sanitized,
            "message_template": log.message_template,
            "error_type": log.error_type,
            "stack_hash": log.stack_hash,
            "resource_type": log.resource_type,
            "labels_json": json.dumps(log.labels_json, ensure_ascii=False, sort_keys=True),
            "raw_log_sha256": log.raw_log_sha256,
            "sanitizer_version": log.sanitizer_version,
        }

    def _row_to_sanitized_log(self, row: Any) -> SanitizedLog:
        timestamp = row["timestamp"]
        if hasattr(timestamp, "isoformat"):
            timestamp_text = timestamp.isoformat().replace("+00:00", "Z")
        else:
            timestamp_text = str(timestamp)
        return SanitizedLog(
            log_id=str(row["raw_log_sha256"]),
            timestamp=timestamp_text,
            service=str(row["service"]),
            environment=str(row["environment"]),
            severity=str(row["severity"]),
            trace_id=str(row["trace_id"] or ""),
            span_id=str(row["span_id"] or ""),
            deploy_id=str(row["deploy_id"] or ""),
            version=str(row["version"] or ""),
            message_sanitized=str(row["message_sanitized"]),
            message_template=str(row["message_template"]),
            error_type=str(row["error_type"]),
            stack_hash=str(row["stack_hash"] or ""),
            resource_type=str(row["resource_type"] or ""),
            labels_json=dict(_json_value(row["labels_json"]) or {}),
            raw_log_sha256=str(row["raw_log_sha256"]),
            sanitizer_version=str(row["sanitizer_version"]),
        )

    def _insert_json_rows(
        self,
        dataset: str,
        table: str,
        rows: list[dict[str, Any]],
        *,
        row_ids: list[str] | None = None,
    ) -> None:
        if not rows:
            return
        target = self._table(dataset, table)
        for start in range(0, len(rows), 500):
            chunk = rows[start : start + 500]
            ids = row_ids[start : start + 500] if row_ids else None
            errors = self.client.insert_rows_json(target, chunk, row_ids=ids)
            if errors:
                raise RuntimeError(f"BigQuery insert_rows_json failed for {target}: {errors}")

    def _query(self, sql: str, params: list[Any] | None = None) -> Any:
        job_config = None
        if params:
            job_config = self.bigquery.QueryJobConfig(query_parameters=params)
        timeout = float(os.environ.get("OES_BIGQUERY_QUERY_TIMEOUT_SECONDS", "45"))
        return self.client.query(sql, job_config=job_config).result(timeout=timeout)

    def _delete_ignoring_streaming_buffer(self, sql: str, params: list[Any]) -> None:
        try:
            self._query(sql, params)
        except Exception as exc:
            if "streaming buffer" in str(exc).casefold():
                return
            raise

    def _table(self, dataset: str, table: str) -> str:
        return f"{self.project_id}.{dataset}.{table}"


def _status_for_review_decision(decision: str, detail: str = "") -> str:
    if decision == "accepted":
        return detail if detail in {"known_issue", "confirmed_candidate", "watchlist"} else "confirmed_candidate"
    if decision == "rejected":
        return detail if detail in {"false_positive", "low_value", "duplicate", "not_actionable", "unsupported"} else "false_positive"
    if decision == "needs_more_data":
        return "needs_more_data"
    return decision


def _sql_literal(value: str) -> str:
    return value.replace("'", "''")


def _stringify_query_row(row: dict[str, Any]) -> dict[str, Any]:
    for key, value in list(row.items()):
        if key == "raw_output":
            row[key] = "" if value is None else str(value)
            continue
        if hasattr(value, "isoformat"):
            row[key] = value.isoformat().replace("+00:00", "Z")
        else:
            row[key] = _json_value(value)
    return row
