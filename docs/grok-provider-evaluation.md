# Grok Provider Evaluation

This note records the Grok replacement test for the amazon-notify full-corpus
review path.

## Result

Grok is available through the Vertex OpenAPI endpoint when requested as
`xai/grok-4.20-reasoning`, but it should remain opt-in for the current public
review path. It is not yet a safe default replacement for Mistral in the
five-provider set.

## Test Scope

| Field | Value |
| --- | --- |
| Evidence SHA256 | `b99da97cab19f026b5475cdaa6100fdd6ebb6d96466a43e6b62a44b99ac414ec` |
| Sanitized rows | 44,944 |
| Evidence Items | 8,519 |
| Providers | Gemini, GPT OSS, Grok, Qwen, GLM |
| Grok model | `grok-4.20-reasoning` |
| Grok request model | `xai/grok-4.20-reasoning` |

## Observed Behavior

The small Grok API smoke test succeeded. The 44,944-row chunked full-corpus run
did not complete because Grok repeatedly returned `RESOURCE_EXHAUSTED` / HTTP
429 under the current project quota.

Partial chunk status at stop time:

| Provider | Chunk status |
| --- | --- |
| Gemini | 86 ok, 2 provider_error |
| GPT OSS | 105 ok, 2 schema_invalid |
| Grok | 26 ok, 27 rate_limited |
| Qwen | 86 ok, 5 schema_invalid |
| GLM | 86 ok, 2 provider_error |

## Decision

Keep Grok as an explicit opt-in provider. Do not replace Mistral in the default
five-provider Cloud Run Job set until the Grok quota and pacing can sustain the
45k-row full-corpus path.

Recommended next validation:

1. Increase or confirm Grok quota for Gemini Enterprise Agent Platform.
2. Re-run Grok with one worker and stricter token-per-minute pacing.
3. Promote Grok into the default provider set only after it reaches at least the
   configured 80% chunk success threshold on the amazon-notify corpus.
