#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from urllib.request import urlopen

from ops_evidence_synthesis.timeutils import format_timestamp


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PUBLIC_BASE_URL = "https://ops-evidence.yukimurata0421.dev"
DEFAULT_PROVIDERS = "local-gemini,local-gpt-oss,local-mistral,local-qwen,local-gemma"
DEFAULT_RUN_ID_PREFIX = "review"
DEFAULT_OUTPUT_DIR_NAME = "analyses"
DEFAULT_SERVICE = "stream_v3_runtime"
DEFAULT_ENVIRONMENT = "stream_v3"
_PENDING_PROMPT_LINES: list[str] = []
_PENDING_TIMESTAMP_LINES: list[str] = []


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    _require_command("gcloud")
    project_id = _value(args.project_id, "PROJECT_ID", _gcloud_project() or "ops-evidence-synthesis")
    bucket = _value(args.bucket, "BUCKET", f"{project_id}-private-artifacts")
    run_id = _value(args.run_id, "RUN_ID", f"{DEFAULT_RUN_ID_PREFIX}-{time.strftime('%Y%m%d%H%M%S', time.gmtime())}")
    public_base_url = _value(args.public_base_url, "PUBLIC_BASE_URL", DEFAULT_PUBLIC_BASE_URL).rstrip("/")
    static_review_base_url = _optional_value(
        args.static_review_base_url,
        "STATIC_REVIEW_BASE_URL",
        f"{public_base_url}/reviews",
    ).rstrip("/")
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
        args.source_root
        or os.environ.get("SOURCE_ROOTS", "")
        or os.environ.get("SOURCE_ROOT", "")
        or os.environ.get("SOURCE_INPUT", ""),
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
    start = _required_timestamp_value(
        args.start or os.environ.get("START", ""),
        "Incident window start",
        "2026-06-14T23:15:50Z",
        env_name="START",
        flag_name="--start",
        no_prompts=args.no_prompts,
    )
    end = _required_timestamp_value(
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
    code_profile_pages_prefix_uri = f"gs://{bucket}/code-profile-pages"
    output_prefix_uri = f"gs://{bucket}/job-runs"
    precomputed_prefix_uri = f"gs://{bucket}/precomputed_review_summaries"
    static_review_prefix_uri = f"gs://{bucket}/review-pages"

    cli = [sys.executable, "-m", "ops_evidence_synthesis.cli"]
    source_context_bundle = None
    source_analysis_bundle = None
    code_profile_url = ""
    code_profile_report_url = ""
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
            "Building source mapping candidates",
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
        _run_step("Checking source mapping candidates", [*cli, "verify-sanitized", str(source_analysis_dir)])
        code_profile_id = _code_profile_public_id(
            run_id=run_id,
            source_context_bundle=source_context_bundle,
            source_analysis_bundle=source_analysis_bundle,
        )
        code_profile_url = f"{public_base_url}/code-profiles/{code_profile_id}/"
        code_profile_report_url = f"{code_profile_url.rstrip('/')}/report.md"
        code_profile_dir = output_dir / "code_profile_review"
        code_profile_artifacts = _write_code_profile_review_artifacts(
            output_dir=code_profile_dir,
            run_id=run_id,
            code_profile_id=code_profile_id,
            code_profile_url=code_profile_url,
            code_profile_report_url=code_profile_report_url,
            source_root=source_root,
            source_context_bundle=source_context_bundle,
            source_context_report=source_context_dir / "source_context_report.md",
            source_analysis_bundle=source_analysis_bundle,
            source_analysis_report=source_analysis_dir / "source_analysis_report.md",
        )
        _run_step(
            "Uploading code profile review page",
            [
                "gcloud",
                "storage",
                "cp",
                str(code_profile_artifacts["html"]),
                str(code_profile_artifacts["markdown"]),
                str(code_profile_artifacts["payload"]),
                f"{code_profile_pages_prefix_uri.rstrip('/')}/{code_profile_id}/",
            ],
        )
        _confirm_code_profile_before_log_analysis(
            source_root=source_root,
            source_context_bundle=source_context_bundle,
            source_context_report=source_context_dir / "source_context_report.md",
            source_analysis_bundle=source_analysis_bundle,
            source_analysis_report=source_analysis_dir / "source_analysis_report.md",
            approval_record_path=output_dir / "code_profile_approval.json",
            code_profile_url=code_profile_url,
            code_profile_report_url=code_profile_report_url,
            no_prompts=args.no_prompts,
            skip_confirmation=args.skip_source_confirmation,
        )

    _run_step(
        "Sanitizing logs",
        [
            *cli,
            "sanitize",
            str(log_input),
            "--out",
            str(sanitized_dir),
            "--start",
            start,
            "--end",
            end,
        ],
    )
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

    if source_root is not None:
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
            "OES_JOB_STATIC_REVIEW_OUTPUT_PREFIX_URI": static_review_prefix_uri,
            "OES_JOB_STATIC_REVIEW_PUBLIC_BASE_URL": static_review_base_url,
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
    review_url = str(job_result.get("static_review_public_url") or "").strip()
    if not review_url:
        review_url = f"{public_base_url}/ui/full-review-page?evidence_sha256={evidence_sha}"
    legacy_review_url = f"{public_base_url}/ui/full-review-page?evidence_sha256={evidence_sha}"
    report_url = str(job_result.get("static_review_report_url") or "").strip()
    if not report_url:
        report_url = f"{public_base_url}/ui/report.md?evidence_sha256={evidence_sha}"

    if not args.no_url_check:
        _check_url(review_url)
        if legacy_review_url != review_url:
            _check_url(legacy_review_url)

    _print_review_summary(
        review_url=review_url,
        report_url=report_url,
        legacy_review_url=legacy_review_url,
        code_profile_url=code_profile_url,
        code_profile_report_url=code_profile_report_url,
        output_dir=output_dir,
        sanitized_dir=sanitized_dir,
        source_context_bundle=source_context_bundle,
        source_analysis_bundle=source_analysis_bundle,
        input_bundle_uri=input_bundle_uri,
        precomputed_review_uri=str(job_result.get("precomputed_review_uri", "")),
        static_review_html_uri=str(job_result.get("static_review_html_uri", "")),
        static_review_report_uri=str(job_result.get("static_review_report_uri", "")),
        show_gcs_uris=args.show_gcs_uris,
    )
    return 0


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Sanitize local logs, stage an Evidence Bundle in GCS, build a review payload, and print the public URL."
    )
    parser.add_argument("--input", default="")
    parser.add_argument("--source-root", action="append", default=[])
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
    parser.add_argument("--static-review-base-url", default="")
    parser.add_argument("--providers", default="")
    parser.add_argument("--provider-mode", default="")
    parser.add_argument("--min-window-hours", default="")
    parser.add_argument("--no-prompts", action="store_true")
    parser.add_argument("--no-url-check", action="store_true")
    parser.add_argument("--show-gcs-uris", action="store_true", help="Print private gs:// artifact URIs after the HTTP review URLs.")
    parser.add_argument(
        "--skip-source-confirmation",
        action="store_true",
        help="Do not pause for the local code profile review before log analysis.",
    )
    return parser.parse_args(argv)


def _value(cli_value: str, env_name: str, default: str) -> str:
    value = str(cli_value or os.environ.get(env_name, "") or default).strip()
    if not value:
        raise SystemExit(f"{env_name} is required")
    return value


def _optional_value(cli_value: str, env_name: str, default: str) -> str:
    return str(cli_value or os.environ.get(env_name, "") or default).strip()


def _print_review_summary(
    *,
    review_url: str,
    report_url: str,
    legacy_review_url: str,
    code_profile_url: str,
    code_profile_report_url: str,
    output_dir: Path,
    sanitized_dir: Path,
    source_context_bundle: Path | None,
    source_analysis_bundle: Path | None,
    input_bundle_uri: str,
    precomputed_review_uri: str,
    static_review_html_uri: str,
    static_review_report_uri: str,
    show_gcs_uris: bool,
) -> None:
    print()
    print("Human review is ready.")
    print(f"Review URL: {review_url}")
    print(f"Markdown report URL: {report_url}")
    if review_url != legacy_review_url:
        print(f"Dynamic review URL: {legacy_review_url}")
    if code_profile_url:
        print(f"Code profile URL: {code_profile_url}")
    if code_profile_report_url:
        print(f"Code profile Markdown URL: {code_profile_report_url}")
    print(f"Local analysis directory: {output_dir}")
    print(f"Sanitized logs: {sanitized_dir}")
    if source_context_bundle is not None and source_analysis_bundle is not None:
        print(f"Sanitized source context: {source_context_bundle}")
        print(f"Source analysis: {source_analysis_bundle}")
    if show_gcs_uris:
        print(f"GCS Evidence Bundle: {input_bundle_uri}")
        print(f"GCS review payload: {precomputed_review_uri}")
        print(f"GCS review HTML: {static_review_html_uri}")
        print(f"GCS review Markdown: {static_review_report_uri}")


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
        if not required and _looks_like_misplaced_source_root_answer(text):
            recovered = _timestamp_suffix_after_existing_dir(text)
            if recovered:
                _PENDING_TIMESTAMP_LINES.append(recovered)
            return example
        return text
    if not required:
        if no_prompts or not sys.stdin.isatty():
            return example
        answer = _read_prompt_line(f"{label} [{example}]: ").strip()
        if _looks_like_misplaced_source_root_answer(answer):
            recovered = _timestamp_suffix_after_existing_dir(answer)
            if recovered:
                _PENDING_TIMESTAMP_LINES.append(recovered)
            return example
        return answer or example
    if no_prompts or not sys.stdin.isatty():
        raise SystemExit(f"{label} is required; set {env_name} or pass {flag_name}. Example: {example}")
    answer = _read_prompt_line(f"{label} (example: {example}): ").strip()
    if not answer:
        raise SystemExit(f"{label} is required")
    return answer


def _required_timestamp_value(
    value: str,
    label: str,
    example: str,
    *,
    env_name: str,
    flag_name: str,
    no_prompts: bool,
) -> str:
    if not value and _PENDING_TIMESTAMP_LINES:
        value = _PENDING_TIMESTAMP_LINES.pop(0)
    while True:
        text = _required_prompt_value(
            value,
            label,
            example,
            env_name=env_name,
            flag_name=flag_name,
            no_prompts=no_prompts,
        )
        if _looks_like_misplaced_source_root_answer(text):
            recovered = _timestamp_suffix_after_existing_dir(text)
            if recovered:
                return format_timestamp(recovered)
            if no_prompts or not sys.stdin.isatty():
                raise SystemExit(f"{env_name} must be ISO-8601 date/time, got: {text}")
            value = ""
            continue
        try:
            return format_timestamp(text)
        except (TypeError, ValueError) as exc:
            recovered = _timestamp_suffix_after_existing_dir(text)
            if recovered:
                return format_timestamp(recovered)
            raise SystemExit(f"{env_name} must be ISO-8601 date/time, got: {text}") from exc


def _absolute_existing_input_path(value: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise SystemExit(f"LOG_INPUT must be an absolute path: {value}")
    if not path.exists():
        raise SystemExit(f"log input was not found: {path}")
    return path


def _confirm_code_profile_before_log_analysis(
    *,
    source_root: Path,
    source_context_bundle: Path,
    source_context_report: Path,
    source_analysis_bundle: Path,
    source_analysis_report: Path,
    approval_record_path: Path | None,
    code_profile_url: str,
    code_profile_report_url: str,
    no_prompts: bool,
    skip_confirmation: bool,
) -> None:
    if no_prompts or skip_confirmation or not sys.stdin.isatty():
        return
    summary = _code_profile_summary(source_context_bundle, source_analysis_bundle)
    print(file=sys.stderr)
    print("Code profile human review is ready.", file=sys.stderr)
    print(f"Code profile URL: {code_profile_url}", file=sys.stderr)
    print(f"Code profile Markdown URL: {code_profile_report_url}", file=sys.stderr)
    print(f"Selected source root: {source_root}", file=sys.stderr)
    print(f"Human-readable source context report: {source_context_report}", file=sys.stderr)
    print(f"Human-readable source analysis report: {source_analysis_report}", file=sys.stderr)
    if summary:
        print(f"Detected project type: {summary.get('detected_project_type') or 'unknown'}", file=sys.stderr)
        print(
            "Source/config counts: "
            f"{summary.get('source_item_count', 0)} source items, "
            f"{summary.get('config_item_count', 0)} config items",
            file=sys.stderr,
        )
        entrypoints = summary.get("entrypoint_candidates") or []
        if entrypoints:
            print(f"Entrypoint candidates: {', '.join(entrypoints[:8])}", file=sys.stderr)
        print(
            "Source mapping candidates: "
            f"{summary.get('component_candidate_count', 0)} components, "
            f"{summary.get('metric_semantics_candidate_count', 0)} metric semantics, "
            f"{summary.get('collector_mapping_candidate_count', 0)} collector mappings",
            file=sys.stderr,
        )
    print("Open the Code profile URL and approve only after checking the review checklist.", file=sys.stderr)
    answer = _read_prompt_line("After human review, type APPROVE to start log analysis [N]: ").strip()
    if answer.casefold() != "approve":
        print(
            "Stopped before log analysis. Review the code profile URL, then rerun or type APPROVE when ready.",
            file=sys.stderr,
        )
        raise SystemExit(0)
    _write_code_profile_approval_record(
        approval_record_path=approval_record_path,
        source_root=source_root,
        source_context_bundle=source_context_bundle,
        source_analysis_bundle=source_analysis_bundle,
        code_profile_url=code_profile_url,
        code_profile_report_url=code_profile_report_url,
        summary=summary,
    )


def _code_profile_summary(source_context_bundle: Path, source_analysis_bundle: Path) -> dict[str, object]:
    try:
        source_context = json.loads(source_context_bundle.read_text(encoding="utf-8"))
        source_analysis = json.loads(source_analysis_bundle.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(source_context, dict):
        return {}
    if not isinstance(source_analysis, dict):
        source_analysis = {}
    project_summary = (
        source_context.get("project_summary") if isinstance(source_context.get("project_summary"), dict) else {}
    )
    display_summary = (
        source_analysis.get("display_summary") if isinstance(source_analysis.get("display_summary"), dict) else {}
    )
    return {
        "detected_project_type": project_summary.get("detected_project_type") or "",
        "entrypoint_candidates": list(project_summary.get("entrypoint_candidates") or []),
        "source_item_count": len(source_context.get("source_items") or []),
        "config_item_count": len(source_context.get("config_items") or []),
        "component_candidate_count": display_summary.get("component_candidate_count")
        if "component_candidate_count" in display_summary
        else len(source_analysis.get("component_candidates") or []),
        "metric_semantics_candidate_count": display_summary.get("metric_semantics_candidate_count")
        if "metric_semantics_candidate_count" in display_summary
        else len(source_analysis.get("metric_semantics_candidates") or []),
        "collector_mapping_candidate_count": display_summary.get("collector_mapping_candidate_count")
        if "collector_mapping_candidate_count" in display_summary
        else len(source_analysis.get("collector_mapping_candidates") or []),
    }


def _code_profile_public_id(*, run_id: str, source_context_bundle: Path, source_analysis_bundle: Path) -> str:
    summary = _code_profile_summary(source_context_bundle, source_analysis_bundle)
    material = {
        "run_id": str(run_id or ""),
        "source_context_sha256": _json_field(source_context_bundle, "source_context_sha256"),
        "analysis_sha256": _json_field(source_analysis_bundle, "analysis_sha256"),
        "summary": summary,
    }
    return hashlib.sha256(json.dumps(material, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _write_code_profile_approval_record(
    *,
    approval_record_path: Path | None,
    source_root: Path,
    source_context_bundle: Path,
    source_analysis_bundle: Path,
    code_profile_url: str,
    code_profile_report_url: str,
    summary: dict[str, object],
) -> None:
    if approval_record_path is None:
        return
    approval_record_path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "schema_version": "code_profile_human_approval.v1",
        "approved": True,
        "approved_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "approval_gate": "source_profile_before_log_analysis",
        "source_root_name": source_root.name or "source-root",
        "local_absolute_path_uploaded": False,
        "code_profile_url": code_profile_url,
        "code_profile_report_url": code_profile_report_url,
        "source_context_sha256": _json_field(source_context_bundle, "source_context_sha256"),
        "analysis_sha256": _json_field(source_analysis_bundle, "analysis_sha256"),
        "summary": summary,
    }
    approval_record_path.write_text(
        json.dumps(record, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )


SIGNAL_CATEGORIES = (
    (
        "ADS-B data freshness and aircraft state",
        ("adsb_freshness", "aircraft", "position", "messages_last_change", "stream1090"),
        "Shows whether aircraft data is still moving and whether the feed is stale or missing.",
    ),
    (
        "Video stream transport",
        ("ffmpeg", "rtmp", "rtmps", "tcp", "notsent", "upload", "ssl_tls", "retrans"),
        "Shows whether the live stream send path, socket backlog, TLS, or upload pressure is unhealthy.",
    ),
    (
        "YouTube publication and public health",
        ("youtube", "yt_", "video_resolver", "public_probe", "watchdog", "quota"),
        "Shows whether the public live page, resolver, API usage, and watchdog state still agree.",
    ),
    (
        "Recovery and supervisor control",
        ("recovery", "restart", "systemd", "watchdog", "blocked", "orchestrator", "remote"),
        "Shows whether automatic recovery, restart control, and supervisor actions fired or were blocked.",
    ),
    (
        "Memory and host capacity",
        ("memory", "mem_", "swap", "rss", "pss", "fd_", "container", "slab", "available"),
        "Shows whether the host or runtime is close to memory, swap, process, or descriptor pressure.",
    ),
    (
        "Audio and automatic DJ path",
        ("audio", "dj", "pulse", "sink", "now_playing", "artist", "capture"),
        "Shows whether audio capture, playback, DJ scheduling, and audible output are healthy.",
    ),
    (
        "Network, WAN, and CPE observation",
        ("network", "wan", "cpe", "dns", "ipv4", "ipv6", "keepalive"),
        "Shows whether local network identity, router events, DNS, or WAN state may explain symptoms.",
    ),
    (
        "Health, SLO, and freshness",
        ("slo", "health", "freshness", "age_seconds", "degraded", "warn", "fail", "heartbeat"),
        "Shows whether monitoring considers the system fresh, degraded, warning, or failed.",
    ),
)


def _code_profile_interpretation(
    source_context: dict[str, object],
    source_analysis: dict[str, object],
) -> dict[str, object]:
    project_summary = (
        source_context.get("project_summary") if isinstance(source_context.get("project_summary"), dict) else {}
    )
    entrypoints = _string_list(project_summary.get("entrypoint_candidates"))
    unit_names = _systemd_unit_names(source_context)
    component_names = _component_names(source_analysis)
    metric_names = _metric_names(source_context, source_analysis)
    surface_names = _unique(metric_names + component_names + unit_names)
    categories = _matched_signal_categories(surface_names)
    return {
        "system_purpose": _system_purpose(entrypoints, unit_names, categories),
        "key_runtime_surfaces": _key_runtime_surfaces(entrypoints, unit_names, component_names),
        "runtime_measurements": categories,
        "do_not_break": _do_not_break(categories),
        "human_review_questions": _human_review_questions(categories),
    }


def _system_purpose(
    entrypoints: list[str],
    unit_names: list[str],
    categories: list[dict[str, object]],
) -> list[str]:
    haystack = " ".join(entrypoints + unit_names).casefold()
    category_names = {str(row.get("name") or "") for row in categories}
    if "adsb" in haystack and ("youtube" in haystack or "ffmpeg" in haystack or "rtmp" in haystack):
        primary = (
            "This code appears to operate an ADS-B live streaming runtime: it keeps aircraft data, "
            "audio, ffmpeg/RTMPS upload, YouTube publication, and health reporting moving under systemd."
        )
    elif "systemd" in haystack or unit_names:
        primary = "This code appears to operate a systemd-managed runtime with background workers and health checks."
    else:
        primary = "This code appears to operate an application runtime with source, config, and monitoring surfaces."
    details = [
        primary,
        (
            "The profile is source/config context only. It should guide which runtime logs to inspect, "
            "but it does not prove that any runtime event happened."
        ),
    ]
    if "Recovery and supervisor control" in category_names:
        details.append("Recovery workers and supervisor units are part of the runtime contract, not just monitoring noise.")
    if "Video stream transport" in category_names:
        details.append("Transport health is a first-class concern because stream continuity depends on ffmpeg, socket, and upload signals.")
    return details


def _key_runtime_surfaces(
    entrypoints: list[str],
    unit_names: list[str],
    component_names: list[str],
) -> list[str]:
    surfaces: list[str] = []
    for label, patterns in (
        ("Stream and publishing units", ("youtube", "ffmpeg", "rtmp", "rtmps", "stream_watchdog")),
        ("Recovery and watchdog units", ("recovery", "watchdog", "restart", "orchestrator")),
        ("Resource and memory guardrails", ("memory", "resource", "swap", "arena")),
        ("Network and WAN observers", ("network", "wan", "cpe", "tcp", "netlink")),
        ("Telemetry exporters and reports", ("prometheus", "report", "status", "notify")),
        ("Audio and DJ workers", ("audio", "dj", "pulse")),
    ):
        examples = _examples_matching(entrypoints + unit_names + component_names, patterns, limit=4)
        if examples:
            surfaces.append(f"{label}: {', '.join(examples)}")
    return surfaces or ["No high-signal runtime surface could be inferred; review the entrypoints before continuing."]


def _matched_signal_categories(names: list[str]) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for label, patterns, interpretation in SIGNAL_CATEGORIES:
        examples = _examples_matching(names, patterns, limit=8)
        if examples:
            rows.append({"name": label, "interpretation": interpretation, "examples": examples})
    return rows or [
        {
            "name": "Unclassified runtime signals",
            "interpretation": "The code exposes candidates, but they did not match known operational categories.",
            "examples": names[:8],
        }
    ]


def _do_not_break(categories: list[dict[str, object]]) -> list[str]:
    names = {str(row.get("name") or "") for row in categories}
    invariants = [
        "Do not turn source/config hints into incident conclusions without cited sanitized log Evidence Items.",
        "Do not require raw source, raw environment values, credential files, or unsanitized logs for the review.",
    ]
    if "ADS-B data freshness and aircraft state" in names:
        invariants.append("Do not break ADS-B message freshness, aircraft count continuity, or stale-feed detection.")
    if "Video stream transport" in names:
        invariants.append("Do not break ffmpeg/RTMPS upload continuity, socket backpressure detection, or stream restart accounting.")
    if "YouTube publication and public health" in names:
        invariants.append("Do not break YouTube live identity, public page health, resolver freshness, or API quota guardrails.")
    if "Recovery and supervisor control" in names:
        invariants.append("Do not make recovery actions more destructive or bypass the existing blocked-action and supervisor checks.")
    if "Memory and host capacity" in names:
        invariants.append("Do not hide memory, swap, file descriptor, or process pressure signals used by runtime guardrails.")
    if "Audio and automatic DJ path" in names:
        invariants.append("Do not break audio capture, playback sink, or automatic DJ continuity signals.")
    if "Network, WAN, and CPE observation" in names:
        invariants.append("Do not collapse network/WAN observer signals into generic failures; keep them separable from stream symptoms.")
    return invariants


def _human_review_questions(categories: list[dict[str, object]]) -> list[str]:
    questions = [
        "Is this the deployed source tree for the incident window, or only a nearby checkout?",
        "Which listed runtime surfaces are in scope for this incident and which should be ignored?",
        "Which log files or state directories contain the matching runtime evidence for these surfaces?",
    ]
    if any(str(row.get("name") or "") == "Recovery and supervisor control" for row in categories):
        questions.append("Were recovery actions expected to act automatically, or should they remain human-gated for this window?")
    if any(str(row.get("name") or "") == "Video stream transport" for row in categories):
        questions.append("Should stream transport failures be judged by ffmpeg/socket evidence, public YouTube evidence, or both?")
    if any(str(row.get("name") or "") == "Memory and host capacity" for row in categories):
        questions.append("Which memory thresholds are operational guardrails versus hard incident evidence?")
    return questions


def _systemd_unit_names(source_context: dict[str, object]) -> list[str]:
    values: list[str] = []
    for row in source_context.get("systemd_units") or []:
        if not isinstance(row, dict):
            continue
        values.extend(
            _string_list(
                [
                    row.get("unit_name"),
                    row.get("description"),
                ]
            )
        )
    return values


def _component_names(source_analysis: dict[str, object]) -> list[str]:
    values: list[str] = []
    for row in source_analysis.get("component_candidates") or []:
        if isinstance(row, dict):
            values.extend(_string_list([row.get("name"), row.get("suggested_role"), row.get("suggested_subsystem")]))
    return values


def _metric_names(source_context: dict[str, object], source_analysis: dict[str, object]) -> list[str]:
    values = _string_list(
        (
            source_context.get("project_summary")
            if isinstance(source_context.get("project_summary"), dict)
            else {}
        ).get("metric_name_candidates")
    )
    for row in source_analysis.get("metric_semantics_candidates") or []:
        if isinstance(row, dict):
            values.extend(_string_list([row.get("metric_name")]))
    for row in source_analysis.get("instrumentation_candidates") or []:
        if not isinstance(row, dict):
            continue
        if str(row.get("instrumentation_type") or "") == "metrics":
            values.extend(_string_list(row.get("candidate_names")))
    return _unique([value for value in values if _looks_like_runtime_signal(value)])


def _looks_like_runtime_signal(value: str) -> bool:
    text = str(value or "").strip()
    if not text:
        return False
    lowered = text.casefold()
    if lowered.startswith("test_"):
        return False
    if lowered in {"assertionerror", "attributeerror", "keyerror", "typeerror", "valueerror"}:
        return False
    if len(text) < 3:
        return False
    return True


def _examples_matching(values: list[str], patterns: tuple[str, ...], *, limit: int) -> list[str]:
    examples: list[str] = []
    lowered_patterns = tuple(pattern.casefold() for pattern in patterns)
    for value in values:
        text = str(value or "").strip()
        lowered = text.casefold()
        if text and any(pattern in lowered for pattern in lowered_patterns):
            examples.append(_shorten_token(text))
        if len(examples) >= limit:
            break
    return _unique(examples)


def _shorten_token(value: str) -> str:
    text = str(value or "").strip()
    if len(text) <= 96:
        return text
    return text[:93].rstrip() + "..."


def _markdown_list(value: object) -> str:
    items = _string_list(value)
    return "\n".join(f"- {item}" for item in items) if items else "- none inferred"


def _markdown_signal_sections(value: object) -> str:
    if not isinstance(value, list):
        return "- none inferred"
    sections: list[str] = []
    for row in value:
        if not isinstance(row, dict):
            continue
        name = str(row.get("name") or "Runtime signal").strip()
        interpretation = str(row.get("interpretation") or "").strip()
        examples = _string_list(row.get("examples"))
        sections.append(f"- {name}: {interpretation}")
        if examples:
            sections.append(f"  Examples: {', '.join(examples[:8])}")
    return "\n".join(sections) if sections else "- none inferred"


def _string_list(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item).strip()]
    text = str(value).strip()
    return [text] if text else []


def _unique(values: list[str]) -> list[str]:
    seen: set[str] = set()
    unique_values: list[str] = []
    for value in values:
        text = str(value or "").strip()
        key = text.casefold()
        if not text or key in seen:
            continue
        seen.add(key)
        unique_values.append(text)
    return unique_values


def _trim_report(text: str, *, max_lines: int) -> str:
    lines = str(text or "").strip().splitlines()
    if len(lines) <= max_lines:
        return "\n".join(lines)
    kept = lines[:max_lines]
    kept.append(f"... trimmed {len(lines) - max_lines} additional lines from generated details ...")
    return "\n".join(kept)


def _write_code_profile_review_artifacts(
    *,
    output_dir: Path,
    run_id: str,
    code_profile_id: str,
    code_profile_url: str,
    code_profile_report_url: str,
    source_root: Path,
    source_context_bundle: Path,
    source_context_report: Path,
    source_analysis_bundle: Path,
    source_analysis_report: Path,
) -> dict[str, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    summary = _code_profile_summary(source_context_bundle, source_analysis_bundle)
    source_context = _read_json_object(source_context_bundle)
    source_analysis = _read_json_object(source_analysis_bundle)
    interpretation = _code_profile_interpretation(source_context, source_analysis)
    context_report = _trim_report(_read_text_or_empty(source_context_report), max_lines=120)
    analysis_report = _trim_report(_read_text_or_empty(source_analysis_report), max_lines=90)
    markdown = _render_code_profile_markdown(
        run_id=run_id,
        code_profile_id=code_profile_id,
        code_profile_url=code_profile_url,
        code_profile_report_url=code_profile_report_url,
        source_root_name=source_root.name or "source-root",
        summary=summary,
        interpretation=interpretation,
        context_report=context_report,
        analysis_report=analysis_report,
    )
    html_text = _render_code_profile_html(
        title="Code Profile Review",
        code_profile_url=code_profile_url,
        code_profile_report_url=code_profile_report_url,
        markdown=markdown,
    )
    payload = {
        "schema_version": "code_profile_review_page.v1",
        "run_id": run_id,
        "code_profile_id": code_profile_id,
        "source_root_name": source_root.name or "source-root",
        "local_absolute_path_uploaded": False,
        "code_profile_url": code_profile_url,
        "code_profile_report_url": code_profile_report_url,
        "summary": summary,
        "interpretation": interpretation,
        "source_context_sha256": source_context.get("source_context_sha256") or "",
        "analysis_sha256": source_analysis.get("analysis_sha256") or "",
        "raw_source_policy": source_context.get("raw_source_policy") or source_analysis.get("raw_source_policy") or "",
        "raw_env_policy": source_context.get("raw_env_policy") or source_analysis.get("raw_env_policy") or "",
    }
    html_path = output_dir / "index.html"
    markdown_path = output_dir / "report.md"
    payload_path = output_dir / "payload.json"
    html_path.write_text(html_text, encoding="utf-8")
    markdown_path.write_text(markdown, encoding="utf-8")
    payload_path.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    return {"html": html_path, "markdown": markdown_path, "payload": payload_path}


def _render_code_profile_markdown(
    *,
    run_id: str,
    code_profile_id: str,
    code_profile_url: str,
    code_profile_report_url: str,
    source_root_name: str,
    summary: dict[str, object],
    interpretation: dict[str, object],
    context_report: str,
    analysis_report: str,
) -> str:
    entrypoints = [str(item) for item in summary.get("entrypoint_candidates") or []]
    entrypoint_text = "\n".join(f"- {item}" for item in entrypoints[:12]) or "- none detected"
    purpose_text = _markdown_list(interpretation.get("system_purpose"))
    measurement_text = _markdown_signal_sections(interpretation.get("runtime_measurements"))
    invariant_text = _markdown_list(interpretation.get("do_not_break"))
    question_text = _markdown_list(interpretation.get("human_review_questions"))
    key_surface_text = _markdown_list(interpretation.get("key_runtime_surfaces"))
    return f"""# Code Profile Review

This page is the human approval checkpoint before log analysis starts.

- run_id: {run_id}
- code_profile_id: {code_profile_id}
- code_profile_url: {code_profile_url}
- markdown_url: {code_profile_report_url}
- selected_source_root_name: {source_root_name}
- local_absolute_path_uploaded: false
- detected_project_type: {summary.get("detected_project_type") or "unknown"}
- source_items: {summary.get("source_item_count", 0)}
- config_items: {summary.get("config_item_count", 0)}
- component_candidates: {summary.get("component_candidate_count", 0)}
- metric_semantics_candidates: {summary.get("metric_semantics_candidate_count", 0)}
- collector_mapping_candidates: {summary.get("collector_mapping_candidate_count", 0)}

## What This Code Appears To Run

{purpose_text}

## Key Runtime Surfaces

{key_surface_text}

## What The Logs Should Measure

{measurement_text}

## What Should Not Be Broken

{invariant_text}

## Human Review Questions

{question_text}

## Approval Checklist

- The selected source root name matches the system under review.
- Entrypoint candidates include the service, timer, or worker units that were deployed in the incident window.
- Source/config counts look plausible for the selected repository.
- Component, metric, and collector candidates look relevant enough to guide log review.
- Code/config is treated as context only. Runtime claims still require cited Evidence Items from sanitized logs.

## Stop Conditions

- Stop if the selected source root is the wrong repository.
- Stop if the entrypoint candidates do not match the deployed service family.
- Stop if this code profile may not match the deployment period under review.
- Stop if source/config counts look unexpectedly low or unexpectedly broad.

## Approval Action

After reviewing this page, return to the terminal and type `APPROVE` to start log analysis. Anything else stops before log analysis.

## Entrypoint Candidates

{entrypoint_text}

## Source Context Report

Trimmed generated report. Open the local analysis directory for the full report when needed.

{context_report.strip()}

## Source Analysis Report

Trimmed generated report. Open the local analysis directory for the full report when needed.

{analysis_report.strip()}
"""


def _render_code_profile_html(
    *,
    title: str,
    code_profile_url: str,
    code_profile_report_url: str,
    markdown: str,
) -> str:
    body = _markdown_to_html(markdown)
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{_html(title)}</title>
  <style>
    :root {{ color-scheme: light; --ink:#182026; --muted:#5b6670; --line:#d7dde2; --panel:#f6f8fa; --accent:#126a72; --warn:#8a5a00; }}
    body {{ margin:0; font:16px/1.55 system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; color:var(--ink); background:#fff; }}
    header {{ border-bottom:1px solid var(--line); background:#f8fafb; }}
    main, .inner {{ max-width:1080px; margin:0 auto; padding:24px; }}
    h1 {{ margin:0 0 8px; font-size:32px; line-height:1.15; letter-spacing:0; }}
    h2 {{ margin:32px 0 10px; font-size:21px; letter-spacing:0; }}
    p {{ margin:8px 0; }}
    a {{ color:var(--accent); }}
    code {{ background:#edf2f4; padding:2px 5px; border-radius:4px; }}
    pre {{ overflow:auto; padding:16px; border:1px solid var(--line); background:#fbfcfd; border-radius:8px; }}
    .lede {{ color:var(--muted); max-width:760px; }}
    .actions {{ display:flex; gap:10px; flex-wrap:wrap; margin-top:16px; }}
    .button {{ display:inline-flex; align-items:center; min-height:36px; padding:0 12px; border:1px solid var(--line); border-radius:6px; text-decoration:none; background:#fff; color:var(--ink); font-weight:650; }}
    .button.primary {{ background:var(--accent); color:#fff; border-color:var(--accent); }}
    .notice {{ margin-top:16px; padding:12px 14px; border:1px solid #e4c46f; border-radius:8px; background:#fff8e1; color:var(--warn); }}
    .content {{ display:grid; gap:8px; }}
    .content ul {{ padding-left:22px; }}
    .content li {{ margin:4px 0; }}
    footer {{ border-top:1px solid var(--line); color:var(--muted); }}
  </style>
</head>
<body>
  <header>
    <div class="inner">
      <h1>{_html(title)}</h1>
      <p class="lede">Human approval checkpoint before log analysis. The page contains sanitized code-profile context only.</p>
      <div class="actions">
        <a class="button primary" href="{_html(code_profile_url)}">Open HTML</a>
        <a class="button" href="{_html(code_profile_report_url)}">Open Markdown</a>
      </div>
      <p class="notice">Review the checklist before approving. Type APPROVE in the terminal only when this profile matches the system and deployment period under review.</p>
    </div>
  </header>
  <main class="content">
    {body}
  </main>
  <footer>
    <div class="inner">Raw source, raw env values, and local absolute paths are not published in this page.</div>
  </footer>
</body>
</html>"""


def _markdown_to_html(markdown: str) -> str:
    blocks: list[str] = []
    in_list = False
    in_code = False
    code_lines: list[str] = []
    for raw_line in str(markdown or "").splitlines():
        line = raw_line.rstrip()
        stripped = line.strip()
        if stripped.startswith("```"):
            if in_code:
                blocks.append("<pre><code>" + _html("\n".join(code_lines)) + "</code></pre>")
                code_lines = []
                in_code = False
            else:
                if in_list:
                    blocks.append("</ul>")
                    in_list = False
                in_code = True
            continue
        if in_code:
            code_lines.append(line)
            continue
        if not stripped:
            if in_list:
                blocks.append("</ul>")
                in_list = False
            continue
        if stripped.startswith("# "):
            if in_list:
                blocks.append("</ul>")
                in_list = False
            blocks.append(f"<h1>{_html(stripped[2:].strip())}</h1>")
        elif stripped.startswith("## "):
            if in_list:
                blocks.append("</ul>")
                in_list = False
            blocks.append(f"<h2>{_html(stripped[3:].strip())}</h2>")
        elif stripped.startswith("- "):
            if not in_list:
                blocks.append("<ul>")
                in_list = True
            blocks.append(f"<li>{_html(stripped[2:].strip())}</li>")
        else:
            if in_list:
                blocks.append("</ul>")
                in_list = False
            blocks.append(f"<p>{_html(stripped)}</p>")
    if in_code:
        blocks.append("<pre><code>" + _html("\n".join(code_lines)) + "</code></pre>")
    if in_list:
        blocks.append("</ul>")
    return "\n".join(blocks)


def _json_field(path: Path, field: str) -> object:
    return _read_json_object(path).get(field) or ""


def _read_json_object(path: Path) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _read_text_or_empty(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def _html(value: object) -> str:
    return html.escape(str(value or ""), quote=True)


def _optional_source_root(value: str | list[str], *, no_prompts: bool) -> Path | None:
    text = _source_root_text(value)
    if not text and not no_prompts and sys.stdin.isatty():
        text = _interactive_source_root_text()
    if not text:
        return None
    paths = [_absolute_source_dir(item) for item in _split_source_roots(text)]
    if not paths:
        return None
    path = _common_source_root(paths) if len(paths) > 1 else _normalize_source_root(paths[0])
    if not path.exists():
        raise SystemExit(f"source code directory was not found: {path}")
    if not path.is_dir():
        raise SystemExit(f"SOURCE_ROOT must be a directory: {path}")
    return path


def _source_root_text(value: str | list[str]) -> str:
    if isinstance(value, list):
        return "\n".join(str(item) for item in value if str(item).strip()).strip()
    return str(value or "").strip()


def _split_source_roots(text: str) -> list[str]:
    normalized = str(text or "").replace("\n", os.pathsep).replace(",", os.pathsep)
    return [part.strip() for part in normalized.split(os.pathsep) if part.strip()]


def _interactive_source_root_text() -> str:
    first = _read_prompt_line("Source code directory or directories [optional]: ").strip()
    if not first:
        return ""
    lines = [first]
    lines.extend(_read_pending_source_root_lines())
    return "\n".join(lines)


def _read_prompt_line(prompt: str) -> str:
    if _PENDING_PROMPT_LINES:
        return _PENDING_PROMPT_LINES.pop(0)
    return input(prompt)


def _read_pending_source_root_lines() -> list[str]:
    if not sys.stdin.isatty():
        return []
    try:
        import select
    except ImportError:  # pragma: no cover - non-POSIX fallback.
        return []

    lines: list[str] = []
    while True:
        ready, _unused_write, _unused_error = select.select([sys.stdin], [], [], 0.2)
        if not ready:
            return lines
        raw = sys.stdin.readline()
        if raw == "":
            return lines
        text = raw.strip()
        if _line_contains_only_source_roots(text):
            lines.append(text)
            continue
        _PENDING_PROMPT_LINES.append(text)
        return lines


def _line_contains_only_source_roots(text: str) -> bool:
    parts = _split_source_roots(text)
    return bool(parts) and all(_is_absolute_existing_dir(part) for part in parts)


def _looks_like_misplaced_source_root_answer(text: str) -> bool:
    value = str(text or "").strip()
    return bool(value) and (value.startswith("/") or _line_contains_only_source_roots(value))


TIMESTAMP_SUFFIX_RE = re.compile(
    r"(?P<timestamp>\d{4}-\d{2}-\d{2}(?:[T ][0-2]\d:[0-5]\d:[0-5]\d(?:[.,]\d+)?(?:Z|[+-]\d{2}:?\d{2})?)?)$"
)


def _timestamp_suffix_after_existing_dir(text: str) -> str:
    value = str(text or "").strip()
    match = TIMESTAMP_SUFFIX_RE.search(value)
    if not match:
        return ""
    prefix = value[: match.start("timestamp")]
    path = Path(prefix).expanduser()
    if path.is_absolute() and path.exists() and path.is_dir():
        return match.group("timestamp")
    return ""


def _is_absolute_existing_dir(value: str) -> bool:
    path = Path(value).expanduser()
    return path.is_absolute() and path.exists() and path.is_dir()


def _absolute_source_dir(value: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise SystemExit(f"SOURCE_ROOT must be an absolute path: {value}")
    if not path.exists():
        raise SystemExit(f"source code directory was not found: {path}")
    if not path.is_dir():
        raise SystemExit(f"SOURCE_ROOT must be a directory: {path}")
    return path


def _common_source_root(paths: list[Path]) -> Path:
    return _normalize_source_root(Path(os.path.commonpath([str(path) for path in paths])))


def _normalize_source_root(path: Path) -> Path:
    if path.name in {"deploy", "docs", "ops", "src", "tests"} and _looks_like_project_root(path.parent):
        return path.parent
    if len(path.parts) >= 2 and path.parent.name == "deploy" and _looks_like_project_root(path.parent.parent):
        return path.parent.parent
    return path


def _looks_like_project_root(path: Path) -> bool:
    return any((path / name).exists() for name in (".git", "pyproject.toml", "src", "tests", "Makefile"))


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
