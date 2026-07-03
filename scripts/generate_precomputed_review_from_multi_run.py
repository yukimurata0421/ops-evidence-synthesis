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
from ops_evidence_synthesis.profile_gate import build_profile_context_summary
from ops_evidence_synthesis.synthesis.priority_scoring import score_review_priority
from ops_evidence_synthesis.timeutils import parse_timestamp
from ops_evidence_synthesis.window_policy import (
    DEFAULT_MIN_ANALYSIS_WINDOW_HOURS,
    validate_minimum_analysis_window,
)


DEFAULT_SOURCE_NOTE = (
    "generated from a recorded e2e API real provider run using a sanitized log corpus and optional sanitized source context"
)
DEFAULT_PROVIDER_MODE = "real_api_vertex_gemini_gpt_oss_mistral_qwen_glm"
PUBLIC_EVIDENCE_REF_LIMIT = 80
PUBLIC_EVIDENCE_SUMMARY_LIMIT = 16
PUBLIC_COUNTER_SUMMARY_LIMIT = 12
PRIMARY_CANDIDATE_MIN_EVIDENCE_REFS = 3
PROFILE_OUTCOME_FALLBACKS = {
    "stream_v3_dell_runtime_source_approved": [
        "Continuous YouTube streaming",
        "ADSB data processing",
    ],
    "stream_v3_arena_server_monitoring_source_approved": [
        "Maintain YouTube stream uptime",
        "Monitor ADSB stream health",
    ],
}


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
    source = _bundle_source(bundle)
    time_window = _bundle_time_window(bundle)
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
    raw_db_corpus_coverage = bundle.get("db_corpus_coverage") if isinstance(bundle.get("db_corpus_coverage"), dict) else {}
    log_count = (
        _int(local_first.get("sanitized_event_count"))
        or _int(raw_db_corpus_coverage.get("total_row_count"))
        or _int(raw_db_corpus_coverage.get("covered_row_count"))
    )
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
        log_count=log_count,
    )
    updated = updated_at or str((api_response.get("pipeline_status") or {}).get("completed_at") or "")
    full_items = _int(corpus_summary.get("full_evidence_item_count"))
    model_items = _int(corpus_summary.get("model_evidence_item_count"))
    model_occurrences = _int(corpus_summary.get("model_occurrence_count"))
    coverage = float(corpus_summary.get("occurrence_coverage_ratio") or 0.0)
    db_corpus_coverage = _bundle_db_corpus_coverage(bundle, fallback_rows=log_count)
    evidence_item_accounting = _evidence_item_accounting(
        full_items=full_items,
        db_corpus_coverage=db_corpus_coverage,
    )
    determinism_scope = _determinism_scope()
    provider_full_corpus_coverage = _provider_full_corpus_coverage(
        api_response,
        full_items=full_items,
    )
    projection_policy = model_projection_policy or (
        "Single-prompt metadata records the bounded Evidence Bundle projection. "
        "Multi-provider synthesis uses chunked full-corpus Evidence Item coverage; row-level raw logs stay out of provider prompts."
    )
    projection_interpretation = _projection_coverage_interpretation(
        service=str(source.get("service") or "service"),
        log_count=log_count,
        full_items=full_items,
        model_items=model_items,
        model_occurrences=model_occurrences,
        coverage=coverage,
        full_corpus_coverage=provider_full_corpus_coverage,
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
        review_targets=targets,
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
            "db_corpus_coverage": db_corpus_coverage,
            "db_corpus_row_count": _int(db_corpus_coverage.get("total_row_count")),
            "db_corpus_covered_row_count": _int(db_corpus_coverage.get("covered_row_count")),
            "db_corpus_coverage_ratio": float(db_corpus_coverage.get("coverage_ratio") or 0.0),
            "db_corpus_pattern_count": _int(db_corpus_coverage.get("pattern_count")),
            "db_corpus_singleton_pattern_count": _int(db_corpus_coverage.get("singleton_pattern_count")),
            "db_corpus_coverage_class_counts": dict(db_corpus_coverage.get("coverage_class_counts") or {}),
            "db_corpus_direct_prompt_row_count": _int(db_corpus_coverage.get("direct_prompt_row_count")),
            "db_corpus_raw_rows_sent_to_providers": bool(db_corpus_coverage.get("raw_rows_sent_to_providers")),
            "db_corpus_row_assignments_sha256": str(db_corpus_coverage.get("row_assignments_sha256") or ""),
            "evidence_item_count": full_items,
            "evidence_item_accounting": evidence_item_accounting,
            "provider_full_corpus_coverage": provider_full_corpus_coverage,
            "provider_full_corpus_evidence_items": _int(provider_full_corpus_coverage.get("full_evidence_item_count")),
            "provider_full_corpus_analyzed_evidence_items": _int(
                provider_full_corpus_coverage.get("analyzed_evidence_item_count")
            ),
            "provider_full_corpus_unassigned_evidence_items": _int(
                provider_full_corpus_coverage.get("unassigned_evidence_item_count")
            ),
            "provider_full_corpus_coverage_ratio": float(provider_full_corpus_coverage.get("coverage_ratio") or 0.0),
            "provider_full_corpus_chunk_count": _int(provider_full_corpus_coverage.get("max_chunk_count")),
            "provider_full_corpus_chunk_manifest_count": _int(
                provider_full_corpus_coverage.get("max_chunk_manifest_entry_count")
            ),
            "provider_full_corpus_chunk_manifest_sha256s": list(
                provider_full_corpus_coverage.get("chunk_manifest_sha256s") or []
            ),
            "determinism_scope": determinism_scope,
            "model_projection_evidence_items": model_items,
            "model_projection_occurrence_count": model_occurrences,
            "model_projection_occurrence_coverage_ratio": coverage,
            "model_projection_policy": projection_policy,
            "model_projection_interpretation": projection_interpretation,
            "raw_log_policy": str(bundle.get("raw_log_policy") or local_first.get("raw_log_policy") or "not_uploaded"),
            "raw_source_policy": str(source_context.get("raw_source_policy") or "not_uploaded"),
            "source_context_sha256": source_context_sha,
            "source_analysis_sha256": source_analysis_sha,
            "token_usage": dict(synthesis.get("token_usage") or {}),
            "log_observations": [
                (
                    f"The run used {db_corpus_coverage.get('covered_row_count', log_count):,}/"
                    f"{db_corpus_coverage.get('total_row_count', log_count):,} sanitized "
                    f"{source.get('service', 'service')} DB rows from {time_window.get('start')} "
                    f"to {time_window.get('end')} as the coverage corpus; direct raw-row prompt count was "
                    f"{db_corpus_coverage.get('direct_prompt_row_count', 0):,}."
                ),
                (
                    f"The local-first Evidence Bundle retained {full_items:,} "
                    f"grouped evidence items, including {db_corpus_coverage.get('singleton_pattern_count', 0):,} "
                    f"singleton pattern(s), {db_corpus_coverage.get('low_frequency_pattern_count', 0):,} "
                    f"low-frequency pattern(s), and {len(bundle.get('signals') or []):,} pre-AI route signal(s)."
                ),
                _evidence_item_accounting_observation(evidence_item_accounting),
                (
                    f"The single-prompt projection used {model_items:,} selected evidence items covering "
                    f"{model_occurrences:,} occurrences ({coverage:.1%} of the sanitized corpus); "
                    f"provider synthesis covered {provider_full_corpus_coverage.get('analyzed_evidence_item_count', 0):,}/"
                    f"{provider_full_corpus_coverage.get('full_evidence_item_count', full_items):,} grouped Evidence Items "
                    f"via chunked calls."
                ),
                projection_interpretation,
                (
                    "Real provider outputs are recorded and hashed; deterministic reproduction applies to "
                    "the canonical merge over sorted recorded chunk outputs and to local fixture regeneration."
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
                _analysis_conclusion_impact(canonical_graph, targets, public_review_counts=public_review_counts),
            ],
        },
        "agent_trace": [],
        "devops_loop": _devops_loop(
            model_items=model_items,
            model_occurrences=model_occurrences,
            provider_statuses=provider_statuses,
            valid_provider_statuses=valid_provider_statuses,
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
                "schema_errors": _compact_provider_schema_errors(run.get("schema_errors") or []),
                "retry": dict(run.get("retry") or {}),
                "chunk_status_counts": dict(run.get("chunk_status_counts") or {}),
                "chunk_failure_count": _int(run.get("chunk_failure_count")),
                "partial_chunk_result": dict(run.get("partial_chunk_result") or {}),
            }
        )
    return sorted(rows, key=lambda row: row["provider_id"])


def _provider_position_for_target(
    provider_id: str,
    provider_status: dict[str, Any],
    *,
    claimed: bool,
    model_run_hash: str,
) -> dict[str, str]:
    if provider_status and (
        str(provider_status.get("status") or "") != "ok" or not bool(provider_status.get("schema_valid"))
    ):
        failure = str(provider_status.get("failure_reason") or "").strip()
        if not failure:
            failure = "Provider output was not schema-valid for this run."
        return {
            "provider_id": provider_id,
            "stance": "provider_error",
            "model_run_hash": model_run_hash,
            "one_line": f"{failure} Excluded from convergence denominator.",
        }
    return {
        "provider_id": provider_id,
        "stance": "claimed" if claimed else "silent",
        "model_run_hash": model_run_hash,
        "one_line": (
            "Projected this canonical review unit from the real API run."
            if claimed
            else "Did not surface this normalized review target."
        ),
    }


def _bundle_time_window(bundle: dict[str, Any]) -> dict[str, str]:
    time_window = bundle.get("time_window") if isinstance(bundle.get("time_window"), dict) else {}
    start = str(
        time_window.get("start")
        or bundle.get("window_start")
        or ((bundle.get("incident_window") or {}).get("start") if isinstance(bundle.get("incident_window"), dict) else "")
        or ""
    )
    end = str(
        time_window.get("end")
        or bundle.get("window_end")
        or ((bundle.get("incident_window") or {}).get("end") if isinstance(bundle.get("incident_window"), dict) else "")
        or ""
    )
    return {"start": start, "end": end}


def _bundle_source(bundle: dict[str, Any]) -> dict[str, str]:
    source = bundle.get("source") if isinstance(bundle.get("source"), dict) else {}
    return {
        "service": str(source.get("service") or bundle.get("service") or ""),
        "environment": str(source.get("environment") or bundle.get("environment") or ""),
    }


def _compact_provider_schema_errors(errors: Any, *, limit: int = 8) -> list[str]:
    compact: list[str] = []
    for error in errors or []:
        text = str(error or "").strip()
        if not text or text in compact:
            continue
        compact.append(text)
        if len(compact) >= limit:
            break
    total = len([error for error in errors or [] if str(error or "").strip()])
    if total > len(compact):
        compact.append(f"... {total - len(compact)} additional schema/error detail(s) omitted from public payload")
    return compact


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
    provider_status_by_id = {str(row["provider_id"]): row for row in provider_statuses}
    run_hashes = {str(row["provider_id"]): str(row.get("raw_output_sha256") or "")[:12] for row in provider_statuses}
    valid_count = max(1, sum(1 for row in provider_statuses if row.get("status") == "ok" and row.get("schema_valid")))
    targets = []
    for target in api_response.get("review_targets") or []:
        if not isinstance(target, dict):
            continue
        claimed = {str(provider) for provider in target.get("providers") or []}
        provider_positions = [
            _provider_position_for_target(
                provider_id,
                provider_status_by_id.get(provider_id, {}),
                claimed=provider_id in claimed,
                model_run_hash=run_hashes.get(provider_id, ""),
            )
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
        original_target_class = str(target.get("class") or "validation_target")
        preliminary_blocked_reason = _blocked_reason(
            target,
            provider_count=provider_count,
            target_class=original_target_class,
        )
        preliminary_missing_evidence = _public_missing_evidence(
            target,
            blocked_reason=preliminary_blocked_reason,
        )
        canonical_review_unit = _public_canonical_review_unit(
            target,
            evidence_refs=evidence_refs,
            evidence_lookup=evidence_lookup,
        )
        public_target = dict(target)
        public_target["canonical_review_unit"] = canonical_review_unit
        if _is_generic_review_unit(str(public_target.get("subsystem") or "")):
            public_target["subsystem"] = canonical_review_unit
        evidence_ref_total_count = len(evidence_refs)
        public_evidence_refs = evidence_refs[:PUBLIC_EVIDENCE_REF_LIMIT]
        evidence_ref_overflow_count = max(0, evidence_ref_total_count - len(public_evidence_refs))
        target_explanation = _public_target_explanation(
            public_target,
            evidence_refs=public_evidence_refs,
            blocked_reason=preliminary_blocked_reason,
            evidence_lookup=evidence_lookup,
            window_start=window_start,
            window_end=window_end,
        )
        public_title = _public_target_title(public_target, canonical_review_unit=canonical_review_unit)
        raw = public_target.get("raw") if isinstance(public_target.get("raw"), dict) else {}
        source_candidate_count = _int(public_target.get("source_candidate_count")) or _int(raw.get("source_candidate_count")) or 1
        evidence_family_count = len({_evidence_family(ref) for ref in evidence_refs if _evidence_family(ref)})
        target_class, classification = _public_target_class(
            public_target,
            original_class=original_target_class,
            provider_count=provider_count,
            valid_count=valid_count,
            evidence_ref_count=evidence_ref_total_count,
            evidence_family_count=evidence_family_count,
            source_candidate_count=source_candidate_count,
            target_explanation=target_explanation,
            missing_evidence=preliminary_missing_evidence,
            blocked_reason=preliminary_blocked_reason,
        )
        blocked_reason = (
            "evidence_thin_primary_candidate; human_review_required; core_evidence_or_user_impact_unverified"
            if classification.get("adjustment") == "demoted_primary_candidate_evidence_thin"
            else preliminary_blocked_reason
        )
        missing_evidence = _public_missing_evidence(target, blocked_reason=blocked_reason)
        if classification.get("adjustment") == "demoted_primary_candidate_evidence_thin":
            missing_evidence = _unique_strings(
                [
                    *missing_evidence,
                    "Evidence strength sufficient for primary candidacy, not just provider agreement.",
                ]
            )
        promotion_state = "primary_candidate" if target_class == "primary_candidate" else "validation"
        priority_result = score_review_priority(
            prior_score=float(public_target.get("review_priority_score") or 0.0),
            promotion_score=float(public_target.get("promotion_score") or 0.0),
            provider_positions=provider_positions,
            total_provider_count=valid_count,
            evidence_ref_count=evidence_ref_total_count,
            evidence_family_count=evidence_family_count,
            source_candidate_count=source_candidate_count,
            target_class=target_class,
            canonical_review_unit=canonical_review_unit,
            title=public_title,
            suspected_issue=str(target_explanation.get("suspected_issue") or ""),
            operational_mechanism=str(target_explanation.get("operational_mechanism") or ""),
            why_it_matters=str(target_explanation.get("why_it_matters") or ""),
            missing_evidence=missing_evidence,
            blocked_reasons=[blocked_reason],
            caveats=list(public_target.get("caveats") or []),
        )
        targets.append(
            {
                "target_id": str(public_target.get("target_id") or public_target.get("review_target_id") or ""),
                "review_target_id": str(public_target.get("review_target_id") or public_target.get("target_id") or ""),
                "title": public_title,
                "class": target_class,
                "original_class": original_target_class,
                "classification": classification,
                "state": str(public_target.get("state") or target_class),
                "status": str(public_target.get("status") or "pending"),
                "subsystem": str(public_target.get("subsystem") or "general"),
                "canonical_review_unit": canonical_review_unit,
                "review_priority_score": priority_result["score"],
                "raw_review_priority_score": round(float(public_target.get("review_priority_score") or 0.0), 4),
                "score_breakdown": priority_result["breakdown"],
                "provider_count": provider_count,
                "recommended_request_type": str(public_target.get("recommended_request_type") or ""),
                "claim": _target_claim(
                    public_target,
                    provider_count=provider_count,
                    valid_count=valid_count,
                    evidence_ref_count=evidence_ref_total_count,
                ),
                "review_reason": _review_reason_summary(
                    public_target,
                    provider_count=provider_count,
                    valid_count=valid_count,
                    evidence_ref_count=evidence_ref_total_count,
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
                        f"{provider_count}/{valid_count} schema-valid "
                        f"{'provider' if valid_count == 1 else 'providers'} projected this review unit "
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
                "evidence_refs": public_evidence_refs,
                "evidence_ref_total_count": evidence_ref_total_count,
                "evidence_ref_display_count": len(public_evidence_refs),
                "evidence_ref_overflow_count": evidence_ref_overflow_count,
                "excluded_evidence_refs": excluded_evidence_refs,
                "missing_evidence": missing_evidence,
                "caveats": list(target.get("caveats") or []),
                "raw": {
                    "baseline_support_score": public_target.get("baseline_support_score"),
                    "canonical_group_key": public_target.get("canonical_group_key"),
                    "rollup_provider_ratio": public_target.get("rollup_provider_ratio"),
                    "source_candidate_count": source_candidate_count,
                },
            }
        )
    return _filter_and_dedupe_public_targets(targets)


def _public_canonical_review_unit(
    target: dict[str, Any],
    *,
    evidence_refs: list[str],
    evidence_lookup: dict[str, dict[str, Any]],
) -> str:
    explicit = str(target.get("canonical_review_unit") or "").strip()
    if not _is_generic_review_unit(explicit):
        return explicit
    subsystem = str(target.get("subsystem") or "").strip()
    if not _is_generic_review_unit(subsystem):
        return subsystem
    inferred = _infer_review_unit_from_target_context(
        target,
        evidence_refs=evidence_refs,
        evidence_lookup=evidence_lookup,
    )
    return inferred or "general"


def _is_generic_review_unit(value: str) -> bool:
    return value.strip().casefold() in {"", "general", "general_review", "validation_target", "unknown", "none", "null"}


def _infer_review_unit_from_target_context(
    target: dict[str, Any],
    *,
    evidence_refs: list[str],
    evidence_lookup: dict[str, dict[str, Any]],
) -> str:
    request_type = str(target.get("recommended_request_type") or "").casefold()
    if "external_dependency" in request_type or "downstream" in request_type:
        return "downstream_dependency"
    if "instrumentation" in request_type or "observability" in request_type or "metric_semantics" in request_type:
        return "observability_contract"
    if "user_impact" in request_type or "outcome" in request_type:
        return "user_experience"
    if "deployment" in request_type or "job_configuration" in request_type or "scheduler" in request_type:
        return "job_configuration"
    if "process_state" in request_type or "runtime" in request_type or "restart" in request_type:
        return "runtime_recovery"

    text = _target_context_text(target, evidence_refs=evidence_refs, evidence_lookup=evidence_lookup)
    scores = {
        "downstream_dependency": _keyword_score(
            text,
            ("external", "dependency", "downstream", "webhook", "discord", "gmail", "http", "tls", "certificate"),
        ),
        "observability_contract": _keyword_score(
            text,
            ("instrumentation", "observability", "metric", "logging", "error_count", "semantic"),
        ),
        "runtime_recovery": _keyword_score(
            text,
            ("restart", "runtime_restart", "watchdog", "systemd", "service_start_failure", "process state", "exit code"),
        ),
        "job_configuration": _keyword_score(
            text,
            ("job_configuration", "configuration", "deployment", "scheduler", "timer", "artifact"),
        ),
        "background_processing": _keyword_score(
            text,
            ("run_once", "run_result", "checkpoint", "pipeline_commit", "pubsub", "processed", "matched", "notified"),
        ),
        "user_experience": _keyword_score(
            text,
            ("user impact", "user_impact", "delivery", "notification", "recipient", "end-to-end", "user outcome"),
        ),
        "service_liveness": _keyword_score(text, ("liveness", "heartbeat", "health", "service health")),
    }
    winner, score = max(scores.items(), key=lambda row: row[1])
    return winner if score > 0 else ""


def _target_context_text(
    target: dict[str, Any],
    *,
    evidence_refs: list[str],
    evidence_lookup: dict[str, dict[str, Any]],
) -> str:
    parts: list[str] = []
    for key in (
        "recommended_request_type",
        "core_target_type",
        "canonical_target_type",
        "review_target_type",
        "title",
        "suspected_issue",
        "operational_mechanism",
        "why_it_matters",
    ):
        parts.append(str(target.get(key) or ""))
    parts.extend(str(item) for item in target.get("missing_evidence") or [] if item)
    for evidence_id in evidence_refs[:80]:
        row = evidence_lookup.get(evidence_id) or {}
        for key in ("message_template", "summary", "event_type", "error_type", "type", "severity_text"):
            parts.append(str(row.get(key) or ""))
    return " ".join(parts).casefold()


def _keyword_score(text: str, keywords: tuple[str, ...]) -> int:
    return sum(1 for keyword in keywords if keyword in text)


def _public_target_title(target: dict[str, Any], *, canonical_review_unit: str) -> str:
    title = str(target.get("title") or "").strip()
    unit = str(canonical_review_unit or target.get("subsystem") or "review unit").strip() or "review unit"
    title_key = title.casefold()
    generic_title = (
        not title
        or title_key == "review target requires validation"
        or title_key.startswith("review target requires validation:")
        or title_key.endswith(": general")
    )
    if title and not generic_title:
        return title
    return f"Review target requires validation: {unit}"


def _filter_and_dedupe_public_targets(targets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    winners: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for target in targets:
        if _is_low_information_public_target(target):
            continue
        key = _public_target_dedupe_key(target)
        current = winners.get(key)
        if current is None:
            winners[key] = target
            order.append(key)
            continue
        if _public_target_rank(target) > _public_target_rank(current):
            winners[key] = target
    return sorted(
        (winners[key] for key in order),
        key=lambda target: (
            -_public_target_rank(target)[0],
            -float(target.get("review_priority_score") or 0.0),
            -_public_target_rank(target)[1],
            str(target.get("canonical_review_unit") or ""),
            str(target.get("target_id") or ""),
        ),
    )


def _public_target_dedupe_key(target: dict[str, Any]) -> str:
    unit = _normalized_dedupe_text(target.get("canonical_review_unit") or target.get("subsystem") or "general")
    if unit == "general":
        return "unit:general"
    return f"unit:{unit}"


def _public_target_rank(target: dict[str, Any]) -> tuple[int, int, float, int, int]:
    class_rank = 2 if str(target.get("class") or "") == "primary_candidate" else 1
    provider_count = _int(target.get("provider_count"))
    priority = float(target.get("review_priority_score") or 0.0)
    evidence_count = len(target.get("evidence_refs") or [])
    source_candidates = _int((target.get("raw") or {}).get("source_candidate_count"))
    return (class_rank, provider_count, priority, evidence_count, source_candidates)


def _public_target_class(
    target: dict[str, Any],
    *,
    original_class: str,
    provider_count: int,
    valid_count: int,
    evidence_ref_count: int,
    evidence_family_count: int,
    source_candidate_count: int,
    target_explanation: dict[str, Any],
    missing_evidence: list[str],
    blocked_reason: str,
) -> tuple[str, dict[str, Any]]:
    original = str(original_class or "validation_target")
    classification = {
        "schema_version": "public_target_classification.v1",
        "original_class": original,
        "final_class": original,
        "adjustment": "",
        "reason": "",
        "provider_support": f"{provider_count}/{max(valid_count, 1)}",
        "evidence_ref_count": evidence_ref_count,
        "evidence_family_count": evidence_family_count,
        "source_candidate_count": source_candidate_count,
        "policy": (
            "Provider convergence can create high-priority validation work, but primary candidacy "
            "requires enough runtime evidence to avoid presenting evidence-thin signals as cause candidates."
        ),
    }
    if original != "primary_candidate":
        return original, classification
    if _primary_candidate_is_evidence_thin(
        target,
        evidence_ref_count=evidence_ref_count,
        evidence_family_count=evidence_family_count,
        source_candidate_count=source_candidate_count,
        target_explanation=target_explanation,
        missing_evidence=missing_evidence,
        blocked_reason=blocked_reason,
    ):
        classification.update(
            {
                "final_class": "validation_target",
                "adjustment": "demoted_primary_candidate_evidence_thin",
                "reason": (
                    "Provider support is treated as technical review signal only because cited runtime "
                    "evidence, source breadth, or user-impact evidence is too thin for public primary candidacy."
                ),
            }
        )
        return "validation_target", classification
    return original, classification


def _primary_candidate_is_evidence_thin(
    target: dict[str, Any],
    *,
    evidence_ref_count: int,
    evidence_family_count: int,
    source_candidate_count: int,
    target_explanation: dict[str, Any],
    missing_evidence: list[str],
    blocked_reason: str,
) -> bool:
    if evidence_ref_count < PRIMARY_CANDIDATE_MIN_EVIDENCE_REFS:
        return True
    if evidence_family_count < 2:
        return True
    if source_candidate_count <= 1 and evidence_ref_count <= PRIMARY_CANDIDATE_MIN_EVIDENCE_REFS:
        return True
    text = _classification_evidence_text(
        target,
        target_explanation=target_explanation,
        missing_evidence=missing_evidence,
        blocked_reason=blocked_reason,
    )
    if any(
        phrase in text
        for phrase in (
            "no specific failure signals",
            "no explicit failure signal",
            "no error logs",
            "no metric spikes",
            "no audio energy",
            "not confirmed",
            "cannot confirm",
            "insufficient evidence",
            "evidence is thin",
            "missing evidence",
            "core_missing_evidence",
            "user_impact_unverified",
        )
    ):
        return True
    if any(
        phrase in text
        for phrase in (
            "specific error logs",
            "metric time-series",
            "status logs",
            "measurement logs",
            "operational outcome evidence",
        )
    ):
        return True
    return False


def _classification_evidence_text(
    target: dict[str, Any],
    *,
    target_explanation: dict[str, Any],
    missing_evidence: list[str],
    blocked_reason: str,
) -> str:
    parts: list[str] = [blocked_reason, *missing_evidence]
    for key in (
        "suspected_issue",
        "operational_mechanism",
        "why_it_matters",
        "why_not_promoted",
        "next_validation_question",
    ):
        parts.append(str(target_explanation.get(key) or target.get(key) or ""))
    for key in ("evidence_summary", "counter_evidence_summary"):
        values = target_explanation.get(key) or target.get(key) or []
        parts.extend(str(item) for item in values if str(item).strip())
    return " ".join(parts).casefold()


def _evidence_family(ref: str) -> str:
    text = str(ref or "").strip().upper()
    if not text:
        return ""
    if "-" in text:
        return text.split("-", 1)[0]
    match = re.match(r"[A-Z]+", text)
    return match.group(0) if match else text


def _is_low_information_public_target(target: dict[str, Any]) -> bool:
    if str(target.get("class") or "") == "primary_candidate":
        return False
    unit = _normalized_dedupe_text(target.get("canonical_review_unit") or target.get("subsystem") or "general")
    issue = str(target.get("suspected_issue") or "").strip()
    mechanism = str(target.get("operational_mechanism") or "").strip()
    why = str(target.get("why_it_matters") or "").strip()
    claim = str(target.get("claim") or "").strip()
    problem_context = " ".join([unit, issue, mechanism, why, claim])
    if _is_non_actionable_status_text(issue) or _is_non_actionable_status_text(mechanism):
        return True
    if unit == "general" and not _contains_positive_problem_signal(problem_context):
        return True
    if issue.casefold() in {"", "unknown", "n/a", "none"} and not _contains_positive_problem_signal(problem_context):
        return True
    return False


def _is_non_actionable_status_text(text: str) -> bool:
    lowered = str(text or "").casefold().strip()
    if not lowered:
        return False
    non_actionable_phrases = (
        "no issue detected",
        "no direct evidence",
        "normal operation",
        "indicates normal",
        "unknown operational status",
        "not a failure",
        "not an incident",
        "healthy state",
    )
    return any(phrase in lowered for phrase in non_actionable_phrases)


def _contains_positive_problem_signal(text: str) -> bool:
    lowered = str(text or "").casefold()
    for phrase in (
        "no direct evidence of",
        "no evidence of",
        "no issue detected",
        "normal operation",
        "indicates normal",
        "unknown operational status",
        "not a failure",
        "not an incident",
    ):
        lowered = lowered.replace(phrase, "")
    return any(token in lowered for token in _PROBLEM_SIGNAL_TOKENS)


def _normalized_dedupe_text(value: object) -> str:
    text = str(value or "").casefold().strip()
    text = re.sub(r"[^a-z0-9]+", "_", text)
    return text.strip("_") or "general"


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
    provider_noun = "provider" if valid_count == 1 else "providers"
    factors = [
        (
            f"{provider_count}/{valid_count} schema-valid {provider_noun} independently projected "
            f"the normalized review unit `{canonical_unit}`."
        ),
        (
            f"{evidence_ref_count} chunk-tracked Evidence Item association(s) tie the unit back to the "
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
    evidence_summary = _compact_public_summary_entries(
        evidence_summary,
        limit=PUBLIC_EVIDENCE_SUMMARY_LIMIT,
        omitted_label="supporting evidence summary",
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
    counter_summary = _compact_public_summary_entries(
        counter_summary,
        limit=PUBLIC_COUNTER_SUMMARY_LIMIT,
        omitted_label="counter or weak-signal summary",
    )
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


def _compact_public_summary_entries(values: list[str], *, limit: int, omitted_label: str) -> list[str]:
    compacted: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value or "").strip()
        if not text:
            continue
        key = re.sub(r"\s+", " ", text.casefold())
        if key in seen:
            continue
        seen.add(key)
        compacted.append(text)
    if len(compacted) <= limit:
        return compacted
    omitted = len(compacted) - limit
    return [
        *compacted[:limit],
        (
            f"{omitted} additional {omitted_label} item(s) are omitted from this public detail view; "
            "the full count remains in evidence_ref_total_count and the chunk manifest."
        ),
    ]


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
    incident_established = _incident_baseline_established(canonical_graph)
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
        "incident_baseline": "established" if incident_established else "open",
        "incident_gate_signal": "signal_present" if incident_established else "not_established",
        "incident_gate_scope": "graph_level_signal_not_target_promotion",
        "target_promotion_policy": (
            "Incident gate signal is a graph-level support signal. Each review target still has its own "
            "promotion state, and promotion remains human-gated until impact and operational outcome evidence "
            "are attached to that target."
        ),
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
            f"{provider_count} schema-valid real provider {'output' if provider_count == 1 else 'outputs'}; "
            f"{convergence_count} {'review unit' if convergence_count == 1 else 'review units'} "
            "had at least two provider positions while impact remains human-gated."
        ),
    }


def _provider_full_corpus_coverage(api_response: dict[str, Any], *, full_items: int) -> dict[str, Any]:
    runs = [row for row in api_response.get("model_runs") or [] if isinstance(row, dict)]
    valid_runs = [
        row
        for row in runs
        if str(row.get("status") or "") == "ok"
        and row.get("schema_valid") is True
    ]
    coverages = [
        row.get("full_corpus_coverage")
        for row in valid_runs
        if isinstance(row.get("full_corpus_coverage"), dict)
    ]
    if not coverages:
        return {
            "schema_version": "provider_full_corpus_coverage.v1",
            "mode": "not_reported",
            "provider_count": len(runs),
            "schema_valid_provider_count": len(valid_runs),
            "full_evidence_item_count": full_items,
            "analyzed_evidence_item_count": 0,
            "coverage_ratio": 0.0,
            "max_chunk_count": 0,
            "all_schema_valid_providers_covered_full_corpus": False,
        }
    ratios = [float(row.get("coverage_ratio") or 0.0) for row in coverages]
    analyzed_counts = [int(row.get("analyzed_evidence_item_count") or 0) for row in coverages]
    full_counts = [int(row.get("full_evidence_item_count") or full_items) for row in coverages]
    chunk_counts = [int(row.get("chunk_count") or 0) for row in coverages]
    chunk_manifest_counts = [int(row.get("chunk_manifest_entry_count") or 0) for row in coverages]
    chunk_manifest_sha256_values = [str(row.get("chunk_manifest_sha256") or "") for row in coverages]
    chunk_manifest_sha256s = sorted({value for value in chunk_manifest_sha256_values if value})
    unassigned_counts = [int(row.get("unassigned_evidence_item_count") or 0) for row in coverages]
    coverage_ratio = min(ratios) if ratios else 0.0
    analyzed = min(analyzed_counts) if analyzed_counts else 0
    total = max(full_counts or [full_items])
    return {
        "schema_version": "provider_full_corpus_coverage.v1",
        "mode": "full_evidence_item_chunking",
        "provider_count": len(runs),
        "schema_valid_provider_count": len(valid_runs),
        "reported_provider_count": len(coverages),
        "full_evidence_item_count": total,
        "analyzed_evidence_item_count": analyzed,
        "coverage_ratio": round(coverage_ratio, 6),
        "max_chunk_count": max(chunk_counts or [0]),
        "max_chunk_manifest_entry_count": max(chunk_manifest_counts or [0]),
        "chunk_manifest_sha256s": chunk_manifest_sha256s,
        "unassigned_evidence_item_count": max(unassigned_counts or [0]),
        "all_provider_chunk_manifests_present": (
            bool(coverages)
            and len(coverages) == len(valid_runs)
            and all(chunk_manifest_sha256_values)
        ),
        "all_schema_valid_providers_covered_full_corpus": (
            len(coverages) == len(valid_runs)
            and bool(valid_runs)
            and coverage_ratio >= 1.0
            and analyzed >= total
            and max(unassigned_counts or [0]) == 0
        ),
    }


def _evidence_item_accounting(*, full_items: int, db_corpus_coverage: dict[str, Any]) -> dict[str, Any]:
    pattern_groups = _int(db_corpus_coverage.get("pattern_count"))
    derived_items = max(0, int(full_items) - pattern_groups)
    return {
        "schema_version": "evidence_item_accounting.v1",
        "total_evidence_items": int(full_items),
        "db_pattern_groups": pattern_groups,
        "derived_metric_or_operational_items": derived_items,
        "explanation": (
            "Evidence Item count can exceed DB pattern groups because deterministic metric, state, "
            "or operational boundary items are added after row-level grouping."
        ),
    }


def _evidence_item_accounting_observation(accounting: dict[str, Any]) -> str:
    total = _int(accounting.get("total_evidence_items"))
    patterns = _int(accounting.get("db_pattern_groups"))
    derived = _int(accounting.get("derived_metric_or_operational_items"))
    if not total or not patterns:
        return str(accounting.get("explanation") or "")
    return (
        f"Evidence accounting: {patterns:,} DB pattern group(s) plus {derived:,} deterministic "
        f"metric/state/operational item(s) produced {total:,} Evidence Item(s)."
    )


def _determinism_scope() -> dict[str, str]:
    return {
        "provider_outputs": "recorded_and_hashed_not_recreated_byte_for_byte",
        "chunk_merge": "deterministic_sort_dedup_over_recorded_chunk_outputs",
        "local_fixture": "byte_equal_regeneration_for_deterministic_local_provider_ci",
    }


def _bundle_db_corpus_coverage(bundle: dict[str, Any], *, fallback_rows: int) -> dict[str, Any]:
    coverage = bundle.get("db_corpus_coverage") if isinstance(bundle.get("db_corpus_coverage"), dict) else {}
    if coverage:
        total = _int(coverage.get("total_row_count"))
        covered = _int(coverage.get("covered_row_count"))
        return {
            "schema_version": str(coverage.get("schema_version") or "db_corpus_coverage.v1"),
            "source_table": str(coverage.get("source_table") or "logs_sanitized"),
            "strategy": str(coverage.get("strategy") or ""),
            "total_row_count": total,
            "covered_row_count": covered,
            "uncovered_row_count": _int(coverage.get("uncovered_row_count")),
            "coverage_ratio": float(coverage.get("coverage_ratio") or (covered / total if total else 1.0)),
            "pattern_count": _int(coverage.get("pattern_count")),
            "singleton_pattern_count": _int(coverage.get("singleton_pattern_count")),
            "low_frequency_pattern_count": _int(coverage.get("low_frequency_pattern_count")),
            "coverage_class_counts": dict(coverage.get("coverage_class_counts") or {}),
            "direct_prompt_row_count": _int(coverage.get("direct_prompt_row_count")),
            "raw_rows_sent_to_providers": bool(coverage.get("raw_rows_sent_to_providers")),
            "prompt_boundary_policy": str(coverage.get("prompt_boundary_policy") or ""),
            "row_assignments_sha256": str(coverage.get("row_assignments_sha256") or ""),
            "row_assignments_in_public_payload": False,
        }
    return {
        "schema_version": "db_corpus_coverage.v1",
        "source_table": "unknown",
        "strategy": "legacy_payload_without_row_coverage_ledger",
        "total_row_count": int(fallback_rows),
        "covered_row_count": 0,
        "uncovered_row_count": int(fallback_rows),
        "coverage_ratio": 0.0 if fallback_rows else 1.0,
        "pattern_count": 0,
        "singleton_pattern_count": 0,
        "low_frequency_pattern_count": 0,
        "coverage_class_counts": {},
        "direct_prompt_row_count": 0,
        "raw_rows_sent_to_providers": False,
        "prompt_boundary_policy": "",
        "row_assignments_sha256": "",
        "row_assignments_in_public_payload": False,
    }


def _projection_coverage_interpretation(
    *,
    service: str,
    log_count: int,
    full_items: int,
    model_items: int,
    model_occurrences: int,
    coverage: float,
    full_corpus_coverage: dict[str, Any] | None = None,
) -> str:
    full_corpus_coverage = full_corpus_coverage if isinstance(full_corpus_coverage, dict) else {}
    base = (
        f"Single-prompt projection coverage is occurrence-weighted, not raw-row coverage: the full sanitized "
        f"{service} bundle keeps {log_count:,} rows and {full_items:,} grouped Evidence Items, while "
        f"the bounded projection tracks {model_items:,} high-signal Evidence Items representing "
        f"{model_occurrences:,} repeated occurrences."
    )
    if bool(full_corpus_coverage.get("all_schema_valid_providers_covered_full_corpus")):
        return (
            f"{base} Multi-provider synthesis then covered "
            f"{int(full_corpus_coverage.get('analyzed_evidence_item_count') or 0):,}/"
            f"{int(full_corpus_coverage.get('full_evidence_item_count') or full_items):,} grouped Evidence Items "
            f"through chunked provider calls across up to "
            f"{int(full_corpus_coverage.get('max_chunk_count') or 0):,} chunk(s), so low-frequency Evidence Items "
            "are analyzed instead of being dropped by frequency."
        )
    if coverage and coverage < 0.25:
        return (
            f"{base} A low percentage means this corpus has a long tail of low-frequency state, "
            "metric, or journal items; those items remain SHA-fixed in the Evidence Bundle for "
            "traceability, but they are not all copied into the bounded single-prompt projection."
        )
    return (
        f"{base} Remaining Evidence Items stay SHA-fixed in the Evidence Bundle for traceability "
        "even when they are outside the bounded single-prompt projection."
    )


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
            "summary": f"{valid_provider_count}/{provider_count} provider outputs were schema-valid; provider identities are recorded with hashes.",
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
    provider_statuses: list[dict[str, Any]],
    valid_provider_statuses: list[dict[str, Any]],
) -> dict[str, Any]:
    provider_count = len(provider_statuses)
    valid_provider_count = len(valid_provider_statuses)
    provider_value = (
        "5 providers"
        if provider_count == 5 and valid_provider_count == 5
        else f"{valid_provider_count}/{provider_count} valid"
    )
    provider_names = _provider_sentence(valid_provider_statuses)
    provider_detail = (
        f"{provider_names} were executed through Vertex-backed endpoints; provider diversity is recorded by model path, not by automatic truth voting."
        if provider_count > 1 and valid_provider_count == provider_count and provider_names
        else (
            f"{provider_names} was executed through a Vertex-backed endpoint; provider identity is recorded with hashes."
            if provider_count == 1 and valid_provider_count == provider_count and provider_names
            else "Requested providers are recorded with status, schema validation, hashes, and retry metadata; only schema-valid outputs contribute review support."
        )
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


def _blocked_reason(target: dict[str, Any], *, provider_count: int, target_class: str | None = None) -> str:
    if str(target_class if target_class is not None else target.get("class") or "") == "primary_candidate":
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
    refs = (
        f"{evidence_ref_count} chunk-tracked Evidence Item association(s)"
        if evidence_ref_count
        else "chunk-tracked Evidence Items unavailable"
    )
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


def _analysis_conclusion_impact(
    canonical_graph: dict[str, Any],
    targets: list[dict[str, Any]],
    *,
    public_review_counts: dict[str, int],
) -> str:
    finding = canonical_graph.get("finding") if isinstance(canonical_graph.get("finding"), dict) else {}
    impact = str(finding.get("impact") or "").strip()
    if "validation target" in impact or "review target" in impact:
        return (
            f"{public_review_counts['primary_targets']} primary candidate and "
            f"{public_review_counts['validation_targets']} validation target(s) remain for human review."
        )
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
    review_targets: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    context = build_profile_context_summary(
        profile_id=profile_id,
        profile_draft=profile_draft,
        approved_profile=approved_profile,
        source_context_sha=source_context_sha,
        source_analysis_sha=source_analysis_sha,
        review_targets=review_targets or [],
    )
    if profile_draft and approved_profile and str(context.get("llm_status") or "") == "persisted":
        context["llm_status"] = "ok"
    if not context.get("provisional_user_outcomes"):
        fallback_outcomes = PROFILE_OUTCOME_FALLBACKS.get(str(profile_id or ""))
        if fallback_outcomes:
            context["provisional_user_outcomes"] = list(fallback_outcomes)
    context["provisional_user_outcomes"] = _normalize_provisional_user_outcomes(
        context.get("provisional_user_outcomes") or []
    )
    return context


def _normalize_provisional_user_outcomes(outcomes: list[Any]) -> list[str]:
    normalized: list[str] = []
    for outcome in outcomes:
        text = str(outcome or "").strip()
        if not text:
            continue
        text = re.sub(r"\s*\(Assumption\)\s*$", " pending human approval", text)
        if text not in normalized:
            normalized.append(text)
    return normalized


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
        "profile_status": str(profile_context.get("profile_status") or ""),
        "confidence_summary": dict(profile_context.get("confidence_summary") or {}),
        "confidence_action": str(profile_context.get("confidence_action") or ""),
        "confirmed_user_outcomes": list(profile_context.get("confirmed_user_outcomes") or []),
        "provisional_user_outcomes": list(profile_context.get("provisional_user_outcomes") or []),
        "human_questions": list(profile_context.get("human_questions") or []),
        "profile_to_review_links": list(profile_context.get("profile_to_review_links") or []),
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
    provider_noun = "provider" if provider_count == 1 else "providers"
    return (
        f"Recorded chunked review of the {log_count:,}-row {service} corpus "
        f"with {valid_provider_count}/{provider_count} schema-valid {provider_noun}"
    )


def _provider_result_sentence(
    provider_statuses: list[dict[str, Any]],
    *,
    valid_provider_statuses: list[dict[str, Any]],
    invalid_provider_statuses: list[dict[str, Any]],
) -> str:
    if provider_statuses and len(valid_provider_statuses) == len(provider_statuses):
        provider_text = _provider_sentence(provider_statuses)
        if len(provider_statuses) == 1:
            return f"{provider_text} returned a schema-valid output."
        return f"{provider_text} all returned schema-valid outputs."
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
        provider_text = _provider_sentence(provider_statuses)
        if len(provider_statuses) == 1:
            return f"{provider_text} returned status=ok and schema_valid=true."
        return f"{provider_text} all returned status=ok and schema_valid=true."
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
        "gemma-agent-platform": "Gemma 4",
        "glm-agent-platform": "GLM",
    }
    return mapping.get(str(provider_id), str(provider_id))


def _provider_label_rank(provider_id: str) -> int:
    order = {
        "gemini-enterprise-agent-platform": 0,
        "openai-gpt-oss-on-vertex": 1,
        "mistral-agent-platform": 2,
        "qwen-agent-platform": 3,
        "gemma-agent-platform": 4,
        "glm-agent-platform": 5,
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
