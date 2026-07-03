# Three Minute Demo Script

Target length: 2:45 to 3:00. Use the live Cloud Run URL. The only public write
path shown is Fast GCP Review, which accepts no arbitrary input and runs a fixed
sanitized sample.

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
https://ops-evidence.yukimurata0421.dev/?evidence_sha256=345430d258752cefef81bfb587b4c210799d02bfc849e0a7ac5dc4c48fddb1d6

Show:

- Raw log policy: `not_uploaded`
- 45,000-row sanitized runtime corpus
- 39 provider-specific chunks
- 3 human-gated primary candidates
- Agent Trace
- Review Graph Arbitration

Narration:
The investigation loop is autonomous up to the evidence boundary. Raw rows stay
local. Every sanitized row is assigned to the coverage ledger, then five real
providers analyze grouped Evidence Items through provider-specific chunks. Each
provider sees the SHA-fixed sanitized bundle and source context, and every claim
must point back to evidence.

## 0:55-1:35 Evidence And Disagreement

Open the detail page.

URL:
https://ops-evidence.yukimurata0421.dev/ui/full-review-page?evidence_sha256=345430d258752cefef81bfb587b4c210799d02bfc849e0a7ac5dc4c48fddb1d6

Show:

- Provider positions
- provider support
- Agreement and promotion gates
- Promotion gate
- Queue rank and tie-breaks when priority scores are equal

Narration:
The product does not turn model output into truth. Provider support
becomes review work, but incident support and user impact still gate promotion.
That is why the system can investigate automatically while keeping causal
judgement human-gated.

## 1:35-2:00 Run Fast GCP Review

Open Fast GCP Review and click the run button.

URL:
https://ops-evidence.yukimurata0421.dev/ui/fast-gcp-review

Show:

- fixed `amazon-notify` sanitized sample
- `gemini-3.1-flash-lite`
- wall time and provider latency
- generated review URL

Narration:
For live GCP verification, the public app runs a fixed sanitized sample from
Cloud Run through Vertex Gemini Flash Lite. It does not accept arbitrary logs or
URLs, but it proves the deployed project can execute the evidence pipeline and
return a review URL.

## 2:00-2:30 Rescore Loop

Open the More data rescore demo.

URL:
https://ops-evidence.yukimurata0421.dev/ui/rescore-demo?id=amazon-notify-more-data-rescore

Show:

- control-plane trace
- before state: `validation_target`
- blocked reason: `user_impact_unverified`
- transition: `needs_more_data -> evidence_collected`
- after state: `primary_candidate`

Narration:
This is the DevOps improvement loop. The system requests the missing user-impact
signal, attaches a child Evidence Bundle, reruns the scoring projection, and
changes the promotion decision only after the missing evidence appears. This is
not a one-shot answer; it is an AI workflow that can improve under evidence.

## 2:30-2:50 Deliver

Open the API view or review graph.

URLs:

- https://ops-evidence.yukimurata0421.dev/ui/api?evidence_sha256=345430d258752cefef81bfb587b4c210799d02bfc849e0a7ac5dc4c48fddb1d6
- https://ops-evidence.yukimurata0421.dev/ui/review-graph?evidence_sha256=345430d258752cefef81bfb587b4c210799d02bfc849e0a7ac5dc4c48fddb1d6

Narration:
The delivered surface is read-only, fast, and deployed on Cloud Run. Public
reviewers can inspect the same fixed artifacts without credentials, raw logs, or
live model calls. Locally, `make demo`, `make ci`, and `make smoke-public`
regenerate and verify the review path.

## 2:50-3:00 Close

Show [submission-links.md](submission-links.md) or the README.

Narration:
The core claim is guarded autonomy: let AI investigate, compare, ask for more
evidence, and re-score, but do not let it invent certainty or take unsafe
operations past the human gate.
