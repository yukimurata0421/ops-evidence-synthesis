# Hackathon Submission

## Summary

Ops Evidence Synthesis is a local-first DevOps incident-review system. It does
not upload raw logs. It turns sanitized evidence into a fixed SHA256 bundle,
runs multiple providers, validates cited claims, and converts disagreement into
human-reviewable targets.

## Demo

- Public entry: https://ops-evidence.yukimurata0421.dev/
- Primary summary: https://ops-evidence.yukimurata0421.dev/?evidence_sha256=345430d258752cefef81bfb587b4c210799d02bfc849e0a7ac5dc4c48fddb1d6
- Primary detail: https://ops-evidence.yukimurata0421.dev/ui/full-review-page?evidence_sha256=345430d258752cefef81bfb587b4c210799d02bfc849e0a7ac5dc4c48fddb1d6
- Primary human-readable API view: https://ops-evidence.yukimurata0421.dev/ui/api?evidence_sha256=345430d258752cefef81bfb587b4c210799d02bfc849e0a7ac5dc4c48fddb1d6
- Primary visual review graph: https://ops-evidence.yukimurata0421.dev/ui/review-graph?evidence_sha256=345430d258752cefef81bfb587b4c210799d02bfc849e0a7ac5dc4c48fddb1d6
- Guarded amazon-notify detail: https://ops-evidence.yukimurata0421.dev/ui/full-review-page?evidence_sha256=b99da97cab19f026b5475cdaa6100fdd6ebb6d96466a43e6b62a44b99ac414ec
- More data rescore demo: https://ops-evidence.yukimurata0421.dev/ui/rescore-demo?id=amazon-notify-more-data-rescore
- JSON summary API: https://ops-evidence.yukimurata0421.dev/ui/summary?evidence_sha256=345430d258752cefef81bfb587b4c210799d02bfc849e0a7ac5dc4c48fddb1d6
- JSON review targets API: https://ops-evidence.yukimurata0421.dev/review-targets?evidence_sha256=345430d258752cefef81bfb587b4c210799d02bfc849e0a7ac5dc4c48fddb1d6
- JSON review graph API with nodes/edges: https://ops-evidence.yukimurata0421.dev/review/graph?evidence_sha256=345430d258752cefef81bfb587b4c210799d02bfc849e0a7ac5dc4c48fddb1d6

Submission URLs:

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

- Evidence SHA256: `b99da97cab19f026b5475cdaa6100fdd6ebb6d96466a43e6b62a44b99ac414ec`
- Public payload: `data/precomputed_review_summaries/b99da97cab19f026b5475cdaa6100fdd6ebb6d96466a43e6b62a44b99ac414ec.json`
- Public manifest: `data/public_evidence_manifests/amazon_notify_real_api.json`
- Run notes: [docs/real-api-5-provider-run.md](docs/real-api-5-provider-run.md)

This run used a 14-day, 44,944-row sanitized DB coverage corpus. Every
sanitized DB row is assigned to a coverage ledger entry before provider
prompts, while provider prompts operate over chunked Evidence Corpora with
chunk manifests, source Evidence Item IDs, and direct raw-row prompt count kept
at zero. Gemini, GPT OSS, Mistral, Qwen, and GLM all returned schema-valid real
API outputs over all 8,519 grouped Evidence Items. The 14-day window uses the
full available amazon-notify sanitized DB corpus after shorter candidate
windows were superseded.

The public entry uses stream_v3 Dell runtime as the primary reviewer path
because it has active human-gated primary candidates. The amazon-notify run is
kept as the guarded-review example: even with 5/5 providers and 100% full-corpus
ledger coverage, it does not auto-promote a cause until profile outcomes and
user impact are approved. Llama and Claude are excluded because they were not
available in this environment. Provider support is treated as review work, not
truth; cited runtime evidence and promotion gates still control what can move
forward.

Additional stream_v3 source-aware real API runs:

- Dell runtime detail: https://ops-evidence.yukimurata0421.dev/ui/full-review-page?evidence_sha256=345430d258752cefef81bfb587b4c210799d02bfc849e0a7ac5dc4c48fddb1d6
- arena-server monitoring detail: https://ops-evidence.yukimurata0421.dev/ui/full-review-page?evidence_sha256=6b7dad773b78274ed9706b02e15478427ad8817e8d8330ba19487d4293eeb3d3
- Run notes: [docs/stream-v3-real-api-runs.md](docs/stream-v3-real-api-runs.md)

These runs use sanitized stream_v3 code context and separate 45,000-row Dell
runtime / 50,000-row arena-server monitoring corpora. Both stream_v3 reviews
meet the minimum 24-hour analysis policy and send every grouped Evidence Item
through chunked full-corpus provider review while keeping raw rows and raw
source local.

Data boundary and compression details:
[docs/data-boundary.md](docs/data-boundary.md). Full row-level sanitized
stream_v3 corpora are not committed; the public evidence is the live URL, fixed
payload, manifest, provider output hashes, and model-projection statistics.

## 30-Second Reviewer Path

1. Open the Summary URL and confirm that a concrete finding appears immediately.
2. Check the Review Graph section: provider convergence is visible, but
   incident and user-impact promotion remains separately gated.
3. Open the Detail URL and inspect provider positions, Evidence Item links, and
   promotion gates.
4. Open the API view to inspect the five-provider chunked run based on
   sanitized logs and code context.
5. Open the More data rescore demo to see `needs_more_data -> evidence_collected`
   and validation target -> primary candidate.
6. Run `make demo && make verify-precomputed` to regenerate the same flagship
   cache from committed public-safe logs.

## Key Points

- The working product is a guarded review loop, not a free-form chat summary.
- Initial UI is precomputed and read-only.
- Provider positions and provider status are visible per review target.
- DB-backed runs assign every sanitized log row in the incident window to a
  grouped Evidence Item before provider chunking, so low-frequency rows are not
  dropped by count alone.
- Convergence score is defined as claimed successful providers divided by all
  successful providers.
- The public primary path shows stream_v3 runtime targets with 5/5 providers
  and human-gated primary candidates; amazon-notify shows the restrained case
  where 5/5 provider support still remains validation work.
- The More data rescore demo shows the AI improvement cycle without public write
  access or live model execution.
- The Agent Trace is backed by an ADK-compatible tool contract; the same tools
  can be wrapped in `AdkApp` for Agent Runtime while Cloud Run serves the
  read-only review artifact.
- The public flagship fixture is regenerated by `make demo` and checked by
  `make verify-precomputed`.
- The release smoke checks public pages, read-only review APIs, missing-cache
  behavior, and the 10 second UI budget.

## Reproducibility Boundary

Real operational analysis is local-first: raw logs and live provider credentials
stay in the operator environment. Cloud Run serves SHA-fixed read-only review
caches. The public repository includes a 6,506-line public-safe amazon-notify
fixture that regenerates the flagship review without network access or secrets.
Private row-level sanitized corpora stay local; public manifests preserve the
row counts, Evidence Item counts, model-projection counts, and provider output
hashes needed to review what was analyzed.

## Build Scope

Evaluate the submission through the live UI, `make demo`, `make ci`, and
`make smoke-public`. Those commands exercise the public product path: sanitized
fixtures, deterministic providers, review arbitration, precomputed review JSON,
the read-only UI, and the read-only More data rescore demo.

For release, `scripts/deploy_public_demo.sh` runs the local gate, secret scan
when available, Cloud Build, Cloud Run update, and live smoke in one command.

The repository also contains reusable foundation code for local sanitization,
provider adapters, Evidence Bundles, storage, review planning, and deployment.
Those modules are kept because they are used by the product path or its tests.
Operator-specific scripts that require private logs, cloud credentials, or
real-provider access are auxiliary and are not required for the five-minute
evaluation path.

## Reviewer Reading Path

1. Open the Summary URL.
2. Open the Detail URL if deeper provider positions are needed.
3. Read [Architecture](docs/architecture.md) for the local-first pipeline.
4. Read [Evidence Bundle contract](docs/evidence_bundle.md) for the safety boundary.
5. Read [Public data boundary](docs/data-boundary.md) for the committed-vs-local artifact split.
6. Read [Current implementation and roadmap](docs/current-vs-architecture-gap.md) for production gaps.

The public documentation set is listed in
[Public documentation inventory](docs/public-documentation-inventory.md).

## Author

Yuki Murata
