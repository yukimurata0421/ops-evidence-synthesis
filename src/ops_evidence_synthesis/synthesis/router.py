from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ops_evidence_synthesis.canonical import sha256_json
from ops_evidence_synthesis.models import ClaimRecord, ParsedResultRecord, PropositionRecord
from ops_evidence_synthesis.profiles import profile_for_bundle, title_for_target_type
from ops_evidence_synthesis.synthesis.structured_evidence import build_structured_evidence
from ops_evidence_synthesis.synthesis.subsystems import (
    OPS_SUBSYSTEMS,
    question_for_subsystem,
    subsystem_for_claim,
    subsystem_for_text,
)
from ops_evidence_synthesis.synthesis.validation import valid_evidence_refs
from ops_evidence_synthesis.timeutils import utc_now


@dataclass(frozen=True, slots=True)
class RoutingResult:
    claims: tuple[ClaimRecord, ...]
    propositions: tuple[PropositionRecord, ...]


def _string_list(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(str(item) for item in value if str(item).strip())


def _finding_status(raw_claim: dict[str, Any], *, result_status: str) -> str:
    status = str(raw_claim.get("finding_status") or result_status or "").strip().casefold()
    claim_type = str(raw_claim.get("claim_type") or "").strip().casefold()
    if claim_type == "insufficient_evidence":
        return "insufficient_evidence"
    if status in {"supported", "contradicted", "insufficient_evidence", "no_finding"}:
        return status
    if claim_type == "counter_evidence":
        return "contradicted"
    return "supported"


def _evidence_identity(raw_claim: dict[str, Any]) -> dict[str, Any]:
    raw_identity = raw_claim.get("evidence_identity")
    if not isinstance(raw_identity, dict):
        return {}
    identity: dict[str, Any] = {}
    for key in ("program", "source", "failure_signature", "time_window"):
        value = str(raw_identity.get(key) or "").strip().casefold()
        if value in {"known", "unknown"}:
            identity[key] = value
        elif value:
            identity[key] = "known"
    return identity


def _model_subsystem(raw_value: Any) -> str:
    value = str(raw_value or "").strip()
    if value in OPS_SUBSYSTEMS or value == "general":
        return value
    mapped = subsystem_for_text(value)
    return mapped if mapped != "general" else "general"


def _cause_key(text: str) -> str:
    folded = text.casefold()
    if (
        "can't open file" in folded
        or "no such file or directory" in folded
        or "configured command missing" in folded
        or "job configuration mismatch" in folded
    ):
        return "job_configuration"
    if "rtmps" in folded or "ffmpeg" in folded or "send-path" in folded:
        return "stream_transport"
    if "youtube" in folded or "resolver" in folded:
        return "youtube_live_health"
    if "watchdog" in folded:
        return "service_health"
    if "stream_v3" in folded or "subsystems_status" in folded or "pod restart" in folded or "service health" in folded:
        return "service_health"
    if "connection pool" in folded or "too many connections" in folded:
        return "database_connection_pool"
    if "database" in folded and "timeout" in folded:
        return "database_timeout"
    if "payment" in folded or "downstream" in folded or "dependency" in folded:
        return "downstream_dependency"
    if "deploy" in folded or "release" in folded or "rollout" in folded:
        return "deployment_regression"
    if "auth" in folded or "permission" in folded:
        return "auth_config"
    return "general_incident_review"


def _question_for_key(key: str) -> str:
    questions = {
        "database_connection_pool": "Should humans review database connection pool saturation as the incident driver?",
        "database_timeout": "Should humans review database timeout regression as the incident driver?",
        "downstream_dependency": "Should humans review downstream dependency behavior as a competing explanation?",
        "deployment_regression": "Should humans validate the latest deployment as a causal change?",
        "auth_config": "Should humans review authorization configuration as the incident driver?",
        "job_configuration": "Should humans review missing configured job commands or supervisor configuration drift?",
        "stream_transport": "Should humans review RTMPS transport or ffmpeg send-path instability?",
        "youtube_live_health": "Should humans review YouTube live health or API evidence instability?",
        "service_health": "Should humans review service health, restart, or recovery failure?",
        "general_incident_review": "What incident hypothesis needs human review first?",
    }
    return questions[key]


def _priority(
    linked_claims: list[ClaimRecord],
    support_summary: str,
    counter_summary: str,
    validation_targets: tuple[str, ...],
) -> str:
    invalid_count = sum(1 for claim in linked_claims if not claim.evidence_refs_valid)
    action_count = sum(1 for claim in linked_claims if claim.temporary_action or claim.permanent_action)
    if invalid_count or counter_summary or len(validation_targets) >= 2:
        return "high"
    if action_count or support_summary:
        return "medium"
    return "low"


def route_claims(bundle: dict[str, Any], parsed_results: list[ParsedResultRecord]) -> RoutingResult:
    now = utc_now()
    claims: list[ClaimRecord] = []
    claims.extend(_rule_claims_from_candidate_targets(bundle, now=now))
    for result in parsed_results:
        if not result.schema_valid:
            continue
        result_status = str(result.parsed_json.get("finding_status") or "").strip()
        raw_claims = result.parsed_json.get("claims") or []
        if not isinstance(raw_claims, list):
            continue
        for index, raw_claim in enumerate(raw_claims, start=1):
            if not isinstance(raw_claim, dict):
                continue
            evidence_refs = _string_list(raw_claim.get("evidence_refs"))
            counter_refs = _string_list(raw_claim.get("counter_evidence_refs"))
            claim_text = str(raw_claim.get("claim_text", "")).strip()
            finding_status = _finding_status(raw_claim, result_status=result_status)
            model_subsystem = _model_subsystem(raw_claim.get("subsystem"))
            computed_subsystem = subsystem_for_claim(
                bundle,
                claim_text,
                (*evidence_refs, *counter_refs),
            )
            subsystem = computed_subsystem if computed_subsystem != "general" else model_subsystem
            claim_id = "claim-" + sha256_json(
                {
                    "evidence_sha256": result.evidence_sha256,
                    "result_id": result.result_id,
                    "provider": result.provider,
                    "index": index,
                    "claim_text": claim_text,
                }
            )[:16]
            refs_valid = valid_evidence_refs(bundle, evidence_refs) and valid_evidence_refs(bundle, counter_refs)
            claims.append(
                ClaimRecord(
                    claim_id=claim_id,
                    evidence_sha256=result.evidence_sha256,
                    result_id=result.result_id,
                    provider=result.provider,
                    claim_type=str(raw_claim.get("claim_type", "support")),
                    claim_text=claim_text,
                    evidence_refs=evidence_refs,
                    counter_evidence_refs=counter_refs,
                    caveats=_string_list(raw_claim.get("caveats")),
                    missing_evidence=_string_list(raw_claim.get("missing_evidence")),
                    temporary_action=str(raw_claim.get("temporary_action", "")).strip(),
                    permanent_action=str(raw_claim.get("permanent_action", "")).strip(),
                    required_authority=str(raw_claim.get("required_authority", "")).strip(),
                    review_status="pending",
                    created_at=now,
                    evidence_refs_valid=refs_valid,
                    subsystem=subsystem,
                    finding_status=finding_status,
                    evidence_identity=_evidence_identity(raw_claim),
                )
            )

    grouped: dict[str, list[ClaimRecord]] = {}
    for claim in claims:
        key = _claim_group_key(claim)
        grouped.setdefault(key, []).append(claim)

    propositions: list[PropositionRecord] = []
    for key, linked_claims in sorted(grouped.items()):
        subsystem = _dominant_subsystem(linked_claims)
        core_target_type = _core_target_type_for_group(linked_claims)
        support_claims = [claim for claim in linked_claims if claim.claim_type == "support"]
        counter_claims = [claim for claim in linked_claims if claim.claim_type == "counter_evidence"]
        validation_claims = [
            claim
            for claim in linked_claims
            if claim.claim_type in {"validation_target", "caveat"} or claim.missing_evidence
        ]
        next_data_claims = [
            claim
            for claim in linked_claims
            if claim.claim_type == "next_data_needed" or claim.missing_evidence
        ]
        support_summary = " ".join(claim.claim_text for claim in support_claims)[:1200]
        counter_summary = " ".join(claim.claim_text for claim in counter_claims)[:1200]
        validation_targets = tuple(claim.claim_text for claim in validation_claims)
        next_data_needed = tuple(
            item
            for claim in next_data_claims
            for item in (claim.missing_evidence or (claim.claim_text,))
        )
        question = (
            _question_for_core_target(bundle, core_target_type, subsystem)
            if core_target_type
            else question_for_subsystem(subsystem)
            if subsystem in OPS_SUBSYSTEMS
            else _question_for_key(_cause_key(" ".join(claim.claim_text for claim in linked_claims)))
        )
        proposition_id = "prop-" + sha256_json(
            {
                "evidence_sha256": bundle["evidence_sha256"],
                "question": question,
                "subsystem": subsystem,
                "core_target_type": core_target_type,
                "linked_claim_ids": [claim.claim_id for claim in linked_claims],
            }
        )[:16]
        propositions.append(
            PropositionRecord(
                proposition_id=proposition_id,
                evidence_sha256=bundle["evidence_sha256"],
                question=question,
                linked_claim_ids=tuple(claim.claim_id for claim in linked_claims),
                support_summary=support_summary,
                counter_summary=counter_summary,
                validation_targets=validation_targets,
                next_data_needed=tuple(dict.fromkeys(next_data_needed)),
                priority=_priority(linked_claims, support_summary, counter_summary, validation_targets),
                review_status="pending",
                created_at=now,
                subsystem=subsystem,
                structured_evidence=build_structured_evidence(bundle, linked_claims),
            )
        )

    return RoutingResult(claims=tuple(claims), propositions=tuple(propositions))


def _rule_claims_from_candidate_targets(bundle: dict[str, Any], *, now: str) -> list[ClaimRecord]:
    claims: list[ClaimRecord] = []
    for target in bundle.get("candidate_targets") or []:
        if not isinstance(target, dict):
            continue
        review_mode = str(target.get("review_mode") or "")
        if review_mode not in {"incident_candidate", "validation_target"}:
            continue
        evidence_refs = tuple(str(ref) for ref in target.get("support_evidence_refs") or [] if str(ref).strip())
        if not evidence_refs:
            continue
        core_target_type = str(target.get("core_target_type") or "")
        target_id = str(target.get("target_id") or core_target_type or "candidate")
        claim_id = "claim-" + sha256_json(
            {
                "evidence_sha256": bundle.get("evidence_sha256"),
                "target_id": target_id,
                "core_target_type": core_target_type,
                "evidence_refs": evidence_refs,
            }
        )[:16]
        missing_evidence = tuple(str(item) for item in target.get("missing_evidence") or [] if str(item).strip())
        caveats = tuple(
            str(item)
            for item in (
                [
                    "This rule-based target is a review routing hint, not a final root-cause judgement.",
                    *(
                        ["Treat this as validation work for the primary candidate."]
                        if review_mode == "validation_target"
                        else []
                    ),
                ]
            )
            if item
        )
        claims.append(
            ClaimRecord(
                claim_id=claim_id,
                evidence_sha256=str(bundle["evidence_sha256"]),
                result_id=f"rule-{target_id}",
                provider="rule-engine",
                claim_type="support",
                claim_text=str(target.get("core_claim") or target.get("title") or ""),
                evidence_refs=evidence_refs,
                counter_evidence_refs=(),
                caveats=caveats,
                missing_evidence=missing_evidence,
                temporary_action=str(target.get("temporary_action") or ""),
                permanent_action=str(target.get("permanent_action") or ""),
                required_authority=str(target.get("required_authority") or "on-call engineer"),
                review_status="pending",
                created_at=now,
                evidence_refs_valid=valid_evidence_refs(bundle, evidence_refs),
                subsystem=str(target.get("subsystem") or "general"),
                finding_status="supported",
                evidence_identity={
                    "program": "known" if target.get("subsystem") else "unknown",
                    "source": "known",
                    "failure_signature": "known",
                    "time_window": "known",
                    "core_target_type": core_target_type,
                    "review_mode": review_mode,
                    "target_id": target_id,
                    "signal_ids": list(target.get("signal_ids") or []),
                    "relationship": str(target.get("relationship") or ""),
                    "parent_target_id": str(target.get("parent_target_id") or ""),
                },
            )
        )
    return claims


def _claim_group_key(claim: ClaimRecord) -> str:
    core_target_type = str((claim.evidence_identity or {}).get("core_target_type") or "")
    if core_target_type:
        return f"{claim.subsystem or 'general'}:{core_target_type}"
    if claim.subsystem and claim.subsystem != "general":
        return claim.subsystem
    return _cause_key(claim.claim_text)


def _core_target_type_for_group(claims: list[ClaimRecord]) -> str:
    for claim in claims:
        value = str((claim.evidence_identity or {}).get("core_target_type") or "")
        if value:
            return value
    return ""


def _question_for_core_target(bundle: dict[str, Any], core_target_type: str, subsystem: str) -> str:
    profile_id = str(profile_for_bundle(bundle).get("profile_id") or "generic")
    title = title_for_target_type(core_target_type, profile_id, default=core_target_type.replace("_", " ").title())
    if core_target_type == "instrumentation_mismatch":
        return "Should humans validate metric/log consistency before accepting or rejecting the primary incident?"
    if core_target_type in {"monitoring_gap", "observability_contract_mismatch"}:
        return f"Should humans validate {title.lower()}?"
    return f"Should humans review {title.lower()}?"


def _dominant_subsystem(claims: list[ClaimRecord]) -> str:
    counts: dict[str, int] = {}
    for claim in claims:
        counts[claim.subsystem] = counts.get(claim.subsystem, 0) + 1
    if not counts:
        return "general"
    return sorted(counts.items(), key=lambda item: (-item[1], item[0]))[0][0]
