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
        description="Generate a precomputed read-only review payload from pipeline output."
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
    parser.add_argument("--target-limit", type=int, default=5)
    parser.add_argument("--source-note", default="generated from public sample fixture with deterministic local providers")
    parser.add_argument("--provider-mode", default="deterministic_local")
    parser.add_argument("--expected-evidence-sha", default="")
    parser.add_argument("--expected-log-count", type=int, default=0)
    parser.add_argument("--require-convergence", action="store_true")
    parser.add_argument("--expected-convergence-score", type=float, default=0.0)
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
        target_limit=args.target_limit,
        source_note=args.source_note,
        provider_mode=args.provider_mode,
    )
    _validate_payload(
        payload,
        expected_evidence_sha=args.expected_evidence_sha,
        expected_log_count=args.expected_log_count,
        require_convergence=args.require_convergence,
        expected_convergence_score=args.expected_convergence_score,
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


def _validate_payload(
    payload: dict,
    *,
    expected_evidence_sha: str,
    expected_log_count: int,
    require_convergence: bool,
    expected_convergence_score: float,
) -> None:
    if expected_evidence_sha and str(payload.get("evidence_sha256") or "") != expected_evidence_sha:
        raise SystemExit(f"expected evidence_sha256={expected_evidence_sha}, got {payload.get('evidence_sha256')}")
    summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
    if expected_log_count and int(summary.get("log_count") or 0) != expected_log_count:
        raise SystemExit(f"expected log_count={expected_log_count}, got {summary.get('log_count')}")
    targets = [target for target in payload.get("targets") or [] if isinstance(target, dict)]
    converged = [
        target
        for target in targets
        if (target.get("agreement") or {}).get("verdict") == "convergence"
        and float((target.get("agreement") or {}).get("convergence_score") or 0.0) > 0.0
    ]
    if require_convergence and not converged:
        raise SystemExit("expected at least one converged review target")
    if expected_convergence_score:
        tolerance = 0.0001
        if not any(
            abs(float((target.get("agreement") or {}).get("convergence_score") or 0.0) - expected_convergence_score)
            <= tolerance
            for target in converged
        ):
            scores = [
                float((target.get("agreement") or {}).get("convergence_score") or 0.0)
                for target in converged
            ]
            raise SystemExit(f"expected convergence_score={expected_convergence_score}, got {scores}")


if __name__ == "__main__":
    raise SystemExit(main())
