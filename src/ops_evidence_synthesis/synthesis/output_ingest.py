from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass
from typing import Any

from ops_evidence_synthesis.canonical import sha256_json, sha256_text
from ops_evidence_synthesis.timeutils import utc_now


_CODE_FENCE_RE = re.compile(r"```(?:json|JSON)?\s*(.*?)\s*```", re.DOTALL)
_NONE_LITERAL_RE = re.compile(r"(?P<prefix>[:\[,]\s*)None(?P<suffix>\s*[,}\]])")
_TRUE_LITERAL_RE = re.compile(r"(?P<prefix>[:\[,]\s*)True(?P<suffix>\s*[,}\]])")
_FALSE_LITERAL_RE = re.compile(r"(?P<prefix>[:\[,]\s*)False(?P<suffix>\s*[,}\]])")
_KEY_VALUE_DOUBLE_WRAPPED_RE = re.compile(
    r'^(?P<prefix>\s*"[^"\n\r]+"\s*:\s*)""(?P<body>.*)""(?P<suffix>\s*(?:[,}\]])?\s*)$'
)
_STRING_TOKEN_INNER_QUOTE_RE = re.compile(
    r'^(?P<indent>\s*)"(?P<prefix>[^"\n\r]*)"(?P<body>[^"\n\r]+)""(?P<trail>\s*,?\s*)$'
)
_DOUBLED_QUOTE_TOKEN_RE = re.compile(r'^(?P<indent>\s*)""(?P<body>.*)""(?P<trail>\s*,?\s*)$')
_HALF_DOUBLED_QUOTE_TOKEN_RE = re.compile(r'^(?P<indent>\s*)""(?P<body>.*)"(?P<trail>\s*,?\s*)$')
_KEY_VALUE_SNIPPET_RE = re.compile(r'^[A-Za-z0-9_.-]+":\s')


@dataclass(frozen=True, slots=True)
class RepairResult:
    text: str
    applied_rules: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ModelOutputParse:
    parsed: dict[str, Any] | None
    parse_status: str
    parse_errors: tuple[str, ...]
    original_parse_errors: tuple[str, ...]
    repaired_output: str
    repaired_output_sha256: str
    repair_applied: bool
    repair_rules: tuple[str, ...]


def repair_json_text(text: str) -> RepairResult:
    """Apply only syntax-level repair rules and keep the source text unchanged."""
    working = text
    applied: list[str] = []

    repaired, changed = _strip_code_fence_wrapper(working)
    if changed:
        working = repaired
        applied.append("strip_code_fence_wrapper:1")

    repaired, changed = _extract_json_window(working)
    if changed:
        working = repaired
        applied.append("extract_json_window:1")

    repaired, changed = _replace_python_literals(working)
    if changed:
        working = repaired
        applied.append(f"python_literal_to_json_literal:{changed}")

    repaired, changed = _repair_key_value_double_wrapped_values(working)
    if changed:
        working = repaired
        applied.append(f"double_wrapped_key_value_string:{changed}")

    repaired, changed = _repair_string_tokens_with_inner_quote(working)
    if changed:
        working = repaired
        applied.append(f"string_token_inner_quote_escape:{changed}")

    repaired, changed = _repair_doubled_quote_tokens(working)
    if changed:
        working = repaired
        applied.append(f"doubled_quote_string_tokens:{changed}")

    return RepairResult(text=working, applied_rules=tuple(applied))


def parse_model_output(raw_output: str) -> ModelOutputParse:
    from ops_evidence_synthesis.synthesis.validation import parse_model_json

    parsed, parse_errors = parse_model_json(raw_output)
    if parsed is not None:
        return ModelOutputParse(
            parsed=parsed,
            parse_status="parsed_original",
            parse_errors=(),
            original_parse_errors=(),
            repaired_output=raw_output,
            repaired_output_sha256=sha256_text(raw_output),
            repair_applied=False,
            repair_rules=(),
        )

    repair = repair_json_text(raw_output)
    if not repair.applied_rules:
        return ModelOutputParse(
            parsed=None,
            parse_status="invalid_json",
            parse_errors=parse_errors,
            original_parse_errors=parse_errors,
            repaired_output=raw_output,
            repaired_output_sha256=sha256_text(raw_output),
            repair_applied=False,
            repair_rules=(),
        )

    repaired_parsed, repaired_errors = parse_model_json(repair.text)
    if repaired_parsed is not None:
        return ModelOutputParse(
            parsed=repaired_parsed,
            parse_status="parsed_repaired",
            parse_errors=(),
            original_parse_errors=parse_errors,
            repaired_output=repair.text,
            repaired_output_sha256=sha256_text(repair.text),
            repair_applied=True,
            repair_rules=repair.applied_rules,
        )

    return ModelOutputParse(
        parsed=None,
        parse_status="invalid_after_repair",
        parse_errors=tuple([*parse_errors, *repaired_errors]),
        original_parse_errors=parse_errors,
        repaired_output=repair.text,
        repaired_output_sha256=sha256_text(repair.text),
        repair_applied=True,
        repair_rules=repair.applied_rules,
    )


def model_output_artifact(
    *,
    run_id: str,
    evidence_sha256: str,
    provider: str,
    model_name: str,
    raw_output_sha256: str,
    parse_result: ModelOutputParse,
    parsed_json_sha256: str,
    schema_valid: bool,
    schema_errors: tuple[str, ...] | list[str],
    status: str,
    created_at: str | None = None,
) -> dict[str, Any]:
    artifact = {
        "schema_version": "ai_output_artifact.v1",
        "run_id": str(run_id),
        "evidence_sha256": str(evidence_sha256),
        "provider": str(provider),
        "model_name": str(model_name),
        "raw_output_sha256": str(raw_output_sha256),
        "repaired_output_sha256": str(parse_result.repaired_output_sha256),
        "parsed_json_sha256": str(parsed_json_sha256),
        "parse_status": str(parse_result.parse_status),
        "parse_errors": list(parse_result.parse_errors),
        "original_parse_errors": list(parse_result.original_parse_errors),
        "repair_applied": bool(parse_result.repair_applied),
        "repair_rules": list(parse_result.repair_rules),
        "schema_valid": bool(schema_valid),
        "schema_errors": [str(error) for error in schema_errors],
        "provider_status": str(status),
        "original_preserved": True,
        "created_at": str(created_at or utc_now()),
    }
    artifact["artifact_id"] = "aio-" + sha256_json(
        {
            "run_id": artifact["run_id"],
            "raw_output_sha256": artifact["raw_output_sha256"],
            "repaired_output_sha256": artifact["repaired_output_sha256"],
            "parse_status": artifact["parse_status"],
        }
    )[:20]
    return artifact


def merge_candidate_observations(
    candidates: list[dict[str, Any]],
    *,
    evidence_sha256: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    passthrough: list[dict[str, Any]] = []
    for candidate in candidates:
        if str(candidate.get("original_class") or "") == "context":
            passthrough.append(candidate)
            continue
        key = canonical_observation_key(candidate, evidence_sha256=evidence_sha256)
        grouped.setdefault(key["canonical_group_key"], []).append({**candidate, **key})

    merged: list[dict[str, Any]] = []
    groups: list[dict[str, Any]] = []
    for key, rows in sorted(grouped.items()):
        merged_candidate, group = _merge_observation_group(key, rows, evidence_sha256=evidence_sha256)
        merged.append(merged_candidate)
        groups.append(group)
    merged.extend(passthrough)
    return merged, groups


def canonical_observation_key(candidate: dict[str, Any], *, evidence_sha256: str) -> dict[str, Any]:
    text = _candidate_text(candidate)
    target_type = _canonical_target_type(candidate, text)
    subject = _canonical_subject(candidate, text)
    review_unit = _canonical_review_unit(candidate, subject)
    review_family = _canonical_review_family(candidate, text, target_type=target_type, review_unit=review_unit)
    key_payload = {
        "evidence_sha256": str(evidence_sha256 or candidate.get("evidence_sha256") or ""),
        "canonical_review_unit": review_unit,
    }
    if review_family:
        key_payload["canonical_review_family"] = review_family
    grouping_dimensions = ["canonical_review_unit"]
    if review_family:
        grouping_dimensions.append("canonical_review_family")
    return {
        "canonical_group_key": sha256_json(key_payload)[:24],
        "canonical_target_type": target_type,
        "canonical_subject": subject,
        "canonical_review_unit": review_unit,
        "canonical_review_family": review_family,
        "canonical_key_contract": {
            "schema_version": "canonical_observation_key.v1",
            "evidence_sha256_scope": "entire_evidence_bundle",
            "evidence_id_set_in_key": False,
            "hash_dimensions": ["evidence_bundle_sha256", *grouping_dimensions],
            "effective_within_bundle_dimensions": grouping_dimensions,
        },
    }


def observation_groups_from_graph(graph: dict[str, Any]) -> list[dict[str, Any]]:
    if not isinstance(graph, dict):
        return []
    evidence_sha256 = str(graph.get("evidence_sha256") or "")
    groups: list[dict[str, Any]] = []
    targets = [
        row
        for row in [
            *(graph.get("primary_targets") or []),
            *(graph.get("validation_targets") or []),
            *(graph.get("monitor_only") or []),
            *(graph.get("auto_archived") or []),
        ]
        if isinstance(row, dict)
    ]
    for target in targets:
        group_key = str(target.get("canonical_group_key") or "")
        if not group_key:
            key = canonical_observation_key(target, evidence_sha256=evidence_sha256)
            group_key = key["canonical_group_key"]
        drawer = target.get("drawer") if isinstance(target.get("drawer"), dict) else {}
        rollup = target.get("rollup") if isinstance(target.get("rollup"), dict) else {}
        group = {
            "schema_version": "canonical_observation_group.v1",
            "group_id": str(target.get("canonical_observation_group_id") or target.get("target_id") or group_key),
            "evidence_sha256": evidence_sha256,
            "canonical_group_key": group_key,
            "canonical_target_type": str(target.get("canonical_target_type") or target.get("core_target_type") or ""),
            "canonical_subject": str(target.get("canonical_subject") or target.get("subsystem") or "general"),
            "canonical_review_unit": str(target.get("canonical_review_unit") or target.get("component") or target.get("subsystem") or "general"),
            "subsystem": str(target.get("subsystem") or "general"),
            "component": str(target.get("component") or ""),
            "class": str(target.get("class") or ""),
            "state": str(target.get("state") or ""),
            "source_target_ids": _unique_str(target.get("source_target_ids") or [target.get("source_target_id") or target.get("target_id")]),
            "source_candidate_count": int(target.get("source_candidate_count") or rollup.get("source_candidate_count") or 1),
            "providers": _unique_str(target.get("providers") or []),
            "provider_count": int(target.get("provider_count") or 0),
            "evidence_refs": _unique_str(target.get("evidence_refs") or []),
            "missing_evidence": _unique_str(target.get("missing_evidence") or drawer.get("missing_evidence") or []),
            "caveats": _unique_str(target.get("caveats") or drawer.get("caveats") or []),
            "support_evidence": drawer.get("support_evidence") or [],
            "counter_evidence": drawer.get("counter_evidence") or [],
            "rollup": rollup,
            "rollup_provider_ratio": float(
                target.get("rollup_provider_ratio") or rollup.get("rollup_provider_ratio") or 0.0
            ),
            "baseline_support_score": float(target.get("baseline_support_score") or rollup.get("baseline_support_score") or 0.0),
            "review_priority_score": float(target.get("review_priority_score") or 0.0),
            "consensus_class": _consensus_class(target),
            "group_json": target,
        }
        groups.append(group)
    return groups


def _strip_code_fence_wrapper(text: str) -> tuple[str, int]:
    matches = _CODE_FENCE_RE.findall(text)
    if not matches:
        return text, 0
    best = max(matches, key=len)
    return (best.strip(), 1) if best.strip() else (text, 0)


def _extract_json_window(text: str) -> tuple[str, int]:
    starts = [idx for idx in (text.find("{"), text.find("[")) if idx >= 0]
    if not starts:
        return text, 0
    start = min(starts)
    end = max(text.rfind("}"), text.rfind("]"))
    if end <= start:
        return text, 0
    candidate = text[start : end + 1]
    if candidate == text or not candidate.strip():
        return text, 0
    return candidate, 1


def _replace_python_literals(text: str) -> tuple[str, int]:
    total = 0
    working = text
    working, count = _NONE_LITERAL_RE.subn(r"\g<prefix>null\g<suffix>", working)
    total += count
    working, count = _TRUE_LITERAL_RE.subn(r"\g<prefix>true\g<suffix>", working)
    total += count
    working, count = _FALSE_LITERAL_RE.subn(r"\g<prefix>false\g<suffix>", working)
    total += count
    return working, total


def _repair_key_value_double_wrapped_values(text: str) -> tuple[str, int]:
    changed = 0
    lines: list[str] = []
    for line, newline in _split_lines(text):
        match = _KEY_VALUE_DOUBLE_WRAPPED_RE.match(line)
        if match is None:
            lines.append(line + newline)
            continue
        body = match.group("body")
        body = body.replace('"\\r\\n"', "\r\n").replace('"\\n"', "\n").replace('"\\t"', "\t")
        lines.append(f"{match.group('prefix')}{json.dumps(body, ensure_ascii=False)}{match.group('suffix')}{newline}")
        changed += 1
    return "".join(lines), changed


def _repair_string_tokens_with_inner_quote(text: str) -> tuple[str, int]:
    changed = 0
    lines: list[str] = []
    for line, newline in _split_lines(text):
        match = _STRING_TOKEN_INNER_QUOTE_RE.match(line)
        if match is None:
            lines.append(line + newline)
            continue
        prefix = match.group("prefix")
        body = match.group("body")
        if not prefix.strip() or not body.strip() or "\\" in prefix or "\\" in body:
            lines.append(line + newline)
            continue
        lines.append(f'{match.group("indent")}"{prefix}\\"{body}\\""{match.group("trail")}{newline}')
        changed += 1
    return "".join(lines), changed


def _repair_doubled_quote_tokens(text: str) -> tuple[str, int]:
    changed = 0
    lines: list[str] = []
    for line, newline in _split_lines(text):
        match = _DOUBLED_QUOTE_TOKEN_RE.match(line) or _HALF_DOUBLED_QUOTE_TOKEN_RE.match(line)
        if match is None:
            lines.append(line + newline)
            continue
        body = match.group("body")
        if _KEY_VALUE_SNIPPET_RE.match(body):
            if not body.startswith('"'):
                body = f'"{body}'
            if ': "' in body and not body.endswith('"'):
                body = f'{body}"'
        lines.append(f"{match.group('indent')}{json.dumps(body, ensure_ascii=False)}{match.group('trail')}{newline}")
        changed += 1
    return "".join(lines), changed


def _split_lines(text: str) -> list[tuple[str, str]]:
    output: list[tuple[str, str]] = []
    for line in text.splitlines(keepends=True):
        if line.endswith("\r\n"):
            output.append((line[:-2], "\r\n"))
        elif line.endswith("\n"):
            output.append((line[:-1], "\n"))
        else:
            output.append((line, ""))
    if not output and text == "":
        return [("", "")]
    return output


def _candidate_text(candidate: dict[str, Any]) -> str:
    raw = candidate.get("raw") if isinstance(candidate.get("raw"), dict) else {}
    explanation = candidate.get("target_explanation") if isinstance(candidate.get("target_explanation"), dict) else {}
    parts = [
        candidate.get("title"),
        candidate.get("impact_summary"),
        candidate.get("core_target_type"),
        candidate.get("subsystem"),
        candidate.get("component"),
        candidate.get("suspected_issue"),
        candidate.get("operational_mechanism"),
        candidate.get("why_it_matters"),
        " ".join(str(item) for item in candidate.get("evidence_summary") or []),
        " ".join(str(item) for item in explanation.get("evidence_summary") or []),
        explanation.get("suspected_issue"),
        explanation.get("operational_mechanism"),
        explanation.get("why_it_matters"),
        " ".join(str(item.get("claim_text") or "") for item in explanation.get("provider_explanations") or [] if isinstance(item, dict)),
        " ".join(str(item) for item in candidate.get("missing_evidence") or []),
        " ".join(str(item) for item in candidate.get("caveats") or []),
        raw.get("title"),
        raw.get("core_claim"),
        raw.get("support_summary"),
    ]
    return " ".join(str(part) for part in parts if str(part or "").strip()).casefold()


def _canonical_target_type(candidate: dict[str, Any], text: str) -> str:
    explicit = str(candidate.get("core_target_type") or candidate.get("review_target_type") or "").strip()
    if explicit and explicit not in {"general", "general_review", "validation_target"}:
        normalized = _normalize_token(explicit)
        if normalized in {"restart_loop", "process_restart", "process_restart_loop"}:
            return "process_restart_loop"
        if normalized in {"transport_failure", "transport_path_failure", "throughput_disappearance"}:
            return "transport_path_failure"
        if normalized in {"freshness_signal_gap", "capture_freshness", "capture_freshness_gap"}:
            return "capture_freshness_gap"
        if normalized in {"external_dependency_failure", "external_dependency_health"}:
            return "external_dependency_health"
        return normalized
    if _is_database_pool_text(text):
        return "database_connection_pool_exhaustion"
    if "database" in text and any(token in text for token in ("timeout", "timed out", "latency", "slow query")):
        return "database_timeout"
    if any(token in text for token in ("payment-gateway", "payment gateway")):
        return "external_dependency_timeout"
    if any(token in text for token in ("restart", "restarted", "crash", "crashed", "exit code", "process state", "loop")):
        return "process_restart_loop"
    if any(token in text for token in ("memory", "oom", "resource pressure", "memory_critical", "memory critical")):
        return "resource_pressure"
    if any(token in text for token in ("traceback", "exception occurred", "unhandled exception", "request processing")):
        return "runtime_exception"
    if any(token in text for token in ("transport", "connection reset", "io error", "i/o error", "throughput", "send-path", "send path", "rtmps", "packet loss", "timeout", "timed out", "cloudflare", "tcp anchor", "anchor observer")):
        return "transport_path_failure"
    if any(token in text for token in ("youtube", "external dependency", "watch url", "ingest health")):
        return "external_dependency_health"
    if any(token in text for token in ("freshness", "capture", "chromium", "timestamp drift")):
        return "capture_freshness_gap"
    if any(token in text for token in ("audio", "viewer", "watch", "user impact", "user-impact", "customer impact")):
        return "user_impact_signal_gap"
    if any(token in text for token in ("observability", "contract", "instrumentation")):
        return "observability_contract_mismatch"
    return explicit or "general_review"


def _canonical_subject(candidate: dict[str, Any], text: str) -> str:
    explicit_target = _normalize_token(str(candidate.get("core_target_type") or ""))
    explicit_subsystem = _normalize_token(str(candidate.get("subsystem") or ""))
    explicit_component = _normalize_token(str(candidate.get("component") or ""))
    if explicit_target == "database_connection_pool_exhaustion":
        return "database_connection_pool"
    if explicit_target == "deployment_regression" or "deployment_regression" in {explicit_subsystem, explicit_component}:
        return "deployment_regression"
    trusted_explicit_units = {
        "database_connection_pool",
        "database_dependency",
        "deployment_regression",
        "job_configuration",
        "observability_contract",
        "payment_gateway",
        "resource_pressure",
        "runtime_recovery",
        "service_liveness",
        "traffic",
        "user_experience",
    }
    if explicit_component in trusted_explicit_units:
        return explicit_component
    if explicit_subsystem in trusted_explicit_units:
        return explicit_subsystem
    if _is_database_pool_text(text):
        return "database_connection_pool"
    if "database" in text and any(token in text for token in ("timeout", "timed out", "latency", "slow query")):
        return "database_dependency"
    if any(token in text for token in ("payment-gateway", "payment gateway")):
        return "payment_gateway"
    if any(token in text for token in ("memory", "oom", "resource pressure", "memory_critical", "memory critical")):
        return "resource_pressure"
    if any(token in text for token in ("traceback", "exception occurred", "unhandled exception", "request processing")):
        return "runtime_exception"
    if any(token in text for token in ("rtmps", "ffmpeg", "send-path", "send path", "transport")):
        return "transport_sender"
    if any(token in text for token in ("cloudflare", "tcp anchor", "anchor observer", "timeout", "timed out")):
        return "transport_sender"
    if any(token in text for token in ("youtube", "watch url", "external ingest", "ingest status")):
        return "external_ingest"
    if any(token in text for token in ("chromium", "capture", "freshness")):
        return "capture_pipeline"
    if "audio" in text:
        return "media_output"
    if any(token in text for token in ("watchdog", "service health", "substate", "process state")):
        return "service_health"
    subsystem = str(candidate.get("subsystem") or "").strip()
    component = str(candidate.get("component") or "").strip()
    return _normalize_token(component or subsystem or "general")


def _is_database_pool_text(text: str) -> bool:
    lowered = str(text or "").casefold()
    if any(token in lowered for token in ("connection pool", "connection_pool", "db_pool", "checkout-db")):
        return True
    return "pool exhaust" in lowered and any(
        token in lowered
        for token in ("database", "postgres", "mysql", "sql", "db connection", "jdbc")
    )


def _canonical_review_unit(candidate: dict[str, Any], subject: str) -> str:
    normalized_subject = _normalize_token(subject or "general")
    if normalized_subject in {
        "transport_sender",
        "database_connection_pool",
        "database_dependency",
        "payment_gateway",
        "deployment_regression",
    }:
        return normalized_subject
    component = _normalize_token(str(candidate.get("component") or ""))
    if component and component not in {"general", "unknown", "none", "null"}:
        return component
    subsystem = _normalize_token(str(candidate.get("subsystem") or ""))
    if subsystem and subsystem not in {"general", "unknown", "none", "null"}:
        return subsystem
    return normalized_subject


def _canonical_review_family(
    candidate: dict[str, Any],
    text: str,
    *,
    target_type: str,
    review_unit: str,
) -> str:
    if str(candidate.get("source") or "") == "evidence_relationship":
        return "evidence_relationship"
    subsystem = _normalize_token(str(candidate.get("subsystem") or ""))
    if subsystem and subsystem not in {"general", "unknown", "none", "null"}:
        return ""
    normalized_unit = _normalize_token(review_unit)
    if normalized_unit not in {"general", "unknown", "none", "null"}:
        return ""
    if target_type and target_type != "general_review":
        return target_type
    if any(token in text for token in ("memory", "oom", "resource pressure", "memory_critical", "memory critical")):
        return "resource_pressure"
    if any(token in text for token in ("traceback", "exception occurred", "unhandled exception", "request processing")):
        return "runtime_exception"
    if any(token in text for token in ("cloudflare", "tcp anchor", "anchor observer", "timeout", "timed out", "transport", "connection reset", "rtmps", "packet loss")):
        return "transport_path_failure"
    return ""


def _merge_observation_group(
    canonical_group_key: str,
    rows: list[dict[str, Any]],
    *,
    evidence_sha256: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    top = sorted(
        rows,
        key=lambda row: (
            -float(row.get("score_before") or 0.0),
            0 if str(row.get("original_class") or "") == "primary_candidate" else 1,
            str(row.get("target_id") or ""),
        ),
    )[0]
    source_target_ids = _unique_str(row.get("target_id") for row in rows)
    providers = _unique_str(provider for row in rows for provider in row.get("providers") or [])
    supporting_providers = _unique_str(
        provider for row in rows for provider in row.get("supporting_providers") or []
    )
    countering_providers = _unique_str(
        provider for row in rows for provider in row.get("countering_providers") or []
    )
    evidence_refs = _unique_str(ref for row in rows for ref in row.get("evidence_refs") or [])
    support_evidence_refs = _unique_str(
        ref for row in rows for ref in row.get("support_evidence_refs") or []
    )
    counter_evidence_refs = _unique_str(
        ref for row in rows for ref in row.get("counter_evidence_refs") or []
    )
    support_evidence = _merge_jsonish(row.get("support_evidence") for row in rows)
    counter_evidence = _merge_jsonish(row.get("counter_evidence") for row in rows)
    missing = _unique_str(item for row in rows for item in row.get("missing_evidence") or [])
    caveats = _unique_str(item for row in rows for item in row.get("caveats") or [])
    target_explanation = _merge_target_explanations(rows, top)
    rollup = _rollup_profile(rows, evidence_refs=evidence_refs)
    source_candidates = _source_candidate_summaries(rows)
    group_id = "cog-" + sha256_json(
        {
            "evidence_sha256": evidence_sha256,
            "canonical_group_key": canonical_group_key,
            "source_target_ids": source_target_ids,
        }
    )[:20]
    original_classes = {str(row.get("original_class") or "") for row in rows}
    evidence_relationship_supported = any(
        str(row.get("source") or "") == "evidence_relationship" for row in rows
    )
    original_class = "primary_candidate" if "primary_candidate" in original_classes else str(top.get("original_class") or "validation_target")
    merged = {
        **top,
        "target_id": group_id,
        "source": "canonical_observation_group",
        "original_class": original_class,
        "providers": providers,
        "provider_count": max(len(providers), max((int(row.get("provider_count") or 0) for row in rows), default=0)),
        "participating_providers": providers,
        "participating_provider_count": max(
            len(providers),
            max((int(row.get("participating_provider_count") or 0) for row in rows), default=0),
        ),
        "supporting_providers": supporting_providers,
        "support_provider_count": len(supporting_providers),
        "countering_providers": countering_providers,
        "counter_provider_count": len(countering_providers),
        "evidence_refs": evidence_refs,
        "support_evidence_refs": support_evidence_refs,
        "counter_evidence_refs": counter_evidence_refs,
        "all_referenced_evidence_refs": evidence_refs,
        "support_evidence": support_evidence,
        "counter_evidence": counter_evidence,
        "missing_evidence": missing,
        "caveats": caveats,
        "target_explanation": target_explanation,
        "suspected_issue": target_explanation.get("suspected_issue", ""),
        "operational_mechanism": target_explanation.get("operational_mechanism", ""),
        "why_it_matters": target_explanation.get("why_it_matters", ""),
        "evidence_summary": target_explanation.get("evidence_summary", []),
        "counter_evidence_summary": target_explanation.get("counter_evidence_summary", []),
        "why_not_promoted": target_explanation.get("why_not_promoted", ""),
        "next_validation_question": target_explanation.get("next_validation_question", ""),
        "score_before": max(float(row.get("score_before") or 0.0) for row in rows),
        "group_id": group_id,
        "canonical_group_key": canonical_group_key,
        "canonical_target_type": str(top.get("canonical_target_type") or ""),
        "canonical_subject": str(top.get("canonical_subject") or ""),
        "canonical_review_unit": str(top.get("canonical_review_unit") or ""),
        "canonical_review_family": str(top.get("canonical_review_family") or ""),
        "canonical_key_contract": dict(top.get("canonical_key_contract") or {}),
        "source_target_ids": source_target_ids,
        "source_candidate_count": len(rows),
        "source_candidates": source_candidates,
        "rollup": rollup,
        "rollup_provider_ratio": rollup["rollup_provider_ratio"],
        "baseline_support_score": rollup["baseline_support_score"],
        "evidence_relationship_supported": evidence_relationship_supported,
        "raw": {
            "source": "canonical_observation_group",
            "source_candidates": [row.get("raw") if isinstance(row.get("raw"), dict) else row for row in rows],
            "source_target_ids": source_target_ids,
        },
    }
    group = {
        "schema_version": "canonical_observation_group.v1",
        "group_id": group_id,
        "evidence_sha256": str(evidence_sha256),
        "canonical_group_key": canonical_group_key,
        "canonical_target_type": str(merged.get("canonical_target_type") or merged.get("core_target_type") or ""),
        "canonical_subject": str(merged.get("canonical_subject") or ""),
        "canonical_review_unit": str(merged.get("canonical_review_unit") or ""),
        "canonical_review_family": str(merged.get("canonical_review_family") or ""),
        "canonical_key_contract": dict(merged.get("canonical_key_contract") or {}),
        "subsystem": str(merged.get("subsystem") or "general"),
        "component": str(merged.get("component") or ""),
        "source_target_ids": source_target_ids,
        "source_candidate_count": len(rows),
        "source_candidates": source_candidates,
        "providers": providers,
        "provider_count": int(merged["provider_count"]),
        "participating_providers": providers,
        "participating_provider_count": int(merged["participating_provider_count"]),
        "supporting_providers": supporting_providers,
        "support_provider_count": len(supporting_providers),
        "countering_providers": countering_providers,
        "counter_provider_count": len(countering_providers),
        "evidence_refs": evidence_refs,
        "support_evidence_refs": support_evidence_refs,
        "counter_evidence_refs": counter_evidence_refs,
        "all_referenced_evidence_refs": evidence_refs,
        "missing_evidence": missing,
        "caveats": caveats,
        "target_explanation": target_explanation,
        "support_evidence": support_evidence,
        "counter_evidence": counter_evidence,
        "rollup": rollup,
        "rollup_provider_ratio": rollup["rollup_provider_ratio"],
        "baseline_support_score": rollup["baseline_support_score"],
        "review_priority_score": float(merged.get("score_before") or 0.0),
        "consensus_class": _consensus_class(merged),
        "group_json": merged,
    }
    return merged, group


def _source_candidate_summaries(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    summaries: list[dict[str, Any]] = []
    for index, row in enumerate(rows, start=1):
        raw = row.get("raw") if isinstance(row.get("raw"), dict) else {}
        explanation = row.get("target_explanation") if isinstance(row.get("target_explanation"), dict) else {}
        providers = _unique_str(row.get("providers") or raw.get("providers") or [])
        supporting_providers = _unique_str(
            row.get("supporting_providers") or raw.get("supporting_providers") or []
        )
        countering_providers = _unique_str(
            row.get("countering_providers") or raw.get("countering_providers") or []
        )
        source_chunks = _unique_str(
            [
                row.get("source_chunk_id"),
                row.get("source_parent_chunk_id"),
                raw.get("source_chunk_id"),
                raw.get("source_parent_chunk_id"),
            ]
        )
        summaries.append(
            {
                "source_candidate_id": str(row.get("target_id") or row.get("group_id") or f"candidate-{index:03d}"),
                "provider_ids": providers,
                "supporting_provider_ids": supporting_providers,
                "countering_provider_ids": countering_providers,
                "canonical_target_type": str(
                    row.get("canonical_target_type")
                    or row.get("core_target_type")
                    or raw.get("core_target_type")
                    or "general_review"
                ),
                "subsystem": str(row.get("subsystem") or raw.get("subsystem") or "general"),
                "component": str(row.get("component") or raw.get("component") or ""),
                "evidence_refs": _unique_str(row.get("evidence_refs") or raw.get("evidence_refs") or [])[:20],
                "support_evidence_refs": _unique_str(
                    row.get("support_evidence_refs") or raw.get("support_evidence_refs") or []
                )[:20],
                "counter_evidence_refs": _unique_str(
                    row.get("counter_evidence_refs") or raw.get("counter_evidence_refs") or []
                )[:20],
                "source_chunk_ids": source_chunks,
                "claim": str(
                    row.get("suspected_issue")
                    or explanation.get("suspected_issue")
                    or row.get("impact_summary")
                    or row.get("title")
                    or raw.get("core_claim")
                    or raw.get("claim_text")
                    or ""
                )[:500],
            }
        )
    return summaries


def _merge_jsonish(values: Any) -> list[Any]:
    seen: set[str] = set()
    output: list[Any] = []
    for value in values:
        items = value if isinstance(value, list) else []
        for item in items:
            key = sha256_json(item) if isinstance(item, (dict, list)) else str(item)
            if key in seen:
                continue
            seen.add(key)
            output.append(item)
    return output


def _merge_target_explanations(rows: list[dict[str, Any]], top: dict[str, Any]) -> dict[str, Any]:
    explanation_rows = [_target_explanation_source(row) for row in rows]
    explanation_rows = [row for row in explanation_rows if row]
    if not explanation_rows:
        explanation_rows = [_target_explanation_source(top)]
    evidence_summary = _unique_str(
        item
        for row in explanation_rows
        for item in row.get("evidence_summary") or []
    )
    counter_summary = _unique_str(
        item
        for row in explanation_rows
        for item in row.get("counter_evidence_summary") or []
    )
    provider_explanations = _merge_jsonish(row.get("provider_explanations") for row in explanation_rows)
    return {
        "schema_version": "target_explanation.v1",
        "suspected_issue": _first_explanation_text(explanation_rows, "suspected_issue")
        or str(top.get("impact_summary") or top.get("title") or ""),
        "operational_mechanism": _first_explanation_text(explanation_rows, "operational_mechanism"),
        "why_it_matters": _first_explanation_text(explanation_rows, "why_it_matters"),
        "evidence_summary": evidence_summary,
        "counter_evidence_summary": counter_summary,
        "why_not_promoted": _first_explanation_text(explanation_rows, "why_not_promoted"),
        "next_validation_question": _first_explanation_text(explanation_rows, "next_validation_question"),
        "provider_explanations": provider_explanations,
    }


def _target_explanation_source(row: dict[str, Any]) -> dict[str, Any]:
    explanation = row.get("target_explanation") if isinstance(row.get("target_explanation"), dict) else {}
    return {
        "suspected_issue": str(row.get("suspected_issue") or explanation.get("suspected_issue") or ""),
        "operational_mechanism": str(row.get("operational_mechanism") or explanation.get("operational_mechanism") or ""),
        "why_it_matters": str(row.get("why_it_matters") or explanation.get("why_it_matters") or ""),
        "evidence_summary": _unique_str([*_string_items(row.get("evidence_summary")), *_string_items(explanation.get("evidence_summary"))]),
        "counter_evidence_summary": _unique_str(
            [
                *_string_items(row.get("counter_evidence_summary")),
                *_string_items(explanation.get("counter_evidence_summary")),
            ]
        ),
        "why_not_promoted": str(row.get("why_not_promoted") or explanation.get("why_not_promoted") or ""),
        "next_validation_question": str(row.get("next_validation_question") or explanation.get("next_validation_question") or ""),
        "provider_explanations": list(explanation.get("provider_explanations") or []),
    }


def _string_items(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value if str(item).strip()]
    text = str(value or "").strip()
    return [text] if text else []


def _first_explanation_text(rows: list[dict[str, Any]], key: str) -> str:
    for row in rows:
        text = str(row.get(key) or "").strip()
        if text:
            return text
    return ""


def _consensus_class(target: dict[str, Any]) -> str:
    provider_count = int(target.get("provider_count") or 0)
    if provider_count >= 2:
        return "multi_provider"
    if provider_count == 1:
        return "single_provider"
    if str(target.get("source") or "").startswith("context"):
        return "context_only"
    return "non_model"


def _rollup_profile(rows: list[dict[str, Any]], *, evidence_refs: list[str]) -> dict[str, Any]:
    provider_candidate_memberships: list[str] = []
    supporting_provider_memberships: list[str] = []
    countering_provider_memberships: list[str] = []
    for row in rows:
        providers = _unique_str(row.get("providers") or [])
        if providers:
            provider_candidate_memberships.extend(providers)
        supporting_providers = _unique_str(row.get("supporting_providers") or [])
        if supporting_providers:
            supporting_provider_memberships.extend(supporting_providers)
        countering_providers = _unique_str(row.get("countering_providers") or [])
        if countering_providers:
            countering_provider_memberships.extend(countering_providers)
    provider_candidate_membership_counts = Counter(provider_candidate_memberships)
    supporting_provider_counts = Counter(supporting_provider_memberships)
    countering_provider_counts = Counter(countering_provider_memberships)
    source_candidate_type_counts = Counter(
        _normalize_token(str(row.get("canonical_target_type") or row.get("core_target_type") or "general_review"))
        for row in rows
    )
    support_evidence_refs = _unique_str(
        ref for row in rows for ref in row.get("support_evidence_refs") or []
    )
    family_counts = Counter(_evidence_family(ref) for ref in support_evidence_refs)
    family_counts.pop("", None)
    source_candidate_count = len(rows)
    support_source_candidate_count = sum(
        1 for row in rows if _unique_str(row.get("supporting_providers") or [])
    )
    independent_provider_count = len(provider_candidate_membership_counts)
    independent_support_provider_count = len(supporting_provider_counts)
    repeated_provider_memberships = sum(
        max(0, count - 1) for count in provider_candidate_membership_counts.values()
    )
    repeated_support_memberships = sum(
        max(0, count - 1) for count in supporting_provider_counts.values()
    )
    provider_bonus = 0.0
    if independent_support_provider_count >= 3:
        provider_bonus = 0.10
    elif independent_support_provider_count == 2:
        provider_bonus = 0.06
    evidence_bonus = min(
        0.06,
        max(0, len(family_counts) - 1) * 0.03
        + max(0, len(support_evidence_refs) - 1) * 0.005,
    )
    type_divergence_penalty = min(0.06, max(0, len(source_candidate_type_counts) - 1) * 0.03)
    repeated_independent_bonus = min(0.04, max(0, support_source_candidate_count - 1) * 0.01)
    same_provider_duplicate_bonus = min(0.02, repeated_support_memberships * 0.005)
    priority_bonus = max(
        0.0,
        min(
            0.18,
            provider_bonus
            + evidence_bonus
            + repeated_independent_bonus
            + same_provider_duplicate_bonus
            - type_divergence_penalty,
        ),
    )
    baseline_support_score = 0.0
    if independent_support_provider_count:
        baseline_support_score = min(
            1.0,
            0.25
            + min(independent_support_provider_count, 3) * 0.15
            + min(len(family_counts), 3) * 0.10
            + min(support_source_candidate_count, 4) * 0.05
            + min(len(support_evidence_refs), 6) * 0.025,
        )
    rollup_provider_ratio = 0.0
    if independent_support_provider_count >= 2 and support_source_candidate_count >= 2:
        rollup_provider_ratio = min(
            1.0,
            independent_support_provider_count
            / max(support_source_candidate_count, independent_support_provider_count, 1),
        )
    return {
        "source_candidate_count": source_candidate_count,
        "support_source_candidate_count": support_source_candidate_count,
        "independent_provider_count": independent_provider_count,
        "independent_support_provider_count": independent_support_provider_count,
        "provider_candidate_membership_counts": dict(sorted(provider_candidate_membership_counts.items())),
        "supporting_provider_counts": dict(sorted(supporting_provider_counts.items())),
        "countering_provider_counts": dict(sorted(countering_provider_counts.items())),
        "provider_vote_counts": dict(sorted(provider_candidate_membership_counts.items())),
        "provider_vote_counts_deprecated_alias_for": "provider_candidate_membership_counts",
        "same_provider_duplicate_count": repeated_provider_memberships,
        "evidence_ref_count": len(support_evidence_refs),
        "all_referenced_evidence_ref_count": len(evidence_refs),
        "evidence_family_count": len(family_counts),
        "evidence_family_counts": dict(sorted(family_counts.items())),
        "source_candidate_type_counts": dict(sorted(source_candidate_type_counts.items())),
        "target_type_votes": dict(sorted(source_candidate_type_counts.items())),
        "target_type_votes_deprecated_alias_for": "source_candidate_type_counts",
        "distinct_target_type_count": len(source_candidate_type_counts),
        "provider_convergence_bonus": round(provider_bonus, 4),
        "evidence_diversity_bonus": round(evidence_bonus, 4),
        "target_type_divergence": len(source_candidate_type_counts) > 1,
        "target_type_divergence_penalty": round(type_divergence_penalty, 4),
        "repeated_independent_claim_bonus": round(repeated_independent_bonus, 4),
        "same_provider_duplicate_bonus": round(same_provider_duplicate_bonus, 4),
        "priority_bonus": round(priority_bonus, 4),
        "rollup_provider_ratio": round(rollup_provider_ratio, 4),
        "rollup_provider_ratio_definition": (
            "independent supporting providers / supporting source candidates; fewer than two supporting sources is 0.0"
        ),
        "baseline_support_score": round(baseline_support_score, 4),
    }


def _evidence_family(ref: str) -> str:
    value = str(ref or "").strip().upper()
    if not value:
        return ""
    if "-" in value:
        return value.split("-", 1)[0]
    match = re.match(r"[A-Z]+", value)
    return match.group(0) if match else value


def _normalize_token(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "_", value.casefold()).strip("_")
    return normalized or "general"


def _unique_str(values: Any) -> list[str]:
    seen: set[str] = set()
    output: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        output.append(text)
    return output
