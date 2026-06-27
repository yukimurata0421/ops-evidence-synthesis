from __future__ import annotations

from typing import Any

from ops_evidence_synthesis.canonical import sha256_json


def normalized_event_from_log(log: Any, *, source_system: str = "") -> dict[str, Any]:
    labels = dict(getattr(log, "labels_json", {}) or {})
    component = labels.get("component") or labels.get("kind") or getattr(log, "resource_type", "") or "unknown"
    payload = {
        "timestamp": str(getattr(log, "timestamp", "")),
        "source_system": source_system or str(getattr(log, "environment", "") or "generic"),
        "service": str(getattr(log, "service", "")),
        "environment": str(getattr(log, "environment", "")),
        "component": str(component or "unknown"),
        "severity": str(getattr(log, "severity", "")),
        "event_type": str(getattr(log, "error_type", "") or labels.get("event_type") or "log_event"),
        "message_template": str(getattr(log, "message_template", "")),
        "message_sanitized": str(getattr(log, "message_sanitized", "")),
        "labels": labels,
        "trace_id": str(getattr(log, "trace_id", "") or "") or None,
        "deploy_id": str(getattr(log, "deploy_id", "") or "") or None,
        "host": str(labels.get("host") or labels.get("node") or ""),
    }
    payload["event_id"] = "EV-" + sha256_json(payload)[:16]
    return payload


def normalized_event_from_mapping(row: dict[str, Any], *, source_system: str, environment: str) -> dict[str, Any]:
    labels = row.get("labels_json") if isinstance(row.get("labels_json"), dict) else {}
    payload = {
        "timestamp": str(row.get("timestamp") or ""),
        "source_system": source_system,
        "service": str(row.get("service") or ""),
        "environment": str(row.get("environment") or environment),
        "component": str(labels.get("component") or labels.get("kind") or row.get("subsystem") or "unknown"),
        "severity": str(row.get("severity") or ""),
        "event_type": str(row.get("error_type") or labels.get("event_type") or "log_event"),
        "message_template": str(row.get("message_template") or ""),
        "message_sanitized": str(row.get("message_sanitized") or row.get("summary") or ""),
        "labels": labels,
        "trace_id": str(row.get("trace_id") or "") or None,
        "deploy_id": str(row.get("deploy_id") or "") or None,
        "host": str(labels.get("host") or labels.get("node") or ""),
    }
    payload["event_id"] = "EV-" + sha256_json(payload)[:16]
    return payload
