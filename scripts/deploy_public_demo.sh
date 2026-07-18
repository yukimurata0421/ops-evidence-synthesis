#!/usr/bin/env bash
set -euo pipefail

PROJECT_ID="${PROJECT_ID:-ops-evidence-synthesis}"
REGION="${REGION:-asia-northeast1}"
SERVICE_NAME="${SERVICE_NAME:-ops-evidence-api}"
REPOSITORY="${REPOSITORY:-ops-evidence}"
IMAGE_NAME="${IMAGE_NAME:-ops-evidence-api}"
TAG="${TAG:-public-demo-$(date -u +%Y%m%d%H%M%S)}"
PUBLIC_BASE_URL="${PUBLIC_BASE_URL:-https://ops-evidence.yukimurata0421.dev}"
PRIVATE_ARTIFACT_BUCKET="${PRIVATE_ARTIFACT_BUCKET:-${PROJECT_ID}-private-artifacts}"
PRECOMPUTED_REVIEW_GCS_PREFIX="${PRECOMPUTED_REVIEW_GCS_PREFIX:-gs://${PRIVATE_ARTIFACT_BUCKET}/precomputed_review_summaries}"
PRECOMPUTED_REVIEW_CACHE_SECONDS="${PRECOMPUTED_REVIEW_CACHE_SECONDS:-0}"
PRECOMPUTED_REVIEW_GCS_FIRST="${PRECOMPUTED_REVIEW_GCS_FIRST:-1}"
PUBLIC_FAST_GCP_REVIEW_CACHE_SECONDS="${PUBLIC_FAST_GCP_REVIEW_CACHE_SECONDS:-3600}"
PUBLIC_FAST_GCP_REVIEW_DAILY_LIMIT="${PUBLIC_FAST_GCP_REVIEW_DAILY_LIMIT:-12}"
PUBLIC_FAST_GCP_REVIEW_CLIENT_DAILY_LIMIT="${PUBLIC_FAST_GCP_REVIEW_CLIENT_DAILY_LIMIT:-2}"
PUBLIC_FAST_GCP_REVIEW_MAX_INSTANCES="${PUBLIC_FAST_GCP_REVIEW_MAX_INSTANCES:-1}"
PUBLIC_FAST_GCP_REVIEW_CONCURRENCY="${PUBLIC_FAST_GCP_REVIEW_CONCURRENCY:-5}"
PUBLIC_FAST_GCP_REVIEW_TIMEOUT_SECONDS="${PUBLIC_FAST_GCP_REVIEW_TIMEOUT_SECONDS:-900}"
PUBLIC_RATE_LIMIT_ENABLED="${PUBLIC_RATE_LIMIT_ENABLED:-1}"
PUBLIC_RATE_LIMIT_MAX_REQUESTS="${PUBLIC_RATE_LIMIT_MAX_REQUESTS:-120}"
PUBLIC_ACTION_RATE_LIMIT_MAX_REQUESTS="${PUBLIC_ACTION_RATE_LIMIT_MAX_REQUESTS:-8}"
PUBLIC_FAST_GCP_REVIEW_OWNER_TOKEN_SECRET="${PUBLIC_FAST_GCP_REVIEW_OWNER_TOKEN_SECRET:-ops-evidence-fast-gcp-review-owner-token}"
API_WRITE_TOKEN_SECRET="${API_WRITE_TOKEN_SECRET:-ops-evidence-api-write-token}"
FAST_GCP_REVIEW_SAMPLE_ROWS="${FAST_GCP_REVIEW_SAMPLE_ROWS:-2000}"
FAST_GCP_CROSS_CHECK_SAMPLE_ROWS="${FAST_GCP_CROSS_CHECK_SAMPLE_ROWS:-200}"

if [[ -n "$(git status --porcelain --untracked-files=all)" ]]; then
  echo "deployment requires a clean repository so the published head identifies the built source" >&2
  exit 2
fi
PUBLISHED_REPOSITORY_HEAD_SHA="$(git rev-parse HEAD)"

if [[ -n "${PYTHON:-}" ]]; then
  PYTHON_BIN="${PYTHON}"
elif [[ -x ".venv/bin/python" ]]; then
  PYTHON_BIN=".venv/bin/python"
else
  PYTHON_BIN="python3"
fi

IMAGE_URI="${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPOSITORY}/${IMAGE_NAME}:${TAG}"

if ! gcloud storage buckets describe "gs://${PRIVATE_ARTIFACT_BUCKET}" --project "${PROJECT_ID}" >/dev/null 2>&1; then
  echo "private artifact bucket does not exist: gs://${PRIVATE_ARTIFACT_BUCKET}" >&2
  echo "Fast GCP Review needs this bucket so generated review URLs survive beyond the serving instance cache." >&2
  exit 2
fi

if ! gcloud secrets describe "${API_WRITE_TOKEN_SECRET}" --project "${PROJECT_ID}" >/dev/null 2>&1; then
  echo "api write token secret not found: ${API_WRITE_TOKEN_SECRET}" >&2
  exit 2
fi

API_WRITE_TOKEN_SECRET_ARGS=(--update-secrets "OES_API_WRITE_TOKEN=${API_WRITE_TOKEN_SECRET}:latest")

OWNER_TOKEN_SECRET_ARGS=()
if gcloud secrets describe "${PUBLIC_FAST_GCP_REVIEW_OWNER_TOKEN_SECRET}" --project "${PROJECT_ID}" >/dev/null 2>&1; then
  OWNER_TOKEN_SECRET_ARGS=(--update-secrets "OES_PUBLIC_FAST_GCP_REVIEW_OWNER_TOKEN=${PUBLIC_FAST_GCP_REVIEW_OWNER_TOKEN_SECRET}:latest")
else
  echo "owner token secret not found; owner quota bypass will be disabled: ${PUBLIC_FAST_GCP_REVIEW_OWNER_TOKEN_SECRET}" >&2
fi

PUBLIC_FAST_GCP_REVIEW_DISABLE_GCS_URI="${PUBLIC_FAST_GCP_REVIEW_DISABLE_GCS_URI:-${PRECOMPUTED_REVIEW_GCS_PREFIX}/public-fast-gcp-review-disabled.json}"

make PYTHON="${PYTHON_BIN}" ci

if command -v gitleaks >/dev/null 2>&1; then
  gitleaks detect --source . --no-banner
else
  echo "gitleaks was not found; skipping local secret scan"
fi

gcloud builds submit \
  --project "${PROJECT_ID}" \
  --config cloudbuild.yaml \
  --substitutions "_REGION=${REGION},_REPOSITORY=${REPOSITORY},_IMAGE=${IMAGE_NAME},_TAG=${TAG}" \
  .

DIGEST_IMAGE_URI="$(
  gcloud artifacts docker images describe "${IMAGE_URI}" \
    --project "${PROJECT_ID}" \
    --format 'value(image_summary.fully_qualified_digest)'
)"
if [[ -z "${DIGEST_IMAGE_URI}" ]]; then
  echo "failed to resolve image digest for ${IMAGE_URI}" >&2
  exit 2
fi
DEPLOYED_IMAGE_DIGEST="${DIGEST_IMAGE_URI##*@}"

gcloud run services update "${SERVICE_NAME}" \
  --project "${PROJECT_ID}" \
  --region "${REGION}" \
  --image "${DIGEST_IMAGE_URI}" \
  --max-instances "${PUBLIC_FAST_GCP_REVIEW_MAX_INSTANCES}" \
  --concurrency "${PUBLIC_FAST_GCP_REVIEW_CONCURRENCY}" \
  --timeout "${PUBLIC_FAST_GCP_REVIEW_TIMEOUT_SECONDS}" \
  "${API_WRITE_TOKEN_SECRET_ARGS[@]}" \
  "${OWNER_TOKEN_SECRET_ARGS[@]}" \
  --update-env-vars "OES_PUBLIC_RUNTIME_GUARD=1,OES_UI_PRECOMPUTED_ONLY=1,OES_UI_FAST_INITIAL=1,OES_PUBLIC_FAST_GCP_REVIEW_ENABLED=1,OES_PRECOMPUTED_REVIEW_CACHE_SECONDS=${PRECOMPUTED_REVIEW_CACHE_SECONDS},OES_PRECOMPUTED_REVIEW_GCS_FIRST=${PRECOMPUTED_REVIEW_GCS_FIRST},OES_PUBLIC_FAST_GCP_REVIEW_CACHE_SECONDS=${PUBLIC_FAST_GCP_REVIEW_CACHE_SECONDS},OES_PUBLIC_FAST_GCP_REVIEW_DAILY_LIMIT=${PUBLIC_FAST_GCP_REVIEW_DAILY_LIMIT},OES_PUBLIC_FAST_GCP_REVIEW_CLIENT_DAILY_LIMIT=${PUBLIC_FAST_GCP_REVIEW_CLIENT_DAILY_LIMIT},OES_PUBLIC_FAST_GCP_REVIEW_DISABLE_GCS_URI=${PUBLIC_FAST_GCP_REVIEW_DISABLE_GCS_URI},OES_PUBLIC_FAST_GCP_REVIEW_DISABLE_CACHE_SECONDS=30,OES_PUBLIC_RATE_LIMIT_ENABLED=${PUBLIC_RATE_LIMIT_ENABLED},OES_PUBLIC_RATE_LIMIT_MAX_REQUESTS=${PUBLIC_RATE_LIMIT_MAX_REQUESTS},OES_PUBLIC_ACTION_RATE_LIMIT_MAX_REQUESTS=${PUBLIC_ACTION_RATE_LIMIT_MAX_REQUESTS},OES_ENABLE_REAL_AI=1,OES_FAST_GCP_GEMINI_MODEL=gemini-3.1-flash-lite,OES_FAST_GCP_GEMINI_THINKING_LEVEL=minimal,OES_FAST_GCP_GEMINI_MAX_OUTPUT_TOKENS=4096,OES_FAST_GCP_GEMINI_TIMEOUT_SECONDS=45,OES_GEMMA_MODEL=gemma-4-26b-a4b-it-maas,OES_GEMMA_LOCATION=global,OES_GEMMA_MAX_OUTPUT_TOKENS=8192,OES_GEMMA_TIMEOUT_SECONDS=240,OES_MULTI_AI_MAX_WORKERS=2,OES_FAST_GCP_REVIEW_SAMPLE_ROWS=${FAST_GCP_REVIEW_SAMPLE_ROWS},OES_FAST_GCP_CROSS_CHECK_SAMPLE_ROWS=${FAST_GCP_CROSS_CHECK_SAMPLE_ROWS},OES_PRECOMPUTED_REVIEW_GCS_PREFIX=${PRECOMPUTED_REVIEW_GCS_PREFIX},OES_FAST_GCP_REVIEW_GCS_PREFIX=${PRECOMPUTED_REVIEW_GCS_PREFIX},OES_PUBLISHED_REPOSITORY_HEAD_SHA=${PUBLISHED_REPOSITORY_HEAD_SHA},OES_DEPLOYED_IMAGE_DIGEST=${DEPLOYED_IMAGE_DIGEST}"

TARGET_REVISION="$(
  gcloud run services describe "${SERVICE_NAME}" \
    --project "${PROJECT_ID}" \
    --region "${REGION}" \
    --format 'value(status.latestCreatedRevisionName)'
)"
if [[ -z "${TARGET_REVISION}" ]]; then
  echo "failed to resolve latest created Cloud Run revision" >&2
  exit 2
fi

READY_REVISION="$(
  for _attempt in {1..90}; do
    ready_status="$(
      gcloud run revisions describe "${TARGET_REVISION}" \
        --project "${PROJECT_ID}" \
        --region "${REGION}" \
        --format 'value(status.conditions[0].status)' 2>/dev/null || true
    )"
    if [[ "${ready_status}" == "True" ]]; then
      echo "${TARGET_REVISION}"
      break
    fi
    sleep 2
  done
)"
if [[ "${READY_REVISION}" != "${TARGET_REVISION}" ]]; then
  echo "latest created revision did not become ready: target=${TARGET_REVISION}, ready=${READY_REVISION:-<none>}" >&2
  exit 2
fi

gcloud run services update-traffic "${SERVICE_NAME}" \
  --project "${PROJECT_ID}" \
  --region "${REGION}" \
  --to-revisions "${READY_REVISION}=100"

SERVICE_URL="$(
  gcloud run services describe "${SERVICE_NAME}" \
    --project "${PROJECT_ID}" \
    --region "${REGION}" \
    --format 'value(status.url)'
)"
DEPLOYED_IMAGE="$(
  gcloud run services describe "${SERVICE_NAME}" \
    --project "${PROJECT_ID}" \
    --region "${REGION}" \
    --format 'value(spec.template.spec.containers[0].image)'
)"
if [[ "${DEPLOYED_IMAGE}" != "${DIGEST_IMAGE_URI}" ]]; then
  echo "deployed image mismatch: expected ${DIGEST_IMAGE_URI}, got ${DEPLOYED_IMAGE}" >&2
  exit 2
fi
TRAFFIC_REVISION="$(
  gcloud run services describe "${SERVICE_NAME}" \
    --project "${PROJECT_ID}" \
    --region "${REGION}" \
    --format 'value(status.traffic[0].revisionName)'
)"
TRAFFIC_PERCENT="$(
  gcloud run services describe "${SERVICE_NAME}" \
    --project "${PROJECT_ID}" \
    --region "${REGION}" \
    --format 'value(status.traffic[0].percent)'
)"
if [[ "${TRAFFIC_REVISION}" != "${READY_REVISION}" || "${TRAFFIC_PERCENT}" != "100" ]]; then
  echo "traffic mismatch: expected ${READY_REVISION}=100, got ${TRAFFIC_REVISION}=${TRAFFIC_PERCENT}" >&2
  exit 2
fi

make PYTHON="${PYTHON_BIN}" PUBLIC_BASE_URL="${PUBLIC_BASE_URL}" smoke-public

echo "tagged_image=${IMAGE_URI}"
echo "deployed_image=${DIGEST_IMAGE_URI}"
echo "deployed_revision=${READY_REVISION}"
echo "service_url=${SERVICE_URL}"
echo "public_url=${PUBLIC_BASE_URL}"
