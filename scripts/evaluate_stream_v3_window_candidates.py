#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import timedelta
from pathlib import Path
from typing import Any

from ops_evidence_synthesis.ai.prompts import compact_bundle_for_model
from ops_evidence_synthesis.local_first import build_bundle_from_sanitized
from ops_evidence_synthesis.timeutils import format_timestamp, parse_timestamp, utc_now
from ops_evidence_synthesis.window_policy import (
    DEFAULT_MIN_ANALYSIS_WINDOW_HOURS,
    validate_minimum_analysis_window,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Evaluate sanitized-log windows and select the longest valid candidate."
    )
    parser.add_argument("--sanitized-events", required=True, help="Local sanitized_events.jsonl input.")
    parser.add_argument("--case-label", default="stream_v3", help="Human-readable case label for reports.")
    parser.add_argument("--service", default="stream_v3_runtime")
    parser.add_argument("--environment", default="dell_runtime")
    parser.add_argument("--profile", default="stream_v3")
    parser.add_argument("--windows-days", default="2,5,7", help="Comma-separated day counts to evaluate.")
    parser.add_argument("--end", default="", help="Window end timestamp. Defaults to max event timestamp.")
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--min-window-hours", type=int, default=DEFAULT_MIN_ANALYSIS_WINDOW_HOURS)
    args = parser.parse_args()

    input_path = Path(args.sanitized_events)
    events = _load_events(input_path)
    if not events:
        raise SystemExit(f"no events found: {input_path}")
    scoped_events = _events_for_scope(events, service=args.service, environment=args.environment)
    end_basis = scoped_events or events
    end = format_timestamp(args.end or max(_event_timestamp(event) for event in end_basis))
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    results: list[dict[str, Any]] = []
    for days in _day_values(args.windows_days):
        start = format_timestamp(parse_timestamp(end) - timedelta(days=days))
        window = validate_minimum_analysis_window(
            start,
            end,
            min_hours=args.min_window_hours,
            context=f"{args.case_label} {days}d candidate",
        )
        bundle_path = out_dir / f"evidence_bundle_{days}d.json"
        bundle = build_bundle_from_sanitized(
            input_path,
            service=args.service,
            environment=args.environment,
            start=start,
            end=end,
            profile_name=args.profile,
            out_path=bundle_path,
            collection_mode=f"{days}d_candidate",
        )
        compact = compact_bundle_for_model(bundle)
        corpus = compact.get("evidence_corpus_summary") if isinstance(compact.get("evidence_corpus_summary"), dict) else {}
        local_first = bundle.get("local_first_summary") if isinstance(bundle.get("local_first_summary"), dict) else {}
        selected = _events_for_window(events, start, end)
        row_summary = _row_summary(selected)
        results.append(
            {
                "window_days": days,
                "window_start": start,
                "window_end": end,
                "analysis_window_hours": window.duration_hours,
                "evidence_sha256": bundle["evidence_sha256"],
                "bundle_path": str(bundle_path),
                "sanitized_row_count": int(local_first.get("sanitized_event_count") or len(selected)),
                "evidence_item_count": len(bundle.get("evidence_items") or []),
                "signal_count": len(bundle.get("signals") or []),
                "model_projection_evidence_items": int(corpus.get("model_evidence_item_count") or 0),
                "model_projection_occurrence_count": int(corpus.get("model_occurrence_count") or 0),
                "model_projection_occurrence_coverage_ratio": float(corpus.get("occurrence_coverage_ratio") or 0.0),
                "top_services": row_summary["services"],
                "top_environments": row_summary["environments"],
                "top_event_types": row_summary["event_types"],
                "top_severities": row_summary["severities"],
            }
        )

    selected = max(results, key=lambda row: (int(row["window_days"]), int(row["sanitized_row_count"])))
    report = {
        "schema_version": "stream_v3_window_candidate_report.v1",
        "generated_at": utc_now(),
        "sanitized_events_input": str(input_path),
        "scope": {
            "service": args.service,
            "environment": args.environment,
            "end_selected_from_matching_scope": bool(scoped_events) and not args.end,
            "matching_scope_row_count": len(scoped_events),
        },
        "source_policy": {
            "raw_logs_committed": False,
            "raw_logs_uploaded_to_model": False,
            "row_level_sanitized_events_committed": False,
            "candidate_report_contains_aggregate_counts_only": True,
        },
        "min_analysis_window_hours": args.min_window_hours,
        "case_label": args.case_label,
        "candidate_count": len(results),
        "selected_window_days": selected["window_days"],
        "selected_evidence_sha256": selected["evidence_sha256"],
        "selected_bundle_path": selected["bundle_path"],
        "candidates": results,
    }
    (out_dir / "window_candidate_report.json").write_text(
        json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    (out_dir / "window_candidate_report.md").write_text(_markdown(report), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


def _load_events(path: Path) -> list[dict[str, Any]]:
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        payload = json.loads(line)
        if isinstance(payload, dict):
            rows.append(payload)
    return rows


def _event_timestamp(event: dict[str, Any]) -> str:
    value = event.get("timestamp") or event.get("observed_timestamp") or event.get("time")
    if not value:
        raise ValueError("sanitized event is missing timestamp")
    return format_timestamp(str(value))


def _events_for_window(events: list[dict[str, Any]], start: str, end: str) -> list[dict[str, Any]]:
    start_dt = parse_timestamp(start)
    end_dt = parse_timestamp(end)
    selected = []
    for event in events:
        timestamp = parse_timestamp(_event_timestamp(event))
        if start_dt <= timestamp <= end_dt:
            selected.append(event)
    return selected


def _events_for_scope(events: list[dict[str, Any]], *, service: str, environment: str) -> list[dict[str, Any]]:
    selected = []
    for event in events:
        if service and str(event.get("service") or "") != service:
            continue
        if environment and str(event.get("environment") or "") != environment:
            continue
        selected.append(event)
    return selected


def _row_summary(events: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    return {
        "services": _top_counts(event.get("service") for event in events),
        "environments": _top_counts(event.get("environment") for event in events),
        "event_types": _top_counts(event.get("event_type") for event in events),
        "severities": _top_counts(event.get("severity_text") for event in events),
    }


def _top_counts(values: Any, *, limit: int = 8) -> list[dict[str, Any]]:
    counts = Counter(str(value or "unknown") for value in values)
    return [{"value": value, "count": count} for value, count in counts.most_common(limit)]


def _day_values(raw: str) -> list[int]:
    values = []
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        days = int(item)
        if days <= 0:
            raise ValueError("window days must be positive")
        values.append(days)
    if not values:
        raise ValueError("--windows-days must contain at least one positive day count")
    return values


def _markdown(report: dict[str, Any]) -> str:
    case_label = str(report.get("case_label") or "sanitized-log")
    lines = [
        f"# {case_label} window candidate report",
        "",
        f"- Generated at: `{report['generated_at']}`",
        f"- Minimum analysis window: `{report['min_analysis_window_hours']}h`",
        f"- Selected window: `{report['selected_window_days']}d`",
        f"- Selected evidence SHA256: `{report['selected_evidence_sha256']}`",
        "",
        "| Window | Rows | Evidence items | Prompt items | Prompt occurrences | Coverage | Evidence SHA256 |",
        "| --- | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    for row in report["candidates"]:
        lines.append(
            "| "
            f"{row['window_days']}d | "
            f"{row['sanitized_row_count']:,} | "
            f"{row['evidence_item_count']:,} | "
            f"{row['model_projection_evidence_items']:,} | "
            f"{row['model_projection_occurrence_count']:,} | "
            f"{row['model_projection_occurrence_coverage_ratio']:.1%} | "
            f"`{row['evidence_sha256']}` |"
        )
    lines.extend(
        [
            "",
            f"The longest valid window is selected for the public {case_label} review. "
            "This report contains aggregate counts only; row-level sanitized events stay local.",
            "",
        ]
    )
    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())
