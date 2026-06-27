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
    "1 converged technical support signal",
    "DevOps Improvement Loop",
    "claimed 2 / silent 1",
    "claimed 1 / silent 2",
]

DETAIL_NEEDLES = [
    "Provider positions",
    "Agreement and baselines",
    "Why not promoted",
    "Convergence score: 0.667",
    "Convergence score: 0.333",
]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check a deployed precomputed review page without mutations.")
    parser.add_argument("--base-url", required=True, help="Cloud Run base URL.")
    parser.add_argument("--evidence-sha", required=True, help="Full evidence SHA256.")
    parser.add_argument("--timeout-seconds", type=float, default=10.0)
    args = parser.parse_args(argv)

    base_url = str(args.base_url).rstrip("/")
    evidence_sha = quote(str(args.evidence_sha), safe="")
    stamp = str(int(time.time()))
    checks = [
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
    ]
    try:
        for name, url, needles in checks:
            status, elapsed, body = _get(url, timeout_seconds=args.timeout_seconds)
            _require(status == 200, f"{name} returned HTTP {status}")
            _require(elapsed <= args.timeout_seconds, f"{name} exceeded {args.timeout_seconds:.1f}s: {elapsed:.3f}s")
            missing = [needle for needle in needles if needle not in body]
            _require(not missing, f"{name} missing required text: {', '.join(missing)}")
            _require("Loading saved result" not in body, f"{name} contains loading placeholder")
            _require("Detailed review state is loading" not in body, f"{name} contains detailed loading placeholder")
            print(f"{name}: http={status} elapsed={elapsed:.3f}s required_text=present")
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


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
