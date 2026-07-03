variable "project_id" {
  type        = string
  description = "GCP project id."
}

variable "region" {
  type        = string
  description = "Cloud Run region."
  default     = "asia-northeast1"
}

variable "service_name" {
  type        = string
  description = "Cloud Run service name."
  default     = "ops-evidence-api"
}

variable "container_image" {
  type        = string
  description = "Container image for the API service."
}

variable "bigquery_location" {
  type        = string
  description = "BigQuery dataset location."
  default     = "asia-northeast1"
}

variable "bigquery_partition_expiration_ms" {
  type        = number
  description = "Default partition expiration for managed BigQuery tables."
  default     = 5184000000
}

variable "api_write_token_secret" {
  type        = string
  description = "Optional Secret Manager secret id containing OES_API_WRITE_TOKEN."
  default     = ""
}

variable "enable_public_invoker" {
  type        = bool
  description = "Grant allUsers Cloud Run invoker. Keep false unless the app is protected by another gateway."
  default     = false
}

variable "notification_channel_ids" {
  type        = list(string)
  description = "Monitoring notification channel resource names."
  default     = []
}

variable "min_instances" {
  type        = number
  description = "Minimum Cloud Run instances."
  default     = 1
}

variable "max_instances" {
  type        = number
  description = "Maximum Cloud Run instances."
  default     = 20
}

variable "container_concurrency" {
  type        = number
  description = "Cloud Run container concurrency."
  default     = 80
}

variable "timeout_seconds" {
  type        = number
  description = "Cloud Run request timeout seconds."
  default     = 300
}

variable "cpu_limit" {
  type        = string
  description = "Cloud Run container CPU limit."
  default     = "2000m"
}

variable "memory_limit" {
  type        = string
  description = "Cloud Run container memory limit."
  default     = "1Gi"
}

variable "runtime_env" {
  type        = map(string)
  description = "Additional Cloud Run environment variables."
  default = {
    OES_GEMINI_PROVIDER                   = "vertex"
    OES_VERTEX_PROJECT                    = "ops-evidence-synthesis"
    OES_VERTEX_LOCATION                   = "global"
    OES_GEMINI_MODEL                      = "gemini-3.1-pro-preview"
    OES_GEMINI_MAX_OUTPUT_TOKENS          = "8192"
    OES_GEMINI_THINKING_LEVEL             = "high"
    OES_CLAUDE_PROVIDER                   = "vertex"
    OES_CLAUDE_PROJECT                    = "ops-evidence-synthesis"
    OES_CLAUDE_LOCATION                   = "global"
    OES_CLAUDE_MODEL                      = "claude-haiku-4-5"
    OES_CLAUDE_MAX_OUTPUT_TOKENS          = "8192"
    OES_GPT_OSS_PROVIDER                  = "vertex"
    OES_GPT_OSS_PROJECT                   = "ops-evidence-synthesis"
    OES_GPT_OSS_LOCATION                  = "us-central1"
    OES_GPT_OSS_MODEL                     = "gpt-oss-120b-maas"
    OES_GPT_OSS_MAX_OUTPUT_TOKENS         = "8192"
    OES_GPT_OSS_TIMEOUT_SECONDS           = "240"
    OES_MISTRAL_PROVIDER                  = "vertex"
    OES_MISTRAL_PROJECT                   = "ops-evidence-synthesis"
    OES_MISTRAL_LOCATION                  = "us-central1"
    OES_MISTRAL_MODEL                     = "mistral-medium-3"
    OES_MISTRAL_MAX_OUTPUT_TOKENS         = "8192"
    OES_MISTRAL_TIMEOUT_SECONDS           = "90"
    OES_LLAMA_PROVIDER                    = "vertex"
    OES_LLAMA_PROJECT                     = "ops-evidence-synthesis"
    OES_LLAMA_LOCATION                    = "us-east5"
    OES_LLAMA_MODEL                       = "llama-4-maverick-17b-128e-instruct-maas"
    OES_LLAMA_MAX_OUTPUT_TOKENS           = "8192"
    OES_LLAMA_TIMEOUT_SECONDS             = "240"
    OES_ALTERNATIVE_PROVIDERS             = "gpt-oss,llama"
    OES_DISABLED_PROVIDERS                = "claude"
    OES_ENABLE_REAL_AI                    = "1"
    OES_STRUCTURED_LOGGING                = "1"
    OES_BIGQUERY_APPLY_SCHEMA_ON_STARTUP  = "0"
    OES_SERVER_PATH_INGEST_ENABLED        = "0"
    OES_REMOTE_COLLECTOR_ENABLED          = "0"
    OES_UI_FAST_INITIAL                   = "1"
    OES_UI_PRECOMPUTED_ONLY               = "1"
    OES_UI_DETAIL_TIMEOUT_MS              = "9500"
    OES_PUBLIC_FAST_GCP_REVIEW_ENABLED    = "1"
    OES_FAST_GCP_GEMINI_MODEL             = "gemini-3.1-flash-lite"
    OES_FAST_GCP_GEMINI_THINKING_LEVEL    = "minimal"
    OES_FAST_GCP_GEMINI_MAX_OUTPUT_TOKENS = "4096"
    OES_FAST_GCP_GEMINI_TIMEOUT_SECONDS   = "45"
    OES_FAST_GCP_REVIEW_SAMPLE_ROWS       = "240"
    OES_WORKFLOW_MAX_ESTIMATED_COST_USD   = "0"
  }
}

variable "private_artifact_bucket_name" {
  type        = string
  description = "Private GCS bucket for sanitized job inputs and recorded outputs."
}

variable "private_artifact_bucket_location" {
  type        = string
  description = "Private artifact bucket location."
  default     = "ASIA-NORTHEAST1"
}

variable "private_artifact_bucket_retention_days" {
  type        = number
  description = "Retention window for private sanitized job artifacts."
  default     = 45
}

variable "postgres_instance_name" {
  type        = string
  description = "Cloud SQL PostgreSQL instance name for provider chunk ledger state."
  default     = "ops-evidence-ledger"
}

variable "postgres_database_version" {
  type        = string
  description = "Cloud SQL PostgreSQL engine version."
  default     = "POSTGRES_16"
}

variable "postgres_database_name" {
  type        = string
  description = "PostgreSQL database name for chunk ledger tables."
  default     = "ops_evidence"
}

variable "postgres_user_name" {
  type        = string
  description = "PostgreSQL user name for chunk ledger writes."
  default     = "ops_evidence_job"
}

variable "postgres_password" {
  type        = string
  description = "PostgreSQL password used to create the ledger user. Keep this in a tfvars file or secret pipeline."
  sensitive   = true
}

variable "postgres_password_secret" {
  type        = string
  description = "Secret Manager secret id containing the PostgreSQL password for Cloud Run Job runtime."
  default     = ""
}

variable "postgres_tier" {
  type        = string
  description = "Cloud SQL machine tier."
  default     = "db-custom-1-3840"
}

variable "postgres_availability_type" {
  type        = string
  description = "Cloud SQL availability type."
  default     = "ZONAL"
}

variable "postgres_disk_size_gb" {
  type        = number
  description = "Initial Cloud SQL disk size in GB."
  default     = 20
}

variable "postgres_deletion_protection" {
  type        = bool
  description = "Enable Cloud SQL deletion protection."
  default     = true
}

variable "postgres_ipv4_enabled" {
  type        = bool
  description = "Enable Cloud SQL public IPv4 endpoint. Access is still controlled by Cloud SQL IAM/client authentication."
  default     = true
}

variable "chunked_review_job_name" {
  type        = string
  description = "Cloud Run Job name for private chunked review execution."
  default     = "ops-evidence-chunked-review"
}

variable "chunked_review_job_timeout_seconds" {
  type        = number
  description = "Cloud Run Job task timeout seconds."
  default     = 3600
}

variable "chunked_review_job_max_retries" {
  type        = number
  description = "Cloud Run Job task retry count."
  default     = 0
}

variable "chunked_review_job_cpu_limit" {
  type        = string
  description = "Cloud Run Job CPU limit."
  default     = "4000m"
}

variable "chunked_review_job_memory_limit" {
  type        = string
  description = "Cloud Run Job memory limit."
  default     = "4Gi"
}

variable "chunked_review_job_provider_mode" {
  type        = string
  description = "Provider execution mode for the private chunked review job."
  default     = "real_or_skip"
}

variable "chunked_review_job_providers" {
  type        = list(string)
  description = "Provider aliases passed to the private chunked review job."
  default     = ["gemini", "gpt-oss", "llama", "qwen", "glm"]
}

variable "chunked_review_job_chunk_workers" {
  type        = number
  description = "Maximum per-provider chunk worker count used by the job."
  default     = 60
}

variable "chunked_review_job_provider_workers" {
  type        = number
  description = "Provider-level worker count used by the job."
  default     = 5
}

variable "chunked_review_job_chunk_retry_attempts" {
  type        = number
  description = "Retry attempts per chunk before status is recorded fail-closed."
  default     = 2
}

variable "chunked_review_job_env" {
  type        = map(string)
  description = "Additional Cloud Run Job environment variables."
  default     = {}
}
