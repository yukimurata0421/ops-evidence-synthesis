#!/usr/bin/env bash
set -euo pipefail

PROJECT_ID="${PROJECT_ID:-ops-evidence-synthesis}"
REGION="${REGION:-asia-northeast1}"
SERVICE_NAME="${SERVICE_NAME:-ops-evidence-api}"
REPOSITORY="${REPOSITORY:-ops-evidence}"
IMAGE_NAME="${IMAGE_NAME:-ops-evidence-api}"
TAG="${TAG:-public-demo-$(date -u +%Y%m%d%H%M%S)}"
PUBLIC_BASE_URL="${PUBLIC_BASE_URL:-https://ops-evidence.yukimurata0421.dev}"

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

DIGEST_IMAGE_URI="$(
  gcloud artifacts docker images describe "${IMAGE_URI}" \
    --project "${PROJECT_ID}" \
    --format 'value(image_summary.fully_qualified_digest)'
)"
if [[ -z "${DIGEST_IMAGE_URI}" ]]; then
  echo "failed to resolve image digest for ${IMAGE_URI}" >&2
  exit 2
fi

gcloud run services update "${SERVICE_NAME}" \
  --project "${PROJECT_ID}" \
  --region "${REGION}" \
  --image "${DIGEST_IMAGE_URI}" \
  --update-env-vars "OES_UI_PRECOMPUTED_ONLY=1,OES_UI_FAST_INITIAL=1"

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
