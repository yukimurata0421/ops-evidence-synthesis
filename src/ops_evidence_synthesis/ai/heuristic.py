from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any

from ops_evidence_synthesis.ai.base import ModelResponse


def _bundle_text(bundle: dict[str, Any]) -> str:
    parts: list[str] = []
    for section in ("logs", "log_patterns", "metric_windows", "operational_evidence", "deployments"):
        for item in bundle.get(section, []):
            parts.append(json.dumps(item, ensure_ascii=False, sort_keys=True))
    return "\n".join(parts).casefold()


def _refs_for(bundle: dict[str, Any], *needles: str, limit: int = 4) -> list[str]:
    refs: list[str] = []
    lowered = [needle.casefold() for needle in needles]
    for ref, payload in (bundle.get("evidence_refs") or {}).items():
        text = json.dumps(payload, ensure_ascii=False, sort_keys=True).casefold()
        if any(needle in text for needle in lowered):
            refs.append(ref)
        if len(refs) >= limit:
            break
    if refs:
        return refs
    return list((bundle.get("evidence_refs") or {}).keys())[:limit]


def _primary_cause(bundle: dict[str, Any]) -> str:
    text = _bundle_text(bundle)
    if "can't open file" in text or "no such file or directory" in text or "job_configuration_mismatch" in text:
        return "configured job command or supervisor script is missing"
    if "failed to start" in text and ".service" in text:
        return "service start failure under supervisor control"
    if "stream_transport" in text or "ffmpeg tcp send sample" in text or "rtmps" in text:
        return "RTMPS transport or ffmpeg send-path instability"
    if "youtube_health" in text or "youtube" in text and "watchdog" in text:
        return "YouTube live health or API evidence instability"
    if "service_health_failure" in text or "healthy=false" in text or "subsystems_status" in text:
        return "service health or recovery failure"
    if "connection_pool_exhausted" in text or "too many connections" in text or "connection pool" in text:
        return "database connection pool saturation"
    if "database_timeout" in text:
        return "database timeout regression"
    if _metric_current(bundle, "http_5xx_count") > 0 or "http 500" in text:
        return "HTTP 5xx regression"
    if "dependency_timeout" in text:
        return "downstream dependency timeout"
    if "runtime_restart" in text or "crashloop" in text:
        return "runtime restart loop"
    if "auth_failure" in text:
        return "authorization configuration failure"
    return "unknown incident driver"


def _metric_current(bundle: dict[str, Any], metric_name: str) -> float:
    for item in bundle.get("metric_windows", []):
        if item.get("metric_name") == metric_name:
            try:
                return float(item.get("current_value") or 0)
            except (TypeError, ValueError):
                return 0.0
    return 0.0


def _has_deploy(bundle: dict[str, Any]) -> bool:
    return bool(bundle.get("deployments"))


def _common_context(bundle: dict[str, Any]) -> str:
    deploys = ", ".join(item.get("deploy_id", "") for item in bundle.get("deployments", []) if item.get("deploy_id"))
    suffix = f" Deployments in scope: {deploys}." if deploys else ""
    return (
        f"{bundle['service']} in {bundle['environment']} from {bundle['window_start']} "
        f"to {bundle['window_end']}."
        f"{suffix}"
    )


def _telemetry_missing_for(cause: str) -> list[str]:
    cause_text = cause.casefold()
    if "configured job" in cause_text or "supervisor script" in cause_text or "service start failure" in cause_text:
        return [
            "current systemd unit file and ExecStart target existence.",
            "deployment or install step that should have created the missing script.",
            "timer history and last successful execution timestamp.",
        ]
    if "rtmps" in cause_text or "ffmpeg" in cause_text:
        return [
            "RTMPS socket retransmit and notsent history.",
            "ffmpeg stderr around the event window.",
            "YouTube ingest health and streamStatus timeline.",
        ]
    if "youtube" in cause_text:
        return [
            "YouTube watch page probe result history.",
            "Data API lifecycle transition history.",
            "Resolver cache age and candidate video lineage.",
        ]
    if "service health" in cause_text or "recovery failure" in cause_text:
        return [
            "service restart or supervisor events.",
            "subsystem health snapshots.",
            "control loop recovery action log.",
        ]
    return ["Database pool metrics and dependency saturation metrics."]


def _permanent_action_for(cause: str) -> str:
    cause_text = cause.casefold()
    if "configured job" in cause_text or "supervisor script" in cause_text or "service start failure" in cause_text:
        return "Make supervisor unit paths part of deploy validation and emit job configuration contract checks."
    if "rtmps" in cause_text or "ffmpeg" in cause_text:
        return "Route RTMPS transport counters and ffmpeg stderr into the evidence lake."
    if "youtube" in cause_text:
        return "Persist YouTube resolver, watchdog, and API-cost evidence as first-class bundle inputs."
    if "service health" in cause_text or "recovery failure" in cause_text:
        return "Route service health snapshots, recovery actions, and supervisor events into the evidence lake."
    return "Add connection pool saturation alerting and deploy guardrails."


@dataclass(frozen=True, slots=True)
class HeuristicProvider:
    provider: str
    model_name: str
    prompt_name: str
    temperature: float = 0.0

    def run(self, bundle: dict[str, Any]) -> ModelResponse:
        started = time.perf_counter()
        payload = self._payload(bundle)
        raw_output = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        return ModelResponse(
            provider=self.provider,
            model_name=self.model_name,
            prompt_name=self.prompt_name,
            temperature=self.temperature,
            raw_output=raw_output,
            latency_ms=max(1, elapsed_ms),
            input_tokens=max(1, len(json.dumps(bundle)) // 4),
            output_tokens=max(1, len(raw_output) // 4),
        )

    def _payload(self, bundle: dict[str, Any]) -> dict[str, Any]:
        if self.prompt_name == "evidence-requirements" or bundle.get("llm_task") == "evidence_requirement_planner":
            return self._evidence_requirements_payload(bundle)
        if self.prompt_name == "root-cause":
            return self._root_cause_payload(bundle)
        if self.prompt_name == "verifier":
            return self._verifier_payload(bundle)
        return self._contrast_payload(bundle)

    def _evidence_requirements_payload(self, bundle: dict[str, Any]) -> dict[str, Any]:
        context = bundle.get("requirement_context") if isinstance(bundle.get("requirement_context"), dict) else {}
        requirements: list[dict[str, Any]] = []
        allowed_signals = [
            str(item)
            for item in context.get("allowed_signal_names") or []
            if str(item)
        ]
        for index, target in enumerate(context.get("validation_targets") or [], start=1):
            if not isinstance(target, dict):
                continue
            reasons = [str(item) for item in target.get("promotion_blocked_reasons") or [] if str(item)]
            reason = reasons[0] if reasons else "missing_evidence"
            request_type = str(target.get("recommended_request_type") or "instrumentation_consistency_query")
            requirements.append(
                {
                    "requirement_id": f"LLM-REQ-{index:03d}",
                    "review_target_id": str(target.get("target_id") or ""),
                    "canonical_review_unit": str(target.get("canonical_review_unit") or ""),
                    "blocked_reason": reason,
                    "question_to_close": (
                        "What evidence would close the promotion gate for "
                        f"{target.get('canonical_review_unit') or target.get('title') or 'this target'}?"
                    ),
                    "required_evidence": [
                        {
                            "evidence_type": "user_impact_signal" if "impact" in reason else "instrumentation_consistency",
                            "source_kind": "metric_or_log" if allowed_signals else "instrumentation_gap",
                            "existing_signal_refs": list(target.get("evidence_refs") or [])[:4],
                            "allowed_signal_names": allowed_signals[:6],
                            "acceptance_criteria": "Collected runtime evidence overlaps the technical failure window and supports the blocked gate.",
                            "rejection_criteria": "Collected runtime evidence is absent, healthy, or does not overlap the technical failure window.",
                            "collection_mode": "manual_read_only",
                            "maps_to_request_type": request_type,
                        }
                    ],
                    "do_not_request": [
                        "raw secrets",
                        "raw env values",
                        "credential files",
                        "unsanitized logs",
                    ],
                    "fallback_if_unavailable": "Record the source as unavailable and keep the target as validation-only.",
                }
            )
        return {"schema_version": "evidence_requirements.v1", "requirements": requirements}

    def _root_cause_payload(self, bundle: dict[str, Any]) -> dict[str, Any]:
        cause = _primary_cause(bundle)
        refs = _refs_for(bundle, "connection", "database", "rtmps", "ffmpeg", "youtube", "error_count", "PATTERN")
        claim = {
            "claim_type": "support",
            "claim_text": f"The leading hypothesis is {cause}. {_common_context(bundle)}",
            "evidence_refs": refs,
            "counter_evidence_refs": [],
            "caveats": ["This is a review target, not an asserted truth."],
            "missing_evidence": _telemetry_missing_for(cause),
            "temporary_action": "Keep mitigations reversible until missing evidence is collected.",
            "permanent_action": _permanent_action_for(cause),
            "required_authority": "service owner or incident commander",
        }
        claims = [claim]
        if _has_deploy(bundle):
            claims.append(
                {
                    "claim_type": "validation_target",
                    "claim_text": "Validate whether the latest deployment changed connection pooling, timeout, or retry behavior.",
                    "evidence_refs": _refs_for(bundle, "deploy", "version", limit=3),
                    "counter_evidence_refs": [],
                    "caveats": [],
                    "missing_evidence": ["Deployment diff and runtime configuration for the incident window."],
                    "temporary_action": "",
                    "permanent_action": "",
                    "required_authority": "release owner",
                }
            )
        return {
            "schema_version": "claim-result/v1",
            "agent_role": "hypothesis_generator",
            "summary": f"Primary local analysis points to {cause}.",
            "claims": claims,
            "propositions": [
                {
                    "question": f"Is {cause} the most useful incident review target?",
                    "linked_claim_hints": [claim["claim_text"]],
                }
            ],
        }

    def _verifier_payload(self, bundle: dict[str, Any]) -> dict[str, Any]:
        cause = _primary_cause(bundle)
        refs = _refs_for(bundle, "metric", "error_count", "unique_trace_count", "rtmps", "youtube", "ffmpeg", limit=4)
        missing = _telemetry_missing_for(cause)
        return {
            "schema_version": "claim-result/v1",
            "agent_role": "evidence_verifier",
            "summary": "Verifier preserves missing data and caveats for human review.",
            "claims": [
                {
                    "claim_type": "caveat",
                    "claim_text": f"The evidence bundle supports prioritizing {cause}, but it does not include all corroborating telemetry.",
                    "evidence_refs": refs,
                    "counter_evidence_refs": [],
                    "caveats": ["The bundle proves review priority, not root-cause truth."],
                    "missing_evidence": missing,
                    "temporary_action": "",
                    "permanent_action": _permanent_action_for(cause),
                    "required_authority": "platform owner",
                },
                {
                    "claim_type": "next_data_needed",
                    "claim_text": f"Collect corroborating telemetry before closing review of {cause}.",
                    "evidence_refs": refs[:2],
                    "counter_evidence_refs": [],
                    "caveats": [],
                    "missing_evidence": missing,
                    "temporary_action": "",
                    "permanent_action": "",
                    "required_authority": "incident commander",
                },
            ],
            "propositions": [
                {
                    "question": f"What extra evidence is needed to validate {cause}?",
                    "linked_claim_hints": ["missing telemetry"],
                }
            ],
        }

    def _contrast_payload(self, bundle: dict[str, Any]) -> dict[str, Any]:
        text = _bundle_text(bundle)
        cause = _primary_cause(bundle)
        refs = _refs_for(bundle, "timeout", "http", "5xx", "dependency", "youtube", "rtmps", "ffmpeg", limit=4)
        if "stream_transport" in text and "youtube_health" in text:
            alternative = "YouTube health evidence may explain the symptoms independently of local RTMPS transport"
            claim_type = "counter_evidence"
        elif "payment-gateway" in text or "dependency_timeout" in text:
            alternative = "a downstream payment gateway timeout may be amplifying the incident"
            claim_type = "counter_evidence"
        else:
            alternative = f"the logs do not provide a strong independent alternative to {cause}"
            claim_type = "caveat"
        return {
            "schema_version": "claim-result/v1",
            "agent_role": "contrast_agent",
            "summary": "Contrast agent keeps disagreement as a validation target.",
            "claims": [
                {
                    "claim_type": claim_type,
                    "claim_text": alternative,
                    "evidence_refs": refs,
                    "counter_evidence_refs": [],
                    "caveats": ["Agreement is not treated as truth; disagreement is queued for review."],
                    "missing_evidence": ["Dependency-specific latency and error budget burn."],
                    "temporary_action": "Check downstream dependency status before applying irreversible changes.",
                    "permanent_action": "Add dependency health to bundle builder inputs.",
                    "required_authority": "on-call engineer",
                }
            ],
            "propositions": [
                {
                    "question": "Is there a competing downstream dependency explanation?",
                    "linked_claim_hints": [alternative],
                }
            ],
        }


def default_local_providers() -> list[HeuristicProvider]:
    return [
        HeuristicProvider("gemini-local", "gemini-simulated-root", "root-cause"),
        HeuristicProvider("gemini-local", "gemini-simulated-verifier", "verifier"),
        HeuristicProvider("external-local", "contrast-simulated", "contrast"),
    ]
