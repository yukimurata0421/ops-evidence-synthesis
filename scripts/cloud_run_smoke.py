#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Smoke test a deployed Ops Evidence Synthesis Cloud Run service.")
    parser.add_argument("--base-url", required=True, help="Cloud Run service URL, e.g. https://service-xyz.a.run.app")
    parser.add_argument("--evidence-bundle", required=True, help="Path to sanitized evidence_bundle.json")
    parser.add_argument("--profile-discovery-bundle", required=True, help="Path to profile_discovery_bundle.json")
    parser.add_argument("--profile-draft", required=True, help="Path to profile_draft.json")
    parser.add_argument("--approved-profile", required=True, help="Path to approved_profile.yaml/.json")
    parser.add_argument(
        "--write-token",
        default=os.environ.get("OES_API_WRITE_TOKEN", ""),
        help="Optional write token for deployments protected by OES_API_WRITE_TOKEN.",
    )
    args = parser.parse_args(argv)

    base_url = str(args.base_url).rstrip("/")
    headers = {"X-OES-Write-Token": args.write_token} if args.write_token else None
    evidence_bundle = _load_json(args.evidence_bundle)
    discovery_bundle = _load_json(args.profile_discovery_bundle)
    profile_draft = _load_json(args.profile_draft)
    approved_profile = _load_json(args.approved_profile)

    checks: list[tuple[str, str]] = []
    try:
        status, _ = _request("GET", f"{base_url}/")
        _require(status == 200, f"GET / returned {status}")
        checks.append(("GET /", str(status)))

        status, uploaded = _request("POST", f"{base_url}/bundles/upload", {"bundle": evidence_bundle}, headers=headers)
        _require(status == 200, f"/bundles/upload returned {status}")
        validation = bool((uploaded.get("server_validation") or {}).get("passed"))
        _require(validation, "/bundles/upload did not pass server validation")
        checks.append(("/bundles/upload", f"{status} validation=true"))

        status, discovery = _request(
            "POST",
            f"{base_url}/profile-discovery/upload",
            {"profile_discovery_bundle": discovery_bundle},
            headers=headers,
        )
        _require(status == 200, f"/profile-discovery/upload returned {status}")
        discovery_validation = bool((discovery.get("server_validation") or {}).get("passed"))
        _require(discovery_validation, "/profile-discovery/upload did not pass server validation")
        checks.append(("/profile-discovery/upload", f"{status} validation=true"))

        status, approved = _request(
            "POST",
            f"{base_url}/profile-drafts/approve",
            {
                "profile_draft": profile_draft,
                "profile_id": approved_profile.get("profile_id") or "cloud_run_smoke_approved",
                "approved_by": "cloud-run-smoke",
                "note": "Cloud Run smoke verification",
            },
            headers=headers,
        )
        _require(status == 200, f"/profile-drafts/approve returned {status}")
        explicit = bool(approved.get("explicit_profile") or (approved.get("approved_profile") or {}).get("explicit_profile"))
        _require(explicit, "/profile-drafts/approve did not return an explicit profile")
        checks.append(("/profile-drafts/approve", f"{status} explicit=true"))

        status, planned = _request(
            "POST",
            f"{base_url}/evidence-requests/plan",
            {
                "evidence_bundle": evidence_bundle,
                "approved_profile": approved_profile,
                "planner_answers": None,
            },
            headers=headers,
        )
        _require(status == 200, f"/evidence-requests/plan returned {status}")
        plan = planned.get("plan") if isinstance(planned.get("plan"), dict) else {}
        planner_executes = bool(((plan.get("execution_policy") or {}).get("planner_executes_commands")))
        _require(planner_executes is False, "planner_executes_commands was not false")
        _require("plan_valid" in plan, "planner plan_valid was missing")
        _require(isinstance(plan.get("planner_quality_warnings"), list), "planner_quality_warnings was missing")
        _require(isinstance(plan.get("incident_window"), dict), "incident_window was missing")
        _require("operator_display_timezone" in plan, "operator_display_timezone was missing")
        _require(bool(planned.get("collection_instructions_markdown")), "collection instructions markdown was missing")
        checks.append(("/evidence-requests/plan", f"{status} plan_valid={str(bool(plan.get('plan_valid'))).lower()} warnings={len(plan.get('planner_quality_warnings') or [])}"))

        status, multi_ai = _request(
            "POST",
            f"{base_url}/ai/multi-run",
            {
                "evidence_bundle": evidence_bundle,
                "approved_profile": approved_profile,
                "providers": ["local-gemini", "local-gpt-oss", "local-mistral"],
                "mode": "local",
            },
            headers=headers,
        )
        _require(status == 200, f"/ai/multi-run returned {status}")
        model_runs = multi_ai.get("model_runs")
        _require(isinstance(model_runs, list) and len(model_runs) >= 3, "/ai/multi-run did not return model runs")
        _require(
            all(isinstance(run, dict) and run.get("status") == "ok" for run in model_runs),
            "/ai/multi-run local providers were not all ok",
        )
        synthesis = multi_ai.get("multi_ai_synthesis")
        _require(isinstance(synthesis, dict), "/ai/multi-run did not return synthesis")
        _require(
            str(synthesis.get("score_note")) == "Score is review priority, not truth probability.",
            "/ai/multi-run score note was missing",
        )
        _require(isinstance(synthesis.get("disagreement_themes"), list), "/ai/multi-run disagreement_themes was missing")
        _require(isinstance(synthesis.get("finding_summary"), dict), "/ai/multi-run finding_summary was missing")
        _require(isinstance(multi_ai.get("canonical_review_graph"), dict), "/ai/multi-run canonical_review_graph was missing")
        _require(bool(multi_ai.get("canonical_graph_status")), "/ai/multi-run canonical_graph_status was missing")
        _require(bool(multi_ai.get("canonical_graph_sha256")), "/ai/multi-run canonical_graph_sha256 was missing")
        _require(bool(multi_ai.get("input_fingerprint_sha256")), "/ai/multi-run input_fingerprint_sha256 was missing")
        checks.append(("/ai/multi-run", f"{status} model_runs={len(model_runs)} disagreement_themes={len(synthesis.get('disagreement_themes') or [])} canonical_graph_status={multi_ai.get('canonical_graph_status')}"))

        status, arbitrated = _request(
            "POST",
            f"{base_url}/review/arbitrate",
            {
                "evidence_bundle": evidence_bundle,
                "approved_profile": approved_profile,
                "multi_ai_synthesis": synthesis,
                "model_runs": model_runs,
                "persist": True,
                "persist_if_stale": True,
            },
            headers=headers,
        )
        _require(status == 200, f"/review/arbitrate returned {status}")
        _require(arbitrated.get("canonical_graph_status") == "persisted", "/review/arbitrate did not persist canonical graph snapshot")
        checks.append(("/review/arbitrate", f"{status} status={arbitrated.get('canonical_graph_status')}"))

        evidence_sha256 = str(evidence_bundle.get("evidence_sha256") or "")
        status, graph_response = _request("GET", f"{base_url}/review/graph?evidence_sha256={evidence_sha256}")
        _require(status == 200, f"GET /review/graph returned {status}")
        _require(bool(graph_response.get("canonical_graph_status")), "/review/graph canonical_graph_status was missing")
        _require(isinstance(graph_response.get("canonical_review_graph"), dict), "/review/graph canonical_review_graph was missing")
        checks.append(("/review/graph", f"{status} status={graph_response.get('canonical_graph_status')}"))

        status, html = _request_text("GET", f"{base_url}/?evidence_sha256={evidence_sha256}")
        _require(status == 200, f"GET /?evidence_sha256 returned {status}")
        _require("Multi-AI runs" in html, "UI did not include Multi-AI panel")
        _require("Disagreement Themes" in html, "UI did not include disagreement themes")
        _require("Planner quality warnings" in html, "UI did not include planner quality warnings")
        _require("Canonical Review Graph" in html, "UI did not include canonical graph panel")
        _require("Canonical graph SHA" in html, "UI did not include canonical graph SHA")
        _require("Input fingerprint" in html, "UI did not include input fingerprint")
        _require("Arbitration version" in html, "UI did not include arbitration version")
        checks.append(("UI multi-AI/planner/canonical panels", f"{status} present=true"))
    except (HTTPError, URLError, TimeoutError, ValueError) as exc:
        print("Cloud Run smoke: failed")
        print(str(exc))
        return 2

    print("Cloud Run smoke: passed")
    for name, result in checks:
        print(f"{name}: {result}")
    return 0


def _load_json(path: str | Path) -> dict[str, object]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return payload


def _request(
    method: str,
    url: str,
    payload: dict[str, object] | None = None,
    *,
    headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, object]]:
    data = None
    request_headers = {"Accept": "application/json", **(headers or {})}
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request_headers["Content-Type"] = "application/json"
    request = Request(url, data=data, headers=request_headers, method=method)
    try:
        with urlopen(request, timeout=30) as response:
            body = response.read().decode("utf-8")
            return int(response.status), _json_or_empty(body)
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise ValueError(f"{method} {url} returned {exc.code}: {_redacted_body(body)}") from exc


def _request_text(method: str, url: str, payload: dict[str, object] | None = None) -> tuple[int, str]:
    data = None
    headers = {"Accept": "text/html"}
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = Request(url, data=data, headers=headers, method=method)
    try:
        with urlopen(request, timeout=30) as response:
            body = response.read().decode("utf-8")
            return int(response.status), body
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise ValueError(f"{method} {url} returned {exc.code}: {_redacted_body(body)}") from exc


def _json_or_empty(body: str) -> dict[str, object]:
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _redacted_body(body: str) -> str:
    if len(body) > 1200:
        body = body[:1200] + "...[truncated]"
    return re.sub(r"Authorization:[^\r\n]*", "Authorization:<redacted>", body, flags=re.IGNORECASE)


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
