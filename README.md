# Ops Evidence Synthesis

[![CI](https://github.com/yukimurata0421/ops-evidence-synthesis/actions/workflows/ci.yml/badge.svg)](https://github.com/yukimurata0421/ops-evidence-synthesis/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/python-3.11%2B-3776AB)
![Local first](https://img.shields.io/badge/raw%20logs-local--first-166d6b)
![Cloud Run](https://img.shields.io/badge/demo-Cloud%20Run-4285F4)
![License](https://img.shields.io/badge/license-all%20rights%20reserved-lightgrey)

Local-first evidence synthesis for DevOps incident review.

Ops Evidence Synthesis turns sanitized operational evidence into a fast,
reviewable incident analysis page. It is built for the failure mode where AI
incident tools sound confident before they have enough evidence.

Live read-only demo:

- Public entry: https://ops-evidence.yukimurata0421.dev/
- Summary: https://ops-evidence.yukimurata0421.dev/?evidence_sha256=7ca07bd8ed4bcb6009b654f17c40576a7b3462c62b2c74011c1623043550ccfb
- Detail: https://ops-evidence.yukimurata0421.dev/ui/full-review-page?evidence_sha256=7ca07bd8ed4bcb6009b654f17c40576a7b3462c62b2c74011c1623043550ccfb
- Human-readable API view: https://ops-evidence.yukimurata0421.dev/ui/api?evidence_sha256=7ca07bd8ed4bcb6009b654f17c40576a7b3462c62b2c74011c1623043550ccfb
- Visual review graph: https://ops-evidence.yukimurata0421.dev/ui/review-graph?evidence_sha256=7ca07bd8ed4bcb6009b654f17c40576a7b3462c62b2c74011c1623043550ccfb
- More data rescore demo: https://ops-evidence.yukimurata0421.dev/ui/rescore-demo?id=amazon-notify-more-data-rescore
- JSON summary API: https://ops-evidence.yukimurata0421.dev/ui/summary?evidence_sha256=7ca07bd8ed4bcb6009b654f17c40576a7b3462c62b2c74011c1623043550ccfb
- JSON review targets API: https://ops-evidence.yukimurata0421.dev/review-targets?evidence_sha256=7ca07bd8ed4bcb6009b654f17c40576a7b3462c62b2c74011c1623043550ccfb
- JSON review graph API with nodes/edges: https://ops-evidence.yukimurata0421.dev/review/graph?evidence_sha256=7ca07bd8ed4bcb6009b654f17c40576a7b3462c62b2c74011c1623043550ccfb

Current hackathon submission surfaces:

- Public GitHub repository URL: https://github.com/yukimurata0421/ops-evidence-synthesis
- Deployed project URL: https://ops-evidence.yukimurata0421.dev/
- Proto Pedia project URL: pending until the project page is created.
- Link list for submission copy/paste: [docs/submission-links.md](docs/submission-links.md)
- Submission checklist: [docs/submission-checklist.md](docs/submission-checklist.md)
- Architecture image: [docs/assets/architecture-devops-ai-agent.svg](docs/assets/architecture-devops-ai-agent.svg)
- Demo video script: [docs/demo-video-script.md](docs/demo-video-script.md)
- ProtoPedia entry draft: [docs/protopedia-entry-v3.md](docs/protopedia-entry-v3.md)
- ProtoPedia Japanese entry draft: [docs/protopedia-entry-japanese.md](docs/protopedia-entry-japanese.md)
- X post draft: [docs/x-post-draft.md](docs/x-post-draft.md)

Real API source-aware run:

- Evidence SHA256: `7ca07bd8ed4bcb6009b654f17c40576a7b3462c62b2c74011c1623043550ccfb`
- Public payload: `data/precomputed_review_summaries/7ca07bd8ed4bcb6009b654f17c40576a7b3462c62b2c74011c1623043550ccfb.json`
- Public manifest: `data/public_evidence_manifests/amazon_notify_real_api.json`
- Run notes: [docs/real-api-qwen-glm-run.md](docs/real-api-qwen-glm-run.md)

This run was generated through a 7-day storeless CLI real-provider path with a
23,400-row sanitized log corpus, a bounded Evidence Bundle model projection,
sanitized source context, an approved profile explicitly included in provider
prompts as human-gated interpretation context, and five schema-valid real
provider outputs: Gemini, gpt-oss, Mistral, Qwen, and GLM. The 7-day window was
selected after 2-day, 5-day, and 7-day candidates were evaluated.

The production workflow is Gemini-led: `gemini-enterprise-agent-platform` is the
required first provider and reference point for comparison, while gpt-oss,
Mistral, Qwen, and GLM are adversarial cross-checks. Gemini is not treated as a
truth source or answer key; runtime claims still need cited evidence IDs and
promotion gates.

stream_v3 real API source-aware runs:

- Dell runtime detail: https://ops-evidence.yukimurata0421.dev/ui/full-review-page?evidence_sha256=aba039fb4c472b45d5f016a8c7accd853d61cc3a00480767fe33fbca6f36c778
- Dell runtime API view: https://ops-evidence.yukimurata0421.dev/ui/api?evidence_sha256=aba039fb4c472b45d5f016a8c7accd853d61cc3a00480767fe33fbca6f36c778
- arena-server monitoring detail: https://ops-evidence.yukimurata0421.dev/ui/full-review-page?evidence_sha256=a09ee4615689dfce1557c2803cdbdf43ce0c285c196c1317cd3d30ee1835d267
- arena-server monitoring API view: https://ops-evidence.yukimurata0421.dev/ui/api?evidence_sha256=a09ee4615689dfce1557c2803cdbdf43ce0c285c196c1317cd3d30ee1835d267
- Run notes: [docs/stream-v3-real-api-runs.md](docs/stream-v3-real-api-runs.md)

These runs used sanitized stream_v3 code context plus separate runtime and
monitoring-plane log corpora. Dell retained 11,399 sanitized runtime rows over
a 7-day analysis window; the arena-server monitoring run retained 4,747
sanitized rows over a 7-day analysis window. Both runs include the approved
profile context in each provider prompt while keeping it separate from runtime
incident evidence.

The public data boundary is documented in
[docs/data-boundary.md](docs/data-boundary.md). Full row-level sanitized
stream_v3 corpora are intentionally not committed; public review is supported
by fixed payloads, evidence manifests, provider output hashes, and the live
read-only UI.

Deterministic local fixture:

- Summary: https://ops-evidence.yukimurata0421.dev/?evidence_sha256=c43cb9ccb916abdb73e71e05b4f643f6419eb74de6324094be25400557f6ed1e
- Detail: https://ops-evidence.yukimurata0421.dev/ui/full-review-page?evidence_sha256=c43cb9ccb916abdb73e71e05b4f643f6419eb74de6324094be25400557f6ed1e
- Human-readable API view: https://ops-evidence.yukimurata0421.dev/ui/api?evidence_sha256=c43cb9ccb916abdb73e71e05b4f643f6419eb74de6324094be25400557f6ed1e

## What You Can Run Now

- The public Cloudflare URL serves a precomputed summary/detail review without
  starting model work on the initial GET.
- `make demo` regenerates the flagship amazon-notify review cache from
  `data/amazon_notify_flagship_logs.jsonl` using deterministic local providers.
- `python -m uvicorn ...` serves the same read-only review UI locally.
- `ops-evidence draft-focused-profile` asks Gemini to profile the sanitized
  code/config and evidence context into the runtime components, logged signals,
  orchestration loops, and read-only collectors that matter for review.
- `make ci` verifies fixture fidelity and runs the full test suite.
- `make smoke-public` checks that the deployed summary/detail pages and
  read-only review APIs load within the 10 second review budget and contain the
  expected review signals.

## What Problem This Solves

In AIOps and Observability AI, the hard problem is not producing another
summary. The hard problem is preventing unsafe certainty.

Common incident-review failure modes:

- Raw logs and source context are too sensitive to upload to arbitrary model
  workflows.
- Model output mixes evidence, interpretation, and suggested actions.
- Dashboard screenshots hide the evidence boundary and provenance.
- Multi-model disagreement is often collapsed into a single confident answer.
- Slow review pages lose evaluator trust before the evidence is visible.
- Follow-up evidence requests are not tied back to the original claims.

This project addresses those gaps with a local-first evidence boundary,
SHA-fixed Evidence Bundles, multi-provider analysis, evidence-reference
validation, disagreement-preserving arbitration, and a read-only Cloud Run UI
that loads a useful review immediately.

The core product stance is conservative: AI may prioritize review work and
request missing evidence, but final causal judgement and operational action stay
human-gated.

## Five-Minute Demo

The public demo path requires no cloud credentials, no network model calls, and
no private logs. It uses deterministic local providers and a committed
public-safe amazon-notify fixture.

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -U pip
python -m pip install -e ".[test,api]"
make demo
make verify-precomputed
python -m uvicorn ops_evidence_synthesis.api:app --host 127.0.0.1 --port 8080
```

Open the generated local review page:

```text
http://127.0.0.1:8080/?evidence_sha256=c43cb9ccb916abdb73e71e05b4f643f6419eb74de6324094be25400557f6ed1e
```

What to look for:

- Provider positions are shown as `claimed` or `silent` per review target.
- Convergence score is `claimed successful providers / all successful providers`.
- Technical convergence does not promote an incident when impact is still open.
- Raw logs are not uploaded; the UI serves a generated, read-only review cache.
- More data rescore demo shows `needs_more_data -> evidence_collected` and a
  promotion change from validation target to primary candidate.
- Agent Trace is generated through an ADK tool contract: the deterministic
  pipeline is wrapped as ADK-compatible tools, while human gates remain explicit.

## Main Pipeline

| Stage | What happens | Main implementation |
| --- | --- | --- |
| Collect | Ingest local JSONL/text logs and optional source/profile context. | `src/ops_evidence_synthesis/ingest.py`, `scripts/analyze_amazon_notify_local.py` |
| Sanitize | Redact sensitive values and verify that raw logs stay outside model input. | `src/ops_evidence_synthesis/local_first.py`, `src/ops_evidence_synthesis/sanitizer.py` |
| Analyze | Run deterministic or configured providers against the same Evidence Bundle. | `src/ops_evidence_synthesis/synthesis/pipeline.py`, `src/ops_evidence_synthesis/ai/` |
| Orchestrate | Wrap the investigation loop as ADK tools and emit a tool-call trace for Agent Runtime / `AdkApp` deployments. | `src/ops_evidence_synthesis/agents/adk_investigator.py` |
| Synthesize | Parse, validate, route, score, compare providers, and persist the Canonical Review Graph/review-target projection. | `src/ops_evidence_synthesis/synthesis/`, `src/ops_evidence_synthesis/precomputed_review.py` |
| Report | Serve a fast, read-only summary/detail page from precomputed review JSON. | `src/ops_evidence_synthesis/api.py`, `src/ops_evidence_synthesis/routes/`, `src/ops_evidence_synthesis/web/`, `data/precomputed_review_summaries/` |

High-level flow:

```text
local logs
  -> sanitize locally
  -> Evidence Bundle with stable SHA256
  -> provider runs
  -> ADK tool-call trace
  -> schema and evidence-reference validation
  -> review target arbitration
  -> persisted canonical_review_graph.v1
  -> precomputed review JSON
  -> read-only summary/detail UI
```

For local source-aware runs, generate sanitized source artifacts first, approve
the profile draft, then pass only those sanitized context files to the product
flow:

```bash
ops-evidence run-case \
  --input data/sample_logs.jsonl \
  --service payment-api \
  --environment prod \
  --start 2026-06-12T10:00:00Z \
  --end 2026-06-12T10:20:00Z \
  --approved-profile path/to/approved_profile.yaml \
  --source-context path/to/source_context_bundle.json \
  --source-analysis path/to/source_analysis_bundle.json
```

## Reviewer Reading Path

Start here if you are evaluating the hackathon submission:

1. [HACKATHON_SUBMISSION.md](HACKATHON_SUBMISSION.md) - short problem, demo, and judging summary.
2. [src/ops_evidence_synthesis/precomputed_review.py](src/ops_evidence_synthesis/precomputed_review.py) - turns pipeline output into the fast UI cache.
3. [tests/test_precomputed_review.py](tests/test_precomputed_review.py) - proves the public fixture is regenerated from code.
4. [src/ops_evidence_synthesis/synthesis/output_ingest.py](src/ops_evidence_synthesis/synthesis/output_ingest.py) - canonical observation rollup and provider-overlap scoring.
5. [src/ops_evidence_synthesis/api.py](src/ops_evidence_synthesis/api.py) - FastAPI app bootstrap and store/provider wiring.
6. [src/ops_evidence_synthesis/routes/api_routes.py](src/ops_evidence_synthesis/routes/api_routes.py) - API routes for ingest, review, progress, and public read-only views.
7. [src/ops_evidence_synthesis/web/precomputed_review.py](src/ops_evidence_synthesis/web/precomputed_review.py) and [src/ops_evidence_synthesis/web/review_page.py](src/ops_evidence_synthesis/web/review_page.py) - HTML/JSON rendering for precomputed and SQLite-backed review pages.
8. [docs/architecture.md](docs/architecture.md) - local-first architecture and review graph.
9. [docs/evidence_bundle.md](docs/evidence_bundle.md) - Evidence Bundle contract and evidence/context boundary.
10. [docs/current-vs-architecture-gap.md](docs/current-vs-architecture-gap.md) - implemented state and production hardening roadmap.

## Test Commands

Install and run the full local gate:

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -U pip
python -m pip install -e ".[test,api]"
make verify-precomputed
make test
```

Run the combined local gate:

```bash
make ci
```

`make` uses `.venv/bin/python` automatically when the repository-local virtual
environment exists; otherwise it falls back to `python3`.

Run the same manual gate used before release if you prefer the shell wrapper:

```bash
PYTHON_BIN=.venv/bin/python scripts/manual_ci.sh
```

Smoke the public Cloud Run demo:

```bash
make smoke-public
```

Deploy and immediately verify the public demo:

```bash
scripts/deploy_public_demo.sh
```

Create a clean public archive from tracked files only:

```bash
make archive-public
```

CI also runs `make verify-precomputed` and `make test` on GitHub Actions.

Optional ADK / Agent Runtime check:

```bash
pip install -e ".[agent]"
ops-evidence adk-trace \
  --precomputed-payload data/precomputed_review_summaries/c43cb9ccb916abdb73e71e05b4f643f6419eb74de6324094be25400557f6ed1e.json \
  --check-runtime
```

## Assets, Samples, and Generated Outputs

Committed public assets:

- `data/amazon_notify_flagship_logs.jsonl` - public-safe 6,506-line flagship fixture.
- `data/public_evidence_manifests/*.json` - compact public manifests for real API reviews, including URLs, evidence hashes, provider hashes, data-boundary flags, and token-compression statistics.
- `data/precomputed_review_summaries/c43cb9c...ed1e.json` - live demo cache regenerated by `make demo` and checked by CI.
- `data/precomputed_review_summaries/7ca07b...0ccfb.json` - real API source-aware review cache generated from a 23,400-row sanitized 7-day corpus, sanitized source context, and five real providers.
- `data/precomputed_review_summaries/aba039f...c778.json` - stream_v3 Dell runtime real API review cache with 11,399 sanitized runtime rows over a 7-day analysis window.
- `data/precomputed_review_summaries/a09ee4...d267.json` - stream_v3 arena-server monitoring real API review cache with 4,747 sanitized monitoring rows.
- `data/sample_logs.jsonl` - compact public-safe sample fixture.
- `data/precomputed_review_summaries/1be4a214...6731.json` - compact sample cache regenerated by `make demo-sample`.
- `sample_projects/profile_discovery_sample/` - small profile-discovery fixture.
- `schemas/` - public JSON contracts for claim results and Evidence Bundles.

Generated or local-only assets:

- `workspace/`, `.venv/`, `.pytest_cache/`, and `__pycache__/` are local generated outputs and are not committed.
- Real operational logs, raw source trees, and private row-level sanitized corpora are not part of the public repository.
- Real-provider execution may require local credentials and is intentionally separate from the public deterministic demo.

## Hackathon Scope and Asset Boundary

The submission path to evaluate is the live read-only UI, the deterministic
local demo, the precomputed review generator, and the tests that prove the
committed review cache is reproducible.

Reusable foundation code is kept in the repository because it is part of the
working product surface: local sanitization, Evidence Bundle creation, provider
adapters, review arbitration, evidence request planning, storage adapters, and
the FastAPI UI. Optional operator scripts under `scripts/` are not required for
the five-minute review path unless the README names them directly. See
[scripts/README.md](scripts/README.md) for the script inventory.

Committed logs are synthetic fixtures. `sample_logs/redaction_fixture.jsonl`
contains intentionally fake tokens and example paths so the sanitizer and
secret-leak checks can be tested. Generated databases, logs, caches, workspaces,
Terraform state, and credential files are ignored by default.

The live Cloud Run page is a read-only delivery surface. Heavy analysis and real
provider runs are local-first by design; the public repository demonstrates
reproducibility through deterministic fixtures.

## Safety Boundary

- Raw logs stay local.
- Model input is the sanitized Evidence Bundle.
- Source, profile, and human context can guide review, but runtime claims need cited evidence IDs.
- API and UI reads prefer the persisted Canonical Review Graph when available.
- Provider agreement is a review signal, not majority-vote truth.
- Score is review priority, not truth probability.
- Final causal judgement and operational action remain human-gated.

## Deployment Template

The repository includes a production-oriented Google Cloud template for teams
that want to operate the same contracts beyond the public demo:

- Cloud Run API service
- BigQuery schemas for evidence and synthesis artifacts
- Cloud Workflows entry point
- Terraform resources under `infra/terraform/`
- Public smoke check for root/detail review pages and read-only review APIs

See [infra/terraform/README.md](infra/terraform/README.md) and
[cloudbuild.yaml](cloudbuild.yaml) for deployment details.

## License

All rights reserved. This repository is published for hackathon review and
demonstration purposes. See [LICENSE](LICENSE).

## Author

Yuki Murata
