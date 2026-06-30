from __future__ import annotations

from dataclasses import dataclass

from ops_evidence_synthesis.timeutils import parse_timestamp


DEFAULT_MIN_ANALYSIS_WINDOW_HOURS = 24


@dataclass(frozen=True, slots=True)
class AnalysisWindow:
    start: str
    end: str
    duration_seconds: int

    @property
    def duration_hours(self) -> float:
        return round(self.duration_seconds / 3600, 6)


def analysis_window(start: str, end: str) -> AnalysisWindow:
    start_dt = parse_timestamp(start)
    end_dt = parse_timestamp(end)
    duration_seconds = int((end_dt - start_dt).total_seconds())
    return AnalysisWindow(start=start, end=end, duration_seconds=duration_seconds)


def validate_minimum_analysis_window(
    start: str,
    end: str,
    *,
    min_hours: int = DEFAULT_MIN_ANALYSIS_WINDOW_HOURS,
    context: str = "analysis window",
) -> AnalysisWindow:
    window = analysis_window(start, end)
    min_seconds = int(min_hours * 3600)
    if window.duration_seconds < min_seconds:
        raise ValueError(
            f"{context} must cover at least {min_hours} hours: "
            f"{start} -> {end} covers {window.duration_hours:.2f} hours"
        )
    return window
