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
- Gemini-led workflow policy: Gemini runs first as the required provider and
  reference point; alternative providers act as cross-checks.
- Safety preflight before provider calls.
- Multi-provider synthesis with agreement groups, disagreement themes, and
  missing-evidence routing.
- Conservative model-output ingest that preserves raw output hashes and records
  parse/repair/schema status without semantic repair.
- Review Target Arbitration through `canonical_review_graph.v1`.
- Deterministic synthesis pipeline and GCP Workflow refresh and persist a
  Canonical Review Graph after scoring/model comparison.
- `/review-targets`, summary, and detail UI prefer the persisted Canonical
  Review Graph when one exists, with legacy target generation only as fallback.
- `run-case` and `arbitrate-review` can consume approved profile, sanitized
  source context, and sanitized source analysis artifacts without accepting raw
  source or raw environment values.
- Separation of technical support from incident-promotion agreement.
- Evidence Request Planner with human-question answers, write-token guarded
  generation, progress state, and explicit "no text changes" output state.
- Review queue UI with bundle provenance, review target cards, evidence drawer,
  review decisions, and More data flow.
- Read-only public More data rescore demo showing child Evidence Bundle evidence
  changing a promotion gate from validation target to primary candidate.
- Thin FastAPI app bootstrap with route handlers and web rendering split into
  dedicated modules.
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

## Intentional Manual Boundaries

- Raw source/config/env metadata is summarized locally by `sanitize-source` and
  `analyze-source`; cloud workflows do not collect or upload raw source trees.
- Profile drafts require human approval before they become an explicit profile.
- The Evidence Request Planner generates read-only collection instructions. It
  does not execute shell commands or collectors from the UI.
- Child bundle re-analysis is explicit: operators collect locally, sanitize,
  verify, upload the child Evidence Bundle, then re-run analysis/arbitration.

## Public Documentation Policy

Public documentation should describe reusable architecture, setup, data
contracts, security boundaries, and roadmap items. It should not include:

- private deployment URLs
- cloud project identifiers
- unexplained image digests outside release-attestation docs
- private or non-review incident evidence hashes
- execution IDs
- local workstation paths
- raw log excerpts from real systems
- dated internal work logs

Public review Evidence SHA values may appear in reviewer-facing run records when
they identify an inspectable public artifact. They should not be mixed with raw
evidence, operator-only execution IDs, or private deployment details.

Detailed work records should remain outside the public repository or be
summarized into stable user-facing documentation.
