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
