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
    OES_GEMINI_PROVIDER                  = "vertex"
    OES_VERTEX_PROJECT                   = "ops-evidence-synthesis"
    OES_VERTEX_LOCATION                  = "global"
    OES_GEMINI_MODEL                     = "gemini-3.1-pro-preview"
    OES_GEMINI_MAX_OUTPUT_TOKENS         = "8192"
    OES_GEMINI_THINKING_LEVEL            = "high"
    OES_CLAUDE_PROVIDER                  = "vertex"
    OES_CLAUDE_PROJECT                   = "ops-evidence-synthesis"
    OES_CLAUDE_LOCATION                  = "global"
    OES_CLAUDE_MODEL                     = "claude-haiku-4-5"
    OES_CLAUDE_MAX_OUTPUT_TOKENS         = "8192"
    OES_GPT_OSS_PROVIDER                 = "vertex"
    OES_GPT_OSS_PROJECT                  = "ops-evidence-synthesis"
    OES_GPT_OSS_LOCATION                 = "us-central1"
    OES_GPT_OSS_MODEL                    = "gpt-oss-120b-maas"
    OES_GPT_OSS_MAX_OUTPUT_TOKENS        = "8192"
    OES_GPT_OSS_TIMEOUT_SECONDS          = "240"
    OES_MISTRAL_PROVIDER                 = "vertex"
    OES_MISTRAL_PROJECT                  = "ops-evidence-synthesis"
    OES_MISTRAL_LOCATION                 = "us-central1"
    OES_MISTRAL_MODEL                    = "mistral-medium-3"
    OES_MISTRAL_MAX_OUTPUT_TOKENS        = "8192"
    OES_MISTRAL_TIMEOUT_SECONDS          = "90"
    OES_ALTERNATIVE_PROVIDERS            = "gpt-oss,mistral"
    OES_DISABLED_PROVIDERS               = "claude"
    OES_ENABLE_REAL_AI                   = "1"
    OES_STRUCTURED_LOGGING               = "1"
    OES_BIGQUERY_APPLY_SCHEMA_ON_STARTUP = "0"
    OES_SERVER_PATH_INGEST_ENABLED       = "0"
    OES_REMOTE_COLLECTOR_ENABLED         = "0"
    OES_UI_FAST_INITIAL                  = "1"
    OES_UI_PRECOMPUTED_ONLY              = "1"
    OES_UI_DETAIL_TIMEOUT_MS             = "9500"
    OES_WORKFLOW_MAX_ESTIMATED_COST_USD  = "0"
  }
}
