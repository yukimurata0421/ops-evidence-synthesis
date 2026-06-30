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
| API revision | `real-api-7d-target-explanations-20260630-regrouped-v2` | `real-api-7d-target-explanations-20260630-regrouped-mistral-budgeted-v2` |
| Canonical graph SHA256 | `e281e51e08bea27f95071e7fe39a78376abf6b56d666f8de09136edb5b5ea5bb` | `f2278f5b175c3081921122bfe9cff5b20c1025a72cd86580bc8790b4689a0efa` |
| Input fingerprint SHA256 | `e83991c8974951d4f348cddaee9b40ed3d55866b4ded46c5a0e169f1b2fef5d5` | `6ca49f85f5a105ed95df084b32c4c23bf57ad961cfe99804ae5c8e74ec3a15d1` |
| Source context SHA256 | `669dc2f9d33ff9ab9d73f04d8c17f8718386815d0c6e536dde14b627232637d2` | `669dc2f9d33ff9ab9d73f04d8c17f8718386815d0c6e536dde14b627232637d2` |
| Source analysis SHA256 | `451320fbd76572c4bf00be20b0ab43825d99eaa518a1b1b99c54ebf7a31e33a5` | `451320fbd76572c4bf00be20b0ab43825d99eaa518a1b1b99c54ebf7a31e33a5` |
| Public payload | `data/precomputed_review_summaries/aba039fb4c472b45d5f016a8c7accd853d61cc3a00480767fe33fbca6f36c778.json` | `data/precomputed_review_summaries/a09ee4615689dfce1557c2803cdbdf43ce0c285c196c1317cd3d30ee1835d267.json` |
| Payload SHA256 | `5aa30b021f8b87ff3eea64e87c2bdaf253837b4a8d69e6d7e58b79cd4a2b52f6` | `60fd4bb4718bfb041b24a166db5860393dacf1f3736bb1e60ded3f9b02608d92` |

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
coverage while keeping the provider prompt bounded.

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
   analysis to the real-provider execution path.
9. Persist provider runs and canonical review graph.
10. Generate read-only public payloads under `data/precomputed_review_summaries/`.

Source context is not incident evidence. Runtime and monitoring claims still
have to cite Evidence Item IDs from the sanitized corpus.

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
was persisted, grouped into Evidence Items, and projected into a bounded model
input.

| Layer | Dell runtime | arena-server monitoring |
| --- | ---: | ---: |
| Sanitized rows in Evidence Bundle | 11,399 | 4,747 |
| Grouped Evidence Items retained | 654 | 1,520 |
| Evidence Items selected for model prompt | 140 | 140 |
| Occurrences represented by selected items | 10,771 | 496 |
| Occurrence coverage | 94.5% | 10.4% |

Dell runtime had dense repeated runtime patterns, so 140 selected items covered
most occurrences. arena-server monitoring had a wider set of one-off state,
metric, and journal items; the selected prompt stayed bounded while the full
sanitized corpus remained in the Evidence Bundle and public payload metadata.
Mistral Medium 3 used a provider-budgeted top-80 projection for the arena run
because the current Google Cloud Mistral Agent Platform context limit is 128k;
Gemini, gpt-oss, Qwen, and GLM used the standard top-140 projection.

## Provider Results

### Dell runtime

| Provider | Model | Status | Schema | Latency ms | Input tokens | Output tokens |
| --- | --- | --- | --- | ---: | ---: | ---: |
| `gemini-enterprise-agent-platform` | `gemini-3.1-pro-preview` | ok | valid | 40,135 | 105,276 | 1,402 |
| `openai-gpt-oss-on-vertex` | `gpt-oss-120b-maas` | ok | valid | 30,078 | 80,120 | 5,613 |
| `mistral-agent-platform` | `mistral-medium-3` | ok | valid | 54,265 | 94,401 | 1,609 |
| `qwen-agent-platform` | `qwen/qwen3-coder-480b-a35b-instruct-maas` | ok | valid | 31,272 | 87,275 | 3,384 |
| `glm-agent-platform` | `zai-org/glm-5-maas` | ok | valid | 80,691 | 80,199 | 5,248 |

### arena-server monitoring

| Provider | Model | Status | Schema | Latency ms | Input tokens | Output tokens |
| --- | --- | --- | --- | ---: | ---: | ---: |
| `gemini-enterprise-agent-platform` | `gemini-3.1-pro-preview` | ok | valid | 44,274 | 156,852 | 1,484 |
| `openai-gpt-oss-on-vertex` | `gpt-oss-120b-maas` | ok | valid | 18,535 | 120,072 | 1,631 |
| `mistral-agent-platform` | `mistral-medium-3` | ok | valid | 52,248 | 122,039 | 1,135 |
| `qwen-agent-platform` | `qwen/qwen3-coder-480b-a35b-instruct-maas` | ok | valid | 28,905 | 130,569 | 1,501 |
| `glm-agent-platform` | `zai-org/glm-5-maas` | ok | valid | 72,581 | 120,284 | 4,307 |

## Review Outcome

| Corpus | Primary candidates | Validation targets | Monitor-only | Auto-archived | Incident promotion gate |
| --- | ---: | ---: | ---: | ---: | --- |
| Dell runtime | 0 | 3 | 2 | 4 | 5/5 provider run succeeded; action remains human-gated |
| arena-server monitoring | 1 | 3 | 2 | 0 | Open; primary candidate remains human-gated |

Both runs intentionally stop at human review. Provider convergence can create
review targets and technical support, but it does not automatically authorize a
final causal judgement or operational action.

The public payload builder also applies the same evidence-window boundary used
for the 7-day bundle. Evidence Items outside the accepted analysis window are
not counted as public target support. Broad `general` findings are split by
review family before roll-up, so transport-path failures, runtime exceptions,
and resource-pressure signals do not collapse into one review target.
Public target counts are recomputed after that window boundary is applied, and
health or recovery-only signals are shown as counter/weak evidence when the
review unit is an incident-like failure candidate.
