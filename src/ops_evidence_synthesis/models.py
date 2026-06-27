from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ops_evidence_synthesis.timeutils import format_timestamp


SEVERITY_ORDER: dict[str, int] = {
    "DEBUG": 10,
    "INFO": 20,
    "NOTICE": 25,
    "WARN": 30,
    "WARNING": 30,
    "ERROR": 40,
    "CRITICAL": 50,
    "ALERT": 60,
    "EMERGENCY": 70,
}


def severity_rank(severity: str) -> int:
    return SEVERITY_ORDER.get(severity.upper(), 0)


@dataclass(frozen=True, slots=True)
class IncidentWindow:
    service: str
    environment: str
    incident_start: str
    incident_end: str
    lookback_minutes: int = 60

    def normalized(self) -> "IncidentWindow":
        return IncidentWindow(
            service=self.service,
            environment=self.environment,
            incident_start=format_timestamp(self.incident_start),
            incident_end=format_timestamp(self.incident_end),
            lookback_minutes=self.lookback_minutes,
        )


@dataclass(frozen=True, slots=True)
class RawLog:
    timestamp: str
    service: str
    environment: str
    severity: str
    message: str
    trace_id: str = ""
    span_id: str = ""
    deploy_id: str = ""
    version: str = ""
    resource_type: str = ""
    labels: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, payload: dict[str, Any]) -> "RawLog":
        return cls(
            timestamp=format_timestamp(str(payload["timestamp"])),
            service=str(payload["service"]),
            environment=str(payload.get("environment", "prod")),
            severity=str(payload.get("severity", "INFO")).upper(),
            message=str(payload.get("message", "")),
            trace_id=str(payload.get("trace_id", "")),
            span_id=str(payload.get("span_id", "")),
            deploy_id=str(payload.get("deploy_id", "")),
            version=str(payload.get("version", "")),
            resource_type=str(payload.get("resource_type", "")),
            labels=dict(payload.get("labels") or {}),
        )


@dataclass(frozen=True, slots=True)
class SanitizedLog:
    log_id: str
    timestamp: str
    service: str
    environment: str
    severity: str
    trace_id: str
    span_id: str
    deploy_id: str
    version: str
    message_sanitized: str
    message_template: str
    error_type: str
    stack_hash: str
    resource_type: str
    labels_json: dict[str, Any]
    raw_log_sha256: str
    sanitizer_version: str


@dataclass(frozen=True, slots=True)
class ModelRunRecord:
    run_id: str
    evidence_sha256: str
    prompt_sha256: str
    model_input_sha256: str
    provider: str
    model_name: str
    temperature: float
    raw_output: str
    raw_output_sha256: str
    latency_ms: int
    input_tokens: int
    output_tokens: int
    status: str
    created_at: str


@dataclass(frozen=True, slots=True)
class ParsedResultRecord:
    result_id: str
    run_id: str
    evidence_sha256: str
    provider: str
    parsed_json: dict[str, Any]
    parsed_json_sha256: str
    schema_valid: bool
    schema_errors: tuple[str, ...]
    created_at: str


@dataclass(frozen=True, slots=True)
class ClaimRecord:
    claim_id: str
    evidence_sha256: str
    result_id: str
    provider: str
    claim_type: str
    claim_text: str
    evidence_refs: tuple[str, ...]
    counter_evidence_refs: tuple[str, ...]
    caveats: tuple[str, ...]
    missing_evidence: tuple[str, ...]
    temporary_action: str
    permanent_action: str
    required_authority: str
    review_status: str
    created_at: str
    evidence_refs_valid: bool
    subsystem: str = "general"
    finding_status: str = "supported"
    evidence_identity: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class PropositionRecord:
    proposition_id: str
    evidence_sha256: str
    question: str
    linked_claim_ids: tuple[str, ...]
    support_summary: str
    counter_summary: str
    validation_targets: tuple[str, ...]
    next_data_needed: tuple[str, ...]
    priority: str
    review_status: str
    created_at: str
    subsystem: str = "general"
    structured_evidence: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class PropositionClusterRecord:
    cluster_id: str
    evidence_sha256: str
    subsystem: str
    claim_signature: str
    representative_proposition_id: str
    member_proposition_ids: tuple[str, ...]
    supporting_providers: tuple[str, ...]
    model_names: tuple[str, ...]
    core_claim: str
    disagreement_summary: str
    review_status: str
    review_visibility: str
    review_priority_score: float
    cluster_json: dict[str, Any]
    created_at: str


@dataclass(frozen=True, slots=True)
class ScoreRecord:
    score_id: str
    proposition_id: str
    schema_score: float
    evidence_ref_score: float
    unsupported_claim_penalty: float
    contradiction_penalty: float
    cross_model_agreement: float
    actionability_score: float
    safety_score: float
    review_priority_score: float
    created_at: str
