# Public Documentation Inventory

This repository keeps only documentation that helps an external reviewer
understand, run, and evaluate Ops Evidence Synthesis without exposing private
operator history or raw incident material.

## Published Documents

| Document | Purpose | Public status |
| --- | --- | --- |
| `README.md` | Fast entry point, demo URLs, local check commands, safety boundary. | Keep |
| `HACKATHON_SUBMISSION.md` | Short reviewer-facing summary for the hackathon gate. | Keep |
| `SECURITY.md` | Public data-boundary and secret-handling policy. | Keep |
| `docs/data-boundary.md` | Public artifact boundary, local-only data policy, and token-compression explanation for real-provider runs. | Keep |
| `docs/architecture.md` | Main architecture overview for the local-first evidence pipeline. | Keep |
| `docs/review-modes-runbook.md` | Mode naming, measured public replay results, and guidance that replay timing is not a live AI benchmark. | Keep |
| `docs/evidence_bundle.md` | Sanitized Evidence Bundle contract and evidence/context boundary. | Keep |
| `docs/current-vs-architecture-gap.md` | Public implementation status and product hardening roadmap. | Keep |
| `docs/assets/architecture-devops-ai-agent.svg` | Reviewer-ready system architecture image. | Keep |
| `docs/real-api-5-provider-run.md` | Source-aware five-provider real API run record for the amazon-notify public payload. | Keep |
| `docs/stream-v3-real-api-runs.md` | Source-aware real API run record for Dell runtime and arena-server stream_v3 payloads. | Keep |
| `hackathon/README.md` | Entry point for the reproducible demo-video asset pack. | Keep |
| `hackathon/02-narration-ja.md` | Current three-minute narration used by the public demo. | Keep |
| `hackathon/claims-and-sources.md` | Bounded claims and source URLs behind the demo. | Keep |
| `hackathon/assets/` | Public screenshots, overlays, and editable SVG sources used by the demo video. | Keep |
| `architecture.txt` | Plain-text architecture reference for tools or readers that prefer text output. | Keep |
| `src/ops_evidence_synthesis/routes/` and `src/ops_evidence_synthesis/web/` | Implementation split for API routing and reviewer-facing page rendering. | Keep |
| `infra/terraform/README.md` | Cloud baseline summary and Terraform wrapper usage. | Keep |
| `sample_projects/profile_discovery_sample/README.md` | Minimal fixture note for profile discovery tests. | Keep |
| `Makefile` | Reviewer commands for deterministic demo regeneration and verification. | Keep |
| `scripts/README.md` | Script inventory that separates the judging path from auxiliary tools. | Keep |
| `scripts/deploy_public_demo.sh` | Release command that runs local gates, deploys Cloud Run, and smokes the live URL. | Keep |
| `.github/workflows/ci.yml` | Public CI gate for fixture fidelity and tests. | Keep |
| `data/public_evidence_manifests/*.json` | Compact public manifests for real API review URLs, hashes, data-boundary flags, and model-projection statistics. | Keep |
| `data/precomputed_review_summaries/*.json` | Read-only UI cache fixtures generated from sanitized evidence. | Keep |
| `data/rescore_demos/*.json` | Read-only More data before/after demos showing child-bundle re-score behavior. | Keep |
| `sample_logs/redaction_fixture.jsonl` | Synthetic redaction fixture with fake token-shaped values. | Keep |

## Reviewer Command Surface

The primary commands are `make demo`, `make verify-precomputed`, `make ci`,
`make smoke-public`, and `make archive-public`. Release verification is
captured by `scripts/deploy_public_demo.sh`. Other scripts are auxiliary
operator tools and should not be treated as the required hackathon demo path
unless a reviewer deliberately wants to inspect cloud deployment or optional
private-log ingestion extensions.

## Intentionally Not Published

The private development tree also contains submission drafts, checklists,
date-stamped implementation records, failure logs, live-operations notes, and
project-specific monitoring writeups. Those documents are intentionally
excluded from the public copy because they can contain private chronology,
local paths, deployment details, incident-specific evidence hashes, or noisy
work-in-progress context.

Public documentation should stay stable and reviewer-oriented:

- problem and solution summary
- safety boundary
- architecture and data contracts
- demo entry points
- local verification commands
- current product gaps
- deployment baseline

It should not publish submission copy drafts, social post drafts, raw logs,
internal operator paths, secrets, private URLs, dated work logs, or
machine-specific incident evidence. Public review Evidence SHA values are
allowed in reviewer-facing run records where they act as public artifact
identifiers.
