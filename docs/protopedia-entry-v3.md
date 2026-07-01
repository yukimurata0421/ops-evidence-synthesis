# ProtoPedia Entry Draft v3

This draft is aligned with the live demo, architecture image, and demo video
script. It intentionally uses `validation_target -> primary_candidate`, not
older placeholder wording.

## Title

Ops Evidence Synthesis

## One-Line Summary

Local-first DevOps incident review agent that keeps raw logs private, runs a
chunked real-provider review over sanitized evidence, and re-scores review
decisions when missing evidence arrives.

## Problem

Incident AI is useful only when it respects the evidence boundary. During real
operations, raw logs can contain secrets, internal identifiers, hostnames, and
personal data. At the same time, a model can produce a confident explanation
even when the evidence is incomplete.

Ops Evidence Synthesis targets that failure mode: unsafe certainty before
enough evidence exists.

## Solution

The system turns local sanitized logs into a SHA-fixed Evidence Bundle, assigns
every sanitized row to a coverage ledger, runs a chunked real-provider review,
validates cited claims, and projects the result into a Canonical Review Graph.

The current public flagship run uses Gemini, GPT OSS, Mistral, Qwen, and GLM
real API outputs over provider-specific chunks. Provider support is review
work, not truth.
Unresolved impact and operational-outcome questions remain validation targets.
Score is review priority, not truth probability.

The public Cloud Run surface is read-only and precomputed. Reviewers can inspect
the exact fixed artifacts without uploading raw logs, using credentials, or
starting live model work from the public URL.

## Why It Is An Agent

The product is not a static dashboard. The investigation loop can:

- inspect sanitized evidence,
- run the reference provider and cross-check providers,
- validate model outputs against schema and evidence references,
- arbitrate provider disagreement,
- create missing-evidence requests,
- attach a child Evidence Bundle,
- re-score the review graph when more evidence arrives.

The autonomy boundary is explicit. AI may investigate, compare, ask for more
evidence, and re-score. Final causal judgement and operational actions remain
human-gated.

## Make

The main review page shows a five-provider source-aware run over the
amazon-notify public payload:

- Evidence SHA256: `b99da97cab19f026b5475cdaa6100fdd6ebb6d96466a43e6b62a44b99ac414ec`
- Sanitized log count: 44,944
- Raw log policy: `not_uploaded`
- Providers: Gemini, GPT OSS, Mistral, Qwen, and GLM
- Maximum chunked provider calls: 105
- Output state: 5/5 schema-valid provider outputs with human-gated review targets

Live URL:
https://ops-evidence.yukimurata0421.dev/?evidence_sha256=b99da97cab19f026b5475cdaa6100fdd6ebb6d96466a43e6b62a44b99ac414ec

Detail URL:
https://ops-evidence.yukimurata0421.dev/ui/full-review-page?evidence_sha256=b99da97cab19f026b5475cdaa6100fdd6ebb6d96466a43e6b62a44b99ac414ec

## Run

The More data re-score demo shows the AI improvement cycle directly:

- Before: `validation_target`
- Promotion score: `0.69`
- Blocked reason: `user_impact_unverified`
- Missing evidence: user-visible delivery failure rows
- Evidence delta: 2 added log rows and 4 added evidence refs
- Transition: `needs_more_data -> evidence_collected`
- After: `primary_candidate`
- Promotion score: `0.84`
- Review priority score: `0.86`
- Blocked reasons: cleared
- Provider positions: recorded for the demo state, with the after state showing
  the target as `claimed`
- Promotion reason: child evidence added user-impact rows and removed
  `user_impact_unverified`

Live URL:
https://ops-evidence.yukimurata0421.dev/ui/rescore-demo?id=amazon-notify-more-data-rescore

This is the core DevOps loop: the AI workflow is not a one-shot answer. It can
ask for missing evidence, attach the new child bundle, and change the review
decision only after the evidence boundary is satisfied.

## Deliver

The public product path is deployed to Cloud Run behind a custom domain:

- Public entry: https://ops-evidence.yukimurata0421.dev/
- API view: https://ops-evidence.yukimurata0421.dev/ui/api?evidence_sha256=b99da97cab19f026b5475cdaa6100fdd6ebb6d96466a43e6b62a44b99ac414ec
- Visual graph: https://ops-evidence.yukimurata0421.dev/ui/review-graph?evidence_sha256=b99da97cab19f026b5475cdaa6100fdd6ebb6d96466a43e6b62a44b99ac414ec

The release path runs:

- `make ci`
- secret scan
- Cloud Build
- Cloud Run update
- live public smoke

The deployed service uses min instances 1 and a project-filtered budget alert
for submission-period availability and spend monitoring.

## Architecture

Architecture image:
[assets/architecture-devops-ai-agent.svg](assets/architecture-devops-ai-agent.svg)

Flow:

```text
raw logs stay local
-> sanitize and verify
-> sanitized code context
-> source-aware system profile draft
-> human-approved profile context
-> SHA-fixed Evidence Bundle
-> Mistral full-corpus chunks
-> recorded provider output
-> schema and evidence-reference validation
-> Canonical Review Graph
-> read-only Cloud Run UI
-> More data child bundle
-> re-score review target
```

## Repository And Demo

- GitHub: https://github.com/yukimurata0421/ops-evidence-synthesis
- Deployed project: https://ops-evidence.yukimurata0421.dev/
- Submission links: [submission-links.md](submission-links.md)
- Demo video script: [demo-video-script.md](demo-video-script.md)
- Submission checklist: [submission-checklist.md](submission-checklist.md)

## Closing

Ops Evidence Synthesis is built around guarded autonomy: give AI enough agency
to investigate, compare, request evidence, and improve the review graph, while
keeping raw data local and keeping final operational judgement behind a human
gate.
