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

The same replacement was applied to the two public stream_v3 review paths. A
later source-aware reinterpretation pass reused the focused code profiles,
called the current five-provider set again, and recomputed the Canonical Review
Graph over recorded Gemini 3.1 Pro, GPT OSS, Mistral, Qwen, and Gemma 4 outputs.

| Corpus | Evidence SHA256 | Rows | Chunks | Reinterpretation wall time | Provider status |
| --- | --- | ---: | ---: | ---: | --- |
| Dell runtime | `345430d258752cefef81bfb587b4c210799d02bfc849e0a7ac5dc4c48fddb1d6` | 45,000 | 33 | 207.108s after cached resume | 5/5 schema-valid |
| arena-server monitoring | `6b7dad773b78274ed9706b02e15478427ad8817e8d8330ba19487d4293eeb3d3` | 50,000 | 18 | 497.909s + 239s GPT OSS retry | 5/5 schema-valid after GPT OSS 120B rerun |

Current public graphs:

- Dell runtime Canonical graph SHA256: `7b8bbf364706cda1b558476b5a08c882356449710612989dbf86ca8a68cb9266`
- arena-server monitoring Canonical graph SHA256: `b78f802e6fcac1e3562aefdf7ff595a71a7898aa0c9af14366ea50a9239ea6ae`

## Fast Cross-Check Lite Measurement

Gemma 4 was also tested in the public Fast GCP Review path against the same
2,000-row fixed sanitized amazon-notify sample as Gemini Flash Lite. The path
uses `run_multi_ai` with `gemini-fast-lite` and `gemma`, so provider execution is
parallel and successful outputs are merged into a separate public review artifact.

| Variant | Providers | Server wall time | Client wall time | Provider latency sum | Result |
| --- | --- | ---: | ---: | ---: | --- |
| Fast GCP Review | Gemini Flash Lite | 11.509s | 11.717s | 27.543s | 1/1 schema-valid, 0 primary / 3 validation |
| Fast Cross-check Lite | Gemini Flash Lite + Gemma 4 | 255.603s | 255.908s | 798.648s | 2/2 schema-valid, 0 primary / 9 validation |

Generated public review IDs:

- Fast GCP Review: `94f8135156127826ed74cb32c6de5f000293fc0a827c84b6e6f97d55a1427b20`
- Fast Cross-check Lite: `32ca03ecb0188e3da835344798210a9e0d817ba03630aca608b600a14e36a503`

Both variants used the same Evidence SHA256 because they read the same fixed
input: `9fa67b71c3f1a3a3a39dc712ae7692e199c4694a3393dcfb3bd4b3ba3a4d9e51`.
The public UI therefore stores generated review artifacts under a separate
`public_review_id` so repeated runs do not overwrite each other by evidence hash.

## Recommendation

Gemma 4 is a viable GLM replacement candidate for the public hackathon story
because it keeps the cross-check model set on Google Cloud and avoids adding
another China-based provider. Treat this as a provider-set refresh rather than
a blanket speed improvement: stream_v3 finished quickly, while amazon-notify
was slower at 44,944 rows and the 2,000-row public cross-check took 255.603s.
Large reviews should keep chunking and ledger accounting enabled, and the live
judge-facing fast path should keep Gemini Flash Lite as the default while
exposing Gemma 4 as an optional cross-check.
