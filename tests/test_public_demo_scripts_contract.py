from __future__ import annotations

import re
from pathlib import Path

from ops_evidence_synthesis.gcp.chunked_review_job import DEFAULT_JOB_PROVIDERS


ROOT = Path(__file__).resolve().parents[1]


def test_deploy_public_demo_preserves_fast_review_runtime_contract() -> None:
    script = (ROOT / "scripts" / "deploy_public_demo.sh").read_text(encoding="utf-8")

    assert 'FAST_GCP_REVIEW_SAMPLE_ROWS="${FAST_GCP_REVIEW_SAMPLE_ROWS:-2000}"' in script
    assert 'PUBLIC_FAST_GCP_REVIEW_CACHE_SECONDS="${PUBLIC_FAST_GCP_REVIEW_CACHE_SECONDS:-3600}"' in script
    assert 'PUBLIC_FAST_GCP_REVIEW_DAILY_LIMIT="${PUBLIC_FAST_GCP_REVIEW_DAILY_LIMIT:-12}"' in script
    assert 'PUBLIC_FAST_GCP_REVIEW_CLIENT_DAILY_LIMIT="${PUBLIC_FAST_GCP_REVIEW_CLIENT_DAILY_LIMIT:-2}"' in script
    assert 'PUBLIC_FAST_GCP_REVIEW_MAX_INSTANCES="${PUBLIC_FAST_GCP_REVIEW_MAX_INSTANCES:-1}"' in script
    assert 'PUBLIC_FAST_GCP_REVIEW_CONCURRENCY="${PUBLIC_FAST_GCP_REVIEW_CONCURRENCY:-5}"' in script
    assert 'PUBLIC_RATE_LIMIT_ENABLED="${PUBLIC_RATE_LIMIT_ENABLED:-1}"' in script
    assert 'PUBLIC_RATE_LIMIT_MAX_REQUESTS="${PUBLIC_RATE_LIMIT_MAX_REQUESTS:-120}"' in script
    assert 'PUBLIC_ACTION_RATE_LIMIT_MAX_REQUESTS="${PUBLIC_ACTION_RATE_LIMIT_MAX_REQUESTS:-8}"' in script
    assert 'API_WRITE_TOKEN_SECRET="${API_WRITE_TOKEN_SECRET:-ops-evidence-api-write-token}"' in script
    assert "OES_FAST_GCP_GEMINI_MODEL=gemini-3.1-flash-lite" in script
    assert "OES_PUBLIC_RUNTIME_GUARD=1" in script
    assert "OES_FAST_GCP_REVIEW_SAMPLE_ROWS=${FAST_GCP_REVIEW_SAMPLE_ROWS}" in script
    assert "OES_PUBLIC_FAST_GCP_REVIEW_CACHE_SECONDS=${PUBLIC_FAST_GCP_REVIEW_CACHE_SECONDS}" in script
    assert "OES_PUBLIC_FAST_GCP_REVIEW_DAILY_LIMIT=${PUBLIC_FAST_GCP_REVIEW_DAILY_LIMIT}" in script
    assert "OES_PUBLIC_FAST_GCP_REVIEW_CLIENT_DAILY_LIMIT=${PUBLIC_FAST_GCP_REVIEW_CLIENT_DAILY_LIMIT}" in script
    assert "OES_PUBLIC_FAST_GCP_REVIEW_DISABLE_GCS_URI=${PUBLIC_FAST_GCP_REVIEW_DISABLE_GCS_URI}" in script
    assert "OES_PUBLIC_RATE_LIMIT_ENABLED=${PUBLIC_RATE_LIMIT_ENABLED}" in script


def test_fast_gcp_and_cloud_run_job_config_keep_provider_storage_and_model_contracts() -> None:
    deploy_script = (ROOT / "scripts" / "deploy_public_demo.sh").read_text(encoding="utf-8")
    main_tf = (ROOT / "infra" / "terraform" / "main.tf").read_text(encoding="utf-8")
    variables_tf = (ROOT / "infra" / "terraform" / "variables.tf").read_text(encoding="utf-8")
    job_source = (ROOT / "src" / "ops_evidence_synthesis" / "gcp" / "chunked_review_job.py").read_text(
        encoding="utf-8"
    )

    expected_providers = ("gemini", "gpt-oss", "mistral", "qwen", "gemma")
    assert DEFAULT_JOB_PROVIDERS == expected_providers
    assert 'DEFAULT_JOB_PROVIDERS = ("gemini", "gpt-oss", "mistral", "qwen", "gemma")' in job_source
    assert 'default     = ["gemini", "gpt-oss", "mistral", "qwen", "gemma"]' in variables_tf
    assert 'OES_JOB_PROVIDERS                     = join(",", var.chunked_review_job_providers)' in main_tf
    assert 'OES_JOB_PROVIDER_MODE                 = var.chunked_review_job_provider_mode' in main_tf
    assert 'OES_CHUNK_RUN_STORE                   = "postgres"' in main_tf
    assert "OES_CLOUD_SQL_CONNECTION_NAME" in main_tf
    assert 'name = "OES_POSTGRES_PASSWORD"' in main_tf
    assert 'mount_path = "/cloudsql"' in main_tf
    assert "cloud_sql_instance" in main_tf
    assert 'command = ["python", "-m", "ops_evidence_synthesis.gcp.chunked_review_job"]' in main_tf
    assert 'OES_JOB_OUTPUT_PREFIX_URI             = "gs://${google_storage_bucket.private_artifacts.name}/job-runs"' in main_tf
    assert (
        'OES_JOB_PRECOMPUTED_OUTPUT_PREFIX_URI = "gs://${google_storage_bucket.private_artifacts.name}/precomputed_review_summaries"'
        in main_tf
    )
    assert (
        'OES_JOB_STATIC_REVIEW_OUTPUT_PREFIX_URI = "gs://${google_storage_bucket.private_artifacts.name}/review-pages"'
        in main_tf
    )
    assert 'public_access_prevention    = "enforced"' in main_tf
    assert "uniform_bucket_level_access = true" in main_tf
    assert re.search(r'OES_QWEN_LOCATION\s*=\s*"global"', variables_tf)
    assert re.search(r'OES_GEMMA_LOCATION\s*=\s*"global"', variables_tf)
    assert re.search(r'OES_MISTRAL_MODEL\s*=\s*"mistral-small-2503"', variables_tf)
    assert re.search(r'OES_GEMMA_MODEL\s*=\s*"gemma-4-26b-a4b-it-maas"', variables_tf)
    assert 'FAST_GCP_REVIEW_SAMPLE_ROWS="${FAST_GCP_REVIEW_SAMPLE_ROWS:-2000}"' in deploy_script
    assert "OES_FAST_GCP_GEMINI_MODEL=gemini-3.1-flash-lite" in deploy_script
    assert "OES_GEMMA_MODEL=gemma-4-26b-a4b-it-maas" in deploy_script
    assert "OES_GEMMA_LOCATION=global" in deploy_script
    assert "OES_PUBLIC_FAST_GCP_REVIEW_DISABLE_CACHE_SECONDS=30" in deploy_script


def test_deploy_public_demo_keeps_ci_secret_scan_digest_and_smoke_gates() -> None:
    script = (ROOT / "scripts" / "deploy_public_demo.sh").read_text(encoding="utf-8")

    assert 'make PYTHON="${PYTHON_BIN}" ci' in script
    assert "gitleaks detect --source . --no-banner" in script
    assert "api write token secret not found" in script
    assert '--update-secrets "OES_API_WRITE_TOKEN=${API_WRITE_TOKEN_SECRET}:latest"' in script
    assert "gcloud artifacts docker images describe" in script
    assert 'if [[ -z "${DIGEST_IMAGE_URI}" ]]; then' in script
    assert 'if [[ "${DEPLOYED_IMAGE}" != "${DIGEST_IMAGE_URI}" ]]; then' in script
    assert 'if [[ "${TRAFFIC_REVISION}" != "${READY_REVISION}" || "${TRAFFIC_PERCENT}" != "100" ]]; then' in script
    assert 'make PYTHON="${PYTHON_BIN}" PUBLIC_BASE_URL="${PUBLIC_BASE_URL}" smoke-public' in script


def test_generate_precomputed_review_script_records_public_review_safety_terms() -> None:
    script = (ROOT / "scripts" / "generate_precomputed_review_from_multi_run.py").read_text(encoding="utf-8")

    assert "Excluded from convergence denominator." in script
    assert "Provider convergence can create high-priority validation work" in script
    assert "requires enough runtime evidence" in script
    assert "incident promotion is not auto-accepted" in script
    assert "Incident gate signal is a graph-level support signal" in script
    assert "Convergence score = claimed successful providers / all successful providers" in script


def test_cloudflare_waf_script_uses_http_ratelimit_ruleset_contract() -> None:
    script = (ROOT / "scripts" / "configure_cloudflare_waf.py").read_text(encoding="utf-8")

    assert 'phase": "http_ratelimit"' in script
    assert '"/client/v4/zones/{zone_id}/rulesets/phases/http_ratelimit/entrypoint"' in script
    assert '"ratelimit"' in script
    assert '"characteristics": ["cf.colo.id", "ip.src"]' in script
    assert '"/public/fast-gcp-review"' in script
    assert 'RULE_MARKER = "Ops Evidence public demo"' in script


def test_budget_guard_script_wires_budget_pubsub_and_runtime_kill_switch() -> None:
    script = (ROOT / "scripts" / "configure_budget_fast_gcp_guard.py").read_text(encoding="utf-8")

    assert "gcloud" in script
    assert "billing" in script
    assert "budgets" in script
    assert "--notifications-rule-pubsub-topic" in script
    assert "pubsub" in script
    assert "--push-endpoint" in script
    assert "/internal/budget-guard/fast-gcp-review" in script
    assert "OES_PUBLIC_FAST_GCP_REVIEW_DISABLE_GCS_URI" in script
    assert "OES_BUDGET_GUARD_TOKEN" in script
