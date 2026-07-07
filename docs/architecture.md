# Architecture

Ops Evidence Synthesis is a local-first incident evidence pipeline. The core
contract is simple: raw operational material stays local, sanitized evidence is
packaged into a stable Evidence Bundle, and downstream AI/model output is
treated as review input rather than truth.

## Data Flow

The current implementation has two bounded input streams. Log data becomes
evidence. Source and profile data become context only. They meet only after
both streams have been sanitized and explicitly approved.

### Log Evidence Flow

```text
raw logs or local log exports
  -> inspect locally
  -> sanitize locally
  -> verify sanitized output
  -> sanitized_events.jsonl / manifest.json / redaction_report.json
  -> build evidence_bundle.v1 for the selected incident window
  -> group sanitized events into Evidence Items and local signals
  -> attach optional db_corpus_coverage row-assignment summary
  -> run_multi_ai builds a model_input policy from the Evidence Bundle
  -> split Evidence Items into provider-specific full-corpus chunks
  -> run provider x chunk calls
  -> record model_run artifacts, chunk records, output hashes, parse status, and schema status
  -> merge recorded chunk outputs deterministically
  -> build canonical_review_graph.v1 and review-target projection
  -> precomputed review payload / review queue / Evidence Request Planner
  -> read-only summary, graph, detail, and markdown report UI
```

Raw log rows are not copied directly into provider prompts. The local-first CLI
path writes normalized sanitized events and then builds a SHA-fixed Evidence
Bundle from the incident window. The DB-backed path can additionally attach
`db_corpus_coverage`, which records how sanitized rows were assigned to
Evidence Items and review chunks.

### Source And Profile Context Flow

```text
raw source/config/unit/env files
  -> sanitize-source locally
  -> source_context_bundle.v1
  -> analyze-source locally
  -> source_analysis_bundle.v1
  -> discover-profile
  -> draft-profile or draft-focused-profile
  -> human approve-profile
  -> approved profile
  -> approved_profile_context / source_context_context / source_analysis_context
  -> model input context for Multi-AI, Review Arbitration, and Evidence Request Planner
```

Source context and approved profile data do not prove runtime behavior. They
help route components, interpret metric names, and turn unanswered operational
questions into missing evidence. Runtime support still has to cite Evidence
Items by `evidence_id`.

### Execution And Storage Roles

SQLite remains useful for local fixtures, offline demos, and lightweight
smoke tests. It stores sanitized logs, Evidence Bundles, model runs, parsed
results, model output artifacts, review targets, canonical review graph
snapshots, pipeline runs, and pipeline events.

The production-oriented deployment uses distinct stores:

- Private GCS stages sanitized input artifacts and recorded job outputs for
  Cloud Run Jobs: Evidence Bundle, approved profile, sanitized source context,
  sanitized source analysis, `multi_ai_run.json`, provider artifacts, and
  precomputed review payloads.
- PostgreSQL is the optional low-latency provider chunk ledger. The implemented
  tables are `provider_chunk_runs` and `provider_chunk_attempts`; they support
  retry/backoff, resumable provider work, and append-only attempt history.
- BigQuery is the audit warehouse. `ops_evidence_raw` stores sanitized log
  rows, `ops_evidence_core` stores derived log patterns and metric windows, and
  `ops_synthesis` stores Evidence Bundles, model runs, parsed results, output
  artifacts, pipeline progress, canonical review graphs, comparison records,
  review targets, and review decisions.
- The public Cloud Run UI can run in precomputed-read mode, where reviewers
  inspect fixed payloads and manifests instead of triggering live model calls.

Cloud workflows and jobs remain Evidence Bundle centric. They can consume
sanitized source/profile artifacts as context, but they do not collect raw
source trees or raw environment values.

## Full-Corpus Coverage Ledger

The corpus boundary is row-complete for DB-backed full-corpus runs. Every
sanitized DB row in the analysis window is assigned before model execution. The
public local-first CLI path may only expose grouped Evidence Items, but the
production-style bundle builder records a `db_corpus_coverage` summary with
row counts, coverage ratios, coverage classes, row-assignment hash, direct
prompt row count, and `raw_rows_sent_to_providers = false`.

Coverage classes separate prompt inclusion from evidence accounting:

- `pattern`: repeated operational evidence.
- `rare`: low-frequency but meaningful evidence.
- `singleton`: one-off evidence that should not disappear behind frequency
  ranking.
- `temporal_bucket`: spikes, gaps, bursts, and time-window summaries.
- `state_transition`: checkpoints, restarts, frontier movement, and service
  state changes.
- `tail_summary`: low-signal remainder that is not directly quoted in a prompt
  but remains visible as a review boundary.

This means the system does not define coverage as "how many raw rows were
copied into a prompt." Provider prompts operate on chunked Evidence Corpora,
while prompt-excluded tail evidence remains visible through corpus counts,
coverage classes, and chunk manifests. A completed full-corpus run should have
zero unassigned Evidence Items for the sanitized corpus it claims to review.

## Chunked Provider Execution

Providers do not receive raw databases. `run_multi_ai` first builds a model
bundle from the Evidence Bundle plus approved context, then splits sanitized
Evidence Items into provider-specific chunks bounded by each provider's token
budget and item limit. The chunk manifest records chunk IDs, chunk type,
Evidence Item IDs, source row counts, time range, coverage classes, prompt
hashes, and the provider that received the chunk.

Provider execution is tracked as `provider x chunk` jobs. The scheduler can
claim ready work with PostgreSQL row locks, run chunks in parallel, and resume
only unfinished work after a crash or quota wait. Successful outputs are stored
and hashed. Attempts are append-only so rate limits, timeouts, schema failures,
and provider errors remain observable instead of being overwritten by a later
success.

Provider result state is kept distinct from review stance:

- `claimed`: the provider read the chunk and surfaced the review unit.
- `silent`: the provider read the chunk successfully but did not surface the
  review unit.
- `conflict`: the provider read the chunk and supplied a contrary claim.
- `provider_error`: the provider did not complete the chunk.
- `schema_invalid`: the provider responded but did not satisfy the output
  contract.
- `retry_exhausted`: the provider could not produce a valid result within the
  configured retry policy.

The canonical review graph is generated by a deterministic merge over recorded
provider outputs. Merge input is sorted and de-duplicated by stable IDs so
parallel completion order cannot change the graph. Real provider text is
recorded and hashed rather than treated as byte-reproducible; byte-level
reproducibility is reserved for deterministic fixtures and for the merge over
recorded artifacts.

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

## Review Priority Scoring

Review priority is now computed as an explainable ranking signal rather than a
flat provider-count cap. The score combines weighted provider support, Gemini
signal, cited-evidence volume, evidence-family diversity, source-candidate
breadth, operational actionability, blockers, and a tiny deterministic
tie-break. Gemini receives a higher provider weight because the hackathon path
is Google Cloud/Gemini-led, but Gemini agreement is still not treated as truth.

The public detail pages expose the scoring breakdown per target. A target can
therefore show why it ranks high, why `generic_runtime` or healthy-status
targets are pushed down, and why a target with Gemini silent is not equivalent
to one where Gemini supported the same review unit. Promotion remains
human-gated regardless of review priority.

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
  -> code profile review URL
  -> terminal APPROVE
  -> Evidence Bundle / Multi-AI / Evidence Request Planner
```

The system does not upload a full source tree. Sanitized source items are short
summaries or excerpts, configuration items are structural summaries, and
environment values are represented only by safe metadata such as key name or
hash, value type, presence, and secret-like flags.

The focused profile path is Gemini-backed and intentionally narrow. In the GCS
handoff review flow, this happens before log analysis: Gemini Pro receives
sanitized source context and source analysis, then the operator reviews the
static code profile URL, records answers in the Human Review Form, and types
`APPROVE` in the terminal before any log Evidence Bundle is built. It asks what
system is being reviewed, what is logged or measured, which runtime components
matter, and what orchestration or watchdog loop is visible from sanitized
source analysis. It is still a draft: source context is not incident evidence,
runtime support claims require `evidence_id`, and every collector remains
read-only until human approval.

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
