# Terraform baseline

This directory defines the production baseline for the API service and private
chunked review job:

- Cloud Run v2 service
- Cloud Run v2 Job for private full-corpus provider runs
- Private GCS bucket for sanitized job inputs, recorded outputs, and precomputed
  review payload handoff
- Cloud SQL for PostgreSQL provider chunk ledger state
- Runtime service account
- BigQuery datasets for raw evidence and synthesis output
- BigQuery tables from `gcp/bigquery/schema.sql`
- IAM required for BigQuery, Vertex AI, GCS, Cloud SQL, and optional secrets
- Cloud Monitoring alert policies for 5xx rate and p95 latency

Use `scripts/terraform_docker.sh` from the repository root when Terraform is not
installed locally. The wrapper runs `hashicorp/terraform:1.9.8` and passes a
short-lived `gcloud auth print-access-token` token into the container.

Keep API write tokens and the PostgreSQL runtime password in Secret Manager and
pass only secret ids through `api_write_token_secret` and
`postgres_password_secret`. The `postgres_password` variable is only for
provisioning the Cloud SQL user; keep it in a private tfvars file or secret
pipeline.
For reproducible manual deploys, set `container_image` to an Artifact Registry
digest (`...@sha256:...`) after Cloud Build finishes.

The private job expects sanitized inputs, for example:

```bash
gcloud storage cp evidence_bundle.json gs://BUCKET/job-inputs/run-001/evidence_bundle.json
gcloud run jobs execute ops-evidence-chunked-review \
  --region asia-northeast1 \
  --update-env-vars OES_JOB_INPUT_BUNDLE_URI=gs://BUCKET/job-inputs/run-001/evidence_bundle.json,OES_JOB_RUN_ID=run-001
```

Optional `OES_JOB_APPROVED_PROFILE_URI`, `OES_JOB_SOURCE_CONTEXT_URI`, and
`OES_JOB_SOURCE_ANALYSIS_URI` should also point at sanitized artifacts.

Example:

```bash
scripts/terraform_docker.sh init -input=false
scripts/terraform_docker.sh plan -input=false
scripts/terraform_docker.sh apply -input=false
```
