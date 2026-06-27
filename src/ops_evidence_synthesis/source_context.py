from __future__ import annotations

import fnmatch
import json
import os
import re
import shlex
import subprocess
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable

from ops_evidence_synthesis.canonical import canonical_json, pretty_json, sha256_json, sha256_text
from ops_evidence_synthesis.evidence_rules import ai_evidence_rules, source_context_rules
from ops_evidence_synthesis.local_first import (
    CANONICALIZATION_VERSION,
    RedactionCounter,
    redact_text,
    scan_sanitized_text,
)
from ops_evidence_synthesis.timeutils import utc_now


SOURCE_CONTEXT_SCHEMA_VERSION = "source_context_bundle.v1"
SOURCE_CONTEXT_BUNDLE_TYPE = "sanitized_source_context_bundle"
SOURCE_ANALYSIS_SCHEMA_VERSION = "source_analysis_bundle.v1"
SOURCE_ANALYSIS_BUNDLE_TYPE = "sanitized_source_analysis_bundle"
RAW_SOURCE_POLICY = "not_uploaded"
RAW_ENV_POLICY = "not_uploaded"
MAX_EXCERPT_BYTES = 4096
MAX_TOTAL_EXCERPT_BYTES = 200000

EXCLUDED_NAMES = {
    ".git",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    "dist",
    "build",
    "vendor",
    ".cache",
    ".pytest_cache",
    "credentials",
    "secrets",
    "private",
}
EXCLUDED_PATTERNS = (
    "*.sqlite",
    "*.sqlite3",
    "*.db",
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "*.zip",
    "*.tar",
    "*.mp4",
    "*.wav",
)
ENTRYPOINT_NAMES = {
    "main.py",
    "app.py",
    "server.py",
    "worker.py",
    "scheduler.py",
    "cli.py",
    "manage.py",
    "index.js",
    "server.js",
}
DEPENDENCY_MANIFEST_NAMES = {
    "pyproject.toml",
    "requirements.txt",
    "package.json",
    "Dockerfile",
    "docker-compose.yml",
    "docker-compose.yaml",
    "compose.yml",
    "compose.yaml",
    "Makefile",
}
CONFIG_NAMES = {
    "README.md",
    "README.rst",
    "README.txt",
    "pyproject.toml",
    "requirements.txt",
    "package.json",
    "Dockerfile",
    "docker-compose.yml",
    "docker-compose.yaml",
    "compose.yml",
    "compose.yaml",
    "Makefile",
}
TEXT_SUFFIXES = {
    ".py",
    ".js",
    ".ts",
    ".json",
    ".yaml",
    ".yml",
    ".toml",
    ".txt",
    ".md",
    ".service",
    ".ini",
    ".cfg",
    ".conf",
    ".sh",
    ".env",
    ".example",
}
LANGUAGE_BY_SUFFIX = {
    ".py": "python",
    ".js": "javascript",
    ".ts": "typescript",
    ".json": "json",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".toml": "toml",
    ".md": "markdown",
    ".sh": "shell",
    ".service": "systemd",
}
SECRET_ENV_KEY_RE = re.compile(
    r"(TOKEN|SECRET|PASSWORD|PASSWD|KEY|PRIVATE|CREDENTIAL|COOKIE|AUTH|API[_-]?KEY|WEBHOOK|SESSION)",
    re.IGNORECASE,
)
SOURCE_SECRET_KEY_RE = re.compile(
    r"\b(TOKEN|SECRET|PASSWORD|PASSWD|PRIVATE[_-]?KEY|CREDENTIAL|COOKIE|AUTHORIZATION|API[_-]?KEY|SESSION)\b",
    re.IGNORECASE,
)
METRIC_RE = re.compile(
    r"\b[A-Za-z_][A-Za-z0-9_]*(?:count|total|seconds|latency|duration|freshness|heartbeat|energy|bytes|errors?|requests?)\b",
    re.IGNORECASE,
)
LOGGER_RE = re.compile(r"\b(?:logger|logging\.getLogger|component|module)\b[^\n]{0,80}", re.IGNORECASE)
IMPORT_RE = re.compile(r"^\s*(?:from\s+[A-Za-z0-9_.]+\s+import\s+|import\s+)")
DEFINITION_RE = re.compile(r"^\s*(?:def|class|async\s+def)\s+[A-Za-z_][A-Za-z0-9_]*")
SYSTEMD_UNIT_RE = re.compile(r"\b[A-Za-z0-9_.@:+\-]+\.service\b")
UNSAFE_LABEL_REPLACEMENTS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?i)\bAuthorization\s*:"), "<REDACTED_SECRET>:"),
    (re.compile(r"(?i)\bBearer\s+"), "<REDACTED_SECRET> "),
    (re.compile(r"(?i)\bBasic\s+"), "<REDACTED_SECRET> "),
    (re.compile(r"(?i)\bCookie\s*:"), "<REDACTED_SECRET>:"),
    (re.compile(r"(?i)\bSet-Cookie\s*:"), "<REDACTED_SECRET>:"),
    (re.compile(r"(?i)\bpassword\s*="), "<REDACTED_SECRET>="),
    (re.compile(r"(?i)\bpasswd\s*="), "<REDACTED_SECRET>="),
    (re.compile(r"(?i)\bsecret\s*="), "<REDACTED_SECRET>="),
    (re.compile(r"(?i)\bprivate_key\b"), "<REDACTED_SECRET>"),
    (re.compile(r"(?i)\bapi_key\b"), "<REDACTED_SECRET>"),
    (re.compile(r"(?i)\baccess_token\b"), "<REDACTED_SECRET>"),
    (re.compile(r"(?i)\brefresh_token\b"), "<REDACTED_SECRET>"),
    (re.compile(r"(?i)\bsession_id\b"), "<REDACTED_SECRET>"),
    (re.compile(r"(?i)\bsk-[A-Za-z0-9_\-]{8,}"), "<REDACTED_SECRET>"),
    (re.compile(r"\bAIza[0-9A-Za-z_\-]{10,}"), "<REDACTED_SECRET>"),
    (re.compile(r"\bya29\.[0-9A-Za-z_\-./]+"), "<REDACTED_SECRET>"),
    (re.compile(r"-----BEGIN PRIVATE KEY-----"), "<REDACTED_SECRET>"),
    (re.compile(r"(?i)\b[A-Za-z0-9_.-]+\.internal\b"), "<URL_HASH:000000000000>"),
)


def sanitize_source(
    project_root: str | Path,
    *,
    service: str,
    environment: str,
    output_dir: str | Path,
) -> dict[str, Any]:
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    bundle = build_source_context_bundle(project_root, service=service, environment=environment)
    bundle_path = output / "source_context_bundle.json"
    report_path = output / "source_context_report.md"
    redaction_path = output / "redaction_report.json"
    bundle_path.write_text(pretty_json(bundle) + "\n", encoding="utf-8")
    report_path.write_text(render_source_context_report(bundle), encoding="utf-8")
    redaction_path.write_text(pretty_json(_source_redaction_report(bundle)) + "\n", encoding="utf-8")
    return {
        "source_context_bundle": str(bundle_path),
        "source_context_report": str(report_path),
        "redaction_report": str(redaction_path),
        "source_context_sha256": bundle["source_context_sha256"],
        "source_item_count": len(bundle.get("source_items") or []),
        "config_item_count": len(bundle.get("config_items") or []),
    }


def build_source_context_bundle(
    project_root: str | Path,
    *,
    service: str,
    environment: str,
) -> dict[str, Any]:
    root = Path(project_root)
    report = RedactionCounter()
    files = list(_iter_project_files(root))
    detected_languages = sorted(
        {language for path in files if (language := _language_for_path(path))}
    )
    source_items: list[dict[str, Any]] = []
    config_items: list[dict[str, Any]] = []
    env_key_summaries: list[dict[str, Any]] = []
    systemd_units: list[dict[str, Any]] = []
    dependency_manifests: list[dict[str, Any]] = []
    total_excerpt_bytes = 0

    for path in files:
        rel = _sanitize(_rel(path, root), report)
        if _is_env_file(path):
            env_key_summaries.extend(_env_key_summaries(path, root, report))
            config_items.append(_config_item(path, root, report, config_type="env_keys_only"))
            continue
        if path.name.endswith(".service"):
            unit = _systemd_unit_summary(path, root, report)
            systemd_units.append(unit)
            config_items.append(_config_item(path, root, report, config_type="systemd_unit", summary=unit))
            continue
        if _is_dependency_manifest(path):
            manifest = _dependency_manifest_summary(path, root, report)
            dependency_manifests.append(manifest)
            config_items.append(_config_item(path, root, report, config_type="dependency_manifest", summary=manifest))
            continue
        if path.name in CONFIG_NAMES or _looks_k8s_manifest(path):
            config_items.append(_config_item(path, root, report, config_type=_config_type(path)))
        if _is_source_file(path):
            item, excerpt_bytes = _source_item(path, root, report, remaining_bytes=MAX_TOTAL_EXCERPT_BYTES - total_excerpt_bytes)
            source_items.append(item)
            total_excerpt_bytes += excerpt_bytes

    project_summary = {
        "detected_languages": detected_languages,
        "detected_project_type": _detected_project_type(files, systemd_units),
        "entrypoint_candidates": sorted(_entrypoint_candidates(files, root, report)),
        "service_unit_candidates": [row["unit_name"] for row in systemd_units],
        "dependency_manifest_candidates": [row["relative_path"] for row in dependency_manifests],
        "logging_config_candidates": sorted(_logging_config_candidates(source_items, config_items)),
        "metric_name_candidates": sorted(_metric_candidates(source_items)),
    }
    bundle: dict[str, Any] = {
        "schema_version": SOURCE_CONTEXT_SCHEMA_VERSION,
        "bundle_type": SOURCE_CONTEXT_BUNDLE_TYPE,
        "raw_source_policy": RAW_SOURCE_POLICY,
        "raw_env_policy": RAW_ENV_POLICY,
        "canonicalization_version": CANONICALIZATION_VERSION,
        "source_context_sha256": "",
        "source": {
            "project_name": _sanitize(root.name or "project", report),
            "project_root_uploaded": False,
            "service": _sanitize(service, report),
            "environment": _sanitize(environment, report),
        },
        "sanitization_policy": {
            "raw_source_uploaded": False,
            "raw_env_values_uploaded": False,
            "raw_credentials_allowed": False,
            "raw_grep_output_uploaded": False,
            "max_excerpt_bytes": MAX_EXCERPT_BYTES,
            "max_total_excerpt_bytes": MAX_TOTAL_EXCERPT_BYTES,
            "sanitize_before_upload": True,
            "verify_sanitized_required": True,
        },
        "project_summary": project_summary,
        "source_items": source_items,
        "config_items": config_items,
        "env_key_summaries": env_key_summaries,
        "systemd_units": systemd_units,
        "dependency_manifests": dependency_manifests,
        "version_context": _version_context(root, files, systemd_units, report),
        "redaction_summary": report.summary(),
        "prompt_rules": source_context_rules(),
        "display_summary": {
            "title": "Sanitized Source Context Bundle",
            "subtitle": "Raw source code and raw env values were not uploaded. This bundle is context, not incident evidence.",
            "source_item_count": len(source_items),
            "config_item_count": len(config_items),
            "env_key_count": len(env_key_summaries),
            "systemd_unit_count": len(systemd_units),
            "primary_badges": [
                f"raw_source_policy:{RAW_SOURCE_POLICY}",
                f"raw_env_policy:{RAW_ENV_POLICY}",
                "context_not_incident_evidence",
            ],
        },
    }
    bundle["source_context_sha256"] = sha256_json(source_context_hash_payload(bundle))
    return bundle


def analyze_source_context(
    source_context_path: str | Path,
    *,
    provider: str,
    output_dir: str | Path,
) -> dict[str, Any]:
    if provider != "local":
        raise ValueError("only --provider local is implemented")
    source_context = _load_json(source_context_path)
    validation = validate_source_context_bundle_for_upload(source_context)
    if not validation["passed"]:
        raise ValueError("source_context_bundle validation failed")
    analysis = build_source_analysis_bundle(source_context)
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    bundle_path = output / "source_analysis_bundle.json"
    report_path = output / "source_analysis_report.md"
    bundle_path.write_text(pretty_json(analysis) + "\n", encoding="utf-8")
    report_path.write_text(render_source_analysis_report(analysis), encoding="utf-8")
    return {
        "source_analysis_bundle": str(bundle_path),
        "source_analysis_report": str(report_path),
        "analysis_sha256": analysis["analysis_sha256"],
        "component_candidate_count": len(analysis.get("component_candidates") or []),
        "metric_semantics_candidate_count": len(analysis.get("metric_semantics_candidates") or []),
    }


def build_source_analysis_bundle(source_context: dict[str, Any]) -> dict[str, Any]:
    source_context_sha = str(source_context.get("source_context_sha256") or "")
    component_candidates = _analysis_component_candidates(source_context)
    metric_candidates = _analysis_metric_semantics_candidates(source_context)
    logger_candidates = _analysis_logger_mapping_candidates(source_context)
    instrumentation_candidates = _analysis_instrumentation_candidates(
        source_context,
        metric_candidates=metric_candidates,
        logger_candidates=logger_candidates,
    )
    collector_candidates = _analysis_collector_mapping_candidates(source_context, component_candidates, metric_candidates)
    analysis: dict[str, Any] = {
        "schema_version": SOURCE_ANALYSIS_SCHEMA_VERSION,
        "bundle_type": SOURCE_ANALYSIS_BUNDLE_TYPE,
        "source_context_sha256": source_context_sha,
        "raw_source_policy": RAW_SOURCE_POLICY,
        "raw_env_policy": RAW_ENV_POLICY,
        "analysis_sha256": "",
        "component_candidates": component_candidates,
        "metric_semantics_candidates": metric_candidates,
        "logger_mapping_candidates": logger_candidates,
        "instrumentation_candidates": instrumentation_candidates,
        "collector_mapping_candidates": collector_candidates,
        "profile_mapping_hints": _analysis_profile_mapping_hints(source_context, component_candidates, metric_candidates),
        "assumptions": [
            "Source Analysis Bundle is context, not incident evidence.",
            "Runtime occurrence and user impact are not inferred from code/config alone.",
            "Metric semantics and component mappings require human review.",
        ],
        "required_human_decisions": [
            "Approve or edit component roles before using them in an explicit profile.",
            "Approve metric zero/increase/decrease semantics.",
            "Confirm whether this source context matches the deployed version during the incident window.",
            "Confirm read-only collector mappings before collection.",
        ],
        "prompt_rules": source_context_rules() + ai_evidence_rules(),
        "display_summary": {
            "title": "Sanitized Source Analysis Bundle",
            "subtitle": "Rule-based source mapping candidates. Context only; not incident evidence.",
            "component_candidate_count": len(component_candidates),
            "metric_semantics_candidate_count": len(metric_candidates),
            "collector_mapping_candidate_count": len(collector_candidates),
            "primary_badges": [
                "provider:local",
                "human_review_required:true",
                "context_not_incident_evidence",
            ],
        },
    }
    analysis["analysis_sha256"] = sha256_json(source_analysis_hash_payload(analysis))
    return analysis


def validate_source_context_bundle_for_upload(bundle: dict[str, Any]) -> dict[str, Any]:
    errors: list[dict[str, Any]] = []
    if not isinstance(bundle, dict):
        return _validation_result(False, [{"type": "invalid_payload", "field": "source_context_bundle"}], [], "", "")
    required = {
        "schema_version": SOURCE_CONTEXT_SCHEMA_VERSION,
        "bundle_type": SOURCE_CONTEXT_BUNDLE_TYPE,
        "raw_source_policy": RAW_SOURCE_POLICY,
        "raw_env_policy": RAW_ENV_POLICY,
    }
    for field, expected in required.items():
        if bundle.get(field) != expected:
            errors.append({"type": "contract_mismatch", "field": field})
    policy = bundle.get("sanitization_policy") if isinstance(bundle.get("sanitization_policy"), dict) else {}
    if policy.get("raw_source_uploaded") is not False:
        errors.append({"type": "contract_mismatch", "field": "sanitization_policy.raw_source_uploaded"})
    if policy.get("raw_env_values_uploaded") is not False:
        errors.append({"type": "contract_mismatch", "field": "sanitization_policy.raw_env_values_uploaded"})
    if policy.get("raw_credentials_allowed") is not False:
        errors.append({"type": "contract_mismatch", "field": "sanitization_policy.raw_credentials_allowed"})
    for field in (
        "source",
        "sanitization_policy",
        "project_summary",
        "source_items",
        "config_items",
        "env_key_summaries",
        "systemd_units",
        "dependency_manifests",
        "version_context",
        "prompt_rules",
    ):
        if field not in bundle:
            errors.append({"type": "missing_field", "field": field})
    expected_sha = sha256_json(source_context_hash_payload(bundle))
    actual_sha = str(bundle.get("source_context_sha256") or "")
    if expected_sha != actual_sha:
        errors.append({"type": "source_context_sha256_mismatch", "field": "source_context_sha256"})
    scan = scan_sanitized_text("source_context_bundle.json", canonical_json(bundle))
    findings = list(scan["findings"])
    if findings:
        errors.append({"type": "unsafe_content", "field": "source_context_bundle"})
    return _validation_result(not errors, errors, findings, expected_sha, actual_sha)


def validate_source_analysis_bundle_for_upload(bundle: dict[str, Any]) -> dict[str, Any]:
    errors: list[dict[str, Any]] = []
    if not isinstance(bundle, dict):
        return _validation_result(False, [{"type": "invalid_payload", "field": "source_analysis_bundle"}], [], "", "")
    required = {
        "schema_version": SOURCE_ANALYSIS_SCHEMA_VERSION,
        "bundle_type": SOURCE_ANALYSIS_BUNDLE_TYPE,
        "raw_source_policy": RAW_SOURCE_POLICY,
        "raw_env_policy": RAW_ENV_POLICY,
    }
    for field, expected in required.items():
        if bundle.get(field) != expected:
            errors.append({"type": "contract_mismatch", "field": field})
    for field in (
        "source_context_sha256",
        "component_candidates",
        "metric_semantics_candidates",
        "logger_mapping_candidates",
        "instrumentation_candidates",
        "collector_mapping_candidates",
        "profile_mapping_hints",
        "prompt_rules",
    ):
        if field not in bundle:
            errors.append({"type": "missing_field", "field": field})
    expected_sha = sha256_json(source_analysis_hash_payload(bundle))
    actual_sha = str(bundle.get("analysis_sha256") or "")
    if expected_sha != actual_sha:
        errors.append({"type": "analysis_sha256_mismatch", "field": "analysis_sha256"})
    scan = scan_sanitized_text("source_analysis_bundle.json", canonical_json(bundle))
    findings = list(scan["findings"])
    if findings:
        errors.append({"type": "unsafe_content", "field": "source_analysis_bundle"})
    return _validation_result(not errors, errors, findings, expected_sha, actual_sha)


def source_context_hash_payload(bundle: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in bundle.items() if key not in {"source_context_sha256"}}


def source_analysis_hash_payload(bundle: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in bundle.items() if key not in {"analysis_sha256"}}


def source_context_to_project_entities(bundle: dict[str, Any]) -> list[dict[str, Any]]:
    entities: list[dict[str, Any]] = []
    summary = bundle.get("project_summary") if isinstance(bundle.get("project_summary"), dict) else {}
    entities.append(
        {
            "entity_type": "file_tree_summary",
            "name": "file_tree",
            "relative_path": "",
            "attributes": {
                "source_context_sha256": bundle.get("source_context_sha256") or "",
                "detected_languages": list(summary.get("detected_languages") or []),
                "detected_project_type": summary.get("detected_project_type") or "generic",
            },
        }
    )
    for item in bundle.get("source_items") or []:
        if not isinstance(item, dict):
            continue
        rel = str(item.get("relative_path") or "")
        entity_type = "script_path" if item.get("entrypoint_candidate") else "file_path"
        entities.append(
            {
                "entity_type": entity_type,
                "name": rel,
                "relative_path": rel,
                "attributes": {
                    "language": item.get("language") or "",
                    "entrypoint_candidate": bool(item.get("entrypoint_candidate")),
                    "metric_names": list(item.get("metric_name_candidates") or []),
                    "logger_candidates": list(item.get("logger_candidates") or []),
                    "excerpt_uploaded": bool(item.get("excerpt_sanitized")),
                    "excerpt_sanitized": list(item.get("excerpt_sanitized") or [])[:3],
                    "source_context_sha256": bundle.get("source_context_sha256") or "",
                },
            }
        )
        for metric in item.get("metric_name_candidates") or []:
            entities.append(
                {
                    "entity_type": "metric_name",
                    "name": str(metric),
                    "relative_path": rel,
                    "attributes": {"defined_in": rel, "source_context_sha256": bundle.get("source_context_sha256") or ""},
                }
            )
    for item in bundle.get("config_items") or []:
        if isinstance(item, dict):
            rel = str(item.get("relative_path") or "")
            entities.append(
                {
                    "entity_type": "config_file",
                    "name": rel,
                    "relative_path": rel,
                    "attributes": {
                        "config_type": item.get("config_type") or "",
                        "source_context_sha256": bundle.get("source_context_sha256") or "",
                    },
                }
            )
    for row in bundle.get("dependency_manifests") or []:
        if isinstance(row, dict):
            rel = str(row.get("relative_path") or row.get("name") or "")
            entities.append(
                {
                    "entity_type": "dependency_manifest",
                    "name": rel,
                    "relative_path": rel,
                    "attributes": dict(row),
                }
            )
    for row in bundle.get("systemd_units") or []:
        if not isinstance(row, dict):
            continue
        entities.append(
            {
                "entity_type": "systemd_unit",
                "name": str(row.get("unit_name") or ""),
                "relative_path": str(row.get("relative_path") or ""),
                "attributes": {
                    "unit_file_path": row.get("unit_file_path_template") or row.get("relative_path") or "",
                    "unit_name": row.get("unit_name") or "",
                    "description": row.get("description") or "",
                    "exec_start_template": row.get("exec_start_template") or "",
                    "working_directory_template": row.get("working_directory_template") or "",
                    "environment_file_template": row.get("environment_file_template") or "",
                    "restart": row.get("restart") or "",
                    "user": row.get("user") or "",
                    "referenced_programs": list(row.get("referenced_programs") or []),
                    "source_context_sha256": bundle.get("source_context_sha256") or "",
                },
            }
        )
    for index, row in enumerate(entities, start=1):
        row["project_entity_id"] = f"SRC-{index:03d}"
        row["source"] = "sanitized_source_context"
    return entities


def load_source_context_bundle(path: str | Path | None) -> dict[str, Any]:
    return _load_json(path)


def load_source_analysis_bundle(path: str | Path | None) -> dict[str, Any]:
    return _load_json(path)


def source_context_model_context(bundle: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(bundle, dict):
        return {}
    return {
        "bundle_type": bundle.get("bundle_type"),
        "source_context_sha256": bundle.get("source_context_sha256"),
        "context_is_not_incident_evidence": True,
        "raw_source_policy": bundle.get("raw_source_policy"),
        "raw_env_policy": bundle.get("raw_env_policy"),
        "project_summary": bundle.get("project_summary") or {},
        "source_items": list(bundle.get("source_items") or [])[:50],
        "config_items": list(bundle.get("config_items") or [])[:50],
        "env_key_summaries": list(bundle.get("env_key_summaries") or [])[:50],
        "systemd_units": list(bundle.get("systemd_units") or [])[:20],
        "version_context": bundle.get("version_context") or {},
        "prompt_rules": bundle.get("prompt_rules") or source_context_rules(),
    }


def source_analysis_model_context(bundle: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(bundle, dict):
        return {}
    return {
        "bundle_type": bundle.get("bundle_type"),
        "analysis_sha256": bundle.get("analysis_sha256"),
        "source_context_sha256": bundle.get("source_context_sha256"),
        "context_is_not_incident_evidence": True,
        "raw_source_policy": bundle.get("raw_source_policy"),
        "raw_env_policy": bundle.get("raw_env_policy"),
        "component_candidates": list(bundle.get("component_candidates") or [])[:50],
        "metric_semantics_candidates": list(bundle.get("metric_semantics_candidates") or [])[:50],
        "logger_mapping_candidates": list(bundle.get("logger_mapping_candidates") or [])[:50],
        "instrumentation_candidates": list(bundle.get("instrumentation_candidates") or [])[:50],
        "collector_mapping_candidates": list(bundle.get("collector_mapping_candidates") or [])[:50],
        "profile_mapping_hints": list(bundle.get("profile_mapping_hints") or [])[:50],
        "prompt_rules": bundle.get("prompt_rules") or source_context_rules(),
    }


def render_source_context_report(bundle: dict[str, Any]) -> str:
    summary = bundle.get("project_summary") if isinstance(bundle.get("project_summary"), dict) else {}
    source = bundle.get("source") if isinstance(bundle.get("source"), dict) else {}
    version = bundle.get("version_context") if isinstance(bundle.get("version_context"), dict) else {}
    lines = [
        "# Sanitized Source Context Bundle",
        "",
        "Ops Evidence Synthesis does not upload raw source code or raw env values.",
        "Source context is context, not incident evidence.",
        "Runtime claims must still cite Evidence Items with evidence_id.",
        "",
        f"- service: {source.get('service') or ''}",
        f"- environment: {source.get('environment') or ''}",
        f"- source_context_sha256: {bundle.get('source_context_sha256') or ''}",
        f"- detected_project_type: {summary.get('detected_project_type') or 'generic'}",
        f"- detected_languages: {', '.join(summary.get('detected_languages') or [])}",
        f"- source_items: {len(bundle.get('source_items') or [])}",
        f"- config_items: {len(bundle.get('config_items') or [])}",
        f"- env_key_summaries: {len(bundle.get('env_key_summaries') or [])}",
        f"- systemd_units: {len(bundle.get('systemd_units') or [])}",
        f"- deployed_version_confirmed: {str(version.get('deployed_version_confirmed') is True).lower()}",
        f"- caveat: {version.get('caveat') or ''}",
        "",
        "## Entrypoints",
    ]
    for item in summary.get("entrypoint_candidates") or []:
        lines.append(f"- {item}")
    lines.append("")
    lines.append("## Metrics")
    for item in summary.get("metric_name_candidates") or []:
        lines.append(f"- {item}")
    lines.append("")
    return "\n".join(lines) + "\n"


def render_source_analysis_report(bundle: dict[str, Any]) -> str:
    lines = [
        "# Sanitized Source Analysis Bundle",
        "",
        "Source Analysis Bundle is context, not incident evidence.",
        "Metric semantics, component mappings, logger mappings, and collector mappings require human review.",
        "Code/config excerpts do not prove runtime occurrence or user impact by themselves.",
        "Support claims about runtime behavior must still cite Evidence Items with evidence_id.",
        "",
        f"- source_context_sha256: {bundle.get('source_context_sha256') or ''}",
        f"- analysis_sha256: {bundle.get('analysis_sha256') or ''}",
        f"- component_candidates: {len(bundle.get('component_candidates') or [])}",
        f"- metric_semantics_candidates: {len(bundle.get('metric_semantics_candidates') or [])}",
        f"- collector_mapping_candidates: {len(bundle.get('collector_mapping_candidates') or [])}",
        "",
    ]
    for row in (bundle.get("component_candidates") or [])[:10]:
        if isinstance(row, dict):
            lines.append(f"- component: {row.get('name') or ''} ({row.get('suggested_subsystem') or 'general'})")
    return "\n".join(lines) + "\n"


def _iter_project_files(root: Path) -> Iterable[Path]:
    if not root.exists():
        return []
    files: list[Path] = []
    for current_text, dirs, filenames in os.walk(root):
        current = Path(current_text)
        try:
            rel_parts = current.relative_to(root).parts
        except ValueError:
            rel_parts = ()
        dirs[:] = [
            name
            for name in sorted(dirs)
            if not _excluded_path(Path(*rel_parts, name) if rel_parts else Path(name))
        ]
        for filename in sorted(filenames):
            path = current / filename
            try:
                rel = path.relative_to(root)
            except ValueError:
                rel = Path(filename)
            if _excluded_path(rel):
                continue
            if not _looks_text(path) and not _is_dependency_manifest(path):
                continue
            files.append(path)
    return sorted(files, key=lambda item: str(item.relative_to(root)))


def _excluded_path(rel: Path) -> bool:
    parts = [part.casefold() for part in rel.parts]
    if any(part in EXCLUDED_NAMES for part in parts):
        return True
    text = str(rel).replace("\\", "/")
    name = rel.name
    return any(fnmatch.fnmatch(text, pattern) or fnmatch.fnmatch(name, pattern) for pattern in EXCLUDED_PATTERNS)


def _source_item(path: Path, root: Path, report: RedactionCounter, *, remaining_bytes: int) -> tuple[dict[str, Any], int]:
    rel = _sanitize(_rel(path, root), report)
    text = _read_text(path)
    excerpts, excerpt_bytes, excluded = _sanitized_excerpts(path, text, report, remaining_bytes=remaining_bytes)
    metrics = sorted({match for match in METRIC_RE.findall(text) if len(match) <= 120})
    loggers = sorted(_logger_candidates(text, report))
    item = {
        "item_id": f"SRC-{sha256_text(rel)[:12]}",
        "relative_path": rel,
        "language": _language_for_path(path),
        "entrypoint_candidate": path.name in ENTRYPOINT_NAMES,
        "size_bytes": _safe_size(path),
        "line_count": len(text.splitlines()),
        "excerpt_sanitized": excerpts,
        "excerpt_policy": {
            "raw_source_uploaded": False,
            "full_source_uploaded": False,
            "max_excerpt_bytes": MAX_EXCERPT_BYTES,
            "secret_like_lines_excluded": excluded,
        },
        "metric_name_candidates": [_sanitize(item, report) for item in metrics[:50]],
        "logger_candidates": loggers[:50],
        "systemd_unit_mentions": sorted({match for match in SYSTEMD_UNIT_RE.findall(text)})[:20],
    }
    return item, excerpt_bytes


def _sanitized_excerpts(
    path: Path,
    text: str,
    report: RedactionCounter,
    *,
    remaining_bytes: int,
) -> tuple[list[str], int, int]:
    if remaining_bytes <= 0:
        return [], 0, 0
    selected: list[str] = []
    excluded = 0
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        include = bool(
            DEFINITION_RE.search(line)
            or IMPORT_RE.search(line)
            or METRIC_RE.search(line)
            or LOGGER_RE.search(line)
            or "ExecStart" in line
        )
        if not include:
            continue
        if SOURCE_SECRET_KEY_RE.search(line):
            excluded += 1
            continue
        sanitized = _sanitize(stripped, report)[:MAX_EXCERPT_BYTES]
        scan = scan_sanitized_text(path.name, sanitized)
        if scan["findings"]:
            excluded += 1
            continue
        selected.append(sanitized)
        if len(selected) >= 6:
            break
    total = 0
    capped: list[str] = []
    for item in selected:
        size = len(item.encode("utf-8"))
        if total + size > min(MAX_EXCERPT_BYTES, remaining_bytes):
            break
        capped.append(item)
        total += size
    return capped, total, excluded


def _config_item(
    path: Path,
    root: Path,
    report: RedactionCounter,
    *,
    config_type: str,
    summary: dict[str, Any] | None = None,
) -> dict[str, Any]:
    rel = _sanitize(_rel(path, root), report)
    payload = {
        "config_id": f"CFG-{sha256_text(rel)[:12]}",
        "relative_path": rel,
        "config_type": config_type,
        "raw_values_uploaded": False,
        "summary": summary if summary is not None else _generic_config_summary(path, report),
    }
    return payload


def _generic_config_summary(path: Path, report: RedactionCounter) -> dict[str, Any]:
    text = _read_text(path)[:65536]
    if path.name.casefold().startswith("readme"):
        headings = [_sanitize(line.strip("# ").strip(), report) for line in text.splitlines() if line.startswith("#")][:10]
        return {"summary_type": "readme_headings", "headings": headings}
    if _looks_k8s_manifest(path):
        return {"summary_type": "k8s_manifest", "kind_candidates": _yaml_kind_candidates(text, report)}
    if path.name == "Dockerfile":
        return {"summary_type": "dockerfile", "instruction_candidates": _dockerfile_instructions(text, report)}
    if path.name in {"docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml"}:
        return {"summary_type": "compose", "service_candidates": _compose_services(text, report)}
    if path.name == "Makefile":
        return {"summary_type": "makefile", "target_candidates": _makefile_targets(text, report)}
    return {"summary_type": "config_file", "line_count": len(text.splitlines())}


def _config_type(path: Path) -> str:
    name = path.name
    if name.casefold().startswith("readme"):
        return "readme"
    if name == "Dockerfile":
        return "dockerfile"
    if name in {"docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml"}:
        return "compose"
    if name == "Makefile":
        return "makefile"
    if _looks_k8s_manifest(path):
        return "k8s_manifest"
    if name in {"pyproject.toml", "requirements.txt", "package.json"}:
        return "dependency_manifest"
    return "config_file"


def _systemd_unit_summary(path: Path, root: Path, report: RedactionCounter) -> dict[str, Any]:
    text = _read_text(path)[:65536]
    fields: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key in {"Description", "ExecStart", "WorkingDirectory", "EnvironmentFile", "Restart", "User"}:
            fields[key] = _template_path(_sanitize(value.strip(), report))
    referenced = _referenced_programs(text, report)
    rel = _sanitize(_rel(path, root), report)
    return {
        "unit_name": path.name,
        "relative_path": rel,
        "unit_file_path_template": rel,
        "description": fields.get("Description", ""),
        "exec_start_template": fields.get("ExecStart", ""),
        "working_directory_template": fields.get("WorkingDirectory", ""),
        "environment_file_template": fields.get("EnvironmentFile", ""),
        "restart": fields.get("Restart", ""),
        "user": _sanitize(fields.get("User", ""), report),
        "referenced_programs": referenced,
        "raw_unit_uploaded": False,
    }


def _dependency_manifest_summary(path: Path, root: Path, report: RedactionCounter) -> dict[str, Any]:
    rel = _sanitize(_rel(path, root), report)
    text = _read_text(path)[:65536]
    names: list[str] = []
    if path.name == "requirements.txt":
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            names.append(_sanitize(re.split(r"[<>=~!;\[]", stripped, maxsplit=1)[0].strip(), report))
    elif path.name == "package.json":
        try:
            payload = json.loads(text)
            for section in ("dependencies", "devDependencies"):
                value = payload.get(section) if isinstance(payload, dict) else {}
                if isinstance(value, dict):
                    names.extend(_sanitize(str(key), report) for key in value.keys())
        except json.JSONDecodeError:
            names = []
    elif path.name == "pyproject.toml":
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("name ="):
                names.append(_sanitize(stripped.split("=", 1)[1].strip().strip('"').strip("'"), report))
    return {
        "relative_path": rel,
        "manifest_type": path.name,
        "dependency_name_candidates": sorted({name for name in names if name})[:100],
        "raw_manifest_uploaded": False,
    }


def _env_key_summaries(path: Path, root: Path, report: RedactionCounter) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line in _read_text(path).splitlines()[:500]:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        key = key.strip()
        secret_like = bool(SECRET_ENV_KEY_RE.search(key))
        safe_key = "" if secret_like else _sanitize(key, report)
        rows.append(
            {
                "relative_path": _sanitize(_rel(path, root), report),
                "key_name": safe_key,
                "key_hash": sha256_text(key.casefold())[:16],
                "value_type": _value_type(value),
                "present": bool(value.strip()),
                "secret_like": secret_like,
                "raw_value_uploaded": False,
                "raw_key_uploaded": not secret_like,
            }
        )
    return rows


def _version_context(root: Path, files: list[Path], units: list[dict[str, Any]], report: RedactionCounter) -> dict[str, Any]:
    mtimes = []
    for path in files:
        try:
            mtimes.append(path.stat().st_mtime)
        except OSError:
            continue
    git = _git_context(root)
    return {
        "git_commit_hash": git.get("commit", ""),
        "git_dirty": git.get("dirty", False),
        "git_available": git.get("available", False),
        "file_mtime_summary": {
            "file_count": len(mtimes),
            "min_mtime": _mtime(min(mtimes)) if mtimes else "",
            "max_mtime": _mtime(max(mtimes)) if mtimes else "",
        },
        "systemd_unit_file_path_templates": [row.get("unit_file_path_template") for row in units if row.get("unit_file_path_template")],
        "exec_start_path_templates": [row.get("exec_start_template") for row in units if row.get("exec_start_template")],
        "deployed_version_confirmed": False,
        "caveat": "Source context may not match the deployed version during the incident window unless deployment evidence confirms it.",
    }


def _git_context(root: Path) -> dict[str, Any]:
    try:
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=root,
            text=True,
            capture_output=True,
            check=True,
            timeout=5,
        ).stdout.strip()
        dirty = bool(
            subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=root,
                text=True,
                capture_output=True,
                check=True,
                timeout=5,
            ).stdout.strip()
        )
        return {"available": True, "commit": commit, "dirty": dirty}
    except Exception:
        return {"available": False, "commit": "", "dirty": False}


def _analysis_component_candidates(source_context: dict[str, Any]) -> list[dict[str, Any]]:
    names: dict[str, dict[str, Any]] = {}

    def add(name: str, *, source_ref: str, confidence: float, features: Iterable[str]) -> None:
        safe = _component_name(name)
        row = names.setdefault(
            safe,
            {
                "component_id": "",
                "name": safe,
                "matched_source_refs": set(),
                "matched_features": set(),
                "suggested_role": _suggest_role(safe),
                "suggested_subsystem": _suggest_subsystem(safe),
                "suggested_core_target_types": set(_core_targets_for_text(safe)),
                "confidence": 0.0,
                "human_review_required": True,
            },
        )
        row["matched_source_refs"].add(source_ref)
        row["matched_features"].update(features)
        row["suggested_core_target_types"].update(_core_targets_for_text(" ".join(features)))
        row["confidence"] = max(float(row["confidence"]), confidence)

    for unit in source_context.get("systemd_units") or []:
        if isinstance(unit, dict):
            name = str(unit.get("unit_name") or unit.get("relative_path") or "")
            add(name, source_ref=str(unit.get("relative_path") or name), confidence=0.86, features=["systemd_unit", str(unit.get("exec_start_template") or "")])
    for item in source_context.get("source_items") or []:
        if not isinstance(item, dict):
            continue
        rel = str(item.get("relative_path") or "")
        if item.get("entrypoint_candidate"):
            add(rel, source_ref=rel, confidence=0.74, features=["entrypoint"])
        for metric in item.get("metric_name_candidates") or []:
            add(str(metric), source_ref=rel, confidence=0.64, features=["metric_definition"])
    if not names:
        source = source_context.get("source") if isinstance(source_context.get("source"), dict) else {}
        add(str(source.get("service") or "service"), source_ref="source.service", confidence=0.5, features=["service_name"])
    rows: list[dict[str, Any]] = []
    for index, row in enumerate(sorted(names.values(), key=lambda item: str(item["name"])), start=1):
        rows.append(
            {
                **row,
                "component_id": f"COMP-SRC-{index:03d}",
                "matched_source_refs": sorted(row["matched_source_refs"]),
                "matched_features": sorted(row["matched_features"]),
                "suggested_core_target_types": sorted(row["suggested_core_target_types"]),
                "confidence": round(float(row["confidence"]), 2),
                "context_is_not_incident_evidence": True,
            }
        )
    return rows


def _analysis_metric_semantics_candidates(source_context: dict[str, Any]) -> list[dict[str, Any]]:
    names = set()
    summary = source_context.get("project_summary") if isinstance(source_context.get("project_summary"), dict) else {}
    names.update(str(item) for item in summary.get("metric_name_candidates") or [] if str(item).strip())
    for item in source_context.get("source_items") or []:
        if isinstance(item, dict):
            names.update(str(metric) for metric in item.get("metric_name_candidates") or [] if str(metric).strip())
    rows = []
    for index, name in enumerate(sorted(names), start=1):
        rows.append(
            {
                "metric_semantics_id": f"METRIC-SRC-{index:03d}",
                "metric_name": name,
                "suggested_semantics": _metric_semantics_for_name(name),
                "source_refs": _source_refs_for_metric(source_context, name),
                "confidence": 0.72,
                "human_review_required": True,
                "context_is_not_incident_evidence": True,
            }
        )
    return rows


def _analysis_logger_mapping_candidates(source_context: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    seen: set[tuple[str, str]] = set()
    for item in source_context.get("source_items") or []:
        if not isinstance(item, dict):
            continue
        rel = str(item.get("relative_path") or "")
        for logger in item.get("logger_candidates") or []:
            key = (str(logger), rel)
            if key in seen:
                continue
            seen.add(key)
            rows.append(
                {
                    "logger_mapping_id": f"LOGGER-SRC-{len(rows)+1:03d}",
                    "logger_name": str(logger),
                    "source_ref": rel,
                    "suggested_component": _component_name(rel),
                    "human_review_required": True,
                    "context_is_not_incident_evidence": True,
                }
            )
    return rows


def _analysis_instrumentation_candidates(
    source_context: dict[str, Any],
    *,
    metric_candidates: list[dict[str, Any]],
    logger_candidates: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    rows = []
    if metric_candidates:
        rows.append(
            {
                "instrumentation_id": "INST-SRC-001",
                "instrumentation_type": "metrics",
                "candidate_names": [row["metric_name"] for row in metric_candidates],
                "interpretation": "Metric names were found in sanitized source excerpts or summaries.",
                "human_review_required": True,
                "context_is_not_incident_evidence": True,
            }
        )
    if logger_candidates:
        rows.append(
            {
                "instrumentation_id": "INST-SRC-002",
                "instrumentation_type": "logging",
                "candidate_names": [row["logger_name"] for row in logger_candidates],
                "interpretation": "Logger references were found in sanitized source excerpts.",
                "human_review_required": True,
                "context_is_not_incident_evidence": True,
            }
        )
    if source_context.get("systemd_units"):
        rows.append(
            {
                "instrumentation_id": "INST-SRC-003",
                "instrumentation_type": "supervisor",
                "candidate_names": [row.get("unit_name") for row in source_context.get("systemd_units") or [] if isinstance(row, dict)],
                "interpretation": "Systemd units provide supervisor configuration context only.",
                "human_review_required": True,
                "context_is_not_incident_evidence": True,
            }
        )
    return rows


def _analysis_collector_mapping_candidates(
    source_context: dict[str, Any],
    component_candidates: list[dict[str, Any]],
    metric_candidates: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    rows = []
    units = [str(row.get("unit_name")) for row in source_context.get("systemd_units") or [] if isinstance(row, dict) and row.get("unit_name")]
    if units:
        rows.append(
            {
                "request_type": "process_state_query",
                "candidate_collectors": ["local_systemd", "local_journal", "local_process"],
                "params": {"units": units},
                "safety_level": "read_only",
                "human_review_required": True,
                "context_is_not_incident_evidence": True,
            }
        )
    if metric_candidates:
        rows.append(
            {
                "request_type": "metric_semantics_query",
                "candidate_collectors": ["local_metrics", "local_prometheus", "local_logs"],
                "params": {"metric_names": [row["metric_name"] for row in metric_candidates]},
                "safety_level": "read_only",
                "human_review_required": True,
                "context_is_not_incident_evidence": True,
            }
        )
    if component_candidates:
        rows.append(
            {
                "request_type": "instrumentation_consistency_query",
                "candidate_collectors": ["local_logs", "local_config"],
                "params": {"components": [row["name"] for row in component_candidates]},
                "safety_level": "read_only",
                "human_review_required": True,
                "context_is_not_incident_evidence": True,
            }
        )
    return rows


def _analysis_profile_mapping_hints(
    source_context: dict[str, Any],
    component_candidates: list[dict[str, Any]],
    metric_candidates: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    hints = []
    for row in component_candidates[:20]:
        hints.append(
            {
                "hint_type": "component_map",
                "name": row.get("name"),
                "suggested_subsystem": row.get("suggested_subsystem"),
                "human_review_required": True,
                "context_is_not_incident_evidence": True,
            }
        )
    for row in metric_candidates[:20]:
        hints.append(
            {
                "hint_type": "metric_semantics",
                "name": row.get("metric_name"),
                "suggested_semantics": row.get("suggested_semantics"),
                "human_review_required": True,
                "context_is_not_incident_evidence": True,
            }
        )
    version = source_context.get("version_context") if isinstance(source_context.get("version_context"), dict) else {}
    if version.get("deployed_version_confirmed") is not True:
        hints.append(
            {
                "hint_type": "version_anchoring_caveat",
                "name": "deployed_version_unconfirmed",
                "caveat": version.get("caveat") or "",
                "human_review_required": True,
                "context_is_not_incident_evidence": True,
            }
        )
    return hints


def _validation_result(
    passed: bool,
    errors: list[dict[str, Any]],
    findings: list[dict[str, Any]],
    expected_sha: str,
    actual_sha: str,
) -> dict[str, Any]:
    return {
        "passed": passed,
        "errors": errors,
        "findings": findings,
        "expected_sha256": expected_sha,
        "actual_sha256": actual_sha,
    }


def _source_redaction_report(bundle: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": "source_redaction_report.v1",
        "raw_source_policy": RAW_SOURCE_POLICY,
        "raw_env_policy": RAW_ENV_POLICY,
        "source_context_sha256": bundle.get("source_context_sha256") or "",
        "summary": bundle.get("redaction_summary") or {},
        "policy": bundle.get("sanitization_policy") or {},
    }


def _load_json(path: str | Path | None) -> dict[str, Any]:
    if not path:
        return {}
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    return payload if isinstance(payload, dict) else {}


def _sanitize(value: Any, report: RedactionCounter) -> str:
    text = redact_text(value, report)
    for pattern, replacement in UNSAFE_LABEL_REPLACEMENTS:
        text = pattern.sub(replacement, text)
    return _template_path(text)


def _template_path(text: str) -> str:
    return text.replace("\\", "/")


def _rel(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return path.name


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _safe_size(path: Path) -> int:
    try:
        return int(path.stat().st_size)
    except OSError:
        return 0


def _mtime(value: float) -> str:
    return datetime.fromtimestamp(value, UTC).isoformat().replace("+00:00", "Z")


def _looks_text(path: Path) -> bool:
    return path.name in CONFIG_NAMES or path.suffix in TEXT_SUFFIXES or _is_env_file(path)


def _is_source_file(path: Path) -> bool:
    return path.suffix in {".py", ".js", ".ts", ".sh"} and not _is_env_file(path)


def _is_env_file(path: Path) -> bool:
    name = path.name
    return name == ".env" or name.startswith(".env.") or name.endswith(".env") or name.endswith(".env.example")


def _is_dependency_manifest(path: Path) -> bool:
    return path.name in DEPENDENCY_MANIFEST_NAMES


def _looks_k8s_manifest(path: Path) -> bool:
    rel = str(path).casefold()
    return path.suffix in {".yaml", ".yml"} and any(term in rel for term in ("k8s", "kubernetes", "deployment", "statefulset"))


def _language_for_path(path: Path) -> str:
    if path.name == "Dockerfile":
        return "dockerfile"
    if path.name == "Makefile":
        return "makefile"
    return LANGUAGE_BY_SUFFIX.get(path.suffix, "")


def _entrypoint_candidates(files: list[Path], root: Path, report: RedactionCounter) -> list[str]:
    rows = []
    for path in files:
        if path.name in ENTRYPOINT_NAMES or path.name.endswith(".service"):
            rows.append(_sanitize(_rel(path, root), report))
    return rows


def _logging_config_candidates(source_items: list[dict[str, Any]], config_items: list[dict[str, Any]]) -> list[str]:
    rows = []
    for item in source_items:
        if item.get("logger_candidates"):
            rows.append(str(item.get("relative_path") or ""))
    for item in config_items:
        rel = str(item.get("relative_path") or "")
        if "logging" in rel.casefold() or "log" in rel.casefold():
            rows.append(rel)
    return [item for item in rows if item]


def _metric_candidates(source_items: list[dict[str, Any]]) -> list[str]:
    rows: set[str] = set()
    for item in source_items:
        rows.update(str(metric) for metric in item.get("metric_name_candidates") or [] if str(metric).strip())
    return sorted(rows)


def _detected_project_type(files: list[Path], units: list[dict[str, Any]]) -> str:
    names = {path.name for path in files}
    if units:
        return "systemd_service"
    if "package.json" in names:
        return "node_service"
    if "pyproject.toml" in names or "requirements.txt" in names:
        return "python_project"
    if any(name in names for name in {"Dockerfile", "docker-compose.yml", "compose.yaml"}):
        return "containerized_service"
    return "generic"


def _logger_candidates(text: str, report: RedactionCounter) -> list[str]:
    rows: set[str] = set()
    for line in text.splitlines():
        if "logging.getLogger" in line:
            rows.add(_sanitize("logging.getLogger", report))
        elif "logger" in line.casefold():
            rows.add(_sanitize(line.strip()[:120], report))
    return sorted(rows)


def _value_type(value: str) -> str:
    stripped = value.strip().strip('"').strip("'")
    if not stripped:
        return "empty"
    if re.fullmatch(r"-?\d+", stripped):
        return "integer"
    if stripped.casefold() in {"true", "false", "yes", "no"}:
        return "boolean"
    if stripped.startswith(("http://", "https://")):
        return "url"
    if "/" in stripped or "\\" in stripped:
        return "path"
    return "string"


def _yaml_kind_candidates(text: str, report: RedactionCounter) -> list[str]:
    return [_sanitize(line.split(":", 1)[1].strip(), report) for line in text.splitlines() if line.strip().startswith("kind:")][:20]


def _dockerfile_instructions(text: str, report: RedactionCounter) -> list[str]:
    rows = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        instruction = stripped.split(maxsplit=1)[0].upper()
        if instruction in {"FROM", "RUN", "CMD", "ENTRYPOINT", "COPY", "ENV", "WORKDIR"}:
            rows.append(_sanitize(instruction, report))
    return rows[:40]


def _compose_services(text: str, report: RedactionCounter) -> list[str]:
    rows = []
    in_services = False
    for line in text.splitlines():
        if line.startswith("services:"):
            in_services = True
            continue
        if in_services:
            match = re.match(r"^\s{2}([A-Za-z0-9_.-]+):\s*$", line)
            if match:
                rows.append(_sanitize(match.group(1), report))
    return rows[:40]


def _makefile_targets(text: str, report: RedactionCounter) -> list[str]:
    rows = []
    for line in text.splitlines():
        match = re.match(r"^([A-Za-z0-9_.-]+):(?:\s|$)", line)
        if match and not match.group(1).startswith("."):
            rows.append(_sanitize(match.group(1), report))
    return rows[:40]


def _referenced_programs(text: str, report: RedactionCounter) -> list[str]:
    programs: set[str] = set()
    for line in text.splitlines():
        if not line.strip().startswith("ExecStart="):
            continue
        raw = line.split("=", 1)[1].strip()
        try:
            parts = shlex.split(raw)
        except ValueError:
            parts = raw.split()
        for part in parts:
            if part.startswith("-"):
                continue
            if "/" in part or part.endswith((".py", ".sh", ".js")):
                programs.add(_sanitize(part, report))
                basename = _basename(part)
                if basename:
                    programs.add(_sanitize(basename, report))
    return sorted(programs)


def _source_refs_for_metric(source_context: dict[str, Any], name: str) -> list[str]:
    refs = []
    for item in source_context.get("source_items") or []:
        if isinstance(item, dict) and name in (item.get("metric_name_candidates") or []):
            refs.append(str(item.get("relative_path") or ""))
    return sorted({ref for ref in refs if ref})


def _metric_semantics_for_name(name: str) -> dict[str, str]:
    folded = name.casefold()
    if any(term in folded for term in ("transport", "throughput", "heartbeat", "freshness", "energy")):
        return {
            "semantic_type": "candidate_health_signal",
            "zero_behavior": "suspicious",
            "increase_behavior": "healthy_or_scale",
            "decrease_behavior": "suspicious",
            "candidate_core_target_type": "throughput_disappearance",
        }
    if any(term in folded for term in ("error", "failure", "failed", "mismatch")):
        return {
            "semantic_type": "candidate_error_signal",
            "zero_behavior": "healthy",
            "increase_behavior": "suspicious",
            "decrease_behavior": "healthy_or_recovery",
            "candidate_core_target_type": "service_start_failure",
        }
    return {
        "semantic_type": "candidate_metric",
        "zero_behavior": "unknown",
        "increase_behavior": "unknown",
        "decrease_behavior": "unknown",
        "candidate_core_target_type": "general",
    }


def _component_name(text: str) -> str:
    name = text.replace("\\", "/").rsplit("/", 1)[-1].replace(".service", "")
    name = re.sub(r"[^A-Za-z0-9_]+", "_", name).strip("_").lower()
    return name or "component"


def _suggest_role(text: str) -> str:
    folded = text.casefold()
    if "watchdog" in folded:
        return "recovery_supervisor"
    if "worker" in folded or "listener" in folded:
        return "background_worker"
    if "server" in folded or "app" in folded:
        return "service_entrypoint"
    if "metric" in folded or "heartbeat" in folded:
        return "instrumentation"
    return "component"


def _suggest_subsystem(text: str) -> str:
    folded = text.casefold()
    if any(term in folded for term in ("watchdog", "restart", "supervisor")):
        return "runtime_recovery"
    if any(term in folded for term in ("systemd", "execstart", ".service")):
        return "job_configuration"
    if any(term in folded for term in ("metric", "heartbeat", "instrumentation")):
        return "monitoring"
    if any(term in folded for term in ("listener", "worker", "job")):
        return "background_processing"
    return "general"


def _core_targets_for_text(text: str) -> set[str]:
    folded = text.casefold()
    targets: set[str] = set()
    if any(term in folded for term in ("missing", "execstart", "systemd", ".service")):
        targets.update({"job_configuration_mismatch", "service_start_failure"})
    if any(term in folded for term in ("watchdog", "restart", "supervisor")):
        targets.add("restart_loop")
    if any(term in folded for term in ("metric", "heartbeat", "instrumentation")):
        targets.add("monitoring_gap")
    return targets or {"general"}


def _basename(value: str) -> str:
    return value.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
