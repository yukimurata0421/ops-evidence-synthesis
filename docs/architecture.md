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
  -> ADK tool contract trace
  -> validate model output
  -> ingest and normalize provider output
  -> build canonical review graph
  -> persist canonical graph snapshot and review-target projection
  -> precomputed review JSON / review queue / Evidence Request Planner
  -> read-only summary/detail UI
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
- Provider agreement is a technical support signal. It is not majority-vote
  truth.
- Provider disagreement is routed to validation targets and evidence requests.

## Runtime Modes

Local mode uses deterministic providers for tests, demos, and offline
development. Real-provider mode is opt-in and can use configured cloud model
providers when credentials and project access are available.

The production-oriented workflow is Gemini-led. It runs
`gemini-enterprise-agent-platform` first as the required analysis provider, then
uses configured alternative providers as cross-checks and compares them back to
Gemini as the reference provider. This keeps Google Cloud AI at the center of
the agent loop while preserving disagreement as review work. Gemini is not a
truth source or answer key; it is the first provider and arbiter context for the
same SHA-fixed evidence.

ADK / Agent Runtime wraps this loop rather than replacing it. The pure
investigation steps are exposed as ADK-compatible tools in
`src/ops_evidence_synthesis/agents/adk_investigator.py`: freeze the Evidence
Bundle, attach sanitized source context, run provider cross-checks, validate
citations, compute review targets, arbitrate the human gate, request more
evidence, draft a system profile, and deliver the read-only review. Public
payloads store that tool contract as `agent_trace`, and environments with the
optional `agent` extra can build a `google.adk.agents.Agent` and
`vertexai.agent_engines.AdkApp` for Agent Runtime deployment.

The API can be served with FastAPI. The app bootstrap is intentionally thin:
route handlers live under `src/ops_evidence_synthesis/routes/`, while
review-page rendering lives under `src/ops_evidence_synthesis/web/`. The same
core contracts are used by the CLI, local UI, Cloud Run deployment, and
workflow endpoints.

The production-oriented Workflow runs bundle creation, provider execution,
validation, routing, scoring, optional provider comparison, and Canonical Review
Graph refresh. The graph refresh persists `canonical_review_graph.v1` and the
derived review-target projection so API/UI reads use the arbitration output as
their primary display source.

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
  -> draft-focused-profile
  -> focused_operational_profile.json
  -> profile_draft.json
  -> human approval
  -> approved profile
  -> Evidence Bundle / Multi-AI / Evidence Request Planner
```

The system does not upload a full source tree. Sanitized source items are short
summaries or excerpts, configuration items are structural summaries, and
environment values are represented only by safe metadata such as key name or
hash, value type, presence, and secret-like flags.

The focused profile path is Gemini-backed and intentionally narrow. It asks
what system is being reviewed, what is logged or measured, which runtime
components matter, and what orchestration or watchdog loop is visible from
sanitized source analysis plus sanitized runtime evidence. It is still a draft:
source context is not incident evidence, runtime support claims require
`evidence_id`, and every collector remains read-only until human approval.

`run-case` and `arbitrate-review` accept only the approved profile and sanitized
source artifacts as context inputs. Cloud workflow inputs remain Evidence
Bundle centric; raw source collection stays on the operator side of the
boundary.

## Review Graph

The Review Target Arbitration stage builds `canonical_review_graph.v1`. This
graph separates:

- provider detection overlap
- technical support signal (`technical_baseline_agreement` in the graph schema)
- incident and user-impact gate (`incident_baseline_agreement` in the graph schema)
- promotion decisions
- score caps
- validation targets
- missing evidence prompts
- More data child-bundle re-score results

A technical support signal can be shown even when incident and user-impact
promotion is not established. Primary incident candidates require cited runtime
evidence and must pass promotion gates such as impact verification, evidence
diversity, caveat checks, and missing-evidence checks.

When a persisted graph exists, `/review/graph`, `/review-targets`, summary UI,
and detail UI load it first. Legacy proposal-based target generation is kept as
a fallback for older local databases and partially completed runs.
