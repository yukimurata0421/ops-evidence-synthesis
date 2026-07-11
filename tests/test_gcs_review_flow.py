from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

from ops_evidence_synthesis.profile_review import build_approved_operational_profile


ROOT = Path(__file__).resolve().parents[1]


def _load_script() -> ModuleType:
    path = ROOT / "scripts" / "gcs_review_flow.py"
    spec = importlib.util.spec_from_file_location("gcs_review_flow", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_log_input_must_be_absolute() -> None:
    script = _load_script()

    with pytest.raises(SystemExit, match="LOG_INPUT must be an absolute path"):
        script._absolute_existing_input_path("data/sample_logs.jsonl")


def test_log_input_must_exist(tmp_path: Path) -> None:
    script = _load_script()
    missing = tmp_path / "missing.jsonl"

    with pytest.raises(SystemExit, match="log input was not found"):
        script._absolute_existing_input_path(str(missing))


def test_output_dir_must_be_absolute() -> None:
    script = _load_script()

    with pytest.raises(SystemExit, match="OUT must be an absolute path"):
        script._absolute_output_dir("workspace/gcs_review/run")


def test_default_output_dir_uses_repo_analyses() -> None:
    script = _load_script()

    assert script._default_output_dir("review-20260706000000") == (
        ROOT / "analyses" / "review-20260706000000"
    )


def test_required_prompt_value_fails_without_tty() -> None:
    script = _load_script()

    with pytest.raises(SystemExit, match="START"):
        script._required_prompt_value(
            "",
            "Incident window start",
            "2026-06-14T23:15:50Z",
            env_name="START",
            flag_name="--start",
            no_prompts=True,
        )


def test_optional_prompt_value_uses_default_without_tty() -> None:
    script = _load_script()

    assert (
        script._required_prompt_value(
            "",
            "Service name",
            "stream_v3_runtime",
            env_name="SERVICE",
            flag_name="--service",
            required=False,
            no_prompts=True,
        )
        == "stream_v3_runtime"
    )


def test_optional_source_root_can_be_omitted_without_tty() -> None:
    script = _load_script()

    assert script._optional_source_root("", no_prompts=True) is None


def test_source_root_accepts_multiple_lines_and_normalizes_to_project_root(tmp_path: Path) -> None:
    script = _load_script()
    root = tmp_path / "stream_v3"
    for relative in ("deploy", "deploy/k3s", "tests", "src"):
        (root / relative).mkdir(parents=True, exist_ok=True)
    (root / "pyproject.toml").write_text("[project]\nname='stream-v3'\n", encoding="utf-8")

    text = "\n".join(
        [
            str(root / "deploy"),
            str(root / "deploy" / "k3s"),
            str(root / "tests"),
            str(root / "src"),
        ]
    )

    assert script._optional_source_root(text, no_prompts=True) == root


def test_single_pasted_source_subdir_normalizes_to_project_root(tmp_path: Path) -> None:
    script = _load_script()
    root = tmp_path / "stream_v3"
    for relative in ("deploy/k3s", "docs", "ops", "src", "tests"):
        (root / relative).mkdir(parents=True, exist_ok=True)
    (root / "pyproject.toml").write_text("[project]\nname='stream-v3'\n", encoding="utf-8")

    assert script._optional_source_root(str(root / "deploy"), no_prompts=True) == root
    assert script._optional_source_root(str(root / "deploy" / "k3s"), no_prompts=True) == root
    assert script._optional_source_root(str(root / "docs"), no_prompts=True) == root


def test_source_root_must_be_absolute() -> None:
    script = _load_script()

    with pytest.raises(SystemExit, match="SOURCE_ROOT must be an absolute path"):
        script._optional_source_root("sample_projects/profile_discovery_sample", no_prompts=True)


def test_source_root_must_be_directory(tmp_path: Path) -> None:
    script = _load_script()
    file_path = tmp_path / "source.py"
    file_path.write_text("print('ok')\n", encoding="utf-8")

    with pytest.raises(SystemExit, match="SOURCE_ROOT must be a directory"):
        script._optional_source_root(str(file_path), no_prompts=True)


def test_timestamp_prompt_values_are_validated() -> None:
    script = _load_script()

    assert (
        script._required_timestamp_value(
            "2026-07-01",
            "Incident window start",
            "2026-06-14T23:15:50Z",
            env_name="START",
            flag_name="--start",
            no_prompts=True,
        )
        == "2026-07-01T00:00:00Z"
    )
    with pytest.raises(SystemExit, match="START must be ISO-8601"):
        script._required_timestamp_value(
            "/path/that/does/not/exist/src2026-07-01",
            "Incident window start",
            "2026-06-14T23:15:50Z",
            env_name="START",
            flag_name="--start",
            no_prompts=True,
        )


def test_misplaced_source_paths_do_not_become_service_or_start_values(tmp_path: Path) -> None:
    script = _load_script()
    script._PENDING_TIMESTAMP_LINES.clear()
    root = tmp_path / "stream_v3"
    src = root / "src"
    src.mkdir(parents=True)

    assert (
        script._required_prompt_value(
            str(src),
            "Service name",
            "stream_v3_runtime",
            env_name="SERVICE",
            flag_name="--service",
            required=False,
            no_prompts=True,
        )
        == "stream_v3_runtime"
    )
    assert (
        script._required_prompt_value(
            f"{src}2026-07-01",
            "Environment",
            "stream_v3",
            env_name="ENVIRONMENT",
            flag_name="--environment",
            required=False,
            no_prompts=True,
        )
        == "stream_v3"
    )
    assert (
        script._required_timestamp_value(
            "",
            "Incident window start",
            "2026-06-14T23:15:50Z",
            env_name="START",
            flag_name="--start",
            no_prompts=True,
        )
        == "2026-07-01T00:00:00Z"
    )


def test_timestamp_prompt_skips_late_pasted_source_root_lines(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script = _load_script()
    script._PENDING_PROMPT_LINES.clear()
    root = tmp_path / "stream_v3"
    for relative in ("docs", "ops", "src", "tests"):
        (root / relative).mkdir(parents=True, exist_ok=True)

    class Tty:
        def isatty(self) -> bool:
            return True

    monkeypatch.setattr(script.sys, "stdin", Tty())
    script._PENDING_PROMPT_LINES[:] = [
        str(root / "src"),
        str(root / "ops"),
        "2026-07-01",
    ]

    assert (
        script._required_timestamp_value(
            "",
            "Incident window start",
            "2026-06-14T23:15:50Z",
            env_name="START",
            flag_name="--start",
            no_prompts=False,
        )
        == "2026-07-01T00:00:00Z"
    )


def test_code_profile_summary_reads_human_check_fields(tmp_path: Path) -> None:
    script = _load_script()
    context_bundle = tmp_path / "source_context_bundle.json"
    analysis_bundle = tmp_path / "source_analysis_bundle.json"
    context_bundle.write_text(
        """
        {
          "project_summary": {
            "detected_project_type": "python_project",
            "entrypoint_candidates": ["src/app.py"]
          },
          "source_items": [{"relative_path": "src/app.py"}],
          "config_items": [{"relative_path": "pyproject.toml"}]
        }
        """,
        encoding="utf-8",
    )
    analysis_bundle.write_text(
        """
        {
          "display_summary": {
            "component_candidate_count": 2,
            "metric_semantics_candidate_count": 3,
            "collector_mapping_candidate_count": 4
          }
        }
        """,
        encoding="utf-8",
    )

    assert script._code_profile_summary(context_bundle, analysis_bundle) == {
        "detected_project_type": "python_project",
        "entrypoint_candidates": ["src/app.py"],
        "source_item_count": 1,
        "config_item_count": 1,
        "component_candidate_count": 2,
        "metric_semantics_candidate_count": 3,
        "collector_mapping_candidate_count": 4,
    }


def test_code_profile_confirmation_can_stop_before_log_analysis(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script = _load_script()
    source_root = tmp_path / "stream_v3"
    source_root.mkdir()
    context_bundle = tmp_path / "source_context_bundle.json"
    context_report = tmp_path / "source_context_report.md"
    analysis_bundle = tmp_path / "source_analysis_bundle.json"
    analysis_report = tmp_path / "source_analysis_report.md"
    context_bundle.write_text(
        '{"project_summary": {"detected_project_type": "python_project"}, "source_items": [], "config_items": []}',
        encoding="utf-8",
    )
    context_report.write_text("# Source Context\n", encoding="utf-8")
    analysis_bundle.write_text(
        '{"display_summary": {"component_candidate_count": 0, "metric_semantics_candidate_count": 0, "collector_mapping_candidate_count": 0}}',
        encoding="utf-8",
    )
    analysis_report.write_text("# Source Analysis\n", encoding="utf-8")

    class Tty:
        def isatty(self) -> bool:
            return True

    monkeypatch.setattr(script.sys, "stdin", Tty())
    script._PENDING_PROMPT_LINES[:] = ["no"]

    with pytest.raises(SystemExit) as exc:
        script._confirm_code_profile_before_log_analysis(
            source_root=source_root,
            source_context_bundle=context_bundle,
            source_context_report=context_report,
            source_analysis_bundle=analysis_bundle,
            source_analysis_report=analysis_report,
            approval_record_path=tmp_path / "code_profile_approval.json",
            code_profile_url="https://example.test/code-profiles/profile-1/",
            code_profile_report_url="https://example.test/code-profiles/profile-1/report.md",
            no_prompts=False,
            skip_confirmation=False,
        )

    assert exc.value.code == 0


def test_code_profile_confirmation_accepts_approve_and_records_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script = _load_script()
    source_root = tmp_path / "stream_v3"
    source_root.mkdir()
    context_bundle = tmp_path / "source_context_bundle.json"
    context_report = tmp_path / "source_context_report.md"
    analysis_bundle = tmp_path / "source_analysis_bundle.json"
    analysis_report = tmp_path / "source_analysis_report.md"
    context_bundle.write_text('{"project_summary": {}, "source_items": [], "config_items": []}', encoding="utf-8")
    context_report.write_text("# Source Context\n", encoding="utf-8")
    analysis_bundle.write_text('{"display_summary": {}}', encoding="utf-8")
    analysis_report.write_text("# Source Analysis\n", encoding="utf-8")

    class Tty:
        def isatty(self) -> bool:
            return True

    monkeypatch.setattr(script.sys, "stdin", Tty())
    script._PENDING_PROMPT_LINES[:] = ["APPROVE"]
    approval_record = tmp_path / "code_profile_approval.json"

    script._confirm_code_profile_before_log_analysis(
        source_root=source_root,
        source_context_bundle=context_bundle,
        source_context_report=context_report,
        source_analysis_bundle=analysis_bundle,
        source_analysis_report=analysis_report,
        approval_record_path=approval_record,
        code_profile_url="https://example.test/code-profiles/profile-1/",
        code_profile_report_url="https://example.test/code-profiles/profile-1/report.md",
        no_prompts=False,
        skip_confirmation=False,
    )
    record = json.loads(approval_record.read_text(encoding="utf-8"))
    assert record["approved"] is True
    assert record["approval_gate"] == "source_profile_before_log_analysis"


def test_code_profile_review_artifacts_are_human_readable(tmp_path: Path) -> None:
    script = _load_script()
    source_root = tmp_path / "stream_v3"
    source_root.mkdir()
    context_bundle = tmp_path / "source_context_bundle.json"
    context_report = tmp_path / "source_context_report.md"
    analysis_bundle = tmp_path / "source_analysis_bundle.json"
    analysis_report = tmp_path / "source_analysis_report.md"
    context_bundle.write_text(
        json.dumps(
            {
                "source_context_sha256": "c" * 64,
                "raw_source_policy": "not_uploaded",
                "raw_env_policy": "not_uploaded",
                "project_summary": {
                    "detected_project_type": "systemd_service",
                    "entrypoint_candidates": ["ops/systemd/example.service"],
                },
                "source_items": [{"relative_path": "src/app.py"}],
                "config_items": [{"relative_path": "pyproject.toml"}],
            }
        ),
        encoding="utf-8",
    )
    context_report.write_text("# Sanitized Source Context Bundle\n\n- source_items: 1\n", encoding="utf-8")
    analysis_bundle.write_text(
        json.dumps(
            {
                "analysis_sha256": "a" * 64,
                "display_summary": {
                    "component_candidate_count": 2,
                    "metric_semantics_candidate_count": 3,
                    "collector_mapping_candidate_count": 4,
                },
            }
        ),
        encoding="utf-8",
    )
    analysis_report.write_text("# Sanitized Source Analysis Bundle\n\n- component_candidates: 2\n", encoding="utf-8")
    focused_profile = tmp_path / "focused_operational_profile.json"
    focused_profile.write_text(
        json.dumps(
            {
                "schema_version": "focused_operational_profile.v1",
                "system_label": "stream_v3_runtime",
                "system_summary": {
                    "system_type": "systemd_service",
                    "primary_purpose": "Keeps a live runtime moving.",
                    "logged_subject": "service health, recovery, and publish signals",
                    "operational_boundary": "read-only source profile before log analysis",
                    "confidence": 0.91,
                },
                "runtime_components": [
                    {"name": "stream watchdog", "role": "observes publishing freshness", "confidence": 0.8}
                ],
                "observability_contract": {
                    "logs": [{"source": "watchdog log", "meaning": "publishing freshness"}],
                    "metrics": [
                        {
                            "metric_name": "upload_pressure",
                            "meaning": "transport pressure",
                            "healthy_direction": "decrease",
                        }
                    ],
                },
                "orchestration_flows": [{"flow_name": "watchdog", "trigger": "stale stream", "steps": ["observe"]}],
                "failure_modes": [
                    {
                        "failure_mode": "stale publish path",
                        "observable_signals": ["stale stream"],
                        "missing_evidence": ["runtime log evidence"],
                    }
                ],
                "read_only_collectors": [{"collector": "service status", "purpose": "check runtime state"}],
                "profile_limits": {
                    "source_context_is_incident_evidence": False,
                    "runtime_claims_require_evidence_id": True,
                    "approval_required_before_explicit_profile": True,
                    "raw_source_sent_to_provider": False,
                    "raw_logs_sent_to_provider": False,
                },
                "human_review_required": ["Confirm this source profile matches the deployed service."],
                "focused_profile_generation": {
                    "provider_id": "gemini-enterprise-agent-platform",
                    "model_name": "gemini-3.1-pro-preview",
                    "prompt_name": "focused-operational-profile",
                    "llm_status": "ok",
                    "fallback_used": False,
                },
            }
        ),
        encoding="utf-8",
    )
    profile_id = script._code_profile_public_id(
        run_id="review-test",
        source_context_bundle=context_bundle,
        source_analysis_bundle=analysis_bundle,
    )

    artifacts = script._write_code_profile_review_artifacts(
        output_dir=tmp_path / "code_profile_review",
        run_id="review-test",
        code_profile_id=profile_id,
        code_profile_url=f"https://example.test/code-profiles/{profile_id}/",
        code_profile_report_url=f"https://example.test/code-profiles/{profile_id}/report.md",
        source_root=source_root,
        source_context_bundle=context_bundle,
        source_context_report=context_report,
        source_analysis_bundle=analysis_bundle,
        source_analysis_report=analysis_report,
        focused_profile=focused_profile,
    )

    html = artifacts["html"].read_text(encoding="utf-8")
    markdown = artifacts["markdown"].read_text(encoding="utf-8")
    payload = json.loads(artifacts["payload"].read_text(encoding="utf-8"))
    assert "Code Profile Review" in html
    assert "Human approval checkpoint before log analysis" in html
    assert 'id="code-profile-human-review-form"' in html
    assert "Answer And Approve" in html
    assert "Save Review" in html
    assert "Show JSON" in html
    assert "Review JSON" in html
    assert 'id="review-json-output"' in html
    assert "Copy APPROVE" in html
    assert "Normalize With Gemini" in html
    assert "Gemini candidate patch" in html
    assert "Review Edited Interpretation" in html
    assert 'id="interpreted-profile-preview"' in html
    assert 'id="interpretation-review-confirmed"' in html
    assert "Approve Reviewed Interpretation" in html
    assert "Download Approved Profile JSON" in html
    assert '"normalize_endpoint": "/profile-reviews/normalize"' in html
    assert '"preview_endpoint": "/profile-reviews/preview"' in html
    assert '"approve_endpoint": "/profile-reviews/approve"' in html
    assert "code_profile_human_review_form.v1" in html
    assert "Confirm this source profile matches the deployed service." in html
    assert html.index("Gemini Questions For Human Approval") < html.index('id="code-profile-human-review-form"')
    assert html.index('id="code-profile-human-review-form"') < html.index("Gemini Runtime Components")
    assert html.index('id="code-profile-human-review-form"') > html.index("Gemini Pro Code Profile")
    assert "Gemini Pro Code Profile" in markdown
    assert "Gemini Questions For Human Approval" in markdown
    assert "Confirm this source profile matches the deployed service." in markdown
    assert "raw_source_sent_to_provider: false" in markdown
    assert "raw_logs_sent_to_provider: false" in markdown
    assert "What This Code Appears To Run" in markdown
    assert "What The Logs Should Measure" in markdown
    assert "What Should Not Be Broken" in markdown
    assert "Answer directly under Gemini Questions For Human Approval" in markdown
    assert "There is no input form" not in markdown
    assert "component_candidates: 2" in markdown
    assert str(source_root) not in html
    assert payload["local_absolute_path_uploaded"] is False
    assert payload["code_profile_id"] == profile_id
    assert isinstance(payload["interpretation"], dict)
    assert payload["focused_profile"]["focused_profile_generation"]["llm_status"] == "ok"
    assert payload["focused_profile"]["focused_profile_generation"]["model_name"] == "gemini-3.1-pro-preview"


def test_main_builds_code_profile_before_log_analysis(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    script = _load_script()
    log_input = tmp_path / "logs.jsonl"
    log_input.write_text('{"ts":"2026-07-01T00:00:00Z","message":"ok"}\n', encoding="utf-8")
    source_root = tmp_path / "stream_v3"
    source_root.mkdir()
    output_dir = tmp_path / "analysis"
    labels: list[str] = []
    focused_payload = {
        "schema_version": "focused_operational_profile.v1",
        "system_label": "stream_v3_runtime",
        "system_summary": {},
        "runtime_components": [],
        "observability_contract": {},
        "orchestration_flows": [],
        "failure_modes": [],
        "read_only_collectors": [],
        "profile_limits": {
            "raw_source_sent_to_provider": False,
            "raw_logs_sent_to_provider": False,
        },
        "human_review_required": [],
        "focused_profile_generation": {
            "provider_id": "gemini-enterprise-agent-platform",
            "model_name": "gemini-3.1-pro-preview",
            "llm_status": "ok",
            "fallback_used": False,
        },
    }
    approved_profile_path = tmp_path / "approved-operational-profile.json"
    approved_profile_path.write_text(
        json.dumps(
            build_approved_operational_profile(
                focused_profile=focused_payload,
                human_review={
                    "schema_version": "code_profile_human_review_form.v1",
                    "reviewer": "test-reviewer",
                    "decision": "approved",
                    "profile_matches_deployment": True,
                    "deployment_period_confirmed": True,
                    "log_scope_confirmed": True,
                    "answers": [],
                    "approval_note": "test approval",
                },
                accepted_patch={
                    "schema_version": "operational_profile_review_patch.v1",
                    "system_summary_overrides": {},
                    "metric_semantics_overrides": [],
                    "component_role_overrides": [],
                    "log_source_overrides": [],
                    "confirmed_user_outcomes": [],
                    "ignored_component_ids": [],
                    "approved_collectors": [],
                    "unresolved_questions": [],
                },
            ),
            sort_keys=True,
        ),
        encoding="utf-8",
    )

    streamed_labels: list[str] = []

    def fake_run_step(
        label: str,
        command: list[str],
        *,
        env: dict[str, str] | None = None,
        stream_stderr: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        labels.append(label)
        if stream_stderr:
            streamed_labels.append(label)
        if label == "Sanitizing source code":
            out_dir = Path(command[command.index("--out") + 1])
            out_dir.mkdir(parents=True, exist_ok=True)
            (out_dir / "source_context_bundle.json").write_text(
                json.dumps(
                    {
                        "source_context_sha256": "c" * 64,
                        "project_summary": {"detected_project_type": "python_project"},
                        "source_items": [],
                        "config_items": [],
                    }
                ),
                encoding="utf-8",
            )
            (out_dir / "source_context_report.md").write_text("# Source Context\n", encoding="utf-8")
        if label == "Building source mapping candidates":
            out_dir = Path(command[command.index("--out") + 1])
            out_dir.mkdir(parents=True, exist_ok=True)
            (out_dir / "source_analysis_bundle.json").write_text(
                json.dumps(
                    {
                        "analysis_sha256": "a" * 64,
                        "display_summary": {
                            "component_candidate_count": 0,
                            "metric_semantics_candidate_count": 0,
                            "collector_mapping_candidate_count": 0,
                        },
                    }
                ),
                encoding="utf-8",
            )
            (out_dir / "source_analysis_report.md").write_text("# Source Analysis\n", encoding="utf-8")
        if label == "Building source profile discovery":
            out_dir = Path(command[command.index("--out") + 1])
            out_dir.mkdir(parents=True, exist_ok=True)
            (out_dir / "profile_discovery_bundle.json").write_text(
                json.dumps({"schema_version": "profile_discovery_bundle.v1", "discovery_sha256": "d" * 64}),
                encoding="utf-8",
            )
        if label == "Analyzing source profile with Gemini Pro":
            assert env is not None
            assert env["GOOGLE_CLOUD_PROJECT"] == "ops-evidence-synthesis"
            assert env["OES_VERTEX_PROJECT"] == "ops-evidence-synthesis"
            out_path = Path(command[command.index("--out") + 1])
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(json.dumps(focused_payload), encoding="utf-8")
        if label == "Building human review page":
            assert stream_stderr is True
            assert env is not None
            assert env["OES_PROGRESS_STDERR"] == "1"
            assert env["OES_JOB_APPROVED_PROFILE_URI"].endswith(
                "/job-inputs/review-test/approved_operational_profile.json"
            )
            assert env["OES_JOB_PROFILE_ID"] == "stream_v3_runtime"
            assert "OES_JOB_SOURCE_CONTEXT_URI" not in env
            assert "OES_JOB_SOURCE_ANALYSIS_URI" not in env
            stdout = json.dumps(
                {
                    "evidence_sha256": "a" * 64,
                    "static_review_public_url": "https://example.test/reviews/aaaaaaaa/",
                    "static_review_report_url": "https://example.test/reviews/aaaaaaaa/report.md",
                }
            )
            return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(script, "_require_command", lambda _name: None)
    monkeypatch.setattr(script, "_gcloud_project", lambda: "ops-evidence-synthesis")
    monkeypatch.setattr(script, "_run_step", fake_run_step)
    monkeypatch.setattr(script, "_check_url", lambda _url: None)

    assert (
        script.main(
            [
                "--input",
                str(log_input),
                "--source-root",
                str(source_root),
                "--service",
                "stream_v3_runtime",
                "--environment",
                "stream_v3",
                "--start",
                "2026-07-01",
                "--end",
                "2026-07-02",
                "--output-dir",
                str(output_dir),
                "--run-id",
                "review-test",
                "--approved-profile",
                str(approved_profile_path),
                "--no-prompts",
            ]
        )
        == 0
    )

    assert labels[:5] == [
        "Sanitizing source code",
        "Checking sanitized source code",
        "Building source mapping candidates",
        "Checking source mapping candidates",
        "Building source profile discovery",
    ]
    assert labels[5:8] == [
        "Analyzing source profile with Gemini Pro",
        "Checking source profile discovery",
        "Uploading code profile review page",
    ]
    assert labels.index("Uploading code profile review page") < labels.index("Sanitizing logs")
    assert labels.index("Uploading Evidence Bundle to GCS") < labels.index("Uploading sanitized source context to GCS")
    assert labels.index("Uploading source analysis to GCS") < labels.index(
        "Uploading approved operational profile to GCS"
    )
    assert streamed_labels == ["Building human review page"]
    output = capsys.readouterr().out
    assert "https://ops-evidence.yukimurata0421.dev/code-profiles/" in output
    assert "gs://" not in output


def test_review_summary_prints_http_urls_without_gcs_by_default(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    script = _load_script()

    script._print_review_summary(
        review_url="https://example.test/reviews/abc/",
        report_url="https://example.test/reviews/abc/report.md",
        legacy_review_url="https://example.test/ui/full-review-page?evidence_sha256=abc",
        code_profile_url="",
        code_profile_report_url="",
        output_dir=tmp_path / "analysis",
        sanitized_dir=tmp_path / "analysis" / "sanitized",
        source_context_bundle=None,
        source_analysis_bundle=None,
        input_bundle_uri="gs://private/job-inputs/abc/evidence_bundle.json",
        precomputed_review_uri="gs://private/precomputed/abc.json",
        static_review_html_uri="gs://private/review-pages/abc/index.html",
        static_review_report_uri="gs://private/review-pages/abc/report.md",
        show_gcs_uris=False,
    )

    output = capsys.readouterr().out
    assert "https://example.test/reviews/abc/" in output
    assert "https://example.test/reviews/abc/report.md" in output
    assert "gs://" not in output


def test_review_summary_can_print_gcs_uris_when_requested(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    script = _load_script()

    script._print_review_summary(
        review_url="https://example.test/reviews/abc/",
        report_url="https://example.test/reviews/abc/report.md",
        legacy_review_url="https://example.test/reviews/abc/",
        code_profile_url="https://example.test/code-profiles/profile-1/",
        code_profile_report_url="https://example.test/code-profiles/profile-1/report.md",
        output_dir=tmp_path / "analysis",
        sanitized_dir=tmp_path / "analysis" / "sanitized",
        source_context_bundle=None,
        source_analysis_bundle=None,
        input_bundle_uri="gs://private/job-inputs/abc/evidence_bundle.json",
        precomputed_review_uri="gs://private/precomputed/abc.json",
        static_review_html_uri="gs://private/review-pages/abc/index.html",
        static_review_report_uri="gs://private/review-pages/abc/report.md",
        show_gcs_uris=True,
    )

    output = capsys.readouterr().out
    assert "Code profile URL: https://example.test/code-profiles/profile-1/" in output
    assert "GCS Evidence Bundle: gs://private/job-inputs/abc/evidence_bundle.json" in output


def test_run_step_streams_stderr_and_keeps_stdout_json(capsys: pytest.CaptureFixture[str]) -> None:
    script = _load_script()

    result = script._run_step(
        "Streaming job",
        [
            sys.executable,
            "-c",
            (
                "import json, sys; "
                "print('visible progress', file=sys.stderr, flush=True); "
                "print(json.dumps({'status': 'ok'}))"
            ),
        ],
        stream_stderr=True,
    )

    captured = capsys.readouterr()
    assert "visible progress" in captured.err
    assert json.loads(result.stdout) == {"status": "ok"}


def test_write_token_is_copied_without_being_printed(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    script = _load_script()
    clipboard_inputs: list[str] = []

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if command[:3] == ["gcloud", "secrets", "versions"]:
            return subprocess.CompletedProcess(command, 0, stdout="clipboard-value\n", stderr="")
        clipboard_inputs.append(str(kwargs.get("input") or ""))
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(script.subprocess, "run", fake_run)
    monkeypatch.setattr(script.shutil, "which", lambda name: "/usr/bin/wl-copy" if name == "wl-copy" else None)

    assert script._copy_write_token_to_clipboard(
        project_id="example-project",
        secret_name="write-token-secret",
    ) is True

    captured = capsys.readouterr()
    assert clipboard_inputs == ["clipboard-value"]
    assert "Write token copied to clipboard" in captured.err
    assert "clipboard-value" not in captured.out + captured.err
