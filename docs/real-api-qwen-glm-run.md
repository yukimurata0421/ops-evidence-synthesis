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
| API revision | `real-api-7d-approved-profile-prompt-20260630` |
| Canonical graph SHA256 | `605b6f522cd82649bc911b3e7614d19b2347455a89c8873f95ff5646b56d6577` |
| Input fingerprint SHA256 | `0b1bed34f2d56df225391311177107380dc4d7d0f16dd8e4160ad6245bf26408` |
| Public payload | `data/precomputed_review_summaries/7ca07bd8ed4bcb6009b654f17c40576a7b3462c62b2c74011c1623043550ccfb.json` |
| Payload SHA256 | `ec7799b59261a7bea0d5c6f4b56fe623644e1038b4c83295d392e8c1c0c9b4b9` |

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
4. Sanitized source context, source analysis, and the approved profile context
   were attached as interpretation context only.
5. Runtime claims still had to cite Evidence Item IDs from the sanitized corpus.
6. The public URL serves a precomputed read-only payload and does not run models
   during page load.

## Profile Gate

The profile is generated from sanitized source/profile discovery context and is
not incident evidence. The public payload records profile context as
`profile_context_summary.v2`:

- profile status: `approved_context_human_gated_outcomes`
- confidence action: `use_for_subsystem_routing_human_gated`
- overall confidence: `0.79`
- confirmed user outcomes: none
- provisional user outcomes: notification processing and watchdog recovery

Those provisional outcomes only shape review routing and missing-evidence
questions. They do not promote a target unless runtime Evidence Items and
human-approved user-impact evidence are attached. The profile question
`Which metrics are zero-is-good or zero-is-bad?` is linked to the
`service_health`, `background_processing`, and `general` review units because
zero processed/matched/notified counts can represent either healthy idle state
or broken processing.

## Token Compression

The API did not send all 23,400 log rows as raw prompt text. The persisted
sanitized corpus was converted into grouped evidence items, then bounded for
model input. The approved profile context was explicitly included in each
provider prompt as human-gated interpretation context, not incident evidence.

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

Total provider token usage was 456,466 input tokens and 15,680 output tokens
across the five successful providers.

## Provider Results

| Provider | Model | Status | Schema | Latency ms | Input tokens | Output tokens |
| --- | --- | --- | --- | ---: | ---: | ---: |
| `gemini-enterprise-agent-platform` | `gemini-3.1-pro-preview` | ok | valid | 28,808 | 103,870 | 1,501 |
| `glm-agent-platform` | `zai-org/glm-5-maas` | ok | valid | 84,036 | 80,982 | 5,370 |
| `mistral-agent-platform` | `mistral-medium-3` | ok | valid | 70,366 | 99,498 | 2,233 |
| `openai-gpt-oss-on-vertex` | `gpt-oss-120b-maas` | ok | valid | 49,484 | 79,136 | 3,764 |
| `qwen-agent-platform` | `qwen/qwen3-coder-480b-a35b-instruct-maas` | ok | valid | 32,465 | 92,980 | 2,812 |

## Review Outcome

The canonical review graph produced:

- 0 primary candidates
- 5 validation targets
- 2 monitor-only context items
- 1 auto-archived target
- 5/5 provider detection overlap

Technical convergence is treated as review support only. Incident and
user-impact promotion remained open because cause, impact, and next action were
not fully aligned and still require human review.

The public UI labels incident evidence as an incident gate signal at graph
level. That signal does not close per-target promotion gates; every review
target still carries its own human-gated promotion state.

## Regeneration Entry Point

The public payload can be regenerated from the recorded real-provider multi-run
response and the matching 7-day Evidence Bundle:

```bash
PYTHONPATH=src python scripts/generate_precomputed_review_from_multi_run.py \
  --multi-run-json workspace/amazon_notify_7d_20260630_profile_prompt_real_api/multi_ai_run.json \
  --evidence-bundle workspace/amazon_notify_7d_20260629/window_candidates/evidence_bundle_7d.json \
  --source-context workspace/real_api_qwen_glm_20260628_144326/source_context/source_context_bundle.json \
  --source-analysis workspace/real_api_qwen_glm_20260628_144326/source_analysis/source_analysis_bundle.json \
  --profile-draft workspace/source_profile_refresh_20260629/real_amazon_notify/profile_discovery/profile_draft.json \
  --approved-profile workspace/source_profile_refresh_20260629/real_amazon_notify/profile_discovery/approved_profile.json \
  --api-revision real-api-7d-approved-profile-prompt-20260630 \
  --profile-id amazon_notify_qwen_glm_full_corpus_approved \
  --updated-at 2026-06-30T11:28:23Z \
  --check
```

The workspace paths above are local execution artifacts and are not required for
the public read-only demo. The committed public artifact is the fixed JSON file
under `data/precomputed_review_summaries/`.
