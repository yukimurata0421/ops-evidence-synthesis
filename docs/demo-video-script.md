# Three Minute Demo Script

Target length: 2:45 to 3:00. Use the live Cloud Run URL and avoid starting any
write path from the public surface.

## 0:00-0:20 Problem

Show the public entry page.

Narration:
Ops Evidence Synthesis targets a specific AIOps failure mode: during incidents,
AI often sounds confident before it has enough evidence. This system keeps raw
logs local, freezes sanitized evidence by SHA256, and turns model disagreement
into review targets instead of a fake single answer.

URL:
https://ops-evidence.yukimurata0421.dev/

## 0:20-0:55 Make

Open the main summary URL.

URL:
https://ops-evidence.yukimurata0421.dev/?evidence_sha256=7ca07bd8ed4bcb6009b654f17c40576a7b3462c62b2c74011c1623043550ccfb

Show:

- Raw log policy: `not_uploaded`
- Five provider run
- Agent Trace
- Review Graph Arbitration

Narration:
The investigation loop is autonomous up to the evidence boundary. Gemini runs
first as the reference provider. gpt-oss, Mistral, Qwen, and GLM act as
adversarial cross-checks. Gemini is not treated as a truth source or answer key. Every
provider sees the same SHA-fixed sanitized bundle, and every claim must point
back to evidence.

## 0:55-1:35 Evidence And Disagreement

Open the detail page.

URL:
https://ops-evidence.yukimurata0421.dev/ui/full-review-page?evidence_sha256=7ca07bd8ed4bcb6009b654f17c40576a7b3462c62b2c74011c1623043550ccfb

Show:

- Provider positions
- `claimed` versus `silent`
- Agreement and promotion gates
- Promotion gate

Narration:
The product does not turn agreement into truth. Technical convergence becomes a
review signal, but incident support and user impact still gate promotion. That
is why the system can investigate automatically while keeping causal judgement
human-gated.

## 1:35-2:20 Run

Open the More data rescore demo.

URL:
https://ops-evidence.yukimurata0421.dev/ui/rescore-demo?id=amazon-notify-more-data-rescore

Show:

- `Gemini-led control plane`
- before state: `validation_target`
- blocked reason: `user_impact_unverified`
- transition: `needs_more_data -> evidence_collected`
- after state: `primary_candidate`

Narration:
This is the DevOps improvement loop. The system requests the missing user-impact
signal, attaches a child Evidence Bundle, reruns the scoring projection, and
changes the promotion decision only after the missing evidence appears. This is
not a one-shot answer; it is an AI workflow that can improve under evidence.

## 2:20-2:45 Deliver

Open the API view or review graph.

URLs:

- https://ops-evidence.yukimurata0421.dev/ui/api?evidence_sha256=7ca07bd8ed4bcb6009b654f17c40576a7b3462c62b2c74011c1623043550ccfb
- https://ops-evidence.yukimurata0421.dev/ui/review-graph?evidence_sha256=7ca07bd8ed4bcb6009b654f17c40576a7b3462c62b2c74011c1623043550ccfb

Narration:
The delivered surface is read-only, fast, and deployed on Cloud Run. Public
reviewers can inspect the same fixed artifacts without credentials, raw logs, or
live model calls. Locally, `make demo`, `make ci`, and `make smoke-public`
regenerate and verify the review path.

## 2:45-3:00 Close

Show [submission-links.md](submission-links.md) or the README.

Narration:
The core claim is guarded autonomy: let AI investigate, compare, ask for more
evidence, and re-score, but do not let it invent certainty or take unsafe
operations past the human gate.
