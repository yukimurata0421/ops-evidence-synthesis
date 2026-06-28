from __future__ import annotations

from typing import Any


OPS_SUBSYSTEMS = (
    "generic_runtime",
    "youtube_health",
    "rtmps_ffmpeg",
    "chromium_capture",
    "audio_energy",
    "runtime_recovery",
    "network_transport",
    "database_connection_pool",
    "database_timeout",
    "downstream_dependency",
    "deployment_regression",
    "auth_config",
    "resource_pressure",
    "observability_contract",
    "background_processing",
    "job_configuration",
    "service_liveness",
    "traffic",
    "user_experience",
)

# Backward-compatible alias for the original stream_v3 validation fixture.
STREAM_V3_SUBSYSTEMS = OPS_SUBSYSTEMS


SUBSYSTEM_QUESTIONS = {
    "generic_runtime": "Should humans review generic process, worker, or runtime health?",
    "youtube_health": "Should humans review YouTube live health or API evidence?",
    "rtmps_ffmpeg": "Should humans review RTMPS transport or ffmpeg send-path instability?",
    "chromium_capture": "Should humans review Chromium capture process instability?",
    "audio_energy": "Should humans review audio energy or headless audio pipeline gaps?",
    "runtime_recovery": "Should humans review runtime recovery, restart, or watchdog behavior?",
    "network_transport": "Should humans review network transport instability?",
    "database_connection_pool": "Should humans review database connection pool saturation as the incident driver?",
    "database_timeout": "Should humans review database timeout regression as the incident driver?",
    "downstream_dependency": "Should humans review downstream dependency behavior as a competing explanation?",
    "deployment_regression": "Should humans validate the latest deployment as a causal change?",
    "auth_config": "Should humans review authorization configuration as the incident driver?",
    "resource_pressure": "Should humans review resource pressure or timeout behavior?",
    "observability_contract": "Should humans review health signal and observability contract mismatch?",
    "background_processing": "Should humans review background worker, scheduled job, or queue processing behavior?",
    "job_configuration": "Should humans review missing configured job commands or supervisor configuration drift?",
    "service_liveness": "Should humans review heartbeat, readiness, or worker liveness evidence?",
    "traffic": "Should humans review throughput, request, output, or business-event counters?",
    "user_experience": "Should humans review latency or user-experience regression evidence?",
    "general": "What incident hypothesis needs human review first?",
}


def subsystem_for_claim(bundle: dict[str, Any], claim_text: str, refs: list[str] | tuple[str, ...]) -> str:
    evidence_refs = bundle.get("evidence_refs") or {}
    parts = [claim_text]
    for ref in refs:
        details = evidence_refs.get(ref) or {}
        if isinstance(details, dict):
            parts.extend(
                str(details.get(key) or "")
                for key in ("subsystem", "type", "summary")
            )
        parts.append(str(ref))
    return subsystem_for_text(" ".join(parts))


def subsystem_for_text(value: Any) -> str:
    text = str(value or "").casefold()
    if any(term in text for term in ("youtube", "watch url", "stream_service_substate")):
        if any(term in text for term in ("dead", "healthy", "watchdog_ok", "health")):
            return "youtube_health"
    if any(term in text for term in ("ffmpeg", "rtmps", "stream-engine", "stream_transport", "send-path", "encoder", "exit code 1")):
        return "rtmps_ffmpeg"
    if any(term in text for term in ("chromium", "chrome", "renderer", "zygote", "gpu", "crashpad", "capture")):
        return "chromium_capture"
    if any(term in text for term in ("audio", "pulseaudio", "pulse", "dbus", "d-bus", "x11", "energy")):
        return "audio_energy"
    if any(term in text for term in ("restart", "runtime_restart", "service_health_failure", "recovery", "systemd", "substate", "runtime", "fast recovery")):
        if any(term in text for term in ("can't open file", "no such file or directory", "execstart", "configured command missing")):
            return "job_configuration"
        return "runtime_recovery"
    if any(term in text for term in ("heartbeat", "liveness", "readiness", "ready", "watch renew", "watch_renew", "watchdog", "polling")):
        return "service_liveness"
    if any(term in text for term in ("background_processing", "background processing", "scheduled job", "queue processing", "worker loop")):
        return "background_processing"
    if any(term in text for term in ("throughput", "request_count", "request count", "notification_forward", "business-event", "output counter")):
        return "traffic"
    if any(term in text for term in ("latency", "p95", "p99", "user experience", "user_experience")):
        return "user_experience"
    if any(term in text for term in ("application process", "worker", "generic_runtime", "runtime host")):
        return "generic_runtime"
    if any(term in text for term in ("connection reset", "connection_reset", "network", "tcp", "peer", "notsent", "unacked", "packet")):
        return "network_transport"
    if any(term in text for term in ("connection pool", "connection_pool", "too many connections", "pool saturation")):
        return "database_connection_pool"
    if "database" in text and "timeout" in text:
        return "database_timeout"
    if any(term in text for term in ("payment-gateway", "dependency_timeout", "downstream", "dependency")):
        return "downstream_dependency"
    if any(term in text for term in ("deploy", "release", "rollout", "deployment")):
        return "deployment_regression"
    if any(term in text for term in ("auth", "permission", "authorization")):
        return "auth_config"
    if any(term in text for term in ("cpu", "memory", "oom", "timeout", "load", "pressure", "peak", "resource")):
        return "resource_pressure"
    if any(
        term in text
        for term in (
            "healthy",
            "critical",
            "warn",
            "error_count",
            "total_log_count",
            "active_hour_count",
            "active_service_count",
            "adequacy",
            "metric",
            "prometheus",
            "alert",
            "observability",
        )
    ):
        return "observability_contract"
    if any(term in text for term in ("can't open file", "no such file or directory", "execstart", "configured command missing")):
        return "job_configuration"
    return "general"


def question_for_subsystem(subsystem: str) -> str:
    return SUBSYSTEM_QUESTIONS.get(subsystem, SUBSYSTEM_QUESTIONS["general"])


def bigquery_predicate_for_subsystem(subsystem: str) -> str:
    terms = {
        "youtube_health": ("youtube", "watchdog", "watchdog_ok", "stream_service_substate", "watch url"),
        "rtmps_ffmpeg": ("rtmps", "ffmpeg", "stream-engine", "send-path", "encoder"),
        "chromium_capture": ("chromium", "chrome", "renderer", "zygote", "gpu", "crashpad", "capture"),
        "audio_energy": ("audio", "pulseaudio", "d-bus", "dbus", "x11", "energy"),
        "runtime_recovery": ("restart", "recovery", "systemd", "substate", "runtime"),
        "network_transport": ("connection reset", "network", "tcp", "peer", "notsent", "unacked"),
        "database_connection_pool": ("connection pool", "connection_pool", "too many connections", "pool saturation"),
        "database_timeout": ("database", "timeout", "db timeout"),
        "downstream_dependency": ("payment-gateway", "dependency_timeout", "downstream", "dependency"),
        "deployment_regression": ("deploy", "release", "rollout", "deployment"),
        "auth_config": ("auth", "permission", "authorization"),
        "resource_pressure": ("cpu", "memory", "oom", "timeout", "load", "pressure", "peak"),
        "observability_contract": ("healthy", "critical", "warn", "adequacy", "metric", "prometheus", "alert"),
        "background_processing": ("background_processing", "background processing", "scheduled job", "queue processing", "worker loop"),
        "job_configuration": ("can't open file", "no such file or directory", "execstart", "configured command missing"),
        "generic_runtime": ("application process", "worker", "runtime host", "generic_runtime"),
        "service_liveness": ("heartbeat", "liveness", "readiness", "ready", "watch renew", "watch_renew", "watchdog", "polling"),
        "traffic": ("throughput", "request_count", "request count", "notification_forward", "business-event", "output counter"),
        "user_experience": ("latency", "p95", "p99", "user experience", "user_experience"),
    }.get(subsystem, ())
    error_types = {
        "youtube_health": ("youtube_health",),
        "rtmps_ffmpeg": ("stream_transport",),
        "chromium_capture": ("browser_capture",),
        "audio_energy": ("audio_pipeline",),
        "runtime_recovery": ("runtime_restart", "service_health_failure"),
        "network_transport": ("network",),
        "database_connection_pool": ("database_connection_pool", "connection_pool_exhausted"),
        "database_timeout": ("database_timeout",),
        "downstream_dependency": ("dependency_timeout", "downstream_dependency"),
        "auth_config": ("auth_failure",),
        "resource_pressure": ("resource_pressure",),
        "job_configuration": ("job_configuration_mismatch", "service_start_failure"),
        "generic_runtime": ("generic_runtime",),
        "service_liveness": ("heartbeat_missing",),
        "traffic": ("throughput_disappearance",),
        "user_experience": ("latency_regression",),
    }.get(subsystem, ())
    if not terms:
        if not error_types:
            return "TRUE"
        return "(" + " OR ".join(f"LOWER(error_type) = '{error_type}'" for error_type in error_types) + ")"
    parts = [f"LOWER(message_sanitized) LIKE '%{term}%'" for term in terms]
    parts.extend(f"LOWER(error_type) = '{error_type}'" for error_type in error_types)
    return "(" + " OR ".join(parts) + ")"
