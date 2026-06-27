#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TF_DIR="$ROOT/infra/terraform"
IMAGE="${TERRAFORM_DOCKER_IMAGE:-hashicorp/terraform:1.9.8}"

if [[ $# -eq 0 ]]; then
  echo "usage: scripts/terraform_docker.sh <terraform-args>" >&2
  exit 2
fi

TOKEN="$(gcloud auth print-access-token)"

docker run --rm \
  -e GOOGLE_OAUTH_ACCESS_TOKEN="$TOKEN" \
  -v "$TF_DIR:/workspace" \
  -w /workspace \
  "$IMAGE" "$@"
