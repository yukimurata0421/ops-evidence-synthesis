# ADR 0002: Model-Aware Provider Result Reuse

- Status: Superseded by ADR 0003
- Date: 2026-07-18

## Context

Provider chunk results are expensive. This decision introduced a model-aware reuse identity for the configured provider, model, and prompt. It did not represent every setting capable of changing model output. The previous PostgreSQL uniqueness and lookup contract used `provider_id` plus `prompt_sha256`. A provider identifier can remain stable while its model generation changes, so that key could return output from a different model for the same prompt.

## Decision

Compute `execution_contract_sha256` from:

- provider ID,
- model name,
- prompt SHA-256.

This v1 key is a configured provider-model-prompt identity, not an exact execution contract.

Use `(provider_id, execution_contract_sha256)` as the PostgreSQL uniqueness contract and use the same execution-contract hash for local-ledger cache lookup. Persist the original provider, model, and prompt fields for inspection.

Legacy rows without the new hash may be read only through compatibility logic that verifies the model name. They are not treated as interchangeable across model generations.

## Alternatives Considered

### Keep provider ID plus prompt SHA-256

Rejected because changing a model behind a stable provider identifier can incorrectly reuse stale output.

### Use provider ID, model name, and prompt SHA-256 as the database key directly

Viable, but the derived contract hash gives local and PostgreSQL stores one stable lookup representation and leaves room to add explicit contract fields in a future version.

### Disable reuse

Rejected because deterministic retries and repeated review builds would make unnecessary provider calls, increase latency and cost, and complicate recovery from partial runs.

## Consequences

- Model upgrades no longer collide with results produced by an older model generation.
- Existing successful results remain reusable when provider, model, and prompt are unchanged.
- Generation settings, prompt rendering, response schema, provider adapter behavior, safety/tool policy, and resolved model revisions are not represented by v1.
- ADR 0003 replaces this limited identity with a versioned request-side execution contract and an explicit mutable-model reuse policy.

## Validation

- Store tests assert the PostgreSQL uniqueness contract.
- Cache tests assert that the same provider and prompt produce different execution contracts for different model names.
- End-to-end chunk tests verify that reusable records match provider, model, and prompt before they are accepted.
