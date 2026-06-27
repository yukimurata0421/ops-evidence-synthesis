# Terraform baseline

This directory defines the production baseline for the API service:

- Cloud Run v2 service
- Runtime service account
- BigQuery datasets for raw evidence and synthesis output
- BigQuery tables from `gcp/bigquery/schema.sql`
- IAM required for BigQuery and Vertex AI
- Cloud Monitoring alert policies for 5xx rate and p95 latency

Use `scripts/terraform_docker.sh` from the repository root when Terraform is not
installed locally. The wrapper runs `hashicorp/terraform:1.9.8` and passes a
short-lived `gcloud auth print-access-token` token into the container.

Keep API write tokens in Secret Manager and pass only the secret id through
`api_write_token_secret`; do not put plaintext tokens in Terraform variables.
For reproducible manual deploys, set `container_image` to an Artifact Registry
digest (`...@sha256:...`) after Cloud Build finishes.

Example:

```bash
scripts/terraform_docker.sh init -input=false
scripts/terraform_docker.sh plan -input=false
scripts/terraform_docker.sh apply -input=false
```
