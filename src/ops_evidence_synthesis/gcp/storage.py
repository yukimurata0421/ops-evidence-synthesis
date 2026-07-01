from __future__ import annotations

import importlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ops_evidence_synthesis.canonical import canonical_json


@dataclass(frozen=True, slots=True)
class GcsUri:
    bucket: str
    blob: str

    @classmethod
    def parse(cls, uri: str) -> "GcsUri":
        text = str(uri or "").strip()
        if not text.startswith("gs://"):
            raise ValueError(f"expected gs:// URI, got: {text}")
        remainder = text[5:]
        bucket, _, blob = remainder.partition("/")
        if not bucket or not blob:
            raise ValueError(f"expected gs://bucket/object URI, got: {text}")
        return cls(bucket=bucket, blob=blob)

    def child(self, name: str) -> "GcsUri":
        suffix = str(name or "").strip().lstrip("/")
        if not suffix:
            raise ValueError("child object name is required")
        return GcsUri(bucket=self.bucket, blob=f"{self.blob.rstrip('/')}/{suffix}")

    def __str__(self) -> str:
        return f"gs://{self.bucket}/{self.blob}"


def read_json(uri: str | GcsUri) -> dict[str, Any]:
    text = read_text(uri)
    payload = json.loads(text)
    if not isinstance(payload, dict):
        raise ValueError(f"GCS JSON object must be a dictionary: {uri}")
    return payload


def write_json(uri: str | GcsUri, payload: dict[str, Any]) -> str:
    text = canonical_json(payload) + "\n"
    write_text(uri, text, content_type="application/json")
    return text


def read_text(uri: str | GcsUri) -> str:
    parsed = uri if isinstance(uri, GcsUri) else GcsUri.parse(str(uri))
    blob = _client().bucket(parsed.bucket).blob(parsed.blob)
    return blob.download_as_text(encoding="utf-8")


def write_text(uri: str | GcsUri, text: str, *, content_type: str = "text/plain") -> None:
    parsed = uri if isinstance(uri, GcsUri) else GcsUri.parse(str(uri))
    blob = _client().bucket(parsed.bucket).blob(parsed.blob)
    blob.upload_from_string(str(text), content_type=content_type)


def upload_file(local_path: str | Path, uri: str | GcsUri, *, content_type: str | None = None) -> None:
    parsed = uri if isinstance(uri, GcsUri) else GcsUri.parse(str(uri))
    blob = _client().bucket(parsed.bucket).blob(parsed.blob)
    blob.upload_from_filename(str(local_path), content_type=content_type)


def download_file(uri: str | GcsUri, local_path: str | Path) -> None:
    parsed = uri if isinstance(uri, GcsUri) else GcsUri.parse(str(uri))
    Path(local_path).parent.mkdir(parents=True, exist_ok=True)
    blob = _client().bucket(parsed.bucket).blob(parsed.blob)
    blob.download_to_filename(str(local_path))


def _client() -> Any:
    try:
        storage = importlib.import_module("google.cloud.storage")
    except ImportError as exc:  # pragma: no cover - optional dependency
        raise RuntimeError('GCS artifact IO requires: pip install -e ".[gcp]"') from exc
    return storage.Client()
