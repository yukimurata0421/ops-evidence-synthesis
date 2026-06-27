#!/usr/bin/env bash
set -euo pipefail

PROJECT_ID="${PROJECT_ID:-ops-evidence-synthesis}"
REGION="${REGION:-asia-northeast1}"
SERVICE_NAME="${SERVICE_NAME:-ops-evidence-api}"
REPOSITORY="${REPOSITORY:-ops-evidence}"
IMAGE_NAME="${IMAGE_NAME:-ops-evidence-api}"
TAG="${TAG:-public-demo-$(date -u +%Y%m%d%H%M%S)}"

if [[ -n "${PYTHON:-}" ]]; then
  PYTHON_BIN="${PYTHON}"
elif [[ -x ".venv/bin/python" ]]; then
  PYTHON_BIN=".venv/bin/python"
else
  PYTHON_BIN="python3"
fi

IMAGE_URI="${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPOSITORY}/${IMAGE_NAME}:${TAG}"

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

gcloud run services update "${SERVICE_NAME}" \
  --project "${PROJECT_ID}" \
  --region "${REGION}" \
  --image "${IMAGE_URI}" \
  --update-env-vars "OES_UI_PRECOMPUTED_ONLY=1,OES_UI_FAST_INITIAL=1"

SERVICE_URL="$(
  gcloud run services describe "${SERVICE_NAME}" \
    --project "${PROJECT_ID}" \
    --region "${REGION}" \
    --format 'value(status.url)'
)"

make PYTHON="${PYTHON_BIN}" PUBLIC_BASE_URL="${PUBLIC_BASE_URL:-${SERVICE_URL}}" smoke-public

echo "deployed_image=${IMAGE_URI}"
echo "service_url=${SERVICE_URL}"
