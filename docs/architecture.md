# Architecture

Ops Evidence Synthesis is a local-first incident evidence pipeline. The core
contract is simple: raw operational material stays local, sanitized evidence is
packaged into a stable Evidence Bundle, and downstream AI/model output is
treated as review input rather than truth.

## Data Flow

```text
raw logs / local artifacts
  -> inspect locally
  -> sanitize locally
  -> verify sanitized output
  -> build Evidence Bundle
  -> run providers
  -> validate model output
  -> ingest and normalize provider output
  -> build canonical review graph
  -> review queue and Evidence Request Planner
```

The local development store is SQLite. The production-oriented schema is
represented by `gcp/bigquery/schema.sql` and uses these logical datasets:

- `ops_evidence_raw`
- `ops_evidence_core`
- `ops_synthesis`

## Pipeline Progress

Pipeline progress is a first-class operational artifact. API and workflow entry
points append events to `pipeline_events` and update the current run in
`pipeline_runs`. This makes status visible even when the work is triggered by a
manual deployment, a browser action, or a workflow worker endpoint.

`pipeline_events` is append-only and rebuildable into the current state.
`pipeline_runs` is a derived snapshot for search, sharing, and UI rendering.
The snapshot carries the current stage, `blocking_reason`, provider frontier
counts, review-target counts, validation-target counts, and child bundle count.
Events carry provider IDs, artifact IDs, input/output hashes, and normalized
reason codes so a failed or waiting workflow can explain where it stopped.

Tracked operations include bundle upload, Multi-AI analysis, synthesis,
provider-only model stages, Evidence Request Planner generation, more-data
refresh, remote collection, and review decisions. The UI reads the same status
contract through `GET /pipeline-status` and renders the latest run for the
selected Evidence Bundle.

## Core Contracts

- Raw logs, raw source files, raw environment values, cookies, credentials, and
  token bodies are not intended to leave the operator's environment.
- Evidence Bundles are canonical JSON documents with a stable SHA256 over the
  evidence content.
- `created_at` and other generation timestamps are metadata, not part of the
  stable evidence hash.
- Every runtime support claim must cite an `evidence_id`.
- Source context, profile context, human answers, and model interpretation are
  context. They are not runtime evidence.
- Score is review priority. It is not truth probability.
- Provider agreement is a baseline review signal. It is not majority-vote truth.
- Provider disagreement is routed to validation targets and evidence requests.

## Runtime Modes

Local mode uses deterministic providers for tests, demos, and offline
development. Real-provider mode is opt-in and can use configured cloud model
providers when credentials and project access are available.

The API can be served with FastAPI, and the same core contracts are used by the
CLI, local UI, Cloud Run deployment, and workflow endpoints.

## Source Context

Source context is optional and local-first. The source-first path produces a
Sanitized Source Context Bundle and a Source Analysis Bundle before profile
approval:

```text
local source/config/unit/env summaries
  -> sanitize-source
  -> source_context_bundle.json
  -> analyze-source
  -> source_analysis_bundle.json
  -> discover-profile
  -> profile_draft.json
  -> human approval
  -> approved profile
  -> Evidence Bundle / Multi-AI / Evidence Request Planner
```

The system does not upload a full source tree. Sanitized source items are short
summaries or excerpts, configuration items are structural summaries, and
environment values are represented only by safe metadata such as key name or
hash, value type, presence, and secret-like flags.

## Review Graph

The Review Target Arbitration stage builds `canonical_review_graph.v1`. This
graph separates:

- provider detection overlap
- technical baseline agreement
- incident baseline agreement
- promotion decisions
- score caps
- validation targets
- missing evidence prompts

A technical baseline can be shown even when incident baseline agreement is not
established. Primary incident candidates require cited runtime evidence and must
pass promotion gates such as impact verification, evidence diversity, caveat
checks, and missing-evidence checks.
