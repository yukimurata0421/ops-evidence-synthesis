# ADR 0003: Versioned Provider Execution Contract and Cache Reuse Policy

- Status: Accepted
- Date: 2026-07-18
- Supersedes: ADR 0002

## Context

ADR 0002 prevented reuse across configured model-name changes, but its v1 hash covered only provider ID, model name, prompt SHA-256, and the output contract name. Output may also change when the rendered model input, prompt instructions, response schema, generation settings, provider adapter, tool contract, safety policy, or model revision changes.

Model names may also be mutable aliases such as `latest`, `default`, `preview`, `experimental`, `beta`, `flash`, or `flash-lite`. A Provider can serve a different model implementation behind the same requested name. Provider-returned model identity is often unavailable until after a request, so a response model ID cannot always participate in the pre-request cache lookup key.

Evidence Bundle SHA identifies the complete input bundle. It does not identify the exact provider-specific, compacted chunk input or the execution behavior applied to that input.

## Decision

Use `provider_execution_contract.v2` as the request-side reuse identity. Compute `execution_contract_sha256` from the Canonical JSON representation of the entire contract.

The contract separates the following dimensions:

- `input.model_input_sha256`: the exact compacted model-input object for this provider and chunk;
- `prompt_contract.prompt_contract_sha256`: prompt name, prompt template hash, input projection settings, prompt renderer version, and response schema version;
- `prompt_contract.rendered_prompt_sha256`: the complete rendered prompt sent by the adapter;
- `generation_config`: effective output-affecting settings such as temperature, maximum output tokens, response configuration, and thinking level;
- `provider`: provider ID, adapter identity, explicit adapter version when available, and adapter source hash;
- `model`: requested model name, request model ID, pre-resolved model identity/revision when available, and mutable-alias classification;
- `request_protocol`: output-relevant API version and model-serving location;
- `safety_policy`, `tool_contract`, and `generation_policy`: versioned execution policies;
- `reuse_policy`: separate within-run and cross-run reuse decisions.

Operational scheduling settings that do not change a successful model output are excluded. These include timeout, retry count, backoff, worker count, pacing, database connection settings, and lock timeouts. A setting that changes the model, prompt, tools, or generation request must be included even if it is used only during fallback.

Continue using `(provider_id, execution_contract_sha256)` as the PostgreSQL uniqueness key. Persist the version and complete contract JSON beside searchable audit columns:

- model input SHA-256;
- prompt contract SHA-256;
- requested model name;
- resolved model name and revision;
- Provider response model ID;
- mutable-model flag;
- cross-run cache reuse policy.

Legacy v1 rows remain available for audit and retry administration, but v2 cache lookup accepts only a matching v2 contract. There is no implicit cross-version reuse.

## Mutable Model Alias Policy

Within-run reuse remains allowed so retry and duplicate scheduling can use a result created during the current run.

Cross-run reuse is default-deny for every model name when no immutable revision is resolved before the request. Cross-run reuse becomes eligible only when an immutable revision is available or an adapter supplies an explicit audited policy; mutable-alias detection remains audit metadata and an explanatory reason, not the only safety gate. Adapter source identity must also be available.

The post-request record stores any Provider-returned model ID and resolved name. This observation is audit metadata and is not retroactively inserted into the pre-request key.

## Validation Provenance

Every written multi-provider output directory includes `validation_provenance.json` with:

- implementation commit SHA and whether relevant source changes were present;
- Evidence Bundle SHA-256;
- requested, resolved, and Provider-returned model identities;
- SHA-256 for `multi_ai_run.json`, `canonical_review_graph.json`, and the other review artifacts;
- explicit public-projection and canonical-graph artifact references.

Published validation must also state the exact test command and result count. A clean implementation commit and the provenance manifest together identify the input, code, execution contract, and resulting review artifacts.

Implementation validation for this decision:

```text
Private implementation checkout:
  Commit: b586a5647ca5d3bbed0f2c46c7c7e7ebd3cbf803
  Python: 3.14.4
  Checkout state: clean main
  Command: make ci
  Result: 518 passed, 3 skipped, 1 warning in 50.96s
Public main checkout:
  Commit: 21fbed35c74bd2950213142d57a33d22536a042e
  Python: 3.14.4
  Checkout state: clean main
  Command: make ci
  Result: 524 passed, 3 skipped, 1 warning in 58.88s
Provider API calls: none
Runtime artifact implementation SHA: recorded by validation_provenance.json for each executed build
```

The 45,000-row validation remains relevant to chunking and recovery behavior:

- 1,036 Evidence Items were processed as 45 chunks across five Providers;
- all 45 final chunks succeeded in 8 minutes 38 seconds;
- retry and backoff recovered eight transient records;
- HTTP 429 was treated as shared quota contention, not assumed to be a chunk-size defect;
- rate-limit handling did not repartition semantic chunks;
- no cause was automatically promoted.

This real-provider run validates semantic chunking, retry recovery, and deterministic arbitration. It predates the final default-deny cross-run policy and does not independently validate every reuse branch of `provider_execution_contract.v2`.

## Alternatives Considered

### Keep the v1 model-aware identity

Rejected as the final design because unchanged provider, model, and input names can still hide prompt, schema, generation, adapter, or model-revision changes.

### Include Provider response model ID in the lookup key

Rejected as the sole solution because that value is usually known only after the request. It is retained as post-request audit evidence.

### Include every runtime setting

Rejected because retry, timeout, concurrency, and storage settings do not normally change a successful model result. Including them would create avoidable cache misses and obscure the boundary between execution identity and operational scheduling.

### Allow mutable aliases across runs by default

Rejected because the requested name does not prove that the served model implementation is unchanged.

## Consequences

- Prompt, schema, generation, adapter, tool, safety, input-projection, and model revisions invalidate stale cache entries.
- Model input identity and prompt execution identity can be inspected independently.
- Models without a pre-resolved immutable revision or explicit audited policy cannot reuse results across independent runs and may incur additional Provider requests.
- Adapter source changes conservatively invalidate cache entries, including some changes that may not affect output.
- v1 result rows remain audit history but are not silently treated as v2-compatible.
