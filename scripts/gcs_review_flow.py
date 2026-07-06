#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from urllib.request import urlopen


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PUBLIC_BASE_URL = "https://ops-evidence.yukimurata0421.dev"
DEFAULT_PROVIDERS = "local-gemini,local-gpt-oss,local-mistral"
DEFAULT_RUN_ID_PREFIX = "review"
DEFAULT_OUTPUT_DIR_NAME = "analyses"
DEFAULT_SERVICE = "stream_v3_runtime"
DEFAULT_ENVIRONMENT = "stream_v3"


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    _require_command("gcloud")
    project_id = _value(args.project_id, "PROJECT_ID", _gcloud_project() or "ops-evidence-synthesis")
    bucket = _value(args.bucket, "BUCKET", f"{project_id}-private-artifacts")
    run_id = _value(args.run_id, "RUN_ID", f"{DEFAULT_RUN_ID_PREFIX}-{time.strftime('%Y%m%d%H%M%S', time.gmtime())}")
    public_base_url = _value(args.public_base_url, "PUBLIC_BASE_URL", DEFAULT_PUBLIC_BASE_URL).rstrip("/")
    provider_mode = _value(args.provider_mode, "PROVIDER_MODE", "local")
    providers = _value(args.providers, "PROVIDERS", DEFAULT_PROVIDERS)
    min_window_hours = _value(args.min_window_hours, "MIN_WINDOW_HOURS", "0")
    output_dir = _absolute_output_dir(_value(args.output_dir, "OUT", str(_default_output_dir(run_id))))

    log_input = _absolute_existing_input_path(
        _required_prompt_value(
            args.input or os.environ.get("LOG_INPUT", ""),
            "Absolute log file or directory",
            "/absolute/path/to/logs.jsonl",
            env_name="LOG_INPUT",
            flag_name="--input",
            no_prompts=args.no_prompts,
        )
    )
    source_root = _optional_source_root(
        args.source_root or os.environ.get("SOURCE_ROOT", "") or os.environ.get("SOURCE_INPUT", ""),
        no_prompts=args.no_prompts,
    )
    service = _required_prompt_value(
        args.service or os.environ.get("SERVICE", ""),
        "Service name",
        DEFAULT_SERVICE,
        env_name="SERVICE",
        flag_name="--service",
        required=False,
        no_prompts=args.no_prompts,
    )
    environment = _required_prompt_value(
        args.environment or os.environ.get("ENVIRONMENT", ""),
        "Environment",
        DEFAULT_ENVIRONMENT,
        env_name="ENVIRONMENT",
        flag_name="--environment",
        required=False,
        no_prompts=args.no_prompts,
    )
    start = _required_prompt_value(
        args.start or os.environ.get("START", ""),
        "Incident window start",
        "2026-06-14T23:15:50Z",
        env_name="START",
        flag_name="--start",
        no_prompts=args.no_prompts,
    )
    end = _required_prompt_value(
        args.end or os.environ.get("END", ""),
        "Incident window end",
        "2026-06-15T23:59:52Z",
        env_name="END",
        flag_name="--end",
        no_prompts=args.no_prompts,
    )

    output_dir.mkdir(parents=True, exist_ok=True)
    sanitized_dir = output_dir / "sanitized"
    evidence_bundle = output_dir / "evidence_bundle.json"
    job_result_path = output_dir / "job_result.json"
    input_bundle_uri = f"gs://{bucket}/job-inputs/{run_id}/evidence_bundle.json"
    source_context_uri = f"gs://{bucket}/job-inputs/{run_id}/source_context_bundle.json"
    source_analysis_uri = f"gs://{bucket}/job-inputs/{run_id}/source_analysis_bundle.json"
    output_prefix_uri = f"gs://{bucket}/job-runs"
    precomputed_prefix_uri = f"gs://{bucket}/precomputed_review_summaries"

    cli = [sys.executable, "-m", "ops_evidence_synthesis.cli"]
    _run_step("Sanitizing logs", [*cli, "sanitize", str(log_input), "--out", str(sanitized_dir)])
    _run_step("Checking sanitized logs", [*cli, "verify-sanitized", str(sanitized_dir)])
    _run_step(
        "Building Evidence Bundle",
        [
            *cli,
            "build-bundle",
            str(sanitized_dir / "sanitized_events.jsonl"),
            "--service",
            service,
            "--environment",
            environment,
            "--start",
            start,
            "--end",
            end,
            "--profile",
            args.profile,
            "--out",
            str(evidence_bundle),
        ]
    )
    _run_step("Uploading Evidence Bundle to GCS", ["gcloud", "storage", "cp", str(evidence_bundle), input_bundle_uri])

    source_context_bundle = None
    source_analysis_bundle = None
    if source_root is not None:
        source_context_dir = output_dir / "source_context"
        source_analysis_dir = output_dir / "source_analysis"
        source_context_bundle = source_context_dir / "source_context_bundle.json"
        source_analysis_bundle = source_analysis_dir / "source_analysis_bundle.json"
        _run_step(
            "Sanitizing source code",
            [
                *cli,
                "sanitize-source",
                "--project-root",
                str(source_root),
                "--service",
                service,
                "--environment",
                environment,
                "--out",
                str(source_context_dir),
            ],
        )
        _run_step("Checking sanitized source code", [*cli, "verify-sanitized", str(source_context_dir)])
        _run_step(
            "Analyzing sanitized source code",
            [
                *cli,
                "analyze-source",
                "--source-context",
                str(source_context_bundle),
                "--provider",
                "local",
                "--out",
                str(source_analysis_dir),
            ],
        )
        _run_step("Checking source analysis", [*cli, "verify-sanitized", str(source_analysis_dir)])
        _run_step(
            "Uploading sanitized source context to GCS",
            ["gcloud", "storage", "cp", str(source_context_bundle), source_context_uri],
        )
        _run_step(
            "Uploading source analysis to GCS",
            ["gcloud", "storage", "cp", str(source_analysis_bundle), source_analysis_uri],
        )

    job_env = dict(os.environ)
    job_env.update(
        {
            "OES_GCS_IO_BACKEND": "gcloud",
            "OES_JOB_INPUT_BUNDLE_URI": input_bundle_uri,
            "OES_JOB_OUTPUT_PREFIX_URI": output_prefix_uri,
            "OES_JOB_PRECOMPUTED_OUTPUT_PREFIX_URI": precomputed_prefix_uri,
            "OES_JOB_RUN_ID": run_id,
            "OES_JOB_PROVIDER_MODE": provider_mode,
            "OES_JOB_PROVIDERS": providers,
            "OES_JOB_MIN_WINDOW_HOURS": min_window_hours,
            "OES_JOB_WRITE_LATEST": "1",
            "PYTHONPATH": str(ROOT / "src"),
        }
    )
    if source_context_bundle is not None:
        job_env["OES_JOB_SOURCE_CONTEXT_URI"] = source_context_uri
    if source_analysis_bundle is not None:
        job_env["OES_JOB_SOURCE_ANALYSIS_URI"] = source_analysis_uri
    result = _run_step(
        "Building human review page",
        [sys.executable, "-m", "ops_evidence_synthesis.gcp.chunked_review_job"],
        env=job_env,
    )
    job_result_path.write_text(result.stdout, encoding="utf-8")
    job_result = json.loads(result.stdout)
    evidence_sha = str(job_result.get("evidence_sha256") or "")
    if not evidence_sha:
        raise SystemExit("job result did not include evidence_sha256")
    review_url = f"{public_base_url}/ui/full-review-page?evidence_sha256={evidence_sha}"

    if not args.no_url_check:
        _check_url(review_url)

    print()
    print("Human review is ready.")
    print(f"Review URL: {review_url}")
    print(f"Local analysis directory: {output_dir}")
    print(f"Sanitized logs: {sanitized_dir}")
    if source_context_bundle is not None and source_analysis_bundle is not None:
        print(f"Sanitized source context: {source_context_bundle}")
        print(f"Source analysis: {source_analysis_bundle}")
    print(f"GCS Evidence Bundle: {input_bundle_uri}")
    print(f"GCS review payload: {job_result.get('precomputed_review_uri', '')}")
    return 0


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Sanitize local logs, stage an Evidence Bundle in GCS, build a review payload, and print the public URL."
    )
    parser.add_argument("--input", default="")
    parser.add_argument("--source-root", default="")
    parser.add_argument("--service", default="")
    parser.add_argument("--environment", default="")
    parser.add_argument("--start", default="")
    parser.add_argument("--end", default="")
    parser.add_argument("--profile", default="generic")
    parser.add_argument("--project-id", default="")
    parser.add_argument("--bucket", default="")
    parser.add_argument("--run-id", default="")
    parser.add_argument("--output-dir", default="")
    parser.add_argument("--public-base-url", default="")
    parser.add_argument("--providers", default="")
    parser.add_argument("--provider-mode", default="")
    parser.add_argument("--min-window-hours", default="")
    parser.add_argument("--no-prompts", action="store_true")
    parser.add_argument("--no-url-check", action="store_true")
    return parser.parse_args(argv)


def _value(cli_value: str, env_name: str, default: str) -> str:
    value = str(cli_value or os.environ.get(env_name, "") or default).strip()
    if not value:
        raise SystemExit(f"{env_name} is required")
    return value


def _required_prompt_value(
    value: str,
    label: str,
    example: str,
    *,
    env_name: str,
    flag_name: str,
    required: bool = True,
    no_prompts: bool,
) -> str:
    text = str(value or "").strip()
    if text:
        return text
    if not required:
        if no_prompts or not sys.stdin.isatty():
            return example
        answer = input(f"{label} [{example}]: ").strip()
        return answer or example
    if no_prompts or not sys.stdin.isatty():
        raise SystemExit(f"{label} is required; set {env_name} or pass {flag_name}. Example: {example}")
    answer = input(f"{label} (example: {example}): ").strip()
    if not answer:
        raise SystemExit(f"{label} is required")
    return answer


def _absolute_existing_input_path(value: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise SystemExit(f"LOG_INPUT must be an absolute path: {value}")
    if not path.exists():
        raise SystemExit(f"log input was not found: {path}")
    return path


def _optional_source_root(value: str, *, no_prompts: bool) -> Path | None:
    text = str(value or "").strip()
    if not text and not no_prompts and sys.stdin.isatty():
        text = input("Source code directory [optional]: ").strip()
    if not text:
        return None
    path = Path(text).expanduser()
    if not path.is_absolute():
        raise SystemExit(f"SOURCE_ROOT must be an absolute path: {text}")
    if not path.exists():
        raise SystemExit(f"source code directory was not found: {path}")
    if not path.is_dir():
        raise SystemExit(f"SOURCE_ROOT must be a directory: {path}")
    return path


def _absolute_output_dir(value: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise SystemExit(f"OUT must be an absolute path: {value}")
    return path


def _default_output_dir(run_id: str) -> Path:
    return ROOT / DEFAULT_OUTPUT_DIR_NAME / run_id


def _gcloud_project() -> str:
    try:
        result = subprocess.run(
            ["gcloud", "config", "get-value", "project"],
            check=True,
            capture_output=True,
            text=True,
        )
    except Exception:
        return ""
    return result.stdout.strip()


def _require_command(name: str) -> None:
    if shutil.which(name) is None:
        raise SystemExit(f"{name} is required on PATH")


def _run_step(
    label: str,
    command: list[str],
    *,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    print(f"{label}...", file=sys.stderr)
    result = _run(command, env=env, capture=True, show_command=False)
    print(f"{label}: done", file=sys.stderr)
    return result


def _run(
    command: list[str],
    *,
    env: dict[str, str] | None = None,
    capture: bool = False,
    show_command: bool = True,
) -> subprocess.CompletedProcess[str]:
    if show_command:
        print("+ " + " ".join(command), file=sys.stderr)
    try:
        return subprocess.run(
            command,
            cwd=ROOT,
            env=env,
            check=True,
            capture_output=capture,
            text=True,
        )
    except subprocess.CalledProcessError as exc:
        print("Command failed: " + " ".join(command), file=sys.stderr)
        if exc.stdout:
            print(exc.stdout, file=sys.stderr)
        if exc.stderr:
            print(exc.stderr, file=sys.stderr)
        raise


def _check_url(url: str) -> None:
    with urlopen(url, timeout=20) as response:
        if response.status != 200:
            raise SystemExit(f"review URL returned HTTP {response.status}: {url}")
        response.read(512)


if __name__ == "__main__":
    raise SystemExit(main())
