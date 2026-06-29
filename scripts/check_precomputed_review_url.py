#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen


ROOT_NEEDLES = [
    "Review Graph Arbitration",
    "DevOps Improvement Loop",
]

PUBLIC_INDEX_NEEDLES = [
    "read-only precomputed reviews",
    "Raw bundles and write APIs are not exposed",
]

DETAIL_NEEDLES = [
    "Provider positions",
    "Agreement and baselines",
    "Promotion gate",
    "Convergence score:",
]

API_VIEW_NEEDLES = [
    "Read-only API View",
    "Summary JSON",
    "Review Targets JSON",
    "Review Graph JSON",
]

VISUAL_GRAPH_NEEDLES = [
    "Review Graph",
    "Nodes and edges",
    "provider",
    "review_target",
]

REVIEW_TARGET_NEEDLES = [
    "precomputed_review_summary",
    "claimed",
    "silent",
]

REVIEW_GRAPH_NEEDLES = [
    "precomputed",
    "review_graph_summary",
    "technical_baseline",
    "nodes",
    "edges",
]

RESCORE_DEMO_NEEDLES = [
    "More data rescore demo",
    "Gemini-led control plane",
    "Provider positions",
    "needs_more_data -&gt; evidence_collected",
    "primary_candidate",
    "user_impact_unverified",
    "qwen-agent-platform",
    "glm-agent-platform",
]

BLOCKED_PUBLIC_READ_PATHS = [
    "/docs",
    "/redoc",
    "/openapi.json",
    "/reviews",
    "/proposals",
    "/comparisons",
    "/clusters",
    "/providers",
    "/workflow/provider-policy",
    "/review-targets",
]

PUBLIC_DEMO_FORBIDDEN_NEEDLES = [
    "local-fail",
    "schema_valid=false",
]

DEEP_REVIEW_CHECKS = {
    "detail",
    "api-view",
    "visual-graph",
    "review-targets",
    "review-graph",
}

TEXT_REVIEW_CHECKS = {
    "detail",
    "api-view",
    "visual-graph",
    "review-graph",
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check a deployed precomputed review page without mutations.")
    parser.add_argument("--base-url", required=True, help="Cloud Run base URL.")
    parser.add_argument("--evidence-sha", required=True, help="Full evidence SHA256.")
    parser.add_argument("--missing-evidence-sha", default="", help="Evidence SHA that must return a clean 404.")
    parser.add_argument("--timeout-seconds", type=float, default=10.0)
    parser.add_argument("--expect-text", action="append", default=[], help="Additional text that must appear on review pages.")
    parser.add_argument("--expect-provider", action="append", default=[], help="Provider id that must appear on review pages.")
    parser.add_argument(
        "--allow-non-valid-provider",
        action="store_true",
        help="Allow schema_valid=false in recorded partial-success real-provider runs.",
    )
    args = parser.parse_args(argv)

    base_url = str(args.base_url).rstrip("/")
    evidence_sha = quote(str(args.evidence_sha), safe="")
    expected_text = [str(item) for item in args.expect_text if str(item)]
    expected_providers = [str(item) for item in args.expect_provider if str(item)]
    forbidden_needles = list(PUBLIC_DEMO_FORBIDDEN_NEEDLES)
    if args.allow_non_valid_provider:
        forbidden_needles = [needle for needle in forbidden_needles if needle != "schema_valid=false"]
    stamp = str(int(time.time()))
    checks = [
        (
            "public-index",
            f"{base_url}/?_={stamp}",
            PUBLIC_INDEX_NEEDLES,
        ),
        (
            "root",
            f"{base_url}/?evidence_sha256={evidence_sha}&_={stamp}",
            ROOT_NEEDLES,
        ),
        (
            "detail",
            f"{base_url}/ui/full-review-page?evidence_sha256={evidence_sha}&_={stamp}",
            DETAIL_NEEDLES,
        ),
        (
            "api-view",
            f"{base_url}/ui/api?evidence_sha256={evidence_sha}&_={stamp}",
            API_VIEW_NEEDLES,
        ),
        (
            "visual-graph",
            f"{base_url}/ui/review-graph?evidence_sha256={evidence_sha}&_={stamp}",
            VISUAL_GRAPH_NEEDLES,
        ),
        (
            "review-targets",
            f"{base_url}/review-targets?evidence_sha256={evidence_sha}&_={stamp}",
            REVIEW_TARGET_NEEDLES,
        ),
        (
            "review-graph",
            f"{base_url}/review/graph?evidence_sha256={evidence_sha}&_={stamp}",
            REVIEW_GRAPH_NEEDLES,
        ),
        (
            "rescore-demo",
            f"{base_url}/ui/rescore-demo?id=amazon-notify-more-data-rescore&_={stamp}",
            RESCORE_DEMO_NEEDLES,
        ),
    ]
    try:
        for name, url, needles in checks:
            status, elapsed, body = _get(url, timeout_seconds=args.timeout_seconds)
            _require(status == 200, f"{name} returned HTTP {status}")
            _require(elapsed <= args.timeout_seconds, f"{name} exceeded {args.timeout_seconds:.1f}s: {elapsed:.3f}s")
            required_needles = list(needles)
            if name in DEEP_REVIEW_CHECKS:
                required_needles.extend(expected_providers)
            if name in TEXT_REVIEW_CHECKS:
                required_needles.extend(expected_text)
            missing = [needle for needle in required_needles if needle not in body]
            _require(not missing, f"{name} missing required text: {', '.join(missing)}")
            _require("Loading saved result" not in body, f"{name} contains loading placeholder")
            _require("Detailed review state is loading" not in body, f"{name} contains detailed loading placeholder")
            _require("Upload Sanitized Evidence Bundle" not in body, f"{name} exposed upload UI")
            _require("Write token" not in body, f"{name} exposed write-token UI")
            forbidden = [needle for needle in forbidden_needles if needle in body]
            _require(not forbidden, f"{name} contains public demo forbidden text: {', '.join(forbidden)}")
            print(f"{name}: http={status} elapsed={elapsed:.3f}s required_text=present")
        health_status, health_elapsed, health_body = _get(
            f"{base_url}/health?_={stamp}",
            timeout_seconds=args.timeout_seconds,
        )
        _require(health_status == 200, f"health returned HTTP {health_status}")
        _require("precomputed_public" in health_body, "health did not report precomputed_public mode")
        print(f"health: http={health_status} elapsed={health_elapsed:.3f}s mode=precomputed_public")
        for path in BLOCKED_PUBLIC_READ_PATHS:
            _check_missing(
                f"blocked-{path.strip('/').replace('/', '-') or 'root'}",
                f"{base_url}{path}?_={stamp}",
                timeout_seconds=args.timeout_seconds,
            )
        if args.missing_evidence_sha:
            missing_sha = quote(str(args.missing_evidence_sha), safe="")
            _check_missing(
                "retired-root",
                f"{base_url}/?evidence_sha256={missing_sha}&_={stamp}",
                timeout_seconds=args.timeout_seconds,
            )
            _check_missing(
                "retired-detail",
                f"{base_url}/ui/full-review-page?evidence_sha256={missing_sha}&_={stamp}",
                timeout_seconds=args.timeout_seconds,
            )
            _check_missing(
                "retired-api-view",
                f"{base_url}/ui/api?evidence_sha256={missing_sha}&_={stamp}",
                timeout_seconds=args.timeout_seconds,
            )
            _check_missing(
                "retired-visual-graph",
                f"{base_url}/ui/review-graph?evidence_sha256={missing_sha}&_={stamp}",
                timeout_seconds=args.timeout_seconds,
            )
            _check_missing(
                "retired-summary",
                f"{base_url}/ui/summary?evidence_sha256={missing_sha}&_={stamp}",
                timeout_seconds=args.timeout_seconds,
            )
            _check_missing(
                "retired-review-targets",
                f"{base_url}/review-targets?evidence_sha256={missing_sha}&_={stamp}",
                timeout_seconds=args.timeout_seconds,
            )
            _check_missing(
                "retired-review-graph",
                f"{base_url}/review/graph?evidence_sha256={missing_sha}&_={stamp}",
                timeout_seconds=args.timeout_seconds,
            )
    except (HTTPError, URLError, TimeoutError, ValueError) as exc:
        print("precomputed review smoke: failed")
        print(str(exc))
        return 2

    print("precomputed review smoke: passed")
    return 0


def _get(url: str, *, timeout_seconds: float) -> tuple[int, float, str]:
    request = Request(url, headers={"Accept": "text/html"}, method="GET")
    started = time.monotonic()
    with urlopen(request, timeout=timeout_seconds) as response:
        body = response.read().decode("utf-8", errors="replace")
        elapsed = time.monotonic() - started
        return int(response.status), elapsed, body


def _check_missing(name: str, url: str, *, timeout_seconds: float) -> None:
    status, elapsed, body = _get_allowing_error(url, timeout_seconds=timeout_seconds)
    _require(status == 404, f"{name} returned HTTP {status}, expected 404")
    _require("Multi-AI disagreement requires validation" not in body, f"{name} returned stale review content")
    _require("Provider positions were not projected" not in body, f"{name} returned stale detail content")
    _require("claimed 1" not in body, f"{name} returned stale provider stance")
    _require("canonical_review_graph" not in body, f"{name} returned stale review graph")
    print(f"{name}: http={status} elapsed={elapsed:.3f}s stale_text=absent")


def _get_allowing_error(url: str, *, timeout_seconds: float) -> tuple[int, float, str]:
    request = Request(url, headers={"Accept": "text/html"}, method="GET")
    started = time.monotonic()
    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            body = response.read().decode("utf-8", errors="replace")
            elapsed = time.monotonic() - started
            return int(response.status), elapsed, body
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        elapsed = time.monotonic() - started
        return int(exc.code), elapsed, body


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
