#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

import yaml

from ops_evidence_synthesis.agents.adk_investigator import build_adk_tool_contract_trace
from ops_evidence_synthesis.ai.prompts import compact_bundle_for_model
from ops_evidence_synthesis.canonical import sha256_json
from ops_evidence_synthesis.precomputed_review import SCORE_DEFINITION, stable_precomputed_review_json
from ops_evidence_synthesis.timeutils import parse_timestamp
from ops_evidence_synthesis.window_policy import (
    DEFAULT_MIN_ANALYSIS_WINDOW_HOURS,
    validate_minimum_analysis_window,
)


DEFAULT_SOURCE_NOTE = (
    "generated from a recorded e2e API real provider run using a sanitized log corpus and optional sanitized source context"
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
    parser.add_argument("--profile-draft", default="", help="Optional profile_draft.json generated from sanitized discovery.")
    parser.add_argument("--approved-profile", default="", help="Optional approved explicit profile JSON/YAML.")
    parser.add_argument("--api-revision", default="", help="API revision that produced the multi-run response.")
    parser.add_argument("--profile-id", default="", help="Approved profile id used for the run.")
    parser.add_argument("--updated-at", default="", help="Timestamp to store in the public payload.")
    parser.add_argument("--output-dir", default="data/precomputed_review_summaries")
    parser.add_argument("--source-note", default=DEFAULT_SOURCE_NOTE)
    parser.add_argument("--provider-mode", default=DEFAULT_PROVIDER_MODE)
    parser.add_argument(
        "--model-projection-policy",
        default="",
        help="Optional public-facing model projection policy text.",
    )
    parser.add_argument(
        "--min-window-hours",
        type=int,
        default=DEFAULT_MIN_ANALYSIS_WINDOW_HOURS,
        help="Minimum analysis window required for public real-provider payloads.",
    )
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
    profile_draft = _load_json(args.profile_draft) if args.profile_draft else {}
    approved_profile = _load_profile(args.approved_profile) if args.approved_profile else {}

    payload = build_payload(
        api_response,
        bundle,
        source_context=source_context,
        source_analysis=source_analysis,
        profile_draft=profile_draft,
        approved_profile=approved_profile,
        api_revision=args.api_revision,
        profile_id=args.profile_id,
        updated_at=args.updated_at,
        source_note=args.source_note,
        provider_mode=args.provider_mode,
        model_projection_policy=args.model_projection_policy,
        log_observations=args.log_observation,
        min_window_hours=args.min_window_hours,
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
    profile_draft: dict[str, Any],
    approved_profile: dict[str, Any],
    api_revision: str,
    profile_id: str,
    updated_at: str,
    source_note: str,
    provider_mode: str,
    model_projection_policy: str,
    log_observations: list[str],
    min_window_hours: int = DEFAULT_MIN_ANALYSIS_WINDOW_HOURS,
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
    window = validate_minimum_analysis_window(
        str(time_window.get("start") or ""),
        str(time_window.get("end") or ""),
        min_hours=min_window_hours,
        context=f"public real-provider payload {evidence_sha256}",
    )
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
    pipeline_status = _pipeline_status(
        api_response,
        valid_provider_count=valid_provider_count,
        provider_count=provider_count,
    )
    log_count = _int(local_first.get("sanitized_event_count"))
    evidence_lookup = _evidence_lookup(bundle)
    targets = _targets(
        api_response,
        provider_statuses=provider_statuses,
        log_count=log_count,
        evidence_lookup=evidence_lookup,
        window_start=window.start,
        window_end=window.end,
    )
    public_review_counts = _public_review_counts(targets, graph_summary=graph_summary)
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
    projection_policy = model_projection_policy or (
        "AI input used a bounded Evidence Bundle projection: top 140 high-signal evidence items; "
        "row-level raw logs stayed out of provider prompts."
    )
    source_context_sha = _source_context_sha(api_response, source_context)
    source_analysis_sha = _source_analysis_sha(api_response, source_analysis)
    provider_sentence = _provider_sentence(provider_statuses)
    provider_result_sentence = _provider_result_sentence(
        provider_statuses,
        valid_provider_statuses=valid_provider_statuses,
        invalid_provider_statuses=invalid_provider_statuses,
    )
    profile_context = _profile_context(
        profile_id=profile_id,
        profile_draft=profile_draft,
        approved_profile=approved_profile,
        source_context_sha=source_context_sha,
        source_analysis_sha=source_analysis_sha,
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
            "min_analysis_window_hours": min_window_hours,
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
                    f"{public_review_counts['primary_targets']} primary candidate and "
                    f"{public_review_counts['validation_targets']} validation target(s) remain human-gated; "
                    "incident promotion is not auto-accepted."
                ),
            },
            "review": {
                "primary_targets": public_review_counts["primary_targets"],
                "validation_targets": public_review_counts["validation_targets"],
                "monitor_only": public_review_counts["monitor_only"],
                "auto_archived": public_review_counts["auto_archived"],
            },
            "providers": {
                "success": valid_provider_count,
                "total": provider_count,
                "pipeline_status": pipeline_status,
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
        "profile_context": profile_context,
        "profile_draft_generation": _profile_draft_generation(profile_context),
        "targets": targets,
        "analysis_context": {
            "schema_version": "real_api_source_context_summary.v2",
            "service": str(source.get("service") or ""),
            "environment": str(source.get("environment") or ""),
            "window_start": str(time_window.get("start") or ""),
            "window_end": str(time_window.get("end") or ""),
            "analysis_window_hours": window.duration_hours,
            "min_analysis_window_hours": min_window_hours,
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
            "model_projection_policy": projection_policy,
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
                    f"The public graph exposes {public_review_counts['primary_targets']} primary candidate, "
                    f"{public_review_counts['validation_targets']} validation target(s), and "
                    f"{public_review_counts['monitor_only']} monitor-only item(s) after the analysis-window boundary is applied."
                ),
                _analysis_conclusion_impact(canonical_graph, targets),
            ],
        },
        "agent_trace": [],
        "devops_loop": _devops_loop(
            model_items=model_items,
            model_occurrences=model_occurrences,
            provider_count=provider_count,
            valid_provider_count=valid_provider_count,
        ),
    }
    payload["agent_trace"] = build_adk_tool_contract_trace(payload)
    payload["generation"]["payload_sha256"] = sha256_json(
        {
            "evidence_sha256": payload["evidence_sha256"],
            "summary": payload["summary"],
            "provider_statuses": payload["provider_statuses"],
            "review_graph_summary": payload["review_graph_summary"],
            "profile_context": payload["profile_context"],
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


def _pipeline_status(api_response: dict[str, Any], *, valid_provider_count: int, provider_count: int) -> str:
    pipeline = api_response.get("pipeline_status") if isinstance(api_response.get("pipeline_status"), dict) else {}
    explicit = str(pipeline.get("status") or "")
    if explicit:
        return explicit
    if provider_count > 0 and valid_provider_count == provider_count:
        return "succeeded"
    if valid_provider_count > 0:
        return "partial"
    return str(api_response.get("canonical_graph_status") or "unknown")


def _targets(
    api_response: dict[str, Any],
    *,
    provider_statuses: list[dict[str, Any]],
    log_count: int,
    evidence_lookup: dict[str, dict[str, Any]],
    window_start: str,
    window_end: str,
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
        source_evidence_refs = list(target.get("evidence_refs") or [])
        evidence_refs, excluded_evidence_refs = _filter_window_evidence_refs(
            source_evidence_refs,
            evidence_lookup=evidence_lookup,
            window_start=window_start,
            window_end=window_end,
        )
        if source_evidence_refs and not evidence_refs:
            continue
        target_class = str(target.get("class") or "validation_target")
        promotion_state = "primary_candidate" if target_class == "primary_candidate" else "validation"
        blocked_reason = _blocked_reason(target, provider_count=provider_count)
        missing_evidence = _public_missing_evidence(target, blocked_reason=blocked_reason)
        target_explanation = _public_target_explanation(
            target,
            evidence_refs=evidence_refs,
            blocked_reason=blocked_reason,
            evidence_lookup=evidence_lookup,
            window_start=window_start,
            window_end=window_end,
        )
        targets.append(
            {
                "target_id": str(target.get("target_id") or target.get("review_target_id") or ""),
                "review_target_id": str(target.get("review_target_id") or target.get("target_id") or ""),
                "title": str(target.get("title") or ""),
                "class": target_class,
                "state": str(target.get("state") or target_class),
                "status": str(target.get("status") or "pending"),
                "subsystem": str(target.get("subsystem") or "general"),
                "canonical_review_unit": str(target.get("canonical_review_unit") or target.get("subsystem") or "general"),
                "review_priority_score": round(float(target.get("review_priority_score") or 0.0), 4),
                "provider_count": provider_count,
                "recommended_request_type": str(target.get("recommended_request_type") or ""),
                "claim": _target_claim(
                    target,
                    provider_count=provider_count,
                    valid_count=valid_count,
                    evidence_ref_count=len(evidence_refs),
                ),
                "review_reason": _review_reason_summary(
                    target,
                    provider_count=provider_count,
                    valid_count=valid_count,
                    evidence_ref_count=len(evidence_refs),
                    blocked_reason=blocked_reason,
                    log_count=log_count,
                ),
                "target_explanation": target_explanation,
                "suspected_issue": str(target_explanation.get("suspected_issue") or ""),
                "operational_mechanism": str(target_explanation.get("operational_mechanism") or ""),
                "why_it_matters": str(target_explanation.get("why_it_matters") or ""),
                "evidence_summary": list(target_explanation.get("evidence_summary") or []),
                "counter_evidence_summary": list(target_explanation.get("counter_evidence_summary") or []),
                "why_not_promoted": str(target_explanation.get("why_not_promoted") or ""),
                "next_validation_question": str(target_explanation.get("next_validation_question") or ""),
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
                        "incident promotion remains human-gated."
                    ),
                },
                "promotion": {
                    "state": promotion_state,
                    "blocked_reason": blocked_reason,
                    "explanation": _promotion_explanation(
                        state=promotion_state,
                        provider_count=provider_count,
                        valid_count=valid_count,
                    ),
                    "score_cap_applied": False,
                    "score_note": "Priority is review urgency, not truth probability.",
                },
                "evidence_refs": evidence_refs,
                "excluded_evidence_refs": excluded_evidence_refs,
                "missing_evidence": missing_evidence,
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


def _review_reason_summary(
    target: dict[str, Any],
    *,
    provider_count: int,
    valid_count: int,
    evidence_ref_count: int,
    blocked_reason: str,
    log_count: int,
) -> dict[str, Any]:
    canonical_unit = str(target.get("canonical_review_unit") or target.get("subsystem") or "general")
    source_candidates = _int(target.get("source_candidate_count")) or 1
    rollup_ratio = target.get("rollup_provider_ratio")
    try:
        rollup_ratio_text = f"{float(rollup_ratio):.3f}"
    except (TypeError, ValueError):
        rollup_ratio_text = "unknown"
    factors = [
        (
            f"{provider_count}/{valid_count} schema-valid providers independently projected "
            f"the normalized review unit `{canonical_unit}`."
        ),
        (
            f"{evidence_ref_count} cited Evidence Item(s) tie the unit back to the "
            f"{log_count:,}-row sanitized corpus."
        ),
        (
            f"{source_candidates} source candidate(s) were rolled up into this canonical unit "
            f"(rollup provider ratio {rollup_ratio_text})."
        ),
        (
            f"Promotion is still blocked by `{blocked_reason}`; this is review work, "
            "not an accepted incident cause."
        ),
    ]
    return {
        "headline": (
            f"Review target created because provider convergence and cited evidence made `{canonical_unit}` "
            "worth human validation."
        ),
        "factors": factors,
        "operator_question": _operator_question(target, blocked_reason=blocked_reason),
    }


def _public_review_counts(targets: list[dict[str, Any]], *, graph_summary: dict[str, Any]) -> dict[str, int]:
    primary = sum(1 for target in targets if str(target.get("class") or "") == "primary_candidate")
    validation = sum(1 for target in targets if str(target.get("class") or "") != "primary_candidate")
    return {
        "primary_targets": primary,
        "validation_targets": validation,
        "monitor_only": _int(graph_summary.get("monitor_only_count")),
        "auto_archived": _int(graph_summary.get("auto_archived_count")),
    }


def _public_target_explanation(
    target: dict[str, Any],
    *,
    evidence_refs: list[str],
    blocked_reason: str,
    evidence_lookup: dict[str, dict[str, Any]],
    window_start: str,
    window_end: str,
) -> dict[str, Any]:
    raw = target.get("target_explanation") if isinstance(target.get("target_explanation"), dict) else {}
    canonical_unit = str(target.get("canonical_review_unit") or target.get("subsystem") or "review unit")
    suspected_issue = (
        _first_non_meta_text(
            target.get("suspected_issue"),
            raw.get("suspected_issue"),
            target.get("impact_summary"),
            target.get("title"),
        )
        or _fallback_suspected_issue(target, canonical_unit=canonical_unit)
    )
    operational_mechanism = (
        str(target.get("operational_mechanism") or raw.get("operational_mechanism") or "").strip()
        or _fallback_operational_mechanism(target, canonical_unit=canonical_unit)
    )
    why_it_matters = (
        str(target.get("why_it_matters") or raw.get("why_it_matters") or "").strip()
        or "This review unit may affect an operational outcome, but the current payload does not prove user impact."
    )
    evidence_summary = _hydrate_evidence_summary(
        [
            *_string_items(target.get("evidence_summary")),
            *_string_items(raw.get("evidence_summary")),
        ],
        evidence_refs=evidence_refs,
        evidence_lookup=evidence_lookup,
    )
    if not evidence_summary:
        evidence_summary = [
            _evidence_summary_for_ref(ref, evidence_lookup.get(ref))
            for ref in evidence_refs[:8]
        ]
    evidence_summary = _filter_summary_entries_to_window(
        evidence_summary,
        evidence_lookup=evidence_lookup,
        window_start=window_start,
        window_end=window_end,
    )
    evidence_summary, inferred_counter_summary = _split_counter_like_support_summary(
        evidence_summary,
        canonical_unit=canonical_unit,
        suspected_issue=suspected_issue,
        operational_mechanism=operational_mechanism,
        why_it_matters=why_it_matters,
    )
    counter_summary = _counter_summary_for_public_window(
        [
            *_string_items(target.get("counter_evidence_summary")),
            *_string_items(raw.get("counter_evidence_summary")),
            *inferred_counter_summary,
        ],
        evidence_lookup=evidence_lookup,
        window_start=window_start,
        window_end=window_end,
    )
    if not counter_summary and blocked_reason:
        counter_summary = [f"Promotion blocker: {blocked_reason}."]
    why_not_promoted = (
        str(target.get("why_not_promoted") or raw.get("why_not_promoted") or "").strip()
        or _why_not_promoted(blocked_reason)
    )
    next_validation_question = (
        str(target.get("next_validation_question") or raw.get("next_validation_question") or "").strip()
        or _operator_question(target, blocked_reason=blocked_reason)
    )
    return {
        "schema_version": "target_explanation.v1",
        "suspected_issue": suspected_issue,
        "operational_mechanism": operational_mechanism,
        "why_it_matters": why_it_matters,
        "evidence_summary": evidence_summary,
        "counter_evidence_summary": counter_summary,
        "why_not_promoted": why_not_promoted,
        "next_validation_question": next_validation_question,
        "provider_explanations": list(raw.get("provider_explanations") or []),
    }


def _public_missing_evidence(target: dict[str, Any], *, blocked_reason: str) -> list[str]:
    missing = _unique_strings(target.get("missing_evidence") or [])
    if "user_impact" in blocked_reason:
        missing.append("User impact or operational outcome evidence tied to this review unit.")
    if "cause" in blocked_reason or "baseline" in blocked_reason:
        missing.append("Causal alignment evidence connecting this review unit to the incident window.")
    if "support_without_evidence" in blocked_reason:
        missing.append("Runtime Evidence Item IDs that support the claim.")
    return _unique_strings(missing)


def _evidence_lookup(bundle: dict[str, Any]) -> dict[str, dict[str, Any]]:
    lookup: dict[str, dict[str, Any]] = {}
    evidence_refs = bundle.get("evidence_refs") if isinstance(bundle.get("evidence_refs"), dict) else {}
    for key, value in evidence_refs.items():
        if isinstance(value, dict):
            lookup[str(key)] = value
    for item in bundle.get("evidence_items") or []:
        if not isinstance(item, dict):
            continue
        evidence_id = str(item.get("evidence_id") or item.get("id") or "")
        if evidence_id:
            lookup[evidence_id] = item
    return lookup


def _evidence_summary_for_ref(ref: str, item: dict[str, Any] | None) -> str:
    if not isinstance(item, dict):
        return f"{ref}: cited runtime evidence for this target; inspect the Evidence Item body before treating it as causal support."
    event_type = str(item.get("event_type") or item.get("type") or "evidence").replace("_", " ")
    component = str(item.get("component") or item.get("source") or "").strip()
    count = _int(item.get("count") or item.get("occurrence_count") or 0)
    first_seen = str(item.get("first_seen") or item.get("timestamp") or "").strip()
    last_seen = str(item.get("last_seen") or item.get("timestamp") or "").strip()
    template = str(item.get("message_template") or item.get("summary") or item.get("example_sanitized") or "").strip()
    parts = [f"{ref}: {event_type}"]
    if component:
        parts.append(f"from {component}")
    if count:
        parts.append(f"observed {count} time(s)")
    if first_seen or last_seen:
        parts.append(f"between {first_seen or 'unknown'} and {last_seen or 'unknown'}")
    sentence = " ".join(parts) + "."
    if template:
        sentence += f" Sanitized pattern: {template[:220]}"
    return sentence


def _hydrate_evidence_summary(
    values: list[str],
    *,
    evidence_refs: list[str],
    evidence_lookup: dict[str, dict[str, Any]],
) -> list[str]:
    hydrated: list[str] = []
    covered_refs: set[str] = set()
    for value in values:
        text = str(value or "").strip()
        if not text:
            continue
        refs = [ref for ref in _summary_ref_tokens(text) if ref in evidence_refs]
        if _summary_ref_tokens(text) and not refs:
            continue
        if refs:
            covered_refs.update(refs)
            ref = refs[0]
            if _is_bare_evidence_ref(text):
                hydrated.append(_evidence_summary_for_ref(ref, evidence_lookup.get(ref)))
                continue
        hydrated.append(text)
    for ref in evidence_refs:
        if ref not in covered_refs:
            hydrated.append(_evidence_summary_for_ref(ref, evidence_lookup.get(ref)))
    return _unique_strings(hydrated)


def _counter_summary_for_public_window(
    values: list[str],
    *,
    evidence_lookup: dict[str, dict[str, Any]],
    window_start: str,
    window_end: str,
) -> list[str]:
    output: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if not text:
            continue
        refs = _summary_ref_tokens(text)
        if refs and not _summary_refs_overlap_window(
            refs,
            evidence_lookup=evidence_lookup,
            window_start=window_start,
            window_end=window_end,
        ):
            continue
        if refs and _is_bare_evidence_ref(text):
            output.append(_evidence_summary_for_ref(refs[0], evidence_lookup.get(refs[0])))
            continue
        output.append(text)
    return _unique_strings(output)


def _filter_summary_entries_to_window(
    values: list[str],
    *,
    evidence_lookup: dict[str, dict[str, Any]],
    window_start: str,
    window_end: str,
) -> list[str]:
    output: list[str] = []
    for value in values:
        text = str(value or "").strip()
        refs = _summary_ref_tokens(text)
        if refs and not _summary_refs_overlap_window(
            refs,
            evidence_lookup=evidence_lookup,
            window_start=window_start,
            window_end=window_end,
        ):
            continue
        output.append(text)
    return _unique_strings(output)


def _summary_refs_overlap_window(
    refs: list[str],
    *,
    evidence_lookup: dict[str, dict[str, Any]],
    window_start: str,
    window_end: str,
) -> bool:
    known_refs = [ref for ref in refs if ref in evidence_lookup]
    if not known_refs:
        return True
    return all(
        _evidence_item_overlaps_window(evidence_lookup.get(ref), window_start=window_start, window_end=window_end)
        for ref in known_refs
    )


def _split_counter_like_support_summary(
    values: list[str],
    *,
    canonical_unit: str,
    suspected_issue: str,
    operational_mechanism: str,
    why_it_matters: str,
) -> tuple[list[str], list[str]]:
    if not _is_problem_review_context(
        canonical_unit=canonical_unit,
        suspected_issue=suspected_issue,
        operational_mechanism=operational_mechanism,
        why_it_matters=why_it_matters,
    ):
        return values, []
    support: list[str] = []
    counter: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if _looks_like_health_or_recovery_signal(text) and not _contains_problem_signal(text):
            counter.append(text)
        else:
            support.append(text)
    return _unique_strings(support), _unique_strings(counter)


def _is_problem_review_context(
    *,
    canonical_unit: str,
    suspected_issue: str,
    operational_mechanism: str,
    why_it_matters: str,
) -> bool:
    unit = canonical_unit.lower()
    if unit in {"service_health", "observability_contract", "background_processing"}:
        return False
    context = " ".join([unit, suspected_issue, operational_mechanism, why_it_matters]).lower()
    return any(token in context for token in _PROBLEM_SIGNAL_TOKENS)


_HEALTH_OR_RECOVERY_SIGNAL_TOKENS = (
    "no timeout",
    "connected=true",
    "healthy=true",
    "status=ok",
    "ok=true",
    "successful",
    "successfully",
    "last_reason: healthy",
    "stall_streak: 0",
    "net_fail_streak: 0",
)


_PROBLEM_SIGNAL_TOKENS = (
    "broken pipe",
    "can't open file",
    "critical",
    "degraded",
    "disconnect",
    "error",
    "exception",
    "failed",
    "failure",
    "incident",
    "invalidargument",
    "low upload",
    "missing",
    "no such file",
    "skipped",
    "stall",
    "timeout",
    "traceback",
    "unhealthy",
)


def _looks_like_health_or_recovery_signal(text: str) -> bool:
    lower = text.lower()
    return any(token in lower for token in _HEALTH_OR_RECOVERY_SIGNAL_TOKENS)


def _contains_problem_signal(text: str) -> bool:
    lower = text.lower()
    lower = lower.replace("no timeout", "")
    lower = lower.replace("without timeout", "")
    return any(token in lower for token in _PROBLEM_SIGNAL_TOKENS)


def _summary_ref_tokens(text: str) -> list[str]:
    return re.findall(r"\b(?:PATTERN|LOG|EV|EVIDENCE|METRIC|OPS)-[A-Za-z0-9-]+\b", text)


def _is_bare_evidence_ref(text: str) -> bool:
    return bool(re.fullmatch(r"\s*(?:PATTERN|LOG|EV|EVIDENCE|METRIC|OPS)-[A-Za-z0-9-]+\s*\.?\s*", text))


def _filter_window_evidence_refs(
    evidence_refs: list[str],
    *,
    evidence_lookup: dict[str, dict[str, Any]],
    window_start: str,
    window_end: str,
) -> tuple[list[str], list[dict[str, str]]]:
    kept: list[str] = []
    excluded: list[dict[str, str]] = []
    for ref in _unique_strings(evidence_refs):
        item = evidence_lookup.get(ref)
        if _evidence_item_overlaps_window(item, window_start=window_start, window_end=window_end):
            kept.append(ref)
            continue
        excluded.append(
            {
                "evidence_ref": ref,
                "reason": "outside_analysis_window",
                "first_seen": str((item or {}).get("first_seen") or (item or {}).get("timestamp") or ""),
                "last_seen": str((item or {}).get("last_seen") or (item or {}).get("timestamp") or ""),
            }
        )
    return kept, excluded


def _evidence_item_overlaps_window(
    item: dict[str, Any] | None,
    *,
    window_start: str,
    window_end: str,
) -> bool:
    if not isinstance(item, dict):
        return True
    start_text = str(item.get("first_seen") or item.get("timestamp") or "").strip()
    end_text = str(item.get("last_seen") or item.get("timestamp") or start_text).strip()
    if not start_text and not end_text:
        return True
    try:
        item_start = parse_timestamp(start_text or end_text)
        item_end = parse_timestamp(end_text or start_text)
        analysis_start = parse_timestamp(window_start)
        analysis_end = parse_timestamp(window_end)
    except Exception:
        return True
    return item_end >= analysis_start and item_start <= analysis_end


def _first_non_meta_text(*values: object) -> str:
    for value in values:
        text = str(value or "").strip()
        if text and not _is_meta_summary(text):
            return text
    return ""


def _is_meta_summary(text: str) -> bool:
    lowered = text.casefold()
    return any(
        token in lowered
        for token in (
            "providers aligned",
            "schema-valid providers projected",
            "review target requires validation",
            "this is not majority-vote truth",
            "technical review support",
        )
    )


def _fallback_operational_mechanism(target: dict[str, Any], *, canonical_unit: str) -> str:
    text = " ".join(
        str(target.get(key) or "")
        for key in ("core_target_type", "canonical_target_type", "subsystem", "component", "title")
    ).casefold()
    if "job" in text or "config" in text or "deployment" in text:
        return "Configuration, deployment, or scheduled-job behavior may be shaping the observed runtime signal."
    if "runtime" in text or "restart" in text or "watchdog" in text:
        return "Runtime recovery or watchdog orchestration may be shaping the observed state transitions."
    if "external" in text or "dependency" in text or "youtube" in text:
        return "An external dependency or downstream health signal may be involved, but it needs independent confirmation."
    if "observability" in text or "instrument" in text or "metric" in text:
        return "The instrumentation contract may be incomplete or inconsistent with runtime behavior."
    return f"The `{canonical_unit}` review unit groups provider claims and cited evidence that need human operational interpretation."


def _fallback_suspected_issue(target: dict[str, Any], *, canonical_unit: str) -> str:
    request_type = str(target.get("recommended_request_type") or "")
    text = " ".join(
        str(target.get(key) or "")
        for key in ("core_target_type", "canonical_target_type", "subsystem", "component", "title")
    ).casefold()
    if "job" in text or "config" in text or "deployment" in text or "deployment_correlation" in request_type:
        return (
            f"Review whether configuration, deployment timing, or scheduled-job behavior for `{canonical_unit}` "
            "correlates with the cited runtime evidence."
        )
    if "runtime" in text or "restart" in text or "watchdog" in text:
        return f"Review whether `{canonical_unit}` indicates a runtime recovery or watchdog behavior that needs validation."
    if "external" in text or "dependency" in text or "youtube" in text:
        return f"Review whether `{canonical_unit}` reflects an external dependency or downstream health issue."
    if "observability" in text or "instrument" in text or "metric" in text:
        return f"Review whether `{canonical_unit}` reflects an instrumentation or observability contract gap."
    return f"Review what operational issue `{canonical_unit}` represents before promoting it."


def _why_not_promoted(blocked_reason: str) -> str:
    if "user_impact" in blocked_reason:
        return "Not promoted because user impact or operational outcome evidence is not attached to this target."
    if "context" in blocked_reason:
        return "Not promoted because context can guide interpretation but cannot prove runtime incident support."
    if "support_without_evidence" in blocked_reason:
        return "Not promoted because runtime support is missing usable Evidence Item IDs."
    if blocked_reason:
        return f"Not promoted because the promotion gate is still open: {blocked_reason}."
    return "Not promoted until a human reviews the cited evidence and confirms the operational outcome."


def _string_items(value: object) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value if str(item).strip()]
    text = str(value or "").strip()
    return [text] if text else []


def _unique_strings(values: object) -> list[str]:
    if not isinstance(values, list):
        values = _string_items(values)
    output: list[str] = []
    for value in values:
        text = str(value).strip()
        if text and text not in output:
            output.append(text)
    return output


def _operator_question(target: dict[str, Any], *, blocked_reason: str) -> str:
    request_type = str(target.get("recommended_request_type") or "")
    canonical_unit = str(target.get("canonical_review_unit") or target.get("subsystem") or "general")
    if request_type:
        return f"Run `{request_type}` to decide whether `{canonical_unit}` should remain a validation target or be promoted."
    if "user_impact" in blocked_reason:
        return f"Attach user-impact or operational outcome evidence before promoting `{canonical_unit}`."
    return f"Review the cited Evidence Item IDs before changing the state of `{canonical_unit}`."


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
    partial_overlap_count = sum(1 for target in targets if 1 < _int(target.get("provider_count")) < provider_count)
    conflict_count = sum(
        1
        for target in targets
        for position in target.get("provider_positions") or []
        if isinstance(position, dict) and str(position.get("stance") or "") == "contradicted"
    )
    return {
        "targets_total": len(targets),
        "primary_promoted_count": sum(1 for target in targets if str(target.get("class") or "") == "primary_candidate"),
        "convergence_count": convergence_count,
        "single_source_count": single_source_count,
        "rule_or_context_count": sum(1 for target in targets if _int(target.get("provider_count")) == 0),
        "partial_overlap_count": partial_overlap_count,
        "conflict_count": conflict_count,
        "auto_archived_count": _int(summary.get("auto_archived_count")),
        "hidden_multi_provider_archived_count": 0,
        "incident_baseline_established_count": 0,
        "technical_baseline": "established" if _technical_baseline_established(canonical_graph) else "open",
        "incident_baseline": "established" if _incident_baseline_established(canonical_graph) else "open",
        "provider_detection_overlap": str((agreement.get("provider_detection_overlap") or {}).get("value") or ""),
        "review_unit_convergence": str((agreement.get("review_unit_convergence") or {}).get("value") or ""),
        "score_definition": "Convergence score = claimed successful providers / all successful providers. Silent providers count against convergence.",
        "note": (
            "Provider convergence is technical support only; causal and impact judgement remains human-gated. "
            "Partial overlap is an overlay count for converged targets where at least one schema-valid provider was silent; "
            "it is not additive with target verdict counts."
        ),
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


def _target_claim(
    target: dict[str, Any],
    *,
    provider_count: int,
    valid_count: int,
    evidence_ref_count: int,
) -> str:
    text = str(target.get("impact_summary") or "").strip()
    if text and "providers aligned on a review signal" not in text:
        return text
    unit = str(target.get("canonical_review_unit") or target.get("subsystem") or "review unit")
    refs = f"{evidence_ref_count} cited Evidence Item(s)" if evidence_ref_count else "cited Evidence Items unavailable"
    if provider_count >= 2:
        return (
            f"{provider_count}/{max(valid_count, 1)} schema-valid providers projected {unit} with {refs}; "
            "this is technical review support, not majority-vote truth."
        )
    if provider_count == 1:
        return (
            f"1/{max(valid_count, 1)} schema-valid provider projected {unit} with {refs}; "
            "the target remains single-source validation work."
        )
    return f"Deterministic routing projected {unit} with {refs}; provider support still needs validation."


def _promotion_explanation(*, state: str, provider_count: int, valid_count: int) -> str:
    if state == "primary_candidate":
        return (
            "Primary candidacy is based on review priority, subsystem relevance, and unresolved operational risk, "
            "not on having the highest provider convergence. Incident promotion remains human-gated."
        )
    if provider_count >= 2:
        return (
            f"{provider_count}/{max(valid_count, 1)} providers converged, so this is technical support; "
            "it remains validation work until user impact or operational outcome evidence is attached."
        )
    if provider_count == 1:
        return "A single provider surfaced this target; it needs corroboration before promotion."
    return "This target is context/rule driven and needs runtime support before promotion."


def _analysis_conclusion_impact(canonical_graph: dict[str, Any], targets: list[dict[str, Any]]) -> str:
    finding = canonical_graph.get("finding") if isinstance(canonical_graph.get("finding"), dict) else {}
    impact = str(finding.get("impact") or "").strip()
    if impact and "providers aligned on a review signal" not in impact:
        return _public_review_language(impact)
    primary = next(
        (
            target
            for target in targets
            if str(target.get("class") or target.get("state") or "") == "primary_candidate"
        ),
        None,
    )
    if isinstance(primary, dict):
        return str(primary.get("claim") or "")
    if targets:
        return str(targets[0].get("claim") or "")
    return impact


def _public_review_language(text: str) -> str:
    replacements = {
        "Providers aligned on a technical baseline": "Providers aligned on technical support",
        "No incident baseline agreement was found": "No incident-promotion agreement was found",
        "technical baseline": "technical support",
        "incident baseline": "incident promotion",
    }
    output = text
    for old, new in replacements.items():
        output = output.replace(old, new)
    return output


def _profile_context(
    *,
    profile_id: str,
    profile_draft: dict[str, Any],
    approved_profile: dict[str, Any],
    source_context_sha: str,
    source_analysis_sha: str,
) -> dict[str, Any]:
    draft_profile = profile_draft.get("profile") if isinstance(profile_draft.get("profile"), dict) else {}
    approved_id = str(approved_profile.get("profile_id") or "")
    effective_profile_id = profile_id or approved_id
    component_map = approved_profile.get("component_map") if isinstance(approved_profile.get("component_map"), dict) else {}
    draft_components = draft_profile.get("components") if isinstance(draft_profile.get("components"), list) else []
    metric_semantics = approved_profile.get("metric_semantics") or draft_profile.get("metric_semantics") or {}
    collector_mappings = approved_profile.get("collector_mappings") or draft_profile.get("collector_mappings") or {}
    required_decisions = profile_draft.get("required_human_decisions")
    if not isinstance(required_decisions, list):
        required_decisions = [
            "Approve profile context before treating it as an explicit operational profile.",
            "Keep source context separate from runtime evidence.",
        ]
    has_context = bool(profile_draft or approved_profile or effective_profile_id or source_context_sha or source_analysis_sha)
    generation = profile_draft.get("profile_generation") if isinstance(profile_draft.get("profile_generation"), dict) else {}
    llm_status = str(generation.get("llm_status") or profile_draft.get("llm_status") or ("persisted" if has_context else "not_run"))
    return {
        "schema_version": "profile_context_summary.v1",
        "profile_id": effective_profile_id,
        "generation_mode": (
            "profile_draft_and_approved_profile"
            if profile_draft and approved_profile
            else "approved_profile_context"
            if approved_profile or effective_profile_id
            else "sanitized_source_context"
            if has_context
            else "not_run"
        ),
        "llm_status": llm_status,
        "approved": bool(approved_profile or effective_profile_id),
        "explicit_profile": bool(approved_profile or effective_profile_id),
        "draft_schema_version": str(profile_draft.get("schema_version") or ""),
        "source_discovery_sha256": str(profile_draft.get("source_discovery_sha256") or ""),
        "source_context_sha256": source_context_sha,
        "source_analysis_sha256": source_analysis_sha,
        "system_type": str(approved_profile.get("system_type") or draft_profile.get("system_type") or ""),
        "purpose": str(approved_profile.get("purpose") or draft_profile.get("purpose") or ""),
        "component_count": len(component_map) if component_map else len(draft_components),
        "metric_semantics_count": len(metric_semantics) if isinstance(metric_semantics, dict | list) else 0,
        "collector_mapping_count": len(collector_mappings) if isinstance(collector_mappings, dict | list) else 0,
        "required_human_decisions": [str(item) for item in required_decisions if str(item or "").strip()][:8],
        "context_is_not_incident_evidence": True,
        "summary": (
            "Profile context was generated or approved from sanitized discovery; it constrains interpretation "
            "but runtime claims still require Evidence Item IDs."
            if has_context
            else "No profile context was recorded for this payload."
        ),
    }


def _profile_draft_generation(profile_context: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": "profile_draft_generation_summary.v1",
        "generation_mode": str(profile_context.get("generation_mode") or "not_run"),
        "llm_status": str(profile_context.get("llm_status") or "not_run"),
        "approved": bool(profile_context.get("approved")),
        "explicit_profile": bool(profile_context.get("explicit_profile")),
        "profile_id": str(profile_context.get("profile_id") or ""),
        "component_count": _int(profile_context.get("component_count")),
        "metric_semantics_count": _int(profile_context.get("metric_semantics_count")),
        "collector_mapping_count": _int(profile_context.get("collector_mapping_count")),
        "required_human_decisions": list(profile_context.get("required_human_decisions") or []),
    }


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


def _load_profile(path: str) -> dict[str, Any]:
    text = Path(path).read_text(encoding="utf-8")
    data = yaml.safe_load(text)
    if not isinstance(data, dict):
        raise SystemExit(f"expected profile mapping: {path}")
    return data


if __name__ == "__main__":
    raise SystemExit(main())
