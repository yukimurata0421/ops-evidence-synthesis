from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from ops_evidence_synthesis import cli
from ops_evidence_synthesis.models import IncidentWindow
from ops_evidence_synthesis.synthesis.pipeline import PipelineResult


class _FakeStore:
    def __init__(self, path: str) -> None:
        self.path = path
        self.initialized = False

    def init_schema(self) -> None:
        self.initialized = True

    def list_review_targets(
        self,
        *,
        limit: int,
        evidence_sha256: str | None = None,
        pending_only: bool = True,
    ) -> dict[str, Any]:
        del limit, pending_only
        return {
            "targets": [
                {
                    "review_target_id": "rt-1",
                    "evidence_sha256": evidence_sha256 or "sha",
                    "subsystem": "runtime",
                    "core_target_type": "restart_loop",
                    "review_target_type": "validation_target",
                }
            ],
            "summary": {"review_targets": 1},
        }

    def list_review_queue(self, *, limit: int) -> list[dict[str, Any]]:
        del limit
        return [
            {
                "proposition_id": "prop-1",
                "priority": "high",
                "review_priority_score": 0.75,
                "question": "Is runtime impact established?",
            }
        ]

    def list_proposals(
        self,
        *,
        limit: int,
        evidence_sha256: str | None,
        pending_only: bool,
    ) -> list[dict[str, Any]]:
        del limit, evidence_sha256, pending_only
        return [
            {
                "proposition_id": "prop-1",
                "priority": "high",
                "review_priority_score": 0.75,
                "question": "Is runtime impact established?",
                "suggested_actions": [{"temporary_action": "collect runtime evidence"}],
            }
        ]

    def record_review(self, proposition_id: str, decision: str, reviewer: str, note: str) -> str:
        assert (proposition_id, decision, reviewer, note) == (
            "prop-1",
            "needs_more_data",
            "operator",
            "collect runtime evidence",
        )
        return "review-1"

    def build_more_data_query_for_target(
        self,
        review_target_id: str,
        *,
        request_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        assert review_target_id == "rt-1"
        return {
            "review_target_id": review_target_id,
            "request_ids": request_ids or [],
            "next_query": {
                "preview_count": 1,
                "queries": [
                    {
                        "request_id": "req-1",
                        "request_type": "runtime_log",
                        "preview_count": 1,
                        "description": "Collect runtime log evidence.",
                    }
                ],
            },
        }


def _pipeline_result() -> PipelineResult:
    return PipelineResult(
        evidence_sha256="e" * 64,
        model_run_count=2,
        parsed_result_count=2,
        claim_count=3,
        proposition_count=2,
        score_count=2,
        cluster_count=1,
        review_queue_count=1,
        canonical_graph_status="persisted",
        canonical_graph_sha256="g" * 64,
        input_fingerprint_sha256="f" * 64,
        primary_review_target_count=1,
        validation_target_count=2,
        monitor_only_count=3,
        auto_archived_count=4,
    )


def test_parser_exposes_all_commands_and_safe_provider_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OES_MULTI_AI_DEFAULT_PROVIDERS", "gemini,mistral")
    parser = cli.build_parser()
    subcommands = next(
        action.choices
        for action in parser._actions
        if isinstance(action, argparse._SubParsersAction)
    )

    assert set(subcommands) == {
        "init-db",
        "inspect",
        "sanitize",
        "verify-sanitized",
        "sanitize-source",
        "analyze-source",
        "build-bundle",
        "discover-profile",
        "draft-profile",
        "draft-focused-profile",
        "approve-profile",
        "adk-trace",
        "run-multi-ai",
        "arbitrate-review",
        "plan-evidence-requests",
        "ingest-logs",
        "analyze-jsonl",
        "run-case",
        "create-bundle",
        "run-incident",
        "run-demo",
        "serve",
        "reviews",
        "proposals",
        "review",
        "collect-more",
    }

    multi = parser.parse_args(
        ["run-multi-ai", "--bundle", "bundle.json", "--profile", "profile.json", "--out", "out"]
    )
    assert multi.providers == "gemini,mistral"
    assert multi.mode == "real_or_skip"
    assert multi.json is False

    collect = parser.parse_args(["collect-more", "rt-1"])
    assert collect.collector_mode == "auto"
    assert collect.ingest_collector is False


def test_parser_rejects_missing_required_args_and_unknown_execution_mode() -> None:
    parser = cli.build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["run-multi-ai", "--bundle", "bundle.json"])
    with pytest.raises(SystemExit):
        parser.parse_args(
            [
                "run-multi-ai",
                "--bundle",
                "bundle.json",
                "--profile",
                "profile.json",
                "--out",
                "out",
                "--mode",
                "unbounded",
            ]
        )


def test_run_case_input_expansion_filters_media_caches_and_duplicates(tmp_path: Path) -> None:
    root = tmp_path / "inputs"
    root.mkdir()
    selected_log = root / "runtime.jsonl"
    selected_log.write_text("{}\n", encoding="utf-8")
    skipped_log = root / "skip-debug.log"
    skipped_log.write_text("debug\n", encoding="utf-8")
    media = root / "audio.mp3"
    media.write_bytes(b"audio")
    notes = root / "notes.md"
    notes.write_text("not a log\n", encoding="utf-8")
    cache = root / ".git"
    cache.mkdir()
    (cache / "hidden.log").write_text("hidden\n", encoding="utf-8")

    selected, skipped = cli._expand_run_case_inputs(
        [str(root), str(selected_log)],
        excludes=["skip-*.log"],
    )

    assert selected == [selected_log]
    assert set(skipped) == {skipped_log, media, notes}
    assert cli._matches_any_pattern(root / "sub" / "app.log", root, ["sub/*.log"])
    assert cli._looks_ingestible_log_file(root / "service.out")
    assert not cli._looks_ingestible_log_file(notes)
    with pytest.raises(SystemExit, match="input path not found"):
        cli._expand_run_case_inputs([str(root / "missing")], excludes=[])


def test_cli_pure_helpers_cover_urls_csv_json_and_target_selection(tmp_path: Path) -> None:
    assert cli._review_url("http://127.0.0.1:8090/", "sha") == "http://127.0.0.1:8090/?evidence_sha256=sha"
    assert cli._review_url("", "sha") == ""
    assert cli._port_from_url("http://127.0.0.1:8090/path") == 8090
    assert cli._port_from_url("https://example.test") is None
    assert cli._port_from_url("http://example.test:not-a-port") is None
    assert cli._csv_values("one, two,,three") == ["one", "two", "three"]

    object_path = tmp_path / "object.json"
    object_path.write_text('{"value": 1}\n', encoding="utf-8")
    assert cli._load_json_file(object_path) == {"value": 1}
    list_path = tmp_path / "list.json"
    list_path.write_text("[]\n", encoding="utf-8")
    with pytest.raises(SystemExit, match="must contain a JSON object"):
        cli._load_json_file(list_path)

    store = _FakeStore(str(tmp_path / "store.sqlite3"))
    assert cli._find_review_target_id(store, evidence_sha256="sha", target="runtime") == "rt-1"
    assert cli._find_review_target_id(store, evidence_sha256="sha", target="missing") == ""


def test_incident_and_human_readable_summaries(capsys: pytest.CaptureFixture[str]) -> None:
    incident = cli._incident_from_args(
        argparse.Namespace(
            service="api",
            environment="prod",
            start="2026-07-18T00:00:00Z",
            end="2026-07-18T01:00:00Z",
            lookback_minutes=120,
        )
    )
    assert incident == IncidentWindow(
        service="api",
        environment="prod",
        incident_start="2026-07-18T00:00:00Z",
        incident_end="2026-07-18T01:00:00Z",
        lookback_minutes=120,
    )

    cli._print_run_case_summary(
        {
            "selected_input_files": 2,
            "review_target_summary": {"primary_review_targets": 1, "validation_targets": 2},
            "review_url": "https://review.example/",
            "serve_command": "serve locally",
        }
    )
    run_case_output = capsys.readouterr().out
    assert "selected_input_files=2" in run_case_output
    assert "primary_review_targets=1" in run_case_output
    assert "validation_targets=2" in run_case_output

    cli._print_multi_ai_summary(
        {
            "evidence_sha256": "sha",
            "model_runs": [{"provider_id": "gemini", "status": "ok", "schema_valid": True}],
            "multi_ai_synthesis": {
                "agreement_groups": [{}],
                "disagreement_groups": [{}],
                "disagreement_themes": [],
                "validation_targets": [{}],
                "score_note": "review priority only",
            },
            "canonical_review_graph": {
                "summary": {"primary_count": 1, "validation_count": 1},
                "promotion_decisions": [],
                "finding": {"title": "Runtime review", "impact": "Impact unconfirmed"},
            },
            "canonical_graph_status": "persisted",
            "canonical_graph_sha256": "graph",
            "input_fingerprint_sha256": "fingerprint",
        },
        "out",
    )
    multi_output = capsys.readouterr().out
    assert "gemini: ok schema_valid=true" in multi_output
    assert "agreement_groups=1" in multi_output
    assert "finding=Runtime review" in multi_output

    cli._print_result(_pipeline_result())
    assert "evidence_sha256=" + "e" * 64 in capsys.readouterr().out


def test_main_initializes_sqlite_and_reads_empty_queues(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db_path = tmp_path / "ops.sqlite3"

    assert cli.main(["--db", str(db_path), "init-db"]) == 0
    assert db_path.is_file()
    assert "initialized" in capsys.readouterr().out

    assert cli.main(["--db", str(db_path), "reviews"]) == 0
    assert capsys.readouterr().out == ""

    assert cli.main(["--db", str(db_path), "proposals", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == []


def test_main_dispatches_local_inspect_sanitize_and_verification(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(cli, "SQLiteStore", _FakeStore)
    monkeypatch.setattr(cli, "inspect_input", lambda path: {"input_path": path, "file_count": 1})
    monkeypatch.setattr(
        cli,
        "sanitize_input",
        lambda path, out, *, start, end: {"input_path": path, "output_dir": out, "start": start, "end": end},
    )
    verification = {"passed": False}
    monkeypatch.setattr(cli, "verify_sanitized_output", lambda path: {**verification, "output_dir": path})
    monkeypatch.setattr(cli, "format_verification_result", lambda result: f"passed={result['passed']}")

    assert cli.main(["inspect", "input.log"]) == 0
    assert json.loads(capsys.readouterr().out)["file_count"] == 1

    assert cli.main(["sanitize", "input.log", "--out", str(tmp_path / "out")]) == 0
    assert json.loads(capsys.readouterr().out)["output_dir"] == str(tmp_path / "out")

    assert cli.main(["verify-sanitized", str(tmp_path / "out")]) == 2
    assert capsys.readouterr().out.strip() == "passed=False"
    verification["passed"] = True
    assert cli.main(["verify-sanitized", str(tmp_path / "out")]) == 0
    assert capsys.readouterr().out.strip() == "passed=True"


def test_main_dispatches_source_and_profile_commands(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(cli, "SQLiteStore", _FakeStore)
    monkeypatch.setattr(cli, "sanitize_source", lambda *args, **kwargs: {"source_context_sha256": "context"})
    monkeypatch.setattr(cli, "analyze_source_context", lambda *args, **kwargs: {"analysis_sha256": "analysis"})
    monkeypatch.setattr(cli, "discover_profile", lambda *args, **kwargs: {"discovery_sha256": "discovery"})
    monkeypatch.setattr(cli, "draft_profile", lambda *args, **kwargs: {"source_discovery_sha256": "discovery"})
    monkeypatch.setattr(
        cli,
        "draft_focused_profile",
        lambda *args, **kwargs: {"source_discovery_sha256": "focused-discovery"},
    )
    monkeypatch.setattr(cli, "approve_profile_draft", lambda *args, **kwargs: {"profile_id": "approved"})

    assert cli.main(
        [
            "sanitize-source",
            "--project-root",
            str(tmp_path),
            "--service",
            "api",
            "--environment",
            "prod",
            "--out",
            str(tmp_path / "source"),
        ]
    ) == 0
    assert json.loads(capsys.readouterr().out)["source_context_sha256"] == "context"

    assert cli.main(
        [
            "analyze-source",
            "--source-context",
            "context.json",
            "--provider",
            "local",
            "--out",
            str(tmp_path / "analysis"),
        ]
    ) == 0
    assert json.loads(capsys.readouterr().out)["analysis_sha256"] == "analysis"

    with pytest.raises(SystemExit, match="--project-root or --source-context is required"):
        cli.main(["discover-profile", "--service", "api", "--environment", "prod", "--out", "out"])
    assert cli.main(
        [
            "discover-profile",
            "--project-root",
            str(tmp_path),
            "--service",
            "api",
            "--environment",
            "prod",
            "--out",
            "out",
        ]
    ) == 0
    assert json.loads(capsys.readouterr().out)["discovery_sha256"] == "discovery"

    assert cli.main(
        ["draft-profile", "--discovery-bundle", "discovery.json", "--provider", "local", "--out", "draft.json"]
    ) == 0
    assert capsys.readouterr().out.strip() == "discovery"

    assert cli.main(
        [
            "draft-focused-profile",
            "--discovery-bundle",
            "discovery.json",
            "--provider",
            "local",
            "--out",
            "focused.json",
        ]
    ) == 0
    assert capsys.readouterr().out.strip() == "focused-discovery"

    assert cli.main(
        [
            "approve-profile",
            "--profile-draft",
            "draft.json",
            "--profile-id",
            "approved",
            "--approved-by",
            "operator",
            "--out",
            "profile.json",
        ]
    ) == 0
    assert json.loads(capsys.readouterr().out)["profile_id"] == "approved"


def test_main_dispatches_adk_multi_ai_arbitration_and_planner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(cli, "SQLiteStore", _FakeStore)
    payload_path = tmp_path / "payload.json"
    payload_path.write_text('{"evidence_sha256": "sha"}\n', encoding="utf-8")
    profile_path = tmp_path / "profile.json"
    profile_path.write_text('{"profile_id": "approved"}\n', encoding="utf-8")
    monkeypatch.setattr(cli, "build_adk_tool_contract_trace", lambda payload: [{"tool": "validate"}])
    monkeypatch.setattr(cli, "adk_dependency_status", lambda: {"available": False})

    trace_path = tmp_path / "trace.json"
    assert cli.main(
        [
            "adk-trace",
            "--precomputed-payload",
            str(payload_path),
            "--out",
            str(trace_path),
            "--check-runtime",
        ]
    ) == 0
    assert json.loads(trace_path.read_text(encoding="utf-8"))["trace"] == [{"tool": "validate"}]
    capsys.readouterr()

    captured: dict[str, Any] = {}

    def fake_multi(bundle: dict[str, Any], profile: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        captured.update({"bundle": bundle, "profile": profile, **kwargs})
        return {
            "evidence_sha256": "sha",
            "model_runs": [],
            "multi_ai_synthesis": {},
            "canonical_review_graph": {},
        }

    monkeypatch.setattr(cli, "run_multi_ai", fake_multi)
    assert cli.main(
        [
            "run-multi-ai",
            "--bundle",
            str(payload_path),
            "--profile",
            str(profile_path),
            "--providers",
            "mistral,qwen",
            "--mode",
            "local",
            "--out",
            str(tmp_path / "multi"),
            "--json",
        ]
    ) == 0
    assert captured["providers"] == ["mistral,qwen"]
    assert captured["mode"] == "local"
    assert json.loads(capsys.readouterr().out)["evidence_sha256"] == "sha"

    resolution = {
        "canonical_review_graph": {
            "summary": {"primary_count": 0, "validation_count": 1},
            "promotion_decisions": [],
        },
        "snapshot": {"snapshot_status": "persisted"},
        "canonical_graph_status": "persisted",
        "canonical_graph_sha256": "graph",
        "input_fingerprint_sha256": "fingerprint",
    }
    monkeypatch.setattr(cli, "resolve_canonical_review_graph_snapshot", lambda *args, **kwargs: resolution)
    arbitration_dir = tmp_path / "arbitration"
    assert cli.main(
        [
            "arbitrate-review",
            "--bundle",
            str(payload_path),
            "--profile",
            str(profile_path),
            "--out",
            str(arbitration_dir),
            "--persist",
            "--json",
        ]
    ) == 0
    assert (arbitration_dir / "canonical_review_graph.json").is_file()
    assert json.loads(capsys.readouterr().out)["canonical_graph_status"] == "persisted"

    monkeypatch.setattr(cli, "plan_evidence_requests", lambda *args, **kwargs: {"plan_id": "plan-1"})
    assert cli.main(
        [
            "plan-evidence-requests",
            "--bundle",
            str(payload_path),
            "--profile",
            str(profile_path),
            "--out",
            str(tmp_path / "plan"),
        ]
    ) == 0
    assert json.loads(capsys.readouterr().out)["plan_id"] == "plan-1"


def test_main_run_case_preserves_context_and_canonical_target_counts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(cli, "SQLiteStore", _FakeStore)
    log_path = tmp_path / "runtime.log"
    log_path.write_text("runtime event\n", encoding="utf-8")
    monkeypatch.setattr(cli, "ingest_log_files", lambda *args, **kwargs: 1)
    captured: dict[str, Any] = {}

    def fake_pipeline(store: Any, incident: IncidentWindow, providers: Any, **kwargs: Any) -> PipelineResult:
        captured.update({"store": store, "incident": incident, "providers": providers, **kwargs})
        return _pipeline_result()

    monkeypatch.setattr(cli, "run_pipeline", fake_pipeline)
    monkeypatch.setattr(cli, "build_provider_list", lambda providers: ["provider:" + value for value in providers])

    assert cli.main(
        [
            "--db",
            str(tmp_path / "case.sqlite3"),
            "run-case",
            "--input",
            str(log_path),
            "--service",
            "runtime",
            "--environment",
            "prod",
            "--start",
            "2026-07-18T00:00:00Z",
            "--end",
            "2026-07-18T01:00:00Z",
            "--provider",
            "local",
            "--review-base-url",
            "http://127.0.0.1:8090",
            "--json",
        ]
    ) == 0

    output = json.loads(capsys.readouterr().out)
    assert output["selected_input_files"] == 1
    assert output["primary_review_target_count"] == 1
    assert output["validation_target_count"] == 2
    assert output["review_target_summary"]["source"] == "canonical_review_graph"
    assert output["review_url"].endswith("?evidence_sha256=" + "e" * 64)
    assert output["serve_command"].endswith("serve --port 8090")
    assert captured["incident"].service == "runtime"
    assert captured["providers"] == ["provider:local"]


def test_main_dispatches_pipeline_commands_without_external_services(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(cli, "SQLiteStore", _FakeStore)
    monkeypatch.setattr(cli, "ingest_log_files", lambda *args, **kwargs: 3)
    monkeypatch.setattr(cli, "build_provider_list", lambda values: values or ["local"])
    monkeypatch.setattr(cli, "run_pipeline", lambda *args, **kwargs: _pipeline_result())
    monkeypatch.setattr(cli, "run_demo", lambda *args, **kwargs: _pipeline_result())

    class FakeBundleBuilder:
        def __init__(self, store: Any) -> None:
            self.store = store

        def build(self, incident: IncidentWindow) -> dict[str, Any]:
            assert incident.service == "runtime"
            return {"evidence_sha256": "bundle-sha"}

    monkeypatch.setattr(cli, "EvidenceBundleBuilder", FakeBundleBuilder)
    incident_args = [
        "--service",
        "runtime",
        "--environment",
        "prod",
        "--start",
        "2026-07-18T00:00:00Z",
        "--end",
        "2026-07-18T01:00:00Z",
    ]

    assert cli.main(["ingest-logs", "--input", "runtime.log"]) == 0
    assert capsys.readouterr().out.strip() == "ingested_logs=3"

    assert cli.main(["analyze-jsonl", "--input", "runtime.log", *incident_args]) == 0
    assert "evidence_sha256=" + "e" * 64 in capsys.readouterr().out

    assert cli.main(["create-bundle", *incident_args]) == 0
    assert capsys.readouterr().out.strip() == "bundle-sha"

    assert cli.main(["run-incident", *incident_args]) == 0
    assert "canonical_graph_status=persisted" in capsys.readouterr().out

    assert cli.main(["run-demo", "--sample", "sample.jsonl"]) == 0
    assert "model_run_count=2" in capsys.readouterr().out


def test_main_serves_and_handles_human_review_commands(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(cli, "SQLiteStore", _FakeStore)
    served: dict[str, Any] = {}
    monkeypatch.setitem(
        sys.modules,
        "uvicorn",
        SimpleNamespace(run=lambda app, **kwargs: served.update({"app": app, **kwargs})),
    )

    assert cli.main(
        [
            "--db",
            str(tmp_path / "serve.sqlite3"),
            "serve",
            "--host",
            "0.0.0.0",
            "--port",
            "8099",
            "--reload",
        ]
    ) == 0
    assert served == {
        "app": "ops_evidence_synthesis.api:app",
        "host": "0.0.0.0",
        "port": 8099,
        "reload": True,
    }

    assert cli.main(["reviews", "--limit", "1"]) == 0
    assert "prop-1\thigh\t0.750" in capsys.readouterr().out

    assert cli.main(["proposals", "--all"]) == 0
    assert "collect runtime evidence" in capsys.readouterr().out

    assert cli.main(
        [
            "review",
            "prop-1",
            "needs_more_data",
            "--reviewer",
            "operator",
            "--note",
            "collect runtime evidence",
        ]
    ) == 0
    assert capsys.readouterr().out.strip() == "review-1"

    assert cli.main(
        [
            "collect-more",
            "rt-1",
            "--request-id",
            "req-1",
            "--need",
            "req-2,req-3",
            "--json",
        ]
    ) == 0
    query = json.loads(capsys.readouterr().out)
    assert query["request_ids"] == ["req-1", "req-2", "req-3"]

    assert cli.main(["collect-more", "--evidence-sha256", "sha", "--target", "runtime"]) == 0
    assert "review_target_id=rt-1" in capsys.readouterr().out
