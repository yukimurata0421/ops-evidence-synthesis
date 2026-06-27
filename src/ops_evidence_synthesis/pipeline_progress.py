from __future__ import annotations

import time
import uuid
from typing import Any

from ops_evidence_synthesis.timeutils import utc_now


PIPELINE_STATUS_SCHEMA_VERSION = "pipeline_status.v1"
ANALYSIS_OPERATIONS = ("multi_ai", "synthesis", "model_stage")
CANONICAL_PIPELINE_STATES = (
    "uploaded",
    "validated",
    "safety_passed",
    "providers_scheduled",
    "provider_completed",
    "provider_failed",
    "parse_failed",
    "schema_validated",
    "arbitration_completed",
    "review_targets_persisted",
    "planner_generated",
    "waiting_human_answers",
    "refined_plan_generated",
    "child_bundle_required",
    "completed",
    "blocked",
)
BLOCKING_REASON_CODES = (
    "blocked_by_safety_preflight",
    "provider_not_configured",
    "provider_timeout",
    "parse_failed",
    "schema_invalid",
    "no_claims_extracted",
    "no_planner_answers",
    "human_input_required",
    "child_bundle_missing",
)

OPERATION_STEPS: dict[str, list[tuple[str, str]]] = {
    "bundle_upload": [
        ("bundle_received", "Evidence Bundle received"),
        ("bundle_validated", "Server validation passed"),
        ("bundle_persisted", "Evidence Bundle persisted"),
    ],
    "multi_ai": [
        ("bundle_persisted", "Evidence Bundle persisted"),
        ("bundle_validated", "Evidence Bundle validated"),
        ("model_input_validated", "Safety preflight passed"),
        ("providers_scheduled", "Provider runs scheduled"),
        ("providers_completed", "Provider runs completed"),
        ("outputs_persisted", "AI outputs persisted"),
        ("canonical_graph_resolved", "Canonical review graph resolved"),
        ("review_targets_ready", "Review targets ready"),
    ],
    "synthesis": [
        ("bundle_persisted", "Evidence Bundle persisted"),
        ("providers_scheduled", "Provider runs scheduled"),
        ("providers_completed", "Provider runs completed"),
        ("outputs_parsed", "AI outputs parsed"),
        ("claims_routed", "Claims routed"),
        ("scores_written", "Scores written"),
        ("clusters_built", "Review clusters built"),
        ("review_targets_persisted", "Review targets persisted"),
    ],
    "model_stage": [
        ("providers_scheduled", "Provider runs scheduled"),
        ("providers_completed", "Provider runs completed"),
        ("outputs_parsed", "AI outputs parsed"),
    ],
    "evidence_request_plan": [
        ("planner_input_validated", "Planner input validated"),
        ("canonical_graph_loaded", "Canonical graph loaded"),
        ("planner_answers_received", "Human-question answers received"),
        ("plan_generated", "Evidence request plan generated"),
        ("instructions_rendered", "Collection instructions rendered"),
    ],
    "more_data_refresh": [
        ("more_data_requested", "More data decision recorded"),
        ("child_bundle_created", "Child Evidence Bundle created"),
        ("model_rerun_completed", "Model rerun completed"),
        ("review_history_updated", "Review history updated"),
    ],
    "remote_collect": [
        ("collector_requested", "Host collection requested"),
        ("collector_completed", "Host collection completed"),
        ("child_bundle_created", "Child Evidence Bundle created"),
        ("model_rerun_completed", "Model rerun completed"),
        ("review_history_updated", "Review history updated"),
    ],
    "review_decision": [
        ("decision_received", "Review decision received"),
        ("decision_persisted", "Review decision persisted"),
    ],
}

TERMINAL_STATUSES = {"succeeded", "failed", "blocked", "needs_input"}
COMPLETE_EVENT_STATUSES = {"succeeded", "completed", "skipped"}
FAILED_EVENT_STATUSES = {"failed", "error", "timeout"}
BLOCKED_EVENT_STATUSES = {"blocked", "blocked_by_safety_preflight"}


def start_pipeline_run(
    store: Any,
    *,
    evidence_sha256: str,
    operation: str,
    summary: dict[str, Any] | None = None,
    parent_pipeline_run_id: str | None = None,
    created_at: str | None = None,
) -> str:
    pipeline_run_id = f"pipe-{uuid.uuid4().hex[:20]}"
    now = created_at or utc_now()
    summary_map = dict(summary or {})
    run = {
        "schema_version": PIPELINE_STATUS_SCHEMA_VERSION,
        "pipeline_run_id": pipeline_run_id,
        "evidence_sha256": str(evidence_sha256 or ""),
        "parent_pipeline_run_id": str(parent_pipeline_run_id or summary_map.get("parent_pipeline_run_id") or ""),
        "operation": str(operation or "pipeline"),
        "status": "running",
        "current_step": "started",
        "total_steps": len(OPERATION_STEPS.get(operation, ())),
        "completed_steps": 0,
        "blocking_reason": "",
        "provider_total": int(summary_map.get("provider_total") or summary_map.get("provider_count") or 0),
        "provider_success": int(summary_map.get("provider_success") or 0),
        "provider_failed": int(summary_map.get("provider_failed") or 0),
        "provider_skipped": int(summary_map.get("provider_skipped") or 0),
        "review_target_count": int(summary_map.get("review_target_count") or 0),
        "validation_target_count": int(summary_map.get("validation_target_count") or 0),
        "child_bundle_count": int(summary_map.get("child_bundle_count") or 0),
        "summary": summary_map,
        "error_message": "",
        "created_at": now,
        "updated_at": now,
        "completed_at": "",
    }
    _upsert_run(store, run)
    record_pipeline_event(
        store,
        pipeline_run_id=pipeline_run_id,
        evidence_sha256=evidence_sha256,
        operation=operation,
        step_key="started",
        status="running",
        message="Pipeline run started",
        metadata=summary or {},
        update_run=False,
    )
    return pipeline_run_id


def record_pipeline_event(
    store: Any,
    *,
    pipeline_run_id: str | None,
    evidence_sha256: str,
    operation: str,
    step_key: str,
    status: str = "completed",
    message: str = "",
    metadata: dict[str, Any] | None = None,
    update_run: bool = True,
) -> None:
    if not pipeline_run_id:
        return
    now = utc_now()
    metadata_map = dict(metadata or {})
    normalized_status = normalize_event_status(status)
    reason_code = _reason_code_for_event(
        step_key=str(step_key or "event"),
        status=normalized_status,
        metadata=metadata_map,
        message=message,
    )
    event = {
        "schema_version": PIPELINE_STATUS_SCHEMA_VERSION,
        "event_id": f"pe-{uuid.uuid4().hex[:24]}",
        "pipeline_run_id": str(pipeline_run_id),
        "evidence_sha256": str(evidence_sha256 or ""),
        "operation": str(operation or "pipeline"),
        "event_type": str(metadata_map.get("event_type") or step_key or "event"),
        "stage": str(metadata_map.get("stage") or operation or "pipeline"),
        "step_key": str(step_key or "event"),
        "step_label": step_label(operation, step_key),
        "status": normalized_status,
        "provider_id": _first_text(metadata_map, "provider_id", "provider"),
        "artifact_id": _first_text(metadata_map, "artifact_id", "run_id", "result_id", "pipeline_run_id"),
        "input_sha256": _first_text(metadata_map, "input_sha256", "model_input_sha256"),
        "output_sha256": _first_text(
            metadata_map,
            "output_sha256",
            "raw_output_sha256",
            "repaired_output_sha256",
            "parsed_json_sha256",
            "child_evidence_sha256",
        ),
        "reason_code": reason_code,
        "message": str(message or ""),
        "ordinal": time.time_ns(),
        "metadata": metadata_map,
        "created_at": now,
    }
    _insert_event(store, event)
    if update_run:
        _update_run_from_event(store, event)


def finish_pipeline_run(
    store: Any,
    *,
    pipeline_run_id: str | None,
    evidence_sha256: str,
    operation: str,
    status: str = "succeeded",
    message: str = "",
    metadata: dict[str, Any] | None = None,
) -> None:
    if not pipeline_run_id:
        return
    final_status = normalize_run_status(status)
    step_key = "completed" if final_status == "succeeded" else final_status
    record_pipeline_event(
        store,
        pipeline_run_id=pipeline_run_id,
        evidence_sha256=evidence_sha256,
        operation=operation,
        step_key=step_key,
        status=final_status,
        message=message or f"Pipeline run {final_status}",
        metadata=metadata or {},
        update_run=False,
    )
    run = _get_run(store, pipeline_run_id)
    if not run:
        return
    now = utc_now()
    total = int(run.get("total_steps") or len(OPERATION_STEPS.get(operation, ())) or 0)
    completed = total if final_status == "succeeded" else int(run.get("completed_steps") or 0)
    run.update(
        {
            "status": final_status,
            "current_step": step_key,
            "completed_steps": completed,
            "blocking_reason": "" if final_status == "succeeded" else _reason_code_for_event(
                step_key=step_key,
                status=final_status,
                metadata=metadata or {},
                message=message,
            ),
            "updated_at": now,
            "completed_at": now,
            "error_message": message if final_status in {"failed", "blocked"} else str(run.get("error_message") or ""),
        }
    )
    summary = dict(run.get("summary") or {})
    summary.update(metadata or {})
    run["summary"] = summary
    _apply_summary_counters(run, metadata or {})
    events = _list_events(store, pipeline_run_id)
    if events:
        _apply_event_frontier(run, events)
    effective_status = _status_with_provider_frontier(str(run.get("status") or final_status), run)
    if effective_status != str(run.get("status") or ""):
        run["status"] = effective_status
        run["current_step"] = "failed" if effective_status == "failed" else effective_status
        run["blocking_reason"] = _blocking_reason_with_provider_frontier(effective_status, run)
        run["error_message"] = str(run.get("error_message") or message or "")
    _upsert_run(store, run)


def pipeline_status_from_store(
    store: Any,
    *,
    evidence_sha256: str = "",
    pipeline_run_id: str = "",
) -> dict[str, Any]:
    if hasattr(store, "get_pipeline_status"):
        return store.get_pipeline_status(evidence_sha256=evidence_sha256, pipeline_run_id=pipeline_run_id)
    return empty_pipeline_status(evidence_sha256=evidence_sha256, pipeline_run_id=pipeline_run_id)


def analysis_pipeline_status_from_store(
    store: Any,
    *,
    evidence_sha256: str = "",
) -> dict[str, Any]:
    if not evidence_sha256:
        return pipeline_status_from_store(store, evidence_sha256=evidence_sha256)
    if hasattr(store, "latest_pipeline_run_by_operations"):
        run = store.latest_pipeline_run_by_operations(evidence_sha256, ANALYSIS_OPERATIONS)
        if run:
            events = _list_events(store, str(run.get("pipeline_run_id") or ""))
            return build_pipeline_status(run, events)
    return pipeline_status_from_store(store, evidence_sha256=evidence_sha256)


def empty_pipeline_status(*, evidence_sha256: str = "", pipeline_run_id: str = "") -> dict[str, Any]:
    return {
        "schema_version": PIPELINE_STATUS_SCHEMA_VERSION,
        "pipeline_run_id": str(pipeline_run_id or ""),
        "evidence_sha256": str(evidence_sha256 or ""),
        "parent_pipeline_run_id": "",
        "operation": "",
        "status": "not_started",
        "current_step": "",
        "current_step_label": "",
        "progress_percent": 0,
        "total_steps": 0,
        "completed_steps": 0,
        "blocking_reason": "",
        "provider_total": 0,
        "provider_success": 0,
        "provider_failed": 0,
        "provider_skipped": 0,
        "review_target_count": 0,
        "validation_target_count": 0,
        "child_bundle_count": 0,
        "summary": {},
        "steps": [],
        "events": [],
        "latest_event": {},
    }


def build_pipeline_status(run: dict[str, Any] | None, events: list[dict[str, Any]]) -> dict[str, Any]:
    if not run:
        evidence_sha = str(events[-1].get("evidence_sha256") or "") if events else ""
        return empty_pipeline_status(evidence_sha256=evidence_sha)
    operation = str(run.get("operation") or "")
    run_status = str(run.get("status") or "")
    steps = _step_statuses(operation, events, run_status=run_status)
    total = int(run.get("total_steps") or len([step for step in steps if step.get("known")]) or len(steps))
    completed = int(run.get("completed_steps") or sum(1 for step in steps if step.get("status") in COMPLETE_EVENT_STATUSES))
    if total <= 0:
        progress_percent = 100 if str(run.get("status")) == "succeeded" else 0
    else:
        progress_percent = round(min(100, max(0, completed / total * 100)))
    frontier = _event_frontier(events, run)
    display_status = _status_with_provider_frontier(str(run.get("status") or "running"), frontier)
    if display_status == "succeeded":
        progress_percent = 100
    blocking_reason = _blocking_reason_with_provider_frontier(display_status, frontier)
    state_timeline = _state_timeline(operation, events, run)
    active_reasons = _active_reason_codes(events, blocking_reason)
    current_state = _current_canonical_state(display_status, state_timeline, blocking_reason)
    events_with_state = _events_with_canonical_state(operation, events)
    return {
        "schema_version": PIPELINE_STATUS_SCHEMA_VERSION,
        "pipeline_run_id": str(run.get("pipeline_run_id") or ""),
        "evidence_sha256": str(run.get("evidence_sha256") or ""),
        "parent_pipeline_run_id": str(run.get("parent_pipeline_run_id") or ""),
        "operation": operation,
        "status": display_status,
        "canonical_state": current_state,
        "current_step": str(run.get("current_step") or ""),
        "current_step_label": step_label(operation, str(run.get("current_step") or "")),
        "progress_percent": progress_percent,
        "total_steps": total,
        "completed_steps": completed,
        "blocking_reason": blocking_reason,
        "provider_total": int(frontier.get("provider_total") or 0),
        "provider_success": int(frontier.get("provider_success") or 0),
        "provider_failed": int(frontier.get("provider_failed") or 0),
        "provider_skipped": int(frontier.get("provider_skipped") or 0),
        "review_target_count": int(frontier.get("review_target_count") or 0),
        "validation_target_count": int(frontier.get("validation_target_count") or 0),
        "child_bundle_count": int(frontier.get("child_bundle_count") or 0),
        "summary": dict(run.get("summary") or {}),
        "error_message": str(run.get("error_message") or ""),
        "created_at": str(run.get("created_at") or ""),
        "updated_at": str(run.get("updated_at") or ""),
        "completed_at": str(run.get("completed_at") or ""),
        "steps": steps,
        "events": events_with_state,
        "latest_event": events_with_state[-1] if events_with_state else {},
        "state_timeline": state_timeline,
        "active_reasons": active_reasons,
        "known_states": list(CANONICAL_PIPELINE_STATES),
        "known_blocking_reasons": list(BLOCKING_REASON_CODES),
    }


def normalize_event_status(status: str) -> str:
    text = str(status or "completed").strip().casefold()
    if text in {"ok", "success", "completed", "complete"}:
        return "completed"
    if text in {"succeeded", "failed", "running", "blocked", "needs_input", "skipped", "timeout"}:
        return text
    if text == "blocked_by_safety_preflight":
        return "blocked"
    if text in {"error", "invalid"}:
        return "failed"
    return text or "completed"


def normalize_run_status(status: str) -> str:
    text = normalize_event_status(status)
    if text in {"completed", "ok", "success"}:
        return "succeeded"
    if text in TERMINAL_STATUSES or text == "running":
        return text
    if text in FAILED_EVENT_STATUSES:
        return "failed"
    if text in BLOCKED_EVENT_STATUSES:
        return "blocked"
    return "running"


def step_label(operation: str, step_key: str) -> str:
    key = str(step_key or "")
    for known_key, label in OPERATION_STEPS.get(str(operation or ""), []):
        if known_key == key:
            return label
    fallback = {
        "started": "Pipeline run started",
        "completed": "Pipeline run completed",
        "failed": "Pipeline run failed",
        "blocked": "Pipeline run blocked",
        "needs_input": "User input required",
    }.get(key)
    return fallback or key.replace("_", " ").strip().title()


def _step_statuses(operation: str, events: list[dict[str, Any]], *, run_status: str = "") -> list[dict[str, Any]]:
    latest_by_step: dict[str, dict[str, Any]] = {}
    for event in events:
        latest_by_step[str(event.get("step_key") or "")] = event
    steps: list[dict[str, Any]] = []
    seen: set[str] = set()
    for key, label in OPERATION_STEPS.get(operation, []):
        event = latest_by_step.get(key, {})
        status = str(event.get("status") or "pending")
        if run_status == "succeeded" and status == "running":
            status = "completed"
        steps.append(
            {
                "step_key": key,
                "step_label": label,
                "status": status,
                "canonical_state": _primary_canonical_state(operation, event),
                "message": str(event.get("message") or ""),
                "updated_at": str(event.get("created_at") or ""),
                "reason_code": str(event.get("reason_code") or ""),
                "metadata": dict(event.get("metadata") or {}),
                "known": True,
            }
        )
        seen.add(key)
    for event in events:
        key = str(event.get("step_key") or "")
        if key in seen or key in {"started", "completed"}:
            continue
        steps.append(
            {
                "step_key": key,
                "step_label": str(event.get("step_label") or step_label(operation, key)),
                "status": str(event.get("status") or ""),
                "canonical_state": _primary_canonical_state(operation, event),
                "message": str(event.get("message") or ""),
                "updated_at": str(event.get("created_at") or ""),
                "reason_code": str(event.get("reason_code") or ""),
                "metadata": dict(event.get("metadata") or {}),
                "known": False,
            }
        )
        seen.add(key)
    return steps


def _update_run_from_event(store: Any, event: dict[str, Any]) -> None:
    run = _get_run(store, str(event.get("pipeline_run_id") or ""))
    if not run:
        return
    operation = str(run.get("operation") or event.get("operation") or "")
    event_status = str(event.get("status") or "")
    current_status = str(run.get("status") or "running")
    run_status = current_status if current_status in TERMINAL_STATUSES else "running"
    if event_status in FAILED_EVENT_STATUSES:
        run_status = "failed"
    elif event_status in BLOCKED_EVENT_STATUSES:
        run_status = "blocked"
    elif event_status == "needs_input":
        run_status = "needs_input"
    step_order = [key for key, _label in OPERATION_STEPS.get(operation, [])]
    step_key = str(event.get("step_key") or "")
    completed_steps = int(run.get("completed_steps") or 0)
    if step_key in step_order and event_status in COMPLETE_EVENT_STATUSES:
        completed_steps = max(completed_steps, step_order.index(step_key) + 1)
    summary = dict(run.get("summary") or {})
    metadata = event.get("metadata") if isinstance(event.get("metadata"), dict) else {}
    if metadata:
        summary.update(
            {
                key: value
                for key, value in metadata.items()
                if key.endswith("_count")
                or key
                in {
                    "provider",
                    "provider_count",
                    "provider_total",
                    "provider_success",
                    "provider_failed",
                    "provider_skipped",
                    "status",
                    "pipeline_run_id",
                    "child_evidence_sha256",
                }
            }
        )
    blocking_reason = str(run.get("blocking_reason") or "")
    reason_code = str(event.get("reason_code") or "")
    if event_status in FAILED_EVENT_STATUSES or event_status in BLOCKED_EVENT_STATUSES or event_status == "needs_input":
        blocking_reason = reason_code or event_status
    run.update(
        {
            "status": run_status,
            "current_step": step_key,
            "completed_steps": completed_steps,
            "blocking_reason": blocking_reason,
            "updated_at": str(event.get("created_at") or utc_now()),
            "error_message": str(event.get("message") or "") if run_status in {"failed", "blocked"} else str(run.get("error_message") or ""),
            "summary": summary,
        }
    )
    _apply_summary_counters(run, metadata, event)
    _upsert_run(store, run)


def _first_text(metadata: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = metadata.get(key)
        if value is not None and str(value):
            return str(value)
    return ""


def _reason_code_for_event(*, step_key: str, status: str, metadata: dict[str, Any], message: str = "") -> str:
    for key in ("reason_code", "blocking_reason", "failure_reason"):
        value = metadata.get(key)
        if value:
            return _normalize_reason_code(str(value))
    metadata_status = str(metadata.get("status") or "").casefold()
    parse_status = str(metadata.get("parse_status") or "").casefold()
    schema_valid = metadata.get("schema_valid")
    message_text = str(message or "").casefold()
    if status in FAILED_EVENT_STATUSES and str(step_key or "") == "child_bundle_created":
        return "child_bundle_missing"
    if status in FAILED_EVENT_STATUSES and "child" in message_text and "bundle" in message_text:
        return "child_bundle_missing"
    if status == "needs_input":
        return "no_planner_answers" if str(step_key or "") == "planner_answers_received" else "human_input_required"
    if str(step_key or "") == "claims_routed" and int(metadata.get("claim_count") or 0) == 0:
        return "no_claims_extracted"
    if status == "timeout" or metadata_status == "timeout" or "timeout" in message_text:
        return "provider_timeout"
    if metadata_status in {"skipped", "skipped_not_configured", "not_configured"}:
        return "provider_not_configured"
    if metadata_status in {"failed", "error"} and "provider" in str(step_key or ""):
        return "provider_failed"
    if status == "blocked" and ("safety" in message_text or metadata_status == "blocked_by_safety_preflight"):
        return "blocked_by_safety_preflight"
    if status == "failed" and parse_status and parse_status not in {"parsed", "ok", "valid"}:
        return "parse_failed"
    if status == "failed" and schema_valid is False:
        return "schema_invalid"
    if status in FAILED_EVENT_STATUSES or status in BLOCKED_EVENT_STATUSES:
        return str(step_key or status)
    return ""


def _normalize_reason_code(value: str) -> str:
    text = str(value or "").strip().casefold().replace("-", "_").replace(" ", "_")
    aliases = {
        "schema_validation_failed": "schema_invalid",
        "schema_validation_error": "schema_invalid",
        "safety_preflight_failed": "blocked_by_safety_preflight",
        "safety_preflight_blocked": "blocked_by_safety_preflight",
        "not_configured": "provider_not_configured",
        "skipped_not_configured": "provider_not_configured",
        "timeout": "provider_timeout",
        "missing_child_bundle": "child_bundle_missing",
        "no_claims": "no_claims_extracted",
    }
    return aliases.get(text, text)


def _primary_canonical_state(operation: str, event: dict[str, Any]) -> str:
    states = _canonical_states_for_event(operation, event)
    return states[-1] if states else ""


def _canonical_states_for_event(operation: str, event: dict[str, Any]) -> list[str]:
    step_key = str(event.get("step_key") or "")
    status = str(event.get("status") or "")
    reason_code = str(event.get("reason_code") or "")
    metadata = event.get("metadata") if isinstance(event.get("metadata"), dict) else {}
    metadata_status = str(metadata.get("status") or "").casefold()
    schema_valid = metadata.get("schema_valid")
    parse_status = str(metadata.get("parse_status") or "").casefold()
    planner_answers_supplied = bool(metadata.get("planner_answers_supplied"))

    states: list[str] = []
    if step_key in {"bundle_received", "bundle_persisted"}:
        states.append("uploaded")
    if step_key in {"bundle_validated", "planner_input_validated"} and status in COMPLETE_EVENT_STATUSES:
        states.append("validated")
    if step_key == "model_input_validated":
        states.append("blocked" if status in BLOCKED_EVENT_STATUSES or reason_code == "blocked_by_safety_preflight" else "safety_passed")
    if step_key == "providers_scheduled":
        states.append("providers_scheduled")
    if step_key == "providers_completed":
        failed = (
            status in FAILED_EVENT_STATUSES
            or status in BLOCKED_EVENT_STATUSES
            or status == "skipped"
            or metadata_status in {"failed", "error", "timeout", "skipped", "skipped_not_configured", "not_configured"}
            or schema_valid is False
        )
        if failed:
            states.append("provider_failed")
        else:
            states.append("provider_completed")
        if schema_valid is True:
            states.append("schema_validated")
        if schema_valid is False and reason_code == "schema_invalid":
            states.append("provider_failed")
        if parse_status and parse_status not in {"parsed", "parsed_original", "ok", "valid"}:
            states.append("parse_failed")
    if step_key in {"outputs_parsed", "outputs_persisted"} and status in COMPLETE_EVENT_STATUSES:
        states.append("schema_validated")
    if reason_code == "parse_failed":
        states.append("parse_failed")
    if step_key in {"canonical_graph_resolved", "clusters_built"} and status in COMPLETE_EVENT_STATUSES:
        states.append("arbitration_completed")
    if step_key in {"review_targets_ready", "review_targets_persisted"} and status in COMPLETE_EVENT_STATUSES:
        states.append("review_targets_persisted")
    if step_key == "planner_answers_received" and (status == "needs_input" or reason_code in {"no_planner_answers", "human_input_required"}):
        states.append("waiting_human_answers")
    if step_key in {"plan_generated", "instructions_rendered"} and status in COMPLETE_EVENT_STATUSES:
        states.append("refined_plan_generated" if planner_answers_supplied else "planner_generated")
    if step_key == "more_data_requested":
        states.append("child_bundle_required")
    if step_key == "child_bundle_created" and status in FAILED_EVENT_STATUSES:
        states.append("child_bundle_required")
    if step_key == "completed" and status == "succeeded":
        states.append("completed")
    if status in FAILED_EVENT_STATUSES or status in BLOCKED_EVENT_STATUSES or reason_code in {"blocked_by_safety_preflight", "child_bundle_missing"}:
        if not states or states[-1] not in {"provider_failed", "parse_failed", "child_bundle_required"}:
            states.append("blocked")
    return _dedupe_states(states)


def _state_timeline(operation: str, events: list[dict[str, Any]], run: dict[str, Any]) -> list[dict[str, Any]]:
    timeline: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for event in events:
        for state in _canonical_states_for_event(operation, event):
            key = (state, str(event.get("step_key") or ""), str(event.get("provider_id") or ""))
            if key in seen:
                continue
            seen.add(key)
            timeline.append(
                {
                    "state": state,
                    "status": str(event.get("status") or ""),
                    "step_key": str(event.get("step_key") or ""),
                    "step_label": str(event.get("step_label") or step_label(operation, str(event.get("step_key") or ""))),
                    "reason_code": str(event.get("reason_code") or ""),
                    "provider_id": str(event.get("provider_id") or ""),
                    "message": str(event.get("message") or ""),
                    "created_at": str(event.get("created_at") or ""),
                }
            )
    run_status = str(run.get("status") or "")
    if run_status in {"failed", "blocked"} and not any(item.get("state") == "blocked" for item in timeline):
        timeline.append(
            {
                "state": "blocked",
                "status": run_status,
                "step_key": str(run.get("current_step") or ""),
                "step_label": step_label(operation, str(run.get("current_step") or "")),
                "reason_code": str(run.get("blocking_reason") or ""),
                "provider_id": "",
                "message": str(run.get("error_message") or ""),
                "created_at": str(run.get("updated_at") or ""),
            }
        )
    return timeline


def _events_with_canonical_state(operation: str, events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    enriched: list[dict[str, Any]] = []
    for event in events:
        event_map = dict(event)
        event_map["canonical_state"] = _primary_canonical_state(operation, event)
        enriched.append(event_map)
    return enriched


def _current_canonical_state(status: str, state_timeline: list[dict[str, Any]], blocking_reason: str) -> str:
    normalized = normalize_run_status(status)
    if normalized == "succeeded":
        return "completed"
    if blocking_reason in {"no_planner_answers", "human_input_required"}:
        return "waiting_human_answers"
    if blocking_reason == "child_bundle_missing":
        return "child_bundle_required"
    if blocking_reason == "parse_failed":
        return "parse_failed"
    if blocking_reason in {"provider_not_configured", "provider_timeout", "schema_invalid"}:
        return "provider_failed"
    if normalized in {"failed", "blocked"}:
        return "blocked"
    for item in reversed(state_timeline):
        state = str(item.get("state") or "")
        if state:
            return state
    return ""


def _active_reason_codes(events: list[dict[str, Any]], blocking_reason: str) -> list[str]:
    reasons: list[str] = []
    if blocking_reason:
        reasons.append(blocking_reason)
    for event in events:
        reason = str(event.get("reason_code") or "")
        if reason:
            reasons.append(reason)
    return [reason for reason in _unique_text(reasons) if reason in BLOCKING_REASON_CODES or reason]


def _dedupe_states(states: list[str]) -> list[str]:
    return [state for state in _unique_text(states) if state in CANONICAL_PIPELINE_STATES]


def _unique_text(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        text = str(value or "")
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


def _apply_summary_counters(
    run: dict[str, Any],
    metadata: dict[str, Any],
    event: dict[str, Any] | None = None,
) -> None:
    if "parent_pipeline_run_id" in metadata:
        run["parent_pipeline_run_id"] = str(metadata.get("parent_pipeline_run_id") or "")
    for source_key, target_key in (
        ("provider_count", "provider_total"),
        ("provider_total", "provider_total"),
        ("provider_success", "provider_success"),
        ("provider_failed", "provider_failed"),
        ("provider_skipped", "provider_skipped"),
        ("review_target_count", "review_target_count"),
        ("validation_target_count", "validation_target_count"),
        ("child_bundle_count", "child_bundle_count"),
    ):
        if source_key in metadata:
            run[target_key] = int(metadata.get(source_key) or 0)
    if event is None:
        return
    step_key = str(event.get("step_key") or "")
    event_status = str(event.get("status") or "")
    provider_id = str(event.get("provider_id") or "")
    metadata_status = str(metadata.get("status") or "").casefold()
    if step_key == "providers_completed" and provider_id:
        schema_valid = metadata.get("schema_valid")
        if (
            event_status in {"completed", "succeeded"} or metadata_status in {"ok", "completed", "succeeded"}
        ) and schema_valid is not False:
            run["provider_success"] = int(run.get("provider_success") or 0) + 1
        elif event_status == "skipped" or metadata_status in {"skipped", "skipped_not_configured", "not_configured"}:
            run["provider_skipped"] = int(run.get("provider_skipped") or 0) + 1
        elif event_status in FAILED_EVENT_STATUSES or event_status in BLOCKED_EVENT_STATUSES or schema_valid is False:
            run["provider_failed"] = int(run.get("provider_failed") or 0) + 1
    if step_key == "child_bundle_created":
        current = int(run.get("child_bundle_count") or 0)
        explicit = int(metadata.get("child_bundle_count") or 0)
        run["child_bundle_count"] = max(current, explicit) if explicit else current + 1


def _apply_event_frontier(run: dict[str, Any], events: list[dict[str, Any]]) -> None:
    run.update(_event_frontier(events, run))


def _status_with_provider_frontier(status: str, frontier: dict[str, Any]) -> str:
    normalized = normalize_run_status(status)
    if normalized != "succeeded":
        return normalized
    provider_total = int(frontier.get("provider_total") or 0)
    provider_success = int(frontier.get("provider_success") or 0)
    provider_failed = int(frontier.get("provider_failed") or 0)
    provider_skipped = int(frontier.get("provider_skipped") or 0)
    if provider_total > 0 and provider_success == 0 and (provider_failed > 0 or provider_skipped > 0):
        return "failed" if provider_failed > 0 else "blocked"
    return normalized


def _blocking_reason_with_provider_frontier(status: str, frontier: dict[str, Any]) -> str:
    if normalize_run_status(status) == "succeeded":
        return ""
    reason = str(frontier.get("blocking_reason") or "")
    if reason:
        return reason
    provider_total = int(frontier.get("provider_total") or 0)
    provider_success = int(frontier.get("provider_success") or 0)
    provider_failed = int(frontier.get("provider_failed") or 0)
    provider_skipped = int(frontier.get("provider_skipped") or 0)
    if provider_total > 0 and provider_success == 0:
        if provider_failed > 0:
            return "provider_failed"
        if provider_skipped > 0:
            return "provider_not_configured"
    return ""


def _event_frontier(events: list[dict[str, Any]], run: dict[str, Any] | None = None) -> dict[str, Any]:
    run = run or {}
    provider_total = int(run.get("provider_total") or 0)
    provider_success = int(run.get("provider_success") or 0)
    provider_failed = int(run.get("provider_failed") or 0)
    provider_skipped = int(run.get("provider_skipped") or 0)
    review_target_count = int(run.get("review_target_count") or 0)
    validation_target_count = int(run.get("validation_target_count") or 0)
    child_bundle_count = int(run.get("child_bundle_count") or 0)
    blocking_reason = str(run.get("blocking_reason") or "")
    provider_events: dict[str, dict[str, Any]] = {}
    child_outputs: set[str] = set()
    for event in events:
        metadata = event.get("metadata") if isinstance(event.get("metadata"), dict) else {}
        if "provider_count" in metadata or "provider_total" in metadata:
            provider_total = max(provider_total, int(metadata.get("provider_count") or metadata.get("provider_total") or 0))
        if isinstance(metadata.get("providers"), list):
            provider_total = max(provider_total, len(metadata.get("providers") or []))
        if "review_target_count" in metadata:
            review_target_count = max(review_target_count, int(metadata.get("review_target_count") or 0))
        if "validation_target_count" in metadata:
            validation_target_count = max(validation_target_count, int(metadata.get("validation_target_count") or 0))
        if str(event.get("step_key") or "") == "child_bundle_created":
            output_sha = str(event.get("output_sha256") or metadata.get("child_evidence_sha256") or "")
            if output_sha:
                child_outputs.add(output_sha)
            elif metadata.get("child_bundle_count"):
                child_bundle_count = max(child_bundle_count, int(metadata.get("child_bundle_count") or 0))
            else:
                child_outputs.add(str(event.get("event_id") or len(child_outputs)))
        status = str(event.get("status") or "")
        reason_code = str(event.get("reason_code") or "")
        if status in FAILED_EVENT_STATUSES or status in BLOCKED_EVENT_STATUSES or status == "needs_input":
            blocking_reason = reason_code or status
        if str(event.get("step_key") or "") == "providers_completed":
            provider_key = str(event.get("artifact_id") or event.get("provider_id") or event.get("event_id") or "")
            if provider_key:
                provider_events[provider_key] = event
    if provider_events:
        provider_success = 0
        provider_failed = 0
        provider_skipped = 0
        for event in provider_events.values():
            metadata = event.get("metadata") if isinstance(event.get("metadata"), dict) else {}
            status = str(event.get("status") or "")
            metadata_status = str(metadata.get("status") or "").casefold()
            schema_valid = metadata.get("schema_valid")
            if (status in {"completed", "succeeded"} or metadata_status in {"ok", "completed", "succeeded"}) and schema_valid is not False:
                provider_success += 1
            elif status == "skipped" or metadata_status in {"skipped", "skipped_not_configured", "not_configured"}:
                provider_skipped += 1
            elif status in FAILED_EVENT_STATUSES or status in BLOCKED_EVENT_STATUSES or schema_valid is False:
                provider_failed += 1
        provider_total = max(provider_total, provider_success + provider_failed + provider_skipped)
    if child_outputs:
        child_bundle_count = max(child_bundle_count, len(child_outputs))
    return {
        "blocking_reason": blocking_reason,
        "provider_total": provider_total,
        "provider_success": provider_success,
        "provider_failed": provider_failed,
        "provider_skipped": provider_skipped,
        "review_target_count": review_target_count,
        "validation_target_count": validation_target_count,
        "child_bundle_count": child_bundle_count,
    }


def _get_run(store: Any, pipeline_run_id: str) -> dict[str, Any] | None:
    if not pipeline_run_id or not hasattr(store, "get_pipeline_run"):
        return None
    return store.get_pipeline_run(pipeline_run_id)


def _upsert_run(store: Any, run: dict[str, Any]) -> None:
    if hasattr(store, "upsert_pipeline_run"):
        store.upsert_pipeline_run(run)


def _insert_event(store: Any, event: dict[str, Any]) -> None:
    if hasattr(store, "insert_pipeline_event"):
        store.insert_pipeline_event(event)


def _list_events(store: Any, pipeline_run_id: str) -> list[dict[str, Any]]:
    if hasattr(store, "list_pipeline_events"):
        try:
            return list(store.list_pipeline_events(pipeline_run_id))
        except Exception:
            return []
    return []
