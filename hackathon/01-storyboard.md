# 3分00秒 storyboard

## Narrative arc

```text
What is OES?
  -> What is stream_v3?
  -> Is recovery evidence enough to call an outage cause?
  -> OES refuses unsupported promotion
  -> the same OES runs on a second system
  -> added evidence changes the review state
```

The memorable transformation is:

```text
5 AI providers support a hypothesis
  != accepted cause

missing user-impact evidence arrives
  -> re-score
  -> primary candidate, still human-gated
```

## Timeline

| Time | Screen asset | Purpose |
| --- | --- | --- |
| 0:00-0:18 | `00-title-card.png` | Define OES and the primary target before any feature claim. |
| 0:18-0:36 | `01-stream-v3-system.png` | Explain what stream_v3 delivers and frame the incident question. |
| 0:36-0:50 | `10-runtime-review-hero.png` + `20-agreement-not-cause.png` | Show the finished evidence-backed review and the core contrast. |
| 0:50-1:18 | `14-code-profile-system-reading.png`, `15-code-profile-human-questions.png`, `07-human-semantics-gate.png` | Gemini reads sanitized code; a human answers, re-reviews candidate JSON, and fixes the approved semantics by SHA. |
| 1:18-1:35 | `11-runtime-agent-trace.png` + `22-guarded-autonomy.png` | Establish agent necessity and the autonomy boundary. |
| 1:35-2:03 | `12-runtime-target.png` + `21-runtime-metrics.png` | Explain one decision from the 45,000-line real run. |
| 2:03-2:28 | `04-platform-live.png`, then a separately recorded Fast GCP run | Prove genericity and a real Google Cloud model call. |
| 2:28-2:50 | `18-rescore-before.png`, `19-rescore-after.png`, `23-rescore-transition.png` | Show that added evidence changes review state. |
| 2:50-3:00 | `05-end-card.png` | Close on the product thesis. |

## Product and target distinction

- Product: Ops Evidence Synthesis (OES), a DevOps incident-review agent for SREs.
- Primary target: stream_v3 delivery runtime.
- stream_v3 outcome: fresh ADS-B visuals and audible program audio continuously
  delivered through YouTube Live.
- Incident question: recovery activity exists, but is it a healthy self-recovery
  or a user-impacting outage?
- Secondary target: amazon-notify, a systemd notification and watchdog service.
- Reason for the second target: demonstrate that the operational-profile JSON
  changes by system while OES core logic remains generic.

## Judging criteria mapping

| Judging axis | What the video proves |
| --- | --- |
| Agent at the center | Agent Trace validates citations, creates targets, requests evidence, and stops at a human gate. |
| Problem approach | Raw-data exposure and confident diagnosis without enough evidence are stated in the first 36 seconds. |
| Usability | Human actions are reduced to answer, approve, and review. |
| Practical value | Real 45,000-line input, five real providers, fixed hashes, and explicit missing evidence. |
| Implementation | Gemini source reading, Agent Platform model API, Cloud Build, Cloud Run, and auditable artifacts. |

## Main edit decision

Record the Fast GCP Review live section separately and splice it into the main
video. Use the single-provider 2,000-row path. Do not run the 200-row Gemma
cross-check in the three-minute video because its measured wall time is about
232 seconds.
