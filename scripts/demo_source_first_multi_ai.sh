#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="${PYTHON:-$ROOT/.venv/bin/python}"
CLI=("$PYTHON" -m ops_evidence_synthesis.cli)
OUT="${OUT:-/tmp/ops_demo_source_first}"

cd "$ROOT"
rm -rf "$OUT"
mkdir -p "$OUT"

"${CLI[@]}" sanitize-source   --project-root sample_projects/profile_discovery_sample   --service unknown-sample   --environment prod   --out "$OUT/source_context" >/tmp/ops_demo_source_first_sanitize_source.json
echo "sanitize-source: OK"

"${CLI[@]}" verify-sanitized "$OUT/source_context" >/tmp/ops_demo_source_first_verify_source.txt

"${CLI[@]}" analyze-source   --source-context "$OUT/source_context/source_context_bundle.json"   --provider local   --out "$OUT/source_analysis" >/tmp/ops_demo_source_first_analyze_source.json
echo "analyze-source: OK"

"${CLI[@]}" verify-sanitized "$OUT/source_analysis" >/tmp/ops_demo_source_first_verify_source_analysis.txt

"${CLI[@]}" sanitize sample_logs/secret_heavy.jsonl   --out "$OUT/evidence" >/tmp/ops_demo_source_first_sanitize_logs.json

"${CLI[@]}" build-bundle "$OUT/evidence/sanitized_events.jsonl"   --service unknown-sample   --environment prod   --start 2026-06-16T00:00:00Z   --end 2026-06-16T18:00:00Z   --profile generic   --out "$OUT/evidence/evidence_bundle.json" >"$OUT/evidence/evidence_sha256.txt"
echo "build evidence bundle: OK"

"${CLI[@]}" verify-sanitized "$OUT/evidence" >/tmp/ops_demo_source_first_verify_evidence.txt

"${CLI[@]}" discover-profile   --source-context "$OUT/source_context/source_context_bundle.json"   --source-analysis "$OUT/source_analysis/source_analysis_bundle.json"   --evidence-bundle "$OUT/evidence/evidence_bundle.json"   --service unknown-sample   --environment prod   --out "$OUT/profile_discovery" >/tmp/ops_demo_source_first_discover_profile.json
echo "discover-profile: OK"

"${CLI[@]}" draft-profile   --discovery-bundle "$OUT/profile_discovery/profile_discovery_bundle.json"   --provider local   --out "$OUT/profile_discovery/profile_draft.json" >/tmp/ops_demo_source_first_profile_draft.json

"${CLI[@]}" approve-profile   --profile-draft "$OUT/profile_discovery/profile_draft.json"   --profile-id unknown_sample_approved   --approved-by api-user   --out "$OUT/profile_discovery/approved_profile.yaml" >"$OUT/profile_discovery/approved_profile_result.json"
echo "approve-profile: OK"

"${CLI[@]}" build-bundle "$OUT/evidence/sanitized_events.jsonl"   --service unknown-sample   --environment prod   --start 2026-06-16T00:00:00Z   --end 2026-06-16T18:00:00Z   --profile "$OUT/profile_discovery/approved_profile.yaml"   --out "$OUT/evidence/explicit_evidence_bundle.json" >"$OUT/evidence/explicit_evidence_sha256.txt"

"${CLI[@]}" run-multi-ai   --bundle "$OUT/evidence/explicit_evidence_bundle.json"   --profile "$OUT/profile_discovery/approved_profile.yaml"   --source-context "$OUT/source_context/source_context_bundle.json"   --source-analysis "$OUT/source_analysis/source_analysis_bundle.json"   --providers local-gemini,local-gpt-oss,local-mistral   --mode local   --out "$OUT/multi_ai" >"$OUT/run_multi_ai.txt"
echo "run-multi-ai: OK"

"${CLI[@]}" verify-sanitized "$OUT/multi_ai" >/tmp/ops_demo_source_first_verify_multi_ai.txt

"${CLI[@]}" plan-evidence-requests   --bundle "$OUT/evidence/explicit_evidence_bundle.json"   --profile "$OUT/profile_discovery/approved_profile.yaml"   --source-analysis "$OUT/source_analysis/source_analysis_bundle.json"   --canonical-review-graph "$OUT/multi_ai/canonical_review_graph.json"   --out "$OUT/evidence_plan" >"$OUT/evidence_plan_result.json"
echo "plan-evidence-requests: OK"

"$PYTHON" - <<'PY' "$OUT/multi_ai/multi_ai_synthesis.json" "$OUT/multi_ai/canonical_review_graph.json" "$OUT/evidence_plan/evidence_request_plan.json"
import json
import sys
synthesis = json.load(open(sys.argv[1], encoding="utf-8"))
graph = json.load(open(sys.argv[2], encoding="utf-8"))
plan = json.load(open(sys.argv[3], encoding="utf-8"))
finding = graph.get("finding") or {}
graph_summary = graph.get("summary") or {}
print("canonical_review_graph: OK")
print(f"canonical_graph_status: {graph.get('canonical_graph_status') or graph.get('snapshot_status') or 'computed_on_request'}")
print(f"canonical_graph_sha256: {graph.get('canonical_graph_sha256') or ''}")
print(f"input_fingerprint_sha256: {graph.get('input_fingerprint_sha256') or ''}")
print(f"agreement_groups: {len(synthesis.get('agreement_groups') or [])}")
print(f"disagreement_groups: {len(synthesis.get('disagreement_groups') or [])}")
print(f"disagreement_themes: {len(synthesis.get('disagreement_themes') or [])}")
print(f"primary_targets: {graph_summary.get('primary_count', 0)}")
print(f"canonical_validation_targets: {graph_summary.get('validation_count', 0)}")
print(f"promotion_decisions: {len(graph.get('promotion_decisions') or [])}")
print(f"planner_quality_warnings: {len(plan.get('planner_quality_warnings') or [])}")
print(f"finding: {finding.get('title') or ''}")
print(f"impact: {finding.get('impact') or ''}")
PY

echo "outputs: $OUT"
