# Changelog

All notable changes to this project are documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and dates use UTC.

## [Unreleased]

### Added

- Portable semantic classification for Evidence Items using `event_family`, `event_name`, `template_fingerprint`, classification provenance, and optional approved-profile overrides.
- Independent `agreement_signal` and `disagreement_signal` fields for mixed provider conclusions.
- Canonical rollup audit data, including target-type votes, provider votes, source-candidate counts, and distinct target-type counts.
- Review UI warnings and source-candidate details for canonical groups that may contain multiple failure types.
- Model-aware provider execution contract hashes for safe PostgreSQL and local-ledger result reuse.
- Versioned provider execution contract v2 covering exact compacted model input, rendered prompt, response schema, generation settings, adapter source, request protocol, safety/tool policy, and model revision metadata.
- `validation_provenance.json` with implementation revision, source state, Provider model identities, and SHA-256 values for generated review artifacts.
- PostgreSQL audit columns for the execution contract JSON/version, input and prompt-contract hashes, requested/resolved model identities, mutable aliases, and cache reuse policy.
- Regression coverage for generic semantic classification, non-exclusive group signals, rollup divergence, review UI audit fields, execution reuse, and prompt-bounded chunk estimates.
- Architecture decisions for semantic review arbitration, model-aware reuse history, and the versioned provider execution contract under `docs/adr/`.

### Changed

- Generalized semantic chunking so grouping no longer depends on one source project or a fixed event taxonomy.
- Defined the canonical group key as Evidence Bundle SHA plus canonical review unit and optional review family. The SHA identifies the whole bundle; it is not an Evidence ID set comparison.
- Replaced the target-type convergence bonus with a divergence penalty when a canonical group contains conflicting target types.
- Changed provider result reuse from provider-plus-prompt identity to a hash of provider, model, and prompt.
- Replaced the limited v1 provider-model-prompt identity with a Canonical v2 request contract; legacy v1 rows are retained for audit but cannot satisfy v2 cache lookups.
- Disabled cross-run cache reuse for mutable model aliases without a pre-resolved immutable revision while preserving within-run retry reuse.
- Recorded Provider-returned model identifiers as post-request audit observations rather than incorrectly treating them as pre-request cache inputs.
- Aligned chunk token estimates with each provider's prompt text boundary, preventing very long sanitized messages from creating false oversized-chunk plans.

### Security

- Added a fail-closed sanitized-input verification gate before local Evidence Bundle construction.

### Verification

- Re-sanitized all 45,000 `stream_v3_runtime` rows with the current rules; the model-input safety preflight reported zero secret, IP, home-path, and internal-URL findings.
- Verified prompt-bounded planning for 1,036 Evidence Items: 45 chunks across five providers with no estimated token-budget overruns.
- Completed real-provider run `stream-v3-runtime-45k-semantic-real-api-20260718-v3` in 8 minutes 38 seconds with 5/5 schema-valid providers and zero final failed chunks.
- Confirmed retry recovery from four HTTP 429 records, three empty-response provider errors, and one schema-invalid response without abandoning the provider run.
- Kept semantic chunk boundaries independent of rate-limit handling; shared-quota contention uses retry and backoff without repartitioning evidence chunks.
