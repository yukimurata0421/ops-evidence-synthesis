from __future__ import annotations

import re
from typing import Any


TEXT_FIELDS = ("message_sanitized", "message_template", "error_type", "labels_json")
_PATH_RE = re.compile(
    r"(?P<path>/[A-Za-z0-9._~+@%=\-][A-Za-z0-9._~+@%=/:\-]*|(?:deployment|systemd|scripts|src|app|ops|bin|lib)/[A-Za-z0-9._~+@%=/:\-]+)"
)
_CANT_OPEN_RE = re.compile(r"(?:can't|cannot) open file ['\"](?P<path>[^'\"]+)['\"]", re.IGNORECASE)
_NO_SUCH_FILE_RE = re.compile(r"No such file or directory: ['\"]?(?P<path>/[^'\"\s]+)", re.IGNORECASE)
_UNIT_RE = re.compile(r"\b[A-Za-z0-9_.@-]+\.(?:service|timer|path|socket)\b")


def normalize_more_data_requests(
    next_data_needed: list[Any] | tuple[Any, ...] | None,
    requests: list[dict[str, Any]] | tuple[dict[str, Any], ...] | None = None,
) -> list[dict[str, Any]]:
    """Return stable request objects with ids, needs, descriptions, and search terms."""

    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, request in enumerate(requests or [], start=1):
        row = {
            "request_id": _safe_id(request.get("request_id") or f"more_data_{index}_query"),
            "profile_request_id": _safe_id(request.get("profile_request_id") or request.get("request_id") or f"more_data_{index}_query"),
            "request_type": _safe_id(request.get("request_type") or request.get("need") or request.get("request_id") or f"more_data_{index}"),
            "need": _safe_id(request.get("need") or request.get("request_id") or f"more_data_{index}"),
            "description": str(request.get("description") or request.get("need") or "").strip(),
            "target_component": str(request.get("target_component") or "").strip(),
            "preferred_sources": list(request.get("preferred_sources") or []),
            "search_terms": list(request.get("search_terms") or []),
        }
        row["terms"] = terms_for_request(row)
        if row["request_id"] not in seen:
            normalized.append(row)
            seen.add(row["request_id"])
    if normalized:
        return normalized
    for index, value in enumerate(next_data_needed or [], start=1):
        description = str(value or "").strip()
        if not description:
            continue
        request_id = _safe_id("_".join(_keywords(description)[:5]) or f"more_data_{index}") + "_query"
        row = {
            "request_id": request_id,
            "need": _safe_id("_".join(_keywords(description)[:4]) or f"more_data_{index}"),
            "description": description,
        }
        row["terms"] = terms_for_request(row)
        if row["request_id"] not in seen:
            normalized.append(row)
            seen.add(row["request_id"])
    return normalized


def filter_more_data_requests(
    requests: list[dict[str, Any]] | tuple[dict[str, Any], ...],
    request_ids: list[Any] | tuple[Any, ...] | set[Any] | None,
) -> list[dict[str, Any]]:
    """Filter normalized request objects by request id aliases used in the UI/API."""

    ids = {_safe_id(value) for value in request_ids or [] if str(value or "").strip()}
    if not ids:
        return [dict(request) for request in requests]
    filtered: list[dict[str, Any]] = []
    for request in requests:
        candidates = {
            _safe_id(request.get("request_id")),
            _safe_id(request.get("profile_request_id")),
            _safe_id(request.get("request_type")),
            _safe_id(request.get("need")),
        }
        if ids & candidates:
            filtered.append(dict(request))
    return filtered


def analyze_more_data_queries(queries: list[dict[str, Any]] | tuple[dict[str, Any], ...] | None) -> list[dict[str, Any]]:
    """Extract machine-checkable observations from More data preview rows."""

    analyses: list[dict[str, Any]] = []
    for query in queries or []:
        if not isinstance(query, dict):
            continue
        rows = [row for row in query.get("preview_rows") or [] if isinstance(row, dict)]
        text_rows = [_analysis_text(row) for row in rows]
        combined_text = "\n".join(text_rows)
        request_type = _safe_id(query.get("request_type") or query.get("need") or query.get("request_id"))
        paths = _unique(_extract_paths(combined_text))
        missing_paths = _unique(_extract_missing_paths(combined_text))
        units = _unique(_UNIT_RE.findall(combined_text))
        commands = _unique(_extract_commands(combined_text))
        observations = _base_observations(rows, query)
        if request_type == "job_definition":
            if units:
                observations.append(f"Observed unit definitions: {', '.join(units[:8])}")
            if commands:
                observations.append(f"Observed configured commands: {', '.join(commands[:6])}")
            if paths:
                observations.append(f"Observed configured or referenced paths: {', '.join(paths[:8])}")
        elif request_type == "installed_artifact":
            if missing_paths:
                observations.append(f"Observed missing artifacts: {', '.join(missing_paths[:8])}")
            elif paths:
                observations.append(f"Observed artifact paths: {', '.join(paths[:8])}")
        elif request_type == "scheduler_history":
            timestamps = _unique(str(row.get("timestamp") or "") for row in rows if row.get("timestamp"))
            if timestamps:
                observations.append(f"Scheduler evidence rows span {min(timestamps)} to {max(timestamps)}")
            if units:
                observations.append(f"Scheduler units observed: {', '.join(units[:8])}")
        elif request_type == "deployment_correlation":
            deploy_rows = [text for text in text_rows if any(term in text.casefold() for term in ("deploy", "release", "rollout", "install", "package", "config"))]
            if deploy_rows:
                observations.append(f"Deployment/configuration terms appeared in {len(deploy_rows)} preview rows")
        elif request_type == "process_state":
            state_terms = _state_terms(combined_text)
            if state_terms:
                observations.append(f"Process state terms observed: {', '.join(state_terms[:10])}")
            if units:
                observations.append(f"Process units observed: {', '.join(units[:8])}")
        analyses.append(
            _drop_empty(
                {
                    "request_id": str(query.get("request_id") or ""),
                    "profile_request_id": str(query.get("profile_request_id") or ""),
                    "request_type": request_type,
                    "need": str(query.get("need") or ""),
                    "status": "evidence_collected" if rows else "no_preview_rows",
                    "row_count": len(rows),
                    "summary": _analysis_summary(query, observations, rows),
                    "observations": observations,
                    "units": units[:20],
                    "paths": paths[:30],
                    "configured_commands": commands[:20],
                    "missing_paths": missing_paths[:30],
                    "evidence_kind": "more_data_analysis",
                }
            )
        )
    comparison = _job_definition_artifact_comparison(analyses)
    if comparison:
        analyses.append(comparison)
    return analyses


def terms_for_request(request: dict[str, Any]) -> list[str]:
    terms: list[str] = []
    terms.extend(str(term) for term in request.get("search_terms") or [])
    terms.extend(str(term) for term in request.get("terms") or [])
    if not terms:
        text = " ".join(
            str(request.get(key) or "")
            for key in ("request_id", "request_type", "need", "description", "target_component")
        ).casefold()
        terms.extend(_keywords(text)[:8])
    return _unique(term.casefold() for term in terms if term)


def _job_definition_artifact_comparison(analyses: list[dict[str, Any]]) -> dict[str, Any]:
    job_paths: set[str] = set()
    artifact_paths: set[str] = set()
    for analysis in analyses:
        request_type = str(analysis.get("request_type") or "")
        if request_type == "job_definition":
            job_paths.update(str(path) for path in analysis.get("paths") or [])
        elif request_type == "installed_artifact":
            artifact_paths.update(str(path) for path in analysis.get("missing_paths") or [])
    if not job_paths or not artifact_paths:
        return {}
    overlap = sorted(job_paths & artifact_paths)
    likely_overlap = overlap or sorted(path for path in artifact_paths if any(path.endswith(job_path.split("/")[-1]) for job_path in job_paths))
    if not likely_overlap:
        return {}
    return {
        "request_id": "job_definition_artifact_comparison",
        "profile_request_id": "job_definition_artifact_comparison",
        "request_type": "artifact_comparison",
        "need": "compare_job_definition_with_installed_artifact",
        "status": "evidence_collected",
        "row_count": 0,
        "summary": "Configured job definition paths match missing installed artifact observations.",
        "observations": [
            f"Configured or referenced paths: {', '.join(sorted(job_paths)[:8])}",
            f"Missing artifact paths: {', '.join(sorted(artifact_paths)[:8])}",
            f"Matched paths: {', '.join(likely_overlap[:8])}",
        ],
        "paths": sorted(job_paths)[:30],
        "missing_paths": sorted(artifact_paths)[:30],
        "matched_paths": likely_overlap[:30],
        "evidence_kind": "more_data_analysis",
    }


def _base_observations(rows: list[dict[str, Any]], query: dict[str, Any]) -> list[str]:
    if rows:
        return [f"{len(rows)} preview rows returned for {query.get('request_id') or 'more_data_query'}."]
    return [f"No preview rows returned for {query.get('request_id') or 'more_data_query'}."]


def _analysis_summary(query: dict[str, Any], observations: list[str], rows: list[dict[str, Any]]) -> str:
    label = str(query.get("request_id") or query.get("request_type") or "more_data_query")
    if observations:
        return f"{label}: {observations[-1]}"
    return f"{label}: {len(rows)} preview rows analyzed."


def _extract_paths(value: str) -> list[str]:
    paths: list[str] = []
    for match in _PATH_RE.finditer(value or ""):
        path = match.group("path").strip().rstrip(".,;:)'\"")
        if path and not _looks_like_status_suffix(path):
            paths.append(path.strip("'\""))
    return _unique(paths)


def _extract_missing_paths(value: str) -> list[str]:
    output: list[str] = []
    for text in (value or "").splitlines():
        folded = text.casefold()
        if not any(term in folded for term in ("no such file", "can't open file", "cannot open file", "missing", "not found")):
            continue
        quoted_targets = [
            match.group("path").strip()
            for pattern in (_CANT_OPEN_RE, _NO_SUCH_FILE_RE)
            for match in pattern.finditer(text)
            if match.group("path").strip()
        ]
        output.extend(quoted_targets or _extract_paths(text))
    return _unique(output)


def _extract_commands(value: str) -> list[str]:
    output: list[str] = []
    for line in (value or "").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        folded = stripped.casefold()
        if any(term in folded for term in ("execstart", "command=", "cmd=", "argv", "can't open file", "cannot open file")):
            output.append(stripped[:240])
    return _unique(output)


def _state_terms(value: str) -> list[str]:
    folded = (value or "").casefold()
    candidates = (
        "failed",
        "failure",
        "exit-code",
        "exited",
        "restart",
        "started",
        "stopped",
        "inactive",
        "dead",
        "running",
        "activating",
        "main process exited",
    )
    return [term for term in candidates if term in folded]


def _row_text(row: dict[str, Any]) -> str:
    parts: list[str] = []
    for field in ("timestamp", "service", "severity", *TEXT_FIELDS):
        value = row.get(field)
        if isinstance(value, dict):
            parts.append(str(value))
        elif value not in (None, ""):
            parts.append(str(value))
    return " ".join(parts)


def _analysis_text(row: dict[str, Any]) -> str:
    return " ".join(
        str(row.get(field) or "")
        for field in ("timestamp", "service", "severity", "message_sanitized", "message_template", "error_type")
        if row.get(field) not in (None, "")
    )


def _looks_like_status_suffix(path: str) -> bool:
    if path.count("/") <= 1 and path.upper() == path:
        return True
    return False


def bigquery_text_predicate_for_request(request: dict[str, Any]) -> str:
    terms = terms_for_request(request)
    if not terms:
        return "TRUE"
    haystack = "LOWER(CONCAT(" + ", ' ', ".join(
        _bigquery_text_expr(field)
        for field in TEXT_FIELDS
    ) + "))"
    return "(" + " OR ".join(f"{haystack} LIKE '%{_sql_literal(term)}%'" for term in terms) + ")"


def sqlite_text_predicate_for_request(request: dict[str, Any]) -> tuple[str, list[str]]:
    terms = terms_for_request(request)
    if not terms:
        return "1 = 1", []
    field_expr = " || ' ' || ".join(f"COALESCE({field}, '')" for field in TEXT_FIELDS)
    predicate = "(" + " OR ".join(f"LOWER({field_expr}) LIKE ?" for _ in terms) + ")"
    return predicate, [f"%{term}%" for term in terms]


def _keywords(value: str) -> list[str]:
    return _unique(
        token
        for token in re.findall(r"[a-z0-9_]{3,}", value.casefold())
        if token not in {"the", "and", "for", "with", "during", "around", "query"}
    )


def _safe_id(value: Any) -> str:
    text = str(value or "").strip().casefold()
    text = re.sub(r"[^a-z0-9_]+", "_", text)
    text = re.sub(r"_+", "_", text).strip("_")
    return text or "more_data"


def _sql_literal(value: str) -> str:
    return value.replace("'", "''")


def _bigquery_text_expr(field: str) -> str:
    if field == "labels_json":
        return "COALESCE(TO_JSON_STRING(labels_json), '')"
    return f"COALESCE(CAST({field} AS STRING), '')"


def _unique(values: Any) -> list[str]:
    output: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if text and text not in output:
            output.append(text)
    return output


def _drop_empty(row: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in row.items() if value not in (None, "", [], {})}
