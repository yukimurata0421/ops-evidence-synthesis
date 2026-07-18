from __future__ import annotations

import tomllib
from pathlib import Path

from ops_evidence_synthesis import __version__
from ops_evidence_synthesis.api import app


def test_package_and_api_versions_match_project_metadata() -> None:
    project_root = Path(__file__).resolve().parents[1]
    metadata = tomllib.loads((project_root / "pyproject.toml").read_text(encoding="utf-8"))

    assert metadata["project"]["version"] == __version__
    assert app.version == __version__
