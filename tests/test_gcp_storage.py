from __future__ import annotations

import pytest

from ops_evidence_synthesis.gcp.storage import GcsUri


def test_gcs_uri_parse_and_child() -> None:
    uri = GcsUri.parse("gs://private-bucket/runs/input/bundle.json")

    assert uri.bucket == "private-bucket"
    assert uri.blob == "runs/input/bundle.json"
    assert str(uri.child("outputs/multi_ai_run.json")) == (
        "gs://private-bucket/runs/input/bundle.json/outputs/multi_ai_run.json"
    )


def test_gcs_uri_rejects_non_gcs_uri() -> None:
    with pytest.raises(ValueError, match="gs://"):
        GcsUri.parse("/tmp/bundle.json")
