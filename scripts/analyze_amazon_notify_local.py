from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import timedelta
from pathlib import Path

from ops_evidence_synthesis.ai.providers import build_provider_list
from ops_evidence_synthesis.ingest import ingest_log_files
from ops_evidence_synthesis.models import IncidentWindow
from ops_evidence_synthesis.storage.sqlite_store import SQLiteStore
from ops_evidence_synthesis.synthesis.pipeline import run_pipeline
from ops_evidence_synthesis.timeutils import format_timestamp, parse_timestamp


def main() -> int:
    parser = argparse.ArgumentParser(description="Ingest and analyze amazon-notify logs with safe defaults.")
    parser.add_argument("--input", required=True, nargs="+", help="JSONL/text log file(s)")
    parser.add_argument("--db", default="workspace/amazon_notify/amazon_notify_local.sqlite3")
    parser.add_argument("--service", default="amazon-notify")
    parser.add_argument("--environment", default="prod")
    parser.add_argument("--start", default="", help="Incident start. Defaults to latest log minus --incident-minutes.")
    parser.add_argument("--end", default="", help="Incident end. Defaults to latest log timestamp.")
    parser.add_argument("--incident-minutes", type=int, default=30)
    parser.add_argument("--lookback-minutes", type=int, default=20160)
    parser.add_argument("--provider", action="append", default=[], help="Default is local. Use --provider gemini for Vertex.")
    parser.add_argument("--targets", type=int, default=10)
    args = parser.parse_args()

    store = SQLiteStore(args.db)
    store.init_schema()
    ingested = ingest_log_files(args.input, store)
    start, end, total = _window_from_db(
        store,
        service=args.service,
        environment=args.environment,
        start=args.start,
        end=args.end,
        incident_minutes=args.incident_minutes,
    )
    result = run_pipeline(
        store,
        IncidentWindow(
            service=args.service,
            environment=args.environment,
            incident_start=start,
            incident_end=end,
            lookback_minutes=args.lookback_minutes,
        ),
        providers=build_provider_list(args.provider),
    )
    print(f"db={args.db}")
    print(f"ingested_logs={ingested}")
    print(f"stored_logs_for_service={total}")
    print(f"incident_start={start}")
    print(f"incident_end={end}")
    for key, value in asdict(result).items():
        print(f"{key}={value}")
    target_set = store.list_review_targets(limit=args.targets, pending_only=False)
    print("review_targets:")
    for target in target_set.get("targets") or []:
        requests = [
            str(request.get("request_id") or "")
            for request in (target.get("drawer") or {}).get("next_evidence_requests") or []
        ]
        print(
            "\t".join(
                [
                    str(target.get("review_target_id") or ""),
                    str(target.get("subsystem") or ""),
                    f"{float(target.get('review_priority_score') or 0):.3f}",
                    str(target.get("title") or ""),
                    ",".join(requests),
                ]
            )
        )
    return 0


def _window_from_db(
    store: SQLiteStore,
    *,
    service: str,
    environment: str,
    start: str,
    end: str,
    incident_minutes: int,
) -> tuple[str, str, int]:
    with store.connect() as conn:
        row = conn.execute(
            """
            SELECT MIN(timestamp) AS min_ts, MAX(timestamp) AS max_ts, COUNT(*) AS count
            FROM logs_sanitized
            WHERE service = ? AND environment = ?
            """,
            (service, environment),
        ).fetchone()
    if row is None or not row["max_ts"]:
        raise SystemExit(f"no logs found for service={service} environment={environment}")
    resolved_end = format_timestamp(end or str(row["max_ts"]))
    if start:
        resolved_start = format_timestamp(start)
    else:
        resolved_start = format_timestamp(parse_timestamp(resolved_end) - timedelta(minutes=max(1, incident_minutes)))
    return resolved_start, resolved_end, int(row["count"] or 0)


if __name__ == "__main__":
    raise SystemExit(main())
