# stream_v3 Real API Source-Aware Runs

This document records the source-aware real-provider analyses for two
stream_v3 evidence sets:

- Dell runtime evidence
- arena-server monitoring-plane evidence

Both runs used the same sanitized stream_v3 source context. Raw logs and raw
source stayed local. The public URLs serve fixed precomputed payloads and do not
run providers on page load.

## Public URLs

### Dell runtime

- Full review page: https://ops-evidence.yukimurata0421.dev/ui/full-review-page?evidence_sha256=64fa79977171fe9bad0664d115ff0ffcf4e248cd12a6a938e62d25cba7b12681
- Human-readable API view: https://ops-evidence.yukimurata0421.dev/ui/api?evidence_sha256=64fa79977171fe9bad0664d115ff0ffcf4e248cd12a6a938e62d25cba7b12681
- Visual review graph: https://ops-evidence.yukimurata0421.dev/ui/review-graph?evidence_sha256=64fa79977171fe9bad0664d115ff0ffcf4e248cd12a6a938e62d25cba7b12681
- JSON review graph: https://ops-evidence.yukimurata0421.dev/review/graph?evidence_sha256=64fa79977171fe9bad0664d115ff0ffcf4e248cd12a6a938e62d25cba7b12681

### arena-server monitoring

- Full review page: https://ops-evidence.yukimurata0421.dev/ui/full-review-page?evidence_sha256=f22b327f601738de5c7011c9424fe7c615ed35ea693f791849a54af8d7271769
- Human-readable API view: https://ops-evidence.yukimurata0421.dev/ui/api?evidence_sha256=f22b327f601738de5c7011c9424fe7c615ed35ea693f791849a54af8d7271769
- Visual review graph: https://ops-evidence.yukimurata0421.dev/ui/review-graph?evidence_sha256=f22b327f601738de5c7011c9424fe7c615ed35ea693f791849a54af8d7271769
- JSON review graph: https://ops-evidence.yukimurata0421.dev/review/graph?evidence_sha256=f22b327f601738de5c7011c9424fe7c615ed35ea693f791849a54af8d7271769

## Fixed Artifacts

| Artifact | Dell runtime | arena-server monitoring |
| --- | --- | --- |
| Evidence SHA256 | `64fa79977171fe9bad0664d115ff0ffcf4e248cd12a6a938e62d25cba7b12681` | `f22b327f601738de5c7011c9424fe7c615ed35ea693f791849a54af8d7271769` |
| Pipeline run | `pipe-55809b4dbea54d618fa4` | `pipe-1c22491f0c7b448aa3d1` |
| API revision | `ops-evidence-api-e2e-00006-6t9` | `ops-evidence-api-e2e-00006-6t9` |
| Canonical graph SHA256 | `9cd95c400ee4831993167b1b5b0d33db81e0a3c9c28197b9991d6e89aa33eb3c` | `3732e88c6e51596eba2331fc63f54dbe4d676da68cf0ad6e620759f39fcdfbf3` |
| Input fingerprint SHA256 | `0c7d08d86500ca23274113fca84ca5376e8e1562b20f456f630a2aa4142252d0` | `2b771e56c49ecf63b0eb5a97e9251ab837174f4476edc1eee61436684d2b1461` |
| Source context SHA256 | `669dc2f9d33ff9ab9d73f04d8c17f8718386815d0c6e536dde14b627232637d2` | `669dc2f9d33ff9ab9d73f04d8c17f8718386815d0c6e536dde14b627232637d2` |
| Source analysis SHA256 | `451320fbd76572c4bf00be20b0ab43825d99eaa518a1b1b99c54ebf7a31e33a5` | `451320fbd76572c4bf00be20b0ab43825d99eaa518a1b1b99c54ebf7a31e33a5` |
| Public payload | `data/precomputed_review_summaries/64fa79977171fe9bad0664d115ff0ffcf4e248cd12a6a938e62d25cba7b12681.json` | `data/precomputed_review_summaries/f22b327f601738de5c7011c9424fe7c615ed35ea693f791849a54af8d7271769.json` |
| Payload SHA256 | `c760b309ebb46b88358b6b85c13bc1c88c48c607df901f764d03b27ef564ac68` | `e2c35e0b4d4dc9625b4635d53bd8344d7d30d333b0ab8996628a733bf19ff4e6` |

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
   analysis to the e2e API.
9. Persist provider runs and canonical review graph.
10. Generate read-only public payloads under `data/precomputed_review_summaries/`.

Source context is not incident evidence. Runtime and monitoring claims still
have to cite Evidence Item IDs from the sanitized corpus.

## Input Evidence

| Corpus | Included evidence | Sanitized rows used by bundle |
| --- | --- | ---: |
| Dell runtime | Kubernetes pod/deploy output, systemd journal snippets, ffmpeg TCP send samples, WAN/netlink observers, persistent TCP anchor observations, runtime state snapshots | 8,011 |
| arena-server monitoring | Compact monitoring JSONL, watchdog and fast-recovery tails, exporter metrics, resource memory assessment, systemd journals, k8s event output, runtime state snapshots, recent runtime log tails | 5,055 |

Credential-like material stayed out of provider input. One Dell OAuth
token-state fragment had already-redacted values but a secret-like key name; it
was excluded before the final sanitized verification.

## Token Compression

The API did not send row-level raw logs as prompt text. Each sanitized corpus
was persisted, grouped into Evidence Items, and projected into a bounded model
input.

| Layer | Dell runtime | arena-server monitoring |
| --- | ---: | ---: |
| Sanitized rows in Evidence Bundle | 8,011 | 5,055 |
| Grouped Evidence Items retained | 654 | 1,680 |
| Evidence Items selected for model prompt | 140 | 140 |
| Occurrences represented by selected items | 7,383 | 496 |
| Occurrence coverage | 92.2% | 9.8% |

Dell runtime had dense repeated runtime patterns, so 140 selected items covered
most occurrences. arena-server monitoring had a wider set of one-off state,
metric, and journal items; the selected prompt stayed bounded while the full
sanitized corpus remained in the Evidence Bundle and public payload metadata.

## Provider Results

### Dell runtime

| Provider | Model | Status | Schema | Latency ms | Input tokens | Output tokens |
| --- | --- | --- | --- | ---: | ---: | ---: |
| `gemini-enterprise-agent-platform` | `gemini-3.1-pro-preview` | failed | invalid | 71,464 | 0 | 0 |
| `openai-gpt-oss-on-vertex` | `gpt-oss-120b-maas` | ok | valid | 39,602 | 42,633 | 2,770 |
| `mistral-agent-platform` | `mistral-medium-3` | ok | valid | 41,379 | 52,432 | 1,592 |
| `qwen-agent-platform` | `qwen/qwen3-coder-480b-a35b-instruct-maas` | ok | valid | 19,856 | 49,412 | 1,570 |
| `glm-agent-platform` | `zai-org/glm-5-maas` | ok | valid | 208,443 | 43,141 | 4,479 |

### arena-server monitoring

| Provider | Model | Status | Schema | Latency ms | Input tokens | Output tokens |
| --- | --- | --- | --- | ---: | ---: | ---: |
| `gemini-enterprise-agent-platform` | `gemini-3.1-pro-preview` | ok | valid | 54,010 | 74,472 | 1,303 |
| `openai-gpt-oss-on-vertex` | `gpt-oss-120b-maas` | ok | valid | 15,785 | 57,242 | 2,850 |
| `mistral-agent-platform` | `mistral-medium-3` | ok | valid | 34,130 | 72,156 | 1,001 |
| `qwen-agent-platform` | `qwen/qwen3-coder-480b-a35b-instruct-maas` | ok | valid | 9,452 | 67,921 | 1,271 |
| `glm-agent-platform` | `zai-org/glm-5-maas` | ok | valid | 142,070 | 58,193 | 3,753 |

## Review Outcome

| Corpus | Primary candidates | Validation targets | Monitor-only | Auto-archived | Incident baseline |
| --- | ---: | ---: | ---: | ---: | --- |
| Dell runtime | 0 | 7 | 2 | 1 | Open |
| arena-server monitoring | 0 | 6 | 2 | 1 | Open |

Both runs intentionally stop at human review. Provider convergence can create
review targets and technical support, but it does not automatically establish
the incident baseline.
