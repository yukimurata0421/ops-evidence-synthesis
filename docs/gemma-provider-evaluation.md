# Gemma Provider Evaluation

This note records the GLM replacement trial with a Google Gemma provider.

## Result

Gemma 2 model cards are visible in Model Garden, but the shared OpenAPI
chat-completions endpoint did not accept `google/gemma-2-27b-it` or
`google/gemma-2-9b-it` in this project. Both returned `404 NOT_FOUND` for
`global` and `us-central1`.

Gemma 4 MaaS was available through the global endpoint:

- Provider ID: `gemma-agent-platform`
- Model: `gemma-4-26b-a4b-it-maas`
- Request model: `google/gemma-4-26b-a4b-it-maas`
- Location: `global`

The small OES prompt smoke test returned schema-valid JSON after raising the
output budget to 8192 tokens.

## Full-Corpus Amazon-Notify Trial

The 44,944-row amazon-notify sanitized Evidence Bundle was analyzed with Gemma
4 using the same chunk-planning path as Gemini.

- Evidence SHA256: `b99da97cab19f026b5475cdaa6100fdd6ebb6d96466a43e6b62a44b99ac414ec`
- Gemma run: `workspace/e2e_real_api_source_sanitize_20260701T003045Z/multi_ai_gemma_replaces_glm_20260702T160243Z`
- Combined run: `workspace/e2e_real_api_source_sanitize_20260701T003045Z/multi_ai_real_5p_gemma_replaces_glm_combined_20260702T163000Z`
- Chunk plan: 86 chunks
- Final Gemma provider status: `ok`
- Final Gemma schema status: `valid`
- Input tokens: `7,049,837`
- Output tokens: `156,218`

One chunk produced a schema-invalid first record, then succeeded on the later
record for the same chunk. The final provider artifact therefore remained
schema-valid with 86 successful chunk results.

## GLM Replacement Combined Run

The combined comparison first used the existing recorded outputs for Gemini,
GPT OSS, Mistral, and Qwen, then replaced GLM with the new Gemma 4 output.
The final public artifact subsequently refreshed the Gemini leg with Gemini 3.1
Pro and recomputed the canonical graph over the five recorded outputs.

| Provider | Model | Status | Schema |
| --- | --- | --- | --- |
| `gemini-enterprise-agent-platform` | `gemini-3.1-pro-preview` | ok | valid |
| `openai-gpt-oss-on-vertex` | `gpt-oss-120b-maas` | ok | valid |
| `mistral-agent-platform` | `mistral-medium-3` | ok | valid |
| `qwen-agent-platform` | `qwen/qwen3-coder-480b-a35b-instruct-maas` | ok | valid |
| `gemma-agent-platform` | `gemma-4-26b-a4b-it-maas` | ok | valid |

Combined graph:

- Canonical graph SHA256: `8ad416a42a0a564ffe9221033cb50dde6a493ae3c8107bf6a69bf358b423d002`
- Successful providers: `5`
- Failed providers: `0`
- Canonical primary targets: `1`
- Canonical validation targets: `14`

## stream_v3 Refresh Trial

The same replacement was applied to the two public stream_v3 review paths, then
Gemini was refreshed with Gemini 3.1 Pro. In both cases Gemma 4 used the same
chunk-planning path as Gemini, then the Canonical Review Graph was recomputed
over recorded Gemini 3.1 Pro, GPT OSS, Mistral, Qwen, and Gemma 4 outputs.

| Corpus | Evidence SHA256 | Rows | Chunks | Gemma 4 wall time | Gemini 3.1 Pro wall time |
| --- | --- | ---: | ---: | ---: | ---: |
| Dell runtime | `a7fc02ea095516eaaed07f4599c3e25f94d092163ed163efccfb6f0300ee50e0` | 27,926 | 19 | 650.87s provider latency | 560.07s provider latency |
| arena-server monitoring | `8d165418fca88f856d8525bbdae804b6b649455450796b2dc44d2134b21abd9a` | 49,942 | 4 | 119.22s provider latency | 109.38s provider latency |

Final public graphs:

- Dell runtime Canonical graph SHA256: `e1c832b8396f32860ce7c2bd5328a6fdde785ffdd9d507a0db764cb2b4788d81`
- arena-server monitoring Canonical graph SHA256: `b5133772b23bdf85b7a33aafa0a425ea0395fe3ed4922e96c794a500cf8a1e86`

## Fast Cross-Check Lite Measurement

Gemma 4 was also tested in the public Fast GCP Review path against a bounded
200-row prefix of the same fixed sanitized amazon-notify fixture used by Gemini
Flash Lite. The path uses `run_multi_ai` with `gemini-fast-lite` and `gemma`, so
provider execution is parallel and successful outputs are merged into a separate
public review artifact. The bounded prefix keeps the live comparison inside the
Cloud Run request limit; the single-provider Fast Review retains 2,000 rows.

| Variant | Providers | Server wall time | Client wall time | Provider latency sum | Result |
| --- | --- | ---: | ---: | ---: | --- |
| Fast GCP Review | Gemini Flash Lite | 13.758s | 13.758s | 31.583s chunk latency | 1/1 schema-valid, 0 primary / 1 validation |
| Fast Cross-check Lite | Gemini Flash Lite + Gemma 4 | 231.935s | 231.935s | 414.631s chunk latency sum | 2/2 schema-valid, 0 primary / 3 validation |

Generated public review IDs:

- Fast GCP Review: `2641cb5fe5850d006864dec4aad3b3d2539e9efcef3753b43d5624f8b6e5136b`
- Fast Cross-check Lite: `6eac99d73635678165f54d1c5b82e96e86d0709ad5fcb243129e33f58400a9e5`

The variants use the same public-safe fixture but intentionally different row
counts, so each has its own Evidence SHA256 and `public_review_id`. Repeated runs
also remain distinct public artifacts and do not overwrite prior model output.

## Recommendation

Gemma 4 is a viable GLM replacement candidate for the public hackathon story
because it keeps the cross-check model set on Google Cloud and avoids adding
another China-based provider. Treat this as a provider-set refresh rather than
a blanket speed improvement: stream_v3 finished quickly, while amazon-notify
was slower at 44,944 rows. Large reviews should keep chunking and ledger
accounting enabled.
