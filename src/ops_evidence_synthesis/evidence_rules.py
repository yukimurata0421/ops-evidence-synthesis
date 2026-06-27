from __future__ import annotations


AI_EVIDENCE_RULES = (
    "System Profile is context, not evidence.",
    "Source Context Bundle is context, not incident evidence.",
    "Source Analysis Bundle is context, not incident evidence.",
    "Code/config excerpts may support profile mapping, metric semantics, logger mapping, collector mapping, and instrumentation interpretation.",
    "Code/config excerpts do not prove runtime occurrence or user impact by themselves.",
    "Evidence Items are evidence.",
    "Every support claim must cite evidence_id.",
    "Support claims about runtime behavior must still cite Evidence Items with evidence_id.",
    "Do not cite System Profile as support evidence.",
    "Do not cite Source Context or Source Analysis as support evidence for runtime occurrence.",
    "If evidence is insufficient, output missing_evidence instead of making a definitive claim.",
    "If profile_confidence is unknown or inferred, avoid definitive diagnosis and ask for required_profile_questions.",
    "If analysis_policy.allow_primary_candidate is false, do not produce a primary incident candidate.",
    "If version anchoring is missing, include a caveat that source context may not match the deployed version during the incident window.",
    "Do not invent log source names, metric names, state file paths, endpoints, or collector commands.",
    "Only name concrete metrics or logs that appear in the evidence bundle, approved profile, or source context; otherwise describe the evidence class generically.",
    "Do not translate missing evidence into ad hoc local commands; leave collection templates to the Evidence Request Planner.",
    "Raw source, raw env values, credentials, and raw grep output must not be requested or used.",
    "Score is review priority, not truth probability.",
)

PROFILE_DISCOVERY_RULES = (
    "Profile Draft is not an explicit profile until human approved.",
    "Discovery Bundle is context for profile generation, not incident evidence.",
    "Do not upload or request raw env values.",
    "Do not infer critical user outcomes as facts; mark them as assumptions.",
    "Metric semantics are candidates until human reviewed.",
    "Collector mappings must be read-only by default.",
    "Do not propose write actions, restart actions, rollback actions, or credential changes.",
)

SOURCE_CONTEXT_RULES = (
    "Source Context Bundle is context, not incident evidence.",
    "Source Analysis Bundle is context, not incident evidence.",
    "Code/config excerpts may support profile mapping, metric semantics, logger mapping, collector mapping, and instrumentation interpretation.",
    "Code/config excerpts do not prove runtime occurrence or user impact by themselves.",
    "Support claims about runtime behavior must still cite Evidence Items with evidence_id.",
    "If version anchoring is missing, include a caveat that source context may not match the deployed version during the incident window.",
    "Raw source, raw env values, credentials, and raw grep output must not be requested or used.",
)

EVIDENCE_REQUEST_PLANNER_RULES = (
    "Evidence Request Planner must not execute commands.",
    "Collection commands are templates for human review only.",
    "Raw command output stays local until sanitized and verified.",
    "Raw env values, credential values, private key bodies, Authorization header values, and Cookie values must not be collected or uploaded.",
    "Planner answers are operational context, not support evidence.",
    "Support claims must still cite evidence_id.",
    "Collection templates must not include invented metrics, log paths, state files, endpoints, or commands.",
    "If a concrete source is not present in the approved profile or source analysis, request the generic evidence class and mark unavailable sources as unavailable.",
    "After collection, sanitize output, run verify-sanitized, and build a child Evidence Bundle before upload.",
)


def ai_evidence_rules() -> list[str]:
    return list(AI_EVIDENCE_RULES)


def ai_evidence_rules_text() -> str:
    return " ".join(AI_EVIDENCE_RULES)


def profile_discovery_rules() -> list[str]:
    return list(PROFILE_DISCOVERY_RULES)


def profile_discovery_rules_text() -> str:
    return " ".join(PROFILE_DISCOVERY_RULES)


def source_context_rules() -> list[str]:
    return list(SOURCE_CONTEXT_RULES)


def source_context_rules_text() -> str:
    return " ".join(SOURCE_CONTEXT_RULES)


def evidence_request_planner_rules() -> list[str]:
    return list(EVIDENCE_REQUEST_PLANNER_RULES)


def evidence_request_planner_rules_text() -> str:
    return " ".join(EVIDENCE_REQUEST_PLANNER_RULES)
