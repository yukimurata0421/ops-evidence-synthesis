from __future__ import annotations

import argparse
import fnmatch
import json
import os
from dataclasses import asdict
from pathlib import Path

from ops_evidence_synthesis.agents.adk_investigator import (
    adk_dependency_status,
    build_adk_tool_contract_trace,
)
from ops_evidence_synthesis.ai.providers import build_provider_list
from ops_evidence_synthesis.bundle import EvidenceBundleBuilder
from ops_evidence_synthesis.collectors.remote import (
    RemoteCollectorConfig,
    collect_remote_evidence,
    collector_targets_from_more_data,
    write_jsonl_events,
)
from ops_evidence_synthesis.ingest import ingest_log_files, sanitize_logs
from ops_evidence_synthesis.local_first import (
    build_bundle_from_sanitized,
    format_verification_result,
    inspect_input,
    sanitize_input,
    verify_sanitized_output,
)
from ops_evidence_synthesis.models import IncidentWindow, RawLog
from ops_evidence_synthesis.evidence_request_planner import plan_evidence_requests
from ops_evidence_synthesis.profile_discovery import (
    approve_profile_draft,
    discover_profile,
    draft_focused_profile,
    draft_profile,
)
from ops_evidence_synthesis.source_context import analyze_source_context, sanitize_source
from ops_evidence_synthesis.storage.sqlite_store import DEFAULT_DB_PATH, SQLiteStore
from ops_evidence_synthesis.synthesis.multi_ai import run_multi_ai
from ops_evidence_synthesis.synthesis.review_arbitration import resolve_canonical_review_graph_snapshot
from ops_evidence_synthesis.synthesis.pipeline import run_demo, run_pipeline


DEFAULT_RUN_CASE_EXCLUDES = (
    ".git",
    ".git/**",
    ".hg",
    ".hg/**",
    ".mypy_cache",
    ".mypy_cache/**",
    ".pytest_cache",
    ".pytest_cache/**",
    ".ruff_cache",
    ".ruff_cache/**",
    ".venv",
    ".venv/**",
    "__pycache__",
    "__pycache__/**",
    "node_modules",
    "node_modules/**",
    "*.mp3",
    "*.wav",
    "*.flac",
    "*.m4a",
    "*.aac",
    "*.ogg",
    "*.mp4",
    "*.mov",
    "*.avi",
    "*.mkv",
    "*.png",
    "*.jpg",
    "*.jpeg",
    "*.gif",
    "*.webp",
    "*.sqlite",
    "*.sqlite3",
    "*.db",
)
DIRECTORY_LOG_FILE_SUFFIXES = {".jsonl", ".ndjson", ".log", ".txt", ".json", ".out"}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ops-evidence",
        description=(
            "Ops Evidence Synthesis. Recommended for sensitive logs: "
            "inspect -> sanitize -> verify-sanitized -> build-bundle -> cloud synthesis."
        ),
        epilog=(
            "Raw logs are not uploaded in the local-first path. "
            "Send only the sanitized evidence_bundle.json to GCP, BigQuery, Gemini, or review tooling. "
            "analyze-jsonl remains a compatibility/dev convenience path."
        ),
    )
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH), help="SQLite DB path for local execution")
    subcommands = parser.add_subparsers(dest="command", required=True)

    subcommands.add_parser("init-db", help="Create local synthesis DB schema")

    inspect = subcommands.add_parser("inspect", help="Inspect raw logs locally without uploading or rewriting them")
    inspect.add_argument("input_path", help="Raw log file or directory")

    sanitize = subcommands.add_parser("sanitize", help="Sanitize arbitrary logs into normalized local JSONL")
    sanitize.add_argument("input_path", help="Raw log file or directory")
    sanitize.add_argument("--out", required=True, help="Output directory")

    verify = subcommands.add_parser("verify-sanitized", help="Verify local-first outputs contain no raw secrets or PII")
    verify.add_argument("output_dir", help="Directory containing sanitized_events.jsonl and related outputs")

    source = subcommands.add_parser(
        "sanitize-source",
        help="Build a Sanitized Source Context Bundle locally without uploading raw source or raw env values",
    )
    source.add_argument("--project-root", required=True, help="Project root to inspect locally")
    source.add_argument("--service", required=True)
    source.add_argument("--environment", required=True)
    source.add_argument("--out", required=True, help="Output directory")

    source_analysis = subcommands.add_parser(
        "analyze-source",
        help="Build a rule-based Source Analysis Bundle from a Sanitized Source Context Bundle",
    )
    source_analysis.add_argument("--source-context", required=True, help="source_context_bundle.json")
    source_analysis.add_argument("--provider", required=True, choices=["local"], help="Analysis provider. Only local is implemented.")
    source_analysis.add_argument("--out", required=True, help="Output directory")

    local_bundle = subcommands.add_parser("build-bundle", help="Build a local-first evidence bundle from sanitized_events.jsonl")
    local_bundle.add_argument("sanitized_events_jsonl", help="Path to sanitized_events.jsonl")
    local_bundle.add_argument("--service", required=True)
    local_bundle.add_argument("--environment", required=True)
    local_bundle.add_argument("--start", required=True, help="Incident window start, ISO-8601")
    local_bundle.add_argument("--end", required=True, help="Incident window end, ISO-8601")
    local_bundle.add_argument("--profile", required=True, help="Profile id such as generic, or a specialized profile")
    local_bundle.add_argument("--parent-evidence-sha256", default="", help="Optional parent Evidence Bundle SHA256 for child bundles")
    local_bundle.add_argument("--evidence-request-plan-id", default="", help="Optional Evidence Request Plan id for child bundles")
    local_bundle.add_argument("--collection-mode", default="", help="Optional collection mode, e.g. manual_read_only_collection")
    local_bundle.add_argument("--out", required=True, help="Output evidence bundle JSON path")

    discover = subcommands.add_parser(
        "discover-profile",
        help="Build a sanitized Profile Discovery Bundle. Recommended path: sanitize-source -> analyze-source -> discover-profile",
    )
    discover.add_argument("--project-root", default="", help="Optional project root to inspect locally")
    discover.add_argument("--source-context", default="", help="source_context_bundle.json from sanitize-source")
    discover.add_argument("--source-analysis", default="", help="source_analysis_bundle.json from analyze-source")
    discover.add_argument("--evidence-bundle", default="", help="Sanitized evidence_bundle.json to seed log-driven retrieval")
    discover.add_argument("--service", required=True)
    discover.add_argument("--environment", required=True)
    discover.add_argument("--out", required=True, help="Output directory")

    draft = subcommands.add_parser(
        "draft-profile",
        help="Create a profile draft from a sanitized Profile Discovery Bundle",
    )
    draft.add_argument("--discovery-bundle", required=True, help="profile_discovery_bundle.json")
    draft.add_argument(
        "--provider",
        required=True,
        choices=["local", "gemini", "vertex-gemini", "gemini-enterprise-agent-platform"],
        help="Draft provider. Use gemini for sanitized source-aware AI profile drafting.",
    )
    draft.add_argument(
        "--model",
        default="",
        help="Optional Gemini model override for profile drafting, for example gemini-3.1-pro-preview.",
    )
    draft.add_argument("--out", required=True, help="Output profile_draft.json path")

    focused = subcommands.add_parser(
        "draft-focused-profile",
        help="Create a focused operational profile from sanitized discovery, code analysis, and evidence",
    )
    focused.add_argument("--discovery-bundle", required=True, help="profile_discovery_bundle.json")
    focused.add_argument(
        "--provider",
        required=True,
        choices=["local", "gemini", "vertex-gemini", "gemini-enterprise-agent-platform"],
        help="Focused profile provider. Use gemini for sanitized source-aware operational profiling.",
    )
    focused.add_argument(
        "--model",
        default="",
        help="Optional Gemini model override, for example gemini-3.1-pro-preview.",
    )
    focused.add_argument("--evidence-bundle", default="", help="Optional sanitized evidence_bundle.json")
    focused.add_argument("--source-context", default="", help="Optional source_context_bundle.json from sanitize-source")
    focused.add_argument("--source-analysis", default="", help="Optional source_analysis_bundle.json from analyze-source")
    focused.add_argument("--out", required=True, help="Output focused_operational_profile.json path")

    approve = subcommands.add_parser(
        "approve-profile",
        help="Approve a profile draft and write an explicit profile JSON document",
    )
    approve.add_argument("--profile-draft", required=True, help="profile_draft.json")
    approve.add_argument("--profile-id", required=True, help="Explicit profile id to write")
    approve.add_argument("--approved-by", required=True, help="Reviewer/operator name")
    approve.add_argument("--note", default="", help="Approval note")
    approve.add_argument("--out", required=True, help="Output profile .yaml/.json path")

    adk_trace = subcommands.add_parser(
        "adk-trace",
        help="Build an ADK tool-call trace from a precomputed review payload",
    )
    adk_trace.add_argument("--precomputed-payload", required=True, help="precomputed_review_summary.v1 JSON")
    adk_trace.add_argument("--out", default="", help="Optional output path for adk_trace_export.v1 JSON")
    adk_trace.add_argument("--check-runtime", action="store_true", help="Report optional ADK import availability")

    multi_ai = subcommands.add_parser(
        "run-multi-ai",
        help="Run first-class multi-AI synthesis from a sanitized Evidence Bundle and approved profile",
    )
    multi_ai.add_argument("--bundle", required=True, help="sanitized evidence_bundle.json")
    multi_ai.add_argument("--profile", required=True, help="approved explicit profile JSON/YAML")
    multi_ai.add_argument(
        "--providers",
        default=os.environ.get("OES_MULTI_AI_DEFAULT_PROVIDERS", "mistral"),
        help=(
            "Comma-separated providers. Default: mistral. "
            "Set OES_MULTI_AI_DEFAULT_PROVIDERS to override the CLI default."
        ),
    )
    multi_ai.add_argument(
        "--mode",
        default="real_or_skip",
        choices=["real_or_skip", "local", "deterministic", "fake"],
        help="Provider execution mode. Real providers require OES_ENABLE_REAL_AI=1; otherwise they are skipped.",
    )
    multi_ai.add_argument("--out", required=True, help="Output directory")
    multi_ai.add_argument("--source-context", default="", help="Optional source_context_bundle.json context input")
    multi_ai.add_argument("--source-analysis", default="", help="Optional source_analysis_bundle.json context input")
    multi_ai.add_argument("--json", action="store_true", help="Print full machine-readable run result")

    arbitrate = subcommands.add_parser(
        "arbitrate-review",
        help="Build canonical_review_graph.v1 from an Evidence Bundle and optional multi-AI synthesis",
    )
    arbitrate.add_argument("--bundle", required=True, help="sanitized evidence_bundle.json")
    arbitrate.add_argument("--profile", default="", help="Optional approved explicit profile JSON/YAML")
    arbitrate.add_argument("--multi-ai-synthesis", default="", help="Optional multi_ai_synthesis.json")
    arbitrate.add_argument("--source-context", default="", help="Optional source_context_bundle.json context input")
    arbitrate.add_argument("--source-analysis", default="", help="Optional source_analysis_bundle.json context input")
    arbitrate.add_argument("--out", required=True, help="Output directory")
    arbitrate.add_argument("--persist", action="store_true", help="Persist snapshot in the configured store")
    arbitrate.add_argument("--persist-if-stale", action="store_true", help="Persist a new snapshot when the latest snapshot is stale")
    arbitrate.add_argument("--json", action="store_true", help="Print full canonical graph")

    plan_requests = subcommands.add_parser(
        "plan-evidence-requests",
        help="Generate a manual read-only evidence collection plan without executing commands",
    )
    plan_requests.add_argument("--bundle", required=True, help="Sanitized evidence_bundle.json")
    plan_requests.add_argument("--profile", required=True, help="Approved explicit profile JSON/YAML")
    plan_requests.add_argument("--answers", default="", help="Optional planner_answers.json")
    plan_requests.add_argument("--source-analysis", default="", help="Optional source_analysis_bundle.json for domain-aware request mapping")
    plan_requests.add_argument("--canonical-review-graph", default="", help="Optional canonical_review_graph.json from Review Target Arbitration")
    plan_requests.add_argument("--out", required=True, help="Output directory")

    ingest = subcommands.add_parser("ingest-logs", help="Sanitize and ingest JSONL or text logs")
    ingest.add_argument("--input", required=True, nargs="+", help="Path(s) to raw JSONL or text logs")

    analyze = subcommands.add_parser(
        "analyze-jsonl",
        help="Sanitize JSONL/text logs, build an evidence bundle, and run synthesis",
    )
    analyze.add_argument("--input", required=True, nargs="+", help="Path(s) to raw JSONL or text logs")
    _add_incident_args(analyze)
    _add_provider_args(analyze)

    run_case = subcommands.add_parser(
        "run-case",
        help="Product flow: select local inputs, ingest sanitized logs into SQL, run AI providers, and print review URL",
    )
    run_case.add_argument("--input", required=True, nargs="+", help="File(s) or directory/directories containing logs")
    run_case.add_argument(
        "--exclude",
        action="append",
        default=[],
        help="Glob to exclude from directory input. Repeatable. Common media files such as *.mp3 are excluded by default.",
    )
    run_case.add_argument(
        "--review-base-url",
        default=os.environ.get("OES_REVIEW_BASE_URL", "http://127.0.0.1:8080"),
        help="Base URL for the review UI link printed after analysis",
    )
    run_case.add_argument("--approved-profile", default="", help="Optional approved explicit profile JSON/YAML")
    run_case.add_argument("--source-context", default="", help="Optional source_context_bundle.json from sanitize-source")
    run_case.add_argument("--source-analysis", default="", help="Optional source_analysis_bundle.json from analyze-source")
    run_case.add_argument("--json", action="store_true", help="Print machine-readable JSON summary")
    _add_incident_args(run_case)
    _add_provider_args(run_case)

    bundle = subcommands.add_parser("create-bundle", help="Build an evidence bundle")
    _add_incident_args(bundle)

    incident = subcommands.add_parser("run-incident", help="Run bundle, model, validation, routing, and scoring")
    _add_incident_args(incident)
    _add_provider_args(incident)

    demo = subcommands.add_parser("run-demo", help="Run the bundled sample incident end to end")
    demo.add_argument("--sample", default="data/sample_logs.jsonl", help="Sample JSONL path")

    serve = subcommands.add_parser("serve", help="Run the local API/UI without invoking uvicorn directly")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8080)
    serve.add_argument("--reload", action="store_true")

    reviews = subcommands.add_parser("reviews", help="List pending review queue")
    reviews.add_argument("--limit", type=int, default=20)

    proposals = subcommands.add_parser("proposals", help="Emit AI proposal output")
    proposals.add_argument("--limit", type=int, default=20)
    proposals.add_argument("--evidence-sha256")
    proposals.add_argument("--all", action="store_true", help="Include reviewed proposals")
    proposals.add_argument("--json", action="store_true", help="Print full JSON proposal payload")

    review = subcommands.add_parser("review", help="Record a human review decision")
    review.add_argument("proposition_id")
    review.add_argument("decision", choices=["accepted", "rejected", "needs_more_data"])
    review.add_argument("--reviewer", default="local-user")
    review.add_argument("--note", default="")

    collect = subcommands.add_parser("collect-more", help="Generate More data query JSON for a review target")
    collect.add_argument("review_target_id", nargs="?", help="Review target id. If omitted, --evidence-sha256/--target are used.")
    collect.add_argument("--evidence-sha256")
    collect.add_argument("--target", default="", help="Subsystem or target selector from next_cli_command")
    collect.add_argument("--need", default="", help="Comma-separated request ids or needs")
    collect.add_argument("--request-id", action="append", default=[], help="Specific request_id to execute")
    collect.add_argument("--json", action="store_true", help="Print the full generated More data request")
    collect.add_argument("--host", default="", help="Run local/SSH remote collector against this host")
    collect.add_argument("--collector-mode", choices=["auto", "local", "ssh"], default="auto")
    collect.add_argument("--ssh-user", default="")
    collect.add_argument("--ssh-key", default="")
    collect.add_argument("--unit", action="append", default=[], help="Additional systemd unit to collect")
    collect.add_argument("--path", action="append", default=[], help="Additional artifact path to stat/hash")
    collect.add_argument("--output", default="", help="Write remote collector JSONL here")
    collect.add_argument("--ingest-collector", action="store_true", help="Ingest collected JSONL into the same SQLite DB")
    return parser


def _add_incident_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--service", required=True)
    parser.add_argument("--environment", required=True)
    parser.add_argument("--start", required=True, help="Incident window start, ISO-8601")
    parser.add_argument("--end", required=True, help="Incident window end, ISO-8601")
    parser.add_argument("--lookback-minutes", type=int, default=60)


def _add_provider_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--provider",
        action="append",
        default=[],
        help=(
            "Model provider(s) to run. Repeat or comma-separate. "
            "Supported: local, gemini, claude, gpt-oss, mistral, qwen, glm, llama. "
            "Default: local heuristic providers."
        ),
    )


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    store = SQLiteStore(args.db)

    if args.command == "init-db":
        store.init_schema()
        print(f"initialized {Path(args.db)}")
        return 0

    if args.command == "inspect":
        print(json.dumps(inspect_input(args.input_path), ensure_ascii=False, sort_keys=True, indent=2))
        return 0

    if args.command == "sanitize":
        print(json.dumps(sanitize_input(args.input_path, args.out), ensure_ascii=False, sort_keys=True, indent=2))
        return 0

    if args.command == "verify-sanitized":
        result = verify_sanitized_output(args.output_dir)
        print(format_verification_result(result))
        return 0 if result["passed"] else 2

    if args.command == "sanitize-source":
        print(
            json.dumps(
                sanitize_source(
                    args.project_root,
                    service=args.service,
                    environment=args.environment,
                    output_dir=args.out,
                ),
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
            )
        )
        return 0

    if args.command == "analyze-source":
        print(
            json.dumps(
                analyze_source_context(
                    args.source_context,
                    provider=args.provider,
                    output_dir=args.out,
                ),
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
            )
        )
        return 0

    if args.command == "build-bundle":
        bundle = build_bundle_from_sanitized(
            args.sanitized_events_jsonl,
            service=args.service,
            environment=args.environment,
            start=args.start,
            end=args.end,
            profile_name=args.profile,
            out_path=args.out,
            parent_evidence_sha256=args.parent_evidence_sha256,
            evidence_request_plan_id=args.evidence_request_plan_id,
            collection_mode=args.collection_mode,
        )
        print(bundle["evidence_sha256"])
        return 0

    if args.command == "discover-profile":
        if not args.project_root and not args.source_context:
            raise SystemExit("--project-root or --source-context is required")
        result = discover_profile(
            args.project_root or None,
            evidence_bundle_path=args.evidence_bundle or None,
            service=args.service,
            environment=args.environment,
            output_dir=args.out,
            source_context_path=args.source_context or None,
            source_analysis_path=args.source_analysis or None,
        )
        print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
        return 0

    if args.command == "draft-profile":
        draft = draft_profile(
            args.discovery_bundle,
            provider=args.provider,
            model_name=args.model,
            out_path=args.out,
        )
        print(draft["source_discovery_sha256"])
        return 0

    if args.command == "draft-focused-profile":
        profile = draft_focused_profile(
            args.discovery_bundle,
            provider=args.provider,
            model_name=args.model,
            evidence_bundle_path=args.evidence_bundle or None,
            source_context_path=args.source_context or None,
            source_analysis_path=args.source_analysis or None,
            out_path=args.out,
        )
        print(profile.get("source_discovery_sha256", ""))
        return 0

    if args.command == "approve-profile":
        result = approve_profile_draft(
            args.profile_draft,
            profile_id=args.profile_id,
            approved_by=args.approved_by,
            note=args.note,
            out_path=args.out,
        )
        print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
        return 0

    if args.command == "adk-trace":
        payload = _load_json_file(args.precomputed_payload)
        result = {
            "schema_version": "adk_trace_export.v1",
            "evidence_sha256": payload.get("evidence_sha256") or "",
            "trace": build_adk_tool_contract_trace(payload),
        }
        if args.check_runtime:
            result["adk_runtime"] = adk_dependency_status()
        if args.out:
            output = Path(args.out)
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
        return 0

    if args.command == "run-multi-ai":
        bundle = _load_json_file(args.bundle)
        approved_profile = _load_json_file(args.profile)
        result = run_multi_ai(
            bundle,
            approved_profile,
            providers=[args.providers] if args.providers else None,
            mode=args.mode,
            output_dir=args.out,
            source_context=_load_json_file(args.source_context) if args.source_context else None,
            source_analysis=_load_json_file(args.source_analysis) if args.source_analysis else None,
        )
        if args.json:
            print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
        else:
            _print_multi_ai_summary(result, args.out)
        return 0

    if args.command == "arbitrate-review":
        bundle = _load_json_file(args.bundle)
        profile = _load_json_file(args.profile) if args.profile else {}
        synthesis = _load_json_file(args.multi_ai_synthesis) if args.multi_ai_synthesis else {}
        resolution = resolve_canonical_review_graph_snapshot(
            store if args.persist or args.persist_if_stale else None,
            bundle,
            multi_ai_synthesis=synthesis,
            approved_profile=profile,
            source_context=_load_json_file(args.source_context) if args.source_context else None,
            source_analysis=_load_json_file(args.source_analysis) if args.source_analysis else None,
            persist_if_missing=bool(args.persist),
            persist_if_stale=bool(args.persist_if_stale),
            created_by="cli",
        )
        graph = resolution.get("canonical_review_graph") or {}
        out = Path(args.out)
        out.mkdir(parents=True, exist_ok=True)
        graph_path = out / "canonical_review_graph.json"
        snapshot_path = out / "canonical_review_graph_snapshot.json"
        graph_path.write_text(json.dumps(graph, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        snapshot_path.write_text(json.dumps(resolution.get("snapshot") or {}, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        if args.json:
            print(json.dumps(resolution, ensure_ascii=False, sort_keys=True, indent=2))
        else:
            finding = graph.get("finding") if isinstance(graph.get("finding"), dict) else {}
            summary = graph.get("summary") if isinstance(graph.get("summary"), dict) else {}
            print(f"canonical_review_graph={graph_path}")
            print(f"canonical_review_graph_snapshot={snapshot_path}")
            print(f"canonical_graph_status={resolution.get('canonical_graph_status') or ''}")
            print(f"canonical_graph_sha256={resolution.get('canonical_graph_sha256') or ''}")
            print(f"input_fingerprint_sha256={resolution.get('input_fingerprint_sha256') or ''}")
            print(f"primary_targets={summary.get('primary_count', 0)}")
            print(f"validation_targets={summary.get('validation_count', 0)}")
            print(f"promotion_decisions={len(graph.get('promotion_decisions') or [])}")
            print(f"finding={finding.get('title') or ''}")
            print(f"impact={finding.get('impact') or ''}")
        return 0

    if args.command == "plan-evidence-requests":
        result = plan_evidence_requests(
            args.bundle,
            args.profile,
            answers_path=args.answers or None,
            source_analysis_path=args.source_analysis or None,
            canonical_review_graph_path=args.canonical_review_graph or None,
            output_dir=args.out,
        )
        print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
        return 0

    if args.command == "ingest-logs":
        count = ingest_log_files(args.input, store)
        print(f"ingested_logs={count}")
        return 0

    if args.command == "analyze-jsonl":
        store.init_schema()
        count = ingest_log_files(args.input, store)
        print(f"ingested_logs={count}")
        result = run_pipeline(
            store,
            _incident_from_args(args),
            providers=build_provider_list(args.provider),
        )
        _print_result(result)
        return 0

    if args.command == "run-case":
        store.init_schema()
        selected_files, skipped_files = _expand_run_case_inputs(args.input, excludes=args.exclude)
        if not selected_files:
            raise SystemExit(
                "no ingestible files selected. Pass explicit *.jsonl/*.log files or adjust --exclude."
            )
        count = ingest_log_files(
            selected_files,
            store,
            service=args.service,
            environment=args.environment,
        )
        result = run_pipeline(
            store,
            _incident_from_args(args),
            providers=build_provider_list(args.provider),
            approved_profile=_load_json_file(args.approved_profile) if args.approved_profile else None,
            source_context=_load_json_file(args.source_context) if args.source_context else None,
            source_analysis=_load_json_file(args.source_analysis) if args.source_analysis else None,
        )
        target_set = store.list_review_targets(limit=5, evidence_sha256=result.evidence_sha256)
        review_target_summary = dict(target_set.get("summary") or {})
        if result.canonical_graph_status:
            review_target_summary.update(
                {
                    "review_targets": result.primary_review_target_count + result.validation_target_count,
                    "primary_review_targets": result.primary_review_target_count,
                    "validation_targets": result.validation_target_count,
                    "monitor_only": result.monitor_only_count,
                    "auto_archived": result.auto_archived_count,
                    "source": "canonical_review_graph",
                }
            )
        review_url = _review_url(args.review_base_url, result.evidence_sha256)
        payload = {
            "selected_input_files": len(selected_files),
            "skipped_input_files": len(skipped_files),
            "ingested_logs": count,
            **asdict(result),
            "review_target_summary": review_target_summary,
            "context_inputs": {
                "approved_profile": bool(args.approved_profile),
                "source_context": bool(args.source_context),
                "source_analysis": bool(args.source_analysis),
            },
            "review_url": review_url,
            "serve_command": f"ops-evidence --db {args.db} serve --port {_port_from_url(args.review_base_url) or 8080}",
        }
        if args.json:
            payload["input_files"] = [str(path) for path in selected_files]
            payload["skipped_files"] = [str(path) for path in skipped_files[:200]]
            print(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2))
        else:
            _print_run_case_summary(payload)
        return 0

    if args.command == "create-bundle":
        bundle = EvidenceBundleBuilder(store).build(_incident_from_args(args))
        print(bundle["evidence_sha256"])
        return 0

    if args.command == "run-incident":
        result = run_pipeline(
            store,
            _incident_from_args(args),
            providers=build_provider_list(args.provider),
        )
        _print_result(result)
        return 0

    if args.command == "run-demo":
        result = run_demo(db_path=args.db, sample_path=args.sample)
        _print_result(result)
        return 0

    if args.command == "serve":
        os.environ["OES_DB_PATH"] = str(args.db)
        try:
            import uvicorn
        except Exception as exc:  # pragma: no cover
            raise SystemExit("Install ops-evidence-synthesis[api] to use 'ops-evidence serve'") from exc
        uvicorn.run("ops_evidence_synthesis.api:app", host=args.host, port=args.port, reload=args.reload)
        return 0

    if args.command == "reviews":
        for item in store.list_review_queue(limit=args.limit):
            print(
                "\t".join(
                    [
                        item["proposition_id"],
                        item["priority"],
                        f"{float(item.get('review_priority_score') or 0):.3f}",
                        item["question"],
                    ]
                )
            )
        return 0

    if args.command == "proposals":
        proposals = store.list_proposals(
            limit=args.limit,
            evidence_sha256=args.evidence_sha256,
            pending_only=not args.all,
        )
        if args.json:
            print(json.dumps(proposals, ensure_ascii=False, sort_keys=True, indent=2))
        else:
            for item in proposals:
                actions = item.get("suggested_actions") or []
                first_action = actions[0] if actions else {}
                action_text = (
                    first_action.get("temporary_action")
                    or first_action.get("permanent_action")
                    or "no suggested action"
                )
                print(
                    "\t".join(
                        [
                            item["proposition_id"],
                            item["priority"],
                            f"{float(item.get('review_priority_score') or 0):.3f}",
                            item["question"],
                            str(action_text),
                        ]
                    )
                )
        return 0

    if args.command == "review":
        review_id = store.record_review(args.proposition_id, args.decision, args.reviewer, args.note)
        print(review_id)
        return 0

    if args.command == "collect-more":
        review_target_id = args.review_target_id or _find_review_target_id(
            store,
            evidence_sha256=args.evidence_sha256,
            target=args.target,
        )
        if not review_target_id:
            raise SystemExit("review target not found; pass review_target_id or --evidence-sha256 with --target")
        request_ids = list(args.request_id or [])
        request_ids.extend(_csv_values(args.need))
        query = store.build_more_data_query_for_target(review_target_id, request_ids=request_ids or None)
        collector_summary: dict[str, object] | None = None
        if args.host or args.unit or args.path:
            collector_summary = _run_collect_more_host(
                store,
                review_target_id=review_target_id,
                query=query,
                request_ids=request_ids,
                host=args.host or "localhost",
                mode=args.collector_mode,
                ssh_user=args.ssh_user,
                ssh_key_path=args.ssh_key,
                units=list(args.unit or []),
                paths=list(args.path or []),
                output=args.output,
                ingest=bool(args.ingest_collector),
            )
        if args.json:
            if collector_summary:
                query["remote_collector"] = collector_summary
            print(json.dumps(query, ensure_ascii=False, sort_keys=True, indent=2))
        else:
            next_query = query.get("next_query") or {}
            print(f"review_target_id={review_target_id}")
            print(f"preview_count={next_query.get('preview_count', 0)}")
            for row in next_query.get("queries") or []:
                print(
                    "\t".join(
                        [
                            str(row.get("request_id") or ""),
                            str(row.get("request_type") or ""),
                            str(row.get("preview_count") or 0),
                            str(row.get("description") or ""),
                        ]
                    )
                )
            if collector_summary:
                print(f"remote_collector_events={collector_summary.get('event_count', 0)}")
                print(f"remote_collector_output={collector_summary.get('output_path', '')}")
                print(f"remote_collector_ingested={collector_summary.get('ingested_logs', 0)}")
        return 0

    raise AssertionError(f"unhandled command: {args.command}")


def _incident_from_args(args: argparse.Namespace) -> IncidentWindow:
    return IncidentWindow(
        service=args.service,
        environment=args.environment,
        incident_start=args.start,
        incident_end=args.end,
        lookback_minutes=args.lookback_minutes,
    )


def _expand_run_case_inputs(
    inputs: list[str],
    *,
    excludes: list[str],
) -> tuple[list[Path], list[Path]]:
    exclude_patterns = [*DEFAULT_RUN_CASE_EXCLUDES, *(excludes or [])]
    selected: list[Path] = []
    skipped: list[Path] = []
    for raw_path in inputs:
        root = Path(raw_path).expanduser()
        if not root.exists():
            raise SystemExit(f"input path not found: {root}")
        if root.is_file():
            if _matches_any_pattern(root, root.parent, exclude_patterns):
                skipped.append(root)
            else:
                selected.append(root)
            continue
        if not root.is_dir():
            skipped.append(root)
            continue
        for current, dirnames, filenames in os.walk(root):
            current_path = Path(current)
            dirnames[:] = [
                dirname
                for dirname in sorted(dirnames)
                if not _matches_any_pattern(current_path / dirname, root, exclude_patterns)
            ]
            for filename in sorted(filenames):
                path = current_path / filename
                if _matches_any_pattern(path, root, exclude_patterns) or not _looks_ingestible_log_file(path):
                    skipped.append(path)
                    continue
                selected.append(path)
    deduped: list[Path] = []
    seen: set[str] = set()
    for path in selected:
        key = str(path.resolve())
        if key in seen:
            continue
        seen.add(key)
        deduped.append(path)
    return deduped, skipped


def _matches_any_pattern(path: Path, root: Path, patterns: list[str] | tuple[str, ...]) -> bool:
    name = path.name
    full = str(path).replace(os.sep, "/")
    try:
        relative = str(path.relative_to(root)).replace(os.sep, "/")
    except ValueError:
        relative = name
    candidates = {name, relative, full}
    for pattern in patterns:
        normalized = str(pattern).replace(os.sep, "/")
        if any(fnmatch.fnmatch(candidate, normalized) for candidate in candidates):
            return True
    return False


def _looks_ingestible_log_file(path: Path) -> bool:
    return path.suffix.casefold() in DIRECTORY_LOG_FILE_SUFFIXES


def _review_url(base_url: str, evidence_sha256: str) -> str:
    base = str(base_url or "").rstrip("/")
    if not base:
        return ""
    return f"{base}/?evidence_sha256={evidence_sha256}"


def _port_from_url(base_url: str) -> int | None:
    text = str(base_url or "")
    marker = "://"
    if marker in text:
        text = text.split(marker, 1)[1]
    host_port = text.split("/", 1)[0]
    if ":" not in host_port:
        return None
    try:
        return int(host_port.rsplit(":", 1)[1])
    except ValueError:
        return None


def _print_run_case_summary(payload: dict[str, object]) -> None:
    ordered_keys = (
        "selected_input_files",
        "skipped_input_files",
        "ingested_logs",
        "evidence_sha256",
        "model_run_count",
        "parsed_result_count",
        "claim_count",
        "proposition_count",
        "cluster_count",
        "review_queue_count",
        "canonical_graph_status",
        "canonical_graph_sha256",
        "input_fingerprint_sha256",
        "primary_review_target_count",
        "validation_target_count",
    )
    for key in ordered_keys:
        print(f"{key}={payload.get(key)}")
    summary = payload.get("review_target_summary")
    if isinstance(summary, dict):
        print(f"primary_review_targets={summary.get('primary_review_targets', 0)}")
        print(f"validation_targets={summary.get('validation_targets', 0)}")
    print(f"review_url={payload.get('review_url')}")
    print(f"serve_command={payload.get('serve_command')}")


def _print_multi_ai_summary(result: dict[str, object], output_dir: str) -> None:
    synthesis = result.get("multi_ai_synthesis") if isinstance(result.get("multi_ai_synthesis"), dict) else {}
    print(f"evidence_sha256={result.get('evidence_sha256')}")
    print(f"model_runs={output_dir}/model_runs.jsonl")
    print(f"multi_ai_synthesis={output_dir}/multi_ai_synthesis.json")
    print(f"canonical_review_graph={output_dir}/canonical_review_graph.json")
    print(f"canonical_review_graph_snapshot={output_dir}/canonical_review_graph_snapshot.json")
    print(f"review_targets={output_dir}/review_targets.json")
    print("providers:")
    for run in result.get("model_runs") or []:
        if not isinstance(run, dict):
            continue
        print(
            "  "
            + f"{run.get('provider_id')}: {run.get('status')} "
            + f"schema_valid={str(bool(run.get('schema_valid'))).lower()}"
        )
    print(f"agreement_groups={len(synthesis.get('agreement_groups') or [])}")
    print(f"disagreement_groups={len(synthesis.get('disagreement_groups') or [])}")
    print(f"disagreement_themes={len(synthesis.get('disagreement_themes') or [])}")
    graph = result.get("canonical_review_graph") if isinstance(result.get("canonical_review_graph"), dict) else {}
    graph_summary = graph.get("summary") if isinstance(graph.get("summary"), dict) else {}
    print(f"validation_targets={len(synthesis.get('validation_targets') or [])}")
    print(f"canonical_graph_status={result.get('canonical_graph_status') or ''}")
    print(f"canonical_graph_sha256={result.get('canonical_graph_sha256') or ''}")
    print(f"input_fingerprint_sha256={result.get('input_fingerprint_sha256') or ''}")
    print(f"canonical_primary_targets={graph_summary.get('primary_count', 0)}")
    print(f"canonical_validation_targets={graph_summary.get('validation_count', 0)}")
    print(f"promotion_decisions={len(graph.get('promotion_decisions') or [])}")
    finding = graph.get("finding") if isinstance(graph.get("finding"), dict) else {}
    print(f"finding={finding.get('title') or ''}")
    print(f"impact={finding.get('impact') or ''}")
    print(f"score_note={synthesis.get('score_note')}")


def _find_review_target_id(store: SQLiteStore, *, evidence_sha256: str | None, target: str) -> str:
    target_set = store.list_review_targets(limit=100, evidence_sha256=evidence_sha256, pending_only=False)
    wanted = str(target or "").strip().casefold()
    for item in target_set.get("targets") or []:
        if not wanted or wanted in {
            str(item.get("subsystem") or "").casefold(),
            str(item.get("core_target_type") or "").casefold(),
            str(item.get("review_target_type") or "").casefold(),
        }:
            return str(item.get("review_target_id") or "")
    return ""


def _csv_values(value: str) -> list[str]:
    return [item.strip() for item in str(value or "").split(",") if item.strip()]


def _load_json_file(path: str | Path) -> dict[str, object]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise SystemExit(f"{path} must contain a JSON object")
    return payload


def _run_collect_more_host(
    store: SQLiteStore,
    *,
    review_target_id: str,
    query: dict[str, object],
    request_ids: list[str],
    host: str,
    mode: str,
    ssh_user: str,
    ssh_key_path: str,
    units: list[str],
    paths: list[str],
    output: str,
    ingest: bool,
) -> dict[str, object]:
    target = store.get_review_target(review_target_id) or {}
    parent = store.get_bundle(str(target.get("evidence_sha256") or "")) or {}
    next_query = query.get("next_query") if isinstance(query.get("next_query"), dict) else {}
    targets = collector_targets_from_more_data(
        next_query,
        units=units,
        paths=paths,
        request_ids=request_ids or None,
    )
    service = str(parent.get("service") or target.get("service") or "ops-evidence")
    environment = str(parent.get("environment") or target.get("environment") or "prod")
    config = RemoteCollectorConfig(
        host=host,
        service=service,
        environment=environment,
        mode=mode,
        ssh_user=ssh_user,
        ssh_key_path=ssh_key_path,
    )
    events = collect_remote_evidence(
        config,
        units=targets["units"],
        paths=targets["paths"],
        request_ids=request_ids or None,
        since=str((next_query.get("search_window") or {}).get("start") or ""),
        until=str((next_query.get("search_window") or {}).get("end") or ""),
    )
    output_path = output or f"workspace/remote_collector_{review_target_id}.jsonl"
    write_jsonl_events(events, output_path)
    ingested = 0
    if ingest and events:
        ingested = store.insert_sanitized_logs(sanitize_logs(RawLog.from_mapping(event) for event in events))
    return {
        "host": host,
        "mode": mode,
        "units": targets["units"],
        "paths": targets["paths"],
        "event_count": len(events),
        "output_path": output_path,
        "ingested_logs": ingested,
    }


def _print_result(result: object) -> None:
    for key, value in asdict(result).items():
        print(f"{key}={value}")


if __name__ == "__main__":
    raise SystemExit(main())
