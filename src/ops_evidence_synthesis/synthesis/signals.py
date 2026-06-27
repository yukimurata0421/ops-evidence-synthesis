from __future__ import annotations

import re
from collections import Counter, defaultdict
from typing import Any

from ops_evidence_synthesis.canonical import sha256_json
from ops_evidence_synthesis.profiles import (
    evidence_requests_for_target_type,
    load_profile,
    metric_semantics,
    profile_for_bundle,
    target_definition,
    target_type_for_text,
    title_for_target_type,
)
from ops_evidence_synthesis.synthesis.subsystems import subsystem_for_text


CORE_TARGET_TYPES = (
    "job_configuration_mismatch",
    "service_start_failure",
    "restart_loop",
    "throughput_disappearance",
    "heartbeat_missing",
    "state_mismatch",
    "external_dependency_failure",
    "network_error_signal",
    "freshness_signal_gap",
    "user_impact_signal_gap",
    "resource_pressure",
    "deployment_regression",
    "monitoring_gap",
    "instrumentation_mismatch",
    "observability_contract_mismatch",
)

EVIDENCE_REQUEST_TYPES = (
    "process_state_query",
    "scheduler_history_query",
    "job_definition_query",
    "installed_artifact_query",
    "deployment_correlation_query",
    "external_dependency_status_query",
    "network_path_query",
    "throughput_signal_query",
    "user_impact_signal_query",
    "freshness_signal_query",
    "log_completeness_query",
    "instrumentation_consistency_query",
)


def build_signal_graph(bundle: dict[str, Any]) -> dict[str, Any]:
    """Derive generic operational signals before model synthesis.

    The output is intentionally profile-aware but not profile-specific: detectors
    produce generic signal and target types, then profile metadata supplies domain
    labels and concrete More data mappings.
    """

    profile_id = str(profile_for_bundle(bundle).get("profile_id") or "generic")
    signals = _dedupe_signals(
        [
            *_metric_signals(bundle, profile_id=profile_id),
            *_log_pattern_signals(bundle, profile_id=profile_id),
            *_operational_gap_signals(bundle, profile_id=profile_id),
            *_instrumentation_conflict_signals(bundle, profile_id=profile_id),
        ]
    )
    candidate_targets = _candidate_targets(signals, profile_id=profile_id)
    _link_validation_targets(candidate_targets)
    return {
        "schema_version": "ops-evidence-signals/v1",
        "core_target_types": list(CORE_TARGET_TYPES),
        "evidence_request_types": list(EVIDENCE_REQUEST_TYPES),
        "signals": signals,
        "candidate_targets": candidate_targets,
        "review_graph_seed": _review_graph_seed(candidate_targets),
    }


def _metric_signals(bundle: dict[str, Any], *, profile_id: str) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for row in bundle.get("metric_windows") or []:
        if not isinstance(row, dict):
            continue
        metric_name = str(row.get("metric_name") or "")
        if not metric_name:
            continue
        semantics = metric_semantics(metric_name, profile_id)
        baseline = _float(row.get("baseline_value"))
        current = _float(row.get("current_value"))
        if baseline is None or current is None:
            continue
        target_type = str(semantics.get("core_target_type") or "")
        subsystem = str(semantics.get("subsystem") or "general")
        zero_behavior = str(semantics.get("zero_behavior") or "")
        semantic_type = str(semantics.get("semantic_type") or "")
        evidence_id = str(row.get("metric_window_id") or "")
        if zero_behavior == "suspicious" and baseline > 0 and current == 0:
            output.append(
                _signal(
                    "zero_is_bad_drop",
                    target_type or "throughput_disappearance",
                    "primary_candidate",
                    subsystem=subsystem,
                    evidence_ids=[evidence_id],
                    summary=f"{metric_name} changed from baseline {baseline:g} to 0.",
                    metric_name=metric_name,
                    severity="high",
                    score_hint=0.92,
                    attributes={"baseline_value": baseline, "current_value": current, "zero_behavior": zero_behavior},
                )
            )
        elif zero_behavior == "healthy" and current == 0 and baseline >= 0:
            output.append(
                _signal(
                    "zero_is_good_stable",
                    target_type or "monitoring_gap",
                    "monitor_only",
                    subsystem=subsystem,
                    evidence_ids=[evidence_id],
                    summary=f"{metric_name} is 0; this is monitor-only unless paired with a missing critical output signal.",
                    metric_name=metric_name,
                    severity="low",
                    score_hint=0.25,
                    attributes={"baseline_value": baseline, "current_value": current, "zero_behavior": zero_behavior},
                )
            )
        elif target_type and semantic_type in {"error", "warning", "restart", "resource"} and current > baseline and current > 0:
            route = "primary_candidate" if target_type in {"restart_loop", "resource_pressure"} and current >= 5 else "validation_target"
            if profile_id != "generic" and subsystem == "generic_runtime" and semantic_type in {"error", "warning"}:
                route = "monitor_only"
            output.append(
                _signal(
                    "metric_spike",
                    target_type,
                    route,
                    subsystem=subsystem,
                    evidence_ids=[evidence_id],
                    summary=f"{metric_name} increased from baseline {baseline:g} to {current:g}.",
                    metric_name=metric_name,
                    severity="medium" if route == "validation_target" else "high",
                    score_hint=0.72 if route == "validation_target" else 0.82,
                    attributes={"baseline_value": baseline, "current_value": current},
                )
            )
    return output


def _log_pattern_signals(bundle: dict[str, Any], *, profile_id: str) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for row in bundle.get("log_patterns") or []:
        if not isinstance(row, dict):
            continue
        evidence_id = str(row.get("pattern_id") or "")
        error_type = str(row.get("error_type") or "")
        count = int(row.get("count") or 0)
        baseline_count = int(row.get("baseline_count") or 0)
        text = " ".join(str(row.get(key) or "") for key in ("message_template", "example_log", "error_type")).casefold()
        if error_type == "job_configuration_mismatch" or _missing_command_text(text):
            output.append(
                _signal(
                    "missing_command",
                    "job_configuration_mismatch",
                    "primary_candidate",
                    subsystem="job_configuration",
                    evidence_ids=[evidence_id],
                    summary=f"Configured command or artifact appears missing in {count} log events.",
                    severity="high",
                    score_hint=0.90,
                    attributes={"count": count, "baseline_count": baseline_count},
                )
            )
        elif error_type == "service_start_failure" or "failed to start" in text:
            output.append(
                _signal(
                    "repeated_failure",
                    "service_start_failure",
                    "validation_target",
                    subsystem="runtime_recovery",
                    evidence_ids=[evidence_id],
                    summary=f"Service start failure pattern occurred {count} times.",
                    severity="medium",
                    score_hint=0.68,
                    attributes={"count": count, "baseline_count": baseline_count},
                )
            )
        else:
            profile_target = target_type_for_text(text, profile_id)
            if profile_target and _is_negative_pattern(text, error_type=error_type, severity=str(row.get("severity_hint") or "")):
                output.append(
                    _signal(
                        "profile_log_pattern",
                        profile_target,
                        "primary_candidate" if _profile_log_target_is_primary(profile_target, profile_id=profile_id) else "validation_target",
                        subsystem=_subsystem_for_profile_target(profile_target, text, profile_id=profile_id),
                        evidence_ids=[evidence_id],
                        summary=f"{title_for_target_type(profile_target, profile_id)} log pattern occurred {count} times.",
                        severity="high" if str(row.get("severity_hint") or "") in {"high", "critical"} else "medium",
                        score_hint=0.86 if _profile_log_target_is_primary(profile_target, profile_id=profile_id) else 0.62,
                        attributes={"count": count, "baseline_count": baseline_count, "error_type": error_type},
                    )
                )
                continue

        if _restart_failure_pattern(text, error_type=error_type):
            output.append(
                _signal(
                    "restart_loop",
                    "restart_loop",
                    "validation_target",
                    subsystem="runtime_recovery",
                    evidence_ids=[evidence_id],
                    summary=f"Restart or recovery-loop pattern occurred {count} times.",
                    severity="medium",
                    score_hint=0.66,
                    attributes={"count": count, "baseline_count": baseline_count},
                )
            )
        elif _network_failure_pattern(text):
            target_type = "network_error_signal" if "connection reset" in text or "reconnect" in text else "external_dependency_failure"
            output.append(
                _signal(
                    "network_or_external_error",
                    target_type,
                    "validation_target",
                    subsystem="network_transport" if target_type == "network_error_signal" else "downstream_dependency",
                    evidence_ids=[evidence_id],
                    summary=f"Network or dependency error pattern occurred {count} times.",
                    severity="medium",
                    score_hint=0.62,
                    attributes={"count": count, "baseline_count": baseline_count},
                )
            )
    return output


def _profile_log_target_is_primary(target_type: str, *, profile_id: str) -> bool:
    if bool(target_definition(target_type, profile_id).get("primary_incident")):
        return True
    return target_type in {"throughput_disappearance", "heartbeat_missing", "job_configuration_mismatch"}


def _subsystem_for_profile_target(target_type: str, text: str, *, profile_id: str) -> str:
    metrics = load_profile(profile_id).get("metrics") or {}
    subsystems = [
        str(definition.get("subsystem") or "")
        for definition in metrics.values()
        if isinstance(definition, dict)
        and str(definition.get("core_target_type") or "") == target_type
        and str(definition.get("subsystem") or "")
    ]
    if subsystems:
        counts = Counter(subsystems)
        return sorted(counts, key=lambda value: (-counts[value], subsystems.index(value), value))[0]
    return subsystem_for_text(text)


def _is_negative_pattern(text: str, *, error_type: str, severity: str) -> bool:
    if _positive_health_pattern(text) and not _strong_failure_text(text):
        return False
    if severity in {"high", "critical"}:
        return True
    if error_type in {
        "dependency_timeout",
        "runtime_restart",
        "service_health_failure",
        "job_configuration_mismatch",
        "service_start_failure",
    }:
        return _strong_failure_text(text) or error_type != "runtime_restart"
    if error_type == "stream_transport":
        return _strong_failure_text(text)
    return _strong_failure_text(text)


def _positive_health_pattern(text: str) -> bool:
    positive_terms = (
        "status=ok",
        "healthy=true",
        "watchdog_ok",
        "runtime_status=running",
        "status=running",
        "last_health_ok=true",
        "judgment=ok",
        "ok=true",
    )
    return any(term in text for term in positive_terms)


def _strong_failure_text(text: str) -> bool:
    return any(
        term in text
        for term in (
            "failed",
            "failure",
            "connect_failure",
            "timed out",
            "timeout",
            "connection reset",
            "crashloop",
            "exited with code",
            "exit code",
            "restarting in",
            "unhealthy",
            "healthy=false",
            "local_ok=false",
            "public_ok=false",
            "api_ok=false",
            "oauth_ok=false",
            "ingest_connected=false",
            "stream_active=false",
            "degraded",
            "network_down",
            "tcp_stall",
            "no such file",
            "not found",
            "missing",
        )
    )


def _restart_failure_pattern(text: str, *, error_type: str) -> bool:
    if _positive_health_pattern(text) and not _strong_failure_text(text):
        return False
    if "crashloop" in text or "restart loop" in text or "runtime restart" in text:
        return True
    if "exited with code" in text or "restarting in" in text:
        return True
    if error_type == "runtime_restart":
        return "kind=restart" in text or _strong_failure_text(text)
    return False


def _network_failure_pattern(text: str) -> bool:
    if _positive_health_pattern(text) and not _strong_failure_text(text):
        return False
    return "connection reset" in text or "reconnect" in text or "timeout" in text or "timed out" in text


def _operational_gap_signals(bundle: dict[str, Any], *, profile_id: str) -> list[dict[str, Any]]:
    del profile_id
    output: list[dict[str, Any]] = []
    for row in bundle.get("operational_evidence") or []:
        if not isinstance(row, dict):
            continue
        incident_count = int(row.get("incident_count") or 0)
        baseline_count = int(row.get("baseline_count") or 0)
        if incident_count != 0 or baseline_count <= 0:
            continue
        request_type = str(row.get("request_type") or row.get("need") or "")
        target_type = _target_type_for_request_type(request_type)
        route = "primary_candidate" if request_type in {"throughput_signal"} else "validation_target"
        output.append(
            _signal(
                "evidence_gap",
                target_type,
                route,
                subsystem=str(row.get("subsystem") or "observability_contract"),
                evidence_ids=[str(row.get("evidence_id") or "")],
                summary=(
                    f"{request_type or 'operational evidence'} was present in baseline "
                    "but absent from the incident window."
                ),
                severity="high" if route == "primary_candidate" else "medium",
                score_hint=0.84 if route == "primary_candidate" else 0.58,
                attributes={
                    "request_id": row.get("request_id"),
                    "request_type": request_type,
                    "incident_count": incident_count,
                    "baseline_count": baseline_count,
                },
            )
        )
    return output


def _instrumentation_conflict_signals(bundle: dict[str, Any], *, profile_id: str) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    metrics = [
        row
        for row in bundle.get("metric_windows") or []
        if isinstance(row, dict) and _float(row.get("current_value")) == 0
    ]
    operational = [
        row
        for row in bundle.get("operational_evidence") or []
        if isinstance(row, dict) and int(row.get("incident_count") or 0) > 0
    ]
    for op_row in operational:
        op_tokens = _tokens(
            " ".join(
                str(op_row.get(key) or "")
                for key in ("request_id", "profile_request_id", "request_type", "need", "summary")
            )
        )
        for metric_row in metrics:
            metric_name = str(metric_row.get("metric_name") or "")
            semantics = metric_semantics(metric_name, profile_id)
            if str(semantics.get("zero_behavior") or "") == "suspicious":
                continue
            metric_tokens = _tokens(metric_name)
            if not _tokens_related(op_tokens, metric_tokens):
                continue
            incident_count = int(op_row.get("incident_count") or 0)
            output.append(
                _signal(
                    "log_metric_conflict",
                    "instrumentation_mismatch",
                    "validation_target",
                    subsystem=str(op_row.get("subsystem") or semantics.get("subsystem") or "observability_contract"),
                    evidence_ids=[str(op_row.get("evidence_id") or ""), str(metric_row.get("metric_window_id") or "")],
                    summary=(
                        f"Operational evidence {op_row.get('evidence_id')} observed {incident_count} "
                        f"{op_row.get('request_type') or op_row.get('request_id')} rows while metric {metric_name} reports 0."
                    ),
                    metric_name=metric_name,
                    severity="medium",
                    score_hint=0.62,
                    attributes={
                        "request_id": op_row.get("request_id"),
                        "request_type": op_row.get("request_type"),
                        "metric_name": metric_name,
                        "operational_incident_count": incident_count,
                        "metric_current_value": metric_row.get("current_value"),
                        "routing_note": "Treat as a validation target, not counter-evidence against the primary incident.",
                    },
                )
            )
    return output


def _candidate_targets(signals: list[dict[str, Any]], *, profile_id: str) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for signal in signals:
        if signal.get("routing_hint") == "auto_archive":
            continue
        key = (
            str(signal.get("core_target_type") or "monitoring_gap"),
            str(signal.get("subsystem") or "general"),
            str(signal.get("routing_hint") or "validation_target"),
        )
        grouped[key].append(signal)

    targets: list[dict[str, Any]] = []
    for index, ((target_type, subsystem, route), rows) in enumerate(sorted(grouped.items()), start=1):
        signal_ids = [str(row.get("signal_id") or "") for row in rows]
        evidence_ids = _unique(
            ref
            for row in rows
            for ref in row.get("evidence_ids") or []
        )
        score_hint = max(float(row.get("score_hint") or 0.0) for row in rows)
        review_mode = _review_mode_for(target_type, route, profile_id=profile_id)
        title = title_for_target_type(target_type, profile_id, default=target_type.replace("_", " ").title())
        missing = _missing_evidence_for_target(target_type, profile_id=profile_id)
        target = {
            "target_id": f"CT-{index:03d}-" + sha256_json({"target_type": target_type, "subsystem": subsystem, "signals": signal_ids})[:10],
            "core_target_type": target_type,
            "title": title,
            "subsystem": subsystem,
            "review_mode": review_mode,
            "routing_hint": route,
            "signal_ids": signal_ids,
            "support_evidence_refs": evidence_ids,
            "counter_evidence_refs": [],
            "missing_evidence": missing,
            "evidence_request_types": [
                str(request.get("request_id") or "")
                for request in evidence_requests_for_target_type(target_type, profile_id)
                if request.get("request_id")
            ],
            "core_claim": _core_claim_for_target(target_type, rows),
            "routing_reason": _routing_reason(rows, review_mode=review_mode),
            "score_hint": round(score_hint, 4),
            "temporary_action": _temporary_action(target_type, review_mode),
            "permanent_action": _permanent_action(target_type),
            "required_authority": "service owner or incident commander" if review_mode == "incident_candidate" else "on-call engineer",
        }
        targets.append(target)
    targets.sort(
        key=lambda item: (
            0 if item.get("review_mode") == "incident_candidate" else 1,
            -float(item.get("score_hint") or 0.0),
            str(item.get("target_id") or ""),
        )
    )
    return targets


def _link_validation_targets(targets: list[dict[str, Any]]) -> None:
    primary = next((target for target in targets if target.get("review_mode") == "incident_candidate"), None)
    if primary is None:
        return
    for target in targets:
        if target is primary or target.get("review_mode") != "validation_target":
            continue
        target["parent_target_id"] = primary.get("target_id")
        target["relationship"] = "validation_target_for_primary_incident"
    primary["related_target_ids"] = [
        str(target.get("target_id") or "")
        for target in targets
        if target.get("parent_target_id") == primary.get("target_id")
    ]


def _review_graph_seed(targets: list[dict[str, Any]]) -> dict[str, Any]:
    primary_targets = [target for target in targets if target.get("review_mode") == "incident_candidate"]
    validation_targets = [target for target in targets if target.get("review_mode") == "validation_target"]
    monitor_targets = [target for target in targets if target.get("review_mode") == "monitor_only"]
    return {
        "primary_candidate_count": len(primary_targets),
        "validation_target_count": len(validation_targets),
        "monitor_only_count": len(monitor_targets),
        "primary_target_ids": [target.get("target_id") for target in primary_targets],
        "validation_target_ids": [target.get("target_id") for target in validation_targets],
        "monitor_only_target_ids": [target.get("target_id") for target in monitor_targets],
    }


def _signal(
    signal_type: str,
    core_target_type: str,
    routing_hint: str,
    *,
    subsystem: str,
    evidence_ids: list[str],
    summary: str,
    metric_name: str = "",
    severity: str,
    score_hint: float,
    attributes: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload = {
        "signal_type": signal_type,
        "core_target_type": core_target_type,
        "routing_hint": routing_hint,
        "subsystem": subsystem or "general",
        "evidence_ids": [ref for ref in evidence_ids if ref],
        "summary": summary,
        "metric_name": metric_name,
        "severity": severity,
        "score_hint": round(score_hint, 4),
        "attributes": attributes or {},
    }
    payload["signal_id"] = "SIG-" + sha256_json(payload)[:12]
    return payload


def _dedupe_signals(signals: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_key: dict[tuple[Any, ...], dict[str, Any]] = {}
    for signal in signals:
        key = (
            signal.get("signal_type"),
            signal.get("core_target_type"),
            signal.get("subsystem"),
            tuple(signal.get("evidence_ids") or []),
            signal.get("metric_name"),
        )
        current = by_key.get(key)
        if current is None or float(signal.get("score_hint") or 0.0) > float(current.get("score_hint") or 0.0):
            by_key[key] = signal
    return sorted(by_key.values(), key=lambda row: (str(row.get("signal_id") or ""), str(row.get("summary") or "")))


def _target_type_for_request_type(request_type: str) -> str:
    return {
        "throughput_signal": "throughput_disappearance",
        "process_state": "service_start_failure",
        "external_dependency_status": "external_dependency_failure",
        "network_path": "network_error_signal",
        "freshness_signal": "freshness_signal_gap",
        "user_impact_signal": "user_impact_signal_gap",
        "state_transition": "state_mismatch",
        "log_completeness": "monitoring_gap",
        "scheduler_history": "monitoring_gap",
        "deployment_correlation": "deployment_regression",
        "resource_pressure": "resource_pressure",
    }.get(str(request_type or ""), "monitoring_gap")


def _review_mode_for(target_type: str, route: str, *, profile_id: str) -> str:
    if route == "monitor_only":
        return "monitor_only"
    if route == "primary_candidate" and bool(target_definition(target_type, profile_id).get("primary_incident")):
        return "incident_candidate"
    if route == "primary_candidate" and target_type in {"throughput_disappearance", "heartbeat_missing", "job_configuration_mismatch"}:
        return "incident_candidate"
    return "validation_target"


def _missing_evidence_for_target(target_type: str, *, profile_id: str) -> list[str]:
    requests = evidence_requests_for_target_type(target_type, profile_id)
    return _unique(
        str(request.get("description") or request.get("need") or request.get("request_id") or "")
        for request in requests
    )


def _core_claim_for_target(target_type: str, signals: list[dict[str, Any]]) -> str:
    primary = sorted(signals, key=lambda row: -float(row.get("score_hint") or 0.0))[0]
    if target_type == "instrumentation_mismatch":
        return (
            f"{primary.get('summary')} This is a validation target for metric/log consistency, "
            "not a rejection of the primary incident."
        )
    return str(primary.get("summary") or "")


def _routing_reason(signals: list[dict[str, Any]], *, review_mode: str) -> list[str]:
    reasons = [f"{signal.get('signal_type')} detected" for signal in signals]
    if review_mode == "incident_candidate":
        reasons.append("strong signal routed as primary candidate before AI synthesis")
    elif review_mode == "validation_target":
        reasons.append("routed as validation target before AI synthesis")
    elif review_mode == "monitor_only":
        reasons.append("routed as monitor-only before AI synthesis")
    return _unique(reasons)


def _temporary_action(target_type: str, review_mode: str) -> str:
    if review_mode == "incident_candidate":
        return "Review the cited evidence and run the linked validation checks before applying irreversible mitigation."
    if target_type == "instrumentation_mismatch":
        return "Compare the metric source with the operational log source for the same window before using either as proof."
    return "Collect the linked More data requests and use the result to strengthen or weaken the primary hypothesis."


def _permanent_action(target_type: str) -> str:
    if target_type == "instrumentation_mismatch":
        return "Add a consistency check between operational logs and exported metrics for this signal family."
    if target_type == "job_configuration_mismatch":
        return "Add deploy-time validation that supervisor commands and installed artifacts match."
    return "Persist the validation signal and make it part of the evidence profile for future incidents."


def _tokens(value: str) -> set[str]:
    stop = {
        "count",
        "query",
        "signal",
        "metric",
        "metrics",
        "stream",
        "stream_v3",
        "v3",
        "ops",
        "evidence",
        "status",
        "state",
        "path",
    }
    return {
        token
        for token in re.findall(r"[a-z0-9]+", str(value or "").casefold())
        if len(token) >= 3 and token not in stop
    }


def _tokens_related(left: set[str], right: set[str]) -> bool:
    if not left or not right:
        return False
    if left & right:
        return True
    joined_left = " ".join(sorted(left))
    joined_right = " ".join(sorted(right))
    return any(token in joined_left for token in right) or any(token in joined_right for token in left)


def _missing_command_text(text: str) -> bool:
    return (
        ("can't open file" in text or "cannot open file" in text or "no such file or directory" in text)
        and any(term in text for term in ("systemd", "execstart", ".service", ".py", ".sh", "configured"))
    )


def _float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _unique(values: Any) -> list[str]:
    output: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if text and text not in output:
            output.append(text)
    return output
