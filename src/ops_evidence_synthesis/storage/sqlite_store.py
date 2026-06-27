from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Any, Iterable

from ops_evidence_synthesis.canonical import sha256_json
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
from ops_evidence_synthesis.synthesis.review_quality import shape_review_queue
from ops_evidence_synthesis.synthesis.more_data import (
    analyze_more_data_queries,
    filter_more_data_requests,
    normalize_more_data_requests,
    sqlite_text_predicate_for_request,
)
from ops_evidence_synthesis.synthesis.review_targets import (
    attach_review_target_artifacts,
    build_review_target_set,
    more_data_request_for_target,
)
from ops_evidence_synthesis.profiles import profile_for_bundle
from ops_evidence_synthesis.synthesis.structured_evidence import build_structured_evidence
from ops_evidence_synthesis.timeutils import utc_now

DEFAULT_DB_PATH = Path("workspace/ops_evidence_synthesis.sqlite3")


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS logs_sanitized (
  log_id TEXT PRIMARY KEY,
  timestamp TEXT NOT NULL,
  service TEXT NOT NULL,
  environment TEXT NOT NULL,
  severity TEXT NOT NULL,
  trace_id TEXT,
  span_id TEXT,
  deploy_id TEXT,
  version TEXT,
  message_sanitized TEXT NOT NULL,
  message_template TEXT NOT NULL,
  error_type TEXT NOT NULL,
  stack_hash TEXT,
  resource_type TEXT,
  labels_json TEXT NOT NULL,
  raw_log_sha256 TEXT NOT NULL,
  sanitizer_version TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS logs_sanitized_lookup
  ON logs_sanitized(service, environment, timestamp, severity);

CREATE TABLE IF NOT EXISTS evidence_bundles (
  evidence_sha256 TEXT PRIMARY KEY,
  schema_version TEXT NOT NULL,
  service TEXT NOT NULL,
  environment TEXT NOT NULL,
  window_start TEXT NOT NULL,
  window_end TEXT NOT NULL,
  query_sql_hash TEXT NOT NULL,
  sanitizer_version TEXT NOT NULL,
  bundle_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS evidence_bundles_lookup
  ON evidence_bundles(service, environment, window_start, window_end);

CREATE TABLE IF NOT EXISTS model_runs (
  run_id TEXT PRIMARY KEY,
  evidence_sha256 TEXT NOT NULL,
  prompt_sha256 TEXT NOT NULL,
  model_input_sha256 TEXT NOT NULL,
  provider TEXT NOT NULL,
  model_name TEXT NOT NULL,
  temperature REAL NOT NULL,
  raw_output TEXT NOT NULL,
  raw_output_sha256 TEXT NOT NULL,
  latency_ms INTEGER NOT NULL,
  input_tokens INTEGER NOT NULL,
  output_tokens INTEGER NOT NULL,
  status TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS model_runs_evidence
  ON model_runs(evidence_sha256, provider);

CREATE TABLE IF NOT EXISTS parsed_results (
  result_id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL,
  evidence_sha256 TEXT NOT NULL,
  provider TEXT NOT NULL,
  parsed_json TEXT NOT NULL,
  parsed_json_sha256 TEXT NOT NULL,
  schema_valid INTEGER NOT NULL,
  schema_errors TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS parsed_results_evidence
  ON parsed_results(evidence_sha256, provider);

CREATE TABLE IF NOT EXISTS model_output_artifacts (
  artifact_id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL,
  evidence_sha256 TEXT NOT NULL,
  provider TEXT NOT NULL,
  model_name TEXT NOT NULL,
  raw_output_sha256 TEXT NOT NULL,
  repaired_output_sha256 TEXT NOT NULL,
  parsed_json_sha256 TEXT NOT NULL,
  parse_status TEXT NOT NULL,
  repair_applied INTEGER NOT NULL,
  repair_rules_json TEXT NOT NULL,
  schema_valid INTEGER NOT NULL,
  schema_errors_json TEXT NOT NULL,
  original_preserved INTEGER NOT NULL,
  artifact_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS model_output_artifacts_lookup
  ON model_output_artifacts(evidence_sha256, provider, created_at);

CREATE TABLE IF NOT EXISTS claims (
  claim_id TEXT PRIMARY KEY,
  evidence_sha256 TEXT NOT NULL,
  result_id TEXT NOT NULL,
  provider TEXT NOT NULL,
  claim_type TEXT NOT NULL,
  claim_text TEXT NOT NULL,
  evidence_refs TEXT NOT NULL,
  counter_evidence_refs TEXT NOT NULL,
  caveats TEXT NOT NULL,
  missing_evidence TEXT NOT NULL,
  temporary_action TEXT,
  permanent_action TEXT,
  required_authority TEXT,
  review_status TEXT NOT NULL,
  created_at TEXT NOT NULL,
  evidence_refs_valid INTEGER NOT NULL,
  subsystem TEXT NOT NULL DEFAULT 'general',
  finding_status TEXT NOT NULL DEFAULT 'supported',
  evidence_identity TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS claims_evidence
  ON claims(evidence_sha256, claim_type, provider);

CREATE TABLE IF NOT EXISTS propositions (
  proposition_id TEXT PRIMARY KEY,
  evidence_sha256 TEXT NOT NULL,
  question TEXT NOT NULL,
  linked_claim_ids TEXT NOT NULL,
  support_summary TEXT NOT NULL,
  counter_summary TEXT NOT NULL,
  validation_targets TEXT NOT NULL,
  next_data_needed TEXT NOT NULL,
  priority TEXT NOT NULL,
  review_status TEXT NOT NULL,
  created_at TEXT NOT NULL,
  subsystem TEXT NOT NULL DEFAULT 'general',
  structured_evidence TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS propositions_review
  ON propositions(review_status, priority, created_at);

CREATE TABLE IF NOT EXISTS scores (
  score_id TEXT PRIMARY KEY,
  proposition_id TEXT NOT NULL,
  schema_score REAL NOT NULL,
  evidence_ref_score REAL NOT NULL,
  unsupported_claim_penalty REAL NOT NULL,
  contradiction_penalty REAL NOT NULL,
  cross_model_agreement REAL NOT NULL,
  actionability_score REAL NOT NULL,
  safety_score REAL NOT NULL,
  review_priority_score REAL NOT NULL,
  created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS scores_prop
  ON scores(proposition_id, created_at);

CREATE TABLE IF NOT EXISTS proposition_clusters (
  cluster_id TEXT PRIMARY KEY,
  evidence_sha256 TEXT NOT NULL,
  subsystem TEXT NOT NULL,
  claim_signature TEXT NOT NULL,
  representative_proposition_id TEXT NOT NULL,
  member_proposition_ids TEXT NOT NULL,
  supporting_providers TEXT NOT NULL,
  model_names TEXT NOT NULL,
  core_claim TEXT NOT NULL,
  disagreement_summary TEXT NOT NULL,
  review_status TEXT NOT NULL,
  review_visibility TEXT NOT NULL,
  review_priority_score REAL NOT NULL,
  cluster_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS proposition_clusters_lookup
  ON proposition_clusters(evidence_sha256, review_visibility, review_priority_score);

CREATE TABLE IF NOT EXISTS review_targets (
  review_target_id TEXT PRIMARY KEY,
  cluster_id TEXT NOT NULL,
  evidence_sha256 TEXT NOT NULL,
  title TEXT NOT NULL,
  subsystem TEXT NOT NULL,
  core_claim TEXT NOT NULL,
  support_json TEXT NOT NULL,
  counter_json TEXT NOT NULL,
  caveats_json TEXT NOT NULL,
  missing_evidence_json TEXT NOT NULL,
  proposal TEXT NOT NULL,
  review_priority_score REAL NOT NULL,
  score_breakdown_json TEXT NOT NULL,
  status TEXT NOT NULL,
  target_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS review_targets_lookup
  ON review_targets(evidence_sha256, status, review_priority_score);

CREATE TABLE IF NOT EXISTS canonical_review_graphs (
  snapshot_id TEXT PRIMARY KEY,
  evidence_sha256 TEXT NOT NULL,
  canonical_graph_sha256 TEXT NOT NULL,
  schema_version TEXT NOT NULL,
  arbitration_version TEXT NOT NULL,
  input_fingerprint_sha256 TEXT NOT NULL,
  input_fingerprint_json TEXT NOT NULL,
  finding_title TEXT NOT NULL,
  finding_impact TEXT NOT NULL,
  primary_count INTEGER NOT NULL,
  validation_count INTEGER NOT NULL,
  monitor_only_count INTEGER NOT NULL,
  auto_archived_count INTEGER NOT NULL,
  promotion_decision_count INTEGER NOT NULL,
  created_at TEXT NOT NULL,
  created_by TEXT NOT NULL,
  snapshot_status TEXT NOT NULL,
  canonical_review_graph_json TEXT NOT NULL,
  UNIQUE(evidence_sha256, input_fingerprint_sha256, canonical_graph_sha256)
);

CREATE INDEX IF NOT EXISTS canonical_review_graphs_lookup
  ON canonical_review_graphs(evidence_sha256, created_at);

CREATE TABLE IF NOT EXISTS canonical_observation_groups (
  group_id TEXT PRIMARY KEY,
  evidence_sha256 TEXT NOT NULL,
  canonical_group_key TEXT NOT NULL,
  canonical_target_type TEXT NOT NULL,
  canonical_subject TEXT NOT NULL,
  subsystem TEXT NOT NULL,
  component TEXT NOT NULL,
  source_target_ids_json TEXT NOT NULL,
  source_candidate_count INTEGER NOT NULL,
  providers_json TEXT NOT NULL,
  provider_count INTEGER NOT NULL,
  evidence_refs_json TEXT NOT NULL,
  missing_evidence_json TEXT NOT NULL,
  caveats_json TEXT NOT NULL,
  support_evidence_json TEXT NOT NULL,
  counter_evidence_json TEXT NOT NULL,
  review_priority_score REAL NOT NULL,
  consensus_class TEXT NOT NULL,
  group_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS canonical_observation_groups_lookup
  ON canonical_observation_groups(evidence_sha256, canonical_group_key, review_priority_score);

CREATE TABLE IF NOT EXISTS model_comparisons (
  comparison_id TEXT PRIMARY KEY,
  evidence_sha256 TEXT NOT NULL,
  baseline_provider TEXT NOT NULL,
  candidate_provider TEXT NOT NULL,
  comparison_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS model_comparisons_lookup
  ON model_comparisons(evidence_sha256, baseline_provider, candidate_provider, created_at);

CREATE TABLE IF NOT EXISTS pipeline_runs (
  pipeline_run_id TEXT PRIMARY KEY,
  evidence_sha256 TEXT NOT NULL,
  parent_pipeline_run_id TEXT NOT NULL DEFAULT '',
  operation TEXT NOT NULL,
  status TEXT NOT NULL,
  current_step TEXT NOT NULL,
  total_steps INTEGER NOT NULL,
  completed_steps INTEGER NOT NULL,
  blocking_reason TEXT NOT NULL DEFAULT '',
  provider_total INTEGER NOT NULL DEFAULT 0,
  provider_success INTEGER NOT NULL DEFAULT 0,
  provider_failed INTEGER NOT NULL DEFAULT 0,
  provider_skipped INTEGER NOT NULL DEFAULT 0,
  review_target_count INTEGER NOT NULL DEFAULT 0,
  validation_target_count INTEGER NOT NULL DEFAULT 0,
  child_bundle_count INTEGER NOT NULL DEFAULT 0,
  summary_json TEXT NOT NULL,
  error_message TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  completed_at TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS pipeline_runs_lookup
  ON pipeline_runs(evidence_sha256, updated_at);

CREATE TABLE IF NOT EXISTS pipeline_events (
  event_id TEXT PRIMARY KEY,
  pipeline_run_id TEXT NOT NULL,
  evidence_sha256 TEXT NOT NULL,
  operation TEXT NOT NULL,
  event_type TEXT NOT NULL DEFAULT '',
  stage TEXT NOT NULL DEFAULT '',
  step_key TEXT NOT NULL,
  step_label TEXT NOT NULL,
  status TEXT NOT NULL,
  provider_id TEXT NOT NULL DEFAULT '',
  artifact_id TEXT NOT NULL DEFAULT '',
  input_sha256 TEXT NOT NULL DEFAULT '',
  output_sha256 TEXT NOT NULL DEFAULT '',
  reason_code TEXT NOT NULL DEFAULT '',
  message TEXT NOT NULL,
  ordinal INTEGER NOT NULL,
  metadata_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS pipeline_events_lookup
  ON pipeline_events(pipeline_run_id, ordinal, created_at);

CREATE TABLE IF NOT EXISTS user_reviews (
  review_id TEXT PRIMARY KEY,
  proposition_id TEXT NOT NULL,
  decision TEXT NOT NULL,
  reviewer TEXT NOT NULL,
  note TEXT NOT NULL,
  created_at TEXT NOT NULL,
  decision_detail TEXT NOT NULL DEFAULT '',
  resulting_status TEXT NOT NULL DEFAULT '',
  generated_query_json TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS user_reviews_prop
  ON user_reviews(proposition_id, created_at);

CREATE TABLE IF NOT EXISTS reviews (
  review_id TEXT PRIMARY KEY,
  review_target_id TEXT NOT NULL,
  decision TEXT NOT NULL,
  reason TEXT NOT NULL,
  human_note TEXT NOT NULL,
  reviewer TEXT NOT NULL,
  created_at TEXT NOT NULL,
  generated_query_json TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS reviews_target
  ON reviews(review_target_id, created_at);
"""


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _json_load(value: str) -> Any:
    return json.loads(value)



def _snapshot_storage_row(snapshot: dict[str, Any], *, snapshot_id: str) -> dict[str, Any]:
    return {
        "snapshot_id": snapshot_id,
        "evidence_sha256": str(snapshot.get("evidence_sha256") or ""),
        "canonical_graph_sha256": str(snapshot.get("canonical_graph_sha256") or ""),
        "schema_version": str(snapshot.get("schema_version") or ""),
        "arbitration_version": str(snapshot.get("arbitration_version") or ""),
        "input_fingerprint_sha256": str(snapshot.get("input_fingerprint_sha256") or ""),
        "input_fingerprint_json": _json(snapshot.get("input_fingerprint_json") or {}),
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
        "canonical_review_graph_json": _json(snapshot.get("canonical_review_graph_json") or {}),
    }


def _snapshot_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "snapshot_id": str(row["snapshot_id"]),
        "evidence_sha256": str(row["evidence_sha256"]),
        "canonical_graph_sha256": str(row["canonical_graph_sha256"]),
        "schema_version": str(row["schema_version"]),
        "arbitration_version": str(row["arbitration_version"]),
        "input_fingerprint_sha256": str(row["input_fingerprint_sha256"]),
        "input_fingerprint_json": dict(_json_load(str(row["input_fingerprint_json"]))),
        "finding_title": str(row["finding_title"]),
        "finding_impact": str(row["finding_impact"]),
        "primary_count": int(row["primary_count"]),
        "validation_count": int(row["validation_count"]),
        "monitor_only_count": int(row["monitor_only_count"]),
        "auto_archived_count": int(row["auto_archived_count"]),
        "promotion_decision_count": int(row["promotion_decision_count"]),
        "created_at": str(row["created_at"]),
        "created_by": str(row["created_by"]),
        "snapshot_status": str(row["snapshot_status"]),
        "canonical_review_graph_json": dict(_json_load(str(row["canonical_review_graph_json"]))),
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
        "summary_json": _json(run.get("summary") or {}),
        "error_message": str(run.get("error_message") or ""),
        "created_at": str(run.get("created_at") or utc_now()),
        "updated_at": str(run.get("updated_at") or utc_now()),
        "completed_at": str(run.get("completed_at") or ""),
    }


def _pipeline_run_row_to_dict(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    return {
        "pipeline_run_id": str(row["pipeline_run_id"]),
        "evidence_sha256": str(row["evidence_sha256"]),
        "parent_pipeline_run_id": str(row["parent_pipeline_run_id"] or ""),
        "operation": str(row["operation"]),
        "status": str(row["status"]),
        "current_step": str(row["current_step"]),
        "total_steps": int(row["total_steps"] or 0),
        "completed_steps": int(row["completed_steps"] or 0),
        "blocking_reason": str(row["blocking_reason"] or ""),
        "provider_total": int(row["provider_total"] or 0),
        "provider_success": int(row["provider_success"] or 0),
        "provider_failed": int(row["provider_failed"] or 0),
        "provider_skipped": int(row["provider_skipped"] or 0),
        "review_target_count": int(row["review_target_count"] or 0),
        "validation_target_count": int(row["validation_target_count"] or 0),
        "child_bundle_count": int(row["child_bundle_count"] or 0),
        "summary": dict(_json_load(str(row["summary_json"] or "{}"))),
        "error_message": str(row["error_message"] or ""),
        "created_at": str(row["created_at"] or ""),
        "updated_at": str(row["updated_at"] or ""),
        "completed_at": str(row["completed_at"] or ""),
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
        "metadata_json": _json(event.get("metadata") or {}),
        "created_at": str(event.get("created_at") or utc_now()),
    }


def _pipeline_event_row_to_dict(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    return {
        "event_id": str(row["event_id"]),
        "pipeline_run_id": str(row["pipeline_run_id"]),
        "evidence_sha256": str(row["evidence_sha256"]),
        "operation": str(row["operation"]),
        "event_type": str(row["event_type"] or ""),
        "stage": str(row["stage"] or ""),
        "step_key": str(row["step_key"]),
        "step_label": str(row["step_label"]),
        "status": str(row["status"]),
        "provider_id": str(row["provider_id"] or ""),
        "artifact_id": str(row["artifact_id"] or ""),
        "input_sha256": str(row["input_sha256"] or ""),
        "output_sha256": str(row["output_sha256"] or ""),
        "reason_code": str(row["reason_code"] or ""),
        "message": str(row["message"] or ""),
        "ordinal": int(row["ordinal"] or 0),
        "metadata": dict(_json_load(str(row["metadata_json"] or "{}"))),
        "created_at": str(row["created_at"] or ""),
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
        "repair_applied": 1 if artifact.get("repair_applied") else 0,
        "repair_rules_json": _json(artifact.get("repair_rules") or []),
        "schema_valid": 1 if artifact.get("schema_valid") else 0,
        "schema_errors_json": _json(artifact.get("schema_errors") or []),
        "original_preserved": 1 if artifact.get("original_preserved") else 0,
        "artifact_json": _json(artifact),
        "created_at": str(artifact.get("created_at") or utc_now()),
    }


def _model_output_artifact_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    artifact = dict(_json_load(str(row["artifact_json"])))
    artifact.update(
        {
            "artifact_id": str(row["artifact_id"]),
            "run_id": str(row["run_id"]),
            "evidence_sha256": str(row["evidence_sha256"]),
            "provider": str(row["provider"]),
            "model_name": str(row["model_name"]),
            "raw_output_sha256": str(row["raw_output_sha256"]),
            "repaired_output_sha256": str(row["repaired_output_sha256"]),
            "parsed_json_sha256": str(row["parsed_json_sha256"]),
            "parse_status": str(row["parse_status"]),
            "repair_applied": bool(row["repair_applied"]),
            "repair_rules": list(_json_load(str(row["repair_rules_json"]))),
            "schema_valid": bool(row["schema_valid"]),
            "schema_errors": list(_json_load(str(row["schema_errors_json"]))),
            "original_preserved": bool(row["original_preserved"]),
            "created_at": str(row["created_at"]),
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
        "source_target_ids_json": _json(group.get("source_target_ids") or []),
        "source_candidate_count": int(group.get("source_candidate_count") or len(group.get("source_target_ids") or []) or 1),
        "providers_json": _json(group.get("providers") or []),
        "provider_count": int(group.get("provider_count") or 0),
        "evidence_refs_json": _json(group.get("evidence_refs") or []),
        "missing_evidence_json": _json(group.get("missing_evidence") or []),
        "caveats_json": _json(group.get("caveats") or []),
        "support_evidence_json": _json(group.get("support_evidence") or []),
        "counter_evidence_json": _json(group.get("counter_evidence") or []),
        "review_priority_score": float(group.get("review_priority_score") or 0.0),
        "consensus_class": str(group.get("consensus_class") or ""),
        "group_json": _json(group),
        "created_at": str(group.get("created_at") or utc_now()),
    }


def _observation_group_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    group = dict(_json_load(str(row["group_json"])))
    group.update(
        {
            "group_id": str(row["group_id"]),
            "evidence_sha256": str(row["evidence_sha256"]),
            "canonical_group_key": str(row["canonical_group_key"]),
            "canonical_target_type": str(row["canonical_target_type"]),
            "canonical_subject": str(row["canonical_subject"]),
            "subsystem": str(row["subsystem"]),
            "component": str(row["component"]),
            "source_target_ids": list(_json_load(str(row["source_target_ids_json"]))),
            "source_candidate_count": int(row["source_candidate_count"]),
            "providers": list(_json_load(str(row["providers_json"]))),
            "provider_count": int(row["provider_count"]),
            "evidence_refs": list(_json_load(str(row["evidence_refs_json"]))),
            "missing_evidence": list(_json_load(str(row["missing_evidence_json"]))),
            "caveats": list(_json_load(str(row["caveats_json"]))),
            "support_evidence": list(_json_load(str(row["support_evidence_json"]))),
            "counter_evidence": list(_json_load(str(row["counter_evidence_json"]))),
            "review_priority_score": float(row["review_priority_score"]),
            "consensus_class": str(row["consensus_class"]),
            "created_at": str(row["created_at"]),
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


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, column_type: str) -> None:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    if any(str(row["name"]) == column for row in rows):
        return
    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {column_type}")


class SQLiteStore:
    def __init__(self, db_path: str | Path = DEFAULT_DB_PATH) -> None:
        self.db_path = Path(db_path)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        last_exc: sqlite3.OperationalError | None = None
        for attempt in range(8):
            try:
                conn = sqlite3.connect(self.db_path, timeout=5.0)
                break
            except sqlite3.OperationalError as exc:
                last_exc = exc
                if attempt < 7:
                    time.sleep(0.1 * (attempt + 1))
        else:
            try:
                fd_count = len(list(Path("/proc/self/fd").iterdir()))
            except Exception:
                fd_count = -1
            raise sqlite3.OperationalError(
                f"unable to open SQLite database at {self.db_path}: {last_exc}; open_fd_count={fd_count}"
            ) from last_exc
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 5000")
        conn.execute("PRAGMA journal_mode = WAL")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def init_schema(self) -> None:
        with self.connect() as conn:
            conn.executescript(SCHEMA_SQL)
            _ensure_column(conn, "claims", "subsystem", "TEXT NOT NULL DEFAULT 'general'")
            _ensure_column(conn, "claims", "finding_status", "TEXT NOT NULL DEFAULT 'supported'")
            _ensure_column(conn, "claims", "evidence_identity", "TEXT NOT NULL DEFAULT '{}'")
            _ensure_column(conn, "propositions", "subsystem", "TEXT NOT NULL DEFAULT 'general'")
            _ensure_column(conn, "propositions", "structured_evidence", "TEXT NOT NULL DEFAULT '{}'")
            _ensure_column(conn, "user_reviews", "decision_detail", "TEXT NOT NULL DEFAULT ''")
            _ensure_column(conn, "user_reviews", "resulting_status", "TEXT NOT NULL DEFAULT ''")
            _ensure_column(conn, "user_reviews", "generated_query_json", "TEXT NOT NULL DEFAULT '{}'")
            _ensure_column(conn, "pipeline_runs", "parent_pipeline_run_id", "TEXT NOT NULL DEFAULT ''")
            _ensure_column(conn, "pipeline_runs", "blocking_reason", "TEXT NOT NULL DEFAULT ''")
            _ensure_column(conn, "pipeline_runs", "provider_total", "INTEGER NOT NULL DEFAULT 0")
            _ensure_column(conn, "pipeline_runs", "provider_success", "INTEGER NOT NULL DEFAULT 0")
            _ensure_column(conn, "pipeline_runs", "provider_failed", "INTEGER NOT NULL DEFAULT 0")
            _ensure_column(conn, "pipeline_runs", "provider_skipped", "INTEGER NOT NULL DEFAULT 0")
            _ensure_column(conn, "pipeline_runs", "review_target_count", "INTEGER NOT NULL DEFAULT 0")
            _ensure_column(conn, "pipeline_runs", "validation_target_count", "INTEGER NOT NULL DEFAULT 0")
            _ensure_column(conn, "pipeline_runs", "child_bundle_count", "INTEGER NOT NULL DEFAULT 0")
            _ensure_column(conn, "pipeline_events", "event_type", "TEXT NOT NULL DEFAULT ''")
            _ensure_column(conn, "pipeline_events", "stage", "TEXT NOT NULL DEFAULT ''")
            _ensure_column(conn, "pipeline_events", "provider_id", "TEXT NOT NULL DEFAULT ''")
            _ensure_column(conn, "pipeline_events", "artifact_id", "TEXT NOT NULL DEFAULT ''")
            _ensure_column(conn, "pipeline_events", "input_sha256", "TEXT NOT NULL DEFAULT ''")
            _ensure_column(conn, "pipeline_events", "output_sha256", "TEXT NOT NULL DEFAULT ''")
            _ensure_column(conn, "pipeline_events", "reason_code", "TEXT NOT NULL DEFAULT ''")
            conn.commit()

    def insert_sanitized_logs(self, logs: Iterable[SanitizedLog]) -> int:
        rows = list(logs)
        if not rows:
            return 0
        self.init_schema()
        with self.connect() as conn:
            conn.executemany(
                """
                INSERT OR REPLACE INTO logs_sanitized (
                  log_id, timestamp, service, environment, severity, trace_id, span_id,
                  deploy_id, version, message_sanitized, message_template, error_type,
                  stack_hash, resource_type, labels_json, raw_log_sha256, sanitizer_version
                ) VALUES (
                  :log_id, :timestamp, :service, :environment, :severity, :trace_id, :span_id,
                  :deploy_id, :version, :message_sanitized, :message_template, :error_type,
                  :stack_hash, :resource_type, :labels_json, :raw_log_sha256, :sanitizer_version
                )
                """,
                [
                    {
                        **asdict(row),
                        "labels_json": _json(row.labels_json),
                    }
                    for row in rows
                ],
            )
            conn.commit()
        return len(rows)

    def fetch_logs(
        self,
        service: str,
        environment: str,
        start: str,
        end: str,
        *,
        min_severity: str | None = None,
    ) -> list[SanitizedLog]:
        self.init_schema()
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM logs_sanitized
                WHERE service = ?
                  AND environment = ?
                  AND timestamp >= ?
                  AND timestamp < ?
                ORDER BY timestamp, log_id
                """,
                (service, environment, start, end),
            ).fetchall()
        min_rank = severity_rank(min_severity) if min_severity else None
        logs: list[SanitizedLog] = []
        for row in rows:
            if min_rank is not None and severity_rank(str(row["severity"])) < min_rank:
                continue
            logs.append(
                SanitizedLog(
                    log_id=str(row["log_id"]),
                    timestamp=str(row["timestamp"]),
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
                    labels_json=dict(_json_load(str(row["labels_json"]))),
                    raw_log_sha256=str(row["raw_log_sha256"]),
                    sanitizer_version=str(row["sanitizer_version"]),
                )
            )
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
        """Return SQL-compressed log pattern summaries for model context."""
        self.init_schema()
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
        with self.connect() as conn:
            rows = conn.execute(
                f"""
                WITH current_patterns AS (
                  SELECT
                    message_template,
                    error_type,
                    COUNT(*) AS count,
                    MIN(timestamp) AS first_seen,
                    MAX(timestamp) AS last_seen,
                    MAX({severity_case}) AS max_severity_rank,
                    MIN(raw_log_sha256) AS example_log_sha256
                  FROM logs_sanitized
                  WHERE service = ?
                    AND environment = ?
                    AND timestamp >= ?
                    AND timestamp < ?
                  GROUP BY message_template, error_type
                ),
                baseline_patterns AS (
                  SELECT
                    message_template,
                    error_type,
                    COUNT(*) AS baseline_count
                  FROM logs_sanitized
                  WHERE service = ?
                    AND environment = ?
                    AND timestamp >= ?
                    AND timestamp < ?
                  GROUP BY message_template, error_type
                )
                SELECT
                  current_patterns.message_template,
                  current_patterns.error_type,
                  current_patterns.count,
                  current_patterns.first_seen,
                  current_patterns.last_seen,
                  current_patterns.max_severity_rank,
                  current_patterns.example_log_sha256,
                  COALESCE(baseline_patterns.baseline_count, 0) AS baseline_count
                FROM current_patterns
                LEFT JOIN baseline_patterns
                  ON baseline_patterns.message_template = current_patterns.message_template
                 AND baseline_patterns.error_type = current_patterns.error_type
                ORDER BY current_patterns.count DESC,
                         current_patterns.message_template,
                         current_patterns.error_type
                LIMIT ?
                """,
                (
                    service,
                    environment,
                    start,
                    end,
                    service,
                    environment,
                    baseline_start,
                    baseline_end,
                    int(limit),
                ),
            ).fetchall()
        return [
            {
                "message_template": str(row["message_template"] or ""),
                "error_type": str(row["error_type"] or ""),
                "count": int(row["count"] or 0),
                "baseline_count": int(row["baseline_count"] or 0),
                "first_seen": str(row["first_seen"] or ""),
                "last_seen": str(row["last_seen"] or ""),
                "max_severity": _severity_for_rank(int(row["max_severity_rank"] or 0)),
                "example_log_sha256": str(row["example_log_sha256"] or ""),
                "aggregation_source": "sqlite_group_by",
            }
            for row in rows
        ]

    def insert_bundle(self, bundle: dict[str, Any]) -> None:
        row = _bundle_storage_row(bundle)
        self.init_schema()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO evidence_bundles (
                  evidence_sha256, schema_version, service, environment, window_start,
                  window_end, query_sql_hash, sanitizer_version, bundle_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row["evidence_sha256"],
                    row["schema_version"],
                    row["service"],
                    row["environment"],
                    row["window_start"],
                    row["window_end"],
                    row["query_sql_hash"],
                    row["sanitizer_version"],
                    _json(bundle),
                    row["created_at"],
                ),
            )
            conn.commit()

    def get_bundle(self, evidence_sha256: str) -> dict[str, Any] | None:
        self.init_schema()
        with self.connect() as conn:
            row = conn.execute(
                "SELECT bundle_json FROM evidence_bundles WHERE evidence_sha256 = ?",
                (evidence_sha256,),
            ).fetchone()
        if row is None:
            return None
        return dict(_json_load(str(row["bundle_json"])))

    def upsert_pipeline_run(self, run: dict[str, Any]) -> None:
        row = _pipeline_run_storage_row(run)
        if not row["pipeline_run_id"]:
            return
        self.init_schema()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO pipeline_runs (
                  pipeline_run_id, evidence_sha256, parent_pipeline_run_id,
                  operation, status, current_step, total_steps, completed_steps,
                  blocking_reason, provider_total, provider_success, provider_failed,
                  provider_skipped, review_target_count, validation_target_count,
                  child_bundle_count, summary_json, error_message,
                  created_at, updated_at, completed_at
                ) VALUES (
                  :pipeline_run_id, :evidence_sha256, :parent_pipeline_run_id,
                  :operation, :status, :current_step, :total_steps, :completed_steps,
                  :blocking_reason, :provider_total, :provider_success, :provider_failed,
                  :provider_skipped, :review_target_count, :validation_target_count,
                  :child_bundle_count, :summary_json, :error_message,
                  :created_at, :updated_at, :completed_at
                )
                ON CONFLICT(pipeline_run_id) DO UPDATE SET
                  evidence_sha256 = excluded.evidence_sha256,
                  parent_pipeline_run_id = excluded.parent_pipeline_run_id,
                  operation = excluded.operation,
                  status = excluded.status,
                  current_step = excluded.current_step,
                  total_steps = excluded.total_steps,
                  completed_steps = excluded.completed_steps,
                  blocking_reason = excluded.blocking_reason,
                  provider_total = excluded.provider_total,
                  provider_success = excluded.provider_success,
                  provider_failed = excluded.provider_failed,
                  provider_skipped = excluded.provider_skipped,
                  review_target_count = excluded.review_target_count,
                  validation_target_count = excluded.validation_target_count,
                  child_bundle_count = excluded.child_bundle_count,
                  summary_json = excluded.summary_json,
                  error_message = excluded.error_message,
                  updated_at = excluded.updated_at,
                  completed_at = excluded.completed_at
                """,
                row,
            )
            conn.commit()

    def insert_pipeline_event(self, event: dict[str, Any]) -> None:
        row = _pipeline_event_storage_row(event)
        if not row["event_id"] or not row["pipeline_run_id"]:
            return
        self.init_schema()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO pipeline_events (
                  event_id, pipeline_run_id, evidence_sha256, operation, event_type,
                  stage, step_key, step_label, status, provider_id, artifact_id,
                  input_sha256, output_sha256, reason_code, message, ordinal,
                  metadata_json, created_at
                ) VALUES (
                  :event_id, :pipeline_run_id, :evidence_sha256, :operation, :event_type,
                  :stage, :step_key, :step_label, :status, :provider_id, :artifact_id,
                  :input_sha256, :output_sha256, :reason_code, :message, :ordinal,
                  :metadata_json, :created_at
                )
                """,
                row,
            )
            conn.commit()

    def get_pipeline_run(self, pipeline_run_id: str) -> dict[str, Any] | None:
        self.init_schema()
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM pipeline_runs WHERE pipeline_run_id = ?",
                (str(pipeline_run_id or ""),),
            ).fetchone()
        return _pipeline_run_row_to_dict(row) if row is not None else None

    def latest_pipeline_run(self, evidence_sha256: str) -> dict[str, Any] | None:
        self.init_schema()
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT *
                FROM pipeline_runs
                WHERE evidence_sha256 = ?
                ORDER BY updated_at DESC, created_at DESC, pipeline_run_id DESC
                LIMIT 1
                """,
                (str(evidence_sha256 or ""),),
            ).fetchone()
        return _pipeline_run_row_to_dict(row) if row is not None else None

    def latest_pipeline_run_by_operations(
        self,
        evidence_sha256: str,
        operations: list[str] | tuple[str, ...],
    ) -> dict[str, Any] | None:
        self.init_schema()
        operation_values = [str(operation) for operation in operations if str(operation)]
        if not operation_values:
            return self.latest_pipeline_run(evidence_sha256)
        placeholders = ",".join("?" for _operation in operation_values)
        with self.connect() as conn:
            row = conn.execute(
                f"""
                SELECT *
                FROM pipeline_runs
                WHERE evidence_sha256 = ?
                  AND operation IN ({placeholders})
                ORDER BY updated_at DESC, created_at DESC, pipeline_run_id DESC
                LIMIT 1
                """,
                (str(evidence_sha256 or ""), *operation_values),
            ).fetchone()
        return _pipeline_run_row_to_dict(row) if row is not None else None

    def list_pipeline_events(self, pipeline_run_id: str) -> list[dict[str, Any]]:
        self.init_schema()
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT *
                FROM pipeline_events
                WHERE pipeline_run_id = ?
                ORDER BY ordinal, created_at, event_id
                """,
                (str(pipeline_run_id or ""),),
            ).fetchall()
        return [_pipeline_event_row_to_dict(row) for row in rows]

    def get_pipeline_status(self, *, evidence_sha256: str = "", pipeline_run_id: str = "") -> dict[str, Any]:
        from ops_evidence_synthesis.pipeline_progress import build_pipeline_status, empty_pipeline_status

        run = self.get_pipeline_run(pipeline_run_id) if pipeline_run_id else self.latest_pipeline_run(evidence_sha256)
        if not run:
            return empty_pipeline_status(evidence_sha256=evidence_sha256, pipeline_run_id=pipeline_run_id)
        events = self.list_pipeline_events(str(run.get("pipeline_run_id") or ""))
        return build_pipeline_status(run, events)

    def latest_bundle(self) -> dict[str, Any] | None:
        self.init_schema()
        with self.connect() as conn:
            row = conn.execute(
                "SELECT bundle_json FROM evidence_bundles ORDER BY created_at DESC LIMIT 1"
            ).fetchone()
        if row is None:
            return None
        return dict(_json_load(str(row["bundle_json"])))

    def save_canonical_review_graph_snapshot(self, snapshot: dict[str, Any]) -> dict[str, Any]:
        self.init_schema()
        snapshot_id = "crg-" + sha256_json(
            {
                "evidence_sha256": snapshot.get("evidence_sha256"),
                "input_fingerprint_sha256": snapshot.get("input_fingerprint_sha256"),
                "canonical_graph_sha256": snapshot.get("canonical_graph_sha256"),
            }
        )[:20]
        row = _snapshot_storage_row(snapshot, snapshot_id=snapshot_id)
        with self.connect() as conn:
            existing = conn.execute(
                """
                SELECT * FROM canonical_review_graphs
                WHERE evidence_sha256 = ?
                  AND input_fingerprint_sha256 = ?
                  AND canonical_graph_sha256 = ?
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (row["evidence_sha256"], row["input_fingerprint_sha256"], row["canonical_graph_sha256"]),
            ).fetchone()
            if existing is not None:
                return _snapshot_row_to_dict(existing)
            conn.execute(
                """
                INSERT INTO canonical_review_graphs (
                  snapshot_id, evidence_sha256, canonical_graph_sha256, schema_version,
                  arbitration_version, input_fingerprint_sha256, input_fingerprint_json,
                  finding_title, finding_impact, primary_count, validation_count,
                  monitor_only_count, auto_archived_count, promotion_decision_count,
                  created_at, created_by, snapshot_status, canonical_review_graph_json
                ) VALUES (
                  :snapshot_id, :evidence_sha256, :canonical_graph_sha256, :schema_version,
                  :arbitration_version, :input_fingerprint_sha256, :input_fingerprint_json,
                  :finding_title, :finding_impact, :primary_count, :validation_count,
                  :monitor_only_count, :auto_archived_count, :promotion_decision_count,
                  :created_at, :created_by, :snapshot_status, :canonical_review_graph_json
                )
                """,
                row,
            )
            conn.commit()
            saved = conn.execute("SELECT * FROM canonical_review_graphs WHERE snapshot_id = ?", (snapshot_id,)).fetchone()
        return _snapshot_row_to_dict(saved) if saved is not None else dict(snapshot)

    def get_latest_canonical_review_graph_snapshot(self, evidence_sha256: str) -> dict[str, Any] | None:
        self.init_schema()
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM canonical_review_graphs
                WHERE evidence_sha256 = ?
                ORDER BY created_at DESC, snapshot_id DESC
                LIMIT 1
                """,
                (str(evidence_sha256 or ""),),
            ).fetchone()
        return _snapshot_row_to_dict(row) if row is not None else None

    def list_canonical_review_graph_snapshots(self, evidence_sha256: str) -> list[dict[str, Any]]:
        self.init_schema()
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM canonical_review_graphs
                WHERE evidence_sha256 = ?
                ORDER BY created_at DESC, snapshot_id DESC
                """,
                (str(evidence_sha256 or ""),),
            ).fetchall()
        return [_snapshot_row_to_dict(row) for row in rows]

    def replace_canonical_observation_groups(self, evidence_sha256: str, groups: Iterable[dict[str, Any]]) -> int:
        rows = [
            _observation_group_storage_row({**group, "evidence_sha256": str(group.get("evidence_sha256") or evidence_sha256)})
            for group in groups
            if isinstance(group, dict)
        ]
        evidence_id = str(evidence_sha256 or "")
        self.init_schema()
        with self.connect() as conn:
            conn.execute("DELETE FROM canonical_observation_groups WHERE evidence_sha256 = ?", (evidence_id,))
            if rows:
                conn.executemany(
                    """
                    INSERT OR REPLACE INTO canonical_observation_groups (
                      group_id, evidence_sha256, canonical_group_key, canonical_target_type,
                      canonical_subject, subsystem, component, source_target_ids_json,
                      source_candidate_count, providers_json, provider_count, evidence_refs_json,
                      missing_evidence_json, caveats_json, support_evidence_json,
                      counter_evidence_json, review_priority_score, consensus_class, group_json,
                      created_at
                    ) VALUES (
                      :group_id, :evidence_sha256, :canonical_group_key, :canonical_target_type,
                      :canonical_subject, :subsystem, :component, :source_target_ids_json,
                      :source_candidate_count, :providers_json, :provider_count, :evidence_refs_json,
                      :missing_evidence_json, :caveats_json, :support_evidence_json,
                      :counter_evidence_json, :review_priority_score, :consensus_class, :group_json,
                      :created_at
                    )
                    """,
                    rows,
                )
            conn.commit()
        return len(rows)

    def list_canonical_observation_groups(self, evidence_sha256: str) -> list[dict[str, Any]]:
        self.init_schema()
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT *
                FROM canonical_observation_groups
                WHERE evidence_sha256 = ?
                ORDER BY review_priority_score DESC, group_id
                """,
                (str(evidence_sha256 or ""),),
            ).fetchall()
        return [_observation_group_row_to_dict(row) for row in rows]

    def list_child_bundles(self, parent_evidence_sha256: str, *, limit: int = 20) -> list[dict[str, Any]]:
        parent = str(parent_evidence_sha256 or "")
        if not parent:
            return []
        self.init_schema()
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT bundle_json FROM evidence_bundles ORDER BY created_at DESC"
            ).fetchall()
        children: list[dict[str, Any]] = []
        for row in rows:
            bundle = dict(_json_load(str(row["bundle_json"])))
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

    def insert_model_run(self, run: ModelRunRecord) -> None:
        self.init_schema()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO model_runs (
                  run_id, evidence_sha256, prompt_sha256, model_input_sha256, provider,
                  model_name, temperature, raw_output, raw_output_sha256, latency_ms,
                  input_tokens, output_tokens, status, created_at
                ) VALUES (
                  :run_id, :evidence_sha256, :prompt_sha256, :model_input_sha256, :provider,
                  :model_name, :temperature, :raw_output, :raw_output_sha256, :latency_ms,
                  :input_tokens, :output_tokens, :status, :created_at
                )
                """,
                asdict(run),
            )
            conn.commit()

    def fetch_model_runs(self, evidence_sha256: str) -> list[ModelRunRecord]:
        self.init_schema()
        with self.connect() as conn:
            rows = conn.execute(
                """
                WITH latest_bundle AS (
                  SELECT created_at
                  FROM evidence_bundles
                  WHERE evidence_sha256 = ?
                  ORDER BY created_at DESC
                  LIMIT 1
                ),
                generation_gate AS (
                  SELECT created_at FROM latest_bundle
                  UNION ALL
                  SELECT '' AS created_at WHERE NOT EXISTS (SELECT 1 FROM latest_bundle)
                )
                SELECT r.*
                FROM model_runs r
                CROSS JOIN generation_gate b
                WHERE r.evidence_sha256 = ?
                  AND r.created_at >= b.created_at
                ORDER BY r.created_at, r.run_id
                """,
                (evidence_sha256, evidence_sha256),
            ).fetchall()
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
                latency_ms=int(row["latency_ms"]),
                input_tokens=int(row["input_tokens"]),
                output_tokens=int(row["output_tokens"]),
                status=str(row["status"]),
                created_at=str(row["created_at"]),
            )
            for row in rows
        ]

    def list_latest_model_runs(self, limit: int = 20) -> list[dict[str, Any]]:
        self.init_schema()
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT
                  run_id, evidence_sha256, prompt_sha256, model_input_sha256,
                  provider, model_name, temperature, raw_output, raw_output_sha256,
                  latency_ms, input_tokens, output_tokens, status, created_at
                FROM (
                  SELECT
                    *,
                    ROW_NUMBER() OVER (PARTITION BY provider ORDER BY created_at DESC, run_id DESC) AS rn
                  FROM model_runs
                )
                WHERE rn = 1
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [_model_run_row_to_dict(row) for row in rows]

    def insert_parsed_result(self, result: ParsedResultRecord) -> None:
        self.init_schema()
        with self.connect() as conn:
            row = asdict(result)
            row["parsed_json"] = _json(result.parsed_json)
            row["schema_valid"] = 1 if result.schema_valid else 0
            row["schema_errors"] = _json(list(result.schema_errors))
            conn.execute(
                """
                INSERT OR REPLACE INTO parsed_results (
                  result_id, run_id, evidence_sha256, provider, parsed_json,
                  parsed_json_sha256, schema_valid, schema_errors, created_at
                ) VALUES (
                  :result_id, :run_id, :evidence_sha256, :provider, :parsed_json,
                  :parsed_json_sha256, :schema_valid, :schema_errors, :created_at
                )
                """,
                row,
            )
            conn.commit()

    def insert_model_output_artifact(self, artifact: dict[str, Any]) -> None:
        self.init_schema()
        row = _model_output_artifact_storage_row(artifact)
        with self.connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO model_output_artifacts (
                  artifact_id, run_id, evidence_sha256, provider, model_name,
                  raw_output_sha256, repaired_output_sha256, parsed_json_sha256,
                  parse_status, repair_applied, repair_rules_json, schema_valid,
                  schema_errors_json, original_preserved, artifact_json, created_at
                ) VALUES (
                  :artifact_id, :run_id, :evidence_sha256, :provider, :model_name,
                  :raw_output_sha256, :repaired_output_sha256, :parsed_json_sha256,
                  :parse_status, :repair_applied, :repair_rules_json, :schema_valid,
                  :schema_errors_json, :original_preserved, :artifact_json, :created_at
                )
                """,
                row,
            )
            conn.commit()

    def list_model_output_artifacts(self, evidence_sha256: str) -> list[dict[str, Any]]:
        self.init_schema()
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT *
                FROM model_output_artifacts
                WHERE evidence_sha256 = ?
                ORDER BY created_at, artifact_id
                """,
                (str(evidence_sha256 or ""),),
            ).fetchall()
        return [_model_output_artifact_row_to_dict(row) for row in rows]

    def fetch_parsed_results(self, evidence_sha256: str) -> list[ParsedResultRecord]:
        self.init_schema()
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM parsed_results
                WHERE evidence_sha256 = ?
                  AND created_at >= COALESCE((
                    SELECT created_at
                    FROM evidence_bundles
                    WHERE evidence_sha256 = ?
                    ORDER BY created_at DESC
                    LIMIT 1
                  ), '')
                ORDER BY created_at, result_id
                """,
                (evidence_sha256, evidence_sha256),
            ).fetchall()
        return [
            ParsedResultRecord(
                result_id=str(row["result_id"]),
                run_id=str(row["run_id"]),
                evidence_sha256=str(row["evidence_sha256"]),
                provider=str(row["provider"]),
                parsed_json=dict(_json_load(str(row["parsed_json"]))),
                parsed_json_sha256=str(row["parsed_json_sha256"]),
                schema_valid=bool(row["schema_valid"]),
                schema_errors=tuple(_json_load(str(row["schema_errors"]))),
                created_at=str(row["created_at"]),
            )
            for row in rows
        ]

    def insert_claims(self, claims: Iterable[ClaimRecord]) -> None:
        rows = list(claims)
        if not rows:
            return
        self.init_schema()
        with self.connect() as conn:
            conn.executemany(
                """
                INSERT OR REPLACE INTO claims (
                  claim_id, evidence_sha256, result_id, provider, claim_type, claim_text,
                  evidence_refs, counter_evidence_refs, caveats, missing_evidence,
                  temporary_action, permanent_action, required_authority, review_status,
                  created_at, evidence_refs_valid, subsystem, finding_status, evidence_identity
                ) VALUES (
                  :claim_id, :evidence_sha256, :result_id, :provider, :claim_type, :claim_text,
                  :evidence_refs, :counter_evidence_refs, :caveats, :missing_evidence,
                  :temporary_action, :permanent_action, :required_authority, :review_status,
                  :created_at, :evidence_refs_valid, :subsystem, :finding_status, :evidence_identity
                )
                """,
                [
                    {
                        **asdict(row),
                        "evidence_refs": _json(list(row.evidence_refs)),
                        "counter_evidence_refs": _json(list(row.counter_evidence_refs)),
                        "caveats": _json(list(row.caveats)),
                        "missing_evidence": _json(list(row.missing_evidence)),
                        "evidence_refs_valid": 1 if row.evidence_refs_valid else 0,
                        "evidence_identity": _json(row.evidence_identity),
                    }
                    for row in rows
                ],
            )
            conn.commit()

    def fetch_claims(self, evidence_sha256: str) -> list[ClaimRecord]:
        self.init_schema()
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM claims
                WHERE evidence_sha256 = ?
                  AND created_at >= COALESCE((
                    SELECT created_at
                    FROM evidence_bundles
                    WHERE evidence_sha256 = ?
                    ORDER BY created_at DESC
                    LIMIT 1
                  ), '')
                ORDER BY created_at, claim_id
                """,
                (evidence_sha256, evidence_sha256),
            ).fetchall()
        return [
            ClaimRecord(
                claim_id=str(row["claim_id"]),
                evidence_sha256=str(row["evidence_sha256"]),
                result_id=str(row["result_id"]),
                provider=str(row["provider"]),
                claim_type=str(row["claim_type"]),
                claim_text=str(row["claim_text"]),
                evidence_refs=tuple(_json_load(str(row["evidence_refs"]))),
                counter_evidence_refs=tuple(_json_load(str(row["counter_evidence_refs"]))),
                caveats=tuple(_json_load(str(row["caveats"]))),
                missing_evidence=tuple(_json_load(str(row["missing_evidence"]))),
                temporary_action=str(row["temporary_action"] or ""),
                permanent_action=str(row["permanent_action"] or ""),
                required_authority=str(row["required_authority"] or ""),
                review_status=str(row["review_status"]),
                created_at=str(row["created_at"]),
                evidence_refs_valid=bool(row["evidence_refs_valid"]),
                subsystem=str(row["subsystem"] or "general"),
                finding_status=str(row["finding_status"] or "supported"),
                evidence_identity=dict(_json_load(str(row["evidence_identity"] or "{}"))),
            )
            for row in rows
        ]

    def insert_propositions(self, propositions: Iterable[PropositionRecord]) -> None:
        rows = list(propositions)
        if not rows:
            return
        self.init_schema()
        with self.connect() as conn:
            conn.executemany(
                """
                INSERT OR REPLACE INTO propositions (
                  proposition_id, evidence_sha256, question, linked_claim_ids,
                  support_summary, counter_summary, validation_targets, next_data_needed,
                  priority, review_status, created_at, subsystem, structured_evidence
                ) VALUES (
                  :proposition_id, :evidence_sha256, :question, :linked_claim_ids,
                  :support_summary, :counter_summary, :validation_targets, :next_data_needed,
                  :priority, :review_status, :created_at, :subsystem, :structured_evidence
                )
                """,
                [
                    {
                        **asdict(row),
                        "linked_claim_ids": _json(list(row.linked_claim_ids)),
                        "validation_targets": _json(list(row.validation_targets)),
                        "next_data_needed": _json(list(row.next_data_needed)),
                        "structured_evidence": _json(row.structured_evidence),
                    }
                    for row in rows
                ],
            )
            conn.commit()

    def fetch_propositions(self, evidence_sha256: str) -> list[PropositionRecord]:
        self.init_schema()
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM propositions
                WHERE evidence_sha256 = ?
                  AND created_at >= COALESCE((
                    SELECT created_at
                    FROM evidence_bundles
                    WHERE evidence_sha256 = ?
                    ORDER BY created_at DESC
                    LIMIT 1
                  ), '')
                ORDER BY created_at, proposition_id
                """,
                (evidence_sha256, evidence_sha256),
            ).fetchall()
        return [
            PropositionRecord(
                proposition_id=str(row["proposition_id"]),
                evidence_sha256=str(row["evidence_sha256"]),
                question=str(row["question"]),
                linked_claim_ids=tuple(_json_load(str(row["linked_claim_ids"]))),
                support_summary=str(row["support_summary"]),
                counter_summary=str(row["counter_summary"]),
                validation_targets=tuple(_json_load(str(row["validation_targets"]))),
                next_data_needed=tuple(_json_load(str(row["next_data_needed"]))),
                priority=str(row["priority"]),
                review_status=str(row["review_status"]),
                created_at=str(row["created_at"]),
                subsystem=str(row["subsystem"] or "general"),
                structured_evidence=dict(_json_load(str(row["structured_evidence"] or "{}"))),
            )
            for row in rows
        ]

    def insert_scores(self, scores: Iterable[ScoreRecord]) -> None:
        rows = list(scores)
        if not rows:
            return
        self.init_schema()
        with self.connect() as conn:
            conn.executemany(
                """
                INSERT OR REPLACE INTO scores (
                  score_id, proposition_id, schema_score, evidence_ref_score,
                  unsupported_claim_penalty, contradiction_penalty, cross_model_agreement,
                  actionability_score, safety_score, review_priority_score, created_at
                ) VALUES (
                  :score_id, :proposition_id, :schema_score, :evidence_ref_score,
                  :unsupported_claim_penalty, :contradiction_penalty, :cross_model_agreement,
                  :actionability_score, :safety_score, :review_priority_score, :created_at
                )
                """,
                [asdict(row) for row in rows],
            )
            conn.commit()

    def insert_proposition_clusters(self, clusters: Iterable[PropositionClusterRecord]) -> None:
        rows = list(clusters)
        if not rows:
            return
        self.init_schema()
        with self.connect() as conn:
            conn.executemany(
                """
                INSERT OR REPLACE INTO proposition_clusters (
                  cluster_id, evidence_sha256, subsystem, claim_signature,
                  representative_proposition_id, member_proposition_ids,
                  supporting_providers, model_names, core_claim, disagreement_summary,
                  review_status, review_visibility, review_priority_score,
                  cluster_json, created_at
                ) VALUES (
                  :cluster_id, :evidence_sha256, :subsystem, :claim_signature,
                  :representative_proposition_id, :member_proposition_ids,
                  :supporting_providers, :model_names, :core_claim, :disagreement_summary,
                  :review_status, :review_visibility, :review_priority_score,
                  :cluster_json, :created_at
                )
                """,
                [
                    {
                        **asdict(row),
                        "member_proposition_ids": _json(list(row.member_proposition_ids)),
                        "supporting_providers": _json(list(row.supporting_providers)),
                        "model_names": _json(list(row.model_names)),
                        "cluster_json": _json(row.cluster_json),
                    }
                    for row in rows
                ],
            )
            conn.commit()

    def list_proposition_clusters(
        self,
        *,
        evidence_sha256: str | None = None,
        limit: int = 50,
        include_hidden: bool = False,
    ) -> list[dict[str, Any]]:
        self.init_schema()
        conditions: list[str] = []
        params: list[Any] = []
        if evidence_sha256:
            conditions.append("c.evidence_sha256 = ?")
            params.append(evidence_sha256)
        else:
            conditions.append("b.incident_rn = 1")
        conditions.append("c.created_at >= b.created_at")
        if not include_hidden:
            conditions.append("c.review_visibility = 'review'")
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        params.append(limit)
        with self.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT c.*
                FROM proposition_clusters c
                JOIN (
                  SELECT
                    eb.*,
                    ROW_NUMBER() OVER (
                      PARTITION BY eb.service, eb.environment, eb.window_start, eb.window_end
                      ORDER BY eb.created_at DESC, eb.evidence_sha256 DESC
                    ) AS incident_rn
                  FROM evidence_bundles eb
                ) b ON b.evidence_sha256 = c.evidence_sha256
                {where}
                ORDER BY c.review_priority_score DESC, c.created_at DESC
                LIMIT ?
                """,
                params,
            ).fetchall()
        return [_cluster_row_to_dict(row) for row in rows]

    def insert_model_comparison(self, comparison: dict[str, Any]) -> None:
        self.init_schema()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO model_comparisons (
                  comparison_id, evidence_sha256, baseline_provider, candidate_provider,
                  comparison_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    comparison["comparison_id"],
                    comparison["evidence_sha256"],
                    comparison["baseline_provider"],
                    comparison["candidate_provider"],
                    _json(comparison),
                    comparison["created_at"],
                ),
            )
            conn.commit()

    def list_model_comparisons(
        self,
        *,
        evidence_sha256: str | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        self.init_schema()
        params: list[Any] = []
        where = ""
        if evidence_sha256:
            where = "WHERE evidence_sha256 = ?"
            params.append(evidence_sha256)
        params.append(limit)
        with self.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT comparison_json
                FROM model_comparisons
                {where}
                ORDER BY created_at DESC
                LIMIT ?
                """,
                params,
            ).fetchall()
        return [dict(_json_load(str(row["comparison_json"]))) for row in rows]

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
        self.init_schema()
        conditions = ["evidence_sha256 = ?"]
        params: list[Any] = [str(evidence_sha256 or "")]
        if pending_only:
            conditions.append("status IN ('pending', 'needs_more_data')")
        where = " AND ".join(conditions)
        with self.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT target_json, status
                FROM review_targets
                WHERE {where}
                ORDER BY review_priority_score DESC, updated_at DESC, review_target_id
                LIMIT ?
                """,
                (*params, int(limit)),
            ).fetchall()
        targets: list[dict[str, Any]] = []
        for row in rows:
            target = dict(_json_load(str(row["target_json"])))
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
        self.init_schema()
        with self.connect() as conn:
            if evidence_id:
                conn.execute("DELETE FROM review_targets WHERE evidence_sha256 = ?", (evidence_id,))
                conn.commit()
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
        self.init_schema()
        with self.connect() as conn:
            if bundle:
                row = conn.execute(
                    """
                    SELECT COUNT(*) AS count
                    FROM logs_sanitized
                    WHERE service = ?
                      AND environment = ?
                    """,
                    (str(bundle.get("service") or ""), str(bundle.get("environment") or "")),
                ).fetchone()
                count = int(row["count"] if row else 0)
                if count:
                    return count
            row = conn.execute("SELECT COUNT(*) AS count FROM logs_sanitized").fetchone()
        return int(row["count"] if row else 0)

    def get_review_target(self, review_target_id: str) -> dict[str, Any] | None:
        target_set = self.list_review_targets(limit=100, pending_only=False)
        for target in target_set["targets"]:
            if str(target.get("review_target_id")) == review_target_id:
                return target
        return self._fetch_stored_review_target(review_target_id)

    def upsert_review_targets(self, targets: Iterable[dict[str, Any]]) -> None:
        rows = list(targets)
        if not rows:
            return
        self.init_schema()
        now = utc_now()
        with self.connect() as conn:
            conn.executemany(
                """
                INSERT INTO review_targets (
                  review_target_id, cluster_id, evidence_sha256, title, subsystem,
                  core_claim, support_json, counter_json, caveats_json,
                  missing_evidence_json, proposal, review_priority_score,
                  score_breakdown_json, status, target_json, created_at, updated_at
                ) VALUES (
                  :review_target_id, :cluster_id, :evidence_sha256, :title, :subsystem,
                  :core_claim, :support_json, :counter_json, :caveats_json,
                  :missing_evidence_json, :proposal, :review_priority_score,
                  :score_breakdown_json, :status, :target_json, :created_at, :updated_at
                )
                ON CONFLICT(review_target_id) DO UPDATE SET
                  cluster_id = excluded.cluster_id,
                  evidence_sha256 = excluded.evidence_sha256,
                  title = excluded.title,
                  subsystem = excluded.subsystem,
                  core_claim = excluded.core_claim,
                  support_json = excluded.support_json,
                  counter_json = excluded.counter_json,
                  caveats_json = excluded.caveats_json,
                  missing_evidence_json = excluded.missing_evidence_json,
                  proposal = excluded.proposal,
                  review_priority_score = excluded.review_priority_score,
                  score_breakdown_json = excluded.score_breakdown_json,
                  status = CASE
                    WHEN review_targets.status IN (
                      'confirmed_candidate', 'known_issue', 'watchlist',
                      'false_positive', 'low_value', 'duplicate', 'not_actionable',
                      'needs_more_data'
                    )
                    THEN review_targets.status
                    ELSE excluded.status
                  END,
                  target_json = excluded.target_json,
                  updated_at = excluded.updated_at
                """,
                [
                    {
                        "review_target_id": str(target["review_target_id"]),
                        "cluster_id": str(target.get("cluster_id") or ""),
                        "evidence_sha256": str(target.get("evidence_sha256") or ""),
                        "title": str(target.get("title") or ""),
                        "subsystem": str(target.get("subsystem") or "general"),
                        "core_claim": str(target.get("core_claim") or ""),
                        "support_json": _json((target.get("drawer") or {}).get("support_evidence") or []),
                        "counter_json": _json((target.get("drawer") or {}).get("counter_evidence") or []),
                        "caveats_json": _json((target.get("drawer") or {}).get("caveats") or []),
                        "missing_evidence_json": _json((target.get("drawer") or {}).get("missing_evidence") or []),
                        "proposal": str(target.get("proposal") or ""),
                        "review_priority_score": float(target.get("review_priority_score") or 0.0),
                        "score_breakdown_json": _json(target.get("score_breakdown") or {}),
                        "status": str(target.get("status") or "pending"),
                        "target_json": _json(target),
                        "created_at": now,
                        "updated_at": now,
                    }
                    for target in rows
                ],
            )
            conn.commit()

    def list_proposals(
        self,
        limit: int = 50,
        *,
        evidence_sha256: str | None = None,
        pending_only: bool = True,
        include_hidden: bool = False,
    ) -> list[dict[str, Any]]:
        self.init_schema()
        conditions: list[str] = []
        params: list[Any] = []
        if evidence_sha256:
            conditions.append("p.evidence_sha256 = ?")
            params.append(evidence_sha256)
        else:
            conditions.append("b.incident_rn = 1")
        conditions.append("p.created_at >= b.created_at")
        if pending_only:
            conditions.append("COALESCE(r.resulting_status, p.review_status) IN ('pending', 'needs_more_data')")
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        raw_limit = max(limit * 6, limit + 50)
        params.append(raw_limit)
        with self.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT
                  p.*,
                  COALESCE(r.resulting_status, p.review_status) AS effective_review_status,
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
                  b.window_start,
                  b.window_end,
                  b.bundle_json
                FROM propositions p
                JOIN (
                  SELECT
                    eb.*,
                    ROW_NUMBER() OVER (
                      PARTITION BY eb.service, eb.environment, eb.window_start, eb.window_end
                      ORDER BY eb.created_at DESC, eb.evidence_sha256 DESC
                    ) AS incident_rn
                  FROM evidence_bundles eb
                ) b ON b.evidence_sha256 = p.evidence_sha256
                LEFT JOIN scores s ON s.proposition_id = p.proposition_id
                LEFT JOIN (
                  SELECT * FROM (
                    SELECT
                      ur.*,
                      ROW_NUMBER() OVER (PARTITION BY ur.proposition_id ORDER BY ur.created_at DESC) AS rn
                    FROM user_reviews ur
                  )
                  WHERE rn = 1
                ) r ON r.proposition_id = p.proposition_id
                {where}
                ORDER BY s.review_priority_score DESC, p.created_at DESC
                LIMIT ?
                """,
                params,
            ).fetchall()

        proposals: list[dict[str, Any]] = []
        claims_cache: dict[str, dict[str, ClaimRecord]] = {}
        models_cache: dict[str, dict[str, str]] = {}
        for row in rows:
            item = dict(row)
            if item.get("effective_review_status"):
                item["review_status"] = str(item.pop("effective_review_status"))
            linked_claim_ids = list(_json_load(str(item["linked_claim_ids"])))
            for key in ("validation_targets", "next_data_needed"):
                item[key] = _json_load(str(item[key]))
            bundle = dict(_json_load(str(item.pop("bundle_json"))))
            evidence_id = str(item["evidence_sha256"])
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
            item["linked_claim_ids"] = linked_claim_ids
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

        review_id = f"review-{uuid.uuid4().hex[:16]}"
        created_at = utc_now()
        status = resulting_status or _status_for_review_decision(decision, decision_detail)
        self.init_schema()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO user_reviews (
                  review_id, proposition_id, decision, reviewer, note, created_at,
                  decision_detail, resulting_status, generated_query_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    review_id,
                    proposition_id,
                    decision,
                    reviewer,
                    note,
                    created_at,
                    decision_detail,
                    status,
                    _json(generated_query or {}),
                ),
            )
            conn.execute(
                "UPDATE propositions SET review_status = ? WHERE proposition_id = ?",
                (status, proposition_id),
            )
            conn.commit()
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
        self.init_schema()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO reviews (
                  review_id, review_target_id, decision, reason, human_note,
                  reviewer, created_at, generated_query_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    review_id,
                    review_target_id,
                    decision,
                    reason,
                    human_note,
                    reviewer,
                    created_at,
                    _json(generated_query),
                ),
            )
            conn.execute(
                """
                UPDATE review_targets
                SET status = ?, updated_at = ?
                WHERE review_target_id = ?
                """,
                (status, created_at, review_target_id),
            )
            conn.commit()

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
        generated_query = {
            "event": "more_data_result",
            "child_evidence_sha256": child_evidence_sha256,
            "refresh_summary": summary,
        }
        status = "more_data_collected"
        self.init_schema()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO reviews (
                  review_id, review_target_id, decision, reason, human_note,
                  reviewer, created_at, generated_query_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    review_id,
                    review_target_id,
                    status,
                    "evidence_collected",
                    human_note,
                    reviewer,
                    created_at,
                    _json(generated_query),
                ),
            )
            conn.execute(
                """
                UPDATE review_targets
                SET status = ?, updated_at = ?
                WHERE review_target_id = ?
                """,
                (status, created_at, review_target_id),
            )
            conn.commit()
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
        self.init_schema()
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT *
                FROM reviews
                WHERE review_target_id = ?
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (review_target_id,),
            ).fetchone()
        if row is None:
            return None
        return {
            "review_id": str(row["review_id"]),
            "review_target_id": str(row["review_target_id"]),
            "decision": str(row["decision"]),
            "reason": str(row["reason"]),
            "human_note": str(row["human_note"]),
            "reviewer": str(row["reviewer"]),
            "created_at": str(row["created_at"]),
            "generated_query": dict(_json_load(str(row["generated_query_json"] or "{}"))),
            "status": _status_for_review_decision(str(row["decision"]), str(row["reason"])),
        }

    def _fetch_stored_review_target(self, review_target_id: str) -> dict[str, Any] | None:
        self.init_schema()
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT target_json, status
                FROM review_targets
                WHERE review_target_id = ?
                """,
                (review_target_id,),
            ).fetchone()
        if row is None:
            return None
        target = dict(_json_load(str(row["target_json"])))
        target["status"] = str(row["status"] or target.get("status") or "pending")
        latest = self._latest_review_for_target(review_target_id)
        if latest:
            target["latest_review"] = latest
        return target

    def count_table(self, table_name: str) -> int:
        if table_name not in {
            "logs_sanitized",
            "evidence_bundles",
            "model_runs",
            "pipeline_runs",
            "pipeline_events",
            "parsed_results",
            "model_output_artifacts",
            "claims",
            "propositions",
            "scores",
            "proposition_clusters",
            "review_targets",
            "canonical_review_graphs",
            "canonical_observation_groups",
            "model_comparisons",
            "user_reviews",
            "reviews",
        }:
            raise ValueError(f"unsupported table: {table_name}")
        self.init_schema()
        with self.connect() as conn:
            row = conn.execute(f"SELECT COUNT(*) AS count FROM {table_name}").fetchone()
        return int(row["count"])

    def build_more_data_query(
        self,
        proposition_id: str,
        *,
        limit: int = 200,
        requests: list[dict[str, Any]] | None = None,
        request_ids: list[Any] | tuple[Any, ...] | None = None,
    ) -> dict[str, Any]:
        self.init_schema()
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT p.*, b.bundle_json
                FROM propositions p
                JOIN evidence_bundles b ON b.evidence_sha256 = p.evidence_sha256
                WHERE p.proposition_id = ?
                """,
                (proposition_id,),
            ).fetchone()
            if row is None:
                return {}
            bundle = dict(_json_load(str(row["bundle_json"])))
            subsystem = str(row["subsystem"] or "general")
            baseline = bundle.get("baseline") if isinstance(bundle.get("baseline"), dict) else {}
            search_start = str(
                baseline.get("start")
                or bundle.get("lookback_window_start")
                or bundle["window_start"]
            )
            search_end = str(bundle["window_end"])
            next_data_needed = list(_json_load(str(row["next_data_needed"])))
            normalized_requests = normalize_more_data_requests(next_data_needed, requests)
            normalized_requests = filter_more_data_requests(normalized_requests, request_ids)
            fallback_sql = (
                "SELECT timestamp, service, severity, message_sanitized, message_template, error_type, labels_json, raw_log_sha256 "
                "FROM logs_sanitized "
                "WHERE environment = ? "
                "AND timestamp >= ? "
                "AND timestamp < ? "
                "ORDER BY timestamp DESC "
                "LIMIT ?"
            )
            fallback_rows = [
                _sqlite_more_data_row(item)
                for item in conn.execute(
                    fallback_sql,
                    (str(bundle["environment"]), search_start, search_end, int(limit)),
                ).fetchall()
            ]
            query_rows = []
            flattened_preview_rows = []
            per_request_limit = max(20, min(int(limit), 100))
            for request in normalized_requests:
                predicate, predicate_params = sqlite_text_predicate_for_request(request)
                request_sql = (
                    "SELECT timestamp, service, severity, message_sanitized, message_template, error_type, labels_json, raw_log_sha256 "
                    "FROM logs_sanitized "
                    "WHERE environment = ? "
                    "AND timestamp >= ? "
                    "AND timestamp < ? "
                    f"AND {predicate} "
                    "ORDER BY timestamp DESC "
                    "LIMIT ?"
                )
                params = [str(bundle["environment"]), search_start, search_end, *predicate_params, per_request_limit]
                rows_for_request = [
                    _sqlite_more_data_row(item)
                    for item in conn.execute(request_sql, params).fetchall()
                ]
                flattened_preview_rows.extend(
                    {**row_item, "request_id": request["request_id"]}
                    for row_item in rows_for_request[:20]
                )
                query_rows.append(
                    {
                        **request,
                        "sql": _sqlite_interpolated_sql(request_sql, params),
                        "preview_count": len(rows_for_request),
                        "preview_rows": rows_for_request[:20],
                    }
                )
        request_analysis = analyze_more_data_queries(query_rows)
        preview_rows = flattened_preview_rows or fallback_rows[:20]
        return {
            "engine": "sqlite",
            "proposition_id": proposition_id,
            "evidence_sha256": str(row["evidence_sha256"]),
            "subsystem": subsystem,
            "next_data_needed": next_data_needed,
            "next_evidence_requests": normalized_requests,
            "search_window": {
                "start": search_start,
                "end": search_end,
                "basis": "baseline_to_incident_end" if baseline.get("start") else "lookback_to_incident_end",
            },
            "sql": (
                "SELECT timestamp, service, severity, message_sanitized, error_type, raw_log_sha256 "
                "FROM logs_sanitized "
                f"WHERE environment = '{bundle['environment']}' "
                f"AND timestamp >= '{search_start}' "
                f"AND timestamp < '{search_end}' "
                "ORDER BY timestamp DESC "
                f"LIMIT {int(limit)}"
            ),
            "queries": query_rows,
            "request_analysis": request_analysis,
            "preview_count": sum(int(query.get("preview_count") or 0) for query in query_rows) if query_rows else len(fallback_rows),
            "preview_rows": preview_rows[:50],
            "fallback_preview_count": len(fallback_rows),
        }


def _sqlite_more_data_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "timestamp": str(row["timestamp"] or ""),
        "service": str(row["service"] or ""),
        "severity": str(row["severity"] or ""),
        "message_sanitized": str(row["message_sanitized"] or ""),
        "message_template": str(row["message_template"] or ""),
        "error_type": str(row["error_type"] or ""),
        "labels_json": dict(_json_load(str(row["labels_json"] or "{}"))),
        "raw_log_sha256": str(row["raw_log_sha256"] or ""),
    }


def _sqlite_interpolated_sql(sql: str, params: list[Any]) -> str:
    output = sql
    for param in params:
        if isinstance(param, (int, float)):
            value = str(param)
        else:
            value = "'" + str(param).replace("'", "''") + "'"
        output = output.replace("?", value, 1)
    return output


def _status_for_review_decision(decision: str, detail: str = "") -> str:
    if decision == "accepted":
        return detail if detail in {"known_issue", "confirmed_candidate", "watchlist"} else "confirmed_candidate"
    if decision == "rejected":
        return detail if detail in {"false_positive", "low_value", "duplicate", "not_actionable", "unsupported"} else "false_positive"
    if decision == "needs_more_data":
        return "needs_more_data"
    return decision


def _cluster_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "cluster_id": str(row["cluster_id"]),
        "evidence_sha256": str(row["evidence_sha256"]),
        "subsystem": str(row["subsystem"]),
        "claim_signature": str(row["claim_signature"]),
        "representative_proposition_id": str(row["representative_proposition_id"]),
        "member_proposition_ids": list(_json_load(str(row["member_proposition_ids"]))),
        "supporting_providers": list(_json_load(str(row["supporting_providers"]))),
        "model_names": list(_json_load(str(row["model_names"]))),
        "core_claim": str(row["core_claim"]),
        "disagreement_summary": str(row["disagreement_summary"]),
        "review_status": str(row["review_status"]),
        "review_visibility": str(row["review_visibility"]),
        "review_priority_score": float(row["review_priority_score"]),
        "cluster_json": dict(_json_load(str(row["cluster_json"]))),
        "created_at": str(row["created_at"]),
    }


def _model_run_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "run_id": str(row["run_id"]),
        "evidence_sha256": str(row["evidence_sha256"]),
        "prompt_sha256": str(row["prompt_sha256"]),
        "model_input_sha256": str(row["model_input_sha256"]),
        "provider": str(row["provider"]),
        "model_name": str(row["model_name"]),
        "temperature": float(row["temperature"]),
        "raw_output": str(row["raw_output"]),
        "raw_output_sha256": str(row["raw_output_sha256"]),
        "latency_ms": int(row["latency_ms"]),
        "input_tokens": int(row["input_tokens"]),
        "output_tokens": int(row["output_tokens"]),
        "status": str(row["status"]),
        "created_at": str(row["created_at"]),
    }
