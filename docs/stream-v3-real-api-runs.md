# stream_v3 Real API Source-Aware Runs

This document records the source-aware real-provider analyses for two
stream_v3 evidence sets:

- Dell runtime evidence
- arena-server monitoring-plane evidence

Both runs used the same sanitized stream_v3 source context. Raw logs and raw
source stayed local. The public URLs serve fixed precomputed payloads and do not
run providers on page load. Both payloads were regenerated from local sanitized
corpora after 2-day, 5-day, and 7-day candidate windows were checked; the
longest valid 7-day window was selected. Both accepted runs have all five
providers schema-valid.

## Public URLs

### Dell runtime

- Full review page: https://ops-evidence.yukimurata0421.dev/ui/full-review-page?evidence_sha256=aba039fb4c472b45d5f016a8c7accd853d61cc3a00480767fe33fbca6f36c778
- Human-readable API view: https://ops-evidence.yukimurata0421.dev/ui/api?evidence_sha256=aba039fb4c472b45d5f016a8c7accd853d61cc3a00480767fe33fbca6f36c778
- Visual review graph: https://ops-evidence.yukimurata0421.dev/ui/review-graph?evidence_sha256=aba039fb4c472b45d5f016a8c7accd853d61cc3a00480767fe33fbca6f36c778
- JSON review graph: https://ops-evidence.yukimurata0421.dev/review/graph?evidence_sha256=aba039fb4c472b45d5f016a8c7accd853d61cc3a00480767fe33fbca6f36c778

### arena-server monitoring

- Full review page: https://ops-evidence.yukimurata0421.dev/ui/full-review-page?evidence_sha256=a09ee4615689dfce1557c2803cdbdf43ce0c285c196c1317cd3d30ee1835d267
- Human-readable API view: https://ops-evidence.yukimurata0421.dev/ui/api?evidence_sha256=a09ee4615689dfce1557c2803cdbdf43ce0c285c196c1317cd3d30ee1835d267
- Visual review graph: https://ops-evidence.yukimurata0421.dev/ui/review-graph?evidence_sha256=a09ee4615689dfce1557c2803cdbdf43ce0c285c196c1317cd3d30ee1835d267
- JSON review graph: https://ops-evidence.yukimurata0421.dev/review/graph?evidence_sha256=a09ee4615689dfce1557c2803cdbdf43ce0c285c196c1317cd3d30ee1835d267

## Fixed Artifacts

| Artifact | Dell runtime | arena-server monitoring |
| --- | --- | --- |
| Evidence SHA256 | `aba039fb4c472b45d5f016a8c7accd853d61cc3a00480767fe33fbca6f36c778` | `a09ee4615689dfce1557c2803cdbdf43ce0c285c196c1317cd3d30ee1835d267` |
| Pipeline run | storeless CLI real-provider run | storeless CLI real-provider run |
| API revision | `real-api-7d-approved-profile-prompt-20260630` | `real-api-7d-approved-profile-prompt-20260630` |
| Canonical graph SHA256 | `55ca3d339c44f8384d9c0c38b9aca1062e670b3caedb55f393e533fed26fcc7e` | `9c17b2fe314a39b2a93fae8ba399bcb360e6f02af7ca0a12bdc92b3122008ca2` |
| Input fingerprint SHA256 | `b249b6de9383cff6b0242097ccbc3edb25218405d290e3bc7115b3595993cb9a` | `2e807098f2436636faf75cceb655d572af2f49b96d9bc79133bf4f179a9efe12` |
| Source context SHA256 | `669dc2f9d33ff9ab9d73f04d8c17f8718386815d0c6e536dde14b627232637d2` | `669dc2f9d33ff9ab9d73f04d8c17f8718386815d0c6e536dde14b627232637d2` |
| Source analysis SHA256 | `451320fbd76572c4bf00be20b0ab43825d99eaa518a1b1b99c54ebf7a31e33a5` | `451320fbd76572c4bf00be20b0ab43825d99eaa518a1b1b99c54ebf7a31e33a5` |
| Public payload | `data/precomputed_review_summaries/aba039fb4c472b45d5f016a8c7accd853d61cc3a00480767fe33fbca6f36c778.json` | `data/precomputed_review_summaries/a09ee4615689dfce1557c2803cdbdf43ce0c285c196c1317cd3d30ee1835d267.json` |
| Payload SHA256 | `151aa69bd5e29f896c439d94e9cf9ac6a027c4b88d2c4d663e9c20b2e787e06e` | `2a58fd6a28f50203df786a24891f895eb3109156230081c348dcee75b423c04f` |

## Dell Runtime Window Selection

Public real-provider reviews must cover at least 24 hours. For the Dell runtime
corpus, three candidate windows were evaluated against the same local sanitized
event set. The longest valid window was selected before the five-provider run.

| Candidate | Window | Sanitized rows | Evidence items | Prompt items | Prompt occurrences | Coverage | Evidence SHA256 |
| --- | --- | ---: | ---: | ---: | ---: | ---: | --- |
| 2 days | 2026-06-24T19:44:54Z to 2026-06-26T19:44:54Z | 9,538 | 654 | 140 | 8,910 | 93.4% | `6897d963fc534e856687767429481c8a81b771a21821faacad45f4e03d548d61` |
| 5 days | 2026-06-21T19:44:54Z to 2026-06-26T19:44:54Z | 11,013 | 654 | 140 | 10,385 | 94.3% | `7b427becdff4176ae718d17f00f03a11ef30fc80e517714cca2094b88a647ad1` |
| 7 days | 2026-06-19T19:44:54Z to 2026-06-26T19:44:54Z | 11,399 | 654 | 140 | 10,771 | 94.5% | `aba039fb4c472b45d5f016a8c7accd853d61cc3a00480767fe33fbca6f36c778` |

## Arena Monitoring Window Selection

The arena-server monitoring corpus was evaluated with the same minimum
24-hour policy. The accepted 7-day bundle includes more state and journal
coverage. Single-prompt projection remains bounded for inspection, while
provider execution covers the full Evidence Item corpus through chunks.

| Candidate | Window | Sanitized rows | Evidence items | Prompt items | Prompt occurrences | Coverage | Evidence SHA256 |
| --- | --- | ---: | ---: | ---: | ---: | ---: | --- |
| 2 days | 2026-06-15T00:12:45Z to 2026-06-17T00:12:45Z | 4,428 | 1,440 | 140 | 479 | 10.8% | `ad5075657268ebd8879f676d1877d3f8497012e190fbb6f3b3c7bd1fe02bdc9d` |
| 5 days | 2026-06-12T00:12:45Z to 2026-06-17T00:12:45Z | 4,448 | 1,460 | 140 | 479 | 10.8% | `4e4460833f739d09b109e69ea752dd1740b076eb649fbc1f25eb825e51c54529` |
| 7 days | 2026-06-10T00:12:45Z to 2026-06-17T00:12:45Z | 4,747 | 1,520 | 140 | 496 | 10.4% | `a09ee4615689dfce1557c2803cdbdf43ce0c285c196c1317cd3d30ee1835d267` |

## Data Boundary

The source-aware path was:

1. Stage a filtered stream_v3 source tree locally.
2. Create `source_context_bundle.json` with raw source excluded.
3. Create `source_analysis_bundle.json` as interpretation context.
4. Sanitize runtime or monitoring logs locally.
5. Verify sanitized output before API use.
6. Build a SHA-fixed Evidence Bundle.
7. Discover and approve a System Profile from sanitized source and evidence.
8. Send only sanitized bundles, approved profile, source context, and source
   analysis to the real-provider execution path, with the approved profile
   explicitly present in each provider prompt as human-gated context.
9. Persist provider runs and canonical review graph.
10. Generate read-only public payloads under `data/precomputed_review_summaries/`.

Source context is not incident evidence. Runtime and monitoring claims still
have to cite Evidence Item IDs from the sanitized corpus.

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
blocked by `user_impact_unverified`, so profile uncertainty remains visible
instead of being hidden behind a generic subsystem label.

## Input Evidence

| Corpus | Included evidence | Sanitized rows used by bundle |
| --- | --- | ---: |
| Dell runtime | Kubernetes pod/deploy output, systemd journal snippets, ffmpeg TCP send samples, WAN/netlink observers, persistent TCP anchor observations, runtime state snapshots | 11,399 |
| arena-server monitoring | Compact monitoring JSONL, watchdog and fast-recovery tails, exporter metrics, resource memory assessment, systemd journals, k8s event output, runtime state snapshots, recent runtime log tails | 4,747 |

Credential-like material stayed out of provider input. One Dell OAuth
token-state fragment had already-redacted values but a secret-like key name; it
was excluded before the final sanitized verification.

## Token Compression

The API did not send row-level raw logs as prompt text. Each sanitized corpus
was persisted and grouped into Evidence Items. Single-prompt projection metadata
records a bounded top-140 slice for quick inspection; multi-provider synthesis
covers all grouped Evidence Items through chunked provider calls.

| Layer | Dell runtime | arena-server monitoring |
| --- | ---: | ---: |
| Sanitized rows in Evidence Bundle | 11,399 | 4,747 |
| Grouped Evidence Items retained | 654 | 1,520 |
| Evidence Items in single-prompt projection | 140 | 140 |
| Evidence Items covered by chunked provider calls | 654 | 1,520 |
| Occurrences represented by selected items | 10,771 | 496 |
| Single-prompt occurrence coverage | 94.5% | 10.4% |
| Provider Evidence Item coverage | 100.0% | 100.0% |

Dell runtime had dense repeated runtime patterns, so 140 selected items covered
most occurrences. arena-server monitoring had a wider set of one-off state,
metric, and journal items, so single-prompt projection coverage is low. That is
now a display/inspection metric rather than an analysis cutoff: every grouped
Evidence Item is still sent through later provider chunks. Projection coverage
is occurrence-weighted, not raw-row coverage, so a low percentage indicates a
long-tail corpus rather than a missing raw-log window.

## Provider Results

### Dell runtime

| Provider | Model | Status | Schema | Latency ms | Input tokens | Output tokens |
| --- | --- | --- | --- | ---: | ---: | ---: |
| `gemini-enterprise-agent-platform` | `gemini-3.1-pro-preview` | ok | valid | 63,989 | 122,058 | 1,522 |
| `glm-agent-platform` | `zai-org/glm-5-maas` | ok | valid | 92,623 | 93,253 | 4,795 |
| `mistral-agent-platform` | `mistral-medium-3` | ok | valid | 49,766 | 109,072 | 1,063 |
| `openai-gpt-oss-on-vertex` | `gpt-oss-120b-maas` | ok | valid | 28,734 | 93,245 | 2,958 |
| `qwen-agent-platform` | `qwen/qwen3-coder-480b-a35b-instruct-maas` | ok | valid | 42,243 | 100,498 | 3,017 |

### arena-server monitoring

| Provider | Model | Status | Schema | Latency ms | Input tokens | Output tokens |
| --- | --- | --- | --- | ---: | ---: | ---: |
| `gemini-enterprise-agent-platform` | `gemini-3.1-pro-preview` | ok | valid | 31,506 | 173,836 | 1,344 |
| `glm-agent-platform` | `zai-org/glm-5-maas` | ok | valid | 98,079 | 133,482 | 4,950 |
| `mistral-agent-platform` | `mistral-medium-3` | ok | valid | 38,738 | 121,624 | 1,349 |
| `openai-gpt-oss-on-vertex` | `gpt-oss-120b-maas` | ok | valid | 35,639 | 112,516 | 2,574 |
| `qwen-agent-platform` | `qwen/qwen3-coder-480b-a35b-instruct-maas` | ok | valid | 17,562 | 143,936 | 1,476 |

## Review Outcome

| Corpus | Primary candidates | Validation targets | Monitor-only | Auto-archived | Incident promotion gate |
| --- | ---: | ---: | ---: | ---: | --- |
| Dell runtime | 1 | 3 | 2 | 0 | 5/5 provider run succeeded; action remains human-gated |
| arena-server monitoring | 0 | 3 | 2 | 2 | Open; all targets remain validation-gated |

Both runs intentionally stop at human review. Provider convergence can create
review targets and technical support, but it does not automatically authorize a
final causal judgement or operational action. The public UI separates a
graph-level incident gate signal from each target's promotion state so
"signal present" is not read as an accepted incident cause.

The public payload builder also applies the same evidence-window boundary used
for the 7-day bundle. Evidence Items outside the accepted analysis window are
not counted as public target support. Broad `general` findings are split by
review family before roll-up, so transport-path failures, runtime exceptions,
and resource-pressure signals do not collapse into one review target.
Public target counts are recomputed after that window boundary is applied, and
health or recovery-only signals are shown as counter/weak evidence when the
review unit is an incident-like failure candidate.
