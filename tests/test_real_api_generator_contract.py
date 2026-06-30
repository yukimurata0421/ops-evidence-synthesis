from __future__ import annotations

import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _generator_module():
    path = ROOT / "scripts" / "generate_precomputed_review_from_multi_run.py"
    spec = importlib.util.spec_from_file_location("generate_precomputed_review_from_multi_run", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_projection_coverage_interpretation_explains_long_tail_low_coverage() -> None:
    module = _generator_module()

    text = module._projection_coverage_interpretation(
        service="stream_v3_monitoring",
        log_count=4747,
        full_items=1520,
        model_items=140,
        model_occurrences=496,
        coverage=0.104487,
    )

    assert "occurrence-weighted, not raw-row coverage" in text
    assert "4,747 rows and 1,520 grouped Evidence Items" in text
    assert "140 high-signal Evidence Items" in text
    assert "496 repeated occurrences" in text
    assert "long tail" in text
    assert "not all copied into the bounded provider prompt" in text


def test_projection_coverage_interpretation_for_dense_corpus_avoids_low_coverage_claim() -> None:
    module = _generator_module()

    text = module._projection_coverage_interpretation(
        service="stream_v3_runtime",
        log_count=11399,
        full_items=654,
        model_items=140,
        model_occurrences=10771,
        coverage=0.944907,
    )

    assert "occurrence-weighted, not raw-row coverage" in text
    assert "Remaining Evidence Items stay SHA-fixed" in text
    assert "long tail" not in text
