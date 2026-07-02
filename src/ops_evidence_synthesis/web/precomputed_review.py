from __future__ import annotations

import json
import os
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
        if str(payload.get("evidence_sha256") or "") != evidence_id:
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
        if str(payload.get("evidence_sha256") or "") != evidence_id:
            continue
        if ttl > 0:
            _PRECOMPUTED_REVIEW_CACHE[evidence_id] = (time.monotonic(), deepcopy(payload))
        return payload
    return None


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
    for demo_id in _public_rescore_demo_ids():
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


def _public_manifest_entries() -> list[dict[str, Any]]:
    index_path = _public_manifest_index_path()
    try:
        index = json.loads(index_path.read_text(encoding="utf-8"))
    except Exception:
        return []
    entries: list[dict[str, Any]] = []
    for raw_path in index.get("manifests") or []:
        manifest_path = Path(str(raw_path))
        if not manifest_path.is_absolute() and not manifest_path.exists():
            candidate = index_path.parent / manifest_path.name
            if candidate.exists():
                manifest_path = candidate
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
            "scale_validation": "Cross-Domain Scale Validation",
        }.get(landing_role, "Cross-Domain Scale Validation")
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
    badge = "Flagship" if featured else str(entry.get("category") or "Review")
    return f"""
      <article class="review-card{' featured' if featured else ''}">
        <div class="card-topline">
          <span class="badge">{_html(badge)}</span>
          <span class="sha">{_html(evidence_sha[:12])}</span>
        </div>
        <h3><a href="{_html(detail_url)}">{_html(str(entry.get("title") or "Precomputed review"))}</a></h3>
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
        <div class="actions">
          <a href="{_html(detail_url)}">Detail</a>
          <a href="{_html(api_url)}">API</a>
          <a href="{_html(graph_url)}">Graph</a>
          <a href="{_html(report_url)}">Report</a>
        </div>
      </article>
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
    scale_entries = [
        row
        for row in manifest_entries
        if row.get("category") not in {"Primary Review", "Guarded Review", "Observation Gap Validation"}
    ]
    primary_cards = "\n".join(_review_card_html(row, featured=True) for row in primary_entries)
    guarded_cards = "\n".join(_review_card_html(row) for row in guarded_entries)
    observation_cards = "\n".join(_review_card_html(row) for row in observation_entries)
    scale_cards = "\n".join(_review_card_html(row) for row in scale_entries)
    if not primary_cards:
        primary_cards = "<p>No primary review is available.</p>"
    if not guarded_cards:
        guarded_cards = "<p>No guarded review is available.</p>"
    if not observation_cards:
        observation_cards = "<p>No observation gap review is available.</p>"
    if not scale_cards:
        scale_cards = "<p>No scale validation review is available.</p>"
    rescore_demo_ids = _public_rescore_demo_ids()
    demo_links = "\n".join(
        (
            f"<a class='loop-link' href='/ui/rescore-demo?id={quote(demo_id)}'>"
            "<strong>More data rescore demo</strong>"
            f"<span>{_html(demo_id)}</span>"
            "<small>needs_more_data -&gt; evidence_collected</small>"
            "</a>"
        )
        for demo_id in rescore_demo_ids
    )
    demo_section = f"<section><h2>Improvement Loop</h2><div class='loop-grid'>{demo_links}</div></section>" if demo_links else ""
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
    rescore_demo_id = rescore_demo_ids[0] if rescore_demo_ids else ""
    rescore_demo_url = f"/ui/rescore-demo?id={_url_quote(rescore_demo_id)}" if rescore_demo_id else "#improvement-loop"
    primary_entry = primary_entries[0] if primary_entries else {}
    gate_provider_total = int(primary_entry.get("provider_count") or 5)
    gate_provider_success = int(primary_entry.get("schema_valid_count") or gate_provider_total or 5)
    gate_signal_label = f"{gate_provider_success} / {gate_provider_total}"
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
            --bg: #eef2f7;
            --surface: #ffffff;
            --surface-2: #f8fafc;
            --ink: #0f1b2d;
            --ink-2: #526173;
            --ink-3: #8a97a8;
            --border: #d9e1ec;
            --accent: #2a6fdb;
            --accent-soft: #e7f0fc;
            --green: #12836b;
            --green-soft: #dff5ee;
            --amber: #9a5b00;
            --amber-soft: #fff4dc;
            --shadow: 0 18px 48px rgba(15, 27, 45, .08);
            --mono: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
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
          p {{ margin: 0; color: var(--ink-2); line-height: 1.55; }}
          .shell {{
            width: min(calc(100% - 48px), 1720px);
            margin: 0 auto;
            padding: 0;
          }}
          .topbar {{
            height: 70px;
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 18px;
            border-bottom: 1px solid var(--border);
          }}
          .brand {{ display: flex; align-items: center; gap: 12px; min-width: 236px; }}
          .brand-mark {{
            width: 34px;
            height: 34px;
            border-radius: 8px;
            display: grid;
            place-items: center;
            background: var(--accent);
            color: #fff;
            font: 700 13px/1 var(--mono);
          }}
          .brand-name {{ font-weight: 750; letter-spacing: 0; }}
          .nav-links, .nav-actions {{ display: flex; align-items: center; flex-wrap: wrap; gap: 8px; }}
          .nav-links a, .button {{
            border: 1px solid var(--border);
            border-radius: 8px;
            background: rgba(255,255,255,.72);
            color: var(--ink-2);
            padding: 8px 10px;
            font-size: 12.5px;
            font-weight: 700;
          }}
          .button.primary {{
            background: var(--accent);
            border-color: var(--accent);
            color: #fff;
          }}
          .live-pill {{
            display: inline-flex;
            align-items: center;
            gap: 7px;
            border: 1px solid var(--border);
            border-radius: 999px;
            padding: 8px 11px;
            background: var(--surface);
            color: var(--ink-2);
            font-size: 12px;
            font-weight: 750;
          }}
          .live-dot {{ width: 7px; height: 7px; border-radius: 50%; background: var(--green); }}
          .hero {{
            display: grid;
            grid-template-columns: minmax(620px, 1.45fr) minmax(420px, .85fr);
            gap: clamp(28px, 4vw, 72px);
            align-items: center;
            padding: 46px 0 34px;
          }}
          .eyebrow {{
            display: inline-flex;
            color: var(--accent);
            background: var(--accent-soft);
            border: 1px solid #d3e4fb;
            border-radius: 999px;
            padding: 7px 11px;
            font: 700 12px/1 var(--mono);
          }}
          h1 {{
            margin: 20px 0 14px;
            font-size: clamp(58px, 4.1vw, 76px);
            line-height: .96;
            letter-spacing: 0;
            max-width: 1080px;
          }}
          h1 span {{ color: var(--accent); }}
          .jp-tagline {{
            margin: 0 0 12px;
            color: var(--ink);
            font-size: 15px;
            font-weight: 800;
            line-height: 1.5;
          }}
          .hero-sub {{ max-width: 980px; font-size: 16.5px; line-height: 1.48; color: var(--ink-2); }}
          .hero-cta {{ display: flex; flex-wrap: wrap; gap: 10px; margin-top: 24px; }}
          .hero-cta .button {{ white-space: nowrap; }}
          .gate-card {{
            border: 1px solid var(--border);
            border-radius: 8px;
            background: rgba(255,255,255,.88);
            box-shadow: var(--shadow);
            padding: 26px;
          }}
          .gate-kicker {{ color: var(--ink-3); font: 700 11px/1 var(--mono); letter-spacing: .08em; text-transform: uppercase; }}
          .gate-big {{ display: flex; align-items: end; gap: 12px; margin-top: 20px; }}
          .gate-big b {{ flex: 0 0 auto; font-size: 52px; line-height: .9; letter-spacing: 0; white-space: nowrap; }}
          .gate-big span {{ flex: 1 1 auto; color: var(--ink-2); font-size: 13px; padding-bottom: 5px; }}
          .gate-bars {{ display: grid; grid-template-columns: repeat(5, 1fr); gap: 6px; margin-top: 22px; }}
          .gate-bars i {{ display: block; height: 34px; border-radius: 8px; background: var(--green); }}
          .gate-div {{ display: grid; grid-template-columns: 1fr auto 1fr; gap: 12px; align-items: center; margin: 24px 0 14px; }}
          .gate-div i {{ height: 1px; background: var(--border); }}
          .gate-div span {{ color: var(--ink-3); font: 700 10px/1 var(--mono); letter-spacing: .1em; }}
          .gate-badge {{
            display: flex;
            gap: 12px;
            align-items: center;
            border: 1px solid var(--border);
            border-radius: 8px;
            background: var(--amber-soft);
            padding: 14px;
          }}
          .gate-lock {{
            width: 34px;
            height: 34px;
            display: grid;
            place-items: center;
            border: 1px solid var(--border);
            border-radius: 8px;
            background: var(--surface);
            color: var(--amber);
            font-weight: 900;
          }}
          .gate-badge b {{ display: block; color: var(--amber); font: 800 12px/1 var(--mono); }}
          .gate-badge small {{ display: block; margin-top: 4px; color: var(--ink-2); font-size: 12px; }}
          .trust {{
            display: grid;
            grid-template-columns: repeat(5, 1fr);
            gap: 12px;
            margin-bottom: 34px;
          }}
          .trust-cell {{
            border: 1px solid var(--border);
            border-radius: 8px;
            background: rgba(255,255,255,.72);
            padding: 16px;
          }}
          .trust-cell b {{ display: block; font-size: 22px; letter-spacing: 0; }}
          .trust-cell span {{ display: block; margin-top: 6px; color: var(--ink-2); font-size: 12px; line-height: 1.4; }}
          .agent-loop {{
            display: grid;
            grid-template-columns: minmax(320px, .45fr) minmax(0, 1fr);
            gap: 14px;
            align-items: stretch;
            margin-bottom: 34px;
          }}
          .agent-panel, .agent-step {{
            border: 1px solid var(--border);
            border-radius: 8px;
            background: var(--surface);
            padding: 16px;
          }}
          .agent-panel strong {{ display: block; font-size: 17px; margin-top: 8px; }}
          .agent-panel p {{ margin-top: 8px; font-size: 13px; }}
          .agent-steps {{
            display: grid;
            grid-template-columns: repeat(5, minmax(0, 1fr));
            gap: 10px;
          }}
          .agent-step b {{ display: block; font-size: 12px; color: var(--ink); }}
          .agent-step span {{ display: block; margin-top: 6px; color: var(--ink-2); font-size: 11.5px; line-height: 1.35; }}
          .mode-grid, .criteria-grid {{
            display: grid;
            gap: 12px;
          }}
          .mode-grid {{ grid-template-columns: repeat(3, minmax(0, 1fr)); }}
          .criteria-grid {{ grid-template-columns: repeat(5, minmax(0, 1fr)); }}
          .mode-card, .criteria-card {{
            border: 1px solid var(--border);
            border-radius: 8px;
            background: var(--surface);
            padding: 16px;
          }}
          .mode-card strong, .criteria-card strong {{ display: block; color: var(--ink); font-size: 14px; }}
          .mode-card span {{ display: block; margin-top: 6px; color: var(--accent); font: 800 11px/1.35 var(--mono); }}
          .mode-card p, .criteria-card p {{ margin-top: 8px; font-size: 12.5px; }}
          section {{ margin-top: 36px; }}
          .section-head {{
            display: flex;
            justify-content: space-between;
            align-items: end;
            gap: 18px;
            margin-bottom: 16px;
          }}
          .kicker {{ color: var(--accent); font: 800 11px/1 var(--mono); letter-spacing: .08em; text-transform: uppercase; }}
          h2 {{ margin: 8px 0 0; font-size: 24px; letter-spacing: 0; }}
          .section-note {{ max-width: 660px; color: var(--ink-2); font-size: 13px; line-height: 1.55; text-align: right; }}
          .review-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(380px, 1fr)); gap: 14px; }}
          .review-card {{
            display: grid;
            gap: 12px;
            padding: 20px;
            border: 1px solid var(--border);
            border-radius: 8px;
            background: var(--surface);
            box-shadow: 0 10px 34px rgba(15, 27, 45, .055);
          }}
          .review-card.featured {{ grid-column: 1 / -1; border-color: #b7c9e4; }}
          .review-card h3 {{ margin: 0; font-size: 18px; line-height: 1.35; letter-spacing: 0; }}
          .review-card p {{ font-size: 13px; }}
          .card-topline {{ display: flex; justify-content: space-between; gap: 12px; align-items: center; }}
          .badge {{
            display: inline-flex;
            border: 1px solid #d3e4fb;
            border-radius: 999px;
            padding: 6px 10px;
            background: var(--accent-soft);
            color: var(--accent);
            font-size: 11px;
            font-weight: 800;
          }}
          .sha, small {{ color: var(--ink-3); font-family: var(--mono); font-size: 11.5px; }}
          .metrics {{ display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 10px; margin: 4px 0 0; }}
          .metrics div {{ border-top: 1px solid var(--border); padding-top: 10px; min-width: 0; }}
          dt {{ color: var(--ink-3); font-size: 10px; text-transform: uppercase; letter-spacing: .06em; }}
          dd {{ margin: 3px 0 0; font-weight: 820; overflow-wrap: anywhere; }}
          .actions {{ display: flex; flex-wrap: wrap; gap: 8px; }}
          .actions a, .loop-link {{
            display: inline-grid;
            gap: 3px;
            border: 1px solid var(--border);
            border-radius: 8px;
            padding: 9px 11px;
            background: var(--surface-2);
            color: var(--ink-2);
            font-size: 12px;
            font-weight: 800;
          }}
          .loop-grid {{ display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; }}
          .archive {{ margin: 38px 0 56px; border-top: 1px solid var(--border); padding-top: 18px; }}
          summary {{ cursor: pointer; color: var(--ink-2); font-weight: 800; }}
          ul {{ list-style: none; padding: 0; margin: 14px 0 0; display: grid; gap: 10px; }}
          li {{ display: grid; gap: 4px; padding: 12px 14px; border: 1px solid var(--border); border-radius: 8px; background: var(--surface); }}
          @media (min-width: 1500px) {{
            .review-grid {{ grid-template-columns: repeat(auto-fit, minmax(420px, 1fr)); }}
          }}
          @media (max-width: 1180px) {{
            .hero {{ grid-template-columns: 1fr; }}
          }}
          @media (max-width: 900px) {{
            .shell {{ width: min(calc(100% - 32px), 1720px); }}
            .topbar {{ height: auto; padding: 16px 0; align-items: flex-start; flex-direction: column; }}
            .hero {{ grid-template-columns: 1fr; padding-top: 42px; }}
            .trust {{ grid-template-columns: repeat(2, 1fr); }}
            .review-grid, .loop-grid {{ grid-template-columns: 1fr; }}
            .agent-loop {{ grid-template-columns: 1fr; }}
            .agent-steps {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
            .mode-grid, .criteria-grid {{ grid-template-columns: 1fr; }}
            .metrics {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
            .section-head {{ align-items: flex-start; flex-direction: column; }}
            .section-note {{ text-align: left; }}
          }}
          @media (max-width: 520px) {{
            h1 {{ font-size: 42px; }}
            .trust {{ grid-template-columns: 1fr; }}
            .agent-steps {{ grid-template-columns: 1fr; }}
            .gate-big b {{ font-size: 42px; }}
          }}
        </style>
      </head>
      <body>
        <main class="shell">
          <nav class="topbar" aria-label="Primary">
            <div class="brand">
              <div class="brand-mark">OE</div>
              <div class="brand-name">Ops Evidence Synthesis</div>
            </div>
            <div class="nav-links">
              <a href="/">Overview</a>
              <a href="#review-modes">Modes</a>
              <a href="#review-set">Review Set</a>
              <a href="#judging-map">Judging Map</a>
              <a href="#improvement-loop">Improvement Loop</a>
              <span class="live-pill"><span class="live-dot"></span>Cloud Run live</span>
            </div>
          </nav>
          <section class="hero">
            <div>
              <span class="eyebrow">Local-first AI-assisted DevOps incident review</span>
              <h1>Evidence before <span>certainty.</span></h1>
              <p class="jp-tagline">AIが断定する前に、運用証拠を固定する。</p>
              <p class="hero-sub">This public surface serves read-only precomputed reviews over sanitized logs, sanitized source context, and approved system profiles. Provider convergence creates review targets, not accepted incident causes.</p>
              <div class="hero-cta">
                <a class="button primary" href="{_html(primary_detail_url)}">Open primary review</a>
                <a class="button primary" href="{_html(rescore_demo_url)}">Watch rescore loop</a>
                <a class="button" href="{_html(primary_report_url)}">Read incident report</a>
                {_public_global_action_links_html()}
              </div>
            </div>
            <aside class="gate-card" aria-label="Human gated promotion summary">
              <div class="gate-kicker">review graph arbitration</div>
              <div class="gate-big"><b>{_html(gate_signal_label)}</b><span>provider signal, not a verdict</span></div>
              <div class="gate-bars"><i></i><i></i><i></i><i></i><i></i></div>
              <div class="gate-div"><i></i><span>PROMOTION GATE</span><i></i></div>
              <div class="gate-badge">
                <div class="gate-lock">HG</div>
                <div><b>0 AUTO-PROMOTED CAUSES</b><small>Incident promotion waits for user impact and operational outcome evidence.</small></div>
              </div>
            </aside>
          </section>
          <section class="trust" aria-label="Public review guarantees">
            <article class="trust-cell"><b>Cloud Run</b><span>read-only UI</span></article>
            <article class="trust-cell"><b>5</b><span>provider recorded runs</span></article>
            <article class="trust-cell"><b>100%</b><span>ledger coverage on curated cases</span></article>
            <article class="trust-cell"><b>0</b><span>auto-accepted incident causes</span></article>
            <article class="trust-cell"><b>Local</b><span>raw logs are not uploaded</span></article>
          </section>
          <section class="agent-loop" aria-label="Agent loop">
            <div class="agent-panel">
              <div class="kicker">Agent loop</div>
              <strong>ADK-compatible trace included</strong>
              <p>Each public review keeps the tool-call trace that turns sanitized evidence into review targets, missing-evidence requests, and a human gate.</p>
            </div>
            <div class="agent-steps">
              <div class="agent-step"><b>Collect</b><span>sanitize logs and source context</span></div>
              <div class="agent-step"><b>Compare</b><span>run provider-specific chunks</span></div>
              <div class="agent-step"><b>Validate</b><span>check citations and source refs</span></div>
              <div class="agent-step"><b>Rescore</b><span>attach more evidence when needed</span></div>
              <div class="agent-step"><b>Gate</b><span>keep incident promotion human-owned</span></div>
            </div>
          </section>
          <section id="review-modes">
            <div class="section-head">
              <div>
                <div class="kicker">Review modes</div>
                <h2>Fast path for judges, deep path for real evidence.</h2>
              </div>
              <p class="section-note">The public URL serves precomputed artifacts immediately. Real provider runs can spend more time because they preserve evidence boundaries before action.</p>
            </div>
            <div class="mode-grid">
              <article class="mode-card"><strong>Fast Review</strong><span>initial triage</span><p>Shows provider positions, review targets, and missing evidence without claiming an accepted cause.</p></article>
              <article class="mode-card"><strong>Evidence Rescore</strong><span>improvement loop</span><p>Attaches a child evidence bundle and shows how more data changes review priority while the gate remains explicit.</p></article>
              <article class="mode-card"><strong>Full Forensic Review</strong><span>45k-50k rows</span><p>Runs chunked provider analysis, citation validation, and deterministic review graph merge over the full sanitized corpus.</p></article>
            </div>
          </section>
          <section id="judging-map">
            <div class="section-head">
              <div>
                <div class="kicker">Hackathon fit</div>
                <h2>Built as the evidence gate before automated action.</h2>
              </div>
              <p class="section-note">The project focuses on the missing middle of DevOps agents: deciding whether there is enough evidence to act.</p>
            </div>
            <div class="criteria-grid">
              <article class="criteria-card"><strong>Agent value</strong><p>Tool-call trace, missing-evidence routing, and rescore loop.</p></article>
              <article class="criteria-card"><strong>Problem fit</strong><p>Prevents unsafe certainty from thin operational evidence.</p></article>
              <article class="criteria-card"><strong>Usability</strong><p>No-login Cloud Run UI with primary review and graph links.</p></article>
              <article class="criteria-card"><strong>Practicality</strong><p>Raw logs stay local; promotion is human-gated.</p></article>
              <article class="criteria-card"><strong>Build depth</strong><p>5 providers, full-corpus ledger, tests, Cloud Build, Cloud Run.</p></article>
            </div>
          </section>
          <section id="review-set">
            <div class="section-head">
              <div>
                <div class="kicker">Public review set</div>
                <h2>Real API runs. Full-corpus ledgers.</h2>
              </div>
              <p class="section-note">Every sanitized DB row is assigned to the coverage ledger before provider chunking. Raw bundles, raw source, and write APIs are not exposed here.</p>
            </div>
            <h2>Primary Review</h2>
            <div class="review-grid">{primary_cards}</div>
          </section>
          <section>
            <h2>Guarded Review</h2>
            <div class="review-grid">{guarded_cards}</div>
          </section>
          <section>
            <h2>Observation Gap Validation</h2>
            <div class="review-grid">{observation_cards}</div>
          </section>
          <section>
            <h2>Cross-Domain Scale Validation</h2>
            <div class="review-grid">{scale_cards}</div>
          </section>
          <section id="improvement-loop">
            <div class="section-head">
              <div>
                <div class="kicker">Operated as production software</div>
                <h2>Convergence is support, not a verdict.</h2>
              </div>
              <p class="section-note">The AI workflow can ask for missing evidence, attach a child bundle, and re-score the review graph without exposing public write paths.</p>
            </div>
          </section>
          {demo_section}
          {archive_section}
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
            "impact": str(finding.get("impact") or ""),
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
            "detail": str(finding.get("impact") or ""),
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
    context = payload.get("analysis_context") if isinstance(payload.get("analysis_context"), dict) else {}
    log_observations = [str(item) for item in context.get("log_observations") or [] if str(item).strip()]
    source_observations = [str(item) for item in context.get("source_observations") or [] if str(item).strip()]
    conclusion_points = [str(item) for item in context.get("analysis_conclusion") or [] if str(item).strip()]
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
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Ops Evidence API View</title>
  <style>
    body {{ margin: 0; background: #f7f8fb; color: #17202a; font-family: Inter, system-ui, sans-serif; }}
    main {{ max-width: 1120px; margin: 0 auto; padding: 28px 20px 44px; display: grid; gap: 14px; }}
    h1, h2, p {{ margin: 0; }}
    h1 {{ font-size: 28px; }}
    h2 {{ font-size: 18px; }}
    p {{ color: #5c6878; line-height: 1.5; }}
    .hero, .readable, .api-card {{ display: grid; gap: 10px; padding: 16px; border: 1px solid #d9e0ea; border-radius: 8px; background: #fff; }}
    .hero {{ border-left: 5px solid #166d6b; }}
    .readable {{ border-left: 5px solid #a15c00; }}
    .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(190px, 1fr)); gap: 10px; }}
    .metric {{ padding: 10px; border: 1px solid #d9e0ea; border-radius: 6px; background: #fbfcfe; }}
    .metric strong {{ display: block; font-size: 22px; }}
    ul {{ margin: 0; padding-left: 20px; color: #334155; line-height: 1.5; }}
    table {{ width: 100%; border-collapse: collapse; overflow: hidden; border: 1px solid #d9e0ea; border-radius: 6px; }}
    th, td {{ padding: 9px 10px; border-bottom: 1px solid #d9e0ea; text-align: left; vertical-align: top; }}
    th {{ background: #f2f5f9; color: #5c6878; font-size: 12px; text-transform: uppercase; }}
    tr:last-child td {{ border-bottom: 0; }}
    .api-card {{ display: grid; gap: 10px; padding: 16px; border: 1px solid #d9e0ea; border-left: 5px solid #2f5f9e; border-radius: 8px; background: #fff; }}
    label {{ color: #5c6878; font-size: 12px; font-weight: 800; text-transform: uppercase; }}
    a {{ color: #0b5cad; font-weight: 800; overflow-wrap: anywhere; }}
    pre {{ margin: 0; padding: 12px; border-radius: 6px; background: #f2f5f9; overflow: auto; font-size: 13px; }}
    code {{ font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; overflow-wrap: anywhere; }}
  </style>
</head>
<body>
  <main>
    <section class="hero">
      <label>Read-only API View</label>
      <h1>{_html(str(finding.get("title") or "Evidence review"))}</h1>
      <p>{_html(str(finding.get("impact") or "The API result is available for review."))}</p>
      <div class="grid">
        <div class="metric"><label>Evidence</label><strong>{_html(evidence_sha256[:12])}</strong></div>
        <div class="metric"><label>Sanitized Logs</label><strong>{_html(str(summary.get("log_count") or 0))}</strong></div>
        <div class="metric"><label>Providers</label><strong>{_html(str(providers.get("success") or 0))}/{_html(str(providers.get("total") or 0))}</strong></div>
        <div class="metric"><label>Validation Targets</label><strong>{_html(str(review.get("validation_targets") or 0))}</strong></div>
      </div>
    </section>
    <section class="readable">
      <label>Human-readable analysis</label>
      <h2>What was analyzed</h2>
      <ul>{''.join(f'<li>{_html(item)}</li>' for item in log_observations) or '<li>Sanitized evidence bundle was analyzed.</li>'}</ul>
      <h2>Code context used</h2>
      <ul>{''.join(f'<li>{_html(item)}</li>' for item in source_observations) or '<li>Sanitized source context was attached when available.</li>'}</ul>
      <h2>Conclusion</h2>
      <ul>{''.join(f'<li>{_html(item)}</li>' for item in conclusion_points) or '<li>Review targets remain human-gated; raw logs are not exposed.</li>'}</ul>
    </section>
    <section class="readable">
      <label>Provider outputs</label>
      <table>
        <thead><tr><th>Provider</th><th>Model</th><th>Status</th><th>Schema</th><th>Output hash</th></tr></thead>
        <tbody>{provider_rows or '<tr><td colspan="5">No provider status was persisted.</td></tr>'}</tbody>
      </table>
    </section>
    <section class="readable">
      <label>Review targets</label>
      <table>
        <thead><tr><th>#</th><th>Target</th><th>Claim</th><th>Agreement</th><th>Displayed refs</th></tr></thead>
        <tbody>{target_rows or '<tr><td colspan="5">No review targets were projected.</td></tr>'}</tbody>
      </table>
    </section>
    <p>The linked endpoints return machine-readable JSON; writes, raw bundles, and execution APIs are not exposed here.</p>
    {cards}
  </main>
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
    context = response.get("analysis_context") if isinstance(response.get("analysis_context"), dict) else {}
    nodes = [row for row in graph_model.get("nodes") or [] if isinstance(row, dict)]
    edges = [row for row in graph_model.get("edges") or [] if isinstance(row, dict)]
    context_cards = ""
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
            <article class="context-cell">
              <label>{_html(label)}</label>
              <strong>{_html(value)}</strong>
            </article>
            """
            for label, value in cells
            if _show_context_cell(label, value)
        )
        projection_interpretation_html = projection_interpretation_html + determinism_html
    node_cards = "\n".join(
        f"""
        <article class="node" data-node-type="{_html(str(node.get("type") or ""))}">
          <label>{_html(str(node.get("type") or "node"))}</label>
          <strong>{_html(str(node.get("label") or node.get("id") or ""))}</strong>
          <p>{_html(str(node.get("state") or node.get("detail") or ""))}</p>
        </article>
        """
        for node in nodes
    )
    edge_rows = "\n".join(
        f"<li><code>{_html(str(edge.get('source') or ''))}</code> -> <code>{_html(str(edge.get('target') or ''))}</code><span>{_html(str(edge.get('relation') or ''))}</span></li>"
        for edge in edges
    )
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Ops Evidence Review Graph</title>
  <style>
    body {{ margin: 0; background: #f7f8fb; color: #17202a; font-family: Inter, system-ui, sans-serif; }}
    main {{ max-width: 1180px; margin: 0 auto; padding: 28px 20px 44px; display: grid; gap: 14px; }}
    h1, h2, p {{ margin: 0; }}
    h1 {{ font-size: 28px; }}
    h2 {{ font-size: 18px; }}
    p {{ color: #5c6878; line-height: 1.5; }}
    .summary, .edges {{ padding: 16px; border: 1px solid #d9e0ea; border-left: 5px solid #166d6b; border-radius: 8px; background: #fff; }}
    .context-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 10px; margin-top: 12px; }}
    .context-cell {{ padding: 10px; border: 1px solid #d9e0ea; border-radius: 6px; background: #fbfcfe; }}
    .graph-map {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(210px, 1fr)); gap: 10px; }}
    .node {{ display: grid; gap: 6px; min-width: 0; padding: 12px; border: 1px solid #d9e0ea; border-radius: 8px; background: #fff; }}
    .node[data-node-type="provider"] {{ border-left: 5px solid #2f5f9e; }}
    .node[data-node-type="review_target"] {{ border-left: 5px solid #a15c00; }}
    .node[data-node-type="baseline"] {{ border-left: 5px solid #166d6b; }}
    label {{ color: #5c6878; font-size: 12px; font-weight: 800; text-transform: uppercase; }}
    strong {{ overflow-wrap: anywhere; }}
    ul {{ display: grid; gap: 7px; margin: 10px 0 0; padding-left: 18px; }}
    li {{ line-height: 1.4; }}
    code {{ font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; overflow-wrap: anywhere; }}
    span {{ margin-left: 8px; color: #5c6878; font-weight: 700; }}
  </style>
</head>
<body>
  <main>
    <section class="summary">
      <label>Review Graph</label>
      <h1>Nodes and edges for evidence {_html(evidence_sha256[:12])}</h1>
      <p>{int(graph_model.get("node_count") or 0)} nodes / {int(graph_model.get("edge_count") or 0)} edges. JSON source: <a href="/review/graph?evidence_sha256={_html(_url_quote(evidence_sha256))}">/review/graph</a></p>
      {projection_interpretation_html if context else ""}
      <div class="context-grid">{context_cards}</div>
    </section>
    <section class="graph-map">{node_cards}</section>
    <section class="edges">
      <h2>Edges</h2>
      <ul>{edge_rows}</ul>
    </section>
  </main>
</body>
</html>"""


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
    impact = _markdown_text(finding.get("impact") or "Review targets are available for human validation.")
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
            "Provider convergence creates validation targets; final causal judgement "
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
    return lines


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
    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n")
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
        "stream_v3_runtime": "stream_v3 Dell runtime",
        "stream_v3_arena_monitoring": "stream_v3 arena-server monitoring",
    }
    if service:
        return service_labels.get(service, service.replace("_", " "))
    if int(review.get("primary_targets") or 0):
        return "Primary Review"
    if int(review.get("validation_targets") or 0):
        return "Validation Review"
    return "Full Review"


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
    cells = [
        (
            f"{int(providers.get('success') or 0)} / {int(providers.get('total') or 0)}",
            "schema-valid providers",
            _display_policy(str(providers.get("pipeline_status") or "precomputed")),
        ),
        (str(int(review.get("primary_targets") or 0)), "primary candidates", "not auto-promoted"),
        (str(int(review.get("validation_targets") or 0)), "validation targets", "human review work"),
        (str(target_count), "review targets", "canonical queue"),
        (_detail_coverage_label(payload), "ledger coverage", "sanitized corpus"),
        (
            _display_policy(raw_policy),
            "raw logs",
            "" if str(raw_policy).strip().lower() in {"not_uploaded", "not uploaded"} else _human_count(log_count),
        ),
    ]
    rows = []
    for value, label, note in cells:
        note_html = f"<small>{_html(note)}</small>" if note else ""
        rows.append(
            f"""
        <article class="stat-cell">
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
        ("GitHub", _public_repo_url(), "repository"),
    ]
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
    total = sum(counts.values()) or int(target.get("provider_count") or 0)
    return claimed, silent, total


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
            <span>{claimed}/{total} claimed</span>
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
          <b>{claimed} claimed / {silent} silent / {convergence_score:.2f}</b>
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
    supplemental_sections = _detail_supplemental_sections(
        provider_panel,
        analysis_context_panel,
        devops_loop_panel,
    )
    finding_title = str(finding.get("title") or "No persisted finding yet")
    finding_impact = str(finding.get("impact") or "Run analysis to create a persisted review result.")
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
    }}
    .breadcrumb {{
      color: var(--ink-3);
      font-size: 13px;
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
    .provider-dot {{
      width: 10px;
      height: 10px;
      border-radius: 50%;
      background: var(--silent);
    }}
    .workspace-provider-card.claimed .provider-dot {{ background: var(--claimed); }}
    .workspace-provider-card.contradicted .provider-dot {{ background: var(--danger); }}
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
  </style>
</head>
<body>
  <div class="page">
    <header class="topbar">
      <div class="brand-row">
        <div class="mark">OE</div>
        <div class="breadcrumb">Reviews / <strong>{_html(case_label)}</strong></div>
      </div>
      <div class="status-row">
        <span class="status-chip"><span class="status-dot"></span>Persisted Review Result</span>
        <span class="evidence-chip">evidence {_html(_short_sha(evidence_sha256))}</span>
      </div>
    </header>
    <main>
      <section class="hero">
        <span class="eyebrow">Canonical Review Graph / {_html(provider_mode)}</span>
        <h1>{_html(finding_title)}</h1>
        <p>{_html(finding_impact)}</p>
        <div class="actions">
          {action_links}
        </div>
        <div class="stat-grid">{summary_cells}</div>
      </section>
      {graph_summary_panel}
      {trace_panel}
      {review_workbench}
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
        f"<p class=\"section-note\">{overflow} additional trace step(s) are retained in the API view.</p>"
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
    rule_or_context = int(summary.get("rule_or_context_count") or 0)
    partial_overlap = int(summary.get("partial_overlap_count") or 0)
    conflicts = int(summary.get("conflict_count") or 0)
    auto_archived = int(summary.get("auto_archived_count") or 0)
    verdict_total = max(1, converged + single_source + rule_or_context)
    converged_width = (converged / verdict_total) * 100
    single_width = (single_source / verdict_total) * 100
    context_width = (rule_or_context / verdict_total) * 100
    stat_cells = [
        (str(converged), "converged targets"),
        (str(single_source), "single-source targets"),
        (str(partial_overlap), "partial overlap overlay"),
        (str(conflicts), "explicit conflicts"),
        (str(summary.get("provider_detection_overlap") or "unknown"), "detection overlap"),
        (str(auto_archived), "auto-archived (post-window)"),
    ]
    stat_html = "".join(
        f"""
        <article class="matrix-cell">
          <strong>{_html(value)}</strong>
          <span>{_html(label)}</span>
        </article>
        """
        for value, label in stat_cells
    )
    note = str(summary.get("note") or "")
    score_definition = str(summary.get("score_definition") or "")
    promotion_policy = str(summary.get("target_promotion_policy") or "")
    incident_gate = _incident_gate_signal_text(summary.get("incident_gate_signal") or summary.get("incident_baseline"))
    summary_text = str(summary.get("summary") or "Provider agreement was evaluated before promotion.")
    policy_text = promotion_policy or "Each target promotion remains human-gated until impact and operational outcome evidence are attached."
    score_text = score_definition or "Convergence score = claimed successful providers / all successful providers."
    note_text = note or "Partial overlap is an overlay count for converged targets where at least one schema-valid provider was silent."
    return f"""
    <section class="section-block graph-arbitration">
      <div class="section-heading">
        <span class="eyebrow">Review Graph Arbitration</span>
        <h2>Convergence is technical support. Impact stays human-gated.</h2>
        <p>{_html(summary_text)}</p>
      </div>
      <div class="review-arbitration-grid">
        <article class="distribution-card">
          <strong class="distribution-title">Target verdict distribution</strong>
          <div class="distribution-bar" aria-label="Target verdict distribution">
            <span class="bar-converged" style="width:{converged_width:.1f}%"></span>
            <span class="bar-single" style="width:{single_width:.1f}%"></span>
            <span class="bar-context" style="width:{context_width:.1f}%"></span>
          </div>
          <div class="legend-row">
            <span><span class="legend-dot bar-converged"></span>{converged} converged</span>
            <span><span class="legend-dot bar-single"></span>{single_source} single-source</span>
            <span><span class="legend-dot bar-context"></span>{partial_overlap} partial overlap</span>
          </div>
          <div class="human-gate">
            <span class="gate-mark">HG</span>
            <div>
              <strong>Incident gate { _html(incident_gate) } · promotion human-gated</strong>
              <p>{_html(f"{conflicts} explicit conflicts · each target promotes on its own evidence")}</p>
              <details class="inline-details">
                <summary>Arbitration notes</summary>
                <p>{_html(policy_text)}</p>
                <p>{_html(score_text)}</p>
                <p>Target promotion: per-target human-gated. Incident gate signal: {_html(incident_gate)}. {_html(note_text)}</p>
              </details>
            </div>
          </div>
        </article>
        <div class="metric-matrix">{stat_html}</div>
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
        segments.append(
            f'<span class="stance-fill silent" style="width:{width:.1f}%" title="{_html(stance)} {count}"></span>'
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
    finding_impact = str(finding.get("impact") or "Run analysis to create a persisted review result.")
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
    row_html = "".join(
        f"""
        <article class="cell">
          <label>{_html(str(row.get("timestamp") or ""))}</label>
          <strong>{_html(str(row.get("message_template") or ""))}</strong>
          <p>{_html(str(row.get("summary") or ""))}</p>
        </article>
        """
        for row in rows
        if isinstance(row, dict)
    )
    providers = control.get("cross_check_providers") if isinstance(control.get("cross_check_providers"), list) else []
    provider_text = ", ".join(str(item) for item in providers if str(item))
    before_reasons = ", ".join(str(item) for item in before.get("blocked_reasons") or []) or "none"
    after_reasons = ", ".join(str(item) for item in after.get("blocked_reasons") or []) or "none"
    before_provider_positions = _rescore_provider_positions_html(before.get("provider_positions"))
    after_provider_positions = _rescore_provider_positions_html(after.get("provider_positions"))
    source_evidence_sha = str(payload.get("source_evidence_sha256") or "")
    action_links = _public_action_links_html(source_evidence_sha) if source_evidence_sha else ""
    source_trace_html = _rescore_source_trace_html(payload)
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>More data rescore demo</title>
  <style>
    :root {{ --ink: #17202a; --muted: #647184; --line: #d8dee8; --bg: #f7f8fb; --panel: #fff; --accent: #166d6b; --warn: #a15c00; }}
    * {{ box-sizing: border-box; }}
    body {{ margin: 0; font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; color: var(--ink); background: var(--bg); letter-spacing: 0; }}
    header {{ padding: 18px 24px; border-bottom: 1px solid var(--line); background: var(--panel); }}
    main {{ max-width: 1120px; margin: 0 auto; padding: 18px 24px 40px; display: grid; gap: 16px; }}
    h1 {{ margin: 0; font-size: 22px; }}
    h2 {{ margin: 0; font-size: 20px; }}
    p {{ margin: 0; color: var(--muted); line-height: 1.45; }}
    code {{ overflow-wrap: anywhere; }}
    .panel {{ border: 1px solid var(--line); border-left: 5px solid var(--accent); border-radius: 8px; background: var(--panel); padding: 16px; display: grid; gap: 12px; }}
    .panel.warn {{ border-left-color: var(--warn); }}
    .grid {{ display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 10px; }}
    .cell {{ border: 1px solid var(--line); border-radius: 6px; background: #fbfcfe; padding: 10px; min-width: 0; }}
    .positions {{ display: grid; grid-template-columns: repeat(5, minmax(0, 1fr)); gap: 8px; }}
    .position {{ border: 1px solid var(--line); border-radius: 6px; background: #fff; padding: 8px; min-width: 0; }}
    .actions {{ display: flex; flex-wrap: wrap; gap: 10px; }}
    label {{ display: block; color: var(--muted); font-size: 12px; font-weight: 800; text-transform: uppercase; margin-bottom: 5px; }}
    strong {{ display: block; font-size: 18px; line-height: 1.25; overflow-wrap: anywhere; }}
    a.button {{ display: inline-block; border: 1px solid var(--line); border-radius: 6px; padding: 8px 10px; color: var(--ink); text-decoration: none; font-weight: 700; }}
    @media (max-width: 760px) {{ main {{ padding: 14px; }} .grid, .positions {{ grid-template-columns: 1fr; }} }}
  </style>
</head>
<body>
  <header><h1>More data rescore demo</h1></header>
  <main>
    <section class="panel">
      <label>Read-only DevOps loop</label>
      <h2>{_html(str(payload.get("title") or "More data child bundle changed the promotion decision"))}</h2>
      <p>Shows the AI improvement cycle judges can inspect without starting model runs from the public URL.</p>
      <p>Source review: <a href="{_html(str(payload.get("source_review_url") or "#"))}">{_html(str(payload.get("source_evidence_sha256") or ""))}</a></p>
      {source_trace_html}
    </section>
    <section class="panel">
      <label>Gemini-led control plane</label>
      <h2>{_html(str(control.get("primary_provider") or "gemini-enterprise-agent-platform"))}</h2>
      <p>{_html(str(control.get("policy") or ""))}</p>
      <p>Cross-check providers: {_html(provider_text)}</p>
    </section>
    <section class="panel warn">
      <label>Before child evidence</label>
      <div class="grid">
        <article class="cell"><label>State</label><strong>{_html(str(before.get("state") or ""))}</strong><p>{_html(str(before.get("title") or ""))}</p></article>
        <article class="cell"><label>Promotion score</label><strong>{float(before.get("promotion_score") or 0):.2f}</strong><p>Priority is not truth probability.</p></article>
        <article class="cell"><label>Blocked reasons</label><strong>{_html(before_reasons)}</strong><p>Missing user-impact evidence blocks promotion.</p></article>
      </div>
      <label>Provider positions</label>
      <div class="positions">{before_provider_positions}</div>
    </section>
    <section class="panel">
      <label>More data refresh</label>
      <h2>{_html(str(loop.get("status_transition") or "needs_more_data -> evidence_collected"))}</h2>
      <p>Child Evidence Bundle <code>{_html(str(loop.get("child_evidence_sha256") or ""))}</code> added {int(loop.get("added_evidence_ref_count") or 0)} evidence refs and {int(loop.get("added_log_count") or 0)} log rows.</p>
      <div class="grid">{row_html}</div>
    </section>
    <section class="panel">
      <label>After re-score</label>
      <div class="grid">
        <article class="cell"><label>State</label><strong>{_html(str(after.get("state") or ""))}</strong><p>{_html(str(after.get("title") or ""))}</p></article>
        <article class="cell"><label>Promotion score</label><strong>{float(after.get("promotion_score") or 0):.2f}</strong><p>Review priority increased after child evidence.</p></article>
        <article class="cell"><label>Blocked reasons</label><strong>{_html(after_reasons)}</strong><p>Primary promotion gate is now closed.</p></article>
      </div>
      <label>Provider positions</label>
      <div class="positions">{after_provider_positions}</div>
    </section>
    <section class="panel">
      <label>Verification</label>
      <p>Covered by <code>{_html(str(verification.get("local_test") or ""))}</code>. Public mode: <code>{_html(str(verification.get("public_mode") or ""))}</code>. Raw logs: <code>{_html(str(verification.get("raw_log_policy") or ""))}</code>.</p>
      <p><a class="button" href="/">Back to public index</a></p>
      <div class="actions">{action_links}</div>
    </section>
  </main>
</body>
</html>"""


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
        <div class="grid">
          <article class="cell"><label>Before target</label><strong>{_html(str(identity.get("title") or ""))}</strong><p>{_html(str(identity.get("state") or ""))}</p></article>
          <article class="cell"><label>Stored score</label><strong>{_html(score_text)}</strong><p>{_html(blocked_reasons)}</p></article>
          <article class="cell"><label>Stored stance</label><strong>{_html(str(identity.get("provider_stance") or ""))}</strong><p>Fixture-level trace, not a live write path.</p></article>
        </div>
        """
    return f"""
      <div class="cell">
        <label>Source trace</label>
        <strong>{_html(str(trace.get("status") or "recorded"))}</strong>
        <p>Before target present in current source review: {_html(contained)}. {_html(str(trace.get("note") or ""))}</p>
      </div>
      {identity_html}
    """


def _rescore_provider_positions_html(positions: object) -> str:
    rows = positions if isinstance(positions, list) else []
    cells = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        provider_id = str(row.get("provider_id") or "")
        stance = str(row.get("stance") or "")
        cells.append(
            "<article class='position'>"
            f"<label>{_html(provider_id)}</label>"
            f"<strong>{_html(stance)}</strong>"
            "</article>"
        )
    if not cells:
        return "<article class='position'><strong>not recorded</strong></article>"
    return "\n".join(cells)


def _html(value: object) -> str:
    import html

    return html.escape(str(value), quote=True)


fast_detail_target_card = _fast_detail_target_card
fast_review_shell = _fast_review_shell
canonical_precomputed_review_sha = _canonical_precomputed_review_sha
precomputed_review_graph_response = _precomputed_review_graph_response
precomputed_review_payload = _precomputed_review_payload
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
