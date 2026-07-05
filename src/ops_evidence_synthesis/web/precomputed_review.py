from __future__ import annotations

import json
import os
import re
import time
from copy import deepcopy
from pathlib import Path
from typing import Any
from urllib.parse import quote

_PRECOMPUTED_REVIEW_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_RESCORE_DEMO_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
PUBLIC_PRIMARY_REVIEW_SHA256 = "345430d258752cefef81bfb587b4c210799d02bfc849e0a7ac5dc4c48fddb1d6"
_PUBLIC_PRECOMPUTED_REVIEW_ALIASES = {
    "64fa79977171fe9bad0664d115ff0ffcf4e248cd12a6a938e62d25cba7b12681": PUBLIC_PRIMARY_REVIEW_SHA256,
}


def _precomputed_review_cache_ttl_seconds() -> int:
    return int(os.environ.get("OES_PRECOMPUTED_REVIEW_CACHE_SECONDS", "300"))


def _human_count(value: int) -> str:
    return f"{int(value):,}"


def _count_noun(value: int, singular: str, plural: str | None = None) -> str:
    count = int(value)
    noun = singular if count == 1 else (plural or f"{singular}s")
    return f"{_human_count(count)} {noun}"


def _review_target_count_text(primary_targets: int, validation_targets: int) -> str:
    return (
        f"{_count_noun(primary_targets, 'primary candidate')} and "
        f"{_count_noun(validation_targets, 'validation target')}"
    )


_PUBLIC_COUNT_TOKEN_PATTERNS = (
    (re.compile(r"\b(\d[\d,]*) primary candidate\(s\)"), "primary candidate", None),
    (re.compile(r"\b(\d[\d,]*) validation target\(s\)"), "validation target", None),
    (re.compile(r"\b(\d[\d,]*) review target\(s\)"), "review target", None),
    (re.compile(r"\b(\d[\d,]*) target\(s\)"), "target", None),
    (re.compile(r"\b(\d[\d,]*) Evidence Item association\(s\)"), "Evidence Item association", None),
    (re.compile(r"\b(\d[\d,]*) Evidence Item\(s\)"), "Evidence Item", "Evidence Items"),
    (re.compile(r"\b(\d[\d,]*) additional trace step\(s\)"), "additional trace step", None),
    (re.compile(r"\b(\d[\d,]*) trace step\(s\)"), "trace step", None),
    (re.compile(r"\b(\d[\d,]*) summary item\(s\)"), "summary item", None),
    (re.compile(r"\b(\d[\d,]*) explicit conflict\(s\)"), "explicit conflict", None),
    (re.compile(r"\b(\d[\d,]*) convergence group\(s\)"), "convergence group", None),
    (re.compile(r"\b(\d[\d,]*) occurrence\(s\)"), "occurrence", None),
    (re.compile(r"\b(\d[\d,]*) chunk\(s\)"), "chunk", None),
    (re.compile(r"\b(\d[\d,]*) row\(s\)"), "row", None),
)
_PUBLIC_GENERIC_COUNT_TOKEN_PATTERN = re.compile(
    r"\b(\d[\d,]*) ([A-Za-z][A-Za-z0-9_/-]*(?: [A-Za-z][A-Za-z0-9_/-]*){0,5})\(s\)"
)
_PUBLIC_NON_ONE_SINGULAR_PATTERNS = (
    (re.compile(r"\b(\d[\d,]*) primary candidate\b(?!s)"), "primary candidate", None),
    (re.compile(r"\b(\d[\d,]*) validation target\b(?!s)"), "validation target", None),
    (re.compile(r"\b(\d[\d,]*) monitor-only item\b(?!s)"), "monitor-only item", None),
)


def _public_count_text(value: object) -> str:
    text = "" if value is None else str(value)
    for pattern, singular, plural in _PUBLIC_COUNT_TOKEN_PATTERNS:
        text = pattern.sub(
            lambda match, singular=singular, plural=plural: _count_noun(
                int(match.group(1).replace(",", "")),
                singular,
                plural,
            ),
            text,
        )
    text = _PUBLIC_GENERIC_COUNT_TOKEN_PATTERN.sub(
        lambda match: _count_noun(int(match.group(1).replace(",", "")), match.group(2)),
        text,
    )
    for pattern, singular, plural in _PUBLIC_NON_ONE_SINGULAR_PATTERNS:
        text = pattern.sub(
            lambda match, singular=singular, plural=plural: (
                match.group(0)
                if int(match.group(1).replace(",", "")) == 1
                else _count_noun(int(match.group(1).replace(",", "")), singular, plural)
            ),
            text,
        )
    return text


def _public_repo_url() -> str:
    return os.environ.get("OES_PUBLIC_REPO_URL", "https://github.com/yukimurata0421/ops-evidence-synthesis").rstrip("/")


def _public_architecture_url() -> str:
    configured = os.environ.get("OES_PUBLIC_ARCHITECTURE_URL", "").strip()
    if configured:
        return configured
    return f"{_public_repo_url()}/blob/main/docs/assets/architecture-devops-ai-agent.svg"


def _public_demo_script_url() -> str:
    configured = os.environ.get("OES_PUBLIC_DEMO_SCRIPT_URL", "").strip()
    if configured:
        return configured
    return f"{_public_repo_url()}/blob/main/docs/demo-video-script.md"


def _public_demo_video_url() -> str:
    return os.environ.get("OES_PUBLIC_DEMO_VIDEO_URL", "").strip()


def _precomputed_review_dirs() -> list[Path]:
    configured = [
        Path(item)
        for item in os.environ.get("OES_PRECOMPUTED_REVIEW_DIRS", "").split(os.pathsep)
        if item.strip()
    ]
    single = os.environ.get("OES_PRECOMPUTED_REVIEW_DIR")
    if single:
        configured.insert(0, Path(single))
    configured.append(Path("data/precomputed_review_summaries"))
    return configured


def _precomputed_review_gcs_uris(evidence_id: str) -> list[str]:
    prefixes: list[str] = []
    single = os.environ.get("OES_PRECOMPUTED_REVIEW_GCS_PREFIX", "").strip()
    if single:
        prefixes.append(single)
    for item in os.environ.get("OES_PRECOMPUTED_REVIEW_GCS_PREFIXES", "").split(","):
        item = item.strip()
        if item:
            prefixes.append(item)
    uris: list[str] = []
    for prefix in prefixes:
        clean = prefix.rstrip("/")
        if clean.startswith("gs://"):
            uris.append(f"{clean}/{evidence_id}.json")
    return uris


def _canonical_precomputed_review_sha(evidence_sha256: str | None) -> str:
    evidence_id = str(evidence_sha256 or "").strip().casefold()
    return _PUBLIC_PRECOMPUTED_REVIEW_ALIASES.get(evidence_id, evidence_id)


def _rescore_demo_dirs() -> list[Path]:
    configured = [
        Path(item)
        for item in os.environ.get("OES_RESCORE_DEMO_DIRS", "").split(os.pathsep)
        if item.strip()
    ]
    single = os.environ.get("OES_RESCORE_DEMO_DIR")
    if single:
        configured.insert(0, Path(single))
    configured.append(Path("data/rescore_demos"))
    return configured


def _precomputed_review_payload(evidence_sha256: str) -> dict[str, Any] | None:
    evidence_id = _canonical_precomputed_review_sha(evidence_sha256)
    if not evidence_id or len(evidence_id) > 128 or any(ch not in "0123456789abcdefABCDEF-" for ch in evidence_id):
        return None
    ttl = _precomputed_review_cache_ttl_seconds()
    cached = _PRECOMPUTED_REVIEW_CACHE.get(evidence_id)
    if ttl > 0 and cached and time.monotonic() - cached[0] < ttl:
        return deepcopy(cached[1])
    for directory in _precomputed_review_dirs():
        path = directory / f"{evidence_id}.json"
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            continue
        except Exception:
            continue
        if not isinstance(payload, dict):
            continue
        if not _payload_matches_precomputed_id(payload, evidence_id):
            continue
        if ttl > 0:
            _PRECOMPUTED_REVIEW_CACHE[evidence_id] = (time.monotonic(), deepcopy(payload))
        return payload
    for uri in _precomputed_review_gcs_uris(evidence_id):
        try:
            from ops_evidence_synthesis.gcp.storage import read_json

            payload = read_json(uri)
        except Exception:
            continue
        if not _payload_matches_precomputed_id(payload, evidence_id):
            continue
        if ttl > 0:
            _PRECOMPUTED_REVIEW_CACHE[evidence_id] = (time.monotonic(), deepcopy(payload))
        return payload
    return None


def _remember_precomputed_review_payload(payload: dict[str, Any]) -> None:
    for cache_id in _precomputed_cache_ids_for_payload(payload):
        _PRECOMPUTED_REVIEW_CACHE[cache_id] = (time.monotonic(), deepcopy(payload))


def _payload_matches_precomputed_id(payload: dict[str, Any], evidence_id: str) -> bool:
    if str(payload.get("evidence_sha256") or "") == evidence_id:
        return True
    generation = payload.get("generation") if isinstance(payload.get("generation"), dict) else {}
    fast_review = generation.get("fast_gcp_review") if isinstance(generation.get("fast_gcp_review"), dict) else {}
    return str(fast_review.get("public_review_id") or "") == evidence_id


def _precomputed_cache_ids_for_payload(payload: dict[str, Any]) -> list[str]:
    generation = payload.get("generation") if isinstance(payload.get("generation"), dict) else {}
    fast_review = generation.get("fast_gcp_review") if isinstance(generation.get("fast_gcp_review"), dict) else {}
    public_review_id = str(fast_review.get("public_review_id") or "").strip()
    if public_review_id:
        return [public_review_id]
    evidence_id = _canonical_precomputed_review_sha(str(payload.get("evidence_sha256") or ""))
    return [evidence_id] if evidence_id else []


def _rescore_demo_payload(demo_id: str) -> dict[str, Any] | None:
    safe_id = str(demo_id or "").strip()
    if not safe_id or len(safe_id) > 96 or any(not (ch.isalnum() or ch in "-_") for ch in safe_id):
        return None
    ttl = _precomputed_review_cache_ttl_seconds()
    cached = _RESCORE_DEMO_CACHE.get(safe_id)
    if ttl > 0 and cached and time.monotonic() - cached[0] < ttl:
        return deepcopy(cached[1])
    for directory in _rescore_demo_dirs():
        path = directory / f"{safe_id}.json"
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            continue
        except Exception:
            continue
        if not isinstance(payload, dict):
            continue
        if str(payload.get("demo_id") or "") != safe_id:
            continue
        if ttl > 0:
            _RESCORE_DEMO_CACHE[safe_id] = (time.monotonic(), deepcopy(payload))
        return payload
    return None


def _public_rescore_demo_ids() -> list[str]:
    ids: list[str] = []
    for directory in _rescore_demo_dirs():
        try:
            paths = sorted(directory.glob("*.json"))
        except Exception:
            continue
        for path in paths:
            safe_id = path.stem
            if safe_id and safe_id not in ids and _rescore_demo_payload(safe_id):
                ids.append(safe_id)
    return ids


def _public_rescore_demo_ids_for_evidence(evidence_sha256: str) -> list[str]:
    evidence_id = _canonical_precomputed_review_sha(evidence_sha256)
    ids: list[str] = []
    for demo_id in _public_rescore_demo_ids():
        payload = _rescore_demo_payload(demo_id)
        if not payload:
            continue
        source_evidence = _canonical_precomputed_review_sha(str(payload.get("source_evidence_sha256") or ""))
        if source_evidence == evidence_id:
            ids.append(demo_id)
    return ids


def _public_action_links_html(evidence_sha256: str, *, include_detail: bool = True) -> str:
    evidence = _url_quote(evidence_sha256)
    links: list[tuple[str, str, str]] = [
        ("Summary", f"/?evidence_sha256={evidence}", "read-only overview"),
    ]
    if include_detail:
        links.append(("Detail", f"/ui/full-review-page?evidence_sha256={evidence}", "full review targets"))
    links.extend(
        [
            ("API View", f"/ui/api?evidence_sha256={evidence}", "human-readable JSON"),
            ("Review Graph", f"/ui/review-graph?evidence_sha256={evidence}", "nodes and provider positions"),
            ("Incident Report", f"/ui/report.md?evidence_sha256={evidence}", "human-readable Markdown report"),
        ]
    )
    for demo_id in _public_rescore_demo_ids_for_evidence(evidence_sha256):
        links.append(("More Data Loop", f"/ui/rescore-demo?id={quote(demo_id)}", demo_id))
    links.extend(
        [
            ("GitHub", _public_repo_url(), "repository"),
            ("Architecture", _public_architecture_url(), "system diagram"),
            ("Demo Script", _public_demo_script_url(), "3 minute walkthrough"),
        ]
    )
    video_url = _public_demo_video_url()
    if video_url:
        links.append(("Demo Video", video_url, "recorded walkthrough"))
    return "".join(
        f'<a class="button" href="{_html(url)}" title="{_html(title)}">{_html(label)}</a>'
        for label, url, title in links
    )


def _public_global_action_links_html() -> str:
    links: list[tuple[str, str, str]] = [
        ("GitHub", _public_repo_url(), "repository"),
        ("Architecture", _public_architecture_url(), "system diagram"),
        ("Demo Script", _public_demo_script_url(), "3 minute walkthrough"),
    ]
    video_url = _public_demo_video_url()
    if video_url:
        links.append(("Demo Video", video_url, "recorded walkthrough"))
    return "".join(
        f'<a class="button" href="{_html(url)}" title="{_html(title)}">{_html(label)}</a>'
        for label, url, title in links
    )


def _precomputed_summary(payload: dict[str, Any] | None, evidence_sha256: str) -> dict[str, Any] | None:
    if not isinstance(payload, dict):
        return None
    summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
    if not summary:
        return None
    return {
        "schema_version": "ui_summary.v1",
        "evidence_sha256": evidence_sha256,
        "status": str(summary.get("status") or "ok"),
        "message": str(summary.get("message") or ""),
        "finding": dict(summary.get("finding") or {}),
        "review": dict(summary.get("review") or {}),
        "providers": dict(summary.get("providers") or {}),
        "baselines": dict(summary.get("baselines") or {}),
        "raw_log_policy": str(summary.get("raw_log_policy") or "unknown"),
        "log_count": int(summary.get("log_count") or 0),
        "canonical_graph_status": str(summary.get("canonical_graph_status") or "precomputed"),
        "canonical_graph_sha256": str(summary.get("canonical_graph_sha256") or ""),
        "input_fingerprint_sha256": str(summary.get("input_fingerprint_sha256") or ""),
        "updated_at": str(payload.get("updated_at") or summary.get("updated_at") or ""),
    }


def _public_manifest_index_path() -> Path:
    return Path(os.environ.get("OES_PUBLIC_MANIFEST_INDEX", "data/public_evidence_manifests/index.json"))


def _public_manifest_paths() -> list[Path]:
    index_path = _public_manifest_index_path()
    try:
        index = json.loads(index_path.read_text(encoding="utf-8"))
    except Exception:
        return []
    paths: list[Path] = []
    for raw_path in index.get("manifests") or []:
        manifest_path = Path(str(raw_path))
        if not manifest_path.is_absolute() and not manifest_path.exists():
            candidate = index_path.parent / manifest_path.name
            if candidate.exists():
                manifest_path = candidate
        paths.append(manifest_path)
    return paths


def _public_manifest_label_for_evidence(evidence_sha256: str) -> str:
    evidence_id = _canonical_precomputed_review_sha(evidence_sha256)
    if not evidence_id:
        return ""
    for manifest_path in _public_manifest_paths():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(manifest, dict):
            continue
        manifest_evidence = _canonical_precomputed_review_sha(str(manifest.get("evidence_sha256") or ""))
        if manifest_evidence != evidence_id:
            continue
        positioning = manifest.get("public_positioning") if isinstance(manifest.get("public_positioning"), dict) else {}
        label = str(positioning.get("title") or manifest.get("title") or "").strip()
        if label:
            return label
    return ""


def _public_finding_impact_text(summary: dict[str, Any] | None, fallback: str) -> str:
    fallback_text = _public_count_text(fallback).strip()
    if fallback_text:
        return fallback_text
    summary = summary if isinstance(summary, dict) else {}
    review = summary.get("review") if isinstance(summary.get("review"), dict) else {}
    providers = summary.get("providers") if isinstance(summary.get("providers"), dict) else {}
    if not review:
        return ""
    primary_targets = int(review.get("primary_targets") or 0)
    validation_targets = int(review.get("validation_targets") or 0)
    provider_success = int(providers.get("success") or 0)
    provider_total = int(providers.get("total") or 0)
    if provider_total:
        provider_text = f"{provider_success} / {provider_total} schema-valid providers returned usable outputs"
    else:
        provider_text = "Recorded provider outputs are available"
    return (
        f"{provider_text}. {_review_target_count_text(primary_targets, validation_targets)} "
        "remain human-gated; incident promotion is not auto-accepted."
    )


def _public_manifest_entries() -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for manifest_path in _public_manifest_paths():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(manifest, dict):
            continue
        evidence_sha = str(manifest.get("evidence_sha256") or "").strip()
        if not evidence_sha:
            continue
        payload = _precomputed_review_payload(evidence_sha) or {}
        summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
        finding = summary.get("finding") if isinstance(summary.get("finding"), dict) else {}
        analysis_context = (
            payload.get("analysis_context") if isinstance(payload.get("analysis_context"), dict) else {}
        )
        review_summary = manifest.get("review_summary") if isinstance(manifest.get("review_summary"), dict) else {}
        sanitized_corpus = (
            manifest.get("sanitized_corpus") if isinstance(manifest.get("sanitized_corpus"), dict) else {}
        )
        token = manifest.get("token_compression") if isinstance(manifest.get("token_compression"), dict) else {}
        provider_summary = (
            manifest.get("provider_summary") if isinstance(manifest.get("provider_summary"), dict) else {}
        )
        summary_providers = summary.get("providers") if isinstance(summary.get("providers"), dict) else {}
        summary_review = summary.get("review") if isinstance(summary.get("review"), dict) else {}
        positioning = manifest.get("public_positioning") if isinstance(manifest.get("public_positioning"), dict) else {}
        case_id = str(manifest.get("case_id") or "")
        landing_role = str(positioning.get("landing_role") or manifest.get("landing_role") or "").strip()
        if not landing_role:
            if case_id == "stream_v3_dell_runtime_real_api":
                landing_role = "primary_review"
            elif case_id == "amazon_notify_real_api":
                landing_role = "guarded_review"
            elif case_id == "stream_v3_arena_monitoring_real_api":
                landing_role = "observation_gap"
            else:
                landing_role = "scale_validation"
        category = {
            "primary_review": "Primary Review",
            "guarded_review": "Guarded Review",
            "observation_gap": "Observation Gap Validation",
            "scale_validation": "Scale Evidence",
        }.get(landing_role, "Scale Evidence")
        entries.append(
            {
                "case_id": case_id,
                "category": category,
                "landing_role": landing_role,
                "landing_rank": int(positioning.get("landing_rank") or manifest.get("landing_rank") or 100),
                "evidence_sha": evidence_sha,
                "title": str(positioning.get("title") or manifest.get("title") or finding.get("title") or "Precomputed review"),
                "finding": str(finding.get("title") or manifest.get("title") or "Precomputed review"),
                "landing_note": str(positioning.get("note") or ""),
                "updated_at": str(payload.get("updated_at") or summary.get("updated_at") or ""),
                "service": str(analysis_context.get("service") or sanitized_corpus.get("service") or ""),
                "environment": str(analysis_context.get("environment") or sanitized_corpus.get("environment") or ""),
                "row_count": int(analysis_context.get("sanitized_log_count") or sanitized_corpus.get("sanitized_row_count") or summary.get("log_count") or 0),
                "window_hours": float(sanitized_corpus.get("analysis_window_hours") or 0.0),
                "evidence_items": int(analysis_context.get("evidence_item_count") or token.get("evidence_item_count") or 0),
                "projected_occurrences": int(analysis_context.get("model_projection_occurrence_count") or token.get("model_projection_occurrence_count") or 0),
                "chunk_count": int(analysis_context.get("provider_full_corpus_chunk_count") or token.get("provider_full_corpus_chunk_count") or 0),
                "full_coverage": float(analysis_context.get("provider_full_corpus_coverage_ratio") or token.get("provider_full_corpus_coverage_ratio") or 0.0),
                "provider_count": int(summary_providers.get("total") or provider_summary.get("provider_count") or 0),
                "schema_valid_count": int(summary_providers.get("success") or provider_summary.get("schema_valid_provider_count") or 0),
                "primary_targets": int(summary_review.get("primary_targets") or review_summary.get("primary_targets") or 0),
                "validation_targets": int(summary_review.get("validation_targets") or review_summary.get("validation_targets") or 0),
            }
        )
    return entries


def _archived_real_api_rows(curated_shas: set[str]) -> list[tuple[str, str, str, str]]:
    rows: list[tuple[str, str, str, str]] = []
    seen: set[str] = set()
    for directory in _precomputed_review_dirs():
        try:
            paths = sorted(directory.glob("*.json"))
        except Exception:
            continue
        for path in paths:
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            if not isinstance(payload, dict):
                continue
            evidence_sha = str(payload.get("evidence_sha256") or path.stem)
            if not evidence_sha or evidence_sha in seen or evidence_sha in curated_shas:
                continue
            generation = payload.get("generation") if isinstance(payload.get("generation"), dict) else {}
            provider_mode = str(generation.get("provider_mode") or "")
            if provider_mode and not provider_mode.startswith("real_api"):
                continue
            seen.add(evidence_sha)
            summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
            finding = summary.get("finding") if isinstance(summary.get("finding"), dict) else {}
            analysis_context = (
                payload.get("analysis_context")
                if isinstance(payload.get("analysis_context"), dict)
                else {}
            )
            profile_context = (
                payload.get("profile_context")
                if isinstance(payload.get("profile_context"), dict)
                else {}
            )
            providers = summary.get("providers") if isinstance(summary.get("providers"), dict) else {}
            profile_id = str(profile_context.get("profile_id") or "approved profile")
            source_attached = bool(analysis_context.get("source_context_sha256"))
            analysis_attached = bool(analysis_context.get("source_analysis_sha256"))
            context_note = (
                "Sanitized source context attached; "
                f"source analysis {'attached' if analysis_attached else 'not attached'}; "
                f"profile={profile_id}; "
                f"providers={int(providers.get('success') or 0)}/{int(providers.get('total') or 0)}"
                if source_attached
                else (
                    "Sanitized log review without source profile context; "
                    f"providers={int(providers.get('success') or 0)}/{int(providers.get('total') or 0)}"
                )
            )
            rows.append(
                (
                    evidence_sha,
                    str(finding.get("title") or "Precomputed review"),
                    str(payload.get("updated_at") or summary.get("updated_at") or ""),
                    context_note,
                )
            )
    rows.sort(key=lambda row: row[2], reverse=True)
    return rows


def _review_card_html(entry: dict[str, Any], *, featured: bool = False) -> str:
    evidence_sha = str(entry.get("evidence_sha") or "")
    evidence = quote(evidence_sha)
    detail_url = f"/ui/full-review-page?evidence_sha256={evidence}"
    api_url = f"/ui/api?evidence_sha256={evidence}"
    graph_url = f"/ui/review-graph?evidence_sha256={evidence}"
    report_url = f"/ui/report.md?evidence_sha256={evidence}"
    row_count = _human_count(int(entry.get("row_count") or 0))
    evidence_items = _human_count(int(entry.get("evidence_items") or 0))
    chunk_count = int(entry.get("chunk_count") or 0)
    coverage = f"{float(entry.get('full_coverage') or 0.0) * 100:.1f}%"
    window_hours = float(entry.get("window_hours") or 0.0)
    window_label = f"{window_hours:.1f}h" if window_hours else "fixed window"
    target_count = int(entry.get("primary_targets") or 0) + int(entry.get("validation_targets") or 0)
    provider_count = int(entry.get("provider_count") or 0)
    schema_valid_count = int(entry.get("schema_valid_count") or 0)
    primary_count = int(entry.get("primary_targets") or 0)
    projected_occurrences = int(entry.get("projected_occurrences") or 0)
    occurrence_note = (
        f"{_human_count(projected_occurrences)} counted occurrence(s) represented"
        if projected_occurrences
        else ""
    )
    note = str(entry.get("landing_note") or "").strip()
    note_html = f"<p>{_html(note)}</p>" if note else ""
    occurrence_html = f"<small>{_html(occurrence_note)}</small>" if occurrence_note else ""
    category = str(entry.get("category") or "Review")
    category_class = {
        "Primary Review": "review-card--primary",
        "Guarded Review": "review-card--guarded",
        "Observation Gap Validation": "review-card--observation",
    }.get(category, "review-card--default")
    badge = "Primary Review" if featured else category
    status_badge = (
        f'<span class="status-badge">{_html(f"要確認 {target_count}件")}</span>'
        if category == "Observation Gap Validation"
        else ""
    )
    title = str(entry.get("title") or "Precomputed review")
    return f"""
      <article class="review-card {category_class}{' featured' if featured else ''}">
        <a class="review-card-main" href="{_html(detail_url)}" aria-label="{_html(f'Open {title} detail review')}">
          <div class="card-topline">
            <span class="badge card-tag">{_html(badge)}</span>
            {status_badge}
            <span class="sha">{_html(evidence_sha[:12])}</span>
            <span class="card-arrow" aria-hidden="true">↗</span>
          </div>
          <h3>{_html(title)}</h3>
          <p>{_html(str(entry.get("finding") or ""))}</p>
          {note_html}
          <dl class="metrics">
            <div><dt>Rows</dt><dd>{row_count}</dd></div>
            <div><dt>Window</dt><dd>{_html(window_label)}</dd></div>
            <div><dt>Evidence Items</dt><dd>{evidence_items}</dd>{occurrence_html}</div>
            <div><dt>Chunks</dt><dd>{chunk_count}</dd></div>
            <div><dt>Coverage</dt><dd>{coverage}</dd></div>
            <div><dt>Providers</dt><dd>{schema_valid_count}/{provider_count}</dd></div>
            <div><dt>Primary</dt><dd>{primary_count}</dd></div>
            <div><dt>Review Targets</dt><dd>{target_count}</dd></div>
          </dl>
        </a>
        <div class="actions" aria-label="{_html(f'Related links for {title}')}">
          <a href="{_html(detail_url)}">Detail</a>
          <a href="{_html(api_url)}">API</a>
          <a href="{_html(graph_url)}">Graph</a>
          <a href="{_html(report_url)}">Report</a>
        </div>
      </article>
    """


def _scale_proof_html(entries: list[dict[str, Any]]) -> str:
    if not entries:
        return ""
    rows = sum(int(row.get("row_count") or 0) for row in entries)
    chunks = sum(int(row.get("chunk_count") or 0) for row in entries)
    primary_targets = sum(int(row.get("primary_targets") or 0) for row in entries)
    target_count = sum(
        int(row.get("primary_targets") or 0) + int(row.get("validation_targets") or 0)
        for row in entries
    )
    services = sorted({str(row.get("service") or row.get("case_id") or "review") for row in entries})
    provider_sets = sorted(
        {
            f"{int(row.get('schema_valid_count') or 0)}/{int(row.get('provider_count') or 0)}"
            for row in entries
            if int(row.get("provider_count") or 0)
        }
    )
    provider_label = ", ".join(provider_sets) if provider_sets else "recorded"
    return f"""
      <aside class="scale-proof" aria-label="Scale proof summary">
        <div class="scale-proof-copy">
          <span class="badge">Scale proof</span>
          <strong>{len(entries)} recorded domains, one evidence-gated review contract.</strong>
          <p>The cards below are the actual review artifacts. This is a scale summary of those three recorded runs, not a separate fourth run.</p>
        </div>
        <dl class="scale-proof-grid">
          <div><dt>Corpora</dt><dd>{len(entries)}</dd></div>
          <div><dt>Services</dt><dd>{_html(', '.join(services))}</dd></div>
          <div><dt>Rows</dt><dd>{_human_count(rows)}</dd></div>
          <div><dt>Chunks</dt><dd>{_human_count(chunks)}</dd></div>
          <div><dt>Coverage</dt><dd>100.0%</dd></div>
          <div><dt>Providers</dt><dd>{_html(provider_label)}</dd></div>
          <div><dt>Primary</dt><dd>{primary_targets}</dd></div>
          <div><dt>Review Targets</dt><dd>{target_count}</dd></div>
        </dl>
      </aside>
    """


def _archived_links_html(rows: list[tuple[str, str, str, str]]) -> str:
    links = "\n".join(
        (
            "<li>"
            f"<a href='/?evidence_sha256={quote(evidence_sha)}'>{_html(title)}</a>"
            f"<span>{_html(evidence_sha[:12])}</span>"
            f"<small>{_html(updated_at)}</small>"
            f"<small>{_html(context_note)}</small>"
            "</li>"
        )
        for evidence_sha, title, updated_at, context_note in rows
    )
    if not links:
        links = "<li><span>No precomputed review is available.</span></li>"
    return links


def _public_precomputed_landing_page() -> str:
    manifest_entries = _public_manifest_entries()
    manifest_entries.sort(key=lambda row: (int(row.get("landing_rank") or 100), str(row.get("title") or "")))
    curated_shas = {str(row.get("evidence_sha") or "") for row in manifest_entries}
    archived_rows = _archived_real_api_rows(curated_shas)
    primary_entries = [row for row in manifest_entries if row.get("category") == "Primary Review"]
    guarded_entries = [row for row in manifest_entries if row.get("category") == "Guarded Review"]
    observation_entries = [row for row in manifest_entries if row.get("category") == "Observation Gap Validation"]
    review_set_entries = primary_entries + guarded_entries + observation_entries
    primary_cards = "\n".join(_review_card_html(row, featured=True) for row in primary_entries)
    guarded_cards = "\n".join(_review_card_html(row) for row in guarded_entries)
    observation_cards = "\n".join(_review_card_html(row) for row in observation_entries)
    scale_proof = _scale_proof_html(review_set_entries)
    if not primary_cards:
        primary_cards = "<p>No primary review is available.</p>"
    if not guarded_cards:
        guarded_cards = "<p>No guarded review is available.</p>"
    if not observation_cards:
        observation_cards = "<p>No observation gap review is available.</p>"
    rescore_demo_ids = _public_rescore_demo_ids()
    archive_section = (
        "<details class='archive'><summary>Archived recorded runs</summary>"
        f"<ul>{_archived_links_html(archived_rows)}</ul>"
        "</details>"
        if archived_rows
        else ""
    )
    primary_evidence = str(primary_entries[0].get("evidence_sha") or "") if primary_entries else ""
    primary_detail_url = (
        f"/ui/full-review-page?evidence_sha256={_url_quote(primary_evidence)}"
        if primary_evidence
        else "#review-set"
    )
    primary_report_url = (
        f"/ui/report.md?evidence_sha256={_url_quote(primary_evidence)}"
        if primary_evidence
        else "#review-set"
    )
    primary_graph_url = (
        f"/ui/review-graph?evidence_sha256={_url_quote(primary_evidence)}"
        if primary_evidence
        else "#review-set"
    )
    rescore_demo_id = rescore_demo_ids[0] if rescore_demo_ids else ""
    rescore_demo_url = f"/ui/rescore-demo?id={_url_quote(rescore_demo_id)}" if rescore_demo_id else "#improvement-loop"
    operation_links = "\n".join(
        [
            (
                f"<a class='loop-link' href='{_html(rescore_demo_url)}'>"
                "<strong>More data rescore demo</strong>"
                f"<span>{_html(rescore_demo_id or 'rescore-demo')}</span>"
                "<small>needs_more_data -&gt; evidence_collected</small>"
                "</a>"
            ),
            (
                f"<a class='loop-link' href='{_html(primary_report_url)}'>"
                "<strong>Markdown incident report</strong>"
                "<span>human-gated review artifact</span>"
                "<small>states provider status, blockers, and human questions</small>"
                "</a>"
            ),
            (
                f"<a class='loop-link' href='{_html(primary_graph_url)}'>"
                "<strong>Review graph</strong>"
                "<span>canonical target projection</span>"
                "<small>shows support, validation, and missing-evidence boundaries</small>"
                "</a>"
            ),
        ]
    )
    primary_entry = primary_entries[0] if primary_entries else {}
    gate_provider_total = int(primary_entry.get("provider_count") or 5)
    gate_provider_success = int(primary_entry.get("schema_valid_count") or gate_provider_total or 5)
    gate_signal_label = f"{gate_provider_success} / {gate_provider_total}"
    total_public_rows = sum(int(row.get("row_count") or 0) for row in manifest_entries)
    total_public_corpora = len(manifest_entries)
    primary_rows = _human_count(int(primary_entry.get("row_count") or 0)) if primary_entry else "45,000"
    primary_candidate_count = int(primary_entry.get("primary_targets") or 0)
    primary_review_targets = int(primary_entry.get("primary_targets") or 0) + int(primary_entry.get("validation_targets") or 0)
    return f"""
    <!doctype html>
    <html lang="en">
      <head>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <title>Ops Evidence Synthesis - Evidence before certainty</title>
        <style>
          :root {{
            color-scheme: light;
            --bg: #f4f2ec;
            --bg-2: #faf8f2;
            --surface: #fffdf8;
            --surface-2: #f7f3e9;
            --ink: #1c1a15;
            --ink-2: #4a463d;
            --ink-3: #7a746a;
            --muted: #8a857a;
            --border: #e5dfd1;
            --border-strong: #cfc7b6;
            --blue: #3f63a8;
            --blue-soft: #eef2f9;
            --green: #2f8a5b;
            --green-soft: #eef7f1;
            --gold: #a7845a;
            --orange: #d1622b;
            --shadow: 0 18px 48px rgba(60, 50, 30, .12);
            --mono: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
            --serif: Georgia, "Times New Roman", serif;
          }}
          * {{ box-sizing: border-box; }}
          html {{ scroll-behavior: smooth; }}
          body {{
            margin: 0;
            min-height: 100vh;
            background: var(--bg);
            color: var(--ink);
            font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
            letter-spacing: 0;
          }}
          a {{ color: inherit; text-decoration: none; }}
          p {{ margin: 0; color: var(--ink-2); line-height: 1.58; }}
          .wrap {{
            width: min(calc(100% - 48px), 1240px);
            margin: 0 auto;
          }}
          .topbar-shell {{
            position: sticky;
            top: 0;
            z-index: 20;
            border-bottom: 1px solid var(--border);
            background: rgba(250, 248, 242, .92);
            backdrop-filter: blur(8px);
          }}
          .topbar {{
            min-height: 66px;
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 18px;
            padding: 14px 0;
          }}
          .brand {{ display: flex; align-items: center; gap: 11px; min-width: 240px; font-weight: 800; }}
          .brand-mark {{
            width: 28px;
            height: 28px;
            border-radius: 7px;
            display: grid;
            place-items: center;
            background: var(--ink);
            color: var(--bg);
            font: 800 11px/1 var(--mono);
          }}
          .nav-links {{ display: flex; align-items: center; flex-wrap: wrap; gap: 22px; color: #6a655b; font-size: 13.5px; }}
          .nav-links a {{ color: #6a655b; }}
          .nav-links a:hover {{ color: var(--ink); }}
          .live-pill {{
            display: inline-flex;
            align-items: center;
            gap: 7px;
            border: 1px solid #bfe0cd;
            border-radius: 999px;
            padding: 6px 11px;
            background: var(--green-soft);
            color: var(--green);
            font: 800 11.5px/1 var(--mono);
          }}
          .live-dot {{ width: 7px; height: 7px; border-radius: 50%; background: var(--green); }}
          .hero {{
            padding: 58px 0 40px;
            text-align: center;
          }}
          .eyebrow {{
            color: var(--gold);
            font: 800 12px/1 var(--mono);
            letter-spacing: .14em;
            text-transform: uppercase;
            margin-bottom: 24px;
          }}
          h1 {{
            margin: 0 auto 24px;
            max-width: 1320px;
            color: var(--ink);
            font-family: var(--serif);
            font-size: clamp(54px, 5.2vw, 80px);
            font-style: italic;
            font-weight: 500;
            line-height: .98;
            letter-spacing: 0;
          }}
          .hero-tagline {{
            margin: -8px auto 18px;
            color: var(--ink-3);
            font-size: 13.5px;
            font-weight: 700;
          }}
          .hero-lead {{
            max-width: 880px;
            margin: 0 auto 14px;
            color: var(--ink-2);
            font-size: 19px;
            line-height: 1.55;
          }}
          .hero-sub {{
            max-width: 820px;
            margin: 0 auto 34px;
            color: var(--ink-3);
            font-size: 15px;
            line-height: 1.6;
          }}
          .hero-cta {{
            display: inline-flex;
            justify-content: center;
            flex-wrap: wrap;
            gap: 11px;
            margin-bottom: 46px;
          }}
          .button {{
            align-items: center;
            border: 1px solid var(--border-strong);
            border-radius: 10px;
            background: transparent;
            color: var(--ink);
            padding: 12px 22px;
            font-size: 14.5px;
            font-weight: 700;
          }}
          .button.primary {{ background: var(--ink); border-color: var(--ink); color: var(--bg); }}
          .button:hover {{ border-color: var(--ink); background: #efe9db; }}
          .button.primary:hover {{ background: #35322a; color: var(--bg); }}
          .hero-stats {{
            display: grid;
            grid-template-columns: repeat(4, minmax(0, 1fr));
            gap: 36px;
            max-width: 880px;
            margin: 0 auto;
            padding-top: 28px;
            border-top: 1px solid var(--border);
          }}
          .hero-stats b {{ display: block; color: var(--ink); font-size: 27px; line-height: 1; }}
          .hero-stats span {{ display: block; margin-top: 5px; color: var(--muted); font-size: 11.5px; }}
          .showcase {{
            width: min(calc(100% - 48px), 1200px);
            margin: 0 auto;
            padding: 12px 0 60px;
          }}
          .showcase-kicker {{ text-align: center; color: var(--gold); font: 800 12px/1 var(--mono); letter-spacing: .12em; text-transform: uppercase; margin-bottom: 18px; }}
          .browser {{
            display: block;
            overflow: hidden;
            border: 1px solid #d5dbe3;
            border-radius: 16px;
            background: #eef1f5;
            box-shadow: 0 40px 90px -44px rgba(30,45,70,.55);
          }}
          .browser-bar {{ display: flex; align-items: center; gap: 8px; padding: 13px 18px; background: #fff; border-bottom: 1px solid #e4e9ef; }}
          .browser-dot {{ width: 11px; height: 11px; border-radius: 50%; background: #e6a5a0; }}
          .browser-dot:nth-child(2) {{ background: #ecc98f; }}
          .browser-dot:nth-child(3) {{ background: #a7d4ad; }}
          .browser-url {{ margin-left: 8px; flex: 1; max-width: 560px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; border: 1px solid #e4e9ef; border-radius: 6px; background: #f2f4f7; color: #8b96a3; padding: 5px 13px; font: 700 12px/1 var(--mono); }}
          .browser-chip {{ color: #3b74d6; background: #eaf1fc; padding: 5px 11px; border-radius: 6px; font: 800 11px/1 var(--mono); }}
          .workspace {{ display: grid; grid-template-columns: 236px minmax(0, 1.2fr) minmax(250px, 1fr); gap: 16px; padding: 22px; }}
          .queue-label, .mock-label {{ color: #9aa4b1; font-size: 11px; letter-spacing: .08em; text-transform: uppercase; margin-bottom: 11px; }}
          .queue {{ display: grid; gap: 7px; }}
          .queue-row {{ border: 1px solid #e9edf2; border-radius: 8px; background: #fff; padding: 10px 12px; }}
          .queue-row.active {{ border-color: #cdd8ec; border-left: 3px solid #3b74d6; }}
          .queue-row div {{ display: flex; justify-content: space-between; gap: 10px; }}
          .queue-row b {{ color: #141c2b; font-size: 13px; overflow-wrap: anywhere; }}
          .queue-row code {{ color: #141c2b; font: 800 12.5px/1 var(--mono); }}
          .queue-row span {{ display: block; margin-top: 3px; color: #3b74d6; font-size: 10.5px; }}
          .mock-panel {{ border: 1px solid #e4e9ef; border-radius: 10px; background: #fff; padding: 17px; }}
          .mock-panel.soft {{ background: #fbfcfd; }}
          .mock-title {{ display: flex; align-items: center; gap: 8px; margin-bottom: 11px; }}
          .mock-title b {{ color: #141c2b; font-size: 14.5px; }}
          .mock-score {{ margin-left: auto; color: #141c2b; font-size: 19px; font-weight: 900; }}
          .mock-pill {{ color: #3b74d6; background: #eaf1fc; padding: 4px 8px; border-radius: 5px; font: 800 10.5px/1 var(--mono); }}
          .mock-copy {{ color: #3a4453; font-size: 14px; line-height: 1.55; }}
          .provider-bars {{ display: grid; gap: 7px; }}
          .provider-bars div {{ display: grid; grid-template-columns: 72px minmax(0, 1fr) 58px; align-items: center; gap: 9px; }}
          .provider-bars span:first-child {{ color: #3a4453; font: 800 11px/1 var(--mono); }}
          .provider-bars i {{ height: 7px; border-radius: 4px; background: #6d9be0; }}
          .provider-bars small {{ color: #77828f; font-size: 10px; }}
          .evidence-list {{ display: grid; gap: 7px; }}
          .evidence-list div {{ display: flex; justify-content: space-between; align-items: center; border: 1px solid #e9edf2; border-radius: 7px; background: #fff; padding: 8px 11px; }}
          .evidence-list code {{ color: #3a4453; font: 800 12px/1 var(--mono); }}
          .evidence-list span {{ color: #77828f; font-size: 10.5px; }}
          .human-gate {{ margin-top: 12px; border: 1px solid #cfe6da; border-radius: 10px; background: linear-gradient(180deg,#f0f7f2,#fff); padding: 15px; }}
          .human-top {{ display: flex; align-items: center; gap: 9px; }}
          .hg {{ width: 33px; height: 33px; border: 1px solid #bfe0cd; border-radius: 8px; display: grid; place-items: center; color: var(--green); font: 900 11px/1 var(--mono); }}
          .human-top b {{ color: #141c2b; font-size: 13.5px; }}
          .human-state {{ margin-left: auto; color: var(--green); background: #e4f4eb; padding: 4px 8px; border-radius: 5px; font: 800 10px/1 var(--mono); }}
          .band {{ background: var(--bg-2); border-top: 1px solid var(--border); border-bottom: 1px solid var(--border); }}
          .band .wrap {{ padding: 52px 0; }}
          .pipeline {{ display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 16px; }}
          .pipe-card {{ border: 1px solid #e7e0d1; border-radius: 13px; background: var(--surface); padding: 20px; }}
          .pipe-card.final {{ background: var(--blue-soft); border-color: #cdd8ec; }}
          .pipe-num {{ color: #c0a875; font: 800 12px/1 var(--mono); }}
          .pipe-card strong {{ display: block; margin-top: 8px; color: var(--ink); font-size: 16px; }}
          .pipe-card p {{ margin-top: 7px; color: var(--ink-3); font-size: 13px; }}
          section {{ padding: 56px 0; }}
          .section-head {{
            display: flex;
            justify-content: space-between;
            align-items: end;
            gap: 18px;
            margin-bottom: 22px;
          }}
          .kicker {{ color: var(--gold); font: 800 12px/1 var(--mono); letter-spacing: .12em; text-transform: uppercase; }}
          h2 {{ margin: 8px 0 0; color: var(--ink); font-family: var(--serif); font-size: 36px; font-weight: 500; letter-spacing: 0; }}
          .section-note {{ max-width: 660px; color: var(--ink-3); font-size: 13px; line-height: 1.55; text-align: right; }}
          .review-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(310px, 1fr)); gap: 18px; }}
          .review-card {{
            display: block;
            position: relative;
            overflow: hidden;
            border: 1px solid #e7e0d1;
            border-radius: 14px;
            background: var(--surface);
            box-shadow: 0 1px 2px rgba(40, 34, 20, .04);
            transition: transform .2s ease-out, box-shadow .2s ease-out, border-color .2s ease-out;
          }}
          .review-card:hover, .review-card:focus-within {{
            border-color: #6d8cc9;
            transform: translateY(-3px);
            box-shadow: 0 14px 32px -22px rgba(50,42,28,.5);
          }}
          .review-card--primary {{ border-color: #d3dcee; }}
          .review-card--guarded {{ border-color: #e4d7bd; }}
          .review-card--guarded::before {{
            content: "";
            position: absolute;
            inset: 0 auto 0 0;
            width: 3px;
            background: #c99335;
          }}
          .review-card--guarded .card-tag {{
            border-color: #e1c47d;
            background: #fff4d7;
            color: #9a681b;
          }}
          .review-card--observation {{ border-color: #d7dfeb; }}
          .review-card.featured {{ grid-column: 1 / -1; }}
          .review-card-main {{
            display: grid;
            gap: 12px;
            padding: 22px 52px 14px 22px;
            color: inherit;
            text-decoration: none;
          }}
          .review-card-main:focus-visible {{
            outline: 2px solid #5c7fc4;
            outline-offset: -4px;
            border-radius: 13px;
          }}
          .review-card h3 {{ margin: 0; color: var(--ink); font-size: 17px; line-height: 1.35; letter-spacing: 0; }}
          .review-card p {{ font-size: 13px; }}
          .scale-proof {{
            display: grid;
            grid-template-columns: minmax(260px, .92fr) minmax(0, 1.4fr);
            gap: 18px;
            margin: 0 0 34px;
            padding: 20px;
            border: 1px solid #d8cfbd;
            border-radius: 14px;
            background: #efe9db;
          }}
          .scale-proof-copy {{ display: grid; align-content: start; gap: 10px; }}
          .scale-proof-copy strong {{ color: var(--ink); font-size: 17px; line-height: 1.35; }}
          .scale-proof-copy p {{ color: var(--ink-3); font-size: 13px; }}
          .scale-proof-grid {{ display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 10px; margin: 0; }}
          .scale-proof-grid div {{ min-width: 0; border-top: 1px solid #d2c8b7; padding-top: 10px; }}
          .card-topline {{ display: flex; gap: 8px; align-items: center; flex-wrap: wrap; padding-right: 8px; }}
          .card-arrow {{
            position: absolute;
            top: 18px;
            right: 18px;
            color: var(--blue);
            opacity: .46;
            font: 900 18px/1 var(--sans);
            transition: transform .2s ease-out, opacity .2s ease-out;
          }}
          .review-card:hover .card-arrow, .review-card:focus-within .card-arrow {{
            opacity: 1;
            transform: translate(2px, -2px);
          }}
          .badge {{
            display: inline-flex;
            border: 1px solid #cdd8ec;
            border-radius: 999px;
            padding: 6px 10px;
            background: var(--blue-soft);
            color: var(--blue);
            font-size: 11px;
            font-weight: 800;
          }}
          .status-badge {{
            display: inline-flex;
            border: 1px solid #cfd9e8;
            border-radius: 999px;
            padding: 6px 9px;
            background: #f4f7fb;
            color: #475b7a;
            font-size: 11px;
            font-weight: 850;
          }}
          .sha, small {{ color: var(--muted); font-family: var(--mono); font-size: 11.5px; }}
          .metrics {{ display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 10px; margin: 4px 0 0; }}
          .metrics div {{ border-top: 1px solid var(--border); padding-top: 10px; min-width: 0; }}
          dt {{ color: var(--muted); font-size: 10px; text-transform: uppercase; letter-spacing: .06em; }}
          dd {{ margin: 3px 0 0; font-weight: 820; overflow-wrap: anywhere; }}
          .actions {{ display: flex; flex-wrap: wrap; gap: 8px; padding: 0 22px 22px; }}
          .actions a, .loop-link {{
            display: inline-grid;
            gap: 3px;
            border: 1px solid var(--border);
            border-radius: 8px;
            padding: 9px 11px;
            background: #fbf7ed;
            color: var(--ink-2);
            font-size: 12px;
            font-weight: 800;
          }}
          .mode-grid, .criteria-grid, .loop-grid {{ display: grid; gap: 14px; }}
          .mode-grid {{ grid-template-columns: repeat(4, minmax(0, 1fr)); }}
          .criteria-grid {{ grid-template-columns: repeat(5, minmax(0, 1fr)); }}
          .mode-card, .criteria-card {{
            border-top: 2px solid var(--ink);
            padding-top: 14px;
          }}
          .mode-card strong, .criteria-card strong {{ display: block; color: var(--ink); font-size: 14.5px; }}
          .mode-card span {{ display: block; margin-top: 6px; color: var(--blue); font: 800 11px/1.35 var(--mono); }}
          .mode-card p, .criteria-card p {{ margin-top: 8px; color: var(--ink-3); font-size: 12.5px; }}
          .loop-grid {{ grid-template-columns: repeat(3, minmax(0, 1fr)); }}
          .archive {{ margin: 0 0 56px; border-top: 1px solid var(--border); padding-top: 18px; }}
          summary {{ cursor: pointer; color: var(--ink-2); font-weight: 800; }}
          ul {{ list-style: none; padding: 0; margin: 14px 0 0; display: grid; gap: 10px; }}
          li {{ display: grid; gap: 4px; padding: 12px 14px; border: 1px solid var(--border); border-radius: 8px; background: var(--surface); }}
          @media (max-width: 1180px) {{
            .workspace {{ grid-template-columns: 1fr; }}
            .mode-grid, .criteria-grid, .loop-grid {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
          }}
          @media (max-width: 900px) {{
            .wrap, .showcase {{ width: min(calc(100% - 32px), 1240px); }}
            .topbar {{ align-items: flex-start; flex-direction: column; }}
            .nav-links {{ gap: 10px; }}
            .hero {{ padding-top: 54px; }}
            .hero-stats, .pipeline {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
            .metrics {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
            .section-head {{ align-items: flex-start; flex-direction: column; }}
            .section-note {{ text-align: left; }}
            .scale-proof {{ grid-template-columns: 1fr; }}
            .scale-proof-grid {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
          }}
          @media (max-width: 520px) {{
            h1 {{ font-size: 48px; }}
            .hero-stats, .pipeline, .mode-grid, .criteria-grid, .loop-grid {{ grid-template-columns: 1fr; }}
            .scale-proof-grid {{ grid-template-columns: 1fr; }}
            .hero-cta {{ display: grid; }}
            .browser-bar {{ flex-wrap: wrap; }}
            .provider-bars div {{ grid-template-columns: 64px minmax(0, 1fr); }}
            .provider-bars small {{ display: none; }}
          }}
        </style>
      </head>
      <body>
        <header class="topbar-shell">
          <div class="wrap topbar">
            <a class="brand" href="/">
              <span class="brand-mark">OE</span>
              <span>Ops Evidence Synthesis</span>
            </a>
            <nav class="nav-links" aria-label="Primary">
              <a href="#review-set">Review Set</a>
              <a href="#judging-map">Judging Map</a>
              <a href="#improvement-loop">Improvement Loop</a>
              <a href="{_html(_public_repo_url())}">GitHub</a>
              <span class="live-pill"><span class="live-dot"></span>Cloud Run live</span>
            </nav>
          </div>
        </header>
        <main>
          <section class="wrap hero">
            <div class="eyebrow">Evidence before certainty</div>
            <h1>Trace any AI claim to its evidence.</h1>
            <p class="hero-tagline" data-ja="AIが断定する前に、運用証拠を固定する。">Freeze operational evidence before the model sounds certain.</p>
            <p class="hero-lead">Do not trust the summary. Trace it. Every AI verdict breaks into cited Evidence IDs, provider stances, missing evidence, and a human gate.</p>
            <p class="hero-sub">This public entry serves read-only precomputed reviews, sanitized logs, sanitized source context, and approved system profiles. Raw logs stay local; sanitized bundles reach Google Cloud for review. Provider convergence creates review targets, not accepted incident causes.</p>
            <div class="hero-cta">
              <a class="button primary" href="{_html(primary_detail_url)}">Open flagship review -&gt;</a>
              <a class="button" href="/ui/fast-gcp-review">Run Fast GCP Review</a>
              <a class="button" href="{_html(primary_report_url)}">Read incident report</a>
              <a class="button" href="{_html(rescore_demo_url)}">Watch rescore loop</a>
              {_public_global_action_links_html()}
            </div>
            <div class="hero-stats" aria-label="Public evidence summary">
              <div><b>{_html(primary_rows)}</b><span>rows analyzed</span></div>
              <div><b>{_html(gate_signal_label)}</b><span>providers - provider signal, not a verdict</span></div>
              <div><b>{primary_candidate_count}</b><span>primary candidates in flagship review</span></div>
              <div><b>0</b><span aria-label="0 AUTO-PROMOTED CAUSES">AUTO-PROMOTED CAUSES</span></div>
            </div>
          </section>
          <section class="showcase" aria-label="Primary review workspace preview">
            <div class="showcase-kicker">Primary review workspace - real API</div>
            <a class="browser" href="{_html(primary_detail_url)}">
              <div class="browser-bar">
                <span class="browser-dot"></span><span class="browser-dot"></span><span class="browser-dot"></span>
                <span class="browser-url">ops-evidence.yukimurata0421.dev/ui/full-review-page</span>
                <span class="browser-chip">real API</span>
              </div>
              <div class="workspace">
                <div>
                  <div class="queue-label">Review queue - {primary_review_targets}</div>
                  <div class="queue">
                    <div class="queue-row active"><div><b>transport_sender</b><code>0.86</code></div><span>validation - 5/5</span></div>
                    <div class="queue-row"><div><b>generic_runtime</b><code>0.86</code></div><span>validation - 4/5</span></div>
                    <div class="queue-row"><div><b>job_configuration</b><code>0.80</code></div><span>validation - 3/5</span></div>
                    <div class="queue-row"><div><b>resource_pressure</b><code>0.80</code></div><span>validation - 3/5</span></div>
                    <small>+ {max(0, primary_review_targets - 4)} targets</small>
                  </div>
                </div>
                <div>
                  <div class="mock-panel">
                    <div class="mock-title"><span class="mock-pill">VALIDATION TARGET</span><b>transport_sender</b><span class="mock-score">0.86</span></div>
                    <p class="mock-copy">Transport sender has provider convergence, but no impact outcome is accepted. The target stays in validation until user-impact evidence is attached.</p>
                  </div>
                  <div class="mock-panel soft" style="margin-top:12px">
                    <div class="mock-label">provider convergence - 5 claimed / 0 silent</div>
                    <div class="provider-bars">
                      <div><span>Gemini</span><i></i><small>arbiter</small></div>
                      <div><span>Gemma 4</span><i></i><small>claimed</small></div>
                      <div><span>Mistral</span><i></i><small>claimed</small></div>
                      <div><span>Qwen</span><i></i><small>claimed</small></div>
                      <div><span>GPT-OSS</span><i></i><small>claimed</small></div>
                    </div>
                  </div>
                </div>
                <div>
                  <div class="mock-panel soft">
                    <div class="mock-label">cited evidence</div>
                    <div class="evidence-list">
                      <div><code>PATTERN-172</code><span>CRITICAL</span></div>
                      <div><code>PATTERN-016</code><span>renderer crash</span></div>
                      <div><code>PATTERN-173</code><span>GPU crash</span></div>
                      <small>+ 6 refs</small>
                    </div>
                  </div>
                  <div class="human-gate">
                    <div class="human-top"><span class="hg">HG</span><b>Human-gated</b><span class="human-state">NOT PROMOTED</span></div>
                    <p style="margin-top:9px;font-size:12.5px">Convergence is support, not a verdict.</p>
                    <p style="margin-top:9px;color:var(--green);font:800 11px/1.3 var(--mono)">next -&gt; attach user-impact evidence?</p>
                  </div>
                </div>
              </div>
            </a>
          </section>
          <div class="band" id="loop">
            <section class="wrap">
              <div class="kicker">How the trust holds</div>
              <h2>Local-first in. Human-gated out.</h2>
              <div class="pipeline" style="margin-top:26px">
                <article class="pipe-card"><div class="pipe-num">01</div><strong>Raw logs stay local</strong><p>Sanitized on your machine. raw_log_policy = not_uploaded.</p></article>
                <article class="pipe-card"><div class="pipe-num">02</div><strong>Bundle to Google Cloud</strong><p>Sanitized evidence and sanitized source context reach the review path. Nothing raw does.</p></article>
                <article class="pipe-card"><div class="pipe-num">03</div><strong>Five models review</strong><p>Gemini, Gemma, Mistral, Qwen, GPT-OSS. Agreement is support, not truth.</p></article>
                <article class="pipe-card final"><div class="pipe-num">04</div><strong>A human says yes</strong><p>Impact and cause stay human-owned. 0 auto-accepted causes.</p></article>
              </div>
            </section>
          </div>
          <section id="review-modes" class="wrap">
            <div class="section-head">
              <div>
                <div class="kicker">Review modes</div>
                <h2>Replay path for reproducibility, AI path for real evidence.</h2>
              </div>
              <p class="section-note">The public URL serves recorded artifacts immediately. Deterministic replay proves reproducibility; real provider runs preserve evidence boundaries before action.</p>
            </div>
            <div class="mode-grid">
              <article class="mode-card"><strong>Public Replay</strong><span>deterministic local</span><p>Replays the public 6,506-line sanitized fixture without external AI API keys; measured review graph generation is about 11 seconds.</p></article>
              <article class="mode-card"><strong>More Data Rescore</strong><span>evidence promotion demo</span><p>Shows `validation_target -&gt; primary_candidate` in about 1 second while the human gate remains explicit.</p></article>
              <article class="mode-card"><strong>Fast GCP Review</strong><span>Gemini Flash Lite</span><p>Runs a fixed sanitized amazon-notify sample from Cloud Run through Vertex Gemini Flash Lite and returns a review URL with measured wall time.</p></article>
              <article class="mode-card"><strong>Full Forensic AI Review</strong><span>45k-50k rows</span><p>Uses precomputed artifacts from larger real ops corpora for deep multi-provider synthesis. ADK-compatible trace included.</p></article>
            </div>
          </section>
          <section id="review-set" class="wrap">
            <div class="section-head">
              <div>
                <div class="kicker">Public review set</div>
                <h2>Real API runs. Full-corpus ledgers.</h2>
              </div>
              <p class="section-note">{_human_count(total_public_corpora)} public corpora - {_human_count(total_public_rows)} rows. Every sanitized DB row is assigned to the coverage ledger before provider chunking.</p>
            </div>
            {scale_proof}
            <h2>Primary Review</h2>
            <div class="review-grid">{primary_cards}</div>
          </section>
          <section class="wrap">
            <h2>Guarded Review</h2>
            <div class="review-grid">{guarded_cards}</div>
          </section>
          <section class="wrap">
            <h2>Observation Gap Validation</h2>
            <div class="review-grid">{observation_cards}</div>
          </section>
          <div class="band" id="judging-map">
            <section class="wrap">
              <div class="kicker">Judging map</div>
              <h2>Built as the evidence gate before automated action.</h2>
              <div class="criteria-grid" style="margin-top:28px">
                <article class="criteria-card"><strong>Agent value</strong><p>Tool-call trace, missing-evidence routing, and a rescore loop.</p></article>
                <article class="criteria-card"><strong>Problem fit</strong><p>Prevents unsafe certainty from thin operational evidence.</p></article>
                <article class="criteria-card"><strong>Usability</strong><p>No-login Cloud Run UI with primary review and graph links.</p></article>
                <article class="criteria-card"><strong>Practicality</strong><p>Raw logs stay local; promotion is human-gated.</p></article>
                <article class="criteria-card"><strong>Build depth</strong><p>5 providers, full-corpus ledger, tests, Cloud Build, Cloud Run.</p></article>
              </div>
            </section>
          </div>
          <section id="improvement-loop" class="wrap">
            <div class="section-head">
              <div>
                <div class="kicker">Operated as production software</div>
                <h2>Convergence is support, not a verdict.</h2>
              </div>
              <p class="section-note">The AI workflow can ask for missing evidence, attach a child bundle, and re-score the review graph without exposing public write paths.</p>
            </div>
            <div class="loop-grid">{operation_links}</div>
          </section>
          <div class="wrap">{archive_section}</div>
        </main>
      </body>
    </html>
    """


def _precomputed_review_target_set(
    payload: dict[str, Any],
    *,
    evidence_sha256: str,
    limit: int = 5,
    pending_only: bool = True,
) -> dict[str, Any]:
    raw_targets = [row for row in payload.get("targets") or [] if isinstance(row, dict)]
    targets: list[dict[str, Any]] = []
    for row in raw_targets:
        target = deepcopy(row)
        target["evidence_sha256"] = evidence_sha256
        target.setdefault("status", "pending")
        target.setdefault("review_target_id", target.get("target_id") or "")
        if pending_only and str(target.get("status") or "pending") not in {"pending", "needs_more_data"}:
            continue
        targets.append(target)
    requested_limit = max(0, int(limit or 0))
    visible_targets = targets[:requested_limit] if requested_limit else targets
    summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
    review = summary.get("review") if isinstance(summary.get("review"), dict) else {}
    return {
        "summary": {
            "review_targets": int(review.get("primary_targets") or 0) + int(review.get("validation_targets") or 0),
            "primary_review_targets": int(review.get("primary_targets") or 0),
            "validation_targets": int(review.get("validation_targets") or len(targets)),
            "monitor_only": int(review.get("monitor_only") or 0),
            "auto_archived": int(review.get("auto_archived") or 0),
            "returned_targets": len(visible_targets),
            "source": "precomputed_review_summary",
        },
        "targets": visible_targets,
    }


def _precomputed_review_graph_response(payload: dict[str, Any], *, evidence_sha256: str) -> dict[str, Any]:
    summary = _precomputed_summary(payload, evidence_sha256) or {}
    review = summary.get("review") if isinstance(summary.get("review"), dict) else {}
    finding = summary.get("finding") if isinstance(summary.get("finding"), dict) else {}
    baselines = summary.get("baselines") if isinstance(summary.get("baselines"), dict) else {}
    graph_summary = payload.get("review_graph_summary") if isinstance(payload.get("review_graph_summary"), dict) else {}
    analysis_context = payload.get("analysis_context") if isinstance(payload.get("analysis_context"), dict) else {}
    profile_context = payload.get("profile_context") if isinstance(payload.get("profile_context"), dict) else {}
    target_set = _precomputed_review_target_set(payload, evidence_sha256=evidence_sha256, limit=0, pending_only=False)
    targets = list(target_set.get("targets") or [])
    primary_targets = [row for row in targets if str(row.get("class") or "") == "primary_candidate"]
    validation_targets = [row for row in targets if str(row.get("class") or "") != "primary_candidate"]
    updated_at = str(payload.get("updated_at") or summary.get("updated_at") or "")
    graph_model = _precomputed_graph_nodes_edges(
        payload,
        evidence_sha256=evidence_sha256,
        summary=summary,
        graph_summary=graph_summary,
        targets=targets,
    )
    graph = {
        "schema_version": "precomputed_review_graph_projection.v1",
        "evidence_sha256": evidence_sha256,
        "snapshot_status": "precomputed",
        "canonical_graph_status": str(summary.get("canonical_graph_status") or "precomputed"),
        "canonical_graph_sha256": str(summary.get("canonical_graph_sha256") or ""),
        "input_fingerprint_sha256": str(summary.get("input_fingerprint_sha256") or ""),
        "score_note": "Priority is review urgency, not truth probability.",
        "summary": {
            "primary_count": int(review.get("primary_targets") or len(primary_targets)),
            "validation_count": int(review.get("validation_targets") or len(validation_targets)),
            "monitor_only_count": int(review.get("monitor_only") or 0),
            "auto_archived_count": int(review.get("auto_archived") or 0),
        },
        "finding": finding,
        "agreement_dimensions": {
            "provider_detection_overlap": {"value": str(graph_summary.get("provider_detection_overlap") or "")},
            "technical_baseline_agreement": {"established": bool(baselines.get("technical"))},
            "incident_baseline_agreement": {"established": bool(baselines.get("incident"))},
            "review_unit_convergence": {
                "value": str(graph_summary.get("review_unit_convergence") or ""),
                "converged_unit_count": int(graph_summary.get("convergence_count") or 0),
            },
        },
        "review_graph_summary": graph_summary,
        "analysis_context": analysis_context,
        "profile_context": profile_context,
        "nodes": graph_model["nodes"],
        "edges": graph_model["edges"],
        "primary_targets": primary_targets,
        "validation_targets": validation_targets,
        "review_targets": targets,
        "display_summary": {
            "title": str(finding.get("title") or ""),
            "impact": _public_finding_impact_text(summary, str(finding.get("impact") or "")),
            "provider_detection_overlap": str(graph_summary.get("provider_detection_overlap") or ""),
            "technical_baseline_agreement": str(graph_summary.get("technical_baseline") or ""),
            "incident_baseline_agreement": str(graph_summary.get("incident_baseline") or ""),
            "incident_gate_signal": _incident_gate_signal_text(
                graph_summary.get("incident_gate_signal") or graph_summary.get("incident_baseline")
            ),
            "score_note": "Priority is review urgency, not truth probability.",
        },
    }
    return {
        "canonical_graph_status": "precomputed",
        "canonical_graph_sha256": str(summary.get("canonical_graph_sha256") or ""),
        "input_fingerprint_sha256": str(summary.get("input_fingerprint_sha256") or ""),
        "graph": graph_model,
        "analysis_context": analysis_context,
        "profile_context": profile_context,
        "canonical_review_graph": graph,
        "snapshot": {
            "evidence_sha256": evidence_sha256,
            "canonical_graph_sha256": str(summary.get("canonical_graph_sha256") or ""),
            "input_fingerprint_sha256": str(summary.get("input_fingerprint_sha256") or ""),
            "created_at": updated_at,
            "created_by": "precomputed_review_summary",
            "snapshot_status": "precomputed",
        },
        "snapshot_created_at": updated_at,
    }


def _precomputed_graph_nodes_edges(
    payload: dict[str, Any],
    *,
    evidence_sha256: str,
    summary: dict[str, Any],
    graph_summary: dict[str, Any],
    targets: list[dict[str, Any]],
) -> dict[str, Any]:
    finding = summary.get("finding") if isinstance(summary.get("finding"), dict) else {}
    baselines = summary.get("baselines") if isinstance(summary.get("baselines"), dict) else {}
    provider_statuses = [row for row in payload.get("provider_statuses") or [] if isinstance(row, dict)]
    nodes: list[dict[str, Any]] = [
        {
            "id": "evidence",
            "type": "evidence_bundle",
            "label": f"Evidence {evidence_sha256[:12]}",
            "detail": f"{int(summary.get('log_count') or 0):,} sanitized logs",
        },
        {
            "id": "finding",
            "type": "finding",
            "label": str(finding.get("title") or "Persisted finding"),
            "detail": _public_finding_impact_text(summary, str(finding.get("impact") or "")),
        },
        {
            "id": "baseline:technical",
            "type": "support_signal",
            "label": "Technical support",
            "state": "established" if baselines.get("technical") else "open",
            "detail": str(graph_summary.get("technical_baseline") or ""),
        },
        {
            "id": "baseline:incident",
            "type": "promotion_gate",
            "label": "Incident gate signal",
            "state": _incident_gate_signal_text(
                graph_summary.get("incident_gate_signal") or graph_summary.get("incident_baseline")
            ),
            "detail": str(
                graph_summary.get("target_promotion_policy")
                or "Graph-level incident signal; target promotion remains per-target and human-gated."
            ),
        },
    ]
    edges: list[dict[str, Any]] = [
        {"id": "evidence->finding", "source": "evidence", "target": "finding", "relation": "produces"},
    ]
    for row in provider_statuses:
        provider_id = str(row.get("provider_id") or "")
        if not provider_id:
            continue
        nodes.append(
            {
                "id": _graph_id("provider", provider_id),
                "type": "provider",
                "label": provider_id,
                "state": str(row.get("status") or "unknown"),
                "schema_valid": bool(row.get("schema_valid")),
                "detail": str(row.get("raw_output_sha256") or "")[:12],
            }
        )
    for index, target in enumerate(targets, start=1):
        target_id = str(target.get("review_target_id") or target.get("target_id") or f"target-{index}")
        target_node_id = _graph_id("target", target_id)
        agreement = target.get("agreement") if isinstance(target.get("agreement"), dict) else {}
        promotion = target.get("promotion") if isinstance(target.get("promotion"), dict) else {}
        nodes.append(
            {
                "id": target_node_id,
                "type": "review_target",
                "label": str(target.get("title") or target_id),
                "state": str(promotion.get("state") or target.get("status") or "validation"),
                "detail": str(agreement.get("summary") or target.get("summary") or ""),
                "convergence_score": agreement.get("convergence_score"),
            }
        )
        edges.extend(
            [
                {
                    "id": f"finding->{target_node_id}",
                    "source": "finding",
                    "target": target_node_id,
                    "relation": "has_review_target",
                },
                {
                    "id": f"{target_node_id}->baseline:technical",
                    "source": target_node_id,
                    "target": "baseline:technical",
                    "relation": str(agreement.get("technical_baseline") or "technical_baseline"),
                },
                {
                    "id": f"{target_node_id}->baseline:incident",
                    "source": target_node_id,
                    "target": "baseline:incident",
                    "relation": str(agreement.get("incident_baseline") or "incident_baseline"),
                },
            ]
        )
        for position in target.get("provider_positions") or []:
            if not isinstance(position, dict):
                continue
            provider_id = str(position.get("provider_id") or "")
            if not provider_id:
                continue
            relation = str(position.get("stance") or "observed")
            edges.append(
                {
                    "id": f"{_graph_id('provider', provider_id)}->{target_node_id}:{relation}",
                    "source": _graph_id("provider", provider_id),
                    "target": target_node_id,
                    "relation": relation,
                    "detail": str(position.get("one_line") or ""),
                    "model_run_hash": str(position.get("model_run_hash") or ""),
                }
            )
    return {
        "schema_version": "review_graph_nodes_edges.v1",
        "node_count": len(nodes),
        "edge_count": len(edges),
        "nodes": nodes,
        "edges": edges,
    }


def _graph_id(prefix: str, value: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in {"-", "_", "."} else "-" for ch in str(value).strip())
    return f"{prefix}:{cleaned[:96] or 'unknown'}"


def _render_precomputed_api_page(evidence_sha256: str, payload: dict[str, Any]) -> str:
    summary = _precomputed_summary(payload, evidence_sha256) or {}
    targets = _precomputed_review_target_set(payload, evidence_sha256=evidence_sha256, limit=3, pending_only=False)
    graph = _precomputed_review_graph_response(payload, evidence_sha256=evidence_sha256)
    graph_model = graph.get("graph") if isinstance(graph.get("graph"), dict) else {}
    finding = summary.get("finding") if isinstance(summary.get("finding"), dict) else {}
    providers = summary.get("providers") if isinstance(summary.get("providers"), dict) else {}
    review = summary.get("review") if isinstance(summary.get("review"), dict) else {}
    raw_policy = str(summary.get("raw_log_policy") or "unknown")
    log_count = int(summary.get("log_count") or 0)
    context = payload.get("analysis_context") if isinstance(payload.get("analysis_context"), dict) else {}
    full_targets = [target for target in payload.get("targets") or [] if isinstance(target, dict)]
    log_observations = [str(item) for item in context.get("log_observations") or [] if str(item).strip()]
    source_observations = [str(item) for item in context.get("source_observations") or [] if str(item).strip()]
    conclusion_points = [str(item) for item in context.get("analysis_conclusion") or [] if str(item).strip()]
    case_label = _detail_case_label(payload, review)
    provider_mode = _detail_provider_mode_label(payload)
    finding_title = str(finding.get("title") or "Evidence review")
    finding_impact = _public_finding_impact_text(
        summary, str(finding.get("impact") or "The API result is available for review.")
    )
    hero_title, hero_impact = _detail_hero_copy(
        payload=payload,
        review=review,
        providers=providers,
        finding_title=finding_title,
        finding_impact=finding_impact,
        log_count=log_count,
    )
    observation_badge = (
        '<span class="eyebrow-pill">Observation Gap</span>' if _detail_is_observation_gap(payload) else ""
    )
    summary_cells = _detail_summary_cells_html(
        payload=payload,
        review=review,
        providers=providers,
        targets=full_targets,
        raw_policy=raw_policy,
        log_count=log_count,
    )
    evidence = _url_quote(evidence_sha256)
    action_links = "".join(
        f'<a class="button" href="{_html(url)}">{_html(label)}</a>'
        for label, url in (
            ("Full review", f"/ui/full-review-page?evidence_sha256={evidence}"),
            ("Review graph", f"/ui/review-graph?evidence_sha256={evidence}"),
            ("Markdown report", f"/ui/report.md?evidence_sha256={evidence}"),
        )
    )
    provider_rows = "\n".join(
        f"""
        <tr>
          <td>{_html(str(row.get("provider_id") or row.get("provider") or ""))}</td>
          <td>{_html(str(row.get("model_name") or ""))}</td>
          <td>{_html(str(row.get("status") or ""))}</td>
          <td>{_html("true" if row.get("schema_valid") else "false")}</td>
          <td><code>{_html(str(row.get("raw_output_sha256") or "")[:12])}</code></td>
        </tr>
        """
        for row in payload.get("provider_statuses") or []
        if isinstance(row, dict)
    )
    target_rows = "\n".join(
        _api_review_target_row(target, index=index + 1)
        for index, target in enumerate(targets.get("targets") or [])
        if isinstance(target, dict)
    )
    base = ""
    links = [
        (
            "Summary JSON",
            f"/ui/summary?evidence_sha256={_url_quote(evidence_sha256)}",
            {
                "schema_version": summary.get("schema_version"),
                "finding": (summary.get("finding") or {}).get("title") if isinstance(summary.get("finding"), dict) else "",
                "providers": summary.get("providers"),
                "review": summary.get("review"),
            },
        ),
        (
            "Review Targets JSON",
            f"/review-targets?evidence_sha256={_url_quote(evidence_sha256)}",
            {
                "source": (targets.get("summary") or {}).get("source"),
                "returned_targets": (targets.get("summary") or {}).get("returned_targets"),
                "target_titles": [str(row.get("title") or "") for row in targets.get("targets") or []],
            },
        ),
        (
            "Review Graph JSON",
            f"/review/graph?evidence_sha256={_url_quote(evidence_sha256)}",
            {
                "schema_version": graph_model.get("schema_version"),
                "nodes": graph_model.get("node_count"),
                "edges": graph_model.get("edge_count"),
                "canonical_graph_status": graph.get("canonical_graph_status"),
            },
        ),
    ]
    cards = "\n".join(
        f"""
        <article class="api-card">
          <label>{_html(title)}</label>
          <a href="{_html(base + href)}">{_html(href)}</a>
          <pre>{_html(json.dumps(sample, ensure_ascii=False, indent=2))}</pre>
        </article>
        """
        for title, href, sample in links
    )
    log_points = "".join(f"<li>{_html(item)}</li>" for item in log_observations) or "<li>Sanitized evidence bundle was analyzed.</li>"
    source_points = "".join(f"<li>{_html(item)}</li>" for item in source_observations) or "<li>Sanitized source context was attached when available.</li>"
    conclusion_html = "".join(f"<li>{_html(item)}</li>" for item in conclusion_points) or "<li>Review targets remain human-gated; raw logs are not exposed.</li>"
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Ops Evidence API View</title>
  <style>
    :root {{
      color-scheme: light;
      --bg: #f4f2ec;
      --surface: #fffdf8;
      --surface-2: #fbf8f1;
      --border: #e4dccb;
      --border-strong: #d3c8b3;
      --ink: #181611;
      --ink-2: #514b40;
      --ink-3: #8b8375;
      --accent: #3f63a8;
      --accent-soft: #eef2f9;
      --claimed: #208a61;
      --amber: #b17a40;
      --green-soft: #ecf6f1;
      --shadow: 0 20px 58px -44px rgba(60, 50, 30, .36);
      --mono: ui-monospace, SFMono-Regular, Menlo, Consolas, "Liberation Mono", monospace;
      --serif: Georgia, "Times New Roman", serif;
    }}
    * {{ box-sizing: border-box; }}
    html {{ max-width: 100%; overflow-x: hidden; }}
    body {{
      margin: 0;
      max-width: 100%;
      overflow-x: hidden;
      background: var(--bg);
      color: var(--ink);
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      letter-spacing: 0;
    }}
    a {{ color: inherit; text-decoration: none; overflow-wrap: anywhere; }}
    code, pre {{ font-family: var(--mono); }}
    p {{ margin: 0; color: var(--ink-2); line-height: 1.58; }}
    .page {{
      width: 100%;
      max-width: 100%;
      overflow-x: hidden;
      padding: 0 0 72px;
    }}
    .topbar {{
      position: sticky;
      top: 0;
      z-index: 20;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
      padding: 16px max(24px, calc((100vw - 1220px) / 2));
      border-bottom: 1px solid var(--border);
      background: rgba(250, 248, 242, .94);
      backdrop-filter: blur(8px);
    }}
    .brand-row, .status-row, .actions {{
      display: flex;
      align-items: center;
      gap: 10px;
      flex-wrap: wrap;
      min-width: 0;
      max-width: 100%;
    }}
    .mark {{
      width: 26px;
      height: 26px;
      border-radius: 7px;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      background: var(--ink);
      color: var(--bg);
      font: 800 11px/1 var(--mono);
      flex: none;
      text-decoration: none;
    }}
    .breadcrumb {{
      display: flex;
      gap: 11px;
      align-items: center;
      min-width: 0;
      max-width: 100%;
      color: var(--ink-3);
      font-size: 13px;
    }}
    .breadcrumb a {{
      color: inherit;
      text-decoration: none;
    }}
    .breadcrumb a:hover, .breadcrumb a:focus-visible {{
      color: var(--ink);
    }}
    .breadcrumb strong {{ display: inline; color: var(--ink); font-weight: 800; }}
    .status-chip, .evidence-chip {{
      display: inline-flex;
      align-items: center;
      gap: 7px;
      border: 1px solid var(--border);
      border-radius: 999px;
      background: rgba(255, 253, 248, .78);
      color: var(--ink-2);
      font: 800 11.5px/1 var(--mono);
      padding: 7px 10px;
      min-width: 0;
      max-width: 100%;
      overflow-wrap: anywhere;
    }}
    .status-chip.live {{
      border-color: #bfe0cd;
      background: var(--green-soft);
      color: var(--claimed);
    }}
    .status-dot {{
      width: 7px;
      height: 7px;
      border-radius: 50%;
      background: var(--claimed);
      flex: none;
    }}
    .top-link {{ color: var(--ink-2); font-size: 13px; }}
    .top-link:hover {{ color: var(--ink); }}
    main {{
      width: min(calc(100% - 48px), 1220px);
      margin: 0 auto;
      display: grid;
      gap: 0;
      min-width: 0;
    }}
    h1, h2, h3 {{ margin: 0; color: var(--ink); letter-spacing: 0; }}
    h1 {{
      max-width: 920px;
      font-family: var(--serif);
      font-size: clamp(38px, 3.7vw, 48px);
      font-weight: 500;
      line-height: 1.08;
    }}
    h2 {{
      font-family: var(--serif);
      font-size: clamp(28px, 3vw, 34px);
      font-weight: 500;
      line-height: 1.08;
    }}
    h3 {{ font-size: 16px; }}
    label, .eyebrow {{
      display: block;
      color: var(--amber);
      font-family: var(--mono);
      font-size: 11px;
      font-weight: 800;
      text-transform: uppercase;
      letter-spacing: .16em;
      line-height: 1.2;
    }}
    .hero {{
      display: grid;
      gap: 24px;
      padding: 78px 0 40px;
    }}
    .hero .eyebrow {{
      display: flex;
      align-items: center;
      gap: 10px;
      flex-wrap: wrap;
      margin-bottom: 0;
    }}
    .eyebrow-pill {{
      display: inline-flex;
      align-items: center;
      border: 1px solid #e0d8c7;
      border-radius: 999px;
      background: #efe9db;
      color: #8a857a;
      padding: 4px 10px;
      font-size: 10px;
      letter-spacing: .08em;
      white-space: nowrap;
    }}
    .hero p {{
      max-width: 780px;
      color: var(--ink-2);
      font-size: 16.5px;
      line-height: 1.62;
    }}
    .button {{
      display: inline-flex;
      align-items: center;
      border: 1px solid var(--border-strong);
      border-radius: 8px;
      background: transparent;
      color: var(--ink);
      padding: 11px 18px;
      font-size: 14px;
      font-weight: 800;
    }}
    .button:hover {{ border-color: var(--ink); background: #efe9db; }}
    .stat-grid {{
      display: grid;
      grid-template-columns: repeat(6, minmax(0, 1fr));
      gap: 1px;
      margin-top: 16px;
      border: 1px solid var(--border);
      border-radius: 10px;
      background: var(--border);
      overflow: hidden;
    }}
    .stat-cell {{
      min-width: 0;
      background: rgba(255, 253, 248, .78);
      padding: 19px 18px;
    }}
    .stat-cell.safe {{ background: var(--green-soft); }}
    .stat-cell strong {{
      display: block;
      color: var(--ink);
      font-size: 23px;
      font-weight: 900;
      line-height: 1;
      overflow-wrap: anywhere;
    }}
    .stat-cell.safe strong {{ color: var(--claimed); }}
    .stat-cell span, .stat-cell small {{
      display: block;
      margin-top: 6px;
      color: var(--ink-3);
      font-size: 11px;
      line-height: 1.35;
    }}
    .section-block {{
      display: grid;
      gap: 22px;
      padding: 52px 0;
      border-top: 1px solid var(--border);
      min-width: 0;
    }}
    .section-heading {{
      display: grid;
      gap: 9px;
      max-width: 780px;
    }}
    .analysis-grid, .api-grid {{
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 16px;
      align-items: start;
    }}
    .readable, .api-card {{
      display: grid;
      gap: 12px;
      min-width: 0;
      overflow: hidden;
      border: 1px solid var(--border);
      border-radius: 12px;
      background: rgba(255, 253, 248, .76);
      box-shadow: var(--shadow);
      padding: 20px;
    }}
    .readable ul {{
      margin: 0;
      padding-left: 19px;
      color: var(--ink-2);
      line-height: 1.55;
      font-size: 13.5px;
    }}
    .table-card {{
      display: grid;
      gap: 14px;
      min-width: 0;
      overflow: hidden;
      border: 1px solid var(--border);
      border-radius: 12px;
      background: var(--surface);
      box-shadow: var(--shadow);
      padding: 20px;
    }}
    .table-scroll {{
      min-width: 0;
      max-width: 100%;
      overflow: auto;
      border: 1px solid var(--border);
      border-radius: 10px;
    }}
    table {{ width: 100%; border-collapse: collapse; table-layout: fixed; }}
    th, td {{
      padding: 11px 12px;
      border-bottom: 1px solid var(--border);
      text-align: left;
      vertical-align: top;
      color: var(--ink-2);
      font-size: 13px;
      line-height: 1.45;
      overflow-wrap: anywhere;
    }}
    th {{
      background: var(--surface-2);
      color: var(--ink-3);
      font: 800 10px/1.2 var(--mono);
      text-transform: uppercase;
      letter-spacing: .08em;
    }}
    tr:last-child td {{ border-bottom: 0; }}
    .api-card a {{
      color: var(--accent);
      font-weight: 800;
      font-size: 13px;
    }}
    pre {{
      margin: 0;
      max-height: 260px;
      overflow: auto;
      max-width: 100%;
      padding: 14px;
      border: 1px solid var(--border);
      border-radius: 8px;
      background: var(--surface-2);
      color: var(--ink-2);
      font-size: 12.5px;
      line-height: 1.45;
    }}
    .boundary-note {{
      color: var(--ink-3);
      font-size: 13px;
    }}
    .footer {{
      display: flex;
      justify-content: space-between;
      gap: 20px;
      padding-top: 22px;
      border-top: 1px solid var(--border);
      color: var(--ink-3);
      font-size: 12px;
    }}
    @media (max-width: 900px) {{
      main {{ width: min(calc(100% - 32px), 1220px); }}
      .topbar {{ position: static; align-items: flex-start; flex-direction: column; padding: 14px 16px; }}
      .hero {{ padding-top: 54px; }}
      .stat-grid, .analysis-grid, .api-grid {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
    }}
    @media (max-width: 760px) {{
      h1 {{ font-size: 31px; line-height: 1.08; overflow-wrap: anywhere; word-break: break-word; }}
      html, body, .page {{ overflow-x: clip; }}
      .topbar {{ width: 100vw; max-width: 100vw; }}
      main {{ width: calc(100vw - 32px); max-width: calc(100vw - 32px); }}
      .hero p {{ font-size: 16px; overflow-wrap: anywhere; }}
      .stat-grid, .analysis-grid, .api-grid {{ grid-template-columns: 1fr; }}
      .breadcrumb, .status-row {{ align-items: flex-start; }}
      .footer {{ display: grid; }}
    }}
  </style>
</head>
<body>
  <div class="page">
    <header class="topbar">
      <div class="brand-row">
        <a class="mark" href="/" aria-label="Ops Evidence home">OE</a>
        <div class="breadcrumb">
          <span>/</span><a href="/#review-set">Reviews</a>
          <span>/</span><a href="/ui/full-review-page?evidence_sha256={_html(evidence)}"><strong>{_html(case_label)}</strong></a>
          <span>/</span><strong>API</strong>
        </div>
      </div>
      <div class="status-row">
        <span class="evidence-chip">evidence {_html(_short_sha(evidence_sha256))}</span>
        <a class="top-link" href="{_html(_public_repo_url())}">GitHub</a>
        <span class="status-chip live"><span class="status-dot"></span>Cloud Run live</span>
      </div>
    </header>
    <main>
    <section class="hero">
      <span class="eyebrow">Read-only API View / {_html(provider_mode)} {observation_badge}</span>
      <h1>{_html(hero_title)}</h1>
      <p>{_html(hero_impact)}</p>
      <div class="actions">
        {action_links}
      </div>
      <div class="stat-grid">{summary_cells}</div>
    </section>
    <section class="section-block">
      <div class="section-heading">
        <span class="eyebrow">Human-readable analysis</span>
        <h2>The API is a review surface, not an execution surface.</h2>
        <p>These cards expose the same precomputed evidence boundary as JSON while keeping raw bundles and write APIs out of the public page.</p>
      </div>
      <div class="analysis-grid">
        <article class="readable">
          <label>What was analyzed</label>
          <ul>{log_points}</ul>
        </article>
        <article class="readable">
          <label>Code context used</label>
          <ul>{source_points}</ul>
        </article>
        <article class="readable">
          <label>Conclusion</label>
          <ul>{conclusion_html}</ul>
        </article>
      </div>
    </section>
    <section class="section-block">
      <div class="section-heading">
        <span class="eyebrow">Endpoint contract</span>
        <h2>Machine-readable JSON stays inspectable.</h2>
      </div>
      <div class="api-grid">{cards}</div>
    </section>
    <section class="section-block">
      <article class="table-card">
        <label>Provider outputs</label>
        <div class="table-scroll">
          <table>
            <thead><tr><th>Provider</th><th>Model</th><th>Status</th><th>Schema</th><th>Output hash</th></tr></thead>
            <tbody>{provider_rows or '<tr><td colspan="5">No provider status was persisted.</td></tr>'}</tbody>
          </table>
        </div>
      </article>
      <article class="table-card">
        <label>Review targets</label>
        <div class="table-scroll">
          <table>
            <thead><tr><th>#</th><th>Target</th><th>Claim</th><th>Agreement</th><th>Displayed refs</th></tr></thead>
            <tbody>{target_rows or '<tr><td colspan="5">No review targets were projected.</td></tr>'}</tbody>
          </table>
        </div>
      </article>
    </section>
    <footer class="footer">
      <span>The linked endpoints return machine-readable JSON; writes, raw bundles, and execution APIs are not exposed here.</span>
      <span><code>canonical_review_graph.v1</code></span>
    </footer>
    </main>
  </div>
</body>
</html>"""


def _api_review_target_row(target: dict[str, Any], *, index: int) -> str:
    agreement = target.get("agreement") if isinstance(target.get("agreement"), dict) else {}
    evidence_refs = target.get("evidence_refs") if isinstance(target.get("evidence_refs"), list) else []
    return f"""
    <tr>
      <td>{index}</td>
      <td>{_html(str(target.get("title") or target.get("review_target_id") or ""))}</td>
      <td>{_html(str(target.get("claim") or target.get("core_claim") or target.get("proposal") or ""))}</td>
      <td>{_html(str(agreement.get("summary") or agreement.get("verdict") or ""))}</td>
      <td>{_html(", ".join(str(item) for item in evidence_refs[:6]) or "none")}</td>
    </tr>
    """


def _render_precomputed_graph_page(evidence_sha256: str, payload: dict[str, Any]) -> str:
    response = _precomputed_review_graph_response(payload, evidence_sha256=evidence_sha256)
    graph_model = response.get("graph") if isinstance(response.get("graph"), dict) else {}
    summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
    finding = summary.get("finding") if isinstance(summary.get("finding"), dict) else {}
    review = summary.get("review") if isinstance(summary.get("review"), dict) else {}
    providers = summary.get("providers") if isinstance(summary.get("providers"), dict) else {}
    graph_summary = payload.get("review_graph_summary") if isinstance(payload.get("review_graph_summary"), dict) else {}
    context = response.get("analysis_context") if isinstance(response.get("analysis_context"), dict) else {}
    nodes = [row for row in graph_model.get("nodes") or [] if isinstance(row, dict)]
    targets = [row for row in payload.get("targets") or [] if isinstance(row, dict)]
    provider_statuses = [row for row in payload.get("provider_statuses") or [] if isinstance(row, dict)]
    provider_ids = [str(row.get("provider_id") or "") for row in provider_statuses if str(row.get("provider_id") or "")]
    if not provider_ids:
        provider_ids = [
            str(node.get("label") or "")
            for node in nodes
            if str(node.get("type") or "") == "provider" and str(node.get("label") or "")
        ]
    target_models = [_review_graph_target_model(target, index=index, provider_ids=provider_ids) for index, target in enumerate(targets, start=1)]
    selected_key = _review_graph_initial_key(target_models)
    target_count = len(target_models)
    provider_total = int(providers.get("total") or len(provider_ids) or 0)
    provider_success = int(providers.get("success") or len(provider_ids) or 0)
    unique_refs = sorted(
        {
            str(ref)
            for target in targets
            for ref in (target.get("evidence_refs") if isinstance(target.get("evidence_refs"), list) else [])
            if str(ref).strip()
        }
    )
    graph_sha = str(summary.get("canonical_graph_sha256") or "")
    service_label = _review_graph_service_label(payload)
    incident_gate_signal = _incident_gate_signal_text(
        graph_summary.get("incident_gate_signal") or graph_summary.get("incident_baseline")
    )
    row_count = int(summary.get("log_count") or context.get("sanitized_log_count") or 0)
    graph_stats = [
        (_human_count(int(graph_model.get("node_count") or len(nodes))), "graph nodes", ""),
        (_human_count(target_count), "review targets", ""),
        (_human_count(int(graph_summary.get("convergence_count") or 0)), "convergence groups", "blue"),
        (_human_count(len(unique_refs)), "cited evidence refs", ""),
        (_human_count(int(graph_summary.get("conflict_count") or 0)), "explicit conflicts", "green"),
        (f"{provider_success}/{provider_total}" if provider_total else _human_count(len(provider_ids)), "providers on graph", ""),
    ]
    stats_html = "".join(
        f"""
        <article class="stat-cell {_html(css)}">
          <strong>{_html(value)}</strong>
          <span>{_html(label)}</span>
        </article>
        """
        for value, label, css in graph_stats
    )
    graph_html = _review_graph_standalone_graph_html(
        target_models,
        provider_ids=provider_ids,
        selected_key=selected_key,
    )
    outcome_html = _review_graph_standalone_outcome_html(target_models, review)
    ledger_html = _review_graph_standalone_ledger_html(
        graph_model,
        summary=summary,
        context=context,
        row_count=row_count,
        unique_ref_count=len(unique_refs),
    )
    action_links = _public_action_links_html(evidence_sha256)
    title = str(
        graph_summary.get("summary")
        or finding.get("impact")
        or "The canonical graph keeps provider positions, cited Evidence Items, and human review gates connected."
    )
    title = _public_count_text(title)
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Ops Evidence Review Graph</title>
  <style>
    :root {{
      color-scheme: light;
      --bg: #f4f2ec;
      --bg-2: #faf8f2;
      --surface: #fffdf8;
      --paper: #ffffff;
      --ink: #1c1a15;
      --ink-2: #4a463d;
      --ink-3: #7a746a;
      --muted: #8a857a;
      --border: #e5dfd1;
      --border-2: #e7e0d1;
      --blue: #3f63a8;
      --blue-soft: #eef2f9;
      --blue-border: #cdd8ec;
      --green: #2f8a5b;
      --green-soft: #eef7f1;
      --green-border: #bfe0cd;
      --gold: #a7845a;
      --tan: #f1ede3;
      --tan-border: #e4ddce;
      --shadow: 0 22px 55px -36px rgba(60, 50, 30, .42);
      --mono: "IBM Plex Mono", ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      --sans: "IBM Plex Sans", Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      --display: "Space Grotesk", var(--sans);
      --serif: "Newsreader", Georgia, "Times New Roman", serif;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      background: var(--bg);
      color: var(--ink);
      font-family: var(--sans);
      letter-spacing: 0;
      -webkit-font-smoothing: antialiased;
    }}
    a {{ color: inherit; text-decoration: none; }}
    p {{ margin: 0; }}
    code {{ font-family: var(--mono); }}
    .page {{ width: 100%; overflow-x: hidden; }}
    .wrap {{ max-width: 1220px; margin: 0 auto; padding-left: 32px; padding-right: 32px; }}
    .nav {{
      position: sticky;
      top: 0;
      z-index: 20;
      border-bottom: 1px solid var(--border);
      background: rgba(250, 248, 242, .9);
      backdrop-filter: blur(8px);
    }}
    .nav-inner {{
      min-height: 58px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 18px;
      padding-top: 14px;
      padding-bottom: 14px;
    }}
    .crumbs, .nav-actions {{ display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }}
    .brand {{ display: inline-flex; align-items: center; gap: 9px; color: var(--ink); font-family: var(--display); font-weight: 700; font-size: 14.5px; }}
    .brand-mark {{ width: 26px; height: 26px; border-radius: 7px; background: var(--ink); color: var(--bg); display: grid; place-items: center; font: 700 11px/1 var(--mono); }}
    .crumbs span, .crumbs a, .nav-actions a {{ color: var(--muted); font-size: 13.5px; }}
    .crumb-sep {{ color: #c9c1b2; }}
    .nav-actions {{ gap: 20px; color: #6a655b; font-size: 13px; }}
    .nav-actions a:hover, .crumbs a:hover {{ color: var(--ink); }}
    .sha-chip {{ color: var(--muted); font: 500 11.5px/1 var(--mono); }}
    .live-chip {{ display: inline-flex; align-items: center; gap: 7px; color: var(--green); border: 1px solid var(--green-border); background: var(--green-soft); padding: 5px 11px; border-radius: 20px; font: 600 11.5px/1 var(--mono); }}
    .live-chip i {{ width: 7px; height: 7px; border-radius: 50%; background: var(--green); }}
    .hero {{ padding-top: 52px; padding-bottom: 34px; }}
    .hero-kickers {{ display: flex; align-items: center; gap: 10px; flex-wrap: wrap; margin-bottom: 18px; }}
    .kicker {{ color: var(--gold); font: 600 12px/1 var(--mono); letter-spacing: .12em; text-transform: uppercase; }}
    .soft-chip {{ color: var(--muted); background: #efe9db; border: 1px solid #e0d8c7; padding: 3px 9px; border-radius: 20px; font: 500 11px/1 var(--mono); }}
    h1 {{ margin: 0 0 18px; max-width: 900px; color: var(--ink); font-family: var(--serif); font-size: 44px; line-height: 1.06; font-weight: 500; letter-spacing: 0; overflow-wrap: anywhere; }}
    .hero-copy {{ max-width: 790px; margin-bottom: 30px; color: var(--ink-2); font-size: 16.5px; line-height: 1.6; }}
    .stat-strip {{ display: flex; gap: 0; flex-wrap: wrap; border: 1px solid var(--border); border-radius: 12px; overflow: hidden; background: var(--border); }}
    .stat-cell {{ flex: 1; min-width: 120px; background: var(--bg-2); padding: 16px 18px; }}
    .stat-cell strong {{ display: block; color: var(--ink); font-family: var(--display); font-size: 22px; line-height: 1; font-weight: 700; overflow-wrap: anywhere; }}
    .stat-cell span {{ display: block; margin-top: 3px; color: var(--muted); font-size: 11px; line-height: 1.35; }}
    .stat-cell.blue strong {{ color: var(--blue); }}
    .stat-cell.green {{ background: var(--green-soft); }}
    .stat-cell.green strong, .stat-cell.green span {{ color: var(--green); }}
    .graph-band {{ background: var(--bg-2); border-top: 1px solid var(--border); border-bottom: 1px solid var(--border); }}
    .graph-section {{ padding-top: 40px; padding-bottom: 40px; }}
    .graph-head {{ display: flex; align-items: baseline; justify-content: space-between; flex-wrap: wrap; gap: 8px; margin-bottom: 6px; }}
    .legend {{ display: flex; gap: 14px; color: var(--ink-3); font-size: 11.5px; align-items: center; flex-wrap: wrap; }}
    .legend span {{ display: inline-flex; align-items: center; gap: 6px; }}
    .legend-line {{ width: 16px; height: 3px; border-radius: 2px; background: var(--blue); display: inline-block; }}
    .legend-dash {{ width: 16px; height: 0; border-top: 2px dashed #b3a894; display: inline-block; }}
    .legend-dot {{ width: 10px; height: 10px; border-radius: 50%; background: var(--green); display: inline-block; }}
    .graph-intro {{ max-width: 790px; margin: 0 0 20px; color: var(--ink-3); font-size: 14px; line-height: 1.6; }}
    .graph-layout {{ display: flex; gap: 22px; align-items: flex-start; flex-wrap: wrap; }}
    .graph-canvas-card, .reading-card {{ background: var(--paper); border: 1px solid var(--border-2); border-radius: 14px; box-shadow: var(--shadow); }}
    .graph-canvas-card {{ flex: 1 1 600px; min-width: 0; padding: 16px 18px; position: relative; }}
    .canvas-labels {{ display: flex; justify-content: space-between; color: #a49b89; font: 500 10.5px/1 var(--mono); letter-spacing: .1em; text-transform: uppercase; padding: 0 4px 8px; }}
    .graph-canvas-scroll {{ overflow-x: auto; }}
    .graph-panel[hidden], .reading-panel[hidden] {{ display: none; }}
    .graph-canvas {{ position: relative; width: 600px; margin: 0 auto; }}
    .graph-lines {{ position: absolute; left: 0; top: 0; width: 600px; overflow: visible; z-index: 0; pointer-events: none; }}
    .bg-edge {{ stroke: var(--blue); stroke-width: 1.3; opacity: .11; }}
    .selected-edge.claimed {{ stroke: var(--blue); stroke-width: 2.2; opacity: .95; }}
    .selected-edge.silent {{ stroke: #b3a894; stroke-width: 1.4; stroke-dasharray: 4 4; opacity: .6; }}
    .selected-edge.provider-error {{ stroke: #c45555; stroke-width: 1.6; stroke-dasharray: 4 4; opacity: .75; }}
    .provider-node {{ position: absolute; left: 0; width: 180px; display: flex; align-items: center; justify-content: flex-end; gap: 8px; z-index: 2; }}
    .provider-pill {{ font-family: var(--mono); font-size: 12px; padding: 6px 11px; border-radius: 8px; white-space: nowrap; color: #9a9484; background: var(--tan); border: 1px solid var(--tan-border); }}
    .provider-dot {{ flex-shrink: 0; width: 9px; height: 9px; border-radius: 50%; background: #cfc7b6; }}
    .provider-node.claimed .provider-pill {{ color: #2b3a52; background: var(--blue-soft); border-color: var(--blue-border); }}
    .provider-node.claimed .provider-dot {{ background: var(--blue); }}
    .provider-node.provider-error .provider-pill {{ color: #994747; background: #fff2f2; border-color: #efc9c9; }}
    .provider-node.provider-error .provider-dot {{ background: #c45555; }}
    .target-node {{ position: absolute; left: 350px; width: 240px; height: 34px; display: flex; align-items: center; gap: 8px; padding: 0 12px; border-radius: 9px; cursor: pointer; background: var(--surface); border: 1px solid var(--border-2); border-left: 3px solid var(--blue); z-index: 2; text-align: left; }}
    .target-node:hover {{ transform: translateX(2px); }}
    .target-node.active {{ background: #fff; border-color: var(--blue); box-shadow: 0 8px 20px -12px rgba(60,50,30,.4); z-index: 3; }}
    .target-node.primary {{ border-left-color: var(--green); }}
    .target-node.single-source {{ border-left-color: #b3a894; }}
    .target-name {{ min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--ink-2); font: 500 12px/1 var(--mono); }}
    .target-node.active .target-name {{ color: var(--ink); }}
    .target-meta {{ margin-left: auto; color: #a49b89; white-space: nowrap; font: 500 11px/1 var(--mono); }}
    .target-node.active.primary .target-meta {{ color: var(--green); }}
    .target-node.active.validation .target-meta {{ color: var(--blue); }}
    .target-node.active.single-source .target-meta {{ color: #a49b89; }}
    .reading-card {{ flex: 1 1 380px; min-width: 340px; overflow: hidden; }}
    .reading-top {{ padding: 20px 22px; border-bottom: 1px solid #efe8d9; }}
    .reading-titleline {{ display: flex; align-items: center; gap: 9px; margin-bottom: 10px; flex-wrap: wrap; }}
    .badge {{ color: var(--blue); background: var(--blue-soft); padding: 3px 9px; border-radius: 5px; font: 600 10.5px/1 var(--mono); letter-spacing: .08em; }}
    .badge.primary {{ color: var(--green); background: #e4f4eb; border: 1px solid var(--green-border); }}
    .badge.single-source {{ color: #8a7b62; background: var(--tan); border: 1px solid var(--tan-border); }}
    .reading-meta {{ color: #a49b89; font: 500 11.5px/1 var(--mono); }}
    .reading-score {{ margin-left: auto; color: var(--ink); font-family: var(--display); font-size: 22px; font-weight: 700; }}
    .reading-top h3 {{ margin: 0 0 4px; color: var(--ink); font-family: var(--display); font-size: 21px; line-height: 1.2; font-weight: 600; overflow-wrap: anywhere; }}
    .reading-note {{ color: var(--muted); font-size: 12px; line-height: 1.45; }}
    .reading-block {{ padding: 16px 22px; border-bottom: 1px solid #efe8d9; background: #fdfbf6; }}
    .reading-block.white {{ background: #fff; padding-top: 18px; padding-bottom: 18px; }}
    .reading-label {{ margin-bottom: 8px; color: #a49b89; font-size: 11px; letter-spacing: .08em; text-transform: uppercase; }}
    .provider-chips, .evidence-chips {{ display: flex; gap: 7px; flex-wrap: wrap; }}
    .provider-chip {{ display: inline-flex; gap: 5px; align-items: center; color: #9a9484; background: var(--tan); border: 1px solid var(--tan-border); padding: 5px 10px; border-radius: 7px; font-size: 12px; }}
    .provider-chip.claimed {{ color: #2b3a52; background: var(--blue-soft); border-color: var(--blue-border); }}
    .provider-chip.arbiter {{ color: #fff; background: var(--blue); border-color: var(--blue); }}
    .provider-chip.provider-error {{ color: #994747; background: #fff2f2; border-color: #efc9c9; }}
    .provider-chip span {{ opacity: .72; font-size: 10.5px; }}
    .suspected {{ margin-bottom: 16px; color: #3a352c; font-size: 13.5px; line-height: 1.5; }}
    .evidence-chip {{ color: var(--ink-2); background: var(--bg-2); border: 1px solid var(--border-2); padding: 4px 9px; border-radius: 6px; font: 500 11.5px/1 var(--mono); }}
    .evidence-chip.empty {{ color: #9a9484; background: #fdfbf6; border-style: dashed; }}
    .gate-block {{ padding: 18px 22px; background: linear-gradient(180deg, var(--green-soft), #fff); }}
    .gate-head {{ display: flex; align-items: center; gap: 10px; margin-bottom: 10px; }}
    .gate-icon {{ width: 34px; height: 34px; border-radius: 8px; border: 1px solid var(--green-border); background: #fff; display: grid; place-items: center; color: var(--green); font: 600 11px/1 var(--mono); }}
    .gate-title {{ color: var(--ink); font-family: var(--display); font-size: 13.5px; font-weight: 600; }}
    .gate-tag {{ margin-left: auto; color: var(--green); background: #e4f4eb; padding: 3px 8px; border-radius: 5px; font: 600 10px/1 var(--mono); }}
    .next-check {{ display: flex; align-items: center; gap: 10px; background: #fff; border: 1px solid #cfe6da; border-radius: 9px; padding: 11px 13px; }}
    .next-check b {{ color: var(--green); flex-shrink: 0; font: 600 11px/1 var(--mono); }}
    .next-check span {{ color: #3a352c; font-size: 13px; line-height: 1.4; }}
    .outcome {{ padding-top: 44px; padding-bottom: 8px; }}
    .outcome-grid {{ display: flex; gap: 14px; flex-wrap: wrap; }}
    .outcome-card {{ flex: 1; min-width: 190px; background: var(--surface); border: 1px solid var(--border-2); border-radius: 12px; padding: 18px; }}
    .outcome-card.blue {{ background: var(--blue-soft); border-color: var(--blue-border); }}
    .outcome-card.green {{ background: var(--green-soft); border-color: var(--green-border); }}
    .outcome-card.dark {{ background: var(--ink); border-color: var(--ink); }}
    .outcome-card strong {{ display: block; color: var(--ink); font-family: var(--display); font-size: 26px; line-height: 1; font-weight: 700; overflow-wrap: anywhere; }}
    .outcome-card.blue strong {{ color: var(--blue); }}
    .outcome-card.green strong {{ color: var(--green); }}
    .outcome-card.dark strong {{ color: var(--bg); }}
    .outcome-card span {{ display: block; margin-top: 4px; color: var(--muted); font-size: 12px; line-height: 1.35; }}
    .outcome-card.green span {{ color: #5f7a6b; }}
    .outcome-card.dark span {{ color: #a89f8d; }}
    .ledger {{ padding-top: 36px; padding-bottom: 52px; }}
    .ledger-head {{ display: flex; align-items: baseline; gap: 12px; margin-bottom: 18px; flex-wrap: wrap; }}
    .ledger-head h2 {{ margin: 0; color: var(--ink); font-family: var(--serif); font-size: 26px; font-weight: 500; line-height: 1.2; letter-spacing: 0; }}
    .ledger-head span {{ color: var(--muted); font: 500 12px/1.35 var(--mono); }}
    .ledger-types, .ledger-math {{ display: flex; gap: 10px; flex-wrap: wrap; margin-bottom: 22px; }}
    .ledger-pill {{ color: var(--ink-2); background: var(--surface); border: 1px solid var(--border-2); padding: 6px 12px; border-radius: 8px; font: 500 12px/1 var(--mono); }}
    .ledger-pill.blue {{ color: #2b3a52; background: var(--blue-soft); border-color: var(--blue-border); }}
    .ledger-pill.silent {{ color: var(--muted); background: var(--tan); border-color: var(--tan-border); }}
    .ledger-pill span {{ color: #a49b89; }}
    .ledger-pill b {{ color: var(--green); font-weight: 600; }}
    .ledger-math .ledger-pill {{ line-height: 1.35; }}
    .context-strip {{ display: flex; gap: 0; flex-wrap: wrap; border: 1px solid var(--border); border-radius: 12px; overflow: hidden; background: var(--border); }}
    .context-cell {{ flex: 1; min-width: 120px; background: var(--bg-2); padding: 15px 18px; }}
    .context-cell.green {{ background: var(--green-soft); }}
    .context-cell strong {{ display: block; color: var(--ink); font-family: var(--display); font-size: 20px; line-height: 1; font-weight: 700; overflow-wrap: anywhere; }}
    .context-cell.green strong {{ color: var(--green); }}
    .context-cell span {{ display: block; color: var(--muted); margin-top: 3px; font-size: 11px; line-height: 1.35; }}
    .context-cell.green span {{ color: #5f7a6b; }}
    .projection-note {{ margin: 12px 0 0; color: var(--ink-3); font-size: 12.5px; line-height: 1.5; }}
    .footer {{ background: var(--ink); }}
    .footer-inner {{ display: flex; justify-content: space-between; flex-wrap: wrap; gap: 16px; align-items: center; padding-top: 36px; padding-bottom: 36px; }}
    .footer .brand-mark {{ background: var(--bg); color: var(--ink); }}
    .footer .brand {{ color: var(--bg); }}
    .footer-actions {{ display: flex; gap: 12px; font-size: 13px; color: #c9c2b3; flex-wrap: wrap; }}
    .footer-actions a, .footer-actions .button {{ color: #c9c2b3; border: 1px solid rgba(244,242,236,.18); border-radius: 8px; padding: 7px 10px; background: transparent; font-size: 12px; font-weight: 700; }}
    .footer-actions a:hover {{ color: #fff; }}
    .footer-sha {{ color: var(--ink-3); font: 500 11px/1 var(--mono); }}
    @media (max-width: 900px) {{
      .wrap {{ padding-left: 20px; padding-right: 20px; }}
      .nav-inner {{ align-items: flex-start; flex-direction: column; }}
      h1 {{ font-size: 34px; }}
      .graph-canvas-card {{ flex-basis: 100%; }}
      .reading-card {{ min-width: 0; flex-basis: 100%; }}
      .graph-layout {{ gap: 16px; }}
    }}
    @media (max-width: 560px) {{
      .hero {{ padding-top: 38px; }}
      .stat-cell, .context-cell {{ min-width: 50%; }}
      .outcome-card {{ min-width: 100%; }}
    }}
  </style>
</head>
<body>
  <div class="page">
    <header class="nav">
      <div class="wrap nav-inner">
        <nav class="crumbs" aria-label="Breadcrumb">
          <a class="brand" href="/"><span class="brand-mark">OE</span><span>Ops Evidence</span></a>
          <span class="crumb-sep">/</span><a href="/#review-set">Reviews</a>
          <span class="crumb-sep">/</span><a href="/ui/full-review-page?evidence_sha256={_html(evidence_sha256)}">{_html(service_label)}</a>
          <span class="crumb-sep">/</span><span>Canonical Review Graph</span>
        </nav>
        <div class="nav-actions">
          <span class="sha-chip">graph_sha {_html(_short_sha(graph_sha) if graph_sha else "precomputed")}</span>
          <a href="{_html(_public_repo_url())}">GitHub</a>
          <span class="live-chip"><i></i>Persisted result</span>
        </div>
      </div>
    </header>

    <main>
      <section class="wrap hero">
        <div class="hero-kickers">
          <span class="kicker">canonical_review_graph.v1</span>
          <span class="soft-chip">nodes &amp; edges - not a verdict</span>
          <span class="soft-chip">Incident gate signal: {_html(incident_gate_signal)}</span>
        </div>
        <h1>Every review target keeps its providers and evidence attached.</h1>
        <p class="hero-copy">{_html(title)} The graph routes and scores review work; it never promotes a cause. Selecting a target lights up its own provider stance ledger.</p>
        <div class="stat-strip" aria-label="Graph statistics">{stats_html}</div>
      </section>

      {graph_html}
      {outcome_html}
      {ledger_html}
    </main>

    <footer class="footer">
      <div class="wrap footer-inner">
        <a class="brand" href="/"><span class="brand-mark">OE</span><span>Ops Evidence Synthesis</span></a>
        <div class="footer-actions">{action_links}</div>
        <span class="footer-sha">graph_sha {_html(_short_sha(graph_sha) if graph_sha else "precomputed")}</span>
      </div>
    </footer>
  </div>
  <script>
    (() => {{
      const panels = [...document.querySelectorAll("[data-graph-panel]")];
      const readings = [...document.querySelectorAll("[data-reading-panel]")];
      const jumps = [...document.querySelectorAll("[data-target-jump]")];
      const selectTarget = (key) => {{
        panels.forEach((panel) => {{ panel.hidden = panel.dataset.graphPanel !== key; }});
        readings.forEach((panel) => {{ panel.hidden = panel.dataset.readingPanel !== key; }});
      }};
      jumps.forEach((jump) => jump.addEventListener("click", () => selectTarget(jump.dataset.targetJump || "")));
      selectTarget("{_js_string(selected_key)}");
    }})();
  </script>
</body>
</html>"""


def _render_precomputed_graph_page_legacy(evidence_sha256: str, payload: dict[str, Any]) -> str:
    response = _precomputed_review_graph_response(payload, evidence_sha256=evidence_sha256)
    graph_model = response.get("graph") if isinstance(response.get("graph"), dict) else {}
    summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
    finding = summary.get("finding") if isinstance(summary.get("finding"), dict) else {}
    review = summary.get("review") if isinstance(summary.get("review"), dict) else {}
    providers = summary.get("providers") if isinstance(summary.get("providers"), dict) else {}
    graph_summary = payload.get("review_graph_summary") if isinstance(payload.get("review_graph_summary"), dict) else {}
    context = response.get("analysis_context") if isinstance(response.get("analysis_context"), dict) else {}
    nodes = [row for row in graph_model.get("nodes") or [] if isinstance(row, dict)]
    edges = [row for row in graph_model.get("edges") or [] if isinstance(row, dict)]
    targets = [row for row in payload.get("targets") or [] if isinstance(row, dict)]
    provider_statuses = [row for row in payload.get("provider_statuses") or [] if isinstance(row, dict)]
    provider_ids = [str(row.get("provider_id") or "") for row in provider_statuses if str(row.get("provider_id") or "")]
    if not provider_ids:
        provider_ids = [
            str(node.get("label") or "")
            for node in nodes
            if str(node.get("type") or "") == "provider" and str(node.get("label") or "")
        ]
    target_models = [_review_graph_target_model(target, index=index, provider_ids=provider_ids) for index, target in enumerate(targets, start=1)]
    selected_key = _review_graph_initial_key(target_models)
    target_count = len(target_models)
    provider_total = int(providers.get("total") or len(provider_ids) or 0)
    provider_success = int(providers.get("success") or len(provider_ids) or 0)
    unique_refs = sorted(
        {
            str(ref)
            for target in targets
            for ref in (target.get("evidence_refs") if isinstance(target.get("evidence_refs"), list) else [])
            if str(ref).strip()
        }
    )
    context_cards = ""
    projection_interpretation_html = ""
    if context:
        cells = [
            ("DB ingested logs", _human_count(_context_count(context.get("db_ingested_log_count")))),
            ("DB corpus coverage", _coverage_text(context.get("db_corpus_coverage_ratio"))),
            ("DB covered rows", _human_count(_context_count(context.get("db_corpus_covered_row_count")))),
            ("DB pattern groups", _human_count(_context_count(context.get("db_corpus_pattern_count")))),
            ("Derived Evidence Items", _human_count(_context_nested_count(context, "evidence_item_accounting", "derived_metric_or_operational_items"))),
            ("DB singleton rows", _human_count(_coverage_class_count(context, "singleton"))),
            ("DB rare rows", _human_count(_coverage_class_count(context, "rare"))),
            ("Prompt-direct DB rows", _human_count(_context_count(context.get("db_corpus_direct_prompt_row_count")))),
            ("Provider corpus coverage", _coverage_text(context.get("provider_full_corpus_coverage_ratio"))),
            ("Provider corpus items", _human_count(_context_count(context.get("provider_full_corpus_analyzed_evidence_items")))),
            ("Provider chunks", _human_count(_context_count(context.get("provider_full_corpus_chunk_count")))),
            ("Chunk manifests", _human_count(_context_count(context.get("provider_full_corpus_chunk_manifest_count")))),
            ("Unassigned items", _human_count(_context_count(context.get("provider_full_corpus_unassigned_evidence_items")))),
            ("Single-prompt projection", _human_count(_context_count(context.get("model_projection_evidence_items")))),
            ("Projected occurrences", _human_count(_context_count(context.get("model_projection_occurrence_count")))),
            ("Projection coverage", _coverage_text(context.get("model_projection_occurrence_coverage_ratio"))),
        ]
        projection_interpretation = str(context.get("model_projection_interpretation") or "").strip()
        determinism_points = _determinism_scope_points(context)
        projection_interpretation_html = (
            f"<p>{_html(projection_interpretation)}</p>" if projection_interpretation else ""
        )
        determinism_html = "".join(f"<p>{_html(point)}</p>" for point in determinism_points)
        context_cards = "".join(
            f"""
            <article class="metric-cell">
              <span>{_html(label)}</span>
              <strong>{_html(value)}</strong>
            </article>
            """
            for label, value in cells
            if _show_context_cell(label, value)
        )
        projection_interpretation_html = projection_interpretation_html + determinism_html
    graph_stats = [
        (_human_count(int(graph_model.get("node_count") or len(nodes))), "ledger nodes"),
        (_human_count(target_count), "review targets"),
        (_human_count(int(graph_summary.get("convergence_count") or 0)), "convergence groups"),
        (_human_count(len(unique_refs)), "cited evidence refs"),
        (_human_count(int(graph_summary.get("conflict_count") or 0)), "explicit conflicts"),
        (f"{provider_success} / {provider_total}" if provider_total else _human_count(len(provider_ids)), "providers on graph"),
    ]
    gate_stats = [
        (_human_count(int(review.get("primary_targets") or graph_summary.get("primary_promoted_count") or 0)), "primary candidates"),
        (_human_count(int(review.get("validation_targets") or 0)), "validation targets"),
        ("0", "auto-promoted causes"),
        ("human", "final judgement owner"),
    ]
    incident_gate_signal = _incident_gate_signal_text(
        graph_summary.get("incident_gate_signal") or graph_summary.get("incident_baseline")
    )
    target_promotion_policy = str(graph_summary.get("target_promotion_policy") or "").strip()
    filter_counts = {
        "all": target_count,
        "primary": sum(1 for model in target_models if model["category"] == "primary"),
        "validation": sum(1 for model in target_models if model["category"] == "validation"),
        "single": sum(1 for model in target_models if model["claimed"] <= 1),
    }
    filters_html = "".join(
        f"<button type='button' class='{_html('active' if key == 'all' else '')}' data-filter='{_html(key)}'>{_html(label)} <span>{_html(str(filter_counts[key]))}</span></button>"
        for key, label in (
            ("all", "All"),
            ("primary", "Primary"),
            ("validation", "Validation"),
            ("single", "Single-source"),
        )
    )
    rail_html = "\n".join(_review_graph_target_rail_html(model, selected_key=selected_key) for model in target_models)
    canvas_html = "\n".join(
        _review_graph_canvas_html(model, provider_ids=provider_ids, selected_key=selected_key) for model in target_models
    )
    selected_html = "\n".join(_review_graph_selected_summary_html(model, selected_key=selected_key) for model in target_models)
    provider_matrix_html = _review_graph_provider_matrix_html(target_models, provider_ids=provider_ids)
    ledger_breakdown_html = _review_graph_ledger_breakdown_html(graph_model)
    edge_rows = "\n".join(
        f"<li><code>{_html(str(edge.get('source') or ''))}</code> <span>-&gt;</span> <code>{_html(str(edge.get('target') or ''))}</code><b>{_html(str(edge.get('relation') or ''))}</b></li>"
        for edge in edges
    )
    stats_html = "".join(
        f"""
        <article class="stat-cell">
          <strong>{_html(value)}</strong>
          <span>{_html(label)}</span>
        </article>
        """
        for value, label in graph_stats
    )
    gate_stats_html = "".join(
        f"""
        <article class="gate-stat">
          <strong>{_html(value)}</strong>
          <span>{_html(label)}</span>
        </article>
        """
        for value, label in gate_stats
    )
    action_links = _public_action_links_html(evidence_sha256)
    graph_sha = str(summary.get("canonical_graph_sha256") or "")
    service_label = _review_graph_service_label(payload)
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Ops Evidence Review Graph</title>
  <style>
    :root {{
      color-scheme: light;
      --bg: #eef2f7;
      --surface: #ffffff;
      --surface-2: #f5f8fc;
      --border: #e1e7f0;
      --ink: #0f1b2d;
      --ink-2: #51617a;
      --ink-3: #8a97ab;
      --accent: #2a6fdb;
      --accent-soft: #e7f0fc;
      --claimed: #12836b;
      --claimed-soft: #e9f4f0;
      --silent: #a2aebf;
      --amber: #b26a00;
      --amber-soft: #f8ecd6;
      --shadow: 0 1px 2px rgba(16,27,45,.05), 0 18px 50px -22px rgba(16,27,45,.28);
      --mono: ui-monospace, SFMono-Regular, Menlo, Consolas, "Liberation Mono", monospace;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      min-height: 100vh;
      background: var(--bg);
      color: var(--ink);
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      letter-spacing: 0;
    }}
    a {{ color: inherit; text-decoration: none; }}
    p {{ margin: 0; color: var(--ink-2); line-height: 1.58; }}
    code {{ font-family: var(--mono); overflow-wrap: anywhere; }}
    .shell {{ width: min(calc(100% - 48px), 1720px); margin: 0 auto; padding: 0 0 64px; }}
    .topbar {{
      min-height: 70px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 18px;
      border-bottom: 1px solid var(--border);
    }}
    .brand {{ display: flex; align-items: center; gap: 12px; min-width: 0; }}
    .brand-mark {{
      width: 34px;
      height: 34px;
      border-radius: 8px;
      display: grid;
      place-items: center;
      background: var(--accent);
      color: #fff;
      font: 800 13px/1 var(--mono);
      flex: none;
    }}
    .crumb {{ color: var(--ink-3); font-size: 13px; overflow-wrap: anywhere; }}
    .crumb a {{ color: inherit; text-decoration: none; }}
    .crumb a:hover, .crumb a:focus-visible {{ color: var(--ink); }}
    .crumb b {{ color: var(--ink); }}
    .chips {{ display: flex; align-items: center; justify-content: flex-end; gap: 8px; flex-wrap: wrap; }}
    .chip {{
      display: inline-flex;
      align-items: center;
      border: 1px solid var(--border);
      border-radius: 999px;
      background: rgba(255,255,255,.74);
      color: var(--ink-2);
      padding: 7px 11px;
      font: 800 12px/1 var(--mono);
      white-space: nowrap;
    }}
    .hero {{ padding: 44px 0 26px; }}
    .kicker {{ color: var(--accent); font: 800 11px/1 var(--mono); letter-spacing: .08em; text-transform: uppercase; }}
    h1 {{ max-width: 930px; margin: 14px 0 0; font-size: clamp(42px, 3.7vw, 62px); line-height: 1.02; letter-spacing: 0; overflow-wrap: anywhere; }}
    h2 {{ margin: 10px 0 0; font-size: 24px; letter-spacing: 0; overflow-wrap: anywhere; }}
    h3 {{ margin: 0; font-size: 18px; letter-spacing: 0; overflow-wrap: anywhere; }}
    .hero p {{ max-width: 820px; margin-top: 16px; font-size: 16px; }}
    .stat-grid {{
      display: grid;
      grid-template-columns: repeat(6, minmax(0, 1fr));
      gap: 1px;
      overflow: hidden;
      border: 1px solid var(--border);
      border-radius: 8px;
      background: var(--border);
      margin-bottom: 34px;
    }}
    .stat-cell {{ min-width: 0; padding: 18px 16px; background: var(--surface); }}
    .stat-cell strong {{ display: block; font-size: 20px; line-height: 1; overflow-wrap: anywhere; }}
    .stat-cell span, .metric-cell span, .gate-stat span {{ display: block; color: var(--ink-3); font-size: 11px; line-height: 1.35; margin-top: 6px; }}
    .ledger-summary {{
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 12px;
      margin-bottom: 22px;
    }}
    .ledger-summary article {{
      min-width: 0;
      border: 1px solid var(--border);
      border-radius: 8px;
      background: var(--surface);
      padding: 16px;
    }}
    .ledger-summary span {{ display: block; color: var(--ink-3); font: 800 10.5px/1 var(--mono); letter-spacing: .05em; text-transform: uppercase; }}
    .ledger-summary strong {{ display: block; margin-top: 8px; color: var(--ink); font-size: 16px; line-height: 1.3; overflow-wrap: anywhere; }}
    .ledger-summary p {{ margin-top: 8px; color: var(--ink-2); font-size: 12.5px; line-height: 1.48; }}
    .provider-matrix-section {{
      margin-bottom: 24px;
      border: 1px solid var(--border);
      border-radius: 8px;
      background: var(--surface);
      padding: 22px;
      box-shadow: var(--shadow);
    }}
    .provider-matrix-head {{
      display: flex;
      justify-content: space-between;
      align-items: end;
      gap: 20px;
      flex-wrap: wrap;
    }}
    .provider-matrix-head p {{ max-width: 760px; margin-top: 8px; font-size: 13px; }}
    .provider-key {{ display: flex; flex-wrap: wrap; gap: 6px; justify-content: flex-end; }}
    .provider-key span {{
      border: 1px solid var(--border);
      border-radius: 999px;
      background: var(--surface-2);
      padding: 5px 8px;
      color: var(--ink-2);
      font: 800 10px/1 var(--mono);
    }}
    .provider-matrix {{ display: grid; gap: 8px; margin-top: 16px; }}
    .matrix-row {{
      display: grid;
      grid-template-columns: minmax(230px, .7fr) minmax(360px, 1.3fr);
      gap: 12px;
      align-items: center;
      border: 1px solid var(--border);
      border-radius: 8px;
      background: var(--surface-2);
      padding: 12px;
    }}
    .matrix-target strong {{ display: block; color: var(--ink); font: 800 12.5px/1.25 var(--mono); overflow-wrap: anywhere; }}
    .matrix-target span {{ display: block; margin-top: 5px; color: var(--ink-3); font-size: 11px; line-height: 1.35; }}
    .matrix-providers {{ display: flex; flex-wrap: wrap; gap: 6px; justify-content: flex-end; }}
    .matrix-provider {{
      display: inline-flex;
      align-items: center;
      gap: 6px;
      border: 1px solid var(--border);
      border-radius: 999px;
      background: var(--surface);
      color: var(--ink-2);
      padding: 6px 8px;
      font: 800 10.5px/1 var(--mono);
    }}
    .matrix-provider::before {{ content: ""; width: 7px; height: 7px; border-radius: 50%; background: var(--silent); }}
    .matrix-provider.claimed {{ border-color: rgba(18,131,107,.45); background: var(--claimed-soft); color: #0b5c4b; }}
    .matrix-provider.claimed::before {{ background: var(--claimed); }}
    .matrix-provider.provider-error {{ border-color: #efc9c9; background: #fff2f2; color: #994747; }}
    .matrix-provider.provider-error::before {{ background: #c45555; }}
    .explorer {{ display: grid; grid-template-columns: minmax(360px, .72fr) minmax(700px, 1.28fr); gap: 20px; align-items: start; }}
    .filters {{ display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 12px; }}
    .filters button {{
      border: 1px solid var(--border);
      border-radius: 999px;
      background: var(--surface);
      color: var(--ink-2);
      padding: 8px 12px;
      font-size: 12px;
      font-weight: 850;
      cursor: pointer;
    }}
    .filters button.active {{ background: var(--accent); border-color: var(--accent); color: #fff; }}
    .filters span {{ opacity: .72; margin-left: 3px; }}
    .target-rail {{
      max-height: 650px;
      overflow: auto;
      padding: 14px;
      border: 1px solid var(--border);
      border-radius: 8px;
      background: var(--surface-2);
      display: grid;
      gap: 8px;
    }}
    .target-rail::-webkit-scrollbar, .canvas-scroll::-webkit-scrollbar {{ width: 8px; height: 8px; }}
    .target-rail::-webkit-scrollbar-thumb, .canvas-scroll::-webkit-scrollbar-thumb {{ background: #cbd5e4; border-radius: 4px; }}
    .target-row {{
      width: 100%;
      text-align: left;
      border: 1px solid var(--border);
      border-radius: 8px;
      background: var(--surface);
      padding: 12px 13px;
      cursor: pointer;
      display: grid;
      gap: 9px;
    }}
    .target-row.active {{ border-color: rgba(42,111,219,.55); box-shadow: 0 0 0 2px rgba(42,111,219,.12); }}
    .target-row[hidden] {{ display: none; }}
    .target-line {{ display: flex; align-items: center; justify-content: space-between; gap: 10px; }}
    .target-key {{ color: var(--ink); font: 800 12.5px/1.2 var(--mono); overflow-wrap: anywhere; }}
    .target-frac {{ color: var(--ink-2); font-size: 10.5px; white-space: nowrap; }}
    .dot {{ display: inline-block; width: 9px; height: 9px; border-radius: 50%; margin-right: 6px; background: var(--silent); }}
    .dot.claimed {{ background: var(--claimed); }}
    .tag {{
      justify-self: start;
      border-radius: 999px;
      padding: 4px 9px;
      background: var(--accent-soft);
      color: var(--accent);
      font-size: 10.5px;
      font-weight: 850;
    }}
    .tag.primary {{ background: var(--amber-soft); color: var(--amber); }}
    .graph-card {{
      padding: 22px;
      border: 1px solid var(--border);
      border-radius: 8px;
      background: var(--surface);
      box-shadow: var(--shadow);
      min-width: 0;
    }}
    .graph-caption {{ color: var(--ink-2); font-size: 13.5px; line-height: 1.5; margin-bottom: 18px; }}
    .graph-caption b {{ color: var(--ink); font-family: var(--mono); }}
    .canvas-scroll {{ overflow-x: auto; }}
    .graph-panel[hidden], .selected-summary[hidden] {{ display: none; }}
    .graph-canvas {{ position: relative; width: 660px; height: 430px; margin: 0 auto; }}
    .canvas-label {{ position: absolute; top: -2px; color: var(--ink-3); font: 800 10px/1 var(--mono); letter-spacing: .05em; text-transform: uppercase; }}
    .canvas-label.providers {{ left: 8px; }}
    .canvas-label.target {{ left: 0; right: 0; text-align: center; }}
    .canvas-label.evidence {{ right: 8px; }}
    .graph-lines {{ position: absolute; inset: 0; width: 660px; height: 430px; pointer-events: none; }}
    .provider-node {{
      position: absolute;
      left: 0;
      width: 190px;
      min-height: 46px;
      display: flex;
      align-items: center;
      gap: 10px;
      padding: 9px 12px;
      border: 1px solid var(--border);
      border-radius: 8px;
      background: var(--surface-2);
    }}
    .provider-node.claimed {{ border-color: rgba(18,131,107,.45); background: var(--claimed-soft); }}
    .provider-node b {{ display: block; color: var(--ink); font-size: 12.5px; line-height: 1.15; overflow-wrap: anywhere; }}
    .provider-node small {{ display: block; margin-top: 2px; color: var(--ink-3); font: 800 10px/1 var(--mono); }}
    .provider-node.claimed small {{ color: #0b5c4b; }}
    .hub {{
      position: absolute;
      left: 266px;
      top: 151px;
      width: 128px;
      height: 128px;
      border-radius: 50%;
      display: flex;
      flex-direction: column;
      align-items: center;
      justify-content: center;
      text-align: center;
      background: #eaf0fb;
      border: 3px solid var(--accent);
      box-shadow: 0 8px 22px -10px rgba(16,27,45,.35);
    }}
    .hub.primary {{ background: #fbf1de; border-color: var(--amber); }}
    .hub-key {{ padding: 0 8px; color: var(--ink); font: 800 11px/1.25 var(--mono); overflow-wrap: anywhere; }}
    .hub-score {{ margin-top: 5px; color: var(--accent); font-size: 24px; font-weight: 900; line-height: 1; }}
    .hub.primary .hub-score {{ color: var(--amber); }}
    .hub .tag {{ margin-top: 7px; }}
    .evidence-node {{
      position: absolute;
      left: 486px;
      min-width: 116px;
      height: 26px;
      display: flex;
      align-items: center;
      gap: 7px;
      padding: 0 10px;
      border: 1px solid var(--border);
      border-radius: 8px;
      background: var(--surface-2);
    }}
    .evidence-node i {{ width: 7px; height: 7px; border-radius: 50%; background: var(--accent); flex: none; }}
    .evidence-node span {{ color: var(--ink-2); font: 800 10.5px/1 var(--mono); white-space: nowrap; }}
    .legend {{ display: flex; gap: 20px; flex-wrap: wrap; padding: 16px 4px 2px; border-top: 1px solid var(--border); margin-top: 14px; }}
    .legend span {{ display: inline-flex; align-items: center; gap: 8px; color: var(--ink-2); font-size: 11.5px; }}
    .legend-line {{ width: 26px; height: 0; border-top: 3px solid var(--claimed); }}
    .legend-line.silent {{ border-top: 2px dashed var(--silent); }}
    .legend-dot {{ width: 11px; height: 11px; border-radius: 50%; border: 2px solid var(--accent); background: #fff; }}
    .lower-grid {{ display: grid; grid-template-columns: minmax(0, 1fr) minmax(360px, .72fr); gap: 20px; margin-top: 24px; }}
    .selected-card, .gate-card, .context-panel, .edge-ledger {{
      border: 1px solid var(--border);
      border-radius: 8px;
      background: var(--surface);
      box-shadow: var(--shadow);
    }}
    .selected-card {{ padding: 24px; }}
    .selected-top {{ display: flex; align-items: center; justify-content: space-between; gap: 12px; flex-wrap: wrap; }}
    .selected-title {{ display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }}
    .selected-title code {{ font-size: 14px; color: var(--ink); font-weight: 800; }}
    .selected-meta {{ color: var(--ink-2); font: 800 12px/1 var(--mono); }}
    .selected-card p {{ margin-top: 16px; color: var(--ink); font-size: 13px; }}
    .next-check {{ display: flex; gap: 8px; margin-top: 16px; padding-top: 16px; border-top: 1px solid var(--border); color: var(--ink-2); font-size: 12.5px; }}
    .next-check b {{ color: var(--accent); font-family: var(--mono); flex: none; }}
    .gate-card {{ padding: 24px; background: var(--amber-soft); box-shadow: none; }}
    .gate-head {{ display: grid; grid-template-columns: 36px minmax(0, 1fr); gap: 12px; align-items: center; }}
    .gate-icon {{ width: 36px; height: 36px; border-radius: 10px; display: grid; place-items: center; background: var(--surface); border: 1px solid var(--border); font-weight: 900; }}
    .gate-head b {{ display: block; color: var(--amber); font-size: 14px; }}
    .gate-head span {{ display: block; margin-top: 2px; color: var(--ink-2); font-size: 12px; }}
    .gate-card p {{ margin-top: 14px; color: var(--ink-2); font-size: 12.5px; }}
    .gate-grid {{ display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px; margin-top: 18px; }}
    .gate-stat {{ padding: 12px 14px; border: 1px solid var(--border); border-radius: 8px; background: var(--surface); }}
    .gate-stat strong {{ display: block; color: var(--ink); font-size: 18px; line-height: 1; }}
    .context-panel {{ margin-top: 26px; padding: 22px; box-shadow: none; }}
    .context-grid {{ display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 10px; margin-top: 16px; }}
    .metric-cell {{ min-width: 0; padding: 12px; border: 1px solid var(--border); border-radius: 8px; background: var(--surface-2); }}
    .metric-cell strong {{ display: block; color: var(--ink); font-size: 15px; overflow-wrap: anywhere; }}
    .context-copy {{ margin-top: 12px; display: grid; gap: 7px; }}
    .context-copy p {{ font-size: 12.5px; }}
    .edge-ledger {{ margin-top: 18px; padding: 18px 22px; box-shadow: none; }}
    .edge-ledger summary {{ cursor: pointer; color: var(--ink); font-weight: 850; }}
    .edge-ledger ul {{ display: grid; gap: 7px; margin: 14px 0 0; padding-left: 0; list-style: none; max-height: 300px; overflow: auto; }}
    .edge-ledger li {{ color: var(--ink-2); font-size: 12px; line-height: 1.45; }}
    .edge-ledger b {{ margin-left: 8px; color: var(--ink-3); }}
    .footer {{ display: flex; justify-content: space-between; gap: 20px; flex-wrap: wrap; margin-top: 36px; padding-top: 24px; border-top: 1px solid var(--border); color: var(--ink-2); font-size: 12.5px; }}
    .actions {{ display: flex; flex-wrap: wrap; gap: 8px; }}
    .actions .button, .footer a {{ border: 1px solid var(--border); border-radius: 8px; background: var(--surface); padding: 8px 10px; color: var(--ink-2); font-size: 12px; font-weight: 800; }}
    @media (max-width: 1180px) {{
      .explorer, .lower-grid {{ grid-template-columns: 1fr; }}
    }}
    @media (max-width: 900px) {{
      .shell {{ width: min(calc(100% - 32px), 1720px); }}
      .topbar {{ align-items: flex-start; flex-direction: column; padding: 16px 0; }}
      .stat-grid {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
      .ledger-summary {{ grid-template-columns: 1fr; }}
      .context-grid {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
      .matrix-row {{ grid-template-columns: 1fr; }}
      .matrix-providers {{ justify-content: flex-start; }}
      h1 {{ font-size: 42px; }}
    }}
    @media (max-width: 560px) {{
      .stat-grid, .context-grid, .gate-grid {{ grid-template-columns: 1fr; }}
      .graph-card {{ padding: 16px; }}
      .target-rail {{ max-height: 440px; }}
    }}
  </style>
</head>
<body>
  <main class="shell">
    <nav class="topbar" aria-label="Primary">
      <div class="brand">
        <a class="brand-mark" href="/" aria-label="Ops Evidence home">OE</a>
        <span class="crumb">
          <a href="/#review-set">Reviews</a>
          /
          <a href="/ui/full-review-page?evidence_sha256={_html(evidence_sha256)}">{_html(service_label)}</a>
          /
          <b>Canonical Review Graph</b>
        </span>
      </div>
      <div class="chips">
        <span class="chip">canonical_review_graph.v1</span>
        <span class="chip">evidence {_html(evidence_sha256[:12])}</span>
      </div>
    </nav>

    <section class="hero">
      <div class="kicker">Review Graph - Nodes and edges - not a verdict</div>
      <h1>Every target keeps a provider stance ledger.</h1>
      <p>{_html(_public_count_text(graph_summary.get("summary") or finding.get("impact") or "The canonical graph keeps provider positions, cited Evidence Items, and human review gates connected."))} The canvas focuses on one target at a time; the matrix below keeps all provider positions visible.</p>
    </section>

    <section class="stat-grid" aria-label="Graph statistics">
      {stats_html}
    </section>

    <section class="ledger-summary" aria-label="Node and edge count breakdown">
      {ledger_breakdown_html}
    </section>

    {provider_matrix_html}

    <section class="explorer">
      <div>
        <div class="filters" aria-label="Target filters">{filters_html}</div>
        <div class="target-rail" data-target-rail>{rail_html}</div>
      </div>
      <div class="graph-card">
        {canvas_html}
      </div>
    </section>

    <section class="lower-grid">
      <div class="selected-card">
        {selected_html}
      </div>
      <aside class="gate-card">
        <div class="gate-head">
          <div class="gate-icon">HG</div>
          <div>
            <b>Promotion stays human-gated</b>
            <span>Incident gate signal: {_html(incident_gate_signal)}. The graph routes and scores review work; it never promotes a cause.</span>
          </div>
        </div>
        <p>{_html(target_promotion_policy or "Target promotion remains per-target and human-gated.")}</p>
        <div class="gate-grid">{gate_stats_html}</div>
      </aside>
    </section>

    <section class="context-panel">
      <div class="kicker">Analysis context</div>
      <h2>Projection and corpus coverage remain visible.</h2>
      <div class="context-copy">{projection_interpretation_html if context else ""}</div>
      <div class="context-grid">{context_cards}</div>
    </section>

    <details class="edge-ledger">
      <summary>Nodes and edges ledger - {int(graph_model.get("node_count") or 0)} nodes / {int(graph_model.get("edge_count") or 0)} edges</summary>
      <ul>{edge_rows}</ul>
    </details>

    <footer class="footer">
      <span>Ops Evidence Synthesis - read-only Cloud Run delivery - graph_sha {_html(_short_sha(graph_sha) if graph_sha else "precomputed")}</span>
      <div class="actions">{action_links}</div>
    </footer>
  </main>
  <script>
    (() => {{
      const rows = [...document.querySelectorAll("[data-target-row]")];
      const panels = [...document.querySelectorAll("[data-target-panel]")];
      const summaries = [...document.querySelectorAll("[data-target-summary]")];
      const filters = [...document.querySelectorAll("[data-filter]")];
      const selectTarget = (key) => {{
        rows.forEach((row) => row.classList.toggle("active", row.dataset.targetRow === key));
        panels.forEach((panel) => {{ panel.hidden = panel.dataset.targetPanel !== key; }});
        summaries.forEach((summary) => {{ summary.hidden = summary.dataset.targetSummary !== key; }});
      }};
      const applyFilter = (filter) => {{
        filters.forEach((button) => button.classList.toggle("active", button.dataset.filter === filter));
        rows.forEach((row) => {{
          const match = filter === "all"
            || row.dataset.category === filter
            || (filter === "single" && row.dataset.singleSource === "1");
          row.hidden = !match;
        }});
        const active = rows.find((row) => row.classList.contains("active") && !row.hidden);
        if (!active) {{
          const first = rows.find((row) => !row.hidden);
          if (first) selectTarget(first.dataset.targetRow || "");
        }}
      }};
      rows.forEach((row) => row.addEventListener("click", () => selectTarget(row.dataset.targetRow || "")));
      filters.forEach((button) => button.addEventListener("click", () => applyFilter(button.dataset.filter || "all")));
      selectTarget("{_js_string(selected_key)}");
      applyFilter("all");
    }})();
  </script>
</body>
</html>"""


def _review_graph_service_label(payload: dict[str, Any]) -> str:
    evidence_sha = str(payload.get("evidence_sha256") or "").strip()
    public_label = _public_manifest_label_for_evidence(evidence_sha)
    if public_label:
        return public_label
    generation = payload.get("generation") if isinstance(payload.get("generation"), dict) else {}
    service = str(generation.get("service") or "").strip()
    if service:
        return service
    profile_context = payload.get("profile_context") if isinstance(payload.get("profile_context"), dict) else {}
    profile_id = str(profile_context.get("profile_id") or "").strip()
    if profile_id:
        return profile_id.replace("_sample_source_approved", "").replace("_source_approved", "").replace("_", " ")
    return "precomputed review"


def _review_graph_target_key(target: dict[str, Any], *, index: int) -> str:
    subsystem = str(target.get("subsystem") or "").strip()
    if subsystem:
        return subsystem
    title = str(target.get("title") or "").strip()
    if ":" in title:
        suffix = title.rsplit(":", 1)[-1].strip()
        if suffix:
            return suffix
    target_id = str(target.get("review_target_id") or target.get("target_id") or "").strip()
    return target_id or f"target_{index:02d}"


def _review_graph_target_state(target: dict[str, Any]) -> str:
    promotion = target.get("promotion") if isinstance(target.get("promotion"), dict) else {}
    return str(promotion.get("state") or target.get("state") or target.get("status") or "validation").strip()


def _review_graph_target_model(target: dict[str, Any], *, index: int, provider_ids: list[str]) -> dict[str, Any]:
    key = _review_graph_target_key(target, index=index)
    state = _review_graph_target_state(target)
    category = "primary" if state == "primary_candidate" else "validation"
    positions = [row for row in target.get("provider_positions") or [] if isinstance(row, dict)]
    by_provider = {str(row.get("provider_id") or ""): row for row in positions}
    provider_rows = []
    for provider_id in provider_ids:
        row = by_provider.get(provider_id, {})
        stance = str(row.get("stance") or "silent").strip() or "silent"
        provider_rows.append(
            {
                "provider_id": provider_id,
                "short": _review_graph_provider_short_name(provider_id),
                "stance": stance,
                "one_line": str(row.get("one_line") or ""),
            }
        )
    claimed = sum(1 for row in provider_rows if row["stance"].casefold() == "claimed")
    silent = sum(1 for row in provider_rows if row["stance"].casefold() == "silent")
    evidence_refs = [str(item) for item in target.get("evidence_refs") or [] if str(item).strip()]
    evidence_ref_total = _target_evidence_ref_total_count(target, evidence_refs)
    agreement = target.get("agreement") if isinstance(target.get("agreement"), dict) else {}
    explanation = target.get("target_explanation") if isinstance(target.get("target_explanation"), dict) else {}
    score = _review_graph_float(
        target.get("review_priority_score"),
        target.get("raw_review_priority_score"),
        agreement.get("convergence_score"),
    )
    suspected = (
        str(target.get("suspected_issue") or "").strip()
        or str(explanation.get("suspected_issue") or "").strip()
        or str(target.get("claim") or "").strip()
        or str(agreement.get("summary") or "").strip()
    )
    next_question = (
        str(target.get("next_validation_question") or "").strip()
        or str(explanation.get("next_validation_question") or "").strip()
        or "What missing operational evidence would confirm or reject this review target?"
    )
    classification = str(target.get("class") or state or category).replace("_", " ").strip()
    if classification == "validation":
        classification = "validation target"
    return {
        "key": key,
        "state": state,
        "category": category,
        "classification": classification,
        "claimed": claimed,
        "silent": silent,
        "provider_rows": provider_rows,
        "score": score,
        "refs": evidence_refs,
        "display_refs": evidence_refs[:8],
        "evidence_ref_total": evidence_ref_total,
        "suspected": suspected,
        "next_question": next_question,
    }


def _review_graph_initial_key(target_models: list[dict[str, Any]]) -> str:
    for model in target_models:
        if model["category"] == "primary" and int(model["silent"]) > 0:
            return str(model["key"])
    for model in target_models:
        if model["category"] == "primary":
            return str(model["key"])
    return str(target_models[0]["key"]) if target_models else ""


def _review_graph_float(*values: object) -> float:
    for value in values:
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return 0.0


def _review_graph_provider_short_name(provider_id: str) -> str:
    provider = provider_id.casefold()
    if "gemini" in provider:
        return "Gemini"
    if "gpt-oss" in provider or "openai" in provider:
        return "GPT-OSS"
    if "mistral" in provider:
        return "Mistral"
    if "qwen" in provider:
        return "Qwen"
    if "glm" in provider:
        return "GLM"
    return provider_id[:18] if provider_id else "provider"


def _review_graph_standalone_class(model: dict[str, Any]) -> str:
    if model["category"] == "primary":
        return "primary"
    if int(model["claimed"]) <= 1:
        return "single-source"
    return "validation"


def _review_graph_standalone_badge(model: dict[str, Any]) -> str:
    graph_class = _review_graph_standalone_class(model)
    if graph_class == "primary":
        return "PRIMARY CANDIDATE"
    if graph_class == "single-source":
        return "SINGLE-SOURCE"
    return "VALIDATION TARGET"


def _review_graph_provider_state_class(stance: object) -> str:
    text = str(stance or "silent").casefold()
    if "error" in text:
        return "provider-error"
    if text == "claimed":
        return "claimed"
    return "silent"


def _review_graph_canvas_geometry(target_count: int, provider_count: int) -> dict[str, Any]:
    canvas_h = max(660, 12 + max(1, target_count) * 54)
    provider_top = 74
    provider_bottom = max(provider_top, canvas_h - 74)
    provider_gap = (provider_bottom - provider_top) / max(1, provider_count - 1)
    target_gap = 54
    return {
        "width": 600,
        "height": canvas_h,
        "provider_x": 186,
        "target_x": 350,
        "provider_y": [provider_top + index * provider_gap for index in range(max(1, provider_count))],
        "target_top": [6 + index * target_gap for index in range(max(1, target_count))],
    }


def _review_graph_standalone_graph_html(
    target_models: list[dict[str, Any]],
    *,
    provider_ids: list[str],
    selected_key: str,
) -> str:
    if not target_models:
        return ""
    panels = "\n".join(
        _review_graph_standalone_canvas_panel_html(
            model,
            target_models=target_models,
            provider_ids=provider_ids,
            selected_key=selected_key,
        )
        for model in target_models
    )
    readings = "\n".join(
        _review_graph_standalone_reading_html(model, provider_ids=provider_ids, selected_key=selected_key)
        for model in target_models
    )
    return f"""
      <section class="graph-band">
        <div class="wrap graph-section">
          <div class="graph-head">
            <div class="kicker">Provider -&gt; target graph - click a target</div>
            <div class="legend" aria-label="Graph legend">
              <span><i class="legend-line"></i>claimed</span>
              <span><i class="legend-dash"></i>silent</span>
              <span><i class="legend-dot"></i>primary</span>
            </div>
          </div>
          <p class="graph-intro">Every claimed position stays drawn as a faint thread; selecting a target lights up its own providers. Silent positions are kept as validation signal and are not dropped from the graph.</p>
          <div class="graph-layout">
            <div class="graph-canvas-card">
              <div class="canvas-labels"><span>Providers</span><span>Review targets - {len(target_models)}</span></div>
              <div class="graph-canvas-scroll">{panels}</div>
            </div>
            <aside class="reading-card">{readings}</aside>
          </div>
        </div>
      </section>
    """


def _review_graph_standalone_canvas_panel_html(
    model: dict[str, Any],
    *,
    target_models: list[dict[str, Any]],
    provider_ids: list[str],
    selected_key: str,
) -> str:
    key = str(model["key"])
    hidden = "" if key == selected_key else " hidden"
    provider_count = max(1, len(provider_ids))
    geometry = _review_graph_canvas_geometry(len(target_models), provider_count)
    provider_y = geometry["provider_y"]
    target_top = geometry["target_top"]
    provider_x = float(geometry["provider_x"])
    target_x = float(geometry["target_x"])
    canvas_h = int(geometry["height"])
    selected_index = next((index for index, row in enumerate(target_models) if str(row["key"]) == key), 0)
    selected_target_cy = target_top[selected_index] + 17

    background_edges: list[str] = []
    for target_index, target in enumerate(target_models):
        if str(target["key"]) == key:
            continue
        target_cy = target_top[target_index] + 17
        for provider_index, row in enumerate(target["provider_rows"]):
            if str(row.get("stance") or "").casefold() != "claimed":
                continue
            y = provider_y[min(provider_index, len(provider_y) - 1)]
            background_edges.append(
                f'<line x1="{provider_x:.1f}" y1="{y:.1f}" x2="{target_x:.1f}" y2="{target_cy:.1f}" class="bg-edge"></line>'
            )
    selected_edges = []
    for provider_index, row in enumerate(model["provider_rows"]):
        y = provider_y[min(provider_index, len(provider_y) - 1)]
        state_class = _review_graph_provider_state_class(row.get("stance"))
        edge_class = "selected-edge claimed" if state_class == "claimed" else f"selected-edge {state_class}"
        selected_edges.append(
            f'<line x1="{provider_x:.1f}" y1="{y:.1f}" x2="{target_x:.1f}" y2="{selected_target_cy:.1f}" class="{_html(edge_class)}"></line>'
        )
    provider_nodes = []
    for provider_index, row in enumerate(model["provider_rows"]):
        y = provider_y[min(provider_index, len(provider_y) - 1)]
        state_class = _review_graph_provider_state_class(row.get("stance"))
        provider_nodes.append(
            f"""
            <div class="provider-node {_html(state_class)}" style="top:{y - 15:.1f}px" title="{_html(str(row.get("provider_id") or ""))}">
              <span class="provider-pill">{_html(str(row.get("short") or ""))}</span>
              <span class="provider-dot"></span>
            </div>
            """
        )
    target_nodes = []
    for target_index, target in enumerate(target_models):
        target_key = str(target["key"])
        active = " active" if target_key == key else ""
        graph_class = _review_graph_standalone_class(target)
        top = target_top[target_index]
        total_positions = int(target["claimed"]) + int(target["silent"])
        target_nodes.append(
            f"""
            <button type="button" class="target-node {graph_class}{active}" style="top:{top:.1f}px" data-target-jump="{_html(target_key)}" aria-pressed="{_html('true' if active else 'false')}">
              <span class="target-name">{_html(target_key)}</span>
              <span class="target-meta">{int(target["claimed"])}/{max(1, total_positions)}</span>
            </button>
            """
        )
    return f"""
      <section class="graph-panel" data-graph-panel="{_html(key)}"{hidden}>
        <div class="graph-canvas" style="height:{canvas_h}px">
          <svg class="graph-lines" viewBox="0 0 600 {canvas_h}" style="height:{canvas_h}px" aria-hidden="true">
            {"".join(background_edges)}
            {"".join(selected_edges)}
          </svg>
          {"".join(provider_nodes)}
          {"".join(target_nodes)}
        </div>
      </section>
    """


def _review_graph_standalone_reading_html(
    model: dict[str, Any],
    *,
    provider_ids: list[str],
    selected_key: str,
) -> str:
    key = str(model["key"])
    hidden = "" if key == selected_key else " hidden"
    graph_class = _review_graph_standalone_class(model)
    badge = _review_graph_standalone_badge(model)
    total_positions = int(model["claimed"]) + int(model["silent"])
    provider_chips = []
    for index, row in enumerate(model["provider_rows"]):
        state_class = _review_graph_provider_state_class(row.get("stance"))
        role = "arbiter" if index == 0 and state_class == "claimed" else state_class.replace("-", " ")
        chip_class = "arbiter" if role == "arbiter" else state_class
        provider_chips.append(
            f"""
            <span class="provider-chip {_html(chip_class)}">
              {_html(str(row.get("short") or ""))} <span>{_html(role)}</span>
            </span>
            """
        )
    refs = [str(item) for item in model["display_refs"]]
    if refs:
        evidence_chips = "".join(f'<span class="evidence-chip">{_html(ref)}</span>' for ref in refs)
    else:
        evidence_chips = '<span class="evidence-chip empty">single-source - refs in API view</span>'
    score = f"{float(model['score']):.2f}"
    reading = (
        f"{int(model['claimed'])} of {max(1, total_positions)} providers claimed a position; "
        f"cites {int(model['evidence_ref_total']) if int(model['evidence_ref_total']) else 'no'} evidence refs."
    )
    gate_tag = "INCIDENT OPEN" if graph_class == "primary" else "NOT PROMOTED"
    return f"""
      <article class="reading-panel" data-reading-panel="{_html(key)}"{hidden}>
        <div class="reading-top">
          <div class="reading-titleline">
            <span class="badge {_html(graph_class)}">{_html(badge)}</span>
            <span class="reading-meta">subsystem {_html(key)}</span>
            <span class="reading-score">{_html(score)}</span>
          </div>
          <h3>{_html(key)}</h3>
          <div class="reading-note">{_html(reading)}</div>
        </div>
        <div class="reading-block">
          <div class="reading-label">Provider positions - {int(model["claimed"])} claimed / {int(model["silent"])} silent</div>
          <div class="provider-chips">{"".join(provider_chips)}</div>
        </div>
        <div class="reading-block white">
          <div class="reading-label">Suspected issue</div>
          <div class="suspected">{_html(str(model["suspected"]))}</div>
          <div class="reading-label">Cited evidence</div>
          <div class="evidence-chips">{evidence_chips}</div>
        </div>
        <div class="gate-block">
          <div class="gate-head">
            <span class="gate-icon">HG</span>
            <div class="gate-title">Promotion stays human-gated</div>
            <span class="gate-tag">{_html(gate_tag)}</span>
          </div>
          <div class="next-check"><b>NEXT -&gt;</b><span>{_html(str(model["next_question"]))}</span></div>
        </div>
      </article>
    """


def _review_graph_standalone_outcome_html(target_models: list[dict[str, Any]], review: dict[str, Any]) -> str:
    primary_models = [model for model in target_models if model["category"] == "primary"]
    primary_count = int(review.get("primary_targets") or len(primary_models))
    validation_count = int(review.get("validation_targets") or max(0, len(target_models) - primary_count))
    primary_label = ", ".join(str(model["key"]) for model in primary_models[:2]) or "none"
    return f"""
      <section class="wrap outcome" aria-label="Review outcome summary">
        <div class="outcome-grid">
          <article class="outcome-card blue"><strong>{_html(_human_count(primary_count))}</strong><span>primary candidate{'' if primary_count == 1 else 's'} - {_html(primary_label)}</span></article>
          <article class="outcome-card"><strong>{_html(_human_count(validation_count))}</strong><span>validation targets</span></article>
          <article class="outcome-card green"><strong>0</strong><span>auto-promoted causes</span></article>
          <article class="outcome-card dark"><strong>human</strong><span>final judgement owner</span></article>
        </div>
      </section>
    """


def _review_graph_ledger_counts(graph_model: dict[str, Any]) -> dict[str, int]:
    nodes = [row for row in graph_model.get("nodes") or [] if isinstance(row, dict)]
    edges = [row for row in graph_model.get("edges") or [] if isinstance(row, dict)]
    target_nodes = sum(1 for node in nodes if str(node.get("type") or "") == "review_target")
    provider_nodes = sum(1 for node in nodes if str(node.get("type") or "") == "provider")
    structural_nodes = max(0, len(nodes) - target_nodes - provider_nodes)
    provider_edges = sum(1 for edge in edges if str(edge.get("source") or "").startswith("provider:"))
    finding_edges = sum(1 for edge in edges if str(edge.get("relation") or "") == "has_review_target")
    gate_edges = sum(1 for edge in edges if str(edge.get("target") or "").startswith("baseline:"))
    evidence_edges = sum(1 for edge in edges if str(edge.get("relation") or "") == "produces")
    return {
        "total_nodes": int(graph_model.get("node_count") or len(nodes)),
        "total_edges": int(graph_model.get("edge_count") or len(edges)),
        "target_nodes": target_nodes,
        "provider_nodes": provider_nodes,
        "structural_nodes": structural_nodes,
        "provider_edges": provider_edges,
        "finding_edges": finding_edges,
        "gate_edges": gate_edges,
        "evidence_edges": evidence_edges,
    }


def _review_graph_standalone_ledger_html(
    graph_model: dict[str, Any],
    *,
    summary: dict[str, Any],
    context: dict[str, Any],
    row_count: int,
    unique_ref_count: int,
) -> str:
    counts = _review_graph_ledger_counts(graph_model)
    provider_items = _context_count(context.get("provider_full_corpus_analyzed_evidence_items"))
    projection_items = _context_count(context.get("model_projection_evidence_items"))
    chunk_count = _context_count(context.get("provider_full_corpus_chunk_count"))
    projected_occurrences = _context_count(context.get("model_projection_occurrence_count"))
    coverage_source = context.get("provider_full_corpus_coverage_ratio")
    if coverage_source is None:
        coverage_source = context.get("model_projection_occurrence_coverage_ratio")
    coverage = _coverage_text(coverage_source)
    raw_policy = str(summary.get("raw_log_policy") or "unknown")
    projection_interpretation = str(context.get("model_projection_interpretation") or "").strip()
    projection_note_html = f'<p class="projection-note">{_html(projection_interpretation)}</p>' if projection_interpretation else ""
    node_math = (
        f"{counts['total_nodes']} nodes = {counts['target_nodes']} target nodes + "
        f"{counts['provider_nodes']} provider nodes + {counts['structural_nodes']} structural nodes"
    )
    edge_math = (
        f"{counts['total_edges']} edges = {counts['provider_edges']} provider positions + "
        f"{counts['finding_edges']} finding links + {counts['gate_edges']} gate links + {counts['evidence_edges']} evidence link"
    )
    evidence_item_value = _human_count(provider_items or projection_items or unique_ref_count)
    if provider_items:
        evidence_item_label = f"evidence items - {_human_count(chunk_count)} chunks" if chunk_count else "evidence items"
    elif projection_items:
        evidence_item_label = "single-prompt projection items"
    else:
        evidence_item_label = "cited evidence refs"
    return f"""
      <section class="wrap ledger" aria-label="Nodes and edges ledger">
        <div class="kicker">Nodes &amp; edges ledger</div>
        <div class="ledger-head">
          <h2>{_html(_human_count(counts["total_nodes"]))} nodes - {_html(_human_count(counts["total_edges"]))} edges, all typed and hashed.</h2>
          <span>deterministic sort and de-dup over recorded outputs</span>
        </div>
        <div class="ledger-math">
          <span class="ledger-pill">Node math: <span>{_html(node_math)}</span></span>
          <span class="ledger-pill">Edge math: <span>{_html(edge_math)}</span></span>
        </div>
        <div class="ledger-types">
          <span class="ledger-pill">evidence -&gt; finding <span>produces</span></span>
          <span class="ledger-pill">finding -&gt; target <span>has_review_target</span></span>
          <span class="ledger-pill blue">provider -&gt; target <span>claimed</span></span>
          <span class="ledger-pill silent">provider -&gt; target <span>silent</span></span>
          <span class="ledger-pill">target -&gt; baseline:technical <b>established</b></span>
          <span class="ledger-pill">target -&gt; baseline:incident <span>open</span></span>
        </div>
        <div class="context-strip">
          <article class="context-cell"><strong>{_html(_human_count(row_count))}</strong><span>DB ingested logs - {coverage} coverage</span></article>
          <article class="context-cell"><strong>{_html(evidence_item_value)}</strong><span>{_html(evidence_item_label)}</span></article>
          <article class="context-cell"><strong>{_html(_human_count(projected_occurrences))}</strong><span>projected occurrences</span></article>
          <article class="context-cell green"><strong>{_html(raw_policy.replace("_", " "))}</strong><span>raw logs stay local</span></article>
        </div>
        {projection_note_html}
      </section>
    """


def _review_graph_ledger_breakdown_html(graph_model: dict[str, Any]) -> str:
    counts = _review_graph_ledger_counts(graph_model)
    items = [
        (
            "Node math",
            f"{counts['total_nodes']} nodes = {counts['target_nodes']} target nodes + {counts['provider_nodes']} provider nodes + {counts['structural_nodes']} structural nodes",
            "Structural nodes are the evidence bundle, persisted finding, technical support signal, and incident gate signal.",
        ),
        (
            "Edge math",
            f"{counts['total_edges']} edges = {counts['provider_edges']} provider positions + {counts['finding_edges']} finding links + {counts['gate_edges']} gate links + {counts['evidence_edges']} evidence link",
            "Provider-position edges include claimed, silent, and other recorded stances so disagreement is preserved.",
        ),
    ]
    return "".join(
        f"""
        <article>
          <span>{_html(label)}</span>
          <strong>{_html(value)}</strong>
          <p>{_html(detail)}</p>
        </article>
        """
        for label, value, detail in items
    )


def _review_graph_provider_matrix_html(target_models: list[dict[str, Any]], *, provider_ids: list[str]) -> str:
    if not target_models:
        return ""
    target_heading = (
        f"All {len(target_models)} targets keep their provider positions."
        if len(target_models) != 1
        else "The target keeps its provider position."
    )
    provider_key = "".join(
        f"<span>{_html(_review_graph_provider_short_name(provider_id))}</span>"
        for provider_id in provider_ids
    )
    rows = []
    for model in target_models:
        provider_pills = []
        for row in model["provider_rows"]:
            stance = str(row["stance"] or "silent").casefold()
            css_state = "provider-error" if "error" in stance else stance
            provider_pills.append(
                f"""
                <span class="matrix-provider {_html(css_state)}" title="{_html(str(row["provider_id"]))}: {_html(str(row["stance"]))}">
                  {_html(str(row["short"]))}
                </span>
                """
            )
        label = "primary candidate" if model["category"] == "primary" else "validation target"
        meta = (
            f"{label} - {int(model['claimed'])} claimed / {int(model['silent'])} silent - "
            f"{int(model['evidence_ref_total'])} evidence refs"
        )
        rows.append(
            f"""
            <article class="matrix-row">
              <div class="matrix-target">
                <strong>{_html(str(model["key"]))}</strong>
                <span>{_html(meta)}</span>
              </div>
              <div class="matrix-providers">{"".join(provider_pills)}</div>
            </article>
            """
        )
    return f"""
    <section class="provider-matrix-section" aria-label="Provider stance matrix">
      <div class="provider-matrix-head">
        <div>
          <div class="kicker">Provider stance matrix</div>
          <h2>{_html(target_heading)}</h2>
          <p>Each row shows the provider stance ledger for a target before the focused graph view opens one target in detail.</p>
        </div>
        <div class="provider-key" aria-label="Providers in this graph">{provider_key}</div>
      </div>
      <div class="provider-matrix">{"".join(rows)}</div>
    </section>
    """


def _review_graph_tag_class(model: dict[str, Any]) -> str:
    return "tag primary" if model["category"] == "primary" else "tag"


def _review_graph_target_rail_html(model: dict[str, Any], *, selected_key: str) -> str:
    key = str(model["key"])
    active = " active" if key == selected_key else ""
    single = "1" if int(model["claimed"]) <= 1 else "0"
    dot_class = "dot claimed" if int(model["claimed"]) > 1 else "dot"
    return f"""
      <button type="button" class="target-row{active}" data-target-row="{_html(key)}" data-category="{_html(str(model["category"]))}" data-single-source="{single}">
        <span class="target-line">
          <span class="target-key">{_html(key)}</span>
          <span class="target-frac"><i class="{dot_class}"></i>{int(model["claimed"])} / {int(model["claimed"]) + int(model["silent"])}</span>
        </span>
        <span class="{_review_graph_tag_class(model)}">{_html(str(model["classification"]))}</span>
      </button>
    """


def _review_graph_canvas_html(model: dict[str, Any], *, provider_ids: list[str], selected_key: str) -> str:
    key = str(model["key"])
    hidden = "" if key == selected_key else " hidden"
    primary = model["category"] == "primary"
    panel_attrs = f'data-target-panel="{_html(key)}"{hidden}'
    provider_count = max(1, len(provider_ids))
    provider_start = 46
    provider_end = 374
    provider_gap = (provider_end - provider_start) / max(1, provider_count - 1)
    provider_nodes = []
    provider_edges = []
    for index, row in enumerate(model["provider_rows"]):
        y = provider_start + provider_gap * index
        claimed = str(row["stance"]).casefold() == "claimed"
        stroke = "#12836b" if claimed else "#a2aebf"
        width = "2.5" if claimed else "1.5"
        dash = "" if claimed else "4 3"
        opacity = ".95" if claimed else ".55"
        node_class = "provider-node claimed" if claimed else "provider-node"
        provider_nodes.append(
            f"""
            <article class="{node_class}" style="top:{y - 23:.1f}px" title="{_html(str(row["provider_id"]))}">
              <i class="{'dot claimed' if claimed else 'dot'}"></i>
              <div><b>{_html(str(row["short"]))}</b><small>{_html(str(row["stance"]))}</small></div>
            </article>
            """
        )
        provider_edges.append(
            f'<path d="M 190,{y:.1f} C 232,{y:.1f} 232,215 266,215" fill="none" stroke="{stroke}" stroke-width="{width}" stroke-dasharray="{dash}" stroke-opacity="{opacity}"></path>'
        )
    refs = [str(item) for item in model["display_refs"]]
    evidence_nodes = []
    evidence_edges = []
    if refs:
        evidence_start = 46
        evidence_end = 374
        evidence_gap = (evidence_end - evidence_start) / max(1, len(refs) - 1)
        for index, ref in enumerate(refs):
            y = evidence_start + evidence_gap * index
            evidence_nodes.append(
                f"""
                <article class="evidence-node" style="top:{y - 13:.1f}px">
                  <i></i><span>{_html(ref)}</span>
                </article>
                """
            )
            evidence_edges.append(
                f'<path d="M 394,215 C 438,215 438,{y:.1f} 486,{y:.1f}" fill="none" stroke="#2a6fdb" stroke-width="1.4" stroke-opacity=".34"></path>'
            )
    else:
        evidence_nodes.append(
            """
            <article class="evidence-node" style="top:202px">
              <i></i><span>no refs</span>
            </article>
            """
        )
        evidence_edges.append('<path d="M 394,215 C 438,215 438,215 486,215" fill="none" stroke="#2a6fdb" stroke-width="1.4" stroke-opacity=".2"></path>')
    caption = (
        f"{int(model['claimed'])} of {int(model['claimed']) + int(model['silent'])} providers claimed a position, "
        f"{int(model['silent'])} stayed silent, and it cites {int(model['evidence_ref_total'])} Evidence Item association(s)."
    )
    return f"""
      <section class="graph-panel" {panel_attrs}>
        <div class="graph-caption">Reading <b>{_html(key)}</b>: {_html(caption)}</div>
        <div class="canvas-scroll">
          <div class="graph-canvas">
            <div class="canvas-label providers">Providers</div>
            <div class="canvas-label target">Review target</div>
            <div class="canvas-label evidence">Cited evidence</div>
            <svg class="graph-lines" viewBox="0 0 660 430" aria-hidden="true">
              {"".join(provider_edges)}
              {"".join(evidence_edges)}
            </svg>
            {"".join(provider_nodes)}
            <article class="hub {'primary' if primary else ''}">
              <div class="hub-key">{_html(key)}</div>
              <div class="hub-score">{float(model["score"]):.2f}</div>
              <span class="{_review_graph_tag_class(model)}">{_html('primary' if primary else 'validation')}</span>
            </article>
            {"".join(evidence_nodes)}
          </div>
        </div>
        <div class="legend">
          <span><i class="legend-line"></i>claimed a position on this target</span>
          <span><i class="legend-line silent"></i>stayed silent and remains validation signal</span>
          <span><i class="legend-dot"></i>cited Evidence Item</span>
        </div>
      </section>
    """


def _review_graph_selected_summary_html(model: dict[str, Any], *, selected_key: str) -> str:
    key = str(model["key"])
    hidden = "" if key == selected_key else " hidden"
    return f"""
      <article class="selected-summary" data-target-summary="{_html(key)}"{hidden}>
        <div class="selected-top">
          <div class="selected-title">
            <span class="{_review_graph_tag_class(model)}">{_html(str(model["classification"]))}</span>
            <code>{_html(key)}</code>
          </div>
          <span class="selected-meta">{int(model["claimed"])} claimed / {int(model["silent"])} silent</span>
        </div>
        <p>{_html(str(model["suspected"]))}</p>
        <div class="next-check"><b>next check -&gt;</b><span>{_html(str(model["next_question"]))}</span></div>
      </article>
    """


def _render_precomputed_markdown_report(evidence_sha256: str, payload: dict[str, Any]) -> str:
    summary = _precomputed_summary(payload, evidence_sha256) or {}
    finding = summary.get("finding") if isinstance(summary.get("finding"), dict) else {}
    review = summary.get("review") if isinstance(summary.get("review"), dict) else {}
    providers = summary.get("providers") if isinstance(summary.get("providers"), dict) else {}
    graph_summary = payload.get("review_graph_summary") if isinstance(payload.get("review_graph_summary"), dict) else {}
    context = payload.get("analysis_context") if isinstance(payload.get("analysis_context"), dict) else {}
    profile_context = payload.get("profile_context") if isinstance(payload.get("profile_context"), dict) else {}
    provider_statuses = [row for row in payload.get("provider_statuses") or [] if isinstance(row, dict)]
    targets = [row for row in payload.get("targets") or [] if isinstance(row, dict)]

    title = _markdown_text(finding.get("title") or "Evidence review")
    impact = _markdown_text(
        _public_finding_impact_text(summary, str(finding.get("impact") or "Review targets are available for human validation."))
    )
    service = _markdown_text(context.get("service") or "")
    environment = _markdown_text(context.get("environment") or "")
    window_start = _markdown_text(context.get("window_start") or "")
    window_end = _markdown_text(context.get("window_end") or "")
    window_text = f"{window_start} to {window_end}" if window_start or window_end else "not recorded"
    canonical_sha = _markdown_text(summary.get("canonical_graph_sha256") or "")
    input_sha = _markdown_text(summary.get("input_fingerprint_sha256") or "")
    updated_at = _markdown_text(payload.get("updated_at") or summary.get("updated_at") or "")

    lines: list[str] = [
        f"# Incident Review Report: {title}",
        "",
        (
            "> This report is review material, not an accepted incident cause. "
            "Provider convergence creates review targets; final causal judgement "
            "and operational action remain human-gated. Provider agreement is not "
            "majority-vote truth."
        ),
        "",
        "## Run Summary",
        "",
        f"- Evidence SHA256: `{_markdown_text(evidence_sha256)}`",
        f"- Updated at: {updated_at or 'not recorded'}",
        f"- Service/environment: {service or 'unknown'} / {environment or 'unknown'}",
        f"- Analysis window: {window_text}",
        f"- Sanitized rows: {_human_count(int(summary.get('log_count') or context.get('sanitized_log_count') or 0))}",
        f"- Providers: {int(providers.get('success') or 0)} / {int(providers.get('total') or 0)} schema-valid or successful outputs",
        (
            f"- Review targets: {int(review.get('primary_targets') or 0)} primary, "
            f"{int(review.get('validation_targets') or 0)} validation, "
            f"{int(review.get('monitor_only') or 0)} monitor-only, "
            f"{int(review.get('auto_archived') or 0)} auto-archived"
        ),
        f"- Canonical graph SHA256: `{canonical_sha or 'not recorded'}`",
        f"- Input fingerprint SHA256: `{input_sha or 'not recorded'}`",
        f"- Raw log policy: {_markdown_text(summary.get('raw_log_policy') or context.get('raw_log_policy') or 'unknown')}",
        "",
        "## Finding Summary",
        "",
        impact,
        "",
        "## Evidence Boundary",
        "",
    ]
    lines.extend(_markdown_bullets(_evidence_boundary_points(context)))
    lines.extend(["", "## Provider Statuses", ""])
    lines.extend(_provider_status_markdown_table(provider_statuses))
    lines.extend(["", "## Human Review Questions", ""])
    lines.extend(_markdown_bullets(_human_review_question_points(profile_context)))
    lines.extend(["", "## Review Queries This Report Supports", ""])
    lines.extend(
        _markdown_bullets(
            [
                "Show the evidence behind the highest-priority primary or validation candidate.",
                "List review units that only one provider surfaced.",
                "List targets that are blocked by missing user-impact evidence.",
                "Show provider stance disagreements without treating majority vote as truth.",
                "Show which profile questions map to downstream review units.",
            ]
        )
    )
    lines.extend(["", "## Arbitration Summary", ""])
    lines.extend(_markdown_bullets(_graph_summary_points(graph_summary)))
    lines.extend(["", "## Top Review Targets", ""])
    if not targets:
        lines.append("No review targets were projected.")
    else:
        for index, target in enumerate(targets, start=1):
            lines.extend(_target_markdown_section(target, index=index))
    lines.extend(["", "## Reproducibility Notes", ""])
    lines.extend(_markdown_bullets(_reproducibility_points(context)))
    return "\n".join(lines).rstrip() + "\n"


def _evidence_boundary_points(context: dict[str, Any]) -> list[str]:
    points = [
        "Raw logs and raw source are not exposed by the public read-only UI.",
    ]
    raw_source_policy = str(context.get("raw_source_policy") or "").strip()
    if raw_source_policy:
        points.append(f"Raw source policy: {raw_source_policy}.")
    if context.get("source_context_sha256"):
        points.append(f"Sanitized source context SHA256: `{context.get('source_context_sha256')}`.")
    if context.get("source_analysis_sha256"):
        points.append(f"Sanitized source analysis SHA256: `{context.get('source_analysis_sha256')}`.")
    db_rows = _context_count(context.get("db_corpus_row_count") or context.get("db_ingested_log_count"))
    covered_rows = _context_count(context.get("db_corpus_covered_row_count"))
    db_coverage = _coverage_text(context.get("db_corpus_coverage_ratio"))
    if db_rows or covered_rows or db_coverage:
        points.append(
            f"DB coverage ledger: {_human_count(covered_rows or db_rows)} / {_human_count(db_rows)} row(s)"
            + (f" ({db_coverage})" if db_coverage else "")
            + "."
        )
    provider_items = _context_count(context.get("provider_full_corpus_analyzed_evidence_items"))
    provider_chunks = _context_count(context.get("provider_full_corpus_chunk_count"))
    provider_coverage = _coverage_text(context.get("provider_full_corpus_coverage_ratio"))
    if provider_items or provider_chunks or provider_coverage:
        points.append(
            f"Provider corpus: {_human_count(provider_items)} Evidence Item(s), "
            f"{_human_count(provider_chunks)} chunk(s), {provider_coverage or 'unknown coverage'}."
        )
    unassigned = _context_count(context.get("provider_full_corpus_unassigned_evidence_items"))
    points.append(f"Unassigned provider Evidence Items: {_human_count(unassigned)}.")
    projection = str(context.get("model_projection_interpretation") or "").strip()
    if projection:
        points.append(projection)
    points.extend(_determinism_scope_points(context))
    return points


def _human_review_question_points(profile_context: dict[str, Any]) -> list[str]:
    points: list[str] = []
    if profile_context.get("profile_id"):
        points.append(f"Approved profile context: `{profile_context.get('profile_id')}`.")
    if profile_context.get("confidence_action"):
        confidence = profile_context.get("confidence_summary") if isinstance(profile_context.get("confidence_summary"), dict) else {}
        overall = confidence.get("overall_confidence")
        confidence_text = f"Profile confidence action: `{profile_context.get('confidence_action')}`"
        if overall not in (None, ""):
            confidence_text += f" with overall confidence {overall}; {_confidence_action_explanation(str(profile_context.get('confidence_action') or ''), overall)}."
        else:
            confidence_text += "."
        points.append(confidence_text)
    provisional = _string_items(profile_context.get("provisional_user_outcomes"))
    if provisional:
        points.append("Provisional user outcomes pending approval: " + "; ".join(provisional[:4]) + ".")
    required = _string_items(profile_context.get("required_human_decisions"))
    points.extend(required[:6])
    human_questions = _string_items(profile_context.get("human_questions"))
    points.extend(human_questions[:8])
    links = profile_context.get("profile_to_review_links")
    if isinstance(links, list):
        for row in links[:5]:
            if not isinstance(row, dict):
                continue
            question = str(row.get("question") or "").strip()
            units = ", ".join(str(item) for item in row.get("review_units") or [] if str(item).strip())
            reason = str(row.get("reason") or "").strip()
            if question and units:
                points.append(f"{question} -> {units}. {reason}".strip())
    if not points:
        points.append("No approved profile questions were attached; treat review units as candidate-only.")
    return points


def _graph_summary_points(graph_summary: dict[str, Any]) -> list[str]:
    if not graph_summary:
        return ["No graph-level arbitration summary was persisted."]
    points = []
    for key, label in (
        ("provider_detection_overlap", "Provider detection overlap"),
        ("technical_baseline", "Technical support"),
        ("incident_gate_signal", "Incident gate signal"),
        ("target_promotion_policy", "Target promotion policy"),
        ("score_definition", "Score definition"),
        ("note", "Count interpretation"),
    ):
        value = str(graph_summary.get(key) or "").strip()
        if value:
            points.append(f"{label}: {value}.")
    points.append(
        f"Target verdict counts: {int(graph_summary.get('convergence_count') or 0)} converged, "
        f"{int(graph_summary.get('single_source_count') or 0)} single-source, "
        f"{int(graph_summary.get('rule_or_context_count') or 0)} rule/context, "
        f"{int(graph_summary.get('conflict_count') or 0)} explicit conflict(s)."
    )
    return points


def _target_markdown_section(target: dict[str, Any], *, index: int) -> list[str]:
    score = float(target.get("review_priority_score") or target.get("priority_score") or 0.0)
    title = _markdown_text(target.get("title") or target.get("review_target_id") or f"Target {index}")
    target_class = _markdown_text(target.get("class") or target.get("target_class") or "review_target")
    subsystem = _markdown_text(target.get("subsystem") or target.get("canonical_review_unit") or "general")
    evidence_refs = target.get("evidence_refs") if isinstance(target.get("evidence_refs"), list) else []
    evidence_ref_total_count = _target_evidence_ref_total_count(target, evidence_refs)
    agreement = target.get("agreement") if isinstance(target.get("agreement"), dict) else {}
    promotion = target.get("promotion") if isinstance(target.get("promotion"), dict) else {}
    explanation = target.get("target_explanation") if isinstance(target.get("target_explanation"), dict) else {}
    review_reason = target.get("review_reason") if isinstance(target.get("review_reason"), dict) else {}
    missing = _string_items(target.get("missing_evidence"))
    caveats = _string_items(target.get("caveats"))
    evidence_summary = _string_items(target.get("evidence_summary") or explanation.get("evidence_summary"))
    counter_summary = _string_items(target.get("counter_evidence_summary") or explanation.get("counter_evidence_summary"))
    lines = [
        f"### Target {index}: {title}",
        "",
        f"- Class/subsystem: `{target_class}` / `{subsystem}`",
        f"- Priority: {score:.3f} (review urgency, not truth probability)",
        f"- Provider stance: {_provider_position_summary(target)}",
        f"- Evidence tracking: {len(evidence_refs)} displayed / {evidence_ref_total_count} chunk-tracked Evidence Item association(s)",
        f"- Agreement: {_markdown_text(_target_agreement_text(target))}",
        f"- Promotion gate: {_markdown_text(_target_promotion_text(target))}",
    ]
    headline = str(review_reason.get("headline") or "").strip()
    if headline:
        lines.append(f"- Why this target is in review: {_markdown_text(headline)}")
    for factor in _string_items(review_reason.get("factors"))[:4]:
        lines.append(f"  - {_markdown_text(factor)}")
    suspected_issue = str(target.get("suspected_issue") or explanation.get("suspected_issue") or "").strip()
    mechanism = str(target.get("operational_mechanism") or explanation.get("operational_mechanism") or "").strip()
    why_it_matters = str(target.get("why_it_matters") or explanation.get("why_it_matters") or "").strip()
    next_question = str(
        target.get("next_validation_question")
        or explanation.get("next_validation_question")
        or target.get("recommended_request_type")
        or ""
    ).strip()
    why_not_promoted = str(target.get("why_not_promoted") or explanation.get("why_not_promoted") or "").strip()
    for label, value in (
        ("Suspected issue", suspected_issue),
        ("Operational mechanism", mechanism),
        ("Why it matters", why_it_matters),
        ("Why not promoted", why_not_promoted),
        ("Next validation question", next_question),
    ):
        if value:
            lines.append(f"- {label}: {_markdown_text(value)}")
    if evidence_summary:
        lines.append("- Evidence summary:")
        for item in evidence_summary[:6]:
            lines.append(f"  - {_markdown_text(item)}")
        if len(evidence_summary) > 6:
            lines.append(f"  - {len(evidence_summary) - 6} additional summary item(s) omitted from this report view.")
    if counter_summary:
        lines.append("- Counter or weak signals:")
        for item in counter_summary[:4]:
            lines.append(f"  - {_markdown_text(item)}")
    if missing:
        lines.append("- Missing evidence:")
        for item in missing[:6]:
            lines.append(f"  - {_markdown_text(item)}")
        if len(missing) > 6:
            lines.append(f"  - {len(missing) - 6} additional missing-evidence item(s) omitted from this report view.")
    elif not str(promotion.get("state") or "").lower().startswith("primary"):
        lines.append("- Missing evidence: none listed; promotion still depends on the gate text above.")
    if caveats:
        lines.append("- Caveats: " + "; ".join(_markdown_text(item) for item in caveats[:4]) + ".")
    lines.append("")
    return [_public_count_text(line) for line in lines]


def _provider_status_markdown_table(provider_statuses: list[dict[str, Any]]) -> list[str]:
    if not provider_statuses:
        return ["No provider status rows were persisted."]
    rows = [
        "| Provider | Model | Status | Schema valid | Output hash |",
        "| --- | --- | --- | --- | --- |",
    ]
    for row in provider_statuses:
        rows.append(
            "| "
            + " | ".join(
                [
                    _markdown_cell(row.get("provider_id") or row.get("provider") or ""),
                    _markdown_cell(row.get("model_name") or ""),
                    _markdown_cell(row.get("status") or ""),
                    _markdown_cell(str(bool(row.get("schema_valid"))).lower()),
                    _markdown_cell(str(row.get("raw_output_sha256") or "")[:12]),
                ]
            )
            + " |"
        )
    return rows


def _reproducibility_points(context: dict[str, Any]) -> list[str]:
    points = [
        "Real provider outputs are recorded and hashed; the report does not claim live model byte-for-byte regeneration.",
        "Canonical graph construction is deterministic over the recorded provider and chunk outputs.",
        "Public pages are read-only and do not start collectors, model calls, or write APIs on GET.",
    ]
    row_assignment_sha = str(context.get("db_corpus_row_assignments_sha256") or "").strip()
    if row_assignment_sha:
        points.append(f"DB row assignment ledger SHA256: `{row_assignment_sha}`.")
    manifest_hashes = context.get("provider_full_corpus_chunk_manifest_sha256s")
    if isinstance(manifest_hashes, list) and manifest_hashes:
        points.append(
            "Chunk manifest SHA256 prefixes: "
            + ", ".join(f"`{str(item)[:12]}`" for item in manifest_hashes[:6])
            + "."
        )
    return points


def _markdown_bullets(points: list[str]) -> list[str]:
    cleaned = [_markdown_text(point) for point in points if str(point).strip()]
    if not cleaned:
        return ["- Not recorded."]
    return [f"- {point}" for point in cleaned]


def _markdown_text(value: object) -> str:
    text = _public_count_text(value).replace("\r\n", "\n").replace("\r", "\n")
    return "\n  ".join(part.strip() for part in text.split("\n") if part.strip())


def _markdown_cell(value: object) -> str:
    text = _markdown_text(value)
    return text.replace("|", "\\|") or " "


def _detail_provider_mode_label(payload: dict[str, Any]) -> str:
    generation = payload.get("generation") if isinstance(payload.get("generation"), dict) else {}
    mode = str(generation.get("provider_mode") or "").strip()
    if "real_api" in mode:
        return "real API"
    if mode:
        return _display_policy(mode)
    return "precomputed"


def _detail_case_label(payload: dict[str, Any], review: dict[str, Any]) -> str:
    context = payload.get("analysis_context") if isinstance(payload.get("analysis_context"), dict) else {}
    service = str(context.get("service") or "").strip()
    service_labels = {
        "stream_v3_runtime": "stream_v3 runtime",
        "stream_v3_arena_monitoring": "stream_v3 monitoring",
        "stream_v3_monitoring": "stream_v3 monitoring",
    }
    if service:
        return service_labels.get(service, service.replace("_", " "))
    if int(review.get("primary_targets") or 0):
        return "Primary Review"
    if int(review.get("validation_targets") or 0):
        return "Validation Review"
    return "Full Review"


def _detail_service_id(payload: dict[str, Any]) -> str:
    context = payload.get("analysis_context") if isinstance(payload.get("analysis_context"), dict) else {}
    return str(context.get("service") or "").strip()


def _detail_is_observation_gap(payload: dict[str, Any]) -> bool:
    return _detail_service_id(payload) in {"stream_v3_arena_monitoring", "stream_v3_monitoring"}


def _detail_hero_copy(
    *,
    payload: dict[str, Any],
    review: dict[str, Any],
    providers: dict[str, Any],
    finding_title: str,
    finding_impact: str,
    log_count: int,
) -> tuple[str, str]:
    if not _detail_is_observation_gap(payload):
        return finding_title, finding_impact
    provider_success = int(providers.get("success") or 0)
    provider_total = int(providers.get("total") or 0)
    primary_targets = int(review.get("primary_targets") or 0)
    validation_targets = int(review.get("validation_targets") or 0)
    row_count = _human_count(log_count)
    title = "Zero accepted causes. This is the system showing restraint."
    impact = (
        f"{finding_title}. Across {row_count} monitoring rows, "
        f"{provider_success} / {provider_total} schema-valid providers raised "
        f"{_review_target_count_text(primary_targets, validation_targets)}. "
        "The UI keeps those signals human-gated because missing liveness and weak observation are review work, "
        "not accepted incident causes."
    )
    return title, impact


def _detail_coverage_label(payload: dict[str, Any]) -> str:
    context = payload.get("analysis_context") if isinstance(payload.get("analysis_context"), dict) else {}
    for key in (
        "provider_full_corpus_coverage_ratio",
        "db_corpus_coverage_ratio",
        "model_projection_occurrence_coverage_ratio",
    ):
        value = _coverage_text(context.get(key))
        if value:
            return value
    return "precomputed"


def _detail_summary_cells_html(
    *,
    payload: dict[str, Any],
    review: dict[str, Any],
    providers: dict[str, Any],
    targets: list[dict[str, Any]],
    raw_policy: str,
    log_count: int,
) -> str:
    target_count = len(targets) or (
        int(review.get("primary_targets") or 0)
        + int(review.get("validation_targets") or 0)
        + int(review.get("monitor_only") or 0)
        + int(review.get("auto_archived") or 0)
    )
    primary_targets = int(review.get("primary_targets") or 0)
    raw_policy_normalized = str(raw_policy).strip().lower().replace("_", " ")
    raw_logs_local = raw_policy_normalized in {"not uploaded", "local"}
    raw_logs_value = "local" if raw_logs_local else _display_policy(raw_policy)
    raw_logs_note = "raw logs not uploaded" if raw_logs_local else _human_count(log_count)
    cells = [
        (
            f"{int(providers.get('success') or 0)} / {int(providers.get('total') or 0)}",
            "schema-valid providers",
            _display_policy(str(providers.get("pipeline_status") or "precomputed")),
        ),
        (str(primary_targets), "primary candidates", "restraint" if primary_targets == 0 else "human-gated"),
        (str(int(review.get("validation_targets") or 0)), "validation targets", "human review work"),
        (str(target_count), "review targets", "canonical queue"),
        (_detail_coverage_label(payload), "ledger coverage", "sanitized corpus"),
        (raw_logs_value, "raw logs", raw_logs_note),
    ]
    rows = []
    for value, label, note in cells:
        note_html = f"<small>{_html(note)}</small>" if note else ""
        safe_class = ""
        if label == "raw logs" and str(value).strip().lower() in {"local", "not uploaded", "not_uploaded"}:
            safe_class = " safe"
        if label == "primary candidates" and str(value).strip() == "0":
            safe_class = " safe"
        rows.append(
            f"""
        <article class="stat-cell{safe_class}">
          <strong>{_html(value)}</strong>
          <span>{_html(label)}</span>
          {note_html}
        </article>
        """
        )
    return "".join(rows)


def _detail_action_links_html(evidence_sha256: str) -> str:
    evidence = _url_quote(evidence_sha256)
    links = [
        ("API view", f"/ui/api?evidence_sha256={evidence}", "human-readable JSON"),
        ("Review graph", f"/ui/review-graph?evidence_sha256={evidence}", "nodes and provider positions"),
        ("Markdown report", f"/ui/report.md?evidence_sha256={evidence}", "human-readable Markdown report"),
    ]
    for demo_id in _public_rescore_demo_ids_for_evidence(evidence_sha256):
        links.append(("More Data Loop", f"/ui/rescore-demo?id={quote(demo_id)}", demo_id))
    links.extend(
        [
            ("GitHub", _public_repo_url(), "repository"),
            ("Architecture", _public_architecture_url(), "system diagram"),
            ("Demo Script", _public_demo_script_url(), "3 minute walkthrough"),
        ]
    )
    return "".join(
        f'<a class="button" href="{_html(url)}" title="{_html(title)}">{_html(label)}</a>'
        for label, url, title in links
    )


def _target_anchor_id(target: dict[str, Any], *, index: int) -> str:
    source = str(
        target.get("review_target_id")
        or target.get("target_id")
        or target.get("canonical_review_unit")
        or target.get("subsystem")
        or index
    ).lower()
    safe = "".join(ch if ch.isalnum() else "-" for ch in source).strip("-")
    while "--" in safe:
        safe = safe.replace("--", "-")
    return f"target-{index}-{safe[:72] or 'review'}"


def _target_score_value(target: dict[str, Any]) -> float:
    try:
        return float(target.get("review_priority_score") or target.get("priority_score") or target.get("score") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _target_group_key(target: dict[str, Any]) -> str:
    target_class = str(target.get("class") or target.get("target_class") or "").casefold()
    if target_class == "primary_candidate":
        return "primary"
    agreement = target.get("agreement") if isinstance(target.get("agreement"), dict) else {}
    verdict = str(agreement.get("verdict") or "").casefold()
    if "single" in verdict:
        return "single"
    return "convergence"


def _target_group_label(group: str) -> str:
    return {
        "primary": "Primary",
        "convergence": "Convergence",
        "single": "Single-source",
    }.get(group, "Review")


def _target_class_label(target: dict[str, Any]) -> str:
    return _display_policy(str(target.get("class") or target.get("target_class") or "review_target"))


def _target_unit_label(target: dict[str, Any]) -> str:
    return str(
        target.get("canonical_review_unit")
        or target.get("subsystem")
        or target.get("component")
        or target.get("title")
        or "review_target"
    )


def _workspace_provider_counts(target: dict[str, Any]) -> tuple[int, int, int]:
    counts = _provider_position_counts(target)
    claimed = counts.get("claimed", 0)
    silent = counts.get("silent", 0)
    total = (
        counts.get("claimed", 0)
        + counts.get("contradicted", 0)
        + counts.get("silent", 0)
    ) or int(target.get("provider_count") or 0)
    return claimed, silent, total


def _workspace_provider_error_count(target: dict[str, Any]) -> int:
    counts = _provider_position_counts(target)
    return sum(
        count
        for stance, count in counts.items()
        if stance not in {"claimed", "contradicted", "silent"}
    )


def _workspace_convergence_label(
    *,
    claimed: int,
    silent: int,
    provider_error_count: int,
    score: float,
) -> str:
    parts = [f"{claimed} claimed", f"{silent} silent"]
    if provider_error_count:
        parts.append(f"{provider_error_count} provider error")
    parts.append(f"{score:.2f}")
    return " / ".join(parts)


def _workspace_queue_claim_label(target: dict[str, Any]) -> str:
    claimed, _silent, total = _workspace_provider_counts(target)
    error_count = _workspace_provider_error_count(target)
    label = f"{claimed}/{max(total, 1)} claimed"
    if error_count:
        label += f" + {error_count} error"
    return label


def _workspace_provider_label(provider_id: str) -> str:
    normalized = provider_id.lower()
    if "gemini" in normalized:
        return "Gemini"
    if "gpt" in normalized or "openai" in normalized:
        return "GPT-OSS"
    if "mistral" in normalized:
        return "Mistral"
    if "qwen" in normalized:
        return "Qwen"
    if "glm" in normalized:
        return "GLM"
    return provider_id or "Provider"


def _workspace_provider_subtitle(provider_id: str) -> str:
    normalized = provider_id.lower()
    if "gemini" in normalized:
        return "reference / arbiter"
    if "vertex" in normalized:
        return "on Vertex"
    if "agent" in normalized:
        return "agent-platform"
    return "provider output"


def _workspace_queue_item_html(target: dict[str, Any], *, index: int) -> str:
    target_id = _target_anchor_id(target, index=index)
    unit = _target_unit_label(target)
    score = _target_score_value(target)
    claimed, _silent, total = _workspace_provider_counts(target)
    total = max(total, 1)
    width = (claimed / total) * 100
    active = " active" if index == 1 else ""
    return f"""
        <button class="workspace-queue-item{active}" type="button" data-target-id="{_html(target_id)}" data-target-group="{_html(_target_group_key(target))}">
          <span class="queue-title-row">
            <strong>{_html(unit)}</strong>
            <b>{score:.2f}</b>
          </span>
          <span class="queue-meta-row">
            <span class="pill">{_html(_target_class_label(target))}</span>
            <span>{_html(_workspace_queue_claim_label(target))}</span>
          </span>
          <span class="workspace-progress" aria-hidden="true">
            <span style="width:{width:.1f}%"></span>
          </span>
        </button>
    """


def _workspace_provider_cards_html(target: dict[str, Any]) -> str:
    positions = [row for row in target.get("provider_positions") or [] if isinstance(row, dict)]
    if not positions:
        return "<p>Provider positions were not projected for this persisted target.</p>"
    rows = []
    for row in positions:
        provider_id = str(row.get("provider_id") or "provider")
        stance = str(row.get("stance") or "silent")
        rows.append(
            f"""
          <article class="workspace-provider-card { _html(stance) }">
            <span class="provider-dot"></span>
            <div>
              <strong>{_html(_workspace_provider_label(provider_id))}</strong>
              <small>{_html(_workspace_provider_subtitle(provider_id))}</small>
            </div>
            <b>{_html(stance)}</b>
          </article>
        """
        )
    return f'<div class="workspace-provider-grid">{"".join(rows)}</div>'


def _workspace_chip_list(items: list[str], *, limit: int = 6, empty: str = "none") -> str:
    values = [str(item) for item in items if str(item).strip()]
    if not values:
        return f"<p>{_html(empty)}</p>"
    chips = "".join(f'<span class="workspace-chip">{_html(item)}</span>' for item in values[:limit])
    if len(values) > limit:
        chips += f'<span class="workspace-chip muted">+ {len(values) - limit} more</span>'
    return f'<div class="workspace-chip-list">{chips}</div>'


def _workspace_target_detail_html(target: dict[str, Any], *, index: int) -> str:
    unit = _target_unit_label(target)
    score = _target_score_value(target)
    target_class = _target_class_label(target)
    claimed, silent, total = _workspace_provider_counts(target)
    provider_error_count = _workspace_provider_error_count(target)
    total = max(total, 1)
    agreement = target.get("agreement") if isinstance(target.get("agreement"), dict) else {}
    convergence_score = _target_score_value({"review_priority_score": agreement.get("convergence_score")})
    explanation = target.get("target_explanation") if isinstance(target.get("target_explanation"), dict) else {}
    suspected_issue = str(target.get("suspected_issue") or explanation.get("suspected_issue") or target.get("claim") or unit)
    operational_mechanism = str(
        target.get("operational_mechanism")
        or explanation.get("operational_mechanism")
        or "Operational mechanism was not supplied by the provider output."
    )
    why_it_matters = str(
        target.get("why_it_matters")
        or explanation.get("why_it_matters")
        or "Outcome impact is not proven by this target alone."
    )
    evidence_refs = [str(item) for item in target.get("evidence_refs") or [] if str(item).strip()]
    counter_items = _string_items(target.get("counter_evidence_summary") or explanation.get("counter_evidence_summary"))
    caveats = [str(item) for item in target.get("caveats") or [] if str(item).strip()]
    missing = [str(item) for item in target.get("missing_evidence") or [] if str(item).strip()]
    promotion = target.get("promotion") if isinstance(target.get("promotion"), dict) else {}
    blocked_reason = str(promotion.get("blocked_reason") or "human_review_required")
    why_not_promoted = str(
        target.get("why_not_promoted")
        or explanation.get("why_not_promoted")
        or promotion.get("explanation")
        or "Human review is required before incident promotion."
    )
    next_check = str(
        target.get("next_validation_question")
        or explanation.get("next_validation_question")
        or target.get("recommended_request_type")
        or "Review cited evidence and missing signals."
    )
    provider_cards = _workspace_provider_cards_html(target)
    counter_html = _workspace_chip_list(counter_items or caveats, limit=4, empty="No counter or caveat signal was persisted.")
    missing_html = _workspace_chip_list(missing, limit=4, empty="No missing evidence was persisted.")
    refs_html = _workspace_chip_list(evidence_refs, limit=8, empty="No cited evidence refs were persisted.")
    return f"""
      <div class="workspace-detail-inner" id="{_html(_target_anchor_id(target, index=index))}">
        <span class="workspace-sr">What this target means operationally. Provider positions. Promotion gate.</span>
        <div class="workspace-detail-head">
          <div>
            <div class="workspace-tag-row">
              <span class="pill">{_html(target_class)}</span>
              <span class="pill">subsystem {_html(unit)}</span>
            </div>
            <h3>{_html(unit)}</h3>
          </div>
          <div class="workspace-score">{score:.2f}<span>review priority</span></div>
        </div>
        <div class="workspace-convergence">
          <span>Provider convergence / {_html(str(agreement.get("verdict") or "pending").replace("_", " "))}</span>
          <b>{_html(_workspace_convergence_label(claimed=claimed, silent=silent, provider_error_count=provider_error_count, score=convergence_score))}</b>
          {_stance_bar_html(target)}
        </div>
        {provider_cards}
        <div class="workspace-three">
          <div><label>Suspected issue</label><p>{_html(suspected_issue)}</p></div>
          <div><label>Operational mechanism</label><p>{_html(operational_mechanism)}</p></div>
          <div><label>Why it matters</label><p>{_html(why_it_matters)}</p></div>
        </div>
        <div class="workspace-evidence-row">
          <div><label>Cited evidence</label>{refs_html}</div>
          <div><label>Counter / weak signals</label>{counter_html}</div>
        </div>
        <div class="workspace-gate">
          <span class="gate-mark">HG</span>
          <div>
            <label>Promotion gate</label>
            <strong>HUMAN-GATED - not promoted</strong>
            <p>{_html(why_not_promoted)}</p>
            <span class="workspace-chip">{_html(blocked_reason)}</span>
          </div>
        </div>
        <div class="workspace-next-check">
          <b>next check -></b>
          <span>{_html(next_check)}</span>
        </div>
        <div>
          <label>Top missing evidence</label>
          {missing_html}
        </div>
      </div>
    """


def _detail_review_workbench(targets: list[dict[str, Any]], target_cards: str) -> str:
    if not targets:
        return """
    <section class="section-block review-section">
      <div class="section-heading">
        <span class="eyebrow">Review targets</span>
        <h2>No persisted review targets</h2>
        <p>No review targets are persisted for this evidence.</p>
      </div>
    </section>"""
    counts = {
        "all": len(targets),
        "primary": sum(1 for target in targets if _target_group_key(target) == "primary"),
        "convergence": sum(1 for target in targets if _target_group_key(target) == "convergence"),
        "single": sum(1 for target in targets if _target_group_key(target) == "single"),
    }
    filters = "".join(
        f'<button class="filter-chip{active}" type="button" data-target-filter="{_html(key)}">{_html(label)} <strong>{count}</strong></button>'
        for key, label, count, active in (
            ("all", "All", counts["all"], " active"),
            ("primary", "Primary", counts["primary"], ""),
            ("convergence", "Convergence", counts["convergence"], ""),
            ("single", "Single-source", counts["single"], ""),
        )
    )
    queue_rows = "".join(
        _workspace_queue_item_html(target, index=index + 1)
        for index, target in enumerate(targets)
    )
    first_detail = _workspace_target_detail_html(targets[0], index=1)
    templates = "".join(
        f'<template data-target-template="{_html(_target_anchor_id(target, index=index + 1))}">'
        f'{_workspace_target_detail_html(target, index=index + 1)}</template>'
        for index, target in enumerate(targets)
    )
    return f"""
    <section class="section-block review-section" id="review-targets">
      <div class="section-heading">
        <span class="eyebrow">Review workspace / {len(targets)} targets</span>
        <h2>Every target carries its own evidence and gate.</h2>
        <p>Filter the priority queue, then open a target. Silent providers, counter-signals, and the blocking reason travel with the card - the next evidence question is always explicit.</p>
      </div>
      <div class="filter-row">{filters}</div>
      <div class="review-workspace" data-review-workspace>
        <aside class="workspace-queue" aria-label="Review target priority queue">
          <div class="workspace-queue-list">
            {queue_rows}
          </div>
        </aside>
        <article class="workspace-detail" data-workspace-detail>
          {first_detail}
        </article>
        <div class="workspace-templates" aria-hidden="true">{templates}</div>
      </div>
      <details class="detail-drawer">
        <summary>
          <span>Open expanded target cards</span>
          <small>Full persisted provider positions, evidence refs, promotion gates, and review explanations</small>
        </summary>
        <div class="target-detail-list">
          {target_cards}
        </div>
      </details>
      <script>
      (() => {{
        const workspace = document.querySelector('[data-review-workspace]');
        if (!workspace) return;
        const detail = workspace.querySelector('[data-workspace-detail]');
        const buttons = Array.from(workspace.querySelectorAll('[data-target-id]'));
        const filters = Array.from(document.querySelectorAll('[data-target-filter]'));
        const templates = new Map(Array.from(workspace.querySelectorAll('template[data-target-template]')).map((template) => [template.dataset.targetTemplate, template]));
        const selectTarget = (targetId) => {{
          const template = templates.get(targetId);
          if (!template || !detail) return;
          detail.innerHTML = template.innerHTML;
          buttons.forEach((button) => button.classList.toggle('active', button.dataset.targetId === targetId));
        }};
        buttons.forEach((button) => {{
          button.addEventListener('click', () => selectTarget(button.dataset.targetId || ''));
        }});
        filters.forEach((filter) => {{
          filter.addEventListener('click', () => {{
            const group = filter.dataset.targetFilter || 'all';
            filters.forEach((item) => item.classList.toggle('active', item === filter));
            let firstVisible = null;
            buttons.forEach((button) => {{
              const visible = group === 'all' || button.dataset.targetGroup === group;
              button.hidden = !visible;
              if (visible && !firstVisible) firstVisible = button;
            }});
            if (firstVisible) selectTarget(firstVisible.dataset.targetId || '');
          }});
        }});
      }})();
      </script>
    </section>"""


def _target_preview_card_html(target: dict[str, Any], *, index: int) -> str:
    title = str(target.get("title") or target.get("core_claim") or target.get("proposal") or f"Review target {index}")
    group = _target_group_key(target)
    score = _target_score_value(target)
    evidence_refs = target.get("evidence_refs") if isinstance(target.get("evidence_refs"), list) else []
    evidence_ref_total_count = _target_evidence_ref_total_count(target, evidence_refs)
    subsystem = str(target.get("subsystem") or target.get("canonical_review_unit") or "general")
    return f"""
        <article class="target-preview {group}">
          <label>Target {index:02d} / {_html(_target_group_label(group))}</label>
          <strong>{_html(title)}</strong>
          <p>{_html(subsystem)}</p>
          <div class="pill-row">
            <span class="pill">priority {score:.2f}</span>
            <span class="pill">{_html(_provider_position_summary(target))}</span>
            <span class="pill">{evidence_ref_total_count} refs</span>
          </div>
          {_stance_bar_html(target)}
        </article>
    """


def _target_queue_row_html(target: dict[str, Any], *, index: int) -> str:
    anchor = _target_anchor_id(target, index=index)
    title = str(target.get("title") or target.get("core_claim") or target.get("proposal") or f"Review target {index}")
    group = _target_group_key(target)
    score = _target_score_value(target)
    evidence_refs = target.get("evidence_refs") if isinstance(target.get("evidence_refs"), list) else []
    evidence_ref_total_count = _target_evidence_ref_total_count(target, evidence_refs)
    return f"""
      <a class="target-nav-card {group}" href="#{_html(anchor)}">
        <span class="target-nav-top">
          <span class="queue-index">{index:02d}</span>
          <span class="queue-class">{_html(_target_group_label(group))}</span>
        </span>
        <strong>{_html(title)}</strong>
        <span class="target-nav-meta">
          <span>{_html(str(target.get("subsystem") or target.get("canonical_review_unit") or "general"))}</span>
          <span>{score:.2f}</span>
          <span>{evidence_ref_total_count} refs</span>
        </span>
        {_stance_bar_html(target)}
      </a>
    """


def _render_precomputed_review_detail_page(evidence_sha256: str, payload: dict[str, Any]) -> str:
    summary = _precomputed_summary(payload, evidence_sha256) or {}
    finding = summary.get("finding") if isinstance(summary.get("finding"), dict) else {}
    review = summary.get("review") if isinstance(summary.get("review"), dict) else {}
    providers = summary.get("providers") if isinstance(summary.get("providers"), dict) else {}
    targets = [target for target in payload.get("targets") or [] if isinstance(target, dict)]
    graph_sha = str(summary.get("canonical_graph_sha256") or "")
    raw_policy = str(summary.get("raw_log_policy") or "unknown")
    log_count = int(summary.get("log_count") or 0)
    target_cards = "\n".join(
        _fast_detail_target_card(
            target,
            index=index + 1,
            anchor_id=_target_anchor_id(target, index=index + 1),
        )
        for index, target in enumerate(targets)
    )
    trace_panel = _precomputed_agent_trace_panel(payload)
    provider_panel = _precomputed_provider_panel(payload, providers)
    graph_summary_panel = _precomputed_review_graph_summary_panel(payload)
    analysis_context_panel = _precomputed_analysis_context_panel(payload)
    devops_loop_panel = _precomputed_devops_loop_panel(payload)
    action_links = _detail_action_links_html(evidence_sha256)
    provider_mode = _detail_provider_mode_label(payload)
    case_label = _detail_case_label(payload, review)
    summary_cells = _detail_summary_cells_html(
        payload=payload,
        review=review,
        providers=providers,
        targets=targets,
        raw_policy=raw_policy,
        log_count=log_count,
    )
    review_workbench = _detail_review_workbench(targets, target_cards)
    observation_gap = _detail_is_observation_gap(payload)
    visible_provider_panel = (
        f'<section class="section-block provider-frontier-visible">{provider_panel}</section>'
        if observation_gap and provider_panel
        else ""
    )
    supplemental_sections = _detail_supplemental_sections(
        "" if observation_gap else provider_panel,
        analysis_context_panel,
        devops_loop_panel,
    )
    finding_title = str(finding.get("title") or "No persisted finding yet")
    finding_impact = _public_finding_impact_text(
        summary, str(finding.get("impact") or "Run analysis to create a persisted review result.")
    )
    hero_title, hero_impact = _detail_hero_copy(
        payload=payload,
        review=review,
        providers=providers,
        finding_title=finding_title,
        finding_impact=finding_impact,
        log_count=log_count,
    )
    observation_badge = '<span class="eyebrow-pill">Observation Gap</span>' if observation_gap else ""
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Ops Evidence Review</title>
  <style>
    :root {{
      color-scheme: light;
      --bg: #eef2f7;
      --surface: #ffffff;
      --surface-2: #f5f8fc;
      --border: #e1e7f0;
      --ink: #0f1b2d;
      --ink-2: #51617a;
      --ink-3: #8a97ab;
      --accent: #2a6fdb;
      --accent-strong: #1e5bc0;
      --accent-soft: #e7f0fc;
      --claimed: #12836b;
      --silent: #a2aebf;
      --amber: #b26a00;
      --amber-soft: #f8ecd6;
      --danger: #b42318;
      --shadow: 0 1px 2px rgba(16,27,45,.05), 0 18px 50px -22px rgba(16,27,45,.28);
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      background: var(--bg);
      color: var(--ink);
      font-family: "IBM Plex Sans", Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      letter-spacing: 0;
    }}
    .page {{
      max-width: 1180px;
      margin: 0 auto;
      padding: 0 34px 72px;
    }}
    .topbar {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
      padding: 20px 0;
      border-bottom: 1px solid var(--border);
    }}
    .brand-row, .status-row, .actions {{
      display: flex;
      align-items: center;
      gap: 10px;
      flex-wrap: wrap;
    }}
    .mark {{
      width: 32px;
      height: 32px;
      border-radius: 8px;
      background: var(--accent);
      display: inline-flex;
      align-items: center;
      justify-content: center;
      color: #fff;
      font-family: "IBM Plex Mono", ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      font-weight: 700;
      font-size: 13px;
      flex: none;
      text-decoration: none;
    }}
    .breadcrumb {{
      color: var(--ink-3);
      font-size: 13px;
    }}
    .breadcrumb a {{
      color: inherit;
      text-decoration: none;
    }}
    .breadcrumb a:hover, .breadcrumb a:focus-visible {{
      color: var(--ink);
    }}
    .breadcrumb strong {{ color: var(--ink); font-weight: 700; }}
    .status-chip, .evidence-chip, .filter-chip, .pill {{
      display: inline-flex;
      align-items: center;
      gap: 7px;
      border: 1px solid var(--border);
      border-radius: 999px;
      background: var(--surface);
      color: var(--ink-2);
      font-size: 12px;
      font-weight: 700;
      padding: 6px 10px;
      min-width: 0;
    }}
    .status-dot {{
      width: 7px;
      height: 7px;
      border-radius: 50%;
      background: var(--claimed);
      flex: none;
    }}
    .evidence-chip, code {{
      font-family: "IBM Plex Mono", ui-monospace, SFMono-Regular, Menlo, Consolas, "Liberation Mono", monospace;
      overflow-wrap: anywhere;
    }}
    main, .section-block {{
      display: grid;
      gap: 18px;
    }}
    h1, h2, p {{ margin: 0; }}
    h1 {{
      font-size: 38px;
      line-height: 1.12;
      font-weight: 750;
      max-width: 900px;
      overflow-wrap: anywhere;
    }}
    h2 {{ font-size: 26px; line-height: 1.22; font-weight: 750; overflow-wrap: anywhere; }}
    h3 {{ margin: 0; font-size: 15px; line-height: 1.3; overflow-wrap: anywhere; }}
    p {{ color: var(--ink-2); line-height: 1.55; }}
    a {{ color: inherit; }}
    .hero {{
      padding: 48px 0 30px;
      display: grid;
      gap: 18px;
    }}
    .hero p {{
      max-width: 800px;
      font-size: 16px;
      line-height: 1.6;
    }}
    .eyebrow, label {{
      display: block;
      color: var(--accent);
      font-family: "IBM Plex Mono", ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      font-size: 11px;
      font-weight: 800;
      text-transform: uppercase;
      letter-spacing: 0;
      margin-bottom: 5px;
    }}
    label {{ color: var(--ink-3); }}
    strong {{
      display: block;
      line-height: 1.25;
      overflow-wrap: anywhere;
    }}
    .actions a, .actions a.button {{
      display: inline-flex;
      align-items: center;
      border: 1px solid var(--border);
      border-radius: 8px;
      background: var(--surface);
      color: var(--ink);
      padding: 9px 12px;
      font-size: 13px;
      font-weight: 800;
      text-decoration: none;
    }}
    .actions a:hover, .actions a.button:hover {{
      border-color: var(--accent);
      color: var(--accent-strong);
    }}
    .stat-grid {{
      display: grid;
      grid-template-columns: repeat(6, minmax(0, 1fr));
      gap: 1px;
      background: var(--border);
      border: 1px solid var(--border);
      border-radius: 8px;
      overflow: hidden;
      margin-top: 8px;
    }}
    .stat-cell {{
      background: var(--surface);
      padding: 18px;
      min-width: 0;
    }}
    .stat-cell strong {{
      font-size: 21px;
      font-weight: 800;
    }}
    .stat-cell span, .stat-cell small {{
      display: block;
      color: var(--ink-3);
      font-size: 12px;
      line-height: 1.35;
      margin-top: 6px;
    }}
    .stat-cell small {{ color: var(--ink-2); }}
    .section-block {{
      padding-top: 46px;
    }}
    .section-heading {{
      display: grid;
      gap: 6px;
      max-width: 780px;
    }}
    .panel {{
      display: grid;
      gap: 12px;
      padding: 20px;
      border: 1px solid var(--border);
      border-radius: 8px;
      background: var(--surface);
      box-shadow: var(--shadow);
      min-width: 0;
    }}
    .panel.secondary {{ background: var(--surface); }}
    .metrics, .trace-grid, .provider-grid, .graph-summary-grid, .target-preview-grid {{
      display: grid;
      gap: 12px;
    }}
    .metrics {{ grid-template-columns: repeat(5, minmax(0, 1fr)); }}
    .trace-grid {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
    .provider-grid {{ grid-template-columns: repeat(5, minmax(0, 1fr)); }}
    .graph-summary-grid {{ grid-template-columns: repeat(4, minmax(0, 1fr)); }}
    .target-preview-grid {{ grid-template-columns: repeat(3, minmax(0, 1fr)); }}
    .metric, .trace-step, .provider-row, .graph-cell, .target-preview {{
      border: 1px solid var(--border);
      border-radius: 6px;
      background: var(--surface-2);
      padding: 12px;
      min-width: 0;
    }}
    .metric strong, .graph-cell strong, .provider-row strong, .trace-step strong {{
      font-size: 18px;
      font-weight: 800;
    }}
    .filter-row {{ display: flex; flex-wrap: wrap; gap: 6px; }}
    .filter-chip {{
      padding: 5px 8px;
      font: inherit;
      font-size: 11px;
      cursor: pointer;
    }}
    .filter-chip.active {{
      background: var(--accent);
      border-color: var(--accent);
      color: #fff;
    }}
    .filter-chip strong {{ display: inline; font-size: 11px; }}
    .review-section {{
      width: auto;
      max-width: none;
      margin-left: calc(-1 * clamp(0px, (100vw - 1180px) / 2, 180px));
      margin-right: calc(-1 * clamp(0px, (100vw - 1180px) / 2, 180px));
    }}
    .review-workspace {{
      display: grid;
      grid-template-columns: minmax(340px, .8fr) minmax(620px, 1.2fr);
      gap: 28px;
      align-items: start;
      min-width: 0;
    }}
    .workspace-queue {{
      border: 1px solid var(--border);
      border-radius: 12px;
      background: rgba(255,255,255,.55);
      box-shadow: var(--shadow);
      padding: 14px;
      max-height: 700px;
      overflow: auto;
      min-width: 0;
    }}
    .workspace-queue-list {{
      display: grid;
      gap: 12px;
    }}
    .workspace-queue-item {{
      display: grid;
      gap: 10px;
      width: 100%;
      padding: 15px 16px;
      border: 1px solid var(--border);
      border-radius: 10px;
      background: var(--surface);
      color: var(--ink);
      font: inherit;
      text-align: left;
      cursor: pointer;
      min-width: 0;
    }}
    .workspace-queue-item[hidden] {{
      display: none;
    }}
    .workspace-queue-item:hover, .workspace-queue-item.active {{
      border-color: var(--accent);
      box-shadow: inset 3px 0 0 var(--accent);
    }}
    .queue-title-row, .queue-meta-row, .workspace-detail-head, .workspace-convergence {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      min-width: 0;
    }}
    .queue-title-row strong {{
      font-family: "IBM Plex Mono", ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      font-size: 14px;
    }}
    .queue-title-row b {{
      font-size: 16px;
      flex: none;
    }}
    .queue-meta-row {{
      color: var(--ink-2);
      font-size: 12px;
    }}
    .workspace-progress {{
      display: block;
      height: 7px;
      border-radius: 999px;
      background: var(--silent);
      overflow: hidden;
    }}
    .workspace-progress span {{
      display: block;
      height: 100%;
      border-radius: inherit;
      background: var(--accent);
    }}
    .workspace-detail {{
      border: 1px solid var(--border);
      border-radius: 12px;
      background: var(--surface);
      box-shadow: var(--shadow);
      padding: 32px;
      min-width: 0;
    }}
    .workspace-detail-inner {{
      display: grid;
      gap: 24px;
      min-width: 0;
    }}
    .workspace-detail h3 {{
      margin: 12px 0 0;
      font-family: "IBM Plex Mono", ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      font-size: 28px;
      line-height: 1.2;
      overflow-wrap: anywhere;
    }}
    .workspace-tag-row {{
      display: flex;
      gap: 8px;
      flex-wrap: wrap;
    }}
    .workspace-score {{
      color: var(--ink);
      font-size: 34px;
      font-weight: 800;
      text-align: right;
      flex: none;
    }}
    .workspace-score span {{
      display: block;
      color: var(--ink-3);
      font-size: 11px;
      font-weight: 800;
      margin-top: 3px;
    }}
    .workspace-convergence {{
      align-items: end;
      border-bottom: 1px solid var(--border);
      padding-bottom: 14px;
      color: var(--ink-2);
      font-size: 13px;
      font-weight: 800;
      flex-wrap: wrap;
    }}
    .workspace-convergence .stance-meter {{
      flex-basis: 100%;
      height: 14px;
      margin-top: 6px;
    }}
    .workspace-provider-grid {{
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 10px;
    }}
    .workspace-provider-card {{
      display: grid;
      grid-template-columns: auto minmax(0, 1fr) auto;
      gap: 10px;
      align-items: center;
      border: 1px solid var(--border);
      border-radius: 8px;
      background: var(--surface-2);
      padding: 12px;
      min-width: 0;
    }}
    .workspace-provider-card small {{
      display: block;
      color: var(--ink-3);
      font-family: "IBM Plex Mono", ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      font-size: 11px;
      margin-top: 2px;
    }}
    .workspace-provider-card b {{
      color: var(--ink-3);
      font-size: 12px;
    }}
    .workspace-provider-card.claimed b {{ color: var(--claimed); }}
    .workspace-provider-card.provider_error b {{ color: var(--danger); }}
    .provider-dot {{
      width: 10px;
      height: 10px;
      border-radius: 50%;
      background: var(--silent);
    }}
    .workspace-provider-card.claimed .provider-dot {{ background: var(--claimed); }}
    .workspace-provider-card.contradicted .provider-dot {{ background: var(--danger); }}
    .workspace-provider-card.provider_error .provider-dot {{ background: var(--danger); }}
    .workspace-three, .workspace-evidence-row, .workspace-chip-list {{
      display: grid;
      gap: 14px;
      min-width: 0;
    }}
    .workspace-three {{
      grid-template-columns: repeat(3, minmax(0, 1fr));
      border-top: 1px solid var(--border);
      border-bottom: 1px solid var(--border);
      padding: 18px 0;
    }}
    .workspace-evidence-row {{
      grid-template-columns: minmax(0, .9fr) minmax(0, 1.1fr);
      border-bottom: 1px solid var(--border);
      padding-bottom: 18px;
    }}
    .workspace-chip-list {{
      grid-template-columns: repeat(2, minmax(0, 1fr));
    }}
    .workspace-chip {{
      display: inline-flex;
      align-items: center;
      min-width: 0;
      border: 1px solid var(--border);
      border-radius: 8px;
      background: var(--surface-2);
      color: var(--ink-2);
      font-size: 12px;
      font-weight: 800;
      line-height: 1.3;
      padding: 8px 10px;
      overflow-wrap: anywhere;
    }}
    .workspace-chip.muted {{ color: var(--ink-3); }}
    .workspace-gate {{
      display: flex;
      gap: 14px;
      align-items: start;
      border: 1px solid rgba(178,106,0,.2);
      border-radius: 10px;
      background: var(--amber-soft);
      padding: 18px;
    }}
    .workspace-gate strong {{
      color: var(--amber);
      font-size: 14px;
    }}
    .workspace-next-check {{
      display: flex;
      gap: 10px;
      align-items: baseline;
      border-top: 1px solid rgba(178,106,0,.22);
      padding-top: 14px;
    }}
    .workspace-next-check b {{
      color: var(--accent);
      font-family: "IBM Plex Mono", ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      flex: none;
    }}
    .workspace-templates, .workspace-sr {{
      position: absolute;
      width: 1px;
      height: 1px;
      overflow: hidden;
      clip: rect(0 0 0 0);
      white-space: nowrap;
    }}
    .target-nav {{ display: grid; gap: 8px; }}
    .target-nav-card {{
      display: grid;
      gap: 8px;
      padding: 12px;
      border: 1px solid var(--border);
      border-radius: 8px;
      background: var(--surface);
      text-decoration: none;
      color: var(--ink);
    }}
    .target-nav-card:hover {{
      border-color: var(--accent);
      background: var(--accent-soft);
    }}
    .target-nav-card.primary {{ box-shadow: inset 3px 0 0 var(--amber); }}
    .target-nav-card.convergence {{ box-shadow: inset 3px 0 0 var(--accent); }}
    .target-nav-card.single {{ box-shadow: inset 3px 0 0 var(--silent); }}
    .target-nav-top, .target-nav-meta {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 8px;
      color: var(--ink-3);
      font-size: 11px;
    }}
    .queue-index {{
      font-family: "IBM Plex Mono", ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      color: var(--accent);
      font-weight: 800;
    }}
    .queue-class {{ color: var(--ink-2); font-weight: 800; }}
    .target-nav-card strong {{ font-size: 13px; line-height: 1.35; }}
    .target-detail-list {{ display: grid; gap: 16px; min-width: 0; }}
    .section-note {{ font-size: 13px; }}
    .review-arbitration-grid {{
      display: grid;
      grid-template-columns: minmax(0, 1.1fr) minmax(360px, .9fr);
      gap: 22px;
      align-items: stretch;
    }}
    .distribution-card {{
      display: grid;
      gap: 18px;
      padding: 24px;
      border: 1px solid var(--border);
      border-radius: 8px;
      background: var(--surface);
      box-shadow: var(--shadow);
      min-width: 0;
    }}
    .distribution-title {{
      font-size: 14px;
      font-weight: 800;
    }}
    .distribution-bar {{
      display: flex;
      height: 16px;
      border-radius: 999px;
      overflow: hidden;
      background: var(--border);
    }}
    .bar-converged {{ background: var(--accent); }}
    .bar-single {{ background: var(--silent); }}
    .bar-context {{ background: var(--amber); }}
    .legend-row {{
      display: flex;
      gap: 18px;
      flex-wrap: wrap;
      color: var(--ink-2);
      font-size: 13px;
      font-weight: 700;
    }}
    .legend-dot {{
      width: 10px;
      height: 10px;
      border-radius: 3px;
      display: inline-block;
      margin-right: 7px;
    }}
    .metric-matrix {{
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      border: 1px solid var(--border);
      border-radius: 8px;
      overflow: hidden;
      background: var(--border);
      box-shadow: var(--shadow);
      gap: 1px;
    }}
    .matrix-cell {{
      background: var(--surface);
      padding: 20px;
      min-width: 0;
    }}
    .matrix-cell strong {{
      font-size: 23px;
      font-weight: 800;
    }}
    .matrix-cell span {{
      display: block;
      margin-top: 7px;
      color: var(--ink-3);
      font-size: 12px;
    }}
    .detail-drawer, .supplemental-details {{
      border: 1px solid var(--border);
      border-radius: 8px;
      background: var(--surface);
      box-shadow: var(--shadow);
      overflow: hidden;
      min-width: 0;
    }}
    .detail-drawer summary, .supplemental-details summary {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
      padding: 16px 18px;
      cursor: pointer;
      font-weight: 800;
    }}
    .detail-drawer summary small, .supplemental-details summary small {{
      color: var(--ink-3);
      font-size: 12px;
      font-weight: 700;
      text-align: right;
    }}
    .detail-drawer[open] .target-detail-list, .supplemental-details[open] .supplemental-grid {{
      border-top: 1px solid var(--border);
      padding: 18px;
    }}
    .inline-details {{
      margin-top: 8px;
      color: var(--ink-2);
      font-size: 12px;
    }}
    .inline-details summary {{
      cursor: pointer;
      font-weight: 800;
      color: var(--amber);
    }}
    .inline-details p {{
      margin-top: 7px;
      font-size: 12px;
      line-height: 1.45;
    }}
    .supplemental-grid {{
      display: grid;
      gap: 18px;
    }}
    .target-preview.primary {{ box-shadow: inset 3px 0 0 var(--amber); }}
    .target-preview.convergence {{ box-shadow: inset 3px 0 0 var(--accent); }}
    .target-preview.single {{ box-shadow: inset 3px 0 0 var(--silent); }}
    .target {{
      display: grid;
      gap: 16px;
      border: 1px solid var(--border);
      border-radius: 8px;
      background: var(--surface);
      box-shadow: var(--shadow);
      padding: 20px;
      min-width: 0;
      scroll-margin-top: 20px;
    }}
    .target:target {{
      border-color: var(--accent);
      box-shadow: inset 4px 0 0 var(--accent), var(--shadow);
    }}
    .target-head {{
      display: grid;
      grid-template-columns: minmax(0, 1fr) auto;
      gap: 16px;
      align-items: start;
    }}
    .score {{
      min-width: 96px;
      text-align: right;
      font-size: 26px;
      font-weight: 800;
      color: var(--accent);
    }}
    .score span, .score small {{
      display: block;
      color: var(--ink-3);
      font-size: 11px;
      font-weight: 800;
      text-transform: uppercase;
      margin-top: 2px;
    }}
    .score small {{
      max-width: 180px;
      color: var(--ink-2);
      font-weight: 600;
      text-transform: none;
      line-height: 1.35;
    }}
    .pill-row {{ display: flex; flex-wrap: wrap; gap: 6px; margin-top: 10px; }}
    .target-class-primary_candidate, .target-class-primary-candidate {{
      color: var(--amber);
      background: var(--amber-soft);
      border-color: rgba(178,106,0,.2);
    }}
    .target-grid {{
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 14px;
    }}
    .field {{
      border-top: 1px solid var(--border);
      padding-top: 10px;
      min-width: 0;
    }}
    .field.full {{ grid-column: 1 / -1; }}
    .field *, .target *, .queue-panel *, .panel * {{
      min-width: 0;
      overflow-wrap: anywhere;
    }}
    .field ul, .target-explanation ul {{
      margin: 8px 0 0;
      padding-left: 18px;
      color: var(--ink-2);
      line-height: 1.45;
    }}
    .target-explanation {{
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 12px 16px;
    }}
    .target-explanation label, .target-explanation p, .target-explanation ul {{ margin: 0; }}
    .position-list {{ display: grid; gap: 6px; }}
    .position-row {{
      display: grid;
      grid-template-columns: minmax(150px, 0.7fr) 96px minmax(0, 1.4fr) 104px;
      gap: 8px;
      align-items: start;
      border: 1px solid var(--border);
      border-radius: 6px;
      background: var(--surface-2);
      padding: 8px;
    }}
    .position-row p {{ color: var(--ink); }}
    .stance {{
      display: inline-flex;
      width: max-content;
      border: 1px solid var(--border);
      border-radius: 999px;
      padding: 3px 7px;
      background: var(--accent-soft);
      color: var(--accent-strong);
      font-size: 12px;
      font-weight: 800;
    }}
    .stance-meter {{
      display: flex;
      height: 8px;
      overflow: hidden;
      border-radius: 999px;
      background: var(--border);
    }}
    .stance-fill.claimed {{ background: var(--claimed); }}
    .stance-fill.contradicted {{ background: var(--danger); }}
    .stance-fill.silent {{ background: var(--silent); }}
    .stance-fill.provider_error {{ background: var(--danger); }}
    .human-gate {{
      display: flex;
      gap: 12px;
      align-items: start;
      padding: 14px;
      border: 1px solid rgba(178,106,0,.2);
      border-radius: 8px;
      background: var(--amber-soft);
      margin-top: 4px;
    }}
    .human-gate > div {{ min-width: 0; }}
    .gate-mark {{
      width: 34px;
      height: 34px;
      border-radius: 8px;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      background: var(--surface);
      border: 1px solid var(--border);
      color: var(--amber);
      font-family: "IBM Plex Mono", ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      font-weight: 800;
      flex: none;
    }}
    .human-gate strong {{ color: var(--amber); font-size: 13px; }}
    .footer {{
      display: flex;
      justify-content: space-between;
      gap: 20px;
      margin-top: 48px;
      padding-top: 20px;
      border-top: 1px solid var(--border);
      color: var(--ink-2);
      font-size: 12px;
    }}
    @media (max-width: 900px) {{
      .stat-grid, .metrics, .trace-grid, .provider-grid, .graph-summary-grid, .target-preview-grid {{
        grid-template-columns: repeat(2, minmax(0, 1fr));
      }}
      .review-section {{
        margin-left: 0;
        margin-right: 0;
      }}
      .review-arbitration-grid {{ grid-template-columns: minmax(0, 1fr); }}
      .review-workspace, .workspace-three, .workspace-evidence-row {{
        grid-template-columns: minmax(0, 1fr);
      }}
      .workspace-queue {{ max-height: 520px; }}
      .review-workbench {{ grid-template-columns: 1fr; }}
      .queue-panel {{ position: static; max-height: none; }}
    }}
    @media (max-width: 760px) {{
      .page {{ padding: 0 14px 48px; }}
      .topbar {{ display: grid; align-items: start; }}
      h1 {{ font-size: 30px; }}
      h2 {{ font-size: 22px; }}
      .stat-grid, .metrics, .target-grid, .target-head, .trace-grid, .provider-grid, .graph-summary-grid, .position-row, .target-preview-grid, .target-explanation, .metric-matrix, .workspace-provider-grid, .workspace-chip-list {{
        grid-template-columns: minmax(0, 1fr);
      }}
      .workspace-detail {{ padding: 18px; }}
      .workspace-detail-head, .workspace-convergence, .workspace-next-check {{ display: grid; }}
      .workspace-score {{ text-align: left; }}
      .detail-drawer summary, .supplemental-details summary {{ display: grid; }}
      .detail-drawer summary small, .supplemental-details summary small {{ text-align: left; }}
      .score {{ text-align: left; }}
    .footer {{ display: grid; }}
    }}
    :root {{
      --bg: #f4f2ec;
      --bg-2: #faf8f2;
      --surface: #fffdf8;
      --surface-2: #fbf8f1;
      --border: #e4dccb;
      --border-strong: #d3c8b3;
      --ink: #181611;
      --ink-2: #514b40;
      --ink-3: #8b8375;
      --accent: #3f63a8;
      --accent-strong: #2f55a0;
      --accent-soft: #eef2f9;
      --claimed: #208a61;
      --silent: #b8c0cb;
      --amber: #b17a40;
      --amber-soft: #f4ead8;
      --danger: #b42318;
      --green-soft: #ecf6f1;
      --shadow: 0 20px 58px -44px rgba(60, 50, 30, .36);
      --mono: ui-monospace, SFMono-Regular, Menlo, Consolas, "Liberation Mono", monospace;
      --serif: Georgia, "Times New Roman", serif;
    }}
    body {{
      background: var(--bg);
      color: var(--ink);
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }}
    .page {{
      max-width: none;
      padding: 0 0 72px;
    }}
    .topbar {{
      position: sticky;
      top: 0;
      z-index: 20;
      width: auto;
      margin: 0;
      padding: 16px max(24px, calc((100vw - 1220px) / 2));
      border-bottom: 1px solid var(--border);
      background: rgba(250, 248, 242, .94);
      backdrop-filter: blur(8px);
    }}
    main {{
      width: min(calc(100% - 48px), 1220px);
      margin: 0 auto;
      gap: 0;
    }}
    .mark {{
      width: 26px;
      height: 26px;
      border-radius: 7px;
      background: var(--ink);
      color: var(--bg);
      font: 800 11px/1 var(--mono);
      text-decoration: none;
    }}
    .breadcrumb {{
      display: flex;
      gap: 11px;
      align-items: center;
      color: var(--ink-3);
      font-size: 13px;
    }}
    .breadcrumb a {{
      color: inherit;
      text-decoration: none;
    }}
    .breadcrumb a:hover, .breadcrumb a:focus-visible {{
      color: var(--ink);
    }}
    .breadcrumb strong {{
      display: inline;
      color: var(--ink);
      font-weight: 800;
    }}
    .status-row {{
      color: var(--ink-3);
      font-size: 13px;
    }}
    .status-chip, .evidence-chip, .filter-chip, .pill {{
      border-color: var(--border);
      background: rgba(255, 253, 248, .78);
      color: var(--ink-2);
      border-radius: 999px;
    }}
    .status-chip.live {{
      border-color: #bfe0cd;
      background: var(--green-soft);
      color: var(--claimed);
      font: 800 11.5px/1 var(--mono);
    }}
    .top-link {{
      color: var(--ink-2);
      text-decoration: none;
      font-size: 13px;
    }}
    .top-link:hover {{ color: var(--ink); }}
    .hero {{
      padding: 78px 0 40px;
      gap: 24px;
    }}
    .eyebrow, label {{
      color: var(--amber);
      font-family: var(--mono);
      font-size: 11px;
      letter-spacing: .16em;
      line-height: 1.2;
    }}
    .hero .eyebrow {{
      display: flex;
      align-items: center;
      gap: 10px;
      flex-wrap: wrap;
      margin-bottom: 0;
    }}
    .eyebrow-pill {{
      display: inline-flex;
      align-items: center;
      border: 1px solid #e0d8c7;
      border-radius: 999px;
      background: #efe9db;
      color: #8a857a;
      padding: 4px 10px;
      font-size: 10px;
      letter-spacing: .08em;
      white-space: nowrap;
    }}
    h1 {{
      max-width: 920px;
      color: var(--ink);
      font-family: var(--serif);
      font-size: clamp(38px, 3.7vw, 48px);
      font-weight: 500;
      line-height: 1.08;
      letter-spacing: 0;
      overflow-wrap: normal;
    }}
    h2 {{
      color: var(--ink);
      font-family: var(--serif);
      font-size: clamp(28px, 3vw, 34px);
      font-weight: 500;
      line-height: 1.08;
      letter-spacing: 0;
    }}
    h3 {{
      color: var(--ink);
      letter-spacing: 0;
    }}
    p {{
      color: var(--ink-2);
      line-height: 1.58;
    }}
    .hero p {{
      max-width: 780px;
      color: var(--ink-2);
      font-size: 16.5px;
      line-height: 1.62;
    }}
    .actions {{
      gap: 12px;
      margin-top: 6px;
    }}
    .actions a, .actions a.button {{
      border-color: var(--border-strong);
      border-radius: 8px;
      background: transparent;
      color: var(--ink);
      padding: 11px 18px;
      font-size: 14px;
    }}
    .actions a:hover, .actions a.button:hover {{
      border-color: var(--ink);
      color: var(--ink);
      background: #efe9db;
    }}
    .stat-grid {{
      grid-template-columns: repeat(6, minmax(0, 1fr));
      gap: 1px;
      margin-top: 16px;
      border: 1px solid var(--border);
      border-radius: 10px;
      background: var(--border);
      box-shadow: none;
    }}
    .stat-cell {{
      background: rgba(255, 253, 248, .78);
      padding: 19px 18px;
    }}
    .stat-cell.safe {{
      background: var(--green-soft);
    }}
    .stat-cell strong {{
      color: var(--ink);
      font-size: 23px;
      font-weight: 900;
      line-height: 1;
    }}
    .stat-cell.safe strong {{
      color: var(--claimed);
    }}
    .stat-cell span, .stat-cell small {{
      color: var(--ink-3);
      font-size: 11px;
    }}
    .section-block {{
      padding: 52px 0;
      border-top: 1px solid var(--border);
      gap: 24px;
    }}
    .section-heading {{
      max-width: 780px;
      gap: 9px;
    }}
    .section-heading p, .section-note {{
      color: var(--ink-3);
      font-size: 14px;
    }}
    .panel, .distribution-card, .detail-drawer, .supplemental-details, .workspace-queue, .workspace-detail, .metric-matrix {{
      border-color: var(--border);
      border-radius: 12px;
      background: rgba(255, 253, 248, .76);
      box-shadow: var(--shadow);
    }}
    .panel.secondary, .metric, .trace-step, .provider-row, .graph-cell, .target-preview, .workspace-provider-card, .workspace-chip, .target, .target-nav-card, .workspace-queue-item {{
      border-color: var(--border);
      background: var(--surface);
    }}
    .review-section {{
      margin-left: 0;
      margin-right: 0;
    }}
    .review-workspace {{
      grid-template-columns: minmax(320px, .76fr) minmax(0, 1.24fr);
      gap: 22px;
    }}
    .workspace-queue {{
      background: rgba(255, 253, 248, .56);
      max-height: 650px;
    }}
    .workspace-queue-item {{
      border-radius: 10px;
      box-shadow: none;
    }}
    .workspace-queue-item:hover, .workspace-queue-item.active {{
      border-color: #c8d5ee;
      box-shadow: inset 3px 0 0 var(--accent);
    }}
    .workspace-progress span {{
      background: var(--accent);
    }}
    .workspace-detail {{
      padding: 28px;
      background: var(--surface);
    }}
    .workspace-detail h3 {{
      color: var(--ink);
      font-size: 27px;
    }}
    .workspace-score {{
      color: var(--ink);
      font-size: 32px;
    }}
    .workspace-gate, .human-gate {{
      border-color: #ead4b4;
      background: var(--amber-soft);
    }}
    .gate-mark {{
      background: var(--surface);
      color: var(--amber);
    }}
    .review-arbitration-grid {{
      grid-template-columns: repeat(4, minmax(0, 1fr)) minmax(340px, 1.15fr);
      gap: 16px;
    }}
    .arbitration-stat, .arbitration-gate {{
      min-width: 0;
      border: 1px solid var(--border);
      border-radius: 10px;
      background: rgba(255, 253, 248, .78);
      padding: 24px 18px;
    }}
    .arbitration-stat strong {{
      color: var(--ink);
      font-size: 28px;
      font-weight: 900;
    }}
    .arbitration-stat.primary strong {{
      color: var(--accent);
    }}
    .arbitration-stat.safe strong {{
      color: var(--claimed);
    }}
    .arbitration-stat span {{
      display: block;
      margin-top: 8px;
      color: var(--ink-3);
      font-size: 12px;
    }}
    .arbitration-gate {{
      display: flex;
      gap: 14px;
      align-items: flex-start;
      background: var(--accent-soft);
      border-color: #cbd8ef;
    }}
    .arbitration-gate strong {{
      color: var(--ink);
      font-size: 14px;
    }}
    .arbitration-gate p {{
      color: #4f5f7b;
      font-size: 13px;
    }}
    .trace-grid {{
      grid-template-columns: repeat(2, minmax(0, 1fr));
    }}
    .trace-step {{
      border-radius: 10px;
      padding: 18px;
    }}
    .provider-grid {{
      grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
    }}
    .graph-summary-grid {{
      grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
    }}
    .footer {{
      width: min(calc(100% - 48px), 1220px);
      margin: 0 auto;
      padding-top: 22px;
      border-color: var(--border);
    }}
    @media (max-width: 1180px) {{
      .topbar {{
        padding-left: 24px;
        padding-right: 24px;
      }}
      .review-arbitration-grid {{
        grid-template-columns: repeat(2, minmax(0, 1fr));
      }}
      .arbitration-gate {{
        grid-column: 1 / -1;
      }}
    }}
    @media (max-width: 900px) {{
      main, .footer {{
        width: min(calc(100% - 32px), 1220px);
      }}
      .hero {{
        padding-top: 54px;
      }}
      .stat-grid, .review-arbitration-grid, .trace-grid {{
        grid-template-columns: repeat(2, minmax(0, 1fr));
      }}
      .review-workspace {{
        grid-template-columns: 1fr;
      }}
    }}
    @media (max-width: 760px) {{
      .topbar {{
        position: static;
        padding: 14px 16px;
      }}
      .breadcrumb, .status-row {{
        align-items: flex-start;
      }}
      h1 {{
        font-size: 31px;
        line-height: 1.08;
        overflow-wrap: anywhere;
        word-break: break-word;
      }}
      .hero p {{
        font-size: 16px;
        overflow-wrap: anywhere;
      }}
      .stat-grid, .review-arbitration-grid, .trace-grid {{
        grid-template-columns: 1fr;
      }}
      .arbitration-gate {{
        display: grid;
      }}
      .workspace-detail {{
        padding: 18px;
      }}
    }}
  </style>
</head>
<body>
  <div class="page">
    <header class="topbar">
      <div class="brand-row">
        <a class="mark" href="/" aria-label="Ops Evidence home">OE</a>
        <div class="breadcrumb">
          <span>/</span><a href="/#review-set">Reviews</a>
          <span>/</span><strong>{_html(case_label)}</strong>
        </div>
      </div>
      <div class="status-row">
        <span class="evidence-chip">evidence {_html(_short_sha(evidence_sha256))}</span>
        <a class="top-link" href="{_html(_public_repo_url())}">GitHub</a>
        <span class="status-chip live"><span class="status-dot"></span>Cloud Run live</span>
      </div>
    </header>
    <main>
      <section class="hero">
        <span class="eyebrow">Canonical Review Graph / {_html(provider_mode)} {observation_badge}</span>
        <h1>{_html(hero_title)}</h1>
        <p>{_html(hero_impact)}</p>
        <div class="actions">
          {action_links}
        </div>
        <div class="stat-grid">{summary_cells}</div>
      </section>
      {graph_summary_panel}
      {trace_panel}
      {review_workbench}
      {visible_provider_panel}
      {supplemental_sections}
      <footer class="footer">
        <span>Ops Evidence Synthesis / read-only delivery / Yuki Murata</span>
        <span><code>canonical_review_graph.v1</code> / {_html(_short_sha(graph_sha) if graph_sha else "precomputed")}</span>
      </footer>
    </main>
  </div>
</body>
</html>"""


def _detail_supplemental_sections(*sections: str) -> str:
    body = "\n".join(section for section in sections if section.strip())
    if not body:
        return ""
    return f"""
    <section class="section-block">
      <details class="supplemental-details">
        <summary>
          <span>Evidence context, provider frontier, and improvement loop</span>
          <small>Expanded only when the reviewer needs audit detail</small>
        </summary>
        <div class="supplemental-grid">
          {body}
        </div>
      </details>
    </section>"""


def _precomputed_agent_trace_panel(payload: dict[str, Any]) -> str:
    steps = [step for step in payload.get("agent_trace") or [] if isinstance(step, dict)]
    if not steps:
        return ""
    visible_steps = steps[:6]
    overflow = max(0, len(steps) - len(visible_steps))
    overflow_note = (
        f"<p class=\"section-note\">{_count_noun(overflow, 'additional trace step')} are retained in the API view.</p>"
        if overflow
        else ""
    )
    rows = "".join(
        f"""
        <article class="trace-step">
          <label>Step {index}</label>
          <strong>{_html(str(step.get("title") or step.get("step") or ""))}</strong>
          <p>{_html(str(step.get("summary") or ""))}</p>
          {_trace_output_facts_html(step)}
          <div class="pill-row">
            <span class="pill">{_html(str(step.get("status") or "completed"))}</span>
            <span class="pill">{_html(str(step.get("artifact") or step.get("tool") or ""))}</span>
          </div>
        </article>
        """
        for index, step in enumerate(visible_steps, start=1)
    )
    return f"""
    <section class="section-block trace-section">
      <div class="section-heading">
        <span class="eyebrow">Agent Trace · ADK tool contract</span>
        <h2>A guarded autonomous investigation loop.</h2>
        <p>Deterministic evidence tools are orchestrated around Gemini-on-Vertex. Final causal judgement and destructive actions stay behind explicit human gates.</p>
      </div>
      <div class="trace-grid">{rows}</div>
      {overflow_note}
    </section>"""


def _trace_output_facts_html(step: dict[str, Any]) -> str:
    output = step.get("output") if isinstance(step.get("output"), dict) else {}
    if not output:
        return ""
    facts: list[str] = []
    if output.get("profile_id"):
        facts.append(f"profile={output.get('profile_id')}")
    for key, label in (
        ("component_count", "components"),
        ("metric_semantics_count", "metric semantics"),
        ("collector_mapping_count", "collectors"),
        ("provider_count", "providers"),
        ("schema_valid_provider_count", "schema-valid"),
        ("full_evidence_items", "Evidence Items"),
        ("analyzed_evidence_items", "analyzed"),
        ("chunk_count", "chunks"),
        ("chunk_manifest_count", "manifests"),
        ("unassigned_evidence_items", "unassigned"),
        ("failed_chunk_count", "failed chunks"),
        ("directly_cited_evidence_ref_count", "directly cited refs"),
        ("chunk_tracked_evidence_ref_total_count", "chunk-tracked refs"),
        ("missing_evidence_count", "missing evidence"),
    ):
        if output.get(key) not in (None, "", 0):
            facts.append(f"{label}={output.get(key)}")
    request_types = output.get("request_types") if isinstance(output.get("request_types"), list) else []
    if request_types:
        facts.append("requests=" + ", ".join(str(item) for item in request_types[:4]))
    blocked = output.get("blocked_reasons") if isinstance(output.get("blocked_reasons"), list) else []
    if blocked:
        facts.append("blocked=" + "; ".join(str(item) for item in blocked[:3]))
    if not facts:
        return ""
    return f"<p>{_html(' / '.join(facts))}</p>"


def _precomputed_provider_panel(payload: dict[str, Any], providers_summary: dict[str, Any]) -> str:
    providers = [row for row in payload.get("provider_statuses") or [] if isinstance(row, dict)]
    if not providers:
        return ""
    generation = payload.get("generation") if isinstance(payload.get("generation"), dict) else {}
    provider_mode = str(generation.get("provider_mode") or "unknown")
    source_note = str(generation.get("source_note") or "")
    generation_note = f" Source: {_html(source_note)}" if source_note else ""
    rows = "".join(
        f"""
        <article class="provider-row">
          <label>{_html(str(row.get("provider_id") or ""))}</label>
          <strong>{_html(str(row.get("status") or "unknown"))}</strong>
          <p>model={_html(str(row.get("model_name") or "unknown"))}</p>
          <p>schema_valid={_html(str(bool(row.get("schema_valid"))).lower())}</p>
          <p><code>{_html(str(row.get("raw_output_sha256") or "")[:12])}</code></p>
        </article>
        """
        for row in providers
    )
    gemini_note = (
        "<p>Gemini is shown as the reference/arbiter provider when present; it is not expected to claim every target. Silent positions remain visible as validation signal.</p>"
        if any("gemini" in str(row.get("provider_id") or "") for row in providers)
        else ""
    )
    return f"""
    <section class="panel">
      <label>Provider Frontier</label>
      <h2>{int(providers_summary.get("success") or 0)} successful / {int(providers_summary.get("total") or 0)} total</h2>
      <p>Served by the public read-only API from a precomputed review cache. Analysis mode: <code>{_html(provider_mode)}</code>.{generation_note}</p>
      <p>Provider disagreement is preserved as validation work, not collapsed into majority truth.</p>
      {gemini_note}
      <div class="provider-grid">{rows}</div>
    </section>"""


def _precomputed_analysis_context_panel(payload: dict[str, Any]) -> str:
    context = payload.get("analysis_context")
    if not isinstance(context, dict) or not context:
        return ""
    profile_context = payload.get("profile_context") if isinstance(payload.get("profile_context"), dict) else {}
    cells = [
        ("DB ingested logs", _human_count(_context_count(context.get("db_ingested_log_count")))),
        ("DB corpus coverage", _coverage_text(context.get("db_corpus_coverage_ratio"))),
        ("DB covered rows", _human_count(_context_count(context.get("db_corpus_covered_row_count")))),
        ("DB pattern groups", _human_count(_context_count(context.get("db_corpus_pattern_count")))),
        ("Derived Evidence Items", _human_count(_context_nested_count(context, "evidence_item_accounting", "derived_metric_or_operational_items"))),
        ("DB singleton rows", _human_count(_coverage_class_count(context, "singleton"))),
        ("DB rare rows", _human_count(_coverage_class_count(context, "rare"))),
        ("Prompt-direct DB rows", _human_count(_context_count(context.get("db_corpus_direct_prompt_row_count")))),
        ("Provider corpus coverage", _coverage_text(context.get("provider_full_corpus_coverage_ratio"))),
        ("Provider corpus items", _human_count(_context_count(context.get("provider_full_corpus_analyzed_evidence_items")))),
        ("Provider chunks", _human_count(_context_count(context.get("provider_full_corpus_chunk_count")))),
        ("Chunk manifests", _human_count(_context_count(context.get("provider_full_corpus_chunk_manifest_count")))),
        ("Unassigned items", _human_count(_context_count(context.get("provider_full_corpus_unassigned_evidence_items")))),
        ("Single-prompt projection", _human_count(_context_count(context.get("model_projection_evidence_items")))),
        ("Projected occurrences", _human_count(_context_count(context.get("model_projection_occurrence_count")))),
        ("Projection coverage", _coverage_text(context.get("model_projection_occurrence_coverage_ratio"))),
    ]
    cell_html = "".join(
        f"""
        <article class="graph-cell">
          <label>{_html(label)}</label>
          <strong>{_html(value)}</strong>
        </article>
        """
        for label, value in cells
        if _show_context_cell(label, value)
    )
    log_points = _context_points(context.get("log_observations"))
    source_points = _context_points(context.get("source_observations"))
    conclusion_points = _context_points(context.get("analysis_conclusion"))
    profile_points = _profile_context_points(profile_context)
    projection_policy = str(context.get("model_projection_policy") or "")
    projection_interpretation = str(context.get("model_projection_interpretation") or "")
    projection_notes = [
        note
        for note in (projection_policy, projection_interpretation)
        if str(note).strip()
    ]
    projection_notes.extend(_determinism_scope_points(context))
    projection_note = "".join(f"<p>{_html(note)}</p>" for note in projection_notes)
    return f"""
    <section class="panel secondary">
      <label>DB-to-model projection</label>
      <h2>Sanitized logs were persisted, bounded, and then analyzed by providers</h2>
      {projection_note}
      <div class="graph-summary-grid">{cell_html}</div>
      <div class="target-grid">
        <div class="field"><label>Log observations</label>{_points_html(log_points)}</div>
        <div class="field"><label>Source observations</label>{_points_html(source_points)}</div>
        <div class="field"><label>Profile context</label>{_points_html(profile_points)}</div>
        <div class="field full"><label>Analysis conclusion</label>{_points_html(conclusion_points)}</div>
      </div>
    </section>"""


def _context_points(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if str(item).strip()]


def _profile_context_points(profile_context: dict[str, Any]) -> list[str]:
    if not profile_context:
        return []
    points = []
    if profile_context.get("profile_id"):
        points.append(f"profile_id={profile_context.get('profile_id')}")
    if profile_context.get("generation_mode"):
        points.append(f"mode={profile_context.get('generation_mode')}")
    if profile_context.get("profile_status"):
        points.append(f"profile_status={profile_context.get('profile_status')}")
    if profile_context.get("confidence_action"):
        confidence = profile_context.get("confidence_summary") if isinstance(profile_context.get("confidence_summary"), dict) else {}
        overall = confidence.get("overall_confidence")
        confidence_text = f"confidence_action={profile_context.get('confidence_action')}"
        if overall not in (None, ""):
            confidence_text += f" (overall={overall}; {_confidence_action_explanation(str(profile_context.get('confidence_action') or ''), overall)})"
        points.append(confidence_text)
    counts = []
    for key, label in (
        ("component_count", "components"),
        ("metric_semantics_count", "metric semantics"),
        ("collector_mapping_count", "collectors"),
    ):
        if profile_context.get(key) not in (None, "", 0):
            counts.append(f"{label}={profile_context.get(key)}")
    if counts:
        points.append(", ".join(counts))
    confirmed = [str(item) for item in profile_context.get("confirmed_user_outcomes") or [] if str(item).strip()]
    provisional = [str(item) for item in profile_context.get("provisional_user_outcomes") or [] if str(item).strip()]
    if confirmed:
        points.append("confirmed_user_outcomes=" + "; ".join(confirmed[:3]))
    if provisional:
        points.append("provisional_user_outcomes_pending_approval=" + "; ".join(provisional[:3]))
    human_questions = [str(item) for item in profile_context.get("human_questions") or [] if str(item).strip()]
    if human_questions:
        points.append("human_questions=" + " / ".join(human_questions[:3]))
    profile_links = profile_context.get("profile_to_review_links")
    if isinstance(profile_links, list):
        link_points = []
        for row in profile_links[:3]:
            if not isinstance(row, dict):
                continue
            units = ", ".join(str(item) for item in row.get("review_units") or [] if str(item).strip())
            question = str(row.get("question") or "").strip()
            if question and units:
                link_points.append(f"{question} -> {units}")
        if link_points:
            points.append("profile_questions_linked_to_review_units=" + " / ".join(link_points))
    if profile_context.get("summary"):
        points.append(str(profile_context.get("summary")))
    return points


def _context_count(value: object) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _coverage_class_count(context: dict[str, Any], coverage_class: str) -> int:
    counts = context.get("db_corpus_coverage_class_counts")
    if not isinstance(counts, dict):
        return 0
    return _context_count(counts.get(coverage_class))


def _context_nested_count(context: dict[str, Any], section: str, key: str) -> int:
    nested = context.get(section)
    if not isinstance(nested, dict):
        return 0
    return _context_count(nested.get(key))


def _determinism_scope_points(context: dict[str, Any]) -> list[str]:
    scope = context.get("determinism_scope")
    if not isinstance(scope, dict):
        return []
    points = []
    provider_outputs = str(scope.get("provider_outputs") or "").strip()
    chunk_merge = str(scope.get("chunk_merge") or "").strip()
    local_fixture = str(scope.get("local_fixture") or "").strip()
    if provider_outputs:
        if provider_outputs in {"recorded_and_hashed", "recorded_and_hashed_not_recreated_byte_for_byte"}:
            points.append("Provider outputs: recorded and hashed; live model responses are not byte-regenerated.")
        else:
            points.append(f"Provider outputs: {provider_outputs}.")
    if chunk_merge:
        if chunk_merge == "deterministic_sort_dedup_over_recorded_chunk_outputs":
            points.append("Chunk merge: deterministic sort and de-dup over recorded chunk outputs.")
        else:
            points.append(f"Chunk merge: {chunk_merge}.")
    if local_fixture:
        if local_fixture in {
            "deterministic_local_provider_ci",
            "byte_equal_regeneration_for_deterministic_local_provider_ci",
        }:
            points.append("Fixture regeneration: byte-equal CI applies to deterministic local fixtures.")
        else:
            points.append(f"Fixture regeneration: {local_fixture}.")
    return points


def _confidence_action_explanation(action: str, overall: object) -> str:
    try:
        score = float(overall)
    except (TypeError, ValueError):
        score = None
    if action == "use_for_subsystem_routing_human_gated":
        return ">=0.75 can route subsystems but still needs human-gated outcomes"
    if action == "candidate_only_requires_profile_review":
        if score is not None and score < 0.75:
            return "<0.75 keeps profile output candidate-only until profile review"
        return "profile output stays candidate-only until profile review"
    if action == "discovery_required_before_routing":
        return "<0.60 requires more discovery before routing"
    return "confidence controls routing strictness"


def _show_context_cell(label: str, value: str) -> bool:
    if not value:
        return False
    if value != "0":
        return True
    return label in {"Prompt-direct DB rows", "Unassigned items"}


def _points_html(points: list[str]) -> str:
    if not points:
        return "<p>No projected notes were persisted.</p>"
    return "<ul>" + "".join(f"<li>{_html(point)}</li>" for point in points) + "</ul>"


def _coverage_text(value: object) -> str:
    try:
        ratio = float(value)
    except (TypeError, ValueError):
        return ""
    if ratio <= 0:
        return ""
    return f"{ratio * 100:.1f}%"


def _incident_gate_signal_text(value: object) -> str:
    text = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    if text in {"established", "signal_present", "present", "true", "1"}:
        return "signal present"
    if text in {"open", "not_established", "none", "false", "0", ""}:
        return "no graph-level signal"
    return text.replace("_", " ")


def _precomputed_review_graph_summary_panel(payload: dict[str, Any]) -> str:
    summary = payload.get("review_graph_summary")
    if not isinstance(summary, dict):
        return ""
    converged = int(summary.get("convergence_count") or 0)
    single_source = int(summary.get("single_source_count") or 0)
    partial_overlap = int(summary.get("partial_overlap_count") or 0)
    conflicts = int(summary.get("conflict_count") or 0)
    auto_archived = int(summary.get("auto_archived_count") or 0)
    note = str(summary.get("note") or "")
    score_definition = str(summary.get("score_definition") or "")
    promotion_policy = str(summary.get("target_promotion_policy") or "")
    incident_gate = _incident_gate_signal_text(summary.get("incident_gate_signal") or summary.get("incident_baseline"))
    summary_text = str(summary.get("summary") or "Provider agreement was evaluated before promotion.")
    policy_text = promotion_policy or "Each target promotion remains human-gated until impact and operational outcome evidence are attached."
    score_text = score_definition or "Convergence score = claimed successful providers / all successful providers."
    note_text = note or "Partial overlap is an overlay count for converged targets where at least one schema-valid provider was silent."
    is_observation_gap = _detail_is_observation_gap(payload)
    section_heading = (
        "Absence of evidence is not evidence of health."
        if is_observation_gap
        else "Convergence is technical support. Impact stays human-gated."
    )
    if is_observation_gap:
        summary_text = (
            f"{converged} review units had at least two provider positions. "
            "The corpus can show normal throughput while positive liveness evidence is still missing, "
            "so every promoted signal remains a question to answer, not a cause to accept."
        )
    gate_title = (
        "Human gate active - no accepted incident cause"
        if is_observation_gap
        else f"Incident gate {incident_gate} - promotion human-gated"
    )
    gate_body = (
        "The graph preserves observation gaps, weak signals, and missing evidence instead of collapsing them into a health claim."
        if is_observation_gap
        else "A graph-level support signal is not a verdict. Each target promotes on its own evidence; promotion stays human-gated until impact evidence is attached."
    )
    stat_cells = [
        (str(converged), "converged targets", "primary"),
        (str(single_source), "single-source targets", ""),
        (str(partial_overlap), "partial overlap", ""),
        (str(conflicts), "explicit conflicts", "safe" if conflicts == 0 else ""),
    ]
    stat_html = "".join(
        f"""
        <article class="arbitration-stat {css_class}">
          <strong>{_html(value)}</strong>
          <span>{_html(label)}</span>
        </article>
        """
        for value, label, css_class in stat_cells
    )
    archived_note = f"{auto_archived} auto-archived post-window" if auto_archived else "no post-window auto-archive"
    detail_note = f"{_html(score_text)} {_html(note_text)} {_html(archived_note)}"
    return f"""
    <section class="section-block graph-arbitration">
      <div class="section-heading">
        <span class="eyebrow">Review Graph Arbitration</span>
        <h2>{_html(section_heading)}</h2>
        <p>{_html(summary_text)}</p>
      </div>
      <div class="review-arbitration-grid">
        {stat_html}
        <article class="arbitration-gate">
          <span class="gate-mark">HG</span>
          <div>
            <strong>{_html(gate_title)}</strong>
            <p>{_html(gate_body)}</p>
            <details class="inline-details">
              <summary>Arbitration notes</summary>
              <p>{_html(policy_text)}</p>
              <p>{detail_note}</p>
              <p>Target promotion: per-target human-gated. Incident gate signal: {_html(incident_gate)}.</p>
            </details>
          </div>
        </article>
      </div>
    </section>"""


def _precomputed_devops_loop_panel(payload: dict[str, Any]) -> str:
    loop = payload.get("devops_loop")
    if not isinstance(loop, dict):
        return ""
    items = [item for item in loop.get("items") or [] if isinstance(item, dict)]
    if not items:
        return ""
    rows = "".join(
        f"""
        <article class="graph-cell">
          <label>{_html(str(item.get("label") or "loop signal"))}</label>
          <strong>{_html(str(item.get("value") or ""))}</strong>
          <p>{_html(str(item.get("detail") or ""))}</p>
        </article>
        """
        for item in items
    )
    return f"""
    <section class="panel">
      <label>DevOps Improvement Loop</label>
      <h2>{_html(str(loop.get("title") or "AI workflow is operated as production software"))}</h2>
      <p>{_html(str(loop.get("summary") or "Pipeline events, regression cases, and tests make the agent loop observable and improvable."))}</p>
      <div class="graph-summary-grid">{rows}</div>
    </section>"""


def _provider_position_counts(target: dict[str, Any]) -> dict[str, int]:
    counts: dict[str, int] = {}
    positions = target.get("provider_positions")
    if not isinstance(positions, list):
        return counts
    for row in positions:
        if not isinstance(row, dict):
            continue
        stance = str(row.get("stance") or "silent").strip() or "silent"
        counts[stance] = counts.get(stance, 0) + 1
    return counts


def _provider_position_summary(target: dict[str, Any]) -> str:
    counts = _provider_position_counts(target)
    if not counts:
        provider_count = int(target.get("provider_count") or 0)
        return f"claimed {provider_count}" if provider_count else "not projected"
    ordered = [
        f"{name} {counts[name]}"
        for name in ("claimed", "contradicted", "silent")
        if counts.get(name)
    ]
    remaining = [
        f"{name} {value}"
        for name, value in sorted(counts.items())
        if name not in {"claimed", "contradicted", "silent"}
    ]
    return " / ".join(ordered + remaining)


def _stance_bar_html(target: dict[str, Any]) -> str:
    counts = _provider_position_counts(target)
    total = sum(counts.values())
    if total <= 0:
        return '<div class="stance-meter" aria-hidden="true"></div>'
    segments = []
    for stance, css_class in (
        ("claimed", "claimed"),
        ("contradicted", "contradicted"),
        ("silent", "silent"),
    ):
        count = counts.get(stance, 0)
        if count <= 0:
            continue
        width = (count / total) * 100
        segments.append(
            f'<span class="stance-fill {css_class}" style="width:{width:.1f}%" title="{_html(stance)} {count}"></span>'
        )
    for stance, count in sorted(counts.items()):
        if stance in {"claimed", "contradicted", "silent"} or count <= 0:
            continue
        width = (count / total) * 100
        css_class = "provider_error" if stance == "provider_error" else "silent"
        segments.append(
            f'<span class="stance-fill {css_class}" style="width:{width:.1f}%" title="{_html(stance)} {count}"></span>'
        )
    return f'<div class="stance-meter" aria-hidden="true">{"".join(segments)}</div>'


def _provider_positions_html(target: dict[str, Any]) -> str:
    positions = [row for row in target.get("provider_positions") or [] if isinstance(row, dict)]
    if not positions:
        return "<p>Provider positions were not projected for this persisted target.</p>"
    rows = "".join(
        f"""
        <article class="position-row">
          <strong>{_html(str(row.get("provider_id") or "provider"))}</strong>
          <span class="stance">{_html(str(row.get("stance") or "silent"))}</span>
          <p>{_html(str(row.get("one_line") or "No normalized statement was projected."))}</p>
          <code>{_html(str(row.get("model_run_hash") or "")[:12])}</code>
        </article>
        """
        for row in positions
    )
    return f'<div class="position-list">{rows}</div>'


def _target_agreement_text(target: dict[str, Any]) -> str:
    agreement = target.get("agreement")
    if not isinstance(agreement, dict):
        return "Agreement projection is not available for this persisted target."
    verdict = str(agreement.get("verdict") or "unknown")
    score = agreement.get("convergence_score")
    try:
        score_text = f"{float(score):.3f}"
    except (TypeError, ValueError):
        score_text = "unknown"
    technical = str(agreement.get("technical_baseline") or "open")
    incident = str(agreement.get("incident_baseline") or "open")
    summary = str(agreement.get("summary") or "")
    definition = str(agreement.get("score_definition") or "")
    definition_text = f" Definition: {definition}." if definition else ""
    base = (
        f"Verdict: {verdict}. Convergence score: {score_text}. "
        f"Technical support: {technical}. Incident promotion: {incident}.{definition_text}"
    )
    return f"{base} {summary}".strip()


def _target_promotion_text(target: dict[str, Any]) -> str:
    promotion = target.get("promotion")
    if not isinstance(promotion, dict):
        return "Promotion gate details are not available for this persisted target."
    state = str(promotion.get("state") or "validation")
    reason = str(promotion.get("blocked_reason") or "human validation required")
    cap = promotion.get("score_cap_applied")
    cap_text = "score cap applied" if cap else "no score cap applied"
    note = str(promotion.get("score_note") or "")
    explanation = str(promotion.get("explanation") or "")
    text = f"State: {state}. Blocked because: {reason}. {cap_text}."
    return f"{text} {explanation} {note}".strip()


def _target_review_reason_html(target: dict[str, Any]) -> str:
    reason = target.get("review_reason") if isinstance(target.get("review_reason"), dict) else {}
    headline = str(reason.get("headline") or "").strip()
    factors = [str(item).strip() for item in reason.get("factors") or [] if str(item).strip()]
    operator_question = str(reason.get("operator_question") or "").strip()
    if not headline:
        canonical_unit = str(target.get("canonical_review_unit") or target.get("subsystem") or "review unit")
        provider_summary = _provider_position_summary(target)
        evidence_refs = target.get("evidence_refs") if isinstance(target.get("evidence_refs"), list) else []
        evidence_ref_total_count = _target_evidence_ref_total_count(target, evidence_refs)
        promotion = target.get("promotion") if isinstance(target.get("promotion"), dict) else {}
        blocked_reason = str(promotion.get("blocked_reason") or "human validation required")
        headline = (
            f"Review target created because `{canonical_unit}` was projected from provider output "
            "and still needs human validation."
        )
        factors = [
            f"Provider stance: {provider_summary}.",
            f"{evidence_ref_total_count} cited Evidence Item(s) are attached.",
            f"Promotion is blocked by `{blocked_reason}`.",
        ]
        operator_question = str(target.get("recommended_request_type") or "Review cited evidence and missing signals.")
    items = "".join(f"<li>{_html(item)}</li>" for item in factors)
    question = f"<p>{_html(operator_question)}</p>" if operator_question else ""
    return f"<p>{_html(headline)}</p><ul>{items}</ul>{question}"


def _target_explanation_html(target: dict[str, Any]) -> str:
    explanation = target.get("target_explanation") if isinstance(target.get("target_explanation"), dict) else {}
    suspected_issue = str(
        target.get("suspected_issue")
        or explanation.get("suspected_issue")
        or target.get("impact_summary")
        or target.get("claim")
        or target.get("title")
        or "Review target needs human validation."
    )
    operational_mechanism = str(
        target.get("operational_mechanism")
        or explanation.get("operational_mechanism")
        or "Operational mechanism was not supplied by the provider output."
    )
    why_it_matters = str(
        target.get("why_it_matters")
        or explanation.get("why_it_matters")
        or "Outcome impact is not proven by this target alone."
    )
    why_not_promoted = str(
        target.get("why_not_promoted")
        or explanation.get("why_not_promoted")
        or _target_promotion_text(target)
    )
    next_question = str(
        target.get("next_validation_question")
        or explanation.get("next_validation_question")
        or target.get("recommended_request_type")
        or "Review cited evidence and missing signals."
    )
    evidence_summary = _string_items(target.get("evidence_summary") or explanation.get("evidence_summary"))
    if not evidence_summary:
        refs = target.get("evidence_refs") if isinstance(target.get("evidence_refs"), list) else []
        evidence_summary = [
            f"{ref}: cited runtime evidence for this target; inspect the Evidence Item body before treating it as causal support."
            for ref in refs[:8]
        ]
    counter_summary = _string_items(
        target.get("counter_evidence_summary") or explanation.get("counter_evidence_summary")
    )
    evidence_items = "".join(f"<li>{_html(item)}</li>" for item in evidence_summary[:8])
    counter_items = "".join(f"<li>{_html(item)}</li>" for item in counter_summary[:6])
    counter_block = f"<label>Counter / weak signals</label><ul>{counter_items}</ul>" if counter_items else ""
    return f"""
      <div class="target-explanation">
        <label>Suspected issue</label><p>{_html(suspected_issue)}</p>
        <label>Operational mechanism</label><p>{_html(operational_mechanism)}</p>
        <label>Why it matters</label><p>{_html(why_it_matters)}</p>
        <label>Evidence summary</label><ul>{evidence_items}</ul>
        {counter_block}
        <label>Why not promoted</label><p>{_html(why_not_promoted)}</p>
        <label>Next validation question</label><p>{_html(next_question)}</p>
      </div>
    """


def _precomputed_target_preview_panel(targets: list[dict[str, Any]]) -> str:
    rows = "".join(
        f"""
        <article class="target-preview">
          <label>{_html(str(target.get("subsystem") or target.get("class") or "review target"))}</label>
          <strong>{_html(str(target.get("title") or "Review target"))}</strong>
          <p>{_html(str(target.get("claim") or target.get("core_claim") or "Evidence-backed validation target."))}</p>
          <div class="pill-row">
            <span class="pill">priority {_html(f"{float(target.get('review_priority_score') or target.get('score') or 0.0):.3f}")}</span>
            <span class="pill">{_html(str((target.get("agreement") or {}).get("verdict") if isinstance(target.get("agreement"), dict) else "agreement pending"))}</span>
            <span class="pill">{_html(_provider_position_summary(target))}</span>
            <span class="pill">{_html(str(target.get("recommended_request_type") or target.get("next_check") or "review"))}</span>
          </div>
        </article>
        """
        for target in targets
    )
    return f"""
    <section class="panel secondary">
      <label>Showcased Review Targets</label>
      <h2>Convergence and human-gated checks before causal judgement</h2>
      <p>The first screen shows both provider convergence and validation work immediately; deeper evidence refs are available in the detailed review.</p>
      <div class="target-preview-grid">{rows}</div>
    </section>"""


def _fast_detail_target_card(target: dict[str, Any], *, index: int, anchor_id: str | None = None) -> str:
    score = _target_score_value(target)
    title = str(target.get("title") or target.get("core_claim") or target.get("proposal") or f"Review target {index}")
    target_class = str(target.get("class") or target.get("target_class") or target.get("review_mode") or "review_target")
    target_class_css = "".join(ch if ch.isalnum() else "-" for ch in target_class.lower()).strip("-")
    status = str(target.get("status") or "pending")
    subsystem = str(target.get("subsystem") or target.get("component") or target.get("canonical_review_unit") or "general")
    evidence_refs = target.get("evidence_refs") if isinstance(target.get("evidence_refs"), list) else []
    evidence_ref_total_count = _target_evidence_ref_total_count(target, evidence_refs)
    evidence_ref_display_count = len(evidence_refs)
    evidence_ref_overflow_count = max(0, int(target.get("evidence_ref_overflow_count") or 0))
    evidence_ref_count_label = (
        f"{evidence_ref_display_count} displayed / {evidence_ref_total_count} chunk-tracked"
        if evidence_ref_total_count != evidence_ref_display_count
        else f"{evidence_ref_display_count} displayed"
    )
    evidence_ref_overflow_note = (
        f" ({evidence_ref_overflow_count} more chunk-tracked ref(s) not printed on this card)"
        if evidence_ref_overflow_count
        else ""
    )
    missing = target.get("missing_evidence") if isinstance(target.get("missing_evidence"), list) else []
    caveats = target.get("caveats") if isinstance(target.get("caveats"), list) else []
    claim = str(target.get("claim") or target.get("core_claim") or target.get("impact_summary") or target.get("proposal") or "")
    action = str(target.get("recommended_validation") or target.get("recommended_request_type") or target.get("proposal") or "")
    agreement = target.get("agreement") if isinstance(target.get("agreement"), dict) else {}
    agreement_verdict = str(agreement.get("verdict") or "agreement pending")
    provider_summary = _provider_position_summary(target)
    provider_positions = _provider_positions_html(target)
    agreement_text = _target_agreement_text(target)
    promotion_text = _target_promotion_text(target)
    review_reason = _target_review_reason_html(target)
    target_explanation = _target_explanation_html(target)
    priority_scoring = _priority_scoring_html(target)
    tie_breaker = _priority_tie_breaker_text(target, index=index, evidence_ref_total_count=evidence_ref_total_count)
    missing_total = len(missing)
    missing_label = "Top missing evidence" if missing_total > 4 else "Missing evidence"
    missing_text = "; ".join(str(item) for item in missing[:4]) or "none"
    if missing_total > 4:
        missing_text = f"{missing_text} ({missing_total - 4} more grouped follow-up item(s) not shown)"
    anchor = anchor_id or _target_anchor_id(target, index=index)
    stance_bar = _stance_bar_html(target)
    return f"""
<article class="target" id="{_html(anchor)}" data-target-group="{_html(_target_group_key(target))}">
  <div class="target-head">
    <div>
      <label>Target {index}</label>
      <h2>{_html(title)}</h2>
      <div class="pill-row">
        <span class="pill target-class-{_html(target_class_css)}">Class: {_html(_target_class_label(target))}</span>
        <span class="pill">Status: {_html(status)}</span>
        <span class="pill">Subsystem: {_html(subsystem)}</span>
        <span class="pill">Agreement: {_html(agreement_verdict)}</span>
        <span class="pill">Provider stance: {_html(provider_summary)}</span>
        <span class="pill">Evidence tracking: {_html(evidence_ref_count_label)}</span>
      </div>
      {stance_bar}
    </div>
    <div class="score">{score:.3f}<span>Priority</span><small>{_html(tie_breaker)}</small></div>
  </div>
  <div class="target-grid">
    <div class="field full"><label>What this target means operationally</label>{target_explanation}</div>
    <div class="field full"><label>Why this target is in review</label>{review_reason}</div>
    {priority_scoring}
    <div class="field full"><label>Observed claim</label><p>{_html(claim or title)}</p></div>
    <div class="field full"><label>Provider positions</label>{provider_positions}</div>
    <div class="field full"><label>Agreement and promotion gates</label><p>{_html(agreement_text)}</p></div>
    <div class="field full">
      <label>Promotion gate</label>
      <div class="human-gate">
        <span class="gate-mark">HG</span>
        <div>
          <strong>Human-gated / not auto-accepted</strong>
          <p>{_html(promotion_text)}</p>
        </div>
      </div>
    </div>
    <div class="field"><label>Next check</label><p>{_html(action or "Review cited evidence and missing signals.")}</p></div>
    <div class="field"><label>{_html(missing_label)}</label><p>{_html(missing_text)}</p></div>
    <div class="field"><label>Displayed evidence refs</label><p>{_html((", ".join(str(item) for item in evidence_refs[:8]) or "none") + evidence_ref_overflow_note)}</p></div>
    <div class="field"><label>Caveats</label><p>{_html("; ".join(str(item) for item in caveats[:4]) or "none")}</p></div>
  </div>
</article>"""


def _priority_scoring_html(target: dict[str, Any]) -> str:
    breakdown = target.get("score_breakdown") if isinstance(target.get("score_breakdown"), dict) else {}
    model = breakdown.get("priority_model") if isinstance(breakdown.get("priority_model"), dict) else breakdown
    if not isinstance(model, dict) or str(model.get("schema_version") or "") != "review_priority_score.v2":
        return ""
    penalties = model.get("penalties") if isinstance(model.get("penalties"), dict) else {}
    rows = [
        ("Weighted provider support", model.get("weighted_provider_support")),
        ("Gemini claimed", "yes" if model.get("gemini_claimed") else "no"),
        ("Evidence volume", model.get("evidence_volume_signal")),
        ("Evidence diversity", model.get("evidence_diversity_signal")),
        ("Source breadth", model.get("source_candidate_signal")),
        ("Actionability", model.get("actionability_signal")),
        ("Penalty", penalties.get("total_penalty")),
        ("Tie-break", model.get("deterministic_tie_break")),
    ]
    items = []
    for label, value in rows:
        if isinstance(value, (int, float)):
            rendered = f"{float(value):.3f}"
        else:
            rendered = str(value)
        items.append(f"<li><b>{_html(label)}:</b> {_html(rendered)}</li>")
    formula = str(model.get("formula") or "")
    note = str(model.get("score_note") or "Priority is review urgency, not truth probability.")
    return (
        "<div class=\"field full\"><label>Priority scoring</label>"
        f"<p>{_html(note)}</p>"
        f"<ul>{''.join(items)}</ul>"
        f"<p>{_html(formula)}</p>"
        "</div>"
    )


def _priority_tie_breaker_text(target: dict[str, Any], *, index: int, evidence_ref_total_count: int) -> str:
    provider_count = int(target.get("provider_count") or 0)
    missing = target.get("missing_evidence") if isinstance(target.get("missing_evidence"), list) else []
    source_candidates = 0
    raw = target.get("raw") if isinstance(target.get("raw"), dict) else {}
    try:
        source_candidates = int(raw.get("source_candidate_count") or 0)
    except (TypeError, ValueError):
        source_candidates = 0
    return (
        f"Queue rank #{index}; tie-break uses provider count {provider_count}, "
        f"chunk-tracked refs {evidence_ref_total_count}, missing items {len(missing)}, "
        f"source candidates {source_candidates}."
    )


def _target_evidence_ref_total_count(target: dict[str, Any], evidence_refs: list[Any]) -> int:
    raw_total = target.get("evidence_ref_total_count")
    try:
        total = int(raw_total)
    except (TypeError, ValueError):
        total = 0
    return max(total, len(evidence_refs))


def _short_sha(value: str) -> str:
    text = str(value or "")
    return text if len(text) <= 24 else f"{text[:12]}...{text[-12:]}"


def _display_policy(value: object) -> str:
    text = str(value or "").strip()
    return text.replace("_", " ").replace("-", " ") if text else "unknown"


def _string_items(value: object) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value if str(item).strip()]
    text = str(value or "").strip()
    return [text] if text else []


def _url_quote(value: str) -> str:
    return quote(str(value or ""), safe="")


def _js_string(value: object) -> str:
    encoded = json.dumps(str(value or ""), ensure_ascii=False)
    return encoded[1:-1]


def _fast_review_shell(evidence_sha256: str, *, precomputed: dict[str, Any] | None = None) -> str:
    precomputed = precomputed if precomputed is not None else _precomputed_review_payload(evidence_sha256)
    summary = _precomputed_summary(precomputed, evidence_sha256) if precomputed else None
    finding = summary.get("finding") if isinstance(summary, dict) and isinstance(summary.get("finding"), dict) else {}
    review = summary.get("review") if isinstance(summary, dict) and isinstance(summary.get("review"), dict) else {}
    providers = summary.get("providers") if isinstance(summary, dict) and isinstance(summary.get("providers"), dict) else {}
    raw_policy = str(summary.get("raw_log_policy") or "pending") if isinstance(summary, dict) else "pending"
    log_count = int(summary.get("log_count") or 0) if isinstance(summary, dict) else 0
    graph_sha = str(summary.get("canonical_graph_sha256") or "") if isinstance(summary, dict) else ""
    updated_at = str(summary.get("updated_at") or "") if isinstance(summary, dict) else ""
    target_previews = [target for target in (precomputed or {}).get("targets", []) if isinstance(target, dict)][:3]
    target_preview_html = _precomputed_target_preview_panel(target_previews) if target_previews else ""
    trace_panel = _precomputed_agent_trace_panel(precomputed or {})
    graph_summary_panel = _precomputed_review_graph_summary_panel(precomputed or {})
    devops_loop_panel = _precomputed_devops_loop_panel(precomputed or {})
    short_sha = _short_sha(evidence_sha256)
    full_url = f"/ui/full-review-page?evidence_sha256={_url_quote(evidence_sha256)}"
    action_links = _public_action_links_html(evidence_sha256)
    finding_title = str(finding.get("title") or "No persisted finding yet")
    finding_impact = _public_finding_impact_text(
        summary, str(finding.get("impact") or "Run analysis to create a persisted review result.")
    )
    provider_text = (
        f"{int(providers.get('success') or 0)} / {int(providers.get('total') or 0)}"
        if providers
        else "pending"
    )
    primary_text = str(int(review.get("primary_targets") or 0)) if review else "pending"
    validation_text = str(int(review.get("validation_targets") or 0)) if review else "pending"
    graph_text = f"Graph {_short_sha(graph_sha)}" if graph_sha else "canonical graph pending"
    raw_log_note = _human_count(log_count) + " sanitized logs" if log_count else "sanitized evidence only"
    initial_state = "Precomputed result ready" if summary else "Report shell ready"
    initial_note = (
        "This first response already contains the persisted review summary."
        if summary
        else "The page is usable while the lightweight summary is fetched."
    )
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Ops Evidence Synthesis</title>
  <style>
    :root {{
      color-scheme: light;
      --ink: #18202a;
      --muted: #647184;
      --line: #d8dee8;
      --bg: #f7f8fb;
      --panel: #ffffff;
      --accent: #166d6b;
      --accent-2: #2f5f9e;
      --warn: #a15c00;
      --danger: #b42318;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background: var(--bg);
      color: var(--ink);
      letter-spacing: 0;
    }}
    header {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 14px;
      padding: 18px 24px;
      border-bottom: 1px solid var(--line);
      background: var(--panel);
    }}
    h1 {{ margin: 0; font-size: 20px; font-weight: 800; }}
    main {{
      display: grid;
      gap: 16px;
      padding: 18px 24px 40px;
      max-width: 1180px;
      margin: 0 auto;
    }}
    .panel {{
      border: 1px solid var(--line);
      border-left: 5px solid var(--accent);
      border-radius: 8px;
      background: var(--panel);
      padding: 16px;
      display: grid;
      gap: 12px;
      min-width: 0;
    }}
    .panel.secondary {{ border-left-color: var(--accent-2); }}
    .panel.warn {{ border-left-color: var(--warn); }}
    .panel.compact {{ border-left-color: #7c6f2b; }}
    .summary-grid {{
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 12px;
    }}
    .result-grid {{
      display: grid;
      grid-template-columns: minmax(0, 1.45fr) repeat(3, minmax(86px, 0.45fr)) minmax(150px, 0.7fr);
      gap: 10px;
      align-items: stretch;
    }}
    .result-cell {{
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 10px;
      min-width: 0;
      background: #fbfcfe;
    }}
    .trace-grid, .target-preview-grid, .graph-summary-grid {{
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 10px;
    }}
    .graph-summary-grid {{ grid-template-columns: repeat(4, minmax(0, 1fr)); }}
    .trace-step, .target-preview, .graph-cell {{
      border: 1px solid var(--line);
      border-radius: 6px;
      background: #fbfcfe;
      padding: 10px;
      min-width: 0;
    }}
    .result-cell strong {{ font-size: 18px; }}
    .result-cell p {{ font-size: 13px; }}
    label {{
      display: block;
      color: var(--muted);
      font-size: 12px;
      font-weight: 800;
      text-transform: uppercase;
      margin-bottom: 5px;
    }}
    strong {{
      display: block;
      font-size: 20px;
      line-height: 1.25;
      overflow-wrap: anywhere;
    }}
    p {{ margin: 0; color: var(--muted); line-height: 1.45; }}
    code {{
      font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, "Liberation Mono", monospace;
      overflow-wrap: anywhere;
    }}
    .meta {{
      color: var(--muted);
      font-size: 12px;
      text-align: right;
      max-width: 560px;
    }}
    .progress {{
      height: 8px;
      border-radius: 999px;
      background: #e8edf4;
      overflow: hidden;
    }}
    .progress div {{
      height: 100%;
      width: 35%;
      border-radius: inherit;
      background: var(--accent-2);
      animation: loading 1.2s ease-in-out infinite alternate;
    }}
    .actions {{ display: flex; flex-wrap: wrap; gap: 10px; }}
    button, a.button {{
      border: 1px solid var(--line);
      border-radius: 6px;
      background: #fff;
      color: var(--ink);
      padding: 8px 10px;
      font: inherit;
      font-weight: 700;
      cursor: pointer;
      text-decoration: none;
    }}
    button.primary, a.button.primary {{
      border-color: var(--accent);
      background: var(--accent);
      color: #fff;
    }}
    .error {{ color: var(--danger); }}
    @keyframes loading {{
      from {{ transform: translateX(-20%); }}
      to {{ transform: translateX(190%); }}
    }}
    @media (max-width: 760px) {{
      header {{ display: grid; align-items: start; }}
      .meta {{ text-align: left; }}
      .summary-grid, .trace-grid, .target-preview-grid, .graph-summary-grid {{ grid-template-columns: 1fr; }}
      .result-grid {{ grid-template-columns: 1fr 1fr; }}
      .result-cell:first-child {{ grid-column: 1 / -1; }}
      main {{ padding: 14px; }}
    }}
  </style>
</head>
<body>
  <header>
    <h1>Ops Evidence Synthesis</h1>
    <div class="meta">Evidence <code>{_html(short_sha)}</code></div>
  </header>
  <main>
    <section class="panel">
      <div class="summary-grid">
        <div>
          <label>Selected Evidence</label>
          <strong>{_html(short_sha)}</strong>
          <p>{_html(updated_at or "Persisted evidence selected")}</p>
        </div>
        <div>
          <label>Initial State</label>
          <strong>{_html(initial_state)}</strong>
          <p>{_html(initial_note)}</p>
        </div>
        <div>
          <label>Delivery Mode</label>
          <strong>Read-only Cloud result</strong>
          <p>Initial GET does not start model runs, collectors, or mutation work.</p>
        </div>
      </div>
    </section>
    <section class="panel compact" id="summary-panel" aria-live="polite">
      <label>Persisted Result Summary</label>
      <div class="result-grid">
        <div class="result-cell">
          <label>Finding</label>
          <strong id="summary-finding">{_html(finding_title)}</strong>
          <p id="summary-impact">{_html(finding_impact)}</p>
        </div>
        <div class="result-cell">
          <label>Providers</label>
          <strong id="summary-providers">{_html(provider_text)}</strong>
          <p>successful model outputs</p>
        </div>
        <div class="result-cell">
          <label>Primary</label>
          <strong id="summary-primary">{_html(primary_text)}</strong>
          <p>promoted targets</p>
        </div>
        <div class="result-cell">
          <label>Validation</label>
          <strong id="summary-validation">{_html(validation_text)}</strong>
          <p>human review targets</p>
        </div>
        <div class="result-cell">
          <label>Raw Logs</label>
          <strong id="summary-raw-policy">{_html(_display_policy(raw_policy))}</strong>
          <p id="summary-graph">{_html(raw_log_note)} / {_html(graph_text)}</p>
        </div>
      </div>
    </section>
    {graph_summary_panel}
    {trace_panel}
    {devops_loop_panel}
    {target_preview_html}
    <section class="panel secondary">
      <label>Detailed Review</label>
      <strong>Review targets and provider status are ready</strong>
      <p>Open the detailed page for the full target list. This route is precomputed and read-only for evaluator self-service.</p>
      <div class="actions">
        <a class="button primary" href="{_html(full_url)}">Open detailed review</a>
        {action_links}
      </div>
    </section>
  </main>
  <script>
    const evidenceSha = "{_js_string(evidence_sha256)}";
    const summaryUrl = `/ui/summary?evidence_sha256=${{encodeURIComponent(evidenceSha)}}`;
    const setText = (id, value) => {{
      const node = document.getElementById(id);
      if (node) node.textContent = value ?? "";
    }};
    const displayPolicy = (value) => String(value || "unknown").replace(/[_-]+/g, " ");

    async function loadSummary() {{
      try {{
        const response = await fetch(summaryUrl, {{headers: {{"Accept": "application/json"}}}});
        if (!response.ok) throw new Error(`HTTP ${{response.status}}`);
        const summary = await response.json();
        setText("summary-finding", summary.finding?.title || "No persisted finding yet");
        setText("summary-impact", summary.finding?.impact || summary.message || "Run analysis to create a review summary.");
        setText("summary-providers", `${{Number(summary.providers?.success || 0)}} / ${{Number(summary.providers?.total || 0)}}`);
        setText("summary-primary", String(Number(summary.review?.primary_targets || 0)));
        setText("summary-validation", String(Number(summary.review?.validation_targets || 0)));
        setText("summary-raw-policy", displayPolicy(summary.raw_log_policy));
        const logCount = Number(summary.log_count || 0);
        const logText = logCount ? `${{logCount.toLocaleString()}} sanitized logs` : "sanitized evidence only";
        const graphText = summary.canonical_graph_sha256 ? `Graph ${{summary.canonical_graph_sha256.slice(0, 12)}}...` : "canonical graph not persisted";
        setText("summary-graph", `${{logText}} / ${{graphText}}`);
      }} catch (error) {{
        console.warn("summary refresh failed", error);
      }}
    }}

    loadSummary();
  </script>
</body>
</html>"""


def _render_rescore_demo_page(demo_id: str) -> str:
    payload = _rescore_demo_payload(demo_id)
    if not payload:
        return ""
    before = payload.get("before") if isinstance(payload.get("before"), dict) else {}
    loop = payload.get("more_data_loop") if isinstance(payload.get("more_data_loop"), dict) else {}
    after = payload.get("after") if isinstance(payload.get("after"), dict) else {}
    control = payload.get("control_plane") if isinstance(payload.get("control_plane"), dict) else {}
    verification = payload.get("verification") if isinstance(payload.get("verification"), dict) else {}
    rows = loop.get("collected_rows") if isinstance(loop.get("collected_rows"), list) else []
    row_html = "".join(_rescore_evidence_row_html(row) for row in rows if isinstance(row, dict))
    if not row_html:
        row_html = "<article class='evidence-row'><p>No child rows recorded.</p></article>"
    providers = control.get("cross_check_providers") if isinstance(control.get("cross_check_providers"), list) else []
    primary_provider = str(control.get("primary_provider") or "gemini-enterprise-agent-platform")
    provider_cards_html = _rescore_control_provider_cards_html(primary_provider, providers)
    before_reasons = ", ".join(str(item) for item in before.get("blocked_reasons") or []) or "none"
    after_reasons = ", ".join(str(item) for item in after.get("blocked_reasons") or []) or "none"
    before_provider_positions = _rescore_provider_positions_html(before.get("provider_positions"))
    after_provider_positions = _rescore_provider_positions_html(after.get("provider_positions"))
    before_stance = _rescore_provider_stance_label(before.get("provider_positions"))
    after_stance = _rescore_provider_stance_label(after.get("provider_positions"))
    source_evidence_sha = str(payload.get("source_evidence_sha256") or "")
    action_links = _public_action_links_html(source_evidence_sha) if source_evidence_sha else ""
    source_trace_html = _rescore_source_trace_html(payload)
    source_review_url = str(payload.get("source_review_url") or "#")
    before_score = float(before.get("promotion_score") or 0)
    after_score = float(after.get("promotion_score") or 0)
    before_width = max(0, min(100, before_score * 100))
    after_width = max(0, min(100, after_score * 100))
    status_transition = str(loop.get("status_transition") or "needs_more_data -> evidence_collected")
    child_evidence_sha = str(loop.get("child_evidence_sha256") or "")
    added_refs = int(loop.get("added_evidence_ref_count") or 0)
    added_logs = int(loop.get("added_log_count") or 0)
    local_test = str(verification.get("local_test") or "")
    raw_policy = str(verification.get("raw_log_policy") or "not_uploaded")
    public_mode = str(verification.get("public_mode") or "read_only_precomputed")
    before_title = str(before.get("title") or "Restart loop requires validation")
    after_title = str(after.get("title") or "Notifier restart loop has user-visible delivery impact")
    ledger_html = "\n".join(
        _rescore_ledger_row_html(field, old, new)
        for field, old, new in [
            ("state", str(before.get("state") or ""), str(after.get("state") or "")),
            ("promotion_score", f"{before_score:.2f}", f"{after_score:.2f}"),
            ("blocked_reasons", before_reasons, after_reasons),
            ("providers_claimed", before_stance, after_stance),
            ("evidence_refs", "baseline set", f"+{added_refs} child refs"),
        ]
    )
    style = """
    :root {
      color-scheme: light;
      --bg: #f4f2ec;
      --bg-2: #faf8f2;
      --surface: #fffdf8;
      --paper: #ffffff;
      --ink: #1c1a15;
      --ink-2: #4a463d;
      --ink-3: #7a746a;
      --muted: #8a857a;
      --border: #e5dfd1;
      --border-2: #e7e0d1;
      --blue: #3f63a8;
      --blue-soft: #eef2f9;
      --blue-border: #cdd8ec;
      --green: #2f8a5b;
      --green-soft: #eef7f1;
      --green-border: #bfe0cd;
      --gold: #a7845a;
      --gold-soft: #f2ead9;
      --warn: #b06a34;
      --warn-soft: #fbeee0;
      --dark: #1c1a15;
      --shadow: 0 24px 55px -34px rgba(60, 50, 30, .42);
      --mono: "IBM Plex Mono", ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      --sans: "IBM Plex Sans", Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      --display: "Space Grotesk", var(--sans);
      --serif: "Newsreader", Georgia, "Times New Roman", serif;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      background: var(--bg);
      color: var(--ink);
      font-family: var(--sans);
      letter-spacing: 0;
      -webkit-font-smoothing: antialiased;
    }
    a { color: inherit; text-decoration: none; }
    p { margin: 0; color: var(--ink-3); line-height: 1.6; }
    code { font-family: var(--mono); overflow-wrap: anywhere; }
    .page { width: 100%; overflow-x: hidden; }
    .wrap { max-width: 1220px; margin: 0 auto; padding-left: 32px; padding-right: 32px; }
    .nav {
      position: sticky;
      top: 0;
      z-index: 20;
      border-bottom: 1px solid var(--border);
      background: rgba(250, 248, 242, .9);
      backdrop-filter: blur(8px);
    }
    .nav-inner {
      min-height: 58px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 18px;
      padding-top: 14px;
      padding-bottom: 14px;
    }
    .crumbs, .nav-actions { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; min-width: 0; }
    .brand { display: inline-flex; align-items: center; gap: 9px; color: var(--ink); font-family: var(--display); font-weight: 700; font-size: 14.5px; }
    .brand-mark { width: 26px; height: 26px; border-radius: 7px; background: var(--ink); color: var(--bg); display: grid; place-items: center; font: 700 11px/1 var(--mono); }
    .crumbs span, .crumbs a, .nav-actions a { color: var(--muted); font-size: 13.5px; }
    .crumb-sep { color: #c9c1b2; }
    .nav-actions { justify-content: flex-end; gap: 16px; }
    .nav-actions a:hover, .crumbs a:hover { color: var(--ink); }
    .live-chip, .soft-chip {
      display: inline-flex;
      align-items: center;
      gap: 7px;
      border-radius: 20px;
      font: 600 11.5px/1 var(--mono);
      white-space: nowrap;
    }
    .live-chip { color: var(--green); border: 1px solid var(--green-border); background: var(--green-soft); padding: 5px 11px; }
    .live-chip i { width: 7px; height: 7px; border-radius: 50%; background: var(--green); }
    .soft-chip { color: var(--muted); background: #efe9db; border: 1px solid #e0d8c7; padding: 4px 10px; }
    .hero { padding-top: 52px; padding-bottom: 34px; }
    .hero-kickers { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; margin-bottom: 18px; }
    .kicker { color: var(--gold); font: 600 12px/1 var(--mono); letter-spacing: .12em; text-transform: uppercase; }
    h1 { margin: 0 0 18px; max-width: 900px; color: var(--ink); font-family: var(--serif); font-size: clamp(42px, 4.4vw, 64px); line-height: 1.03; font-weight: 500; letter-spacing: 0; overflow-wrap: anywhere; }
    h2 { margin: 0; color: var(--ink); font-family: var(--serif); font-size: 32px; line-height: 1.1; font-weight: 500; letter-spacing: 0; overflow-wrap: anywhere; }
    h3 { margin: 0; color: var(--ink); font-family: var(--display); font-size: 20px; line-height: 1.25; font-weight: 600; letter-spacing: 0; overflow-wrap: anywhere; }
    .hero-copy { max-width: 830px; color: var(--ink-2); font-size: 16.5px; line-height: 1.6; }
    .source-link { display: inline-flex; max-width: 100%; margin-top: 20px; color: var(--blue); font: 700 12px/1.4 var(--mono); overflow-wrap: anywhere; }
    .delta-strip {
      display: grid;
      grid-template-columns: .75fr .75fr 1.15fr 1.35fr;
      gap: 0;
      margin-top: 34px;
      border: 1px solid var(--border);
      border-radius: 12px;
      overflow: hidden;
      background: var(--border);
    }
    .delta-cell { min-width: 0; background: var(--bg-2); padding: 16px 18px; }
    .delta-cell span { display: block; margin-bottom: 5px; color: var(--muted); font: 600 11px/1.3 var(--mono); text-transform: uppercase; }
    .delta-cell strong { display: block; color: var(--ink); font-family: var(--display); font-size: 21px; line-height: 1.1; overflow-wrap: anywhere; }
    .delta-cell:nth-child(4) strong { font-family: var(--mono); font-size: 15px; line-height: 1.25; }
    .delta-cell em { display: block; margin-top: 4px; color: var(--green); font-style: normal; font-size: 12px; }
    .theater { margin-top: 18px; padding: 42px 0; background: #efe9db; border-top: 1px solid var(--border); border-bottom: 1px solid var(--border); }
    .section-head {
      display: flex;
      align-items: flex-end;
      justify-content: space-between;
      gap: 20px;
      flex-wrap: wrap;
      margin-bottom: 20px;
    }
    .section-head p { max-width: 720px; margin-top: 8px; color: var(--ink-3); font-size: 14.5px; }
    .controls { display: flex; flex-direction: column; align-items: flex-end; gap: 10px; }
    .segments {
      display: flex;
      gap: 4px;
      padding: 4px;
      border: 1px solid var(--border);
      border-radius: 10px;
      background: var(--surface);
    }
    button, .button {
      border: 1px solid var(--border-2);
      border-radius: 8px;
      background: var(--surface);
      color: var(--ink-2);
      padding: 8px 12px;
      font: 700 12.5px/1 var(--display);
      cursor: pointer;
    }
    button:hover, .button:hover { border-color: #d4c8b4; color: var(--ink); }
    button:disabled { cursor: not-allowed; opacity: .55; }
    .segments button { border: 0; background: transparent; color: var(--muted); }
    .segments button.active { background: #efe9db; color: var(--ink); }
    .actions { display: flex; align-items: center; justify-content: flex-end; gap: 8px; flex-wrap: wrap; }
    .button.primary, button.primary { border-color: var(--ink); background: var(--ink); color: var(--bg); }
    .stage {
      display: grid;
      grid-template-columns: minmax(320px, .84fr) minmax(560px, 1.16fr);
      gap: 22px;
      align-items: stretch;
    }
    .bundle-card, .target-card, .ledger-card, .provider-card, .source-trace {
      border: 1px solid var(--border-2);
      border-radius: 14px;
      background: var(--paper);
      box-shadow: var(--shadow);
      min-width: 0;
    }
    .bundle-card {
      display: flex;
      flex-direction: column;
      padding: 24px;
      background: var(--surface);
      transition: border-color .2s ease, box-shadow .2s ease;
    }
    .bundle-card.active { border-color: var(--green-border); box-shadow: 0 24px 55px -34px rgba(47, 138, 91, .5); }
    .bundle-top { display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; }
    .mono { color: var(--muted); font: 500 11.5px/1.5 var(--mono); overflow-wrap: anywhere; }
    .phase-chip { flex: none; border: 1px solid #e6dcc6; border-radius: 6px; background: var(--gold-soft); color: var(--gold); padding: 4px 9px; font: 600 10.5px/1 var(--mono); }
    .bundle-card.active .phase-chip { color: var(--green); border-color: var(--green-border); background: var(--green-soft); }
    .bundle-card h3 { margin-top: 18px; font-size: 19px; }
    .bundle-card p { margin-top: 8px; color: var(--ink-3); font-size: 13.5px; }
    .evidence-list { display: grid; gap: 10px; margin-top: 20px; }
    .evidence-row { border: 1px solid #ece5d6; border-radius: 9px; background: #faf7f0; padding: 12px 14px; }
    .bundle-card.active .evidence-row { border-color: var(--green-border); background: #f5faf6; }
    .evidence-row div { display: flex; align-items: center; justify-content: space-between; gap: 10px; }
    .evidence-row time { color: var(--muted); font: 500 11px/1.3 var(--mono); }
    .evidence-row span { color: var(--blue); border: 1px solid var(--blue-border); border-radius: 6px; background: var(--blue-soft); padding: 3px 8px; font: 600 10.5px/1 var(--mono); }
    .evidence-row p { margin-top: 8px; color: var(--ink-2); font-size: 12.5px; }
    .feed-line { margin-top: auto; padding-top: 18px; display: flex; align-items: center; gap: 9px; color: var(--muted); font-size: 12.5px; }
    .feed-line b { color: var(--green); font: 600 16px/1 var(--mono); }
    .target-card { overflow: hidden; background: var(--paper); }
    .phase-view[hidden] { display: none; }
    .target-accent { height: 4px; width: 100%; background: var(--blue); }
    .phase-view[data-phase-view="after"] .target-accent { background: var(--green); }
    .target-main { padding: 22px 24px; border-bottom: 1px solid #efe8d9; }
    .target-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 16px; margin-bottom: 14px; }
    .target-meta { display: flex; align-items: center; gap: 9px; flex-wrap: wrap; margin-bottom: 12px; }
    .state-chip { display: inline-flex; border: 1px solid var(--blue-border); border-radius: 5px; background: var(--blue-soft); color: var(--blue); padding: 4px 9px; font: 600 10.5px/1 var(--mono); letter-spacing: .08em; text-transform: uppercase; }
    .state-chip.primary { border-color: var(--green-border); background: #e4f4eb; color: var(--green); }
    .unit { color: #a49b89; font: 500 11.5px/1 var(--mono); }
    .score { margin-left: auto; color: var(--blue); font-family: var(--display); font-size: 32px; font-weight: 700; line-height: 1; }
    .phase-view[data-phase-view="after"] .score { color: var(--green); }
    .scorebar { height: 8px; width: 100%; border-radius: 5px; overflow: hidden; background: #efe8d9; }
    .scorebar i { display: block; height: 100%; border-radius: 5px; background: var(--blue); transition: width .25s ease; }
    .scorebar i.promoted { background: var(--green); }
    .score-note { display: flex; justify-content: space-between; gap: 10px; margin-top: 6px; color: #a49b89; font: 500 10.5px/1.35 var(--mono); }
    .block-row, .provider-block { padding: 18px 24px; border-bottom: 1px solid #efe8d9; }
    .block-row { background: #fdfbf6; }
    .row-label { margin-bottom: 10px; color: #a49b89; font: 600 11px/1 var(--mono); letter-spacing: .06em; text-transform: uppercase; }
    .reason-badge { display: inline-flex; max-width: 100%; border: 1px solid #f0dcc2; border-radius: 8px; background: var(--warn-soft); color: var(--warn); padding: 6px 12px; font: 500 12px/1.35 var(--mono); overflow-wrap: anywhere; }
    .reason-badge.clear { border-color: var(--green-border); background: var(--green-soft); color: var(--green); }
    .provider-head { display: flex; align-items: center; justify-content: space-between; gap: 12px; margin-bottom: 12px; color: #a49b89; font: 600 11px/1 var(--mono); letter-spacing: .06em; text-transform: uppercase; }
    .provider-head span { color: var(--muted); letter-spacing: 0; text-transform: none; }
    .positions { display: flex; flex-direction: column; gap: 8px; }
    .position { display: flex; align-items: center; gap: 11px; min-width: 0; border: 1px solid #ece5d6; border-radius: 9px; background: #faf7f0; padding: 9px 12px; }
    .position.claimed { border-color: var(--blue-border); background: var(--blue-soft); }
    .position .marker { width: 9px; height: 9px; border-radius: 50%; flex: none; background: #cfc7b6; }
    .position.claimed .marker { background: var(--blue); transform: scale(1.15); }
    .phase-view[data-phase-view="after"] .position.claimed .marker { background: var(--green); }
    .position b { min-width: 0; color: var(--ink-2); font: 600 12.5px/1.25 var(--mono); overflow-wrap: anywhere; }
    .position small { margin-left: auto; color: #a49b89; font: 600 11px/1 var(--mono); }
    .position.claimed small { color: var(--blue); }
    .phase-view[data-phase-view="after"] .position.claimed small { color: var(--green); }
    .gate-box { padding: 18px 24px; background: linear-gradient(180deg,#f6f1e8,#fff); }
    .gate-box.promoted { background: linear-gradient(180deg,#eef7f1,#fff); }
    .gate-line { display: flex; align-items: center; gap: 11px; min-width: 0; }
    .gate-icon { width: 36px; height: 36px; flex: none; border: 1px solid #e6dcc6; border-radius: 9px; background: #fff; display: grid; place-items: center; color: var(--gold); font: 600 12px/1 var(--mono); }
    .gate-box.promoted .gate-icon { border-color: var(--green-border); color: var(--green); }
    .gate-line strong { display: block; font-family: var(--display); font-size: 14px; color: var(--ink); }
    .gate-line p { margin-top: 2px; color: var(--muted); font-size: 11.5px; }
    .gate-tag { margin-left: auto; border-radius: 6px; background: var(--gold-soft); color: var(--gold); padding: 4px 9px; font: 600 10px/1 var(--mono); }
    .gate-box.promoted .gate-tag { background: #e4f4eb; color: var(--green); }
    .run-result { margin-top: 18px; margin-bottom: 18px; border: 1px solid var(--border-2); border-radius: 10px; background: var(--surface); padding: 14px 16px; color: var(--ink-3); font-size: 12.5px; line-height: 1.55; overflow-wrap: anywhere; }
    .run-result b { color: var(--ink); }
    .run-result code { color: var(--green); font-weight: 700; }
    .run-result[hidden] { display: none; }
    .ledger-section, .control-section, .trace-section, .verification-section { padding-top: 44px; padding-bottom: 8px; }
    .ledger-card { margin-top: 16px; overflow: hidden; box-shadow: none; }
    .ledger-head, .ledger-row { display: grid; grid-template-columns: minmax(180px, 1.2fr) minmax(0, 1fr) minmax(0, 1fr); }
    .ledger-head { background: #efe9db; }
    .ledger-head div { color: var(--muted); font: 600 11px/1 var(--mono); letter-spacing: .06em; text-transform: uppercase; }
    .ledger-head div, .ledger-row div { padding: 13px 18px; border-bottom: 1px solid #efe8d9; min-width: 0; }
    .ledger-row:nth-child(odd) { background: #fdfbf6; }
    .ledger-row:last-child div { border-bottom: 0; }
    .ledger-row b { color: var(--ink-3); font: 500 12.5px/1.4 var(--mono); }
    .ledger-row code { color: var(--muted); font-size: 13.5px; }
    .ledger-row .after { background: #f5faf6; }
    .ledger-row .after code { color: var(--green); font-weight: 700; }
    .provider-grid { display: grid; grid-template-columns: repeat(5, minmax(0, 1fr)); gap: 12px; margin-top: 22px; }
    .provider-card { padding: 16px; box-shadow: none; background: var(--surface); }
    .provider-card.baseline { border-color: var(--blue-border); background: var(--blue-soft); }
    .provider-card h3 { display: flex; align-items: center; justify-content: space-between; gap: 8px; margin: 0 0 8px; font-size: 13.5px; }
    .provider-card span { border: 1px solid #e6dcc6; border-radius: 5px; background: var(--gold-soft); color: var(--ink-3); padding: 3px 7px; font-size: 10px; font-weight: 600; }
    .provider-card.baseline span { border-color: var(--blue-border); background: #e3ebf7; color: var(--blue); }
    .provider-card code { display: block; color: var(--muted); font-size: 11px; line-height: 1.45; }
    .source-trace { padding: 22px; box-shadow: none; }
    .source-trace h2 { margin-top: 8px; font-family: var(--serif); font-size: 28px; font-weight: 500; }
    .source-trace p { margin-top: 8px; font-size: 14px; }
    .trace-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 12px; margin-top: 18px; }
    .trace-cell { border: 1px solid var(--border); border-radius: 10px; background: var(--bg-2); padding: 14px; min-width: 0; }
    .trace-cell label { display: block; margin-bottom: 7px; color: var(--muted); font: 600 11px/1 var(--mono); letter-spacing: .06em; text-transform: uppercase; }
    .trace-cell strong { display: block; color: var(--ink); font-size: 13.5px; overflow-wrap: anywhere; }
    .trace-cell p { margin-top: 5px; font-size: 12.5px; }
    .verification-card {
      display: flex;
      align-items: center;
      gap: 26px;
      flex-wrap: wrap;
      border-radius: 14px;
      background: var(--dark);
      padding: 26px 28px;
    }
    .verification-card .kicker { color: #a89f8d; }
    .verification-card code { color: var(--bg); font-size: 13px; line-height: 1.6; }
    .verify-stat { min-width: 150px; display: flex; flex-direction: column; gap: 4px; }
    .verify-stat strong { color: var(--bg); font-family: var(--display); font-size: 16px; }
    .verify-stat span { color: #a89f8d; font-size: 12px; }
    .verify-stat.green strong { color: #7fd0a0; }
    .footer { margin-top: 44px; background: var(--dark); }
    .footer-inner { display: flex; align-items: center; justify-content: space-between; gap: 16px; flex-wrap: wrap; padding-top: 36px; padding-bottom: 36px; }
    .footer .brand { color: var(--bg); }
    .footer .brand-mark { background: var(--bg); color: var(--dark); }
    .footer .actions { justify-content: flex-end; }
    .footer .button { border-color: rgba(244,242,236,.2); background: transparent; color: #c9c2b3; }
    .footer .button:hover { color: var(--bg); border-color: rgba(244,242,236,.4); }
    .foot-id { color: var(--ink-3); font: 500 11px/1.4 var(--mono); }
    @media (max-width: 1040px) {
      .stage { grid-template-columns: 1fr; }
      .provider-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
      .delta-strip { grid-template-columns: repeat(2, minmax(0, 1fr)); }
    }
    @media (max-width: 760px) {
      .wrap { padding-left: 20px; padding-right: 20px; }
      .nav-inner, .section-head { align-items: flex-start; flex-direction: column; }
      .controls { align-items: stretch; width: 100%; }
      .segments, .actions { width: 100%; }
      .segments button, .actions button { flex: 1; }
      .target-head, .gate-line { align-items: flex-start; flex-direction: column; }
      .score, .gate-tag { margin-left: 0; }
      .ledger-card { overflow-x: auto; }
      .ledger-head, .ledger-row { min-width: 720px; }
      .provider-grid, .trace-grid, .delta-strip { grid-template-columns: 1fr; }
    }
    @media (max-width: 520px) {
      h1 { font-size: 40px; }
      h2 { font-size: 28px; }
      .theater { padding-top: 34px; padding-bottom: 34px; }
    }
    """
    script = f"""
    (() => {{
      const demoId = {json.dumps(str(payload.get("demo_id") or demo_id))};
      const buttons = [...document.querySelectorAll("[data-phase-button]")];
      const views = [...document.querySelectorAll("[data-phase-view]")];
      const bundle = document.querySelector("[data-bundle-card]");
      const chip = document.querySelector("[data-phase-chip]");
      const playButton = document.querySelector("[data-play-rescore]");
      const runButton = document.querySelector("[data-run-rescore]");
      const liveButton = document.querySelector("[data-run-live-rescore]");
      const runResult = document.querySelector("[data-run-rescore-result]");
      let ownerAccess = false;
      const esc = (value) => {{
        const node = document.createElement("span");
        node.textContent = String(value ?? "");
        return node.innerHTML;
      }};
      const createRunId = () => {{
        if (globalThis.crypto && typeof globalThis.crypto.randomUUID === "function") {{
          return `fixed-rescore-${{globalThis.crypto.randomUUID().replace(/-/g, "").slice(0, 12)}}`;
        }}
        return `fixed-rescore-${{Date.now().toString(36)}}${{Math.random().toString(16).slice(2, 10)}}`;
      }};
      const createLiveRunId = () => createRunId().replace("fixed-rescore", "live-rescore");
      const setPhase = (phase) => {{
        buttons.forEach((button) => button.classList.toggle("active", button.dataset.phaseButton === phase));
        views.forEach((view) => {{ view.hidden = view.dataset.phaseView !== phase; }});
        if (bundle) bundle.classList.toggle("active", phase === "after");
        if (chip) chip.textContent = phase === "after" ? "evidence_collected" : "needs_more_data";
      }};
      buttons.forEach((button) => button.addEventListener("click", () => setPhase(button.dataset.phaseButton || "before")));
      if (playButton) {{
        playButton.addEventListener("click", () => {{
          playButton.disabled = true;
          setPhase("before");
          window.setTimeout(() => {{
            setPhase("after");
            playButton.disabled = false;
          }}, 650);
        }});
      }}
      const showOwnerControls = () => {{
        ownerAccess = true;
        if (liveButton) liveButton.hidden = false;
      }};
      const activateOwnerSessionFromHash = async () => {{
        const hash = String(window.location.hash || "").replace(/^#/, "");
        if (!hash) return;
        const params = new URLSearchParams(hash);
        const token = params.get("owner_token") || params.get("owner-token");
        if (!token) return;
        window.history.replaceState(null, "", window.location.pathname + window.location.search);
        try {{
          const response = await fetch("/public/fast-gcp-review/owner-session", {{
            method: "POST",
            headers: {{ "content-type": "application/json", "accept": "application/json" }},
            body: JSON.stringify({{ owner_token: token }})
          }});
          const payload = await response.json();
          if (response.ok && payload.owner_access) showOwnerControls();
        }} catch (error) {{}}
      }};
      const refreshOwnerSession = async () => {{
        try {{
          const response = await fetch("/public/fast-gcp-review/owner-session", {{
            headers: {{ "accept": "application/json" }}
          }});
          const payload = await response.json();
          if (response.ok && payload.owner_access) showOwnerControls();
        }} catch (error) {{}}
      }};
      const runRescore = async (liveModel) => {{
        if (!runResult) return;
        if (liveModel && !ownerAccess) {{
          runResult.hidden = false;
          runResult.textContent = "Owner access is required for live model rescore.";
          return;
        }}
        if (runButton) runButton.disabled = true;
        if (liveButton) liveButton.disabled = true;
        if (playButton) playButton.disabled = true;
        runResult.hidden = false;
        runResult.textContent = liveModel ? "Running live model rescore from sanitized child evidence..." : "Running fixed rescore from sanitized child evidence...";
        try {{
          const response = await fetch("/public/rescore-demo/run", {{
            method: "POST",
            headers: {{ "content-type": "application/json", "accept": "application/json" }},
            body: JSON.stringify({{ demo_id: demoId, run_id: liveModel ? createLiveRunId() : createRunId(), live_model: liveModel }})
          }});
          const result = await response.json();
          if (!response.ok) {{
            const detail = result.detail && typeof result.detail === "object" ? result.detail.message : result.detail;
            throw new Error(detail || JSON.stringify(result));
          }}
          setPhase("after");
          const providers = result.providers || {{}};
          const providerLine = liveModel ? `<br><b>Providers</b> ${{esc(providers.success ?? 0)}}/${{esc(providers.total ?? 0)}} schema-valid` : "";
          runResult.innerHTML = `<b>${{liveModel ? "Live model rescore" : "Fixed rescore"}} completed.</b> ${{esc(result.timing.wall_seconds)}}s, model API called: <code>${{esc(result.model_api_called)}}</code>${{providerLine}}<br><b>Transition</b> ${{esc(result.transition.status)}}<br><b>Before</b> primary ${{esc(result.before.primary_count)}} / validation ${{esc(result.before.validation_count)}}<br><b>After</b> primary ${{esc(result.after.primary_count)}} / validation ${{esc(result.after.validation_count)}}<br><b>Child bundle</b> <code>${{esc(result.child.evidence_sha256)}}</code>`;
        }} catch (error) {{
          runResult.textContent = (liveModel ? "Live model rescore failed: " : "Fixed rescore failed: ") + error.message;
        }} finally {{
          if (runButton) runButton.disabled = false;
          if (liveButton) liveButton.disabled = false;
          if (playButton) playButton.disabled = false;
        }}
      }};
      if (runButton) runButton.addEventListener("click", () => runRescore(false));
      if (liveButton) liveButton.addEventListener("click", () => runRescore(true));
      activateOwnerSessionFromHash().then(refreshOwnerSession);
      setPhase("before");
    }})();
    """
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>More data rescore demo</title>
  <style>{style}</style>
</head>
<body>
  <div class="page">
    <nav class="nav" aria-label="Primary">
      <div class="wrap nav-inner">
        <div class="crumbs">
          <a class="brand" href="/"><span class="brand-mark">OE</span><span>Ops Evidence</span></a>
          <span class="crumb-sep">/</span>
          <a href="/#review-set">Reviews</a>
          <span class="crumb-sep">/</span>
          <a href="{_html(source_review_url)}">notifier restart loop</a>
          <span class="crumb-sep">/</span>
          <span>Rescore Loop</span>
        </div>
        <div class="nav-actions">
          <span class="live-chip"><i></i>{_html(public_mode)}</span>
          <a href="{_html(_public_repo_url())}">GitHub</a>
          <span class="soft-chip">{_html(raw_policy)}</span>
        </div>
      </div>
    </nav>

    <header class="wrap hero">
      <div class="hero-kickers">
        <div class="kicker">Read-only DevOps loop</div>
        <span class="soft-chip">{_html(str(payload.get("demo_id") or demo_id))}</span>
      </div>
      <h1>More data changed the promotion decision.</h1>
      <p class="hero-copy">A child Evidence Bundle adds user-impact proof to the same target graph. The persisted demo shows <code>{_html(status_transition)}</code> and a human-gated promotion from <code>{_html(str(before.get("state") or ""))}</code> to <code>{_html(str(after.get("state") or ""))}</code> without exposing raw logs.</p>
      <a class="source-link" href="{_html(source_review_url)}">Source review - {_html(_short_sha(source_evidence_sha))}</a>
      <div class="delta-strip" aria-label="Before and after rescore summary">
        <article class="delta-cell"><span>Before score</span><strong>{before_score:.2f}</strong><em>{_html(str(before.get("state") or ""))}</em></article>
        <article class="delta-cell"><span>After score</span><strong>{after_score:.2f}</strong><em>{_html(str(after.get("state") or ""))}</em></article>
        <article class="delta-cell"><span>Provider stance</span><strong>{_html(before_stance)}</strong><em>{_html(after_stance)}</em></article>
        <article class="delta-cell"><span>Blocking reason</span><strong>{_html(before_reasons)}</strong><em>{_html(after_reasons)}</em></article>
      </div>
    </header>

    <section class="theater">
      <div class="wrap">
        <div class="section-head">
          <div>
            <div class="kicker">Rescore theater</div>
            <h2>{_html(status_transition)}</h2>
            <p>The target below promotes on its own evidence after the child bundle lands. Review priority is not truth probability; it is a queueing signal for human-gated operations work. This public action does not call model APIs.</p>
          </div>
          <div class="controls">
            <div class="segments" role="tablist" aria-label="Rescore phase">
              <button type="button" class="active" data-phase-button="before">Before</button>
              <button type="button" data-phase-button="after">After</button>
            </div>
            <div class="actions">
              <button type="button" data-play-rescore>Play Rescore animation</button>
              <button class="primary" type="button" data-run-rescore>Run Fixed Rescore</button>
              <button type="button" data-run-live-rescore hidden>Run Live Model Rescore</button>
            </div>
          </div>
        </div>
        <div class="run-result" data-run-rescore-result hidden></div>
        <div class="stage">
          <aside class="bundle-card" data-bundle-card>
            <div class="bundle-top">
              <span class="mono">{_html(child_evidence_sha or "public-demo-child-user-impact")}</span>
              <span class="phase-chip" data-phase-chip>needs_more_data</span>
            </div>
            <h3>Child Evidence Bundle</h3>
            <p>Adds <b>{added_refs} evidence refs</b> and <b>{added_logs} log rows</b> tying the restart loop to user-visible notification impact.</p>
            <div class="evidence-list">{row_html}</div>
            <div class="feed-line"><b>-&gt;</b><span>Feeds the same target evidence graph and triggers a re-score.</span></div>
            <div class="feed-line"><code>{_html(status_transition)}</code></div>
          </aside>

          <article class="target-card">
            <div class="phase-view" data-phase-view="before">
              <div class="target-accent"></div>
              <div class="target-main">
                <div class="target-head">
                  <div>
                    <div class="target-meta"><span class="state-chip">{_html(str(before.get("state") or ""))}</span><span class="unit">subsystem notifier</span></div>
                    <h3>{_html(before_title)}</h3>
                  </div>
                  <div class="score">{before_score:.2f}</div>
                </div>
                <div class="scorebar"><i style="width: {before_width:.1f}%"></i></div>
                <div class="score-note"><span>promotion score</span><span>priority != truth probability</span></div>
              </div>
              <div class="block-row">
                <div class="row-label">Blocking reasons</div>
                <span class="reason-badge">{_html(before_reasons)}</span>
              </div>
              <div class="provider-block">
                <div class="provider-head">Provider positions <span>{_html(before_stance)}</span></div>
                <div class="positions">{before_provider_positions}</div>
              </div>
              <div class="gate-box">
                <div class="gate-line">
                  <span class="gate-icon">HG</span>
                  <div><strong>Promotion blocked - needs more data</strong><p>Missing user-impact evidence blocks promotion.</p></div>
                  <span class="gate-tag">BLOCKED</span>
                </div>
              </div>
            </div>

            <div class="phase-view" data-phase-view="after" hidden>
              <div class="target-accent"></div>
              <div class="target-main">
                <div class="target-head">
                  <div>
                    <div class="target-meta"><span class="state-chip primary">{_html(str(after.get("state") or ""))}</span><span class="unit">subsystem notifier</span></div>
                    <h3>{_html(after_title)}</h3>
                  </div>
                  <div class="score">{after_score:.2f}</div>
                </div>
                <div class="scorebar"><i class="promoted" style="width: {after_width:.1f}%"></i></div>
                <div class="score-note"><span>promotion score</span><span>priority != truth probability</span></div>
              </div>
              <div class="block-row">
                <div class="row-label">Blocking reasons</div>
                <span class="reason-badge clear">{_html(after_reasons)}</span>
              </div>
              <div class="provider-block">
                <div class="provider-head">Provider positions <span>{_html(after_stance)}</span></div>
                <div class="positions">{after_provider_positions}</div>
              </div>
              <div class="gate-box promoted">
                <div class="gate-line">
                  <span class="gate-icon">HG</span>
                  <div><strong>Primary promotion gate closed</strong><p>Impact evidence is attached; human sign-off still owns action.</p></div>
                  <span class="gate-tag">UNBLOCKED</span>
                </div>
              </div>
            </div>
          </article>
        </div>
      </div>
    </section>

    <section class="wrap ledger-section">
      <div class="kicker">State transition - before -> after</div>
      <div class="ledger-card">
        <div class="ledger-head"><div>Field</div><div>Before child evidence</div><div>After re-score</div></div>
        {ledger_html}
      </div>
    </section>

    <section class="wrap control-section">
      <div class="kicker">Gemini-led control plane</div>
      <h2>Gemini is the baseline. The rest cross-check it.</h2>
      <p class="hero-copy">{_html(str(control.get("policy") or ""))} Their silence is preserved as validation signal, never dropped.</p>
      <div class="provider-grid">{provider_cards_html}</div>
    </section>

    <section class="wrap trace-section">
      {source_trace_html}
    </section>

    <section class="wrap verification-section">
      <div class="verification-card">
        <div style="flex:2; min-width:280px;">
          <div class="kicker">Verification</div>
          <code>{_html(local_test)}</code>
        </div>
        <div class="verify-stat"><strong>{_html(public_mode)}</strong><span>public mode - no model runs from the URL</span></div>
        <div class="verify-stat green"><strong>{_html(raw_policy)}</strong><span>raw logs stay local</span></div>
      </div>
    </section>

    <footer class="footer">
      <div class="wrap footer-inner">
        <a class="brand" href="/"><span class="brand-mark">OE</span><span>Ops Evidence Synthesis</span></a>
        <div class="actions">{action_links}</div>
        <span class="foot-id">{_html(str(payload.get("demo_id") or demo_id))}</span>
      </div>
    </footer>
  </div>
  <script>{script}</script>
</body>
</html>"""


def _render_rescore_demo_page_legacy(demo_id: str) -> str:
    payload = _rescore_demo_payload(demo_id)
    if not payload:
        return ""
    before = payload.get("before") if isinstance(payload.get("before"), dict) else {}
    loop = payload.get("more_data_loop") if isinstance(payload.get("more_data_loop"), dict) else {}
    after = payload.get("after") if isinstance(payload.get("after"), dict) else {}
    control = payload.get("control_plane") if isinstance(payload.get("control_plane"), dict) else {}
    verification = payload.get("verification") if isinstance(payload.get("verification"), dict) else {}
    rows = loop.get("collected_rows") if isinstance(loop.get("collected_rows"), list) else []
    row_html = "".join(_rescore_evidence_row_html(row) for row in rows if isinstance(row, dict))
    providers = control.get("cross_check_providers") if isinstance(control.get("cross_check_providers"), list) else []
    provider_text = ", ".join(str(item) for item in providers if str(item))
    before_reasons = ", ".join(str(item) for item in before.get("blocked_reasons") or []) or "none"
    after_reasons = ", ".join(str(item) for item in after.get("blocked_reasons") or []) or "none"
    before_provider_positions = _rescore_provider_positions_html(before.get("provider_positions"))
    after_provider_positions = _rescore_provider_positions_html(after.get("provider_positions"))
    before_stance = _rescore_provider_stance_label(before.get("provider_positions"))
    after_stance = _rescore_provider_stance_label(after.get("provider_positions"))
    source_evidence_sha = str(payload.get("source_evidence_sha256") or "")
    action_links = _public_action_links_html(source_evidence_sha) if source_evidence_sha else ""
    source_trace_html = _rescore_source_trace_html(payload)
    source_review_url = str(payload.get("source_review_url") or "#")
    before_score = float(before.get("promotion_score") or 0)
    after_score = float(after.get("promotion_score") or 0)
    before_width = max(0, min(100, before_score * 100))
    after_width = max(0, min(100, after_score * 100))
    status_transition = str(loop.get("status_transition") or "needs_more_data -> evidence_collected")
    child_evidence_sha = str(loop.get("child_evidence_sha256") or "")
    ledger_html = "\n".join(
        _rescore_ledger_row_html(field, old, new)
        for field, old, new in [
            ("State", str(before.get("state") or ""), str(after.get("state") or "")),
            ("Promotion score", f"{before_score:.2f}", f"{after_score:.2f}"),
            ("Blocked reasons", before_reasons, after_reasons),
            ("Provider stance", before_stance, after_stance),
            ("Decision", "needs_more_data", "evidence_collected"),
        ]
    )
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>More data rescore demo</title>
  <style>
    :root {{
      color-scheme: light;
      --bg: #eef2f7;
      --surface: #ffffff;
      --surface-2: #f6f9fd;
      --line: #d9e1ec;
      --ink: #0f1b2d;
      --ink-2: #51617a;
      --ink-3: #8a97ab;
      --accent: #2a6fdb;
      --accent-soft: #e7f0fc;
      --claimed: #12836b;
      --claimed-soft: #e1f1ec;
      --silent: #a2aebf;
      --amber: #b26a00;
      --amber-soft: #f8ecd6;
      --shadow: 0 1px 2px rgba(16, 27, 45, .05), 0 18px 50px -22px rgba(16, 27, 45, .28);
      --mono: ui-monospace, SFMono-Regular, Menlo, Consolas, "Liberation Mono", monospace;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      min-height: 100vh;
      background: var(--bg);
      color: var(--ink);
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      letter-spacing: 0;
    }}
    a {{ color: inherit; text-decoration: none; }}
    p {{ margin: 0; color: var(--ink-2); line-height: 1.55; }}
    code {{ font-family: var(--mono); overflow-wrap: anywhere; }}
    .shell {{ width: min(calc(100% - 48px), 1720px); margin: 0 auto; padding: 0 0 56px; }}
    .topbar {{
      min-height: 70px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 18px;
      border-bottom: 1px solid var(--line);
    }}
    .brand {{ display: flex; align-items: center; gap: 12px; min-width: 230px; }}
    .brand-mark {{
      width: 34px;
      height: 34px;
      border-radius: 8px;
      display: grid;
      place-items: center;
      background: var(--accent);
      color: #fff;
      font: 800 13px/1 var(--mono);
    }}
    .brand-name {{ font-weight: 800; }}
    .chips {{ display: flex; align-items: center; flex-wrap: wrap; gap: 8px; justify-content: flex-end; }}
    .chip {{
      display: inline-flex;
      align-items: center;
      gap: 7px;
      border: 1px solid var(--line);
      border-radius: 999px;
      padding: 7px 11px;
      background: rgba(255,255,255,.74);
      color: var(--ink-2);
      font-size: 12px;
      font-weight: 750;
    }}
    .dot {{ width: 7px; height: 7px; border-radius: 50%; background: var(--claimed); }}
    .hero {{
      display: grid;
      grid-template-columns: minmax(620px, 1.05fr) minmax(460px, .85fr);
      gap: clamp(28px, 4vw, 72px);
      align-items: center;
      padding: 44px 0 30px;
    }}
    .kicker {{ color: var(--accent); font: 800 11px/1 var(--mono); letter-spacing: .08em; text-transform: uppercase; }}
    h1 {{
      max-width: 960px;
      margin: 14px 0 0;
      font-size: clamp(42px, 3.8vw, 64px);
      line-height: 1.02;
      letter-spacing: 0;
      overflow-wrap: anywhere;
    }}
    h2 {{ margin: 10px 0 0; font-size: 24px; letter-spacing: 0; overflow-wrap: anywhere; }}
    h3 {{ margin: 0; font-size: 20px; letter-spacing: 0; line-height: 1.25; overflow-wrap: anywhere; }}
    .hero p {{ max-width: 820px; margin-top: 16px; font-size: 16px; }}
    .source-link {{ display: inline-flex; align-items: center; flex-wrap: wrap; max-width: 100%; gap: 8px; margin-top: 20px; color: var(--accent); font-size: 13px; font-weight: 800; overflow-wrap: anywhere; }}
    .source-trace h2 {{ font-size: clamp(20px, 1.5vw, 24px); }}
    .control-plane, .target-card, .bundle-card, .ledger-card, .verify-card {{
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--surface);
      box-shadow: var(--shadow);
    }}
    .control-plane {{
      display: grid;
      grid-template-columns: 42px minmax(0, 1fr);
      gap: 16px;
      align-items: center;
      padding: 18px 22px;
    }}
    .control-icon {{
      width: 40px;
      height: 40px;
      border-radius: 10px;
      display: grid;
      place-items: center;
      background: var(--accent-soft);
    }}
    .control-icon span {{ width: 14px; height: 14px; border-radius: 50%; background: var(--accent); }}
    .control-plane strong {{ display: block; font-size: 14.5px; }}
    .control-plane p {{ margin-top: 3px; font-size: 12.5px; }}
    .console-head {{
      display: flex;
      align-items: flex-end;
      justify-content: space-between;
      gap: 20px;
      margin-top: 40px;
      flex-wrap: wrap;
    }}
    .segments {{
      display: flex;
      gap: 4px;
      padding: 4px;
      border: 1px solid var(--line);
      border-radius: 12px;
      background: var(--surface-2);
    }}
    .segments button {{
      border: 0;
      border-radius: 9px;
      padding: 8px 16px;
      background: transparent;
      color: var(--ink-3);
      font: 800 12.5px/1 Inter, ui-sans-serif, system-ui, sans-serif;
      cursor: pointer;
    }}
    .segments button.active {{ background: var(--surface); color: var(--ink); box-shadow: 0 1px 3px rgba(16,27,45,.12); }}
    .rescore-grid {{
      display: grid;
      grid-template-columns: minmax(640px, 1.2fr) minmax(380px, .8fr);
      gap: 20px;
      margin-top: 22px;
      align-items: start;
    }}
    .hero > *, .rescore-grid > * {{ min-width: 0; }}
    .target-card {{ padding: 28px; }}
    .phase-view[hidden] {{ display: none; }}
    .target-head {{ display: flex; justify-content: space-between; gap: 16px; align-items: start; }}
    .state-chip {{
      display: inline-flex;
      border-radius: 999px;
      padding: 5px 10px;
      font-size: 11px;
      font-weight: 850;
      letter-spacing: .02em;
      background: var(--surface-2);
      color: var(--ink-2);
    }}
    .state-chip.primary {{ background: var(--amber-soft); color: var(--amber); }}
    .unit {{ margin-left: 8px; color: var(--ink-3); font: 700 11.5px/1 var(--mono); }}
    .score {{ text-align: right; flex: none; }}
    .score strong {{ display: block; font-size: 38px; line-height: 1; letter-spacing: 0; }}
    .score span {{ display: block; margin-top: 4px; color: var(--ink-3); font-size: 10.5px; }}
    .scorebar {{ height: 12px; border-radius: 6px; background: var(--line); overflow: hidden; margin-top: 20px; }}
    .scorebar i {{ display: block; height: 100%; border-radius: inherit; background: var(--accent); }}
    .scorebar i.promoted {{ background: var(--claimed); }}
    .note {{ margin-top: 7px; color: var(--ink-3); font-size: 11px; }}
    .provider-block {{ margin-top: 20px; padding-top: 18px; border-top: 1px solid var(--line); }}
    .provider-head {{ display: flex; justify-content: space-between; gap: 12px; margin-bottom: 12px; }}
    .provider-head b {{ font-size: 12px; }}
    .provider-head span {{ color: var(--ink-2); font: 700 12px/1 var(--mono); }}
    .positions {{ display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 8px; }}
    .position {{
      display: flex;
      align-items: center;
      gap: 10px;
      min-width: 0;
      padding: 9px 12px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--surface-2);
    }}
    .position .marker {{ width: 9px; height: 9px; border-radius: 50%; flex: none; background: var(--silent); }}
    .position.claimed .marker {{ background: var(--claimed); }}
    .position b {{ min-width: 0; color: var(--ink); font-size: 12px; overflow-wrap: anywhere; }}
    .position small {{ margin-left: auto; color: var(--ink-3); font-size: 11px; font-weight: 800; }}
    .position.claimed small {{ color: var(--claimed); }}
    .gate-box {{
      display: grid;
      grid-template-columns: 34px minmax(0, 1fr);
      gap: 12px;
      align-items: center;
      margin-top: 20px;
      padding: 16px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--amber-soft);
    }}
    .gate-box.promoted {{ background: var(--claimed-soft); }}
    .gate-icon {{
      width: 34px;
      height: 34px;
      display: grid;
      place-items: center;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--surface);
      font-weight: 900;
    }}
    .gate-box b {{ color: var(--amber); font-size: 13px; letter-spacing: .02em; }}
    .gate-box.promoted b {{ color: var(--claimed); }}
    .gate-box p {{ margin-top: 3px; font-size: 12px; }}
    .bundle-card {{ padding: 24px; }}
    .bundle-card.active {{ border-color: var(--claimed); box-shadow: 0 18px 50px -22px rgba(18,131,107,.4); }}
    .bundle-top {{ display: flex; justify-content: space-between; gap: 12px; align-items: center; }}
    .mono {{ color: var(--ink-2); font: 700 11.5px/1 var(--mono); overflow-wrap: anywhere; }}
    .phase-chip {{ border-radius: 999px; padding: 5px 10px; background: var(--amber-soft); color: var(--amber); font-size: 11px; font-weight: 850; }}
    .bundle-card.active .phase-chip {{ background: var(--claimed-soft); color: var(--claimed); }}
    .bundle-card h3 {{ margin-top: 14px; font-size: 16px; }}
    .bundle-card p {{ margin-top: 5px; font-size: 12.5px; }}
    .evidence-list {{ display: grid; gap: 10px; margin-top: 18px; }}
    .evidence-row {{
      padding: 13px 15px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--surface-2);
    }}
    .bundle-card.active .evidence-row {{ background: var(--claimed-soft); }}
    .evidence-row div {{ display: flex; align-items: center; justify-content: space-between; gap: 10px; }}
    .evidence-row time {{ color: var(--ink-3); font: 700 11px/1 var(--mono); }}
    .evidence-row span {{ border-radius: 999px; padding: 4px 10px; background: var(--surface); color: var(--claimed); font-size: 11px; font-weight: 800; }}
    .evidence-row p {{ margin-top: 7px; color: var(--ink); font-size: 12.5px; }}
    .transition {{ display: flex; gap: 8px; align-items: center; margin-top: 18px; padding-top: 16px; border-top: 1px solid var(--line); font: 800 12px/1 var(--mono); }}
    .transition span:first-child {{ color: var(--ink-3); }}
    .transition span:nth-child(2) {{ color: var(--accent); }}
    .transition span:last-child {{ color: var(--claimed); }}
    .source-trace {{ margin-top: 18px; }}
    .trace-grid {{ display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 10px; margin-top: 10px; }}
    .trace-cell {{
      border: 1px solid var(--line);
      border-radius: 8px;
      background: rgba(255,255,255,.72);
      padding: 12px;
      min-width: 0;
    }}
    .trace-cell label, .ledger-head div {{
      display: block;
      color: var(--ink-3);
      font: 800 10.5px/1 var(--mono);
      letter-spacing: .05em;
      text-transform: uppercase;
      margin-bottom: 7px;
    }}
    .trace-cell strong {{ display: block; color: var(--ink); font-size: 13px; overflow-wrap: anywhere; }}
    .trace-cell p {{ margin-top: 5px; font-size: 12px; }}
    .ledger-section {{ padding-top: 46px; }}
    .ledger-card {{ overflow: hidden; margin-top: 18px; }}
    .ledger-head, .ledger-row {{ display: grid; grid-template-columns: minmax(180px, .7fr) minmax(0, 1fr) minmax(0, 1fr); }}
    .ledger-head {{ background: var(--surface-2); }}
    .ledger-head div, .ledger-row div {{ padding: 14px 20px; border-bottom: 1px solid var(--line); }}
    .ledger-row:last-child div {{ border-bottom: 0; }}
    .ledger-row b {{ color: var(--ink); font-size: 13px; }}
    .ledger-row code {{ color: var(--ink-2); font-size: 12.5px; }}
    .ledger-row .after code {{ color: var(--claimed); font-weight: 800; }}
    .verify-card {{
      display: grid;
      grid-template-columns: 34px minmax(0, 1fr);
      gap: 14px;
      align-items: center;
      margin-top: 32px;
      padding: 20px 22px;
      background: var(--surface-2);
      box-shadow: none;
    }}
    .verify-card b {{ display: block; font-size: 13px; }}
    .verify-card code {{ color: var(--ink-2); font-size: 11.5px; }}
    .footer {{
      display: flex;
      justify-content: space-between;
      gap: 20px;
      flex-wrap: wrap;
      margin-top: 36px;
      padding-top: 24px;
      border-top: 1px solid var(--line);
      color: var(--ink-2);
      font-size: 12.5px;
    }}
    .actions {{ display: flex; flex-wrap: wrap; gap: 8px; }}
    .actions .button, .footer a {{
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--surface);
      padding: 8px 10px;
      color: var(--ink-2);
      font-size: 12px;
      font-weight: 800;
      cursor: pointer;
    }}
    .actions .button.primary {{
      background: var(--accent);
      border-color: var(--accent);
      color: #fff;
    }}
    .actions .button:disabled {{
      opacity: .55;
      cursor: not-allowed;
    }}
    .run-result {{
      margin-top: 14px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--surface-2);
      padding: 14px 16px;
      color: var(--ink-2);
      font-size: 12.5px;
      line-height: 1.55;
      overflow-wrap: anywhere;
    }}
    .run-result b {{ color: var(--ink); }}
    .run-result code {{ color: var(--claimed); font-weight: 800; }}
    .run-result[hidden] {{
      display: none;
    }}
    @media (max-width: 1180px) {{
      .hero, .rescore-grid {{ grid-template-columns: 1fr; }}
    }}
    @media (max-width: 900px) {{
      .shell {{ width: min(calc(100% - 32px), 1720px); }}
      .topbar {{ align-items: flex-start; flex-direction: column; padding: 16px 0; }}
      .hero {{ padding-top: 36px; }}
      .positions, .trace-grid {{ grid-template-columns: 1fr; }}
      .ledger-card {{ overflow-x: auto; }}
      .ledger-head, .ledger-row {{ min-width: 720px; }}
    }}
    @media (max-width: 520px) {{
      h1 {{ font-size: 42px; }}
      .target-head {{ display: grid; }}
      .score {{ text-align: left; }}
      .segments {{ width: 100%; }}
      .segments button {{ flex: 1; padding-inline: 10px; }}
    }}
  </style>
</head>
<body>
  <main class="shell">
    <nav class="topbar" aria-label="Primary">
      <a class="brand" href="/">
        <span class="brand-mark">OE</span>
        <span class="brand-name">Ops Evidence Synthesis</span>
      </a>
      <div class="chips">
        <span class="chip">read_only_precomputed</span>
        <span class="chip"><span class="dot"></span>raw logs {_html(str(verification.get("raw_log_policy") or "not_uploaded"))}</span>
      </div>
    </nav>

    <section class="hero">
      <div>
        <div class="kicker">Read-only DevOps improvement loop</div>
        <h1>More data changed the promotion decision.</h1>
        <p>The AI improvement cycle judges can inspect without starting a single model run. A child Evidence Bundle adds user-impact evidence, and one review target crosses from validation work to a promoted primary candidate. Every step remains auditable.</p>
        <a class="source-link" href="{_html(source_review_url)}">Source review - {_html(_short_sha(source_evidence_sha))}</a>
      </div>
      {source_trace_html}
    </section>

    <section class="control-plane">
      <div class="control-icon"><span></span></div>
      <div>
        <strong>Gemini-led control plane - {_html(str(control.get("primary_provider") or "gemini-enterprise-agent-platform"))}</strong>
        <p>{_html(str(control.get("policy") or ""))} Cross-checks: {_html(provider_text)}.</p>
      </div>
    </section>

    <section>
      <div class="console-head">
        <div>
          <div class="kicker">Rescore console - interactive</div>
          <h2>Toggle the child evidence bundle.</h2>
          <p>Run the fixed sanitized child-bundle rescore in Cloud Run. This public action does not call model APIs.</p>
        </div>
        <div>
          <div class="segments" role="tablist" aria-label="Rescore phase">
            <button type="button" class="active" data-phase-button="before">Before more data</button>
            <button type="button" data-phase-button="after">After more data</button>
          </div>
          <div class="actions" style="justify-content: flex-end; margin-top: 10px;">
            <button class="button primary" type="button" data-run-rescore>Run Fixed Rescore</button>
            <button class="button" type="button" data-run-live-rescore hidden>Run Live Model Rescore</button>
          </div>
        </div>
      </div>
      <div class="run-result" data-run-rescore-result hidden></div>

      <div class="rescore-grid">
        <article class="target-card">
          <div class="phase-view" data-phase-view="before">
            <div class="target-head">
              <div>
                <span class="state-chip">{_html(str(before.get("state") or ""))}</span>
                <span class="unit">notifier_restart_loop</span>
                <h3>{_html(str(before.get("title") or ""))}</h3>
              </div>
              <div class="score"><strong>{before_score:.2f}</strong><span>promotion score</span></div>
            </div>
            <div class="scorebar"><i style="width: {before_width:.0f}%"></i></div>
            <div class="note">Priority is review urgency, not truth probability.</div>
            <div class="provider-block">
              <div class="provider-head"><b>Provider positions</b><span>{_html(before_stance)}</span></div>
              <div class="positions">{before_provider_positions}</div>
            </div>
            <div class="gate-box">
              <div class="gate-icon">HG</div>
              <div><b>HUMAN-GATED - promotion blocked</b><p>Blocked reasons: {_html(before_reasons)}. Missing user-impact evidence blocks promotion.</p></div>
            </div>
          </div>

          <div class="phase-view" data-phase-view="after" hidden>
            <div class="target-head">
              <div>
                <span class="state-chip primary">{_html(str(after.get("state") or ""))}</span>
                <span class="unit">notifier_restart_loop</span>
                <h3>{_html(str(after.get("title") or ""))}</h3>
              </div>
              <div class="score"><strong>{after_score:.2f}</strong><span>promotion score</span></div>
            </div>
            <div class="scorebar"><i class="promoted" style="width: {after_width:.0f}%"></i></div>
            <div class="note">Review priority increased after child evidence. Score is still review urgency, not truth probability.</div>
            <div class="provider-block">
              <div class="provider-head"><b>Provider positions</b><span>{_html(after_stance)}</span></div>
              <div class="positions">{after_provider_positions}</div>
            </div>
            <div class="gate-box promoted">
              <div class="gate-icon">OK</div>
              <div><b>GATE CLOSED - promoted to primary</b><p>Blocked reasons: {_html(after_reasons)}. The primary promotion gate is now closed.</p></div>
            </div>
          </div>
        </article>

        <aside class="bundle-card" data-bundle-card>
          <div class="bundle-top">
            <span class="mono">{_html(child_evidence_sha or "public-demo-child-user-impact")}</span>
            <span class="phase-chip" data-phase-chip>needs_more_data</span>
          </div>
          <h3>Child Evidence Bundle</h3>
          <p>Adds <b>{int(loop.get("added_evidence_ref_count") or 0)} evidence refs</b> and <b>{int(loop.get("added_log_count") or 0)} log rows</b> tying the restart loop to user-visible impact.</p>
          <div class="evidence-list">{row_html}</div>
          <div class="transition" aria-label="{_html(status_transition)}"><span>needs_more_data</span><span>-&gt;</span><span>evidence_collected</span></div>
        </aside>
      </div>
    </section>

    <section class="ledger-section">
      <div class="kicker">State transition ledger</div>
      <h2>Auditable before -> after diff.</h2>
      <div class="ledger-card">
        <div class="ledger-head"><div>Field</div><div>Before</div><div>After</div></div>
        {ledger_html}
      </div>
    </section>

    <section class="verify-card">
      <div class="gate-icon">OK</div>
      <div>
        <b>Covered by an automated test - not a live write path</b>
        <p><code>{_html(str(verification.get("local_test") or ""))}</code></p>
      </div>
    </section>

    <footer class="footer">
      <span>Ops Evidence Synthesis - read-only Cloud Run delivery</span>
      <div class="actions">
        <a class="button" href="/">Summary</a>
        {action_links}
      </div>
    </footer>
  </main>
  <script>
    (() => {{
      const demoId = {json.dumps(str(payload.get("demo_id") or demo_id))};
      const buttons = [...document.querySelectorAll("[data-phase-button]")];
      const views = [...document.querySelectorAll("[data-phase-view]")];
      const bundle = document.querySelector("[data-bundle-card]");
      const chip = document.querySelector("[data-phase-chip]");
      const runButton = document.querySelector("[data-run-rescore]");
      const liveButton = document.querySelector("[data-run-live-rescore]");
      const runResult = document.querySelector("[data-run-rescore-result]");
      let ownerAccess = false;
      const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (ch) => ({{
        "&": "&amp;",
        "<": "&lt;",
        ">": "&gt;",
        '"': "&quot;",
        "'": "&#39;"
      }}[ch]));
      const createRunId = () => {{
        if (globalThis.crypto && typeof globalThis.crypto.randomUUID === "function") {{
          return `fixed-rescore-${{globalThis.crypto.randomUUID().replace(/-/g, "").slice(0, 12)}}`;
        }}
        return `fixed-rescore-${{Date.now().toString(36)}}${{Math.random().toString(16).slice(2, 10)}}`;
      }};
      const createLiveRunId = () => createRunId().replace("fixed-rescore", "live-rescore");
      const setPhase = (phase) => {{
        buttons.forEach((button) => button.classList.toggle("active", button.dataset.phaseButton === phase));
        views.forEach((view) => {{ view.hidden = view.dataset.phaseView !== phase; }});
        if (bundle) bundle.classList.toggle("active", phase === "after");
        if (chip) chip.textContent = phase === "after" ? "evidence_collected" : "needs_more_data";
      }};
      buttons.forEach((button) => button.addEventListener("click", () => setPhase(button.dataset.phaseButton || "before")));
      const showOwnerControls = () => {{
        ownerAccess = true;
        if (liveButton) liveButton.hidden = false;
      }};
      const activateOwnerSessionFromHash = async () => {{
        const hash = String(window.location.hash || "").replace(/^#/, "");
        if (!hash) return;
        const params = new URLSearchParams(hash);
        const token = params.get("owner_token") || params.get("owner-token");
        if (!token) return;
        window.history.replaceState(null, "", window.location.pathname + window.location.search);
        try {{
          const response = await fetch("/public/fast-gcp-review/owner-session", {{
            method: "POST",
            headers: {{ "content-type": "application/json", "accept": "application/json" }},
            body: JSON.stringify({{ owner_token: token }})
          }});
          const payload = await response.json();
          if (response.ok && payload.owner_access) showOwnerControls();
        }} catch (error) {{}}
      }};
      const refreshOwnerSession = async () => {{
        try {{
          const response = await fetch("/public/fast-gcp-review/owner-session", {{
            headers: {{ "accept": "application/json" }}
          }});
          const payload = await response.json();
          if (response.ok && payload.owner_access) showOwnerControls();
        }} catch (error) {{}}
      }};
      const runRescore = async (liveModel) => {{
        if (!runResult) return;
        if (liveModel && !ownerAccess) {{
          runResult.hidden = false;
          runResult.textContent = "Owner access is required for live model rescore.";
          return;
        }}
        const activeButton = liveModel ? liveButton : runButton;
        if (activeButton) activeButton.disabled = true;
        if (runButton) runButton.disabled = true;
        if (liveButton) liveButton.disabled = true;
          runResult.hidden = false;
        runResult.textContent = liveModel ? "Running live model rescore from sanitized child evidence..." : "Running fixed rescore from sanitized child evidence...";
          try {{
            const response = await fetch("/public/rescore-demo/run", {{
              method: "POST",
              headers: {{ "content-type": "application/json", "accept": "application/json" }},
            body: JSON.stringify({{ demo_id: demoId, run_id: liveModel ? createLiveRunId() : createRunId(), live_model: liveModel }})
            }});
            const result = await response.json();
            if (!response.ok) {{
              const detail = result.detail && typeof result.detail === "object" ? result.detail.message : result.detail;
              throw new Error(detail || JSON.stringify(result));
            }}
            setPhase("after");
          const providers = result.providers || {{}};
          const providerLine = liveModel ? `<br><b>Providers</b> ${{esc(providers.success ?? 0)}}/${{esc(providers.total ?? 0)}} schema-valid` : "";
          runResult.innerHTML = `<b>${{liveModel ? "Live model rescore" : "Fixed rescore"}} completed.</b> ${{esc(result.timing.wall_seconds)}}s, model API called: <code>${{esc(result.model_api_called)}}</code>${{providerLine}}<br><b>Transition</b> ${{esc(result.transition.status)}}<br><b>Before</b> primary ${{esc(result.before.primary_count)}} / validation ${{esc(result.before.validation_count)}}<br><b>After</b> primary ${{esc(result.after.primary_count)}} / validation ${{esc(result.after.validation_count)}}<br><b>Child bundle</b> <code>${{esc(result.child.evidence_sha256)}}</code>`;
          }} catch (error) {{
          runResult.textContent = (liveModel ? "Live model rescore failed: " : "Fixed rescore failed: ") + error.message;
          }} finally {{
          if (runButton) runButton.disabled = false;
          if (liveButton) liveButton.disabled = false;
          }}
      }};
      if (runButton) runButton.addEventListener("click", () => runRescore(false));
      if (liveButton) liveButton.addEventListener("click", () => runRescore(true));
      activateOwnerSessionFromHash().then(refreshOwnerSession);
      setPhase("before");
    }})();
  </script>
</body>
</html>"""


def _rescore_control_provider_cards_html(primary_provider: str, providers: list[object]) -> str:
    provider_ids: list[tuple[str, str]] = []
    seen: set[str] = set()
    primary = str(primary_provider or "").strip()
    if primary:
        provider_ids.append((primary, "baseline"))
        seen.add(primary)
    for item in providers:
        provider_id = str(item or "").strip()
        if not provider_id or provider_id in seen:
            continue
        provider_ids.append((provider_id, "cross-check"))
        seen.add(provider_id)
    if not provider_ids:
        provider_ids.append(("gemini-enterprise-agent-platform", "baseline"))
    return "\n".join(
        f"""
        <article class="provider-card {'baseline' if role == 'baseline' else ''}">
          <h3>{_html(_rescore_provider_short_name(provider_id))}<span>{_html(role)}</span></h3>
          <code>{_html(provider_id)}</code>
        </article>
        """
        for provider_id, role in provider_ids
    )


def _rescore_source_trace_html(payload: dict[str, Any]) -> str:
    trace = payload.get("source_trace") if isinstance(payload.get("source_trace"), dict) else {}
    if not trace:
        return ""
    identity = trace.get("before_target_identity") if isinstance(trace.get("before_target_identity"), dict) else {}
    blocked_reasons = ", ".join(str(item) for item in identity.get("blocked_reasons") or []) or "none"
    contained = "yes" if trace.get("current_source_review_contains_before_target") else "no"
    identity_html = ""
    if identity:
        score = identity.get("promotion_score")
        try:
            score_text = f"{float(score):.2f}"
        except (TypeError, ValueError):
            score_text = str(score or "")
        identity_html = f"""
        <div class="trace-grid">
          <article class="trace-cell"><label>Before target</label><strong>{_html(str(identity.get("title") or ""))}</strong><p>{_html(str(identity.get("state") or ""))}</p></article>
          <article class="trace-cell"><label>Stored score</label><strong>{_html(score_text)}</strong><p>{_html(blocked_reasons)}</p></article>
          <article class="trace-cell"><label>Stored stance</label><strong>{_html(str(identity.get("provider_stance") or ""))}</strong><p>Fixture-level trace, not a live write path.</p></article>
        </div>
        """
    return f"""
      <aside class="source-trace">
        <div class="kicker">Source trace</div>
        <h2>{_html(str(trace.get("status") or "recorded"))}</h2>
        <p>Before target present in current source review: {_html(contained)}. {_html(str(trace.get("note") or ""))}</p>
        {identity_html}
      </aside>
    """


def _rescore_evidence_row_html(row: dict[str, Any]) -> str:
    return f"""
      <article class="evidence-row">
        <div>
          <time>{_html(str(row.get("timestamp") or ""))}</time>
          <span>{_html(str(row.get("message_template") or "notification_not_delivered"))}</span>
        </div>
        <p>{_html(str(row.get("summary") or ""))}</p>
      </article>
    """


def _rescore_ledger_row_html(field: str, before: str, after: str) -> str:
    return f"""
      <div class="ledger-row">
        <div><b>{_html(field)}</b></div>
        <div><code>{_html(before)}</code></div>
        <div class="after"><code>{_html(after)}</code></div>
      </div>
    """


def _rescore_provider_stance_label(positions: object) -> str:
    rows = [row for row in positions if isinstance(row, dict)] if isinstance(positions, list) else []
    claimed = sum(1 for row in rows if str(row.get("stance") or "").casefold() == "claimed")
    silent = sum(1 for row in rows if str(row.get("stance") or "").casefold() == "silent")
    if not rows:
        return "not recorded"
    return f"{claimed} claimed - {silent} silent"


def _rescore_provider_short_name(provider_id: str) -> str:
    provider = provider_id.casefold()
    if "gemini" in provider:
        return "Gemini"
    if "gpt-oss" in provider or "openai" in provider:
        return "GPT-OSS"
    if "mistral" in provider:
        return "Mistral"
    if "qwen" in provider:
        return "Qwen"
    if "glm" in provider:
        return "GLM"
    return provider_id[:18] if provider_id else "provider"


def _rescore_provider_positions_html(positions: object) -> str:
    rows = positions if isinstance(positions, list) else []
    cells = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        provider_id = str(row.get("provider_id") or "")
        stance = str(row.get("stance") or "")
        stance_class = "claimed" if stance.casefold() == "claimed" else "silent"
        cells.append(
            f"<article class='position {stance_class}' title='{_html(provider_id)}'>"
            "<span class='marker'></span>"
            f"<b>{_html(_rescore_provider_short_name(provider_id))}</b>"
            f"<small>{_html(stance)}</small>"
            "</article>"
        )
    if not cells:
        return "<article class='position'><b>not recorded</b></article>"
    return "\n".join(cells)


def _html(value: object) -> str:
    import html

    return html.escape(_public_count_text(value), quote=True)


fast_detail_target_card = _fast_detail_target_card
fast_review_shell = _fast_review_shell
canonical_precomputed_review_sha = _canonical_precomputed_review_sha
precomputed_review_graph_response = _precomputed_review_graph_response
precomputed_review_payload = _precomputed_review_payload
rescore_demo_payload = _rescore_demo_payload
remember_precomputed_review_payload = _remember_precomputed_review_payload
precomputed_review_target_set = _precomputed_review_target_set
precomputed_summary = _precomputed_summary
public_precomputed_landing_page = _public_precomputed_landing_page
render_rescore_demo_page = _render_rescore_demo_page
render_precomputed_api_page = _render_precomputed_api_page
render_precomputed_graph_page = _render_precomputed_graph_page
render_precomputed_markdown_report = _render_precomputed_markdown_report
render_precomputed_review_detail_page = _render_precomputed_review_detail_page
short_sha = _short_sha
url_quote = _url_quote

__all__ = [
    "fast_detail_target_card",
    "fast_review_shell",
    "canonical_precomputed_review_sha",
    "precomputed_review_graph_response",
    "precomputed_review_payload",
    "rescore_demo_payload",
    "remember_precomputed_review_payload",
    "precomputed_review_target_set",
    "precomputed_summary",
    "public_precomputed_landing_page",
    "render_rescore_demo_page",
    "render_precomputed_api_page",
    "render_precomputed_graph_page",
    "render_precomputed_markdown_report",
    "render_precomputed_review_detail_page",
    "short_sha",
    "url_quote",
]
