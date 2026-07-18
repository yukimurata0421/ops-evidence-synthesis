from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from fastapi.testclient import TestClient

from ops_evidence_synthesis.ai.base import ModelResponse
from ops_evidence_synthesis.ai.execution_contract import (
    build_provider_execution_contract,
    provider_execution_contract_sha256,
)
from ops_evidence_synthesis.ai.runtime import SafetyPreflightResult
from ops_evidence_synthesis.local_first import build_bundle_from_sanitized, sanitize_input
from ops_evidence_synthesis.profile_discovery import approve_profile_draft, discover_profile, draft_profile
from ops_evidence_synthesis.synthesis.multi_ai import (
    PROVIDER_CHUNK_LEDGER_FILENAME,
    SCORE_NOTE,
    _ArtifactEnvelope,
    _ProviderChunkLedger,
    _adaptive_subchunk_retry_enabled,
    _artifact_execution_status,
    _bundle_for_evidence_chunk,
    _chunk_input_tokens_per_minute,
    _chunk_min_start_interval_seconds,
    _chunk_start_interval_seconds,
    _chunk_target_tokens,
    _chunk_worker_count,
    _claim_groups,
    _evidence_chunk_size,
    _evidence_item_chunks,
    _estimated_evidence_item_tokens,
    _merge_chunk_claim_payloads,
    _normalize_claim_result_payload,
    _provider_chunk_ledger_for_output_dir,
    _provider_rate_limit_cooldown_seconds,
    _provider_worker_count,
    _retry_after_seconds_from_text,
    _run_provider_full_corpus,
    _with_claim_group_signals,
    finding_impact_from_synthesis,
    provider_chunk_plan_summary,
    run_multi_ai,
    synthesize_multi_ai,
)
from ops_evidence_synthesis.synthesis.validation import validate_claim_result


ROOT = Path(__file__).resolve().parents[1]


def test_claim_group_agreement_and_disagreement_signals_are_independent() -> None:
    group = _with_claim_group_signals(
        {
            "provider_count": 2,
            "support_provider_count": 2,
            "support_claim_count": 2,
            "counter_claim_count": 1,
            "caveat_claim_count": 0,
            "validation_claim_count": 0,
            "missing_evidence": ["request trace"],
            "unsupported": False,
        },
        successful_provider_count=3,
    )

    assert group["agreement_signal"] is True
    assert group["disagreement_signal"] is True
    assert group["signals"]["relationship"] == "independent_non_exclusive"
    assert group["signals"]["disagreement_reasons"] == ["counter_claim", "missing_evidence"]


def _claim_run(provider_id: str, claim: dict[str, Any]) -> dict[str, Any]:
    return {
        "provider_id": provider_id,
        "status": "ok",
        "schema_valid": True,
        "parsed_result": {"claims": [claim]},
    }


def _review_claim(claim_type: str, *, finding_status: str = "supported") -> dict[str, Any]:
    return {
        "claim_type": claim_type,
        "finding_status": finding_status,
        "claim_text": f"{claim_type} review",
        "core_target_type": "runtime_exception",
        "subsystem": "runtime_recovery",
        "component": "worker",
        "evidence_refs": ["EVIDENCE-001"],
        "counter_evidence_refs": [],
        "missing_evidence": [],
    }


def test_one_support_and_one_counter_is_not_agreement() -> None:
    support = _review_claim("support")
    counter = _review_claim("counter_evidence")
    counter["evidence_refs"] = []
    counter["counter_evidence_refs"] = ["EVIDENCE-001"]

    groups = _claim_groups(
        [_claim_run("provider-a", support), _claim_run("provider-b", counter)],
        {"EVIDENCE-001"},
    )
    group = _with_claim_group_signals(groups[0], successful_provider_count=2)

    assert group["agreement_signal"] is False
    assert group["disagreement_signal"] is True
    assert group["participating_provider_count"] == 2
    assert group["support_provider_count"] == 1
    assert group["counter_evidence_refs"] == ["EVIDENCE-001"]


def test_two_supports_and_one_counter_can_signal_agreement_and_disagreement() -> None:
    support_a = _review_claim("support")
    support_b = _review_claim("support")
    counter = _review_claim("counter_evidence")
    counter["evidence_refs"] = []
    counter["counter_evidence_refs"] = ["EVIDENCE-001"]

    groups = _claim_groups(
        [
            _claim_run("provider-a", support_a),
            _claim_run("provider-b", support_b),
            _claim_run("provider-c", counter),
        ],
        {"EVIDENCE-001"},
    )
    group = _with_claim_group_signals(groups[0], successful_provider_count=3)

    assert group["agreement_signal"] is True
    assert group["disagreement_signal"] is True
    assert group["support_provider_count"] == 2
    assert group["participating_provider_count"] == 3


def test_caveats_and_insufficient_findings_do_not_contribute_support() -> None:
    caveat_a = _review_claim("caveat")
    caveat_b = _review_claim("caveat")
    insufficient = _review_claim("support", finding_status="insufficient_evidence")

    caveat_group = _with_claim_group_signals(
        _claim_groups(
            [_claim_run("provider-a", caveat_a), _claim_run("provider-b", caveat_b)],
            {"EVIDENCE-001"},
        )[0],
        successful_provider_count=2,
    )
    insufficient_group = _with_claim_group_signals(
        _claim_groups([_claim_run("provider-c", insufficient)], {"EVIDENCE-001"})[0],
        successful_provider_count=2,
    )

    assert caveat_group["agreement_signal"] is False
    assert caveat_group["disagreement_signal"] is True
    assert insufficient_group["support_claim_count"] == 0
    assert insufficient_group["insufficient_evidence_claim_count"] == 1
    assert insufficient_group["agreement_signal"] is False
    assert insufficient_group["disagreement_signal"] is True


def test_unknown_counter_reference_is_a_validity_failure_and_is_retained() -> None:
    counter = _review_claim("counter_evidence")
    counter["evidence_refs"] = []
    counter["counter_evidence_refs"] = ["EVIDENCE-UNKNOWN"]

    group = _with_claim_group_signals(
        _claim_groups([_claim_run("provider-a", counter)], {"EVIDENCE-001"})[0],
        successful_provider_count=1,
    )

    assert group["unsupported_signal"] is True
    assert group["unsupported_reason"] == "invalid_evidence_reference"
    assert group["invalid_evidence_refs"] == ["EVIDENCE-UNKNOWN"]
    assert group["counter_evidence_refs"] == ["EVIDENCE-UNKNOWN"]
    assert group["agreement_signal"] is False
    assert group["disagreement_signal"] is False


def test_provider_worker_count_can_be_limited_for_real_api_stability(monkeypatch) -> None:
    monkeypatch.delenv("OES_MULTI_AI_MAX_WORKERS", raising=False)
    assert _provider_worker_count(5) == 5

    monkeypatch.setenv("OES_MULTI_AI_MAX_WORKERS", "1")
    assert _provider_worker_count(5) == 1

    monkeypatch.setenv("OES_MULTI_AI_MAX_WORKERS", "99")
    assert _provider_worker_count(5) == 5


def test_chunk_worker_count_can_be_limited_for_real_api_stability(monkeypatch) -> None:
    monkeypatch.delenv("OES_MULTI_AI_CHUNK_MAX_WORKERS", raising=False)
    monkeypatch.delenv("OES_MULTI_AI_CHUNK_MAX_WORKERS_BY_PROVIDER", raising=False)
    monkeypatch.delenv("OES_MULTI_AI_CHUNK_MAX_WORKERS_GEMINI_ENTERPRISE_AGENT_PLATFORM", raising=False)
    assert _chunk_worker_count(10) == 4

    monkeypatch.setenv("OES_MULTI_AI_CHUNK_MAX_WORKERS", "1")
    assert _chunk_worker_count(10) == 1

    monkeypatch.setenv("OES_MULTI_AI_CHUNK_MAX_WORKERS", "99")
    assert _chunk_worker_count(3) == 3


def test_chunk_worker_count_uses_provider_specific_limits(monkeypatch) -> None:
    monkeypatch.setenv("OES_MULTI_AI_CHUNK_MAX_WORKERS", "2")
    monkeypatch.setenv("OES_MULTI_AI_CHUNK_MAX_WORKERS_GEMINI_ENTERPRISE_AGENT_PLATFORM", "8")

    assert _chunk_worker_count(20, "gemini-enterprise-agent-platform") == 8
    assert _chunk_worker_count(20, "qwen-agent-platform") == 2

    monkeypatch.delenv("OES_MULTI_AI_CHUNK_MAX_WORKERS_GEMINI_ENTERPRISE_AGENT_PLATFORM", raising=False)
    monkeypatch.setenv(
        "OES_MULTI_AI_CHUNK_MAX_WORKERS_BY_PROVIDER",
        "gemini-enterprise-agent-platform=7,mistral-agent-platform=2",
    )

    assert _chunk_worker_count(20, "gemini-enterprise-agent-platform") == 7
    assert _chunk_worker_count(20, "mistral-agent-platform") == 2
    assert _chunk_worker_count(20, "glm-agent-platform") == 2


def test_llama_uses_gemini_chunk_token_budget() -> None:
    assert _chunk_target_tokens("llama-agent-platform") == _chunk_target_tokens("gemini-enterprise-agent-platform")


def test_mistral_defaults_to_large_low_parallel_chunks(monkeypatch) -> None:
    monkeypatch.delenv("OES_MULTI_AI_EVIDENCE_CHUNK_SIZE", raising=False)
    monkeypatch.delenv("OES_MULTI_AI_EVIDENCE_CHUNK_SIZE_MISTRAL_AGENT_PLATFORM", raising=False)
    monkeypatch.delenv("OES_MULTI_AI_EVIDENCE_CHUNK_SIZE_BY_PROVIDER", raising=False)
    monkeypatch.delenv("OES_MULTI_AI_CHUNK_MAX_WORKERS", raising=False)
    monkeypatch.delenv("OES_MULTI_AI_CHUNK_MAX_WORKERS_MISTRAL_AGENT_PLATFORM", raising=False)
    monkeypatch.delenv("OES_MULTI_AI_CHUNK_MAX_WORKERS_BY_PROVIDER", raising=False)

    assert _chunk_target_tokens("mistral-agent-platform") == 120_000
    assert _evidence_chunk_size("mistral-agent-platform") == 500
    assert _chunk_worker_count(20, "mistral-agent-platform") == 1
    assert _chunk_input_tokens_per_minute("mistral-agent-platform") == 60_000
    assert _chunk_min_start_interval_seconds("mistral-agent-platform") == 120.0
    assert _provider_rate_limit_cooldown_seconds("mistral-agent-platform") == 180.0


def test_mistral_chunk_count_is_smaller_than_gemini_for_tiny_items(monkeypatch) -> None:
    monkeypatch.delenv("OES_MULTI_AI_EVIDENCE_CHUNK_SIZE", raising=False)
    monkeypatch.delenv("OES_MULTI_AI_EVIDENCE_CHUNK_SIZE_MISTRAL_AGENT_PLATFORM", raising=False)
    monkeypatch.delenv("OES_MULTI_AI_CHUNK_TARGET_TOKENS_MISTRAL_AGENT_PLATFORM", raising=False)
    evidence_items = [
        {
            "evidence_id": f"PATTERN-{index:03d}",
            "coverage_class": "pattern",
            "component": "dispatcher",
            "event_type": "delivery_state",
            "message_template": "short repeated delivery state",
            "count": 50,
        }
        for index in range(1, 421)
    ]
    bundle = {
        "evidence_sha256": "mistral-smallest-chunk-count-sha",
        "evidence_items": evidence_items,
        "evidence_refs": {str(item["evidence_id"]): item for item in evidence_items},
    }

    gemini_chunks = _evidence_item_chunks(bundle, provider_id="gemini-enterprise-agent-platform")
    mistral_chunks = _evidence_item_chunks(bundle, provider_id="mistral-agent-platform")

    assert len(gemini_chunks) == 3
    assert len(mistral_chunks) == 1
    assert len(mistral_chunks[0]) == 420


def test_mistral_chunk_start_interval_uses_estimated_tokens(monkeypatch) -> None:
    monkeypatch.setenv("OES_MULTI_AI_CHUNK_INPUT_TOKENS_PER_MINUTE_MISTRAL_AGENT_PLATFORM", "60000")
    items = [
        {
            "evidence_id": "PATTERN-001",
            "coverage_class": "pattern",
            "component": "worker",
            "event_type": "delivery_state",
            "message_template": "x" * 1000,
        }
    ]

    assert _chunk_input_tokens_per_minute("mistral-agent-platform") == 60_000
    assert _chunk_start_interval_seconds("mistral-agent-platform", items) > 0
    assert _chunk_start_interval_seconds("gemini-enterprise-agent-platform", items) == 0


def test_llama_reuses_gemini_chunk_plan_even_with_llama_specific_env(monkeypatch) -> None:
    monkeypatch.setenv("OES_MULTI_AI_EVIDENCE_CHUNK_SIZE", "140")
    monkeypatch.setenv("OES_MULTI_AI_CHUNK_TARGET_TOKENS_GEMINI_ENTERPRISE_AGENT_PLATFORM", "4000")
    monkeypatch.setenv("OES_MULTI_AI_CHUNK_TARGET_TOKENS_LLAMA_AGENT_PLATFORM", "9000")
    evidence_items = [
        {
            "evidence_id": f"PATTERN-{index:03d}",
            "coverage_class": "singleton",
            "component": "worker",
            "event_type": "traceback",
            "example_sanitized": "traceback frame " + ("x" * 3000),
            "count": 1,
        }
        for index in range(1, 8)
    ]
    bundle = {
        "evidence_sha256": "llama-gemini-chunk-plan-sha",
        "evidence_items": evidence_items,
        "evidence_refs": {str(item["evidence_id"]): item for item in evidence_items},
    }

    gemini_chunks = _evidence_item_chunks(bundle, provider_id="gemini-enterprise-agent-platform")
    llama_chunks = _evidence_item_chunks(bundle, provider_id="llama-agent-platform")

    assert _chunk_target_tokens("llama-agent-platform") == 4000
    assert [[item["evidence_id"] for item in chunk] for chunk in llama_chunks] == [
        [item["evidence_id"] for item in chunk] for chunk in gemini_chunks
    ]


def test_evidence_chunks_use_provider_token_budget_and_semantic_buckets(monkeypatch) -> None:
    monkeypatch.setenv("OES_MULTI_AI_EVIDENCE_CHUNK_SIZE", "140")
    monkeypatch.setenv("OES_MULTI_AI_CHUNK_TARGET_TOKENS_OPENAI_GPT_OSS_ON_VERTEX", "4000")
    evidence_items: list[dict[str, Any]] = []
    for index in range(1, 4):
        evidence_items.append(
            {
                "evidence_id": f"PATTERN-{index:03d}",
                "coverage_class": "pattern",
                "component": "dispatcher",
                "event_type": "delivery_state",
                "message_template": "short repeated delivery state",
                "count": 50,
            }
        )
    for index in range(4, 8):
        evidence_items.append(
            {
                "evidence_id": f"PATTERN-{index:03d}",
                "coverage_class": "singleton",
                "component": "worker",
                "event_type": "traceback",
                "example_sanitized": "traceback frame " + ("x" * 3000),
                "count": 1,
            }
        )
    bundle = {
        "evidence_sha256": "token-budget-sha",
        "evidence_items": evidence_items,
        "evidence_refs": {str(item["evidence_id"]): item for item in evidence_items},
    }

    item_count_chunks = _evidence_item_chunks(bundle, provider_id="local-gemini")
    token_budget_chunks = _evidence_item_chunks(bundle, provider_id="openai-gpt-oss-on-vertex")

    assert _chunk_target_tokens("local-gemini") == 0
    assert _chunk_target_tokens("openai-gpt-oss-on-vertex") == 4000
    assert len(item_count_chunks) == 1
    assert len(token_budget_chunks) > 1
    assert [item["evidence_id"] for item in token_budget_chunks[0]] == ["PATTERN-001", "PATTERN-002", "PATTERN-003"]
    assert all(
        {str(item["coverage_class"]) for item in chunk} in ({"pattern"}, {"singleton"})
        for chunk in token_budget_chunks
    )


def test_chunk_estimate_applies_the_provider_prompt_text_boundary(monkeypatch) -> None:
    monkeypatch.setenv("OES_GPT_OSS_MAX_TEXT_CHARS", "480")
    item = {
        "evidence_id": "PATTERN-001",
        "coverage_class": "singleton",
        "component": "worker",
        "event_type": "runtime_state",
        "message_template": "x" * 200_000,
        "example_sanitized": "y" * 200_000,
        "count": 1,
    }

    estimated = _estimated_evidence_item_tokens(
        item,
        provider_id="openai-gpt-oss-on-vertex",
    )

    assert estimated < 2_000
    assert len(_evidence_item_chunks(
        {"evidence_items": [item]},
        provider_id="openai-gpt-oss-on-vertex",
    )) == 1


def test_chunk_claim_merge_sorts_by_manifest_chunk_index_not_input_order() -> None:
    provider = SimpleNamespace(provider="gemini-enterprise-agent-platform")
    envelopes = [
        _chunk_envelope(
            chunk_index=2,
            chunk_id="chunk-runtime-002",
            claim_text="second chunk claim",
            evidence_refs=["PATTERN-002"],
        ),
        _chunk_envelope(
            chunk_index=1,
            chunk_id="chunk-runtime-001",
            claim_text="first chunk claim",
            evidence_refs=["PATTERN-001"],
        ),
    ]

    payload = _merge_chunk_claim_payloads(provider, envelopes, chunk_count=2)

    assert [row["source_chunk_index"] for row in payload["claims"]] == [1, 2]
    assert [row["claim_text"] for row in payload["claims"]] == ["first chunk claim", "second chunk claim"]


def _chunk_envelope(
    *,
    chunk_index: int,
    chunk_id: str,
    claim_text: str,
    evidence_refs: list[str],
) -> _ArtifactEnvelope:
    parsed = {
        "schema_version": "claim-result/v1",
        "agent_role": "test",
        "finding_status": "supported",
        "summary": f"chunk {chunk_index}",
        "claims": [
            {
                "claim_type": "operational",
                "claim_text": claim_text,
                "evidence_refs": evidence_refs,
                "counter_evidence_refs": [],
            }
        ],
        "propositions": [],
    }
    artifact = {
        "status": "ok",
        "schema_valid": True,
        "model_input_context": {
            "full_corpus_coverage": {
                "chunk": {
                    "chunk_index": chunk_index,
                    "chunk_id": chunk_id,
                    "chunk_type": "runtime",
                }
            }
        },
    }
    return _ArtifactEnvelope(
        artifact=artifact,
        raw_output=json.dumps(parsed, sort_keys=True),
        parsed_payload=parsed,
        output_parse=None,
    )


def _bundle_and_profile(tmp_path: Path) -> tuple[dict[str, object], dict[str, object]]:
    sanitized_dir = tmp_path / "sanitized"
    sanitize_input(ROOT / "sample_logs" / "redaction_fixture.jsonl", sanitized_dir)
    bundle = build_bundle_from_sanitized(
        sanitized_dir / "sanitized_events.jsonl",
        service="demo-payment",
        environment="prod",
        start="2026-06-16T00:00:00Z",
        end="2026-06-16T18:00:00Z",
        profile_name="generic",
        out_path=tmp_path / "evidence_bundle.json",
    )
    discovery = discover_profile(
        ROOT / "sample_projects" / "profile_discovery_sample",
        evidence_bundle_path=tmp_path / "evidence_bundle.json",
        service="demo-payment",
        environment="prod",
        output_dir=tmp_path / "discovery",
    )
    assert discovery["discovery_sha256"]
    draft_profile(
        tmp_path / "discovery" / "profile_discovery_bundle.json",
        provider="local",
        out_path=tmp_path / "profile_draft.json",
    )
    approve_profile_draft(
        tmp_path / "profile_draft.json",
        profile_id="demo-payment-approved",
        approved_by="pytest",
        out_path=tmp_path / "approved_profile.yaml",
    )
    approved = json.loads((tmp_path / "approved_profile.yaml").read_text(encoding="utf-8"))
    return bundle, approved


def test_run_multi_ai_cli_generates_artifacts_with_local_providers(tmp_path: Path) -> None:
    bundle, _profile = _bundle_and_profile(tmp_path)
    out = tmp_path / "multi_ai"
    command = [
        sys.executable,
        "-m",
        "ops_evidence_synthesis.cli",
        "run-multi-ai",
        "--bundle",
        str(tmp_path / "evidence_bundle.json"),
        "--profile",
        str(tmp_path / "approved_profile.yaml"),
        "--providers",
        "local-gemini,local-gpt-oss,local-mistral",
        "--out",
        str(out),
    ]
    completed = subprocess.run(command, cwd=ROOT, text=True, capture_output=True, check=True)
    assert "local-gemini: ok schema_valid=true" in completed.stdout
    model_runs = [json.loads(line) for line in (out / "model_runs.jsonl").read_text(encoding="utf-8").splitlines()]
    synthesis = json.loads((out / "multi_ai_synthesis.json").read_text(encoding="utf-8"))
    run_envelope = json.loads((out / "multi_ai_run.json").read_text(encoding="utf-8"))
    profile_context = json.loads((out / "profile_context.json").read_text(encoding="utf-8"))
    review_targets = json.loads((out / "review_targets.json").read_text(encoding="utf-8"))
    canonical_graph = json.loads((out / "canonical_review_graph.json").read_text(encoding="utf-8"))
    provenance = json.loads((out / "validation_provenance.json").read_text(encoding="utf-8"))

    assert len(model_runs) == 3
    for run in model_runs:
        assert run["schema_version"] == "model_run.v1"
        assert run["provider_id"]
        assert run["status"] == "ok"
        assert run["schema_valid"] is True
        assert run["raw_output_sha256"]
        assert run["parsed_json_sha256"]
        assert run["retry"]["attempts"] == 1
        assert "estimated_cost_usd" in run["cost_estimate"]
        assert "raw_output" not in run
        assert run["safety_preflight"]["raw_logs_sent_to_providers"] is False

    assert synthesis["schema_version"] == "multi_ai_synthesis.v1"
    assert synthesis["evidence_sha256"] == bundle["evidence_sha256"]
    assert len(synthesis["agreement_groups"]) >= 1
    assert len(synthesis["disagreement_groups"]) >= 1
    assert len(synthesis["disagreement_themes"]) >= 1
    assert len(synthesis["validation_targets"]) >= 1
    assert synthesis["finding_summary"]["finding"]
    assert synthesis["token_usage"]["input_tokens"] >= 0
    assert synthesis["cost_estimate"]["pricing_source"] in {"env", "not_configured"}
    assert synthesis["score_note"] == SCORE_NOTE
    assert run_envelope["profile_context"]["schema_version"] == "profile_context_summary.v2"
    assert run_envelope["profile_context"]["context_is_not_incident_evidence"] is True
    assert run_envelope["model_runs"][0]["model_input_context"]["approved_profile_context_included"] is True
    assert run_envelope["model_runs"][0]["model_input_context"]["profile_status"]
    assert run_envelope["model_runs"][0]["model_input_context"]["model_input_sha256"]
    assert profile_context["schema_version"] == "profile_context_summary.v2"
    assert profile_context["context_is_not_incident_evidence"] is True
    assert review_targets
    assert canonical_graph["schema_version"] == "canonical_review_graph.v1"
    assert "agreement_dimensions" in canonical_graph
    assert provenance["schema_version"] == "multi_ai_validation_provenance.v1"
    assert provenance["implementation"]["commit_sha"]
    assert len(provenance["artifacts"]["multi_ai_run.json"]["sha256"]) == 64
    assert provenance["public_projection_artifact"]["sha256"] == provenance["artifacts"]["multi_ai_run.json"][
        "sha256"
    ]
    assert provenance["canonical_graph_artifact"]["sha256"] == provenance["artifacts"][
        "canonical_review_graph.json"
    ]["sha256"]


def test_support_claim_without_evidence_id_is_unsupported() -> None:
    bundle = {"evidence_sha256": "sha", "evidence_refs": {"LOG-1": {"message": "safe"}}}
    model_runs = [
        {
            "provider_id": "local-gemini",
            "status": "ok",
            "schema_valid": True,
            "parsed_result": {
                "claims": [
                    {
                        "claim_type": "support",
                        "claim_text": "Restart loop is likely.",
                        "evidence_refs": [],
                        "missing_evidence": [],
                    }
                ]
            },
            "safety_preflight": {"passed": True},
        }
    ]
    synthesis = synthesize_multi_ai(bundle, model_runs)
    assert synthesis["claim_groups"][0]["unsupported"] is True
    assert synthesis["auto_archived"][0]["reason"] == "unsupported_support_without_evidence_id"


def test_claim_result_normalization_infers_refs_from_evidence_summary() -> None:
    payload = {
        "schema_version": "claim-result/v1",
        "summary": "Memory pressure should be reviewed.",
        "claims": [
            {
                "claim_type": "support",
                "claim_text": "Memory pressure is visible in PATTERN-1504.",
                "evidence_summary": [
                    'PATTERN-1504: stream_v3_memory_critical_count{window="rolling_1h"} 26.0',
                    'PATTERN-1505: stream_v3_memory_critical_count{window="rolling_24h"} 355.0',
                ],
            }
        ],
    }

    normalized, rules = _normalize_claim_result_payload(
        payload,
        known_refs={"PATTERN-1504", "PATTERN-1505", "PATTERN-9999"},
    )

    assert rules == ("evidence_refs_from_summary:0:2",)
    assert normalized["claims"][0]["evidence_refs"] == ["PATTERN-1504", "PATTERN-1505"]
    valid, errors = validate_claim_result(normalized)
    assert valid is True
    assert errors == ()


def test_safety_preflight_blocks_secret_like_model_input(tmp_path: Path) -> None:
    bundle, profile = _bundle_and_profile(tmp_path)
    unsafe = dict(bundle)
    unsafe["evidence_items"] = [
        *list(bundle.get("evidence_items") or []),
        {"evidence_id": "LEAK-1", "example_sanitized": "Authorization: Bearer raw-token-123456789"},
    ]
    result = run_multi_ai(unsafe, profile, providers=["local-gemini"], output_dir=tmp_path / "blocked")
    run = result["model_runs"][0]
    assert run["status"] == "blocked_by_safety_preflight"
    assert run["failure_reason"] == "secret_like_pattern_detected"
    assert run["safety_preflight"]["passed"] is False


def test_external_provider_unconfigured_is_skipped_not_configured(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("OES_ENABLE_REAL_AI", raising=False)
    bundle, profile = _bundle_and_profile(tmp_path)
    result = run_multi_ai(bundle, profile, providers=["gemini"], output_dir=tmp_path / "skip")
    run = result["model_runs"][0]
    assert run["provider_id"] == "gemini-enterprise-agent-platform"
    assert run["status"] == "skipped_not_configured"
    assert result["multi_ai_synthesis"]["skipped_provider_count"] == 1


def test_provider_failure_does_not_block_other_provider_synthesis(tmp_path: Path) -> None:
    bundle, profile = _bundle_and_profile(tmp_path)
    result = run_multi_ai(
        bundle,
        profile,
        providers=["local-gemini", "local-fail", "local-gpt-oss"],
        output_dir=tmp_path / "partial",
    )
    statuses = {run["provider_id"]: run["status"] for run in result["model_runs"]}
    assert statuses["local-fail"] == "failed"
    assert statuses["local-gemini"] == "ok"
    assert statuses["local-gpt-oss"] == "ok"
    assert result["multi_ai_synthesis"]["successful_provider_count"] == 2
    assert result["multi_ai_synthesis"]["failed_provider_count"] == 1
    assert result["multi_ai_synthesis"]["provider_execution_status_counts"]["provider_error"] == 1
    failed_status = next(
        row
        for row in result["multi_ai_synthesis"]["provider_statuses"]
        if row["provider_id"] == "local-fail"
    )
    assert failed_status["execution_status"] == "provider_error"
    assert failed_status["failure_is_not_silent"] is True
    assert len(result["multi_ai_synthesis"]["agreement_groups"]) >= 1


def test_synthesis_counts_legacy_error_status_as_failed() -> None:
    synthesis = synthesize_multi_ai(
        {"evidence_sha256": "sha", "evidence_refs": {}},
        [
            {
                "provider_id": "legacy-provider",
                "status": "error",
                "schema_valid": False,
                "parsed_result": {"claims": []},
                "safety_preflight": {"passed": True},
            }
        ],
    )

    assert synthesis["failed_provider_count"] == 1


def test_multi_ai_api_and_ui_panel_include_provider_statuses(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("OES_STORE", "sqlite")
    monkeypatch.setenv("OES_DB_PATH", str(tmp_path / "api.sqlite3"))
    bundle, profile = _bundle_and_profile(tmp_path)

    from ops_evidence_synthesis.api import app

    with TestClient(app) as client:
        response = client.post(
            "/ai/multi-run",
            json={
                "evidence_bundle": bundle,
                "approved_profile": profile,
                "providers": ["local-gemini", "local-gpt-oss", "local-mistral"],
                "mode": "local",
            },
        )
        assert response.status_code == 200, response.text
        payload = response.json()
        assert len(payload["model_runs"]) == 3
        assert len(payload["multi_ai_synthesis"]["provider_statuses"]) == 3
        assert "disagreement_themes" in payload["multi_ai_synthesis"]
        assert payload["multi_ai_synthesis"]["score_note"] == SCORE_NOTE

        status = client.get(f"/pipeline-status?pipeline_run_id={payload['pipeline_run_id']}").json()
        assert status["operation"] == "multi_ai"
        assert status["canonical_state"] == "completed"
        scheduled_step = next(step for step in status["steps"] if step["step_key"] == "providers_scheduled")
        assert scheduled_step["status"] == "completed"
        assert {item["state"] for item in status["state_timeline"]} >= {
            "uploaded",
            "validated",
            "safety_passed",
            "providers_scheduled",
            "provider_completed",
            "schema_validated",
            "arbitration_completed",
            "review_targets_persisted",
            "completed",
        }

        html = client.get(f"/?evidence_sha256={bundle['evidence_sha256']}&full=1").text
        assert "Multi-AI runs" in html
        assert "State: completed" in html
        assert "validated" in html
        assert "Disagreement Themes" in html
        assert "Canonical Review Graph" in html
        assert "Provider detection overlap" in html
        assert "Planner quality warnings" in html
        assert "Collection timezone" in html
        assert "Operator display timezone" in html
        assert "component_map_select" in html
        assert "Raw logs were not sent to providers" in html


def test_model_input_policy_states_raw_logs_are_not_sent(tmp_path: Path) -> None:
    bundle, profile = _bundle_and_profile(tmp_path)
    result = run_multi_ai(
        bundle,
        profile,
        providers=["local-gemini", "local-gpt-oss", "local-mistral"],
        output_dir=tmp_path / "policy",
    )
    synthesis = result["multi_ai_synthesis"]
    assert synthesis["safety"]["raw_logs_sent_to_providers"] is False
    assert "Raw logs are never sent to providers" in synthesis["safety"]["policy"]
    assert all(run["safety_preflight"]["raw_logs_sent_to_providers"] is False for run in result["model_runs"])


def test_multi_ai_chunks_all_evidence_items_instead_of_sampling_tail(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("OES_MULTI_AI_EVIDENCE_CHUNK_SIZE", "2")
    evidence_items = [
        {
            "evidence_id": f"PATTERN-{index:03d}",
            "type": "log_pattern",
            "coverage_class": "singleton",
            "event_type": "database_timeout" if index == 1 else "runtime_restart",
            "severity_text": "error",
            "count": 1,
            "source_log_count": 1,
            "first_seen": f"2026-06-16T00:0{index}:00Z",
            "last_seen": f"2026-06-16T00:0{index}:30Z",
            "message_template": f"low-frequency runtime_restart marker {index}",
            "example_sanitized": f"runtime_restart low-frequency marker {index}",
            "component": "worker",
            "source": "sanitized_events",
        }
        for index in range(1, 6)
    ]
    bundle = {
        "schema_version": "evidence_bundle.v1",
        "bundle_type": "sanitized_evidence_bundle",
        "evidence_sha256": "chunked-full-corpus-sha",
        "raw_log_policy": "not_uploaded",
        "source": {"service": "demo-worker", "environment": "prod"},
        "service": "demo-worker",
        "environment": "prod",
        "time_window": {"start": "2026-06-16T00:00:00Z", "end": "2026-06-16T01:00:00Z"},
        "local_first_summary": {"raw_logs_uploaded": False, "sanitized_event_count": 5},
        "evidence_items": evidence_items,
        "evidence_refs": {str(item["evidence_id"]): item for item in evidence_items},
        "signals": [],
        "prompt_rules": [],
    }

    result = run_multi_ai(bundle, {}, providers=["local-gemini"], output_dir=tmp_path / "chunked")
    run = result["model_runs"][0]
    coverage = run["full_corpus_coverage"]

    assert run["status"] == "ok"
    assert run["schema_valid"] is True
    assert coverage["mode"] == "full_evidence_item_chunking"
    assert coverage["full_evidence_item_count"] == 5
    assert coverage["analyzed_evidence_item_count"] == 5
    assert coverage["omitted_evidence_item_count"] == 0
    assert coverage["unassigned_evidence_item_count"] == 0
    assert coverage["direct_prompt_evidence_item_count"] == 5
    assert coverage["tail_evidence_item_count"] == 0
    assert coverage["coverage_ratio"] == 1.0
    assert coverage["chunk_count"] == 3
    assert coverage["chunk_manifest_entry_count"] == 3
    assert coverage["chunk_manifest_sha256"]
    assert coverage["coverage_class_counts"] == {"singleton": 5}
    assert [row["chunk_id"] for row in run["chunk_results"]] == [
        "chunk-rare-singleton-001",
        "chunk-rare-singleton-002",
        "chunk-rare-singleton-003",
    ]
    assert [row["evidence_item_count"] for row in run["chunk_results"]] == [2, 2, 1]
    assert [row["source_log_count"] for row in run["chunk_results"]] == [2, 2, 1]
    assert all(row["provider_prompt_sha256"] for row in run["chunk_results"])
    assert {claim["source_chunk_index"] for claim in run["parsed_result"]["claims"]} == {1, 2, 3}
    assert {claim["source_chunk_id"] for claim in run["parsed_result"]["claims"]} == {
        "chunk-rare-singleton-001",
        "chunk-rare-singleton-002",
        "chunk-rare-singleton-003",
    }
    assert any(
        "PATTERN-005" in claim.get("evidence_refs", [])
        for claim in run["parsed_result"]["claims"]
    )
    assert result["context_inputs"]["full_corpus_coverage"]["coverage_ratio"] == 1.0


def test_provider_chunk_plan_summary_matches_chunking(monkeypatch) -> None:
    monkeypatch.setenv("OES_MULTI_AI_EVIDENCE_CHUNK_SIZE", "2")
    evidence_items = [
        {
            "evidence_id": f"PATTERN-{index:03d}",
            "type": "log_pattern",
            "coverage_class": "singleton",
            "event_type": "runtime_restart",
            "count": 1,
            "source_log_count": 1,
            "component": "worker",
        }
        for index in range(1, 6)
    ]
    bundle = {
        "schema_version": "evidence_bundle.v1",
        "evidence_sha256": "chunk-plan-sha",
        "raw_log_policy": "not_uploaded",
        "service": "demo-worker",
        "environment": "prod",
        "time_window": {"start": "2026-06-16T00:00:00Z", "end": "2026-06-16T01:00:00Z"},
        "local_first_summary": {"raw_logs_uploaded": False, "sanitized_event_count": 5},
        "evidence_items": evidence_items,
        "evidence_refs": {str(item["evidence_id"]): item for item in evidence_items},
        "signals": [],
        "prompt_rules": [],
    }

    plan = provider_chunk_plan_summary(bundle, providers=["local-gemini"], mode="local")
    provider_plan = plan["providers"][0]

    assert plan["provider_count"] == 1
    assert plan["provider_chunk_count"] == 3
    assert plan["evidence_item_count"] == 5
    assert plan["source_log_count"] == 5
    assert provider_plan["provider"] == "local-gemini"
    assert provider_plan["chunk_count"] == 3
    assert [row["evidence_item_count"] for row in provider_plan["chunks"]] == [2, 2, 1]
    assert [row["source_log_count"] for row in provider_plan["chunks"]] == [2, 2, 1]
    assert all(row["estimated_input_tokens"] > 0 for row in provider_plan["chunks"])


@dataclass(frozen=True, slots=True)
class ChunkBarrierProvider:
    provider: str = "chunk-barrier-provider"
    model_name: str = "chunk-barrier-model"
    prompt_name: str = "root-cause"
    temperature: float = 0.0
    barrier: threading.Barrier | None = None
    events: list[tuple[int, str, float]] | None = None
    lock: Any | None = None

    def run(self, bundle: dict[str, Any]) -> ModelResponse:
        chunk = bundle.get("full_corpus_chunk") if isinstance(bundle.get("full_corpus_chunk"), dict) else {}
        chunk_index = int(chunk.get("chunk_index") or 1)
        evidence_ids = list(chunk.get("evidence_ids") or [])
        self._record(chunk_index, "entered")
        if self.barrier is not None:
            self.barrier.wait(timeout=2.0)
        self._record(chunk_index, "released")
        return ModelResponse(
            provider=self.provider,
            model_name=self.model_name,
            prompt_name=self.prompt_name,
            temperature=self.temperature,
            raw_output=json.dumps(
                {
                    "schema_version": "claim-result/v1",
                    "agent_role": "chunk_test",
                    "finding_status": "supported",
                    "summary": f"chunk {chunk_index} reviewed",
                    "claims": [
                        {
                            "claim_type": "support",
                            "claim_text": f"chunk {chunk_index} evidence reviewed",
                            "evidence_refs": evidence_ids[:1],
                            "missing_evidence": [],
                        }
                    ],
                    "propositions": [],
                },
                sort_keys=True,
            ),
            latency_ms=1,
            input_tokens=1,
            output_tokens=1,
        )

    def _record(self, chunk_index: int, action: str) -> None:
        if self.events is None:
            return
        row = (chunk_index, action, time.monotonic())
        if self.lock is None:
            self.events.append(row)
            return
        with self.lock:
            self.events.append(row)


@dataclass(frozen=True, slots=True)
class ChunkFailureProvider:
    provider: str = "chunk-failure-provider"
    model_name: str = "chunk-failure-model"
    prompt_name: str = "root-cause"
    temperature: float = 0.0

    def run(self, bundle: dict[str, Any]) -> ModelResponse:
        chunk = bundle.get("full_corpus_chunk") if isinstance(bundle.get("full_corpus_chunk"), dict) else {}
        chunk_index = int(chunk.get("chunk_index") or 1)
        if chunk_index == 2:
            return ModelResponse(
                provider=self.provider,
                model_name=self.model_name,
                prompt_name=self.prompt_name,
                temperature=self.temperature,
                raw_output=json.dumps({"status": "failed", "chunk_index": chunk_index}, sort_keys=True),
                latency_ms=1,
                input_tokens=1,
                output_tokens=1,
                status="failed",
            )
        evidence_ids = list(chunk.get("evidence_ids") or [])
        return ModelResponse(
            provider=self.provider,
            model_name=self.model_name,
            prompt_name=self.prompt_name,
            temperature=self.temperature,
            raw_output=json.dumps(
                {
                    "schema_version": "claim-result/v1",
                    "agent_role": "chunk_failure_test",
                    "finding_status": "supported",
                    "summary": f"chunk {chunk_index} reviewed",
                    "claims": [
                        {
                            "claim_type": "support",
                            "claim_text": f"chunk {chunk_index} evidence reviewed",
                            "evidence_refs": evidence_ids[:1],
                            "missing_evidence": [],
                        }
                    ],
                    "propositions": [],
                },
                sort_keys=True,
            ),
            latency_ms=1,
            input_tokens=1,
            output_tokens=1,
        )


@dataclass(frozen=True, slots=True)
class FlakyChunkProvider:
    provider: str = "flaky-chunk-provider"
    model_name: str = "flaky-chunk-model"
    prompt_name: str = "root-cause"
    temperature: float = 0.0
    calls: dict[int, int] | None = None

    def run(self, bundle: dict[str, Any]) -> ModelResponse:
        chunk = bundle.get("full_corpus_chunk") if isinstance(bundle.get("full_corpus_chunk"), dict) else {}
        chunk_index = int(chunk.get("chunk_index") or 1)
        if self.calls is not None:
            self.calls[chunk_index] = self.calls.get(chunk_index, 0) + 1
        if chunk_index == 2 and self.calls is not None and self.calls[chunk_index] == 1:
            return ModelResponse(
                provider=self.provider,
                model_name=self.model_name,
                prompt_name=self.prompt_name,
                temperature=self.temperature,
                raw_output=json.dumps({"status": "transient", "chunk_index": chunk_index}, sort_keys=True),
                latency_ms=1,
                input_tokens=1,
                output_tokens=1,
                status="failed",
            )
        evidence_ids = list(chunk.get("evidence_ids") or [])
        return ModelResponse(
            provider=self.provider,
            model_name=self.model_name,
            prompt_name=self.prompt_name,
            temperature=self.temperature,
            raw_output=json.dumps(
                {
                    "schema_version": "claim-result/v1",
                    "agent_role": "flaky_chunk_test",
                    "finding_status": "supported",
                    "summary": f"chunk {chunk_index} reviewed after retry",
                    "claims": [
                        {
                            "claim_type": "support",
                            "claim_text": f"chunk {chunk_index} evidence reviewed",
                            "evidence_refs": evidence_ids[:1],
                            "missing_evidence": [],
                        }
                    ],
                    "propositions": [],
                },
                sort_keys=True,
            ),
            latency_ms=1,
            input_tokens=1,
            output_tokens=1,
        )


@dataclass(frozen=True, slots=True)
class CountingChunkProvider:
    provider: str = "counting-chunk-provider"
    model_name: str = "counting-chunk-model"
    prompt_name: str = "root-cause"
    temperature: float = 0.0
    cache_reuse_policy: str = ""
    calls: list[int] | None = None
    lock: Any | None = None

    def run(self, bundle: dict[str, Any]) -> ModelResponse:
        chunk = bundle.get("full_corpus_chunk") if isinstance(bundle.get("full_corpus_chunk"), dict) else {}
        chunk_index = int(chunk.get("chunk_index") or 1)
        if self.calls is not None:
            if self.lock is None:
                self.calls.append(chunk_index)
            else:
                with self.lock:
                    self.calls.append(chunk_index)
        evidence_ids = list(chunk.get("evidence_ids") or [])
        return ModelResponse(
            provider=self.provider,
            model_name=self.model_name,
            prompt_name=self.prompt_name,
            temperature=self.temperature,
            raw_output=json.dumps(
                {
                    "schema_version": "claim-result/v1",
                    "agent_role": "counting_chunk_test",
                    "finding_status": "supported",
                    "summary": f"chunk {chunk_index} reviewed",
                    "claims": [
                        {
                            "claim_type": "support",
                            "claim_text": f"counting chunk {chunk_index} evidence reviewed",
                            "evidence_refs": evidence_ids[:1],
                            "missing_evidence": [],
                        }
                    ],
                    "propositions": [],
                },
                sort_keys=True,
            ),
            latency_ms=1,
            input_tokens=1,
            output_tokens=1,
        )


@dataclass(frozen=True, slots=True)
class RetryBarrierProvider:
    provider: str = "retry-barrier-provider"
    model_name: str = "retry-barrier-model"
    prompt_name: str = "root-cause"
    temperature: float = 0.0
    calls: dict[int, int] | None = None
    barrier: threading.Barrier | None = None
    events: list[tuple[int, str, float]] | None = None
    lock: Any | None = None

    def run(self, bundle: dict[str, Any]) -> ModelResponse:
        chunk = bundle.get("full_corpus_chunk") if isinstance(bundle.get("full_corpus_chunk"), dict) else {}
        chunk_index = int(chunk.get("chunk_index") or 1)
        if self.calls is not None:
            self.calls[chunk_index] = self.calls.get(chunk_index, 0) + 1
            if self.calls[chunk_index] == 1:
                return ModelResponse(
                    provider=self.provider,
                    model_name=self.model_name,
                    prompt_name=self.prompt_name,
                    temperature=self.temperature,
                    raw_output=json.dumps({"status": "transient", "chunk_index": chunk_index}, sort_keys=True),
                    latency_ms=1,
                    input_tokens=1,
                    output_tokens=1,
                    status="failed",
                )
        self._record(chunk_index, "retry_entered")
        if self.barrier is not None:
            self.barrier.wait(timeout=2.0)
        self._record(chunk_index, "retry_released")
        evidence_ids = list(chunk.get("evidence_ids") or [])
        return ModelResponse(
            provider=self.provider,
            model_name=self.model_name,
            prompt_name=self.prompt_name,
            temperature=self.temperature,
            raw_output=json.dumps(
                {
                    "schema_version": "claim-result/v1",
                    "agent_role": "retry_barrier_test",
                    "finding_status": "supported",
                    "summary": f"chunk {chunk_index} reviewed after parallel retry",
                    "claims": [
                        {
                            "claim_type": "support",
                            "claim_text": f"chunk {chunk_index} retry evidence reviewed",
                            "evidence_refs": evidence_ids[:1],
                            "missing_evidence": [],
                        }
                    ],
                    "propositions": [],
                },
                sort_keys=True,
            ),
            latency_ms=1,
            input_tokens=1,
            output_tokens=1,
        )

    def _record(self, chunk_index: int, action: str) -> None:
        if self.events is None:
            return
        row = (chunk_index, action, time.monotonic())
        if self.lock is None:
            self.events.append(row)
            return
        with self.lock:
            self.events.append(row)


@dataclass(frozen=True, slots=True)
class AdaptiveSplitProvider:
    provider: str = "adaptive-split-provider"
    model_name: str = "adaptive-split-model"
    prompt_name: str = "root-cause"
    temperature: float = 0.0
    calls: list[tuple[int, int, bool]] | None = None
    lock: Any | None = None

    def run(self, bundle: dict[str, Any]) -> ModelResponse:
        chunk = bundle.get("full_corpus_chunk") if isinstance(bundle.get("full_corpus_chunk"), dict) else {}
        chunk_index = int(chunk.get("chunk_index") or 1)
        evidence_ids = list(chunk.get("evidence_ids") or [])
        is_subchunk = bool(chunk.get("parent_chunk_id"))
        self._record(chunk_index, len(evidence_ids), is_subchunk)
        if not is_subchunk and len(evidence_ids) > 1:
            return ModelResponse(
                provider=self.provider,
                model_name=self.model_name,
                prompt_name=self.prompt_name,
                temperature=self.temperature,
                raw_output=json.dumps({"status": "needs_subchunk", "chunk_index": chunk_index}, sort_keys=True),
                latency_ms=1,
                input_tokens=1,
                output_tokens=1,
                status="timeout",
            )
        return ModelResponse(
            provider=self.provider,
            model_name=self.model_name,
            prompt_name=self.prompt_name,
            temperature=self.temperature,
            raw_output=json.dumps(
                {
                    "schema_version": "claim-result/v1",
                    "agent_role": "adaptive_split_test",
                    "finding_status": "supported",
                    "summary": f"chunk {chunk_index} subchunk reviewed",
                    "claims": [
                        {
                            "claim_type": "support",
                            "claim_text": f"adaptive chunk {chunk_index} evidence reviewed",
                            "evidence_refs": evidence_ids[:1],
                            "missing_evidence": [],
                        }
                    ],
                    "propositions": [],
                },
                sort_keys=True,
            ),
            latency_ms=1,
            input_tokens=1,
            output_tokens=1,
        )

    def _record(self, chunk_index: int, evidence_count: int, is_subchunk: bool) -> None:
        if self.calls is None:
            return
        row = (chunk_index, evidence_count, is_subchunk)
        if self.lock is None:
            self.calls.append(row)
            return
        with self.lock:
            self.calls.append(row)


def _chunk_contract_bundle(item_count: int) -> dict[str, Any]:
    evidence_items = [
        {
            "evidence_id": f"PATTERN-{index:03d}",
            "type": "log_pattern",
            "coverage_class": "singleton",
            "event_type": "rare_event",
            "severity_text": "INFO",
            "count": 1,
            "source_log_count": 1,
            "first_seen": f"2026-06-16T00:{index:02d}:00Z",
            "last_seen": f"2026-06-16T00:{index:02d}:30Z",
            "message_template": f"rare event word {index}",
            "source": "logs_sanitized",
        }
        for index in range(1, item_count + 1)
    ]
    return {
        "schema_version": "evidence_bundle.v1",
        "bundle_type": "sanitized_evidence_bundle",
        "evidence_sha256": "chunk-contract-sha",
        "raw_log_policy": "not_uploaded",
        "service": "chunk-contract",
        "environment": "prod",
        "window_start": "2026-06-16T00:00:00Z",
        "window_end": "2026-06-16T01:00:00Z",
        "evidence_items": evidence_items,
        "evidence_refs": {str(item["evidence_id"]): item for item in evidence_items},
        "signals": [],
    }


def test_provider_chunks_run_in_parallel_and_merge_deterministically(monkeypatch) -> None:
    monkeypatch.setenv("OES_MULTI_AI_EVIDENCE_CHUNK_SIZE", "2")
    monkeypatch.setenv("OES_MULTI_AI_CHUNK_MAX_WORKERS", "3")
    events: list[tuple[int, str, float]] = []
    provider = ChunkBarrierProvider(
        barrier=threading.Barrier(3),
        events=events,
        lock=threading.Lock(),
    )

    envelope = _run_provider_full_corpus(
        _chunk_contract_bundle(6),
        provider,
        SafetyPreflightResult(True, (), "", 0),
    )
    artifact = envelope.artifact

    actions = [action for _, action, _ in events]
    assert actions.count("entered") == 3
    assert actions.count("released") == 3
    assert max(index for index, action in enumerate(actions) if action == "entered") < min(
        index for index, action in enumerate(actions) if action == "released"
    )
    assert artifact["status"] == "ok"
    assert artifact["schema_valid"] is True
    assert [row["chunk_index"] for row in artifact["chunk_results"]] == [1, 2, 3]
    assert [row["chunk_id"] for row in artifact["chunk_results"]] == [
        "chunk-rare-singleton-001",
        "chunk-rare-singleton-002",
        "chunk-rare-singleton-003",
    ]
    assert [row["evidence_item_count"] for row in artifact["chunk_results"]] == [2, 2, 2]
    assert {claim["source_chunk_index"] for claim in artifact["parsed_result"]["claims"]} == {1, 2, 3}
    assert {claim["source_chunk_id"] for claim in artifact["parsed_result"]["claims"]} == {
        "chunk-rare-singleton-001",
        "chunk-rare-singleton-002",
        "chunk-rare-singleton-003",
    }
    assert artifact["full_corpus_coverage"]["coverage_ratio"] == 1.0


def test_chunk_merge_fails_closed_when_any_chunk_fails(monkeypatch) -> None:
    monkeypatch.setenv("OES_MULTI_AI_EVIDENCE_CHUNK_SIZE", "2")
    monkeypatch.setenv("OES_MULTI_AI_CHUNK_MAX_WORKERS", "3")

    envelope = _run_provider_full_corpus(
        _chunk_contract_bundle(6),
        ChunkFailureProvider(),
        SafetyPreflightResult(True, (), "", 0),
    )
    artifact = envelope.artifact

    assert artifact["status"] == "failed"
    assert artifact["schema_valid"] is False
    assert artifact["failure_reason"] == "chunked_full_corpus_provider_error"
    assert artifact["execution_status"] == "provider_error"
    assert artifact["failure_is_not_silent"] is True
    assert artifact["chunk_status_counts"] == {"ok": 2, "provider_error": 1}
    assert artifact["chunk_failure_count"] == 1
    assert [row["status"] for row in artifact["chunk_results"]] == ["ok", "failed", "ok"]
    assert [row["execution_status"] for row in artifact["chunk_results"]] == ["ok", "provider_error", "ok"]
    assert artifact["chunk_results"][1]["last_error_type"] == "provider_error"
    assert artifact["full_corpus_coverage"]["coverage_ratio"] == 1.0


def test_chunk_merge_uses_high_coverage_partial_results(monkeypatch) -> None:
    monkeypatch.setenv("OES_MULTI_AI_EVIDENCE_CHUNK_SIZE", "2")
    monkeypatch.setenv("OES_MULTI_AI_CHUNK_MAX_WORKERS", "5")

    envelope = _run_provider_full_corpus(
        _chunk_contract_bundle(10),
        ChunkFailureProvider(),
        SafetyPreflightResult(True, (), "", 0),
    )
    artifact = envelope.artifact

    assert artifact["status"] == "ok"
    assert artifact["schema_valid"] is True
    assert artifact["chunk_status_counts"] == {"ok": 4, "provider_error": 1}
    assert artifact["chunk_failure_count"] == 1
    assert artifact["partial_chunk_result"] == {
        "usable": True,
        "policy": "schema_valid_success_chunks_are_usable_when_success_ratio_meets_threshold",
        "success_chunk_count": 4,
        "total_chunk_count": 5,
        "success_ratio": 0.8,
        "min_success_ratio": 0.8,
        "failure_count": 1,
    }
    assert {claim["source_chunk_index"] for claim in artifact["parsed_result"]["claims"]} == {1, 3, 4, 5}
    assert artifact["execution_status"] == "ok"


def test_chunk_retry_recovers_transient_chunk_failure(monkeypatch) -> None:
    monkeypatch.setenv("OES_MULTI_AI_EVIDENCE_CHUNK_SIZE", "2")
    monkeypatch.setenv("OES_MULTI_AI_CHUNK_MAX_WORKERS", "3")
    monkeypatch.setenv("OES_MULTI_AI_CHUNK_RETRY_ATTEMPTS", "2")
    calls: dict[int, int] = {}

    envelope = _run_provider_full_corpus(
        _chunk_contract_bundle(6),
        FlakyChunkProvider(calls=calls),
        SafetyPreflightResult(True, (), "", 0),
    )
    artifact = envelope.artifact

    assert artifact["status"] == "ok"
    assert artifact["schema_valid"] is True
    assert [row["status"] for row in artifact["chunk_results"]] == ["ok", "ok", "ok"]
    assert calls[2] == 2
    assert {claim["source_chunk_index"] for claim in artifact["parsed_result"]["claims"]} == {1, 2, 3}


def test_failed_chunk_retries_run_in_parallel(monkeypatch) -> None:
    monkeypatch.setenv("OES_MULTI_AI_EVIDENCE_CHUNK_SIZE", "2")
    monkeypatch.setenv("OES_MULTI_AI_CHUNK_MAX_WORKERS", "3")
    monkeypatch.setenv("OES_MULTI_AI_CHUNK_RETRY_ATTEMPTS", "1")
    events: list[tuple[int, str, float]] = []
    calls: dict[int, int] = {}

    envelope = _run_provider_full_corpus(
        _chunk_contract_bundle(6),
        RetryBarrierProvider(
            calls=calls,
            barrier=threading.Barrier(3),
            events=events,
            lock=threading.Lock(),
        ),
        SafetyPreflightResult(True, (), "", 0),
    )
    artifact = envelope.artifact

    actions = [action for _, action, _ in events]
    assert actions.count("retry_entered") == 3
    assert actions.count("retry_released") == 3
    assert max(index for index, action in enumerate(actions) if action == "retry_entered") < min(
        index for index, action in enumerate(actions) if action == "retry_released"
    )
    assert calls == {1: 2, 2: 2, 3: 2}
    assert artifact["status"] == "ok"
    assert artifact["schema_valid"] is True
    assert [row["status"] for row in artifact["chunk_results"]] == ["ok", "ok", "ok"]


def test_failed_chunk_can_retry_as_adaptive_subchunks(monkeypatch) -> None:
    monkeypatch.setenv("OES_MULTI_AI_EVIDENCE_CHUNK_SIZE", "2")
    monkeypatch.setenv("OES_MULTI_AI_CHUNK_MAX_WORKERS", "4")
    monkeypatch.setenv("OES_MULTI_AI_CHUNK_RETRY_ATTEMPTS", "1")
    monkeypatch.setenv("OES_MULTI_AI_ADAPTIVE_SUBCHUNK_RETRY", "1")
    calls: list[tuple[int, int, bool]] = []

    envelope = _run_provider_full_corpus(
        _chunk_contract_bundle(4),
        AdaptiveSplitProvider(calls=calls, lock=threading.Lock()),
        SafetyPreflightResult(True, (), "", 0),
    )
    artifact = envelope.artifact

    assert artifact["status"] == "ok"
    assert artifact["schema_valid"] is True
    assert [row["status"] for row in artifact["chunk_results"]] == ["ok", "ok"]
    assert [row["adaptive_retry"] for row in artifact["chunk_results"]] == [True, True]
    assert [row["adaptive_subchunk_count"] for row in artifact["chunk_results"]] == [2, 2]
    assert (1, 2, False) in calls
    assert (2, 2, False) in calls
    assert sum(1 for _, evidence_count, is_subchunk in calls if evidence_count == 1 and is_subchunk) == 4
    assert all(
        claim.get("source_subchunk_id")
        for claim in artifact["parsed_result"]["claims"]
    )


def test_adaptive_subchunk_retry_only_splits_size_related_failures(monkeypatch) -> None:
    monkeypatch.setenv("OES_MULTI_AI_ADAPTIVE_SUBCHUNK_RETRY", "1")
    items = _chunk_contract_bundle(2)["evidence_items"]

    assert _adaptive_subchunk_retry_enabled("mistral-agent-platform", items, failure_status="timeout") is True
    assert _adaptive_subchunk_retry_enabled("mistral-agent-platform", items, failure_status="context_length") is True
    assert _adaptive_subchunk_retry_enabled("mistral-agent-platform", items, failure_status="rate_limited") is False
    assert _adaptive_subchunk_retry_enabled("mistral-agent-platform", items, failure_status="schema_invalid") is False
    assert _adaptive_subchunk_retry_enabled("mistral-agent-platform", items, failure_status="provider_error") is False


def test_provider_chunk_ledger_reuses_successful_chunks(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("OES_MULTI_AI_EVIDENCE_CHUNK_SIZE", "2")
    monkeypatch.setenv("OES_MULTI_AI_CHUNK_MAX_WORKERS", "2")
    bundle = _chunk_contract_bundle(4)
    ledger = _provider_chunk_ledger_for_output_dir(tmp_path)
    first_calls: list[int] = []

    first = _run_provider_full_corpus(
        bundle,
        CountingChunkProvider(
            calls=first_calls,
            lock=threading.Lock(),
            cache_reuse_policy="allowed",
        ),
        SafetyPreflightResult(True, (), "", 0),
        chunk_ledger=ledger,
    )

    assert first.artifact["status"] == "ok"
    assert sorted(first_calls) == [1, 2]
    records = [json.loads(line) for line in (tmp_path / PROVIDER_CHUNK_LEDGER_FILENAME).read_text().splitlines()]
    assert len(records) == 2
    assert {row["status"] for row in records} == {"ok"}
    assert all(row["prompt_sha256"] for row in records)
    assert all(row["artifact"]["schema_valid"] is True for row in records)

    reloaded_ledger = _provider_chunk_ledger_for_output_dir(tmp_path)
    second_calls: list[int] = []
    second = _run_provider_full_corpus(
        bundle,
        CountingChunkProvider(
            calls=second_calls,
            lock=threading.Lock(),
            cache_reuse_policy="allowed",
        ),
        SafetyPreflightResult(True, (), "", 0),
        chunk_ledger=reloaded_ledger,
    )

    assert second.artifact["status"] == "ok"
    assert second_calls == []
    assert [row["status"] for row in second.artifact["chunk_results"]] == ["ok", "ok"]
    assert len((tmp_path / PROVIDER_CHUNK_LEDGER_FILENAME).read_text().splitlines()) == 2


def test_single_chunk_uses_the_same_execution_contract_ledger_path(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("OES_MULTI_AI_EVIDENCE_CHUNK_SIZE", "20")
    bundle = _chunk_contract_bundle(1)
    ledger = _provider_chunk_ledger_for_output_dir(tmp_path)
    first_calls: list[int] = []

    first = _run_provider_full_corpus(
        bundle,
        CountingChunkProvider(calls=first_calls, cache_reuse_policy="allowed"),
        SafetyPreflightResult(True, (), "", 0),
        chunk_ledger=ledger,
    )

    assert first.artifact["status"] == "ok"
    assert first_calls == [1]
    assert len((tmp_path / PROVIDER_CHUNK_LEDGER_FILENAME).read_text().splitlines()) == 1

    reloaded = _provider_chunk_ledger_for_output_dir(tmp_path)
    second_calls: list[int] = []
    second = _run_provider_full_corpus(
        bundle,
        CountingChunkProvider(calls=second_calls, cache_reuse_policy="allowed"),
        SafetyPreflightResult(True, (), "", 0),
        chunk_ledger=reloaded,
    )

    assert second.artifact["status"] == "ok"
    assert second_calls == []
    assert len((tmp_path / PROVIDER_CHUNK_LEDGER_FILENAME).read_text().splitlines()) == 1


def test_provider_chunk_ledger_reuses_failed_chunks_only_when_enabled(monkeypatch) -> None:
    provider = CountingChunkProvider(
        provider="mistral-agent-platform",
        model_name="mistral-small-2503",
        cache_reuse_policy="allowed",
    )
    bundle = _bundle_for_evidence_chunk(
        _chunk_contract_bundle(1),
        evidence_items=_chunk_contract_bundle(1)["evidence_items"],
        chunk_index=1,
        total_chunks=1,
        provider_id=provider.provider,
    )
    execution_contract = build_provider_execution_contract(provider, bundle)
    execution_contract_sha256 = provider_execution_contract_sha256(execution_contract)
    record = {
        "provider_id": provider.provider,
        "model_name": provider.model_name,
        "execution_contract_sha256": execution_contract_sha256,
        "execution_contract": execution_contract,
        "status": "rate_limited",
        "artifact": {"status": "failed", "schema_valid": False},
        "parsed_payload": {"claims": [], "propositions": []},
    }
    ledger = _ProviderChunkLedger(
        records=[record],
        cache={execution_contract_sha256: record},
    )

    monkeypatch.delenv("OES_MULTI_AI_REUSE_FAILED_CHUNK_RECORDS", raising=False)
    monkeypatch.delenv("OES_MULTI_AI_REUSE_FAILED_CHUNK_RECORDS_BY_PROVIDER", raising=False)
    assert ledger.reusable_record(provider.provider, execution_contract) is None

    monkeypatch.setenv("OES_MULTI_AI_REUSE_FAILED_CHUNK_RECORDS", "1")
    assert ledger.reusable_record(provider.provider, execution_contract) == record

    monkeypatch.delenv("OES_MULTI_AI_REUSE_FAILED_CHUNK_RECORDS", raising=False)
    monkeypatch.setenv("OES_MULTI_AI_REUSE_FAILED_CHUNK_RECORDS_BY_PROVIDER", "llama-agent-platform=1")
    assert ledger.reusable_record(provider.provider, execution_contract) is None

    llama_provider = CountingChunkProvider(
        provider="llama-agent-platform",
        model_name="llama-4-maverick-17b-128e-instruct-maas",
        cache_reuse_policy="allowed",
    )
    llama_contract = build_provider_execution_contract(llama_provider, bundle)
    llama_contract_sha256 = provider_execution_contract_sha256(llama_contract)
    llama_record = {
        **record,
        "provider_id": llama_provider.provider,
        "model_name": llama_provider.model_name,
        "execution_contract_sha256": llama_contract_sha256,
        "execution_contract": llama_contract,
    }
    llama_ledger = _ProviderChunkLedger(
        records=[llama_record],
        cache={llama_contract_sha256: llama_record},
    )
    assert llama_ledger.reusable_record(llama_provider.provider, llama_contract) == llama_record


def test_provider_chunk_ledger_cache_key_includes_model_name() -> None:
    bundle = _bundle_for_evidence_chunk(
        _chunk_contract_bundle(1),
        evidence_items=_chunk_contract_bundle(1)["evidence_items"],
        chunk_index=1,
        total_chunks=1,
        provider_id="gemini-enterprise-agent-platform",
    )
    pro_provider = CountingChunkProvider(
        provider="gemini-enterprise-agent-platform",
        model_name="gemini-3.1-pro-preview",
    )
    flash_provider = CountingChunkProvider(
        provider="gemini-enterprise-agent-platform",
        model_name="gemini-3.1-flash-lite",
    )
    pro_contract = build_provider_execution_contract(pro_provider, bundle)
    flash_contract = build_provider_execution_contract(flash_provider, bundle)
    pro_record = {
        "provider_id": "gemini-enterprise-agent-platform",
        "model_name": "gemini-3.1-pro-preview",
        "execution_contract_sha256": provider_execution_contract_sha256(pro_contract),
        "execution_contract": pro_contract,
        "status": "ok",
        "artifact": {"status": "ok", "schema_valid": True},
        "parsed_payload": {"claims": [{"claim_text": "pro output"}], "propositions": []},
    }
    flash_record = {
        **pro_record,
        "model_name": "gemini-3.1-flash-lite",
        "execution_contract_sha256": provider_execution_contract_sha256(flash_contract),
        "execution_contract": flash_contract,
        "parsed_payload": {"claims": [{"claim_text": "flash output"}], "propositions": []},
    }
    ledger = _ProviderChunkLedger(records=[], cache={})
    ledger.append(pro_record)
    ledger.append(flash_record)

    assert (
        ledger.reusable_record(
            "gemini-enterprise-agent-platform",
            pro_contract,
        )["parsed_payload"]["claims"][0]["claim_text"]
        == "pro output"
    )
    assert (
        ledger.reusable_record(
            "gemini-enterprise-agent-platform",
            flash_contract,
        )["parsed_payload"]["claims"][0]["claim_text"]
        == "flash output"
    )
    other_contract = build_provider_execution_contract(
        CountingChunkProvider(
            provider="gemini-enterprise-agent-platform",
            model_name="gemini-other",
        ),
        bundle,
    )
    assert ledger.reusable_record("gemini-enterprise-agent-platform", other_contract) is None


def test_mutable_model_alias_reuses_only_records_created_in_current_run() -> None:
    bundle = _bundle_for_evidence_chunk(
        _chunk_contract_bundle(1),
        evidence_items=_chunk_contract_bundle(1)["evidence_items"],
        chunk_index=1,
        total_chunks=1,
        provider_id="mutable-provider",
    )
    provider = CountingChunkProvider(
        provider="mutable-provider",
        model_name="model-latest",
    )
    execution_contract = build_provider_execution_contract(provider, bundle)
    execution_contract_sha256 = provider_execution_contract_sha256(execution_contract)
    record = {
        "provider_id": provider.provider,
        "model_name": provider.model_name,
        "execution_contract_sha256": execution_contract_sha256,
        "execution_contract": execution_contract,
        "status": "ok",
        "artifact": {"status": "ok", "schema_valid": True},
        "parsed_payload": {"claims": [{"claim_text": "current output"}], "propositions": []},
    }

    reloaded = _ProviderChunkLedger(
        records=[record],
        cache={execution_contract_sha256: record},
    )
    assert reloaded.reusable_record(provider.provider, execution_contract) is None

    current = _ProviderChunkLedger(records=[], cache={})
    current.append(record)
    assert current.reusable_record(provider.provider, execution_contract) == record


def test_v1_chunk_record_cannot_satisfy_v2_cache_lookup() -> None:
    bundle = _bundle_for_evidence_chunk(
        _chunk_contract_bundle(1),
        evidence_items=_chunk_contract_bundle(1)["evidence_items"],
        chunk_index=1,
        total_chunks=1,
        provider_id="provider-a",
    )
    provider = CountingChunkProvider(provider="provider-a", model_name="model-v1")
    execution_contract = build_provider_execution_contract(provider, bundle)
    execution_contract_sha256 = provider_execution_contract_sha256(execution_contract)
    legacy_record = {
        "provider_id": provider.provider,
        "model_name": provider.model_name,
        "execution_contract_sha256": execution_contract_sha256,
        "execution_contract": {
            "schema_version": "provider_execution_contract.v1",
            "provider_id": provider.provider,
            "model_name": provider.model_name,
            "prompt_sha256": "legacy",
        },
        "status": "ok",
        "artifact": {"status": "ok", "schema_valid": True},
        "parsed_payload": {"claims": [{"claim_text": "legacy output"}], "propositions": []},
    }
    ledger = _ProviderChunkLedger(
        records=[legacy_record],
        cache={execution_contract_sha256: legacy_record},
    )

    assert ledger.reusable_record(provider.provider, execution_contract) is None


def test_chunk_failure_classifier_separates_scheduler_and_schema_failures() -> None:
    assert _artifact_execution_status(
        {
            "status": "failed",
            "schema_valid": False,
            "failure_reason": "provider_exception",
            "provider_error": {"message": "HTTP 429 resource exhausted; Retry-After: 17"},
            "retry": {"attempts": 1, "max_attempts": 1, "retryable": True},
        }
    ) == "rate_limited"
    assert _retry_after_seconds_from_text("HTTP 429 resource exhausted; Retry-After: 17") == 17
    assert _artifact_execution_status(
        {
            "status": "ok",
            "schema_valid": False,
            "parse_status": "parsed_original",
            "schema_errors": ["claims[0].evidence_refs is required"],
            "retry": {"attempts": 1, "max_attempts": 1, "retryable": False},
        }
    ) == "schema_invalid"
    assert _artifact_execution_status(
        {
            "status": "ok",
            "schema_valid": False,
            "parse_status": "invalid_after_repair",
            "schema_errors": ["invalid JSON"],
            "retry": {"attempts": 1, "max_attempts": 1, "retryable": False},
        }
    ) == "deterministic_parse_failure"


def test_chunk_bundle_keeps_db_coverage_summary_out_of_row_ledger() -> None:
    bundle = _chunk_contract_bundle(3)
    bundle["db_corpus_coverage"] = {
        "schema_version": "db_corpus_coverage.v1",
        "total_row_count": 3,
        "covered_row_count": 3,
        "uncovered_row_count": 0,
        "coverage_ratio": 1.0,
        "row_assignments_sha256": "ledger-sha",
        "row_assignments": [{"log_id": "row-1"}, {"log_id": "row-2"}, {"log_id": "row-3"}],
    }
    items = list(bundle["evidence_items"])[:2]

    chunk = _bundle_for_evidence_chunk(
        bundle,
        evidence_items=items,
        chunk_index=1,
        total_chunks=2,
    )

    assert "row_assignments" in bundle["db_corpus_coverage"]
    assert chunk["db_corpus_coverage"]["row_assignments_sha256"] == "ledger-sha"
    assert "row_assignments" not in chunk["db_corpus_coverage"]
    assert chunk["evidence_items"] == items
    assert set(chunk["evidence_refs"]) == {"PATTERN-001", "PATTERN-002"}


def test_disagreement_without_agreement_generates_validation_finding() -> None:
    bundle = {"evidence_sha256": "sha", "evidence_refs": {"LOG-1": {"message": "safe"}, "LOG-2": {"message": "safe"}}}
    model_runs = [
        {
            "provider_id": "provider-a",
            "status": "ok",
            "schema_valid": True,
            "parsed_result": {
                "claims": [
                    {
                        "claim_type": "support",
                        "claim_text": "External dependency timeout caused http_5xx.",
                        "core_target_type": "external_dependency_failure",
                        "component": "edge",
                        "evidence_refs": ["LOG-1"],
                        "missing_evidence": ["external dependency status"],
                    }
                ]
            },
            "safety_preflight": {"passed": True},
        },
        {
            "provider_id": "provider-b",
            "status": "ok",
            "schema_valid": True,
            "parsed_result": {
                "claims": [
                    {
                        "claim_type": "support",
                        "claim_text": "Audio delivery user impact is unclear.",
                        "core_target_type": "user_impact_signal_gap",
                        "component": "audio",
                        "evidence_refs": ["LOG-2"],
                        "missing_evidence": ["audio delivery metric"],
                    }
                ]
            },
            "safety_preflight": {"passed": True},
        },
    ]
    synthesis = synthesize_multi_ai(bundle, model_runs)
    assert len(synthesis["agreement_groups"]) == 0
    assert len(synthesis["disagreement_groups"]) == 2
    assert len(synthesis["disagreement_themes"]) >= 2
    finding = finding_impact_from_synthesis(synthesis)
    assert finding["finding"] == "Multi-AI disagreement requires validation"
    assert "No incident-promotion agreement was found" in finding["impact"]
    assert synthesis["finding_summary"] == finding
