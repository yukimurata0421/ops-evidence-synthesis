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
   - An approved operational profile may override generic semantics while retaining the generic result for audit.

2. Form a canonical review group from:
   - Evidence Bundle SHA,
   - canonical review unit,
   - optional canonical review family.

   The Evidence Bundle SHA identifies the complete input bundle. It does not compare individual Evidence ID sets. Within one run, semantic review unit and optional family perform the meaningful split, which absorbs small Evidence ID shifts but can also create over-rollup risk.

3. Treat agreement and disagreement as independent signals:
   - agreement is present when the configured provider-support condition is met;
   - disagreement is present when counter-claims, caveats, validation claims, unsupported conclusions, or missing evidence are present;
   - both signals may be true for the same group.

4. Treat distinct target types as divergence:
   - preserve `target_type_votes` and `distinct_target_type_count`;
   - apply a bounded divergence penalty instead of a convergence bonus;
   - preserve provider votes, source-candidate counts, and pre-rollup candidates;
   - expose these fields and a visible warning in the human review UI.

## Alternatives Considered

### Exact Evidence ID set equality

Rejected because providers frequently select adjacent lines or different representatives of the same pattern. Exact equality would turn citation drift into artificial disagreement and duplicate review targets.

### Subsystem-only grouping

Rejected because a subsystem can contain independent restart, runtime exception, resource pressure, dependency, and configuration failures. It is too coarse as the sole semantic key.

### Exact target-type grouping

Rejected as the only rule because providers may use different but related type labels for the same operational mechanism. Target type is retained as an auditable vote and divergence signal instead.

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
- A fixed 45,000-row real-provider corpus is used as a regression case for chunk coverage and post-merge review behavior.

