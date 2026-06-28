from __future__ import annotations

import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from ops_evidence_synthesis.ai import default_local_providers
from ops_evidence_synthesis.ai.base import ModelProvider, ModelResponse
from ops_evidence_synthesis.ai.runtime import (
    blocked_provider_response,
    compact_model_input_sha256,
    run_provider_with_retries,
    safety_preflight_for_bundle,
)
from ops_evidence_synthesis.bundle import EvidenceBundleBuilder
from ops_evidence_synthesis.canonical import sha256_json, sha256_text
from ops_evidence_synthesis.ingest import ingest_jsonl
from ops_evidence_synthesis.models import IncidentWindow, ModelRunRecord, ParsedResultRecord, ScoreRecord
from ops_evidence_synthesis.pipeline_progress import (
    finish_pipeline_run,
    record_pipeline_event,
    start_pipeline_run,
)
from ops_evidence_synthesis.storage.sqlite_store import SQLiteStore
from ops_evidence_synthesis.synthesis.clustering import persist_proposition_clusters
from ops_evidence_synthesis.synthesis.multi_ai import model_run_artifacts_from_records, synthesize_multi_ai
from ops_evidence_synthesis.synthesis.output_ingest import model_output_artifact, parse_model_output
from ops_evidence_synthesis.synthesis.review_arbitration import resolve_canonical_review_graph_snapshot
from ops_evidence_synthesis.synthesis.router import RoutingResult, route_claims
from ops_evidence_synthesis.synthesis.scoring import score_propositions
from ops_evidence_synthesis.synthesis.validation import validate_claim_result
from ops_evidence_synthesis.timeutils import utc_now


@dataclass(frozen=True, slots=True)
class PipelineResult:
    evidence_sha256: str
    model_run_count: int
    parsed_result_count: int
    claim_count: int
    proposition_count: int
    score_count: int
    cluster_count: int
    review_queue_count: int
    canonical_graph_status: str = ""
    canonical_graph_sha256: str = ""
    input_fingerprint_sha256: str = ""
    primary_review_target_count: int = 0
    validation_target_count: int = 0
    monitor_only_count: int = 0
    auto_archived_count: int = 0


def run_pipeline(
    store: SQLiteStore,
    incident: IncidentWindow,
    providers: Iterable[ModelProvider] | None = None,
    *,
    approved_profile: dict | None = None,
    source_context: dict | None = None,
    source_analysis: dict | None = None,
) -> PipelineResult:
    store.init_schema()
    bundle = EvidenceBundleBuilder(store).build(incident)
    pipeline_run_id = start_pipeline_run(
        store,
        evidence_sha256=str(bundle["evidence_sha256"]),
        operation="synthesis",
        summary={"service": bundle.get("service"), "environment": bundle.get("environment")},
    )
    record_pipeline_event(
        store,
        pipeline_run_id=pipeline_run_id,
        evidence_sha256=str(bundle["evidence_sha256"]),
        operation="synthesis",
        step_key="bundle_persisted",
        status="completed",
        message="Evidence Bundle built and persisted.",
    )
    return run_synthesis_for_bundle(
        store,
        bundle,
        providers,
        pipeline_run_id=pipeline_run_id,
        approved_profile=approved_profile,
        source_context=source_context,
        source_analysis=source_analysis,
    )


def run_synthesis_for_bundle(
    store: SQLiteStore,
    bundle: dict,
    providers: Iterable[ModelProvider] | None = None,
    *,
    pipeline_run_id: str | None = None,
    parent_pipeline_run_id: str | None = None,
    approved_profile: dict | None = None,
    source_context: dict | None = None,
    source_analysis: dict | None = None,
) -> PipelineResult:
    owns_pipeline_run = pipeline_run_id is None
    if owns_pipeline_run:
        pipeline_run_id = start_pipeline_run(
            store,
            evidence_sha256=str(bundle["evidence_sha256"]),
            operation="synthesis",
            summary={"service": bundle.get("service"), "environment": bundle.get("environment")},
            parent_pipeline_run_id=parent_pipeline_run_id,
        )
        record_pipeline_event(
            store,
            pipeline_run_id=pipeline_run_id,
            evidence_sha256=str(bundle["evidence_sha256"]),
            operation="synthesis",
            step_key="bundle_persisted",
            status="completed",
            message="Evidence Bundle persisted.",
        )
    provider_list = list(providers) if providers is not None else default_local_providers()
    try:
        parsed_results = run_model_stage(store, bundle, provider_list, pipeline_run_id=pipeline_run_id, operation="synthesis")
        routing = run_route_stage(store, bundle, parsed_results, pipeline_run_id=pipeline_run_id, operation="synthesis")
        scores = run_score_stage(store, routing, parsed_results)
        record_pipeline_event(
            store,
            pipeline_run_id=pipeline_run_id,
            evidence_sha256=str(bundle["evidence_sha256"]),
            operation="synthesis",
            step_key="scores_written",
            status="completed",
            message=f"{len(scores)} scores written.",
            metadata={"score_count": len(scores)},
        )
        clusters = persist_proposition_clusters(store, bundle["evidence_sha256"])
        record_pipeline_event(
            store,
            pipeline_run_id=pipeline_run_id,
            evidence_sha256=str(bundle["evidence_sha256"]),
            operation="synthesis",
            step_key="clusters_built",
            status="completed",
            message=f"{len(clusters)} review clusters built.",
            metadata={"cluster_count": len(clusters)},
        )

        review_queue = store.list_review_queue(
            limit=max(1000, len(routing.propositions)),
            evidence_sha256=bundle["evidence_sha256"],
        )
        target_set: dict = {"summary": {}, "targets": []}
        if hasattr(store, "list_review_targets"):
            target_set = store.list_review_targets(limit=5, evidence_sha256=bundle["evidence_sha256"], persist=True)
            summary = dict(target_set.get("summary") or {})
            record_pipeline_event(
                store,
                pipeline_run_id=pipeline_run_id,
                evidence_sha256=str(bundle["evidence_sha256"]),
                operation="synthesis",
                step_key="review_targets_persisted",
                status="completed",
                message="Review targets persisted.",
                metadata={
                    "review_target_count": int(summary.get("review_targets") or 0),
                    "primary_review_target_count": int(summary.get("primary_review_targets") or 0),
                    "validation_target_count": int(summary.get("validation_targets") or 0),
                },
            )
        graph_resolution = run_canonical_graph_stage(
            store,
            bundle,
            parsed_results,
            legacy_review_targets=list(target_set.get("targets") or []),
            legacy_summary=dict(target_set.get("summary") or {}),
            pipeline_run_id=pipeline_run_id,
            operation="synthesis",
            approved_profile=approved_profile,
            source_context=source_context,
            source_analysis=source_analysis,
        )
        graph = graph_resolution.get("canonical_review_graph") if isinstance(graph_resolution, dict) else {}
        graph_summary = graph.get("summary") if isinstance(graph, dict) and isinstance(graph.get("summary"), dict) else {}

        result = PipelineResult(
            evidence_sha256=bundle["evidence_sha256"],
            model_run_count=len(provider_list),
            parsed_result_count=len(parsed_results),
            claim_count=len(routing.claims),
            proposition_count=len(routing.propositions),
            score_count=len(scores),
            cluster_count=len(clusters),
            review_queue_count=len(review_queue),
            canonical_graph_status=str(graph_resolution.get("canonical_graph_status") or ""),
            canonical_graph_sha256=str(graph_resolution.get("canonical_graph_sha256") or ""),
            input_fingerprint_sha256=str(graph_resolution.get("input_fingerprint_sha256") or ""),
            primary_review_target_count=int(graph_summary.get("primary_count") or 0),
            validation_target_count=int(graph_summary.get("validation_count") or 0),
            monitor_only_count=int(graph_summary.get("monitor_only_count") or 0),
            auto_archived_count=int(graph_summary.get("auto_archived_count") or 0),
        )
        finish_pipeline_run(
            store,
            pipeline_run_id=pipeline_run_id,
            evidence_sha256=str(bundle["evidence_sha256"]),
            operation="synthesis",
            status="succeeded",
            message="Synthesis pipeline completed.",
            metadata={"review_queue_count": len(review_queue)},
        )
        return result
    except Exception as exc:
        finish_pipeline_run(
            store,
            pipeline_run_id=pipeline_run_id,
            evidence_sha256=str(bundle.get("evidence_sha256") or ""),
            operation="synthesis",
            status="failed",
            message=str(exc),
        )
        raise


def run_canonical_graph_stage(
    store: SQLiteStore,
    bundle: dict,
    parsed_results: list[ParsedResultRecord] | None = None,
    *,
    legacy_review_targets: list[dict] | None = None,
    legacy_summary: dict | None = None,
    pipeline_run_id: str | None = None,
    operation: str = "synthesis",
    approved_profile: dict | None = None,
    source_context: dict | None = None,
    source_analysis: dict | None = None,
) -> dict:
    evidence_sha = str(bundle.get("evidence_sha256") or "")
    runs = store.fetch_model_runs(evidence_sha) if hasattr(store, "fetch_model_runs") else []
    parsed = parsed_results if parsed_results is not None else (
        store.fetch_parsed_results(evidence_sha) if hasattr(store, "fetch_parsed_results") else []
    )
    artifacts = model_run_artifacts_from_records(runs, parsed)
    synthesis = synthesize_multi_ai(bundle, artifacts)
    resolution = resolve_canonical_review_graph_snapshot(
        store,
        bundle,
        model_runs=artifacts,
        multi_ai_synthesis=synthesis,
        approved_profile=approved_profile or {},
        source_context=source_context or {},
        source_analysis=source_analysis or {},
        legacy_review_targets=legacy_review_targets or [],
        legacy_summary=legacy_summary or {},
        persist_if_missing=True,
        persist_if_stale=True,
        created_by=operation,
    )
    graph = resolution.get("canonical_review_graph") if isinstance(resolution, dict) else {}
    summary = graph.get("summary") if isinstance(graph, dict) and isinstance(graph.get("summary"), dict) else {}
    record_pipeline_event(
        store,
        pipeline_run_id=pipeline_run_id,
        evidence_sha256=evidence_sha,
        operation=operation,
        step_key="canonical_graph_resolved",
        status="completed",
        message="Canonical review graph resolved and persisted.",
        metadata={
            "canonical_graph_status": resolution.get("canonical_graph_status") or "",
            "canonical_graph_sha256": resolution.get("canonical_graph_sha256") or "",
            "input_fingerprint_sha256": resolution.get("input_fingerprint_sha256") or "",
            "primary_count": int(summary.get("primary_count") or 0),
            "validation_count": int(summary.get("validation_count") or 0),
            "monitor_only_count": int(summary.get("monitor_only_count") or 0),
            "auto_archived_count": int(summary.get("auto_archived_count") or 0),
        },
    )
    return resolution


def run_model_stage(
    store: SQLiteStore,
    bundle: dict,
    providers: Iterable[ModelProvider] | None = None,
    *,
    pipeline_run_id: str | None = None,
    operation: str = "model_stage",
) -> list[ParsedResultRecord]:
    provider_list = list(providers) if providers is not None else default_local_providers()
    owns_pipeline_run = pipeline_run_id is None
    if owns_pipeline_run:
        pipeline_run_id = start_pipeline_run(
            store,
            evidence_sha256=str(bundle["evidence_sha256"]),
            operation=operation,
            summary={"provider_count": len(provider_list)},
        )
    record_pipeline_event(
        store,
        pipeline_run_id=pipeline_run_id,
        evidence_sha256=str(bundle["evidence_sha256"]),
        operation=operation,
        step_key="providers_scheduled",
        status="running",
        message=f"{len(provider_list)} provider run(s) scheduled.",
        metadata={"provider_count": len(provider_list), "providers": [provider.provider for provider in provider_list]},
    )
    try:
        responses = _run_providers_parallel(bundle, provider_list)
        model_input_sha256 = compact_model_input_sha256(bundle)
        parsed_results: list[ParsedResultRecord] = []
        for response in responses:
            model_run = ModelRunRecord(
                run_id=f"run-{uuid.uuid4().hex[:16]}",
                evidence_sha256=bundle["evidence_sha256"],
                prompt_sha256=sha256_text(f"{response.provider}:{response.model_name}:{response.prompt_name}"),
                model_input_sha256=model_input_sha256,
                provider=response.provider,
                model_name=response.model_name,
                temperature=response.temperature,
                raw_output=response.raw_output,
                raw_output_sha256=sha256_text(response.raw_output),
                latency_ms=response.latency_ms,
                input_tokens=response.input_tokens,
                output_tokens=response.output_tokens,
                status=response.status,
                created_at=utc_now(),
            )
            store.insert_model_run(model_run)

            output_parse = parse_model_output(response.raw_output)
            if output_parse.parsed is None:
                parsed_payload = {
                    "errors": list(output_parse.parse_errors),
                    "raw_output_sha256": model_run.raw_output_sha256,
                    "parse_status": output_parse.parse_status,
                    "repair_applied": output_parse.repair_applied,
                    "repair_rules": list(output_parse.repair_rules),
                }
                schema_valid = False
                schema_errors = output_parse.parse_errors
            else:
                schema_valid, validation_errors = validate_claim_result(output_parse.parsed)
                parsed_payload = output_parse.parsed
                schema_errors = validation_errors

            parsed_result = ParsedResultRecord(
                result_id=f"result-{uuid.uuid4().hex[:16]}",
                run_id=model_run.run_id,
                evidence_sha256=bundle["evidence_sha256"],
                provider=response.provider,
                parsed_json=parsed_payload,
                parsed_json_sha256=sha256_json(parsed_payload),
                schema_valid=schema_valid,
                schema_errors=schema_errors,
                created_at=utc_now(),
            )
            store.insert_parsed_result(parsed_result)
            _persist_model_output_artifact(
                store,
                model_run=model_run,
                output_parse=output_parse,
                parsed_json_sha256=parsed_result.parsed_json_sha256,
                schema_valid=schema_valid,
                schema_errors=schema_errors,
            )
            provider_status = "completed" if response.status == "ok" else "skipped" if response.status == "skipped_not_configured" else response.status
            record_pipeline_event(
                store,
                pipeline_run_id=pipeline_run_id,
                evidence_sha256=str(bundle["evidence_sha256"]),
                operation=operation,
                step_key="providers_completed",
                status=provider_status,
                message=f"{response.provider} finished with status {response.status}.",
                metadata={
                    "provider": response.provider,
                    "provider_id": response.provider,
                    "model_name": response.model_name,
                    "status": response.status,
                    "artifact_id": model_run.run_id,
                    "run_id": model_run.run_id,
                    "result_id": parsed_result.result_id,
                    "model_input_sha256": model_run.model_input_sha256,
                    "raw_output_sha256": model_run.raw_output_sha256,
                    "parsed_json_sha256": parsed_result.parsed_json_sha256,
                    "schema_valid": schema_valid,
                    "parse_status": getattr(output_parse, "parse_status", ""),
                    "latency_ms": response.latency_ms,
                },
            )
            parsed_results.append(parsed_result)
        record_pipeline_event(
            store,
            pipeline_run_id=pipeline_run_id,
            evidence_sha256=str(bundle["evidence_sha256"]),
            operation=operation,
            step_key="outputs_parsed",
            status="completed",
            message=f"{len(parsed_results)} model output(s) parsed.",
            metadata={"parsed_result_count": len(parsed_results), "schema_valid_count": sum(1 for result in parsed_results if result.schema_valid)},
        )
        if owns_pipeline_run:
            finish_pipeline_run(
                store,
                pipeline_run_id=pipeline_run_id,
                evidence_sha256=str(bundle["evidence_sha256"]),
                operation=operation,
                status="succeeded",
                message="Model stage completed.",
            )
        return parsed_results
    except Exception as exc:
        if owns_pipeline_run:
            finish_pipeline_run(
                store,
                pipeline_run_id=pipeline_run_id,
                evidence_sha256=str(bundle.get("evidence_sha256") or ""),
                operation=operation,
                status="failed",
                message=str(exc),
            )
        raise


def _run_providers_parallel(bundle: dict, providers: list[ModelProvider]) -> list[ModelResponse]:
    if not providers:
        return []
    preflight = safety_preflight_for_bundle(bundle)
    if not preflight.passed:
        return [blocked_provider_response(provider, preflight) for provider in providers]
    if len(providers) == 1:
        return [_run_provider(bundle, providers[0])]

    responses_by_index: dict[int, ModelResponse] = {}
    max_workers = min(len(providers), 8)
    with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="oes-model") as executor:
        futures = {
            executor.submit(_run_provider, bundle, provider): index
            for index, provider in enumerate(providers)
        }
        for future in as_completed(futures):
            index = futures[future]
            responses_by_index[index] = future.result()
    return [responses_by_index[index] for index in range(len(providers))]


def _run_provider(bundle: dict, provider: ModelProvider) -> ModelResponse:
    return run_provider_with_retries(provider, bundle).response


def run_route_stage(
    store: SQLiteStore,
    bundle: dict,
    parsed_results: list[ParsedResultRecord] | None = None,
    *,
    pipeline_run_id: str | None = None,
    operation: str = "synthesis",
) -> RoutingResult:
    effective_results = parsed_results if parsed_results is not None else store.fetch_parsed_results(bundle["evidence_sha256"])
    routing = route_claims(bundle, effective_results)
    _persist_routing(store, routing)
    record_pipeline_event(
        store,
        pipeline_run_id=pipeline_run_id,
        evidence_sha256=str(bundle["evidence_sha256"]),
        operation=operation,
        step_key="claims_routed",
        status="completed",
        message=f"{len(routing.claims)} claims and {len(routing.propositions)} propositions routed.",
        metadata={"claim_count": len(routing.claims), "proposition_count": len(routing.propositions)},
    )
    return routing


def run_score_stage(
    store: SQLiteStore,
    routing: RoutingResult,
    parsed_results: list[ParsedResultRecord],
) -> tuple[ScoreRecord, ...]:
    scores = score_propositions(routing.propositions, routing.claims, parsed_results)
    store.insert_scores(scores)
    return scores


def run_demo(
    *,
    db_path: str | Path = "workspace/ops_evidence_synthesis.sqlite3",
    sample_path: str | Path = "data/sample_logs.jsonl",
) -> PipelineResult:
    store = SQLiteStore(db_path)
    store.init_schema()
    ingest_jsonl(sample_path, store)
    incident = IncidentWindow(
        service="payment-api",
        environment="prod",
        incident_start="2026-06-12T10:00:00Z",
        incident_end="2026-06-12T10:20:00Z",
        lookback_minutes=45,
    )
    return run_pipeline(store, incident)


def _persist_routing(store: SQLiteStore, routing: RoutingResult) -> None:
    store.insert_claims(routing.claims)
    store.insert_propositions(routing.propositions)


def _persist_model_output_artifact(
    store: SQLiteStore,
    *,
    model_run: ModelRunRecord,
    output_parse: object,
    parsed_json_sha256: str,
    schema_valid: bool,
    schema_errors: tuple[str, ...],
) -> None:
    if not hasattr(store, "insert_model_output_artifact"):
        return
    artifact = model_output_artifact(
        run_id=model_run.run_id,
        evidence_sha256=model_run.evidence_sha256,
        provider=model_run.provider,
        model_name=model_run.model_name,
        raw_output_sha256=model_run.raw_output_sha256,
        parse_result=output_parse,  # type: ignore[arg-type]
        parsed_json_sha256=parsed_json_sha256,
        schema_valid=schema_valid,
        schema_errors=schema_errors,
        status=model_run.status,
        created_at=model_run.created_at,
    )
    store.insert_model_output_artifact(artifact)
