# Real API Source-Aware Five-Provider Run

This document records the guarded public read-only amazon-notify artifact
generated from schema-valid Gemini, GPT OSS, Mistral, Qwen, and GLM real API
outputs. Llama and Claude are excluded because they were not available in this
environment. The public entry page shows the stream_v3 runtime run first because
that run has active human-gated primary candidates; this amazon-notify run is
kept as the restraint example where 5/5 provider support still stops at
validation.

## Public URLs

- Full review page: https://ops-evidence.yukimurata0421.dev/ui/full-review-page?evidence_sha256=b99da97cab19f026b5475cdaa6100fdd6ebb6d96466a43e6b62a44b99ac414ec
- Human-readable API view: https://ops-evidence.yukimurata0421.dev/ui/api?evidence_sha256=b99da97cab19f026b5475cdaa6100fdd6ebb6d96466a43e6b62a44b99ac414ec
- Visual review graph: https://ops-evidence.yukimurata0421.dev/ui/review-graph?evidence_sha256=b99da97cab19f026b5475cdaa6100fdd6ebb6d96466a43e6b62a44b99ac414ec
- JSON review graph: https://ops-evidence.yukimurata0421.dev/review/graph?evidence_sha256=b99da97cab19f026b5475cdaa6100fdd6ebb6d96466a43e6b62a44b99ac414ec

## Fixed Artifacts

| Artifact | Value |
| --- | --- |
| Evidence SHA256 | `b99da97cab19f026b5475cdaa6100fdd6ebb6d96466a43e6b62a44b99ac414ec` |
| Pipeline run | `real-api-5p-no-llama-claude-fresh-mistral-20260701` |
| API revision | `real-api-5p-no-llama-claude-fresh-mistral-20260701T114127Z` |
| Canonical graph SHA256 | `657eb44204cdcd616c8d7c4cdf4065b2080150f1ba65b0b6d775c0276994f643` |
| Input fingerprint SHA256 | `73f4c0957d7864624e331f75ffa651e98a27a5e7c6b65c6103fe0f9f72dc0858` |
| Public payload | `data/precomputed_review_summaries/b99da97cab19f026b5475cdaa6100fdd6ebb6d96466a43e6b62a44b99ac414ec.json` |
| Payload SHA256 | `a34c54049c45a08c266c172f9e9025ee4edb9b03c8d5678769da74bfdce3c2f3` |

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
| `gemini-enterprise-agent-platform` | `gemini-3.1-flash-lite` | ok | valid | 86 | 7,135,493 | 97,655 |
| `openai-gpt-oss-on-vertex` | `gpt-oss-120b-maas` | ok | valid | 105 | 5,679,336 | 310,980 |
| `mistral-agent-platform` | `mistral-medium-3` | ok | valid | 51 | 5,029,427 | 51,204 |
| `qwen-agent-platform` | `qwen/qwen3-coder-480b-a35b-instruct-maas` | ok | valid | 86 | 6,138,445 | 144,896 |
| `glm-agent-platform` | `zai-org/glm-5-maas` | ok | valid | 86 | 5,525,534 | 397,825 |

Recorded provider outputs are hashed; deterministic reproduction applies to the
canonical merge over sorted recorded chunk outputs, not to recreating a live
model response byte-for-byte.

## Review Outcome

The canonical review graph produced:

- 0 primary candidates
- 12 validation targets
- 2 monitor-only context items
- 2 auto-archived targets
- 5/5 provider detection overlap

Provider convergence is technical support, not proof. Incident and user-impact
promotion remain human-gated until cause, impact, and next action are supported
by cited operational evidence.

## Regeneration Entry Point

The public payload can be regenerated from the recorded real-provider multi-run
response and the matching 14-day Evidence Bundle:

```bash
PYTHONPATH=src python scripts/generate_precomputed_review_from_multi_run.py \
  --multi-run-json workspace/e2e_real_api_source_sanitize_20260701T003045Z/multi_ai_real_5p_no_llama_claude_combined_fresh_mistral_20260701T114127Z/multi_ai_run.json \
  --evidence-bundle workspace/e2e_real_api_source_sanitize_20260701T003045Z/evidence_bundle.json \
  --source-context workspace/e2e_real_api_source_sanitize_20260701T003045Z/source_context/source_context_bundle.json \
  --source-analysis workspace/e2e_real_api_source_sanitize_20260701T003045Z/source_analysis/source_analysis_bundle.json \
  --profile-draft workspace/e2e_real_api_source_sanitize_20260701T003045Z/profile_draft.json \
  --approved-profile workspace/e2e_real_api_source_sanitize_20260701T003045Z/approved_profile.json \
  --profile-id amazon_notify_e2e_20260701t003045z_approved \
  --api-revision real-api-5p-no-llama-claude-fresh-mistral-20260701T114127Z \
  --provider-mode real_api_vertex_gemini_gpt_oss_mistral_qwen_glm_chunked_full_corpus \
  --min-window-hours 24 \
  --output-dir data/precomputed_review_summaries
```

The workspace paths above are local execution artifacts and are not required for
the public read-only demo. The committed public artifact is the fixed JSON file
under `data/precomputed_review_summaries/`.
