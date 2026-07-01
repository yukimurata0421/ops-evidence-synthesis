# Evidence Bundle Contract

An Evidence Bundle is the sanitized, hash-addressed artifact passed from local
collection to synthesis and review. It is the main safety boundary in Ops
Evidence Synthesis.

## Purpose

Evidence Bundles let the system analyze incidents without uploading raw logs,
raw source files, raw environment values, credential files, cookies, or token
bodies.

The recommended path is:

```text
raw operational material
  -> local sanitize
  -> local verify-sanitized
  -> sanitized normalized events
  -> DB row coverage ledger
  -> evidence_bundle.json
  -> provider input / review graph / evidence request planning
```

## Required Safety Properties

A sanitized Evidence Bundle must satisfy these properties:

- `schema_version` is `evidence_bundle.v1`.
- `bundle_type` is `sanitized_evidence_bundle`.
- `raw_log_policy` is `not_uploaded`.
- `local_first_summary.raw_logs_uploaded` is `false`.
- Raw authorization headers, cookies, API keys, password values, private key
  bodies, credential files, raw email addresses, raw IP addresses, user home
  paths, and internal URLs are not present.
- Runtime support evidence is represented by Evidence Items with stable
  `evidence_id` values.
- DB-backed bundles include `db_corpus_coverage`, which assigns every
  `logs_sanitized` row in the incident window to a grouped Evidence Item before
  provider chunking.
- The stable evidence hash excludes generation metadata such as `created_at`.

## DB Corpus Coverage

For SQLite-backed runs, the DB remains local and is not sent to providers. The
bundle builder reads the full incident window from `logs_sanitized`, groups rows
by sanitized `message_template` and `error_type`, and emits one `PATTERN-*`
Evidence Item per group. Count alone does not decide inclusion: singleton and
low-frequency groups are retained as Evidence Items.

The `db_corpus_coverage` ledger records:

- total, covered, and uncovered sanitized DB rows
- pattern count and singleton/low-frequency pattern counts
- a SHA256 over row-to-Evidence-Item assignments
- sanitized row assignment metadata for local audit

Provider calls receive the sanitized Evidence Items in bounded chunks. They do
not receive the SQLite DB file or the full row assignment ledger.

## Evidence Versus Context

The system deliberately separates evidence from context:

- Evidence Items are runtime evidence.
- System profiles describe expected behavior and interpretation rules.
- Source context describes code/config structure after sanitization.
- Human answers describe operational context.
- Model output is interpretation.

Only Evidence Items with `evidence_id` can support runtime incident claims.
Profiles, source context, human answers, and model explanations can guide
review, but they must not be cited as runtime support evidence.

## Source Context Bundles

Source context is optional and local-first. A Sanitized Source Context Bundle can
include:

- sanitized file tree summaries
- short sanitized source excerpts
- configuration structure summaries
- dependency manifest summaries
- systemd unit templates
- environment key summaries without values
- version anchoring metadata

It must not include raw source files, raw environment values, raw credential
files, or unrestricted recursive search output.

## Source Analysis Bundles

A Source Analysis Bundle contains review hints derived from sanitized source
context:

- component candidates
- metric semantics candidates
- logger mapping candidates
- instrumentation candidates
- collector mapping candidates
- profile mapping hints
- required human decisions

These mappings remain candidates until a human approves an explicit profile.

## Profile Discovery

Profile Discovery can generate a draft profile from sanitized evidence and
sanitized source context. Draft profiles are not trusted runtime profiles until
they are explicitly approved.

```text
Evidence Bundle / Source Context Bundle
  -> profile_discovery_bundle.json
  -> profile_draft.json
  -> human review
  -> approved profile
```

The approved profile can then be used by Multi-AI synthesis and the Evidence
Request Planner.

## Evidence Request Planner

The Evidence Request Planner produces a collection checklist, not an executable
agent plan. It must not run commands by itself.

Planner output can include:

- missing evidence classes
- required granularity
- read-only command templates
- metric query templates
- human questions
- sanitization steps
- child Evidence Bundle instructions

Command templates are placeholders. Operators must review and adapt them to
their own environment, run collection locally, sanitize raw output, verify the
sanitized result, and upload only a child Evidence Bundle.

Human answers are operational context. They do not become support evidence.

## Child Bundle Lineage

Follow-up collection should produce a child Evidence Bundle with lineage
metadata:

- `parent_evidence_sha256`
- `evidence_request_plan_id`
- `collection_mode`
- sanitized evidence items collected after the planner request

The UI may show Bundle Provenance for the selected bundle and Follow-up
Collections only when child bundles exist.

## Multi-Provider Output

Provider output is stored as model run artifacts and then parsed into review
inputs. Agreement is a review signal, not truth. Disagreement becomes validation
work. Missing evidence becomes an Evidence Request Planner prompt.

The review graph and promotion gates enforce these rules:

- support claims must cite `evidence_id`
- context-only support is downgraded
- single-signal evidence can be capped
- unverified user impact blocks incident promotion
- blocking caveats prevent automatic primary promotion

Score remains review priority, not truth probability.
