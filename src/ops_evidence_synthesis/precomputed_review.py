from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any

from ops_evidence_synthesis.agents.adk_investigator import build_adk_tool_contract_trace
from ops_evidence_synthesis.canonical import sha256_json
from ops_evidence_synthesis.models import ModelRunRecord, ParsedResultRecord
from ops_evidence_synthesis.profile_gate import build_profile_context_summary
from ops_evidence_synthesis.storage.sqlite_store import SQLiteStore
from ops_evidence_synthesis.timeutils import utc_now


SCORE_DEFINITION = "supporting schema-valid providers / all schema-valid providers"
PUBLIC_DEMO_PROVIDERS = ("local-gemini", "local-gpt-oss", "local-mistral")


def build_precomputed_review_summary(
    store: SQLiteStore,
    evidence_sha256: str,
    *,
    updated_at: str | None = None,
    target_limit: int = 5,
    source_note: str = "generated from deterministic local pipeline",
    provider_mode: str = "deterministic_local",
    source_context: dict[str, Any] | None = None,
    source_analysis: dict[str, Any] | None = None,
    profile_draft: dict[str, Any] | None = None,
    approved_profile: dict[str, Any] | None = None,
    profile_id: str = "",
) -> dict[str, Any]:
    """Project persisted pipeline output into the fast read-only review payload."""
    bundle = store.get_bundle(evidence_sha256)
    if not bundle:
        raise ValueError(f"evidence bundle not found: {evidence_sha256}")

    target_set = store.list_review_targets(
        limit=target_limit,
        evidence_sha256=evidence_sha256,
        pending_only=False,
        persist=True,
    )
    model_runs = store.fetch_model_runs(evidence_sha256)
    parsed_results = store.fetch_parsed_results(evidence_sha256)
    provider_statuses = _provider_statuses(model_runs, parsed_results)
    successful_runs = [run for run in model_runs if run.status == "ok" and _schema_valid(run, parsed_results)]
    targets = _project_targets(
        target_set.get("targets") or [],
        successful_runs=successful_runs,
    )
    graph_summary = _review_graph_summary(targets, target_set.get("summary") or {}, successful_runs)
    summary = _summary_payload(
        bundle=bundle,
        target_set_summary=target_set.get("summary") or {},
        provider_statuses=provider_statuses,
        graph_summary=graph_summary,
        targets=targets,
    )
    source_context = source_context if isinstance(source_context, dict) else {}
    source_analysis = source_analysis if isinstance(source_analysis, dict) else {}
    profile_draft = profile_draft if isinstance(profile_draft, dict) else {}
    approved_profile = approved_profile if isinstance(approved_profile, dict) else {}
    source_context_sha = _source_context_sha(source_context)
    source_analysis_sha = _source_analysis_sha(source_analysis)
    profile_context = _profile_context(
        profile_id=profile_id,
        profile_draft=profile_draft,
        approved_profile=approved_profile,
        source_context_sha=source_context_sha,
        source_analysis_sha=source_analysis_sha,
        review_targets=targets,
    )
    payload = {
        "schema_version": "precomputed_review_summary.v1",
        "evidence_sha256": str(evidence_sha256),
        "updated_at": str(updated_at or bundle.get("window_end") or utc_now()),
        "generation": {
            "schema_version": "precomputed_review_generation.v1",
            "generator": "ops_evidence_synthesis.precomputed_review",
            "source_note": source_note,
            "provider_mode": str(provider_mode),
            "score_definition": SCORE_DEFINITION,
            "raw_log_policy": str(bundle.get("raw_log_policy") or "not_uploaded"),
        },
        "summary": summary,
        "analysis_context": _analysis_context(
            bundle=bundle,
            profile_id=str(profile_context.get("profile_id") or ""),
            source_context=source_context,
            source_analysis=source_analysis,
            source_context_sha=source_context_sha,
            source_analysis_sha=source_analysis_sha,
        ),
        "profile_context": profile_context,
        "profile_draft_generation": _profile_draft_generation(profile_context),
        "agent_trace": [],
        "devops_loop": _devops_loop(),
        "provider_statuses": provider_statuses,
        "review_graph_summary": graph_summary,
        "targets": targets,
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


def write_precomputed_review_summary(payload: dict[str, Any], output_dir: str | Path) -> Path:
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    evidence_sha256 = str(payload.get("evidence_sha256") or "")
    if not evidence_sha256:
        raise ValueError("payload is missing evidence_sha256")
    path = output / f"{evidence_sha256}.json"
    path.write_text(_stable_json(payload), encoding="utf-8")
    return path


def stable_precomputed_review_json(payload: dict[str, Any]) -> str:
    return _stable_json(payload)


def _summary_payload(
    *,
    bundle: dict[str, Any],
    target_set_summary: dict[str, Any],
    provider_statuses: list[dict[str, Any]],
    graph_summary: dict[str, Any],
    targets: list[dict[str, Any]],
) -> dict[str, Any]:
    successful = sum(1 for row in provider_statuses if row["status"] == "ok" and row["schema_valid"])
    total = len(provider_statuses)
    primary = sum(1 for target in targets if str(target.get("class") or "") == "incident_candidate")
    validation = max(0, len(targets) - primary)
    if primary:
        title = "Evidence-backed incident candidates require human validation"
    elif targets:
        title = "Multi-AI disagreement requires validation"
    else:
        title = "No review targets were generated"
    impact = (
        f"{validation} validation target(s) remain for human review; "
        "score is review priority, not truth probability."
    )
    if graph_summary.get("convergence_count"):
        impact = (
            f"{graph_summary['convergence_count']} target(s) show provider convergence, "
            f"and {validation} validation target(s) remain human-gated."
        )
    graph_hash = sha256_json({"targets": targets, "graph_summary": graph_summary})
    return {
        "schema_version": "ui_summary.v1",
        "status": "ok",
        "finding": {"title": title, "impact": impact},
        "review": {
            "primary_targets": primary,
            "validation_targets": validation,
            "monitor_only": int(target_set_summary.get("monitor_only") or 0),
            "auto_archived": int(target_set_summary.get("auto_archived") or 0),
        },
        "providers": {
            "success": successful,
            "total": total,
            "pipeline_status": "completed",
        },
        "baselines": {
            "technical": bool(graph_summary.get("convergence_count")),
            "incident": False,
        },
        "raw_log_policy": str(bundle.get("raw_log_policy") or "not_uploaded"),
        "log_count": _bundle_log_count(bundle, target_set_summary),
        "canonical_graph_status": "pipeline_generated",
        "canonical_graph_sha256": graph_hash,
        "input_fingerprint_sha256": sha256_json(
            {
                "evidence_sha256": bundle.get("evidence_sha256"),
                "model_outputs": [row.get("raw_output_sha256") for row in provider_statuses],
                "targets": [
                    {
                        "review_target_id": target.get("review_target_id"),
                        "agreement": target.get("agreement"),
                    }
                    for target in targets
                ],
            }
        ),
    }


def _bundle_log_count(bundle: dict[str, Any], target_set_summary: dict[str, Any]) -> int:
    local_first_summary = bundle.get("local_first_summary") if isinstance(bundle.get("local_first_summary"), dict) else {}
    for value in (
        local_first_summary.get("sanitized_event_count"),
        bundle.get("sanitized_event_count"),
        target_set_summary.get("sanitized_log_count"),
    ):
        try:
            count = int(value)
        except (TypeError, ValueError):
            continue
        if count >= 0:
            return count
    return 0


def _project_targets(
    raw_targets: list[dict[str, Any]],
    *,
    successful_runs: list[ModelRunRecord],
) -> list[dict[str, Any]]:
    projected = []
    for index, target in enumerate(raw_targets, start=1):
        drawer = target.get("drawer") if isinstance(target.get("drawer"), dict) else {}
        evidence_refs = _evidence_refs(target, drawer)
        missing_evidence = _missing_evidence(target, drawer)
        provider_positions = _provider_positions(target, drawer, successful_runs=successful_runs)
        supporting = sum(1 for row in provider_positions if row.get("supports_agreement") is True)
        countering = sum(1 for row in provider_positions if row.get("signals_disagreement") is True)
        participating = sum(1 for row in provider_positions if row["stance"] != "silent")
        total_successful = len(successful_runs)
        convergence_score = supporting / total_successful if total_successful else 0.0
        verdict = "convergence" if supporting >= 2 else "single_source" if supporting == 1 else "rule_or_context"
        rollup = _target_rollup_projection(target)
        source_candidates = [dict(row) for row in target.get("source_candidates") or [] if isinstance(row, dict)][:100]
        stable_id = "prt-" + sha256_json(
            {
                "evidence_refs": evidence_refs,
                "index": index,
                "subsystem": target.get("subsystem"),
                "title": target.get("title"),
                "claim": target.get("core_claim") or target.get("proposal"),
            }
        )[:20]
        projected.append(
            {
                "review_target_id": stable_id,
                "title": str(target.get("title") or f"Review target {index}"),
                "class": str(target.get("review_mode") or "validation_target"),
                "subsystem": str(target.get("subsystem") or "general"),
                "canonical_review_unit": str(target.get("canonical_review_unit") or target.get("subsystem") or "general"),
                "canonical_review_family": str(target.get("canonical_review_family") or ""),
                "canonical_key_contract": dict(target.get("canonical_key_contract") or {}),
                "review_priority_score": round(float(target.get("review_priority_score") or 0.0), 4),
                "provider_count": supporting,
                "support_provider_count": supporting,
                "counter_provider_count": countering,
                "participating_provider_count": participating,
                "recommended_request_type": _recommended_request_type(drawer),
                "claim": _observed_claim(target),
                "provider_positions": provider_positions,
                "agreement": {
                    "verdict": verdict,
                    "convergence_score": round(convergence_score, 10),
                    "score_definition": SCORE_DEFINITION,
                    "technical_baseline": "established" if supporting >= 2 else "open",
                    "incident_baseline": "open",
                    "summary": _agreement_summary(supporting, total_successful, verdict),
                },
                "promotion": {
                    "state": "validation",
                    "blocked_reason": _blocked_reason(supporting),
                    "explanation": _promotion_explanation(
                        state="validation",
                        claimed=supporting,
                        total_successful=total_successful,
                    ),
                    "score_cap_applied": False,
                    "score_note": "Priority is review urgency, not truth probability.",
                },
                "evidence_refs": evidence_refs,
                "missing_evidence": missing_evidence,
                "rollup": rollup,
                "source_candidates": source_candidates,
                "overmerge_review": {
                    "required": bool(rollup["source_candidate_count"] > 1 or rollup["distinct_target_type_count"] > 1),
                    "mixed_target_types": rollup["distinct_target_type_count"] > 1,
                    "warning": (
                        "Multiple target types were rolled into this canonical review unit. Compare candidate-type counts and source candidates."
                        if rollup["distinct_target_type_count"] > 1
                        else (
                            "Multiple source candidates were rolled into this canonical review unit. Confirm that they describe one issue."
                            if rollup["source_candidate_count"] > 1
                            else ""
                        )
                    ),
                },
            }
        )
    return projected


def _target_rollup_projection(target: dict[str, Any]) -> dict[str, Any]:
    rollup = target.get("rollup") if isinstance(target.get("rollup"), dict) else {}
    source_candidate_type_counts = (
        rollup.get("source_candidate_type_counts")
        if isinstance(rollup.get("source_candidate_type_counts"), dict)
        else rollup.get("target_type_votes")
        if isinstance(rollup.get("target_type_votes"), dict)
        else {}
    )
    provider_candidate_membership_counts = (
        rollup.get("provider_candidate_membership_counts")
        if isinstance(rollup.get("provider_candidate_membership_counts"), dict)
        else rollup.get("provider_vote_counts")
        if isinstance(rollup.get("provider_vote_counts"), dict)
        else {}
    )
    supporting_provider_counts = (
        rollup.get("supporting_provider_counts")
        if isinstance(rollup.get("supporting_provider_counts"), dict)
        else {}
    )
    countering_provider_counts = (
        rollup.get("countering_provider_counts")
        if isinstance(rollup.get("countering_provider_counts"), dict)
        else {}
    )
    source_candidate_count = int(rollup.get("source_candidate_count") or target.get("source_candidate_count") or 1)
    distinct_target_type_count = int(
        rollup.get("distinct_target_type_count") or len(source_candidate_type_counts) or 1
    )
    return {
        "source_candidate_count": source_candidate_count,
        "support_source_candidate_count": int(rollup.get("support_source_candidate_count") or 0),
        "independent_provider_count": int(rollup.get("independent_provider_count") or 0),
        "independent_support_provider_count": int(
            rollup.get("independent_support_provider_count") or target.get("support_provider_count") or 0
        ),
        "provider_candidate_membership_counts": dict(sorted(provider_candidate_membership_counts.items())),
        "supporting_provider_counts": dict(sorted(supporting_provider_counts.items())),
        "countering_provider_counts": dict(sorted(countering_provider_counts.items())),
        "source_candidate_type_counts": dict(sorted(source_candidate_type_counts.items())),
        "provider_vote_counts": dict(sorted(provider_candidate_membership_counts.items())),
        "target_type_votes": dict(sorted(source_candidate_type_counts.items())),
        "distinct_target_type_count": distinct_target_type_count,
        "target_type_divergence": distinct_target_type_count > 1,
        "target_type_divergence_penalty": float(rollup.get("target_type_divergence_penalty") or 0.0),
        "rollup_provider_ratio": float(rollup.get("rollup_provider_ratio") or target.get("rollup_provider_ratio") or 0.0),
    }


def _provider_positions(
    target: dict[str, Any],
    drawer: dict[str, Any],
    *,
    successful_runs: list[ModelRunRecord],
) -> list[dict[str, Any]]:
    supporting_providers, countering_providers, participating_providers = _provider_sets(target, drawer)
    rows = []
    for run in sorted(successful_runs, key=lambda item: item.provider):
        supporting = run.provider in supporting_providers
        countering = run.provider in countering_providers
        participating = run.provider in participating_providers
        stance = (
            "support_and_counter"
            if supporting and countering
            else "support"
            if supporting
            else "counter"
            if countering
            else "caveat_or_validation"
            if participating
            else "silent"
        )
        rows.append(
            {
                "provider_id": run.provider,
                "stance": stance,
                "model_run_hash": run.raw_output_sha256[:12],
                "one_line": _provider_one_line(target, run.provider, stance),
                "supports_agreement": supporting,
                "signals_disagreement": countering,
            }
        )
    return rows


def _provider_sets(target: dict[str, Any], drawer: dict[str, Any]) -> tuple[set[str], set[str], set[str]]:
    explicit_supporting = target.get("supporting_providers")
    if isinstance(explicit_supporting, list):
        supporting = {str(provider) for provider in explicit_supporting if str(provider).strip()}
        countering = {
            str(provider) for provider in target.get("countering_providers") or [] if str(provider).strip()
        }
        participating = {
            str(provider)
            for provider in target.get("participating_providers") or target.get("providers") or []
            if str(provider).strip()
        }
        return supporting, countering, participating | supporting | countering

    supporting: set[str] = set()
    countering: set[str] = set()
    participating: set[str] = set()
    claim_rows = [claim for claim in drawer.get("claims") or [] if isinstance(claim, dict)]
    for claim in claim_rows:
        if not isinstance(claim, dict):
            continue
        provider = str(claim.get("provider") or "").strip()
        claim_type = str(claim.get("claim_type") or "").casefold()
        if not provider or provider == "rule-engine":
            continue
        participating.add(provider)
        if claim_type == "counter_evidence":
            countering.add(provider)
        elif claim_type not in {"caveat", "context", "validation_target", "next_data_needed"}:
            supporting.add(provider)
    if claim_rows:
        return supporting, countering, participating
    raw_providers = target.get("providers")
    if isinstance(raw_providers, list):
        supporting.update(str(provider) for provider in raw_providers if str(provider).strip())
    return supporting, countering, set(supporting)


def _provider_one_line(target: dict[str, Any], provider: str, stance: str) -> str:
    if stance == "silent":
        return "Did not surface this normalized review target."
    if stance == "counter":
        return "Supplied counter-evidence for this normalized review target."
    if stance == "support_and_counter":
        return "Supplied both support and counter-evidence for this normalized review target."
    if stance == "caveat_or_validation":
        return "Participated with a caveat or validation request, but did not support the claim."
    title = str(target.get("title") or "review target")
    return f"Supported {title} as evidence-backed review work."


def _observed_claim(target: dict[str, Any]) -> str:
    text = str(target.get("core_claim") or target.get("proposal") or target.get("title") or "")
    sentences = [item.strip() for item in text.split(". ") if item.strip()]
    if len(sentences) < 2:
        return text
    seen: set[str] = set()
    unique: list[str] = []
    for sentence in sentences:
        key = sentence.rstrip(".")
        if key in seen:
            continue
        seen.add(key)
        unique.append(sentence)
    return ". ".join(unique)


def _provider_statuses(
    model_runs: list[ModelRunRecord],
    parsed_results: list[ParsedResultRecord],
) -> list[dict[str, Any]]:
    schema_by_run = {result.run_id: bool(result.schema_valid) for result in parsed_results}
    return [
        {
            "provider_id": run.provider,
            "status": run.status,
            "schema_valid": bool(schema_by_run.get(run.run_id, False)),
            "raw_output_sha256": run.raw_output_sha256,
        }
        for run in sorted(model_runs, key=lambda item: item.provider)
    ]


def _schema_valid(run: ModelRunRecord, parsed_results: list[ParsedResultRecord]) -> bool:
    return any(result.run_id == run.run_id and result.schema_valid for result in parsed_results)


def _review_graph_summary(
    targets: list[dict[str, Any]],
    target_set_summary: dict[str, Any],
    successful_runs: list[ModelRunRecord],
) -> dict[str, Any]:
    convergence_count = sum(1 for target in targets if (target.get("agreement") or {}).get("verdict") == "convergence")
    single_source_count = sum(1 for target in targets if (target.get("agreement") or {}).get("verdict") == "single_source")
    rule_count = sum(1 for target in targets if (target.get("agreement") or {}).get("verdict") == "rule_or_context")
    total_successful = len(successful_runs)
    max_supporting = max(
        (int(target.get("support_provider_count") or target.get("provider_count") or 0) for target in targets),
        default=0,
    )
    partial_overlap_count = sum(
        1 for target in targets if 1 < int(target.get("provider_count") or 0) < max(total_successful, 1)
    )
    explicit_conflict_count = sum(
        1
        for target in targets
        for position in target.get("provider_positions") or []
        if isinstance(position, dict)
        and (
            position.get("signals_disagreement") is True
            or str(position.get("stance") or "") in {"counter", "support_and_counter", "contradicted"}
        )
    )
    summary = (
        f"{convergence_count} converged target(s), {single_source_count} single-source target(s), "
        f"and {rule_count} rule/context target(s). Incident promotion remains human-gated."
    )
    return {
        "targets_total": len(targets),
        "convergence_count": convergence_count,
        "partial_overlap_count": partial_overlap_count,
        "conflict_count": explicit_conflict_count,
        "single_source_count": single_source_count,
        "rule_or_context_count": rule_count,
        "incident_baseline_established_count": 0,
        "primary_promoted_count": 0,
        "provider_detection_overlap": f"{max_supporting}/{max(total_successful, 1)}",
        "technical_baseline": "partial" if convergence_count else "open",
        "incident_baseline": "open",
        "review_unit_convergence": "partial" if convergence_count else "none",
        "auto_archived_count": int(target_set_summary.get("auto_archived") or 0),
        "hidden_multi_provider_archived_count": 0,
        "summary": summary,
        "note": (
            "Provider convergence is treated as technical support only; causal judgement remains human-gated. "
            "Partial overlap is an overlay count for targets where some schema-valid providers were silent; "
            "it is not additive with converged/single-source target counts."
        ),
        "score_definition": (
            "Convergence score = supporting schema-valid providers / all schema-valid providers. "
            "Counter, caveat, validation-only, and silent positions do not count as support."
        ),
    }


def _devops_loop() -> dict[str, Any]:
    return {
        "title": "AI workflow is operated as production software",
        "summary": "The public repository verifies that the fast UI cache can be regenerated from a deterministic local pipeline.",
        "items": [
            {
                "label": "Deterministic replay",
                "value": "local providers",
                "detail": "The public demo uses local providers that require no keys or network access.",
            },
            {
                "label": "Fixture fidelity",
                "value": "byte comparison",
                "detail": "CI regenerates the public fixture and compares it with the committed cache.",
            },
            {
                "label": "Provider frontier",
                "value": "failed outputs retained",
                "detail": "A simulated provider failure remains visible in provider status.",
            },
            {
                "label": "Post-deploy smoke",
                "value": "root + detail checked",
                "detail": "The release smoke script checks the public review pages and the UI time budget.",
            },
        ],
    }


def _evidence_refs(target: dict[str, Any], drawer: dict[str, Any]) -> list[str]:
    refs = []
    for source in (
        target.get("evidence_refs"),
        [_evidence_id(item) for item in drawer.get("evidence_refs_read") or []],
        [item.get("evidence_id") for item in drawer.get("support_evidence") or [] if isinstance(item, dict)],
    ):
        if isinstance(source, list):
            refs.extend(str(item) for item in source if str(item).strip())
    return _unique(refs)


def _evidence_id(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("evidence_id") or "")
    return str(value or "")


def _missing_evidence(target: dict[str, Any], drawer: dict[str, Any]) -> list[str]:
    values = []
    for source in (target.get("missing_evidence"), drawer.get("missing_evidence")):
        if isinstance(source, list):
            values.extend(str(item) for item in source if str(item).strip())
    return _unique(values)


def _recommended_request_type(drawer: dict[str, Any]) -> str:
    requests = [row for row in drawer.get("next_evidence_requests") or [] if isinstance(row, dict)]
    if not requests:
        return "human_validation"
    return str(requests[0].get("request_type") or requests[0].get("type") or "human_validation")


def _agreement_summary(claimed: int, total: int, verdict: str) -> str:
    if verdict == "convergence":
        return f"{claimed}/{total} schema-valid providers supported this review unit; incident promotion remains human-gated."
    if verdict == "single_source":
        return f"{claimed}/{total} schema-valid providers supported this review unit, so it stays validation-only."
    return "This target is rule/context-driven and remains validation-only until provider evidence or human evidence closes the gate."


def _blocked_reason(claimed: int) -> str:
    if claimed >= 2:
        return "incident_baseline_open; user_impact_unverified; causal_direction_unverified"
    if claimed == 1:
        return "single_provider_only; user_impact_unverified"
    return "no_provider_support; human_validation_required"


def _promotion_explanation(*, state: str, claimed: int, total_successful: int) -> str:
    if state == "primary_candidate":
        return (
            "Primary candidacy is selected by review priority and incident relevance, not by maximum "
            "provider count. It still stops at the human gate until impact and causality are validated."
        )
    if claimed >= 2:
        return (
            f"{claimed}/{max(total_successful, 1)} providers supplied support, so this is technical review support; "
            "it remains a validation target until user impact or operational outcome evidence closes the gate."
        )
    if claimed == 1:
        return "A single provider surfaced this target, so it remains validation work until corroborated."
    return "This target came from deterministic routing or context and needs runtime evidence before promotion."


def _analysis_context(
    *,
    bundle: dict[str, Any],
    profile_id: str,
    source_context: dict[str, Any],
    source_analysis: dict[str, Any],
    source_context_sha: str,
    source_analysis_sha: str,
) -> dict[str, Any]:
    source = bundle.get("source") if isinstance(bundle.get("source"), dict) else {}
    time_window = bundle.get("time_window") if isinstance(bundle.get("time_window"), dict) else {}
    local_first = bundle.get("local_first_summary") if isinstance(bundle.get("local_first_summary"), dict) else {}
    source_summary = source_context.get("project_summary") if isinstance(source_context.get("project_summary"), dict) else {}
    analysis_summary = source_analysis.get("summary") if isinstance(source_analysis.get("summary"), dict) else {}
    source_item_count = len(source_context.get("source_items") or [])
    config_item_count = len(source_context.get("config_items") or [])
    component_count = len(source_analysis.get("component_candidates") or [])
    metric_count = len(source_analysis.get("metric_semantics_candidates") or [])
    collector_count = len(source_analysis.get("collector_mapping_candidates") or [])
    log_count = _bundle_log_count(bundle, {})
    db_corpus_coverage = _bundle_db_corpus_coverage(bundle, fallback_rows=log_count)
    return {
        "schema_version": "deterministic_source_context_summary.v1",
        "service": str(source.get("service") or bundle.get("service") or ""),
        "environment": str(source.get("environment") or bundle.get("environment") or ""),
        "window_start": str(time_window.get("start") or bundle.get("window_start") or ""),
        "window_end": str(time_window.get("end") or bundle.get("window_end") or ""),
        "profile_id": profile_id,
        "sanitized_log_count": log_count,
        "db_ingested_log_count": log_count,
        "db_corpus_coverage": db_corpus_coverage,
        "db_corpus_row_count": int(db_corpus_coverage.get("total_row_count") or 0),
        "db_corpus_covered_row_count": int(db_corpus_coverage.get("covered_row_count") or 0),
        "db_corpus_coverage_ratio": float(db_corpus_coverage.get("coverage_ratio") or 0.0),
        "db_corpus_pattern_count": int(db_corpus_coverage.get("pattern_count") or 0),
        "db_corpus_singleton_pattern_count": int(db_corpus_coverage.get("singleton_pattern_count") or 0),
        "db_corpus_row_assignments_sha256": str(db_corpus_coverage.get("row_assignments_sha256") or ""),
        "evidence_item_count": len(bundle.get("evidence_items") or []),
        "raw_log_policy": str(bundle.get("raw_log_policy") or local_first.get("raw_log_policy") or "not_uploaded"),
        "raw_source_policy": str(source_context.get("raw_source_policy") or "not_uploaded"),
        "source_context_sha256": source_context_sha,
        "source_analysis_sha256": source_analysis_sha,
        "source_item_count": source_item_count,
        "config_item_count": config_item_count,
        "component_candidate_count": component_count,
        "metric_semantics_candidate_count": metric_count,
        "collector_mapping_candidate_count": collector_count,
        "detected_languages": list(source_summary.get("detected_languages") or []),
        "entrypoint_candidates": list(source_summary.get("entrypoint_candidates") or [])[:12],
        "analysis_summary": analysis_summary,
        "source_observations": [
            (
                f"Sanitized source context was attached with source_context_sha256={source_context_sha}."
                if source_context_sha
                else "No sanitized source context was attached."
            ),
            (
                f"Source analysis was attached with analysis_sha256={source_analysis_sha}."
                if source_analysis_sha
                else "No source analysis bundle was attached."
            ),
            "Source context is interpretation context only; runtime support still has to cite Evidence Item IDs.",
        ],
    }


def _bundle_db_corpus_coverage(bundle: dict[str, Any], *, fallback_rows: int) -> dict[str, Any]:
    coverage = bundle.get("db_corpus_coverage") if isinstance(bundle.get("db_corpus_coverage"), dict) else {}
    if coverage:
        total = int(coverage.get("total_row_count") or 0)
        covered = int(coverage.get("covered_row_count") or 0)
        return {
            "schema_version": str(coverage.get("schema_version") or "db_corpus_coverage.v1"),
            "source_table": str(coverage.get("source_table") or "logs_sanitized"),
            "strategy": str(coverage.get("strategy") or ""),
            "total_row_count": total,
            "covered_row_count": covered,
            "uncovered_row_count": int(coverage.get("uncovered_row_count") or 0),
            "coverage_ratio": float(coverage.get("coverage_ratio") or (covered / total if total else 1.0)),
            "pattern_count": int(coverage.get("pattern_count") or 0),
            "singleton_pattern_count": int(coverage.get("singleton_pattern_count") or 0),
            "low_frequency_pattern_count": int(coverage.get("low_frequency_pattern_count") or 0),
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
        "row_assignments_sha256": "",
        "row_assignments_in_public_payload": False,
    }


def _profile_context(
    *,
    profile_id: str,
    profile_draft: dict[str, Any],
    approved_profile: dict[str, Any],
    source_context_sha: str,
    source_analysis_sha: str,
    review_targets: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return build_profile_context_summary(
        profile_id=profile_id,
        profile_draft=profile_draft,
        approved_profile=approved_profile,
        source_context_sha=source_context_sha,
        source_analysis_sha=source_analysis_sha,
        review_targets=review_targets or [],
    )


def _profile_draft_generation(profile_context: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": "profile_draft_generation_summary.v1",
        "generation_mode": str(profile_context.get("generation_mode") or "not_run"),
        "llm_status": str(profile_context.get("llm_status") or "not_run"),
        "approved": bool(profile_context.get("approved")),
        "explicit_profile": bool(profile_context.get("explicit_profile")),
        "profile_id": str(profile_context.get("profile_id") or ""),
        "component_count": int(profile_context.get("component_count") or 0),
        "metric_semantics_count": int(profile_context.get("metric_semantics_count") or 0),
        "collector_mapping_count": int(profile_context.get("collector_mapping_count") or 0),
        "profile_status": str(profile_context.get("profile_status") or ""),
        "confidence_summary": dict(profile_context.get("confidence_summary") or {}),
        "confidence_action": str(profile_context.get("confidence_action") or ""),
        "confirmed_user_outcomes": list(profile_context.get("confirmed_user_outcomes") or []),
        "provisional_user_outcomes": list(profile_context.get("provisional_user_outcomes") or []),
        "human_questions": list(profile_context.get("human_questions") or []),
        "profile_to_review_links": list(profile_context.get("profile_to_review_links") or []),
        "required_human_decisions": list(profile_context.get("required_human_decisions") or []),
    }


def _source_context_sha(source_context: dict[str, Any]) -> str:
    return str(source_context.get("source_context_sha256") or "")


def _source_analysis_sha(source_analysis: dict[str, Any]) -> str:
    return str(source_analysis.get("analysis_sha256") or "")


def _unique(values: list[str]) -> list[str]:
    seen: set[str] = set()
    output: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        output.append(text)
    return output


def _stable_json(payload: dict[str, Any]) -> str:
    import json

    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
