from __future__ import annotations

import json
from typing import Any

from ops_evidence_synthesis.synthesis.subsystems import OPS_SUBSYSTEMS


_VALID_SUBSYSTEMS = [*OPS_SUBSYSTEMS, "general"]
_VALID_CLAIM_TYPES = [
    "support",
    "counter_evidence",
    "caveat",
    "validation_target",
    "next_data_needed",
    "insufficient_evidence",
]
_VALID_FINDING_STATUSES = [
    "supported",
    "contradicted",
    "insufficient_evidence",
    "no_finding",
]
_VALID_IDENTITY_VALUES = ["known", "unknown", ""]

CLAIM_RESULT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["schema_version", "summary", "claims"],
    "additionalProperties": True,
    "properties": {
        "schema_version": {"type": "string"},
        "agent_role": {"type": "string"},
        "finding_status": {
            "type": "string",
            "enum": _VALID_FINDING_STATUSES,
        },
        "summary": {"type": "string"},
        "claims": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["claim_type", "claim_text", "evidence_refs"],
                "additionalProperties": True,
                "properties": {
                    "claim_type": {
                        "type": "string",
                        "enum": _VALID_CLAIM_TYPES,
                    },
                    "finding_status": {
                        "type": "string",
                        "enum": _VALID_FINDING_STATUSES,
                    },
                    "claim_text": {"type": "string", "minLength": 1},
                    "subsystem": {
                        "type": "string",
                    },
                    "evidence_refs": {
                        "type": "array",
                        "items": {"type": "string", "minLength": 1},
                    },
                    "counter_evidence_refs": {
                        "type": "array",
                        "items": {"type": "string", "minLength": 1},
                    },
                    "caveats": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "missing_evidence": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "evidence_identity": {
                        "type": "object",
                        "additionalProperties": True,
                        "properties": {
                            "program": {"type": "string"},
                            "source": {"type": "string"},
                            "failure_signature": {"type": "string"},
                            "time_window": {"type": "string"},
                        },
                    },
                    "temporary_action": {"type": "string"},
                    "permanent_action": {"type": "string"},
                    "required_authority": {"type": "string"},
                },
            },
        },
        "propositions": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["question"],
                "properties": {
                    "question": {"type": "string", "minLength": 1},
                    "subsystem": {"type": "string"},
                    "linked_claim_hints": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                },
            },
        },
    },
}


def parse_model_json(raw_output: str) -> tuple[dict[str, Any] | None, tuple[str, ...]]:
    try:
        parsed = json.loads(raw_output)
    except json.JSONDecodeError as exc:
        return None, (f"invalid JSON: line {exc.lineno}, column {exc.colno}: {exc.msg}",)
    if not isinstance(parsed, dict):
        return None, ("top-level model output must be an object",)
    return parsed, ()


def validate_claim_result(payload: dict[str, Any]) -> tuple[bool, tuple[str, ...]]:
    try:
        from jsonschema import Draft202012Validator
    except Exception:
        return _fallback_validate(payload)

    validator = Draft202012Validator(CLAIM_RESULT_SCHEMA)
    errors = sorted(validator.iter_errors(payload), key=lambda error: list(error.path))
    if not errors:
        return True, ()
    messages = []
    for error in errors:
        path = "$" if not error.path else "$." + ".".join(str(item) for item in error.path)
        messages.append(f"{path}: {error.message}")
    return False, tuple(messages)


def _fallback_validate(payload: dict[str, Any]) -> tuple[bool, tuple[str, ...]]:
    errors: list[str] = []
    if not isinstance(payload.get("schema_version"), str):
        errors.append("$.schema_version: required string")
    if not isinstance(payload.get("summary"), str):
        errors.append("$.summary: required string")
    claims = payload.get("claims")
    if not isinstance(claims, list):
        errors.append("$.claims: required array")
    else:
        allowed = set(_VALID_CLAIM_TYPES)
        valid_statuses = set(_VALID_FINDING_STATUSES)
        if payload.get("finding_status") and payload.get("finding_status") not in valid_statuses:
            errors.append("$.finding_status: unsupported value")
        for index, claim in enumerate(claims):
            if not isinstance(claim, dict):
                errors.append(f"$.claims.{index}: must be object")
                continue
            if claim.get("claim_type") not in allowed:
                errors.append(f"$.claims.{index}.claim_type: unsupported value")
            if not isinstance(claim.get("claim_text"), str) or not claim.get("claim_text"):
                errors.append(f"$.claims.{index}.claim_text: required string")
            if not isinstance(claim.get("evidence_refs"), list):
                errors.append(f"$.claims.{index}.evidence_refs: required array")
            if claim.get("subsystem") is not None and not isinstance(claim.get("subsystem"), str):
                errors.append(f"$.claims.{index}.subsystem: must be string")
            if claim.get("finding_status") and claim.get("finding_status") not in valid_statuses:
                errors.append(f"$.claims.{index}.finding_status: unsupported value")
            identity = claim.get("evidence_identity")
            if identity is not None:
                if not isinstance(identity, dict):
                    errors.append(f"$.claims.{index}.evidence_identity: must be object")
                else:
                    for key in ("program", "source", "failure_signature", "time_window"):
                        if key in identity and not isinstance(identity.get(key), str):
                            errors.append(f"$.claims.{index}.evidence_identity.{key}: must be string")
    return not errors, tuple(errors)


def valid_evidence_refs(bundle: dict[str, Any], refs: list[str] | tuple[str, ...]) -> bool:
    known = set((bundle.get("evidence_refs") or {}).keys())
    return all(ref in known for ref in refs)


def evidence_ref_errors(bundle: dict[str, Any], refs: list[str] | tuple[str, ...]) -> tuple[str, ...]:
    known = set((bundle.get("evidence_refs") or {}).keys())
    return tuple(ref for ref in refs if ref not in known)
