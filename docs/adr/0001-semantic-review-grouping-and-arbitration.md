# ADR 0001: Semantic Review Grouping and Auditable Arbitration

- Status: Accepted
- Date: 2026-07-18

## Context

Different providers can describe the same operational failure while citing nearby but non-identical Evidence IDs. Requiring exact Evidence ID equality fragments those conclusions. Grouping only by subsystem has the opposite failure mode: distinct failure types in the same operational area can be rolled into one review target.

Provider conclusions can also contain support and opposition at the same time. For example, two providers may support a hypothesis while another supplies counter-evidence, a caveat, or a missing-evidence requirement. A single `agreement OR disagreement` state cannot represent that result.

The grouping and arbitration path must remain deterministic, portable across source projects, inspectable by an operator, and safe when profile-specific semantics are unavailable.

## Decision

1. Classify each Evidence Item into a portable semantic identity:
   - `event_family` is a broad operational failure domain.
   - `event_name` is a concrete event or failure type.
   - `template_fingerprint` separates weak or unknown event names without depending on source-specific identifiers.
   - `classification_source` and `classification_confidence` preserve provenance.
   - Profile rules apply only with `semantic_rule_trust=human_approved` or `packaged_allowlist`.
   - An approved operational profile may override generic semantics while retaining the complete pre-override `generic_classification` and applied `profile_override` for audit.

2. Form a canonical review group from:
   - Evidence Bundle SHA,
   - canonical review unit,
   - optional canonical review family.

   The Evidence Bundle SHA identifies the complete input bundle. It does not compare individual Evidence ID sets. Within one run, semantic review unit and optional family perform the meaningful split, which absorbs small Evidence ID shifts but can also create over-rollup risk.

3. Treat agreement and disagreement as independent signals:
   - agreement is present when at least two distinct providers have an effective support stance;
   - support with `finding_status=insufficient_evidence` or `no_finding` does not count as support;
   - disagreement is present when counter-claims, caveats, validation claims, insufficient-evidence conclusions, or missing evidence are present;
   - both signals may be true for the same group.

   Unsupported is a third independent validity signal. Missing or unknown Evidence references, including `counter_evidence_refs`, do not represent provider disagreement; they are excluded from agreement/disagreement scoring and routed to Auto Archive as invalid citations.

4. Treat distinct target types as divergence:
   - preserve `source_candidate_type_counts` and `distinct_target_type_count`;
   - apply a bounded divergence penalty instead of a convergence bonus;
   - preserve provider candidate-membership counts, supporting- and countering-provider counts, source-candidate counts, and pre-rollup candidates;
   - expose these fields and a visible warning in the human review UI.

   The legacy keys `target_type_votes` and `provider_vote_counts` remain compatibility aliases. They are counts of source-candidate types and provider candidate memberships, not majority votes. Provider convergence bonuses, baseline support scores, and rollup ratios use distinct supporting providers only.

## Alternatives Considered

### Exact Evidence ID set equality

Rejected because providers frequently select adjacent lines or different representatives of the same pattern. Exact equality would turn citation drift into artificial disagreement and duplicate review targets.

### Subsystem-only grouping

Rejected because a subsystem can contain independent restart, runtime exception, resource pressure, dependency, and configuration failures. It is too coarse as the sole semantic key.

### Exact target-type grouping

Rejected as the only rule because providers may use different but related type labels for the same operational mechanism. Target type is retained as an auditable source-candidate count and divergence signal instead.

### Exclusive agreement/disagreement states

Rejected because mixed support, counter-evidence, caveats, and missing evidence are valid simultaneous observations.

### Embedding or model-based clustering

Deferred because it introduces non-determinism, additional provider dependency, threshold tuning, and a more difficult audit trail. It may later be used only as a review hint, not as the canonical grouping authority.

## Consequences

Positive consequences:

- Citation drift is less likely to fragment one operational issue.
- Generic inputs receive useful semantics without source-specific rules.
- Operators can see when a reduced review queue hides multiple source candidates or target types.
- Arbitration represents mixed provider evidence without forcing a false binary state.

Trade-offs:

- Canonical grouping can still over-roll up distinct failures that share a review unit and family.
- Weak generic classifications rely on template fingerprints and may split semantically equivalent text variants.
- UI warnings improve detection but do not automatically split a group; the operator remains the final authority.

## Validation

- Unit tests cover generic classification, approved-profile overrides, semantic chunk keys, independent group signals, divergence penalties, and public review audit fields.
- The 45,000-row regression corpus was re-sanitized with the current rules before bundle construction. All rows were accepted, the resulting 1,036 Evidence Items passed the model-input safety preflight, and the Evidence Bundle SHA is `b7d56da85abe109ab044e05d4fc7b40462615e5b230db2b570f717c83762ab96`.
- Prompt-bounded token estimation planned 45 provider chunks with no budget overruns: Gemini 10, GPT OSS 10, Mistral 5, Qwen 10, and Gemma 10.
- Real-provider run `stream-v3-runtime-45k-semantic-real-api-20260718-v3` completed successfully in 8 minutes 38 seconds. All five providers produced schema-valid final outputs and all 45 final chunks succeeded.
- The retry ledger retained eight transient records: four HTTP 429 rate limits, three empty-response provider errors, and one schema-invalid response. All were recovered; the final failed-chunk count was zero.
- The HTTP 429 records coincided with API activity outside this Job and are treated as shared provider-quota contention, not as evidence that the semantic chunk boundaries were too large. Retry and backoff handle that contention; chunk repartitioning is not adopted as a rate-limit mitigation.
- The resulting deterministic review artifact contains seven internal validation targets. The public projection reduces this to six operator-visible validation targets plus six monitor-only and two archived observations; no cause was auto-promoted.
