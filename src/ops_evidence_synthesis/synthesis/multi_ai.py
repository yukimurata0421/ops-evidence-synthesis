from __future__ import annotations

import json
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from ops_evidence_synthesis.ai.base import ModelProvider, ModelResponse
from ops_evidence_synthesis.ai.provider_registry import build_multi_ai_providers, provider_infos
from ops_evidence_synthesis.ai.runtime import (
    SafetyPreflightResult,
    blocked_provider_response,
    cost_estimate_for_response,
    normalize_model_status,
    run_provider_with_retries,
    safety_preflight_for_model_input,
    summarize_model_run_costs,
)
from ops_evidence_synthesis.canonical import canonical_json, pretty_json, sha256_json, sha256_text
from ops_evidence_synthesis.models import ModelRunRecord, ParsedResultRecord
from ops_evidence_synthesis.pipeline_progress import (
    finish_pipeline_run,
    record_pipeline_event,
    start_pipeline_run,
)
from ops_evidence_synthesis.source_context import (
    source_analysis_model_context,
    source_context_model_context,
    validate_source_analysis_bundle_for_upload,
    validate_source_context_bundle_for_upload,
)
from ops_evidence_synthesis.synthesis.output_ingest import model_output_artifact, parse_model_output
from ops_evidence_synthesis.synthesis.validation import validate_claim_result
from ops_evidence_synthesis.timeutils import utc_now


MODEL_RUN_SCHEMA_VERSION = "model_run.v1"
MULTI_AI_SYNTHESIS_SCHEMA_VERSION = "multi_ai_synthesis.v1"
SCORE_NOTE = "Score is review priority, not truth probability."
SUPPORTED_MODEL_STATUSES = {
    "ok",
    "failed",
    "skipped_not_configured",
    "timeout",
    "blocked_by_safety_preflight",
}
FAILED_MODEL_STATUSES = {"failed", "error", "timeout", "blocked_by_safety_preflight"}


@dataclass(frozen=True, slots=True)
class _ArtifactEnvelope:
    artifact: dict[str, Any]
    raw_output: str
    parsed_payload: dict[str, Any]
    output_parse: Any


def run_multi_ai(
    evidence_bundle: dict[str, Any],
    approved_profile: dict[str, Any] | None = None,
    *,
    providers: Iterable[str] | None = None,
    mode: str = "real_or_skip",
    output_dir: str | Path | None = None,
    store: Any | None = None,
    source_context: dict[str, Any] | None = None,
    source_analysis: dict[str, Any] | None = None,
    pipeline_run_id: str | None = None,
) -> dict[str, Any]:
    source_context = source_context or {}
    source_analysis = source_analysis or {}
    _validate_optional_source_context(source_context, source_analysis)
    bundle = _model_bundle(
        evidence_bundle,
        approved_profile or {},
        source_context=source_context,
        source_analysis=source_analysis,
    )
    provider_list = build_multi_ai_providers(providers, mode=mode)
    evidence_sha = str(evidence_bundle.get("evidence_sha256") or "")
    owns_pipeline_run = pipeline_run_id is None and store is not None
    if owns_pipeline_run:
        pipeline_run_id = start_pipeline_run(
            store,
            evidence_sha256=evidence_sha,
            operation="multi_ai",
            summary={"provider_count": len(provider_list), "mode": mode},
        )
    try:
        record_pipeline_event(
            store,
            pipeline_run_id=pipeline_run_id,
            evidence_sha256=evidence_sha,
            operation="multi_ai",
            step_key="bundle_persisted",
            status="completed",
            message="Evidence Bundle persisted for multi-provider analysis.",
        )
        record_pipeline_event(
            store,
            pipeline_run_id=pipeline_run_id,
            evidence_sha256=evidence_sha,
            operation="multi_ai",
            step_key="bundle_validated",
            status="completed",
            message="Evidence Bundle is valid for multi-provider analysis.",
        )
        record_pipeline_event(
            store,
            pipeline_run_id=pipeline_run_id,
            evidence_sha256=evidence_sha,
            operation="multi_ai",
            step_key="model_input_validated",
            status="running",
            message="Safety preflight will run before provider calls.",
        )
        model_runs = _run_model_artifacts(bundle, provider_list, store=store, pipeline_run_id=pipeline_run_id)
        record_pipeline_event(
            store,
            pipeline_run_id=pipeline_run_id,
            evidence_sha256=evidence_sha,
            operation="multi_ai",
            step_key="outputs_persisted",
            status="completed",
            message=f"{len(model_runs)} model output artifact(s) persisted.",
            metadata={
                "model_run_count": len(model_runs),
                "schema_valid_count": sum(1 for run in model_runs if bool(run.get("schema_valid"))),
                "ok_count": sum(1 for run in model_runs if str(run.get("status")) == "ok"),
            },
        )
        synthesis = synthesize_multi_ai(evidence_bundle, model_runs)
        synthesis["source_context_policy"] = _source_context_policy_summary(bundle)
        from ops_evidence_synthesis.synthesis.review_arbitration import resolve_canonical_review_graph_snapshot

        graph_resolution = resolve_canonical_review_graph_snapshot(
            store,
            evidence_bundle,
            model_runs=model_runs,
            multi_ai_synthesis=synthesis,
            approved_profile=approved_profile or {},
            source_context=source_context,
            source_analysis=source_analysis,
            persist_if_missing=store is not None,
            persist_if_stale=store is not None,
            created_by="multi-run",
        )
        canonical_review_graph = graph_resolution.get("canonical_review_graph") or {}
        synthesis["canonical_review_graph_summary"] = canonical_review_graph.get("summary") or {}
        review_targets = list(canonical_review_graph.get("review_targets") or [])
        record_pipeline_event(
            store,
            pipeline_run_id=pipeline_run_id,
            evidence_sha256=evidence_sha,
            operation="multi_ai",
            step_key="canonical_graph_resolved",
            status="completed",
            message="Canonical review graph resolved.",
            metadata={
                "canonical_graph_status": graph_resolution.get("canonical_graph_status") or "",
                "canonical_graph_sha256": graph_resolution.get("canonical_graph_sha256") or "",
            },
        )
        record_pipeline_event(
            store,
            pipeline_run_id=pipeline_run_id,
            evidence_sha256=evidence_sha,
            operation="multi_ai",
            step_key="review_targets_ready",
            status="completed",
            message=f"{len(review_targets)} review target(s) ready.",
            metadata={
                "review_target_count": len(review_targets),
                "validation_target_count": sum(
                    1
                    for target in review_targets
                    if str(target.get("target_class") or target.get("class") or "") == "validation_target"
                ),
            },
        )
        result = {
            "schema_version": "multi_ai_run.v1",
            "evidence_sha256": evidence_sha,
            "pipeline_run_id": pipeline_run_id or "",
            "provider_registry": provider_infos(),
            "context_inputs": _context_input_summary(bundle),
            "model_runs": model_runs,
            "multi_ai_synthesis": synthesis,
            "canonical_review_graph": canonical_review_graph,
            "canonical_graph_status": graph_resolution.get("canonical_graph_status"),
            "canonical_graph_sha256": graph_resolution.get("canonical_graph_sha256"),
            "input_fingerprint_sha256": graph_resolution.get("input_fingerprint_sha256"),
            "canonical_graph_snapshot": graph_resolution.get("snapshot") or {},
            "review_targets": review_targets,
        }
        if graph_resolution.get("persistence_warning"):
            result["persistence_warning"] = graph_resolution.get("persistence_warning")
        provider_success = sum(
            1 for run in model_runs if str(run.get("status") or "") == "ok" and run.get("schema_valid") is True
        )
        provider_skipped = sum(
            1
            for run in model_runs
            if str(run.get("status") or "") in {"skipped", "skipped_not_configured"}
        )
        provider_failed = max(0, len(model_runs) - provider_success - provider_skipped)
        if provider_success > 0:
            final_status = "succeeded"
            final_message = "Multi-provider analysis completed."
            final_reason = ""
        elif provider_failed > 0:
            final_status = "failed"
            final_message = "No provider produced a usable output."
            final_reason = "provider_failed"
        else:
            final_status = "blocked"
            final_message = "No provider was available to produce output."
            final_reason = "provider_not_configured"
        final_metadata = {
            "review_target_count": len(review_targets),
            "provider_total": len(model_runs),
            "provider_success": provider_success,
            "provider_failed": provider_failed,
            "provider_skipped": provider_skipped,
        }
        if final_reason:
            final_metadata["reason_code"] = final_reason
        finish_pipeline_run(
            store,
            pipeline_run_id=pipeline_run_id,
            evidence_sha256=evidence_sha,
            operation="multi_ai",
            status=final_status,
            message=final_message,
            metadata=final_metadata,
        )
        if output_dir is not None:
            write_multi_ai_outputs(result, output_dir)
        return result
    except Exception as exc:
        finish_pipeline_run(
            store,
            pipeline_run_id=pipeline_run_id,
            evidence_sha256=evidence_sha,
            operation="multi_ai",
            status="failed",
            message=str(exc),
        )
        raise


def write_multi_ai_outputs(result: dict[str, Any], output_dir: str | Path) -> None:
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    with (out / "model_runs.jsonl").open("w", encoding="utf-8") as handle:
        for run in result.get("model_runs") or []:
            handle.write(json.dumps(run, ensure_ascii=False, sort_keys=True) + "\n")
    (out / "multi_ai_synthesis.json").write_text(
        pretty_json(result.get("multi_ai_synthesis") or {}) + "\n",
        encoding="utf-8",
    )
    (out / "review_targets.json").write_text(
        pretty_json(result.get("review_targets") or []) + "\n",
        encoding="utf-8",
    )
    (out / "canonical_review_graph.json").write_text(
        pretty_json(result.get("canonical_review_graph") or {}) + "\n",
        encoding="utf-8",
    )
    (out / "canonical_review_graph_snapshot.json").write_text(
        pretty_json(result.get("canonical_graph_snapshot") or {}) + "\n",
        encoding="utf-8",
    )


def safety_preflight(model_input: dict[str, Any]) -> SafetyPreflightResult:
    return safety_preflight_for_model_input(model_input, filename="multi_ai_model_input.json")


def model_run_artifacts_from_records(runs: list[Any], parsed_results: list[Any]) -> list[dict[str, Any]]:
    parsed_by_run = {str(result.run_id): result for result in parsed_results}
    artifacts: list[dict[str, Any]] = []
    for run in runs:
        parsed = parsed_by_run.get(str(run.run_id))
        claims = []
        proposed = []
        missing: list[str] = []
        caveats: list[str] = []
        parsed_sha = ""
        schema_valid = False
        schema_errors: list[str] = []
        if parsed is not None:
            payload = parsed.parsed_json
            claims = [claim for claim in payload.get("claims") or [] if isinstance(claim, dict)]
            proposed = [row for row in payload.get("propositions") or [] if isinstance(row, dict)]
            missing = _unique_text(
                item for claim in claims for item in claim.get("missing_evidence") or [] if str(item).strip()
            )
            caveats = _unique_text(item for claim in claims for item in claim.get("caveats") or [] if str(item).strip())
            parsed_sha = parsed.parsed_json_sha256
            schema_valid = bool(parsed.schema_valid)
            schema_errors = [str(item) for item in parsed.schema_errors]
        artifacts.append(
            {
                "schema_version": MODEL_RUN_SCHEMA_VERSION,
                "run_id": run.run_id,
                "evidence_sha256": run.evidence_sha256,
                "provider_id": run.provider,
                "display_name": run.provider,
                "model_name": run.model_name,
                "status": run.status,
                "latency_ms": run.latency_ms,
                "input_tokens": run.input_tokens,
                "output_tokens": run.output_tokens,
                "raw_output_sha256": run.raw_output_sha256,
                "parsed_json_sha256": parsed_sha,
                "schema_valid": schema_valid,
                "schema_errors": schema_errors,
                "failure_reason": "" if run.status == "ok" and schema_valid else str(run.status),
                "parsed_result": {
                    "claims": claims,
                    "missing_evidence": missing,
                    "caveats": caveats,
                    "proposed_review_targets": proposed,
                },
                "safety_preflight": {
                    "passed": run.status != "blocked_by_safety_preflight",
                    "raw_logs_sent_to_providers": False,
                },
            }
        )
    return artifacts


def synthesize_multi_ai(evidence_bundle: dict[str, Any], model_runs: list[dict[str, Any]]) -> dict[str, Any]:
    successful = [run for run in model_runs if run.get("status") == "ok" and run.get("schema_valid") is True]
    failed = [
        run
        for run in model_runs
        if run.get("status") in FAILED_MODEL_STATUSES
        or (run.get("status") == "ok" and run.get("schema_valid") is not True)
    ]
    cost_summary = summarize_model_run_costs(model_runs)
    known_refs = _known_evidence_refs(evidence_bundle)
    claim_groups = _claim_groups(successful, known_refs)
    agreement_groups = [
        group
        for group in claim_groups
        if int(group["provider_count"]) >= 2 and int(group["support_claim_count"]) > 0 and not group["unsupported"]
    ]
    disagreement_groups = [
        group
        for group in claim_groups
        if not group["unsupported"]
        and (
            int(group["counter_claim_count"]) > 0
            or int(group["caveat_claim_count"]) > 0
            or int(group["validation_claim_count"]) > 0
            or bool(group["missing_evidence"])
            or (len(successful) > 1 and int(group["provider_count"]) == 1)
        )
    ]
    unsupported_groups = [group for group in claim_groups if group["unsupported"]]
    primary_candidates = [_candidate_from_group(group, "agreement_baseline_signal") for group in agreement_groups]
    validation_targets = [_candidate_from_group(group, "validation_target") for group in disagreement_groups]
    auto_archived = [
        {
            "group_id": group["group_id"],
            "reason": "unsupported_support_without_evidence_id",
            "providers": group["providers"],
            "claim_count": group["claim_count"],
        }
        for group in unsupported_groups
    ]
    missing_requests = _missing_evidence_requests(disagreement_groups)
    disagreement_themes = _disagreement_themes(disagreement_groups)
    finding_summary = finding_impact_from_synthesis(
        {
            "agreement_groups": agreement_groups,
            "disagreement_groups": disagreement_groups,
            "primary_candidates": primary_candidates,
            "validation_targets": validation_targets,
        }
    )
    return {
        "schema_version": MULTI_AI_SYNTHESIS_SCHEMA_VERSION,
        "evidence_sha256": str(evidence_bundle.get("evidence_sha256") or ""),
        "provider_count": len(model_runs),
        "successful_provider_count": len(successful),
        "failed_provider_count": len(failed),
        "skipped_provider_count": sum(1 for run in model_runs if run.get("status") == "skipped_not_configured"),
        "token_usage": {
            "input_tokens": cost_summary["input_tokens"],
            "output_tokens": cost_summary["output_tokens"],
        },
        "cost_estimate": {
            "estimated_cost_usd": cost_summary["estimated_cost_usd"],
            "priced_run_count": cost_summary["priced_run_count"],
            "pricing_source": cost_summary["pricing_source"],
        },
        "claim_groups": claim_groups,
        "agreement_groups": agreement_groups,
        "disagreement_groups": disagreement_groups,
        "disagreement_themes": disagreement_themes,
        "primary_candidates": primary_candidates,
        "validation_targets": validation_targets,
        "finding_summary": finding_summary,
        "monitor_only": [],
        "auto_archived": auto_archived,
        "missing_evidence_requests": missing_requests,
        "provider_statuses": [_provider_status(run) for run in model_runs],
        "safety": {
            "all_provider_inputs_passed": all(
                bool((run.get("safety_preflight") or {}).get("passed")) for run in model_runs
            ),
            "raw_logs_sent_to_providers": False,
            "policy": "Raw logs are never sent to providers. Only sanitized Evidence Bundles are used as model input.",
        },
        "score_note": SCORE_NOTE,
    }


def _unique_text(values: Any) -> list[str]:
    output: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if text and text not in output:
            output.append(text)
    return output


def _run_model_artifacts(
    bundle: dict[str, Any],
    providers: list[ModelProvider],
    *,
    store: Any | None = None,
    pipeline_run_id: str | None = None,
) -> list[dict[str, Any]]:
    if not providers:
        return []
    evidence_sha = str(bundle.get("evidence_sha256") or "")
    record_pipeline_event(
        store,
        pipeline_run_id=pipeline_run_id,
        evidence_sha256=evidence_sha,
        operation="multi_ai",
        step_key="providers_scheduled",
        status="running",
        message=f"{len(providers)} provider run(s) scheduled.",
        metadata={"provider_count": len(providers), "providers": [provider.provider for provider in providers]},
    )
    model_input = _model_input(bundle)
    preflight = safety_preflight(model_input)
    if not preflight.passed:
        record_pipeline_event(
            store,
            pipeline_run_id=pipeline_run_id,
            evidence_sha256=evidence_sha,
            operation="multi_ai",
            step_key="model_input_validated",
            status="blocked",
            message=preflight.failure_reason or "Safety preflight blocked provider input.",
            metadata={
                "reason_code": "blocked_by_safety_preflight",
                "finding_count": preflight.finding_count,
                "finding_types": list(preflight.finding_types),
            },
        )
        envelopes = [_blocked_artifact(bundle, provider, preflight) for provider in providers]
        _persist_artifact_envelopes(store, envelopes)
        _record_multi_ai_provider_events(store, pipeline_run_id, [envelope.artifact for envelope in envelopes])
        return [envelope.artifact for envelope in envelopes]
    record_pipeline_event(
        store,
        pipeline_run_id=pipeline_run_id,
        evidence_sha256=evidence_sha,
        operation="multi_ai",
        step_key="model_input_validated",
        status="completed",
        message="Safety preflight passed.",
    )
    if len(providers) == 1:
        envelopes = [_run_single_provider(bundle, providers[0], preflight)]
        _persist_artifact_envelopes(store, envelopes)
        _record_multi_ai_provider_events(store, pipeline_run_id, [envelope.artifact for envelope in envelopes])
        return [envelope.artifact for envelope in envelopes]

    by_index: dict[int, _ArtifactEnvelope] = {}
    with ThreadPoolExecutor(max_workers=min(len(providers), 8), thread_name_prefix="oes-multi-ai") as executor:
        futures = {
            executor.submit(_run_single_provider, bundle, provider, preflight): index
            for index, provider in enumerate(providers)
        }
        for future in as_completed(futures):
            by_index[futures[future]] = future.result()
    envelopes = [by_index[index] for index in range(len(providers))]
    _persist_artifact_envelopes(store, envelopes)
    _record_multi_ai_provider_events(store, pipeline_run_id, [envelope.artifact for envelope in envelopes])
    return [envelope.artifact for envelope in envelopes]


def _record_multi_ai_provider_events(store: Any | None, pipeline_run_id: str | None, artifacts: list[dict[str, Any]]) -> None:
    for artifact in artifacts:
        status = str(artifact.get("status") or "")
        schema_valid = bool(artifact.get("schema_valid"))
        if status == "ok" and schema_valid:
            event_status = "completed"
        elif status == "skipped_not_configured":
            event_status = "skipped"
        elif status == "blocked_by_safety_preflight":
            event_status = "blocked"
        else:
            event_status = "failed"
        record_pipeline_event(
            store,
            pipeline_run_id=pipeline_run_id,
            evidence_sha256=str(artifact.get("evidence_sha256") or ""),
            operation="multi_ai",
            step_key="providers_completed",
            status=event_status,
            message=f"{artifact.get('provider_id')} finished with status {status}.",
            metadata={
                "provider": artifact.get("provider_id") or "",
                "provider_id": artifact.get("provider_id") or "",
                "model_name": artifact.get("model_name") or "",
                "status": status,
                "artifact_id": artifact.get("run_id") or "",
                "run_id": artifact.get("run_id") or "",
                "raw_output_sha256": artifact.get("raw_output_sha256") or "",
                "repaired_output_sha256": artifact.get("repaired_output_sha256") or "",
                "parsed_json_sha256": artifact.get("parsed_json_sha256") or "",
                "reason_code": "blocked_by_safety_preflight"
                if status == "blocked_by_safety_preflight"
                else artifact.get("failure_reason") or "",
                "schema_valid": schema_valid,
                "parse_status": artifact.get("parse_status") or "",
                "latency_ms": artifact.get("latency_ms") or 0,
            },
        )


def _run_single_provider(
    bundle: dict[str, Any],
    provider: ModelProvider,
    preflight: SafetyPreflightResult,
) -> _ArtifactEnvelope:
    provider_result = run_provider_with_retries(provider, bundle)
    response = provider_result.response
    response_status = normalize_model_status(response.status)
    if response_status == "ok":
        status = "ok"
    elif response_status == "skipped_not_configured":
        status = "skipped_not_configured"
    elif response_status == "timeout":
        status = "timeout"
    else:
        status = "failed"
    latency_ms = response.latency_ms
    parsed_payload: dict[str, Any] = {}
    parsed_result = _empty_parsed_result()
    schema_valid = False
    schema_errors: tuple[str, ...] = ()
    output_parse = parse_model_output(response.raw_output)
    if status == "ok":
        if output_parse.parsed is None:
            parsed_payload = {
                "errors": list(output_parse.parse_errors),
                "raw_output_sha256": sha256_text(response.raw_output),
                "parse_status": output_parse.parse_status,
                "repair_applied": output_parse.repair_applied,
                "repair_rules": list(output_parse.repair_rules),
            }
            schema_errors = output_parse.parse_errors
        else:
            schema_valid, schema_errors = validate_claim_result(output_parse.parsed)
            parsed_payload = output_parse.parsed
            parsed_result = _parsed_result_payload(output_parse.parsed)
    else:
        parsed_payload = _empty_parsed_result()
    artifact = _artifact_from_response(
        bundle,
        response,
        status=status,
        latency_ms=latency_ms,
        parsed_payload=parsed_payload,
        parsed_result=parsed_result,
        schema_valid=schema_valid,
        schema_errors=schema_errors,
        preflight=preflight,
        failure_reason=_failure_reason(status, schema_valid, schema_errors),
        retry=provider_result.retry_metadata(),
        cost_estimate=cost_estimate_for_response(response),
        output_parse=output_parse,
    )
    return _ArtifactEnvelope(
        artifact=artifact,
        raw_output=response.raw_output,
        parsed_payload=parsed_payload,
        output_parse=output_parse,
    )


def _artifact_from_response(
    bundle: dict[str, Any],
    response: ModelResponse,
    *,
    status: str,
    latency_ms: int,
    parsed_payload: dict[str, Any],
    parsed_result: dict[str, Any],
    schema_valid: bool,
    schema_errors: tuple[str, ...],
    preflight: SafetyPreflightResult,
    failure_reason: str,
    retry: dict[str, Any] | None = None,
    cost_estimate: dict[str, Any] | None = None,
    output_parse: Any | None = None,
) -> dict[str, Any]:
    parsed_json_sha256 = sha256_json(parsed_payload) if parsed_payload else ""
    return {
        "schema_version": MODEL_RUN_SCHEMA_VERSION,
        "run_id": f"run-{uuid.uuid4().hex[:16]}",
        "evidence_sha256": str(bundle.get("evidence_sha256") or ""),
        "provider_id": response.provider,
        "display_name": _display_name(response.provider),
        "model_name": response.model_name,
        "status": status if status in SUPPORTED_MODEL_STATUSES else "failed",
        "latency_ms": latency_ms,
        "input_tokens": int(response.input_tokens or 0),
        "output_tokens": int(response.output_tokens or 0),
        "raw_output_sha256": sha256_text(response.raw_output),
        "parsed_json_sha256": parsed_json_sha256,
        "parse_status": str(getattr(output_parse, "parse_status", "not_run") or "not_run"),
        "repair_applied": bool(getattr(output_parse, "repair_applied", False)),
        "repair_rules": list(getattr(output_parse, "repair_rules", ()) or ()),
        "repaired_output_sha256": str(getattr(output_parse, "repaired_output_sha256", "") or ""),
        "schema_valid": bool(schema_valid),
        "schema_errors": list(schema_errors),
        "failure_reason": failure_reason,
        "retry": retry
        or {
            "attempts": 1,
            "max_attempts": 1,
            "retried": False,
            "retryable": False,
            "failure_reason": "",
            "exception_type": "",
        },
        "cost_estimate": cost_estimate or cost_estimate_for_response(response),
        "parsed_result": parsed_result,
        "safety_preflight": {
            "passed": preflight.passed,
            "finding_types": list(preflight.finding_types),
            "failure_reason": preflight.failure_reason,
            "finding_count": preflight.finding_count,
            "raw_logs_sent_to_providers": False,
        },
        "created_at": utc_now(),
    }


def _blocked_artifact(
    bundle: dict[str, Any],
    provider: ModelProvider,
    preflight: SafetyPreflightResult,
) -> _ArtifactEnvelope:
    response = blocked_provider_response(provider, preflight)
    output_parse = parse_model_output(response.raw_output)
    artifact = _artifact_from_response(
        bundle,
        response,
        status="blocked_by_safety_preflight",
        latency_ms=0,
        parsed_payload={},
        parsed_result=_empty_parsed_result(),
        schema_valid=False,
        schema_errors=(),
        preflight=preflight,
        failure_reason=preflight.failure_reason,
        retry={
            "attempts": 0,
            "max_attempts": 0,
            "retried": False,
            "retryable": False,
            "failure_reason": preflight.failure_reason,
            "exception_type": "",
        },
        cost_estimate=cost_estimate_for_response(response),
        output_parse=output_parse,
    )
    return _ArtifactEnvelope(
        artifact=artifact,
        raw_output=response.raw_output,
        parsed_payload={},
        output_parse=output_parse,
    )


def _persist_artifact_envelopes(store: Any | None, envelopes: list[_ArtifactEnvelope]) -> None:
    if store is None:
        return
    for envelope in envelopes:
        _persist_artifact(
            store,
            envelope.artifact,
            envelope.raw_output,
            envelope.parsed_payload,
            output_parse=envelope.output_parse,
        )


def _persist_artifact(
    store: Any | None,
    artifact: dict[str, Any],
    raw_output: str,
    parsed_payload: dict[str, Any],
    *,
    output_parse: Any | None = None,
) -> None:
    if store is None:
        return
    run = ModelRunRecord(
        run_id=str(artifact["run_id"]),
        evidence_sha256=str(artifact["evidence_sha256"]),
        prompt_sha256=sha256_text(f"{artifact['provider_id']}:{artifact['model_name']}:multi-ai"),
        model_input_sha256=str(artifact["evidence_sha256"]),
        provider=str(artifact["provider_id"]),
        model_name=str(artifact["model_name"]),
        temperature=0.0,
        raw_output=raw_output,
        raw_output_sha256=str(artifact["raw_output_sha256"]),
        latency_ms=int(artifact["latency_ms"]),
        input_tokens=int(artifact["input_tokens"]),
        output_tokens=int(artifact["output_tokens"]),
        status=str(artifact["status"]),
        created_at=str(artifact["created_at"]),
    )
    store.insert_model_run(run)
    parsed_result = ParsedResultRecord(
        result_id=f"result-{uuid.uuid4().hex[:16]}",
        run_id=run.run_id,
        evidence_sha256=run.evidence_sha256,
        provider=run.provider,
        parsed_json=parsed_payload or _empty_parsed_result(),
        parsed_json_sha256=str(artifact["parsed_json_sha256"] or sha256_json(parsed_payload or {})),
        schema_valid=bool(artifact["schema_valid"]),
        schema_errors=tuple(str(error) for error in artifact["schema_errors"]),
        created_at=utc_now(),
    )
    store.insert_parsed_result(parsed_result)
    if hasattr(store, "insert_model_output_artifact"):
        parse_result = output_parse or parse_model_output(raw_output)
        store.insert_model_output_artifact(
            model_output_artifact(
                run_id=run.run_id,
                evidence_sha256=run.evidence_sha256,
                provider=run.provider,
                model_name=run.model_name,
                raw_output_sha256=run.raw_output_sha256,
                parse_result=parse_result,
                parsed_json_sha256=parsed_result.parsed_json_sha256,
                schema_valid=bool(artifact["schema_valid"]),
                schema_errors=tuple(str(error) for error in artifact["schema_errors"]),
                status=run.status,
                created_at=run.created_at,
            )
        )


def _synthetic_response(provider: ModelProvider, status: str, reason: str) -> ModelResponse:
    return ModelResponse(
        provider=provider.provider,
        model_name=provider.model_name,
        prompt_name=provider.prompt_name,
        temperature=provider.temperature,
        raw_output=json.dumps(
            {"schema_version": "provider-error/v1", "status": status, "failure_reason": reason},
            ensure_ascii=False,
            sort_keys=True,
        ),
        latency_ms=1,
        input_tokens=0,
        output_tokens=0,
        status=status,
    )


def _validate_optional_source_context(source_context: dict[str, Any], source_analysis: dict[str, Any]) -> None:
    if source_context:
        validation = validate_source_context_bundle_for_upload(source_context)
        if not validation["passed"]:
            raise ValueError("source_context_bundle validation failed")
    if source_analysis:
        validation = validate_source_analysis_bundle_for_upload(source_analysis)
        if not validation["passed"]:
            raise ValueError("source_analysis_bundle validation failed")


def _context_input_summary(bundle: dict[str, Any]) -> dict[str, Any]:
    source_context = bundle.get("source_context_context") if isinstance(bundle.get("source_context_context"), dict) else {}
    source_analysis = bundle.get("source_analysis_context") if isinstance(bundle.get("source_analysis_context"), dict) else {}
    return {
        "source_context_included": bool(source_context),
        "source_analysis_included": bool(source_analysis),
        "source_context_sha256": source_context.get("source_context_sha256") or "",
        "source_analysis_sha256": source_analysis.get("analysis_sha256") or "",
        "context_is_not_incident_evidence": True,
        "support_claims_must_cite_evidence_id": True,
    }


def _source_context_policy_summary(bundle: dict[str, Any]) -> dict[str, Any]:
    policy = bundle.get("model_input_policy") if isinstance(bundle.get("model_input_policy"), dict) else {}
    source_context = bundle.get("source_context_context") if isinstance(bundle.get("source_context_context"), dict) else {}
    version = source_context.get("version_context") if isinstance(source_context.get("version_context"), dict) else {}
    return {
        "source_context_is_incident_evidence": False,
        "source_analysis_is_incident_evidence": False,
        "support_claims_must_cite_evidence_id": True,
        "raw_source_sent_to_providers": bool(policy.get("raw_source_sent_to_providers")) is True,
        "raw_env_values_sent_to_providers": bool(policy.get("raw_env_values_sent_to_providers")) is True,
        "deployed_version_confirmed": bool(version.get("deployed_version_confirmed")) is True,
        "version_caveat": version.get("caveat") or "",
    }


def _model_bundle(
    evidence_bundle: dict[str, Any],
    approved_profile: dict[str, Any],
    *,
    source_context: dict[str, Any] | None = None,
    source_analysis: dict[str, Any] | None = None,
) -> dict[str, Any]:
    bundle = json.loads(json.dumps(evidence_bundle, ensure_ascii=False))
    source = bundle.get("source") if isinstance(bundle.get("source"), dict) else {}
    window = bundle.get("time_window") if isinstance(bundle.get("time_window"), dict) else {}
    bundle.setdefault("service", source.get("service") or "unknown-service")
    bundle.setdefault("environment", source.get("environment") or "unknown-environment")
    bundle.setdefault("window_start", window.get("start") or bundle.get("incident_start") or "")
    bundle.setdefault("window_end", window.get("end") or bundle.get("incident_end") or "")
    evidence_items = [item for item in bundle.get("evidence_items") or [] if isinstance(item, dict)]
    if not isinstance(bundle.get("evidence_refs"), dict) or not bundle.get("evidence_refs"):
        refs: dict[str, Any] = {}
        for index, item in enumerate(evidence_items, start=1):
            evidence_id = str(item.get("evidence_id") or item.get("id") or f"EVIDENCE-{index:03d}")
            refs[evidence_id] = item
        bundle["evidence_refs"] = refs
    if "logs" not in bundle:
        bundle["logs"] = evidence_items[:20]
    if "log_patterns" not in bundle:
        bundle["log_patterns"] = _patterns_from_evidence_items(evidence_items)
    bundle["approved_profile_context"] = _approved_profile_context(approved_profile)
    bundle["source_context_context"] = source_context_model_context(source_context or {})
    bundle["source_analysis_context"] = source_analysis_model_context(source_analysis or {})
    bundle["model_input_policy"] = _model_input_policy(bundle)
    return bundle


def _model_input(bundle: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": "multi_ai_model_input.v1",
        "evidence_sha256": bundle.get("evidence_sha256"),
        "service": bundle.get("service"),
        "environment": bundle.get("environment"),
        "window_start": bundle.get("window_start"),
        "window_end": bundle.get("window_end"),
        "evidence_refs": bundle.get("evidence_refs") or {},
        "logs": bundle.get("logs") or [],
        "log_patterns": bundle.get("log_patterns") or [],
        "metric_windows": bundle.get("metric_windows") or [],
        "operational_evidence": bundle.get("operational_evidence") or [],
        "signals": bundle.get("signals") or [],
        "approved_profile_context": bundle.get("approved_profile_context") or {},
        "source_context_context": bundle.get("source_context_context") or {},
        "source_analysis_context": bundle.get("source_analysis_context") or {},
        "model_input_policy": bundle.get("model_input_policy") or _model_input_policy(bundle),
    }


def _model_input_policy(bundle: dict[str, Any]) -> dict[str, Any]:
    has_source_context = bool(bundle.get("source_context_context"))
    has_source_analysis = bool(bundle.get("source_analysis_context"))
    return {
        "raw_logs_sent_to_providers": False,
        "raw_logs_uploaded": bool((bundle.get("local_first_summary") or {}).get("raw_logs_uploaded")) is True,
        "input_artifact": "sanitized_evidence_bundle",
        "source_context_included": has_source_context,
        "source_analysis_included": has_source_analysis,
        "source_context_is_incident_evidence": False,
        "source_analysis_is_incident_evidence": False,
        "raw_source_sent_to_providers": False,
        "raw_env_values_sent_to_providers": False,
        "raw_grep_output_sent_to_providers": False,
        "support_claims_must_cite_evidence_id": True,
        "score_note": SCORE_NOTE,
        "policy_text": (
            "Raw logs are never sent to providers. Source Context and Source Analysis are context, "
            "not incident evidence. Support claims about runtime behavior must cite Evidence Items with evidence_id."
        ),
    }


def _approved_profile_context(profile: dict[str, Any]) -> dict[str, Any]:
    if not profile:
        return {"explicit_profile": False, "context_is_not_evidence": True}
    return {
        "profile_id": str(profile.get("profile_id") or ""),
        "explicit_profile": bool(
            profile.get("explicit_profile")
            or ((profile.get("review_policy") or {}).get("profile_draft_approved") is True)
        ),
        "context_is_not_evidence": True,
        "require_evidence_id_for_support": True,
        "system_profile": profile.get("system_profile") if isinstance(profile.get("system_profile"), dict) else {},
        "metric_semantics": profile.get("metric_semantics") if isinstance(profile.get("metric_semantics"), dict) else {},
        "action_constraints": list(profile.get("action_constraints") or []),
    }


def _patterns_from_evidence_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    patterns: list[dict[str, Any]] = []
    for index, item in enumerate(items[:20], start=1):
        evidence_id = str(item.get("evidence_id") or item.get("id") or f"EVIDENCE-{index:03d}")
        text = str(
            item.get("message_template")
            or item.get("message_sanitized")
            or item.get("summary")
            or item.get("example_sanitized")
            or item
        )
        patterns.append(
            {
                "pattern_id": f"PATTERN-{index:03d}",
                "message_template": text[:500],
                "count": int(item.get("count") or item.get("occurrence_count") or 1),
                "first_seen": str(item.get("timestamp") or item.get("first_seen") or ""),
                "last_seen": str(item.get("timestamp") or item.get("last_seen") or ""),
                "max_severity": str(item.get("severity") or "INFO"),
                "evidence_refs": [evidence_id],
            }
        )
    return patterns


def _parsed_result_payload(parsed: dict[str, Any]) -> dict[str, Any]:
    claims = [claim for claim in parsed.get("claims") or [] if isinstance(claim, dict)]
    return {
        "claims": claims,
        "missing_evidence": _unique(
            item for claim in claims for item in claim.get("missing_evidence") or [] if str(item).strip()
        ),
        "caveats": _unique(item for claim in claims for item in claim.get("caveats") or [] if str(item).strip()),
        "proposed_review_targets": [row for row in parsed.get("propositions") or [] if isinstance(row, dict)],
    }


def _empty_parsed_result() -> dict[str, Any]:
    return {"claims": [], "missing_evidence": [], "caveats": [], "proposed_review_targets": []}


def _claim_groups(model_runs: list[dict[str, Any]], known_refs: set[str]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for run in model_runs:
        provider_id = str(run.get("provider_id") or "")
        for claim in ((run.get("parsed_result") or {}).get("claims") or []):
            if not isinstance(claim, dict):
                continue
            refs = _string_list(claim.get("evidence_refs"))
            core_target_type = _core_target_type(claim)
            subsystem = _subsystem(claim)
            component = str(claim.get("component") or claim.get("source_name") or "")
            key = sha256_json(
                {
                    "core_target_type": core_target_type,
                    "subsystem": subsystem,
                    "component": component,
                    "evidence_refs": sorted(refs),
                }
            )[:16]
            grouped.setdefault(key, []).append({"provider_id": provider_id, "claim": claim})
    groups: list[dict[str, Any]] = []
    for key, rows in sorted(grouped.items()):
        claims = [row["claim"] for row in rows]
        providers = _unique(row["provider_id"] for row in rows)
        evidence_refs = _unique(ref for claim in claims for ref in _string_list(claim.get("evidence_refs")))
        unsupported = any(
            str(claim.get("claim_type") or "support") == "support"
            and (not _string_list(claim.get("evidence_refs")) or not set(_string_list(claim.get("evidence_refs"))).issubset(known_refs))
            for claim in claims
        )
        claim_types = [str(claim.get("claim_type") or "support") for claim in claims]
        group = {
            "group_id": f"cg-{key}",
            "core_target_type": _core_target_type(claims[0]),
            "subsystem": _subsystem(claims[0]),
            "component": str(claims[0].get("component") or claims[0].get("source_name") or ""),
            "evidence_refs": evidence_refs,
            "providers": providers,
            "provider_count": len(providers),
            "claim_count": len(claims),
            "support_claim_count": sum(1 for item in claim_types if item == "support"),
            "counter_claim_count": sum(1 for item in claim_types if item == "counter_evidence"),
            "caveat_claim_count": sum(1 for item in claim_types if item == "caveat"),
            "validation_claim_count": sum(1 for item in claim_types if item in {"validation_target", "next_data_needed"}),
            "missing_evidence": _unique(
                item for claim in claims for item in claim.get("missing_evidence") or [] if str(item).strip()
            ),
            "claims": [
                {
                    "provider_id": row["provider_id"],
                    "claim_type": str(row["claim"].get("claim_type") or "support"),
                    "claim_text": str(row["claim"].get("claim_text") or ""),
                    "evidence_refs": _string_list(row["claim"].get("evidence_refs")),
                    "missing_evidence": _string_list(row["claim"].get("missing_evidence")),
                }
                for row in rows
            ],
            "unsupported": unsupported,
            "unsupported_reason": "support_claim_without_valid_evidence_id" if unsupported else "",
        }
        groups.append(group)
    return groups


def _candidate_from_group(group: dict[str, Any], review_mode: str) -> dict[str, Any]:
    title = _target_title(group)
    impact_summary = _target_impact_summary(group, review_mode)
    return {
        "review_mode": review_mode,
        "group_id": group["group_id"],
        "core_target_type": group["core_target_type"],
        "subsystem": group["subsystem"],
        "providers": group["providers"],
        "provider_count": group["provider_count"],
        "evidence_refs": group["evidence_refs"],
        "missing_evidence": group["missing_evidence"],
        "title": title,
        "impact_summary": impact_summary,
        "score_note": SCORE_NOTE,
    }


def finding_impact_from_synthesis(synthesis: dict[str, Any]) -> dict[str, str]:
    primary = [row for row in synthesis.get("primary_candidates") or [] if isinstance(row, dict)]
    agreement = [row for row in synthesis.get("agreement_groups") or [] if isinstance(row, dict)]
    disagreement = [row for row in synthesis.get("disagreement_groups") or [] if isinstance(row, dict)]
    if primary:
        top = primary[0]
        return {
            "finding": str(top.get("title") or _target_title(top)),
            "impact": str(top.get("impact_summary") or _target_impact_summary(top, "agreement_baseline_signal")),
        }
    if agreement:
        return {
            "finding": f"{len(agreement)} technical review signals detected",
            "impact": "Agreement is used as a review signal, not as truth.",
        }
    if disagreement:
        return {
            "finding": "Multi-AI disagreement requires validation",
            "impact": (
                f"No incident-promotion agreement was found. {len(disagreement)} disagreement groups were routed "
                "to validation targets, and no primary incident candidate was promoted."
            ),
        }
    return {
        "finding": "Evidence requires profile or additional context",
        "impact": "No sufficiently supported review target was promoted.",
    }


def _target_title(group: dict[str, Any]) -> str:
    core = str(group.get("core_target_type") or "general_review")
    component = str(group.get("component") or group.get("subsystem") or "target")
    labels = {
        "job_configuration_mismatch": "Configuration mismatch requires review",
        "restart_loop": "Restart loop requires validation",
        "throughput_disappearance": "Throughput disappearance requires validation",
        "external_dependency_failure": "External dependency status requires validation",
        "deployment_correlation": "Deployment correlation requires validation",
        "freshness_signal_gap": "Freshness drift requires validation",
        "user_impact_signal_gap": "User impact signal requires validation",
        "instrumentation_mismatch": "Instrumentation mismatch requires validation",
        "general_review": "Review target requires validation",
    }
    label = labels.get(core, core.replace("_", " ").title())
    return f"{label}: {component}" if component and component != "target" else label


def _target_impact_summary(group: dict[str, Any], review_mode: str) -> str:
    refs = len(group.get("evidence_refs") or [])
    provider_count = int(group.get("provider_count") or 0)
    if review_mode == "agreement_baseline_signal":
        return f"{provider_count} providers aligned on a review signal with {refs} cited Evidence Items; this is not majority-vote truth."
    missing = len(group.get("missing_evidence") or [])
    return f"Provider claims diverged and require validation; {missing} missing-evidence prompts remain."


def _disagreement_themes(groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    buckets: dict[str, dict[str, Any]] = {}
    for group in groups:
        theme, request = _theme_for_disagreement(group)
        row = buckets.setdefault(
            theme,
            {
                "theme": theme,
                "group_count": 0,
                "recommended_validation": request,
                "group_ids": [],
            },
        )
        row["group_count"] += 1
        row["group_ids"].append(str(group.get("group_id") or ""))
    return sorted(buckets.values(), key=lambda row: (-int(row["group_count"]), str(row["theme"])))


def _theme_for_disagreement(group: dict[str, Any]) -> tuple[str, str]:
    text = canonical_json(
        {
            "core_target_type": group.get("core_target_type"),
            "subsystem": group.get("subsystem"),
            "component": group.get("component"),
            "missing_evidence": group.get("missing_evidence"),
            "claims": group.get("claims"),
        }
    ).casefold()
    if any(token in text for token in ("external", "dependency", "http_5xx", "timeout")):
        return "External dependency vs local instrumentation gap", "external_dependency_status_query"
    if any(token in text for token in ("user impact", "delivery", "ingest", "watch", "audio")):
        return "User impact signal is unclear", "user_impact_signal_query"
    if any(token in text for token in ("freshness", "stale", "timestamp", "drift")):
        return "Freshness drift requires timestamp validation", "freshness_signal_query"
    if any(token in text for token in ("metric", "count", "aggregation", "mismatch")):
        return "Metric/log instrumentation mismatch", "instrumentation_consistency_query"
    if any(token in text for token in ("deployment", "config", "version")):
        return "Deployment or configuration correlation unclear", "deployment_correlation_query"
    return "General disagreement requires validation", "instrumentation_consistency_query"


def _missing_evidence_requests(groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    requests: dict[str, dict[str, Any]] = {}
    for group in groups:
        for item in group.get("missing_evidence") or []:
            text = str(item).strip()
            if not text:
                continue
            key = sha256_text(f"{group['group_id']}:{text}")[:12]
            requests[key] = {
                "request_id": f"MER-{key}",
                "group_id": group["group_id"],
                "question": text,
                "providers": group.get("providers") or [],
                "reason": "missing_evidence_from_multi_ai_synthesis",
            }
    return list(requests.values())


def _review_targets_from_synthesis(synthesis: dict[str, Any]) -> list[dict[str, Any]]:
    targets: list[dict[str, Any]] = []
    for row in synthesis.get("primary_candidates") or []:
        targets.append({"target_type": "primary_candidate", **row})
    for row in synthesis.get("validation_targets") or []:
        targets.append({"target_type": "validation_target", **row})
    return targets


def _known_evidence_refs(bundle: dict[str, Any]) -> set[str]:
    refs = bundle.get("evidence_refs") if isinstance(bundle.get("evidence_refs"), dict) else {}
    known = set(str(key) for key in refs)
    for item in bundle.get("evidence_items") or []:
        if isinstance(item, dict):
            known.add(str(item.get("evidence_id") or item.get("id") or ""))
    return {item for item in known if item}


def _core_target_type(claim: dict[str, Any]) -> str:
    raw = str(
        claim.get("core_target_type")
        or (claim.get("evidence_identity") or {}).get("core_target_type")
        or ""
    )
    if raw:
        return raw
    text = str(claim.get("claim_text") or "").casefold()
    if "configured job" in text or "supervisor" in text or "no such file" in text:
        return "job_configuration_mismatch"
    if "restart" in text or "crashloop" in text:
        return "restart_loop"
    if "rtmps" in text or "ffmpeg" in text or "send-path" in text:
        return "throughput_disappearance"
    if "dependency" in text or "downstream" in text:
        return "external_dependency_failure"
    if "deployment" in text or "deploy" in text:
        return "deployment_correlation"
    return "general_review"


def _subsystem(claim: dict[str, Any]) -> str:
    value = str(claim.get("subsystem") or "").strip()
    if value:
        return value
    text = str(claim.get("claim_text") or "").casefold()
    if "rtmps" in text or "ffmpeg" in text:
        return "rtmps_ffmpeg"
    if "youtube" in text:
        return "youtube_live"
    if "restart" in text or "supervisor" in text:
        return "runtime_recovery"
    return "general"


def _provider_status(run: dict[str, Any]) -> dict[str, Any]:
    return {
        "provider_id": run.get("provider_id"),
        "display_name": run.get("display_name"),
        "model_name": run.get("model_name"),
        "status": run.get("status"),
        "latency_ms": run.get("latency_ms"),
        "input_tokens": run.get("input_tokens"),
        "output_tokens": run.get("output_tokens"),
        "raw_output_sha256": run.get("raw_output_sha256"),
        "parsed_json_sha256": run.get("parsed_json_sha256"),
        "schema_valid": run.get("schema_valid"),
        "schema_errors": run.get("schema_errors") or [],
        "failure_reason": run.get("failure_reason") or "",
        "retry": run.get("retry") or {},
        "cost_estimate": run.get("cost_estimate") or {},
    }


def _failure_reason(status: str, schema_valid: bool, schema_errors: tuple[str, ...]) -> str:
    if status == "ok" and schema_valid:
        return ""
    if status == "ok" and schema_errors:
        return "schema_validation_failed"
    if status == "skipped_not_configured":
        return "provider_not_configured"
    if status == "timeout":
        return "provider_timeout"
    if status == "blocked_by_safety_preflight":
        return "secret_like_pattern_detected"
    return "provider_failed"


def _display_name(provider_id: str) -> str:
    for info in provider_infos():
        if info["provider_id"] == provider_id:
            return str(info["display_name"])
    return provider_id


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if str(item).strip()]


def _unique(values: Iterable[Any]) -> list[str]:
    output: list[str] = []
    for value in values:
        text = str(value).strip()
        if text and text not in output:
            output.append(text)
    return output
