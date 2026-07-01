# Public Data Boundary

This repository intentionally separates three things that are often mixed
together in AI incident-review demos:

1. raw operational material,
2. row-level sanitized evidence, and
3. reviewer-facing analysis artifacts.

The public repository commits reviewer-facing analysis artifacts and public-safe
fixtures. It does not commit raw production logs, raw source trees, local
workspace databases, cloud credentials, or full row-level sanitized corpora for
private stream_v3 evidence.

## What Is Public

The public directory contains:

- `data/amazon_notify_flagship_logs.jsonl`: a 6,506-line public-safe fixture for
  deterministic local demo regeneration.
- `data/sample_logs.jsonl`: a compact public-safe fixture for smoke tests.
- `data/precomputed_review_summaries/*.json`: SHA-fixed read-only review payloads
  served by the public UI.
- `data/public_evidence_manifests/*.json`: compact manifests that tie public
  URLs, evidence hashes, provider hashes, data boundaries, and model-projection
  statistics together.
- `schemas/`: public contracts for Evidence Bundles and review output.

The public amazon-notify fixture is safe to commit because it was built for the
demo path. The stream_v3 Dell runtime and arena-server monitoring row-level
corpora are not committed, even after sanitization, because sanitized operational
logs can still expose topology, timing, incident cadence, internal service
shape, and operator workflow details that are not necessary for public review.

## What Stays Local

The following artifacts stay in the operator environment:

- raw logs,
- raw source trees,
- local `workspace/` outputs,
- local SQLite/PostgreSQL staging databases,
- private cloud staging stores such as GCS objects and BigQuery audit exports,
- full row-level `sanitized_events.jsonl` corpora for private systems,
- API credentials and provider runtime configuration.

Public reviewers do not need those local files to inspect the product behavior.
They can use the live UI, the fixed payload JSON, and the public evidence
manifests to verify what was analyzed and how the model input was bounded.

## Analysis Flow

The source-aware real-provider path is:

1. collect raw logs and source locally,
2. sanitize logs and build a SHA-fixed Evidence Bundle,
3. generate sanitized source context and source-analysis bundles,
4. discover or approve a System Profile,
5. persist the sanitized corpus in the analysis store,
6. assign every sanitized row to the coverage ledger,
7. group the corpus into Evidence Items,
8. build provider-specific Evidence Chunks,
9. run provider x chunk jobs and validate JSON against schema,
10. deterministically merge chunk claims into a canonical review graph,
11. publish a read-only payload and manifest.

This means the public URL is not a live model execution page. It is a stable
review surface for a completed analysis. The provider outputs, graph summary,
payload hash, input fingerprint, and model-projection counts are fixed.

## Token Compression

The system does not pass every log row as raw prompt text. It compresses the
sanitized corpus in two stages.

First, row-level sanitized events are grouped into Evidence Items. Each item
keeps stable evidence IDs, counts, first/last observed timestamps, normalized
message templates, severity/service/environment metadata, and any deterministic
signals. This removes duplicated log bodies while preserving count and timing
evidence.

Second, provider prompts receive a bounded projection of the highest-signal
Evidence Items. Selection favors severity, frequency, operational relevance,
runtime state, recovery markers, and evidence needed to test competing claims.
Low-signal tail patterns remain in the stored sanitized corpus and are reflected
through corpus-level counts, not by copying row bodies into model prompts.

The chunked provider pass is broader than the prompt projection. Every grouped
Evidence Item is assigned to at least one provider chunk, including rare,
singleton, temporal, state-transition, and tail-summary evidence. This keeps
the review boundary row-complete without forcing every sanitized row body into
every model prompt.

The current real-provider public cases separate the single-prompt projection
from the chunked full-corpus provider pass:

| Case | Sanitized rows | Grouped Evidence Items | Prompt Evidence Items | Prompt occurrences | Prompt coverage | Provider chunks | Provider item coverage |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| amazon-notify real API | 44,944 | 8,519 | 140 | 34,774 | 77.4% | 105 | 100.0% |
| stream_v3 Dell runtime | 45,000 | 1,012 | 140 | 107,160 | 99.2% | 33 | 100.0% |
| stream_v3 arena-server monitoring | 50,000 | 21 | 21 | 63,056 | 100.0% | 18 | 100.0% |

The prompt coverage column is an inspection metric, not an analysis cutoff.
Provider calls run over chunked Evidence Corpora and retain source Evidence
Item IDs through the merge step. Evidence that is outside the bounded projection
is still assigned to the ledger and covered by a chunk unless it is explicitly
recorded as unassigned; the public cases above have zero unassigned Evidence
Items.

## Public Proof Without Publishing Private Rows

For each real-provider run, the public manifest records:

- the live review URL,
- the precomputed payload path,
- the evidence SHA256,
- the pipeline run ID,
- the canonical graph SHA256,
- the input fingerprint SHA256,
- the provider output hashes,
- the sanitized row count,
- the Evidence Item count,
- the model-projection count and occurrence coverage,
- the review outcome counts.

That gives reviewers a stable audit trail without exposing private row-level
operational data. The manifest is intentionally small enough to read by hand and
strict enough to validate in tests against the fixed payload.

## How To Verify Locally

Run:

```bash
make verify-precomputed
make test
```

The test suite checks that public manifests match their corresponding
precomputed payloads, that row counts and projection counts are consistent, and
that the manifests do not point at local workspace files as public artifacts.
