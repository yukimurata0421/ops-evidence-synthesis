# Real API Source-Aware Run

This document records the public read-only artifact generated from the
source-aware real-provider amazon-notify run.

## Public URLs

- Full review page: https://ops-evidence.yukimurata0421.dev/ui/full-review-page?evidence_sha256=7ca07bd8ed4bcb6009b654f17c40576a7b3462c62b2c74011c1623043550ccfb
- Human-readable API view: https://ops-evidence.yukimurata0421.dev/ui/api?evidence_sha256=7ca07bd8ed4bcb6009b654f17c40576a7b3462c62b2c74011c1623043550ccfb
- Visual review graph: https://ops-evidence.yukimurata0421.dev/ui/review-graph?evidence_sha256=7ca07bd8ed4bcb6009b654f17c40576a7b3462c62b2c74011c1623043550ccfb
- JSON review graph: https://ops-evidence.yukimurata0421.dev/review/graph?evidence_sha256=7ca07bd8ed4bcb6009b654f17c40576a7b3462c62b2c74011c1623043550ccfb

## Fixed Artifacts

| Artifact | Value |
| --- | --- |
| Evidence SHA256 | `7ca07bd8ed4bcb6009b654f17c40576a7b3462c62b2c74011c1623043550ccfb` |
| Pipeline run | `storeless CLI real-provider run` |
| API revision | `real-api-7d-target-explanations-20260630-regrouped-v2` |
| Canonical graph SHA256 | `87c3d02ab1b0fad2e120e756477ec77dd6cd78eba1f83525b6d7af6128798aea` |
| Input fingerprint SHA256 | `875c8061b0e7a8bc13b6c7f2b806b4fe0eb6b10ffe786d66978a21c3dbb45fff` |
| Public payload | `data/precomputed_review_summaries/7ca07bd8ed4bcb6009b654f17c40576a7b3462c62b2c74011c1623043550ccfb.json` |
| Payload SHA256 | `bfc4384341253c101d9d75a201432cbfeccbf4389fde948d30e739c2f951290e` |

## Window Selection

Public real-provider reviews must cover at least 24 hours. For amazon-notify,
three candidate windows were evaluated against the same local sanitized corpus.
The longest valid 7-day window was selected before the five-provider run.

| Candidate | Window | Sanitized rows | Evidence items | Prompt items | Prompt occurrences | Coverage | Evidence SHA256 |
| --- | --- | ---: | ---: | ---: | ---: | ---: | --- |
| 2 days | 2026-06-14T18:35:25Z to 2026-06-16T18:35:25Z | 6,404 | 687 | 140 | 5,821 | 90.9% | `c134e1915ba20d03d243e6877874fbcbc81f82cde41bf06bef8841778d3bd8e4` |
| 5 days | 2026-06-11T18:35:25Z to 2026-06-16T18:35:25Z | 16,498 | 1,830 | 140 | 14,190 | 86.0% | `8df07cc4b76d69974975735282fc7875afd8b9b53c9d26c2d7db8f38c516461f` |
| 7 days | 2026-06-09T18:35:25Z to 2026-06-16T18:35:25Z | 23,400 | 2,759 | 140 | 19,649 | 84.0% | `7ca07bd8ed4bcb6009b654f17c40576a7b3462c62b2c74011c1623043550ccfb` |

## Data Boundary

The run used a local-first evidence boundary.

1. Raw amazon-notify logs stayed local.
2. 23,400 rows were sanitized and fixed into a 7-day Evidence Bundle.
3. The Evidence Bundle was fixed by SHA256 before provider execution.
4. Sanitized source context and source analysis were attached as interpretation
   context only.
5. Runtime claims still had to cite Evidence Item IDs from the sanitized corpus.
6. The public URL serves a precomputed read-only payload and does not run models
   during page load.

## Token Compression

The API did not send all 23,400 log rows as raw prompt text. The persisted
sanitized corpus was converted into grouped evidence items, then bounded for
model input.

| Layer | Count |
| --- | ---: |
| Sanitized log rows in DB/API corpus | 23,400 |
| Grouped Evidence Items retained in bundle | 2,759 |
| Evidence Items selected for model prompt | 140 |
| Occurrences represented by selected items | 19,649 |
| Occurrence coverage | 84.0% |

Selection prioritized high-severity, high-count, and operationally interesting
patterns such as run results, watchdog/service state, token refresh, Pub/Sub
idle behavior, status snapshots, and notification workflow events. The prompt
kept evidence IDs, counts, first/last seen timestamps, compact templates, and
profile context. Low-signal tail patterns remained in the persisted sanitized
corpus and were represented by corpus-level counts rather than row bodies.

Total provider token usage was 439,625 input tokens and 11,721 output tokens
across the five successful providers.

## Provider Results

| Provider | Model | Status | Schema | Latency ms | Input tokens | Output tokens |
| --- | --- | --- | --- | ---: | ---: | ---: |
| `gemini-enterprise-agent-platform` | `gemini-3.1-pro-preview` | ok | valid | 54,414 | 99,903 | 1,069 |
| `openai-gpt-oss-on-vertex` | `gpt-oss-120b-maas` | ok | valid | 23,429 | 75,988 | 2,998 |
| `mistral-agent-platform` | `mistral-medium-3` | ok | valid | 53,613 | 96,045 | 1,487 |
| `qwen-agent-platform` | `qwen/qwen3-coder-480b-a35b-instruct-maas` | ok | valid | 25,771 | 89,831 | 1,812 |
| `glm-agent-platform` | `zai-org/glm-5-maas` | ok | valid | 55,239 | 77,858 | 4,355 |

## Review Outcome

The canonical review graph produced:

- 0 primary candidates
- 4 validation targets
- 2 monitor-only context items
- 2 auto-archived targets
- 5/5 provider detection overlap

Technical convergence is treated as review support only. Incident and
user-impact promotion remained open because cause, impact, and next action were
not fully aligned and still require human review.

## Regeneration Entry Point

The public payload can be regenerated from the recorded real-provider multi-run
response and the matching 7-day Evidence Bundle:

```bash
PYTHONPATH=src python scripts/generate_precomputed_review_from_multi_run.py \
  --multi-run-json workspace/amazon_notify_7d_20260630_real_api_explanations/api_multi_run_7d_explanations.json \
  --evidence-bundle workspace/amazon_notify_7d_20260629/window_candidates/evidence_bundle_7d.json \
  --source-context workspace/real_api_qwen_glm_20260628_144326/source_context/source_context_bundle.json \
  --source-analysis workspace/real_api_qwen_glm_20260628_144326/source_analysis/source_analysis_bundle.json \
  --profile-draft workspace/source_profile_refresh_20260629/real_amazon_notify/profile_discovery/profile_draft.json \
  --approved-profile workspace/source_profile_refresh_20260629/real_amazon_notify/profile_discovery/approved_profile.json \
  --api-revision real-api-7d-target-explanations-20260630-regrouped-v2 \
  --profile-id amazon_notify_qwen_glm_full_corpus_approved \
  --updated-at 2026-06-30T10:09:03Z \
  --check
```

The workspace paths above are local execution artifacts and are not required for
the public read-only demo. The committed public artifact is the fixed JSON file
under `data/precomputed_review_summaries/`.
