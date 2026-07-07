#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import secrets
import shutil
import subprocess
import sys
from dataclasses import dataclass
from typing import Any


DEFAULT_PROJECT = "ops-evidence-synthesis"
DEFAULT_REGION = "asia-northeast1"
DEFAULT_SERVICE = "ops-evidence-api"
DEFAULT_BUDGET_NAME = "Ops Evidence Hackathon Budget"
DEFAULT_TOPIC = "ops-evidence-budget-alerts"
DEFAULT_SUBSCRIPTION = "ops-evidence-budget-guard-fast-gcp-review"
DEFAULT_TOKEN_SECRET = "ops-evidence-budget-guard-token"
DEFAULT_ARTIFACT_BUCKET = "ops-evidence-synthesis-private-artifacts"


@dataclass(frozen=True, slots=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Wire Cloud Billing budget notifications to the Fast GCP Review kill switch."
    )
    parser.add_argument("--project", default=DEFAULT_PROJECT)
    parser.add_argument("--region", default=DEFAULT_REGION)
    parser.add_argument("--service", default=DEFAULT_SERVICE)
    parser.add_argument("--billing-account", default=os.environ.get("BILLING_ACCOUNT", ""))
    parser.add_argument("--budget", default=os.environ.get("BUDGET_ID_OR_NAME", DEFAULT_BUDGET_NAME))
    parser.add_argument("--topic", default=DEFAULT_TOPIC)
    parser.add_argument("--subscription", default=DEFAULT_SUBSCRIPTION)
    parser.add_argument("--token-secret", default=DEFAULT_TOKEN_SECRET)
    parser.add_argument("--disable-threshold", default=os.environ.get("BUDGET_GUARD_DISABLE_THRESHOLD", "0.9"))
    parser.add_argument(
        "--disable-gcs-uri",
        default=os.environ.get(
            "FAST_GCP_REVIEW_DISABLE_GCS_URI",
            f"gs://{DEFAULT_ARTIFACT_BUCKET}/precomputed_review_summaries/public-fast-gcp-review-disabled.json",
        ),
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    if shutil.which("gcloud") is None:
        print("gcloud was not found in PATH.", file=sys.stderr)
        return 2
    if not args.billing_account:
        print("Set --billing-account or BILLING_ACCOUNT.", file=sys.stderr)
        return 2
    topic_name = f"projects/{args.project}/topics/{args.topic}"
    print(f"project={args.project}")
    print(f"budget={args.budget}")
    print(f"topic={topic_name}")
    print(f"subscription={args.subscription}")
    print(f"disable_gcs_uri={args.disable_gcs_uri}")
    if args.dry_run:
        print("budget_guard=plan_only")
        return 0

    service_url = _service_url(args.project, args.region, args.service)
    if not service_url:
        print("Cloud Run service URL was not found.", file=sys.stderr)
        return 2
    budget_name = _resolve_budget(args.billing_account, args.budget)
    if not budget_name:
        print(f"Budget was not found: {args.budget}", file=sys.stderr)
        return 2

    push_endpoint = f"{service_url.rstrip('/')}/internal/budget-guard/fast-gcp-review"
    print(f"service_url={service_url}")
    print(f"resolved_budget={budget_name}")

    _ensure_topic(args.project, args.topic)
    _ensure_secret(args.project, args.token_secret)
    token = _secret_value(args.project, args.token_secret)
    if not token:
        print("Budget guard token secret is empty.", file=sys.stderr)
        return 2
    endpoint_with_token = push_endpoint + "?token=" + token
    _ensure_push_subscription(args.project, args.subscription, args.topic, endpoint_with_token)
    _run_checked(
        [
            "gcloud",
            "billing",
            "budgets",
            "update",
            budget_name,
            "--billing-account",
            args.billing_account,
            "--notifications-rule-pubsub-topic",
            topic_name,
        ]
    )
    _run_checked(
        [
            "gcloud",
            "run",
            "services",
            "update",
            args.service,
            "--project",
            args.project,
            "--region",
            args.region,
            "--update-secrets",
            f"OES_BUDGET_GUARD_TOKEN={args.token_secret}:latest",
            "--update-env-vars",
            (
                f"OES_PUBLIC_FAST_GCP_REVIEW_DISABLE_GCS_URI={args.disable_gcs_uri},"
                f"OES_BUDGET_GUARD_DISABLE_THRESHOLD={args.disable_threshold},"
                "OES_PUBLIC_FAST_GCP_REVIEW_DISABLE_CACHE_SECONDS=30"
            ),
        ]
    )
    print("budget_guard=configured")
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


def _resolve_budget(billing_account: str, budget: str) -> str:
    if budget.startswith("billingAccounts/") or "/budgets/" in budget:
        return budget
    result = _run(
        [
            "gcloud",
            "billing",
            "budgets",
            "list",
            "--billing-account",
            billing_account,
            "--format=json",
        ]
    )
    if result.returncode != 0:
        return ""
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        return ""
    if not isinstance(payload, list):
        return ""
    for item in payload:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "")
        display = str(item.get("displayName") or "")
        if budget == display or name.endswith("/" + budget):
            return name
    return ""


def _ensure_topic(project: str, topic: str) -> None:
    described = _run(["gcloud", "pubsub", "topics", "describe", topic, "--project", project])
    if described.returncode == 0:
        return
    _run_checked(["gcloud", "pubsub", "topics", "create", topic, "--project", project])


def _ensure_secret(project: str, secret_name: str) -> None:
    described = _run(["gcloud", "secrets", "describe", secret_name, "--project", project])
    if described.returncode != 0:
        _run_checked(
            [
                "gcloud",
                "secrets",
                "create",
                secret_name,
                "--project",
                project,
                "--replication-policy",
                "automatic",
            ]
        )
    if _secret_value(project, secret_name):
        return
    token = secrets.token_urlsafe(32)
    _run_checked(
        ["gcloud", "secrets", "versions", "add", secret_name, "--project", project, "--data-file=-"],
        stdin=token + "\n",
    )


def _secret_value(project: str, secret_name: str) -> str:
    result = _run(
        [
            "gcloud",
            "secrets",
            "versions",
            "access",
            "latest",
            "--secret",
            secret_name,
            "--project",
            project,
        ]
    )
    return result.stdout.strip() if result.returncode == 0 else ""


def _ensure_push_subscription(project: str, subscription: str, topic: str, endpoint: str) -> None:
    described = _run(["gcloud", "pubsub", "subscriptions", "describe", subscription, "--project", project])
    command = "update" if described.returncode == 0 else "create"
    args = [
        "gcloud",
        "pubsub",
        "subscriptions",
        command,
        subscription,
        "--project",
        project,
        "--push-endpoint",
        endpoint,
        "--expiration-period",
        "never",
        "--ack-deadline",
        "30",
    ]
    if command == "create":
        args.extend(["--topic", topic, "--topic-project", project])
    _run_checked(args)


def _run(command: list[str], *, stdin: str | None = None) -> CommandResult:
    completed = subprocess.run(command, input=stdin, check=False, text=True, capture_output=True)
    return CommandResult(completed.returncode, completed.stdout, completed.stderr)


def _run_checked(command: list[str], *, stdin: str | None = None) -> CommandResult:
    result = _run(command, stdin=stdin)
    if result.returncode != 0:
        sys.stdout.write(result.stdout)
        sys.stderr.write(result.stderr)
        raise SystemExit(result.returncode)
    return result


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
