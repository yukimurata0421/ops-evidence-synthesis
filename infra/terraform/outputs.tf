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
