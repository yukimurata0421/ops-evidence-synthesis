from __future__ import annotations

import json
from pathlib import Path

from ops_evidence_synthesis.ai.prompts import compact_bundle_for_model
from ops_evidence_synthesis.event_semantics import (
    classify_event_semantics,
    enrich_evidence_item_semantics,
)
from ops_evidence_synthesis.local_first import build_bundle_from_sanitized, build_evidence_items
from ops_evidence_synthesis.synthesis.multi_ai import (
    _semantic_evidence_buckets,
    _semantic_key_for_item,
)


def test_structured_event_fields_take_priority_over_closed_generic_taxonomy() -> None:
    semantics = classify_event_semantics(
        "delivery failed",
        "ERROR",
        {
            "error_type": "tls_certificate_failure",
            "error_code": "CERT_VERIFY_FAILED",
            "exception_class": "ssl.SSLCertVerificationError",
        },
        template="delivery failed",
    )

    assert semantics["event_family"] == "network"
    assert semantics["event_name"] == "tls_certificate_failure"
    assert semantics["error_code"] == "CERT_VERIFY_FAILED"
    assert semantics["exception_class"] == "ssl_sslcertverificationerror"
    assert semantics["protocol"] == "tls"
    assert semantics["classification_source"] == "structured_field"


def test_approved_profile_can_classify_non_english_product_specific_event() -> None:
    item = {
        "event_type": "unknown",
        "component": "sender",
        "message_template": "証明書の検証に失敗しました",
        "severity_text": "ERROR",
    }
    rules = [
        {
            "id": "transport-tls-certificate",
            "match": {"component": "sender", "message_contains": "証明書"},
            "event_family": "network",
            "event_name": "tls_certificate_failure",
            "subsystem": "transport_sender",
            "confidence": 0.97,
        }
    ]

    unapproved = enrich_evidence_item_semantics(
        item,
        profile_event_semantics=rules,
        profile_approved=False,
    )
    approved = enrich_evidence_item_semantics(
        item,
        profile_event_semantics=rules,
        profile_approved=True,
    )

    assert unapproved["event_name"] == "unknown"
    assert unapproved["classification_source"] == "template_fingerprint"
    assert approved["event_family"] == "network"
    assert approved["event_name"] == "tls_certificate_failure"
    assert approved["subsystem"] == "transport_sender"
    assert approved["classification_source"] == "approved_profile:transport_tls_certificate"
    assert approved["classification_confidence"] == 0.97
    assert approved["semantic_rule_trust"] == "human_approved"
    assert approved["generic_classification"] == {
        "event_family": "general",
        "event_name": "unknown",
        "classification_source": "template_fingerprint",
        "classification_confidence": 0.55,
        "protocol": "",
        "error_code": "",
        "exception_class": "",
        "template_fingerprint": approved["template_fingerprint"],
    }
    assert approved["profile_override"] == {
        "rule_id": "transport_tls_certificate",
        "semantic_rule_trust": "human_approved",
        "event_family": "network",
        "event_name": "tls_certificate_failure",
        "subsystem": "transport_sender",
        "classification_confidence": 0.97,
    }


def test_unapproved_semantic_rule_trust_cannot_apply_override() -> None:
    item = {
        "component": "sender",
        "message_template": "certificate validation failed",
        "severity_text": "ERROR",
    }
    rules = [
        {
            "id": "force-healthy",
            "match": {"component": "sender"},
            "event_family": "state",
            "event_name": "healthy",
        }
    ]

    enriched = enrich_evidence_item_semantics(
        item,
        profile_event_semantics=rules,
        semantic_rule_trust="unapproved",
    )

    assert enriched["semantic_rule_trust"] == "unapproved"
    assert enriched["event_name"] != "healthy"
    assert "profile_override" not in enriched


def test_unknown_templates_have_distinct_audit_keys_but_share_coarse_packing_bucket() -> None:
    first = enrich_evidence_item_semantics(
        {
            "evidence_id": "PATTERN-001",
            "coverage_class": "pattern",
            "component": "worker",
            "event_type": "unknown",
            "severity_text": "ERROR",
            "message_template": "キューの整合性検査に失敗しました",
        }
    )
    second = enrich_evidence_item_semantics(
        {
            "evidence_id": "PATTERN-002",
            "coverage_class": "pattern",
            "component": "worker",
            "event_type": "unknown",
            "severity_text": "ERROR",
            "message_template": "キャッシュ世代の切り替えに失敗しました",
        }
    )

    assert first["template_fingerprint"] != second["template_fingerprint"]
    assert _semantic_key_for_item(first) != _semantic_key_for_item(second)
    assert len(_semantic_evidence_buckets([first, second])) == 1


def test_structured_error_codes_prevent_evidence_item_collapse() -> None:
    base = {
        "event_type": "dependency_error",
        "event_family": "dependency",
        "event_name": "dependency_error",
        "severity_text": "error",
        "message_template": "dependency request failed",
        "component": "worker",
        "source_system": "jsonl",
        "classification_source": "structured_field",
        "classification_confidence": 0.98,
        "protocol": "grpc",
        "template_fingerprint": "tmpl-fixed",
    }
    events = [
        {**base, "error_code": "RESOURCE_EXHAUSTED", "timestamp": "2026-07-17T00:00:00Z", "event_id": "EV-1"},
        {**base, "error_code": "UNAVAILABLE", "timestamp": "2026-07-17T00:00:01Z", "event_id": "EV-2"},
    ]

    items = build_evidence_items(events)

    assert len(items) == 2
    assert {item["error_code"] for item in items} == {"RESOURCE_EXHAUSTED", "UNAVAILABLE"}


def test_explicit_profile_semantics_reach_bundle_and_model_input(tmp_path: Path) -> None:
    events_path = tmp_path / "sanitized_events.jsonl"
    events_path.write_text(
        json.dumps(
            {
                "timestamp": "2026-07-17T00:00:00Z",
                "source_system": "jsonl",
                "event_type": "unknown",
                "severity_text": "error",
                "message_template": "証明書の検証に失敗しました",
                "message_sanitized": "証明書の検証に失敗しました",
                "component": "sender",
                "event_id": "EV-1",
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    profile_path = tmp_path / "approved_profile.json"
    profile_path.write_text(
        json.dumps(
            {
                "profile_id": "mailer",
                "profile_label": "Mailer",
                "event_semantics": [
                    {
                        "id": "transport-tls-certificate",
                        "match": {"component": "sender", "message_contains": "証明書"},
                        "event_family": "network",
                        "event_name": "tls_certificate_failure",
                        "subsystem": "transport_sender",
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    bundle = build_bundle_from_sanitized(
        events_path,
        service="mailer",
        environment="prod",
        start="2026-07-17T00:00:00Z",
        end="2026-07-17T00:10:00Z",
        profile_name=str(profile_path),
        out_path=tmp_path / "bundle.json",
    )
    item = bundle["evidence_items"][0]
    compact_item = compact_bundle_for_model(bundle)["evidence_items"][0]

    assert bundle["source"]["profile_confidence"] == "explicit"
    assert item["event_name"] == "tls_certificate_failure"
    assert item["subsystem"] == "transport_sender"
    assert compact_item["event_family"] == "network"
    assert compact_item["classification_source"] == "approved_profile:transport_tls_certificate"
    assert compact_item["template_fingerprint"].startswith("tmpl-")
