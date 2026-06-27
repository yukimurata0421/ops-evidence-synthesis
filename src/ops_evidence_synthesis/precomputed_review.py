from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any

from ops_evidence_synthesis.canonical import sha256_json
from ops_evidence_synthesis.models import ModelRunRecord, ParsedResultRecord
from ops_evidence_synthesis.storage.sqlite_store import SQLiteStore
from ops_evidence_synthesis.timeutils import utc_now


SCORE_DEFINITION = "claimed successful providers / all successful providers"
PUBLIC_DEMO_PROVIDERS = ("local-gemini", "local-gpt-oss", "local-mistral", "local-fail")


def build_precomputed_review_summary(
    store: SQLiteStore,
    evidence_sha256: str,
    *,
    updated_at: str | None = None,
    target_limit: int = 5,
    source_note: str = "generated from deterministic local pipeline",
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
    payload = {
        "schema_version": "precomputed_review_summary.v1",
        "evidence_sha256": str(evidence_sha256),
        "updated_at": str(updated_at or bundle.get("window_end") or utc_now()),
        "generation": {
            "schema_version": "precomputed_review_generation.v1",
            "generator": "ops_evidence_synthesis.precomputed_review",
            "source_note": source_note,
            "provider_mode": "deterministic_local",
            "score_definition": SCORE_DEFINITION,
            "raw_log_policy": str(bundle.get("raw_log_policy") or "not_uploaded"),
        },
        "summary": summary,
        "agent_trace": _agent_trace(bundle, provider_statuses, targets),
        "devops_loop": _devops_loop(),
        "provider_statuses": provider_statuses,
        "review_graph_summary": graph_summary,
        "targets": targets,
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
        "log_count": int(target_set_summary.get("sanitized_log_count") or 0),
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
        claimed = sum(1 for row in provider_positions if row["stance"] == "claimed")
        total_successful = len(successful_runs)
        convergence_score = claimed / total_successful if total_successful else 0.0
        verdict = "convergence" if claimed >= 2 else "single_source" if claimed == 1 else "rule_or_context"
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
                "review_priority_score": round(float(target.get("review_priority_score") or 0.0), 4),
                "provider_count": claimed,
                "recommended_request_type": _recommended_request_type(drawer),
                "claim": _observed_claim(target),
                "provider_positions": provider_positions,
                "agreement": {
                    "verdict": verdict,
                    "convergence_score": round(convergence_score, 10),
                    "score_definition": SCORE_DEFINITION,
                    "technical_baseline": "established" if claimed >= 2 else "open",
                    "incident_baseline": "open",
                    "summary": _agreement_summary(claimed, total_successful, verdict),
                },
                "promotion": {
                    "state": "validation",
                    "blocked_reason": _blocked_reason(claimed),
                    "score_cap_applied": False,
                    "score_note": "Priority is review urgency, not truth probability.",
                },
                "evidence_refs": evidence_refs,
                "missing_evidence": missing_evidence,
            }
        )
    return projected


def _provider_positions(
    target: dict[str, Any],
    drawer: dict[str, Any],
    *,
    successful_runs: list[ModelRunRecord],
) -> list[dict[str, Any]]:
    claimed_providers = _claimed_providers(target, drawer)
    rows = []
    for run in sorted(successful_runs, key=lambda item: item.provider):
        stance = "claimed" if run.provider in claimed_providers else "silent"
        rows.append(
            {
                "provider_id": run.provider,
                "stance": stance,
                "model_run_hash": run.raw_output_sha256[:12],
                "one_line": _provider_one_line(target, run.provider, stance),
            }
        )
    return rows


def _claimed_providers(target: dict[str, Any], drawer: dict[str, Any]) -> set[str]:
    providers: set[str] = set()
    claim_rows = [claim for claim in drawer.get("claims") or [] if isinstance(claim, dict)]
    for claim in claim_rows:
        if not isinstance(claim, dict):
            continue
        provider = str(claim.get("provider") or "").strip()
        claim_type = str(claim.get("claim_type") or "").casefold()
        if provider and provider != "rule-engine" and claim_type not in {"caveat", "counter_evidence", "context"}:
            providers.add(provider)
    if providers or claim_rows:
        return providers
    raw_providers = target.get("providers")
    if isinstance(raw_providers, list):
        providers.update(str(provider) for provider in raw_providers if str(provider).strip())
    return providers


def _provider_one_line(target: dict[str, Any], provider: str, stance: str) -> str:
    if stance == "silent":
        return "Did not surface this normalized review target."
    title = str(target.get("title") or "review target")
    return f"Projected {title} as evidence-backed review work."


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
    max_claimed = max((int(target.get("provider_count") or 0) for target in targets), default=0)
    summary = (
        f"{convergence_count} converged target(s), {single_source_count} single-source target(s), "
        f"and {rule_count} rule/context target(s). Incident baseline remains human-gated."
    )
    return {
        "targets_total": len(targets),
        "convergence_count": convergence_count,
        "conflict_count": 0,
        "single_source_count": single_source_count,
        "rule_or_context_count": rule_count,
        "incident_baseline_established_count": 0,
        "primary_promoted_count": 0,
        "provider_detection_overlap": f"{max_claimed}/{max(total_successful, 1)}",
        "technical_baseline": "partial" if convergence_count else "open",
        "incident_baseline": "open",
        "review_unit_convergence": "partial" if convergence_count else "none",
        "auto_archived_count": int(target_set_summary.get("auto_archived") or 0),
        "hidden_multi_provider_archived_count": 0,
        "summary": summary,
        "note": "Provider convergence is treated as technical support only; causal judgement remains human-gated.",
        "score_definition": (
            "Convergence score = claimed successful providers / all successful providers. "
            "Silent providers count against convergence."
        ),
    }


def _agent_trace(
    bundle: dict[str, Any],
    provider_statuses: list[dict[str, Any]],
    targets: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    log_count = int(bundle.get("log_count") or 0) or len(bundle.get("logs") or []) or len(bundle.get("evidence_items") or [])
    successful = sum(1 for row in provider_statuses if row["status"] == "ok" and row["schema_valid"])
    total = len(provider_statuses)
    return [
        {
            "step": "sanitize",
            "status": "completed",
            "artifact": "sanitized_events.jsonl",
            "title": "Sanitize local evidence",
            "summary": f"{log_count} sanitized evidence item(s) were prepared locally. Raw logs were not uploaded.",
        },
        {
            "step": "bundle",
            "status": "completed",
            "artifact": "evidence_bundle.json",
            "title": "Freeze Evidence Bundle",
            "summary": "The review input was fixed by SHA256 before model output was projected.",
        },
        {
            "step": "multi_model",
            "status": "completed",
            "artifact": "model_runs",
            "title": "Run deterministic providers",
            "summary": f"{successful}/{total} provider outputs were schema-valid; failed providers remain visible.",
        },
        {
            "step": "arbitrate",
            "status": "completed",
            "artifact": "review_targets",
            "title": "Arbitrate review targets",
            "summary": f"{len(targets)} target(s) were projected with provider stance and human-gated promotion.",
        },
        {
            "step": "deliver",
            "status": "completed",
            "artifact": "precomputed_review_summary",
            "title": "Deliver read-only cache",
            "summary": "The UI serves this generated payload without starting model runs on initial GET.",
        },
    ]


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
        return f"{claimed}/{total} successful providers projected this review unit; incident baseline remains open."
    if verdict == "single_source":
        return f"{claimed}/{total} successful providers projected this review unit, so it stays validation-only."
    return "This target is rule/context-driven and remains validation-only until provider evidence or human evidence closes the gate."


def _blocked_reason(claimed: int) -> str:
    if claimed >= 2:
        return "incident_baseline_open; user_impact_unverified; causal_direction_unverified"
    if claimed == 1:
        return "single_provider_only; user_impact_unverified"
    return "no_provider_claim; human_validation_required"


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
