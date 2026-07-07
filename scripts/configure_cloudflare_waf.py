#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict, dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


DEFAULT_DOMAIN = "ops-evidence.yukimurata0421.dev"
DEFAULT_ROOT_DOMAIN = "yukimurata0421.dev"
DEFAULT_TOKEN_ENV = "CLOUDFLARE_API_TOKEN"
RULE_MARKER = "Ops Evidence public demo"


@dataclass(frozen=True, slots=True)
class PlannedRule:
    description: str
    expression: str
    requests_per_period: int
    period: int
    mitigation_timeout: int


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Configure Cloudflare WAF rate limits for the public demo.")
    parser.add_argument("--domain", default=DEFAULT_DOMAIN)
    parser.add_argument("--root-domain", default=DEFAULT_ROOT_DOMAIN)
    parser.add_argument("--cloudflare-token-env", default=DEFAULT_TOKEN_ENV)
    parser.add_argument("--cloudflare-zone-id", default="")
    parser.add_argument("--read-requests-per-minute", type=int, default=120)
    parser.add_argument("--action-requests-per-minute", type=int, default=8)
    parser.add_argument("--mitigation-timeout", type=int, default=600)
    parser.add_argument("--apply", action="store_true", help="Apply changes. Without this flag, only print the plan.")
    args = parser.parse_args(argv)

    rules = _planned_rules(args)
    print("cloudflare_waf_plan=" + json.dumps([asdict(rule) for rule in rules], sort_keys=True))
    if not args.apply:
        print("cloudflare_waf=plan_only")
        return 0

    token = os.environ.get(args.cloudflare_token_env, "").strip()
    if not token:
        print(f"{args.cloudflare_token_env} is not set.", file=sys.stderr)
        return 2
    zone_id = args.cloudflare_zone_id or _cloudflare_zone_id(token, args.root_domain)
    if not zone_id:
        print(f"Cloudflare zone for {args.root_domain} was not found.", file=sys.stderr)
        return 2

    entrypoint = _cloudflare_request(
        token,
        "GET",
        f"/client/v4/zones/{zone_id}/rulesets/phases/http_ratelimit/entrypoint",
        allow_404=True,
    )
    payload_rules = [_rule_payload(rule) for rule in rules]
    if not entrypoint:
        created = _cloudflare_request(
            token,
            "POST",
            f"/client/v4/zones/{zone_id}/rulesets",
            {
                "name": "Ops Evidence public demo rate limits",
                "description": "Rate limits for public demo reviewer paths.",
                "kind": "zone",
                "phase": "http_ratelimit",
                "rules": payload_rules,
            },
        )
        print("cloudflare_waf=created ruleset_id=" + str((created.get("result") or {}).get("id") or ""))
        return 0

    result = entrypoint.get("result") if isinstance(entrypoint.get("result"), dict) else {}
    ruleset_id = str(result.get("id") or "")
    if not ruleset_id:
        print("Cloudflare entrypoint ruleset id was not returned.", file=sys.stderr)
        return 2
    retained = [
        rule
        for rule in result.get("rules", [])
        if isinstance(rule, dict) and not str(rule.get("description") or "").startswith(RULE_MARKER)
    ]
    updated = _cloudflare_request(
        token,
        "PUT",
        f"/client/v4/zones/{zone_id}/rulesets/{ruleset_id}",
        {
            "name": str(result.get("name") or "zone"),
            "description": str(result.get("description") or "Zone http_ratelimit entrypoint"),
            "kind": "zone",
            "phase": "http_ratelimit",
            "rules": retained + payload_rules,
        },
    )
    print("cloudflare_waf=updated ruleset_id=" + str((updated.get("result") or {}).get("id") or ruleset_id))
    return 0


def _planned_rules(args: argparse.Namespace) -> list[PlannedRule]:
    action_paths = '{"/public/fast-gcp-review" "/public/rescore-demo/run" "/public/fast-gcp-review/owner-session"}'
    return [
        PlannedRule(
            description=f"{RULE_MARKER}: public read paths",
            expression=f'(http.host eq "{args.domain}" and http.request.method in {{"GET" "HEAD"}})',
            requests_per_period=max(1, int(args.read_requests_per_minute)),
            period=60,
            mitigation_timeout=max(60, int(args.mitigation_timeout)),
        ),
        PlannedRule(
            description=f"{RULE_MARKER}: live action paths",
            expression=(
                f'(http.host eq "{args.domain}" and http.request.method eq "POST" '
                f"and http.request.uri.path in {action_paths})"
            ),
            requests_per_period=max(1, int(args.action_requests_per_minute)),
            period=60,
            mitigation_timeout=max(60, int(args.mitigation_timeout)),
        ),
    ]


def _rule_payload(rule: PlannedRule) -> dict[str, Any]:
    return {
        "description": rule.description,
        "expression": rule.expression,
        "action": "block",
        "action_parameters": {
            "response": {
                "status_code": 429,
                "content": "public demo rate limit exceeded",
                "content_type": "text/plain",
            }
        },
        "ratelimit": {
            "characteristics": ["cf.colo.id", "ip.src"],
            "period": rule.period,
            "requests_per_period": rule.requests_per_period,
            "mitigation_timeout": rule.mitigation_timeout,
            "requests_to_origin": True,
        },
    }


def _cloudflare_zone_id(token: str, root_domain: str) -> str:
    response = _cloudflare_request(
        token,
        "GET",
        "/client/v4/zones?" + urlencode({"name": root_domain, "status": "active"}),
    )
    result = response.get("result")
    if not isinstance(result, list) or not result:
        return ""
    first = result[0]
    return str(first.get("id") or "") if isinstance(first, dict) else ""


def _cloudflare_request(
    token: str,
    method: str,
    path: str,
    payload: dict[str, Any] | None = None,
    *,
    allow_404: bool = False,
) -> dict[str, Any] | None:
    data = None
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = Request("https://api.cloudflare.com" + path, data=data, headers=headers, method=method)
    try:
        with urlopen(request, timeout=30) as response:
            body = response.read().decode("utf-8")
    except HTTPError as exc:
        if allow_404 and exc.code == 404:
            return None
        raise RuntimeError(f"Cloudflare API request failed: {method} {path}: {exc}") from exc
    except URLError as exc:
        raise RuntimeError(f"Cloudflare API request failed: {method} {path}: {exc}") from exc
    parsed = _json_or_none(body)
    if not isinstance(parsed, dict) or not parsed.get("success", False):
        raise RuntimeError(f"Cloudflare API request was not successful: {method} {path}: {body[:500]}")
    return parsed


def _json_or_none(raw: str) -> Any:
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
