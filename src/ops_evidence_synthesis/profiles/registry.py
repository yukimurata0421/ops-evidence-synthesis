from __future__ import annotations

import json
import os
import re
from collections import Counter
from functools import lru_cache
from importlib import resources
from pathlib import Path
from typing import Any


GENERIC_PROFILE_ID = "generic"
PROFILE_DIR_ENV = "OES_PROFILE_DIR"


@lru_cache(maxsize=8)
def load_profile(profile_id: str) -> dict[str, Any]:
    normalized = normalize_profile_id(profile_id)
    text, default_semantic_rule_trust = _profile_document(normalized)
    if text is None:
        normalized = GENERIC_PROFILE_ID
        text, default_semantic_rule_trust = _profile_document(normalized)
    if text is None:
        raise FileNotFoundError("generic profile is missing")
    loaded = dict(_load_profile_mapping(text, normalized))
    loaded["profile_id"] = str(loaded.get("profile_id") or normalized)
    loaded["semantic_rule_trust"] = _semantic_rule_trust(
        loaded.get("semantic_rule_trust"),
        default=default_semantic_rule_trust,
    )
    return loaded


def profile_for_bundle(bundle: dict[str, Any] | None) -> dict[str, Any]:
    bundle = bundle or {}
    profile = bundle.get("profile") if isinstance(bundle.get("profile"), dict) else {}
    profile_id = str(profile.get("profile_id") or bundle.get("profile_id") or "").strip()
    if not profile_id:
        environment = normalize_profile_id(bundle.get("environment") or "")
        profile_id = environment or GENERIC_PROFILE_ID
        if load_profile(profile_id).get("profile_id") == GENERIC_PROFILE_ID:
            service_raw = str(bundle.get("service") or "").strip()
            service_profile_id = normalize_profile_id(service_raw)
            if service_raw and service_profile_id != GENERIC_PROFILE_ID and load_profile(service_profile_id).get("profile_id") == service_profile_id:
                profile_id = service_profile_id
    elif normalize_profile_id(profile_id) == GENERIC_PROFILE_ID:
        service_raw = str(bundle.get("service") or "").strip()
        service_profile_id = normalize_profile_id(service_raw)
        if service_raw and service_profile_id != GENERIC_PROFILE_ID and load_profile(service_profile_id).get("profile_id") == service_profile_id:
            profile_id = service_profile_id
    loaded = load_profile(profile_id)
    loaded_source_system = str(loaded.get("source_system") or "")
    loaded_profile_id = str(loaded.get("profile_id") or profile_id)
    input_profile_id = normalize_profile_id(profile.get("profile_id") or "")
    profile_source_system = (
        profile.get("source_system")
        if input_profile_id and input_profile_id != GENERIC_PROFILE_ID
        else ""
    )
    return {
        **loaded,
        "profile_id": loaded_profile_id,
        "source_system": (
            profile_source_system
            or (loaded_source_system if loaded_profile_id != GENERIC_PROFILE_ID else "")
            or bundle.get("service")
            or bundle.get("environment")
            or loaded_source_system
            or profile_id
        ),
    }


def profile_context_for_bundle(bundle: dict[str, Any] | None) -> dict[str, Any]:
    profile = profile_for_bundle(bundle)
    context = {
        "profile": {
            "profile_id": str(profile.get("profile_id") or GENERIC_PROFILE_ID),
            "profile_label": str(profile.get("profile_label") or profile.get("profile_id") or GENERIC_PROFILE_ID),
            "source_system": str(profile.get("source_system") or ""),
        },
        "system_profile": profile.get("system_profile") or {},
        "operational_contract": profile.get("operational_contract") or {},
        "log_sources": profile.get("log_sources") or [],
        "metric_semantics": profile.get("metric_semantics") or profile.get("metrics") or {},
        "event_semantics": profile.get("event_semantics") or [],
        "semantic_rule_trust": str(profile.get("semantic_rule_trust") or "unapproved"),
        "component_map": profile.get("component_map") or {},
        "known_benign_noise": profile.get("known_benign_noise") or [],
        "action_constraints": profile.get("action_constraints") or [],
        "review_policy": profile.get("review_policy") or {},
        "runtime_ownership": profile.get("runtime_ownership") or {},
        "primary_positive_evidence": profile.get("primary_positive_evidence") or {},
        "failure_absence_evidence": profile.get("failure_absence_evidence") or {},
        "classification_overrides": profile.get("classification_overrides") or [],
        "support_evidence_requirements": profile.get("support_evidence_requirements") or {},
        "context_note": (
            profile.get("context_note")
            or "System profile fields are interpretation context only. They are not evidence and must not be cited as support."
        ),
    }
    return {key: value for key, value in context.items() if value not in ({}, [], "")}


def operational_evidence_specs(profile_id: str) -> list[dict[str, Any]]:
    """Return profile-defined operational evidence extractors.

    Specs are intentionally data-driven so new systems can add operational
    evidence without changing the bundle builder.
    """

    profile = load_profile(profile_id)
    specs = profile.get("operational_evidence_specs") or []
    if not isinstance(specs, list):
        return []
    output: list[dict[str, Any]] = []
    for index, raw_spec in enumerate(specs, start=1):
        if not isinstance(raw_spec, dict):
            continue
        request_id = str(raw_spec.get("request_id") or raw_spec.get("profile_request_id") or f"operational_evidence_{index}_query")
        need = str(raw_spec.get("need") or raw_spec.get("request_type") or request_id.removesuffix("_query"))
        spec = {
            "evidence_id": str(raw_spec.get("evidence_id") or f"OPS-{index:03d}"),
            "request_id": request_id,
            "profile_request_id": str(raw_spec.get("profile_request_id") or request_id),
            "request_type": str(raw_spec.get("request_type") or need),
            "need": need,
            "subsystem": str(raw_spec.get("subsystem") or "general"),
            "summary": str(raw_spec.get("summary") or raw_spec.get("description") or need.replace("_", " ")),
            "terms": _string_list(raw_spec.get("terms") or raw_spec.get("search_terms") or []),
            "metric_names": _string_list(raw_spec.get("metric_names") or []),
            "source_names": _string_list(raw_spec.get("source_names") or raw_spec.get("preferred_sources") or []),
        }
        output.append(spec)
    return output


def profile_id_for_item(item: dict[str, Any] | None) -> str:
    item = item or {}
    profile = item.get("profile") if isinstance(item.get("profile"), dict) else {}
    profile_id = str(item.get("profile_id") or profile.get("profile_id") or "").strip()
    if profile_id:
        return profile_id
    environment = str(item.get("environment") or "").casefold()
    if environment:
        normalized_environment = normalize_profile_id(environment)
        if load_profile(normalized_environment).get("profile_id") == normalized_environment:
            return normalized_environment
    service_raw = str(item.get("service") or "").strip()
    service_profile_id = normalize_profile_id(service_raw)
    if service_raw and service_profile_id != GENERIC_PROFILE_ID and load_profile(service_profile_id).get("profile_id") == service_profile_id:
        return service_profile_id
    subsystem = str(item.get("subsystem") or "").casefold()
    if subsystem in {"youtube_health", "rtmps_ffmpeg", "chromium_capture", "audio_energy"}:
        return "stream_v3"
    text = " ".join(
        str(item.get(key) or "")
        for key in ("question", "support_summary", "counter_summary")
    ).casefold()
    if any(term in text for term in ("ffmpeg", "rtmps", "youtube", "audio_energy", "capture_freshness", "chromium")):
        return "stream_v3"
    return GENERIC_PROFILE_ID


def profile_label(profile_id: str) -> str:
    profile = load_profile(profile_id)
    return str(profile.get("profile_label") or profile.get("profile_id") or profile_id)


def normalize_profile_id(value: Any) -> str:
    text = str(value or "").strip().casefold().replace("-", "_")
    text = re.sub(r"[^a-z0-9_]+", "_", text)
    text = re.sub(r"_+", "_", text).strip("_")
    return text or GENERIC_PROFILE_ID


def metric_semantics(metric_name: str, profile_id: str = GENERIC_PROFILE_ID) -> dict[str, Any]:
    metric = str(metric_name or "").strip()
    profile = load_profile(profile_id)
    generic = load_profile(GENERIC_PROFILE_ID)
    profile_metrics = profile.get("metrics") if isinstance(profile.get("metrics"), dict) else {}
    generic_metrics = generic.get("metrics") if isinstance(generic.get("metrics"), dict) else {}
    semantics = profile_metrics.get(metric) or generic_metrics.get(metric) or {}
    if semantics:
        return {**dict(semantics), "metric_name": metric}
    return {
        "metric_name": metric,
        "semantic_type": "unknown",
        "zero_behavior": "unknown",
        "subsystem": "general",
        "core_target_type": "",
    }


def available_profile_ids() -> tuple[str, ...]:
    ids = set()
    for directory in _local_profile_dirs():
        if not directory.is_dir():
            continue
        for path in directory.iterdir():
            if path.name.startswith("__") or path.suffix not in {".yaml", ".json"}:
                continue
            ids.add(path.stem)
    for path in resources.files("ops_evidence_synthesis.profiles").iterdir():
        if path.name.startswith("__") or path.suffix != ".yaml":
            continue
        ids.add(path.stem)
    return tuple(sorted(ids))


def _profile_document(normalized_profile_id: str) -> tuple[str | None, str]:
    for directory in _local_profile_dirs():
        for suffix in (".yaml", ".json"):
            path = directory / f"{normalized_profile_id}{suffix}"
            if path.is_file():
                return path.read_text(encoding="utf-8"), "unapproved"
    package_path = resources.files("ops_evidence_synthesis.profiles").joinpath(f"{normalized_profile_id}.yaml")
    if package_path.is_file():
        return package_path.read_text(encoding="utf-8"), "packaged_allowlist"
    return None, "unapproved"


def _semantic_rule_trust(value: Any, *, default: str) -> str:
    normalized = str(value or default or "unapproved").strip().casefold()
    if normalized in {"human_approved", "packaged_allowlist"}:
        return normalized
    return "unapproved"


def _load_profile_mapping(text: str, profile_id: str) -> dict[str, Any]:
    try:
        loaded = json.loads(text)
    except json.JSONDecodeError as json_error:
        try:
            import yaml
        except ImportError as import_error:
            raise ValueError(
                f"profile '{profile_id}' is not JSON and PyYAML is not installed for YAML parsing"
            ) from import_error
        loaded = yaml.safe_load(text)
        if loaded is None:
            loaded = {}
        if not isinstance(loaded, dict):
            raise ValueError(f"profile '{profile_id}' must contain a mapping") from json_error
        return dict(loaded)
    if not isinstance(loaded, dict):
        raise ValueError(f"profile '{profile_id}' must contain a mapping")
    return dict(loaded)


def _local_profile_dirs() -> tuple[Path, ...]:
    values = [item for item in os.environ.get(PROFILE_DIR_ENV, "").split(os.pathsep) if item]
    paths = [Path(value) for value in values]
    paths.append(Path.cwd() / "profiles")
    return tuple(paths)


def metric_names_by_zero_behavior(zero_behavior: str, *, include_profiles: tuple[str, ...] | None = None) -> set[str]:
    output: set[str] = set()
    for profile_id in include_profiles or available_profile_ids():
        metrics = load_profile(profile_id).get("metrics") or {}
        for metric_name, semantics in metrics.items():
            if str((semantics or {}).get("zero_behavior") or "") == zero_behavior:
                output.add(str(metric_name))
    return output


def target_type_for_metric(metric_name: str, profile_id: str = GENERIC_PROFILE_ID) -> str:
    semantics = metric_semantics(metric_name, profile_id)
    return str(semantics.get("core_target_type") or "")


def target_type_for_subsystem(subsystem: str, profile_id: str = GENERIC_PROFILE_ID) -> str:
    subsystem_name = str(subsystem or "").strip()
    if not subsystem_name:
        return ""
    for profile_key in _profile_precedence(profile_id):
        metrics = load_profile(profile_key).get("metrics") or {}
        target_types: list[str] = []
        for semantics in metrics.values():
            if not isinstance(semantics, dict):
                continue
            if str(semantics.get("subsystem") or "") != subsystem_name:
                continue
            target_type = str(semantics.get("core_target_type") or "")
            if target_type:
                target_types.append(target_type)
        if target_types:
            counts = Counter(target_types)
            return sorted(counts, key=lambda value: (-counts[value], target_types.index(value), value))[0]
    return ""


def target_type_for_text(text: str, profile_id: str = GENERIC_PROFILE_ID) -> str:
    folded = str(text or "").casefold()
    priority_targets = ("job_configuration_mismatch", "service_start_failure")
    for profile_key in _profile_precedence(profile_id):
        target_types = load_profile(profile_key).get("target_types") or {}
        for target_type in priority_targets:
            definition = target_types.get(target_type) or {}
            terms = [str(term).casefold() for term in (definition or {}).get("text_terms") or []]
            if terms and any(term in folded for term in terms):
                return str(target_type)
        for target_type, definition in target_types.items():
            if target_type in priority_targets:
                continue
            terms = [str(term).casefold() for term in (definition or {}).get("text_terms") or []]
            if terms and any(term in folded for term in terms):
                return str(target_type)
    return ""


def target_definition(target_type: str, profile_id: str = GENERIC_PROFILE_ID) -> dict[str, Any]:
    target = str(target_type or "").strip()
    for profile_key in _profile_precedence(profile_id):
        target_types = load_profile(profile_key).get("target_types") or {}
        if target in target_types:
            return dict(target_types[target])
    return {}


def title_for_target_type(target_type: str, profile_id: str = GENERIC_PROFILE_ID, *, default: str = "") -> str:
    definition = target_definition(target_type, profile_id)
    return str(definition.get("domain_label") or definition.get("title") or default or target_type.replace("_", " ").title())


def evidence_requests_for_target_type(target_type: str, profile_id: str = GENERIC_PROFILE_ID) -> list[dict[str, Any]]:
    definition = target_definition(target_type, profile_id)
    request_ids = [str(value) for value in definition.get("evidence_requests") or [] if value]
    if not request_ids:
        return []
    profile = load_profile(profile_id)
    generic = load_profile(GENERIC_PROFILE_ID)
    profile_requests = profile.get("evidence_requests") if isinstance(profile.get("evidence_requests"), dict) else {}
    generic_requests = generic.get("evidence_requests") if isinstance(generic.get("evidence_requests"), dict) else {}
    output: list[dict[str, Any]] = []
    for request_id in request_ids:
        base = dict(generic_requests.get(request_id) or {})
        override = dict(profile_requests.get(request_id) or {})
        request = {**base, **override}
        if not request:
            continue
        request["request_id"] = request_id
        request.setdefault("request_type", request_id.removesuffix("_query"))
        request.setdefault("need", request["request_type"])
        request.setdefault("description", request["need"].replace("_", " "))
        output.append(request)
    return output


def _profile_precedence(profile_id: str) -> tuple[str, ...]:
    profile_id = normalize_profile_id(profile_id)
    if profile_id == GENERIC_PROFILE_ID:
        return (GENERIC_PROFILE_ID,)
    return (profile_id, GENERIC_PROFILE_ID)


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if str(item).strip()]
