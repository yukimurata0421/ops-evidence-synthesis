#!/usr/bin/env bash
set -euo pipefail

if [[ "${OES_ENABLE_REAL_AI:-}" != "1" ]]; then
  echo "real multi-AI demo: skipped"
  echo "Set OES_ENABLE_REAL_AI=1 and Vertex/provider environment variables to run real providers."
  exit 0
fi

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="${PYTHON:-$ROOT/.venv/bin/python}"
CLI=("$PYTHON" -m ops_evidence_synthesis.cli)
OUT="${OUT:-/tmp/ops_demo_multi_ai_real}"
SANITIZED="$OUT/sanitized"
DISCOVERY="$OUT/profile_discovery"
PLAN_DIR="$OUT/evidence_plan"
MULTI_AI="$OUT/multi_ai_real"

cd "$ROOT"
rm -rf "$OUT"
mkdir -p "$OUT"

"${CLI[@]}" sanitize sample_logs/secret_heavy.jsonl --out "$SANITIZED" >/dev/null
echo "sanitize: OK"

"${CLI[@]}" verify-sanitized "$SANITIZED" >/dev/null
echo "verify: OK"

"${CLI[@]}" build-bundle "$SANITIZED/sanitized_events.jsonl" \
  --service demo-payment \
  --environment prod \
  --start 2026-06-16T00:00:00Z \
  --end 2026-06-16T18:00:00Z \
  --profile generic \
  --out "$OUT/generic_evidence_bundle.json" >/dev/null
echo "build generic bundle: OK"

"${CLI[@]}" discover-profile \
  --project-root sample_projects/profile_discovery_sample \
  --evidence-bundle "$OUT/generic_evidence_bundle.json" \
  --service demo-payment \
  --environment prod \
  --out "$DISCOVERY" >/dev/null
echo "discover-profile: OK"

"${CLI[@]}" draft-profile \
  --discovery-bundle "$DISCOVERY/profile_discovery_bundle.json" \
  --provider local \
  --out "$OUT/profile_draft.json" >/dev/null
echo "draft-profile: OK"

"${CLI[@]}" approve-profile \
  --profile-draft "$OUT/profile_draft.json" \
  --profile-id demo-payment-approved \
  --approved-by demo-real-reviewer \
  --out "$OUT/approved_profile.yaml" >/dev/null
echo "approve-profile: OK"

"${CLI[@]}" build-bundle "$SANITIZED/sanitized_events.jsonl" \
  --service demo-payment \
  --environment prod \
  --start 2026-06-16T00:00:00Z \
  --end 2026-06-16T18:00:00Z \
  --profile "$OUT/approved_profile.yaml" \
  --out "$OUT/explicit_evidence_bundle.json" >/dev/null
echo "build explicit bundle: OK"

"${CLI[@]}" plan-evidence-requests \
  --bundle "$OUT/explicit_evidence_bundle.json" \
  --profile "$OUT/approved_profile.yaml" \
  --out "$PLAN_DIR" >/dev/null
echo "plan-evidence-requests: OK"

"${CLI[@]}" run-multi-ai \
  --bundle "$OUT/explicit_evidence_bundle.json" \
  --profile "$OUT/approved_profile.yaml" \
  --providers gemini,gpt-oss-on-vertex,mistral,claude \
  --mode real_or_skip \
  --out "$MULTI_AI"

echo "outputs: $OUT"
