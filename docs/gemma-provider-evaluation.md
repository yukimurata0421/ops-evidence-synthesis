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

The combined comparison used the existing recorded outputs for Gemini, GPT OSS,
Mistral, and Qwen, then replaced GLM with the new Gemma 4 output.

| Provider | Model | Status | Schema |
| --- | --- | --- | --- |
| `gemini-enterprise-agent-platform` | `gemini-3.1-flash-lite` | ok | valid |
| `openai-gpt-oss-on-vertex` | `gpt-oss-120b-maas` | ok | valid |
| `mistral-agent-platform` | `mistral-medium-3` | ok | valid |
| `qwen-agent-platform` | `qwen/qwen3-coder-480b-a35b-instruct-maas` | ok | valid |
| `gemma-agent-platform` | `gemma-4-26b-a4b-it-maas` | ok | valid |

Combined graph:

- Canonical graph SHA256: `5c525b6369855440bc40975dcfab0fa90895cda8849ec7cfdc0b9f6a561d105c`
- Successful providers: `5`
- Failed providers: `0`
- Canonical primary targets: `2`
- Canonical validation targets: `13`

## stream_v3 Refresh Trial

The same replacement was applied to the two public stream_v3 review paths. In
both cases Gemma 4 used the same chunk-planning path as Gemini, then the
Canonical Review Graph was recomputed over recorded Gemini, GPT OSS, Mistral,
Qwen, and Gemma 4 outputs.

| Corpus | Evidence SHA256 | Rows | Chunks | Gemma 4 wall time | Merge time | Payload generation |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| Dell runtime | `345430d258752cefef81bfb587b4c210799d02bfc849e0a7ac5dc4c48fddb1d6` | 45,000 | 32 | 380.47s | 2.736s | 1.47s |
| arena-server monitoring | `6b7dad773b78274ed9706b02e15478427ad8817e8d8330ba19487d4293eeb3d3` | 50,000 | 18 | 166.85s | 1.796s | 1.17s |

Final public graphs:

- Dell runtime Canonical graph SHA256: `e5cfc53f9226b8237aa971a57d89075eab8c8748c9a07666abb8abbc0232ac49`
- arena-server monitoring Canonical graph SHA256: `786505578a25454378ebdda2404a5b62e72982761cab9d338a8e04dd0b84f530`

## Recommendation

Gemma 4 is a viable GLM replacement candidate for the public hackathon story
because it keeps the cross-check model set on Google Cloud and avoids adding
another China-based provider. It is slower than a small smoke test suggests, so
large reviews should keep chunking and ledger accounting enabled.
