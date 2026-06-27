# Ops Evidence Synthesis

Local-first evidence synthesis for DevOps incident review.

## Demo

- Summary: https://ops-evidence-api-vn3uyu4gia-an.a.run.app/?evidence_sha256=5d0b5a918de1f99852498da2c8558d14993fe33b2259d23ac0ece59a900b48d9
- Detail: https://ops-evidence-api-vn3uyu4gia-an.a.run.app/ui/full-review-page?evidence_sha256=5d0b5a918de1f99852498da2c8558d14993fe33b2259d23ac0ece59a900b48d9

## What It Does

1. Sanitizes raw operational logs locally.
2. Freezes a sanitized Evidence Bundle by SHA256.
3. Runs multiple model providers against the same evidence.
4. Validates model output against evidence references.
5. Preserves disagreement as review targets instead of majority-voting truth.
6. Serves a fast read-only Cloud Run review page.

## Documentation

- [Hackathon submission](HACKATHON_SUBMISSION.md)
- [Architecture](docs/architecture.md)
- [Evidence Bundle contract](docs/evidence_bundle.md)
- [Current implementation and roadmap](docs/current-vs-architecture-gap.md)
- [Public documentation inventory](docs/public-documentation-inventory.md)
- [Security](SECURITY.md)

## Safety Boundary

- Raw logs stay local.
- Model input is the sanitized Evidence Bundle.
- Score is review priority, not truth probability.
- Final causal judgement and operational action remain human-gated.

## Local Check

```bash
python -m pip install -e ".[test,api]"
python -m pytest
```

## Public URL Smoke

```bash
python scripts/check_precomputed_review_url.py \
  --base-url https://ops-evidence-api-vn3uyu4gia-an.a.run.app \
  --evidence-sha 5d0b5a918de1f99852498da2c8558d14993fe33b2259d23ac0ece59a900b48d9
```

## Author

Yuki Murata
