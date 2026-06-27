from __future__ import annotations

import json
import re
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

from ops_evidence_synthesis.models import RawLog, SanitizedLog
from ops_evidence_synthesis.sanitizer import sanitize_log
from ops_evidence_synthesis.storage.sqlite_store import SQLiteStore
from ops_evidence_synthesis.timeutils import format_timestamp

_TIMESTAMP_KEYS = (
    "timestamp",
    "ts_utc",
    "time",
    "ts",
    "datetime",
    "created_at",
    "checked_ts_utc",
)
_SERVICE_KEYS = (
    "service",
    "stream_service",
    "component",
    "subsystem",
    "job",
    "repo",
    "app",
    "name",
)
_ENVIRONMENT_KEYS = ("environment", "env", "mode", "namespace")
_SEVERITY_KEYS = ("severity", "level", "log_level")
_TEXT_TS_RE = re.compile(
    r"^\[?(?P<timestamp>\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2})(?:[,.]\d+)?\]?\s*"
)
_TEXT_LEVEL_RE = re.compile(r"\[(?P<level>DEBUG|INFO|NOTICE|WARN|WARNING|ERROR|CRITICAL|ALERT|EMERGENCY)\]")


def load_jsonl(path: str | Path) -> list[RawLog]:
    return load_log_file(path)


def load_log_file(path: str | Path) -> list[RawLog]:
    return list(iter_log_file(path))


def iter_log_file(path: str | Path) -> Iterable[RawLog]:
    source_path = Path(path)
    fallback_time = datetime.fromtimestamp(source_path.stat().st_mtime, tz=UTC)
    with source_path.open("r", encoding="utf-8", errors="replace") as file:
        for line_number, line in enumerate(file, start=1):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                payload = json.loads(stripped)
            except json.JSONDecodeError as exc:
                yield _raw_from_text_line(stripped, source_path, line_number, fallback_time, exc)
                continue
            if not isinstance(payload, dict):
                yield _raw_from_text_line(
                    stripped,
                    source_path,
                    line_number,
                    fallback_time,
                    "json_row_not_object",
                )
                continue
            yield _raw_from_event(payload, source_path, line_number, fallback_time)


def sanitize_logs(
    raw_logs: Iterable[RawLog],
    *,
    service: str | None = None,
    environment: str | None = None,
) -> list[SanitizedLog]:
    return [sanitize_log(_with_case_labels(raw, service=service, environment=environment)) for raw in raw_logs]


def ingest_jsonl(
    path: str | Path,
    store: SQLiteStore,
    *,
    service: str | None = None,
    environment: str | None = None,
) -> int:
    raw_logs = load_log_file(path)
    return store.insert_sanitized_logs(sanitize_logs(raw_logs, service=service, environment=environment))


def ingest_log_files(
    paths: Iterable[str | Path],
    store: SQLiteStore,
    *,
    service: str | None = None,
    environment: str | None = None,
) -> int:
    total = 0
    for path in paths:
        total += ingest_jsonl(path, store, service=service, environment=environment)
    return total


def _with_case_labels(raw: RawLog, *, service: str | None, environment: str | None) -> RawLog:
    service_text = str(service or "").strip()
    environment_text = str(environment or "").strip()
    if not service_text and not environment_text:
        return raw
    labels = dict(raw.labels)
    if service_text and raw.service != service_text:
        labels.setdefault("original_service", raw.service)
    if environment_text and raw.environment != environment_text:
        labels.setdefault("original_environment", raw.environment)
    return replace(
        raw,
        service=service_text or raw.service,
        environment=environment_text or raw.environment,
        labels=labels,
    )


def _raw_from_event(
    payload: dict[str, Any],
    source_path: Path,
    line_number: int,
    fallback_time: datetime,
) -> RawLog:
    timestamp = _resolve_timestamp(payload, fallback_time + timedelta(microseconds=line_number))
    service = _resolve_string(payload, _SERVICE_KEYS) or source_path.stem
    environment = _resolve_string(payload, _ENVIRONMENT_KEYS) or _infer_environment(payload, source_path)
    severity = _resolve_severity(payload)
    message = _resolve_message(payload)
    labels = {
        key: value
        for key, value in payload.items()
        if key
        not in {
            *_TIMESTAMP_KEYS,
            *_SERVICE_KEYS,
            *_ENVIRONMENT_KEYS,
            *_SEVERITY_KEYS,
            "message",
            "msg",
            "text",
            "event",
        }
    }
    labels["source_path"] = str(source_path)
    labels["source_line"] = line_number
    return RawLog(
        timestamp=timestamp,
        service=service,
        environment=environment,
        severity=severity,
        message=message,
        trace_id=str(payload.get("trace_id") or payload.get("event_id") or ""),
        span_id=str(payload.get("span_id") or ""),
        deploy_id=str(payload.get("deploy_id") or payload.get("revision") or ""),
        version=str(payload.get("version") or payload.get("release") or ""),
        resource_type=str(payload.get("resource_type") or payload.get("kind") or source_path.suffix.lstrip(".")),
        labels=labels,
    )


def _raw_from_text_line(
    line: str,
    source_path: Path,
    line_number: int,
    fallback_time: datetime,
    json_error: json.JSONDecodeError | str,
) -> RawLog:
    timestamp = fallback_time + timedelta(microseconds=line_number)
    message = line
    ts_match = _TEXT_TS_RE.match(line)
    if ts_match:
        timestamp = _parse_loose_timestamp(ts_match.group("timestamp"))
        message = line[ts_match.end() :].strip()
    level_match = _TEXT_LEVEL_RE.search(message)
    severity = level_match.group("level") if level_match else _severity_from_text(message)
    if level_match:
        message = _TEXT_LEVEL_RE.sub("", message, count=1).strip()
    service = _service_from_text(message) or source_path.stem
    return RawLog(
        timestamp=format_timestamp(timestamp),
        service=service,
        environment=_infer_environment({}, source_path),
        severity=severity.upper(),
        message=message,
        resource_type=source_path.suffix.lstrip(".") or "text",
        labels={
            "source_path": str(source_path),
            "source_line": line_number,
            "parse_mode": "text",
            "json_parse_error": json_error.msg if isinstance(json_error, json.JSONDecodeError) else str(json_error),
        },
    )


def _resolve_timestamp(payload: dict[str, Any], fallback_time: datetime) -> str:
    for key in _TIMESTAMP_KEYS:
        value = payload.get(key)
        if value in (None, ""):
            continue
        if isinstance(value, (int, float)):
            return format_timestamp(datetime.fromtimestamp(float(value), tz=UTC))
        return format_timestamp(str(value))
    return format_timestamp(fallback_time)


def _parse_loose_timestamp(value: str) -> datetime:
    text = value.replace(" ", "T")
    return datetime.fromisoformat(text).replace(tzinfo=UTC)


def _resolve_string(payload: dict[str, Any], keys: tuple[str, ...]) -> str:
    for key in keys:
        value = payload.get(key)
        if value not in (None, ""):
            return str(value)
    metric = payload.get("metric")
    if isinstance(metric, dict):
        for key in keys:
            value = metric.get(key)
            if value not in (None, ""):
                return str(value)
    return ""


def _infer_environment(payload: dict[str, Any], source_path: Path) -> str:
    namespace = payload.get("namespace")
    if namespace:
        return str(namespace)
    path_text = str(source_path)
    if "stream_v3" in path_text or "stream-v3" in path_text:
        return "stream_v3"
    return "prod"


def _resolve_severity(payload: dict[str, Any]) -> str:
    explicit = _resolve_string(payload, _SEVERITY_KEYS)
    if explicit:
        return explicit.upper()
    status = str(payload.get("status") or "").casefold()
    if status in {"critical", "fatal", "emergency"}:
        return "CRITICAL"
    if status in {"error", "failed", "fail", "unhealthy"}:
        return "ERROR"
    if status in {"warn", "warning", "degraded"}:
        return "WARN"
    if payload.get("healthy") is False:
        return "ERROR"
    if payload.get("ok") is False or payload.get("success") is False:
        return "ERROR"
    failure_kind = str(payload.get("failure_kind") or "").casefold()
    if failure_kind and failure_kind != "none":
        return "ERROR"
    judgment = str(payload.get("judgment") or "").casefold()
    if judgment in {"warn", "warning", "degraded"}:
        return "WARN"
    if judgment in {"error", "failed", "unhealthy"}:
        return "ERROR"
    kind = str(payload.get("kind") or payload.get("event") or "").casefold()
    if any(token in kind for token in ("error", "fail", "panic", "crash")):
        return "ERROR"
    if any(token in kind for token in ("restart", "recover", "degraded", "backpressure")):
        return "WARN"
    return "INFO"


def _resolve_message(payload: dict[str, Any]) -> str:
    for key in ("message", "msg", "text", "event"):
        value = payload.get(key)
        if value not in (None, ""):
            return _append_event_context(str(value), payload)
    kind = str(payload.get("kind") or payload.get("event_id") or "event")
    return _append_event_context(kind, payload)


def _append_event_context(message: str, payload: dict[str, Any]) -> str:
    context_keys = (
        "kind",
        "status",
        "healthy",
        "failure_kind",
        "failure_subkind",
        "incident_stage",
        "incident_reason",
        "judgment",
        "judgment_reason",
        "action",
        "mbps",
        "notsent",
        "unacked",
        "lastsnd_ms",
        "stream_active",
        "ingest_connected",
        "api_cost_projected_units_per_day",
        "query",
    )
    parts = [message]
    for key in context_keys:
        value = payload.get(key)
        if value not in (None, ""):
            parts.append(f"{key}={value}")
    return " ".join(parts)


def _severity_from_text(message: str) -> str:
    lowered = message.casefold()
    if any(token in lowered for token in ("critical", "fatal", "panic", "crash")):
        return "CRITICAL"
    if any(token in lowered for token in ("error", "failed", "failure", "exception", "traceback")):
        return "ERROR"
    if any(token in lowered for token in ("warn", "degraded", "restart", "recover", "timeout")):
        return "WARN"
    return "INFO"


def _service_from_text(message: str) -> str:
    if message.startswith("[") and "]" in message:
        candidate = message[1 : message.index("]")].strip()
        if candidate:
            return candidate
    return ""
