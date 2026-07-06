#!/usr/bin/env python3
from __future__ import annotations

import argparse
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
DEFAULT_PROVIDERS = "local-gemini,local-gpt-oss,local-mistral"
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
    output_prefix_uri = f"gs://{bucket}/job-runs"
    precomputed_prefix_uri = f"gs://{bucket}/precomputed_review_summaries"
    static_review_prefix_uri = f"gs://{bucket}/review-pages"

    cli = [sys.executable, "-m", "ops_evidence_synthesis.cli"]
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
        _confirm_code_profile_before_log_analysis(
            source_root=source_root,
            source_context_bundle=source_context_bundle,
            source_context_report=source_context_dir / "source_context_report.md",
            source_analysis_bundle=source_analysis_bundle,
            source_analysis_report=source_analysis_dir / "source_analysis_report.md",
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
    text = _required_prompt_value(
        value,
        label,
        example,
        env_name=env_name,
        flag_name=flag_name,
        no_prompts=no_prompts,
    )
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
    no_prompts: bool,
    skip_confirmation: bool,
) -> None:
    if no_prompts or skip_confirmation or not sys.stdin.isatty():
        return
    summary = _code_profile_summary(source_context_bundle, source_analysis_bundle)
    print(file=sys.stderr)
    print("Code profile review is ready.", file=sys.stderr)
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
    answer = _read_prompt_line("Continue with log analysis using this code profile? Type yes to continue [y/N]: ").strip()
    if answer.casefold() not in {"y", "yes"}:
        print("Stopped before log analysis. Review the code profile, then rerun with corrected source paths.", file=sys.stderr)
        raise SystemExit(0)


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
        ready, _unused_write, _unused_error = select.select([sys.stdin], [], [], 0.02)
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
    if path.name in {"ops", "src", "tests"} and _looks_like_project_root(path.parent):
        return path.parent
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
