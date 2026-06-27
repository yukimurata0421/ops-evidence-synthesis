from __future__ import annotations

from pathlib import Path


def test_workflow_has_provider_timeout_and_skip_controls() -> None:
    workflow = Path("gcp/workflows/ops-evidence-synthesis.yaml").read_text(encoding="utf-8")

    assert "skip_alternatives" in workflow
    assert "skip_compare" in workflow
    assert "gemini_timeout_seconds" in workflow
    assert "alternatives_timeout_seconds" in workflow
    assert "compare_timeout_seconds" in workflow
    assert "provider_status" in workflow
    assert "workflow/provider-policy" in workflow
    assert "provider_policy" in workflow
    assert "provider_max_retries" in workflow
    assert "http.default_retry_predicate" in workflow
    assert "cost_policy" in workflow
    assert "set_alternatives_error" in workflow
    assert "set_compare_skipped" in workflow
    assert "primary_review_target_count" in workflow
    assert "validation_target_count" in workflow
