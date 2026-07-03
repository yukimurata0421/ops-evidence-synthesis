# stream_v3 Real API Source-Aware Runs

This document records the current public stream_v3 real-provider analyses for
two separate evidence sets:

- Dell runtime evidence
- arena-server monitoring-plane evidence

Both runs used sanitized stream_v3 source context and double-sanitized log
corpora. Raw logs and raw source stayed local. The public URLs serve fixed
precomputed payloads and do not run providers on page load.

The current run is a source-aware reinterpretation pass: the pipeline returns to
sanitized code/profile context, then reinterprets each sanitized log corpus with
real provider APIs. Dell runtime and arena-server monitoring use different
approved focused profiles and different source context hashes.

## Public URLs

### Dell runtime

- Full review page: https://ops-evidence.yukimurata0421.dev/ui/full-review-page?evidence_sha256=345430d258752cefef81bfb587b4c210799d02bfc849e0a7ac5dc4c48fddb1d6
- Human-readable API view: https://ops-evidence.yukimurata0421.dev/ui/api?evidence_sha256=345430d258752cefef81bfb587b4c210799d02bfc849e0a7ac5dc4c48fddb1d6
- Visual review graph: https://ops-evidence.yukimurata0421.dev/ui/review-graph?evidence_sha256=345430d258752cefef81bfb587b4c210799d02bfc849e0a7ac5dc4c48fddb1d6
- JSON review graph: https://ops-evidence.yukimurata0421.dev/review/graph?evidence_sha256=345430d258752cefef81bfb587b4c210799d02bfc849e0a7ac5dc4c48fddb1d6

### arena-server monitoring

- Full review page: https://ops-evidence.yukimurata0421.dev/ui/full-review-page?evidence_sha256=6b7dad773b78274ed9706b02e15478427ad8817e8d8330ba19487d4293eeb3d3
- Human-readable API view: https://ops-evidence.yukimurata0421.dev/ui/api?evidence_sha256=6b7dad773b78274ed9706b02e15478427ad8817e8d8330ba19487d4293eeb3d3
- Visual review graph: https://ops-evidence.yukimurata0421.dev/ui/review-graph?evidence_sha256=6b7dad773b78274ed9706b02e15478427ad8817e8d8330ba19487d4293eeb3d3
- JSON review graph: https://ops-evidence.yukimurata0421.dev/review/graph?evidence_sha256=6b7dad773b78274ed9706b02e15478427ad8817e8d8330ba19487d4293eeb3d3

## Fixed Artifacts

| Artifact | Dell runtime | arena-server monitoring |
| --- | --- | --- |
| Evidence SHA256 | `345430d258752cefef81bfb587b4c210799d02bfc849e0a7ac5dc4c48fddb1d6` | `6b7dad773b78274ed9706b02e15478427ad8817e8d8330ba19487d4293eeb3d3` |
| API revision | `real-api-stream-v3-dell-45k-5p-reinterpret-20260703T003058Z` | `real-api-stream-v3-arena-50k-5p-reinterpret-20260703T003058Z` |
| Canonical graph SHA256 | `7b8bbf364706cda1b558476b5a08c882356449710612989dbf86ca8a68cb9266` | `2350ddd8b2ac0d2dce23f3f637136871630d48f735b27769249d2d8907bca8da` |
| Input fingerprint SHA256 | `372a6d7c8ef29935ff040b53e45342854491fe5f4c37941dadef0faf7e965f4c` | `7faf89f3256066acad93992aa7f11f13138466240c1acba77fd27dd3ffcb938d` |
| Source context SHA256 | `3b124da80b8ba7176004f06223742a6a1779225f3008ed3251b03dfbe2db12d2` | `a312fd5e4df8c2085f259581fa811cfee54978e14ad32b788708fb36e346fbd4` |
| Source analysis SHA256 | `6832aa7e5926dbc0ecb4f9d9e4d16e97cac27630853a2bb9e627c52f4c34b0cb` | `6af86a8571062130eedb5e62de03b07b0fdf2d10fc4d6eddb5b05c3b1079c65c` |
| Public payload | `data/precomputed_review_summaries/345430d258752cefef81bfb587b4c210799d02bfc849e0a7ac5dc4c48fddb1d6.json` | `data/precomputed_review_summaries/6b7dad773b78274ed9706b02e15478427ad8817e8d8330ba19487d4293eeb3d3.json` |
| Payload SHA256 | `3052f72169715f416f168e72fe970c7be6f2bc209fbcd201cd9b8660673fc7f3` | `9e54f51f0259be8524d35518378b76ce6ad3f95e145e0af2b7528e760c96df74` |

## Analysis Windows

The local source files used for staging contained more than the accepted public
window. The accepted public windows satisfy both constraints: roughly 40,000 to
50,000 rows and at least 24 hours of evidence. The row-level files remain local
only.

| Corpus | Accepted public rows | Accepted public range | Window hours |
| --- | ---: | --- | ---: |
| Dell runtime | 45,000 | 2026-06-14T23:15:50Z to 2026-06-15T23:59:52Z | 24.733889 |
| arena-server monitoring | 50,000 | 2026-06-18T09:54:00Z to 2026-06-19T10:48:55Z | 24.915278 |

## Data Boundary

The source-aware path was:

1. Stage sanitized BigQuery exports locally.
2. Double-sanitize each selected row through the public sanitizer, including
   nested label values.
3. Verify the selected sanitized JSONL with `scan_sanitized_text`.
4. Build a SHA-fixed Evidence Bundle and DB coverage ledger.
5. Attach sanitized source context and sanitized source analysis as
   interpretation context, not incident evidence.
6. Send only sanitized Evidence Items, approved profile context, and source
   context to real provider APIs.
7. Merge recorded provider chunk outputs into the Canonical Review Graph.
8. Generate read-only public payloads under `data/precomputed_review_summaries/`.

Source context is not incident evidence. Runtime and monitoring claims still
have to cite Evidence Item IDs from the sanitized corpus.

## Full-Corpus Coverage

The single-prompt projection is an inspection slice. The real-provider run uses
chunked full-corpus review so that lower-frequency Evidence Items do not
disappear behind occurrence-weighted selection.

| Layer | Dell runtime | arena-server monitoring |
| --- | ---: | ---: |
| Sanitized DB rows in accepted window | 45,000 | 50,000 |
| Covered DB rows in ledger | 45,000 | 50,000 |
| Grouped Evidence Items retained | 1,012 | 21 |
| Evidence Items in single-prompt projection | 140 | 21 |
| Occurrences represented by selected items | 107,160 | 63,056 |
| Single-prompt occurrence coverage | 99.2% | 100.0% |
| Provider chunk count | 33 | 18 |
| Evidence Items covered by provider chunks | 1,012 | 21 |
| Provider Evidence Item coverage | 100.0% | 100.0% |
| Unassigned Evidence Items | 0 | 0 |
| Raw rows sent directly to providers | 0 | 0 |

The merge is deterministic over recorded provider outputs: provider responses
are hashed, then chunk claims are sorted and deduplicated before canonical
review graph generation.

## Public Classification Policy

Provider agreement is not enough to create a public primary candidate. If a
provider-converged target has thin cited runtime evidence, unresolved core
missing evidence, or no user-impact confirmation, the public payload keeps it as
a validation target. The review priority score can still be high; it is review
urgency, not truth probability.

## Profile Gate

Both stream_v3 profiles are generated from sanitized source/profile discovery
context and are not incident evidence. The current focused profiles are strong
enough for subsystem routing, but user outcomes and incident promotion remain
human-gated:

| Corpus | Profile status | Confidence action | Overall confidence | Confirmed outcomes | Provisional outcomes |
| --- | --- | --- | ---: | --- | --- |
| Dell runtime | `approved_context_human_gated_outcomes` | `use_for_subsystem_routing_human_gated` | 0.828 | none | Continuous YouTube streaming, ADSB data processing |
| arena-server monitoring | `approved_context_human_gated_outcomes` | `use_for_subsystem_routing_human_gated` | 0.826 | none | Maintain YouTube stream uptime, Monitor ADSB stream health |

The focused profiles do not accept critical user outcomes as facts. They create
human-gated questions about user impact, metric semantics, and diagnostic noise.
In the public payload, those questions are linked to review units whose promotion
is blocked by missing impact or operational outcome evidence.

## Provider Results

The current provider set is Gemini 3.1 Pro, GPT OSS, Mistral, Qwen, and Gemma 4.
Provider failures are not converted to silent positions. For arena-server
monitoring, GPT OSS fell below the partial-chunk usability threshold and is
exposed as a provider failure in this reinterpretation payload.

### Dell runtime

| Provider | Model | Status | Schema | Latency ms | Input tokens | Output tokens | Chunk status |
| --- | --- | --- | --- | ---: | ---: | ---: | --- |
| `gemini-enterprise-agent-platform` | `gemini-3.1-pro-preview` | ok | valid | 3,835,306 | 1,606,707 | 24,487 | ok=32 |
| `gemma-agent-platform` | `gemma-4-26b-a4b-it-maas` | ok | valid | 878,425 | 1,574,835 | 41,527 | ok=32 |
| `mistral-agent-platform` | `mistral-small-2503` | ok | valid | 124,824 | 601,032 | 11,393 | ok=7, schema_invalid=1 |
| `openai-gpt-oss-on-vertex` | `gpt-oss-20b-maas` | ok | valid | 557,410 | 1,185,529 | 88,070 | ok=32, provider_error=1 |
| `qwen-agent-platform` | `qwen/qwen3-coder-480b-a35b-instruct-maas` | ok | valid | 451,496 | 1,289,925 | 44,581 | ok=32 |

### arena-server monitoring

| Provider | Model | Status | Schema | Latency ms | Input tokens | Output tokens | Chunk status |
| --- | --- | --- | --- | ---: | ---: | ---: | --- |
| `gemini-enterprise-agent-platform` | `gemini-3.1-pro-preview` | ok | valid | 1,693,220 | 771,031 | 9,809 | ok=16, timeout=2 |
| `gemma-agent-platform` | `gemma-4-26b-a4b-it-maas` | ok | valid | 503,930 | 849,344 | 20,429 | ok=18 |
| `mistral-agent-platform` | `mistral-small-2503` | ok | valid | 20,786 | 50,852 | 1,985 | not chunked |
| `openai-gpt-oss-on-vertex` | `gpt-oss-20b-maas` | failed | invalid | 348,607 | 513,654 | 35,363 | ok=14, provider_error=4 |
| `qwen-agent-platform` | `qwen/qwen3-coder-480b-a35b-instruct-maas` | ok | valid | 210,207 | 670,814 | 21,821 | ok=18 |

The public payload preserves provider hashes so a stale deployment can be
detected by comparing the rendered provider hashes with this document.

## Measured Reinterpretation Timing

The timing below measures the source-aware reinterpretation path over existing
sanitized Evidence Bundles and focused profiles. Provider latency is the sum of
chunk latencies recorded in provider outputs; wall time is the command elapsed
time for the reinterpretation run that produced the public payload.

| Step | Dell runtime | arena-server monitoring |
| --- | ---: | ---: |
| Real API reinterpretation wall time | 207.108s | 497.909s |
| Total provider latency sum | 5,847,461 ms | 2,776,750 ms |
| Gemini 3.1 Pro provider latency sum | 3,835,306 ms | 1,693,220 ms |
| Schema-valid providers | 5/5 | 4/5 |
| Public review targets | 11 | 9 |

Dell's measured wall time is the successful resume after cached chunks from an
earlier full attempt; the first attempt was stopped after roughly 28 minutes
while provider rate-limit recovery was still active. The arena-server run
completed in one measured pass and preserved the GPT OSS partial failure as a
visible provider status.

## Review Outcome

| Corpus | Primary candidates | Validation targets | Monitor-only | Auto-archived | Incident promotion gate |
| --- | ---: | ---: | ---: | ---: | --- |
| Dell runtime | 0 | 11 | 2 | 4 | Open; all targets remain human-gated |
| arena-server monitoring | 0 | 9 | 2 | 2 | Open; all targets remain human-gated |

Both runs intentionally stop at human review. Provider convergence can create
review targets and technical support, but it does not automatically authorize a
final causal judgement or operational action. The public UI separates a
graph-level incident gate signal from each target's promotion state so "signal
present" is not read as an accepted incident cause.
