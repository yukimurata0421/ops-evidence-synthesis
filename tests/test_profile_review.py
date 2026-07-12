from __future__ import annotations

import copy
import json

import pytest

from ops_evidence_synthesis.ai.base import ModelResponse
from ops_evidence_synthesis.canonical import sha256_json
from ops_evidence_synthesis.profile_review import (
    ProfileReviewError,
    build_approved_operational_profile,
    build_profile_review_interpretation_preview,
    normalize_profile_review_with_provider,
    validate_approved_operational_profile,
    validate_profile_review_patch,
)


def focused_profile() -> dict:
    return {
        "schema_version": "focused_operational_profile.v1",
        "system_label": "stream-runtime",
        "source_discovery_sha256": "d" * 64,
        "source_context_sha256": "c" * 64,
        "source_analysis_sha256": "a" * 64,
        "system_summary": {
            "system_type": "streaming_service",
            "primary_purpose": "Keep a public live stream available.",
            "logged_subject": "stream transport and publication health",
            "operational_boundary": "read-only diagnosis",
            "confidence": 0.8,
        },
        "runtime_components": [
            {
                "component_id": "publisher",
                "name": "publisher",
                "role": "Publishes the stream.",
                "confidence": 0.8,
            }
        ],
        "observability_contract": {
            "logs": [{"source": "publisher_log", "meaning": "Publisher process events."}],
            "metrics": [
                {
                    "metric_name": "publish_gap_seconds",
                    "meaning": "Time since last successful publish.",
                    "healthy_direction": "decrease",
                }
            ],
            "heartbeats": [],
        },
        "orchestration_flows": [],
        "failure_modes": [],
        "read_only_collectors": [
            {
                "collector": "service_status",
                "purpose": "Read service state.",
                "safety_level": "read_only",
            }
        ],
        "profile_limits": {
            "source_context_is_incident_evidence": False,
            "runtime_claims_require_evidence_id": True,
            "approval_required_before_explicit_profile": True,
            "raw_source_sent_to_provider": False,
            "raw_logs_sent_to_provider": False,
        },
        "human_review_required": ["Is zero publish gap healthy?"],
    }


def human_review(*, approved: bool = False) -> dict:
    return {
        "schema_version": "code_profile_human_review_form.v1",
        "reviewer": "operator-1",
        "decision": "approved" if approved else "",
        "profile_matches_deployment": approved,
        "deployment_period_confirmed": approved,
        "log_scope_confirmed": approved,
        "answers": [
            {
                "question": "Is zero publish gap healthy?",
                "answer": "Yes. Zero is healthy; increasing values are suspicious.",
            }
        ],
        "approval_note": "Confirmed against the deployed runtime.",
    }


def candidate_patch() -> dict:
    return {
        "schema_version": "operational_profile_review_patch.v1",
        "system_summary_overrides": {
            "primary_purpose": "Keep the public live stream available to viewers."
        },
        "metric_semantics_overrides": [
            {
                "metric_name": "publish_gap_seconds",
                "meaning": "Seconds since the last successful publication.",
                "healthy_direction": "decrease",
                "zero_behavior": "healthy",
                "increase_behavior": "suspicious",
                "decrease_behavior": "healthy",
                "reason": "Human confirmed zero is the desired state.",
                "provenance": "human_answer",
            }
        ],
        "component_role_overrides": [],
        "log_source_overrides": [],
        "confirmed_user_outcomes": ["Viewers can continuously watch the public stream."],
        "ignored_component_ids": [],
        "approved_collectors": ["service_status"],
        "unresolved_questions": [],
    }


class FakeGeminiProfileReviewProvider:
    provider = "gemini-enterprise-agent-platform"
    model_name = "gemini-3.1-pro-preview"
    prompt_name = "profile-review-normalization"
    temperature = 0.0

    def run(self, bundle: dict) -> ModelResponse:
        assert bundle["llm_task"] == "profile_review_normalization"
        assert bundle["normalization_policy"]["candidate_patch_only"] is True
        assert bundle["normalization_policy"]["human_final_approval_required"] is True
        assert "raw source" not in json.dumps(bundle).casefold()
        return ModelResponse(
            provider=self.provider,
            model_name=self.model_name,
            prompt_name=self.prompt_name,
            temperature=self.temperature,
            raw_output=json.dumps(candidate_patch()),
            latency_ms=1,
            input_tokens=100,
            output_tokens=100,
        )


class StaticGeminiProfileReviewProvider(FakeGeminiProfileReviewProvider):
    def __init__(self, raw_output: str) -> None:
        self.raw_output = raw_output

    def run(self, bundle: dict) -> ModelResponse:
        assert bundle["human_review"]["answers"][0]["answer"].startswith("Yes. Zero is healthy")
        return ModelResponse(
            provider=self.provider,
            model_name=self.model_name,
            prompt_name=self.prompt_name,
            temperature=self.temperature,
            raw_output=self.raw_output,
            latency_ms=1,
            input_tokens=100,
            output_tokens=100,
        )


def test_gemini_normalizes_human_answers_into_valid_candidate_patch() -> None:
    result = normalize_profile_review_with_provider(
        focused_profile(),
        human_review(),
        FakeGeminiProfileReviewProvider(),
    )

    assert result["status"] == "candidate_patch_ready"
    assert result["validation"] == {"passed": True, "errors": []}
    assert result["patch"]["metric_semantics_overrides"][0]["zero_behavior"] == "healthy"
    assert result["change_summary"]["metric_semantics"] == 1
    assert result["normalization"]["model_name"] == "gemini-3.1-pro-preview"
    assert len(result["normalization"]["model_input_sha256"]) == 64


def test_candidate_patch_cannot_introduce_unknown_metric() -> None:
    patch = candidate_patch()
    patch["metric_semantics_overrides"][0]["metric_name"] = "invented_metric"

    assert validate_profile_review_patch(patch, focused_profile()) == [
        "unknown metric_name: invented_metric"
    ]


def test_candidate_patch_rejects_unknown_references_and_unsupported_semantics() -> None:
    patch = candidate_patch()
    metric = patch["metric_semantics_overrides"][0]
    metric["healthy_direction"] = "up_and_to_the_right"
    metric["zero_behavior"] = "perfect"
    patch["component_role_overrides"] = [
        {
            "component_id": "invented-component",
            "role": "Unknown component",
            "reason": "Not present in the source profile.",
            "provenance": "human_answer",
        }
    ]
    patch["log_source_overrides"] = [
        {
            "source": "invented-log",
            "meaning": "Unknown log",
            "reason": "Not present in the source profile.",
            "provenance": "human_answer",
        }
    ]
    patch["ignored_component_ids"] = ["invented-component"]
    patch["approved_collectors"] = ["write-capable-collector"]

    assert validate_profile_review_patch(patch, focused_profile()) == [
        "unknown collector: write-capable-collector",
        "unknown component_id: invented-component",
        "unknown ignored component_id: invented-component",
        "unknown log source: invented-log",
        "unsupported healthy_direction for publish_gap_seconds: up_and_to_the_right",
        "unsupported zero_behavior for publish_gap_seconds: perfect",
    ]


@pytest.mark.parametrize(
    ("raw_output", "expected_error"),
    [
        ("not-json", "returned invalid JSON"),
        (
            json.dumps({**candidate_patch(), "unsupported_top_level": True}),
            "contains unsupported fields",
        ),
        (
            json.dumps(
                {
                    **candidate_patch(),
                    "metric_semantics_overrides": [
                        {
                            **candidate_patch()["metric_semantics_overrides"][0],
                            "metric_name": "provider-invented-metric",
                        }
                    ],
                }
            ),
            "unknown metric_name: provider-invented-metric",
        ),
        (
            json.dumps(
                {
                    **candidate_patch(),
                    "metric_semantics_overrides": [
                        {
                            **candidate_patch()["metric_semantics_overrides"][0],
                            "reason": "Use token sk-test-fakekey1234567890",
                        }
                    ],
                }
            ),
            "failed safety validation",
        ),
    ],
)
def test_provider_normalization_rejects_invalid_or_unsafe_output(
    raw_output: str,
    expected_error: str,
) -> None:
    with pytest.raises(ProfileReviewError, match=expected_error):
        normalize_profile_review_with_provider(
            focused_profile(),
            human_review(),
            StaticGeminiProfileReviewProvider(raw_output),
        )


def test_human_approved_profile_is_hash_bound_and_applies_semantics() -> None:
    profile = build_approved_operational_profile(
        focused_profile=focused_profile(),
        human_review=human_review(approved=True),
        accepted_patch=candidate_patch(),
        normalization={
            "provider_id": "gemini-enterprise-agent-platform",
            "model_name": "gemini-3.1-pro-preview",
            "model_input_sha256": "i" * 64,
            "parsed_output_sha256": "o" * 64,
        },
    )

    assert validate_approved_operational_profile(profile, focused_profile=focused_profile()) == []
    assert profile["metric_semantics"]["publish_gap_seconds"]["zero_behavior"] == "healthy"
    assert profile["review_policy"]["source_access_after_approval"] == "disabled"
    assert profile["human_review"]["reviewer"] == "operator-1"
    assert (
        profile["metric_semantics"]["publish_gap_seconds"]["review_provenance"]
        == "human_approved:gemini-enterprise-agent-platform"
    )
    assert len(profile["approved_profile_sha256"]) == 64


def test_interpreted_profile_returns_to_human_review_before_approval() -> None:
    preview = build_profile_review_interpretation_preview(
        focused_profile=focused_profile(),
        human_review=human_review(approved=True),
        accepted_patch=candidate_patch(),
        normalization={
            "provider_id": "gemini-enterprise-agent-platform",
            "model_name": "gemini-3.1-pro-preview",
        },
    )

    interpreted = preview["interpreted_profile"]
    assert preview["status"] == "ready_for_human_re_review"
    assert preview["answer_count"] == 1
    assert preview["unresolved_question_count"] == 0
    assert len(preview["reviewed_patch_sha256"]) == 64
    assert interpreted["status"] == "candidate_interpretation"
    assert interpreted["explicit_profile"] is False
    assert interpreted["human_review"]["decision"] == "pending_interpretation_review"
    assert interpreted["review_policy"]["source_access_after_approval"] == "pending_interpretation_review"
    assert interpreted["metric_semantics"]["publish_gap_seconds"]["zero_behavior"] == "healthy"


def test_approved_profile_tampering_is_detected() -> None:
    profile = build_approved_operational_profile(
        focused_profile=focused_profile(),
        human_review=human_review(approved=True),
        accepted_patch=candidate_patch(),
    )
    assert (
        profile["metric_semantics"]["publish_gap_seconds"]["review_provenance"]
        == "human_approved:deterministic"
    )
    assert profile["approval_provenance"]["normalization_provider_id"] == "deterministic"
    tampered = copy.deepcopy(profile)
    tampered["system_profile"]["purpose"] = "A different purpose."

    assert "approved_profile_sha256 mismatch" in validate_approved_operational_profile(tampered)


@pytest.mark.parametrize(
    ("section", "key", "value", "expected"),
    [
        ("human_review", "decision", "rejected", "human_review.decision must be approved"),
        ("human_review", "reviewer", " ", "human_review.reviewer is required"),
        (
            "review_policy",
            "source_access_after_approval",
            "enabled",
            "review_policy.source_access_after_approval must be disabled",
        ),
        (
            "review_policy",
            "context_is_not_evidence",
            False,
            "review_policy.context_is_not_evidence must be true",
        ),
        (
            "review_policy",
            "require_evidence_id_for_support",
            False,
            "review_policy.require_evidence_id_for_support must be true",
        ),
    ],
)
def test_approved_profile_validator_requires_human_approval_contract(
    section: str,
    key: str,
    value: object,
    expected: str,
) -> None:
    profile = build_approved_operational_profile(
        focused_profile=focused_profile(),
        human_review=human_review(approved=True),
        accepted_patch=candidate_patch(),
    )
    profile[section][key] = value
    profile.pop("approved_profile_sha256")
    profile["approved_profile_sha256"] = sha256_json(profile)

    assert expected in validate_approved_operational_profile(profile)


def test_normalization_rejects_secret_like_human_answer() -> None:
    review = human_review()
    review["answers"][0]["answer"] = "Use token sk-test-fakekey1234567890"

    with pytest.raises(ProfileReviewError, match="safety preflight"):
        normalize_profile_review_with_provider(
            focused_profile(),
            review,
            FakeGeminiProfileReviewProvider(),
        )


@pytest.mark.parametrize(
    ("endpoint", "payload", "expected_detail"),
    [
        ("/profile-reviews/normalize", {}, "focused_profile object is required"),
        (
            "/profile-reviews/normalize",
            {"focused_profile": focused_profile()},
            "human_review object is required",
        ),
        (
            "/profile-reviews/preview",
            {"focused_profile": focused_profile(), "human_review": human_review()},
            "accepted_patch object is required",
        ),
        (
            "/profile-reviews/preview",
            {
                "focused_profile": focused_profile(),
                "human_review": human_review(),
                "accepted_patch": candidate_patch(),
                "normalization": [],
            },
            "normalization must be an object or null",
        ),
        (
            "/profile-reviews/approve",
            {"focused_profile": focused_profile(), "human_review": human_review(approved=True)},
            "accepted_patch object is required",
        ),
        (
            "/profile-reviews/approve",
            {
                "focused_profile": focused_profile(),
                "human_review": human_review(approved=True),
                "accepted_patch": candidate_patch(),
                "normalization": [],
            },
            "normalization must be an object or null",
        ),
    ],
)
def test_profile_review_api_rejects_malformed_workflow_payloads(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    endpoint: str,
    payload: dict,
    expected_detail: str,
) -> None:
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient

    from ops_evidence_synthesis.api import app

    monkeypatch.setenv("OES_STORE", "sqlite")
    monkeypatch.setenv("OES_DB_PATH", str(tmp_path / "profile-review-invalid-api.sqlite3"))
    monkeypatch.setenv("OES_API_WRITE_TOKEN", "profile-review-token")

    with TestClient(app) as client:
        response = client.post(
            endpoint,
            headers={"x-oes-write-token": "profile-review-token"},
            json=payload,
        )

    assert response.status_code == 400
    assert response.json()["detail"] == expected_detail


def test_profile_review_api_requires_token_then_returns_approved_json(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient

    from ops_evidence_synthesis.api import app
    from ops_evidence_synthesis.routes import api_routes

    monkeypatch.setenv("OES_STORE", "sqlite")
    monkeypatch.setenv("OES_DB_PATH", str(tmp_path / "profile-review-api.sqlite3"))
    monkeypatch.setenv("OES_API_WRITE_TOKEN", "profile-review-token")
    monkeypatch.setattr(
        api_routes,
        "_PROFILE_REVIEW_PROVIDER_FACTORY",
        lambda: FakeGeminiProfileReviewProvider(),
    )

    with TestClient(app) as client:
        blocked = client.post(
            "/profile-reviews/normalize",
            json={"focused_profile": focused_profile(), "human_review": human_review()},
        )
        normalized = client.post(
            "/profile-reviews/normalize",
            headers={"x-oes-write-token": "profile-review-token"},
            json={"focused_profile": focused_profile(), "human_review": human_review()},
        )
        premature_approval = client.post(
            "/profile-reviews/approve",
            headers={"x-oes-write-token": "profile-review-token"},
            json={
                "focused_profile": focused_profile(),
                "human_review": human_review(approved=True),
                "accepted_patch": normalized.json()["patch"],
                "normalization": normalized.json()["normalization"],
                "profile_id": "stream-runtime",
            },
        )
        preview = client.post(
            "/profile-reviews/preview",
            headers={"x-oes-write-token": "profile-review-token"},
            json={
                "focused_profile": focused_profile(),
                "human_review": human_review(approved=True),
                "accepted_patch": normalized.json()["patch"],
                "normalization": normalized.json()["normalization"],
                "profile_id": "stream-runtime",
            },
        )
        changed_patch = copy.deepcopy(normalized.json()["patch"])
        changed_patch["system_summary_overrides"]["primary_purpose"] = "Changed after review."
        changed_after_review = client.post(
            "/profile-reviews/approve",
            headers={"x-oes-write-token": "profile-review-token"},
            json={
                "focused_profile": focused_profile(),
                "human_review": human_review(approved=True),
                "accepted_patch": changed_patch,
                "normalization": normalized.json()["normalization"],
                "reviewed_patch_sha256": preview.json()["reviewed_patch_sha256"],
                "interpretation_review_confirmed": True,
                "profile_id": "stream-runtime",
            },
        )
        approved = client.post(
            "/profile-reviews/approve",
            headers={"x-oes-write-token": "profile-review-token"},
            json={
                "focused_profile": focused_profile(),
                "human_review": human_review(approved=True),
                "accepted_patch": normalized.json()["patch"],
                "normalization": normalized.json()["normalization"],
                "reviewed_patch_sha256": preview.json()["reviewed_patch_sha256"],
                "interpretation_review_confirmed": True,
                "profile_id": "stream-runtime",
            },
        )

    assert blocked.status_code == 403
    assert normalized.status_code == 200, normalized.text
    assert premature_approval.status_code == 400
    assert preview.status_code == 200, preview.text
    assert preview.json()["status"] == "ready_for_human_re_review"
    assert changed_after_review.status_code == 400
    assert changed_after_review.json()["detail"] == "edited patch changed after interpretation review"
    assert approved.status_code == 200, approved.text
    assert approved.json()["approved_profile"]["status"] == "approved"
    assert approved.json()["approved_profile"]["human_review"]["interpretation_review_confirmed"] is True
    assert (
        approved.json()["approved_profile"]["human_review"]["reviewed_patch_sha256"]
        == preview.json()["reviewed_patch_sha256"]
    )
    assert len(approved.json()["approved_profile_sha256"]) == 64
