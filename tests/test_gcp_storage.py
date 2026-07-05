from __future__ import annotations

import subprocess

import pytest

from ops_evidence_synthesis.gcp import storage
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


def test_gcs_read_text_can_use_gcloud_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    def fake_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        return subprocess.CompletedProcess(args=args, returncode=0, stdout='{"ok": true}\n', stderr="")

    monkeypatch.setenv("OES_GCS_IO_BACKEND", "gcloud")
    monkeypatch.setattr(storage.subprocess, "run", fake_run)

    assert storage.read_text("gs://private-bucket/reviews/payload.json") == '{"ok": true}\n'
    assert calls == [["gcloud", "storage", "cat", "gs://private-bucket/reviews/payload.json"]]


def test_gcs_write_text_can_use_gcloud_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    def fake_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")

    monkeypatch.setenv("OES_GCS_IO_BACKEND", "gcloud")
    monkeypatch.setattr(storage.subprocess, "run", fake_run)

    storage.write_text("gs://private-bucket/reviews/payload.json", "payload", content_type="application/json")

    assert len(calls) == 1
    assert calls[0][:4] == ["gcloud", "storage", "cp", "--content-type=application/json"]
    assert calls[0][-1] == "gs://private-bucket/reviews/payload.json"
