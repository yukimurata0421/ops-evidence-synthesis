# Current Implementation and Roadmap

This document summarizes the public implementation status without deployment
history, private URLs, project-specific incident data, or local operator paths.

## Implemented

- Local-first raw log inspection, sanitization, and sanitized output
  verification.
- Evidence Bundle creation with stable SHA256 identity.
- SQLite-backed local development store.
- BigQuery-oriented schema for production-style storage.
- Provider registry and per-provider `model_run.v1` artifacts.
- Safety preflight before provider calls.
- Multi-provider synthesis with agreement groups, disagreement themes, and
  missing-evidence routing.
- Conservative model-output ingest that preserves raw output hashes and records
  parse/repair/schema status without semantic repair.
- Review Target Arbitration through `canonical_review_graph.v1`.
- Separation of technical baseline agreement from incident baseline agreement.
- Evidence Request Planner with human-question answers, write-token guarded
  generation, progress state, and explicit "no text changes" output state.
- Review queue UI with bundle provenance, review target cards, evidence drawer,
  review decisions, and More data flow.
- Write-token guard for mutation routes.
- Structured JSON logging for API requests.
- Docker-based Terraform wrapper and baseline Terraform resources.
- Browser-level UI regression coverage and manual CI script.

## Production-Oriented Gaps

The current codebase is usable as a reference implementation, but these areas
still need hardening before operating it as a general hosted product:

- Continuous deployment and hosted integration tests.
- Queue-based fan-out for provider execution.
- Sensitive Data Protection or equivalent managed redaction for high-risk
  environments.
- Stronger secret rotation and notification-channel automation.
- Provider cost budgets and per-provider quota policy in deployment config.
- Long-running provider execution isolation with job or worker-pool semantics.
- Stronger append-only run lineage for BigQuery projections.
- Optional read-side authentication for private review pages.
- More calibration data across different incident domains.
- A user-facing profile editor and profile validation UI.

## Public Documentation Policy

Public documentation should describe reusable architecture, setup, data
contracts, security boundaries, and roadmap items. It should not include:

- private deployment URLs
- cloud project identifiers
- image digests
- exact incident evidence hashes
- execution IDs
- local workstation paths
- raw log excerpts from real systems
- dated internal work logs

Detailed work records should remain outside the public repository or be
summarized into stable user-facing documentation.
