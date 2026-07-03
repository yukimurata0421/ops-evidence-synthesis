# Claude Opus Provider Evaluation

This note records the Claude Opus replacement trial for the public provider set.

## Result

Claude Opus was reachable through Vertex AI in the `global` location with:

- Provider ID: `claude-agent-platform`
- Model: `claude-opus-4-8`
- Publisher: `anthropic`
- Location: `global`

The model is not currently usable as a Mistral replacement for the public
five-provider set. Both the default chunked smoke run and a serial, low-parallel
retry returned HTTP 429 for every chunk:

```text
Quota exceeded for aiplatform.googleapis.com/global_online_prediction_requests_per_base_model
base model: anthropic-claude-opus
```

This is different from a model-not-found or access-denied failure. The request
reached the Claude Opus base model, but the available online prediction quota
was insufficient.

## Smoke Test Boundary

The smoke tests used the existing sanitized payment-api Evidence Bundle,
approved profile, sanitized source context, and sanitized source analysis. Raw
logs and raw source were not sent.

Two settings were tried:

| Trial | Chunk policy | Outcome |
| --- | --- | --- |
| Default Claude chunk policy | 3 chunks, provider defaults | `failed`, `schema_valid=false`, 3 rate-limited chunks |
| Serial Opus policy | worker=1, larger chunk budget, 60s start interval | `failed`, `schema_valid=false`, all retry attempts rate-limited |

The second trial rules out simple local fan-out as the main cause. It still
failed after serializing requests, so the blocker is provider quota rather than
OES chunk scheduling.

## Recommendation

Do not replace Mistral with Claude Opus in the public five-provider set yet.
Keep Mistral as the currently working non-Google-family provider until Claude
Opus produces a schema-valid chunked run.

Promotion criteria for adopting Claude Opus:

1. A small sanitized fixture returns `status=ok` and `schema_valid=true`.
2. Provider chunk ledger records no final `rate_limited` chunks.
3. A 40k-50k row run reaches at least the existing partial-success threshold.
4. The canonical graph is regenerated and provider hashes change from the
   Mistral-based artifact.

