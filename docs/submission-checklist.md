# Submission Checklist

Status as of 2026-07-01 after the public Cloud Run deployment.

## Official Sources Checked

The canonical URL list is maintained in
[submission-links.md](submission-links.md#official-event-sources).

Do not use `https://techplay.jp/event/984731` as a submission source; that page
is for a different event.

## Required Submission Surfaces

- Public GitHub repository URL: https://github.com/yukimurata0421/ops-evidence-synthesis
- Deployed project URL: https://ops-evidence.yukimurata0421.dev/
- ProtoPedia project URL: pending until the project page is created.
- Final Google Form submission: pending until ProtoPedia URL is available.
- Required ProtoPedia tag: `findy_hackathon`
- X post hashtag: `#findy_hackathon`

## ProtoPedia Assets

- Architecture image: [assets/architecture-devops-ai-agent.svg](assets/architecture-devops-ai-agent.svg)
- Demo video script: [demo-video-script.md](demo-video-script.md)
- ProtoPedia entry draft: [protopedia-entry-v3.md](protopedia-entry-v3.md)
- ProtoPedia Japanese entry draft: [protopedia-entry-japanese.md](protopedia-entry-japanese.md)
- X post draft: [x-post-draft.md](x-post-draft.md)
- Copy/paste URL set: [submission-links.md](submission-links.md)

## Live Deployment State

- Cloud Run service: `ops-evidence-api`
- Cloud Run region: `asia-northeast1`
- Public custom domain: https://ops-evidence.yukimurata0421.dev/
- Deployed revision: `ops-evidence-api-00214-5fm`
- Deployed image digest: `asia-northeast1-docker.pkg.dev/ops-evidence-synthesis/ops-evidence/ops-evidence-api@sha256:5c4aabda9f125ca30524f00587f2978e5044a304410ff64eea478b437550a05b`
- Public smoke after deploy: passed
- Fast GCP Review smoke: passed with `gemini-3.1-flash-lite`, 2,000 fixed sanitized amazon-notify rows, server wall time 11.879s.
- Fast Cross-check Lite smoke: passed with `gemini-3.1-flash-lite` + `gemma-4-26b-a4b-it-maas`, 2,000 fixed sanitized amazon-notify rows, server wall time 255.603s.
- Digest note: this digest is a public demo release attestation, not a private
  execution identifier.
- Cloud Run min instances: `1`
- Billing budget alert: `Ops Evidence Hackathon Budget`, 3000 JPY/month, project-filtered to `ops-evidence-synthesis`
- Budget alerts notify only; they do not automatically stop spend.

## Remaining Manual Actions

- Create the ProtoPedia project page.
- Upload the architecture image.
- Upload a 3 minute YouTube or Vimeo demo video.
- Replace pending ProtoPedia URL entries in `README.md`, `HACKATHON_SUBMISSION.md`, `docs/submission-links.md`, and this file.
- Paste GitHub, deployed project, and ProtoPedia URLs into the final Google Form.
- Post on X with `#findy_hackathon`.
