from __future__ import annotations

import re
from dataclasses import asdict
from typing import Any

from ops_evidence_synthesis.canonical import canonical_json, sha256_text
from ops_evidence_synthesis.models import RawLog, SanitizedLog

SANITIZER_VERSION = "regex-2026-06-15"

_EMAIL_RE = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)
_IPV4_RE = re.compile(r"\b(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|1?\d?\d)\b")
_JWT_RE = re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b")
_AUTH_RE = re.compile(r"(?i)\bauthorization\s*[:=]\s*(?:bearer|basic)\s+[A-Za-z0-9._~+/\-]+=*")
_COOKIE_RE = re.compile(r"(?i)\b(?:set-cookie|cookie)\s*[:=]\s*[^,\n]+")
_SECRET_RE = re.compile(
    r"(?i)\b(api[_-]?key|token|secret|password)\s*[:=]\s*['\"]?[A-Za-z0-9_.:/=+\-]{10,}['\"]?"
)
_BARE_SECRET_TOKEN_RE = re.compile(
    r"(?i)\b(?:sk-proj|sk|ghp|github_pat|xoxb|xoxp|xoxa|xoxr)[A-Za-z0-9_.:/=+\-]{10,}\b"
)
_RTMPS_STREAM_KEY_RE = re.compile(r"(?i)(rtmps://[^ \n]+/live2/)[A-Za-z0-9_-]+")
_ID_RE = re.compile(r"(?i)\b(user_id|order_id|customer_id|account_id)\s*[:=]\s*[A-Za-z0-9_-]{4,}")
_UUID_RE = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.IGNORECASE)
_USER_HOME_PATH_RE = re.compile(
    r"(?P<path>(?:/home/[^/\s:'\"]+|/Users/[^/\s:'\"]+)(?:/[^\s:'\"]+)*)",
    re.IGNORECASE,
)
_WINDOWS_USER_PATH_RE = re.compile(
    r"(?P<path>[A-Z]:\\Users\\[^\\\s:'\"]+(?:\\[^\\\s:'\"]+)*)",
    re.IGNORECASE,
)
_HEX_RE = re.compile(r"\b0x[0-9a-f]{6,}\b", re.IGNORECASE)
_LONG_NUMBER_RE = re.compile(r"\b\d{4,}\b")
_SMALL_NUMBER_RE = re.compile(r"\b\d+\b")
_WS_RE = re.compile(r"\s+")
_NON_EMPTY_FAILURE_KIND_RE = re.compile(r"\bfailure_kind=(?!none\b|null\b|false\b|0\b)[^\s,;]+", re.IGNORECASE)


def sanitize_text(message: str) -> str:
    sanitized = _AUTH_RE.sub("<AUTH_HEADER>", message)
    sanitized = _COOKIE_RE.sub("<COOKIE>", sanitized)
    sanitized = _JWT_RE.sub("<JWT>", sanitized)
    sanitized = _RTMPS_STREAM_KEY_RE.sub(r"\1<STREAM_KEY>", sanitized)
    sanitized = _SECRET_RE.sub("<SECRET>", sanitized)
    sanitized = _BARE_SECRET_TOKEN_RE.sub("<SECRET>", sanitized)
    sanitized = _USER_HOME_PATH_RE.sub(_local_path_replacement, sanitized)
    sanitized = _WINDOWS_USER_PATH_RE.sub(_local_path_replacement, sanitized)
    sanitized = _EMAIL_RE.sub("<EMAIL>", sanitized)
    sanitized = _IPV4_RE.sub("<IP>", sanitized)
    sanitized = _ID_RE.sub(lambda match: f"{match.group(1)}=<ID>", sanitized)
    sanitized = _UUID_RE.sub("<UUID>", sanitized)
    return _WS_RE.sub(" ", sanitized).strip()


def _local_path_replacement(match: re.Match[str]) -> str:
    path = match.group("path")
    basename = path.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
    if not basename or basename in {".", ".."}:
        return "<LOCAL_PATH>"
    return f"<LOCAL_PATH>/{basename}"


def message_template(message: str) -> str:
    templated = _HEX_RE.sub("<HEX>", message)
    templated = _UUID_RE.sub("<UUID>", templated)
    templated = _LONG_NUMBER_RE.sub("<NUM>", templated)
    templated = _SMALL_NUMBER_RE.sub("<N>", templated)
    return _WS_RE.sub(" ", templated).strip()


def detect_error_type(message: str, severity: str) -> str:
    text = message.casefold()
    if (
        ("can't open file" in text or "no such file or directory" in text)
        and any(term in text for term in ("systemd", "execstart", ".service", ".py", ".sh"))
    ):
        return "job_configuration_mismatch"
    if "failed to start" in text and ".service" in text:
        return "service_start_failure"
    if "connection pool" in text or "too many connections" in text:
        return "connection_pool_exhausted"
    if "database" in text and ("timeout" in text or "timed out" in text):
        return "database_timeout"
    if "deadline exceeded" in text or "timeout" in text or "timed out" in text:
        return "dependency_timeout"
    if "rtmps" in text or "ffmpeg tcp send sample" in text or "ingest_connected=false" in text:
        return "stream_transport"
    if "youtube" in text and ("watchdog" in text or "api" in text or "live" in text):
        return "youtube_health"
    if "healthy=false" in text or _NON_EMPTY_FAILURE_KIND_RE.search(text) or "judgment=failed" in text:
        return "service_health_failure"
    if "5xx" in text or " 500 " in f" {text} " or "http 500" in text:
        return "http_5xx"
    if (
        "unauthorized" in text
        or "permission denied" in text
        or "authentication failed" in text
        or "authorization failed" in text
        or "auth_failure" in text
    ):
        return "auth_failure"
    if "restart" in text or "crashloop" in text or "oom" in text:
        return "runtime_restart"
    if "deploy" in text or "rollout" in text:
        return "deployment_event"
    if severity.upper() in {"ERROR", "CRITICAL", "ALERT", "EMERGENCY"}:
        return "application_error"
    return "none"


def stack_hash(message: str) -> str:
    text = message.casefold()
    if "traceback" not in text and " stack " not in f" {text} " and "\n at " not in text:
        return ""
    return sha256_text(message_template(text))[:16]


def sanitize_labels(labels: dict[str, Any]) -> dict[str, Any]:
    return {str(key): _sanitize_label_value(value) for key, value in labels.items()}


def _sanitize_label_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _sanitize_label_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_sanitize_label_value(item) for item in value]
    if isinstance(value, tuple):
        return [_sanitize_label_value(item) for item in value]
    if isinstance(value, str):
        return sanitize_text(value)
    return value


def sanitize_log(raw: RawLog) -> SanitizedLog:
    raw_hash = sha256_text(canonical_json(asdict(raw)))
    clean_message = sanitize_text(raw.message)
    return SanitizedLog(
        log_id=raw_hash,
        timestamp=raw.timestamp,
        service=raw.service,
        environment=raw.environment,
        severity=raw.severity.upper(),
        trace_id=raw.trace_id,
        span_id=raw.span_id,
        deploy_id=raw.deploy_id,
        version=raw.version,
        message_sanitized=clean_message,
        message_template=message_template(clean_message),
        error_type=detect_error_type(clean_message, raw.severity),
        stack_hash=stack_hash(clean_message),
        resource_type=raw.resource_type,
        labels_json=sanitize_labels(raw.labels),
        raw_log_sha256=raw_hash,
        sanitizer_version=SANITIZER_VERSION,
    )
