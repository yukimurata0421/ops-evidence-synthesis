CREATE SCHEMA IF NOT EXISTS `${PROJECT_ID}.ops_evidence_raw`
OPTIONS(location = "asia-northeast1");
CREATE SCHEMA IF NOT EXISTS `${PROJECT_ID}.ops_evidence_core`
OPTIONS(location = "asia-northeast1");
CREATE SCHEMA IF NOT EXISTS `${PROJECT_ID}.ops_synthesis`
OPTIONS(location = "asia-northeast1");

CREATE TABLE IF NOT EXISTS `${PROJECT_ID}.ops_evidence_raw.logs_sanitized` (
  timestamp TIMESTAMP NOT NULL,
  service STRING NOT NULL,
  environment STRING NOT NULL,
  severity STRING NOT NULL,
  trace_id STRING,
  span_id STRING,
  deploy_id STRING,
  version STRING,
  message_sanitized STRING NOT NULL,
  message_template STRING NOT NULL,
  error_type STRING NOT NULL,
  stack_hash STRING,
  resource_type STRING,
  labels_json JSON,
  raw_log_sha256 STRING NOT NULL,
  sanitizer_version STRING NOT NULL
)
PARTITION BY DATE(timestamp)
CLUSTER BY service, environment, severity;

CREATE TABLE IF NOT EXISTS `${PROJECT_ID}.ops_evidence_core.log_patterns` (
  pattern_id STRING NOT NULL,
  service STRING NOT NULL,
  environment STRING NOT NULL,
  window_start TIMESTAMP NOT NULL,
  window_end TIMESTAMP NOT NULL,
  message_template STRING NOT NULL,
  error_type STRING NOT NULL,
  count INT64 NOT NULL,
  baseline_count INT64,
  first_seen TIMESTAMP,
  last_seen TIMESTAMP,
  example_log STRING,
  example_log_sha256 STRING,
  embedding ARRAY<FLOAT64>,
  severity_hint STRING
)
PARTITION BY DATE(window_start)
CLUSTER BY service, environment, error_type;

CREATE TABLE IF NOT EXISTS `${PROJECT_ID}.ops_evidence_core.metric_windows` (
  metric_window_id STRING NOT NULL,
  service STRING NOT NULL,
  window_start TIMESTAMP NOT NULL,
  window_end TIMESTAMP NOT NULL,
  metric_name STRING NOT NULL,
  baseline_value FLOAT64,
  current_value FLOAT64,
  delta FLOAT64,
  delta_pct FLOAT64,
  severity_hint STRING
)
PARTITION BY DATE(window_start)
CLUSTER BY service, metric_name;

CREATE TABLE IF NOT EXISTS `${PROJECT_ID}.ops_synthesis.evidence_bundles` (
  evidence_sha256 STRING NOT NULL,
  schema_version STRING NOT NULL,
  service STRING NOT NULL,
  environment STRING NOT NULL,
  window_start TIMESTAMP NOT NULL,
  window_end TIMESTAMP NOT NULL,
  query_sql_hash STRING NOT NULL,
  sanitizer_version STRING NOT NULL,
  bundle_json JSON NOT NULL,
  created_at TIMESTAMP NOT NULL
)
PARTITION BY DATE(created_at)
CLUSTER BY service, environment, evidence_sha256;

CREATE TABLE IF NOT EXISTS `${PROJECT_ID}.ops_synthesis.model_runs` (
  run_id STRING NOT NULL,
  evidence_sha256 STRING NOT NULL,
  prompt_sha256 STRING NOT NULL,
  model_input_sha256 STRING NOT NULL,
  provider STRING NOT NULL,
  model_name STRING NOT NULL,
  temperature FLOAT64 NOT NULL,
  raw_output STRING NOT NULL,
  raw_output_sha256 STRING NOT NULL,
  latency_ms INT64,
  input_tokens INT64,
  output_tokens INT64,
  status STRING NOT NULL,
  created_at TIMESTAMP NOT NULL
)
PARTITION BY DATE(created_at)
CLUSTER BY evidence_sha256, provider, status;

CREATE TABLE IF NOT EXISTS `${PROJECT_ID}.ops_synthesis.pipeline_runs` (
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
CLUSTER BY evidence_sha256, pipeline_run_id, status;

CREATE TABLE IF NOT EXISTS `${PROJECT_ID}.ops_synthesis.pipeline_events` (
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
CLUSTER BY evidence_sha256, pipeline_run_id, step_key;

CREATE TABLE IF NOT EXISTS `${PROJECT_ID}.ops_synthesis.parsed_results` (
  result_id STRING NOT NULL,
  run_id STRING NOT NULL,
  evidence_sha256 STRING NOT NULL,
  provider STRING NOT NULL,
  parsed_json JSON NOT NULL,
  parsed_json_sha256 STRING NOT NULL,
  schema_valid BOOL NOT NULL,
  schema_errors ARRAY<STRING>,
  created_at TIMESTAMP NOT NULL
)
PARTITION BY DATE(created_at)
CLUSTER BY evidence_sha256, provider, schema_valid;

CREATE TABLE IF NOT EXISTS `${PROJECT_ID}.ops_synthesis.model_output_artifacts` (
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
CLUSTER BY evidence_sha256, provider, parse_status;

CREATE TABLE IF NOT EXISTS `${PROJECT_ID}.ops_synthesis.claims` (
  claim_id STRING NOT NULL,
  evidence_sha256 STRING NOT NULL,
  result_id STRING NOT NULL,
  provider STRING NOT NULL,
  claim_type STRING NOT NULL,
  claim_text STRING NOT NULL,
  evidence_refs ARRAY<STRING>,
  counter_evidence_refs ARRAY<STRING>,
  caveats ARRAY<STRING>,
  missing_evidence ARRAY<STRING>,
  temporary_action STRING,
  permanent_action STRING,
  required_authority STRING,
  review_status STRING NOT NULL,
  evidence_refs_valid BOOL NOT NULL,
  created_at TIMESTAMP NOT NULL,
  subsystem STRING,
  finding_status STRING,
  evidence_identity JSON
)
PARTITION BY DATE(created_at)
CLUSTER BY evidence_sha256, claim_type, provider;

ALTER TABLE `${PROJECT_ID}.ops_synthesis.claims`
ADD COLUMN IF NOT EXISTS finding_status STRING;

ALTER TABLE `${PROJECT_ID}.ops_synthesis.claims`
ADD COLUMN IF NOT EXISTS evidence_identity JSON;

CREATE TABLE IF NOT EXISTS `${PROJECT_ID}.ops_synthesis.propositions` (
  proposition_id STRING NOT NULL,
  evidence_sha256 STRING NOT NULL,
  question STRING NOT NULL,
  linked_claim_ids ARRAY<STRING>,
  support_summary STRING,
  counter_summary STRING,
  validation_targets ARRAY<STRING>,
  next_data_needed ARRAY<STRING>,
  priority STRING NOT NULL,
  review_status STRING NOT NULL,
  created_at TIMESTAMP NOT NULL,
  subsystem STRING,
  structured_evidence JSON
)
PARTITION BY DATE(created_at)
CLUSTER BY evidence_sha256, review_status, priority;

CREATE TABLE IF NOT EXISTS `${PROJECT_ID}.ops_synthesis.scores` (
  score_id STRING NOT NULL,
  proposition_id STRING NOT NULL,
  schema_score FLOAT64 NOT NULL,
  evidence_ref_score FLOAT64 NOT NULL,
  unsupported_claim_penalty FLOAT64 NOT NULL,
  contradiction_penalty FLOAT64 NOT NULL,
  cross_model_agreement FLOAT64 NOT NULL,
  actionability_score FLOAT64 NOT NULL,
  safety_score FLOAT64 NOT NULL,
  review_priority_score FLOAT64 NOT NULL,
  created_at TIMESTAMP NOT NULL
)
PARTITION BY DATE(created_at)
CLUSTER BY proposition_id;

CREATE TABLE IF NOT EXISTS `${PROJECT_ID}.ops_synthesis.proposition_clusters` (
  cluster_id STRING NOT NULL,
  evidence_sha256 STRING NOT NULL,
  subsystem STRING NOT NULL,
  claim_signature STRING NOT NULL,
  representative_proposition_id STRING NOT NULL,
  member_proposition_ids ARRAY<STRING>,
  supporting_providers ARRAY<STRING>,
  model_names ARRAY<STRING>,
  core_claim STRING,
  disagreement_summary STRING,
  review_status STRING NOT NULL,
  review_visibility STRING NOT NULL,
  review_priority_score FLOAT64 NOT NULL,
  cluster_json JSON NOT NULL,
  created_at TIMESTAMP NOT NULL
)
PARTITION BY DATE(created_at)
CLUSTER BY evidence_sha256, review_visibility, subsystem;

CREATE TABLE IF NOT EXISTS `${PROJECT_ID}.ops_synthesis.review_targets` (
  review_target_id STRING NOT NULL,
  cluster_id STRING NOT NULL,
  evidence_sha256 STRING NOT NULL,
  title STRING NOT NULL,
  subsystem STRING NOT NULL,
  core_claim STRING,
  support_json JSON,
  counter_json JSON,
  caveats_json JSON,
  missing_evidence_json JSON,
  proposal STRING,
  review_priority_score FLOAT64 NOT NULL,
  score_breakdown_json JSON,
  status STRING NOT NULL,
  target_json JSON NOT NULL,
  created_at TIMESTAMP NOT NULL,
  updated_at TIMESTAMP NOT NULL
)
PARTITION BY DATE(created_at)
CLUSTER BY evidence_sha256, status, subsystem;

CREATE TABLE IF NOT EXISTS `${PROJECT_ID}.ops_synthesis.canonical_review_graphs` (
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
CLUSTER BY evidence_sha256, input_fingerprint_sha256, canonical_graph_sha256;

CREATE TABLE IF NOT EXISTS `${PROJECT_ID}.ops_synthesis.canonical_observation_groups` (
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
CLUSTER BY evidence_sha256, canonical_group_key, canonical_target_type;

CREATE TABLE IF NOT EXISTS `${PROJECT_ID}.ops_synthesis.model_comparisons` (
  comparison_id STRING NOT NULL,
  evidence_sha256 STRING NOT NULL,
  baseline_provider STRING NOT NULL,
  candidate_provider STRING NOT NULL,
  comparison_json JSON NOT NULL,
  created_at TIMESTAMP NOT NULL
)
PARTITION BY DATE(created_at)
CLUSTER BY evidence_sha256, baseline_provider, candidate_provider;

CREATE TABLE IF NOT EXISTS `${PROJECT_ID}.ops_synthesis.user_reviews` (
  review_id STRING NOT NULL,
  proposition_id STRING NOT NULL,
  decision STRING NOT NULL,
  reviewer STRING NOT NULL,
  note STRING,
  created_at TIMESTAMP NOT NULL,
  decision_detail STRING,
  resulting_status STRING,
  generated_query_json JSON
)
PARTITION BY DATE(created_at)
CLUSTER BY proposition_id, decision;

CREATE TABLE IF NOT EXISTS `${PROJECT_ID}.ops_synthesis.reviews` (
  review_id STRING NOT NULL,
  review_target_id STRING NOT NULL,
  decision STRING NOT NULL,
  reason STRING,
  human_note STRING,
  reviewer STRING NOT NULL,
  created_at TIMESTAMP NOT NULL,
  generated_query_json JSON
)
PARTITION BY DATE(created_at)
CLUSTER BY review_target_id, decision;

ALTER TABLE `${PROJECT_ID}.ops_synthesis.claims`
ADD COLUMN IF NOT EXISTS subsystem STRING;

ALTER TABLE `${PROJECT_ID}.ops_synthesis.propositions`
ADD COLUMN IF NOT EXISTS subsystem STRING;

ALTER TABLE `${PROJECT_ID}.ops_synthesis.propositions`
ADD COLUMN IF NOT EXISTS structured_evidence JSON;

ALTER TABLE `${PROJECT_ID}.ops_synthesis.user_reviews`
ADD COLUMN IF NOT EXISTS decision_detail STRING;

ALTER TABLE `${PROJECT_ID}.ops_synthesis.user_reviews`
ADD COLUMN IF NOT EXISTS resulting_status STRING;

ALTER TABLE `${PROJECT_ID}.ops_synthesis.user_reviews`
ADD COLUMN IF NOT EXISTS generated_query_json JSON;

ALTER TABLE `${PROJECT_ID}.ops_synthesis.review_targets`
ADD COLUMN IF NOT EXISTS support_json JSON;

ALTER TABLE `${PROJECT_ID}.ops_synthesis.review_targets`
ADD COLUMN IF NOT EXISTS counter_json JSON;

ALTER TABLE `${PROJECT_ID}.ops_synthesis.review_targets`
ADD COLUMN IF NOT EXISTS caveats_json JSON;

ALTER TABLE `${PROJECT_ID}.ops_synthesis.review_targets`
ADD COLUMN IF NOT EXISTS missing_evidence_json JSON;

ALTER TABLE `${PROJECT_ID}.ops_synthesis.reviews`
ADD COLUMN IF NOT EXISTS generated_query_json JSON;
