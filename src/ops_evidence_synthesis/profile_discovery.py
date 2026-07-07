from __future__ import annotations

import json
import os
import re
import shlex
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable

from ops_evidence_synthesis.ai.base import ModelProvider
from ops_evidence_synthesis.ai.runtime import (
    run_provider_with_retries,
    safe_provider_error_message,
    safety_preflight_for_model_input,
)
from ops_evidence_synthesis.ai.vertex import VertexGeminiProvider
from ops_evidence_synthesis.canonical import canonical_json, pretty_json, sha256_json, sha256_text
from ops_evidence_synthesis.evidence_rules import ai_evidence_rules, profile_discovery_rules, source_context_rules
from ops_evidence_synthesis.local_first import (
    CANONICALIZATION_VERSION,
    RAW_LOG_POLICY,
    REQUIRED_PROFILE_QUESTIONS,
    RedactionCounter,
    redact_mapping,
    redact_text,
    scan_sanitized_text,
)
from ops_evidence_synthesis.profiles.registry import normalize_profile_id
from ops_evidence_synthesis.source_context import (
    load_source_analysis_bundle,
    load_source_context_bundle,
    source_context_to_project_entities,
    validate_source_analysis_bundle_for_upload,
    validate_source_context_bundle_for_upload,
)
from ops_evidence_synthesis.timeutils import utc_now


PROFILE_DISCOVERY_SCHEMA_VERSION = "profile_discovery_bundle.v1"
PROFILE_DISCOVERY_BUNDLE_TYPE = "sanitized_profile_discovery_bundle"
PROFILE_DRAFT_SCHEMA_VERSION = "profile_draft.v1"
PROFILE_DRAFT_TYPE = "profile_mapping_draft"
PROFILE_DRAFT_AI_SCHEMA_VERSION = "profile_draft_ai.v1"
FOCUSED_PROFILE_SCHEMA_VERSION = "focused_operational_profile.v1"
FOCUSED_PROFILE_MODEL_INPUT_SCHEMA_VERSION = "focused_operational_profile_model_input.v1"
FOCUSED_PROFILE_GENERATION_SCHEMA_VERSION = "focused_operational_profile_generation.v1"
RAW_CONFIG_POLICY = "not_uploaded"
RAW_LOGS_POLICY = "not_uploaded"
DEFAULT_PROFILE_DRAFT_GEMINI_MODEL = "gemini-3.1-pro-preview"
DEFAULT_FOCUSED_PROFILE_GEMINI_MODEL = "gemini-3.1-pro-preview"
PROFILE_DRAFT_GEMINI_ALIASES = {
    "gemini",
    "vertex-gemini",
    "gemini-enterprise-agent-platform",
}

IGNORED_DIRS = {
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
}
IGNORED_SUFFIXES = {
    ".sqlite",
    ".sqlite3",
    ".db",
    ".parquet",
    ".mp4",
    ".wav",
    ".zip",
    ".tar",
    ".gz",
    ".tgz",
    ".xz",
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    ".pdf",
}
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
DEPENDENCY_MANIFESTS = {
    "pyproject.toml",
    "requirements.txt",
    "package.json",
    "Dockerfile",
    "docker-compose.yml",
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
    ".env",
    ".example",
    ".ini",
    ".cfg",
    ".conf",
    ".sh",
}
SECRET_ENV_KEY_RE = re.compile(
    r"(TOKEN|SECRET|PASSWORD|PASSWD|KEY|PRIVATE|CREDENTIAL|COOKIE|AUTH|API_KEY|WEBHOOK|STREAM_KEY)",
    re.IGNORECASE,
)
SYSTEMD_UNIT_RE = re.compile(r"\b[A-Za-z0-9_.@:+\-]+\.service\b")
PATH_RE = re.compile(r"(?:<USER_HOME>|/)[A-Za-z0-9_./@%+\-]+")
URL_RE = re.compile(r"\bhttps?://[A-Za-z0-9._~:/?#\[\]@!$&'()*+,;=%\-]+")
DOMAIN_RE = re.compile(r"\b[A-Za-z0-9][A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
LOGGER_RE = re.compile(r"\b(?:logger|module|component)=([A-Za-z_][A-Za-z0-9_.-]{2,})")
METRIC_RE = re.compile(
    r"\b[A-Za-z_][A-Za-z0-9_]*(?:count|total|seconds|latency|duration|freshness|heartbeat|energy|bytes|errors?)\b",
    re.IGNORECASE,
)
METRIC_HINT_RE = re.compile(r"\b(heartbeat|freshness|audio_energy|capture_freshness|stream_transport)\b", re.IGNORECASE)
PROCESS_RE = re.compile(r"\b(?:process|program|cmd|command)=([A-Za-z0-9_.@/\-]+)")
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


def discover_profile(
    project_root: str | Path | None = None,
    *,
    evidence_bundle_path: str | Path | None,
    service: str,
    environment: str,
    output_dir: str | Path,
    source_context_path: str | Path | None = None,
    source_analysis_path: str | Path | None = None,
) -> dict[str, Any]:
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    bundle = build_profile_discovery_bundle(
        project_root,
        evidence_bundle_path=evidence_bundle_path,
        service=service,
        environment=environment,
        source_context_path=source_context_path,
        source_analysis_path=source_analysis_path,
    )
    discovery_path = output / "profile_discovery_bundle.json"
    manifest_path = output / "manifest.json"
    report_path = output / "redaction_report.json"
    discovery_path.write_text(pretty_json(bundle) + "\n", encoding="utf-8")
    report_path.write_text(pretty_json(_discovery_redaction_report(bundle)) + "\n", encoding="utf-8")
    manifest_path.write_text(pretty_json(_discovery_manifest(project_root, bundle)) + "\n", encoding="utf-8")
    return {
        "profile_discovery_bundle": str(discovery_path),
        "manifest": str(manifest_path),
        "redaction_report": str(report_path),
        "observed_entity_count": len(bundle["observed_entities"]),
        "project_entity_count": len(bundle["project_entities"]),
        "entity_link_count": len(bundle["entity_links"]),
        "discovery_sha256": bundle["discovery_sha256"],
    }


def build_profile_discovery_bundle(
    project_root: str | Path | None = None,
    *,
    evidence_bundle_path: str | Path | None,
    evidence_bundle: dict[str, Any] | None = None,
    service: str,
    environment: str,
    source_context_path: str | Path | None = None,
    source_analysis_path: str | Path | None = None,
    source_context_bundle: dict[str, Any] | None = None,
    source_analysis_bundle: dict[str, Any] | None = None,
) -> dict[str, Any]:
    root = Path(project_root) if project_root else None
    report = RedactionCounter()
    evidence_bundle = dict(evidence_bundle) if isinstance(evidence_bundle, dict) else (_load_json(evidence_bundle_path) if evidence_bundle_path else {})
    source_context = dict(source_context_bundle) if isinstance(source_context_bundle, dict) else load_source_context_bundle(source_context_path)
    source_analysis = dict(source_analysis_bundle) if isinstance(source_analysis_bundle, dict) else load_source_analysis_bundle(source_analysis_path)
    if source_context:
        validation = validate_source_context_bundle_for_upload(source_context)
        if not validation["passed"]:
            raise ValueError("source_context_bundle validation failed")
    if source_analysis:
        validation = validate_source_analysis_bundle_for_upload(source_analysis)
        if not validation["passed"]:
            raise ValueError("source_analysis_bundle validation failed")
    evidence_sha256 = str(evidence_bundle.get("evidence_sha256") or "")
    observed_entities = extract_observed_entities(evidence_bundle, service=service, report=report)
    if source_context:
        project_entities = source_context_to_project_entities(source_context)
    elif root is not None:
        project_entities = discover_project_entities(root, observed_entities=observed_entities, report=report)
    else:
        project_entities = []
    entity_links = link_entities(observed_entities, project_entities)
    component_candidates = _merge_candidates(
        build_component_candidates(observed_entities, project_entities, entity_links),
        _source_analysis_rows(source_analysis, "component_candidates"),
        key_fields=("name",),
    )
    metric_candidates = _merge_candidates(
        build_metric_semantics_candidates(observed_entities, project_entities),
        _source_analysis_rows(source_analysis, "metric_semantics_candidates"),
        key_fields=("metric_name",),
    )
    collector_candidates = _merge_candidates(
        build_collector_mapping_candidates(observed_entities, component_candidates, metric_candidates),
        _source_analysis_rows(source_analysis, "collector_mapping_candidates"),
        key_fields=("request_type",),
    )
    redaction_summary = report.summary()
    redaction_total = sum(int(value) for value in redaction_summary.values())
    detected_project_type = detect_project_type(project_entities)
    bundle: dict[str, Any] = {
        "schema_version": PROFILE_DISCOVERY_SCHEMA_VERSION,
        "bundle_type": PROFILE_DISCOVERY_BUNDLE_TYPE,
        "raw_config_policy": RAW_CONFIG_POLICY,
        "raw_logs_policy": RAW_LOGS_POLICY,
        "canonicalization_version": CANONICALIZATION_VERSION,
        "discovery_sha256": "",
        "source": {
            "project_name": _sanitize_discovery_text(
                (source_context.get("source") or {}).get("project_name") if source_context else (root.name if root else "project"),
                report,
            ),
            "service": _sanitize_discovery_text(service, report),
            "environment": _sanitize_discovery_text(environment, report),
            "project_root_uploaded": False,
            "evidence_sha256": evidence_sha256,
            "profile_confidence": "inferred" if observed_entities else "unknown",
            "source_context_sha256": source_context.get("source_context_sha256") or "",
            "source_analysis_sha256": source_analysis.get("analysis_sha256") or "",
        },
        "discovery_policy": {
            "mode": "source_first_sanitized_context" if source_context else "thin_baseline_plus_log_driven_retrieval",
            "upload_raw_source": False,
            "upload_raw_env_values": False,
            "max_file_excerpt_bytes": 8192,
            "max_total_excerpt_bytes": 200000,
            "ignored_dirs": sorted(IGNORED_DIRS),
            "source_context_is_incident_evidence": False,
            "source_analysis_is_incident_evidence": False,
        },
        "local_first_summary": {
            "raw_configs_uploaded": False,
            "raw_logs_uploaded": False,
            "raw_source_uploaded": False,
            "raw_env_values_uploaded": False,
            "detected_project_type": detected_project_type,
            "observed_entity_count": len(observed_entities),
            "project_entity_count": len(project_entities),
            "entity_link_count": len(entity_links),
            "redaction_total": redaction_total,
            "discovery_sha256": "",
            "source_context_sha256": source_context.get("source_context_sha256") or "",
            "source_analysis_sha256": source_analysis.get("analysis_sha256") or "",
        },
        "display_summary": {
            "title": "Source-first profile discovery" if source_context else "Log-driven profile discovery",
            "subtitle": (
                "Raw source and raw env values were not uploaded. Mapping was generated from "
                "Sanitized Source Context, Source Analysis, and sanitized evidence entities."
                if source_context
                else "Raw source and raw env values were not uploaded. Mapping was generated from "
                "sanitized log entities and project structure."
            ),
            "primary_badges": [
                f"raw_config_policy:{RAW_CONFIG_POLICY}",
                f"raw_logs_policy:{RAW_LOGS_POLICY}",
                "profile_draft:requires_human_review",
            ],
        },
        "observed_entities": observed_entities,
        "project_entities": project_entities,
        "entity_links": entity_links,
        "component_candidates": component_candidates,
        "metric_semantics_candidates": metric_candidates,
        "collector_mapping_candidates": collector_candidates,
        "external_dependency_candidates": build_external_dependency_candidates(observed_entities),
        "required_profile_questions": _profile_discovery_questions(observed_entities, component_candidates, metric_candidates),
        "redaction_summary": redaction_summary,
        "prompt_rules": ai_evidence_rules() + profile_discovery_rules() + source_context_rules(),
    }
    if source_context:
        bundle["source_context_summary"] = {
            "source_context_sha256": source_context.get("source_context_sha256") or "",
            "bundle_type": source_context.get("bundle_type") or "",
            "context_is_not_incident_evidence": True,
            "version_context": source_context.get("version_context") or {},
            "project_summary": source_context.get("project_summary") or {},
        }
    if source_analysis:
        bundle["source_analysis_summary"] = {
            "analysis_sha256": source_analysis.get("analysis_sha256") or "",
            "bundle_type": source_analysis.get("bundle_type") or "",
            "context_is_not_incident_evidence": True,
            "component_candidate_count": len(source_analysis.get("component_candidates") or []),
            "metric_semantics_candidate_count": len(source_analysis.get("metric_semantics_candidates") or []),
        }
    bundle["discovery_sha256"] = sha256_json(profile_discovery_hash_payload(bundle))
    bundle["local_first_summary"]["discovery_sha256"] = bundle["discovery_sha256"]
    return bundle


def draft_profile(
    discovery_bundle_path: str | Path,
    *,
    provider: str,
    out_path: str | Path,
    model_name: str = "",
) -> dict[str, Any]:
    discovery = _load_json(discovery_bundle_path)
    provider_name = provider.strip().casefold().replace("_", "-")
    if provider_name == "local":
        draft = build_profile_draft(discovery)
    elif provider_name in PROFILE_DRAFT_GEMINI_ALIASES:
        draft = build_profile_draft_with_provider(
            discovery,
            _profile_draft_gemini_provider(model_name=model_name),
        )
    else:
        supported = "local, gemini, vertex-gemini, gemini-enterprise-agent-platform"
        raise ValueError(f"unsupported profile draft provider '{provider}'. Supported providers: {supported}")
    output = Path(out_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(pretty_json(draft) + "\n", encoding="utf-8")
    return draft


def draft_focused_profile(
    discovery_bundle_path: str | Path,
    *,
    provider: str,
    out_path: str | Path,
    model_name: str = "",
    evidence_bundle_path: str | Path | None = None,
    source_context_path: str | Path | None = None,
    source_analysis_path: str | Path | None = None,
) -> dict[str, Any]:
    discovery = _load_json(discovery_bundle_path)
    evidence_bundle = _load_json(evidence_bundle_path) if evidence_bundle_path else {}
    source_context = load_source_context_bundle(source_context_path) if source_context_path else {}
    source_analysis = load_source_analysis_bundle(source_analysis_path) if source_analysis_path else {}
    if source_context:
        validation = validate_source_context_bundle_for_upload(source_context)
        if not validation["passed"]:
            raise ValueError("source_context_bundle validation failed")
    if source_analysis:
        validation = validate_source_analysis_bundle_for_upload(source_analysis)
        if not validation["passed"]:
            raise ValueError("source_analysis_bundle validation failed")

    provider_name = provider.strip().casefold().replace("_", "-")
    if provider_name == "local":
        profile = build_focused_profile(
            discovery,
            evidence_bundle=evidence_bundle,
            source_context=source_context,
            source_analysis=source_analysis,
        )
    elif provider_name in PROFILE_DRAFT_GEMINI_ALIASES:
        profile = build_focused_profile_with_provider(
            discovery,
            _focused_profile_gemini_provider(model_name=model_name),
            evidence_bundle=evidence_bundle,
            source_context=source_context,
            source_analysis=source_analysis,
        )
    else:
        supported = "local, gemini, vertex-gemini, gemini-enterprise-agent-platform"
        raise ValueError(f"unsupported focused profile provider '{provider}'. Supported providers: {supported}")
    output = Path(out_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(pretty_json(profile) + "\n", encoding="utf-8")
    return profile


def build_profile_draft_with_provider(discovery: dict[str, Any], provider: ModelProvider) -> dict[str, Any]:
    base = build_profile_draft(discovery)
    model_input = _profile_draft_model_input(discovery)
    metadata: dict[str, Any] = {
        "schema_version": "profile_draft_generation.v1",
        "generation_mode": "gemini_profile_draft",
        "provider_id": getattr(provider, "provider", ""),
        "model_name": getattr(provider, "model_name", ""),
        "prompt_name": getattr(provider, "prompt_name", ""),
        "llm_status": "not_started",
        "fallback_used": False,
        "source_discovery_sha256": discovery.get("discovery_sha256") or "",
        "model_input_sha256": sha256_json(model_input),
    }
    preflight = safety_preflight_for_model_input(model_input, filename="profile_draft_model_input.json")
    if not preflight.passed:
        metadata.update(
            {
                "llm_status": "blocked_by_safety_preflight",
                "fallback_used": True,
                "failure_reason": preflight.failure_reason,
                "finding_types": list(preflight.finding_types),
                "finding_count": preflight.finding_count,
            }
        )
        return _with_profile_generation_metadata(base, metadata)

    run = run_provider_with_retries(provider, model_input)
    metadata.update(run.retry_metadata())
    metadata["llm_status"] = run.response.status
    metadata["raw_output_sha256"] = sha256_text(run.response.raw_output)
    if run.response.status != "ok":
        metadata.update(
            {
                "fallback_used": True,
                "failure_reason": run.failure_reason or run.response.status,
                "provider_error_message": _provider_error_message_from_raw_output(run.response.raw_output),
            }
        )
        return _with_profile_generation_metadata(base, metadata)

    from ops_evidence_synthesis.synthesis.output_ingest import parse_model_output

    parsed = parse_model_output(run.response.raw_output)
    metadata.update(
        {
            "parse_status": parsed.parse_status,
            "repair_applied": parsed.repair_applied,
            "repair_rules": list(parsed.repair_rules),
            "repaired_output_sha256": parsed.repaired_output_sha256,
        }
    )
    if parsed.parsed is None:
        metadata.update(
            {
                "fallback_used": True,
                "failure_reason": "invalid_profile_draft_json",
                "parse_errors": [safe_provider_error_message(error, max_chars=240) for error in parsed.parse_errors],
            }
        )
        return _with_profile_generation_metadata(base, metadata)

    metadata["parsed_json_sha256"] = sha256_json(parsed.parsed)
    normalized = _profile_draft_from_ai_payload(parsed.parsed, discovery, base)
    metadata["llm_status"] = "ok"
    metadata["fallback_used"] = False
    return _with_profile_generation_metadata(normalized, metadata)


def approve_profile_draft(
    profile_draft_path: str | Path,
    *,
    profile_id: str,
    approved_by: str,
    out_path: str | Path,
    note: str = "",
) -> dict[str, Any]:
    draft = _load_json(profile_draft_path)
    approved = approved_profile_from_draft(
        draft,
        profile_id=profile_id,
        approved_by=approved_by,
        note=note,
    )
    output = Path(out_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(pretty_json(approved) + "\n", encoding="utf-8")
    _clear_profile_loader_cache()
    return {
        "approved_profile": str(output),
        "profile_id": approved["profile_id"],
        "approved": True,
        "explicit_profile": True,
        "source_discovery_sha256": approved.get("profile_discovery_approval", {}).get("source_discovery_sha256", ""),
    }


def approved_profile_from_draft(
    draft: dict[str, Any],
    *,
    profile_id: str,
    approved_by: str,
    note: str = "",
) -> dict[str, Any]:
    if not isinstance(draft, dict) or draft.get("schema_version") != PROFILE_DRAFT_SCHEMA_VERSION:
        raise ValueError("profile_draft.v1 is required")
    if draft.get("approved") is not False or draft.get("explicit_profile") is not False:
        raise ValueError("only unapproved profile drafts can be approved")
    profile = draft.get("profile") if isinstance(draft.get("profile"), dict) else {}
    normalized = normalize_profile_id(profile_id or profile.get("profile_id") or "approved_profile")
    metric_semantics = profile.get("metric_semantics") if isinstance(profile.get("metric_semantics"), dict) else {}
    system_type = str(profile.get("system_type") or "generic")
    purpose = str(profile.get("purpose") or "Human-approved profile generated from Profile Discovery.")
    component_map = profile.get("component_map") if isinstance(profile.get("component_map"), dict) else {}
    approved_profile = {
        "profile_id": normalized,
        "profile_label": f"{normalized} approved profile",
        "source_system": normalized,
        "system_profile": {
            "system_name": normalized,
            "system_type": system_type,
            "purpose": purpose,
            "critical_user_outcomes": list(profile.get("critical_outcomes") or []),
            "profile_scope": "Human-approved profile generated from sanitized Profile Discovery.",
        },
        "operational_contract": {
            "expected_normal": [],
            "failure_indicators": [],
            "non_critical_noise": [],
        },
        "log_sources": list(profile.get("log_sources") or []),
        "component_map": component_map,
        "metric_semantics": metric_semantics,
        "metrics": _metrics_from_semantics(metric_semantics, component_map),
        "known_benign_noise": list(profile.get("known_benign_noise") or []),
        "action_constraints": [
            *list(profile.get("action_constraints") or []),
            "System Profile is context, not evidence.",
            "Support claims must cite evidence_id.",
        ],
        "collector_mappings": profile.get("collector_mappings") or {},
        "review_policy": {
            "context_is_not_evidence": True,
            "require_evidence_id_for_support": True,
            "profile_draft_approved": True,
        },
        "profile_discovery_approval": {
            "approved": True,
            "explicit_profile": True,
            "approved_by": redact_text(approved_by, RedactionCounter()),
            "approved_at": utc_now(),
            "approval_note": redact_text(note, RedactionCounter()),
            "source_discovery_sha256": draft.get("source_discovery_sha256") or "",
            "source_draft_schema_version": draft.get("schema_version"),
            "source_draft_type": draft.get("draft_type"),
        },
        "context_note": "System profile fields are interpretation context only. They are not evidence and must not be cited as support.",
    }
    return redact_mapping(approved_profile, RedactionCounter())


def build_profile_draft(discovery: dict[str, Any]) -> dict[str, Any]:
    source = discovery.get("source") if isinstance(discovery.get("source"), dict) else {}
    component_candidates = [row for row in discovery.get("component_candidates") or [] if isinstance(row, dict)]
    metric_candidates = [row for row in discovery.get("metric_semantics_candidates") or [] if isinstance(row, dict)]
    collector_candidates = [row for row in discovery.get("collector_mapping_candidates") or [] if isinstance(row, dict)]
    profile_id = _slug(str(source.get("service") or source.get("project_name") or "discovered_profile"))
    component_map = {
        _slug(str(row.get("name") or row.get("component_id"))): {
            "name": row.get("name"),
            "role": row.get("suggested_role"),
            "subsystem": row.get("suggested_subsystem"),
            "core_target_types": row.get("suggested_core_target_types") or [],
            "confidence": row.get("confidence"),
            "human_review_required": True,
        }
        for row in component_candidates
    }
    metric_semantics = {
        str(row.get("metric_name")): {
            **(row.get("suggested_semantics") if isinstance(row.get("suggested_semantics"), dict) else {}),
            "confidence": row.get("confidence"),
            "human_review_required": True,
        }
        for row in metric_candidates
        if row.get("metric_name")
    }
    collector_mappings = {
        str(row.get("request_type") or f"collector_{index:03d}"): {
            "candidate_collectors": row.get("candidate_collectors") or [],
            "params": row.get("params") or {},
            "safety_level": "read_only",
            "human_review_required": True,
        }
        for index, row in enumerate(collector_candidates, start=1)
    }
    draft = {
        "schema_version": PROFILE_DRAFT_SCHEMA_VERSION,
        "draft_type": PROFILE_DRAFT_TYPE,
        "source_discovery_sha256": discovery.get("discovery_sha256") or "",
        "human_review_required": True,
        "approved": False,
        "explicit_profile": False,
        "display_summary": {
            "title": "Profile draft requires human review",
            "subtitle": "Component mappings and metric semantics are suggestions, not an explicit profile.",
            "primary_badges": [
                "approved:false",
                "explicit_profile:false",
                "collector_mappings:read_only",
            ],
        },
        "profile": {
            "profile_id": profile_id,
            "system_type": discovery.get("local_first_summary", {}).get("detected_project_type", "generic"),
            "purpose": "Draft profile generated from sanitized log-observed entities and project structure.",
            "critical_outcomes": [],
            "component_map": component_map,
            "metric_semantics": metric_semantics,
            "log_sources": _draft_log_sources(discovery),
            "known_benign_noise": [],
            "action_constraints": [
                "Collector mappings are read-only until human approved.",
                "Do not propose credential changes from a profile draft.",
            ],
            "collector_mappings": collector_mappings,
        },
        "confidence_summary": {
            "overall_confidence": _average([row.get("confidence") for row in component_candidates + metric_candidates]),
            "component_mapping_confidence": _average([row.get("confidence") for row in component_candidates]),
            "metric_semantics_confidence": _average([row.get("confidence") for row in metric_candidates]),
            "collector_mapping_confidence": _average([0.7 for _row in collector_candidates]),
        },
        "assumptions": [
            "Critical user outcomes were not inferred as facts.",
            "Component roles are candidates derived from sanitized entity links.",
        ],
        "required_human_decisions": [
            "Approve or edit component roles before using this as an explicit profile.",
            "Approve metric zero/increase/decrease semantics.",
            "Confirm critical user outcomes.",
            "Confirm read-only collector mappings before collection.",
        ],
        "required_profile_questions": discovery.get("required_profile_questions") or REQUIRED_PROFILE_QUESTIONS,
        "prompt_rules": ai_evidence_rules() + profile_discovery_rules(),
    }
    return redact_mapping(draft, RedactionCounter())


def build_focused_profile(
    discovery: dict[str, Any],
    *,
    evidence_bundle: dict[str, Any] | None = None,
    source_context: dict[str, Any] | None = None,
    source_analysis: dict[str, Any] | None = None,
) -> dict[str, Any]:
    source = discovery.get("source") if isinstance(discovery.get("source"), dict) else {}
    evidence_bundle = evidence_bundle if isinstance(evidence_bundle, dict) else {}
    source_context = source_context if isinstance(source_context, dict) else {}
    source_analysis = source_analysis if isinstance(source_analysis, dict) else {}
    components = _focused_component_rows(discovery, source_analysis)
    metrics = _focused_metric_rows(discovery, source_analysis)
    collectors = _focused_collector_rows(discovery, source_analysis)
    log_sources = _focused_log_sources(discovery, evidence_bundle, source_analysis)
    system_label = str(source.get("service") or source.get("project_name") or "discovered-system")
    project_summary = source_context.get("project_summary") if isinstance(source_context.get("project_summary"), dict) else {}
    detected_type = str(
        discovery.get("local_first_summary", {}).get("detected_project_type")
        or project_summary.get("detected_project_type")
        or "operational_service"
    )
    focused = {
        "schema_version": FOCUSED_PROFILE_SCHEMA_VERSION,
        "system_label": _sanitize_discovery_text(system_label, RedactionCounter()),
        "source_discovery_sha256": discovery.get("discovery_sha256") or "",
        "source_evidence_sha256": evidence_bundle.get("evidence_sha256") or "",
        "source_context_sha256": source_context.get("source_context_sha256") or "",
        "source_analysis_sha256": source_analysis.get("analysis_sha256") or "",
        "human_review_required": [
            "Confirm the system purpose and user-impact boundary before approving as an explicit profile.",
            "Confirm metric semantics and healthy direction before using them as incident support.",
            "Confirm read-only collectors before any follow-up data collection.",
        ],
        "system_summary": {
            "system_type": _sanitize_discovery_text(detected_type, RedactionCounter()),
            "primary_purpose": _focused_primary_purpose(project_summary, components, log_sources),
            "logged_subject": _focused_logged_subject(log_sources, metrics),
            "operational_boundary": "Sanitized code/config context can explain component roles; runtime claims still require Evidence Item ids.",
            "confidence": _average([row.get("confidence") for row in components]) or 0.6,
        },
        "runtime_components": [
            {
                "component_id": _slug(str(row.get("component_id") or row.get("name") or f"component_{index:03d}")),
                "name": _sanitize_discovery_text(str(row.get("name") or row.get("component_id") or f"component_{index:03d}"), RedactionCounter()),
                "role": _sanitize_discovery_text(
                    str(row.get("suggested_role") or row.get("role") or "Candidate runtime component."),
                    RedactionCounter(),
                ),
                "evidence_refs": _refs_from_row(row),
                "source_context_refs": _source_refs_from_row(row),
                "confidence": _safe_float(row.get("confidence"), default=0.6),
            }
            for index, row in enumerate(components, start=1)
        ],
        "observability_contract": {
            "logs": log_sources,
            "metrics": [
                {
                    "metric_name": _sanitize_discovery_text(str(row.get("metric_name") or row.get("name") or ""), RedactionCounter()),
                    "meaning": _sanitize_discovery_text(_metric_meaning(row), RedactionCounter()),
                    "healthy_direction": _metric_healthy_direction(row),
                    "evidence_refs": _refs_from_row(row),
                    "source_context_refs": _source_refs_from_row(row),
                }
                for row in metrics
                if row.get("metric_name") or row.get("name")
            ],
            "heartbeats": [
                {
                    "name": _sanitize_discovery_text(str(row.get("metric_name") or row.get("name") or ""), RedactionCounter()),
                    "meaning": _sanitize_discovery_text(_metric_meaning(row), RedactionCounter()),
                    "evidence_refs": _refs_from_row(row),
                    "source_context_refs": _source_refs_from_row(row),
                }
                for row in metrics
                if "heartbeat" in str(row.get("metric_name") or row.get("name") or row.get("suggested_semantics") or "").casefold()
            ][:8],
            "state_files": _focused_state_files(source_context, source_analysis),
        },
        "orchestration_flows": _focused_orchestration_flows(components, collectors),
        "failure_modes": _focused_failure_modes(metrics, components),
        "read_only_collectors": [
            {
                "collector": _sanitize_discovery_text(
                    str(row.get("request_type") or row.get("collector") or f"collector_{index:03d}"),
                    RedactionCounter(),
                ),
                "purpose": _sanitize_discovery_text(
                    str(row.get("request_description") or row.get("description") or row.get("need") or "Collect read-only profile evidence."),
                    RedactionCounter(),
                ),
                "safety_level": "read_only",
            }
            for index, row in enumerate(collectors[:10], start=1)
        ],
        "profile_limits": {
            "source_context_is_incident_evidence": False,
            "runtime_claims_require_evidence_id": True,
            "approval_required_before_explicit_profile": True,
            "raw_source_sent_to_provider": False,
            "raw_logs_sent_to_provider": False,
            "notes": [
                "This profile is derived from sanitized artifacts only.",
                "Source context explains expected structure, not incident truth.",
            ],
        },
    }
    return redact_mapping(_normalize_focused_profile(focused), RedactionCounter())


def build_focused_profile_with_provider(
    discovery: dict[str, Any],
    provider: ModelProvider,
    *,
    evidence_bundle: dict[str, Any] | None = None,
    source_context: dict[str, Any] | None = None,
    source_analysis: dict[str, Any] | None = None,
) -> dict[str, Any]:
    base = build_focused_profile(
        discovery,
        evidence_bundle=evidence_bundle,
        source_context=source_context,
        source_analysis=source_analysis,
    )
    model_input = _focused_profile_model_input(
        discovery,
        evidence_bundle=evidence_bundle,
        source_context=source_context,
        source_analysis=source_analysis,
    )
    metadata: dict[str, Any] = {
        "schema_version": FOCUSED_PROFILE_GENERATION_SCHEMA_VERSION,
        "generation_mode": "gemini_focused_operational_profile",
        "provider_id": getattr(provider, "provider", ""),
        "model_name": getattr(provider, "model_name", ""),
        "prompt_name": getattr(provider, "prompt_name", ""),
        "llm_status": "not_started",
        "fallback_used": False,
        "source_discovery_sha256": discovery.get("discovery_sha256") or "",
        "source_evidence_sha256": (evidence_bundle or {}).get("evidence_sha256") if isinstance(evidence_bundle, dict) else "",
        "source_context_sha256": (source_context or {}).get("source_context_sha256") if isinstance(source_context, dict) else "",
        "source_analysis_sha256": (source_analysis or {}).get("analysis_sha256") if isinstance(source_analysis, dict) else "",
        "model_input_sha256": sha256_json(model_input),
    }
    preflight = safety_preflight_for_model_input(model_input, filename="focused_profile_model_input.json")
    if not preflight.passed:
        metadata.update(
            {
                "llm_status": "blocked_by_safety_preflight",
                "fallback_used": True,
                "failure_reason": preflight.failure_reason,
                "finding_types": list(preflight.finding_types),
                "finding_count": preflight.finding_count,
            }
        )
        return _with_focused_profile_generation_metadata(base, metadata)

    run = run_provider_with_retries(provider, model_input)
    metadata.update(run.retry_metadata())
    metadata["llm_status"] = run.response.status
    metadata["raw_output_sha256"] = sha256_text(run.response.raw_output)
    if run.response.status != "ok":
        metadata.update(
            {
                "fallback_used": True,
                "failure_reason": run.failure_reason or run.response.status,
                "provider_error_message": _provider_error_message_from_raw_output(run.response.raw_output),
            }
        )
        return _with_focused_profile_generation_metadata(base, metadata)

    from ops_evidence_synthesis.synthesis.output_ingest import parse_model_output

    parsed = parse_model_output(run.response.raw_output)
    metadata.update(
        {
            "parse_status": parsed.parse_status,
            "repair_applied": parsed.repair_applied,
            "repair_rules": list(parsed.repair_rules),
            "repaired_output_sha256": parsed.repaired_output_sha256,
        }
    )
    if parsed.parsed is None:
        metadata.update(
            {
                "fallback_used": True,
                "failure_reason": "invalid_focused_profile_json",
                "parse_errors": [safe_provider_error_message(error, max_chars=240) for error in parsed.parse_errors],
            }
        )
        return _with_focused_profile_generation_metadata(base, metadata)

    metadata["parsed_json_sha256"] = sha256_json(parsed.parsed)
    normalized = _focused_profile_from_ai_payload(parsed.parsed, base)
    metadata["llm_status"] = "ok"
    metadata["fallback_used"] = False
    return _with_focused_profile_generation_metadata(normalized, metadata)


def _focused_component_rows(discovery: dict[str, Any], source_analysis: dict[str, Any]) -> list[dict[str, Any]]:
    rows = _merge_candidates(
        [row for row in discovery.get("component_candidates") or [] if isinstance(row, dict)],
        _source_analysis_rows(source_analysis, "component_candidates"),
        key_fields=("name",),
    )
    return _rank_focused_rows(rows, limit=12)


def _focused_metric_rows(discovery: dict[str, Any], source_analysis: dict[str, Any]) -> list[dict[str, Any]]:
    rows = _merge_candidates(
        [row for row in discovery.get("metric_semantics_candidates") or [] if isinstance(row, dict)],
        _source_analysis_rows(source_analysis, "metric_semantics_candidates"),
        key_fields=("metric_name",),
    )
    return _rank_focused_rows(rows, limit=20)


def _focused_collector_rows(discovery: dict[str, Any], source_analysis: dict[str, Any]) -> list[dict[str, Any]]:
    rows = _merge_candidates(
        [row for row in discovery.get("collector_mapping_candidates") or [] if isinstance(row, dict)],
        _source_analysis_rows(source_analysis, "collector_mapping_candidates"),
        key_fields=("request_type",),
    )
    return _rank_focused_rows(rows, limit=10)


def _focused_log_sources(
    discovery: dict[str, Any],
    evidence_bundle: dict[str, Any],
    source_analysis: dict[str, Any],
) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in _draft_log_sources(discovery):
        source_id = str(row.get("source_id") or row.get("id") or "").strip()
        if not source_id or source_id in seen:
            continue
        seen.add(source_id)
        output.append(
            {
                "source": _sanitize_discovery_text(source_id, RedactionCounter()),
                "meaning": _sanitize_discovery_text(str(row.get("description") or "Sanitized discovered log source."), RedactionCounter()),
                "evidence_refs": _refs_from_row(row),
                "source_context_refs": _source_refs_from_row(row),
            }
        )
    for item in evidence_bundle.get("evidence_items") or []:
        if not isinstance(item, dict):
            continue
        source = str(item.get("source") or item.get("component") or item.get("event_type") or "").strip()
        if not source or source in seen:
            continue
        seen.add(source)
        output.append(
            {
                "source": _sanitize_discovery_text(source, RedactionCounter()),
                "meaning": "Sanitized runtime evidence source.",
                "evidence_refs": _refs_from_row(item),
                "source_context_refs": [],
            }
        )
        if len(output) >= 12:
            break
    for row in _rank_focused_rows(_source_analysis_rows(source_analysis, "logger_mapping_candidates"), limit=12):
        source = str(row.get("logger") or row.get("name") or row.get("source") or "").strip()
        if not source or source in seen:
            continue
        seen.add(source)
        output.append(
            {
                "source": _sanitize_discovery_text(source, RedactionCounter()),
                "meaning": _sanitize_discovery_text(str(row.get("meaning") or row.get("role") or "Sanitized source logger mapping."), RedactionCounter()),
                "evidence_refs": [],
                "source_context_refs": _source_refs_from_row(row),
            }
        )
        if len(output) >= 12:
            break
    return output


def _focused_primary_purpose(
    project_summary: dict[str, Any],
    components: list[dict[str, Any]],
    log_sources: list[dict[str, Any]],
) -> str:
    summary = str(project_summary.get("summary") or project_summary.get("purpose") or "").strip()
    if summary:
        return _sanitize_discovery_text(summary, RedactionCounter())
    names = _unique_strings([row.get("name") for row in components[:5]], limit=5)
    if names:
        return f"Operate and monitor runtime components: {', '.join(names)}."
    sources = _unique_strings([row.get("source") for row in log_sources[:5]], limit=5)
    if sources:
        return f"Operate and monitor sanitized log sources: {', '.join(sources)}."
    return "Operate a sanitized system whose explicit purpose requires human confirmation."


def _focused_logged_subject(log_sources: list[dict[str, Any]], metrics: list[dict[str, Any]]) -> str:
    log_names = _unique_strings([row.get("source") for row in log_sources[:4]], limit=4)
    metric_names = _unique_strings([row.get("metric_name") or row.get("name") for row in metrics[:4]], limit=4)
    if log_names and metric_names:
        return f"Logs: {', '.join(log_names)}. Metrics: {', '.join(metric_names)}."
    if log_names:
        return f"Logs: {', '.join(log_names)}."
    if metric_names:
        return f"Metrics: {', '.join(metric_names)}."
    return "Sanitized operational logs, metrics, and source-derived instrumentation candidates."


def _focused_state_files(source_context: dict[str, Any], source_analysis: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for field in ("config_items", "source_items"):
        for row in source_context.get(field) or []:
            if isinstance(row, dict) and _row_has_any(row, ("state", "pid", "json", "cache", "checkpoint", "lock")):
                rows.append(row)
    for row in source_analysis.get("instrumentation_candidates") or []:
        if isinstance(row, dict) and _row_has_any(row, ("state", "pid", "checkpoint", "cache", "file")):
            rows.append(row)
    output: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in _rank_focused_rows(rows, limit=10):
        name = str(row.get("path") or row.get("name") or row.get("file") or row.get("source_id") or "").strip()
        if not name or name in seen:
            continue
        seen.add(name)
        output.append(
            {
                "name": _sanitize_discovery_text(name, RedactionCounter()),
                "meaning": _sanitize_discovery_text(str(row.get("meaning") or row.get("description") or "Source-derived state/config artifact."), RedactionCounter()),
                "evidence_refs": [],
                "source_context_refs": _source_refs_from_row(row),
            }
        )
    return output


def _focused_orchestration_flows(
    components: list[dict[str, Any]],
    collectors: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    orchestration_components = [
        row for row in components if _row_has_any(row, ("watchdog", "orchestr", "recover", "restart", "scheduler", "timer", "worker"))
    ]
    if not orchestration_components and not collectors:
        return []
    names = _unique_strings([row.get("name") or row.get("component_id") for row in orchestration_components[:6]], limit=6)
    collector_names = _unique_strings([row.get("request_type") or row.get("collector") for row in collectors[:4]], limit=4)
    flow_name = "Operational Recovery Loop" if names else "Read-only Evidence Collection Loop"
    steps = _unique_strings(
        [
            "Observe sanitized logs, metrics, or state signals.",
            "Compare runtime signals with source-derived component and metric expectations.",
            "Route missing evidence to read-only collectors or human review.",
            "Keep final operational action behind human approval.",
        ],
        limit=8,
    )
    return [
        {
            "flow_name": flow_name,
            "trigger": "Runtime signal, watchdog event, scheduled check, or reviewer request.",
            "steps": steps,
            "owned_by_components": names or collector_names,
            "evidence_refs": _unique_strings([ref for row in orchestration_components for ref in _refs_from_row(row)], limit=12),
            "source_context_refs": _unique_strings([ref for row in orchestration_components for ref in _source_refs_from_row(row)], limit=12),
            "confidence": _average([row.get("confidence") for row in orchestration_components]) or 0.6,
        }
    ]


def _focused_failure_modes(metrics: list[dict[str, Any]], components: list[dict[str, Any]]) -> list[dict[str, Any]]:
    modes: list[dict[str, Any]] = []
    for row in metrics[:8]:
        name = str(row.get("metric_name") or row.get("name") or "")
        if not name:
            continue
        modes.append(
            {
                "failure_mode": f"{name} outside expected direction",
                "observable_signals": [name],
                "missing_evidence": ["Human must confirm metric semantics before treating this as support evidence."],
                "confidence": _safe_float(row.get("confidence"), default=0.5),
            }
        )
    for row in components[:4]:
        name = str(row.get("name") or row.get("component_id") or "")
        if not name or not _row_has_any(row, ("watchdog", "recover", "restart", "service", "worker")):
            continue
        modes.append(
            {
                "failure_mode": f"{name} liveness or orchestration gap",
                "observable_signals": _unique_strings([*_refs_from_row(row), *_source_refs_from_row(row)], limit=8),
                "missing_evidence": ["Confirm runtime state and user impact with read-only evidence."],
                "confidence": _safe_float(row.get("confidence"), default=0.5),
            }
        )
    return modes[:12]


def _metric_meaning(row: dict[str, Any]) -> str:
    semantics = row.get("suggested_semantics") if isinstance(row.get("suggested_semantics"), dict) else {}
    return str(
        row.get("meaning")
        or row.get("description")
        or semantics.get("semantic_type")
        or row.get("semantic_type")
        or "Candidate operational metric."
    )


def _metric_healthy_direction(row: dict[str, Any]) -> str:
    semantics = row.get("suggested_semantics") if isinstance(row.get("suggested_semantics"), dict) else row
    zero = str(semantics.get("zero_behavior") or "").casefold()
    increase = str(semantics.get("increase_behavior") or "").casefold()
    decrease = str(semantics.get("decrease_behavior") or "").casefold()
    if "healthy" in increase:
        return "increase"
    if "healthy" in decrease:
        return "decrease"
    if "healthy" in zero:
        return "zero"
    if "suspicious" in zero or "unhealthy" in zero:
        return "nonzero"
    return "unknown"


def _refs_from_row(row: dict[str, Any]) -> list[str]:
    values: list[Any] = []
    for key in ("evidence_refs", "evidence_ids", "support_evidence_refs", "counter_evidence_refs"):
        values.extend(row.get(key) or [])
    for key in ("evidence_id", "pattern_id", "metric_window_id", "signal_id"):
        if row.get(key):
            values.append(row.get(key))
    return _unique_strings(values, limit=12)


def _source_refs_from_row(row: dict[str, Any]) -> list[str]:
    values: list[Any] = []
    for key in ("source_context_refs", "source_refs", "source_item_refs", "config_refs", "source_ids"):
        values.extend(row.get(key) or [])
    for key in ("source_id", "config_id", "unit_name", "path", "file", "name", "metric_name", "request_type"):
        if row.get(key):
            values.append(row.get(key))
    return _unique_strings(values, limit=12)


def _rank_focused_rows(rows: list[dict[str, Any]], *, limit: int) -> list[dict[str, Any]]:
    typed = [row for row in rows if isinstance(row, dict)]
    return sorted(typed, key=lambda row: (-_focused_row_score(row), str(row.get("name") or row.get("metric_name") or "")))[:limit]


def _focused_row_score(row: dict[str, Any]) -> float:
    text = json.dumps(row, ensure_ascii=False, sort_keys=True).casefold()
    score = 0.0
    for keyword in (
        "systemd",
        ".service",
        "watchdog",
        "orchestr",
        "recovery",
        "restart",
        "collector",
        "exporter",
        "logger",
        "metric",
        "heartbeat",
        "freshness",
        "state",
        "pid",
        "queue",
        "pubsub",
        "scheduler",
        "worker",
        "daemon",
        "rtmp",
        "rtmps",
        "ffmpeg",
        "stream",
        "audio",
        "service",
        "health",
        "liveness",
    ):
        if keyword in text:
            score += 4.0
    score += _safe_float(row.get("confidence"), default=0.0)
    try:
        score += min(float(row.get("count") or 0), 10.0)
    except (TypeError, ValueError):
        pass
    return score


def _row_has_any(row: dict[str, Any], needles: Iterable[str]) -> bool:
    text = json.dumps(row, ensure_ascii=False, sort_keys=True).casefold()
    return any(needle in text for needle in needles)


def _bounded_confidence(value: Any, *, default: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = default
    return max(0.0, min(1.0, number))


def _profile_draft_gemini_provider(*, model_name: str = "") -> VertexGeminiProvider:
    model = (
        model_name.strip()
        or os.environ.get("OES_PROFILE_DRAFT_GEMINI_MODEL", "").strip()
        or DEFAULT_PROFILE_DRAFT_GEMINI_MODEL
    )
    return VertexGeminiProvider.from_env(
        prompt_name="profile-draft",
        model_name=model,
        max_output_tokens=_int_env("OES_PROFILE_DRAFT_GEMINI_MAX_OUTPUT_TOKENS", 8192),
        timeout_seconds=_int_env("OES_PROFILE_DRAFT_GEMINI_TIMEOUT_SECONDS", 180),
    )


def _focused_profile_gemini_provider(*, model_name: str = "") -> VertexGeminiProvider:
    model = (
        model_name.strip()
        or os.environ.get("OES_FOCUSED_PROFILE_GEMINI_MODEL", "").strip()
        or os.environ.get("OES_PROFILE_DRAFT_GEMINI_MODEL", "").strip()
        or DEFAULT_FOCUSED_PROFILE_GEMINI_MODEL
    )
    return VertexGeminiProvider.from_env(
        prompt_name="focused-operational-profile",
        model_name=model,
        max_output_tokens=_int_env("OES_FOCUSED_PROFILE_GEMINI_MAX_OUTPUT_TOKENS", 14000),
        timeout_seconds=_int_env("OES_FOCUSED_PROFILE_GEMINI_TIMEOUT_SECONDS", 240),
    )


def _profile_draft_model_input(discovery: dict[str, Any]) -> dict[str, Any]:
    from ops_evidence_synthesis.ai.prompts import compact_profile_discovery_for_model

    return {
        "llm_task": "profile_draft",
        "schema_version": "profile_draft_model_input.v1",
        "profile_discovery": compact_profile_discovery_for_model(discovery),
        "profile_draft_policy": {
            "raw_source_sent_to_provider": False,
            "raw_env_values_sent_to_provider": False,
            "raw_logs_sent_to_provider": False,
            "input_artifact": "sanitized_profile_discovery_bundle",
            "draft_requires_human_approval": True,
            "source_context_is_incident_evidence": False,
            "support_claims_must_cite_evidence_id": True,
        },
    }


def _focused_profile_model_input(
    discovery: dict[str, Any],
    *,
    evidence_bundle: dict[str, Any] | None = None,
    source_context: dict[str, Any] | None = None,
    source_analysis: dict[str, Any] | None = None,
) -> dict[str, Any]:
    from ops_evidence_synthesis.ai.prompts import compact_focused_profile_input_for_model

    raw_input = {
        "llm_task": "focused_operational_profile",
        "schema_version": FOCUSED_PROFILE_MODEL_INPUT_SCHEMA_VERSION,
        "profile_discovery": discovery,
        "evidence_bundle": evidence_bundle if isinstance(evidence_bundle, dict) else {},
        "source_context": source_context if isinstance(source_context, dict) else {},
        "source_analysis": source_analysis if isinstance(source_analysis, dict) else {},
        "focused_profile_policy": {
            "raw_source_sent_to_provider": False,
            "raw_env_values_sent_to_provider": False,
            "raw_logs_sent_to_provider": False,
            "input_artifacts": [
                "sanitized_profile_discovery_bundle",
                "sanitized_evidence_bundle",
                "sanitized_source_context",
                "sanitized_source_analysis",
            ],
            "source_context_is_incident_evidence": False,
            "runtime_claims_must_cite_evidence_id": True,
            "draft_requires_human_approval": True,
            "collector_mappings_must_be_read_only": True,
            "profile_focus": [
                "what_system_is_this",
                "what_is_logged_or_measured",
                "which_runtime_components_matter",
                "what_orchestration_or_watchdog_loop_exists",
            ],
        },
    }
    model_input = compact_focused_profile_input_for_model(raw_input)
    model_input["llm_task"] = "focused_operational_profile"
    model_input["schema_version"] = FOCUSED_PROFILE_MODEL_INPUT_SCHEMA_VERSION
    model_input["focused_profile_policy"] = raw_input["focused_profile_policy"]
    return model_input


def _profile_draft_from_ai_payload(
    payload: dict[str, Any],
    discovery: dict[str, Any],
    base: dict[str, Any],
) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return base
    profile_payload = payload.get("profile") if isinstance(payload.get("profile"), dict) else payload
    draft = json.loads(json.dumps(base, ensure_ascii=False))
    profile = draft.setdefault("profile", {})
    if isinstance(profile_payload.get("system_type"), str) and profile_payload["system_type"].strip():
        profile["system_type"] = _sanitize_discovery_text(profile_payload["system_type"], RedactionCounter())
    if isinstance(profile_payload.get("purpose"), str) and profile_payload["purpose"].strip():
        profile["purpose"] = _sanitize_discovery_text(profile_payload["purpose"], RedactionCounter())

    critical = _string_list(profile_payload.get("critical_outcomes") or profile_payload.get("critical_user_outcomes"))
    if critical:
        profile["critical_outcomes"] = critical[:12]

    ai_components = _component_map_from_ai(profile_payload.get("components") or profile_payload.get("component_map"))
    if ai_components:
        profile["component_map"] = {**profile.get("component_map", {}), **ai_components}

    ai_metrics = _metric_semantics_from_ai(profile_payload.get("metric_semantics"))
    if ai_metrics:
        profile["metric_semantics"] = {**profile.get("metric_semantics", {}), **ai_metrics}

    ai_collectors = _collector_mappings_from_ai(profile_payload.get("collector_mappings"))
    if ai_collectors:
        profile["collector_mappings"] = {**profile.get("collector_mappings", {}), **ai_collectors}

    ai_sources = _log_sources_from_ai(profile_payload.get("log_sources"))
    if ai_sources:
        profile["log_sources"] = _merge_log_sources(profile.get("log_sources") or [], ai_sources)

    profile["known_benign_noise"] = _unique_strings(
        [*list(profile.get("known_benign_noise") or []), *_string_list(profile_payload.get("known_benign_noise"))],
        limit=30,
    )
    profile["action_constraints"] = _unique_strings(
        [
            *list(profile.get("action_constraints") or []),
            *_string_list(profile_payload.get("action_constraints")),
            "This AI-generated profile draft is context only until human approval.",
            "Runtime support claims must cite evidence_id from Evidence Items.",
            "Collector mappings are read-only until human approved.",
        ],
        limit=40,
    )
    draft["assumptions"] = _unique_strings(
        [
            *list(draft.get("assumptions") or []),
            *_string_list(profile_payload.get("assumptions")),
            "Gemini analyzed sanitized Profile Discovery context, not raw source or raw logs.",
        ],
        limit=30,
    )
    draft["required_human_decisions"] = _unique_strings(
        [
            *list(draft.get("required_human_decisions") or []),
            *_string_list(profile_payload.get("required_human_decisions")),
            "Approve or edit Gemini-generated profile fields before incident review.",
        ],
        limit=30,
    )
    summary = draft.setdefault("display_summary", {})
    badges = list(summary.get("primary_badges") or [])
    summary["primary_badges"] = _unique_strings(
        [*badges, "profile_draft_provider:gemini", "source_context:sanitized", "human_approval:required"],
        limit=12,
    )
    draft["human_review_required"] = True
    draft["approved"] = False
    draft["explicit_profile"] = False
    draft["source_discovery_sha256"] = discovery.get("discovery_sha256") or draft.get("source_discovery_sha256") or ""
    return redact_mapping(draft, RedactionCounter())


def _focused_profile_from_ai_payload(payload: dict[str, Any], base: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return base
    normalized = json.loads(json.dumps(payload, ensure_ascii=False))
    for key in (
        "source_discovery_sha256",
        "source_evidence_sha256",
        "source_context_sha256",
        "source_analysis_sha256",
    ):
        if not normalized.get(key) and base.get(key):
            normalized[key] = base.get(key)
    if not normalized.get("system_label"):
        normalized["system_label"] = base.get("system_label") or "discovered-system"
    return redact_mapping(_normalize_focused_profile(normalized), RedactionCounter())


def _normalize_focused_profile(profile: dict[str, Any]) -> dict[str, Any]:
    output = json.loads(json.dumps(profile, ensure_ascii=False))
    output["schema_version"] = FOCUSED_PROFILE_SCHEMA_VERSION
    output["system_label"] = _sanitize_discovery_text(str(output.get("system_label") or "discovered-system"), RedactionCounter())

    summary = output.get("system_summary") if isinstance(output.get("system_summary"), dict) else {}
    output["system_summary"] = {
        "system_type": _sanitize_discovery_text(str(summary.get("system_type") or "operational_service"), RedactionCounter()),
        "primary_purpose": _sanitize_discovery_text(str(summary.get("primary_purpose") or "Human-reviewed operational profile candidate."), RedactionCounter()),
        "logged_subject": _sanitize_discovery_text(str(summary.get("logged_subject") or "Sanitized operational logs and metrics."), RedactionCounter()),
        "operational_boundary": _sanitize_discovery_text(
            str(summary.get("operational_boundary") or "Source context is interpretation context, not incident evidence."),
            RedactionCounter(),
        ),
        "confidence": _bounded_confidence(summary.get("confidence"), default=0.6),
    }

    output["runtime_components"] = [
        {
            "component_id": _slug(str(row.get("component_id") or row.get("name") or f"component_{index:03d}")),
            "name": _sanitize_discovery_text(str(row.get("name") or row.get("component_id") or f"component_{index:03d}"), RedactionCounter()),
            "role": _sanitize_discovery_text(str(row.get("role") or "Candidate runtime component."), RedactionCounter()),
            "evidence_refs": _string_list(row.get("evidence_refs"))[:12],
            "source_context_refs": _string_list(row.get("source_context_refs"))[:12],
            "confidence": _bounded_confidence(row.get("confidence"), default=0.6),
        }
        for index, row in enumerate(_rows_from_ai(output.get("runtime_components"))[:12], start=1)
    ]

    contract = output.get("observability_contract") if isinstance(output.get("observability_contract"), dict) else {}
    output["observability_contract"] = {
        "logs": [_normalize_named_ref(row, name_key="source") for row in _rows_from_ai(contract.get("logs"))[:12]],
        "metrics": [_normalize_metric_ref(row) for row in _rows_from_ai(contract.get("metrics"))[:20]],
        "heartbeats": [_normalize_named_ref(row, name_key="name") for row in _rows_from_ai(contract.get("heartbeats"))[:10]],
        "state_files": [_normalize_named_ref(row, name_key="name") for row in _rows_from_ai(contract.get("state_files"))[:10]],
    }
    output["orchestration_flows"] = [
        {
            "flow_name": _sanitize_discovery_text(str(row.get("flow_name") or row.get("name") or f"flow_{index:03d}"), RedactionCounter()),
            "trigger": _sanitize_discovery_text(str(row.get("trigger") or ""), RedactionCounter()),
            "steps": _string_list(row.get("steps"))[:12],
            "owned_by_components": _string_list(row.get("owned_by_components"))[:12],
            "evidence_refs": _string_list(row.get("evidence_refs"))[:12],
            "source_context_refs": _string_list(row.get("source_context_refs"))[:12],
            "confidence": _bounded_confidence(row.get("confidence"), default=0.6),
        }
        for index, row in enumerate(_rows_from_ai(output.get("orchestration_flows"))[:8], start=1)
    ]
    output["failure_modes"] = [
        {
            "failure_mode": _sanitize_discovery_text(str(row.get("failure_mode") or row.get("name") or f"failure_mode_{index:03d}"), RedactionCounter()),
            "observable_signals": _string_list(row.get("observable_signals"))[:12],
            "missing_evidence": _string_list(row.get("missing_evidence"))[:12],
            "confidence": _bounded_confidence(row.get("confidence"), default=0.5),
        }
        for index, row in enumerate(_rows_from_ai(output.get("failure_modes"))[:12], start=1)
    ]
    output["read_only_collectors"] = [
        {
            "collector": _sanitize_discovery_text(str(row.get("collector") or row.get("request_type") or f"collector_{index:03d}"), RedactionCounter()),
            "purpose": _sanitize_discovery_text(str(row.get("purpose") or row.get("description") or "Collect read-only evidence."), RedactionCounter()),
            "safety_level": "read_only",
        }
        for index, row in enumerate(_rows_from_ai(output.get("read_only_collectors"))[:10], start=1)
    ]
    limits = output.get("profile_limits") if isinstance(output.get("profile_limits"), dict) else {}
    output["profile_limits"] = {
        "source_context_is_incident_evidence": False,
        "runtime_claims_require_evidence_id": True,
        "approval_required_before_explicit_profile": True,
        "raw_source_sent_to_provider": False,
        "raw_logs_sent_to_provider": False,
        "notes": _unique_strings(
            [
                *_string_list(limits.get("notes")),
                "This profile is derived from sanitized artifacts only.",
                "Source context explains expected structure, not incident truth.",
            ],
            limit=12,
        ),
    }
    output["human_review_required"] = _unique_strings(
        [
            *_string_list(output.get("human_review_required")),
            "Approve or edit the focused profile before using it as an explicit system profile.",
        ],
        limit=20,
    )
    return output


def _normalize_named_ref(row: dict[str, Any], *, name_key: str) -> dict[str, Any]:
    name = str(row.get(name_key) or row.get("name") or row.get("source") or "")
    return {
        name_key: _sanitize_discovery_text(name, RedactionCounter()),
        "meaning": _sanitize_discovery_text(str(row.get("meaning") or ""), RedactionCounter()),
        "evidence_refs": _string_list(row.get("evidence_refs"))[:12],
        "source_context_refs": _string_list(row.get("source_context_refs"))[:12],
    }


def _normalize_metric_ref(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "metric_name": _sanitize_discovery_text(str(row.get("metric_name") or row.get("name") or ""), RedactionCounter()),
        "meaning": _sanitize_discovery_text(str(row.get("meaning") or ""), RedactionCounter()),
        "healthy_direction": str(row.get("healthy_direction") or "unknown"),
        "evidence_refs": _string_list(row.get("evidence_refs"))[:12],
        "source_context_refs": _string_list(row.get("source_context_refs"))[:12],
    }


def _with_profile_generation_metadata(draft: dict[str, Any], metadata: dict[str, Any]) -> dict[str, Any]:
    output = json.loads(json.dumps(draft, ensure_ascii=False))
    output["profile_generation"] = redact_mapping(metadata, RedactionCounter())
    summary = output.setdefault("display_summary", {})
    badges = list(summary.get("primary_badges") or [])
    status = str(metadata.get("llm_status") or "unknown")
    mode = str(metadata.get("generation_mode") or "unknown")
    if metadata.get("fallback_used"):
        badges.append("profile_draft:fallback_used")
    badges.extend([f"generation:{mode}", f"llm_status:{status}"])
    summary["primary_badges"] = _unique_strings(badges, limit=14)
    return redact_mapping(output, RedactionCounter())


def _with_focused_profile_generation_metadata(profile: dict[str, Any], metadata: dict[str, Any]) -> dict[str, Any]:
    output = json.loads(json.dumps(profile, ensure_ascii=False))
    output["focused_profile_generation"] = redact_mapping(metadata, RedactionCounter())
    notes = output.setdefault("profile_limits", {}).setdefault("notes", [])
    if isinstance(notes, list):
        status = str(metadata.get("llm_status") or "unknown")
        mode = str(metadata.get("generation_mode") or "unknown")
        notes.extend([f"generation:{mode}", f"llm_status:{status}"])
        if metadata.get("fallback_used"):
            notes.append("focused_profile:fallback_used")
        output["profile_limits"]["notes"] = _unique_strings(notes, limit=16)
    return redact_mapping(_normalize_focused_profile(output), RedactionCounter())


def _component_map_from_ai(value: Any) -> dict[str, dict[str, Any]]:
    components: dict[str, dict[str, Any]] = {}
    for index, row in enumerate(_rows_from_ai(value), start=1):
        name = str(row.get("name") or row.get("component_name") or row.get("component_id") or "").strip()
        component_id = _slug(str(row.get("component_id") or name or f"component_{index:03d}"))
        if not component_id:
            continue
        components[component_id] = {
            "name": _sanitize_discovery_text(name or component_id, RedactionCounter()),
            "role": _sanitize_discovery_text(str(row.get("role") or row.get("suggested_role") or "Candidate component"), RedactionCounter()),
            "subsystem": _slug(str(row.get("subsystem") or row.get("suggested_subsystem") or "general")),
            "core_target_types": _string_list(row.get("core_target_types") or row.get("suggested_core_target_types"))[:8],
            "confidence": _safe_float(row.get("confidence"), default=0.7),
            "generation_source": "gemini_profile_draft",
            "human_review_required": True,
        }
    return components


def _metric_semantics_from_ai(value: Any) -> dict[str, dict[str, Any]]:
    metrics: dict[str, dict[str, Any]] = {}
    for row in _rows_from_ai(value):
        name = str(row.get("metric_name") or row.get("name") or "").strip()
        if not name:
            continue
        metrics[_sanitize_discovery_text(name, RedactionCounter())] = {
            "semantic_type": str(row.get("semantic_type") or "candidate"),
            "zero_behavior": str(row.get("zero_behavior") or "unknown"),
            "increase_behavior": str(row.get("increase_behavior") or "unknown"),
            "decrease_behavior": str(row.get("decrease_behavior") or "unknown"),
            "subsystem": _slug(str(row.get("subsystem") or "general")),
            "core_target_type": _slug(str(row.get("core_target_type") or row.get("candidate_core_target_type") or "general")),
            "confidence": _safe_float(row.get("confidence"), default=0.7),
            "generation_source": "gemini_profile_draft",
            "human_review_required": True,
        }
    return metrics


def _collector_mappings_from_ai(value: Any) -> dict[str, dict[str, Any]]:
    collectors: dict[str, dict[str, Any]] = {}
    for index, row in enumerate(_rows_from_ai(value), start=1):
        request_type = _slug(str(row.get("request_type") or row.get("need") or f"collector_{index:03d}"))
        if not request_type:
            continue
        collectors[request_type] = {
            "candidate_collectors": _string_list(row.get("candidate_collectors") or row.get("collectors"))[:8],
            "params": row.get("params") if isinstance(row.get("params"), dict) else {},
            "safety_level": "read_only",
            "generation_source": "gemini_profile_draft",
            "human_review_required": True,
        }
    return collectors


def _log_sources_from_ai(value: Any) -> list[dict[str, str]]:
    sources: list[dict[str, str]] = []
    for index, row in enumerate(_rows_from_ai(value), start=1):
        source_id = _slug(str(row.get("source_id") or row.get("id") or row.get("name") or f"log_source_{index:03d}"))
        description = str(row.get("description") or row.get("meaning") or "Candidate log source derived from sanitized context.")
        if source_id:
            sources.append(
                {
                    "source_id": source_id,
                    "description": _sanitize_discovery_text(description, RedactionCounter()),
                    "generation_source": "gemini_profile_draft",
                }
            )
    return sources


def _merge_log_sources(base: list[Any], generated: list[dict[str, str]]) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for row in [*base, *generated]:
        if not isinstance(row, dict):
            continue
        source_id = _slug(str(row.get("source_id") or row.get("id") or row.get("name") or ""))
        if source_id and source_id not in merged:
            merged[source_id] = {**row, "source_id": source_id}
    return list(merged.values())[:30]


def _rows_from_ai(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return [row for row in value if isinstance(row, dict)]
    if isinstance(value, dict):
        rows = []
        for key, row in value.items():
            if isinstance(row, dict):
                rows.append({**row, "name": row.get("name") or str(key)})
        return rows
    return []


def _string_list(value: Any) -> list[str]:
    values = value if isinstance(value, list) else [value] if value not in (None, "") else []
    output: list[str] = []
    for item in values:
        text = _sanitize_discovery_text(str(item or "").strip(), RedactionCounter())
        if text:
            output.append(text)
    return _unique_strings(output, limit=100)


def _unique_strings(values: Iterable[Any], *, limit: int) -> list[str]:
    output: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        output.append(text)
        if len(output) >= limit:
            break
    return output


def _safe_float(value: Any, *, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _int_env(key: str, default: int) -> int:
    try:
        return int(os.environ.get(key, str(default)))
    except ValueError:
        return default


def _provider_error_message_from_raw_output(raw_output: str) -> str:
    try:
        payload = json.loads(raw_output)
    except json.JSONDecodeError:
        return safe_provider_error_message(raw_output, max_chars=500)
    if not isinstance(payload, dict):
        return safe_provider_error_message(raw_output, max_chars=500)
    message = payload.get("message") or payload.get("error") or payload.get("failure_reason") or ""
    return safe_provider_error_message(str(message), max_chars=500)


def _metrics_from_semantics(metric_semantics: dict[str, Any], component_map: dict[str, Any]) -> dict[str, Any]:
    output: dict[str, Any] = {}
    default_subsystem = "general"
    if component_map:
        first_component = next(iter(component_map.values()))
        if isinstance(first_component, dict) and first_component.get("subsystem"):
            default_subsystem = str(first_component["subsystem"])
    for metric_name, semantics in metric_semantics.items():
        if not isinstance(semantics, dict):
            continue
        output[str(metric_name)] = {
            "semantic_type": str(semantics.get("semantic_type") or "candidate"),
            "zero_behavior": str(semantics.get("zero_behavior") or "unknown"),
            "increase_behavior": str(semantics.get("increase_behavior") or "unknown"),
            "decrease_behavior": str(semantics.get("decrease_behavior") or "unknown"),
            "subsystem": str(semantics.get("subsystem") or default_subsystem),
            "core_target_type": str(semantics.get("candidate_core_target_type") or semantics.get("core_target_type") or "general"),
        }
    return output


def validate_profile_discovery_bundle_for_upload(bundle: dict[str, Any]) -> dict[str, Any]:
    errors: list[dict[str, Any]] = []
    if not isinstance(bundle, dict):
        return {
            "passed": False,
            "errors": [{"type": "invalid_payload", "field": "bundle"}],
            "findings": [],
            "expected_discovery_sha256": "",
            "actual_discovery_sha256": "",
        }
    summary = bundle.get("local_first_summary") if isinstance(bundle.get("local_first_summary"), dict) else {}
    required_values = {
        "schema_version": PROFILE_DISCOVERY_SCHEMA_VERSION,
        "bundle_type": PROFILE_DISCOVERY_BUNDLE_TYPE,
        "raw_config_policy": RAW_CONFIG_POLICY,
        "raw_logs_policy": RAW_LOGS_POLICY,
    }
    for field, expected in required_values.items():
        if bundle.get(field) != expected:
            errors.append({"type": "contract_mismatch", "field": field})
    if summary.get("raw_configs_uploaded") is not False:
        errors.append({"type": "contract_mismatch", "field": "local_first_summary.raw_configs_uploaded"})
    if summary.get("raw_logs_uploaded") is not False:
        errors.append({"type": "contract_mismatch", "field": "local_first_summary.raw_logs_uploaded"})
    for field in (
        "observed_entities",
        "project_entities",
        "entity_links",
        "component_candidates",
        "required_profile_questions",
        "local_first_summary",
        "source",
        "discovery_policy",
        "prompt_rules",
    ):
        if field not in bundle:
            errors.append({"type": "missing_field", "field": field})
    for field in ("observed_entities", "project_entities", "entity_links", "component_candidates", "prompt_rules"):
        if not isinstance(bundle.get(field), list):
            errors.append({"type": "contract_mismatch", "field": field})
    expected_sha = sha256_json(profile_discovery_hash_payload(bundle))
    actual_sha = str(bundle.get("discovery_sha256") or "")
    if expected_sha != actual_sha:
        errors.append({"type": "discovery_sha256_mismatch", "field": "discovery_sha256"})
    scan = scan_sanitized_text("profile_discovery_bundle.json", canonical_json(bundle))
    findings = list(scan["findings"])
    if findings:
        errors.append({"type": "unsafe_content", "field": "profile_discovery_bundle"})
    return {
        "passed": not errors,
        "errors": errors,
        "findings": findings,
        "expected_discovery_sha256": expected_sha,
        "actual_discovery_sha256": actual_sha,
    }


def profile_discovery_hash_payload(bundle: dict[str, Any]) -> dict[str, Any]:
    payload = {key: value for key, value in bundle.items() if key not in {"discovery_sha256", "created_at"}}
    summary = payload.get("local_first_summary")
    if isinstance(summary, dict):
        payload["local_first_summary"] = {
            key: value for key, value in summary.items() if key not in {"discovery_sha256"}
        }
    return payload


def extract_observed_entities(bundle: dict[str, Any], *, service: str, report: RedactionCounter) -> list[dict[str, Any]]:
    collector: dict[tuple[str, str], dict[str, Any]] = {}

    def add(name: Any, entity_type: str, *, seen_in: str, evidence_refs: Iterable[str] = (), confidence: float = 0.7) -> None:
        text = _sanitize_discovery_text(name, report)
        if not text or text in {"<PATH>", "<NUM>", "<TIMESTAMP>"}:
            return
        key = (entity_type, text.casefold())
        row = collector.setdefault(
            key,
            {
                "name": text,
                "entity_type": entity_type,
                "seen_in": set(),
                "evidence_refs": set(),
                "source": "evidence_bundle",
                "confidence": 0.0,
            },
        )
        row["seen_in"].add(seen_in)
        row["evidence_refs"].update(str(ref) for ref in evidence_refs if ref)
        row["confidence"] = max(float(row["confidence"]), confidence)

    source = bundle.get("source") if isinstance(bundle.get("source"), dict) else {}
    add(source.get("service") or service, "service_name", seen_in="source", confidence=0.9)
    for item in bundle.get("evidence_items") or []:
        if not isinstance(item, dict):
            continue
        evidence_ref = str(item.get("evidence_id") or "")
        component = item.get("component")
        if component:
            add(component, _classify_entity_name(str(component)), seen_in="evidence_items", evidence_refs=[evidence_ref], confidence=0.9)
        if item.get("event_type"):
            add(item["event_type"], "error_type", seen_in="evidence_items", evidence_refs=[evidence_ref], confidence=0.85)
        if item.get("source"):
            add(item["source"], "logger_name", seen_in="evidence_items", evidence_refs=[evidence_ref], confidence=0.55)
        for field in ("message_template", "example_sanitized"):
            text = str(item.get(field) or "")
            _extract_entities_from_text(text, add, seen_in="evidence_items", evidence_refs=[evidence_ref])
    for signal in bundle.get("signals") or []:
        if not isinstance(signal, dict):
            continue
        refs = [str(ref) for ref in signal.get("evidence_refs") or []]
        if signal.get("signal_type"):
            add(signal["signal_type"], "error_type", seen_in="signals", evidence_refs=refs, confidence=0.8)
        if signal.get("component"):
            add(signal["component"], _classify_entity_name(str(signal["component"])), seen_in="signals", evidence_refs=refs, confidence=0.85)

    rows: list[dict[str, Any]] = []
    for index, row in enumerate(
        sorted(collector.values(), key=lambda item: (str(item["entity_type"]), str(item["name"]))),
        start=1,
    ):
        rows.append(
            {
                "entity_id": f"OBS-{index:03d}",
                "name": row["name"],
                "entity_type": row["entity_type"],
                "seen_in": sorted(row["seen_in"]),
                "evidence_refs": sorted(row["evidence_refs"]),
                "source": "evidence_bundle",
                "confidence": round(float(row["confidence"]), 2),
            }
        )
    return rows


def discover_project_entities(
    project_root: Path,
    *,
    observed_entities: list[dict[str, Any]],
    report: RedactionCounter,
) -> list[dict[str, Any]]:
    files = list(_iter_project_files(project_root))
    entities: list[dict[str, Any]] = []
    tree_summary = {
        "entity_type": "file_tree_summary",
        "name": "file_tree",
        "relative_path": "",
        "attributes": {
            "file_count": len(files),
            "top_level": sorted({path.relative_to(project_root).parts[0] for path in files if path.relative_to(project_root).parts}),
        },
    }
    entities.append(tree_summary)
    for path in files:
        rel = _rel(path, project_root)
        basename = path.name
        suffix = path.suffix
        if basename.endswith(".service"):
            entities.append(_systemd_entity(path, project_root, report))
        if _is_env_file(path):
            entities.extend(_env_key_entities(path, project_root, report))
        if basename in DEPENDENCY_MANIFESTS or suffix in {".yaml", ".yml"} and "k8s" in rel.casefold():
            entities.append(_dependency_manifest_entity(path, project_root, report))
        if basename in ENTRYPOINT_NAMES:
            entities.append(
                {
                    "entity_type": "script_path",
                    "name": _sanitize_discovery_text(rel, report),
                    "relative_path": _sanitize_discovery_text(rel, report),
                    "attributes": {"entrypoint_candidate": True, "basename": basename},
                }
            )
        entities.append(
            {
                "entity_type": "file_path",
                "name": _sanitize_discovery_text(rel, report),
                "relative_path": _sanitize_discovery_text(rel, report),
                "attributes": {"basename": basename, "suffix": suffix},
            }
        )
    entities.extend(_code_reference_entities(project_root, files, observed_entities, report))
    unique = _unique_project_entities(entities)
    for index, row in enumerate(unique, start=1):
        row["project_entity_id"] = f"PRJ-{index:03d}"
        row["source"] = "project_discovery"
    return unique


def link_entities(observed_entities: list[dict[str, Any]], project_entities: list[dict[str, Any]]) -> list[dict[str, Any]]:
    links: list[dict[str, Any]] = []
    for obs in observed_entities:
        for prj in project_entities:
            score, match_type, features = _link_score(obs, prj)
            if score < 0.15:
                continue
            links.append(
                {
                    "observed_entity_id": obs["entity_id"],
                    "project_entity_id": prj["project_entity_id"],
                    "match_type": match_type,
                    "match_features": features,
                    "confidence": round(min(score, 0.98), 2),
                }
            )
    dedup: dict[tuple[str, str, str], dict[str, Any]] = {}
    for link in links:
        key = (link["observed_entity_id"], link["project_entity_id"], link["match_type"])
        current = dedup.get(key)
        if current is None or float(link["confidence"]) > float(current["confidence"]):
            dedup[key] = link
    rows = sorted(
        dedup.values(),
        key=lambda row: (-float(row["confidence"]), row["observed_entity_id"], row["project_entity_id"], row["match_type"]),
    )
    for index, row in enumerate(rows, start=1):
        row["link_id"] = f"LINK-{index:03d}"
    return rows


def build_component_candidates(
    observed_entities: list[dict[str, Any]],
    project_entities: list[dict[str, Any]],
    entity_links: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    obs_by_id = {row["entity_id"]: row for row in observed_entities}
    prj_by_id = {row["project_entity_id"]: row for row in project_entities}
    grouped: dict[str, dict[str, Any]] = {}
    for link in entity_links:
        obs = obs_by_id.get(str(link.get("observed_entity_id")))
        prj = prj_by_id.get(str(link.get("project_entity_id")))
        if not obs or not prj:
            continue
        name = _component_name(str(obs.get("name") or prj.get("name") or "component"))
        row = grouped.setdefault(
            name,
            {
                "name": name,
                "matched_entities": set(),
                "confidence": 0.0,
                "core_target_types": set(),
            },
        )
        row["matched_entities"].update([str(obs.get("name")), str(prj.get("name"))])
        row["confidence"] = max(float(row["confidence"]), float(link.get("confidence") or 0))
        row["core_target_types"].update(_core_targets_for_text(" ".join(row["matched_entities"])))
    for obs in observed_entities:
        if obs.get("entity_type") in {"systemd_unit", "component_name", "service_name"}:
            name = _component_name(str(obs.get("name") or "component"))
            row = grouped.setdefault(
                name,
                {
                    "name": name,
                    "matched_entities": set(),
                    "confidence": float(obs.get("confidence") or 0.5),
                    "core_target_types": set(),
                },
            )
            row["matched_entities"].add(str(obs.get("name")))
            row["core_target_types"].update(_core_targets_for_text(str(obs.get("name"))))
    candidates: list[dict[str, Any]] = []
    for index, (name, row) in enumerate(sorted(grouped.items()), start=1):
        text = " ".join(sorted(row["matched_entities"]))
        candidates.append(
            {
                "component_id": f"COMP-{index:03d}",
                "name": name,
                "matched_entities": sorted(entity for entity in row["matched_entities"] if entity),
                "suggested_role": _suggest_role(text),
                "suggested_subsystem": _suggest_subsystem(text),
                "suggested_core_target_types": sorted(row["core_target_types"] or _core_targets_for_text(text)),
                "confidence": round(float(row["confidence"]), 2),
                "human_review_required": True,
            }
        )
    return candidates


def build_metric_semantics_candidates(
    observed_entities: list[dict[str, Any]],
    project_entities: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    names = {
        str(row.get("name"))
        for row in observed_entities + project_entities
        if row.get("entity_type") in {"metric_name", "code_reference"} and _looks_like_metric(str(row.get("name") or ""))
    }
    rows: list[dict[str, Any]] = []
    for index, name in enumerate(sorted(names), start=1):
        rows.append(
            {
                "metric_semantics_id": f"METRIC-{index:03d}",
                "metric_name": name,
                "suggested_semantics": _metric_semantics_for_name(name),
                "confidence": 0.72,
                "human_review_required": True,
            }
        )
    return rows


def build_collector_mapping_candidates(
    observed_entities: list[dict[str, Any]],
    component_candidates: list[dict[str, Any]],
    metric_candidates: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    units = sorted({str(row.get("name")) for row in observed_entities if row.get("entity_type") == "systemd_unit"})
    process_names = sorted(
        {
            _basename(str(row.get("name")))
            for row in observed_entities
            if row.get("entity_type") in {"program_name", "process_name", "script_path"}
        }
    )
    rows: list[dict[str, Any]] = []
    if units or process_names:
        rows.append(
            {
                "request_type": "process_state_query",
                "candidate_collectors": ["local_systemd", "local_journal", "local_process"],
                "params": {"units": units, "process_names": process_names},
                "safety_level": "read_only",
                "human_review_required": True,
            }
        )
    if metric_candidates:
        rows.append(
            {
                "request_type": "throughput_signal_query",
                "candidate_collectors": ["local_metrics", "local_prometheus", "local_logs"],
                "params": {"metric_names": [row["metric_name"] for row in metric_candidates]},
                "safety_level": "read_only",
                "human_review_required": True,
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
            }
        )
    return rows


def build_external_dependency_candidates(observed_entities: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for index, row in enumerate(
        [item for item in observed_entities if item.get("entity_type") in {"endpoint", "domain"}],
        start=1,
    ):
        rows.append(
            {
                "dependency_id": f"DEP-{index:03d}",
                "name": row.get("name"),
                "entity_type": row.get("entity_type"),
                "confidence": row.get("confidence"),
                "human_review_required": True,
            }
        )
    return rows


def detect_project_type(project_entities: list[dict[str, Any]]) -> str:
    names = {str(row.get("name") or "") for row in project_entities}
    types = {str(row.get("entity_type") or "") for row in project_entities}
    if "systemd_unit" in types:
        return "systemd_service"
    if any(name.endswith("package.json") for name in names):
        return "node_service"
    if any(name.endswith("pyproject.toml") or name.endswith("requirements.txt") for name in names):
        return "python_project"
    if any("Dockerfile" in name or "compose" in name for name in names):
        return "containerized_service"
    return "generic"


def _load_json(path: str | Path | None) -> dict[str, Any]:
    if not path:
        return {}
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    return payload if isinstance(payload, dict) else {}


def _source_analysis_rows(source_analysis: dict[str, Any], field: str) -> list[dict[str, Any]]:
    return [row for row in source_analysis.get(field) or [] if isinstance(row, dict)]


def _merge_candidates(
    base_rows: list[dict[str, Any]],
    extra_rows: list[dict[str, Any]],
    *,
    key_fields: tuple[str, ...],
) -> list[dict[str, Any]]:
    merged: dict[tuple[str, ...], dict[str, Any]] = {}
    for row in [*base_rows, *extra_rows]:
        key = tuple(str(row.get(field) or "").casefold() for field in key_fields)
        if not any(key):
            key = (json.dumps(row, ensure_ascii=False, sort_keys=True),)
        current = merged.get(key)
        candidate = dict(row)
        candidate["human_review_required"] = True
        if "context_is_not_incident_evidence" in candidate or extra_rows:
            candidate["context_is_not_incident_evidence"] = True
        if current is None:
            merged[key] = candidate
            continue
        current_conf = float(current.get("confidence") or 0)
        new_conf = float(candidate.get("confidence") or 0)
        if new_conf >= current_conf:
            merged[key] = {**current, **candidate, "confidence": max(current_conf, new_conf)}
    return list(merged.values())


def _extract_entities_from_text(
    text: str,
    add: Any,
    *,
    seen_in: str,
    evidence_refs: Iterable[str],
) -> None:
    for match in SYSTEMD_UNIT_RE.findall(text):
        add(match, "systemd_unit", seen_in=seen_in, evidence_refs=evidence_refs, confidence=0.95)
    for match in PATH_RE.findall(text):
        entity_type = "script_path" if match.endswith((".py", ".sh", ".js")) else "file_path"
        add(match, entity_type, seen_in=seen_in, evidence_refs=evidence_refs, confidence=0.8)
        basename = _basename(match)
        if basename and "." in basename:
            add(basename, "program_name", seen_in=seen_in, evidence_refs=evidence_refs, confidence=0.65)
    for match in URL_RE.findall(text):
        add(match, "endpoint", seen_in=seen_in, evidence_refs=evidence_refs, confidence=0.75)
    for match in DOMAIN_RE.findall(text):
        if "<" not in match:
            add(match, "domain", seen_in=seen_in, evidence_refs=evidence_refs, confidence=0.55)
    for match in LOGGER_RE.findall(text):
        add(match, "logger_name", seen_in=seen_in, evidence_refs=evidence_refs, confidence=0.65)
    for match in PROCESS_RE.findall(text):
        add(match, "process_name", seen_in=seen_in, evidence_refs=evidence_refs, confidence=0.65)
    for match in METRIC_RE.findall(text):
        add(match, "metric_name", seen_in=seen_in, evidence_refs=evidence_refs, confidence=0.75)
    if "metric" in text.casefold() or "heartbeat" in text.casefold() or "freshness" in text.casefold():
        for match in METRIC_HINT_RE.findall(text):
            add(match, "metric_name", seen_in=seen_in, evidence_refs=evidence_refs, confidence=0.6)


def _classify_entity_name(name: str) -> str:
    if SYSTEMD_UNIT_RE.search(name):
        return "systemd_unit"
    if name.endswith((".py", ".sh", ".js")) or "/" in name:
        return "script_path" if name.endswith((".py", ".sh", ".js")) else "file_path"
    if _looks_like_metric(name):
        return "metric_name"
    return "component_name"


def _iter_project_files(root: Path) -> Iterable[Path]:
    if not root.exists():
        return []
    files: list[Path] = []
    for current_text, dirs, filenames in os.walk(root):
        current = Path(current_text)
        dirs[:] = [name for name in dirs if name not in IGNORED_DIRS]
        for filename in sorted(filenames):
            path = current / filename
            if any(part in IGNORED_DIRS for part in path.relative_to(root).parts):
                continue
            if path.suffix in IGNORED_SUFFIXES:
                continue
            files.append(path)
    return sorted(files, key=lambda item: str(item.relative_to(root)))


def _systemd_entity(path: Path, root: Path, report: RedactionCounter) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8", errors="replace")[:65536]
    fields: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key in {"Description", "ExecStart", "WorkingDirectory", "EnvironmentFile", "Restart", "User"}:
            fields[key] = _sanitize_discovery_text(value.strip(), report)
    referenced = _referenced_programs(text, report)
    return {
        "entity_type": "systemd_unit",
        "name": path.name,
        "relative_path": _sanitize_discovery_text(_rel(path, root), report),
        "attributes": {
            "unit_file_path": _sanitize_discovery_text(_rel(path, root), report),
            "unit_name": path.name,
            "description": fields.get("Description", ""),
            "exec_start_template": fields.get("ExecStart", ""),
            "working_directory_template": fields.get("WorkingDirectory", ""),
            "environment_file_template": fields.get("EnvironmentFile", ""),
            "restart": fields.get("Restart", ""),
            "user": fields.get("User", ""),
            "referenced_programs": referenced,
        },
    }


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
                programs.add(_sanitize_discovery_text(part, report))
                basename = _basename(part)
                if basename:
                    programs.add(basename)
    return sorted(programs)


def _env_key_entities(path: Path, root: Path, report: RedactionCounter) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines()[:500]:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        key = key.strip()
        secret_like = bool(SECRET_ENV_KEY_RE.search(key))
        safe_name = _safe_env_key_name(key) if secret_like else _sanitize_discovery_text(key, report)
        rows.append(
            {
                "entity_type": "env_key",
                "name": safe_name,
                "relative_path": _sanitize_discovery_text(_rel(path, root), report),
                "attributes": {
                    "value_uploaded": False,
                    "value_type": _value_type(value),
                    "secret_like": secret_like,
                    "original_name_uploaded": not secret_like,
                },
            }
        )
    return rows


def _dependency_manifest_entity(path: Path, root: Path, report: RedactionCounter) -> dict[str, Any]:
    return {
        "entity_type": "dependency_manifest",
        "name": _sanitize_discovery_text(_rel(path, root), report),
        "relative_path": _sanitize_discovery_text(_rel(path, root), report),
        "attributes": {"manifest_type": path.name},
    }


def _code_reference_entities(
    root: Path,
    files: list[Path],
    observed_entities: list[dict[str, Any]],
    report: RedactionCounter,
) -> list[dict[str, Any]]:
    terms = _search_terms(observed_entities)
    if not terms:
        return []
    rows: list[dict[str, Any]] = []
    total_bytes = 0
    for path in files:
        if not _looks_text(path):
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")[:65536]
        except OSError:
            continue
        for term in terms:
            if term not in text:
                continue
            excerpts: list[str] = []
            for line in text.splitlines():
                if term in line:
                    clean = _sanitize_discovery_text(line.strip(), report)
                    scan = scan_sanitized_text(path.name, clean)
                    if scan["findings"]:
                        clean = f"{term} <REDACTED_SECRET>"
                    excerpts.append(clean[:500])
                if len(excerpts) >= 3:
                    break
            if not excerpts:
                continue
            size = sum(len(item.encode("utf-8")) for item in excerpts)
            if total_bytes + size > 200000:
                return rows
            total_bytes += size
            rows.append(
                {
                    "entity_type": "code_reference",
                    "name": _sanitize_discovery_text(term, report),
                    "relative_path": _sanitize_discovery_text(_rel(path, root), report),
                    "attributes": {
                        "matched_term": _sanitize_discovery_text(term, report),
                        "excerpt_uploaded": True,
                        "excerpt_sanitized": excerpts,
                    },
                }
            )
    return rows


def _search_terms(observed_entities: list[dict[str, Any]]) -> list[str]:
    terms: set[str] = set()
    for row in observed_entities:
        name = str(row.get("name") or "")
        entity_type = str(row.get("entity_type") or "")
        if entity_type in {"metric_name", "logger_name"} and len(name) >= 4:
            terms.add(name)
        if entity_type in {"script_path", "program_name"}:
            basename = _basename(name)
            if basename and len(basename) >= 4:
                terms.add(basename)
    return sorted(terms)


def _unique_project_entities(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_key: dict[tuple[str, str, str], dict[str, Any]] = {}
    for row in rows:
        key = (str(row.get("entity_type")), str(row.get("name")), str(row.get("relative_path", "")))
        by_key.setdefault(key, row)
    return sorted(by_key.values(), key=lambda item: (str(item.get("entity_type")), str(item.get("name")), str(item.get("relative_path", ""))))


def _link_score(obs: dict[str, Any], prj: dict[str, Any]) -> tuple[float, str, list[str]]:
    obs_name = str(obs.get("name") or "")
    prj_name = str(prj.get("name") or "")
    obs_type = str(obs.get("entity_type") or "")
    prj_type = str(prj.get("entity_type") or "")
    attrs = prj.get("attributes") if isinstance(prj.get("attributes"), dict) else {}
    features: list[str] = []
    score = 0.0
    match_type = "weak_name_match"
    if obs_type == "systemd_unit" and prj_type == "systemd_unit" and obs_name == attrs.get("unit_name"):
        score += 0.4
        features.append("unit_name")
        match_type = "systemd_unit_exact_match"
        if attrs.get("exec_start_template"):
            score += 0.2
            features.append("exec_start")
    obs_base = _basename(obs_name)
    referenced = [str(item) for item in attrs.get("referenced_programs") or []]
    referenced_bases = {_basename(item) for item in referenced}
    if obs_type in {"script_path", "file_path", "program_name"} and obs_base:
        if obs_base in referenced_bases:
            score += 0.3
            features.append("exec_start_basename")
            match_type = "exec_start_path_basename_match"
        if obs_base == _basename(prj_name):
            score += 0.3
            features.append("script_basename")
            match_type = "script_path_match"
    if obs_type == "metric_name" and prj_type == "code_reference" and obs_name == attrs.get("matched_term"):
        score += 0.3
        features.append("metric_name_definition")
        match_type = "metric_name_definition_match"
    if obs_name and prj_name and obs_name.casefold() == prj_name.casefold():
        score += 0.15
        features.append("name_exact")
    elif obs_base and obs_base == _basename(prj_name):
        score += 0.1
        features.append("file_tree_basename")
    return score, match_type, sorted(set(features))


def _metric_semantics_for_name(name: str) -> dict[str, str]:
    folded = name.casefold()
    if any(term in folded for term in ("transport", "throughput", "heartbeat", "freshness", "energy")):
        return {
            "zero_behavior": "suspicious",
            "increase_behavior": "healthy_or_scale",
            "decrease_behavior": "suspicious",
            "candidate_core_target_type": "throughput_disappearance",
        }
    if any(term in folded for term in ("error", "failure", "failed")):
        return {
            "zero_behavior": "healthy",
            "increase_behavior": "suspicious",
            "decrease_behavior": "healthy_or_recovery",
            "candidate_core_target_type": "service_start_failure",
        }
    return {
        "zero_behavior": "unknown",
        "increase_behavior": "unknown",
        "decrease_behavior": "unknown",
        "candidate_core_target_type": "general",
    }


def _core_targets_for_text(text: str) -> set[str]:
    folded = text.casefold()
    targets: set[str] = set()
    if any(term in folded for term in ("missing", "execstart", "systemd", ".service")):
        targets.update({"job_configuration_mismatch", "service_start_failure"})
    if any(term in folded for term in ("watchdog", "restart", "supervisor")):
        targets.add("restart_loop")
    if any(term in folded for term in ("ffmpeg", "rtmp", "rtmps", "connection")):
        targets.add("network_error_signal")
    if any(term in folded for term in ("metric", "heartbeat", "instrumentation")):
        targets.add("monitoring_gap")
    return targets or {"general"}


def _suggest_subsystem(text: str) -> str:
    folded = text.casefold()
    if any(term in folded for term in ("watchdog", "restart", "supervisor")):
        return "runtime_recovery"
    if any(term in folded for term in ("systemd", "execstart", "missing path", ".service")):
        return "job_configuration"
    if any(term in folded for term in ("ffmpeg", "rtmp", "rtmps")):
        return "rtmps_ffmpeg"
    if any(term in folded for term in ("youtube", "ingest", "gmail", "discord", "webhook")):
        return "external_dependency"
    if any(term in folded for term in ("audio", "pulse")):
        return "audio_pipeline"
    if any(term in folded for term in ("capture", "chromium")):
        return "capture_pipeline"
    if any(term in folded for term in ("db", "database", "postgres", "mysql")):
        return "database"
    if any(term in folded for term in ("queue", "pubsub", "kafka", "sqs")):
        return "messaging"
    if any(term in folded for term in ("auth", "token", "credential")):
        return "auth_config"
    return "general"


def _suggest_role(text: str) -> str:
    folded = text.casefold()
    if "watchdog" in folded:
        return "watchdog"
    if "listener" in folded or "pubsub" in folded:
        return "listener"
    if "worker" in folded:
        return "worker"
    if "scheduler" in folded or "timer" in folded:
        return "scheduler"
    if ".service" in folded:
        return "systemd_service"
    return "component"


def _component_name(text: str) -> str:
    name = text.rsplit("/", 1)[-1].replace(".service", "")
    name = re.sub(r"[^A-Za-z0-9_]+", "_", name).strip("_")
    return name or "component"


def _profile_discovery_questions(
    observed_entities: list[dict[str, Any]],
    component_candidates: list[dict[str, Any]],
    metric_candidates: list[dict[str, Any]],
) -> list[str]:
    questions = list(REQUIRED_PROFILE_QUESTIONS)
    if component_candidates:
        questions.append("Which discovered component roles are correct?")
    if metric_candidates:
        questions.append("Which metric semantics should become explicit profile rules?")
    if not observed_entities:
        questions.append("Which service or log source should seed profile discovery?")
    return questions


def _draft_log_sources(discovery: dict[str, Any]) -> list[dict[str, Any]]:
    sources = []
    for row in discovery.get("observed_entities") or []:
        if isinstance(row, dict) and row.get("entity_type") in {"logger_name", "systemd_unit", "service_name"}:
            sources.append(
                {
                    "source_name": row.get("name"),
                    "source_type": row.get("entity_type"),
                    "meaning": "Candidate log source derived from sanitized profile discovery.",
                    "human_review_required": True,
                }
            )
    return sources


def _discovery_manifest(project_root: str | Path | None, bundle: dict[str, Any]) -> dict[str, Any]:
    source = bundle.get("source") if isinstance(bundle.get("source"), dict) else {}
    return {
        "schema_version": "profile_discovery_manifest.v1",
        "created_at": utc_now(),
        "project_name": Path(project_root).name if project_root else str(source.get("project_name") or "project"),
        "raw_log_policy": RAW_LOG_POLICY,
        "raw_config_policy": RAW_CONFIG_POLICY,
        "raw_logs_policy": RAW_LOGS_POLICY,
        "discovery_sha256": bundle["discovery_sha256"],
        "outputs": {
            "profile_discovery_bundle": "profile_discovery_bundle.json",
            "redaction_report": "redaction_report.json",
        },
        "local_first_summary": bundle["local_first_summary"],
    }


def _discovery_redaction_report(bundle: dict[str, Any]) -> dict[str, Any]:
    summary = bundle.get("redaction_summary") if isinstance(bundle.get("redaction_summary"), dict) else {}
    examples = [
        {"type": key, "replacement": "<REDACTED_SECRET>", "count": count}
        for key, count in sorted(summary.items())
        if int(count or 0)
    ]
    if not examples:
        examples = [{"type": "secret_like", "replacement": "<REDACTED_SECRET>", "count": 0}]
    return {
        "schema_version": "profile_discovery_redaction_report.v1",
        "raw_config_policy": RAW_CONFIG_POLICY,
        "raw_logs_policy": RAW_LOGS_POLICY,
        "raw_log_policy": RAW_LOG_POLICY,
        "summary": summary,
        "examples": examples,
    }


def _sanitize_discovery_text(value: Any, report: RedactionCounter) -> str:
    text = redact_text(value, report)
    for pattern, replacement in UNSAFE_LABEL_REPLACEMENTS:
        text = pattern.sub(replacement, text)
    return text


def _safe_env_key_name(key: str) -> str:
    return f"<SECRET_ENV_KEY_HASH:{sha256_text(key.casefold())[:12]}>"


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


def _is_env_file(path: Path) -> bool:
    name = path.name
    return name == ".env" or name.startswith(".env.") or name.endswith(".env") or name.endswith(".env.example")


def _looks_text(path: Path) -> bool:
    return path.suffix in TEXT_SUFFIXES or path.name in DEPENDENCY_MANIFESTS or _is_env_file(path)


def _looks_like_metric(name: str) -> bool:
    return bool(METRIC_RE.fullmatch(name) or METRIC_HINT_RE.fullmatch(name))


def _rel(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return path.name


def _basename(value: str) -> str:
    return value.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]


def _slug(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9_]+", "_", value).strip("_").lower()
    return slug or "profile"


def _average(values: Iterable[Any]) -> float:
    nums = [float(value) for value in values if isinstance(value, (int, float))]
    if not nums:
        return 0.0
    return round(sum(nums) / len(nums), 2)


def _clear_profile_loader_cache() -> None:
    try:
        from ops_evidence_synthesis.profiles.registry import load_profile

        load_profile.cache_clear()
    except Exception:
        return
