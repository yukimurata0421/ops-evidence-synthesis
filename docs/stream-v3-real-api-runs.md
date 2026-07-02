# stream_v3 Real API Source-Aware Runs

This document records the current public stream_v3 real-provider analyses for
two separate evidence sets:

- Dell runtime evidence
- arena-server monitoring-plane evidence

Both runs used sanitized stream_v3 source context and double-sanitized log
corpora. Raw logs and raw source stayed local. The public URLs serve fixed
precomputed payloads and do not run providers on page load.

The accepted public runs prioritize the user's requested scale target: about
40,000 to 50,000 recent rows while still satisfying the minimum 24-hour analysis
policy. Larger source files were staged locally for baseline context, but
row-level sanitized corpora are not committed.

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
| API revision | `real-api-stream-v3-dell-45k-5p-20260701T122915Z` | `real-api-stream-v3-arena-50k-5p-20260701T125019Z` |
| Canonical graph SHA256 | `39882a3aeeb9805a815164e99b9cd62bc3219d70b454db0c3572b3789e15a034` | `acf5812eb2158dad48abf469570787be624235b51dad62ec30664baf2efe196b` |
| Input fingerprint SHA256 | `7d5b76bd2af38e20635b26db50d351f82f509ff17698710c2aa78e24b4f98c79` | `4336d93ffe4e8a8229192dee049941fb42606e8abbfe94bcd31abd12810485de` |
| Source context SHA256 | `669dc2f9d33ff9ab9d73f04d8c17f8718386815d0c6e536dde14b627232637d2` | `669dc2f9d33ff9ab9d73f04d8c17f8718386815d0c6e536dde14b627232637d2` |
| Source analysis SHA256 | `451320fbd76572c4bf00be20b0ab43825d99eaa518a1b1b99c54ebf7a31e33a5` | `451320fbd76572c4bf00be20b0ab43825d99eaa518a1b1b99c54ebf7a31e33a5` |
| Public payload | `data/precomputed_review_summaries/345430d258752cefef81bfb587b4c210799d02bfc849e0a7ac5dc4c48fddb1d6.json` | `data/precomputed_review_summaries/6b7dad773b78274ed9706b02e15478427ad8817e8d8330ba19487d4293eeb3d3.json` |
| Payload SHA256 | `6d7ae438a8424cd71ed4fd2848e723c15adb5fa6aee683306cd47987eb34be91` | `787ebbf85c9e63ea208986f0bb83aa3122630c344127aea06a7057809a798ce5` |

## Analysis Windows

The local source files used for staging contained more than the accepted public
window:

| Corpus | Local staged source rows | Local staged range | Accepted public rows | Accepted public range | Window hours |
| --- | ---: | --- | ---: | --- | ---: |
| Dell runtime | 185,338 | 2026-06-02T00:00:11Z to 2026-06-15T23:59:51Z | 45,000 | 2026-06-14T23:15:50Z to 2026-06-15T23:59:52Z | 24.733889 |
| arena-server monitoring | 347,107 | 2026-06-01T00:00:10Z to 2026-06-19T10:48:54Z | 50,000 | 2026-06-18T09:54:00Z to 2026-06-19T10:48:55Z | 24.915278 |

The accepted windows were selected because they satisfy both constraints:
roughly 40,000 to 50,000 rows and at least 24 hours of evidence. The row-level
files remain local only.

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

## Profile Gate

Both stream_v3 profiles are generated from sanitized source/profile discovery
context and are not incident evidence. They are intentionally treated as lower
confidence routing context:

| Corpus | Profile status | Confidence action | Overall confidence | Confirmed outcomes | Provisional outcomes |
| --- | --- | --- | ---: | --- | --- |
| Dell runtime | `approved_context_human_gated_outcomes` | `candidate_only_requires_profile_review` | 0.69 | none | Continuous YouTube streaming; ADSB data processing |
| arena-server monitoring | `approved_context_human_gated_outcomes` | `candidate_only_requires_profile_review` | 0.69 | none | Maintain YouTube stream uptime; Monitor ADSB stream health |

The provisional outcomes are not accepted facts. They create human-gated
questions about user impact, metric semantics, and diagnostic noise. In the
public payload, those questions are linked to review units whose promotion is
blocked by missing impact or operational outcome evidence.

## Provider Results

### Dell runtime

| Provider | Model | Status | Schema | Latency ms | Input tokens | Output tokens |
| --- | --- | --- | --- | ---: | ---: | ---: |
| `gemini-enterprise-agent-platform` | `gemini-3.1-flash-lite` | ok | valid | 356,969 | 2,816,851 | 27,282 |
| `glm-agent-platform` | `zai-org/glm-5-maas` | ok | valid | 2,047,860 | 2,126,674 | 131,652 |
| `mistral-agent-platform` | `mistral-medium-3` | ok | valid | 463,430 | 993,715 | 12,543 |
| `openai-gpt-oss-on-vertex` | `gpt-oss-120b-maas` | ok | valid | 951,467 | 2,194,793 | 90,872 |
| `qwen-agent-platform` | `qwen/qwen3-coder-480b-a35b-instruct-maas` | ok | valid | 658,227 | 2,211,909 | 54,521 |

### arena-server monitoring

| Provider | Model | Status | Schema | Latency ms | Input tokens | Output tokens |
| --- | --- | --- | --- | ---: | ---: | ---: |
| `gemini-enterprise-agent-platform` | `gemini-3.1-flash-lite` | ok | valid | 200,949 | 1,343,372 | 9,740 |
| `glm-agent-platform` | `zai-org/glm-5-maas` | ok | valid | 949,143 | 1,010,033 | 65,698 |
| `mistral-agent-platform` | `mistral-medium-3` | ok | valid | 50,526 | 73,326 | 1,588 |
| `openai-gpt-oss-on-vertex` | `gpt-oss-120b-maas` | ok | valid | 570,844 | 1,020,898 | 46,571 |
| `qwen-agent-platform` | `qwen/qwen3-coder-480b-a35b-instruct-maas` | ok | valid | 332,661 | 1,030,256 | 19,589 |

Mistral finished both accepted runs. The public payload preserves provider
hashes so a stale deployment can be detected by comparing the rendered provider
hashes with this document.

## Review Outcome

| Corpus | Primary candidates | Validation targets | Monitor-only | Auto-archived | Incident promotion gate |
| --- | ---: | ---: | ---: | ---: | --- |
| Dell runtime | 3 | 13 | 2 | 2 | Open; primary candidates remain human-gated |
| arena-server monitoring | 1 | 8 | 2 | 1 | Open; primary candidate remains human-gated |

Both runs intentionally stop at human review. Provider convergence can create
review targets and technical support, but it does not automatically authorize a
final causal judgement or operational action. The public UI separates a
graph-level incident gate signal from each target's promotion state so "signal
present" is not read as an accepted incident cause.
