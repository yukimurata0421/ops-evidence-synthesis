# stream_v3 Real API Source-Approved Runs

This document records the accepted public runs for two separate evidence sets:

- stream_v3 runtime: 45,000 staged input lines
- arena-server monitoring plane: 50,000 staged input lines

Raw logs and raw source stayed local. Code was sanitized first, Gemini 3.1 Pro
produced a focused profile, human answers were normalized by Gemini, and the
reviewed interpretation was SHA-fixed. Log analysis then used only the approved
profile and sanitized Evidence Items; source access was disabled after approval.

## Public URLs

| Surface | Runtime | Monitoring |
| --- | --- | --- |
| Code Profile | https://ops-evidence.yukimurata0421.dev/code-profiles/31dd5326f0e9e052697975e7174d9de6ebf7c2fde58625cb96ce41f29faab621/ | https://ops-evidence.yukimurata0421.dev/code-profiles/a762211461c691c7392dd1ff5e774b63f1932b939329693be41017c843a94cc4/ |
| Static review | https://ops-evidence.yukimurata0421.dev/reviews/ab18d62c4e628e190345fa218834ca74276f556191d2f068a969f7922945a471/ | https://ops-evidence.yukimurata0421.dev/reviews/8d165418fca88f856d8525bbdae804b6b649455450796b2dc44d2134b21abd9a/ |
| Full review | https://ops-evidence.yukimurata0421.dev/ui/full-review-page?evidence_sha256=ab18d62c4e628e190345fa218834ca74276f556191d2f068a969f7922945a471 | https://ops-evidence.yukimurata0421.dev/ui/full-review-page?evidence_sha256=8d165418fca88f856d8525bbdae804b6b649455450796b2dc44d2134b21abd9a |
| API view | https://ops-evidence.yukimurata0421.dev/ui/api?evidence_sha256=ab18d62c4e628e190345fa218834ca74276f556191d2f068a969f7922945a471 | https://ops-evidence.yukimurata0421.dev/ui/api?evidence_sha256=8d165418fca88f856d8525bbdae804b6b649455450796b2dc44d2134b21abd9a |
| Review graph | https://ops-evidence.yukimurata0421.dev/ui/review-graph?evidence_sha256=ab18d62c4e628e190345fa218834ca74276f556191d2f068a969f7922945a471 | https://ops-evidence.yukimurata0421.dev/ui/review-graph?evidence_sha256=8d165418fca88f856d8525bbdae804b6b649455450796b2dc44d2134b21abd9a |

## Fixed Artifacts

| Artifact | Runtime | Monitoring |
| --- | --- | --- |
| Evidence SHA256 | `ab18d62c4e628e190345fa218834ca74276f556191d2f068a969f7922945a471` | `8d165418fca88f856d8525bbdae804b6b649455450796b2dc44d2134b21abd9a` |
| Pipeline run | `stream-v3-runtime-45k-real-api-20260712-v5` | `stream-v3-monitoring-50k-real-api-20260711-v3` |
| Canonical graph SHA256 | `50d4fa99a36b5a8f7a0af6c035964317894cc19a1ef308df81acee456b54ec0d` | `b5133772b23bdf85b7a33aafa0a425ea0395fe3ed4922e96c794a500cf8a1e86` |
| Input fingerprint SHA256 | `f3c559653bc441fa710205ff4cb6ddca81c33fe7275b4a62bebf8433ff102c69` | `ad9fd983765f62a14c5ed09260c6ca3d1549e76a724ecd966429e2e499277cd1` |
| Approved profile SHA256 | `77ceaa551a41d4a9e24fa3533de0bfe7df1f17a56702d6ed13e1e6b5342ce709` | `17fd209acd501ff5ebfd28dafcd83e6ebb23e7695ac19a13dd661a6ca1de428e` |
| Public payload | `data/precomputed_review_summaries/ab18d62c4e628e190345fa218834ca74276f556191d2f068a969f7922945a471.json` | `data/precomputed_review_summaries/8d165418fca88f856d8525bbdae804b6b649455450796b2dc44d2134b21abd9a.json` |
| Payload SHA256 | `5e6c4856c9d370265eda0a2d12538b2d989244bd1c805fe407b8cc0ac17b9dab` | `62709cf1f070393cd8b51bb85beebb2d6e59f5e573971597afa8a27c9bbe58e9` |

The API revision field is intentionally empty for these local-orchestrated,
GCS-published runs; the pipeline run ID, evidence SHA, model output hashes, and
canonical graph SHA are the run attestations.

## Windows and Coverage

| Metric | Runtime | Monitoring |
| --- | ---: | ---: |
| Staged input lines | 45,000 | 50,000 |
| Sanitized event rows accepted | 45,000 | 49,942 |
| Rejected lines | 0 | 0 |
| Window start | `2026-06-14T23:15:50Z` | `2026-06-18T09:54:00Z` |
| Window end | `2026-06-15T23:59:52Z` | `2026-06-19T10:48:55Z` |
| Window hours | 24.733889 | 24.915278 |
| Grouped Evidence Items | 1,035 | 25 |
| Single-prompt Evidence Items | 140 | 25 |
| Single-prompt occurrences | 44,105 | 49,942 |
| Single-prompt occurrence coverage | 98.0111% | 100.0% |
| Maximum chunks per provider | 21 | 4 |
| Evidence Items covered by provider chunks | 1,035 | 25 |
| Provider Evidence Item coverage | 100.0% | 100.0% |
| Unassigned Evidence Items | 0 | 0 |
| Raw rows sent directly to providers | 0 | 0 |

The corrected runtime sanitizer accounted for all 45,000 input lines: 45,000
events were accepted, 0 were outside the selected window, and 0 were rejected.
Each row is represented by a grouped Evidence Item occurrence; row bodies are
not sent directly to providers.

## Profile Gate

| Metric | Runtime | Monitoring |
| --- | --- | --- |
| Profile ID | `stream_v3_runtime_source_approved_20260711` | `stream_v3_monitoring_source_approved_20260711` |
| Status | `approved_context_human_gated_outcomes` | `approved_context_human_gated_outcomes` |
| Confidence action | `approved_human_reviewed` | `approved_human_reviewed` |
| Overall confidence | 0.733 | 0.82 |
| Confirmed outcome | Continuously available public YouTube live stream with fresh ADS-B visual content and audible program audio. | a continuously available public YouTube live stream with fresh ADS-B visual content and audible program audio |
| Source access after approval | disabled | disabled |

System-specific semantics live in the approved operational profile JSON, not in
the generic engine. The core flow remains: sanitized source discovery, human
answers, Gemini normalization, human re-review, SHA approval, then log analysis.

## Provider Results

All five providers finished with `status=ok` and `schema_valid=true`.

### Runtime

| Provider | Model | Chunks | Failed chunks | Latency ms | Input tokens | Output tokens |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| Gemini | `gemini-3.1-pro-preview` | 21 | 0 | 502,542 | 670,688 | 15,392 |
| Gemma | `gemma-4-26b-a4b-it-maas` | 21 | 0 | 456,999 | 649,772 | 27,241 |
| Mistral | `mistral-small-2503` | 6 | 0 | 58,314 | 253,438 | 5,641 |
| GPT OSS | `gpt-oss-20b-maas` | 21 | 0 | 243,729 | 514,074 | 65,795 |
| Qwen | `qwen/qwen3-coder-480b-a35b-instruct-maas` | 21 | 0 | 245,766 | 583,833 | 31,100 |

All runtime providers completed every final chunk with schema-valid output.
The provider ledger records retries separately; final failed chunk count is 0.

### Monitoring

| Provider | Model | Chunks | Failed chunks | Latency ms | Input tokens | Output tokens |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| Gemini | `gemini-3.1-pro-preview` | 4 | 0 | 109,375 | 60,927 | 3,567 |
| Gemma | `gemma-4-26b-a4b-it-maas` | 4 | 0 | 119,216 | 56,943 | 5,782 |
| Mistral | `mistral-small-2503` | 1 | 0 | 15,285 | 21,975 | 1,800 |
| GPT OSS | `gpt-oss-20b-maas` | 4 | 0 | 49,143 | 47,195 | 14,541 |
| Qwen | `qwen/qwen3-coder-480b-a35b-instruct-maas` | 4 | 0 | 48,193 | 48,441 | 5,992 |

## Review Outcome

| Corpus | Primary candidates | Validation targets | Monitor-only | Auto-archived |
| --- | ---: | ---: | ---: | ---: |
| Runtime | 0 | 10 | 4 | 7 |
| Monitoring | 0 | 2 | 3 | 2 |

The monitoring result distinguishes two real timeout observations from the
historical `last_close_reason` values embedded in healthy records. Normal or
absence-only projections are retained for audit as monitor-only/archived items,
not displayed as unresolved incident targets.

Scores are review priority, not truth probability. Provider convergence creates
technical review support; causal and user-impact promotion remains human-gated.
