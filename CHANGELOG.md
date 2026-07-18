# Changelog

All notable changes to this project are documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and dates use UTC.

## [Unreleased]

## [0.2.0] - 2026-07-18

### Added

- Portable semantic classification for Evidence Items using `event_family`, `event_name`, `template_fingerprint`, classification provenance, and optional approved-profile overrides.
- Independent agreement, disagreement, and unsupported-validity signals for mixed provider conclusions and invalid citations.
- Stance-specific provider sets and Evidence references, including participating, supporting, countering, caveat, validation, and insufficient-evidence membership.
- Canonical rollup audit data with source-candidate type counts, provider candidate-membership counts, supporting- and countering-provider counts, source-candidate counts, and distinct target-type counts.
- Review UI warnings and source-candidate details for canonical groups that may contain multiple failure types.
- Model-aware provider execution contract hashes for safe PostgreSQL and local-ledger result reuse.
- Versioned provider execution contract v2 covering exact compacted model input, rendered prompt, response schema, generation settings, adapter source, request protocol, safety/tool policy, and model revision metadata.
- `validation_provenance.json` with implementation revision, source state, Provider model identities, and SHA-256 values for generated review artifacts.
- Derived public-artifact lineage with source multi-run, Provider output, and per-chunk output SHA-256 values.
- Separate tested-implementation, artifact-generation, published-repository, and deployed-image revision roles in validation provenance.
- PostgreSQL audit columns for the execution contract JSON/version, input and prompt-contract hashes, requested/resolved model identities, mutable aliases, and cache reuse policy.
- Regression coverage for generic semantic classification, non-exclusive group signals, rollup divergence, review UI audit fields, execution reuse, and prompt-bounded chunk estimates.
- Architecture decisions for semantic review arbitration, model-aware reuse history, and the versioned provider execution contract under `docs/adr/`.

### Changed

- Generalized semantic chunking so grouping no longer depends on one source project or a fixed event taxonomy.
- Defined the canonical group key as Evidence Bundle SHA plus canonical review unit and optional review family. The SHA identifies the whole bundle; it is not an Evidence ID set comparison.
- Replaced the target-type convergence bonus with a divergence penalty when a canonical group contains conflicting target types.
- Changed provider result reuse from provider-plus-prompt identity to a hash of provider, model, and prompt.
- Replaced the limited v1 provider-model-prompt identity with a Canonical v2 request contract; legacy v1 rows are retained for audit but cannot satisfy v2 cache lookups.
- Changed cross-run cache reuse to default-deny unless an immutable model revision is pre-resolved or an explicit audited adapter policy allows reuse; preview, experimental, beta, flash, and flash-lite aliases are marked mutable.
- Routed one-chunk provider runs through the same versioned execution-contract Ledger/cache path as multi-chunk runs.
- Required explicit semantic-rule trust and retained complete generic classification plus profile-override audit objects.
- Changed agreement and rollup convergence scoring to use distinct supporting providers rather than all participating providers.
- Re-synthesize immutable recorded Provider outputs before public projection so the Flagship applies the current stance-aware Agreement contract without new Provider API calls.
- Recorded Provider-returned model identifiers as post-request audit observations rather than incorrectly treating them as pre-request cache inputs.
- Aligned chunk token estimates with each provider's prompt text boundary, preventing very long sanitized messages from creating false oversized-chunk plans.

### Security

- Added a fail-closed sanitized-input verification gate before local Evidence Bundle construction.

### Removed

- Removed internal demo-production notes, staged capture instructions, editable overlays, and historical screenshots from the public `main` tree while retaining the final video and current reviewer-facing records.

### Verification

- Re-sanitized all 45,000 `stream_v3_runtime` rows with the current rules; the model-input safety preflight reported zero secret, IP, home-path, and internal-URL findings.
- Verified prompt-bounded planning for 1,036 Evidence Items: 45 chunks across five providers with no estimated token-budget overruns.
- Completed real-provider run `stream-v3-runtime-45k-semantic-real-api-20260718-v3` in 8 minutes 38 seconds with 5/5 schema-valid providers and zero final failed chunks.
- Confirmed retry recovery from four HTTP 429 records, three empty-response provider errors, and one schema-invalid response without abandoning the provider run.
- Kept semantic chunk boundaries independent of rate-limit handling; shared-quota contention uses retry and backoff without repartitioning evidence chunks.

## [0.1.0] - 2026-07-12

### Added

- Initial public hackathon release with a local-first evidence pipeline, deterministic review paths, a Cloud Run review UI, and a five-provider recorded incident review.

[Unreleased]: https://github.com/yukimurata0421/ops-evidence-synthesis/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/yukimurata0421/ops-evidence-synthesis/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/yukimurata0421/ops-evidence-synthesis/releases/tag/v0.1.0
