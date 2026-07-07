# Review Modes and Measured Public Replay

This note records how to describe the public demo modes and the local timing
measurements. The important distinction is that provider choice and review depth
are separate axes: a deterministic public replay can be fast and reproducible,
while a live AI review can spend more time on real provider calls and chunked
evidence coverage.

## Mode Names

| Mode | What it proves | What it does not claim |
| --- | --- | --- |
| Public Deterministic Replay | A committed public-safe fixture can regenerate a review graph locally without external AI API calls. | It is not a live AI latency benchmark. |
| More Data Rescore / Evidence Promotion Demo | New evidence can change a review target from `validation_target` to `primary_candidate` while preserving the human gate. | It does not auto-accept an incident cause. |
| Fast GCP Review | The deployed Cloud Run service can run a fixed sanitized amazon-notify sample through Vertex Gemini Flash Lite and return a measured review URL. | It is not the 45k-50k row forensic path and it does not accept arbitrary logs or URLs. |
| Fast Cross-check Lite | The same fixed sample can run Gemini Flash Lite and Gemma 4 in parallel and persist a separate public review artifact. | It is not the default fast path because Gemma 4 is materially slower on this sample. |
| Full Forensic AI Review | Larger real operations corpora can be reviewed through chunk fan-out, provider disagreement handling, and canonical graph merge. | It is served publicly as a precomputed artifact for immediate judge inspection. |

## Measured Public Replay Results

These measurements were taken on the committed public-safe amazon-notify fixture:
`data/amazon_notify_flagship_logs.jsonl`.

| Run | Time | Sanitized rows | Evidence Items | Providers | Review output | Evidence SHA256 |
| --- | ---: | ---: | ---: | --- | --- | --- |
| Public Replay - scoped initial review | 11.24s | 6,506 | 68 | 3/3 deterministic local | 0 primary / 1 validation | `265efc80247662d799b57b6a641509541b2e019ff3822825f2517687ab9954e8` |
| More Data Rescore | 1.12s | Existing parent + child bundle | n/a | n/a | `validation_target -> primary_candidate`, score 0.84 | preserved demo snapshot |
| Public Replay - full fixture review | 11.61s | 6,506 | 106 | 3/3 deterministic local | 0 primary / 1 validation | `3ee1f95fe1567c8b8bdbf3630100a52a24c7a76450d8b22afffc397c6a7df19d` |
| Fast GCP Review - live Cloud Run | status persisted; provider latency 29.854s | 2,000 | 570 | 1/1 real Vertex model | 0 primary / 1 validation | `5ae4f02d8390ecff4007c641c95fbfaa38af6356e4b53ff8267876a63e61781f` public review ID |
| Fast Cross-check Lite - live Cloud Run | status persisted; provider latency sum 416.490s | 2,000 | 570 | 2/2 real Vertex models | 0 primary / 5 validation | `9c09eaf87d152911e39a3d52bd982c8d68e397f6cf05505c3261804f5e070f27` public review ID |

## Interpretation

Do not present the two replay runs as a fast-versus-full speed comparison. They
are both deterministic local replays over the same 6,506-line fixture, both
include SQLite ingest, both use local deterministic providers, and neither waits
on external AI APIs or 45k-50k row chunk fan-out.

The public replay numbers are useful because they show that the committed
fixture can regenerate reviewer-visible artifacts quickly and reproducibly. The
strongest speed number is the More Data Rescore path: it demonstrates that once
evidence is attached, promotion-state recomputation can happen in about one
second while the human gate remains explicit.

Fast GCP Review is the short public live path. It uses a fixed sanitized
amazon-notify sample, runs from Cloud Run, calls Vertex Gemini Flash Lite, and
returns a normal review URL plus wall-clock timing. It is intentionally separate
from the larger full-forensic runs so judges can verify live GCP execution
without waiting for 45k-50k row chunk fan-out.

Fast Cross-check Lite uses the same fixed input and calls Gemini Flash Lite plus
Gemma 4 through the same `run_multi_ai` path. The two provider calls are launched
in parallel, and chunk calls are also parallelized where the provider settings
allow it. The measured 255.603s server wall time shows that Gemma 4 should remain
an optional cross-check path, not the default live demo path.

The larger real API runs should be described separately as recorded full
forensic reviews. Their value is full-corpus evidence accounting, chunked
provider execution, provider status visibility, and deterministic merge over
recorded provider outputs.

Recommended sentence:

```text
Ops Evidence Synthesis focuses on the missing step before action: evidence-grounded review.
```

Japanese reviewer-facing sentence:

```text
Ops Evidence Synthesisは、行動の前段階にある「その判断をしてよいだけの証拠があるか」を扱うDevOps Review Agentです。
```

## Reproduction Commands

Scoped public replay:

```bash
/usr/bin/time -p ops-evidence run-case \
  --input data/amazon_notify_flagship_logs.jsonl \
  --db workspace/mode_runs/fast_review.sqlite3 \
  --output-dir workspace/mode_runs/fast \
  --service amazon-notify \
  --environment prod \
  --start 2026-06-25T23:34:06Z \
  --end 2026-06-26T23:32:21Z
```

More Data Rescore:

```bash
/usr/bin/time -p .venv/bin/pytest \
  tests/test_api_more_data.py::test_more_data_child_bundle_rescores_parent_graph_and_promotion \
  -q
```

Full public fixture replay:

```bash
/usr/bin/time -p make demo
```

Local review UI check:

```bash
python -m uvicorn ops_evidence_synthesis.api:app \
  --host 127.0.0.1 \
  --port 8097
```

Open these paths after the local server starts:

```text
http://127.0.0.1:8097/ui/full-review-page?evidence_sha256=265efc80247662d799b57b6a641509541b2e019ff3822825f2517687ab9954e8
http://127.0.0.1:8097/ui/full-review-page?evidence_sha256=3ee1f95fe1567c8b8bdbf3630100a52a24c7a76450d8b22afffc397c6a7df19d
http://127.0.0.1:8097/ui/rescore-demo?id=amazon-notify-more-data-rescore
```
