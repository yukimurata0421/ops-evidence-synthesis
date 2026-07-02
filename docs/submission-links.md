# Submission Links

Current reviewer-facing URLs for the hackathon submission.

## Official Event Sources

- ProtoPedia event page: https://protopedia.net/event/devops-ai-agent-hackathon
- Findy Notion official page: https://findy.notion.site/DevOps-AI-Agent-Hackathon-32a04bf5e7e4806786f2c871e8b6cb00
- Google Cloud Japan blog: https://cloud.google.com/blog/ja/products/ai-machine-learning/devops-ai-agent-hackathon-2026
- TechPlay mirror, correct event id: https://techplay.jp/event/995186

## Required Surfaces

- Public GitHub repository URL: https://github.com/yukimurata0421/ops-evidence-synthesis
- Deployed project URL: https://ops-evidence.yukimurata0421.dev/
- Proto Pedia project URL: pending until the project page is created.

## Primary Demo URLs

- Public entry: https://ops-evidence.yukimurata0421.dev/
- Primary summary: https://ops-evidence.yukimurata0421.dev/?evidence_sha256=345430d258752cefef81bfb587b4c210799d02bfc849e0a7ac5dc4c48fddb1d6
- Primary detail: https://ops-evidence.yukimurata0421.dev/ui/full-review-page?evidence_sha256=345430d258752cefef81bfb587b4c210799d02bfc849e0a7ac5dc4c48fddb1d6
- Primary incident report: https://ops-evidence.yukimurata0421.dev/ui/report.md?evidence_sha256=345430d258752cefef81bfb587b4c210799d02bfc849e0a7ac5dc4c48fddb1d6
- Primary human-readable API view: https://ops-evidence.yukimurata0421.dev/ui/api?evidence_sha256=345430d258752cefef81bfb587b4c210799d02bfc849e0a7ac5dc4c48fddb1d6
- Primary visual review graph: https://ops-evidence.yukimurata0421.dev/ui/review-graph?evidence_sha256=345430d258752cefef81bfb587b4c210799d02bfc849e0a7ac5dc4c48fddb1d6
- Guarded amazon-notify detail: https://ops-evidence.yukimurata0421.dev/ui/full-review-page?evidence_sha256=b99da97cab19f026b5475cdaa6100fdd6ebb6d96466a43e6b62a44b99ac414ec
- Guarded amazon-notify incident report: https://ops-evidence.yukimurata0421.dev/ui/report.md?evidence_sha256=b99da97cab19f026b5475cdaa6100fdd6ebb6d96466a43e6b62a44b99ac414ec
- Guarded amazon-notify API view: https://ops-evidence.yukimurata0421.dev/ui/api?evidence_sha256=b99da97cab19f026b5475cdaa6100fdd6ebb6d96466a43e6b62a44b99ac414ec
- More data rescore demo: https://ops-evidence.yukimurata0421.dev/ui/rescore-demo?id=amazon-notify-more-data-rescore
- Architecture image: docs/assets/architecture-devops-ai-agent.svg
- Demo video script: docs/demo-video-script.md
- ProtoPedia entry draft: docs/protopedia-entry-v3.md
- ProtoPedia Japanese entry draft: docs/protopedia-entry-japanese.md
- X post draft: docs/x-post-draft.md

## Machine-Readable Review URLs

- JSON summary API: https://ops-evidence.yukimurata0421.dev/ui/summary?evidence_sha256=345430d258752cefef81bfb587b4c210799d02bfc849e0a7ac5dc4c48fddb1d6
- JSON review targets API: https://ops-evidence.yukimurata0421.dev/review-targets?evidence_sha256=345430d258752cefef81bfb587b4c210799d02bfc849e0a7ac5dc4c48fddb1d6
- JSON review graph API with nodes/edges: https://ops-evidence.yukimurata0421.dev/review/graph?evidence_sha256=345430d258752cefef81bfb587b4c210799d02bfc849e0a7ac5dc4c48fddb1d6

## Additional Real API Validation URLs

- stream_v3 Dell runtime 45k detail: https://ops-evidence.yukimurata0421.dev/ui/full-review-page?evidence_sha256=345430d258752cefef81bfb587b4c210799d02bfc849e0a7ac5dc4c48fddb1d6
- stream_v3 Dell runtime 45k incident report: https://ops-evidence.yukimurata0421.dev/ui/report.md?evidence_sha256=345430d258752cefef81bfb587b4c210799d02bfc849e0a7ac5dc4c48fddb1d6
- stream_v3 Dell runtime 45k API view: https://ops-evidence.yukimurata0421.dev/ui/api?evidence_sha256=345430d258752cefef81bfb587b4c210799d02bfc849e0a7ac5dc4c48fddb1d6
- stream_v3 arena-server monitoring 50k detail: https://ops-evidence.yukimurata0421.dev/ui/full-review-page?evidence_sha256=6b7dad773b78274ed9706b02e15478427ad8817e8d8330ba19487d4293eeb3d3
- stream_v3 arena-server monitoring 50k incident report: https://ops-evidence.yukimurata0421.dev/ui/report.md?evidence_sha256=6b7dad773b78274ed9706b02e15478427ad8817e8d8330ba19487d4293eeb3d3
- stream_v3 arena-server monitoring 50k API view: https://ops-evidence.yukimurata0421.dev/ui/api?evidence_sha256=6b7dad773b78274ed9706b02e15478427ad8817e8d8330ba19487d4293eeb3d3

## Operational Readiness

- Cloud Run revision after deploy: `ops-evidence-api-00179-vv2`
- Deployed image digest: `asia-northeast1-docker.pkg.dev/ops-evidence-synthesis/ops-evidence/ops-evidence-api@sha256:26bc0130edd630e5a6b0544d349270c8c97fc2c1a7ab1141a421ff640567f2b9`
- Digest note: this digest is a public demo release attestation, not a private
  execution identifier.
- Public smoke after deploy: passed
- Cloud Run min instances: `1`
- Billing budget alert: `Ops Evidence Hackathon Budget`, 3000 JPY/month, project-filtered to `ops-evidence-synthesis`
- Budget note: alerts notify only; they do not automatically stop spend.
- ADK tool contract: implemented and visible in each `agent_trace`.
- Vertex Agent Runtime / Agent Engine status: the public artifact claims an
  ADK-compatible tool contract and Cloud Run serving path; no public
  `reasoningEngine` resource ID is recorded in this repository.
