from __future__ import annotations

import os

import pytest

from ops_evidence_synthesis.web import precomputed_review as web_precomputed
from ops_evidence_synthesis.web.precomputed_review import _precomputed_review_payload


@pytest.mark.skipif(
    os.environ.get("OES_RUN_GCS_INTEGRATION") != "1",
    reason="manual GCS integration; set OES_RUN_GCS_INTEGRATION=1 and OES_PRECOMPUTED_REVIEW_GCS_PREFIX",
)
def test_manual_gcs_precomputed_review_lookup_reads_private_artifact_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    prefix = os.environ.get("OES_PRECOMPUTED_REVIEW_GCS_PREFIX", "").strip()
    assert prefix.startswith("gs://"), "OES_PRECOMPUTED_REVIEW_GCS_PREFIX must be a gs:// URI"
    evidence_sha = os.environ.get(
        "OES_TEST_GCS_EVIDENCE_SHA",
        "ab18d62c4e628e190345fa218834ca74276f556191d2f068a969f7922945a471",
    )
    monkeypatch.setenv("OES_PRECOMPUTED_REVIEW_DIR", "/tmp/oes-empty-precomputed")
    monkeypatch.setenv("OES_PRECOMPUTED_REVIEW_CACHE_SECONDS", "0")
    web_precomputed._PRECOMPUTED_REVIEW_CACHE.clear()

    payload = _precomputed_review_payload(evidence_sha)

    assert payload is not None
    assert payload["evidence_sha256"] == evidence_sha
    assert payload["summary"]["providers"]["success"] >= 1
    assert payload["summary"]["review"]["validation_targets"] >= 1
