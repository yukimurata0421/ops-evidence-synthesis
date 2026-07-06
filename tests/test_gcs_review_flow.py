from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest


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
