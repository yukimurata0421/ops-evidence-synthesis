from __future__ import annotations

import pytest

from ops_evidence_synthesis.window_policy import validate_minimum_analysis_window


def test_validate_minimum_analysis_window_accepts_24_hours() -> None:
    window = validate_minimum_analysis_window(
        "2026-06-25T00:00:00Z",
        "2026-06-26T00:00:00Z",
    )

    assert window.duration_hours == 24


def test_validate_minimum_analysis_window_rejects_short_window() -> None:
    with pytest.raises(ValueError, match="at least 24 hours"):
        validate_minimum_analysis_window(
            "2026-06-26T03:31:23Z",
            "2026-06-26T17:38:45Z",
            context="public real-provider payload",
        )
