from __future__ import annotations

import json
import re
import shlex
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from itertools import combinations
from pathlib import Path
from typing import Any, Iterable

from ops_evidence_synthesis.canonical import canonical_json, pretty_json, sha256_json, sha256_text
from ops_evidence_synthesis.evidence_rules import ai_evidence_rules
from ops_evidence_synthesis.profiles import available_profile_ids, load_profile
from ops_evidence_synthesis.profiles.registry import normalize_profile_id
from ops_evidence_synthesis.timeutils import format_timestamp, parse_timestamp, utc_now


SANITIZER_VERSION = "sanitize.v1.1"
CANONICALIZATION_VERSION = "canonical_json.v1"
RAW_LOG_POLICY = "not_uploaded"
LARGE_SEEK_THRESHOLD_BYTES = 50 * 1024 * 1024
LARGE_SEEK_SAFETY_MARGIN_BYTES = 8 * 1024 * 1024

REQUIRED_PROFILE_QUESTIONS = [
    "What is the critical user outcome?",
    "Which component is the main service path?",
    "Are restart failures expected or harmful?",
    "Which metrics are zero-is-good or zero-is-bad?",
    "Which logs indicate user impact rather than diagnostic noise?",
]

DETECTED_FORMATS = {
    "jsonl",
    "journald_json",
    "syslog",
    "logfmt",
    "key_value",
    "nginx_access",
    "apache_access",
    "python_traceback",
    "java_stacktrace",
    "kubernetes_container_log",
    "plain_text",
}

SUMMARY_KEYS = (
    "email",
    "ip_address",
    "secret_like",
    "authorization_header",
    "cookie",
    "token_like",
    "internal_url",
    "user_home",
    "id_like",
    "basic_auth",
    "gmail_credential",
    "pubsub_credential",
    "cloud_credential",
)

EXAMPLE_REPLACEMENTS = {
    "email": "<EMAIL_HASH:sha256_prefix>",
    "ip_address": "<IP_HASH:sha256_prefix>",
    "secret_like": "<REDACTED_SECRET>",
    "authorization_header": "<REDACTED_SECRET>",
    "cookie": "<REDACTED_SECRET>",
    "token_like": "<REDACTED_SECRET>",
    "internal_url": "<URL_HASH:sha256_prefix>",
    "user_home": "<USER_HOME>",
    "id_like": "<ID_HASH:sha256_prefix>",
    "basic_auth": "<REDACTED_SECRET>",
    "gmail_credential": "<REDACTED_SECRET>",
    "pubsub_credential": "<REDACTED_SECRET>",
    "cloud_credential": "<REDACTED_SECRET>",
}

TIMESTAMP_KEYS = (
    "timestamp",
    "time",
    "ts",
    "datetime",
    "created_at",
    "observed_timestamp",
    "ts_utc",
    "checked_ts_utc",
    "@timestamp",
    "__REALTIME_TIMESTAMP",
)
SEVERITY_KEYS = ("severity", "level", "log_level", "priority", "PRIORITY")
SERVICE_KEYS = (
    "service",
    "app",
    "application",
    "stream_service",
    "job",
    "unit",
    "_SYSTEMD_UNIT",
    "SYSLOG_IDENTIFIER",
    "kubernetes.container_name",
    "container_name",
)
COMPONENT_KEYS = (
    "component",
    "subsystem",
    "module",
    "logger",
    "kind",
    "resource_type",
    "_SYSTEMD_UNIT",
    "SYSLOG_IDENTIFIER",
    "kubernetes.container_name",
)
ENVIRONMENT_KEYS = ("environment", "env", "namespace", "mode", "kubernetes.namespace_name")
HOST_KEYS = ("host", "hostname", "_HOSTNAME", "node", "kubernetes.host")
MESSAGE_KEYS = ("message", "msg", "text", "event", "log", "MESSAGE", "@message")
TRACE_KEYS = ("trace_id", "trace", "logging.googleapis.com/trace")
SPAN_KEYS = ("span_id", "span")
DEPLOY_KEYS = ("deploy_id", "revision", "release", "version")

SEVERITY_NUMBERS = {
    "trace": 1,
    "debug": 5,
    "info": 9,
    "notice": 10,
    "warn": 13,
    "warning": 13,
    "error": 17,
    "critical": 21,
    "alert": 22,
    "emergency": 23,
    "fatal": 24,
}
JOURNALD_PRIORITY = {
    "0": "emergency",
    "1": "alert",
    "2": "critical",
    "3": "error",
    "4": "warning",
    "5": "notice",
    "6": "info",
    "7": "debug",
}

CORE_TARGET_BY_EVENT_TYPE = {
    "missing_file": "job_configuration_mismatch",
    "missing_command": "job_configuration_mismatch",
    "permission_denied": "state_mismatch",
    "service_start_failure": "service_start_failure",
    "restart_loop": "restart_loop",
    "process_exit": "restart_loop",
    "timeout": "external_dependency_failure",
    "connection_reset": "network_error_signal",
    "dns_failure": "network_error_signal",
    "auth_failure": "external_dependency_failure",
    "config_error": "job_configuration_mismatch",
    "dependency_unreachable": "external_dependency_failure",
    "http_5xx": "user_impact_signal_gap",
    "oom": "resource_pressure",
    "state_mismatch": "state_mismatch",
    "monitoring_gap": "monitoring_gap",
    "instrumentation_mismatch": "instrumentation_mismatch",
    "warning": "general",
    "info": "general",
    "unknown": "general",
}

SKIP_DIRS = {".git", ".hg", ".svn", ".venv", "venv", "__pycache__", ".pytest_cache", "chromium_profile"}
SKIP_FILE_NAMES = {"LOCK", "CURRENT"}
SKIP_FILE_SUFFIXES = {
    ".aac",
    ".avi",
    ".bin",
    ".db",
    ".flac",
    ".gif",
    ".jpeg",
    ".jpg",
    ".m4a",
    ".mkv",
    ".mov",
    ".mp3",
    ".mp4",
    ".ogg",
    ".pb",
    ".pcap",
    ".png",
    ".pma",
    ".sqlite",
    ".sqlite3",
    ".wav",
    ".webp",
}

ISO_TS_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}[T ][0-2]\d:[0-5]\d:[0-5]\d(?:[.,]\d+)?(?:Z|[+-]\d{2}:?\d{2})?\b")
PATH_DATE_RE = re.compile(r"(?<!\d)(20\d{2})[-_]?([01]\d)[-_]?([0-3]\d)(?!\d)")
TEXT_TS_PREFIX_RE = re.compile(r"^\[?(?P<timestamp>\d{4}-\d{2}-\d{2}[T ][0-2]\d:[0-5]\d:[0-5]\d(?:[.,]\d+)?)\]?\s*")
SYSLOG_RE = re.compile(
    r"^(?P<mon>[A-Z][a-z]{2})\s+(?P<day>\d{1,2})\s+(?P<hms>\d{2}:\d{2}:\d{2})\s+"
    r"(?P<host>\S+)\s+(?P<program>[A-Za-z0-9_.@/\-]+)(?:\[(?P<pid>\d+)\])?:\s+(?P<message>.*)$"
)
K8S_RE = re.compile(
    r"^(?P<timestamp>\d{4}-\d{2}-\d{2}T[0-2]\d:[0-5]\d:[0-5]\d(?:\.\d+)?Z)\s+"
    r"(?P<stream>stdout|stderr)\s+(?P<tag>[FP])\s+(?P<message>.*)$"
)
ACCESS_RE = re.compile(
    r'^(?P<ip>\S+)\s+\S+\s+(?P<user>\S+)\s+\[(?P<time>[^\]]+)\]\s+'
    r'"(?P<method>[A-Z]+)\s+(?P<path>\S+)(?:\s+HTTP/(?P<http_version>[^"]+))?"\s+'
    r"(?P<status>\d{3})\s+(?P<size>\S+)"
)
KEY_VALUE_RE = re.compile(r"\b([A-Za-z_][A-Za-z0-9_.\-]*)=('[^']*'|\"[^\"]*\"|[^\s,;]+)")
KEY_COLON_RE = re.compile(r"\b([A-Za-z_][A-Za-z0-9_.\-]*)\s*:\s*([^\s,;]+)")
LEVEL_RE = re.compile(r"\b(DEBUG|INFO|NOTICE|WARN|WARNING|ERROR|CRITICAL|ALERT|EMERGENCY|FATAL)\b", re.IGNORECASE)
PY_TRACE_RE = re.compile(r"Traceback \(most recent call last\)|File \"[^\"]+\", line \d+|^[A-Za-z_][\w.]*Error:", re.IGNORECASE)
JAVA_TRACE_RE = re.compile(r"\b(?:Exception|Error):|^\s*at\s+[a-zA-Z_$][\w.$]*\([^)]*\)")

EMAIL_RE = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)
IPV4_RE = re.compile(r"\b(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|1?\d?\d)\b")
UUID_RE = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.IGNORECASE)
HASH_RE = re.compile(r"\b(?:sha256:)?[0-9a-f]{32,128}\b", re.IGNORECASE)
GOOGLE_API_KEY_RE = re.compile(r"\bAIza[0-9A-Za-z_\-]{20,}\b")
GOOGLE_OAUTH_RE = re.compile(r"\bya29\.[0-9A-Za-z_\-./]+\b")
JWT_RE = re.compile(r"\beyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\b")
SK_KEY_RE = re.compile(r"\bsk-(?:live|test|proj)?-?[A-Za-z0-9_\-]{12,}\b", re.IGNORECASE)
BEARER_RE = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=\-]{8,}")
BASIC_RE = re.compile(r"(?i)\bBasic\s+[A-Za-z0-9+/=]{8,}")
PRIVATE_KEY_BLOCK_RE = re.compile(r"-----BEGIN PRIVATE KEY-----.*?-----END PRIVATE KEY-----")
AUTH_HEADER_RE = re.compile(r"(?i)\b(Authorization\s*[:=]\s*)(?:Bearer|Basic)?\s*[A-Za-z0-9._~+/=\-]{6,}")
COOKIE_RE = re.compile(r"(?i)\b(?:Cookie|Set-Cookie)\s*[:=]\s*[^,\s;]+(?:;\s*[^,\s;=]+=[^,\s;]+)*")
SECRET_KV_RE = re.compile(
    r"(?i)\b("
    r"api[_-]?key|x[-_]?api[-_]?key|access[_-]?token|refresh[_-]?token|id[_-]?token|"
    r"session(?:[_-]?id)?|password|passwd|secret|client[_-]?secret|private[_-]?key|"
    r"google_application_credentials|credentials|credential|service_account|gmail[_-]?token|pubsub[_-]?token"
    r")\s*[:=]\s*('(?:[^']*)'|\"(?:[^\"]*)\"|[^\s,;]+)"
)
ID_KV_RE = re.compile(
    r"(?i)\b(user[_-]?id|order[_-]?id|tracking[_-]?id|track[_-]?id|customer[_-]?id|account[_-]?id)"
    r"\s*[:=]\s*('(?:[^']*)'|\"(?:[^\"]*)\"|[A-Za-z0-9_.:\-]{3,})"
)
USER_HOME_RE = re.compile(r"(?i)(?:/home/[^/\s:]+|/Users/[^/\s:]+|[A-Z]:\\Users\\[^\\\s:]+)")
INTERNAL_URL_RE = re.compile(
    r"(?i)\bhttps?://(?:"
    r"localhost|127\.0\.0\.1|10\.\d{1,3}\.\d{1,3}\.\d{1,3}|"
    r"192\.168\.\d{1,3}\.\d{1,3}|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}|"
    r"metadata\.google\.internal|[^/\s'\"]+\.(?:internal|local|corp|lan)"
    r")(?::\d+)?(?:/[^\s'\"]*)?"
)
POSIX_PATH_RE = re.compile(r"(?<![A-Za-z0-9_])(?:<USER_HOME>/|/)(?:[A-Za-z0-9._@%+\-]+/)*[A-Za-z0-9._@%+\-]+")
WINDOWS_PATH_RE = re.compile(r"(?i)\b[A-Z]:\\(?:[^\\\s:]+\\)+[^\\\s:]*")
NUMBER_RE = re.compile(r"\b\d+(?:\.\d+)?\b")
REDACTION_TOKEN_RE = re.compile(r"<(?:EMAIL_HASH|IP_HASH|ID_HASH|URL_HASH):[0-9a-f]{12}>")
ALLOWED_PLACEHOLDER_RE = re.compile(
    r"<REDACTED_SECRET>|<USER_HOME>|<(?:EMAIL_HASH|IP_HASH|ID_HASH|URL_HASH):[0-9a-f]{12}>"
)
VERIFY_TARGET_FILES = (
    "sanitized_events.jsonl",
    "manifest.json",
    "redaction_report.json",
    "rejected_lines.jsonl",
    "evidence_bundle.json",
    "child_evidence_bundle.json",
    "profile_discovery_bundle.json",
    "profile_draft.json",
    "approved_profile.yaml",
    "approved_profile.json",
    "evidence_request_plan.json",
    "planner_answers.json",
    "collection_instructions.md",
    "source_context_bundle.json",
    "source_context_report.md",
    "source_analysis_bundle.json",
    "source_analysis_report.md",
    "model_runs.jsonl",
    "multi_ai_synthesis.json",
    "review_targets.json",
)
VERIFY_SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("secret_like", re.compile(r"(?i)\bAuthorization\s*:")),
    ("secret_like", re.compile(r"(?i)\bBearer\s+")),
    ("secret_like", re.compile(r"(?i)\bBasic\s+")),
    ("secret_like", re.compile(r"(?i)\bCookie\s*:")),
    ("secret_like", re.compile(r"(?i)\bSet-Cookie\s*:")),
    ("secret_like", re.compile(r"(?i)\bpassword\s*=")),
    ("secret_like", re.compile(r"(?i)\bpasswd\s*=")),
    ("secret_like", re.compile(r"(?i)\bsecret\s*=")),
    ("secret_like", re.compile(r"(?i)\bprivate_key\b")),
    ("secret_like", re.compile(r"(?i)\bapi_key\b")),
    ("secret_like", re.compile(r"(?i)\baccess_token\b")),
    ("secret_like", re.compile(r"(?i)\brefresh_token\b")),
    ("secret_like", re.compile(r"(?i)\bsession_id\b")),
    ("secret_like", re.compile(r"(?i)\bsk-[A-Za-z0-9_\-]{8,}")),
    ("secret_like", re.compile(r"\bAIza[0-9A-Za-z_\-]{10,}")),
    ("secret_like", re.compile(r"\bya29\.[0-9A-Za-z_\-./]+")),
    ("secret_like", re.compile(r"-----BEGIN PRIVATE KEY-----")),
)
VERIFY_INTERNAL_URL_RE = re.compile(r"(?i)\bhttps?://(?:internal[^\s'\",}]*|[^/\s'\",}]*\.internal[^\s'\",}]*)")
VERIFY_INTERNAL_DOMAIN_RE = re.compile(r"(?i)\b[A-Za-z0-9_.-]+\.internal\b")
VERIFY_SCAN_TOKENS = (
    "authorization",
    "bearer",
    "basic ",
    "cookie",
    "set-cookie",
    "password",
    "passwd",
    "secret",
    "private_key",
    "api_key",
    "access_token",
    "refresh_token",
    "session_id",
    "sk-",
    "aiza",
    "ya29.",
    "begin private key",
    "http://",
    "https://",
    ".internal",
    "/home/",
    "/users/",
    "\\users\\",
)
REDACTION_SCAN_TOKENS = (
    "authorization",
    "bearer",
    "basic ",
    "cookie",
    "set-cookie",
    "password",
    "passwd",
    "secret",
    "api_key",
    "api-key",
    "apikey",
    "access_token",
    "refresh_token",
    "id_token",
    "session",
    "credential",
    "private_key",
    "private key",
    "client_secret",
    "google_application_credentials",
    "gmail",
    "pubsub",
    "sk-",
    "aiza",
    "ya29.",
    "eyj",
    "http://",
    "https://",
    "localhost",
    ".internal",
    "/home/",
    "/users/",
    "\\users\\",
    "user_id",
    "order_id",
    "tracking_id",
    "track_id",
    "customer_id",
    "account_id",
)
REDACTION_SCAN_RE = re.compile("|".join(re.escape(token) for token in REDACTION_SCAN_TOKENS), re.IGNORECASE)


@dataclass(frozen=True)
class InputLine:
    source_path: Path
    line_number: int
    text: str
    fallback_timestamp: str


@dataclass(frozen=True)
class ParsedLine:
    detected_format: str
    timestamp: str | None
    observed_timestamp: str
    timestamp_inferred: bool
    source_system: str
    service: str
    environment: str
    host: str
    component: str
    severity_text: str
    severity_number: int
    message: str
    attributes: dict[str, Any]
    trace_id: str | None = None
    span_id: str | None = None
    deploy_id: str | None = None


@dataclass
class EvidencePatternStats:
    event_type: str
    severity_text: str
    message_template: str
    component: str
    source_system: str
    count: int = 0
    first_seen: str = ""
    last_seen: str = ""
    example_sanitized: str = ""
    example_sort_key: tuple[str, str] = ("", "")
    trace_hashes: set[str] = field(default_factory=set)


@dataclass
class BundleEventSummary:
    count: int
    grouped: dict[tuple[str, str, str, str, str], EvidencePatternStats]
    source_counts: Counter[str]
    detected_format_counts: Counter[str]
    profile_supports_inference: bool


class RedactionCounter:
    def __init__(self) -> None:
        self.counts: Counter[str] = Counter()

    def add(self, key: str, count: int = 1) -> None:
        if count > 0:
            self.counts[key] += count

    def summary(self) -> dict[str, int]:
        return {key: int(self.counts.get(key, 0)) for key in SUMMARY_KEYS}

    def report(self) -> dict[str, Any]:
        summary = self.summary()
        examples = [
            {"type": key, "replacement": EXAMPLE_REPLACEMENTS[key], "count": count}
            for key, count in summary.items()
            if count
        ]
        if not examples:
            examples.append({"type": "secret_like", "replacement": "<REDACTED_SECRET>", "count": 0})
        return {
            "schema_version": "redaction_report.v1",
            "sanitizer_version": SANITIZER_VERSION,
            "raw_log_policy": RAW_LOG_POLICY,
            "summary": summary,
            "examples": examples,
        }


def inspect_input(input_path: str | Path) -> dict[str, Any]:
    format_counts: Counter[str] = Counter()
    sources: Counter[str] = Counter()
    timestamp_fields: Counter[str] = Counter()
    severity_fields: Counter[str] = Counter()
    service_candidates: Counter[str] = Counter()
    sensitive_count = 0
    explicit_profile = False
    sample_lines: list[str] = []
    line_count = 0

    for item in iter_input_lines(input_path):
        line_count += 1
        if len(sample_lines) < 50:
            sample_lines.append(item.text)
        detected_format = detect_format(item.text)
        format_counts[detected_format] += 1
        payload = _json_object(item.text)
        if payload:
            for key in TIMESTAMP_KEYS:
                if _lookup(payload, key) not in (None, ""):
                    timestamp_fields[key] += 1
            for key in SEVERITY_KEYS:
                if _lookup(payload, key) not in (None, ""):
                    severity_fields[key] += 1
            for key in SERVICE_KEYS + COMPONENT_KEYS:
                value = _lookup(payload, key)
                if value not in (None, ""):
                    service_candidates[str(value)] += 1
            if payload.get("profile_id") or payload.get("profile"):
                explicit_profile = True
        parsed = parse_line(item)
        if parsed.source_system:
            sources[parsed.source_system] += 1
        if parsed.service:
            service_candidates[parsed.service] += 1
        probe = RedactionCounter()
        redact_text(item.text, probe)
        sensitive_count += sum(probe.summary().values())

    detected_format = _dominant(format_counts, default="plain_text")
    profile_confidence = _profile_confidence(
        explicit_profile=explicit_profile,
        service_candidates=service_candidates,
        detected_format=detected_format,
    )
    return {
        "detected_format": detected_format,
        "detected_sources": _top_keys(sources, fallback=[Path(input_path).name]),
        "timestamp_field_candidates": _top_keys(timestamp_fields),
        "severity_field_candidates": _top_keys(severity_fields),
        "service_component_candidates": _top_keys(service_candidates),
        "sensitive_candidates_count": sensitive_count,
        "profile_confidence": profile_confidence,
        "suggested_system_type": suggest_system_type(
            detected_format=detected_format,
            sources=list(sources),
            service_candidates=list(service_candidates),
            sample_text="\n".join(sample_lines),
        ),
        "line_count": line_count,
    }


def sanitize_input(
    input_path: str | Path,
    output_dir: str | Path,
    *,
    start: str = "",
    end: str = "",
) -> dict[str, Any]:
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    sanitized_path = output / "sanitized_events.jsonl"
    manifest_path = output / "manifest.json"
    report_path = output / "redaction_report.json"
    rejected_path = output / "rejected_lines.jsonl"

    report = RedactionCounter()
    event_count = 0
    rejected_count = 0
    first_timestamp = ""
    last_timestamp = ""
    sources: Counter[str] = Counter()
    format_counts: Counter[str] = Counter()
    service_candidates: Counter[str] = Counter()
    explicit_profile = False
    window_start = format_timestamp(start) if start else ""
    window_end = format_timestamp(end) if end else ""
    window_start_dt = parse_timestamp(window_start) if window_start else None
    window_end_dt = parse_timestamp(window_end) if window_end else None

    with sanitized_path.open("w", encoding="utf-8") as sanitized_file, rejected_path.open("w", encoding="utf-8") as rejected_file:
        for item in iter_input_lines(input_path, include_empty=True, start=window_start, end=window_end):
            if not item.text.strip():
                rejected_count += 1
                rejected_file.write(
                    canonical_json(
                        {
                            "source_path": redact_text(str(item.source_path), report),
                            "line_number": item.line_number,
                            "line_sha256": sha256_text(item.text),
                            "raw_ref": f"sha256:{sha256_text(item.text)[:16]}:{item.source_path.name}:{item.line_number}",
                            "reason": "empty_line",
                        }
                    )
                    + "\n"
                )
                continue
            if (
                window_start_dt is not None
                and window_end_dt is not None
                and _raw_line_is_outside_window(item.text, window_start_dt, window_end_dt)
            ):
                continue
            try:
                parsed = parse_line(item)
                format_counts[parsed.detected_format] += 1
                if parsed.attributes.get("profile_id") or parsed.attributes.get("profile"):
                    explicit_profile = True
                if parsed.service:
                    service_candidates[parsed.service] += 1
                if parsed.component:
                    service_candidates[parsed.component] += 1
                event = normalize_parsed_line(parsed, item, report)
                if (
                    window_start_dt is not None
                    and window_end_dt is not None
                    and not _event_is_in_window(event, window_start_dt, window_end_dt, include_inferred=False)
                ):
                    continue
            except Exception as exc:  # pragma: no cover - defensive fallback, no raw line is persisted.
                rejected_count += 1
                rejected_file.write(
                    canonical_json(
                        {
                            "source_path": redact_text(str(item.source_path), report),
                            "line_number": item.line_number,
                            "line_sha256": sha256_text(item.text),
                            "raw_ref": f"sha256:{sha256_text(item.text)[:16]}:{item.source_path.name}:{item.line_number}",
                            "reason": type(exc).__name__,
                        }
                    )
                    + "\n"
                )
                continue
            sanitized_file.write(canonical_json(event) + "\n")
            event_count += 1
            event_ts = str(event.get("timestamp") or event.get("observed_timestamp") or "")
            if event_ts:
                if not first_timestamp or event_ts < first_timestamp:
                    first_timestamp = event_ts
                if not last_timestamp or event_ts > last_timestamp:
                    last_timestamp = event_ts
            sources[str(event.get("source_system") or "")] += 1

    redacted_input_path = redact_text(str(Path(input_path)), report)
    redaction_report = report.report()
    report_path.write_text(pretty_json(redaction_report) + "\n", encoding="utf-8")
    redaction_summary = redaction_report["summary"]
    redaction_total = sum(int(value) for value in redaction_summary.values())
    detected_format = _dominant(format_counts, default="plain_text")
    profile_confidence = _profile_confidence(
        explicit_profile=explicit_profile,
        service_candidates=service_candidates,
        detected_format=detected_format,
    )
    manifest = {
        "schema_version": "sanitized_events_manifest.v1",
        "created_at": utc_now(),
        "input_path": redacted_input_path,
        "raw_log_policy": RAW_LOG_POLICY,
        "sanitizer_version": SANITIZER_VERSION,
        "event_count": event_count,
        "rejected_count": rejected_count,
        "detected_format": detected_format,
        "profile_confidence": profile_confidence,
        "source_system": _dominant(sources, default=detected_format),
        "time_range": {
            "start": first_timestamp,
            "end": last_timestamp,
        },
        "input_time_window": {"start": window_start, "end": window_end} if window_start and window_end else {},
        "outputs": {
            "sanitized_events": "sanitized_events.jsonl",
            "redaction_report": "redaction_report.json",
            "rejected_lines": "rejected_lines.jsonl",
        },
        "local_first_summary": {
            "raw_logs_uploaded": False,
            "raw_log_policy": RAW_LOG_POLICY,
            "sanitized_event_count": event_count,
            "redaction_total": redaction_total,
            "detected_format": detected_format,
            "profile_confidence": profile_confidence,
            "evidence_sha256": "",
        },
    }
    manifest_path.write_text(pretty_json(manifest) + "\n", encoding="utf-8")
    return {
        "sanitized_events": str(sanitized_path),
        "manifest": str(manifest_path),
        "redaction_report": str(report_path),
        "rejected_lines": str(rejected_path),
        "event_count": event_count,
        "rejected_count": rejected_count,
    }


def build_bundle_from_sanitized(
    sanitized_events_path: str | Path,
    *,
    service: str,
    environment: str,
    start: str,
    end: str,
    profile_name: str,
    out_path: str | Path,
    parent_evidence_sha256: str = "",
    evidence_request_plan_id: str = "",
    collection_mode: str = "",
) -> dict[str, Any]:
    input_path = Path(sanitized_events_path)
    window_start = format_timestamp(start)
    window_end = format_timestamp(end)
    selected_summary, all_summary = _summarize_sanitized_events(input_path, window_start, window_end)
    summary = selected_summary if selected_summary.count else all_summary
    profile, profile_confidence = _resolve_profile(
        profile_name,
        profile_supports_inference=summary.profile_supports_inference,
    )
    profile_id = str(profile.get("profile_id") or normalize_profile_id(profile_name))
    profile_display_name = str(profile.get("profile_label") or profile_id or profile_name or "unknown")
    profile_redactions = RedactionCounter()
    profile_context = {
        "system_profile": redact_mapping(_system_profile(profile), profile_redactions),
        "operational_contract": redact_mapping(profile.get("operational_contract") or {}, profile_redactions),
        "log_sources": redact_mapping(profile.get("log_sources") or [], profile_redactions),
        "metric_semantics": redact_mapping(profile.get("metric_semantics") or profile.get("metrics") or {}, profile_redactions),
        "component_map": redact_mapping(profile.get("component_map") or {}, profile_redactions),
        "known_benign_noise": redact_mapping(profile.get("known_benign_noise") or [], profile_redactions),
        "action_constraints": redact_mapping(profile.get("action_constraints") or [], profile_redactions),
        "query_mappings": redact_mapping(profile.get("query_mappings") or {}, profile_redactions),
    }
    source_system = _source_system_for_bundle(summary.source_counts, profile, service)
    evidence_items = build_evidence_items_from_summary(summary)
    evidence_relationships = build_evidence_relationships_from_summary(summary, evidence_items)
    signals = build_signals(evidence_items)
    redaction_summary = _merge_redaction_summaries(_load_redaction_summary(input_path), profile_redactions.summary())
    manifest = _load_manifest(input_path)
    detected_format = str(manifest.get("detected_format") or _dominant(summary.detected_format_counts, default="unknown"))
    analysis_policy = analysis_policy_for_profile(profile_confidence)
    local_first_summary = {
        "raw_logs_uploaded": False,
        "raw_log_policy": RAW_LOG_POLICY,
        "sanitized_event_count": summary.count,
        "redaction_total": sum(int(value) for value in redaction_summary.values()),
        "detected_format": detected_format,
        "profile_confidence": profile_confidence,
        "evidence_sha256": "",
    }
    display_summary = {
        "title": "Local-first sanitized evidence bundle",
        "subtitle": "Raw logs were not uploaded. Sanitized events were verified locally.",
        "primary_badges": [
            f"raw_log_policy:{RAW_LOG_POLICY}",
            "verify_sanitized:passed",
            f"profile_confidence:{profile_confidence}",
        ],
    }
    bundle: dict[str, Any] = {
        "schema_version": "evidence_bundle.v1",
        "bundle_type": "sanitized_evidence_bundle",
        "raw_log_policy": RAW_LOG_POLICY,
        "canonicalization_version": CANONICALIZATION_VERSION,
        "evidence_sha256": "",
        "source": {
            "source_system": source_system,
            "service": service,
            "environment": environment,
            "detected_format": detected_format,
            "profile_name": profile_display_name,
            "profile_confidence": profile_confidence,
        },
        "time_window": {"start": window_start, "end": window_end},
        "analysis_policy": analysis_policy,
        "system_profile": profile_context["system_profile"],
        "operational_contract": profile_context["operational_contract"],
        "log_sources": profile_context["log_sources"],
        "metric_semantics": profile_context["metric_semantics"],
        "component_map": profile_context["component_map"],
        "known_benign_noise": profile_context["known_benign_noise"],
        "action_constraints": profile_context["action_constraints"],
        "query_mappings": profile_context["query_mappings"],
        "local_first_summary": local_first_summary,
        "display_summary": display_summary,
        "redaction_summary": redaction_summary,
        "required_profile_questions": REQUIRED_PROFILE_QUESTIONS if analysis_policy["require_profile_questions"] else [],
        "evidence_items": evidence_items,
        "evidence_relationships": evidence_relationships,
        "signals": signals,
        "prompt_rules": ai_evidence_rules(),
    }
    if parent_evidence_sha256:
        bundle["parent_evidence_sha256"] = str(parent_evidence_sha256)
    if evidence_request_plan_id:
        bundle["evidence_request_plan_id"] = str(evidence_request_plan_id)
    if collection_mode:
        bundle["collection_mode"] = str(collection_mode)
        bundle["raw_output_policy"] = "local_only"
        bundle["sanitize_before_upload"] = True
        bundle["verify_sanitized_required"] = True
    bundle["evidence_sha256"] = sha256_json(_bundle_hash_payload(bundle))
    bundle["local_first_summary"]["evidence_sha256"] = bundle["evidence_sha256"]
    output = Path(out_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(pretty_json(bundle) + "\n", encoding="utf-8")
    return bundle


def _summarize_sanitized_events(
    input_path: Path,
    window_start: str,
    window_end: str,
) -> tuple[BundleEventSummary, BundleEventSummary]:
    selected = _empty_bundle_event_summary()
    all_events = _empty_bundle_event_summary()
    start_dt = parse_timestamp(window_start)
    end_dt = parse_timestamp(window_end)
    with input_path.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            if not line.strip():
                continue
            event = _json_object(line)
            if not isinstance(event, dict):
                continue
            _add_event_to_summary(all_events, event)
            if _event_is_in_window(event, start_dt, end_dt):
                _add_event_to_summary(selected, event)
    return selected, all_events


def _empty_bundle_event_summary() -> BundleEventSummary:
    return BundleEventSummary(
        count=0,
        grouped={},
        source_counts=Counter(),
        detected_format_counts=Counter(),
        profile_supports_inference=False,
    )


def _add_event_to_summary(summary: BundleEventSummary, event: dict[str, Any]) -> None:
    summary.count += 1
    source_system = str(event.get("source_system") or "")
    if source_system:
        summary.source_counts[source_system] += 1
    attrs = event.get("attributes") if isinstance(event.get("attributes"), dict) else {}
    detected_format = str(attrs.get("detected_format") or "")
    if detected_format:
        summary.detected_format_counts[detected_format] += 1
    if _event_supports_profile_inference(event):
        summary.profile_supports_inference = True
    key = (
        str(event.get("event_type") or "unknown"),
        str(event.get("severity_text") or "info"),
        str(event.get("message_template") or ""),
        str(event.get("component") or "unknown"),
        str(event.get("source_system") or "generic"),
    )
    event_time = _event_time(event)
    event_id = str(event.get("event_id") or "")
    stats = summary.grouped.get(key)
    if stats is None:
        stats = EvidencePatternStats(
            event_type=key[0],
            severity_text=key[1],
            message_template=key[2],
            component=key[3],
            source_system=key[4],
        )
        summary.grouped[key] = stats
    stats.count += 1
    if not stats.first_seen or event_time < stats.first_seen:
        stats.first_seen = event_time
    if not stats.last_seen or event_time > stats.last_seen:
        stats.last_seen = event_time
    sort_key = (event_time, event_id)
    if not stats.example_sanitized or sort_key < stats.example_sort_key:
        stats.example_sanitized = str(event.get("message_sanitized") or "")
        stats.example_sort_key = sort_key
    trace_id = str(event.get("trace_id") or attrs.get("trace_id") or "").strip()
    if trace_id:
        stats.trace_hashes.add(sha256_text(trace_id))


def _event_is_in_window(
    event: dict[str, Any],
    start_dt: datetime,
    end_dt: datetime,
    *,
    include_inferred: bool = True,
) -> bool:
    attrs = event.get("attributes") if isinstance(event.get("attributes"), dict) else {}
    if attrs.get("timestamp_inferred") and include_inferred:
        return True
    value = event.get("timestamp") or event.get("observed_timestamp")
    try:
        parsed = parse_timestamp(str(value))
    except (TypeError, ValueError):
        return True
    return start_dt <= parsed <= end_dt


def _raw_line_is_outside_window(line: str, start_dt: datetime, end_dt: datetime) -> bool:
    parsed = _raw_line_timestamp(line)
    if parsed is None:
        return False
    return parsed < start_dt or parsed > end_dt


def _raw_line_timestamp(line: str) -> datetime | None:
    match = ISO_TS_RE.search(line[:512])
    if not match:
        return None
    try:
        return parse_timestamp(match.group(0))
    except (TypeError, ValueError):
        return None


def build_evidence_items(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str, str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for event in events:
        key = (
            str(event.get("event_type") or "unknown"),
            str(event.get("severity_text") or "info"),
            str(event.get("message_template") or ""),
            str(event.get("component") or "unknown"),
            str(event.get("source_system") or "generic"),
        )
        grouped[key].append(event)
    items: list[dict[str, Any]] = []
    for index, (key, rows) in enumerate(
        sorted(grouped.items(), key=lambda item: (-len(item[1]), item[0][0], item[0][2], item[0][3], item[0][4])),
        start=1,
    ):
        event_type, severity_text, template, component, _source = key
        sorted_rows = sorted(rows, key=lambda row: (_event_time(row), str(row.get("event_id") or "")))
        items.append(
            {
                "evidence_id": f"PATTERN-{index:03d}",
                "type": "log_pattern",
                "event_type": event_type,
                "severity_text": severity_text,
                "count": len(rows),
                "first_seen": _event_time(sorted_rows[0]),
                "last_seen": _event_time(sorted_rows[-1]),
                "message_template": template,
                "example_sanitized": str(sorted_rows[0].get("message_sanitized") or ""),
                "component": component,
                "source": "sanitized_events",
            }
        )
    return items


def build_evidence_items_from_summary(summary: BundleEventSummary) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for index, (key, stats) in enumerate(
        sorted(summary.grouped.items(), key=lambda item: (-item[1].count, item[0][0], item[0][2], item[0][3], item[0][4])),
        start=1,
    ):
        event_type, severity_text, template, component, _source = key
        items.append(
            {
                "evidence_id": f"PATTERN-{index:03d}",
                "type": "log_pattern",
                "event_type": event_type,
                "severity_text": severity_text,
                "count": stats.count,
                "first_seen": stats.first_seen,
                "last_seen": stats.last_seen,
                "message_template": template,
                "example_sanitized": stats.example_sanitized,
                "component": component,
                "source": "sanitized_events",
                "trace_id_count": len(stats.trace_hashes),
            }
        )
    return items


def build_evidence_relationships_from_summary(
    summary: BundleEventSummary,
    evidence_items: list[dict[str, Any]],
) -> dict[str, Any]:
    ordered_stats = [
        stats
        for _key, stats in sorted(
            summary.grouped.items(),
            key=lambda item: (-item[1].count, item[0][0], item[0][2], item[0][3], item[0][4]),
        )
    ]
    trace_sets = {
        str(item.get("evidence_id") or ""): set(stats.trace_hashes)
        for item, stats in zip(evidence_items, ordered_stats)
        if item.get("evidence_id")
    }
    item_by_id = {str(item.get("evidence_id") or ""): item for item in evidence_items if item.get("evidence_id")}
    high_signal_ids = [
        evidence_id
        for evidence_id, item in sorted(
            item_by_id.items(),
            key=lambda pair: (-_severity_rank(str(pair[1].get("severity_text") or "info")), -int(pair[1].get("count") or 0), pair[0]),
        )
        if _severity_rank(str(item.get("severity_text") or "info")) >= _severity_rank("warning")
    ][:32]
    relationships: list[dict[str, Any]] = []
    for left_id, right_id in combinations(high_signal_ids, 2):
        left = item_by_id[left_id]
        right = item_by_id[right_id]
        left_traces = trace_sets.get(left_id, set())
        right_traces = trace_sets.get(right_id, set())
        if not left_traces or not right_traces:
            continue
        shared = len(left_traces.intersection(right_traces))
        if not shared and not _evidence_time_ranges_overlap(left, right):
            continue
        relationships.append(
            {
                "relationship_type": "shared_trace" if shared else "overlapping_window_no_shared_trace",
                "left_evidence_id": left_id,
                "right_evidence_id": right_id,
                "shared_trace_count": shared,
                "left_trace_count": len(left_traces),
                "right_trace_count": len(right_traces),
                "left_trace_coverage_ratio": round(shared / len(left_traces), 6),
                "right_trace_coverage_ratio": round(shared / len(right_traces), 6),
                "raw_trace_ids_exposed": False,
            }
        )
    deployment_items = [item for item in evidence_items if _is_deployment_evidence_item(item)]
    high_signal_items = [item_by_id[evidence_id] for evidence_id in high_signal_ids]
    for deployment in deployment_items:
        try:
            deployment_time = parse_timestamp(str(deployment.get("last_seen") or deployment.get("first_seen") or ""))
        except (TypeError, ValueError):
            continue
        for signal in high_signal_items:
            try:
                signal_time = parse_timestamp(str(signal.get("first_seen") or ""))
            except (TypeError, ValueError):
                continue
            gap_seconds = int((signal_time - deployment_time).total_seconds())
            if gap_seconds < 0 or gap_seconds > 1800:
                continue
            relationships.append(
                {
                    "relationship_type": "deployment_precedes_signal",
                    "left_evidence_id": str(deployment.get("evidence_id") or ""),
                    "right_evidence_id": str(signal.get("evidence_id") or ""),
                    "gap_seconds": gap_seconds,
                    "causality_established": False,
                    "raw_trace_ids_exposed": False,
                }
            )
    return {
        "schema_version": "evidence_relationships.v1",
        "relationship_count": len(relationships),
        "trace_id_policy": "hashed_for_local_correlation_not_exposed",
        "relationships": relationships,
    }


def _evidence_time_ranges_overlap(left: dict[str, Any], right: dict[str, Any]) -> bool:
    try:
        left_start = parse_timestamp(str(left.get("first_seen") or ""))
        left_end = parse_timestamp(str(left.get("last_seen") or ""))
        right_start = parse_timestamp(str(right.get("first_seen") or ""))
        right_end = parse_timestamp(str(right.get("last_seen") or ""))
    except (TypeError, ValueError):
        return False
    return max(left_start, right_start) <= min(left_end, right_end)


def _is_deployment_evidence_item(item: dict[str, Any]) -> bool:
    text = " ".join(
        str(item.get(key) or "")
        for key in ("event_type", "message_template", "example_sanitized")
    ).casefold()
    return "deploy rollout" in text or "deployment completed" in text


def build_signals(evidence_items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for item in evidence_items:
        event_type = str(item.get("event_type") or "unknown")
        target = CORE_TARGET_BY_EVENT_TYPE.get(event_type, "general")
        grouped[(event_type, target)].append(item)
    signals: list[dict[str, Any]] = []
    for index, ((event_type, target), rows) in enumerate(
        sorted(grouped.items(), key=lambda item: (item[0][1], item[0][0])),
        start=1,
    ):
        count = sum(int(row.get("count") or 0) for row in rows)
        severity = max((str(row.get("severity_text") or "info") for row in rows), key=_severity_rank)
        signals.append(
            {
                "signal_id": f"SIG-{index:03d}",
                "signal_type": event_type,
                "core_target_type": target,
                "evidence_refs": [str(row["evidence_id"]) for row in rows],
                "component": _dominant(Counter(str(row.get("component") or "unknown") for row in rows), default="unknown"),
                "count": count,
                "confidence": _signal_confidence(event_type, severity, count),
            }
        )
    return signals


def analysis_policy_for_profile(profile_confidence: str) -> dict[str, Any]:
    profile_mode = str(profile_confidence or "unknown")
    explicit = profile_mode == "explicit"
    return {
        "profile_mode": profile_mode if profile_mode in {"unknown", "inferred", "explicit"} else "unknown",
        "explicit_profile": explicit,
        "allow_primary_candidate": explicit,
        "prefer_generic_signals": not explicit,
        "require_profile_questions": not explicit,
    }


def verify_sanitized_output(output_dir: str | Path) -> dict[str, Any]:
    base = Path(output_dir)
    findings: list[dict[str, Any]] = []
    counts: Counter[str] = Counter()
    checked_files = 0
    raw_log_policy = ""

    for filename in VERIFY_TARGET_FILES:
        path = base / filename
        if not path.exists():
            continue
        checked_files += 1
        if filename in {"manifest.json", "evidence_bundle.json"}:
            raw_log_policy = raw_log_policy or _raw_policy_from_file(path)
        scan = scan_sanitized_file(path, filename=filename)
        findings.extend(scan["findings"])
        counts.update(scan["counts"])

    if raw_log_policy and raw_log_policy != RAW_LOG_POLICY:
        findings.append({"file": "manifest/evidence_bundle", "line": None, "type": "raw_log_policy"})
        counts["raw_log_policy"] += 1

    return {
        "passed": not findings,
        "checked_files": checked_files,
        "secret_like_patterns": int(counts.get("secret_like", 0)),
        "raw_email_patterns": int(counts.get("raw_email", 0)),
        "raw_ip_patterns": int(counts.get("raw_ip", 0)),
        "raw_home_paths": int(counts.get("raw_home_path", 0)),
        "raw_internal_urls": int(counts.get("raw_internal_url", 0)),
        "raw_log_policy": raw_log_policy or "unknown",
        "findings": findings,
    }


def validate_evidence_bundle_for_upload(bundle: dict[str, Any]) -> dict[str, Any]:
    errors: list[dict[str, Any]] = []
    if not isinstance(bundle, dict):
        return {
            "passed": False,
            "errors": [{"type": "invalid_payload", "field": "bundle"}],
            "findings": [],
            "expected_evidence_sha256": "",
            "actual_evidence_sha256": "",
        }

    source = bundle.get("source") if isinstance(bundle.get("source"), dict) else {}
    summary = bundle.get("local_first_summary") if isinstance(bundle.get("local_first_summary"), dict) else {}
    policy = bundle.get("analysis_policy") if isinstance(bundle.get("analysis_policy"), dict) else {}
    required = {
        "schema_version": "evidence_bundle.v1",
        "bundle_type": "sanitized_evidence_bundle",
        "raw_log_policy": RAW_LOG_POLICY,
    }
    for field, expected in required.items():
        if bundle.get(field) != expected:
            errors.append({"type": "contract_mismatch", "field": field})
    if summary.get("raw_logs_uploaded") is not False:
        errors.append({"type": "contract_mismatch", "field": "local_first_summary.raw_logs_uploaded"})
    if source.get("profile_confidence") not in {"unknown", "inferred", "explicit"}:
        errors.append({"type": "contract_mismatch", "field": "source.profile_confidence"})
    for field in ("analysis_policy", "evidence_items", "signals", "prompt_rules", "local_first_summary", "source", "time_window"):
        if field not in bundle:
            errors.append({"type": "missing_field", "field": field})
    if not isinstance(bundle.get("evidence_items"), list):
        errors.append({"type": "contract_mismatch", "field": "evidence_items"})
    if not isinstance(bundle.get("signals"), list):
        errors.append({"type": "contract_mismatch", "field": "signals"})
    if not isinstance(bundle.get("prompt_rules"), list):
        errors.append({"type": "contract_mismatch", "field": "prompt_rules"})

    expected_sha = sha256_json(_bundle_hash_payload(bundle))
    actual_sha = str(bundle.get("evidence_sha256") or "")
    if actual_sha != expected_sha:
        errors.append({"type": "evidence_sha256_mismatch", "field": "evidence_sha256"})

    scan = scan_sanitized_text("evidence_bundle.json", canonical_json(bundle))
    findings = list(scan["findings"])
    if findings:
        errors.append({"type": "unsafe_content", "field": "evidence_bundle"})

    return {
        "passed": not errors,
        "errors": errors,
        "findings": findings,
        "expected_evidence_sha256": expected_sha,
        "actual_evidence_sha256": actual_sha,
        "profile_mode": str(policy.get("profile_mode") or source.get("profile_confidence") or "unknown"),
        "allow_primary_candidate": bool(policy.get("allow_primary_candidate")),
    }


def scan_sanitized_text(filename: str, text: str) -> dict[str, Any]:
    findings: list[dict[str, Any]] = []
    counts: Counter[str] = Counter()
    for line_number, line in enumerate(str(text).splitlines() or [str(text)], start=1):
        scan = _scan_sanitized_line(filename, line, line_number)
        findings.extend(scan["findings"])
        counts.update(scan["counts"])
    return {"findings": findings, "counts": counts}


def scan_sanitized_file(path: str | Path, *, filename: str | None = None) -> dict[str, Any]:
    target = Path(path)
    display_name = filename or target.name
    findings: list[dict[str, Any]] = []
    counts: Counter[str] = Counter()
    with target.open("r", encoding="utf-8", errors="replace") as handle:
        saw_line = False
        for line_number, line in enumerate(handle, start=1):
            saw_line = True
            scan = _scan_sanitized_line(display_name, line.rstrip("\r\n"), line_number)
            findings.extend(scan["findings"])
            counts.update(scan["counts"])
        if not saw_line:
            scan = _scan_sanitized_line(display_name, "", 1)
            findings.extend(scan["findings"])
            counts.update(scan["counts"])
    return {"findings": findings, "counts": counts}


def _scan_sanitized_line(filename: str, line: str, line_number: int) -> dict[str, Any]:
    findings: list[dict[str, Any]] = []
    counts: Counter[str] = Counter()
    if not _needs_verification_scan(line):
        return {"findings": findings, "counts": counts}
    safe_line = ALLOWED_PLACEHOLDER_RE.sub("", line)
    for pattern_type, pattern in VERIFY_SECRET_PATTERNS:
        if pattern.search(safe_line):
            findings.append({"file": filename, "line": line_number, "type": pattern_type})
            counts[pattern_type] += 1
            break
    if EMAIL_RE.search(safe_line):
        findings.append({"file": filename, "line": line_number, "type": "raw_email"})
        counts["raw_email"] += 1
    if IPV4_RE.search(safe_line):
        findings.append({"file": filename, "line": line_number, "type": "raw_ip"})
        counts["raw_ip"] += 1
    if USER_HOME_RE.search(safe_line):
        findings.append({"file": filename, "line": line_number, "type": "raw_home_path"})
        counts["raw_home_path"] += 1
    if VERIFY_INTERNAL_URL_RE.search(safe_line):
        findings.append({"file": filename, "line": line_number, "type": "raw_internal_url"})
        counts["raw_internal_url"] += 1
    elif VERIFY_INTERNAL_DOMAIN_RE.search(safe_line):
        findings.append({"file": filename, "line": line_number, "type": "raw_internal_url"})
        counts["raw_internal_url"] += 1
    return {"findings": findings, "counts": counts}


def _needs_verification_scan(line: str) -> bool:
    folded = line.casefold()
    if any(token in folded for token in VERIFY_SCAN_TOKENS):
        return True
    if "@" in line:
        return True
    return "." in line and IPV4_RE.search(line) is not None


def format_verification_result(result: dict[str, Any]) -> str:
    status = "passed" if result.get("passed") else "failed"
    lines = [f"Sanitized output verification: {status}"]
    if not result.get("passed"):
        for finding in result.get("findings") or []:
            line = finding.get("line")
            suffix = f" at line {line}" if line else ""
            lines.append(f"- {finding.get('file')}: {finding.get('type')} pattern remained{suffix}")
    lines.extend(
        [
            f"Checked files: {result.get('checked_files', 0)}",
            f"Secret-like patterns: {result.get('secret_like_patterns', 0)}",
            f"Raw email patterns: {result.get('raw_email_patterns', 0)}",
            f"Raw IP patterns: {result.get('raw_ip_patterns', 0)}",
            f"Raw home paths: {result.get('raw_home_paths', 0)}",
            f"Raw internal URLs: {result.get('raw_internal_urls', 0)}",
            f"Raw log policy: {result.get('raw_log_policy') or 'unknown'}",
        ]
    )
    return "\n".join(lines)


def normalize_parsed_line(parsed: ParsedLine, item: InputLine, report: RedactionCounter) -> dict[str, Any]:
    message_sanitized = redact_text(parsed.message, report)
    attributes = redact_mapping(parsed.attributes, report)
    source_path = redact_text(str(item.source_path), report)
    attributes["source_path"] = source_path
    attributes["source_line"] = item.line_number
    attributes["detected_format"] = parsed.detected_format
    if parsed.timestamp_inferred:
        attributes["timestamp_inferred"] = True
    event_type = infer_event_type(message_sanitized, parsed.severity_text, attributes)
    payload: dict[str, Any] = {
        "timestamp": parsed.timestamp,
        "observed_timestamp": parsed.observed_timestamp,
        "source_system": redact_text(parsed.source_system or parsed.detected_format, report),
        "service": redact_text(parsed.service or item.source_path.stem or "unknown", report),
        "environment": redact_text(parsed.environment or "unknown", report),
        "host": redact_text(parsed.host or "", report),
        "component": redact_text(parsed.component or "unknown", report),
        "severity_text": normalize_severity(parsed.severity_text),
        "severity_number": parsed.severity_number,
        "event_type": event_type,
        "message_template": message_template(message_sanitized),
        "message_sanitized": message_sanitized,
        "attributes": attributes,
        "trace_id": redact_text(parsed.trace_id, report) if parsed.trace_id else None,
        "span_id": redact_text(parsed.span_id, report) if parsed.span_id else None,
        "deploy_id": redact_text(parsed.deploy_id, report) if parsed.deploy_id else None,
        "raw_ref": f"sha256:{sha256_text(item.text)[:16]}:{item.source_path.name}:{item.line_number}",
        "sanitizer_version": SANITIZER_VERSION,
    }
    if payload["timestamp"] is None:
        payload.pop("timestamp")
    payload["event_id"] = "EV-" + sha256_json(payload)[:16]
    return payload


def parse_line(item: InputLine) -> ParsedLine:
    detected = detect_format(item.text)
    payload = _json_object(item.text)
    if payload:
        return _parse_json_line(payload, item, detected)
    if detected == "kubernetes_container_log":
        return _parse_kubernetes_line(item)
    if detected in {"nginx_access", "apache_access"}:
        return _parse_access_line(item, detected)
    if detected == "syslog":
        return _parse_syslog_line(item)
    if detected in {"logfmt", "key_value"}:
        return _parse_key_value_line(item, detected)
    return _parse_plain_line(item, detected)


def detect_format(line: str) -> str:
    stripped = line.strip()
    if not stripped:
        return "plain_text"
    payload = _json_object(stripped)
    if payload is not None:
        if any(key in payload for key in ("__REALTIME_TIMESTAMP", "_SYSTEMD_UNIT", "SYSLOG_IDENTIFIER", "PRIORITY", "MESSAGE")):
            return "journald_json"
        return "jsonl"
    if K8S_RE.match(stripped):
        return "kubernetes_container_log"
    if ACCESS_RE.match(stripped):
        return "nginx_access" if any(token in stripped for token in ("rt=", "upstream", "request_time")) else "apache_access"
    if PY_TRACE_RE.search(stripped):
        return "python_traceback"
    if JAVA_TRACE_RE.search(stripped):
        return "java_stacktrace"
    if SYSLOG_RE.match(stripped):
        return "syslog"
    if len(KEY_VALUE_RE.findall(stripped)) >= 2:
        return "logfmt"
    if KEY_VALUE_RE.search(stripped) or KEY_COLON_RE.search(stripped):
        return "key_value"
    return "plain_text"


def infer_event_type(message: str, severity_text: str, attributes: dict[str, Any] | None = None) -> str:
    message_text = message.casefold()
    text = " ".join([message, canonical_json(attributes or {})]).casefold()
    if "no such file or directory" in text:
        if any(term in text for term in ("can't open file", "execstart", "executable", "command", ".service", ".py", ".sh")):
            return "missing_command"
        return "missing_file"
    if "command not found" in text or "executable file not found" in text:
        return "missing_command"
    if "permission denied" in text or "operation not permitted" in text or "access denied" in text:
        return "permission_denied"
    if "failed with result 'exit-code'" in text or "failed to start" in text or "main process exited" in text:
        return "service_start_failure"
    if "start request repeated too quickly" in text or "restart loop" in text or "crashloop" in text or "back-off restarting" in text:
        return "restart_loop"
    if "process exited" in text or "exited with status" in text or "exit status" in text:
        return "process_exit"
    if "connection reset by peer" in text or "connection reset" in text:
        return "connection_reset"
    if (
        "http 5" in message_text
        or re.search(r"\b(?:status|status_code|http_status|response_status)\s*[=:]\s*5\d\d\b", message_text)
        or _attributes_contain_http_5xx(attributes or {})
    ):
        return "http_5xx"
    if "timed out" in text or "timeout" in text or "deadline exceeded" in text:
        return "timeout"
    if "temporary failure in name resolution" in text or "dns" in text and any(term in text for term in ("failure", "failed", "nxdomain")):
        return "dns_failure"
    if any(term in text for term in ("unauthorized", "forbidden", "authentication failed", "authorization failed", "invalid credentials")):
        return "auth_failure"
    if any(term in text for term in ("invalid config", "configuration error", "config_error", "bad configuration")):
        return "config_error"
    if any(term in text for term in ("connection refused", "unreachable", "no route to host", "upstream unavailable")):
        return "dependency_unreachable"
    if "out of memory" in text or "oom" in text or "memory cgroup out of memory" in text:
        return "oom"
    if any(term in text for term in ("state mismatch", "status mismatch", "expected state", "actual state", "contradicts", "healthy but")):
        return "state_mismatch"
    if any(
        term in text
        for term in (
            "no logs received",
            "log gap",
            "metric missing",
            "metrics missing",
            "heartbeat missing",
            "freshness gap",
            "stale data",
            "no samples",
        )
    ):
        return "monitoring_gap"
    if any(
        term in text
        for term in (
            "instrumentation mismatch",
            "schema mismatch",
            "parser failed",
            "parse failed",
            "missing label",
            "missing metric label",
            "unknown field",
        )
    ):
        return "instrumentation_mismatch"
    severity = normalize_severity(severity_text)
    if severity in {"warn", "warning"}:
        return "warning"
    if severity in {"info", "notice", "debug"}:
        return "info"
    return "unknown"


def _attributes_contain_http_5xx(attributes: dict[str, Any]) -> bool:
    status_keys = {"status", "status_code", "http_status", "response_status", "response_code"}
    for key, value in attributes.items():
        normalized_key = str(key).strip().casefold().replace("-", "_")
        if normalized_key in status_keys:
            match = re.search(r"\b(\d{3})\b", str(value))
            if match and 500 <= int(match.group(1)) <= 599:
                return True
        if isinstance(value, dict) and _attributes_contain_http_5xx(value):
            return True
    return False


def message_template(message: str) -> str:
    text = message
    text = REDACTION_TOKEN_RE.sub(lambda match: "<" + match.group(0).split("_", 1)[0].lstrip("<") + ">", text)
    text = re.sub(r"<REDACTED_SECRET>", "<SECRET>", text)
    text = INTERNAL_URL_RE.sub("<URL>", text)
    text = EMAIL_RE.sub("<EMAIL>", text)
    text = IPV4_RE.sub("<IP>", text)
    text = ISO_TS_RE.sub("<TIMESTAMP>", text)
    text = UUID_RE.sub("<UUID>", text)
    text = HASH_RE.sub("<HASH>", text)
    text = WINDOWS_PATH_RE.sub("<PATH>", text)
    text = POSIX_PATH_RE.sub("<PATH>", text)
    text = NUMBER_RE.sub("<NUM>", text)
    return _compact_ws(text)


def redact_text(value: Any, report: RedactionCounter | None = None) -> str:
    if value is None:
        return ""
    report = report or RedactionCounter()
    text = str(value)
    if not _needs_redaction_scan(text):
        return _compact_ws(text)

    text = _sub_count(USER_HOME_RE, text, lambda _m: "<USER_HOME>", report, "user_home")
    text = _sub_count(INTERNAL_URL_RE, text, lambda match: f"<URL_HASH:{_hash_prefix(match.group(0))}>", report, "internal_url")

    def auth_repl(match: re.Match[str]) -> str:
        report.add("authorization_header")
        report.add("token_like")
        if match.group(0).casefold().find("basic") >= 0:
            report.add("basic_auth")
        return "<REDACTED_SECRET>"

    text = AUTH_HEADER_RE.sub(auth_repl, text)

    def cookie_repl(match: re.Match[str]) -> str:
        report.add("cookie")
        report.add("secret_like")
        return "<REDACTED_SECRET>"

    text = COOKIE_RE.sub(cookie_repl, text)

    def secret_repl(match: re.Match[str]) -> str:
        key = match.group(1)
        folded = key.casefold()
        report.add("secret_like")
        if "token" in folded or "session" in folded:
            report.add("token_like")
        if "gmail" in folded:
            report.add("gmail_credential")
        if "pubsub" in folded:
            report.add("pubsub_credential")
        if "google" in folded or "credential" in folded or "service_account" in folded:
            report.add("cloud_credential")
        return "<REDACTED_SECRET>"

    text = SECRET_KV_RE.sub(secret_repl, text)

    def id_repl(match: re.Match[str]) -> str:
        key = match.group(1)
        raw = _strip_quotes(match.group(2))
        report.add("id_like")
        return f"{key}=<ID_HASH:{_hash_prefix(raw)}>"

    text = ID_KV_RE.sub(id_repl, text)

    text = _sub_count(GOOGLE_API_KEY_RE, text, lambda _m: "<REDACTED_SECRET>", report, "cloud_credential", extra=("secret_like",))
    text = _sub_count(GOOGLE_OAUTH_RE, text, lambda _m: "<REDACTED_SECRET>", report, "cloud_credential", extra=("token_like",))
    text = _sub_count(JWT_RE, text, lambda _m: "<REDACTED_SECRET>", report, "token_like", extra=("secret_like",))
    text = _sub_count(SK_KEY_RE, text, lambda _m: "<REDACTED_SECRET>", report, "secret_like")
    text = _sub_count(PRIVATE_KEY_BLOCK_RE, text, lambda _m: "<REDACTED_SECRET>", report, "secret_like")
    text = _sub_count(BEARER_RE, text, lambda _m: "<REDACTED_SECRET>", report, "token_like")
    text = _sub_count(BASIC_RE, text, lambda _m: "<REDACTED_SECRET>", report, "basic_auth", extra=("token_like",))
    text = _sub_count(EMAIL_RE, text, lambda match: f"<EMAIL_HASH:{_hash_prefix(match.group(0).casefold())}>", report, "email")
    text = _sub_count(IPV4_RE, text, lambda match: f"<IP_HASH:{_hash_prefix(match.group(0))}>", report, "ip_address")
    return _compact_ws(text)


def redact_mapping(value: Any, report: RedactionCounter) -> Any:
    if isinstance(value, dict):
        clean: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            folded = key_text.casefold()
            if _is_sensitive_key(folded):
                clean[_unique_key(clean, "redacted_secret")] = "<REDACTED_SECRET>"
                report.add("secret_like")
                if "token" in folded or "session" in folded:
                    report.add("token_like")
                if "gmail" in folded:
                    report.add("gmail_credential")
                if "pubsub" in folded:
                    report.add("pubsub_credential")
                if "google" in folded or "credential" in folded or "private_key" in folded:
                    report.add("cloud_credential")
            elif _is_id_key(folded) and item not in (None, ""):
                clean[key_text] = f"<ID_HASH:{_hash_prefix(str(item))}>"
                report.add("id_like")
            else:
                clean[key_text] = redact_mapping(item, report)
        return clean
    if isinstance(value, list):
        return [redact_mapping(item, report) for item in value]
    if isinstance(value, str):
        return redact_text(value, report)
    return value


def iter_input_lines(
    input_path: str | Path,
    *,
    include_empty: bool = False,
    start: str = "",
    end: str = "",
) -> Iterable[InputLine]:
    start_dt = parse_timestamp(start) if start else None
    end_dt = parse_timestamp(end) if end else None
    for file_path in iter_input_files(input_path, start=start, end=end):
        yield from _iter_file_input_lines(
            file_path,
            include_empty=include_empty,
            start_dt=start_dt,
            end_dt=end_dt,
        )


def iter_input_files(input_path: str | Path, *, start: str = "", end: str = "") -> Iterable[Path]:
    path = Path(input_path)
    start_date, end_date = _date_window(start, end)
    if path.is_file():
        yield path
        return
    if not path.exists():
        raise FileNotFoundError(path)
    for child in sorted(path.rglob("*")):
        if not child.is_file():
            continue
        if not _path_matches_date_window(child, start_date, end_date):
            continue
        if any(part in SKIP_DIRS for part in child.parts):
            continue
        if child.name in SKIP_FILE_NAMES or child.name.startswith("MANIFEST-"):
            continue
        if child.suffix.casefold() in SKIP_FILE_SUFFIXES:
            continue
        yield child


def _iter_file_input_lines(
    file_path: Path,
    *,
    include_empty: bool,
    start_dt: datetime | None,
    end_dt: datetime | None,
) -> Iterable[InputLine]:
    fallback = _fallback_timestamp(file_path)
    fallback_dt = parse_timestamp(fallback)
    offset = _estimated_large_file_offset(file_path, start_dt, end_dt)
    with file_path.open("rb") as handle:
        if offset > 0:
            handle.seek(offset)
            handle.readline()
        line_number = 0
        for raw_line in handle:
            line_number += 1
            text = raw_line.decode("utf-8", errors="replace").rstrip("\r\n")
            if not include_empty and not text.strip():
                continue
            line_ts = _raw_line_timestamp(text) if start_dt is not None or end_dt is not None else None
            if start_dt is not None and line_ts is not None and line_ts < start_dt:
                continue
            if end_dt is not None and line_ts is not None:
                if line_ts > end_dt:
                    break
            observed = format_timestamp(fallback_dt + timedelta(seconds=line_number))
            yield InputLine(file_path, line_number, text, observed)


def _estimated_large_file_offset(file_path: Path, start_dt: datetime | None, end_dt: datetime | None) -> int:
    if start_dt is None or end_dt is None:
        return 0
    try:
        size = file_path.stat().st_size
    except OSError:
        return 0
    if size < LARGE_SEEK_THRESHOLD_BYTES:
        return 0
    first_ts = _first_line_timestamp(file_path)
    last_ts = _last_line_timestamp(file_path)
    if first_ts is None or last_ts is None or first_ts >= last_ts:
        return 0
    if last_ts < start_dt or first_ts > end_dt:
        return size
    if start_dt <= first_ts:
        return 0
    binary_offset = _binary_seek_offset_for_timestamp(file_path, size, start_dt)
    if binary_offset is not None:
        return max(0, binary_offset - LARGE_SEEK_SAFETY_MARGIN_BYTES)
    span = (last_ts - first_ts).total_seconds()
    if span <= 0:
        return 0
    fraction = max(0.0, min(1.0, (start_dt - first_ts).total_seconds() / span))
    return max(0, int(size * fraction) - LARGE_SEEK_SAFETY_MARGIN_BYTES)


def _binary_seek_offset_for_timestamp(file_path: Path, size: int, target: datetime) -> int | None:
    low = 0
    high = max(0, size - 1)
    candidate: int | None = None
    for _ in range(40):
        if low > high:
            break
        mid = (low + high) // 2
        sample = _timestamp_at_or_after_offset(file_path, mid)
        if sample is None:
            high = mid - 1
            continue
        position, timestamp = sample
        if timestamp < target:
            low = max(mid + 1, position + 1)
        else:
            candidate = position
            high = mid - 1
    return candidate


def _timestamp_at_or_after_offset(file_path: Path, offset: int) -> tuple[int, datetime] | None:
    try:
        with file_path.open("rb") as handle:
            if offset > 0:
                handle.seek(offset)
                handle.readline()
            position = handle.tell()
            line = handle.readline()
    except OSError:
        return None
    if not line:
        return None
    timestamp = _raw_line_timestamp(line.decode("utf-8", errors="replace"))
    if timestamp is None:
        return None
    return position, timestamp


def _first_line_timestamp(file_path: Path) -> datetime | None:
    try:
        with file_path.open("rb") as handle:
            return _raw_line_timestamp(handle.readline().decode("utf-8", errors="replace"))
    except OSError:
        return None


def _last_line_timestamp(file_path: Path) -> datetime | None:
    try:
        with file_path.open("rb") as handle:
            handle.seek(0, 2)
            size = handle.tell()
            offset = min(size, 8192)
            handle.seek(size - offset)
            chunk = handle.read().decode("utf-8", errors="replace")
    except OSError:
        return None
    for line in reversed([row for row in chunk.splitlines() if row.strip()]):
        timestamp = _raw_line_timestamp(line)
        if timestamp is not None:
            return timestamp
    return None


def _date_window(start: str, end: str) -> tuple[date | None, date | None]:
    if not start or not end:
        return None, None
    try:
        return parse_timestamp(start).date(), parse_timestamp(end).date()
    except (TypeError, ValueError):
        return None, None


def _path_matches_date_window(path: Path, start: date | None, end: date | None) -> bool:
    if start is None or end is None:
        return True
    dates = _dates_from_path(path)
    if not dates:
        return True
    return max(dates) >= start and min(dates) <= end


def _dates_from_path(path: Path) -> list[date]:
    dates: list[date] = []
    for match in PATH_DATE_RE.finditer(str(path)):
        try:
            dates.append(date(int(match.group(1)), int(match.group(2)), int(match.group(3))))
        except ValueError:
            continue
    return dates


def suggest_system_type(
    *,
    detected_format: str,
    sources: list[str],
    service_candidates: list[str],
    sample_text: str,
) -> str:
    text = " ".join([detected_format, *sources, *service_candidates, sample_text]).casefold()
    if "systemd" in text or ".service" in text or detected_format == "journald_json":
        return "systemd_service"
    if any(term in text for term in ("cron", "timer", "scheduled", "airflow", "dag", "job")):
        return "scheduled_job"
    if detected_format in {"nginx_access", "apache_access"} or any(term in text for term in ("http", "api", "request", "5xx")):
        return "web_api"
    if any(term in text for term in ("batch", "worker", "queue")):
        return "batch_job"
    if any(term in text for term in ("stream", "ffmpeg", "kafka", "pubsub", "consumer")):
        return "streaming_runtime"
    if any(term in text for term in ("postgres", "mysql", "sqlite", "database", "db")):
        return "database_service"
    return "generic"


def _parse_json_line(payload: dict[str, Any], item: InputLine, detected_format: str) -> ParsedLine:
    timestamp = _timestamp_from_payload(payload)
    message = _first_value(payload, MESSAGE_KEYS) or _event_message_from_payload(payload)
    severity = _severity_from_payload(payload, message)
    attributes = {
        key: value
        for key, value in payload.items()
        if key not in set(MESSAGE_KEYS)
    }
    return ParsedLine(
        detected_format=detected_format,
        timestamp=timestamp,
        observed_timestamp=item.fallback_timestamp,
        timestamp_inferred=timestamp is None,
        source_system=str(_first_value(payload, ("source_system", "resource_type", "kind", "logName")) or detected_format),
        service=str(_first_value(payload, SERVICE_KEYS) or item.source_path.stem),
        environment=str(_first_value(payload, ENVIRONMENT_KEYS) or "unknown"),
        host=str(_first_value(payload, HOST_KEYS) or ""),
        component=str(_first_value(payload, COMPONENT_KEYS) or "unknown"),
        severity_text=severity,
        severity_number=severity_number(severity),
        message=str(message),
        attributes=attributes,
        trace_id=_optional_string(_first_value(payload, TRACE_KEYS)),
        span_id=_optional_string(_first_value(payload, SPAN_KEYS)),
        deploy_id=_optional_string(_first_value(payload, DEPLOY_KEYS)),
    )


def _parse_kubernetes_line(item: InputLine) -> ParsedLine:
    match = K8S_RE.match(item.text.strip())
    assert match is not None
    message = match.group("message")
    severity = _severity_from_text(message)
    return ParsedLine(
        detected_format="kubernetes_container_log",
        timestamp=format_timestamp(match.group("timestamp")),
        observed_timestamp=item.fallback_timestamp,
        timestamp_inferred=False,
        source_system="kubernetes_container_log",
        service=item.source_path.stem,
        environment="unknown",
        host="",
        component=match.group("stream"),
        severity_text=severity,
        severity_number=severity_number(severity),
        message=message,
        attributes={"stream": match.group("stream"), "tag": match.group("tag")},
    )


def _parse_access_line(item: InputLine, detected_format: str) -> ParsedLine:
    match = ACCESS_RE.match(item.text.strip())
    assert match is not None
    status = int(match.group("status"))
    severity = "error" if status >= 500 else "warning" if status >= 400 else "info"
    timestamp = _parse_access_timestamp(match.group("time"))
    path = match.group("path")
    message = f"{match.group('method')} {path} -> HTTP {status}"
    return ParsedLine(
        detected_format=detected_format,
        timestamp=timestamp,
        observed_timestamp=item.fallback_timestamp,
        timestamp_inferred=timestamp is None,
        source_system=detected_format,
        service=item.source_path.stem,
        environment="unknown",
        host="",
        component="http_access",
        severity_text=severity,
        severity_number=severity_number(severity),
        message=message,
        attributes={
            "client_ip": match.group("ip"),
            "user": match.group("user"),
            "http_method": match.group("method"),
            "path": path,
            "status_code": status,
            "response_size": match.group("size"),
        },
    )


def _parse_syslog_line(item: InputLine) -> ParsedLine:
    match = SYSLOG_RE.match(item.text.strip())
    assert match is not None
    timestamp = _parse_syslog_timestamp(match, item)
    message = match.group("message")
    severity = _severity_from_text(message)
    program = match.group("program")
    return ParsedLine(
        detected_format="syslog",
        timestamp=timestamp,
        observed_timestamp=item.fallback_timestamp,
        timestamp_inferred=timestamp is None,
        source_system="syslog",
        service=program,
        environment="unknown",
        host=match.group("host"),
        component=program,
        severity_text=severity,
        severity_number=severity_number(severity),
        message=message,
        attributes={"program": program, "pid": match.group("pid") or ""},
    )


def _parse_key_value_line(item: InputLine, detected_format: str) -> ParsedLine:
    values = parse_key_values(item.text)
    message = str(values.get("message") or values.get("msg") or values.get("event") or item.text)
    timestamp = _timestamp_from_payload(values)
    severity = str(_first_value(values, SEVERITY_KEYS) or _severity_from_text(message))
    return ParsedLine(
        detected_format=detected_format,
        timestamp=timestamp,
        observed_timestamp=item.fallback_timestamp,
        timestamp_inferred=timestamp is None,
        source_system=str(values.get("source_system") or detected_format),
        service=str(_first_value(values, SERVICE_KEYS) or item.source_path.stem),
        environment=str(_first_value(values, ENVIRONMENT_KEYS) or "unknown"),
        host=str(_first_value(values, HOST_KEYS) or ""),
        component=str(_first_value(values, COMPONENT_KEYS) or "unknown"),
        severity_text=severity,
        severity_number=severity_number(severity),
        message=message,
        attributes=values,
        trace_id=_optional_string(_first_value(values, TRACE_KEYS)),
        span_id=_optional_string(_first_value(values, SPAN_KEYS)),
        deploy_id=_optional_string(_first_value(values, DEPLOY_KEYS)),
    )


def _parse_plain_line(item: InputLine, detected_format: str) -> ParsedLine:
    text = item.text.strip()
    timestamp: str | None = None
    message = text
    match = TEXT_TS_PREFIX_RE.match(text)
    if match:
        timestamp = _parse_any_timestamp(match.group("timestamp"))
        message = text[match.end() :].strip()
    elif ISO_TS_RE.search(text):
        timestamp = _parse_any_timestamp(ISO_TS_RE.search(text).group(0))  # type: ignore[union-attr]
    severity = _severity_from_text(message)
    values = parse_key_values(message)
    service = str(_first_value(values, SERVICE_KEYS) or _service_from_text(message) or item.source_path.stem)
    component = str(_first_value(values, COMPONENT_KEYS) or _component_from_text(message) or "unknown")
    return ParsedLine(
        detected_format=detected_format,
        timestamp=timestamp,
        observed_timestamp=item.fallback_timestamp,
        timestamp_inferred=timestamp is None,
        source_system=detected_format,
        service=service,
        environment=str(_first_value(values, ENVIRONMENT_KEYS) or "unknown"),
        host=str(_first_value(values, HOST_KEYS) or ""),
        component=component,
        severity_text=severity,
        severity_number=severity_number(severity),
        message=message,
        attributes=values,
    )


def parse_key_values(text: str) -> dict[str, Any]:
    output: dict[str, Any] = {}
    try:
        parts = shlex.split(text)
    except ValueError:
        parts = text.split()
    for part in parts:
        if "=" not in part:
            continue
        key, raw = part.split("=", 1)
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.\-]*", key):
            output[key] = _coerce_value(raw.strip("\"'"))
    if not output:
        for key, value in KEY_COLON_RE.findall(text):
            output[key] = _coerce_value(value.strip("\"'"))
    return output


def normalize_severity(value: str) -> str:
    text = str(value or "info").strip().casefold()
    if text == "warning":
        return "warning"
    if text == "warn":
        return "warning"
    if text in SEVERITY_NUMBERS:
        return text
    if text in JOURNALD_PRIORITY:
        return JOURNALD_PRIORITY[text]
    return "info"


def severity_number(value: str) -> int:
    return SEVERITY_NUMBERS.get(normalize_severity(value), 0)


def _json_object(line: str) -> dict[str, Any] | None:
    try:
        payload = json.loads(line)
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def _lookup(payload: dict[str, Any], key: str) -> Any:
    if "." not in key:
        return payload.get(key)
    current: Any = payload
    for part in key.split("."):
        if not isinstance(current, dict):
            return None
        current = current.get(part)
    return current


def _first_value(payload: dict[str, Any], keys: Iterable[str]) -> Any:
    for key in keys:
        value = _lookup(payload, key)
        if value not in (None, ""):
            return value
    return None


def _timestamp_from_payload(payload: dict[str, Any]) -> str | None:
    value = _first_value(payload, TIMESTAMP_KEYS)
    if value in (None, ""):
        return None
    if "__REALTIME_TIMESTAMP" in payload:
        try:
            return format_timestamp(datetime.fromtimestamp(int(str(value)) / 1_000_000, tz=UTC))
        except (TypeError, ValueError, OSError):
            pass
    return _parse_any_timestamp(value)


def _parse_any_timestamp(value: Any) -> str | None:
    try:
        if isinstance(value, (int, float)):
            number = float(value)
            if number > 1_000_000_000_000_000:
                number = number / 1_000_000_000
            elif number > 1_000_000_000_000:
                number = number / 1000
            return format_timestamp(datetime.fromtimestamp(number, tz=UTC))
        text = str(value).strip().replace(",", ".")
        if text.endswith("Z") or re.search(r"[+-]\d{2}:?\d{2}$", text):
            return format_timestamp(text)
        if " " in text and "T" not in text:
            text = text.replace(" ", "T", 1)
        return format_timestamp(text)
    except (TypeError, ValueError, OSError):
        return None


def _parse_syslog_timestamp(match: re.Match[str], item: InputLine) -> str | None:
    try:
        year = parse_timestamp(item.fallback_timestamp).year
        parsed = datetime.strptime(
            f"{year} {match.group('mon')} {match.group('day')} {match.group('hms')}",
            "%Y %b %d %H:%M:%S",
        ).replace(tzinfo=UTC)
        return format_timestamp(parsed)
    except ValueError:
        return None


def _parse_access_timestamp(value: str) -> str | None:
    for fmt in ("%d/%b/%Y:%H:%M:%S %z", "%d/%b/%Y:%H:%M:%S"):
        try:
            parsed = datetime.strptime(value, fmt)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=UTC)
            return format_timestamp(parsed)
        except ValueError:
            continue
    return None


def _severity_from_payload(payload: dict[str, Any], message: Any) -> str:
    value = _first_value(payload, SEVERITY_KEYS)
    if value not in (None, ""):
        return normalize_severity(str(value))
    status = str(payload.get("status") or "").casefold()
    if status in {"error", "failed", "fail", "unhealthy", "critical", "fatal"}:
        return "error" if status != "critical" else "critical"
    if status in {"warn", "warning", "degraded"}:
        return "warning"
    if payload.get("healthy") is False or payload.get("ok") is False or payload.get("success") is False:
        return "error"
    return _severity_from_text(str(message))


def _severity_from_text(message: str) -> str:
    match = LEVEL_RE.search(message)
    if match:
        return normalize_severity(match.group(1))
    text = message.casefold()
    if any(term in text for term in ("critical", "fatal", "panic", "emergency")):
        return "critical"
    if any(term in text for term in ("error", "failed", "failure", "denied", "unauthorized", "exception", "traceback")):
        return "error"
    if any(term in text for term in ("warn", "degraded", "retry")):
        return "warning"
    return "info"


def _event_message_from_payload(payload: dict[str, Any]) -> str:
    kind = str(payload.get("kind") or payload.get("event_type") or payload.get("event_id") or "event")
    context = []
    for key in ("status", "healthy", "failure_kind", "reason", "action", "http_status", "status_code"):
        if payload.get(key) not in (None, ""):
            context.append(f"{key}={payload[key]}")
    return " ".join([kind, *context])


def _service_from_text(message: str) -> str:
    match = re.search(r"\b([A-Za-z0-9_.@\-]+\.service)\b", message)
    return match.group(1) if match else ""


def _component_from_text(message: str) -> str:
    match = re.search(r"\b(?:component|subsystem|module)=([A-Za-z0-9_.@\-]+)\b", message)
    return match.group(1) if match else ""


def _profile_confidence(
    *,
    explicit_profile: bool,
    service_candidates: Counter[str],
    detected_format: str,
) -> str:
    if explicit_profile:
        return "explicit"
    profile_ids = set(available_profile_ids())
    for candidate in service_candidates:
        if normalize_profile_id(candidate) in profile_ids and normalize_profile_id(candidate) != "generic":
            return "inferred"
    if detected_format in {"journald_json", "nginx_access", "apache_access", "kubernetes_container_log"}:
        return "inferred"
    return "unknown"


def _resolve_profile(profile_name: str, *, profile_supports_inference: bool = False) -> tuple[dict[str, Any], str]:
    profile_path = Path(profile_name) if profile_name else None
    if profile_path and profile_path.is_file():
        try:
            payload = json.loads(profile_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            payload = {}
        if isinstance(payload, dict) and payload:
            return payload, "explicit"
    normalized = normalize_profile_id(profile_name)
    if normalized == "generic":
        confidence = "inferred" if profile_supports_inference else "unknown"
        return load_profile("generic"), confidence
    if profile_name and normalized in set(available_profile_ids()):
        return load_profile(normalized), "explicit"
    return {}, "unknown"


def _events_support_profile_inference(events: list[dict[str, Any]]) -> bool:
    for event in events:
        if _event_supports_profile_inference(event):
            return True
    return False


def _event_supports_profile_inference(event: dict[str, Any]) -> bool:
    attrs = event.get("attributes") if isinstance(event.get("attributes"), dict) else {}
    text = " ".join(
        [
            str(event.get("component") or ""),
            str(event.get("source_system") or ""),
            str(event.get("message_sanitized") or ""),
            str(attrs.get("detected_format") or ""),
        ]
    ).casefold()
    return ".service" in text or any(
        kind in text
        for kind in ("journald_json", "syslog", "kubernetes_container_log", "nginx_access", "apache_access")
    )


def _system_profile(profile: dict[str, Any]) -> dict[str, Any]:
    if isinstance(profile.get("system_profile"), dict):
        return dict(profile["system_profile"])
    keys = ("profile_id", "system_type", "purpose", "critical_outcomes")
    return {key: profile[key] for key in keys if key in profile}


def _events_for_window(events: list[dict[str, Any]], start: str, end: str) -> list[dict[str, Any]]:
    start_dt = parse_timestamp(start)
    end_dt = parse_timestamp(end)
    selected = []
    for event in events:
        attrs = event.get("attributes") if isinstance(event.get("attributes"), dict) else {}
        if attrs.get("timestamp_inferred"):
            selected.append(event)
            continue
        value = event.get("timestamp") or event.get("observed_timestamp")
        try:
            parsed = parse_timestamp(str(value))
        except (TypeError, ValueError):
            selected.append(event)
            continue
        if start_dt <= parsed <= end_dt:
            selected.append(event)
    return selected or events


def _source_system_for_bundle(source_counts: Counter[str], profile: dict[str, Any], service: str) -> str:
    counts = Counter({key: count for key, count in source_counts.items() if key})
    return str(profile.get("source_system") or _dominant(counts, default=service or "generic"))


def _load_redaction_summary(sanitized_path: Path) -> dict[str, int]:
    report_path = sanitized_path.parent / "redaction_report.json"
    if not report_path.exists():
        return {}
    report = json.loads(report_path.read_text(encoding="utf-8"))
    summary = report.get("summary") if isinstance(report, dict) else {}
    return {key: int(summary.get(key, 0)) for key in SUMMARY_KEYS} if isinstance(summary, dict) else {}


def _merge_redaction_summaries(*summaries: dict[str, int]) -> dict[str, int]:
    counts: Counter[str] = Counter()
    for summary in summaries:
        for key in SUMMARY_KEYS:
            counts[key] += int(summary.get(key, 0))
    return {key: int(counts.get(key, 0)) for key in SUMMARY_KEYS}


def _load_manifest(sanitized_path: Path) -> dict[str, Any]:
    manifest_path = sanitized_path.parent / "manifest.json"
    if not manifest_path.exists():
        return {}
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return manifest if isinstance(manifest, dict) else {}


def _bundle_hash_payload(bundle: dict[str, Any]) -> dict[str, Any]:
    payload = {
        key: value
        for key, value in bundle.items()
        if key not in {"evidence_sha256", "created_at"}
    }
    summary = payload.get("local_first_summary")
    if isinstance(summary, dict):
        payload["local_first_summary"] = {
            key: value
            for key, value in summary.items()
            if key not in {"evidence_sha256"}
        }
    return payload


def _raw_policy_from_file(path: Path) -> str:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return ""
    if not isinstance(payload, dict):
        return ""
    value = payload.get("raw_log_policy")
    if value:
        return str(value)
    summary = payload.get("local_first_summary")
    if isinstance(summary, dict) and summary.get("raw_log_policy"):
        return str(summary["raw_log_policy"])
    return ""


def _event_time(event: dict[str, Any]) -> str:
    return str(event.get("timestamp") or event.get("observed_timestamp") or "")


def _severity_rank(value: str) -> int:
    return severity_number(value)


def _signal_confidence(event_type: str, severity: str, count: int) -> float:
    if event_type in {"unknown", "info"}:
        return 0.35
    base = 0.8 if severity_number(severity) >= severity_number("error") else 0.65
    if count >= 5:
        base += 0.1
    return round(min(base, 0.95), 2)


def _dominant(counts: Counter[str], *, default: str) -> str:
    if not counts:
        return default
    return sorted(counts.items(), key=lambda item: (-item[1], item[0]))[0][0] or default


def _top_keys(counts: Counter[str], *, fallback: list[str] | None = None, limit: int = 10) -> list[str]:
    output = [key for key, _count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:limit] if key]
    return output or list(fallback or [])


def _unique_key(values: dict[str, Any], prefix: str) -> str:
    if prefix not in values:
        return prefix
    index = 2
    while f"{prefix}_{index}" in values:
        index += 1
    return f"{prefix}_{index}"


def _sub_count(
    pattern: re.Pattern[str],
    text: str,
    repl: Any,
    report: RedactionCounter,
    key: str,
    *,
    extra: tuple[str, ...] = (),
) -> str:
    text, count = pattern.subn(repl, text)
    if count:
        report.add(key, count)
        for extra_key in extra:
            report.add(extra_key, count)
    return text


def _needs_redaction_scan(text: str) -> bool:
    if not text:
        return False
    if REDACTION_SCAN_RE.search(text):
        return True
    if "@" in text:
        return True
    return text.count(".") >= 3 and any(char.isdigit() for char in text)


def _hash_prefix(value: str) -> str:
    return sha256_text(value)[:12]


def _strip_quotes(value: str) -> str:
    text = str(value)
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {"'", '"'}:
        return text[1:-1]
    return text


def _compact_ws(value: str) -> str:
    if not any(char.isspace() for char in value):
        return value
    return re.sub(r"\s+", " ", value).strip()


def _is_sensitive_key(key: str) -> bool:
    return bool(
        re.search(
            r"api[_-]?key|token|session(?:[_-]?id)?|password|passwd|secret|private[_-]?key|"
            r"credential|client[_-]?secret|authorization|cookie|gmail|pubsub",
            key,
        )
    )


def _is_id_key(key: str) -> bool:
    return bool(re.search(r"(?:^|[_-])(user|order|tracking|track|customer|account)[_-]?id$", key))


def _coerce_value(value: str) -> Any:
    lowered = value.casefold()
    if lowered in {"true", "false"}:
        return lowered == "true"
    if lowered in {"null", "none"}:
        return None
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        return value


def _optional_string(value: Any) -> str | None:
    if value in (None, ""):
        return None
    return str(value)


def _fallback_timestamp(path: Path) -> str:
    return format_timestamp(datetime.fromtimestamp(path.stat().st_mtime, tz=UTC))
