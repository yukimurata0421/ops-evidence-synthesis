# Verified claims and source URLs

Use only these bounded claims in the video and submission text.

## Product and target

- Product: Ops Evidence Synthesis (OES).
- Product category: guarded DevOps incident-review agent for SREs.
- Primary target: `stream_v3` delivery runtime.
- Target outcome: continuously available YouTube Live delivery with fresh
  ADS-B visual content and audible program audio.

## Runtime real-provider review

- Staged input: 45,000 lines.
- Sanitized accepted events: 45,000 / 45,000 input lines.
- Grouped Evidence Items: 1,035.
- Evidence Item coverage: 1,035 / 1,035.
- Providers: five real provider outputs recorded; all five finished with
  schema-valid output and zero failed final chunks.
- Public state: 0 primary candidates and 10 validation targets.
- Raw row direct prompt count: 0.
- Evidence SHA256:
  `ab18d62c4e628e190345fa218834ca74276f556191d2f068a969f7922945a471`.

URLs:

- Code Profile: https://ops-evidence.yukimurata0421.dev/code-profiles/31dd5326f0e9e052697975e7174d9de6ebf7c2fde58625cb96ce41f29faab621/
- Full Review: https://ops-evidence.yukimurata0421.dev/ui/full-review-page?evidence_sha256=ab18d62c4e628e190345fa218834ca74276f556191d2f068a969f7922945a471
- Review Graph: https://ops-evidence.yukimurata0421.dev/ui/review-graph?evidence_sha256=ab18d62c4e628e190345fa218834ca74276f556191d2f068a969f7922945a471

## Fast GCP Review

- Hosting: Cloud Run.
- Model execution platform: Gemini Enterprise Agent Platform API, formerly
  called the Vertex AI API.
- Model: Model Garden Google publisher model `gemini-3.1-flash-lite`.
- Logic revision: `source-approved-evidence-v2`.
- Fixed sanitized rows: 2,000.
- Arbitrary input accepted: false.
- Raw log policy: `not_uploaded`.
- Verified run: `fast-gcp-review-20260712-source-approved-v2-final`.
- Verified result: 1 / 1 schema-valid, 0 primary, 1 validation.
- Measured server wall time: 13.758 seconds. Say `about 14 seconds`, not a
  guaranteed latency SLA.

URLs:

- Run page: https://ops-evidence.yukimurata0421.dev/ui/fast-gcp-review
- Verified result: https://ops-evidence.yukimurata0421.dev/ui/full-review-page?evidence_sha256=2641cb5fe5850d006864dec4aad3b3d2539e9efcef3753b43d5624f8b6e5136b

## More Data Rescore

- Demo ID: `amazon-notify-more-data-rescore`.
- Transition: `needs_more_data -> evidence_collected`.
- Review-state change: validation target to primary candidate.
- Final incident cause and operational action remain human-gated.
- This public button runs a fixed sanitized child-bundle re-score and does not
  start a model API call.

URL:

- https://ops-evidence.yukimurata0421.dev/ui/rescore-demo?id=amazon-notify-more-data-rescore

## Google Cloud naming

Current narration:

```text
Cloud Run上のOESが、Gemini Enterprise Agent Platform API経由で、
Model GardenのGemini 3.1 Flash-Liteを呼び出しています。
```

Implementation evidence:

```text
aiplatform.googleapis.com/v1/projects/{project}/locations/global/
publishers/google/models/gemini-3.1-flash-lite:generateContent
```

Do not claim use of the Gemini Enterprise app or a deployed Agent Runtime
resource. OES exposes an ADK-compatible tool contract in Agent Trace, while the
public application itself is served by Cloud Run.
