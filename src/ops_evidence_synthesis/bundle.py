from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from dataclasses import asdict
from datetime import timedelta
from typing import Any

from ops_evidence_synthesis.canonical import sha256_json, sha256_text
from ops_evidence_synthesis.event_semantics import enrich_evidence_item_semantics
from ops_evidence_synthesis.models import IncidentWindow, SanitizedLog, severity_rank
from ops_evidence_synthesis.normalize import normalized_event_from_log
from ops_evidence_synthesis.profiles import metric_semantics, operational_evidence_specs, profile_context_for_bundle
from ops_evidence_synthesis.sanitizer import SANITIZER_VERSION
from ops_evidence_synthesis.synthesis.signals import build_signal_graph
from ops_evidence_synthesis.storage.sqlite_store import SQLiteStore
from ops_evidence_synthesis.timeutils import format_timestamp, parse_timestamp, utc_now


_KEY_VALUE_RE = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)=([^\s]+)")
_STATE_BOUNDARY_TERMS = (
    "checkpoint",
    "frontier",
    "state_transition",
    "deployment",
    "deploy",
    "runtime_restart",
    "service_start",
    "watchdog",
    "systemd",
)


def _duration_minutes(start: str, end: str) -> int:
    delta = parse_timestamp(end) - parse_timestamp(start)
    return max(1, int(delta.total_seconds() // 60))


def _severity_hint(count: int, baseline_count: int, max_severity: str) -> str:
    rank = severity_rank(max_severity)
    delta = count - baseline_count
    if rank >= severity_rank("CRITICAL") or (count >= 5 and baseline_count == 0):
        return "critical"
    if delta >= 4 or rank >= severity_rank("ERROR"):
        return "high"
    if delta >= 2:
        return "medium"
    return "low"


def _pattern_template_hash(pattern: dict[str, Any]) -> str:
    return sha256_json(
        {
            "message_template": str(pattern.get("message_template") or ""),
            "error_type": str(pattern.get("error_type") or "none"),
        }
    )


def _pattern_coverage_class(pattern: dict[str, Any]) -> str:
    count = int(pattern.get("covered_log_count") or pattern.get("count") or 0)
    if count == 1:
        return "singleton"
    if count <= 3:
        return "rare"
    if _pattern_has_state_boundary(pattern):
        return "state_transition"
    return "pattern"


def _pattern_has_state_boundary(pattern: dict[str, Any]) -> bool:
    event_text = (
        f"{pattern.get('error_type') or ''} {pattern.get('message_template') or ''}"
    ).lower()
    return any(term in event_text for term in _STATE_BOUNDARY_TERMS)


def _pattern_coverage_facets(pattern: dict[str, Any], coverage_class: str) -> list[str]:
    facets = [coverage_class]
    if _pattern_has_state_boundary(pattern) and "state_transition" not in facets:
        facets.append("state_transition")
    return facets


def _coverage_assignment_reason(coverage_class: str) -> str:
    return {
        "pattern": "high_frequency_operational_pattern",
        "rare": "low_frequency_evidence_preserved",
        "singleton": "single_occurrence_preserved",
        "state_transition": "state_or_checkpoint_boundary",
        "temporal_bucket": "temporal_variation_boundary",
        "tail_summary": "low_signal_tail_accounted_for",
    }.get(coverage_class, "evidence_item_boundary_assignment")


def _metric(
    metric_window_id: str,
    service: str,
    window_start: str,
    window_end: str,
    metric_name: str,
    baseline_value: float,
    current_value: float,
    severity_hint: str,
) -> dict[str, Any]:
    delta = current_value - baseline_value
    delta_pct = 0.0 if baseline_value == 0 else (delta / baseline_value) * 100.0
    return {
        "metric_window_id": metric_window_id,
        "service": service,
        "window_start": window_start,
        "window_end": window_end,
        "metric_name": metric_name,
        "baseline_value": baseline_value,
        "current_value": current_value,
        "delta": delta,
        "delta_pct": round(delta_pct, 2),
        "severity_hint": severity_hint,
    }


class EvidenceBundleBuilder:
    def __init__(self, store: SQLiteStore) -> None:
        self.store = store

    def build(self, incident: IncidentWindow) -> dict[str, Any]:
        normalized = incident.normalized()
        start_dt = parse_timestamp(normalized.incident_start)
        end_dt = parse_timestamp(normalized.incident_end)
        lookback_start = format_timestamp(start_dt - timedelta(minutes=normalized.lookback_minutes))

        current_logs = self.store.fetch_logs(
            normalized.service,
            normalized.environment,
            normalized.incident_start,
            normalized.incident_end,
        )
        baseline_logs = self.store.fetch_logs(
            normalized.service,
            normalized.environment,
            lookback_start,
            normalized.incident_start,
        )

        log_sample_candidates = [
            log
            for log in current_logs
            if severity_rank(log.severity) >= severity_rank("WARN")
            or log.error_type
            in {
                "deployment_event",
                "runtime_restart",
                "stream_transport",
                "youtube_health",
                "service_health_failure",
            }
        ]
        if not log_sample_candidates:
            log_sample_candidates = current_logs[:100]

        log_items = self._log_evidence(log_sample_candidates)
        normalized_events = [
            normalized_event_from_log(log, source_system=normalized.environment)
            for log in log_sample_candidates[:100]
        ]
        patterns = self._patterns(
            normalized,
            current_logs,
            baseline_logs,
            baseline_start=lookback_start,
        )
        metrics = self._metrics(normalized, current_logs, baseline_logs)
        deployments = self._deployments(current_logs, baseline_logs)
        profile_context = profile_context_for_bundle({"service": normalized.service, "environment": normalized.environment})
        profile_id = str((profile_context.get("profile") or {}).get("profile_id") or "")
        operational_evidence = self._operational_evidence(
            normalized,
            current_logs,
            baseline_logs,
            profile_id=profile_id,
        )
        evidence_items = self._evidence_items(
            patterns,
            metrics,
            operational_evidence,
        )
        profile_event_semantics = profile_context.get("event_semantics") or []
        evidence_items = [
            enrich_evidence_item_semantics(
                item,
                profile_event_semantics=profile_event_semantics,
                profile_approved=bool(profile_event_semantics),
            )
            for item in evidence_items
        ]
        db_corpus_coverage = self._db_corpus_coverage(current_logs, patterns)
        signal_graph_input = {
            **profile_context,
            "service": normalized.service,
            "environment": normalized.environment,
            "window_start": normalized.incident_start,
            "window_end": normalized.incident_end,
            "logs": log_items,
            "log_patterns": patterns,
            "metric_windows": metrics,
            "operational_evidence": operational_evidence,
            "evidence_items": evidence_items,
            "db_corpus_coverage": db_corpus_coverage,
        }
        signal_graph = build_signal_graph(signal_graph_input)

        query_fingerprint = {
            "service": normalized.service,
            "environment": normalized.environment,
            "window_start": normalized.incident_start,
            "window_end": normalized.incident_end,
            "lookback_minutes": normalized.lookback_minutes,
            "builder": "local-sqlite-v2-full-corpus-coverage",
        }
        query_sql_hash = sha256_json(query_fingerprint)

        evidence_refs: dict[str, Any] = {}
        for item in log_items:
            evidence_refs[item["evidence_id"]] = {
                "type": "log",
                "summary": item["message_sanitized"],
                "timestamp": item["timestamp"],
            }
        for item in patterns:
            evidence_refs[item["pattern_id"]] = {
                "type": "log_pattern",
                "summary": item["message_template"],
                "count": item["count"],
                "baseline_count": item.get("baseline_count", 0),
                "first_seen": item.get("first_seen", ""),
                "last_seen": item.get("last_seen", ""),
                    "severity_hint": item.get("severity_hint", ""),
                    "aggregation_source": item.get("aggregation_source", ""),
                    "covered_log_count": item.get("covered_log_count", item.get("count", 0)),
                }
        for item in metrics:
            evidence_refs[item["metric_window_id"]] = {
                "type": "metric_window",
                "summary": f"{item['metric_name']} {item['baseline_value']} -> {item['current_value']}",
            }

        for item in operational_evidence:
            evidence_refs[item["evidence_id"]] = {
                "type": "operational_evidence",
                "summary": item["summary"],
                "incident_count": item["incident_count"],
                "baseline_count": item["baseline_count"],
                "baseline_daily_average": item["baseline_daily_average"],
                "subsystem": item["subsystem"],
                "request_id": item["request_id"],
                "profile_request_id": item.get("profile_request_id", ""),
                "sample_count": len(item.get("samples") or []),
            }

        bundle = {
            "schema_version": "ops-evidence-bundle/v1",
            **profile_context,
            "service": normalized.service,
            "environment": normalized.environment,
            "window_start": normalized.incident_start,
            "window_end": normalized.incident_end,
            "incident_window": {
                "start": normalized.incident_start,
                "end": normalized.incident_end,
            },
            "lookback": _lookback_label(normalized.lookback_minutes),
            "baseline": {
                "mode": "previous_window",
                "start": lookback_start,
                "end": normalized.incident_start,
            },
            "lookback_window_start": lookback_start,
            "lookback_minutes": normalized.lookback_minutes,
            "query_sql_hash": query_sql_hash,
            "sanitizer_version": SANITIZER_VERSION,
            "incident": {
                "incident_id": f"INC-{sha256_json(query_fingerprint)[:12]}",
                "duration_minutes": _duration_minutes(normalized.incident_start, normalized.incident_end),
            },
            "evidence_refs": evidence_refs,
            "evidence_items": evidence_items,
            "db_corpus_coverage": db_corpus_coverage,
            "normalized_events": normalized_events,
            "logs": log_items,
            "log_patterns": patterns,
            "metric_windows": metrics,
            "operational_evidence": operational_evidence,
            "evidence_signals": signal_graph["signals"],
            "candidate_targets": signal_graph["candidate_targets"],
            "review_graph_seed": signal_graph["review_graph_seed"],
            "core_target_types": signal_graph["core_target_types"],
            "evidence_request_types": signal_graph["evidence_request_types"],
            "deployments": deployments,
            "similar_past_incidents": [],
            "created_at": utc_now(),
        }

        hash_payload = {key: value for key, value in bundle.items() if key not in {"created_at", "evidence_sha256"}}
        evidence_sha256 = sha256_json(hash_payload)
        bundle["evidence_sha256"] = evidence_sha256
        self.store.insert_bundle(bundle)
        return bundle

    def _log_evidence(self, logs: list[SanitizedLog]) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for index, log in enumerate(logs[:100], start=1):
            payload = asdict(log)
            payload["evidence_id"] = f"LOG-{index:03d}"
            payload["raw_log_sha256"] = log.raw_log_sha256
            items.append(payload)
        return items

    def _patterns(
        self,
        incident: IncidentWindow,
        current_logs: list[SanitizedLog],
        baseline_logs: list[SanitizedLog],
        *,
        baseline_start: str,
    ) -> list[dict[str, Any]]:
        sql_patterns = self._sql_patterns(
            incident,
            baseline_start=baseline_start,
            current_log_count=len(current_logs),
        )
        if sql_patterns:
            return sql_patterns

        grouped: dict[tuple[str, str], list[SanitizedLog]] = defaultdict(list)
        for log in current_logs:
            grouped[(log.message_template, log.error_type)].append(log)

        baseline_counts = Counter((log.message_template, log.error_type) for log in baseline_logs)
        patterns: list[dict[str, Any]] = []
        sorted_groups = sorted(
            grouped.items(),
            key=lambda item: (-len(item[1]), item[0][0], item[0][1]),
        )
        for index, ((template, error_type), logs) in enumerate(sorted_groups, start=1):
            first_seen = min(log.timestamp for log in logs)
            last_seen = max(log.timestamp for log in logs)
            max_severity = max((log.severity for log in logs), key=severity_rank)
            baseline_count = baseline_counts[(template, error_type)]
            patterns.append(
                {
                    "pattern_id": f"PATTERN-{index:03d}",
                    "service": incident.service,
                    "environment": incident.environment,
                    "window_start": incident.incident_start,
                    "window_end": incident.incident_end,
                    "message_template": template,
                    "error_type": error_type,
                    "count": len(logs),
                    "baseline_count": baseline_count,
                    "first_seen": first_seen,
                    "last_seen": last_seen,
                    "example_log": logs[0].message_sanitized,
                    "example_log_sha256": logs[0].raw_log_sha256,
                    "aggregation_source": "python_selected_logs",
                    "embedding": [],
                    "max_severity": max_severity,
                    "covered_log_count": len(logs),
                    "db_row_coverage_role": "pattern_group",
                    "severity_hint": _severity_hint(len(logs), baseline_count, max_severity),
                }
            )
        return patterns

    def _sql_patterns(
        self,
        incident: IncidentWindow,
        *,
        baseline_start: str,
        current_log_count: int,
    ) -> list[dict[str, Any]]:
        fetch = getattr(self.store, "fetch_log_pattern_summaries", None)
        if not callable(fetch):
            return []
        try:
            summaries = fetch(
                incident.service,
                incident.environment,
                incident.incident_start,
                incident.incident_end,
                baseline_start=baseline_start,
                baseline_end=incident.incident_start,
                limit=max(1, current_log_count),
            )
        except Exception:
            return []
        patterns: list[dict[str, Any]] = []
        for index, row in enumerate(summaries, start=1):
            count = int(row.get("count") or 0)
            baseline_count = int(row.get("baseline_count") or 0)
            max_severity = str(row.get("max_severity") or "INFO")
            patterns.append(
                {
                    "pattern_id": f"PATTERN-{index:03d}",
                    "service": incident.service,
                    "environment": incident.environment,
                    "window_start": incident.incident_start,
                    "window_end": incident.incident_end,
                    "message_template": str(row.get("message_template") or ""),
                    "error_type": str(row.get("error_type") or "none"),
                    "count": count,
                    "baseline_count": baseline_count,
                    "first_seen": str(row.get("first_seen") or ""),
                    "last_seen": str(row.get("last_seen") or ""),
                    "example_log_sha256": str(row.get("example_log_sha256") or ""),
                    "aggregation_source": str(row.get("aggregation_source") or "sql_group_by"),
                    "embedding": [],
                    "max_severity": max_severity,
                    "covered_log_count": count,
                    "db_row_coverage_role": "pattern_group",
                    "severity_hint": _severity_hint(count, baseline_count, max_severity),
                }
            )
        return patterns

    def _evidence_items(
        self,
        patterns: list[dict[str, Any]],
        metrics: list[dict[str, Any]],
        operational_evidence: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for pattern in patterns:
            evidence_id = str(pattern.get("pattern_id") or "")
            coverage_class = _pattern_coverage_class(pattern)
            coverage_facets = _pattern_coverage_facets(pattern, coverage_class)
            template_hash = _pattern_template_hash(pattern)
            covered_log_count = int(pattern.get("covered_log_count") or pattern.get("count") or 0)
            items.append(
                {
                    "evidence_id": evidence_id,
                    "type": "log_pattern",
                    "coverage_class": coverage_class,
                    "coverage_facets": coverage_facets,
                    "event_type": str(pattern.get("error_type") or "none"),
                    "severity_text": str(pattern.get("max_severity") or pattern.get("severity_hint") or "INFO"),
                    "count": int(pattern.get("count") or 0),
                    "baseline_count": int(pattern.get("baseline_count") or 0),
                    "first_seen": str(pattern.get("first_seen") or ""),
                    "last_seen": str(pattern.get("last_seen") or ""),
                    "message_template": str(pattern.get("message_template") or ""),
                    "template_hash": template_hash,
                    "example_log_sha256": str(pattern.get("example_log_sha256") or ""),
                    "component": "unknown",
                    "source": "logs_sanitized",
                    "source_log_count": covered_log_count,
                    "prompt_boundary": {
                        "mode": "chunked_evidence_item",
                        "raw_row_direct_prompt": False,
                        "direct_prompt": True,
                        "assignment_reason": _coverage_assignment_reason(coverage_class),
                    },
                    "db_row_coverage": {
                        "source_table": "logs_sanitized",
                        "coverage_role": "pattern_group",
                        "coverage_class": coverage_class,
                        "coverage_facets": coverage_facets,
                        "covered_log_count": covered_log_count,
                        "assignment_key": "message_template,error_type",
                        "template_hash": template_hash,
                        "assignment_reason": _coverage_assignment_reason(coverage_class),
                    },
                }
            )
        for metric in metrics:
            metric_id = str(metric.get("metric_window_id") or "")
            items.append(
                {
                    "evidence_id": metric_id,
                    "type": "metric_window",
                    "event_type": str(metric.get("metric_name") or "metric_window"),
                    "severity_text": str(metric.get("severity_hint") or "INFO"),
                    "coverage_class": "temporal_bucket",
                    "count": 1,
                    "first_seen": str(metric.get("window_start") or ""),
                    "last_seen": str(metric.get("window_end") or ""),
                    "message_template": (
                        f"{metric.get('metric_name')} baseline={metric.get('baseline_value')} "
                        f"current={metric.get('current_value')} delta={metric.get('delta')}"
                    ),
                    "source": "derived_metric_window",
                    "source_log_count": 0,
                    "prompt_boundary": {
                        "mode": "chunked_evidence_item",
                        "raw_row_direct_prompt": False,
                        "direct_prompt": True,
                        "assignment_reason": _coverage_assignment_reason("temporal_bucket"),
                    },
                }
            )
        for row in operational_evidence:
            evidence_id = str(row.get("evidence_id") or "")
            interpretation = row.get("interpretation") if isinstance(row.get("interpretation"), dict) else {}
            items.append(
                {
                    "evidence_id": evidence_id,
                    "type": "operational_evidence",
                    "event_type": str(row.get("request_type") or row.get("need") or "operational_evidence"),
                    "severity_text": str(interpretation.get("severity_hint") or ("NOTICE" if row.get("incident_count") else "INFO")),
                    "coverage_class": "state_transition",
                    "count": int(row.get("incident_count") or 0),
                    "baseline_count": int(row.get("baseline_count") or 0),
                    "first_seen": str((row.get("samples") or [{}])[-1].get("timestamp") or ""),
                    "last_seen": str((row.get("samples") or [{}])[0].get("timestamp") or ""),
                    "message_template": str(row.get("summary") or ""),
                    "component": str(row.get("subsystem") or "general"),
                    "source": "operational_evidence_spec",
                    "source_log_count": int(row.get("incident_count") or 0),
                    "prompt_boundary": {
                        "mode": "chunked_evidence_item",
                        "raw_row_direct_prompt": False,
                        "direct_prompt": True,
                        "assignment_reason": _coverage_assignment_reason("state_transition"),
                    },
                }
            )
        return [item for item in items if item.get("evidence_id")]

    def _db_corpus_coverage(
        self,
        logs: list[SanitizedLog],
        patterns: list[dict[str, Any]],
    ) -> dict[str, Any]:
        key_to_pattern = {
            (str(pattern.get("message_template") or ""), str(pattern.get("error_type") or "")): pattern
            for pattern in patterns
        }
        assignments: list[dict[str, Any]] = []
        uncovered = 0
        for log in logs:
            pattern = key_to_pattern.get((log.message_template, log.error_type))
            pattern_id = str((pattern or {}).get("pattern_id") or "")
            if not pattern_id:
                uncovered += 1
            coverage_class = _pattern_coverage_class(pattern or {}) if pattern_id else "unassigned"
            coverage_facets = _pattern_coverage_facets(pattern or {}, coverage_class) if pattern_id else ["unassigned"]
            template_hash = _pattern_template_hash(pattern or {"message_template": log.message_template, "error_type": log.error_type})
            assignments.append(
                {
                    "log_id": log.log_id,
                    "raw_log_sha256": log.raw_log_sha256,
                    "sanitized_log_sha256": log.log_id,
                    "timestamp": log.timestamp,
                    "severity": log.severity,
                    "error_type": log.error_type,
                    "evidence_id": pattern_id,
                    "evidence_item_id": pattern_id,
                    "review_boundary_id": pattern_id,
                    "template_hash": template_hash,
                    "coverage_class": coverage_class,
                    "coverage_facets": coverage_facets,
                    "direct_prompt": False,
                    "prompt_boundary": "raw_row_to_evidence_item_to_chunk_manifest",
                    "assignment_reason": _coverage_assignment_reason(coverage_class),
                }
            )
        severity_counts = Counter(log.severity for log in logs)
        error_type_counts = Counter(log.error_type or "none" for log in logs)
        pattern_counts = Counter(str(row.get("evidence_id") or "") for row in assignments if row.get("evidence_id"))
        total = len(logs)
        covered = total - uncovered
        return {
            "schema_version": "db_corpus_coverage.v1",
            "source_table": "logs_sanitized",
            "strategy": "assign_every_sanitized_log_to_message_template_error_type_pattern",
            "total_row_count": total,
            "covered_row_count": covered,
            "uncovered_row_count": uncovered,
            "coverage_ratio": round(covered / total, 6) if total else 1.0,
            "pattern_count": len(patterns),
            "singleton_pattern_count": sum(1 for count in pattern_counts.values() if count == 1),
            "low_frequency_pattern_count": sum(1 for count in pattern_counts.values() if count <= 3),
            "coverage_class_counts": dict(sorted(Counter(row["coverage_class"] for row in assignments).items())),
            "direct_prompt_row_count": sum(1 for row in assignments if row.get("direct_prompt") is True),
            "raw_rows_sent_to_providers": False,
            "prompt_boundary_policy": (
                "Sanitized DB rows are assigned to Evidence Items and review chunks; raw rows are not copied "
                "directly into provider prompts."
            ),
            "row_assignments_sha256": sha256_json(assignments),
            "row_assignments": assignments,
            "severity_counts": dict(sorted(severity_counts.items())),
            "error_type_counts": dict(sorted(error_type_counts.items())),
        }

    def _metrics(
        self,
        incident: IncidentWindow,
        current_logs: list[SanitizedLog],
        baseline_logs: list[SanitizedLog],
    ) -> list[dict[str, Any]]:
        current_error_count = sum(1 for log in current_logs if severity_rank(log.severity) >= severity_rank("ERROR"))
        baseline_error_count = sum(1 for log in baseline_logs if severity_rank(log.severity) >= severity_rank("ERROR"))
        current_warn_count = sum(1 for log in current_logs if log.severity in {"WARN", "WARNING"})
        baseline_warn_count = sum(1 for log in baseline_logs if log.severity in {"WARN", "WARNING"})
        current_restart_count = sum(1 for log in current_logs if log.error_type == "runtime_restart")
        baseline_restart_count = sum(1 for log in baseline_logs if log.error_type == "runtime_restart")
        current_deploy_count = len({log.deploy_id for log in current_logs if log.deploy_id})
        baseline_deploy_count = len({log.deploy_id for log in baseline_logs if log.deploy_id})
        current_unique_traces = len({log.trace_id for log in current_logs if log.trace_id})
        baseline_unique_traces = len({log.trace_id for log in baseline_logs if log.trace_id})
        current_5xx = sum(1 for log in current_logs if log.error_type == "http_5xx")
        baseline_5xx = sum(1 for log in baseline_logs if log.error_type == "http_5xx")

        raw = [
            ("error_count", baseline_error_count, current_error_count, "high" if current_error_count > baseline_error_count else "low"),
            ("warn_count", baseline_warn_count, current_warn_count, "medium" if current_warn_count > baseline_warn_count else "low"),
            ("runtime_restart_count", baseline_restart_count, current_restart_count, "high" if current_restart_count else "low"),
            ("deploy_id_count", baseline_deploy_count, current_deploy_count, "medium" if current_deploy_count else "low"),
            ("unique_trace_count", baseline_unique_traces, current_unique_traces, "medium"),
            ("http_5xx_count", baseline_5xx, current_5xx, "high" if current_5xx else "low"),
        ]
        return [
            _metric(
                f"METRIC-{index:03d}",
                incident.service,
                incident.incident_start,
                incident.incident_end,
                metric_name,
                float(baseline),
                float(current),
                severity_hint,
            )
            for index, (metric_name, baseline, current, severity_hint) in enumerate(raw, start=1)
        ]

    def _operational_evidence(
        self,
        incident: IncidentWindow,
        current_logs: list[SanitizedLog],
        baseline_logs: list[SanitizedLog],
        *,
        profile_id: str,
    ) -> list[dict[str, Any]]:
        specs = operational_evidence_specs(profile_id)
        if not specs:
            return []
        baseline_days = _baseline_days(incident.incident_start, incident.lookback_minutes)
        output: list[dict[str, Any]] = []
        for spec in specs:
            current_matches = [log for log in current_logs if _matches_operational_spec(log, spec)]
            baseline_matches = [log for log in baseline_logs if _matches_operational_spec(log, spec)]
            samples = [_operational_sample(log) for log in _latest_logs(current_matches, limit=8)]
            observations = _operational_observations(
                spec,
                current_logs=current_logs,
                matching_logs=current_matches,
                profile_id=profile_id,
            )
            incident_count = len(current_matches)
            baseline_count = len(baseline_matches)
            output.append(
                {
                    "evidence_id": spec["evidence_id"],
                    "request_id": spec["request_id"],
                    "profile_request_id": spec.get("profile_request_id", ""),
                    "request_type": spec.get("request_type") or spec["need"],
                    "need": spec["need"],
                    "summary": spec["summary"],
                    "subsystem": spec["subsystem"],
                    "incident_count": incident_count,
                    "baseline_count": baseline_count,
                    "baseline_daily_average": round(baseline_count / baseline_days, 4),
                    "samples": samples,
                    "observations": observations,
                    "interpretation": _operational_interpretation(
                        incident_count=incident_count,
                        baseline_daily_average=baseline_count / baseline_days,
                    ),
                }
            )
        return output

    def _deployments(
        self,
        current_logs: list[SanitizedLog],
        baseline_logs: list[SanitizedLog],
    ) -> list[dict[str, Any]]:
        seen: dict[str, dict[str, Any]] = {}
        for source, logs in (("lookback", baseline_logs), ("incident", current_logs)):
            for log in logs:
                if not log.deploy_id:
                    continue
                existing = seen.setdefault(
                    log.deploy_id,
                    {
                        "deploy_id": log.deploy_id,
                        "version": log.version,
                        "first_seen": log.timestamp,
                        "last_seen": log.timestamp,
                        "source_windows": set(),
                        "event_count": 0,
                    },
                )
                existing["first_seen"] = min(existing["first_seen"], log.timestamp)
                existing["last_seen"] = max(existing["last_seen"], log.timestamp)
                existing["source_windows"].add(source)
                existing["event_count"] += 1
        deployments: list[dict[str, Any]] = []
        for item in sorted(seen.values(), key=lambda row: (row["first_seen"], row["deploy_id"])):
            deployments.append(
                {
                    **item,
                    "source_windows": sorted(item["source_windows"]),
                    "deploy_sha256": sha256_text(item["deploy_id"]),
                }
            )
        return deployments


def _lookback_label(minutes: int) -> str:
    if minutes % 1440 == 0:
        return f"{minutes // 1440}d"
    if minutes % 60 == 0:
        return f"{minutes // 60}h"
    return f"{minutes}m"


def _baseline_days(incident_start: str, lookback_minutes: int) -> float:
    start_dt = parse_timestamp(incident_start) - timedelta(minutes=lookback_minutes)
    end_dt = parse_timestamp(incident_start)
    days = (end_dt - start_dt).total_seconds() / 86400
    return max(days, 1.0)


def _matches_operational_spec(log: SanitizedLog, spec: dict[str, Any]) -> bool:
    text = _search_text(log)
    labels = _event_labels(log)
    metric_name = str(labels.get("metric_name") or "")
    source_name = _source_name(log)
    metric_names = {str(name) for name in spec.get("metric_names") or () if str(name)}
    source_names = {str(name) for name in spec.get("source_names") or () if str(name)}
    if metric_name and metric_name in metric_names:
        return True
    if source_name and source_name in source_names:
        return True
    return any(str(term).casefold() in text for term in spec.get("terms") or ())


def _search_text(log: SanitizedLog) -> str:
    labels = log.labels_json if isinstance(log.labels_json, dict) else {}
    return " ".join(
        [
            log.message_sanitized,
            log.message_template,
            log.error_type,
            log.resource_type,
            json.dumps(labels, ensure_ascii=True, sort_keys=True),
        ]
    ).casefold()


def _latest_logs(logs: list[SanitizedLog], *, limit: int) -> list[SanitizedLog]:
    return sorted(logs, key=lambda log: (log.timestamp, log.raw_log_sha256), reverse=True)[:limit]


def _operational_sample(log: SanitizedLog) -> dict[str, Any]:
    labels = _event_labels(log)
    return {
        "timestamp": log.timestamp,
        "service": log.service,
        "severity": log.severity,
        "source_name": _source_name(log),
        "message_sanitized": _compact_text(log.message_sanitized, limit=360),
        "message_template": _compact_text(log.message_template, limit=220),
        "error_type": log.error_type,
        "observed_fields": _interesting_fields(labels, log.message_sanitized),
        "raw_log_sha256": log.raw_log_sha256,
    }


def _operational_observations(
    spec: dict[str, Any],
    *,
    current_logs: list[SanitizedLog],
    matching_logs: list[SanitizedLog],
    profile_id: str,
) -> list[dict[str, Any]]:
    observations: list[dict[str, Any]] = []
    metric_names = {str(name) for name in spec.get("metric_names") or ()}
    for log in _latest_metric_logs(current_logs, metric_names=metric_names):
        metric_name = str(_event_labels(log).get("metric_name") or "")
        observed_value = _coerce_scalar(_event_labels(log).get("metric_value"))
        metric_key = metric_name.removeprefix("stream_v3_")
        observations.append(
            {
                "kind": "latest_metric",
                "source_name": _source_name(log),
                "timestamp": log.timestamp,
                "metric_name": metric_name,
                "observed_value": observed_value,
                "assessment": _metric_assessment(metric_key, observed_value, profile_id=profile_id),
            }
        )
    for log in _latest_logs(matching_logs, limit=3):
        fields = _interesting_fields(_event_labels(log), log.message_sanitized)
        if not fields:
            continue
        observations.append(
            {
                "kind": "latest_status",
                "source_name": _source_name(log),
                "timestamp": log.timestamp,
                "observed_value": fields,
                "severity": log.severity,
            }
        )
    return observations[:18]


def _latest_metric_logs(logs: list[SanitizedLog], *, metric_names: set[str]) -> list[SanitizedLog]:
    by_metric: dict[str, SanitizedLog] = {}
    for log in logs:
        labels = _event_labels(log)
        metric_name = str(labels.get("metric_name") or "")
        if not metric_name or metric_name not in metric_names:
            continue
        existing = by_metric.get(metric_name)
        if existing is None or (log.timestamp, log.raw_log_sha256) > (existing.timestamp, existing.raw_log_sha256):
            by_metric[metric_name] = log
    return [by_metric[name] for name in sorted(by_metric)]


def _event_labels(log: SanitizedLog) -> dict[str, Any]:
    labels = log.labels_json if isinstance(log.labels_json, dict) else {}
    nested = labels.get("labels")
    if isinstance(nested, dict):
        return nested
    return labels


def _source_name(log: SanitizedLog) -> str:
    labels = _event_labels(log)
    return str(labels.get("source_name") or log.resource_type or "")


def _interesting_fields(labels: dict[str, Any], message: str) -> dict[str, Any]:
    keys = {
        "status",
        "healthy",
        "judgment",
        "local_ok",
        "public_ok",
        "api_ok",
        "oauth_ok",
        "ingest_connected",
        "stream_active",
        "fail_count",
        "current_fail",
        "historical_degraded",
        "stream_service_substate",
        "runtime_status",
        "last_health_ok",
        "ffmpeg_pid",
        "restart_count",
        "mbps",
        "notsent",
        "unacked",
        "lastsnd_ms",
        "bytes_sent_delta",
    }
    fields = {key: _coerce_scalar(value) for key, value in labels.items() if key in keys}
    for key, value in _parse_key_values(message).items():
        if key in keys and key not in fields:
            fields[key] = value
    return fields


def _parse_key_values(message: str) -> dict[str, Any]:
    values: dict[str, Any] = {}
    for key, raw in _KEY_VALUE_RE.findall(message):
        values[key] = _coerce_scalar(raw.rstrip(",;"))
    return values


def _coerce_scalar(value: Any) -> Any:
    if isinstance(value, str):
        text = value.strip()
        lowered = text.casefold()
        if lowered in {"true", "false"}:
            return lowered == "true"
        if lowered in {"none", "null"}:
            return None
        try:
            return int(text)
        except ValueError:
            pass
        try:
            return float(text)
        except ValueError:
            return text
    return value


def _metric_assessment(metric_name: str, observed_value: Any, *, profile_id: str) -> str:
    semantics = metric_semantics(metric_name, profile_id)
    zero_behavior = str(semantics.get("zero_behavior") or "")
    value = _numeric_value(observed_value)
    if value is None:
        return "observed"
    if zero_behavior == "suspicious" and value == 0:
        return "suspicious_zero_or_gap"
    if zero_behavior == "healthy" and value == 0:
        return "healthy_zero"
    if metric_name.endswith(("_ok", "_connected", "_active", "_available", "_present", "_up")):
        return "healthy" if value >= 1 else "suspicious_zero_or_gap"
    if metric_name in {"current_fail", "notify_active_incidents"}:
        return "healthy_zero" if value == 0 else "active_failure"
    if metric_name.endswith(("fail_count", "fault_count", "warning_count", "critical_count")):
        return "healthy_zero" if value == 0 else "nonzero_count"
    return "observed"


def _numeric_value(value: Any) -> float | None:
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _operational_interpretation(*, incident_count: int, baseline_daily_average: float) -> str:
    if incident_count == 0 and baseline_daily_average > 0:
        return "zero_is_bad_or_evidence_gap: this source was present in baseline but absent in the incident window."
    if incident_count > 0 and baseline_daily_average == 0:
        return "new_incident_evidence: this source appeared in the incident window but not in baseline."
    if incident_count > 0:
        return "evidence_available: inspect observations before accepting or rejecting the review target."
    return "no_evidence_available: use this as a missing-evidence signal, not proof of normal behavior."


def _compact_text(value: Any, *, limit: int) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return f"{text[:limit]}...[truncated {len(text) - limit} chars]"
