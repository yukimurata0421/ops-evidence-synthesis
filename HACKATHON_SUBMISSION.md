# Hackathon Submission

## Summary

Ops Evidence Synthesis is a local-first DevOps AI agent for incident review.
It does not upload raw logs. It turns sanitized evidence into a fixed SHA256
bundle, runs multiple providers, validates cited claims, and converts
disagreement into human-reviewable targets.

## Demo

- Summary: https://ops-evidence-api-vn3uyu4gia-an.a.run.app/?evidence_sha256=5d0b5a918de1f99852498da2c8558d14993fe33b2259d23ac0ece59a900b48d9
- Detail: https://ops-evidence-api-vn3uyu4gia-an.a.run.app/ui/full-review-page?evidence_sha256=5d0b5a918de1f99852498da2c8558d14993fe33b2259d23ac0ece59a900b48d9

## Key Points

- Initial UI is precomputed and read-only.
- Multi-provider positions are visible per review target.
- Convergence score is defined as claimed successful providers divided by all successful providers.
- One target shows technical convergence; incident baseline remains open.
- The release smoke checks both public pages and the 10 second UI budget.

## Reviewer Reading Path

1. Open the Summary URL.
2. Open the Detail URL if deeper provider positions are needed.
3. Read [Architecture](docs/architecture.md) for the local-first pipeline.
4. Read [Evidence Bundle contract](docs/evidence_bundle.md) for the safety boundary.
5. Read [Current implementation and roadmap](docs/current-vs-architecture-gap.md) for production gaps.

The public documentation set is listed in
[Public documentation inventory](docs/public-documentation-inventory.md).

## Author

Yuki Murata
