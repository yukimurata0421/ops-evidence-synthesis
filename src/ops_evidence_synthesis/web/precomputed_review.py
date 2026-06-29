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


def _precomputed_review_cache_ttl_seconds() -> int:
    return int(os.environ.get("OES_PRECOMPUTED_REVIEW_CACHE_SECONDS", "300"))


def _human_count(value: int) -> str:
    return f"{int(value):,}"


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
    evidence_id = str(evidence_sha256 or "").strip()
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


def _public_precomputed_landing_page() -> str:
    rows: list[tuple[str, str, str]] = []
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
            if not evidence_sha or evidence_sha in seen:
                continue
            generation = payload.get("generation") if isinstance(payload.get("generation"), dict) else {}
            provider_mode = str(generation.get("provider_mode") or "")
            if provider_mode and not provider_mode.startswith("real_api"):
                continue
            seen.add(evidence_sha)
            summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
            finding = summary.get("finding") if isinstance(summary.get("finding"), dict) else {}
            rows.append(
                (
                    evidence_sha,
                    str(finding.get("title") or "Precomputed review"),
                    str(payload.get("updated_at") or summary.get("updated_at") or ""),
                )
            )
    links = "\n".join(
        (
            "<li>"
            f"<a href='/?evidence_sha256={quote(evidence_sha)}'>{_html(title)}</a>"
            f"<span>{_html(evidence_sha[:12])}</span>"
            f"<small>{_html(updated_at)}</small>"
            "</li>"
        )
        for evidence_sha, title, updated_at in rows
    )
    if not links:
        links = "<li><span>No precomputed review is available.</span></li>"
    demo_links = "\n".join(
        (
            "<li>"
            f"<a href='/ui/rescore-demo?id={quote(demo_id)}'>More data rescore demo</a>"
            f"<span>{_html(demo_id)}</span>"
            "<small>read-only before/after loop</small>"
            "</li>"
        )
        for demo_id in _public_rescore_demo_ids()
    )
    demo_section = f"<h2>Improvement loops</h2><ul>{demo_links}</ul>" if demo_links else ""
    return f"""
    <!doctype html>
    <html>
      <head>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <title>Ops Evidence Synthesis</title>
        <style>
          body {{ font-family: Inter, system-ui, sans-serif; margin: 0; color: #17202a; background: #f6f8fb; }}
          main {{ max-width: 760px; margin: 0 auto; padding: 48px 20px; }}
          h1 {{ font-size: 30px; margin: 0 0 12px; }}
          h2 {{ font-size: 18px; margin: 30px 0 0; }}
          p {{ color: #4a5565; line-height: 1.6; }}
          ul {{ list-style: none; padding: 0; margin: 24px 0 0; display: grid; gap: 10px; }}
          li {{ display: grid; gap: 4px; padding: 14px 16px; border: 1px solid #d9e2ec; border-radius: 8px; background: #fff; }}
          a {{ color: #0b5cad; font-weight: 700; text-decoration: none; }}
          span, small {{ color: #627083; font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }}
        </style>
      </head>
      <body>
        <main>
          <h1>Ops Evidence Synthesis</h1>
          <p>This public surface serves read-only precomputed reviews. Raw bundles and write APIs are not exposed here.</p>
          <ul>{links}</ul>
          {demo_section}
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
            "score_note": "Priority is review urgency, not truth probability.",
        },
    }
    return {
        "canonical_graph_status": "precomputed",
        "canonical_graph_sha256": str(summary.get("canonical_graph_sha256") or ""),
        "input_fingerprint_sha256": str(summary.get("input_fingerprint_sha256") or ""),
        "graph": graph_model,
        "analysis_context": analysis_context,
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
            "type": "baseline",
            "label": "Technical baseline",
            "state": "established" if baselines.get("technical") else "open",
            "detail": str(graph_summary.get("technical_baseline") or ""),
        },
        {
            "id": "baseline:incident",
            "type": "baseline",
            "label": "Incident baseline",
            "state": "established" if baselines.get("incident") else "open",
            "detail": str(graph_summary.get("incident_baseline") or ""),
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
        <thead><tr><th>#</th><th>Target</th><th>Claim</th><th>Agreement</th><th>Evidence refs</th></tr></thead>
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
            ("Model projection", _human_count(_context_count(context.get("model_projection_evidence_items")))),
            ("Projected occurrences", _human_count(_context_count(context.get("model_projection_occurrence_count")))),
            ("Projection coverage", _coverage_text(context.get("model_projection_occurrence_coverage_ratio"))),
        ]
        context_cards = "".join(
            f"""
            <article class="context-cell">
              <label>{_html(label)}</label>
              <strong>{_html(value)}</strong>
            </article>
            """
            for label, value in cells
            if value and value != "0"
        )
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


def _render_precomputed_review_detail_page(evidence_sha256: str, payload: dict[str, Any]) -> str:
    summary = _precomputed_summary(payload, evidence_sha256) or {}
    finding = summary.get("finding") if isinstance(summary.get("finding"), dict) else {}
    review = summary.get("review") if isinstance(summary.get("review"), dict) else {}
    providers = summary.get("providers") if isinstance(summary.get("providers"), dict) else {}
    targets = [target for target in payload.get("targets") or [] if isinstance(target, dict)]
    graph_sha = str(summary.get("canonical_graph_sha256") or "")
    raw_policy = str(summary.get("raw_log_policy") or "unknown")
    log_count = int(summary.get("log_count") or 0)
    target_cards = "\n".join(_fast_detail_target_card(target, index=index + 1) for index, target in enumerate(targets))
    trace_panel = _precomputed_agent_trace_panel(payload)
    provider_panel = _precomputed_provider_panel(payload, providers)
    graph_summary_panel = _precomputed_review_graph_summary_panel(payload)
    analysis_context_panel = _precomputed_analysis_context_panel(payload)
    devops_loop_panel = _precomputed_devops_loop_panel(payload)
    summary_url = f"/?evidence_sha256={_url_quote(evidence_sha256)}"
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Ops Evidence Review</title>
  <style>
    :root {{
      color-scheme: light;
      --ink: #17202a;
      --muted: #5c6878;
      --line: #d9e0ea;
      --bg: #f7f8fb;
      --panel: #ffffff;
      --accent: #166d6b;
      --accent-2: #2f5f9e;
      --warn: #a15c00;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      background: var(--bg);
      color: var(--ink);
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      letter-spacing: 0;
    }}
    header {{
      display: flex;
      justify-content: space-between;
      gap: 14px;
      padding: 18px 24px;
      border-bottom: 1px solid var(--line);
      background: var(--panel);
    }}
    main {{
      display: grid;
      gap: 14px;
      max-width: 1180px;
      margin: 0 auto;
      padding: 18px 24px 40px;
    }}
    h1, h2, p {{ margin: 0; }}
    h1 {{ font-size: 20px; }}
    h2 {{ font-size: 18px; line-height: 1.3; overflow-wrap: anywhere; }}
    p {{ color: var(--muted); line-height: 1.45; }}
    code {{ font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, "Liberation Mono", monospace; overflow-wrap: anywhere; }}
    .meta {{ color: var(--muted); font-size: 12px; text-align: right; }}
    .panel {{
      display: grid;
      gap: 12px;
      padding: 16px;
      border: 1px solid var(--line);
      border-left: 5px solid var(--accent);
      border-radius: 8px;
      background: var(--panel);
      min-width: 0;
    }}
    .panel.secondary {{ border-left-color: var(--accent-2); }}
    .metrics {{
      display: grid;
      grid-template-columns: minmax(0, 1.45fr) repeat(3, minmax(92px, 0.45fr)) minmax(150px, 0.7fr);
      gap: 10px;
    }}
    .metric, .target, .trace-step, .provider-row, .graph-cell {{
      border: 1px solid var(--line);
      border-radius: 6px;
      background: #fbfcfe;
      padding: 10px;
      min-width: 0;
    }}
    label {{
      display: block;
      margin-bottom: 5px;
      color: var(--muted);
      font-size: 12px;
      font-weight: 800;
      text-transform: uppercase;
    }}
    strong {{
      display: block;
      font-size: 20px;
      line-height: 1.25;
      overflow-wrap: anywhere;
    }}
    .targets, .trace-grid, .provider-grid, .graph-summary-grid {{ display: grid; gap: 10px; }}
    .trace-grid {{ grid-template-columns: repeat(3, minmax(0, 1fr)); }}
    .provider-grid {{ grid-template-columns: repeat(4, minmax(0, 1fr)); }}
    .graph-summary-grid {{ grid-template-columns: repeat(4, minmax(0, 1fr)); }}
    .target-head {{
      display: grid;
      grid-template-columns: minmax(0, 1fr) auto;
      gap: 12px;
      align-items: start;
    }}
    .score {{
      min-width: 84px;
      text-align: right;
      font-size: 22px;
      font-weight: 800;
      color: var(--accent);
    }}
    .score span {{ display: block; color: var(--muted); font-size: 11px; font-weight: 800; text-transform: uppercase; }}
    .pill-row {{ display: flex; flex-wrap: wrap; gap: 6px; margin-top: 8px; }}
    .pill {{
      border: 1px solid var(--line);
      border-radius: 999px;
      padding: 4px 7px;
      background: #fff;
      color: var(--muted);
      font-size: 12px;
      font-weight: 700;
    }}
    .target-grid {{
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 10px;
      margin-top: 10px;
    }}
    .field {{
      border-top: 1px solid var(--line);
      padding-top: 8px;
      min-width: 0;
    }}
    .field.full {{ grid-column: 1 / -1; }}
    .position-list {{ display: grid; gap: 6px; }}
    .position-row {{
      display: grid;
      grid-template-columns: minmax(150px, 0.6fr) 96px minmax(0, 1.4fr) 96px;
      gap: 8px;
      align-items: start;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: #fff;
      padding: 8px;
    }}
    .position-row p {{ color: var(--ink); }}
    .stance {{
      display: inline-flex;
      width: max-content;
      border: 1px solid var(--line);
      border-radius: 999px;
      padding: 3px 7px;
      background: #eef6f5;
      color: var(--accent);
      font-size: 12px;
      font-weight: 800;
    }}
    .actions {{ display: flex; flex-wrap: wrap; gap: 10px; }}
    a.button {{
      display: inline-flex;
      align-items: center;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: #fff;
      color: var(--ink);
      padding: 8px 10px;
      font-weight: 800;
      text-decoration: none;
    }}
    @media (max-width: 900px) {{
      .metrics, .trace-grid, .provider-grid, .graph-summary-grid {{ grid-template-columns: 1fr 1fr; }}
    }}
    @media (max-width: 760px) {{
      header {{ display: grid; }}
      .meta {{ text-align: left; }}
      main {{ padding: 14px; }}
      .metrics, .target-grid, .target-head, .trace-grid, .provider-grid, .graph-summary-grid, .position-row {{ grid-template-columns: 1fr; }}
      .score {{ text-align: left; }}
    }}
  </style>
</head>
<body>
  <header>
    <h1>Ops Evidence Review</h1>
    <div class="meta">Evidence <code>{_html(_short_sha(evidence_sha256))}</code></div>
  </header>
  <main>
    <section class="panel">
      <label>Persisted Review Result</label>
      <h2>{_html(str(finding.get("title") or "No persisted finding yet"))}</h2>
      <p>{_html(str(finding.get("impact") or "Run analysis to create a persisted review result."))}</p>
      <div class="metrics">
        <div class="metric"><label>Canonical graph</label><strong>{_html(_short_sha(graph_sha) if graph_sha else "precomputed")}</strong></div>
        <div class="metric"><label>Providers</label><strong>{int(providers.get("success") or 0)} / {int(providers.get("total") or 0)}</strong></div>
        <div class="metric"><label>Primary</label><strong>{int(review.get("primary_targets") or 0)}</strong></div>
        <div class="metric"><label>Validation</label><strong>{int(review.get("validation_targets") or 0)}</strong></div>
        <div class="metric"><label>Raw logs</label><strong>{_html(_display_policy(raw_policy))}</strong><p>{_html(_human_count(log_count) if log_count else "sanitized bundle")}</p></div>
      </div>
    </section>
    {graph_summary_panel}
    {analysis_context_panel}
    {trace_panel}
    {devops_loop_panel}
    {provider_panel}
    <section class="panel secondary">
      <label>Review Targets</label>
      <div class="targets">
        {target_cards or '<section class="target">No review targets are persisted for this evidence.</section>'}
      </div>
      <div class="actions">
        <a class="button" href="{_html(summary_url)}">Back to summary</a>
      </div>
    </section>
  </main>
</body>
</html>"""


def _precomputed_agent_trace_panel(payload: dict[str, Any]) -> str:
    steps = [step for step in payload.get("agent_trace") or [] if isinstance(step, dict)]
    if not steps:
        return ""
    rows = "".join(
        f"""
        <article class="trace-step">
          <label>Step {index}</label>
          <strong>{_html(str(step.get("title") or step.get("step") or ""))}</strong>
          <p>{_html(str(step.get("summary") or ""))}</p>
          <div class="pill-row">
            <span class="pill">{_html(str(step.get("status") or "completed"))}</span>
            <span class="pill">{_html(str(step.get("artifact") or step.get("tool") or ""))}</span>
          </div>
        </article>
        """
        for index, step in enumerate(steps, start=1)
    )
    return f"""
    <section class="panel secondary">
      <label>Agent Trace</label>
      <h2>Guarded autonomous investigation loop</h2>
      <p>The system advances evidence collection and review planning, while final causal judgement and destructive actions stay human-gated.</p>
      <div class="trace-grid">{rows}</div>
    </section>"""


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
    return f"""
    <section class="panel">
      <label>Provider Frontier</label>
      <h2>{int(providers_summary.get("success") or 0)} successful / {int(providers_summary.get("total") or 0)} total</h2>
      <p>Served by the public read-only API from a precomputed review cache. Analysis mode: <code>{_html(provider_mode)}</code>.{generation_note}</p>
      <p>Provider disagreement is preserved as validation work, not collapsed into majority truth.</p>
      <div class="provider-grid">{rows}</div>
    </section>"""


def _precomputed_analysis_context_panel(payload: dict[str, Any]) -> str:
    context = payload.get("analysis_context")
    if not isinstance(context, dict) or not context:
        return ""
    cells = [
        ("DB ingested logs", _human_count(_context_count(context.get("db_ingested_log_count")))),
        ("Model projection", _human_count(_context_count(context.get("model_projection_evidence_items")))),
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
        if value and value != "0"
    )
    log_points = _context_points(context.get("log_observations"))
    source_points = _context_points(context.get("source_observations"))
    conclusion_points = _context_points(context.get("analysis_conclusion"))
    projection_policy = str(context.get("model_projection_policy") or "")
    projection_note = f"<p>{_html(projection_policy)}</p>" if projection_policy else ""
    return f"""
    <section class="panel secondary">
      <label>DB-to-model projection</label>
      <h2>Sanitized logs were persisted, bounded, and then analyzed by providers</h2>
      {projection_note}
      <div class="graph-summary-grid">{cell_html}</div>
      <div class="target-grid">
        <div class="field"><label>Log observations</label>{_points_html(log_points)}</div>
        <div class="field"><label>Source observations</label>{_points_html(source_points)}</div>
        <div class="field full"><label>Analysis conclusion</label>{_points_html(conclusion_points)}</div>
      </div>
    </section>"""


def _context_points(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if str(item).strip()]


def _context_count(value: object) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


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


def _precomputed_review_graph_summary_panel(payload: dict[str, Any]) -> str:
    summary = payload.get("review_graph_summary")
    if not isinstance(summary, dict):
        return ""
    cells = [
        ("Converged", str(int(summary.get("convergence_count") or 0))),
        ("Conflicting", str(int(summary.get("conflict_count") or 0))),
        ("Single-source", str(int(summary.get("single_source_count") or 0))),
        ("Primary promoted", str(int(summary.get("primary_promoted_count") or 0))),
        ("Incident baseline", str(summary.get("incident_baseline") or "open")),
        ("Technical baseline", str(summary.get("technical_baseline") or "open")),
        ("Detection overlap", str(summary.get("provider_detection_overlap") or "unknown")),
        ("Auto-archived", str(int(summary.get("auto_archived_count") or 0))),
    ]
    cell_html = "".join(
        f"""
        <article class="graph-cell">
          <label>{_html(label)}</label>
          <strong>{_html(value)}</strong>
        </article>
        """
        for label, value in cells
    )
    note = str(summary.get("note") or "")
    note_html = f"<p>{_html(note)}</p>" if note else ""
    score_definition = str(summary.get("score_definition") or "")
    score_definition_html = f"<p>{_html(score_definition)}</p>" if score_definition else ""
    return f"""
    <section class="panel secondary">
      <label>Review Graph Arbitration</label>
      <h2>{_html(str(summary.get("summary") or "Provider agreement was evaluated before promotion."))}</h2>
      {note_html}
      {score_definition_html}
      <div class="graph-summary-grid">{cell_html}</div>
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
        f"Technical baseline: {technical}. Incident baseline: {incident}.{definition_text}"
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
    text = f"State: {state}. Blocked because: {reason}. {cap_text}."
    return f"{text} {note}".strip()


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


def _fast_detail_target_card(target: dict[str, Any], *, index: int) -> str:
    score = float(target.get("review_priority_score") or target.get("priority_score") or 0.0)
    title = str(target.get("title") or target.get("core_claim") or target.get("proposal") or f"Review target {index}")
    target_class = str(target.get("class") or target.get("target_class") or target.get("review_mode") or "review_target")
    status = str(target.get("status") or "pending")
    subsystem = str(target.get("subsystem") or target.get("component") or target.get("canonical_review_unit") or "general")
    evidence_refs = target.get("evidence_refs") if isinstance(target.get("evidence_refs"), list) else []
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
    return f"""
<article class="target">
  <div class="target-head">
    <div>
      <label>Target {index}</label>
      <h2>{_html(title)}</h2>
      <div class="pill-row">
        <span class="pill">Class: {_html(target_class)}</span>
        <span class="pill">Status: {_html(status)}</span>
        <span class="pill">Subsystem: {_html(subsystem)}</span>
        <span class="pill">Agreement: {_html(agreement_verdict)}</span>
        <span class="pill">Provider stance: {_html(provider_summary)}</span>
        <span class="pill">Evidence refs: {_html(str(len(evidence_refs)))}</span>
      </div>
    </div>
    <div class="score">{score:.3f}<span>Priority</span></div>
  </div>
  <div class="target-grid">
    <div class="field full"><label>Observed claim</label><p>{_html(claim or title)}</p></div>
    <div class="field full"><label>Provider positions</label>{provider_positions}</div>
    <div class="field full"><label>Agreement and baselines</label><p>{_html(agreement_text)}</p></div>
    <div class="field full"><label>Why not promoted</label><p>{_html(promotion_text)}</p></div>
    <div class="field"><label>Next check</label><p>{_html(action or "Review cited evidence and missing signals.")}</p></div>
    <div class="field"><label>Missing evidence</label><p>{_html("; ".join(str(item) for item in missing[:4]) or "none")}</p></div>
    <div class="field"><label>Evidence refs</label><p>{_html(", ".join(str(item) for item in evidence_refs[:8]) or "none")}</p></div>
    <div class="field"><label>Caveats</label><p>{_html("; ".join(str(item) for item in caveats[:4]) or "none")}</p></div>
  </div>
</article>"""


def _short_sha(value: str) -> str:
    text = str(value or "")
    return text if len(text) <= 24 else f"{text[:12]}...{text[-12:]}"


def _display_policy(value: object) -> str:
    text = str(value or "").strip()
    return text.replace("_", " ").replace("-", " ") if text else "unknown"


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
    label {{ display: block; color: var(--muted); font-size: 12px; font-weight: 800; text-transform: uppercase; margin-bottom: 5px; }}
    strong {{ display: block; font-size: 18px; line-height: 1.25; overflow-wrap: anywhere; }}
    a.button {{ display: inline-block; border: 1px solid var(--line); border-radius: 6px; padding: 8px 10px; color: var(--ink); text-decoration: none; font-weight: 700; }}
    @media (max-width: 760px) {{ main {{ padding: 14px; }} .grid {{ grid-template-columns: 1fr; }} }}
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
    </section>
    <section class="panel">
      <label>Verification</label>
      <p>Covered by <code>{_html(str(verification.get("local_test") or ""))}</code>. Public mode: <code>{_html(str(verification.get("public_mode") or ""))}</code>. Raw logs: <code>{_html(str(verification.get("raw_log_policy") or ""))}</code>.</p>
      <p><a class="button" href="/">Back to public index</a></p>
    </section>
  </main>
</body>
</html>"""


def _html(value: object) -> str:
    import html

    return html.escape(str(value), quote=True)


fast_detail_target_card = _fast_detail_target_card
fast_review_shell = _fast_review_shell
precomputed_review_graph_response = _precomputed_review_graph_response
precomputed_review_payload = _precomputed_review_payload
precomputed_review_target_set = _precomputed_review_target_set
precomputed_summary = _precomputed_summary
public_precomputed_landing_page = _public_precomputed_landing_page
render_rescore_demo_page = _render_rescore_demo_page
render_precomputed_api_page = _render_precomputed_api_page
render_precomputed_graph_page = _render_precomputed_graph_page
render_precomputed_review_detail_page = _render_precomputed_review_detail_page
short_sha = _short_sha
url_quote = _url_quote

__all__ = [
    "fast_detail_target_card",
    "fast_review_shell",
    "precomputed_review_graph_response",
    "precomputed_review_payload",
    "precomputed_review_target_set",
    "precomputed_summary",
    "public_precomputed_landing_page",
    "render_rescore_demo_page",
    "render_precomputed_api_page",
    "render_precomputed_graph_page",
    "render_precomputed_review_detail_page",
    "short_sha",
    "url_quote",
]
