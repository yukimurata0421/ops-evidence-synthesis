output "service_uri" {
  description = "Cloud Run service URI."
  value       = google_cloud_run_v2_service.api.uri
}

output "service_account_email" {
  description = "Runtime service account email."
  value       = google_service_account.api.email
}

output "raw_dataset" {
  description = "Raw evidence BigQuery dataset."
  value       = google_bigquery_dataset.raw.dataset_id
}

output "core_dataset" {
  description = "Core derived evidence BigQuery dataset."
  value       = google_bigquery_dataset.core.dataset_id
}

output "synthesis_dataset" {
  description = "Synthesis BigQuery dataset."
  value       = google_bigquery_dataset.synthesis.dataset_id
}

output "private_artifact_bucket" {
  description = "Private GCS bucket for sanitized job inputs and recorded outputs."
  value       = "gs://${google_storage_bucket.private_artifacts.name}"
}

output "chunked_review_job_name" {
  description = "Cloud Run Job name for private chunked review execution."
  value       = google_cloud_run_v2_job.chunked_review.name
}

output "postgres_connection_name" {
  description = "Cloud SQL PostgreSQL connection name for the ledger."
  value       = google_sql_database_instance.postgres.connection_name
}

output "postgres_database" {
  description = "PostgreSQL ledger database name."
  value       = google_sql_database.ledger.name
}
