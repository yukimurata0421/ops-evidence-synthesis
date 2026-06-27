#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="${PYTHON:-$ROOT/.venv/bin/python}"
CLI=("$PYTHON" -m ops_evidence_synthesis.cli)
OUT="${OUT:-/tmp/ops_demo_multi_ai}"
SANITIZED="$OUT/sanitized"
DISCOVERY="$OUT/profile_discovery"
PLAN_DIR="$OUT/evidence_plan"
MULTI_AI="$OUT/multi_ai"
CHILD="$OUT/child"

cd "$ROOT"
rm -rf "$OUT"
mkdir -p "$OUT"

"${CLI[@]}" sanitize sample_logs/redaction_fixture.jsonl --out "$SANITIZED" >/tmp/ops_demo_multi_ai_sanitize.json
echo "sanitize: OK"

"${CLI[@]}" verify-sanitized "$SANITIZED" >/tmp/ops_demo_multi_ai_verify.txt
echo "verify: OK"

"${CLI[@]}" build-bundle "$SANITIZED/sanitized_events.jsonl" \
  --service demo-payment \
  --environment prod \
  --start 2026-06-16T00:00:00Z \
  --end 2026-06-16T18:00:00Z \
  --profile generic \
  --out "$OUT/generic_evidence_bundle.json" >"$OUT/generic_evidence_sha256.txt"
echo "build generic bundle: OK"

"${CLI[@]}" discover-profile \
  --project-root sample_projects/profile_discovery_sample \
  --evidence-bundle "$OUT/generic_evidence_bundle.json" \
  --service demo-payment \
  --environment prod \
  --out "$DISCOVERY" >"$OUT/discover_profile.json"
echo "discover-profile: OK"

"${CLI[@]}" draft-profile \
  --discovery-bundle "$DISCOVERY/profile_discovery_bundle.json" \
  --provider local \
  --out "$OUT/profile_draft.json" >"$OUT/profile_draft_sha256.txt"
echo "draft-profile: OK"

"${CLI[@]}" approve-profile \
  --profile-draft "$OUT/profile_draft.json" \
  --profile-id demo-payment-approved \
  --approved-by demo-local-reviewer \
  --out "$OUT/approved_profile.yaml" >"$OUT/approved_profile_result.json"
echo "approve-profile: OK"

"${CLI[@]}" build-bundle "$SANITIZED/sanitized_events.jsonl" \
  --service demo-payment \
  --environment prod \
  --start 2026-06-16T00:00:00Z \
  --end 2026-06-16T18:00:00Z \
  --profile "$OUT/approved_profile.yaml" \
  --out "$OUT/explicit_evidence_bundle.json" >"$OUT/explicit_evidence_sha256.txt"
echo "build explicit bundle: OK"

"${CLI[@]}" run-multi-ai \
  --bundle "$OUT/explicit_evidence_bundle.json" \
  --profile "$OUT/approved_profile.yaml" \
  --providers local-gemini,local-gpt-oss,local-mistral \
  --mode local \
  --out "$MULTI_AI" >"$OUT/run_multi_ai.txt"
echo "run-multi-ai: OK"

"${CLI[@]}" plan-evidence-requests \
  --bundle "$OUT/explicit_evidence_bundle.json" \
  --profile "$OUT/approved_profile.yaml" \
  --canonical-review-graph "$MULTI_AI/canonical_review_graph.json" \
  --out "$PLAN_DIR" >"$OUT/evidence_plan_result.json"
echo "plan-evidence-requests: OK"

PLAN_ID="$("$PYTHON" -c 'import json,sys; print(json.load(open(sys.argv[1]))["plan_id"])' "$PLAN_DIR/evidence_request_plan.json")"
PARENT_SHA="$(cat "$OUT/explicit_evidence_sha256.txt")"
mkdir -p "$CHILD"
"${CLI[@]}" build-bundle "$SANITIZED/sanitized_events.jsonl" \
  --service demo-payment \
  --environment prod \
  --start 2026-06-16T18:00:00Z \
  --end 2026-06-16T19:00:00Z \
  --profile "$OUT/approved_profile.yaml" \
  --parent-evidence-sha256 "$PARENT_SHA" \
  --evidence-request-plan-id "$PLAN_ID" \
  --collection-mode manual_read_only_collection \
  --out "$CHILD/child_evidence_bundle.json" >"$OUT/child_evidence_sha256.txt"
echo "child evidence bundle lineage: OK"

echo "providers:"
"$PYTHON" - <<'PY' "$MULTI_AI/model_runs.jsonl"
import json
import sys
for line in open(sys.argv[1], encoding="utf-8"):
    row = json.loads(line)
    print(f"  {row['provider_id']}: {row['status']} schema_valid={str(bool(row['schema_valid'])).lower()}")
PY

"$PYTHON" - <<'PY' "$MULTI_AI/multi_ai_synthesis.json" "$MULTI_AI/canonical_review_graph.json" "$OUT/generic_evidence_bundle.json" "$DISCOVERY/profile_discovery_bundle.json" "$PLAN_DIR/evidence_request_plan.json"
import json
import sys
synthesis = json.load(open(sys.argv[1], encoding="utf-8"))
graph = json.load(open(sys.argv[2], encoding="utf-8"))
generic = json.load(open(sys.argv[3], encoding="utf-8"))
discovery = json.load(open(sys.argv[4], encoding="utf-8"))
plan = json.load(open(sys.argv[5], encoding="utf-8"))
print("multi_ai_synthesis: OK")
print("canonical_review_graph: OK")
print(f"canonical_graph_status: {graph.get('canonical_graph_status') or graph.get('snapshot_status') or 'computed_on_request'}")
print(f"canonical_graph_sha256: {graph.get('canonical_graph_sha256') or ''}")
print(f"input_fingerprint_sha256: {graph.get('input_fingerprint_sha256') or ''}")
finding = graph.get("finding") or {}
graph_summary = graph.get("summary") or {}
print(f"agreement_groups: {len(synthesis.get('agreement_groups') or [])}")
print(f"disagreement_groups: {len(synthesis.get('disagreement_groups') or [])}")
print(f"disagreement_themes: {len(synthesis.get('disagreement_themes') or [])}")
print(f"validation_targets: {len(synthesis.get('validation_targets') or [])}")
print(f"primary_targets: {graph_summary.get('primary_count', 0)}")
print(f"canonical_validation_targets: {graph_summary.get('validation_count', 0)}")
print(f"promotion_decisions: {len(graph.get('promotion_decisions') or [])}")
print(f"planner_quality_warnings: {len(plan.get('planner_quality_warnings') or [])}")
print(f"finding: {finding.get('title') or ''}")
print(f"impact: {finding.get('impact') or ''}")
print(f"evidence_sha256: {generic.get('evidence_sha256')}")
print(f"discovery_sha256: {discovery.get('discovery_sha256')}")
print(f"plan_id: {plan.get('plan_id')}")
PY

echo "outputs: $OUT"
