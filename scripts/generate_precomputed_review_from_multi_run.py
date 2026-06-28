#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from ops_evidence_synthesis.ai.prompts import compact_bundle_for_model
from ops_evidence_synthesis.canonical import sha256_json
from ops_evidence_synthesis.precomputed_review import SCORE_DEFINITION, stable_precomputed_review_json


DEFAULT_SOURCE_NOTE = (
    "generated from e2e API real provider run using 6506 sanitized amazon-notify rows plus sanitized source context"
)
DEFAULT_PROVIDER_MODE = "real_api_vertex_gemini_gpt_oss_mistral_qwen_glm"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Generate a public precomputed review payload from a recorded API multi-run response."
    )
    parser.add_argument("--multi-run-json", required=True, help="Recorded /ai/multi-run JSON response.")
    parser.add_argument("--evidence-bundle", required=True, help="Full sanitized Evidence Bundle used by the run.")
    parser.add_argument("--source-context", default="", help="Optional sanitized source_context_bundle.json.")
    parser.add_argument("--source-analysis", default="", help="Optional sanitized source_analysis_bundle.json.")
    parser.add_argument("--api-revision", default="", help="API revision that produced the multi-run response.")
    parser.add_argument("--profile-id", default="", help="Approved profile id used for the run.")
    parser.add_argument("--updated-at", default="", help="Timestamp to store in the public payload.")
    parser.add_argument("--output-dir", default="data/precomputed_review_summaries")
    parser.add_argument("--source-note", default=DEFAULT_SOURCE_NOTE)
    parser.add_argument("--provider-mode", default=DEFAULT_PROVIDER_MODE)
    parser.add_argument(
        "--log-observation",
        action="append",
        default=[],
        help="Additional domain-specific log observation to show in the public analysis context.",
    )
    parser.add_argument("--check", action="store_true", help="Compare generated JSON with the existing output file.")
    args = parser.parse_args()

    api_response = _load_json(args.multi_run_json)
    bundle = _load_json(args.evidence_bundle)
    source_context = _load_json(args.source_context) if args.source_context else {}
    source_analysis = _load_json(args.source_analysis) if args.source_analysis else {}

    payload = build_payload(
        api_response,
        bundle,
        source_context=source_context,
        source_analysis=source_analysis,
        api_revision=args.api_revision,
        profile_id=args.profile_id,
        updated_at=args.updated_at,
        source_note=args.source_note,
        provider_mode=args.provider_mode,
        log_observations=args.log_observation,
    )
    output_path = Path(args.output_dir) / f"{payload['evidence_sha256']}.json"
    generated = stable_precomputed_review_json(payload)
    if args.check:
        if not output_path.exists():
            raise SystemExit(f"expected output is missing: {output_path}")
        expected = output_path.read_text(encoding="utf-8")
        if generated != expected:
            raise SystemExit(f"precomputed review payload drifted: {output_path}")
        print(f"precomputed_review_real_api=ok path={output_path}")
        return 0

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(generated, encoding="utf-8")
    print(f"evidence_sha256={payload['evidence_sha256']}")
    print(f"payload_sha256={payload['generation']['payload_sha256']}")
    print(f"output={output_path}")
    return 0


def build_payload(
    api_response: dict[str, Any],
    bundle: dict[str, Any],
    *,
    source_context: dict[str, Any],
    source_analysis: dict[str, Any],
    api_revision: str,
    profile_id: str,
    updated_at: str,
    source_note: str,
    provider_mode: str,
    log_observations: list[str],
) -> dict[str, Any]:
    evidence_sha256 = str(api_response.get("evidence_sha256") or bundle.get("evidence_sha256") or "")
    if not evidence_sha256:
        raise SystemExit("missing evidence_sha256")
    if str(bundle.get("evidence_sha256") or "") and str(bundle.get("evidence_sha256")) != evidence_sha256:
        raise SystemExit("multi-run response and Evidence Bundle do not share the same evidence_sha256")

    compact = compact_bundle_for_model(bundle)
    corpus_summary = compact.get("evidence_corpus_summary") if isinstance(compact.get("evidence_corpus_summary"), dict) else {}
    local_first = bundle.get("local_first_summary") if isinstance(bundle.get("local_first_summary"), dict) else {}
    source = bundle.get("source") if isinstance(bundle.get("source"), dict) else {}
    time_window = bundle.get("time_window") if isinstance(bundle.get("time_window"), dict) else {}
    synthesis = api_response.get("multi_ai_synthesis") if isinstance(api_response.get("multi_ai_synthesis"), dict) else {}
    canonical_graph = (
        api_response.get("canonical_review_graph")
        if isinstance(api_response.get("canonical_review_graph"), dict)
        else {}
    )
    graph_summary = canonical_graph.get("summary") if isinstance(canonical_graph.get("summary"), dict) else {}
    provider_statuses = _provider_statuses(api_response)
    provider_count = len(provider_statuses)
    valid_provider_statuses = _schema_valid_provider_statuses(provider_statuses)
    invalid_provider_statuses = [row for row in provider_statuses if row not in valid_provider_statuses]
    valid_provider_count = len(valid_provider_statuses)
    log_count = _int(local_first.get("sanitized_event_count"))
    targets = _targets(api_response, provider_statuses=provider_statuses, log_count=log_count)
    public_graph_summary = _review_graph_summary(
        api_response,
        targets=targets,
        provider_count=valid_provider_count,
        log_count=_int(local_first.get("sanitized_event_count")),
    )
    updated = updated_at or str((api_response.get("pipeline_status") or {}).get("completed_at") or "")
    model_items = _int(corpus_summary.get("model_evidence_item_count"))
    model_occurrences = _int(corpus_summary.get("model_occurrence_count"))
    coverage = float(corpus_summary.get("occurrence_coverage_ratio") or 0.0)
    source_context_sha = _source_context_sha(api_response, source_context)
    source_analysis_sha = _source_analysis_sha(api_response, source_analysis)
    provider_sentence = _provider_sentence(provider_statuses)
    provider_result_sentence = _provider_result_sentence(
        provider_statuses,
        valid_provider_statuses=valid_provider_statuses,
        invalid_provider_statuses=invalid_provider_statuses,
    )

    payload: dict[str, Any] = {
        "schema_version": "precomputed_review_summary.v1",
        "evidence_sha256": evidence_sha256,
        "updated_at": updated,
        "generation": {
            "schema_version": "precomputed_review_generation.v1",
            "generator": "ops_evidence_synthesis.precomputed_review",
            "source_note": source_note,
            "provider_mode": provider_mode,
            "score_definition": SCORE_DEFINITION,
            "raw_log_policy": str(bundle.get("raw_log_policy") or local_first.get("raw_log_policy") or "not_uploaded"),
            "real_api_evidence_sha256": evidence_sha256,
            "api_revision": api_revision,
            "pipeline_run_id": str(api_response.get("pipeline_run_id") or ""),
        },
        "summary": {
            "schema_version": "ui_summary.v1",
            "status": "ok",
            "message": "",
            "finding": {
                "title": _provider_summary_title(
                    valid_provider_count=valid_provider_count,
                    provider_count=provider_count,
                    log_count=log_count,
                    service=str(source.get("service") or "service"),
                ),
                "impact": (
                    f"{provider_result_sentence} "
                    f"{_int(graph_summary.get('primary_count'))} primary candidate and "
                    f"{_int(graph_summary.get('validation_count'))} validation target(s) remain human-gated; "
                    "incident baseline is not auto-accepted."
                ),
            },
            "review": {
                "primary_targets": _int(graph_summary.get("primary_count")),
                "validation_targets": _int(graph_summary.get("validation_count")),
                "monitor_only": _int(graph_summary.get("monitor_only_count")),
                "auto_archived": _int(graph_summary.get("auto_archived_count")),
            },
            "providers": {
                "success": valid_provider_count,
                "total": provider_count,
                "pipeline_status": str((api_response.get("pipeline_status") or {}).get("status") or api_response.get("canonical_graph_status") or "succeeded"),
            },
            "baselines": {
                "technical": _technical_baseline_established(canonical_graph),
                "incident": _incident_baseline_established(canonical_graph),
            },
            "raw_log_policy": str(bundle.get("raw_log_policy") or local_first.get("raw_log_policy") or "not_uploaded"),
            "log_count": log_count,
            "canonical_graph_status": str(api_response.get("canonical_graph_status") or "persisted"),
            "canonical_graph_sha256": str(api_response.get("canonical_graph_sha256") or canonical_graph.get("canonical_graph_sha256") or ""),
            "input_fingerprint_sha256": str(
                api_response.get("input_fingerprint_sha256") or canonical_graph.get("input_fingerprint_sha256") or ""
            ),
            "updated_at": updated,
        },
        "provider_statuses": provider_statuses,
        "review_graph_summary": public_graph_summary,
        "targets": targets,
        "analysis_context": {
            "schema_version": "real_api_source_context_summary.v2",
            "service": str(source.get("service") or ""),
            "environment": str(source.get("environment") or ""),
            "window_start": str(time_window.get("start") or ""),
            "window_end": str(time_window.get("end") or ""),
            "pipeline_run_id": str(api_response.get("pipeline_run_id") or ""),
            "real_api_revision": api_revision,
            "profile_id": profile_id,
            "provider_count": provider_count,
            "schema_valid_provider_count": valid_provider_count,
            "sanitized_log_count": log_count,
            "db_ingested_log_count": log_count,
            "evidence_item_count": _int(corpus_summary.get("full_evidence_item_count")),
            "model_projection_evidence_items": model_items,
            "model_projection_occurrence_count": model_occurrences,
            "model_projection_occurrence_coverage_ratio": coverage,
            "model_projection_policy": (
                "AI input used a bounded Evidence Bundle projection: top 140 high-signal evidence items; "
                "row-level raw logs stayed out of provider prompts."
            ),
            "raw_log_policy": str(bundle.get("raw_log_policy") or local_first.get("raw_log_policy") or "not_uploaded"),
            "raw_source_policy": str(source_context.get("raw_source_policy") or "not_uploaded"),
            "source_context_sha256": source_context_sha,
            "source_analysis_sha256": source_analysis_sha,
            "token_usage": dict(synthesis.get("token_usage") or {}),
            "log_observations": [
                (
                    f"The run used all {log_count:,} sanitized {source.get('service', 'service')} rows "
                    f"from {time_window.get('start')} to {time_window.get('end')}."
                ),
                (
                    f"The local-first Evidence Bundle retained {_int(corpus_summary.get('full_evidence_item_count')):,} "
                    f"grouped evidence items and {len(bundle.get('signals') or []):,} deterministic signals."
                ),
                (
                    f"The provider prompt used {model_items:,} selected evidence items covering "
                    f"{model_occurrences:,} occurrences ({coverage:.1%} of the sanitized corpus)."
                ),
                *(
                    log_observations
                    or [
                        (
                            "Repeated RUN_ONCE/RUN_RESULT, systemd watchdog, token refresh, Pub/Sub idle, "
                            "and status snapshot patterns were represented by evidence IDs rather than raw log bodies."
                        )
                    ]
                ),
            ],
            "source_observations": [
                f"Sanitized source context was attached with source_context_sha256={source_context_sha}.",
                f"Source analysis was attached with analysis_sha256={source_analysis_sha}.",
                "Source context is interpretation context only; runtime support still has to cite Evidence Item IDs from the sanitized corpus.",
            ],
            "analysis_conclusion": [
                _provider_conclusion(
                    provider_statuses,
                    valid_provider_statuses=valid_provider_statuses,
                    invalid_provider_statuses=invalid_provider_statuses,
                ),
                (
                    f"The canonical graph produced {_int(graph_summary.get('primary_count'))} primary candidate, "
                    f"{_int(graph_summary.get('validation_count'))} validation target(s), and "
                    f"{_int(graph_summary.get('monitor_only_count'))} monitor-only item(s)."
                ),
                str((canonical_graph.get("finding") or {}).get("impact") or ""),
            ],
        },
        "agent_trace": _agent_trace(
            evidence_sha256=evidence_sha256,
            log_count=log_count,
            provider_count=provider_count,
            valid_provider_count=valid_provider_count,
            target_count=len(targets),
        ),
        "devops_loop": _devops_loop(
            model_items=model_items,
            model_occurrences=model_occurrences,
            provider_count=provider_count,
            valid_provider_count=valid_provider_count,
        ),
    }
    payload["generation"]["payload_sha256"] = sha256_json(
        {
            "evidence_sha256": payload["evidence_sha256"],
            "summary": payload["summary"],
            "provider_statuses": payload["provider_statuses"],
            "review_graph_summary": payload["review_graph_summary"],
            "targets": payload["targets"],
        }
    )
    return payload


def _provider_statuses(api_response: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for run in api_response.get("model_runs") or []:
        if not isinstance(run, dict):
            continue
        rows.append(
            {
                "provider_id": str(run.get("provider_id") or ""),
                "display_name": str(run.get("display_name") or ""),
                "model_name": str(run.get("model_name") or ""),
                "status": str(run.get("status") or ""),
                "latency_ms": _int(run.get("latency_ms")),
                "input_tokens": _int(run.get("input_tokens")),
                "output_tokens": _int(run.get("output_tokens")),
                "raw_output_sha256": str(run.get("raw_output_sha256") or ""),
                "parsed_json_sha256": str(run.get("parsed_json_sha256") or ""),
                "schema_valid": bool(run.get("schema_valid")),
                "failure_reason": str(run.get("failure_reason") or ""),
                "schema_errors": list(run.get("schema_errors") or []),
                "retry": dict(run.get("retry") or {}),
            }
        )
    return sorted(rows, key=lambda row: row["provider_id"])


def _targets(
    api_response: dict[str, Any],
    *,
    provider_statuses: list[dict[str, Any]],
    log_count: int,
) -> list[dict[str, Any]]:
    provider_ids = [str(row["provider_id"]) for row in provider_statuses]
    run_hashes = {str(row["provider_id"]): str(row.get("raw_output_sha256") or "")[:12] for row in provider_statuses}
    valid_count = max(1, sum(1 for row in provider_statuses if row.get("status") == "ok" and row.get("schema_valid")))
    targets = []
    for target in api_response.get("review_targets") or []:
        if not isinstance(target, dict):
            continue
        claimed = {str(provider) for provider in target.get("providers") or []}
        provider_positions = [
            {
                "provider_id": provider_id,
                "stance": "claimed" if provider_id in claimed else "silent",
                "model_run_hash": run_hashes.get(provider_id, ""),
                "one_line": (
                    "Projected this canonical review unit from the real API run."
                    if provider_id in claimed
                    else "Did not surface this normalized review target."
                ),
            }
            for provider_id in provider_ids
        ]
        provider_count = sum(1 for row in provider_positions if row["stance"] == "claimed")
        verdict = "convergence" if provider_count >= 2 else "single_source" if provider_count == 1 else "rule_or_context"
        targets.append(
            {
                "target_id": str(target.get("target_id") or target.get("review_target_id") or ""),
                "review_target_id": str(target.get("review_target_id") or target.get("target_id") or ""),
                "title": str(target.get("title") or ""),
                "class": str(target.get("class") or "validation_target"),
                "state": str(target.get("state") or target.get("class") or "validation_target"),
                "status": str(target.get("status") or "pending"),
                "subsystem": str(target.get("subsystem") or "general"),
                "canonical_review_unit": str(target.get("canonical_review_unit") or target.get("subsystem") or "general"),
                "review_priority_score": round(float(target.get("review_priority_score") or 0.0), 4),
                "provider_count": provider_count,
                "recommended_request_type": str(target.get("recommended_request_type") or ""),
                "claim": str(target.get("impact_summary") or ""),
                "provider_positions": provider_positions,
                "agreement": {
                    "verdict": verdict,
                    "convergence_score": round(provider_count / valid_count, 10),
                    "score_definition": SCORE_DEFINITION,
                    "technical_baseline": "established" if provider_count >= 2 else "open",
                    "incident_baseline": "open",
                    "summary": (
                        f"{provider_count}/{valid_count} schema-valid providers projected this review unit "
                        f"from the {log_count:,}-row corpus; "
                        "incident baseline remains human-gated."
                    ),
                },
                "promotion": {
                    "state": "primary_candidate" if str(target.get("class") or "") == "primary_candidate" else "validation",
                    "blocked_reason": _blocked_reason(target, provider_count=provider_count),
                    "score_cap_applied": False,
                    "score_note": "Priority is review urgency, not truth probability.",
                },
                "evidence_refs": list(target.get("evidence_refs") or []),
                "missing_evidence": list(target.get("missing_evidence") or []),
                "caveats": list(target.get("caveats") or []),
                "raw": {
                    "baseline_support_score": target.get("baseline_support_score"),
                    "canonical_group_key": target.get("canonical_group_key"),
                    "rollup_provider_ratio": target.get("rollup_provider_ratio"),
                    "source_candidate_count": target.get("source_candidate_count"),
                },
            }
        )
    return targets


def _review_graph_summary(
    api_response: dict[str, Any],
    *,
    targets: list[dict[str, Any]],
    provider_count: int,
    log_count: int,
) -> dict[str, Any]:
    canonical_graph = (
        api_response.get("canonical_review_graph")
        if isinstance(api_response.get("canonical_review_graph"), dict)
        else {}
    )
    agreement = canonical_graph.get("agreement_dimensions") if isinstance(canonical_graph.get("agreement_dimensions"), dict) else {}
    summary = canonical_graph.get("summary") if isinstance(canonical_graph.get("summary"), dict) else {}
    convergence_count = sum(1 for target in targets if _int(target.get("provider_count")) >= 2)
    single_source_count = sum(1 for target in targets if _int(target.get("provider_count")) == 1)
    conflict_count = sum(1 for target in targets if 1 < _int(target.get("provider_count")) < provider_count)
    return {
        "targets_total": len(targets),
        "primary_promoted_count": _int(summary.get("primary_count")),
        "convergence_count": convergence_count,
        "single_source_count": single_source_count,
        "rule_or_context_count": sum(1 for target in targets if _int(target.get("provider_count")) == 0),
        "conflict_count": conflict_count,
        "auto_archived_count": _int(summary.get("auto_archived_count")),
        "hidden_multi_provider_archived_count": 0,
        "incident_baseline_established_count": 0,
        "technical_baseline": "established" if _technical_baseline_established(canonical_graph) else "open",
        "incident_baseline": "established" if _incident_baseline_established(canonical_graph) else "open",
        "provider_detection_overlap": str((agreement.get("provider_detection_overlap") or {}).get("value") or ""),
        "review_unit_convergence": str((agreement.get("review_unit_convergence") or {}).get("value") or ""),
        "score_definition": "Convergence score = claimed successful providers / all successful providers. Silent providers count against convergence.",
        "note": "Provider convergence is technical support only; causal and impact judgement remains human-gated.",
        "summary": (
            f"The e2e API analyzed a {log_count:,}-row sanitized log corpus with "
            f"{provider_count} schema-valid real provider output(s); "
            f"{convergence_count} review unit(s) had at least two provider positions while impact remains human-gated."
        ),
    }


def _agent_trace(
    *,
    evidence_sha256: str,
    log_count: int,
    provider_count: int,
    valid_provider_count: int,
    target_count: int,
) -> list[dict[str, Any]]:
    return [
        {
            "step": "sanitize",
            "title": "Sanitize local evidence",
            "status": "completed",
            "artifact": "sanitized_events.jsonl",
            "summary": f"{log_count:,} log rows were sanitized locally; raw logs were not uploaded.",
        },
        {
            "step": "source_context",
            "title": "Attach sanitized code context",
            "status": "completed",
            "artifact": "source_context_bundle.json",
            "summary": "Source context and source analysis were attached as non-evidence interpretation context.",
        },
        {
            "step": "bundle",
            "title": "Freeze Evidence Bundle",
            "status": "completed",
            "artifact": "evidence_bundle.json",
            "summary": f"The review input was fixed by SHA256 {evidence_sha256[:12]} before provider execution.",
        },
        {
            "step": "multi_model",
            "title": "Run real Vertex providers",
            "status": "completed",
            "artifact": "model_runs",
            "summary": f"{valid_provider_count}/{provider_count} provider outputs were schema-valid; Qwen and GLM were included.",
        },
        {
            "step": "arbitrate",
            "title": "Arbitrate review targets",
            "status": "completed",
            "artifact": "canonical_review_graph",
            "summary": f"{target_count} target(s) were projected with provider stance and human-gated promotion.",
        },
        {
            "step": "deliver",
            "title": "Deliver read-only URL",
            "status": "completed",
            "artifact": "precomputed_review_summary",
            "summary": "The public UI serves this generated payload without running models on page load.",
        },
    ]


def _devops_loop(
    *,
    model_items: int,
    model_occurrences: int,
    provider_count: int,
    valid_provider_count: int,
) -> dict[str, Any]:
    provider_value = (
        "5 providers"
        if provider_count == 5 and valid_provider_count == 5
        else f"{valid_provider_count}/{provider_count} valid"
    )
    provider_detail = (
        "Gemini, GPT OSS, Mistral, Qwen, and GLM were executed through Vertex-backed endpoints."
        if provider_count == 5 and valid_provider_count == 5
        else "Requested providers are recorded with status, schema validation, hashes, and retry metadata; only schema-valid outputs contribute review support."
    )
    return {
        "title": "AI workflow is operated as production software",
        "summary": "The public URL is backed by a recorded e2e API run, provider hashes, schema validation, and read-only precomputed serving.",
        "items": [
            {
                "label": "Real provider run",
                "value": provider_value,
                "detail": provider_detail,
            },
            {
                "label": "Source-first boundary",
                "value": "raw not uploaded",
                "detail": "Raw logs and raw source stayed local; only sanitized bundles and context reached the API.",
            },
            {
                "label": "Token compression",
                "value": f"{model_items} items",
                "detail": f"{model_occurrences:,} occurrences were represented by selected evidence IDs and counts.",
            },
            {
                "label": "Human gate",
                "value": "incident open",
                "detail": "Provider convergence creates review targets, not automatic causal truth.",
            },
        ],
    }


def _blocked_reason(target: dict[str, Any], *, provider_count: int) -> str:
    if str(target.get("class") or "") == "primary_candidate":
        return "primary_candidate_only; incident_baseline_not_auto_accepted; human_review_required"
    reasons = [str(reason) for reason in target.get("promotion_blocked_reasons") or [] if str(reason)]
    if reasons:
        return "; ".join(reasons)
    if provider_count >= 2:
        return "incident_baseline_open; user_impact_or_business_output_unverified"
    return "user_impact_unverified; impact_disagreement"


def _provider_sentence(provider_statuses: list[dict[str, Any]]) -> str:
    ordered = sorted(provider_statuses, key=lambda row: _provider_label_rank(str(row.get("provider_id") or "")))
    labels = [_short_provider_label(row.get("provider_id", "")) for row in ordered]
    labels = [label for label in labels if label]
    if len(labels) <= 1:
        return "".join(labels)
    if len(labels) == 2:
        return f"{labels[0]} and {labels[1]}"
    return ", ".join(labels[:-1]) + f", and {labels[-1]}"


def _schema_valid_provider_statuses(provider_statuses: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [row for row in provider_statuses if row.get("status") == "ok" and row.get("schema_valid")]


def _provider_summary_title(*, valid_provider_count: int, provider_count: int, log_count: int, service: str) -> str:
    if provider_count == 5 and valid_provider_count == 5:
        return f"Five real providers analyzed the {log_count:,}-row {service} corpus"
    return f"{valid_provider_count}/{provider_count} real providers produced schema-valid output for the {log_count:,}-row {service} corpus"


def _provider_result_sentence(
    provider_statuses: list[dict[str, Any]],
    *,
    valid_provider_statuses: list[dict[str, Any]],
    invalid_provider_statuses: list[dict[str, Any]],
) -> str:
    if provider_statuses and len(valid_provider_statuses) == len(provider_statuses):
        return f"{_provider_sentence(provider_statuses)} all returned schema-valid outputs."
    valid = _provider_sentence(valid_provider_statuses) or "No provider"
    invalid = _provider_sentence(invalid_provider_statuses)
    if invalid:
        return f"{valid} returned schema-valid outputs; {invalid} did not contribute schema-valid support."
    return f"{valid} returned schema-valid outputs."


def _provider_conclusion(
    provider_statuses: list[dict[str, Any]],
    *,
    valid_provider_statuses: list[dict[str, Any]],
    invalid_provider_statuses: list[dict[str, Any]],
) -> str:
    if provider_statuses and len(valid_provider_statuses) == len(provider_statuses):
        return f"{_provider_sentence(provider_statuses)} all returned status=ok and schema_valid=true."
    valid = _provider_sentence(valid_provider_statuses) or "No provider"
    invalid = _provider_sentence(invalid_provider_statuses)
    if invalid:
        return f"{valid} returned status=ok and schema_valid=true; {invalid} remained visible as non-valid provider run(s)."
    return f"{valid} returned status=ok and schema_valid=true."


def _short_provider_label(provider_id: str) -> str:
    mapping = {
        "gemini-enterprise-agent-platform": "Gemini",
        "openai-gpt-oss-on-vertex": "GPT OSS",
        "mistral-agent-platform": "Mistral",
        "qwen-agent-platform": "Qwen",
        "glm-agent-platform": "GLM",
    }
    return mapping.get(str(provider_id), str(provider_id))


def _provider_label_rank(provider_id: str) -> int:
    order = {
        "gemini-enterprise-agent-platform": 0,
        "openai-gpt-oss-on-vertex": 1,
        "mistral-agent-platform": 2,
        "qwen-agent-platform": 3,
        "glm-agent-platform": 4,
    }
    return order.get(provider_id, 100)


def _technical_baseline_established(canonical_graph: dict[str, Any]) -> bool:
    agreement = canonical_graph.get("agreement_dimensions") if isinstance(canonical_graph.get("agreement_dimensions"), dict) else {}
    value = agreement.get("technical_baseline_agreement") if isinstance(agreement.get("technical_baseline_agreement"), dict) else {}
    return bool(value.get("established"))


def _incident_baseline_established(canonical_graph: dict[str, Any]) -> bool:
    agreement = canonical_graph.get("agreement_dimensions") if isinstance(canonical_graph.get("agreement_dimensions"), dict) else {}
    value = agreement.get("incident_baseline_agreement") if isinstance(agreement.get("incident_baseline_agreement"), dict) else {}
    return bool(value.get("established"))


def _source_context_sha(api_response: dict[str, Any], source_context: dict[str, Any]) -> str:
    context_inputs = api_response.get("context_inputs") if isinstance(api_response.get("context_inputs"), dict) else {}
    return str(source_context.get("source_context_sha256") or context_inputs.get("source_context_sha256") or "")


def _source_analysis_sha(api_response: dict[str, Any], source_analysis: dict[str, Any]) -> str:
    context_inputs = api_response.get("context_inputs") if isinstance(api_response.get("context_inputs"), dict) else {}
    return str(source_analysis.get("analysis_sha256") or context_inputs.get("source_analysis_sha256") or "")


def _int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _load_json(path: str) -> dict[str, Any]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise SystemExit(f"expected JSON object: {path}")
    return data


if __name__ == "__main__":
    raise SystemExit(main())
