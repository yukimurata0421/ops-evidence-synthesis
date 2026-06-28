# Real API Source-Aware Run

This document records the public read-only artifact generated from the
source-aware real-provider amazon-notify run.

## Public URLs

- Full review page: https://ops-evidence.yukimurata0421.dev/ui/full-review-page?evidence_sha256=7e95346cbf15de7f104631b72d784e02665d0cc1488e42a4ccf69b76fe47308d
- Human-readable API view: https://ops-evidence.yukimurata0421.dev/ui/api?evidence_sha256=7e95346cbf15de7f104631b72d784e02665d0cc1488e42a4ccf69b76fe47308d
- Visual review graph: https://ops-evidence.yukimurata0421.dev/ui/review-graph?evidence_sha256=7e95346cbf15de7f104631b72d784e02665d0cc1488e42a4ccf69b76fe47308d
- JSON review graph: https://ops-evidence.yukimurata0421.dev/review/graph?evidence_sha256=7e95346cbf15de7f104631b72d784e02665d0cc1488e42a4ccf69b76fe47308d

## Fixed Artifacts

| Artifact | Value |
| --- | --- |
| Evidence SHA256 | `7e95346cbf15de7f104631b72d784e02665d0cc1488e42a4ccf69b76fe47308d` |
| Pipeline run | `pipe-3097375a9dcb41919089` |
| API revision | `ops-evidence-api-e2e-00006-6t9` |
| Canonical graph SHA256 | `30ab89331c7389a11ce29394d8d33d3f2b8f6a48c2bfac1646b70aa9acd90215` |
| Input fingerprint SHA256 | `4c1f7d780fd1c713fc987824fddca067f585a75618058f9cff49189677708547` |
| Public payload | `data/precomputed_review_summaries/7e95346cbf15de7f104631b72d784e02665d0cc1488e42a4ccf69b76fe47308d.json` |
| Payload SHA256 | `f17d1546dd7161c3e7edd07eeb9f50d2a7639d0735ed4c8699dfe2a15e141874` |

## Data Boundary

The run used a local-first evidence boundary.

1. Raw amazon-notify logs stayed local.
2. 6,506 rows were sanitized and persisted as the API-side corpus.
3. The Evidence Bundle was fixed by SHA256 before provider execution.
4. Sanitized source context and source analysis were attached as interpretation
   context only.
5. Runtime claims still had to cite Evidence Item IDs from the sanitized corpus.
6. The public URL serves a precomputed read-only payload and does not run models
   during page load.

## Token Compression

The API did not send all 6,506 log rows as raw prompt text. The persisted
sanitized corpus was converted into grouped evidence items, then bounded for
model input.

| Layer | Count |
| --- | ---: |
| Sanitized log rows in DB/API corpus | 6,506 |
| Grouped Evidence Items retained in bundle | 1,639 |
| Evidence Items selected for model prompt | 140 |
| Occurrences represented by selected items | 4,939 |
| Occurrence coverage | 75.9% |

Selection prioritized high-severity, high-count, and operationally interesting
patterns such as run results, watchdog/service state, token refresh, Pub/Sub
idle behavior, status snapshots, and notification workflow events. The prompt
kept evidence IDs, counts, first/last seen timestamps, compact templates, and
profile context. Low-signal tail patterns remained in the persisted sanitized
corpus and were represented by corpus-level counts rather than row bodies.

Total provider token usage was 307,972 input tokens and 8,026 output tokens
across the five successful providers.

## Provider Results

| Provider | Model | Status | Schema | Latency ms | Input tokens | Output tokens |
| --- | --- | --- | --- | ---: | ---: | ---: |
| `gemini-enterprise-agent-platform` | `gemini-3.1-pro-preview` | ok | valid | 38,230 | 70,530 | 588 |
| `openai-gpt-oss-on-vertex` | `gpt-oss-120b-maas` | ok | valid | 33,262 | 53,409 | 2,799 |
| `mistral-agent-platform` | `mistral-medium-3` | ok | valid | 28,499 | 67,140 | 809 |
| `qwen-agent-platform` | `qwen/qwen3-coder-480b-a35b-instruct-maas` | ok | valid | 25,058 | 62,631 | 1,708 |
| `glm-agent-platform` | `zai-org/glm-5-maas` | ok | valid | 36,551 | 54,262 | 2,122 |

## Review Outcome

The canonical review graph produced:

- 1 primary candidate
- 6 validation targets
- 2 monitor-only context items
- 0 auto-archived targets
- 5/5 provider detection overlap

Technical convergence is treated as review support only. The incident baseline
remained open because cause, impact, and next action were not fully aligned and
still require human review.

## Regeneration Entry Point

The public payload can be regenerated from the recorded API multi-run response
and the matching Evidence Bundle:

```bash
PYTHONPATH=src python scripts/generate_precomputed_review_from_multi_run.py \
  --multi-run-json workspace/real_api_qwen_glm_20260628_144326/api_multi_run.json \
  --evidence-bundle workspace/real_api_qwen_glm_20260628_144326/evidence_bundle_full_6506.json \
  --source-context workspace/real_api_qwen_glm_20260628_144326/source_context/source_context_bundle.json \
  --source-analysis workspace/real_api_qwen_glm_20260628_144326/source_analysis/source_analysis_bundle.json \
  --api-revision ops-evidence-api-e2e-00006-6t9 \
  --profile-id amazon_notify_qwen_glm_full_corpus_approved \
  --updated-at 2026-06-28T14:48:00Z \
  --check
```

The workspace paths above are local execution artifacts and are not required for
the public read-only demo. The committed public artifact is the fixed JSON file
under `data/precomputed_review_summaries/`.
