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
| `docs/architecture.md` | Main architecture overview for the local-first evidence pipeline. | Keep |
| `docs/evidence_bundle.md` | Sanitized Evidence Bundle contract and evidence/context boundary. | Keep |
| `docs/current-vs-architecture-gap.md` | Public implementation status and product hardening roadmap. | Keep |
| `architecture.txt` | Plain-text architecture reference for tools or readers that prefer text output. | Keep |
| `infra/terraform/README.md` | Cloud baseline summary and Terraform wrapper usage. | Keep |
| `sample_projects/profile_discovery_sample/README.md` | Minimal fixture note for profile discovery tests. | Keep |
| `Makefile` | Reviewer commands for deterministic demo regeneration and verification. | Keep |
| `.github/workflows/ci.yml` | Public CI gate for fixture fidelity and tests. | Keep |
| `data/precomputed_review_summaries/*.json` | Read-only UI cache fixtures generated from sanitized evidence. | Keep |
| `sample_logs/redaction_fixture.jsonl` | Synthetic redaction fixture with fake token-shaped values. | Keep |

## Reviewer Command Surface

The primary commands are `make demo`, `make verify-precomputed`, `make ci`, and
`make smoke-public`. Other scripts are auxiliary operator tools and should not
be treated as the required hackathon demo path unless a reviewer deliberately
wants to inspect cloud deployment or private-log ingestion extensions.

## Intentionally Not Published

The private development tree also contains date-stamped implementation records,
failure logs, live-operations notes, and project-specific monitoring writeups.
Those documents are intentionally excluded from the public copy because they can
contain private chronology, local paths, deployment details, incident-specific
evidence hashes, or noisy work-in-progress context.

Public documentation should stay stable and reviewer-oriented:

- problem and solution summary
- safety boundary
- architecture and data contracts
- demo entry points
- local verification commands
- current product gaps
- deployment baseline

It should not publish raw logs, internal operator paths, cloud project IDs,
image digests, secrets, private URLs, dated work logs, or machine-specific
incident evidence.
