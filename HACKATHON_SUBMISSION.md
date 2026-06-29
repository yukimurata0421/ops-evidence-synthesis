# Hackathon Submission

## Summary

Ops Evidence Synthesis is a local-first DevOps incident-review system. It does
not upload raw logs. It turns sanitized evidence into a fixed SHA256 bundle,
runs multiple providers, validates cited claims, and converts disagreement into
human-reviewable targets.

## Demo

- Public entry: https://ops-evidence.yukimurata0421.dev/
- Summary: https://ops-evidence.yukimurata0421.dev/?evidence_sha256=7e95346cbf15de7f104631b72d784e02665d0cc1488e42a4ccf69b76fe47308d
- Detail: https://ops-evidence.yukimurata0421.dev/ui/full-review-page?evidence_sha256=7e95346cbf15de7f104631b72d784e02665d0cc1488e42a4ccf69b76fe47308d
- Human-readable API view: https://ops-evidence.yukimurata0421.dev/ui/api?evidence_sha256=7e95346cbf15de7f104631b72d784e02665d0cc1488e42a4ccf69b76fe47308d
- Visual review graph: https://ops-evidence.yukimurata0421.dev/ui/review-graph?evidence_sha256=7e95346cbf15de7f104631b72d784e02665d0cc1488e42a4ccf69b76fe47308d
- More data rescore demo: https://ops-evidence.yukimurata0421.dev/ui/rescore-demo?id=amazon-notify-more-data-rescore
- JSON summary API: https://ops-evidence.yukimurata0421.dev/ui/summary?evidence_sha256=7e95346cbf15de7f104631b72d784e02665d0cc1488e42a4ccf69b76fe47308d
- JSON review targets API: https://ops-evidence.yukimurata0421.dev/review-targets?evidence_sha256=7e95346cbf15de7f104631b72d784e02665d0cc1488e42a4ccf69b76fe47308d
- JSON review graph API with nodes/edges: https://ops-evidence.yukimurata0421.dev/review/graph?evidence_sha256=7e95346cbf15de7f104631b72d784e02665d0cc1488e42a4ccf69b76fe47308d

Submission URLs:

- Public GitHub repository URL: https://github.com/yukimurata0421/ops-evidence-synthesis
- Deployed project URL: https://ops-evidence.yukimurata0421.dev/
- Proto Pedia project URL: pending until the project page is created.
- Link list for submission copy/paste: [docs/submission-links.md](docs/submission-links.md)
- Submission checklist: [docs/submission-checklist.md](docs/submission-checklist.md)
- Architecture image: [docs/assets/architecture-devops-ai-agent.svg](docs/assets/architecture-devops-ai-agent.svg)
- Demo video script: [docs/demo-video-script.md](docs/demo-video-script.md)
- ProtoPedia entry draft: [docs/protopedia-entry-v3.md](docs/protopedia-entry-v3.md)

Real API source-aware run:

- Evidence SHA256: `7e95346cbf15de7f104631b72d784e02665d0cc1488e42a4ccf69b76fe47308d`
- Public payload: `data/precomputed_review_summaries/7e95346cbf15de7f104631b72d784e02665d0cc1488e42a4ccf69b76fe47308d.json`
- Public manifest: `data/public_evidence_manifests/amazon_notify_real_api.json`
- Run notes: [docs/real-api-qwen-glm-run.md](docs/real-api-qwen-glm-run.md)

This run used a 6,506-row sanitized e2e log corpus persisted in the API store,
a bounded DB-derived model projection, sanitized source context, and Gemini /
gpt-oss / Mistral / Qwen / GLM via the e2e API. All five provider outputs were
`status=ok` and `schema_valid=true`.

The cloud workflow is Gemini-led. `gemini-enterprise-agent-platform` is the
required first analysis provider and the comparison baseline; the other providers
act as adversarial cross-checks.

Additional stream_v3 source-aware real API runs:

- Dell runtime detail: https://ops-evidence.yukimurata0421.dev/ui/full-review-page?evidence_sha256=64fa79977171fe9bad0664d115ff0ffcf4e248cd12a6a938e62d25cba7b12681
- arena-server monitoring detail: https://ops-evidence.yukimurata0421.dev/ui/full-review-page?evidence_sha256=f22b327f601738de5c7011c9424fe7c615ed35ea693f791849a54af8d7271769
- Run notes: [docs/stream-v3-real-api-runs.md](docs/stream-v3-real-api-runs.md)

These runs use sanitized stream_v3 code context and separate 8,011-row Dell
runtime / 5,055-row arena-server monitoring corpora.

Data boundary and compression details:
[docs/data-boundary.md](docs/data-boundary.md). Full row-level sanitized
stream_v3 corpora are not committed; the public evidence is the live URL, fixed
payload, manifest, provider output hashes, and model-projection statistics.

## 30-Second Reviewer Path

1. Open the Summary URL and confirm that a concrete finding appears immediately.
2. Check the Review Graph section: provider convergence is visible, but
   incident baseline remains separately gated.
3. Open the Detail URL and inspect provider positions: `claimed` and `silent`
   are visible per provider.
4. Open the API view to inspect the five-provider run based on sanitized logs
   and code context.
5. Open the More data rescore demo to see `needs_more_data -> evidence_collected`
   and validation target -> primary candidate.
6. Run `make demo && make verify-precomputed` to regenerate the same flagship
   cache from committed public-safe logs.

## Key Points

- The working product is a guarded review loop, not a free-form chat summary.
- Initial UI is precomputed and read-only.
- Multi-provider positions are visible per review target.
- Convergence score is defined as claimed successful providers divided by all
  successful providers.
- One target shows technical convergence; incident baseline remains open.
- The More data rescore demo shows the AI improvement cycle without public write
  access or live model execution.
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
