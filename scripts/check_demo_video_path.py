#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen


DEFAULT_CODE_PROFILE_ID = "31dd5326f0e9e052697975e7174d9de6ebf7c2fde58625cb96ce41f29faab621"
DEFAULT_RUNTIME_REVIEW_ID = "b7d56da85abe109ab044e05d4fc7b40462615e5b230db2b570f717c83762ab96"
DEFAULT_FAST_RUN_ID = "fast-gcp-review-20260712-source-approved-v2-final"
DEFAULT_CROSS_RUN_ID = "fast-cross-check-20260712-source-approved-v2-200-final"
DEFAULT_FAST_REVIEW_ID = "2641cb5fe5850d006864dec4aad3b3d2539e9efcef3753b43d5624f8b6e5136b"
DEFAULT_CROSS_REVIEW_ID = "6eac99d73635678165f54d1c5b82e96e86d0709ad5fcb243129e33f58400a9e5"
LOGIC_REVISION = "source-approved-evidence-v2"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify every public screen used by the hackathon demo video.")
    parser.add_argument("--base-url", default="https://ops-evidence.yukimurata0421.dev")
    parser.add_argument("--timeout-seconds", type=float, default=15.0)
    parser.add_argument("--code-profile-id", default=DEFAULT_CODE_PROFILE_ID)
    parser.add_argument("--runtime-review-id", default=DEFAULT_RUNTIME_REVIEW_ID)
    parser.add_argument("--fast-run-id", default=DEFAULT_FAST_RUN_ID)
    parser.add_argument("--cross-run-id", default=DEFAULT_CROSS_RUN_ID)
    parser.add_argument("--fast-review-id", default=DEFAULT_FAST_REVIEW_ID)
    parser.add_argument("--cross-review-id", default=DEFAULT_CROSS_REVIEW_ID)
    args = parser.parse_args(argv)

    base = str(args.base_url).rstrip("/")
    timeout = float(args.timeout_seconds)
    stamp = str(int(time.time()))

    _check_text(
        "code-profile",
        f"{base}/code-profiles/{quote(args.code_profile_id, safe='')}/?_={stamp}",
        [
            "Gemini Pro Code Profile",
            "Gemini System Reading",
            "Gemini Questions For Human Approval",
            "Normalize With Gemini",
            "Approve Reviewed Interpretation",
            "source_context_is_incident_evidence",
        ],
        timeout,
    )
    _check_text(
        "runtime-review",
        f"{base}/ui/full-review-page?evidence_sha256={quote(args.runtime_review_id, safe='')}&_={stamp}",
        [
            "Agent Trace · ADK tool contract",
            "adk:tool:freeze_evidence_bundle",
            "adk:tool:run_cross_check_providers",
            "adk:tool:validate_citations",
            "adk:tool:compute_review_targets",
            "45,000 sanitized rows",
            "1,036/1,036 Evidence Item",
            "0 primary candidate(s)",
            "Review target requires validation: youtube_health",
            "profile_id=stream_v3_runtime_source_approved_20260711",
        ],
        timeout,
    )
    _check_text(
        "fast-gcp-review-page",
        f"{base}/ui/fast-gcp-review?_={stamp}",
        [
            LOGIC_REVISION,
            "gemini-3.1-flash-lite",
            "Cross-check rows",
            ">200<",
            "Run Live Fast Review",
            "Run Live Cross-check",
        ],
        timeout,
    )

    fast_status = _get_json(
        "fast-status",
        f"{base}/public/fast-gcp-review/status?run_id={quote(args.fast_run_id, safe='')}&_={stamp}",
        timeout,
    )
    _require_successful_run(
        fast_status,
        expected_rows=2000,
        expected_providers=1,
        expected_review_id=args.fast_review_id,
    )
    cross_status = _get_json(
        "cross-status",
        f"{base}/public/fast-gcp-review/status?run_id={quote(args.cross_run_id, safe='')}&_={stamp}",
        timeout,
    )
    _require_successful_run(
        cross_status,
        expected_rows=200,
        expected_providers=2,
        expected_review_id=args.cross_review_id,
    )

    for name, review_id in (
        ("verified-fast-review", args.fast_review_id),
        ("verified-cross-review", args.cross_review_id),
    ):
        _check_text(
            name,
            f"{base}/ui/full-review-page?evidence_sha256={quote(review_id, safe='')}&_={stamp}",
            ["Provider positions", "Review target priority queue", "review priority"],
            timeout,
        )

    _check_text(
        "more-data-rescore-page",
        f"{base}/ui/rescore-demo?id=amazon-notify-more-data-rescore&_={stamp}",
        [
            "More data changed the promotion decision",
            "needs_more_data -&gt; evidence_collected",
            "Run Fixed Rescore",
            "primary_candidate",
        ],
        timeout,
    )
    rescore = _post_json(
        "fixed-rescore",
        f"{base}/public/rescore-demo/run",
        {
            "demo_id": "amazon-notify-more-data-rescore",
            "run_id": f"demo-video-smoke-{stamp}",
        },
        timeout,
    )
    _require(rescore.get("status") == "ok", "fixed-rescore did not return status=ok")
    _require(rescore.get("model_api_called") is False, "fixed-rescore unexpectedly called a model API")
    _require(rescore.get("arbitrary_input_accepted") is False, "fixed-rescore accepted arbitrary input")
    before = rescore.get("before") if isinstance(rescore.get("before"), dict) else {}
    after = rescore.get("after") if isinstance(rescore.get("after"), dict) else {}
    transition = rescore.get("transition") if isinstance(rescore.get("transition"), dict) else {}
    _require(int(before.get("primary_count") or 0) == 0, "fixed-rescore before state is not validation-only")
    _require(int(after.get("primary_count") or 0) == 1, "fixed-rescore did not produce one primary candidate")
    _require(
        transition.get("status") == "needs_more_data -> evidence_collected",
        "fixed-rescore transition is incorrect",
    )
    print("fixed-rescore: transition=needs_more_data -> evidence_collected model_api_called=false")
    print("demo video path smoke: passed")
    return 0


def _require_successful_run(
    payload: dict[str, Any],
    *,
    expected_rows: int,
    expected_providers: int,
    expected_review_id: str,
) -> None:
    _require(payload.get("status") == "succeeded", "saved live run did not succeed")
    _require(payload.get("current_step") == "completed", "saved live run is not complete")
    input_summary = payload.get("input") if isinstance(payload.get("input"), dict) else {}
    providers = payload.get("providers") if isinstance(payload.get("providers"), dict) else {}
    review = payload.get("review") if isinstance(payload.get("review"), dict) else {}
    _require(input_summary.get("logic_revision") == LOGIC_REVISION, "saved run uses an old logic revision")
    _require(int(input_summary.get("sample_rows") or 0) == expected_rows, "saved run row count is incorrect")
    _require(input_summary.get("raw_log_policy") == "not_uploaded", "saved run raw-log policy is incorrect")
    _require(int(providers.get("total") or 0) == expected_providers, "saved run provider count is incorrect")
    _require(int(providers.get("success") or 0) == expected_providers, "saved run provider execution failed")
    _require(int(providers.get("schema_valid") or 0) == expected_providers, "saved run schema validation failed")
    _require(review.get("public_review_id") == expected_review_id, "saved run review ID is incorrect")


def _check_text(name: str, url: str, needles: list[str], timeout: float) -> None:
    status, elapsed, body = _request(name, url, timeout=timeout)
    _require(status == 200, f"{name} returned HTTP {status}")
    missing = [needle for needle in needles if needle not in body]
    _require(not missing, f"{name} missing required text: {', '.join(missing)}")
    print(f"{name}: http=200 elapsed={elapsed:.3f}s required_text=present")


def _get_json(name: str, url: str, timeout: float) -> dict[str, Any]:
    status, elapsed, body = _request(name, url, timeout=timeout, accept="application/json")
    _require(status == 200, f"{name} returned HTTP {status}")
    payload = json.loads(body)
    _require(isinstance(payload, dict), f"{name} did not return a JSON object")
    print(f"{name}: http=200 elapsed={elapsed:.3f}s status={payload.get('status')}")
    return payload


def _post_json(name: str, url: str, payload: dict[str, Any], timeout: float) -> dict[str, Any]:
    body = json.dumps(payload, ensure_ascii=True).encode("utf-8")
    status, elapsed, text = _request(
        name,
        url,
        timeout=timeout,
        accept="application/json",
        method="POST",
        data=body,
        content_type="application/json",
    )
    _require(status == 200, f"{name} returned HTTP {status}: {text[:240]}")
    result = json.loads(text)
    _require(isinstance(result, dict), f"{name} did not return a JSON object")
    print(f"{name}: http=200 elapsed={elapsed:.3f}s status={result.get('status')}")
    return result


def _request(
    name: str,
    url: str,
    *,
    timeout: float,
    accept: str = "text/html,application/json;q=0.9,text/plain;q=0.8",
    method: str = "GET",
    data: bytes | None = None,
    content_type: str = "",
) -> tuple[int, float, str]:
    headers = {"Accept": accept, "User-Agent": "ops-evidence-demo-video-smoke/1.0"}
    if content_type:
        headers["Content-Type"] = content_type
    request = Request(url, data=data, headers=headers, method=method)
    started = time.perf_counter()
    try:
        with urlopen(request, timeout=timeout) as response:
            return int(response.status), time.perf_counter() - started, response.read().decode("utf-8", errors="replace")
    except HTTPError as exc:
        return int(exc.code), time.perf_counter() - started, exc.read().decode("utf-8", errors="replace")
    except URLError as exc:
        raise RuntimeError(f"{name} request failed: {exc}") from exc


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


if __name__ == "__main__":
    raise SystemExit(main())
