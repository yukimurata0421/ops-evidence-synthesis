from __future__ import annotations

from typing import Any

from ops_evidence_synthesis.gcp.bigquery import BigQueryOps


class _FakeBigQuery:
    class ScalarQueryParameter:
        def __init__(self, name: str, type_: str, value: Any) -> None:
            self.name = name
            self.type_ = type_
            self.value = value

    class QueryJobConfig:
        def __init__(self, query_parameters: list[Any]) -> None:
            self.query_parameters = query_parameters


def test_fetch_model_runs_bigquery_generation_gate_has_from_clause(monkeypatch) -> None:
    captured: dict[str, str] = {}
    store = BigQueryOps.__new__(BigQueryOps)
    store.bigquery = _FakeBigQuery
    store.project_id = "demo-project"
    store.location = "asia-northeast1"

    def fake_query(sql: str, params: list[Any] | None = None) -> list[Any]:
        captured["sql"] = sql
        return []

    monkeypatch.setattr(store, "_query", fake_query)

    assert store.fetch_model_runs("sha") == []
    assert "FROM (SELECT 1)" in captured["sql"]
    assert "SELECT TIMESTAMP('1970-01-01') AS created_at\n          WHERE" not in captured["sql"]
