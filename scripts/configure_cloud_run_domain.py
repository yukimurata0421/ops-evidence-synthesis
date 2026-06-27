#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


DEFAULT_PROJECT = "ops-evidence-synthesis"
DEFAULT_REGION = "asia-northeast1"
DEFAULT_SERVICE = "ops-evidence-api"
DEFAULT_DOMAIN = "ops-evidence.yukimurata0421.dev"
DEFAULT_ROOT_DOMAIN = "yukimurata0421.dev"


@dataclass(frozen=True, slots=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Create or inspect the Cloud Run custom domain mapping for Ops Evidence Synthesis. "
            "If Google domain ownership is not verified yet, this prints the exact next steps."
        )
    )
    parser.add_argument("--project", default=DEFAULT_PROJECT)
    parser.add_argument("--region", default=DEFAULT_REGION)
    parser.add_argument("--service", default=DEFAULT_SERVICE)
    parser.add_argument("--domain", default=DEFAULT_DOMAIN)
    parser.add_argument("--root-domain", default=DEFAULT_ROOT_DOMAIN)
    parser.add_argument("--dry-run", action="store_true", help="Only check state and print planned commands")
    parser.add_argument(
        "--start-verification",
        action="store_true",
        help="Run 'gcloud domains verify'. This may open a browser and wait for manual Search Console work.",
    )
    parser.add_argument(
        "--cloudflare-token-env",
        default="CLOUDFLARE_API_TOKEN",
        help="Env var containing a Cloudflare API token. If unset, DNS instructions are printed.",
    )
    parser.add_argument("--cloudflare-zone-id", default="", help="Optional Cloudflare zone id for root domain")
    parser.add_argument(
        "--apply-cloudflare-dns",
        action="store_true",
        help="Create/update the Cloudflare DNS records returned by Cloud Run. Records are DNS-only.",
    )
    args = parser.parse_args(argv)

    if shutil.which("gcloud") is None:
        print("gcloud was not found in PATH.", file=sys.stderr)
        return 2

    print(f"project={args.project}")
    print(f"region={args.region}")
    print(f"service={args.service}")
    print(f"domain={args.domain}")

    service_url = _service_url(args.project, args.region, args.service)
    if service_url:
        print(f"cloud_run_url={service_url}")

    verified_domains = _verified_domains(args.project)
    print("verified_domains=" + (",".join(verified_domains) if verified_domains else "<none>"))

    if not _domain_is_covered(args.domain, verified_domains):
        print()
        print("Google domain ownership is not verified for this domain yet.")
        print("Run this, complete Search Console verification, and add the TXT record in Cloudflare DNS if requested:")
        print(f"  gcloud domains verify {args.root_domain}")
        print()
        print("After verification finishes, rerun:")
        print("  " + _rerun_command(args, include_apply_dns=args.apply_cloudflare_dns))
        if args.start_verification and not args.dry_run:
            print()
            print("Starting gcloud domain verification now. This may wait for browser/DNS confirmation.")
            verify = _run(["gcloud", "domains", "verify", args.root_domain])
            sys.stdout.write(verify.stdout)
            sys.stderr.write(verify.stderr)
            return verify.returncode
        return 2

    mapping = _describe_mapping(args.project, args.region, args.domain)
    if not mapping:
        create_cmd = [
            "gcloud",
            "beta",
            "run",
            "domain-mappings",
            "create",
            "--service",
            args.service,
            "--domain",
            args.domain,
            "--region",
            args.region,
            "--project",
            args.project,
            "--format=json",
            "--quiet",
        ]
        print()
        print("Cloud Run domain mapping is not present.")
        print("create_command=" + " ".join(create_cmd))
        if args.dry_run:
            return 0
        created = _run(create_cmd)
        if created.returncode != 0:
            sys.stdout.write(created.stdout)
            sys.stderr.write(created.stderr)
            return created.returncode
        mapping = _json_or_none(created.stdout) or _describe_mapping(args.project, args.region, args.domain)

    if not mapping:
        print("Domain mapping was not returned by gcloud.", file=sys.stderr)
        return 2

    records = _resource_records(mapping)
    print()
    print("Cloud Run domain mapping is present.")
    for record in records:
        print(_format_record(record))

    if not records:
        print("No DNS resource records were returned yet. Rerun after Cloud Run finishes provisioning.")
        return 0

    dns_ready = _dns_records_are_present(args.domain, records)
    if args.apply_cloudflare_dns:
        token = os.environ.get(args.cloudflare_token_env, "")
        if not token:
            print()
            print(f"{args.cloudflare_token_env} is not set, so Cloudflare DNS was not changed.")
            if dns_ready:
                print("DNS already resolves to the Cloud Run target. Waiting for Google certificate provisioning.")
            else:
                print(_manual_cloudflare_message(args.domain, records))
            return 2
        zone_id = args.cloudflare_zone_id or _cloudflare_zone_id(token, args.root_domain)
        if not zone_id:
            print(f"Cloudflare zone for {args.root_domain} was not found.", file=sys.stderr)
            return 2
        for record in records:
            _upsert_cloudflare_record(token, zone_id, record)
        print("cloudflare_dns=updated")
    else:
        print()
        if dns_ready:
            print("DNS already resolves to the Cloud Run target. Waiting for Google certificate provisioning.")
        else:
            print(_manual_cloudflare_message(args.domain, records))

    print()
    print(f"public_url=https://{args.domain}/")
    if service_url:
        print(f"fallback_url={service_url}/")
    return 0


def _service_url(project: str, region: str, service: str) -> str:
    result = _run(
        [
            "gcloud",
            "run",
            "services",
            "describe",
            service,
            "--region",
            region,
            "--project",
            project,
            "--format=value(status.url)",
        ]
    )
    return result.stdout.strip() if result.returncode == 0 else ""


def _verified_domains(project: str) -> list[str]:
    result = _run(
        [
            "gcloud",
            "domains",
            "list-user-verified",
            "--project",
            project,
            "--format=json",
        ]
    )
    if result.returncode != 0:
        return []
    payload = _json_or_none(result.stdout)
    if not isinstance(payload, list):
        return []
    domains: list[str] = []
    for item in payload:
        if isinstance(item, str):
            domains.append(item)
        elif isinstance(item, dict):
            domain = item.get("id") or item.get("domain") or item.get("name")
            if isinstance(domain, str):
                domains.append(domain)
    return sorted(set(domain.strip().lower().rstrip(".") for domain in domains if domain.strip()))


def _domain_is_covered(domain: str, verified_domains: list[str]) -> bool:
    normalized = domain.lower().rstrip(".")
    return any(normalized == verified or normalized.endswith("." + verified) for verified in verified_domains)


def _describe_mapping(project: str, region: str, domain: str) -> dict[str, Any] | None:
    result = _run(
        [
            "gcloud",
            "beta",
            "run",
            "domain-mappings",
            "describe",
            "--domain",
            domain,
            "--region",
            region,
            "--project",
            project,
            "--format=json",
        ]
    )
    if result.returncode != 0:
        return None
    payload = _json_or_none(result.stdout)
    return payload if isinstance(payload, dict) else None


def _resource_records(mapping: dict[str, Any]) -> list[dict[str, str]]:
    status = mapping.get("status") if isinstance(mapping.get("status"), dict) else {}
    raw_records = status.get("resourceRecords") if isinstance(status, dict) else None
    if not isinstance(raw_records, list):
        return []
    records: list[dict[str, str]] = []
    for item in raw_records:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").rstrip(".")
        record_type = str(item.get("type") or "").upper()
        rrdata = str(item.get("rrdata") or "").rstrip(".")
        if name and record_type and rrdata:
            records.append({"name": name, "type": record_type, "content": rrdata})
    return records


def _format_record(record: dict[str, str]) -> str:
    return f"dns_record type={record['type']} name={record['name']} content={record['content']}"


def _manual_cloudflare_message(domain: str, records: list[dict[str, str]]) -> str:
    lines = [
        "Cloudflare DNS still needs these DNS-only records. Keep Proxy status as DNS only until Google cert provisioning is complete:",
    ]
    for record in records:
        name = _cloudflare_record_name(domain, record["name"])
        lines.append(f"  {record['type']} {name} -> {record['content']}")
    return "\n".join(lines)


def _dns_records_are_present(domain: str, records: list[dict[str, str]]) -> bool:
    if shutil.which("dig") is None:
        return False
    for record in records:
        query_name = _dns_query_name(domain, record["name"])
        if record["type"] == "CNAME":
            result = _run(["dig", "+short", "CNAME", query_name, "@1.1.1.1"])
            values = {line.strip().rstrip(".").lower() for line in result.stdout.splitlines() if line.strip()}
            if record["content"].rstrip(".").lower() not in values:
                return False
            continue
        result = _run(["dig", "+short", record["type"], query_name, "@1.1.1.1"])
        values = {line.strip().rstrip(".").lower() for line in result.stdout.splitlines() if line.strip()}
        if record["content"].rstrip(".").lower() not in values:
            return False
    return True


def _dns_query_name(domain: str, record_name: str) -> str:
    normalized = record_name.rstrip(".")
    if "." in normalized:
        return normalized
    return domain.rstrip(".")


def _cloudflare_record_name(domain: str, record_name: str) -> str:
    return "@" if record_name.rstrip(".") == domain.rstrip(".") else record_name.rstrip(".")


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


def _upsert_cloudflare_record(token: str, zone_id: str, record: dict[str, str]) -> None:
    params = urlencode({"type": record["type"], "name": record["name"]})
    existing = _cloudflare_request(token, "GET", f"/client/v4/zones/{zone_id}/dns_records?{params}")
    payload = {
        "type": record["type"],
        "name": record["name"],
        "content": record["content"],
        "ttl": 300,
        "proxied": False,
    }
    result = existing.get("result")
    if isinstance(result, list) and result:
        record_id = result[0].get("id") if isinstance(result[0], dict) else ""
        if record_id:
            _cloudflare_request(token, "PUT", f"/client/v4/zones/{zone_id}/dns_records/{record_id}", payload)
            print(f"cloudflare_dns=updated {_format_record(record)}")
            return
    _cloudflare_request(token, "POST", f"/client/v4/zones/{zone_id}/dns_records", payload)
    print(f"cloudflare_dns=created {_format_record(record)}")


def _cloudflare_request(
    token: str,
    method: str,
    path: str,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    data = None
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = Request("https://api.cloudflare.com" + path, data=data, headers=headers, method=method)
    try:
        with urlopen(request, timeout=30) as response:
            body = response.read().decode("utf-8")
    except (HTTPError, URLError) as exc:
        raise RuntimeError(f"Cloudflare API request failed: {method} {path}: {exc}") from exc
    parsed = _json_or_none(body)
    if not isinstance(parsed, dict) or not parsed.get("success", False):
        raise RuntimeError(f"Cloudflare API request was not successful: {method} {path}: {body[:500]}")
    return parsed


def _rerun_command(args: argparse.Namespace, *, include_apply_dns: bool) -> str:
    command = [
        "python3",
        "scripts/configure_cloud_run_domain.py",
        "--project",
        args.project,
        "--region",
        args.region,
        "--service",
        args.service,
        "--domain",
        args.domain,
        "--root-domain",
        args.root_domain,
    ]
    if include_apply_dns:
        command.append("--apply-cloudflare-dns")
    return " ".join(command)


def _json_or_none(raw: str) -> Any:
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def _run(command: list[str]) -> CommandResult:
    completed = subprocess.run(command, check=False, text=True, capture_output=True)
    return CommandResult(completed.returncode, completed.stdout, completed.stderr)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
