# Real API Source-Aware Five-Provider Run

This document records the guarded public read-only amazon-notify artifact
generated from schema-valid Gemini 3.1 Pro, GPT OSS, Mistral, Qwen, and Gemma 4 real
API outputs. GLM was replaced by Gemma 4 for this public amazon-notify payload.
Llama and Claude are excluded because they were not available in this
environment. The public entry page shows the stream_v3 runtime run first as the
primary reviewer path even though it has 0 primary candidates; this
amazon-notify run is kept as the restraint example where 5/5 provider support
can create a primary candidate, but still does not auto-accept an incident cause
or operational action.

## Public URLs

- Full review page: https://ops-evidence.yukimurata0421.dev/ui/full-review-page?evidence_sha256=b99da97cab19f026b5475cdaa6100fdd6ebb6d96466a43e6b62a44b99ac414ec
- Human-readable API view: https://ops-evidence.yukimurata0421.dev/ui/api?evidence_sha256=b99da97cab19f026b5475cdaa6100fdd6ebb6d96466a43e6b62a44b99ac414ec
- Visual review graph: https://ops-evidence.yukimurata0421.dev/ui/review-graph?evidence_sha256=b99da97cab19f026b5475cdaa6100fdd6ebb6d96466a43e6b62a44b99ac414ec
- JSON review graph: https://ops-evidence.yukimurata0421.dev/review/graph?evidence_sha256=b99da97cab19f026b5475cdaa6100fdd6ebb6d96466a43e6b62a44b99ac414ec

## Fixed Artifacts

| Artifact | Value |
| --- | --- |
| Evidence SHA256 | `b99da97cab19f026b5475cdaa6100fdd6ebb6d96466a43e6b62a44b99ac414ec` |
| Pipeline run | `amazon-notify-gemini-pro-gemma4-combined-20260702T224500Z` |
| API revision | `real-api-5p-gemini-pro-gemma4-20260702T224500Z` |
| Canonical graph SHA256 | `8ad416a42a0a564ffe9221033cb50dde6a493ae3c8107bf6a69bf358b423d002` |
| Input fingerprint SHA256 | `3c9fd8af64ea816956c44d78d6f42516f67c93463902fa7de2459eef230b9840` |
| Public payload | `data/precomputed_review_summaries/b99da97cab19f026b5475cdaa6100fdd6ebb6d96466a43e6b62a44b99ac414ec.json` |
| Payload SHA256 | `26b43ac39b671cd09c2fa2f9c87d3faef10a2573482643002f9b3acf261caffa` |

## Window Selection

Public real-provider reviews must cover at least 24 hours. For amazon-notify,
the current guarded run uses the full available 14-day sanitized DB corpus.
Earlier 2-day, 5-day, and 7-day candidates were superseded because they were
shorter than the available evidence window.

| Selected window | Time range | Sanitized rows | Evidence items | Prompt items | Prompt occurrences | Single-prompt occurrence coverage |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| 14 days | 2026-06-02T00:00:43Z to 2026-06-16T18:35:26Z | 44,944 | 8,519 | 140 | 34,774 | 77.4% |

## Data Boundary

The run used a local-first evidence boundary.

1. Raw amazon-notify logs stayed local.
2. Sanitization produced a 44,944-row DB corpus and an 8,519-item Evidence
   Bundle.
3. Every sanitized DB row was assigned to a coverage ledger entry before model
   prompts.
4. Providers received chunked sanitized Evidence Items plus sanitized source and
   profile context, not raw logs or raw source.
5. Runtime claims still had to cite Evidence Item IDs from the sanitized corpus.
6. The public URL serves a precomputed read-only payload and does not run models
   during page load.

## Full-Corpus Chunking

The API did not send 44,944 raw log rows as prompt text. The persisted sanitized
corpus was converted into grouped Evidence Items and then split into
provider-specific chunks. Single-prompt projection metadata still records the
top-140 high-signal slice for quick inspection, but real provider execution
covered all grouped Evidence Items.

| Layer | Count |
| --- | ---: |
| Sanitized log rows in DB/API corpus | 44,944 |
| Coverage ledger row assignments | 44,944 |
| Grouped Evidence Items retained in bundle | 8,519 |
| Evidence Items in single-prompt projection | 140 |
| Evidence Items covered by chunked provider calls | 8,519 |
| Maximum provider chunks | 105 |
| Provider Evidence Item coverage | 100.0% |

Coverage means every sanitized row is accounted for in the review boundary. It
does not mean every row is copied into a prompt. Direct raw-row prompt count is
zero; models see bounded, sanitized Evidence Items with IDs and chunk
manifests.

## Provider Results

| Provider | Model | Status | Schema | Chunks | Input tokens | Output tokens |
| --- | --- | --- | --- | ---: | ---: | ---: |
| `gemini-enterprise-agent-platform` | `gemini-3.1-pro-preview` | ok | valid | 86 | 7,135,493 | 54,636 |
| `openai-gpt-oss-on-vertex` | `gpt-oss-120b-maas` | ok | valid | 105 | 5,679,336 | 310,980 |
| `mistral-agent-platform` | `mistral-medium-3` | ok | valid | 51 | 5,029,427 | 51,204 |
| `qwen-agent-platform` | `qwen/qwen3-coder-480b-a35b-instruct-maas` | ok | valid | 86 | 6,138,445 | 144,896 |
| `gemma-agent-platform` | `gemma-4-26b-a4b-it-maas` | ok | valid | 86 | 7,049,837 | 156,218 |

Recorded provider outputs are hashed; deterministic reproduction applies to the
canonical merge over sorted recorded chunk outputs, not to recreating a live
model response byte-for-byte.

## Measured Gemini 3.1 Pro Refresh Timing

Gemini 3.1 Pro replaced the earlier Flash Lite provider output for this public
artifact. Wall time is command elapsed time for the Gemini-only real API run;
provider latency is the sum of chunk latencies recorded in the provider output
and is not the same as elapsed wall time.

| Step | Value |
| --- | ---: |
| Gemini 3.1 Pro real API wall time | 3,127.13s |
| Gemini 3.1 Pro provider latency sum | 7,608,922 ms |
| Gemini 3.1 Pro successful chunks | 86 |

## Review Outcome

The canonical review graph produced:

- 1 primary candidate
- 10 validation targets
- 2 monitor-only context items
- 3 auto-archived targets
- 5/5 provider detection overlap

Provider convergence is technical support, not proof. Incident and user-impact
promotion remain human-gated until cause, impact, and next action are supported
by cited operational evidence.

## Regeneration Entry Point

The public payload can be regenerated from the recorded real-provider multi-run
response and the matching 14-day Evidence Bundle:

```bash
PYTHONPATH=src python scripts/generate_precomputed_review_from_multi_run.py \
  --multi-run-json workspace/e2e_real_api_source_sanitize_20260701T003045Z/multi_ai_real_5p_gemini_pro_gemma4_combined_20260702T224500Z/multi_ai_run.json \
  --evidence-bundle workspace/e2e_real_api_source_sanitize_20260701T003045Z/evidence_bundle.json \
  --source-context workspace/e2e_real_api_source_sanitize_20260701T003045Z/source_context/source_context_bundle.json \
  --source-analysis workspace/e2e_real_api_source_sanitize_20260701T003045Z/source_analysis/source_analysis_bundle.json \
  --profile-draft workspace/e2e_real_api_source_sanitize_20260701T003045Z/profile_draft.json \
  --approved-profile workspace/e2e_real_api_source_sanitize_20260701T003045Z/approved_profile.json \
  --profile-id amazon_notify_e2e_20260701t003045z_approved \
  --api-revision real-api-5p-gemini-pro-gemma4-20260702T224500Z \
  --provider-mode real_api_vertex_gemini_3_1_pro_gpt_oss_mistral_qwen_gemma4_chunked_full_corpus \
  --min-window-hours 24 \
  --output-dir data/precomputed_review_summaries
```

The workspace paths above are local execution artifacts and are not required for
the public read-only demo. The committed public artifact is the fixed JSON file
under `data/precomputed_review_summaries/`.
