from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from typing import Iterable

from ops_evidence_synthesis.ai.vertex import VertexGeminiProvider
from ops_evidence_synthesis.bundle import EvidenceBundleBuilder
from ops_evidence_synthesis.gcp.bigquery import BigQueryOps
from ops_evidence_synthesis.ingest import iter_log_file
from ops_evidence_synthesis.models import IncidentWindow, SanitizedLog
from ops_evidence_synthesis.sanitizer import sanitize_log
from ops_evidence_synthesis.synthesis.pipeline import run_synthesis_for_bundle
from ops_evidence_synthesis.timeutils import parse_timestamp


DEFAULT_START = "2026-06-02T00:00:00Z"
DEFAULT_END = "2026-06-16T00:00:00Z"
DEFAULT_SOURCE_ROOT = "workspace/private/stream_v3/.state"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Load 14d stream_v3 logs into BigQuery and run Gemini synthesis.")
    parser.add_argument("--project", default="ops-evidence-synthesis")
    parser.add_argument("--location", default="asia-northeast1")
    parser.add_argument("--source-root", default=DEFAULT_SOURCE_ROOT)
    parser.add_argument("--start", default=DEFAULT_START)
    parser.add_argument("--end", default=DEFAULT_END)
    parser.add_argument("--environment", default="stream_v3")
    parser.add_argument("--service", default="stream_v3-aggregate")
    parser.add_argument("--lookback-minutes", type=int, default=0)
    parser.add_argument("--model", default="gemini-3.1-flash-lite")
    parser.add_argument("--out", default="workspace/stream_v3_14d_sanitized.bigquery.jsonl")
    parser.add_argument("--max-rows", type=int, default=0)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    source_root = Path(args.source_root)
    output_path = Path(args.out)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    bq = BigQueryOps(args.project, location=args.location)
    bq.apply_schema()

    files = list(discover_log_files(source_root))
    stats = write_sanitized_jsonl(
        files,
        output_path,
        start=args.start,
        end=args.end,
        max_rows=args.max_rows,
    )

    bq.delete_logs(environment=args.environment, start=args.start, end=args.end)
    loaded_rows = bq.load_sanitized_logs_jsonl(output_path)

    incident = IncidentWindow(
        service=args.service,
        environment=args.environment,
        incident_start=args.start,
        incident_end=args.end,
        lookback_minutes=args.lookback_minutes,
    )
    bundle = EvidenceBundleBuilder(bq).build(incident)
    bq.delete_synthesis_for_evidence(bundle["evidence_sha256"])

    provider = VertexGeminiProvider(
        model_name=args.model,
        project_id=args.project,
        location="global",
        max_output_tokens=8192,
    )
    result = run_synthesis_for_bundle(bq, bundle, providers=[provider])
    proposals = bq.list_proposals(
        limit=10,
        evidence_sha256=result.evidence_sha256,
        pending_only=False,
    )

    summary = {
        "source_root": str(source_root),
        "source_file_count": len(files),
        "sanitized_rows_written": stats["rows"],
        "bigquery_rows_loaded": loaded_rows,
        "window_start": args.start,
        "window_end": args.end,
        "environment": args.environment,
        "service": args.service,
        "top_services": stats["services"].most_common(10),
        "severity_counts": stats["severities"],
        "error_type_counts": stats["error_types"].most_common(10),
        "evidence_sha256": result.evidence_sha256,
        "model_run_count": result.model_run_count,
        "parsed_result_count": result.parsed_result_count,
        "claim_count": result.claim_count,
        "proposition_count": result.proposition_count,
        "score_count": result.score_count,
        "cluster_count": result.cluster_count,
        "review_queue_count": result.review_queue_count,
        "proposals": proposals,
    }
    print(json.dumps(_jsonable(summary), ensure_ascii=False, indent=2))
    return 0


def discover_log_files(root: Path) -> Iterable[Path]:
    suffixes = {".jsonl", ".log", ".txt"}
    skip_path_parts = {
        "chromium_profile",
        "local-run",
        "cache",
    }
    skip_name_parts = {
        "_ls",
        "_list",
        "_files",
        "_dirs",
        "capture_file",
        "ffmpeg_encoders",
        "pod_name",
        "timestamp",
    }
    for path in sorted(root.rglob("*")):
        if any(part in skip_path_parts for part in path.parts):
            continue
        if not path.is_file() or path.suffix not in suffixes or path.stat().st_size == 0:
            continue
        name = path.name
        if path.suffix == ".txt" and any(part in name for part in skip_name_parts):
            continue
        yield path


def write_sanitized_jsonl(
    paths: Iterable[Path],
    output_path: Path,
    *,
    start: str,
    end: str,
    max_rows: int = 0,
) -> dict[str, object]:
    start_dt = parse_timestamp(start)
    end_dt = parse_timestamp(end)
    services: Counter[str] = Counter()
    severities: Counter[str] = Counter()
    error_types: Counter[str] = Counter()
    rows = 0

    with output_path.open("w", encoding="utf-8") as output:
        for path in paths:
            try:
                raw_iter = iter_log_file(path)
                for raw in raw_iter:
                    timestamp = parse_timestamp(raw.timestamp)
                    if timestamp < start_dt or timestamp >= end_dt:
                        continue
                    clean = sanitize_log(raw)
                    output.write(json.dumps(sanitized_log_row(clean), ensure_ascii=False, sort_keys=True) + "\n")
                    rows += 1
                    services[clean.service] += 1
                    severities[clean.severity] += 1
                    error_types[clean.error_type] += 1
                    if max_rows and rows >= max_rows:
                        return {
                            "rows": rows,
                            "services": services,
                            "severities": dict(severities),
                            "error_types": error_types,
                        }
            except ValueError:
                continue
    return {
        "rows": rows,
        "services": services,
        "severities": dict(severities),
        "error_types": error_types,
    }


def sanitized_log_row(log: SanitizedLog) -> dict[str, object]:
    return {
        "timestamp": log.timestamp,
        "service": log.service,
        "environment": log.environment,
        "severity": log.severity,
        "trace_id": log.trace_id,
        "span_id": log.span_id,
        "deploy_id": log.deploy_id,
        "version": log.version,
        "message_sanitized": log.message_sanitized,
        "message_template": log.message_template,
        "error_type": log.error_type,
        "stack_hash": log.stack_hash,
        "resource_type": log.resource_type,
        "labels_json": log.labels_json,
        "raw_log_sha256": log.raw_log_sha256,
        "sanitizer_version": log.sanitizer_version,
    }


def _jsonable(value: object) -> object:
    if isinstance(value, Counter):
        return dict(value)
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    if isinstance(value, tuple):
        return [_jsonable(item) for item in value]
    return value


if __name__ == "__main__":
    raise SystemExit(main())
