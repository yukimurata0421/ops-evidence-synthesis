from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts import generate_precomputed_review


def _valid_payload() -> dict:
    return {
        "evidence_sha256": "e" * 64,
        "summary": {"log_count": 2000},
        "targets": [
            {
                "review_target_id": "rt-converged",
                "agreement": {
                    "verdict": "convergence",
                    "convergence_score": 0.83,
                },
            },
            {
                "review_target_id": "rt-disagreement",
                "agreement": {
                    "verdict": "disagreement",
                    "convergence_score": 0.0,
                },
            },
        ],
    }


def test_validate_precomputed_payload_accepts_expected_fixture_contract() -> None:
    generate_precomputed_review._validate_payload(
        _valid_payload(),
        expected_evidence_sha="e" * 64,
        expected_log_count=2000,
        require_convergence=True,
        expected_convergence_score=0.83,
    )


def test_validate_precomputed_payload_rejects_fixture_identity_drift() -> None:
    with pytest.raises(SystemExit) as excinfo:
        generate_precomputed_review._validate_payload(
            _valid_payload(),
            expected_evidence_sha="f" * 64,
            expected_log_count=2000,
            require_convergence=False,
            expected_convergence_score=0.0,
        )

    assert "expected evidence_sha256=" in str(excinfo.value)


def test_validate_precomputed_payload_rejects_log_count_drift() -> None:
    with pytest.raises(SystemExit) as excinfo:
        generate_precomputed_review._validate_payload(
            _valid_payload(),
            expected_evidence_sha="",
            expected_log_count=1999,
            require_convergence=False,
            expected_convergence_score=0.0,
        )

    assert "expected log_count=1999" in str(excinfo.value)


def test_validate_precomputed_payload_rejects_missing_or_wrong_convergence() -> None:
    payload = _valid_payload()
    payload["targets"] = [
        {
            "review_target_id": "rt-disagreement",
            "agreement": {"verdict": "disagreement", "convergence_score": 0.0},
        }
    ]

    with pytest.raises(SystemExit) as missing:
        generate_precomputed_review._validate_payload(
            payload,
            expected_evidence_sha="",
            expected_log_count=0,
            require_convergence=True,
            expected_convergence_score=0.0,
        )
    assert "expected at least one converged review target" in str(missing.value)

    with pytest.raises(SystemExit) as wrong_score:
        generate_precomputed_review._validate_payload(
            _valid_payload(),
            expected_evidence_sha="",
            expected_log_count=0,
            require_convergence=True,
            expected_convergence_score=0.91,
        )
    assert "expected convergence_score=0.91" in str(wrong_score.value)


def test_generate_precomputed_review_loaders_require_mapping_inputs(tmp_path: Path) -> None:
    json_path = tmp_path / "not-object.json"
    profile_path = tmp_path / "not-profile.yaml"
    valid_profile_path = tmp_path / "profile.yaml"
    json_path.write_text(json.dumps(["not", "a", "mapping"]), encoding="utf-8")
    profile_path.write_text("- not\n- a\n- mapping\n", encoding="utf-8")
    valid_profile_path.write_text("profile_id: approved-demo\nexplicit_profile: true\n", encoding="utf-8")

    with pytest.raises(SystemExit) as json_error:
        generate_precomputed_review._load_json(str(json_path))
    with pytest.raises(SystemExit) as profile_error:
        generate_precomputed_review._load_profile(str(profile_path))

    assert "expected JSON object" in str(json_error.value)
    assert "expected profile mapping" in str(profile_error.value)
    assert generate_precomputed_review._load_profile(str(valid_profile_path)) == {
        "profile_id": "approved-demo",
        "explicit_profile": True,
    }
