locals {
  bigquery_tables = {
    "ops_evidence_raw.logs_sanitized" = {
      dataset_id      = google_bigquery_dataset.raw.dataset_id
      table_id        = "logs_sanitized"
      partition_field = "timestamp"
      clustering      = ["service", "environment", "severity"]
      schema = [
        { name = "timestamp", type = "TIMESTAMP", mode = "REQUIRED" },
        { name = "service", type = "STRING", mode = "REQUIRED" },
        { name = "environment", type = "STRING", mode = "REQUIRED" },
        { name = "severity", type = "STRING", mode = "REQUIRED" },
        { name = "trace_id", type = "STRING" },
        { name = "span_id", type = "STRING" },
        { name = "deploy_id", type = "STRING" },
        { name = "version", type = "STRING" },
        { name = "message_sanitized", type = "STRING", mode = "REQUIRED" },
        { name = "message_template", type = "STRING", mode = "REQUIRED" },
        { name = "error_type", type = "STRING", mode = "REQUIRED" },
        { name = "stack_hash", type = "STRING" },
        { name = "resource_type", type = "STRING" },
        { name = "labels_json", type = "JSON" },
        { name = "raw_log_sha256", type = "STRING", mode = "REQUIRED" },
        { name = "sanitizer_version", type = "STRING", mode = "REQUIRED" },
      ]
    }

    "ops_evidence_core.log_patterns" = {
      dataset_id      = google_bigquery_dataset.core.dataset_id
      table_id        = "log_patterns"
      partition_field = "window_start"
      clustering      = ["service", "environment", "error_type"]
      schema = [
        { name = "pattern_id", type = "STRING", mode = "REQUIRED" },
        { name = "service", type = "STRING", mode = "REQUIRED" },
        { name = "environment", type = "STRING", mode = "REQUIRED" },
        { name = "window_start", type = "TIMESTAMP", mode = "REQUIRED" },
        { name = "window_end", type = "TIMESTAMP", mode = "REQUIRED" },
        { name = "message_template", type = "STRING", mode = "REQUIRED" },
        { name = "error_type", type = "STRING", mode = "REQUIRED" },
        { name = "count", type = "INTEGER", mode = "REQUIRED" },
        { name = "baseline_count", type = "INTEGER" },
        { name = "first_seen", type = "TIMESTAMP" },
        { name = "last_seen", type = "TIMESTAMP" },
        { name = "example_log", type = "STRING" },
        { name = "example_log_sha256", type = "STRING" },
        { name = "embedding", type = "FLOAT", mode = "REPEATED" },
        { name = "severity_hint", type = "STRING" },
      ]
    }

    "ops_evidence_core.metric_windows" = {
      dataset_id      = google_bigquery_dataset.core.dataset_id
      table_id        = "metric_windows"
      partition_field = "window_start"
      clustering      = ["service", "metric_name"]
      schema = [
        { name = "metric_window_id", type = "STRING", mode = "REQUIRED" },
        { name = "service", type = "STRING", mode = "REQUIRED" },
        { name = "window_start", type = "TIMESTAMP", mode = "REQUIRED" },
        { name = "window_end", type = "TIMESTAMP", mode = "REQUIRED" },
        { name = "metric_name", type = "STRING", mode = "REQUIRED" },
        { name = "baseline_value", type = "FLOAT" },
        { name = "current_value", type = "FLOAT" },
        { name = "delta", type = "FLOAT" },
        { name = "delta_pct", type = "FLOAT" },
        { name = "severity_hint", type = "STRING" },
      ]
    }

    "ops_synthesis.evidence_bundles" = {
      dataset_id      = google_bigquery_dataset.synthesis.dataset_id
      table_id        = "evidence_bundles"
      partition_field = "created_at"
      clustering      = ["service", "environment", "evidence_sha256"]
      schema = [
        { name = "evidence_sha256", type = "STRING", mode = "REQUIRED" },
        { name = "schema_version", type = "STRING", mode = "REQUIRED" },
        { name = "service", type = "STRING", mode = "REQUIRED" },
        { name = "environment", type = "STRING", mode = "REQUIRED" },
        { name = "window_start", type = "TIMESTAMP", mode = "REQUIRED" },
        { name = "window_end", type = "TIMESTAMP", mode = "REQUIRED" },
        { name = "query_sql_hash", type = "STRING", mode = "REQUIRED" },
        { name = "sanitizer_version", type = "STRING", mode = "REQUIRED" },
        { name = "bundle_json", type = "JSON", mode = "REQUIRED" },
        { name = "created_at", type = "TIMESTAMP", mode = "REQUIRED" },
      ]
    }

    "ops_synthesis.model_runs" = {
      dataset_id      = google_bigquery_dataset.synthesis.dataset_id
      table_id        = "model_runs"
      partition_field = "created_at"
      clustering      = ["evidence_sha256", "provider", "status"]
      schema = [
        { name = "run_id", type = "STRING", mode = "REQUIRED" },
        { name = "evidence_sha256", type = "STRING", mode = "REQUIRED" },
        { name = "prompt_sha256", type = "STRING", mode = "REQUIRED" },
        { name = "model_input_sha256", type = "STRING", mode = "REQUIRED" },
        { name = "provider", type = "STRING", mode = "REQUIRED" },
        { name = "model_name", type = "STRING", mode = "REQUIRED" },
        { name = "temperature", type = "FLOAT", mode = "REQUIRED" },
        { name = "raw_output", type = "STRING", mode = "REQUIRED" },
        { name = "raw_output_sha256", type = "STRING", mode = "REQUIRED" },
        { name = "latency_ms", type = "INTEGER" },
        { name = "input_tokens", type = "INTEGER" },
        { name = "output_tokens", type = "INTEGER" },
        { name = "status", type = "STRING", mode = "REQUIRED" },
        { name = "created_at", type = "TIMESTAMP", mode = "REQUIRED" },
      ]
    }

    "ops_synthesis.pipeline_runs" = {
      dataset_id      = google_bigquery_dataset.synthesis.dataset_id
      table_id        = "pipeline_runs"
      partition_field = "created_at"
      clustering      = ["evidence_sha256", "pipeline_run_id", "status"]
      schema = [
        { name = "pipeline_run_id", type = "STRING", mode = "REQUIRED" },
        { name = "evidence_sha256", type = "STRING", mode = "REQUIRED" },
        { name = "parent_pipeline_run_id", type = "STRING" },
        { name = "operation", type = "STRING", mode = "REQUIRED" },
        { name = "status", type = "STRING", mode = "REQUIRED" },
        { name = "current_step", type = "STRING", mode = "REQUIRED" },
        { name = "total_steps", type = "INTEGER", mode = "REQUIRED" },
        { name = "completed_steps", type = "INTEGER", mode = "REQUIRED" },
        { name = "blocking_reason", type = "STRING" },
        { name = "provider_total", type = "INTEGER" },
        { name = "provider_success", type = "INTEGER" },
        { name = "provider_failed", type = "INTEGER" },
        { name = "provider_skipped", type = "INTEGER" },
        { name = "review_target_count", type = "INTEGER" },
        { name = "validation_target_count", type = "INTEGER" },
        { name = "child_bundle_count", type = "INTEGER" },
        { name = "summary_json", type = "JSON", mode = "REQUIRED" },
        { name = "error_message", type = "STRING", mode = "REQUIRED" },
        { name = "created_at", type = "TIMESTAMP", mode = "REQUIRED" },
        { name = "updated_at", type = "TIMESTAMP", mode = "REQUIRED" },
        { name = "completed_at", type = "TIMESTAMP" },
      ]
    }

    "ops_synthesis.pipeline_events" = {
      dataset_id      = google_bigquery_dataset.synthesis.dataset_id
      table_id        = "pipeline_events"
      partition_field = "created_at"
      clustering      = ["evidence_sha256", "pipeline_run_id", "step_key"]
      schema = [
        { name = "event_id", type = "STRING", mode = "REQUIRED" },
        { name = "pipeline_run_id", type = "STRING", mode = "REQUIRED" },
        { name = "evidence_sha256", type = "STRING", mode = "REQUIRED" },
        { name = "operation", type = "STRING", mode = "REQUIRED" },
        { name = "event_type", type = "STRING" },
        { name = "stage", type = "STRING" },
        { name = "step_key", type = "STRING", mode = "REQUIRED" },
        { name = "step_label", type = "STRING", mode = "REQUIRED" },
        { name = "status", type = "STRING", mode = "REQUIRED" },
        { name = "provider_id", type = "STRING" },
        { name = "artifact_id", type = "STRING" },
        { name = "input_sha256", type = "STRING" },
        { name = "output_sha256", type = "STRING" },
        { name = "reason_code", type = "STRING" },
        { name = "message", type = "STRING", mode = "REQUIRED" },
        { name = "ordinal", type = "INTEGER", mode = "REQUIRED" },
        { name = "metadata_json", type = "JSON", mode = "REQUIRED" },
        { name = "created_at", type = "TIMESTAMP", mode = "REQUIRED" },
      ]
    }

    "ops_synthesis.parsed_results" = {
      dataset_id      = google_bigquery_dataset.synthesis.dataset_id
      table_id        = "parsed_results"
      partition_field = "created_at"
      clustering      = ["evidence_sha256", "provider", "schema_valid"]
      schema = [
        { name = "result_id", type = "STRING", mode = "REQUIRED" },
        { name = "run_id", type = "STRING", mode = "REQUIRED" },
        { name = "evidence_sha256", type = "STRING", mode = "REQUIRED" },
        { name = "provider", type = "STRING", mode = "REQUIRED" },
        { name = "parsed_json", type = "JSON", mode = "REQUIRED" },
        { name = "parsed_json_sha256", type = "STRING", mode = "REQUIRED" },
        { name = "schema_valid", type = "BOOLEAN", mode = "REQUIRED" },
        { name = "schema_errors", type = "STRING", mode = "REPEATED" },
        { name = "created_at", type = "TIMESTAMP", mode = "REQUIRED" },
      ]
    }

    "ops_synthesis.model_output_artifacts" = {
      dataset_id      = google_bigquery_dataset.synthesis.dataset_id
      table_id        = "model_output_artifacts"
      partition_field = "created_at"
      clustering      = ["evidence_sha256", "provider", "parse_status"]
      schema = [
        { name = "artifact_id", type = "STRING", mode = "REQUIRED" },
        { name = "run_id", type = "STRING", mode = "REQUIRED" },
        { name = "evidence_sha256", type = "STRING", mode = "REQUIRED" },
        { name = "provider", type = "STRING", mode = "REQUIRED" },
        { name = "model_name", type = "STRING", mode = "REQUIRED" },
        { name = "raw_output_sha256", type = "STRING", mode = "REQUIRED" },
        { name = "repaired_output_sha256", type = "STRING", mode = "REQUIRED" },
        { name = "parsed_json_sha256", type = "STRING", mode = "REQUIRED" },
        { name = "parse_status", type = "STRING", mode = "REQUIRED" },
        { name = "repair_applied", type = "BOOLEAN", mode = "REQUIRED" },
        { name = "repair_rules", type = "STRING", mode = "REPEATED" },
        { name = "schema_valid", type = "BOOLEAN", mode = "REQUIRED" },
        { name = "schema_errors", type = "STRING", mode = "REPEATED" },
        { name = "original_preserved", type = "BOOLEAN", mode = "REQUIRED" },
        { name = "artifact_json", type = "JSON", mode = "REQUIRED" },
        { name = "created_at", type = "TIMESTAMP", mode = "REQUIRED" },
      ]
    }

    "ops_synthesis.claims" = {
      dataset_id      = google_bigquery_dataset.synthesis.dataset_id
      table_id        = "claims"
      partition_field = "created_at"
      clustering      = ["evidence_sha256", "claim_type", "provider"]
      schema = [
        { name = "claim_id", type = "STRING", mode = "REQUIRED" },
        { name = "evidence_sha256", type = "STRING", mode = "REQUIRED" },
        { name = "result_id", type = "STRING", mode = "REQUIRED" },
        { name = "provider", type = "STRING", mode = "REQUIRED" },
        { name = "claim_type", type = "STRING", mode = "REQUIRED" },
        { name = "claim_text", type = "STRING", mode = "REQUIRED" },
        { name = "evidence_refs", type = "STRING", mode = "REPEATED" },
        { name = "counter_evidence_refs", type = "STRING", mode = "REPEATED" },
        { name = "caveats", type = "STRING", mode = "REPEATED" },
        { name = "missing_evidence", type = "STRING", mode = "REPEATED" },
        { name = "temporary_action", type = "STRING" },
        { name = "permanent_action", type = "STRING" },
        { name = "required_authority", type = "STRING" },
        { name = "review_status", type = "STRING", mode = "REQUIRED" },
        { name = "evidence_refs_valid", type = "BOOLEAN", mode = "REQUIRED" },
        { name = "created_at", type = "TIMESTAMP", mode = "REQUIRED" },
        { name = "subsystem", type = "STRING" },
        { name = "finding_status", type = "STRING" },
        { name = "evidence_identity", type = "JSON" },
      ]
    }

    "ops_synthesis.propositions" = {
      dataset_id      = google_bigquery_dataset.synthesis.dataset_id
      table_id        = "propositions"
      partition_field = "created_at"
      clustering      = ["evidence_sha256", "review_status", "priority"]
      schema = [
        { name = "proposition_id", type = "STRING", mode = "REQUIRED" },
        { name = "evidence_sha256", type = "STRING", mode = "REQUIRED" },
        { name = "question", type = "STRING", mode = "REQUIRED" },
        { name = "linked_claim_ids", type = "STRING", mode = "REPEATED" },
        { name = "support_summary", type = "STRING" },
        { name = "counter_summary", type = "STRING" },
        { name = "validation_targets", type = "STRING", mode = "REPEATED" },
        { name = "next_data_needed", type = "STRING", mode = "REPEATED" },
        { name = "priority", type = "STRING", mode = "REQUIRED" },
        { name = "review_status", type = "STRING", mode = "REQUIRED" },
        { name = "created_at", type = "TIMESTAMP", mode = "REQUIRED" },
        { name = "subsystem", type = "STRING" },
        { name = "structured_evidence", type = "JSON" },
      ]
    }

    "ops_synthesis.scores" = {
      dataset_id      = google_bigquery_dataset.synthesis.dataset_id
      table_id        = "scores"
      partition_field = "created_at"
      clustering      = ["proposition_id"]
      schema = [
        { name = "score_id", type = "STRING", mode = "REQUIRED" },
        { name = "proposition_id", type = "STRING", mode = "REQUIRED" },
        { name = "schema_score", type = "FLOAT", mode = "REQUIRED" },
        { name = "evidence_ref_score", type = "FLOAT", mode = "REQUIRED" },
        { name = "unsupported_claim_penalty", type = "FLOAT", mode = "REQUIRED" },
        { name = "contradiction_penalty", type = "FLOAT", mode = "REQUIRED" },
        { name = "cross_model_agreement", type = "FLOAT", mode = "REQUIRED" },
        { name = "actionability_score", type = "FLOAT", mode = "REQUIRED" },
        { name = "safety_score", type = "FLOAT", mode = "REQUIRED" },
        { name = "review_priority_score", type = "FLOAT", mode = "REQUIRED" },
        { name = "created_at", type = "TIMESTAMP", mode = "REQUIRED" },
      ]
    }

    "ops_synthesis.proposition_clusters" = {
      dataset_id      = google_bigquery_dataset.synthesis.dataset_id
      table_id        = "proposition_clusters"
      partition_field = "created_at"
      clustering      = ["evidence_sha256", "review_visibility", "subsystem"]
      schema = [
        { name = "cluster_id", type = "STRING", mode = "REQUIRED" },
        { name = "evidence_sha256", type = "STRING", mode = "REQUIRED" },
        { name = "subsystem", type = "STRING", mode = "REQUIRED" },
        { name = "claim_signature", type = "STRING", mode = "REQUIRED" },
        { name = "representative_proposition_id", type = "STRING", mode = "REQUIRED" },
        { name = "member_proposition_ids", type = "STRING", mode = "REPEATED" },
        { name = "supporting_providers", type = "STRING", mode = "REPEATED" },
        { name = "model_names", type = "STRING", mode = "REPEATED" },
        { name = "core_claim", type = "STRING" },
        { name = "disagreement_summary", type = "STRING" },
        { name = "review_status", type = "STRING", mode = "REQUIRED" },
        { name = "review_visibility", type = "STRING", mode = "REQUIRED" },
        { name = "review_priority_score", type = "FLOAT", mode = "REQUIRED" },
        { name = "cluster_json", type = "JSON", mode = "REQUIRED" },
        { name = "created_at", type = "TIMESTAMP", mode = "REQUIRED" },
      ]
    }

    "ops_synthesis.review_targets" = {
      dataset_id      = google_bigquery_dataset.synthesis.dataset_id
      table_id        = "review_targets"
      partition_field = "created_at"
      clustering      = ["evidence_sha256", "status", "subsystem"]
      schema = [
        { name = "review_target_id", type = "STRING", mode = "REQUIRED" },
        { name = "cluster_id", type = "STRING", mode = "REQUIRED" },
        { name = "evidence_sha256", type = "STRING", mode = "REQUIRED" },
        { name = "title", type = "STRING", mode = "REQUIRED" },
        { name = "subsystem", type = "STRING", mode = "REQUIRED" },
        { name = "core_claim", type = "STRING" },
        { name = "support_json", type = "JSON" },
        { name = "counter_json", type = "JSON" },
        { name = "caveats_json", type = "JSON" },
        { name = "missing_evidence_json", type = "JSON" },
        { name = "proposal", type = "STRING" },
        { name = "review_priority_score", type = "FLOAT", mode = "REQUIRED" },
        { name = "score_breakdown_json", type = "JSON" },
        { name = "status", type = "STRING", mode = "REQUIRED" },
        { name = "target_json", type = "JSON", mode = "REQUIRED" },
        { name = "created_at", type = "TIMESTAMP", mode = "REQUIRED" },
        { name = "updated_at", type = "TIMESTAMP", mode = "REQUIRED" },
      ]
    }

    "ops_synthesis.canonical_review_graphs" = {
      dataset_id      = google_bigquery_dataset.synthesis.dataset_id
      table_id        = "canonical_review_graphs"
      partition_field = "created_at"
      clustering      = ["evidence_sha256", "input_fingerprint_sha256", "canonical_graph_sha256"]
      schema = [
        { name = "evidence_sha256", type = "STRING", mode = "REQUIRED" },
        { name = "canonical_graph_sha256", type = "STRING", mode = "REQUIRED" },
        { name = "schema_version", type = "STRING", mode = "REQUIRED" },
        { name = "arbitration_version", type = "STRING", mode = "REQUIRED" },
        { name = "input_fingerprint_sha256", type = "STRING", mode = "REQUIRED" },
        { name = "input_fingerprint_json", type = "JSON", mode = "REQUIRED" },
        { name = "finding_title", type = "STRING" },
        { name = "finding_impact", type = "STRING" },
        { name = "primary_count", type = "INTEGER", mode = "REQUIRED" },
        { name = "validation_count", type = "INTEGER", mode = "REQUIRED" },
        { name = "monitor_only_count", type = "INTEGER", mode = "REQUIRED" },
        { name = "auto_archived_count", type = "INTEGER", mode = "REQUIRED" },
        { name = "promotion_decision_count", type = "INTEGER", mode = "REQUIRED" },
        { name = "created_at", type = "TIMESTAMP", mode = "REQUIRED" },
        { name = "created_by", type = "STRING", mode = "REQUIRED" },
        { name = "snapshot_status", type = "STRING", mode = "REQUIRED" },
        { name = "canonical_review_graph_json", type = "JSON", mode = "REQUIRED" },
      ]
    }

    "ops_synthesis.canonical_observation_groups" = {
      dataset_id      = google_bigquery_dataset.synthesis.dataset_id
      table_id        = "canonical_observation_groups"
      partition_field = "created_at"
      clustering      = ["evidence_sha256", "canonical_group_key", "canonical_target_type"]
      schema = [
        { name = "group_id", type = "STRING", mode = "REQUIRED" },
        { name = "evidence_sha256", type = "STRING", mode = "REQUIRED" },
        { name = "canonical_group_key", type = "STRING", mode = "REQUIRED" },
        { name = "canonical_target_type", type = "STRING", mode = "REQUIRED" },
        { name = "canonical_subject", type = "STRING", mode = "REQUIRED" },
        { name = "subsystem", type = "STRING", mode = "REQUIRED" },
        { name = "component", type = "STRING" },
        { name = "source_target_ids", type = "STRING", mode = "REPEATED" },
        { name = "source_candidate_count", type = "INTEGER", mode = "REQUIRED" },
        { name = "providers", type = "STRING", mode = "REPEATED" },
        { name = "provider_count", type = "INTEGER", mode = "REQUIRED" },
        { name = "evidence_refs", type = "STRING", mode = "REPEATED" },
        { name = "missing_evidence", type = "STRING", mode = "REPEATED" },
        { name = "caveats", type = "STRING", mode = "REPEATED" },
        { name = "support_evidence", type = "JSON" },
        { name = "counter_evidence", type = "JSON" },
        { name = "review_priority_score", type = "FLOAT", mode = "REQUIRED" },
        { name = "consensus_class", type = "STRING", mode = "REQUIRED" },
        { name = "group_json", type = "JSON", mode = "REQUIRED" },
        { name = "created_at", type = "TIMESTAMP", mode = "REQUIRED" },
      ]
    }

    "ops_synthesis.model_comparisons" = {
      dataset_id      = google_bigquery_dataset.synthesis.dataset_id
      table_id        = "model_comparisons"
      partition_field = "created_at"
      clustering      = ["evidence_sha256", "baseline_provider", "candidate_provider"]
      schema = [
        { name = "comparison_id", type = "STRING", mode = "REQUIRED" },
        { name = "evidence_sha256", type = "STRING", mode = "REQUIRED" },
        { name = "baseline_provider", type = "STRING", mode = "REQUIRED" },
        { name = "candidate_provider", type = "STRING", mode = "REQUIRED" },
        { name = "comparison_json", type = "JSON", mode = "REQUIRED" },
        { name = "created_at", type = "TIMESTAMP", mode = "REQUIRED" },
      ]
    }

    "ops_synthesis.user_reviews" = {
      dataset_id      = google_bigquery_dataset.synthesis.dataset_id
      table_id        = "user_reviews"
      partition_field = "created_at"
      clustering      = ["proposition_id", "decision"]
      schema = [
        { name = "review_id", type = "STRING", mode = "REQUIRED" },
        { name = "proposition_id", type = "STRING", mode = "REQUIRED" },
        { name = "decision", type = "STRING", mode = "REQUIRED" },
        { name = "reviewer", type = "STRING", mode = "REQUIRED" },
        { name = "note", type = "STRING" },
        { name = "created_at", type = "TIMESTAMP", mode = "REQUIRED" },
        { name = "decision_detail", type = "STRING" },
        { name = "resulting_status", type = "STRING" },
        { name = "generated_query_json", type = "JSON" },
      ]
    }

    "ops_synthesis.reviews" = {
      dataset_id      = google_bigquery_dataset.synthesis.dataset_id
      table_id        = "reviews"
      partition_field = "created_at"
      clustering      = ["review_target_id", "decision"]
      schema = [
        { name = "review_id", type = "STRING", mode = "REQUIRED" },
        { name = "review_target_id", type = "STRING", mode = "REQUIRED" },
        { name = "decision", type = "STRING", mode = "REQUIRED" },
        { name = "reason", type = "STRING" },
        { name = "human_note", type = "STRING" },
        { name = "reviewer", type = "STRING", mode = "REQUIRED" },
        { name = "created_at", type = "TIMESTAMP", mode = "REQUIRED" },
        { name = "generated_query_json", type = "JSON" },
      ]
    }
  }
}

resource "google_bigquery_table" "managed" {
  for_each            = local.bigquery_tables
  dataset_id          = each.value.dataset_id
  table_id            = each.value.table_id
  deletion_protection = true
  schema              = jsonencode(each.value.schema)
  clustering          = each.value.clustering

  time_partitioning {
    type          = "DAY"
    field         = each.value.partition_field
    expiration_ms = var.bigquery_partition_expiration_ms
  }
}
