from __future__ import annotations

import re
import unicodedata
from typing import Any

from ops_evidence_synthesis.canonical import sha256_text


WEAK_EVENT_NAMES = {"event", "info", "warning", "unknown"}
_EMPTY_VALUES = {"", "false", "n/a", "na", "none", "null", "true", "unknown"}
_PLACEHOLDER_EVENT_NAMES = {
    "error",
    "event",
    "exception",
    "failure",
    "info",
    "log",
    "message",
    "none",
    "unknown",
    "warning",
}
_EXPLICIT_EVENT_KEYS = (
    "event_type",
    "error_type",
    "exception_type",
    "exception_class",
    "failure_kind",
    "event_class",
)
_ERROR_CODE_KEYS = (
    "error_code",
    "errno",
    "sqlstate",
    "grpc_status",
    "response_code",
    "status_code",
    "http_status",
)
_EXCEPTION_CLASS_KEYS = ("exception_class", "exception_type")
_FAMILY_BY_EVENT_NAME = {
    "auth_failure": "authentication",
    "config_error": "configuration",
    "connection_reset": "network",
    "dependency_unreachable": "dependency",
    "dns_failure": "network",
    "http_5xx": "dependency",
    "instrumentation_mismatch": "observability",
    "missing_command": "configuration",
    "missing_file": "configuration",
    "monitoring_gap": "observability",
    "oom": "resource",
    "permission_denied": "authentication",
    "process_exit": "runtime",
    "restart_loop": "runtime",
    "service_start_failure": "runtime",
    "state_mismatch": "state",
    "timeout": "dependency",
}


def classify_event_semantics(
    message: str,
    severity_text: str,
    attributes: dict[str, Any] | None = None,
    *,
    template: str = "",
    explicit_event_type: str = "",
) -> dict[str, Any]:
    """Return deterministic, extensible semantics for sanitized event text."""

    attributes = attributes or {}
    normalized_template = str(template or message or "").strip()
    template_fingerprint = f"tmpl-{sha256_text(normalized_template)[:16]}"
    error_code = _first_attribute_value(attributes, _ERROR_CODE_KEYS, code=True)
    exception_class = _first_attribute_value(attributes, _EXCEPTION_CLASS_KEYS)
    explicit_name = _normalize_semantic_name(explicit_event_type) or _first_attribute_value(
        attributes,
        _EXPLICIT_EVENT_KEYS,
    )
    protocol = _detect_protocol(message, attributes)
    generic_name = _infer_generic_event_name(message, severity_text, attributes)

    if explicit_name and explicit_name not in _PLACEHOLDER_EVENT_NAMES:
        event_name = explicit_name
        source = "structured_field"
        confidence = 0.98
    elif exception_class:
        event_name = exception_class
        source = "structured_field"
        confidence = 0.96
    elif generic_name not in WEAK_EVENT_NAMES:
        event_name = generic_name
        source = "generic_protocol" if protocol else "generic_rule"
        confidence = 0.9
    elif error_code and not _is_success_code(error_code):
        event_name = f"error_code_{_normalize_semantic_name(error_code)}"
        source = "structured_field"
        confidence = 0.9
    else:
        event_name = generic_name
        source = "template_fingerprint" if event_name in WEAK_EVENT_NAMES else "severity_fallback"
        confidence = 0.55 if event_name in WEAK_EVENT_NAMES else 0.65

    explicit_family = _first_attribute_value(attributes, ("event_family", "failure_domain", "category"))
    event_family = explicit_family or _event_family(event_name, protocol=protocol, message=message)
    return {
        "event_family": event_family,
        "event_name": event_name,
        "error_code": error_code,
        "exception_class": exception_class,
        "protocol": protocol,
        "classification_source": source,
        "classification_confidence": round(confidence, 4),
        "template_fingerprint": template_fingerprint,
    }


def enrich_evidence_item_semantics(
    item: dict[str, Any],
    *,
    profile_event_semantics: object = None,
    profile_approved: bool = False,
) -> dict[str, Any]:
    """Attach generic semantics and, when approved, deterministic profile overrides."""

    enriched = dict(item)
    semantics = classify_event_semantics(
        str(item.get("message_template") or item.get("example_sanitized") or ""),
        str(item.get("severity_text") or item.get("severity_hint") or ""),
        item,
        template=str(item.get("message_template") or item.get("example_sanitized") or ""),
        explicit_event_type=str(item.get("event_name") or item.get("event_type") or item.get("type") or ""),
    )
    for key, value in semantics.items():
        if item.get(key) not in (None, ""):
            enriched[key] = item[key]
        else:
            enriched[key] = value

    if profile_approved:
        matched = _matching_profile_rule(enriched, profile_event_semantics)
        if matched is not None:
            rule_id, rule = matched
            prior_family = str(enriched.get("event_family") or "general")
            prior_name = str(enriched.get("event_name") or enriched.get("event_type") or "unknown")
            event_name = _normalize_semantic_name(rule.get("event_name") or rule.get("classify_as")) or prior_name
            event_family = _normalize_semantic_name(rule.get("event_family")) or _event_family(
                event_name,
                protocol=str(enriched.get("protocol") or ""),
                message=str(enriched.get("message_template") or ""),
            )
            enriched.update(
                {
                    "generic_event_family": prior_family,
                    "generic_event_name": prior_name,
                    "event_family": event_family,
                    "event_name": event_name,
                    "classification_source": f"approved_profile:{rule_id}",
                    "classification_confidence": _bounded_confidence(rule.get("confidence"), default=1.0),
                }
            )
            subsystem = _normalize_semantic_name(rule.get("subsystem") or rule.get("component"))
            if subsystem:
                enriched["subsystem"] = subsystem

    enriched["event_type"] = str(enriched.get("event_name") or enriched.get("event_type") or "unknown")
    return enriched


def semantic_identity_for_item(item: dict[str, Any]) -> dict[str, str]:
    event_name = _normalize_semantic_name(item.get("event_name") or item.get("event_type") or item.get("type")) or "event"
    protocol = _normalize_semantic_name(item.get("protocol"))
    event_family = _normalize_semantic_name(item.get("event_family")) or _event_family(
        event_name,
        protocol=protocol,
        message=str(item.get("message_template") or ""),
    )
    fingerprint = str(item.get("template_fingerprint") or "").strip()
    if not fingerprint:
        template = str(item.get("message_template") or item.get("example_sanitized") or event_name)
        fingerprint = f"tmpl-{sha256_text(template)[:16]}"
    return {
        "event_family": event_family,
        "event_name": event_name,
        "template_fingerprint": fingerprint,
    }


def _matching_profile_rule(
    item: dict[str, Any],
    profile_event_semantics: object,
) -> tuple[str, dict[str, Any]] | None:
    for index, rule in enumerate(_profile_rules(profile_event_semantics), start=1):
        if rule.get("enabled") is False:
            continue
        conditions = rule.get("match") if isinstance(rule.get("match"), dict) else rule.get("when")
        if not isinstance(conditions, dict) or not conditions:
            continue
        if _profile_rule_matches(item, conditions):
            rule_id = _normalize_semantic_name(rule.get("id") or f"event_semantics_{index:03d}")
            return rule_id or f"event_semantics_{index:03d}", rule
    return None


def _profile_rules(value: object) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return [dict(row) for row in value if isinstance(row, dict)]
    if not isinstance(value, dict):
        return []
    rows: list[dict[str, Any]] = []
    for key, raw in value.items():
        if not isinstance(raw, dict):
            continue
        row = dict(raw)
        if not isinstance(row.get("match"), dict) and not isinstance(row.get("when"), dict):
            row["match"] = {"event_type": key}
        row.setdefault("id", str(key))
        rows.append(row)
    return rows


def _profile_rule_matches(item: dict[str, Any], conditions: dict[str, Any]) -> bool:
    recognized = False
    aliases = {
        "event_type": "event_name",
        "event_name": "event_name",
        "event_family": "event_family",
        "component": "component",
        "subsystem": "subsystem",
        "source": "source",
        "protocol": "protocol",
        "error_code": "error_code",
        "exception_class": "exception_class",
    }
    for condition_key, item_key in aliases.items():
        if condition_key not in conditions:
            continue
        recognized = True
        if not _matches_expected(item.get(item_key), conditions[condition_key]):
            return False

    text = unicodedata.normalize(
        "NFKC",
        " ".join(
            str(item.get(key) or "")
            for key in ("message_template", "example_sanitized", "event_name", "component", "source")
        ),
    ).casefold()
    for key, mode in (
        ("message_contains", "all"),
        ("message_contains_all", "all"),
        ("message_contains_any", "any"),
        ("template_contains", "all"),
        ("template_contains_all", "all"),
        ("template_contains_any", "any"),
    ):
        if key not in conditions:
            continue
        recognized = True
        terms = _string_list(conditions[key])
        if not terms:
            return False
        matches = [unicodedata.normalize("NFKC", term).casefold() in text for term in terms]
        if (mode == "all" and not all(matches)) or (mode == "any" and not any(matches)):
            return False
    return recognized


def _matches_expected(actual: Any, expected: Any) -> bool:
    actual_value = _normalize_semantic_name(actual)
    expected_values = {_normalize_semantic_name(value) for value in _string_list(expected)}
    expected_values.discard("")
    return bool(actual_value and actual_value in expected_values)


def _string_list(value: Any) -> list[str]:
    if isinstance(value, (str, int, float)):
        return [str(value)]
    if isinstance(value, list | tuple | set):
        return [str(row) for row in value if str(row).strip()]
    return []


def _first_attribute_value(
    attributes: dict[str, Any],
    keys: tuple[str, ...],
    *,
    code: bool = False,
) -> str:
    rows = _flatten_scalars(attributes)
    for wanted in keys:
        for _path, key, value in rows:
            if key != wanted:
                continue
            normalized = _normalize_code(value) if code else _normalize_semantic_name(value)
            if normalized.casefold() not in _EMPTY_VALUES:
                return normalized
    return ""


def _flatten_scalars(
    value: dict[str, Any],
    *,
    path: tuple[str, ...] = (),
    depth: int = 0,
) -> list[tuple[tuple[str, ...], str, Any]]:
    if depth > 4:
        return []
    rows: list[tuple[tuple[str, ...], str, Any]] = []
    for raw_key, child in value.items():
        key = str(raw_key).strip().casefold().replace("-", "_")
        child_path = (*path, key)
        if isinstance(child, dict):
            rows.extend(_flatten_scalars(child, path=child_path, depth=depth + 1))
        elif isinstance(child, (str, bool, int, float)):
            rows.append((child_path, key, child))
    return rows


def _semantic_attribute_text(attributes: dict[str, Any]) -> str:
    keys = {
        "action",
        "error",
        "error_type",
        "event_class",
        "event_type",
        "exception",
        "exception_type",
        "failure",
        "failure_reason",
        "outcome",
        "reason",
        "result",
        "sample_reason",
        "state",
        "status",
    }
    return " ".join(
        str(value)
        for _path, key, value in _flatten_scalars(attributes)
        if key in keys and str(value).strip().casefold() not in _EMPTY_VALUES
    )


def _infer_generic_event_name(message: str, severity_text: str, attributes: dict[str, Any]) -> str:
    message_text = str(message or "").casefold()
    text = " ".join((str(message or ""), _semantic_attribute_text(attributes))).casefold()
    if "no such file or directory" in text:
        if any(term in text for term in ("can't open file", "execstart", "executable", "command", ".service", ".py", ".sh")):
            return "missing_command"
        return "missing_file"
    if "command not found" in text or "executable file not found" in text:
        return "missing_command"
    if any(term in text for term in ("permission denied", "operation not permitted", "access denied")):
        return "permission_denied"
    if any(term in text for term in ("failed with result 'exit-code'", "failed to start", "main process exited")):
        return "service_start_failure"
    if any(term in text for term in ("start request repeated too quickly", "restart loop", "crashloop", "back-off restarting")):
        return "restart_loop"
    if any(term in text for term in ("process exited", "exited with status", "exit status")):
        return "process_exit"
    if "connection reset by peer" in text or "connection reset" in text:
        return "connection_reset"
    if (
        "http 5" in message_text
        or re.search(r"\b(?:status|status_code|http_status|response_status)\s*[=:]\s*5\d\d\b", message_text)
        or _attributes_contain_http_5xx(attributes)
    ):
        return "http_5xx"
    if _contains_non_negated_signal(text, ("timed out", "timeout", "deadline exceeded")):
        return "timeout"
    if "temporary failure in name resolution" in text or "dns" in text and any(term in text for term in ("failure", "failed", "nxdomain")):
        return "dns_failure"
    if any(term in text for term in ("unauthorized", "forbidden", "authentication failed", "authorization failed", "invalid credentials")):
        return "auth_failure"
    if any(term in text for term in ("invalid config", "configuration error", "config_error", "bad configuration")):
        return "config_error"
    if any(term in text for term in ("connection refused", "unreachable", "no route to host", "upstream unavailable")):
        return "dependency_unreachable"
    if _contains_non_negated_signal(text, ("memory cgroup out of memory", "out of memory", "oom")):
        return "oom"
    if any(term in text for term in ("state mismatch", "status mismatch", "expected state", "actual state", "contradicts", "healthy but")):
        return "state_mismatch"
    if any(term in text for term in ("no logs received", "log gap", "metric missing", "metrics missing", "heartbeat missing", "freshness gap", "stale data", "no samples")):
        return "monitoring_gap"
    if any(term in text for term in ("instrumentation mismatch", "schema mismatch", "parser failed", "parse failed", "missing label", "missing metric label", "unknown field")):
        return "instrumentation_mismatch"
    severity = str(severity_text or "").strip().casefold()
    if severity in {"warn", "warning"}:
        return "warning"
    if severity in {"trace", "debug", "info", "notice"}:
        return "info"
    return "unknown"


def _contains_non_negated_signal(text: str, terms: tuple[str, ...]) -> bool:
    for term in terms:
        for match in re.finditer(rf"\b{re.escape(term)}\b", text):
            prefix = text[max(0, match.start() - 100) : match.start()]
            suffix = text[match.end() : match.end() + 80]
            sentence_prefix = re.split(r"[.;!?]", prefix)[-1]
            if re.search(r"\b(?:no|without)\b[^.;!?]{0,90}$", sentence_prefix):
                continue
            if re.match(r"[^.;!?]{0,50}\b(?:not observed|not present|absent)\b", suffix):
                continue
            return True
    return False


def _attributes_contain_http_5xx(attributes: dict[str, Any]) -> bool:
    status_keys = {"status", "status_code", "http_status", "response_status", "response_code"}
    for _path, key, value in _flatten_scalars(attributes):
        if key not in status_keys:
            continue
        match = re.search(r"\b(\d{3})\b", str(value))
        if match and 500 <= int(match.group(1)) <= 599:
            return True
    return False


def _detect_protocol(message: str, attributes: dict[str, Any]) -> str:
    text = " ".join((str(message or ""), _semantic_attribute_text(attributes))).casefold()
    attribute_keys = {key for _path, key, _value in _flatten_scalars(attributes)}
    if any(term in text for term in ("tls", "ssl", "x509", "certificate")):
        return "tls"
    if "grpc" in text or "grpc_status" in attribute_keys:
        return "grpc"
    if "dns" in text or "nxdomain" in text:
        return "dns"
    if any(key in attribute_keys for key in ("http_status", "status_code", "response_status")) or re.search(r"\bhttp(?:/\d(?:\.\d)?)?\b", text):
        return "http"
    if "sqlstate" in attribute_keys or any(term in text for term in ("postgres", "mysql", "sqlite", "sqlstate")):
        return "sql"
    if "systemd" in text or ".service" in text:
        return "systemd"
    if any(term in text for term in ("kubernetes", "k8s", "pod", "crashloop")):
        return "kubernetes"
    if any(term in text for term in ("tcp", "udp", "socket", "connection reset", "connection refused")):
        return "network"
    return ""


def _event_family(event_name: str, *, protocol: str, message: str) -> str:
    if event_name in _FAMILY_BY_EVENT_NAME:
        return _FAMILY_BY_EVENT_NAME[event_name]
    text = " ".join((event_name, protocol, message)).casefold()
    groups = (
        ("network", ("tls", "ssl", "certificate", "dns", "socket", "tcp", "udp", "network", "connection_reset")),
        ("datastore", ("sql", "database", "postgres", "mysql", "sqlite", "redis")),
        ("authentication", ("auth", "credential", "permission", "forbidden", "unauthorized")),
        ("configuration", ("config", "missing_file", "missing_command", "schema")),
        ("resource", ("memory", "cpu", "disk", "quota", "oom", "resource")),
        ("runtime", ("process", "restart", "systemd", "kubernetes", "crashloop", "service_start")),
        ("observability", ("monitor", "metric", "heartbeat", "freshness", "instrumentation", "telemetry")),
        ("scheduler", ("scheduler", "scheduled", "cron", "job", "queue")),
        ("dependency", ("timeout", "deadline", "unreachable", "upstream", "dependency", "grpc", "http_5xx")),
    )
    for family, terms in groups:
        if any(term in text for term in terms):
            return family
    return "general"


def _normalize_semantic_name(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).strip().casefold()
    output: list[str] = []
    previous_separator = False
    for char in text:
        if char.isalnum():
            output.append(char)
            previous_separator = False
        elif not previous_separator:
            output.append("_")
            previous_separator = True
    return "".join(output).strip("_")[:120]


def _normalize_code(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).strip()
    text = re.sub(r"\s+", "_", text)
    text = re.sub(r"[^A-Za-z0-9_.:-]+", "_", text)
    return text.strip("_")[:120]


def _is_success_code(value: str) -> bool:
    match = re.fullmatch(r"\D*(\d{1,3})\D*", value)
    if not match:
        return value.casefold() in {"ok", "success", "successful"}
    code = int(match.group(1))
    return code == 0 or 100 <= code < 400


def _bounded_confidence(value: Any, *, default: float) -> float:
    try:
        confidence = float(value)
    except (TypeError, ValueError):
        confidence = default
    return round(max(0.0, min(confidence, 1.0)), 4)
