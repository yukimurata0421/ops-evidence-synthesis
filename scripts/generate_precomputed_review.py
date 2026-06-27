#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path

from ops_evidence_synthesis.ai.provider_registry import build_multi_ai_providers
from ops_evidence_synthesis.ingest import ingest_jsonl
from ops_evidence_synthesis.models import IncidentWindow
from ops_evidence_synthesis.precomputed_review import (
    PUBLIC_DEMO_PROVIDERS,
    build_precomputed_review_summary,
    stable_precomputed_review_json,
    write_precomputed_review_summary,
)
from ops_evidence_synthesis.storage.sqlite_store import SQLiteStore
from ops_evidence_synthesis.synthesis.pipeline import run_pipeline


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Generate a precomputed read-only review payload from deterministic local pipeline output."
    )
    parser.add_argument("--input", default="data/sample_logs.jsonl", help="Public-safe JSONL log fixture.")
    parser.add_argument("--db", default="workspace/public_demo/public_demo.sqlite3")
    parser.add_argument("--service", default="payment-api")
    parser.add_argument("--environment", default="prod")
    parser.add_argument("--start", default="2026-06-12T10:00:00Z")
    parser.add_argument("--end", default="2026-06-12T10:20:00Z")
    parser.add_argument("--lookback-minutes", type=int, default=45)
    parser.add_argument("--provider", action="append", default=[], help="Provider names. Defaults to the public deterministic set.")
    parser.add_argument("--updated-at", default="2026-06-12T10:20:00Z")
    parser.add_argument("--output-dir", default="data/precomputed_review_summaries")
    parser.add_argument("--check", action="store_true", help="Compare generated JSON with the committed output file.")
    args = parser.parse_args()

    db_path = Path(args.db)
    if db_path.exists():
        db_path.unlink()
    db_path.parent.mkdir(parents=True, exist_ok=True)

    store = SQLiteStore(db_path)
    store.init_schema()
    ingest_jsonl(args.input, store)
    providers = build_multi_ai_providers(args.provider or PUBLIC_DEMO_PROVIDERS, mode="local")
    result = run_pipeline(
        store,
        IncidentWindow(
            service=args.service,
            environment=args.environment,
            incident_start=args.start,
            incident_end=args.end,
            lookback_minutes=args.lookback_minutes,
        ),
        providers=providers,
    )
    payload = build_precomputed_review_summary(
        store,
        result.evidence_sha256,
        updated_at=args.updated_at,
        source_note="generated from public sample fixture with deterministic local providers",
    )
    output_path = Path(args.output_dir) / f"{result.evidence_sha256}.json"
    generated = stable_precomputed_review_json(payload)
    if args.check:
        if not output_path.exists():
            raise SystemExit(f"expected fixture is missing: {output_path}")
        expected = output_path.read_text(encoding="utf-8")
        if generated != expected:
            raise SystemExit(f"precomputed review fixture drifted: {output_path}")
        print(f"precomputed_review_fixture=ok path={output_path}")
        return 0
    path = write_precomputed_review_summary(payload, args.output_dir)
    print(f"evidence_sha256={result.evidence_sha256}")
    print(f"output={path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
