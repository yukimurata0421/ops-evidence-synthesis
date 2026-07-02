from __future__ import annotations

import json
import os
import re
import threading
import time
import uuid
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
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
from ops_evidence_synthesis.profile_gate import build_approved_profile_model_context, build_profile_context_summary
from ops_evidence_synthesis.source_context import (
    source_analysis_model_context,
    source_context_model_context,
    validate_source_analysis_bundle_for_upload,
    validate_source_context_bundle_for_upload,
)
from ops_evidence_synthesis.storage.provider_chunk_runs import (
    ProviderChunkRunStore,
    build_provider_chunk_run_store_from_env,
)
from ops_evidence_synthesis.synthesis.output_ingest import model_output_artifact, parse_model_output
from ops_evidence_synthesis.synthesis.validation import validate_claim_result
from ops_evidence_synthesis.timeutils import utc_now


MODEL_RUN_SCHEMA_VERSION = "model_run.v1"
MULTI_AI_SYNTHESIS_SCHEMA_VERSION = "multi_ai_synthesis.v1"
SCORE_NOTE = "Score is review priority, not truth probability."
DEFAULT_EVIDENCE_CHUNK_SIZE = 140
DEFAULT_CHUNK_TARGET_TOKENS = 70_000
PARTIAL_CHUNK_SUCCESS_MIN_RATIO = 0.8
PROVIDER_CHUNK_TARGET_TOKENS = {
    "gemini-enterprise-agent-platform": 80_000,
    "openai-gpt-oss-on-vertex": 64_000,
    "qwen-agent-platform": 80_000,
    "glm-agent-platform": 80_000,
    "gemma-agent-platform": 80_000,
    "llama-agent-platform": 80_000,
    "mistral-agent-platform": 120_000,
    "claude-agent-platform": 48_000,
}
PROVIDER_EVIDENCE_CHUNK_SIZE = {
    "mistral-agent-platform": 500,
}
PROVIDER_CHUNK_WORKER_DEFAULTS = {
    "mistral-agent-platform": 1,
}
PROVIDER_CHUNK_INPUT_TOKENS_PER_MINUTE = {
    "mistral-agent-platform": 60_000,
}
PROVIDER_CHUNK_MIN_START_INTERVAL_SECONDS = {
    "mistral-agent-platform": 120.0,
}
PROVIDER_RATE_LIMIT_COOLDOWN_SECONDS = {
    "mistral-agent-platform": 180.0,
}
MIN_ADAPTIVE_SUBCHUNK_TOKENS = 8_000
_EVIDENCE_REF_RE = re.compile(r"\b(?:PATTERN|LOG|EV|EVIDENCE)-\d+\b")
SUPPORTED_MODEL_STATUSES = {
    "ok",
    "failed",
    "skipped_not_configured",
    "timeout",
    "blocked_by_safety_preflight",
}
FAILED_MODEL_STATUSES = {"failed", "error", "timeout", "blocked_by_safety_preflight"}
CHUNK_FAILURE_STATUSES = {
    "context_length",
    "deterministic_parse_failure",
    "empty_response",
    "provider_error",
    "rate_limited",
    "retry_exhausted",
    "safety_filter",
    "schema_invalid",
    "timeout",
}
PROVIDER_CHUNK_LEDGER_FILENAME = "provider_chunk_runs.jsonl"


@dataclass(frozen=True, slots=True)
class _ArtifactEnvelope:
    artifact: dict[str, Any]
    raw_output: str
    parsed_payload: dict[str, Any]
    output_parse: Any


@dataclass(slots=True)
class _ProviderChunkStartPacer:
    provider_id: str
    last_started_at: float = 0.0
    blocked_until: float = 0.0
    lock: threading.Lock = field(default_factory=threading.Lock)

    def wait(self, items: list[dict[str, Any]]) -> None:
        interval = _chunk_start_interval_seconds(self.provider_id, items)
        if interval <= 0:
            return
        with self.lock:
            now = time.monotonic()
            if self.blocked_until > now:
                time.sleep(self.blocked_until - now)
                now = time.monotonic()
            if self.last_started_at > 0:
                sleep_for = max(0.0, self.last_started_at + interval - now)
                if sleep_for > 0:
                    time.sleep(sleep_for)
            self.last_started_at = time.monotonic()

    def note_result(self, envelope: _ArtifactEnvelope) -> None:
        if _chunk_envelope_execution_status(envelope) != "rate_limited":
            return
        cooldown = _provider_rate_limit_cooldown_seconds(self.provider_id)
        if cooldown <= 0:
            return
        with self.lock:
            self.blocked_until = max(self.blocked_until, time.monotonic() + cooldown)


@dataclass(slots=True)
class _ProviderChunkLedger:
    records: list[dict[str, Any]]
    cache: dict[str, dict[str, Any]]
    path: Path | None = None
    backend: ProviderChunkRunStore | None = None
    lock: threading.Lock = field(default_factory=threading.Lock)

    def success_record(self, provider_id: str, prompt_sha256: str, model_name: str = "") -> dict[str, Any] | None:
        if self.backend is not None:
            record = self.backend.success_record(provider_id, prompt_sha256)
            if record is not None and _chunk_record_is_success(record) and _chunk_record_matches_model(record, model_name):
                self.cache[_chunk_cache_key(provider_id, prompt_sha256, model_name)] = record
                return record
        record = self.cache.get(_chunk_cache_key(provider_id, prompt_sha256, model_name))
        if not record:
            return None
        if _chunk_record_is_success(record) and _chunk_record_matches_model(record, model_name):
            return record
        return None

    def reusable_record(self, provider_id: str, prompt_sha256: str, model_name: str = "") -> dict[str, Any] | None:
        success = self.success_record(provider_id, prompt_sha256, model_name)
        if success is not None:
            return success
        if not _reuse_failed_chunk_records(provider_id):
            return None
        if self.backend is not None:
            record = self.backend.latest_record(provider_id, prompt_sha256)
            if _chunk_record_is_reusable(record) and _chunk_record_matches_model(record, model_name):
                self.cache[_chunk_cache_key(provider_id, prompt_sha256, model_name)] = record
                return record
        record = self.cache.get(_chunk_cache_key(provider_id, prompt_sha256, model_name))
        return record if _chunk_record_is_reusable(record) and _chunk_record_matches_model(record, model_name) else None

    def append(self, record: dict[str, Any]) -> None:
        with self.lock:
            self.records.append(record)
            provider_id = str(record.get("provider_id") or "")
            model_name = str(record.get("model_name") or "")
            prompt_sha256 = str(record.get("prompt_sha256") or "")
            if provider_id and prompt_sha256:
                self.cache[_chunk_cache_key(provider_id, prompt_sha256, model_name)] = record
            if self.path is not None:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
        if self.backend is not None:
            self.backend.upsert_record(record)


def _chunk_record_is_success(record: dict[str, Any]) -> bool:
    if str(record.get("status") or "") != "ok":
        return False
    artifact = record.get("artifact") if isinstance(record.get("artifact"), dict) else {}
    parsed_payload = record.get("parsed_payload") if isinstance(record.get("parsed_payload"), dict) else {}
    return artifact.get("schema_valid") is True and bool(parsed_payload)


def _chunk_record_is_reusable(record: dict[str, Any] | None) -> bool:
    if not isinstance(record, dict):
        return False
    artifact = record.get("artifact") if isinstance(record.get("artifact"), dict) else {}
    parsed_payload = record.get("parsed_payload") if isinstance(record.get("parsed_payload"), dict) else {}
    if not artifact or not parsed_payload:
        return False
    return bool(str(record.get("status") or ""))


def _chunk_record_matches_model(record: dict[str, Any], model_name: str = "") -> bool:
    requested = str(model_name or "").strip()
    if not requested:
        return True
    recorded = str(record.get("model_name") or "").strip()
    return bool(recorded) and recorded == requested


def _reuse_failed_chunk_records(provider_id: str = "") -> bool:
    mapped = _mapped_provider_setting("OES_MULTI_AI_REUSE_FAILED_CHUNK_RECORDS_BY_PROVIDER", provider_id)
    if mapped:
        return mapped.strip().casefold() in {"1", "true", "yes", "on"}
    raw = os.environ.get("OES_MULTI_AI_REUSE_FAILED_CHUNK_RECORDS", "").strip().casefold()
    return raw in {"1", "true", "yes", "on"}


def _chunk_cache_key(provider_id: str, prompt_sha256: str, model_name: str = "") -> str:
    return sha256_text(f"{provider_id}:{model_name}:{prompt_sha256}")


def _provider_chunk_ledger_for_output_dir(output_dir: str | Path | None) -> _ProviderChunkLedger:
    path = Path(output_dir) / PROVIDER_CHUNK_LEDGER_FILENAME if output_dir is not None else None
    records: list[dict[str, Any]] = []
    cache: dict[str, dict[str, Any]] = {}
    if path is not None and path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(record, dict):
                continue
            records.append(record)
            provider_id = str(record.get("provider_id") or "")
            model_name = str(record.get("model_name") or "")
            prompt_sha256 = str(record.get("prompt_sha256") or "")
            if provider_id and prompt_sha256:
                cache[_chunk_cache_key(provider_id, prompt_sha256, model_name)] = record
    backend = build_provider_chunk_run_store_from_env()
    if backend is not None:
        backend.init_schema()
    return _ProviderChunkLedger(records=records, cache=cache, path=path, backend=backend)


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
    chunk_ledger = _provider_chunk_ledger_for_output_dir(output_dir)
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
        model_runs = _run_model_artifacts(
            bundle,
            provider_list,
            store=store,
            pipeline_run_id=pipeline_run_id,
            chunk_ledger=chunk_ledger,
        )
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
        profile_context = build_profile_context_summary(
            profile_id=str((approved_profile or {}).get("profile_id") or ""),
            profile_draft={},
            approved_profile=approved_profile or {},
            source_context_sha=str((source_context or {}).get("source_context_sha256") or ""),
            source_analysis_sha=str((source_analysis or {}).get("analysis_sha256") or ""),
            review_targets=review_targets,
        )
        result = {
            "schema_version": "multi_ai_run.v1",
            "evidence_sha256": evidence_sha,
            "pipeline_run_id": pipeline_run_id or "",
            "provider_registry": provider_infos(),
            "context_inputs": _context_input_summary(bundle),
            "profile_context": profile_context,
            "model_runs": model_runs,
            "provider_chunk_runs": chunk_ledger.records,
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
    (out / "multi_ai_run.json").write_text(
        pretty_json(_public_multi_ai_run_result(result)) + "\n",
        encoding="utf-8",
    )
    (out / "profile_context.json").write_text(
        pretty_json(result.get("profile_context") or {}) + "\n",
        encoding="utf-8",
    )
    with (out / "model_runs.jsonl").open("w", encoding="utf-8") as handle:
        for run in result.get("model_runs") or []:
            handle.write(json.dumps(run, ensure_ascii=False, sort_keys=True) + "\n")
    with (out / PROVIDER_CHUNK_LEDGER_FILENAME).open("w", encoding="utf-8") as handle:
        for run in result.get("provider_chunk_runs") or []:
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


def _public_multi_ai_run_result(result: dict[str, Any]) -> dict[str, Any]:
    """Persist a replayable run envelope without raw model response bodies."""
    return {
        key: value
        for key, value in result.items()
        if key
        in {
            "schema_version",
            "evidence_sha256",
            "pipeline_run_id",
            "provider_registry",
            "context_inputs",
            "profile_context",
            "model_runs",
            "provider_chunk_runs",
            "multi_ai_synthesis",
            "canonical_review_graph",
            "canonical_graph_status",
            "canonical_graph_sha256",
            "input_fingerprint_sha256",
            "canonical_graph_snapshot",
            "review_targets",
            "persistence_warning",
        }
    }


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
    provider_execution_status_counts = Counter(_artifact_execution_status(run) for run in model_runs)
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
        "provider_execution_status_counts": dict(sorted(provider_execution_status_counts.items())),
        "provider_failure_count_excluding_silent": sum(
            count
            for status, count in provider_execution_status_counts.items()
            if status not in {"ok", "skipped_not_configured"}
        ),
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
    chunk_ledger: _ProviderChunkLedger | None = None,
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
        metadata={
            "provider_count": len(providers),
            "providers": [provider.provider for provider in providers],
            "full_corpus_coverage": _full_corpus_coverage_summary(bundle),
        },
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
        envelopes = [_run_provider_full_corpus(bundle, providers[0], preflight, chunk_ledger=chunk_ledger)]
        _persist_artifact_envelopes(store, envelopes)
        _record_multi_ai_provider_events(store, pipeline_run_id, [envelope.artifact for envelope in envelopes])
        return [envelope.artifact for envelope in envelopes]

    by_index: dict[int, _ArtifactEnvelope] = {}
    with ThreadPoolExecutor(max_workers=_provider_worker_count(len(providers)), thread_name_prefix="oes-multi-ai") as executor:
        futures = {
            executor.submit(_run_provider_full_corpus, bundle, provider, preflight, chunk_ledger=chunk_ledger): index
            for index, provider in enumerate(providers)
        }
        for future in as_completed(futures):
            by_index[futures[future]] = future.result()
    envelopes = [by_index[index] for index in range(len(providers))]
    _persist_artifact_envelopes(store, envelopes)
    _record_multi_ai_provider_events(store, pipeline_run_id, [envelope.artifact for envelope in envelopes])
    return [envelope.artifact for envelope in envelopes]


def _provider_worker_count(provider_count: int) -> int:
    if provider_count <= 1:
        return 1
    raw = os.environ.get("OES_MULTI_AI_MAX_WORKERS", "").strip()
    if not raw:
        return min(provider_count, 8)
    try:
        requested = int(raw)
    except ValueError:
        return min(provider_count, 8)
    return max(1, min(provider_count, requested))


def _record_multi_ai_provider_events(store: Any | None, pipeline_run_id: str | None, artifacts: list[dict[str, Any]]) -> None:
    for artifact in artifacts:
        status = str(artifact.get("status") or "")
        execution_status = _artifact_execution_status(artifact)
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
                "execution_status": execution_status,
                "failure_is_not_silent": execution_status in CHUNK_FAILURE_STATUSES,
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
    schema_repair_rules: tuple[str, ...] = ()
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
            parsed_payload, schema_repair_rules = _normalize_claim_result_payload(
                output_parse.parsed,
                known_refs=_known_evidence_refs(bundle),
            )
            schema_valid, schema_errors = validate_claim_result(parsed_payload)
            parsed_result = _parsed_result_payload(parsed_payload)
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
        schema_repair_rules=schema_repair_rules,
    )
    if status != "ok" and isinstance(output_parse.parsed, dict):
        artifact["provider_error"] = _provider_error_detail(output_parse.parsed)
    _annotate_execution_status(artifact)
    return _ArtifactEnvelope(
        artifact=artifact,
        raw_output=response.raw_output,
        parsed_payload=parsed_payload,
        output_parse=output_parse,
    )


def _run_provider_chunk(
    bundle: dict[str, Any],
    provider: ModelProvider,
    preflight: SafetyPreflightResult,
    chunk_ledger: _ProviderChunkLedger | None = None,
) -> _ArtifactEnvelope:
    cached_envelope = _cached_provider_chunk_envelope(bundle, provider, chunk_ledger)
    if cached_envelope is not None:
        return cached_envelope

    chunk = bundle.get("full_corpus_chunk") if isinstance(bundle.get("full_corpus_chunk"), dict) else {}
    started_at = utc_now()
    envelope = _run_single_provider(bundle, provider, preflight)
    finished_at = utc_now()
    if chunk_ledger is not None and chunk:
        chunk_ledger.append(
            _provider_chunk_run_record(
                bundle=bundle,
                provider=provider,
                envelope=envelope,
                started_at=started_at,
                finished_at=finished_at,
            )
        )
    return envelope


def _envelope_from_chunk_ledger_record(record: dict[str, Any]) -> _ArtifactEnvelope:
    artifact = json.loads(json.dumps(record.get("artifact") or {}, ensure_ascii=False))
    parsed_payload = json.loads(json.dumps(record.get("parsed_payload") or {}, ensure_ascii=False))
    artifact["provider_chunk_cache_hit"] = True
    retry = artifact.get("retry") if isinstance(artifact.get("retry"), dict) else {}
    artifact["retry"] = {**retry, "cache_hit": True}
    _annotate_execution_status(artifact)
    raw_output = json.dumps(parsed_payload, ensure_ascii=False, sort_keys=True)
    return _ArtifactEnvelope(
        artifact=artifact,
        raw_output=raw_output,
        parsed_payload=parsed_payload,
        output_parse=parse_model_output(raw_output),
    )


def _provider_chunk_run_record(
    *,
    bundle: dict[str, Any],
    provider: ModelProvider,
    envelope: _ArtifactEnvelope,
    started_at: str,
    finished_at: str,
) -> dict[str, Any]:
    artifact = envelope.artifact
    chunk = _artifact_chunk_manifest(artifact)
    prompt_sha256 = str(chunk.get("provider_prompt_sha256") or "")
    execution_status = _artifact_execution_status(artifact)
    retry = artifact.get("retry") if isinstance(artifact.get("retry"), dict) else {}
    failure_message = _artifact_failure_message(artifact)
    return {
        "schema_version": "provider_chunk_run.v1",
        "run_id": str(artifact.get("run_id") or ""),
        "evidence_sha256": str(bundle.get("evidence_sha256") or artifact.get("evidence_sha256") or ""),
        "provider_id": provider.provider,
        "model_name": provider.model_name,
        "chunk_id": str(chunk.get("chunk_id") or ""),
        "chunk_index": int(chunk.get("chunk_index") or 0),
        "chunk_count": int(chunk.get("chunk_count") or 0),
        "chunk_type": str(chunk.get("chunk_type") or ""),
        "prompt_sha256": prompt_sha256,
        "prompt_cache_key": _chunk_cache_key(provider.provider, prompt_sha256, provider.model_name) if prompt_sha256 else "",
        "status": execution_status,
        "provider_status": str(artifact.get("status") or ""),
        "schema_valid": bool(artifact.get("schema_valid")),
        "attempt_count": int(retry.get("attempts") or 0),
        "max_attempts": int(retry.get("max_attempts") or 0),
        "retried": bool(retry.get("retried")),
        "retryable": bool(retry.get("retryable")),
        "last_error_type": "" if execution_status == "ok" else execution_status,
        "last_error_message": failure_message[:1000],
        "retry_after_sec": _retry_after_seconds_from_text(failure_message),
        "input_tokens": int(artifact.get("input_tokens") or 0),
        "output_tokens": int(artifact.get("output_tokens") or 0),
        "latency_ms": int(artifact.get("latency_ms") or 0),
        "raw_output_sha256": str(artifact.get("raw_output_sha256") or ""),
        "parsed_output_sha256": str(artifact.get("parsed_json_sha256") or ""),
        "parse_status": str(artifact.get("parse_status") or ""),
        "repair_applied": bool(artifact.get("repair_applied")),
        "repair_rules": list(artifact.get("repair_rules") or []),
        "semantic_keys": list(chunk.get("semantic_keys") or []),
        "coverage_classes": list(chunk.get("coverage_classes") or []),
        "source_log_count": int(chunk.get("source_log_count") or 0),
        "evidence_item_count": int(chunk.get("evidence_item_count") or 0),
        "estimated_input_tokens": int(chunk.get("estimated_input_tokens") or 0),
        "token_budget": int(chunk.get("token_budget") or 0),
        "started_at": started_at,
        "finished_at": finished_at,
        "artifact": artifact,
        "parsed_payload": envelope.parsed_payload,
    }


def _run_provider_full_corpus(
    bundle: dict[str, Any],
    provider: ModelProvider,
    preflight: SafetyPreflightResult,
    *,
    chunk_ledger: _ProviderChunkLedger | None = None,
) -> _ArtifactEnvelope:
    chunks = _evidence_item_chunks(bundle, provider_id=provider.provider)
    if len(chunks) <= 1:
        envelope = _run_single_provider(bundle, provider, preflight)
        coverage = _full_corpus_coverage_summary(bundle, chunk_count=1, provider_id=provider.provider)
        envelope.artifact["full_corpus_coverage"] = coverage
        envelope.artifact.setdefault("model_input_context", {})["full_corpus_coverage"] = coverage
        return envelope

    child_envelopes = _run_provider_chunks_parallel(bundle, provider, preflight, chunks, chunk_ledger=chunk_ledger)

    return _merge_chunked_provider_envelopes(
        bundle,
        provider,
        preflight,
        child_envelopes,
        chunk_count=len(chunks),
    )


def _run_provider_chunks_parallel(
    bundle: dict[str, Any],
    provider: ModelProvider,
    preflight: SafetyPreflightResult,
    chunks: list[list[dict[str, Any]]],
    *,
    chunk_ledger: _ProviderChunkLedger | None = None,
) -> list[_ArtifactEnvelope]:
    pacer = _ProviderChunkStartPacer(provider.provider)
    if len(chunks) <= 1:
        child_bundle = _bundle_for_evidence_chunk(
            bundle,
            evidence_items=chunks[0] if chunks else [],
            chunk_index=1,
            total_chunks=1,
            provider_id=provider.provider,
        )
        pacer.wait(chunks[0] if chunks else [])
        return [_run_provider_chunk(child_bundle, provider, preflight, chunk_ledger=chunk_ledger)]

    by_index: dict[int, _ArtifactEnvelope] = {}
    with ThreadPoolExecutor(
        max_workers=_chunk_worker_count(len(chunks), provider.provider),
        thread_name_prefix=f"oes-{provider.provider}-chunk",
    ) as executor:
        futures = {}
        for index, items in enumerate(chunks, start=1):
            child_bundle = _bundle_for_evidence_chunk(
                bundle,
                evidence_items=items,
                chunk_index=index,
                total_chunks=len(chunks),
                provider_id=provider.provider,
            )
            futures[
                executor.submit(
                    _run_provider_chunk_with_pacing,
                    child_bundle,
                    provider,
                    preflight,
                    items,
                    pacer,
                    chunk_ledger,
                )
            ] = index
        for future in as_completed(futures):
            by_index[futures[future]] = future.result()
    _retry_failed_chunk_envelopes(
        bundle,
        provider,
        preflight,
        chunks,
        by_index,
        pacer=pacer,
        chunk_ledger=chunk_ledger,
    )
    return [by_index[index] for index in range(1, len(chunks) + 1)]


def _run_provider_chunk_with_pacing(
    child_bundle: dict[str, Any],
    provider: ModelProvider,
    preflight: SafetyPreflightResult,
    items: list[dict[str, Any]],
    pacer: _ProviderChunkStartPacer,
    chunk_ledger: _ProviderChunkLedger | None,
) -> _ArtifactEnvelope:
    cached_envelope = _cached_provider_chunk_envelope(child_bundle, provider, chunk_ledger)
    if cached_envelope is not None:
        return cached_envelope
    pacer.wait(items)
    envelope = _run_provider_chunk(child_bundle, provider, preflight, chunk_ledger=chunk_ledger)
    pacer.note_result(envelope)
    return envelope


def _cached_provider_chunk_envelope(
    bundle: dict[str, Any],
    provider: ModelProvider,
    chunk_ledger: _ProviderChunkLedger | None,
) -> _ArtifactEnvelope | None:
    if chunk_ledger is None:
        return None
    chunk = bundle.get("full_corpus_chunk") if isinstance(bundle.get("full_corpus_chunk"), dict) else {}
    prompt_sha256 = str(chunk.get("provider_prompt_sha256") or "")
    if not prompt_sha256:
        return None
    cached = chunk_ledger.reusable_record(provider.provider, prompt_sha256, provider.model_name)
    if cached is None:
        return None
    return _envelope_from_chunk_ledger_record(cached)


def _retry_failed_chunk_envelopes(
    bundle: dict[str, Any],
    provider: ModelProvider,
    preflight: SafetyPreflightResult,
    chunks: list[list[dict[str, Any]]],
    by_index: dict[int, _ArtifactEnvelope],
    *,
    pacer: _ProviderChunkStartPacer,
    chunk_ledger: _ProviderChunkLedger | None = None,
) -> None:
    attempts = _chunk_retry_attempts()
    if attempts <= 0:
        return
    total_chunks = len(chunks)
    for _attempt in range(attempts):
        failed_indexes = [
            index
            for index in range(1, total_chunks + 1)
            if by_index.get(index) is None or _chunk_envelope_retryable(by_index[index])
        ]
        if not failed_indexes:
            return
        retry_workers = _retry_chunk_worker_count(
            failed_indexes,
            provider_id=provider.provider,
            by_index=by_index,
        )
        retry_delay = _chunk_retry_delay_seconds(
            failed_indexes,
            provider_id=provider.provider,
            by_index=by_index,
            attempt_index=_attempt + 1,
        )
        if retry_delay > 0:
            time.sleep(retry_delay)
        with ThreadPoolExecutor(
            max_workers=retry_workers,
            thread_name_prefix=f"oes-{provider.provider}-chunk-retry",
        ) as executor:
            futures = {}
            for index in failed_indexes:
                items = chunks[index - 1]
                failure_status = _chunk_envelope_execution_status(by_index.get(index))
                if _adaptive_subchunk_retry_enabled(provider.provider, items, failure_status=failure_status):
                    futures[
                        executor.submit(
                            _run_adaptive_subchunk_retry,
                            bundle,
                            provider,
                            preflight,
                            items,
                            index,
                            total_chunks,
                            pacer=pacer,
                            chunk_ledger=chunk_ledger,
                        )
                    ] = index
                    continue
                child_bundle = _bundle_for_evidence_chunk(
                    bundle,
                    evidence_items=items,
                    chunk_index=index,
                    total_chunks=total_chunks,
                    provider_id=provider.provider,
                )
                futures[
                    executor.submit(
                        _run_provider_chunk_with_pacing,
                        child_bundle,
                        provider,
                        preflight,
                        items,
                        pacer,
                        chunk_ledger,
                    )
                ] = index
            for future in as_completed(futures):
                by_index[futures[future]] = future.result()


def _chunk_envelope_failed(envelope: _ArtifactEnvelope) -> bool:
    artifact = envelope.artifact
    return str(artifact.get("status") or "") != "ok" or artifact.get("schema_valid") is not True


def _chunk_envelope_retryable(envelope: _ArtifactEnvelope) -> bool:
    if not _chunk_envelope_failed(envelope):
        return False
    status = _chunk_envelope_execution_status(envelope)
    return status not in {"safety_filter", "skipped_not_configured"}


def _chunk_envelope_execution_status(envelope: _ArtifactEnvelope | None) -> str:
    if envelope is None:
        return "provider_error"
    return _artifact_execution_status(envelope.artifact)


def _retry_chunk_worker_count(
    failed_indexes: list[int],
    *,
    provider_id: str,
    by_index: dict[int, _ArtifactEnvelope],
) -> int:
    statuses = {_chunk_envelope_execution_status(by_index.get(index)) for index in failed_indexes}
    if "rate_limited" in statuses:
        return 1
    if "timeout" in statuses:
        return min(2, _chunk_worker_count(len(failed_indexes), provider_id))
    return _chunk_worker_count(len(failed_indexes), provider_id)


def _chunk_retry_delay_seconds(
    failed_indexes: list[int],
    *,
    provider_id: str,
    by_index: dict[int, _ArtifactEnvelope],
    attempt_index: int,
) -> float:
    retry_after_values: list[int] = []
    statuses: set[str] = set()
    for index in failed_indexes:
        envelope = by_index.get(index)
        statuses.add(_chunk_envelope_execution_status(envelope))
        if envelope is None:
            continue
        retry_after = _retry_after_seconds_from_text(_artifact_failure_message(envelope.artifact))
        if retry_after > 0:
            retry_after_values.append(retry_after)
    if retry_after_values:
        return float(min(max(retry_after_values), 120))
    if "rate_limited" not in statuses:
        return 0.0
    provider_cooldown = _provider_rate_limit_cooldown_seconds(provider_id)
    if provider_cooldown > 0:
        return min(provider_cooldown * max(1, attempt_index), 600.0)
    raw = os.environ.get("OES_MULTI_AI_RATE_LIMIT_BACKOFF_SECONDS", "").strip()
    try:
        base = float(raw) if raw else 0.25
    except ValueError:
        base = 0.25
    return max(0.0, min(base * max(1, attempt_index), 30.0))


def _adaptive_subchunk_retry_enabled(
    provider_id: str,
    items: list[dict[str, Any]],
    *,
    failure_status: str = "",
) -> bool:
    if len(items) <= 1:
        return False
    if failure_status in {"context_length", "timeout"}:
        return True
    if failure_status in {
        "rate_limited",
        "schema_invalid",
        "deterministic_parse_failure",
        "empty_response",
        "provider_error",
        "retry_exhausted",
        "safety_filter",
        "skipped_not_configured",
    }:
        return False
    raw = os.environ.get("OES_MULTI_AI_ADAPTIVE_SUBCHUNK_RETRY", "").strip().casefold()
    if raw in {"0", "false", "no", "off"}:
        return False
    if raw in {"1", "true", "yes", "on"}:
        return True
    return _chunk_target_tokens(provider_id) > 0


def _run_adaptive_subchunk_retry(
    bundle: dict[str, Any],
    provider: ModelProvider,
    preflight: SafetyPreflightResult,
    items: list[dict[str, Any]],
    parent_chunk_index: int,
    parent_chunk_count: int,
    *,
    pacer: _ProviderChunkStartPacer | None = None,
    chunk_ledger: _ProviderChunkLedger | None = None,
) -> _ArtifactEnvelope:
    pacer = pacer or _ProviderChunkStartPacer(provider.provider)
    subchunks = _adaptive_subchunks_for_failed_chunk(items, provider.provider)
    if len(subchunks) <= 1:
        child_bundle = _bundle_for_evidence_chunk(
            bundle,
            evidence_items=items,
            chunk_index=parent_chunk_index,
            total_chunks=parent_chunk_count,
            provider_id=provider.provider,
        )
        return _run_provider_chunk_with_pacing(child_bundle, provider, preflight, items, pacer, chunk_ledger)
    parent_manifest = _chunk_manifest_from_items(
        items,
        chunk_index=parent_chunk_index,
        total_chunks=parent_chunk_count,
        full_evidence_item_count=len([row for row in bundle.get("evidence_items") or [] if isinstance(row, dict)]),
        provider_id=provider.provider,
        adaptive_retry=True,
    )
    sub_envelopes_by_index: dict[int, _ArtifactEnvelope] = {}
    with ThreadPoolExecutor(
        max_workers=_chunk_worker_count(len(subchunks), provider.provider),
        thread_name_prefix=f"oes-{provider.provider}-subchunk-retry",
    ) as executor:
        futures = {}
        for sub_index, sub_items in enumerate(subchunks, start=1):
            child_bundle = _bundle_for_evidence_chunk(
                bundle,
                evidence_items=sub_items,
                chunk_index=parent_chunk_index,
                total_chunks=parent_chunk_count,
                provider_id=provider.provider,
                parent_chunk_id=str(parent_manifest.get("chunk_id") or ""),
                subchunk_index=sub_index,
                subchunk_count=len(subchunks),
                adaptive_retry=True,
            )
            futures[
                executor.submit(
                    _run_provider_chunk_with_pacing,
                    child_bundle,
                    provider,
                    preflight,
                    sub_items,
                    pacer,
                    chunk_ledger,
                )
            ] = sub_index
        for future in as_completed(futures):
            sub_envelopes_by_index[futures[future]] = future.result()
    sub_envelopes = [sub_envelopes_by_index[index] for index in range(1, len(subchunks) + 1)]
    return _merge_adaptive_subchunk_envelopes(
        bundle,
        provider,
        preflight,
        parent_manifest,
        sub_envelopes,
        parent_chunk_count=parent_chunk_count,
    )


def _adaptive_subchunks_for_failed_chunk(items: list[dict[str, Any]], provider_id: str) -> list[list[dict[str, Any]]]:
    if len(items) <= 1:
        return [items]
    parent_budget = _chunk_target_tokens(provider_id)
    sub_budget = max(MIN_ADAPTIVE_SUBCHUNK_TOKENS, parent_budget // 2) if parent_budget > 0 else 0
    max_items = max(1, min(_evidence_chunk_size(provider_id), (len(items) + 1) // 2))
    if sub_budget > 0:
        subchunks = _pack_evidence_items_by_semantic_token_budget(
            items,
            max_items=max_items,
            token_budget=sub_budget,
            provider_id=provider_id,
        )
    else:
        subchunks = [items[index : index + max_items] for index in range(0, len(items), max_items)]
    if len(subchunks) == 1 and len(items) > 1:
        midpoint = (len(items) + 1) // 2
        subchunks = [items[:midpoint], items[midpoint:]]
    return [subchunk for subchunk in subchunks if subchunk]


def _merge_adaptive_subchunk_envelopes(
    bundle: dict[str, Any],
    provider: ModelProvider,
    preflight: SafetyPreflightResult,
    parent_manifest: dict[str, Any],
    sub_envelopes: list[_ArtifactEnvelope],
    *,
    parent_chunk_count: int,
) -> _ArtifactEnvelope:
    parsed_payload = _merge_chunk_claim_payloads(provider, sub_envelopes, chunk_count=parent_chunk_count)
    aggregate_raw_output = json.dumps(parsed_payload, ensure_ascii=False, sort_keys=True)
    output_parse = parse_model_output(aggregate_raw_output)
    schema_valid, schema_errors = validate_claim_result(parsed_payload)
    child_statuses = [str(envelope.artifact.get("status") or "") for envelope in sub_envelopes]
    child_schema_valid = [bool(envelope.artifact.get("schema_valid")) for envelope in sub_envelopes]
    status = (
        "ok"
        if child_statuses
        and all(status == "ok" for status in child_statuses)
        and all(child_schema_valid)
        and schema_valid
        else "failed"
    )
    response = ModelResponse(
        provider=provider.provider,
        model_name=provider.model_name,
        prompt_name=provider.prompt_name,
        temperature=provider.temperature,
        raw_output=aggregate_raw_output,
        latency_ms=sum(int(envelope.artifact.get("latency_ms") or 0) for envelope in sub_envelopes),
        input_tokens=sum(int(envelope.artifact.get("input_tokens") or 0) for envelope in sub_envelopes),
        output_tokens=sum(int(envelope.artifact.get("output_tokens") or 0) for envelope in sub_envelopes),
        status=status,
    )
    all_schema_errors = schema_errors if status == "ok" else (*schema_errors, *_chunk_failure_errors(sub_envelopes))
    artifact = _artifact_from_response(
        bundle,
        response,
        status=status,
        latency_ms=response.latency_ms,
        parsed_payload=parsed_payload,
        parsed_result=_parsed_result_payload(parsed_payload) if schema_valid and status == "ok" else _empty_parsed_result(),
        schema_valid=schema_valid and status == "ok",
        schema_errors=all_schema_errors,
        preflight=preflight,
        failure_reason="" if status == "ok" else _chunked_failure_reason(sub_envelopes, schema_errors),
        retry={"adaptive_subchunk_retry": True, "subchunk_count": len(sub_envelopes)},
        cost_estimate=_sum_child_costs(sub_envelopes),
        output_parse=output_parse,
        schema_repair_rules=tuple(
            _unique(
                f"subchunk_{index}:{rule}"
                for index, envelope in enumerate(sub_envelopes, start=1)
                for rule in envelope.artifact.get("repair_rules") or []
                if str(rule).strip()
            )
        ),
    )
    coverage = _full_corpus_coverage_summary(bundle, chunk_count=parent_chunk_count, provider_id=provider.provider)
    coverage["chunk"] = parent_manifest
    artifact["full_corpus_coverage"] = coverage
    artifact.setdefault("model_input_context", {})["full_corpus_coverage"] = coverage
    artifact["adaptive_subchunk_results"] = _chunk_result_summaries(sub_envelopes)
    artifact["chunk_status_counts"] = _chunk_status_counts(sub_envelopes)
    artifact["chunk_failure_count"] = sum(
        count
        for status_key, count in artifact["chunk_status_counts"].items()
        if status_key not in {"ok", "skipped_not_configured"}
    )
    _annotate_execution_status(artifact)
    return _ArtifactEnvelope(
        artifact=artifact,
        raw_output=aggregate_raw_output,
        parsed_payload=parsed_payload,
        output_parse=output_parse,
    )


def _chunk_retry_attempts() -> int:
    raw = os.environ.get("OES_MULTI_AI_CHUNK_RETRY_ATTEMPTS", "").strip()
    if not raw:
        return 1
    try:
        requested = int(raw)
    except ValueError:
        return 1
    return max(0, min(requested, 5))


def _evidence_item_chunks(bundle: dict[str, Any], provider_id: str = "") -> list[list[dict[str, Any]]]:
    items = [row for row in bundle.get("evidence_items") or [] if isinstance(row, dict)]
    if not items:
        return [[]]
    chunk_size = _evidence_chunk_size(provider_id)
    token_budget = _chunk_target_tokens(provider_id)
    if token_budget <= 0:
        return [items[index : index + chunk_size] for index in range(0, len(items), chunk_size)]
    return _pack_evidence_items_by_semantic_token_budget(
        items,
        max_items=chunk_size,
        token_budget=token_budget,
        provider_id=provider_id,
    )


def _pack_evidence_items_by_semantic_token_budget(
    items: list[dict[str, Any]],
    *,
    max_items: int,
    token_budget: int,
    provider_id: str = "",
) -> list[list[dict[str, Any]]]:
    chunks: list[list[dict[str, Any]]] = []
    for bucket in _semantic_evidence_buckets(items):
        current: list[dict[str, Any]] = []
        current_tokens = _chunk_prompt_overhead_tokens()
        for item in bucket:
            item_tokens = _estimated_evidence_item_tokens(item)
            would_exceed_items = len(current) >= max_items
            would_exceed_tokens = bool(current) and current_tokens + item_tokens > token_budget
            if would_exceed_items or would_exceed_tokens:
                chunks.append(current)
                current = []
                current_tokens = _chunk_prompt_overhead_tokens()
            current.append(item)
            current_tokens += item_tokens
        if current:
            chunks.append(current)
    if _merge_small_semantic_chunks_enabled(provider_id):
        chunks = _merge_adjacent_chunks_by_token_budget(chunks, max_items=max_items, token_budget=token_budget)
    return chunks or [[]]


def _merge_small_semantic_chunks_enabled(provider_id: str = "") -> bool:
    raw = _merge_small_semantic_chunks_setting(provider_id).strip().casefold()
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    return str(provider_id or "").strip().casefold() == "mistral-agent-platform"


def _merge_small_semantic_chunks_setting(provider_id: str = "") -> str:
    provider_key = _provider_worker_env_key(provider_id)
    if provider_key:
        raw = os.environ.get(f"OES_MULTI_AI_MERGE_SMALL_SEMANTIC_CHUNKS_{provider_key}", "").strip()
        if raw:
            return raw
    mapped = _mapped_provider_setting("OES_MULTI_AI_MERGE_SMALL_SEMANTIC_CHUNKS_BY_PROVIDER", provider_id)
    if mapped:
        return mapped
    return os.environ.get("OES_MULTI_AI_MERGE_SMALL_SEMANTIC_CHUNKS", "").strip()


def _merge_adjacent_chunks_by_token_budget(
    chunks: list[list[dict[str, Any]]],
    *,
    max_items: int,
    token_budget: int,
) -> list[list[dict[str, Any]]]:
    if token_budget <= 0 or len(chunks) <= 1:
        return chunks
    merged: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    current_tokens = _chunk_prompt_overhead_tokens()
    for chunk in chunks:
        if not chunk:
            continue
        chunk_item_tokens = sum(_estimated_evidence_item_tokens(item) for item in chunk)
        would_exceed_items = bool(current) and len(current) + len(chunk) > max_items
        would_exceed_tokens = bool(current) and current_tokens + chunk_item_tokens > token_budget
        if would_exceed_items or would_exceed_tokens:
            merged.append(current)
            current = []
            current_tokens = _chunk_prompt_overhead_tokens()
        current.extend(chunk)
        current_tokens += chunk_item_tokens
    if current:
        merged.append(current)
    return merged or chunks


def _semantic_evidence_buckets(items: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    order: list[str] = []
    buckets: dict[str, list[dict[str, Any]]] = {}
    for item in items:
        key = _semantic_key_for_item(item)
        if key not in buckets:
            order.append(key)
            buckets[key] = []
        buckets[key].append(item)
    return [buckets[key] for key in order]


def _semantic_keys_for_items(items: list[dict[str, Any]]) -> list[str]:
    keys: list[str] = []
    for item in items:
        key = _semantic_key_for_item(item)
        if key not in keys:
            keys.append(key)
    return keys


def _semantic_key_for_item(item: dict[str, Any]) -> str:
    coverage_class = str(item.get("coverage_class") or "").strip() or _fallback_coverage_class(item)
    subsystem = (
        str(item.get("subsystem") or "").strip()
        or str(item.get("component") or "").strip()
        or str(item.get("service") or "").strip()
        or "unknown"
    )
    event_type = (
        str(item.get("event_type") or "").strip()
        or str(item.get("type") or "").strip()
        or "event"
    )
    if coverage_class in {"pattern", "state_transition", "temporal_bucket"}:
        return f"{coverage_class}:{subsystem}:{event_type}"
    if coverage_class in {"rare", "singleton"}:
        return f"rare_singleton:{subsystem}:{event_type}"
    return f"{coverage_class}:{subsystem}:{event_type}"


def _estimated_chunk_input_tokens(items: list[dict[str, Any]]) -> int:
    return _chunk_prompt_overhead_tokens() + sum(_estimated_evidence_item_tokens(item) for item in items)


def _estimated_evidence_item_tokens(item: dict[str, Any]) -> int:
    text = json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return max(1, (len(text) + 1) // 2 + 32)


def _chunk_prompt_overhead_tokens() -> int:
    return 2_500


def _evidence_chunk_size(provider_id: str = "") -> int:
    normalized_provider_id = str(provider_id or "").strip().casefold()
    provider_default = PROVIDER_EVIDENCE_CHUNK_SIZE.get(normalized_provider_id, DEFAULT_EVIDENCE_CHUNK_SIZE)
    raw = _evidence_chunk_size_setting(provider_id)
    if not raw:
        return provider_default
    try:
        requested = int(raw)
    except ValueError:
        return provider_default
    hard_limit = max(provider_default, DEFAULT_EVIDENCE_CHUNK_SIZE)
    return max(1, min(requested, hard_limit))


def _evidence_chunk_size_setting(provider_id: str = "") -> str:
    provider_key = _provider_worker_env_key(provider_id)
    if provider_key:
        raw = os.environ.get(f"OES_MULTI_AI_EVIDENCE_CHUNK_SIZE_{provider_key}", "").strip()
        if raw:
            return raw
    mapped = _mapped_provider_setting("OES_MULTI_AI_EVIDENCE_CHUNK_SIZE_BY_PROVIDER", provider_id)
    if mapped:
        return mapped
    return os.environ.get("OES_MULTI_AI_EVIDENCE_CHUNK_SIZE", "").strip()


def _chunk_target_tokens(provider_id: str = "") -> int:
    planning_provider_id = _chunk_planning_provider_id(provider_id)
    raw = _chunk_target_token_setting(planning_provider_id)
    if raw:
        try:
            requested = int(raw)
        except ValueError:
            requested = _default_chunk_target_tokens(planning_provider_id)
    else:
        requested = _default_chunk_target_tokens(planning_provider_id)
    return max(0, requested)


def _chunk_planning_provider_id(provider_id: str = "") -> str:
    normalized_provider_id = str(provider_id or "").strip().casefold()
    if normalized_provider_id in {"gemma-agent-platform", "grok-agent-platform", "llama-agent-platform"}:
        return "gemini-enterprise-agent-platform"
    return provider_id


def _chunk_target_token_setting(provider_id: str = "") -> str:
    provider_key = _provider_worker_env_key(provider_id)
    if provider_key:
        raw = os.environ.get(f"OES_MULTI_AI_CHUNK_TARGET_TOKENS_{provider_key}", "").strip()
        if raw:
            return raw
    mapped = _mapped_provider_setting("OES_MULTI_AI_CHUNK_TARGET_TOKENS_BY_PROVIDER", provider_id)
    if mapped:
        return mapped
    return os.environ.get("OES_MULTI_AI_CHUNK_TARGET_TOKENS", "").strip()


def _default_chunk_target_tokens(provider_id: str = "") -> int:
    normalized_provider_id = str(provider_id or "").strip().casefold()
    if not normalized_provider_id:
        return 0
    return PROVIDER_CHUNK_TARGET_TOKENS.get(normalized_provider_id, 0)


def _chunk_worker_count(chunk_count: int, provider_id: str = "") -> int:
    if chunk_count <= 1:
        return 1
    raw = _chunk_worker_count_setting(provider_id)
    normalized_provider_id = str(provider_id or "").strip().casefold()
    default_workers = PROVIDER_CHUNK_WORKER_DEFAULTS.get(normalized_provider_id, 4)
    default = min(chunk_count, default_workers)
    if not raw:
        return default
    try:
        requested = int(raw)
    except ValueError:
        return default
    return max(1, min(chunk_count, requested))


def _chunk_start_interval_seconds(provider_id: str, items: list[dict[str, Any]]) -> float:
    tokens_per_minute = _chunk_input_tokens_per_minute(provider_id)
    token_interval = 0.0
    if tokens_per_minute > 0:
        estimated_tokens = _estimated_chunk_input_tokens(items)
        token_interval = max(0.0, (estimated_tokens / tokens_per_minute) * 60.0)
    return max(token_interval, _chunk_min_start_interval_seconds(provider_id))


def _chunk_min_start_interval_seconds(provider_id: str = "") -> float:
    normalized_provider_id = str(provider_id or "").strip().casefold()
    default = PROVIDER_CHUNK_MIN_START_INTERVAL_SECONDS.get(normalized_provider_id, 0.0)
    raw = _chunk_min_start_interval_setting(provider_id)
    if not raw:
        return default
    try:
        requested = float(raw)
    except ValueError:
        return default
    return max(0.0, min(requested, 600.0))


def _chunk_min_start_interval_setting(provider_id: str = "") -> str:
    provider_key = _provider_worker_env_key(provider_id)
    if provider_key:
        raw = os.environ.get(f"OES_MULTI_AI_CHUNK_MIN_START_INTERVAL_SECONDS_{provider_key}", "").strip()
        if raw:
            return raw
    mapped = _mapped_provider_setting("OES_MULTI_AI_CHUNK_MIN_START_INTERVAL_SECONDS_BY_PROVIDER", provider_id)
    if mapped:
        return mapped
    return os.environ.get("OES_MULTI_AI_CHUNK_MIN_START_INTERVAL_SECONDS", "").strip()


def _chunk_input_tokens_per_minute(provider_id: str = "") -> int:
    normalized_provider_id = str(provider_id or "").strip().casefold()
    default = PROVIDER_CHUNK_INPUT_TOKENS_PER_MINUTE.get(normalized_provider_id, 0)
    raw = _chunk_input_tokens_per_minute_setting(provider_id)
    if not raw:
        return default
    try:
        requested = int(raw)
    except ValueError:
        return default
    return max(0, requested)


def _chunk_input_tokens_per_minute_setting(provider_id: str = "") -> str:
    provider_key = _provider_worker_env_key(provider_id)
    if provider_key:
        raw = os.environ.get(f"OES_MULTI_AI_CHUNK_INPUT_TOKENS_PER_MINUTE_{provider_key}", "").strip()
        if raw:
            return raw
    mapped = _mapped_provider_setting("OES_MULTI_AI_CHUNK_INPUT_TOKENS_PER_MINUTE_BY_PROVIDER", provider_id)
    if mapped:
        return mapped
    return os.environ.get("OES_MULTI_AI_CHUNK_INPUT_TOKENS_PER_MINUTE", "").strip()


def _provider_rate_limit_cooldown_seconds(provider_id: str = "") -> float:
    normalized_provider_id = str(provider_id or "").strip().casefold()
    default = PROVIDER_RATE_LIMIT_COOLDOWN_SECONDS.get(normalized_provider_id, 0.0)
    raw = _provider_rate_limit_cooldown_setting(provider_id)
    if not raw:
        return default
    try:
        requested = float(raw)
    except ValueError:
        return default
    return max(0.0, min(requested, 600.0))


def _provider_rate_limit_cooldown_setting(provider_id: str = "") -> str:
    provider_key = _provider_worker_env_key(provider_id)
    if provider_key:
        raw = os.environ.get(f"OES_MULTI_AI_RATE_LIMIT_COOLDOWN_SECONDS_{provider_key}", "").strip()
        if raw:
            return raw
    mapped = _mapped_provider_setting("OES_MULTI_AI_RATE_LIMIT_COOLDOWN_SECONDS_BY_PROVIDER", provider_id)
    if mapped:
        return mapped
    return os.environ.get("OES_MULTI_AI_RATE_LIMIT_COOLDOWN_SECONDS", "").strip()


def _chunk_worker_count_setting(provider_id: str = "") -> str:
    provider_key = _provider_worker_env_key(provider_id)
    if provider_key:
        raw = os.environ.get(f"OES_MULTI_AI_CHUNK_MAX_WORKERS_{provider_key}", "").strip()
        if raw:
            return raw
    mapped = _mapped_chunk_worker_count(provider_id)
    if mapped:
        return mapped
    return os.environ.get("OES_MULTI_AI_CHUNK_MAX_WORKERS", "").strip()


def _mapped_chunk_worker_count(provider_id: str) -> str:
    return _mapped_provider_setting("OES_MULTI_AI_CHUNK_MAX_WORKERS_BY_PROVIDER", provider_id)


def _mapped_provider_setting(env_key: str, provider_id: str) -> str:
    provider_key = _provider_worker_env_key(provider_id)
    if not provider_key:
        return ""
    raw_map = os.environ.get(env_key, "").strip()
    if not raw_map:
        return ""
    normalized_provider_id = _normalize_provider_worker_key(provider_id)
    for entry in raw_map.split(","):
        if not entry.strip() or "=" not in entry:
            continue
        key, value = entry.split("=", 1)
        normalized_key = _normalize_provider_worker_key(key)
        if normalized_key in {provider_key, normalized_provider_id}:
            return value.strip()
    return ""


def _provider_worker_env_key(provider_id: str) -> str:
    normalized = _normalize_provider_worker_key(provider_id)
    return normalized.strip("_")


def _normalize_provider_worker_key(value: str) -> str:
    return "".join(char if char.isalnum() else "_" for char in str(value).upper())


def _bundle_for_evidence_chunk(
    bundle: dict[str, Any],
    *,
    evidence_items: list[dict[str, Any]],
    chunk_index: int,
    total_chunks: int,
    provider_id: str = "",
    parent_chunk_id: str = "",
    subchunk_index: int = 0,
    subchunk_count: int = 0,
    adaptive_retry: bool = False,
) -> dict[str, Any]:
    chunk = dict(bundle)
    chunk["db_corpus_coverage"] = _model_db_corpus_coverage(bundle.get("db_corpus_coverage") or {})
    evidence_ids = _evidence_item_ids(evidence_items)
    chunk_manifest = _chunk_manifest_from_items(
        evidence_items,
        chunk_index=chunk_index,
        total_chunks=total_chunks,
        full_evidence_item_count=len([row for row in bundle.get("evidence_items") or [] if isinstance(row, dict)]),
        provider_id=provider_id,
        parent_chunk_id=parent_chunk_id,
        subchunk_index=subchunk_index,
        subchunk_count=subchunk_count,
        adaptive_retry=adaptive_retry,
    )
    chunk["evidence_items"] = evidence_items
    refs = bundle.get("evidence_refs") if isinstance(bundle.get("evidence_refs"), dict) else {}
    chunk["evidence_refs"] = {
        evidence_id: refs.get(evidence_id) or item
        for evidence_id, item in zip(evidence_ids, evidence_items)
    }
    chunk["log_patterns"] = _patterns_from_evidence_items(evidence_items)
    chunk["logs"] = evidence_items[: min(len(evidence_items), 20)]
    for key in ("metric_windows", "operational_evidence", "evidence_signals", "signals", "candidate_targets", "normalized_events"):
        chunk[key] = _rows_for_evidence_ids(bundle.get(key) or [], evidence_ids=evidence_ids)
    chunk["full_corpus_chunk"] = chunk_manifest
    policy = dict(chunk.get("model_input_policy") or {})
    policy["full_corpus_chunking"] = chunk["full_corpus_chunk"]
    policy["row_level_coverage"] = {
        "raw_rows_sent_to_providers": False,
        "row_assignments_in_prompt": False,
        "boundary": "db_row_to_evidence_item_to_review_chunk",
    }
    chunk["model_input_policy"] = policy
    prompt_hash = sha256_json(_model_input(chunk))
    chunk["full_corpus_chunk"]["provider_prompt_sha256"] = prompt_hash
    chunk["full_corpus_chunk"]["provider_prompt_hash_scope"] = "model_input_before_provider_prompt_sha_field"
    chunk["model_input_policy"]["full_corpus_chunking"] = chunk["full_corpus_chunk"]
    return chunk


def _chunk_manifest_from_items(
    evidence_items: list[dict[str, Any]],
    *,
    chunk_index: int,
    total_chunks: int,
    full_evidence_item_count: int,
    provider_id: str = "",
    parent_chunk_id: str = "",
    subchunk_index: int = 0,
    subchunk_count: int = 0,
    adaptive_retry: bool = False,
) -> dict[str, Any]:
    evidence_ids = _evidence_item_ids(evidence_items)
    chunk_type = _chunk_type_for_evidence_items(evidence_items)
    coverage_classes = _coverage_classes_for_items(evidence_items)
    semantic_keys = _semantic_keys_for_items(evidence_items)
    chunk_id = f"chunk-{chunk_type.replace('_', '-')}-{chunk_index:03d}"
    if parent_chunk_id and subchunk_index:
        chunk_id = f"{parent_chunk_id}-retry-{subchunk_index:03d}"
    token_budget = _chunk_target_tokens(provider_id)
    return {
        "schema_version": "multi_ai_full_corpus_chunk.v1",
        "mode": "full_evidence_item_chunking",
        "chunk_id": chunk_id,
        "chunk_index": chunk_index,
        "chunk_count": total_chunks,
        "chunk_type": chunk_type,
        "semantic_keys": semantic_keys,
        "chunk_size": _evidence_chunk_size(provider_id),
        "token_budget": token_budget,
        "estimated_input_tokens": _estimated_chunk_input_tokens(evidence_items),
        "packing_strategy": "semantic_bucket_token_budget" if token_budget > 0 else "item_count",
        "provider_id": provider_id,
        "parent_chunk_id": parent_chunk_id,
        "subchunk_index": subchunk_index,
        "subchunk_count": subchunk_count,
        "adaptive_retry": adaptive_retry,
        "evidence_item_count": len(evidence_items),
        "evidence_ids": evidence_ids,
        "evidence_ids_sha256": sha256_json(evidence_ids),
        "source_log_count": _source_log_count_for_items(evidence_items),
        "time_range": _time_range_for_items(evidence_items),
        "coverage_classes": coverage_classes,
        "coverage_class_counts": _coverage_class_counts_for_items(evidence_items),
        "full_evidence_item_count": full_evidence_item_count,
        "provider_prompt_sha256": "",
        "provider_prompt_hash_scope": "",
    }


def _chunk_type_for_evidence_items(items: list[dict[str, Any]]) -> str:
    classes = _coverage_classes_for_items(items)
    types = {str(item.get("type") or "unknown") for item in items}
    if not items:
        return "empty"
    if len(classes) == 1:
        coverage_class = classes[0]
        if coverage_class in {"rare", "singleton"}:
            return "rare_singleton"
        return coverage_class
    if {"rare", "singleton"}.intersection(classes):
        return "rare_singleton"
    if types == {"metric_window"}:
        return "temporal_bucket"
    if types == {"operational_evidence"}:
        return "state_transition"
    return "mixed_evidence"


def _coverage_classes_for_items(items: list[dict[str, Any]]) -> list[str]:
    classes: list[str] = []
    for item in items:
        coverage_class = str(item.get("coverage_class") or "").strip()
        if not coverage_class:
            coverage_class = _fallback_coverage_class(item)
        if coverage_class not in classes:
            classes.append(coverage_class)
    return classes


def _coverage_class_counts_for_items(items: list[dict[str, Any]]) -> dict[str, int]:
    counts = Counter(_fallback_coverage_class(item) if not str(item.get("coverage_class") or "").strip() else str(item.get("coverage_class") or "").strip() for item in items)
    return dict(sorted(counts.items()))


def _fallback_coverage_class(item: dict[str, Any]) -> str:
    item_type = str(item.get("type") or "")
    if item_type == "metric_window":
        return "temporal_bucket"
    if item_type == "operational_evidence":
        return "state_transition"
    count = int(item.get("source_log_count") or item.get("count") or 0)
    if count == 1:
        return "singleton"
    if count <= 3:
        return "rare"
    return "pattern"


def _source_log_count_for_items(items: list[dict[str, Any]]) -> int:
    total = 0
    for item in items:
        coverage = item.get("db_row_coverage") if isinstance(item.get("db_row_coverage"), dict) else {}
        total += int(item.get("source_log_count") or coverage.get("covered_log_count") or item.get("count") or 0)
    return total


def _time_range_for_items(items: list[dict[str, Any]]) -> dict[str, str]:
    starts = sorted(str(item.get("first_seen") or "") for item in items if str(item.get("first_seen") or "").strip())
    ends = sorted(str(item.get("last_seen") or "") for item in items if str(item.get("last_seen") or "").strip())
    return {
        "start": starts[0] if starts else "",
        "end": ends[-1] if ends else "",
    }


def _rows_for_evidence_ids(rows: Any, *, evidence_ids: list[str]) -> list[dict[str, Any]]:
    if not isinstance(rows, list) or not evidence_ids:
        return []
    evidence_id_set = set(evidence_ids)
    selected: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        refs = set(_row_evidence_refs(row))
        if refs and refs.intersection(evidence_id_set):
            selected.append(row)
    return selected


def _row_evidence_refs(row: dict[str, Any]) -> list[str]:
    refs: list[str] = []
    for key in ("evidence_id", "id", "pattern_id", "metric_window_id", "signal_id", "target_id", "request_id"):
        value = str(row.get(key) or "")
        if value:
            refs.append(value)
    for key in ("evidence_refs", "evidence_ids", "counter_evidence_refs"):
        refs.extend(_string_list(row.get(key)))
    return _unique(refs)


def _evidence_item_ids(items: list[dict[str, Any]]) -> list[str]:
    ids: list[str] = []
    for index, item in enumerate(items, start=1):
        ids.append(str(item.get("evidence_id") or item.get("id") or f"EVIDENCE-{index:03d}"))
    return ids


def _merge_chunked_provider_envelopes(
    bundle: dict[str, Any],
    provider: ModelProvider,
    preflight: SafetyPreflightResult,
    child_envelopes: list[_ArtifactEnvelope],
    *,
    chunk_count: int,
) -> _ArtifactEnvelope:
    parsed_payload = _merge_chunk_claim_payloads(provider, child_envelopes, chunk_count=chunk_count)
    aggregate_raw_output = json.dumps(parsed_payload, ensure_ascii=False, sort_keys=True)
    output_parse = parse_model_output(aggregate_raw_output)
    schema_valid, schema_errors = validate_claim_result(parsed_payload)
    child_statuses = [str(envelope.artifact.get("status") or "") for envelope in child_envelopes]
    child_schema_valid = [bool(envelope.artifact.get("schema_valid")) for envelope in child_envelopes]
    valid_chunk_count = sum(
        1
        for envelope in child_envelopes
        if str(envelope.artifact.get("status") or "") == "ok"
        and envelope.artifact.get("schema_valid") is True
    )
    partial_chunk_result_usable = _partial_chunk_result_usable(
        valid_chunk_count=valid_chunk_count,
        chunk_count=chunk_count,
        schema_valid=schema_valid,
    )
    if child_statuses and all(status == "skipped_not_configured" for status in child_statuses):
        status = "skipped_not_configured"
    elif child_statuses and all(status == "ok" for status in child_statuses) and all(child_schema_valid) and schema_valid:
        status = "ok"
    elif partial_chunk_result_usable:
        status = "ok"
    else:
        status = "failed"
    response = ModelResponse(
        provider=provider.provider,
        model_name=provider.model_name,
        prompt_name=provider.prompt_name,
        temperature=provider.temperature,
        raw_output=aggregate_raw_output,
        latency_ms=sum(int(envelope.artifact.get("latency_ms") or 0) for envelope in child_envelopes),
        input_tokens=sum(int(envelope.artifact.get("input_tokens") or 0) for envelope in child_envelopes),
        output_tokens=sum(int(envelope.artifact.get("output_tokens") or 0) for envelope in child_envelopes),
        status=status,
    )
    aggregate_cost = _sum_child_costs(child_envelopes)
    retry = _aggregate_retry_metadata(child_envelopes, status=status)
    schema_repair_rules = tuple(
        _unique(
            f"chunk_{index}:{rule}"
            for index, envelope in enumerate(child_envelopes, start=1)
            for rule in envelope.artifact.get("repair_rules") or []
            if str(rule).strip()
        )
    )
    all_schema_errors = schema_errors if status == "ok" else (*schema_errors, *_chunk_failure_errors(child_envelopes))
    artifact = _artifact_from_response(
        bundle,
        response,
        status=status,
        latency_ms=response.latency_ms,
        parsed_payload=parsed_payload,
        parsed_result=_parsed_result_payload(parsed_payload) if schema_valid and status == "ok" else _empty_parsed_result(),
        schema_valid=schema_valid and status == "ok",
        schema_errors=all_schema_errors,
        preflight=preflight,
        failure_reason="" if status == "ok" else _chunked_failure_reason(child_envelopes, schema_errors),
        retry=retry,
        cost_estimate=aggregate_cost,
        output_parse=output_parse,
        schema_repair_rules=schema_repair_rules,
    )
    coverage = _full_corpus_coverage_summary(bundle, chunk_count=chunk_count, provider_id=provider.provider)
    artifact["full_corpus_coverage"] = coverage
    artifact.setdefault("model_input_context", {})["full_corpus_coverage"] = coverage
    artifact["chunk_results"] = _chunk_result_summaries(child_envelopes)
    artifact["chunk_status_counts"] = _chunk_status_counts(child_envelopes)
    artifact["chunk_failure_count"] = sum(
        count
        for status_key, count in artifact["chunk_status_counts"].items()
        if status_key not in {"ok", "skipped_not_configured"}
    )
    artifact["partial_chunk_result"] = {
        "usable": bool(partial_chunk_result_usable),
        "policy": "schema_valid_success_chunks_are_usable_when_success_ratio_meets_threshold",
        "success_chunk_count": valid_chunk_count,
        "total_chunk_count": int(chunk_count),
        "success_ratio": round(valid_chunk_count / chunk_count, 6) if chunk_count else 1.0,
        "min_success_ratio": PARTIAL_CHUNK_SUCCESS_MIN_RATIO,
        "failure_count": int(artifact["chunk_failure_count"]),
    }
    _annotate_execution_status(artifact)
    return _ArtifactEnvelope(
        artifact=artifact,
        raw_output=aggregate_raw_output,
        parsed_payload=parsed_payload,
        output_parse=output_parse,
    )


def _partial_chunk_result_usable(*, valid_chunk_count: int, chunk_count: int, schema_valid: bool) -> bool:
    if not schema_valid or chunk_count <= 1:
        return False
    if valid_chunk_count >= chunk_count:
        return True
    if valid_chunk_count <= 0:
        return False
    return (valid_chunk_count / chunk_count) >= PARTIAL_CHUNK_SUCCESS_MIN_RATIO


def _merge_chunk_claim_payloads(
    provider: ModelProvider,
    child_envelopes: list[_ArtifactEnvelope],
    *,
    chunk_count: int,
) -> dict[str, Any]:
    claims: list[dict[str, Any]] = []
    propositions: list[dict[str, Any]] = []
    summaries: list[str] = []
    seen_claims: set[str] = set()
    for ordinal, envelope in enumerate(child_envelopes, start=1):
        artifact = envelope.artifact
        if str(artifact.get("status") or "") != "ok" or artifact.get("schema_valid") is not True:
            continue
        chunk = _artifact_chunk_manifest(artifact)
        index = int(chunk.get("chunk_index") or ordinal)
        chunk_id = str(chunk.get("chunk_id") or f"chunk-{index:03d}")
        parsed = envelope.parsed_payload if isinstance(envelope.parsed_payload, dict) else {}
        summary = str(parsed.get("summary") or "").strip()
        if summary:
            summaries.append(f"chunk {index}: {summary}")
        for claim in parsed.get("claims") or []:
            if not isinstance(claim, dict):
                continue
            annotated = dict(claim)
            annotated["source_chunk_index"] = index
            annotated["source_chunk_id"] = chunk_id
            annotated["source_chunk_type"] = str(chunk.get("chunk_type") or "")
            annotated["source_chunk_count"] = chunk_count
            if chunk.get("parent_chunk_id"):
                annotated["source_parent_chunk_id"] = str(chunk.get("parent_chunk_id") or "")
                annotated["source_subchunk_id"] = chunk_id
                annotated["source_subchunk_index"] = int(chunk.get("subchunk_index") or ordinal)
                annotated["source_subchunk_count"] = int(chunk.get("subchunk_count") or 0)
            key = sha256_json(
                {
                    "claim_type": annotated.get("claim_type"),
                    "claim_text": annotated.get("claim_text"),
                    "evidence_refs": sorted(_string_list(annotated.get("evidence_refs"))),
                    "counter_evidence_refs": sorted(_string_list(annotated.get("counter_evidence_refs"))),
                }
            )
            if key in seen_claims:
                continue
            seen_claims.add(key)
            claims.append(annotated)
        for proposition in parsed.get("propositions") or []:
            if isinstance(proposition, dict):
                annotated_proposition = dict(proposition)
                annotated_proposition["source_chunk_index"] = index
                annotated_proposition["source_chunk_id"] = chunk_id
                annotated_proposition["source_chunk_type"] = str(chunk.get("chunk_type") or "")
                if chunk.get("parent_chunk_id"):
                    annotated_proposition["source_parent_chunk_id"] = str(chunk.get("parent_chunk_id") or "")
                    annotated_proposition["source_subchunk_id"] = chunk_id
                    annotated_proposition["source_subchunk_index"] = int(chunk.get("subchunk_index") or ordinal)
                    annotated_proposition["source_subchunk_count"] = int(chunk.get("subchunk_count") or 0)
                propositions.append(annotated_proposition)
    claims = sorted(
        claims,
        key=lambda row: (
            int(row.get("source_chunk_index") or 0),
            str(row.get("source_chunk_id") or ""),
            str(row.get("claim_type") or ""),
            str(row.get("claim_text") or ""),
            sorted(_string_list(row.get("evidence_refs"))),
            sorted(_string_list(row.get("counter_evidence_refs"))),
        ),
    )
    propositions = sorted(
        propositions,
        key=lambda row: (
            int(row.get("source_chunk_index") or 0),
            str(row.get("source_chunk_id") or ""),
            str(row.get("proposition_id") or ""),
            str(row.get("text") or row.get("claim_text") or row.get("summary") or ""),
        ),
    )
    return {
        "schema_version": "claim-result/v1",
        "agent_role": f"{provider.provider}_full_corpus_chunk_merger",
        "finding_status": "supported" if claims else "insufficient_evidence",
        "summary": (
            f"{provider.provider} analyzed the complete Evidence Item corpus in {chunk_count} chunk(s). "
            + (" ".join(summaries[:3]) if summaries else "No schema-valid chunk claim was returned.")
        ),
        "claims": claims,
        "propositions": propositions,
        "chunk_coverage": {
            "schema_version": "multi_ai_chunk_coverage.v1",
            "mode": "full_evidence_item_chunking",
            "chunk_count": chunk_count,
            "schema_valid_chunk_count": sum(
                1
                for envelope in child_envelopes
                if str(envelope.artifact.get("status") or "") == "ok"
                and envelope.artifact.get("schema_valid") is True
            ),
            "chunk_status_counts": _chunk_status_counts(child_envelopes),
            "failed_chunk_count": sum(
                1
                for envelope in child_envelopes
                if _artifact_execution_status(envelope.artifact) not in {"ok", "skipped_not_configured"}
            ),
        },
    }


def _sum_child_costs(child_envelopes: list[_ArtifactEnvelope]) -> dict[str, Any]:
    input_tokens = sum(int(envelope.artifact.get("input_tokens") or 0) for envelope in child_envelopes)
    output_tokens = sum(int(envelope.artifact.get("output_tokens") or 0) for envelope in child_envelopes)
    estimated = 0.0
    priced = 0
    for envelope in child_envelopes:
        cost = envelope.artifact.get("cost_estimate") if isinstance(envelope.artifact.get("cost_estimate"), dict) else {}
        estimated += float(cost.get("estimated_cost_usd") or 0.0)
        if cost.get("pricing_source") == "env":
            priced += 1
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "estimated_cost_usd": round(estimated, 8),
        "priced_run_count": priced,
        "pricing_source": "env" if priced else "not_configured",
    }


def _aggregate_retry_metadata(child_envelopes: list[_ArtifactEnvelope], *, status: str) -> dict[str, Any]:
    retries = [
        envelope.artifact.get("retry")
        for envelope in child_envelopes
        if isinstance(envelope.artifact.get("retry"), dict)
    ]
    failures = _unique(str(retry.get("failure_reason") or "") for retry in retries if retry.get("failure_reason"))
    return {
        "attempts": sum(int(retry.get("attempts") or 0) for retry in retries),
        "max_attempts": sum(int(retry.get("max_attempts") or 0) for retry in retries),
        "retried": any(bool(retry.get("retried")) for retry in retries),
        "retryable": any(bool(retry.get("retryable")) for retry in retries),
        "failure_reason": "; ".join(failures) if status != "ok" else "",
        "exception_type": "; ".join(
            _unique(str(retry.get("exception_type") or "") for retry in retries if retry.get("exception_type"))
        ),
    }


def _chunk_failure_errors(child_envelopes: list[_ArtifactEnvelope]) -> tuple[str, ...]:
    errors: list[str] = []
    for index, envelope in enumerate(child_envelopes, start=1):
        artifact = envelope.artifact
        if str(artifact.get("status") or "") == "ok" and artifact.get("schema_valid") is True:
            continue
        reason = _artifact_execution_status(artifact)
        message = _artifact_failure_message(artifact)
        if message:
            reason = f"{reason}: {message}"
        errors.append(f"chunk {index}: {reason}")
    return tuple(errors)


def _chunked_failure_reason(child_envelopes: list[_ArtifactEnvelope], schema_errors: tuple[str, ...]) -> str:
    counts = _chunk_status_counts(child_envelopes)
    failures = {status: count for status, count in counts.items() if status not in {"ok", "skipped_not_configured"}}
    if failures:
        if len(failures) == 1:
            return f"chunked_full_corpus_{next(iter(failures))}"
        return "chunked_full_corpus_mixed_failures"
    if schema_errors:
        return "chunked_full_corpus_schema_validation_failed"
    return "chunked_full_corpus_provider_failed"


def _chunk_result_summaries(child_envelopes: list[_ArtifactEnvelope]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for index, envelope in enumerate(child_envelopes, start=1):
        artifact = envelope.artifact
        chunk = _artifact_chunk_manifest(artifact)
        execution_status = _artifact_execution_status(artifact)
        retry = artifact.get("retry") if isinstance(artifact.get("retry"), dict) else {}
        failure_message = _artifact_failure_message(artifact)
        rows.append(
            {
                "chunk_id": str(chunk.get("chunk_id") or f"chunk-{index:03d}"),
                "chunk_index": index,
                "chunk_type": str(chunk.get("chunk_type") or ""),
                "semantic_keys": list((chunk or {}).get("semantic_keys") or []),
                "status": str(artifact.get("status") or ""),
                "execution_status": execution_status,
                "last_error_type": "" if execution_status == "ok" else execution_status,
                "last_error_message": failure_message[:1000],
                "attempt_count": int(retry.get("attempts") or 0),
                "max_attempts": int(retry.get("max_attempts") or 0),
                "retry_after_sec": _retry_after_seconds_from_text(failure_message),
                "schema_valid": bool(artifact.get("schema_valid")),
                "evidence_item_count": int((chunk or {}).get("evidence_item_count") or 0),
                "estimated_input_tokens": int((chunk or {}).get("estimated_input_tokens") or 0),
                "token_budget": int((chunk or {}).get("token_budget") or 0),
                "packing_strategy": str((chunk or {}).get("packing_strategy") or ""),
                "adaptive_retry": bool((chunk or {}).get("adaptive_retry")),
                "adaptive_subchunk_count": len(artifact.get("adaptive_subchunk_results") or []),
                "source_log_count": int((chunk or {}).get("source_log_count") or 0),
                "coverage_classes": list((chunk or {}).get("coverage_classes") or []),
                "time_range": dict((chunk or {}).get("time_range") or {}),
                "provider_prompt_sha256": str((chunk or {}).get("provider_prompt_sha256") or ""),
                "raw_output_sha256": str(artifact.get("raw_output_sha256") or ""),
                "parsed_json_sha256": str(artifact.get("parsed_json_sha256") or ""),
                "input_tokens": int(artifact.get("input_tokens") or 0),
                "output_tokens": int(artifact.get("output_tokens") or 0),
            }
        )
    return rows


def _chunk_status_counts(child_envelopes: list[_ArtifactEnvelope]) -> dict[str, int]:
    counts = Counter(_artifact_execution_status(envelope.artifact) for envelope in child_envelopes)
    return dict(sorted(counts.items()))


def _artifact_chunk_manifest(artifact: dict[str, Any]) -> dict[str, Any]:
    context = artifact.get("model_input_context") if isinstance(artifact.get("model_input_context"), dict) else {}
    coverage = context.get("full_corpus_coverage") if isinstance(context.get("full_corpus_coverage"), dict) else {}
    chunk = coverage.get("chunk") if isinstance(coverage.get("chunk"), dict) else {}
    return chunk


def _full_corpus_coverage_summary(
    bundle: dict[str, Any],
    *,
    chunk_count: int | None = None,
    provider_id: str = "",
) -> dict[str, Any]:
    items = [row for row in bundle.get("evidence_items") or [] if isinstance(row, dict)]
    ids = _evidence_item_ids(items)
    chunk = bundle.get("full_corpus_chunk") if isinstance(bundle.get("full_corpus_chunk"), dict) else {}
    chunk_manifest = _chunk_manifest_rows_for_bundle(bundle, chunk_count=chunk_count, provider_id=provider_id)
    total = int(chunk.get("full_evidence_item_count") or len(items))
    analyzed = len(items)
    if not chunk:
        analyzed = total
    omitted = max(0, total - analyzed if chunk else 0)
    chunks = _evidence_item_chunks(bundle, provider_id=provider_id)
    estimated_tokens = sum(_estimated_chunk_input_tokens(chunk_items) for chunk_items in chunks if chunk_items)
    return {
        "schema_version": "multi_ai_full_corpus_coverage.v1",
        "mode": "full_evidence_item_chunking",
        "packing_strategy": "semantic_bucket_token_budget" if _chunk_target_tokens(provider_id) > 0 else "item_count",
        "provider_id": provider_id,
        "token_budget": _chunk_target_tokens(provider_id),
        "estimated_input_tokens": estimated_tokens,
        "full_evidence_item_count": total,
        "analyzed_evidence_item_count": analyzed,
        "omitted_evidence_item_count": omitted,
        "direct_prompt_evidence_item_count": analyzed,
        "summarized_evidence_item_count": 0,
        "tail_evidence_item_count": 0,
        "unassigned_evidence_item_count": omitted,
        "coverage_ratio": round((analyzed / total), 6) if total else 1.0,
        "chunk_size": _evidence_chunk_size(),
        "chunk_count": int(chunk_count or chunk.get("chunk_count") or max(1, len(chunks))),
        "evidence_ids_sha256": sha256_json(ids),
        "coverage_class_counts": _coverage_class_counts_for_items(items),
        "chunk_manifest_entry_count": len(chunk_manifest),
        "chunk_manifest_sha256": sha256_json(chunk_manifest),
        "boundary_policy": (
            "Every Evidence Item is assigned to a provider review chunk; prompt-excluded row-level tails remain "
            "visible through the DB coverage ledger."
        ),
        "chunk": chunk,
    }


def _chunk_manifest_rows_for_bundle(
    bundle: dict[str, Any],
    *,
    chunk_count: int | None = None,
    provider_id: str = "",
) -> list[dict[str, Any]]:
    chunk = bundle.get("full_corpus_chunk") if isinstance(bundle.get("full_corpus_chunk"), dict) else {}
    if chunk:
        return [_compact_chunk_manifest_row(chunk)]
    chunks = _evidence_item_chunks(bundle, provider_id=provider_id)
    total_chunks = int(chunk_count or len(chunks) or 1)
    full_count = len([row for row in bundle.get("evidence_items") or [] if isinstance(row, dict)])
    rows: list[dict[str, Any]] = []
    for index, items in enumerate(chunks, start=1):
        manifest = _chunk_manifest_from_items(
            items,
            chunk_index=index,
            total_chunks=total_chunks,
            full_evidence_item_count=full_count,
            provider_id=provider_id,
        )
        rows.append(_compact_chunk_manifest_row(manifest))
    return rows


def _compact_chunk_manifest_row(chunk: dict[str, Any]) -> dict[str, Any]:
    return {
        "chunk_id": str(chunk.get("chunk_id") or ""),
        "chunk_index": int(chunk.get("chunk_index") or 0),
        "chunk_type": str(chunk.get("chunk_type") or ""),
        "semantic_keys": list(chunk.get("semantic_keys") or []),
        "token_budget": int(chunk.get("token_budget") or 0),
        "estimated_input_tokens": int(chunk.get("estimated_input_tokens") or 0),
        "packing_strategy": str(chunk.get("packing_strategy") or ""),
        "adaptive_retry": bool(chunk.get("adaptive_retry")),
        "evidence_item_count": int(chunk.get("evidence_item_count") or 0),
        "evidence_ids_sha256": str(chunk.get("evidence_ids_sha256") or ""),
        "source_log_count": int(chunk.get("source_log_count") or 0),
        "time_range": dict(chunk.get("time_range") or {}),
        "coverage_classes": list(chunk.get("coverage_classes") or []),
        "coverage_class_counts": dict(chunk.get("coverage_class_counts") or {}),
        "provider_prompt_sha256": str(chunk.get("provider_prompt_sha256") or ""),
    }


def _model_db_corpus_coverage(coverage: Any) -> dict[str, Any]:
    if not isinstance(coverage, dict):
        return {}
    return {
        "schema_version": str(coverage.get("schema_version") or ""),
        "source_table": str(coverage.get("source_table") or ""),
        "strategy": str(coverage.get("strategy") or ""),
        "total_row_count": int(coverage.get("total_row_count") or 0),
        "covered_row_count": int(coverage.get("covered_row_count") or 0),
        "uncovered_row_count": int(coverage.get("uncovered_row_count") or 0),
        "coverage_ratio": float(coverage.get("coverage_ratio") or 0.0),
        "pattern_count": int(coverage.get("pattern_count") or 0),
        "singleton_pattern_count": int(coverage.get("singleton_pattern_count") or 0),
        "low_frequency_pattern_count": int(coverage.get("low_frequency_pattern_count") or 0),
        "coverage_class_counts": dict(coverage.get("coverage_class_counts") or {}),
        "direct_prompt_row_count": int(coverage.get("direct_prompt_row_count") or 0),
        "raw_rows_sent_to_providers": bool(coverage.get("raw_rows_sent_to_providers")),
        "prompt_boundary_policy": str(coverage.get("prompt_boundary_policy") or ""),
        "row_assignments_sha256": str(coverage.get("row_assignments_sha256") or ""),
        "row_assignments_in_prompt": False,
    }


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
    schema_repair_rules: tuple[str, ...] = (),
) -> dict[str, Any]:
    parsed_json_sha256 = sha256_json(parsed_payload) if parsed_payload else ""
    repair_rules = [
        *list(getattr(output_parse, "repair_rules", ()) or ()),
        *list(schema_repair_rules),
    ]
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
        "repair_applied": bool(getattr(output_parse, "repair_applied", False) or schema_repair_rules),
        "repair_rules": repair_rules,
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
        "model_input_context": _model_input_context_summary(bundle),
        "safety_preflight": {
            "passed": preflight.passed,
            "finding_types": list(preflight.finding_types),
            "failure_reason": preflight.failure_reason,
            "finding_count": preflight.finding_count,
            "raw_logs_sent_to_providers": False,
        },
        "created_at": utc_now(),
    }


def _model_input_context_summary(bundle: dict[str, Any]) -> dict[str, Any]:
    approved_context = (
        bundle.get("approved_profile_context")
        if isinstance(bundle.get("approved_profile_context"), dict)
        else {}
    )
    source_context = (
        bundle.get("source_context_context")
        if isinstance(bundle.get("source_context_context"), dict)
        else {}
    )
    source_analysis = (
        bundle.get("source_analysis_context")
        if isinstance(bundle.get("source_analysis_context"), dict)
        else {}
    )
    return {
        "schema_version": "multi_ai_model_input_context_summary.v1",
        "model_input_sha256": sha256_json(_model_input(bundle)),
        "approved_profile_context_included": bool(approved_context),
        "approved_profile_context_sha256": sha256_json(approved_context) if approved_context else "",
        "profile_id": str(approved_context.get("profile_id") or ""),
        "profile_status": str(approved_context.get("profile_status") or ""),
        "confidence_action": str(approved_context.get("confidence_action") or ""),
        "confirmed_user_outcomes": list(approved_context.get("confirmed_user_outcomes") or []),
        "provisional_user_outcomes": list(approved_context.get("provisional_user_outcomes") or []),
        "human_questions": list(approved_context.get("human_questions") or [])[:8],
        "source_context_included": bool(source_context),
        "source_context_sha256": str(source_context.get("source_context_sha256") or ""),
        "source_analysis_included": bool(source_analysis),
        "source_analysis_sha256": str(source_analysis.get("analysis_sha256") or ""),
        "raw_source_sent_to_providers": False,
        "raw_logs_sent_to_providers": False,
        "context_is_not_incident_evidence": True,
        "db_corpus_coverage": _model_db_corpus_coverage(bundle.get("db_corpus_coverage") or {}),
        "full_corpus_coverage": _full_corpus_coverage_summary(bundle),
    }


def _normalize_claim_result_payload(
    payload: dict[str, Any],
    *,
    known_refs: set[str],
) -> tuple[dict[str, Any], tuple[str, ...]]:
    claims = payload.get("claims")
    if not isinstance(claims, list):
        return payload, ()

    changed = False
    normalized_claims: list[Any] = []
    rules: list[str] = []
    for index, claim in enumerate(claims):
        if not isinstance(claim, dict):
            normalized_claims.append(claim)
            continue
        refs = _string_list(claim.get("evidence_refs"))
        if refs:
            normalized_claims.append(claim)
            continue
        inferred_refs = _extract_known_evidence_refs_from_claim(claim, known_refs=known_refs)
        if not inferred_refs:
            normalized_claims.append(claim)
            continue
        updated = dict(claim)
        updated["evidence_refs"] = inferred_refs
        normalized_claims.append(updated)
        changed = True
        rules.append(f"evidence_refs_from_summary:{index}:{len(inferred_refs)}")

    if not changed:
        return payload, ()
    normalized = dict(payload)
    normalized["claims"] = normalized_claims
    return normalized, tuple(rules)


def _extract_known_evidence_refs_from_claim(claim: dict[str, Any], *, known_refs: set[str]) -> list[str]:
    texts: list[str] = [str(claim.get("claim_text") or "")]
    for key in ("evidence_summary", "counter_evidence_summary", "missing_evidence", "caveats"):
        texts.extend(_string_list(claim.get(key)))
    refs: list[str] = []
    for text in texts:
        for match in _EVIDENCE_REF_RE.findall(text):
            if match in known_refs and match not in refs:
                refs.append(match)
    return refs


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
    _annotate_execution_status(artifact)
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
        "db_corpus_coverage": _model_db_corpus_coverage(bundle.get("db_corpus_coverage") or {}),
        "full_corpus_coverage": _full_corpus_coverage_summary(bundle),
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
        "db_corpus_coverage": _model_db_corpus_coverage(bundle.get("db_corpus_coverage") or {}),
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
        "sqlite_db_file_sent_to_providers": False,
        "db_row_assignments_sent_to_providers": False,
        "db_corpus_coverage_summary_included": bool(bundle.get("db_corpus_coverage")),
        "support_claims_must_cite_evidence_id": True,
        "score_note": SCORE_NOTE,
        "policy_text": (
            "Raw logs are never sent to providers. Source Context and Source Analysis are context, "
            "not incident evidence. Support claims about runtime behavior must cite Evidence Items with evidence_id."
        ),
    }


def _approved_profile_context(profile: dict[str, Any]) -> dict[str, Any]:
    return build_approved_profile_model_context(profile)


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
        target_explanation = _target_explanation_from_claims(claims, rows, review_mode="")
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
            "target_explanation": target_explanation,
            "suspected_issue": target_explanation.get("suspected_issue", ""),
            "operational_mechanism": target_explanation.get("operational_mechanism", ""),
            "why_it_matters": target_explanation.get("why_it_matters", ""),
            "evidence_summary": target_explanation.get("evidence_summary", []),
            "counter_evidence_summary": target_explanation.get("counter_evidence_summary", []),
            "why_not_promoted": target_explanation.get("why_not_promoted", ""),
            "next_validation_question": target_explanation.get("next_validation_question", ""),
            "claims": [
                {
                    "provider_id": row["provider_id"],
                    "claim_type": str(row["claim"].get("claim_type") or "support"),
                    "claim_text": str(row["claim"].get("claim_text") or ""),
                    "suspected_issue": str(row["claim"].get("suspected_issue") or ""),
                    "operational_mechanism": str(row["claim"].get("operational_mechanism") or ""),
                    "why_it_matters": str(row["claim"].get("why_it_matters") or ""),
                    "evidence_summary": _string_list(row["claim"].get("evidence_summary")),
                    "counter_evidence_summary": _string_list(row["claim"].get("counter_evidence_summary")),
                    "why_not_promoted": str(row["claim"].get("why_not_promoted") or ""),
                    "next_validation_question": str(row["claim"].get("next_validation_question") or ""),
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
    target_explanation = dict(group.get("target_explanation") or {})
    target_explanation.setdefault("review_mode", review_mode)
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
        "target_explanation": target_explanation,
        "suspected_issue": str(group.get("suspected_issue") or target_explanation.get("suspected_issue") or ""),
        "operational_mechanism": str(group.get("operational_mechanism") or target_explanation.get("operational_mechanism") or ""),
        "why_it_matters": str(group.get("why_it_matters") or target_explanation.get("why_it_matters") or ""),
        "evidence_summary": _string_list(group.get("evidence_summary") or target_explanation.get("evidence_summary")),
        "counter_evidence_summary": _string_list(
            group.get("counter_evidence_summary") or target_explanation.get("counter_evidence_summary")
        ),
        "why_not_promoted": str(group.get("why_not_promoted") or target_explanation.get("why_not_promoted") or ""),
        "next_validation_question": str(
            group.get("next_validation_question") or target_explanation.get("next_validation_question") or ""
        ),
        "score_note": SCORE_NOTE,
    }


def _target_explanation_from_claims(
    claims: list[dict[str, Any]],
    rows: list[dict[str, Any]],
    *,
    review_mode: str,
) -> dict[str, Any]:
    suspected_issue = _first_claim_text(claims, "suspected_issue") or _first_claim_text(claims, "claim_text")
    operational_mechanism = _first_claim_text(claims, "operational_mechanism")
    why_it_matters = _first_claim_text(claims, "why_it_matters")
    why_not_promoted = _first_claim_text(claims, "why_not_promoted")
    next_validation_question = _first_claim_text(claims, "next_validation_question")
    evidence_summary = _unique(
        item
        for claim in claims
        for item in _string_list(claim.get("evidence_summary"))
    )
    counter_evidence_summary = _unique(
        item
        for claim in claims
        for item in _string_list(claim.get("counter_evidence_summary"))
    )
    if not evidence_summary:
        evidence_summary = [
            f"{ref}: cited by provider output; inspect the Evidence Item to confirm what it shows."
            for ref in _unique(ref for claim in claims for ref in _string_list(claim.get("evidence_refs")))[:8]
        ]
    provider_explanations = []
    for row in rows:
        claim = row["claim"]
        provider_explanations.append(
            {
                "provider_id": row["provider_id"],
                "claim_type": str(claim.get("claim_type") or "support"),
                "claim_text": str(claim.get("claim_text") or ""),
                "suspected_issue": str(claim.get("suspected_issue") or ""),
                "operational_mechanism": str(claim.get("operational_mechanism") or ""),
                "why_it_matters": str(claim.get("why_it_matters") or ""),
                "why_not_promoted": str(claim.get("why_not_promoted") or ""),
                "next_validation_question": str(claim.get("next_validation_question") or ""),
                "evidence_refs": _string_list(claim.get("evidence_refs")),
            }
        )
    return {
        "schema_version": "target_explanation.v1",
        "review_mode": review_mode,
        "suspected_issue": suspected_issue,
        "operational_mechanism": operational_mechanism,
        "why_it_matters": why_it_matters,
        "evidence_summary": evidence_summary,
        "counter_evidence_summary": counter_evidence_summary,
        "why_not_promoted": why_not_promoted,
        "next_validation_question": next_validation_question,
        "provider_explanations": provider_explanations,
    }


def _first_claim_text(claims: list[dict[str, Any]], key: str) -> str:
    for claim in claims:
        text = str(claim.get(key) or "").strip()
        if text:
            return text
    return ""


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
    execution_status = _artifact_execution_status(run)
    return {
        "provider_id": run.get("provider_id"),
        "display_name": run.get("display_name"),
        "model_name": run.get("model_name"),
        "status": run.get("status"),
        "execution_status": execution_status,
        "failure_is_not_silent": execution_status in CHUNK_FAILURE_STATUSES,
        "latency_ms": run.get("latency_ms"),
        "input_tokens": run.get("input_tokens"),
        "output_tokens": run.get("output_tokens"),
        "raw_output_sha256": run.get("raw_output_sha256"),
        "parsed_json_sha256": run.get("parsed_json_sha256"),
        "schema_valid": run.get("schema_valid"),
        "schema_errors": run.get("schema_errors") or [],
        "failure_reason": run.get("failure_reason") or "",
        "retry": run.get("retry") or {},
        "chunk_status_counts": dict(run.get("chunk_status_counts") or {}),
        "chunk_failure_count": int(run.get("chunk_failure_count") or 0),
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


def _artifact_execution_status(artifact: dict[str, Any]) -> str:
    status = str(artifact.get("status") or "").strip()
    schema_valid = bool(artifact.get("schema_valid"))
    if status == "ok" and schema_valid:
        return "ok"
    if status == "skipped_not_configured":
        return "skipped_not_configured"

    text = _artifact_failure_text(artifact)
    if status == "blocked_by_safety_preflight" or _text_has_any(text, ("safety", "filter", "blocked")):
        return "safety_filter"
    if _text_has_any(text, ("context length", "context_length", "maximum context", "max context", "token limit")):
        return "context_length"
    if _text_has_any(text, ("429", "rate limit", "rate_limit", "quota", "resource exhausted", "throttle")):
        return "rate_limited"
    if status == "timeout" or _text_has_any(text, ("timeout", "timed out", "deadline")):
        return "timeout"
    if _text_has_any(text, ("schema invalid", "schema_invalid", "schema validation", "schema_validation")):
        return "schema_invalid"
    if status == "ok" and not schema_valid:
        parse_status = str(artifact.get("parse_status") or "")
        if parse_status in {"invalid_json", "invalid_after_repair"}:
            return "deterministic_parse_failure"
        return "schema_invalid"
    if _text_has_any(text, ("empty response", "empty_response", "no response")):
        return "empty_response"

    retry = artifact.get("retry") if isinstance(artifact.get("retry"), dict) else {}
    attempts = int(retry.get("attempts") or 0)
    max_attempts = int(retry.get("max_attempts") or 0)
    if attempts and max_attempts and attempts >= max_attempts and bool(retry.get("retryable")):
        return "retry_exhausted"
    if status in {"failed", "error"}:
        return "provider_error"
    return "provider_error"


def _annotate_execution_status(artifact: dict[str, Any]) -> None:
    execution_status = _artifact_execution_status(artifact)
    artifact["execution_status"] = execution_status
    artifact["failure_is_not_silent"] = execution_status in CHUNK_FAILURE_STATUSES


def _artifact_failure_text(artifact: dict[str, Any]) -> str:
    retry = artifact.get("retry") if isinstance(artifact.get("retry"), dict) else {}
    provider_error = artifact.get("provider_error") if isinstance(artifact.get("provider_error"), dict) else {}
    values: list[str] = [
        str(artifact.get("status") or ""),
        str(artifact.get("failure_reason") or ""),
        str(artifact.get("parse_status") or ""),
        str(retry.get("failure_reason") or ""),
        str(retry.get("exception_type") or ""),
        str(provider_error.get("error_type") or ""),
        str(provider_error.get("failure_reason") or ""),
        str(provider_error.get("message") or ""),
        str(provider_error.get("exception_type") or ""),
    ]
    values.extend(str(item) for item in artifact.get("schema_errors") or [])
    return " ".join(value for value in values if value).casefold()


def _artifact_failure_message(artifact: dict[str, Any]) -> str:
    retry = artifact.get("retry") if isinstance(artifact.get("retry"), dict) else {}
    provider_error = artifact.get("provider_error") if isinstance(artifact.get("provider_error"), dict) else {}
    parts = [
        str(artifact.get("failure_reason") or ""),
        str(artifact.get("parse_status") or ""),
        "; ".join(str(item) for item in artifact.get("schema_errors") or [] if str(item).strip()),
        str(retry.get("failure_reason") or ""),
        str(retry.get("exception_type") or ""),
        str(provider_error.get("error_type") or ""),
        str(provider_error.get("failure_reason") or ""),
        str(provider_error.get("message") or ""),
        str(provider_error.get("exception_type") or ""),
    ]
    return " | ".join(part for part in parts if part)


def _provider_error_detail(payload: dict[str, Any]) -> dict[str, str]:
    return {
        "schema_version": str(payload.get("schema_version") or ""),
        "status": str(payload.get("status") or ""),
        "error_type": str(payload.get("error_type") or ""),
        "failure_reason": str(payload.get("failure_reason") or ""),
        "message": str(payload.get("message") or "")[:1000],
        "exception_type": str(payload.get("exception_type") or ""),
    }


def _retry_after_seconds_from_text(text: str) -> int:
    match = re.search(r"retry[-_\s]*after(?:[_\s]*(?:seconds|sec))?\D{0,8}(\d{1,5})", str(text), re.IGNORECASE)
    if not match:
        return 0
    try:
        return max(0, min(int(match.group(1)), 86_400))
    except ValueError:
        return 0


def _text_has_any(text: str, needles: tuple[str, ...]) -> bool:
    return any(needle in text for needle in needles)


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
