resource "google_service_account" "api" {
  account_id   = "${var.service_name}-sa"
  display_name = "Ops Evidence API runtime"
}

locals {
  base_env = {
    OES_STORE             = "bigquery"
    OES_GCP_PROJECT       = var.project_id
    OES_BIGQUERY_LOCATION = var.bigquery_location
  }
  cloud_run_env = merge(
    local.base_env,
    {
      OES_PRECOMPUTED_REVIEW_GCS_PREFIX = "gs://${google_storage_bucket.private_artifacts.name}/precomputed_review_summaries"
    },
    var.runtime_env,
  )
  chunked_review_job_env = merge(
    local.base_env,
    var.runtime_env,
    {
      OES_CHUNK_RUN_STORE                   = "postgres"
      OES_CLOUD_SQL_CONNECTION_NAME         = google_sql_database_instance.postgres.connection_name
      OES_POSTGRES_DB                       = google_sql_database.ledger.name
      OES_POSTGRES_USER                     = google_sql_user.ledger.name
      OES_JOB_OUTPUT_PREFIX_URI             = "gs://${google_storage_bucket.private_artifacts.name}/job-runs"
      OES_JOB_PRECOMPUTED_OUTPUT_PREFIX_URI = "gs://${google_storage_bucket.private_artifacts.name}/precomputed_review_summaries"
      OES_JOB_STATIC_REVIEW_OUTPUT_PREFIX_URI = "gs://${google_storage_bucket.private_artifacts.name}/review-pages"
      OES_JOB_PROVIDER_MODE                 = var.chunked_review_job_provider_mode
      OES_JOB_PROVIDERS                     = join(",", var.chunked_review_job_providers)
      OES_MULTI_AI_CHUNK_MAX_WORKERS        = tostring(var.chunked_review_job_chunk_workers)
      OES_MULTI_AI_CHUNK_MAX_WORKERS_BY_PROVIDER = "mistral-agent-platform=1"
      OES_MULTI_AI_PROVIDER_WORKERS         = tostring(var.chunked_review_job_provider_workers)
      OES_MULTI_AI_CHUNK_RETRY_ATTEMPTS     = tostring(var.chunked_review_job_chunk_retry_attempts)
      OES_MULTI_AI_MERGE_SMALL_SEMANTIC_CHUNKS = "1"
      OES_MULTI_AI_RATE_LIMIT_BACKOFF_SECONDS = "30"
      OES_MODEL_MAX_ATTEMPTS                = "5"
      OES_MODEL_RETRY_BASE_SECONDS          = "5"
      OES_MODEL_RETRY_MAX_SECONDS           = "60"
    },
    var.chunked_review_job_env,
  )
}

resource "google_storage_bucket" "private_artifacts" {
  name                        = var.private_artifact_bucket_name
  location                    = var.private_artifact_bucket_location
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  force_destroy               = false

  versioning {
    enabled = true
  }

  lifecycle_rule {
    condition {
      age = var.private_artifact_bucket_retention_days
    }
    action {
      type = "Delete"
    }
  }
}

resource "google_bigquery_dataset" "raw" {
  dataset_id                      = "ops_evidence_raw"
  location                        = var.bigquery_location
  delete_contents_on_destroy      = false
  default_partition_expiration_ms = var.bigquery_partition_expiration_ms
  default_table_expiration_ms     = var.bigquery_partition_expiration_ms
}

resource "google_bigquery_dataset" "core" {
  dataset_id                      = "ops_evidence_core"
  location                        = var.bigquery_location
  delete_contents_on_destroy      = false
  default_partition_expiration_ms = var.bigquery_partition_expiration_ms
  default_table_expiration_ms     = var.bigquery_partition_expiration_ms
}

resource "google_bigquery_dataset" "synthesis" {
  dataset_id                      = "ops_synthesis"
  location                        = var.bigquery_location
  delete_contents_on_destroy      = false
  default_partition_expiration_ms = var.bigquery_partition_expiration_ms
  default_table_expiration_ms     = var.bigquery_partition_expiration_ms
}

resource "google_project_iam_member" "api_bigquery_user" {
  project = var.project_id
  role    = "roles/bigquery.jobUser"
  member  = "serviceAccount:${google_service_account.api.email}"
}

resource "google_bigquery_dataset_iam_member" "api_raw_editor" {
  dataset_id = google_bigquery_dataset.raw.dataset_id
  role       = "roles/bigquery.dataEditor"
  member     = "serviceAccount:${google_service_account.api.email}"
}

resource "google_bigquery_dataset_iam_member" "api_core_editor" {
  dataset_id = google_bigquery_dataset.core.dataset_id
  role       = "roles/bigquery.dataEditor"
  member     = "serviceAccount:${google_service_account.api.email}"
}

resource "google_bigquery_dataset_iam_member" "api_synthesis_editor" {
  dataset_id = google_bigquery_dataset.synthesis.dataset_id
  role       = "roles/bigquery.dataEditor"
  member     = "serviceAccount:${google_service_account.api.email}"
}

resource "google_project_iam_member" "api_vertex_user" {
  project = var.project_id
  role    = "roles/aiplatform.user"
  member  = "serviceAccount:${google_service_account.api.email}"
}

resource "google_storage_bucket_iam_member" "api_private_artifact_object_admin" {
  bucket = google_storage_bucket.private_artifacts.name
  role   = "roles/storage.objectAdmin"
  member = "serviceAccount:${google_service_account.api.email}"
}

resource "google_project_iam_member" "api_cloud_sql_client" {
  project = var.project_id
  role    = "roles/cloudsql.client"
  member  = "serviceAccount:${google_service_account.api.email}"
}

resource "google_secret_manager_secret_iam_member" "api_write_token_accessor" {
  count     = var.api_write_token_secret == "" ? 0 : 1
  project   = var.project_id
  secret_id = var.api_write_token_secret
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.api.email}"
}

resource "google_secret_manager_secret_iam_member" "api_postgres_password_accessor" {
  count     = var.postgres_password_secret == "" ? 0 : 1
  project   = var.project_id
  secret_id = var.postgres_password_secret
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.api.email}"
}

resource "google_sql_database_instance" "postgres" {
  name             = var.postgres_instance_name
  database_version = var.postgres_database_version
  region           = var.region

  deletion_protection = var.postgres_deletion_protection

  settings {
    tier              = var.postgres_tier
    availability_type = var.postgres_availability_type
    disk_size         = var.postgres_disk_size_gb
    disk_type         = "PD_SSD"
    disk_autoresize   = true

    backup_configuration {
      enabled                        = true
      point_in_time_recovery_enabled = true
    }

    ip_configuration {
      ipv4_enabled = var.postgres_ipv4_enabled
    }
  }
}

resource "google_sql_database" "ledger" {
  name     = var.postgres_database_name
  instance = google_sql_database_instance.postgres.name
}

resource "google_sql_user" "ledger" {
  name     = var.postgres_user_name
  instance = google_sql_database_instance.postgres.name
  password = var.postgres_password
}

resource "google_cloud_run_v2_service" "api" {
  name     = var.service_name
  location = var.region
  ingress  = "INGRESS_TRAFFIC_ALL"

  template {
    service_account                  = google_service_account.api.email
    timeout                          = "${var.timeout_seconds}s"
    max_instance_request_concurrency = var.container_concurrency

    scaling {
      min_instance_count = var.min_instances
      max_instance_count = var.max_instances
    }

    containers {
      image = var.container_image

      ports {
        container_port = 8080
        name           = "http1"
      }

      dynamic "env" {
        for_each = local.cloud_run_env
        content {
          name  = env.key
          value = env.value
        }
      }

      dynamic "env" {
        for_each = var.api_write_token_secret == "" ? [] : [var.api_write_token_secret]
        content {
          name = "OES_API_WRITE_TOKEN"
          value_source {
            secret_key_ref {
              secret  = env.value
              version = "latest"
            }
          }
        }
      }

      resources {
        limits = {
          cpu    = var.cpu_limit
          memory = var.memory_limit
        }
        cpu_idle          = false
        startup_cpu_boost = true
      }

      startup_probe {
        failure_threshold     = 1
        period_seconds        = 240
        timeout_seconds       = 240
        initial_delay_seconds = 0

        tcp_socket {
          port = 8080
        }
      }
    }
  }

  lifecycle {
    ignore_changes = [
      client,
      client_version,
    ]
  }
}

resource "google_cloud_run_v2_job" "chunked_review" {
  name     = var.chunked_review_job_name
  location = var.region

  template {
    template {
      service_account = google_service_account.api.email
      timeout         = "${var.chunked_review_job_timeout_seconds}s"
      max_retries     = var.chunked_review_job_max_retries

      containers {
        image   = var.container_image
        command = ["python", "-m", "ops_evidence_synthesis.gcp.chunked_review_job"]

        dynamic "env" {
          for_each = local.chunked_review_job_env
          content {
            name  = env.key
            value = env.value
          }
        }

        dynamic "env" {
          for_each = var.postgres_password_secret == "" ? [] : [var.postgres_password_secret]
          content {
            name = "OES_POSTGRES_PASSWORD"
            value_source {
              secret_key_ref {
                secret  = env.value
                version = "latest"
              }
            }
          }
        }

        resources {
          limits = {
            cpu    = var.chunked_review_job_cpu_limit
            memory = var.chunked_review_job_memory_limit
          }
        }

        volume_mounts {
          name       = "cloudsql"
          mount_path = "/cloudsql"
        }
      }

      volumes {
        name = "cloudsql"
        cloud_sql_instance {
          instances = [google_sql_database_instance.postgres.connection_name]
        }
      }
    }
  }
}

resource "google_cloud_run_v2_service_iam_member" "public_invoker" {
  count    = var.enable_public_invoker ? 1 : 0
  name     = google_cloud_run_v2_service.api.name
  location = google_cloud_run_v2_service.api.location
  role     = "roles/run.invoker"
  member   = "allUsers"
}

resource "google_monitoring_alert_policy" "cloud_run_5xx" {
  display_name = "${var.service_name} 5xx response rate"
  combiner     = "OR"
  enabled      = true

  conditions {
    display_name = "5xx responses"
    condition_threshold {
      filter          = "resource.type=\"cloud_run_revision\" AND resource.labels.service_name=\"${var.service_name}\" AND metric.type=\"run.googleapis.com/request_count\" AND metric.labels.response_code_class=\"5xx\""
      duration        = "300s"
      comparison      = "COMPARISON_GT"
      threshold_value = 5
      aggregations {
        alignment_period   = "300s"
        per_series_aligner = "ALIGN_RATE"
      }
    }
  }

  notification_channels = var.notification_channel_ids
}

resource "google_monitoring_alert_policy" "cloud_run_latency" {
  display_name = "${var.service_name} p95 latency"
  combiner     = "OR"
  enabled      = true

  conditions {
    display_name = "p95 request latency over 30s"
    condition_threshold {
      filter          = "resource.type=\"cloud_run_revision\" AND resource.labels.service_name=\"${var.service_name}\" AND metric.type=\"run.googleapis.com/request_latencies\""
      duration        = "300s"
      comparison      = "COMPARISON_GT"
      threshold_value = 30000
      aggregations {
        alignment_period     = "300s"
        per_series_aligner   = "ALIGN_PERCENTILE_95"
        cross_series_reducer = "REDUCE_MEAN"
        group_by_fields      = ["resource.labels.service_name"]
      }
    }
  }

  notification_channels = var.notification_channel_ids
}
