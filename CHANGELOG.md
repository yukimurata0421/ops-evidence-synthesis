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
- Regression coverage for generic semantic classification, non-exclusive group signals, rollup divergence, review UI audit fields, execution reuse, and prompt-bounded chunk estimates.
- Architecture decisions for semantic review arbitration and model-aware provider result reuse under `docs/adr/`.

### Changed

- Generalized semantic chunking so grouping no longer depends on one source project or a fixed event taxonomy.
- Defined the canonical group key as Evidence Bundle SHA plus canonical review unit and optional review family. The SHA identifies the whole bundle; it is not an Evidence ID set comparison.
- Replaced the target-type convergence bonus with a divergence penalty when a canonical group contains conflicting target types.
- Changed provider result reuse from provider-plus-prompt identity to a hash of provider, model, and prompt.
- Aligned chunk token estimates with each provider's prompt text boundary, preventing very long sanitized messages from creating false oversized-chunk plans.

### Security

- Added a fail-closed sanitized-input verification gate before local Evidence Bundle construction.

### Verification

- Real-provider rerun results for the 45,000-row `stream_v3_runtime` corpus will be recorded here after completion.
