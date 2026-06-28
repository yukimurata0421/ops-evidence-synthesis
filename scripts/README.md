# Script Inventory

Use the Makefile first for the review path. These scripts are grouped by role
so auxiliary operator tools do not look like required demo steps.

## Primary Review and Release

- `generate_precomputed_review.py` regenerates the committed review caches.
- `generate_precomputed_review_from_multi_run.py` regenerates a public cache
  from a recorded real-provider `/ai/multi-run` response and matching Evidence
  Bundle. Use `--log-observation` to add corpus-specific context without
  changing the stored provider response.
- `check_precomputed_review_url.py` smokes the live read-only public URL.
- `deploy_public_demo.sh` runs local gates, deploys Cloud Run, and smokes live.
- `manual_ci.sh` runs the local verification gate used before release.

## Local Demo Helpers

- `analyze_amazon_notify_local.py` analyzes local amazon-notify logs and can
  write a precomputed review summary.
- `demo_source_first_multi_ai.sh` and `demo_full_multi_ai.sh` exercise local
  sanitized demo flows.
- `demo_full_multi_ai_real.sh` is opt-in only and skips unless real-provider
  environment variables are explicitly enabled.
- `serve_local_viewer.py` starts a local viewer for a SQLite-backed workspace.

## Auxiliary Cloud Or Operator Tools

These are not part of the five-minute judging path.

- `cloud_run_smoke.py` checks a mutable Cloud Run deployment with a write token.
- `configure_cloud_run_domain.py` helps inspect or configure a custom domain.
- `terraform_docker.sh` runs Terraform through a container.
- `run_stream_v3_bigquery_14d.py` and `run_stream_v3_bigquery_aggregate.py`
  are retained as public examples of a second profile, not as the flagship demo.
