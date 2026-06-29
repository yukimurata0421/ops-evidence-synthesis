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
- Summary: https://ops-evidence.yukimurata0421.dev/?evidence_sha256=7e95346cbf15de7f104631b72d784e02665d0cc1488e42a4ccf69b76fe47308d
- Detail: https://ops-evidence.yukimurata0421.dev/ui/full-review-page?evidence_sha256=7e95346cbf15de7f104631b72d784e02665d0cc1488e42a4ccf69b76fe47308d
- Human-readable API view: https://ops-evidence.yukimurata0421.dev/ui/api?evidence_sha256=7e95346cbf15de7f104631b72d784e02665d0cc1488e42a4ccf69b76fe47308d
- Visual review graph: https://ops-evidence.yukimurata0421.dev/ui/review-graph?evidence_sha256=7e95346cbf15de7f104631b72d784e02665d0cc1488e42a4ccf69b76fe47308d
- More data rescore demo: https://ops-evidence.yukimurata0421.dev/ui/rescore-demo?id=amazon-notify-more-data-rescore
- Architecture image: docs/assets/architecture-devops-ai-agent.svg
- Demo video script: docs/demo-video-script.md
- ProtoPedia entry draft: docs/protopedia-entry-v3.md
- ProtoPedia Japanese entry draft: docs/protopedia-entry-japanese.md
- X post draft: docs/x-post-draft.md

## Machine-Readable Review URLs

- JSON summary API: https://ops-evidence.yukimurata0421.dev/ui/summary?evidence_sha256=7e95346cbf15de7f104631b72d784e02665d0cc1488e42a4ccf69b76fe47308d
- JSON review targets API: https://ops-evidence.yukimurata0421.dev/review-targets?evidence_sha256=7e95346cbf15de7f104631b72d784e02665d0cc1488e42a4ccf69b76fe47308d
- JSON review graph API with nodes/edges: https://ops-evidence.yukimurata0421.dev/review/graph?evidence_sha256=7e95346cbf15de7f104631b72d784e02665d0cc1488e42a4ccf69b76fe47308d

## Operational Readiness

- Cloud Run revision after deploy: `ops-evidence-api-00136-xf5`
- Deployed image digest: `asia-northeast1-docker.pkg.dev/ops-evidence-synthesis/ops-evidence/ops-evidence-api@sha256:3520027ce775ae779a7c8c262884392b0b8c7c66ac9b9e66c7eb919a0719b404`
- Public smoke after deploy: passed
- Cloud Run min instances: `1`
- Billing budget alert: `Ops Evidence Hackathon Budget`, 3000 JPY/month, project-filtered to `ops-evidence-synthesis`
- Budget note: alerts notify only; they do not automatically stop spend.
