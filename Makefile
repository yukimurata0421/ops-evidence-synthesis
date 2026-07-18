PYTHON ?= $(shell if [ -x .venv/bin/python ]; then echo .venv/bin/python; else echo python3; fi)
PYTEST_ARGS ?=
PROJECT_ID ?= ops-evidence-synthesis
PUBLIC_BASE_URL ?= https://ops-evidence.yukimurata0421.dev
PUBLIC_EVIDENCE_SHA ?= b7d56da85abe109ab044e05d4fc7b40462615e5b230db2b570f717c83762ab96
RETIRED_EVIDENCE_SHA ?= 5d0b5a918de1f99852498da2c8558d14993fe33b2259d23ac0ece59a900b48d9
SAMPLE_EVIDENCE_SHA ?= 518a25bd716c2c37ba10db0f3a56ab6562eb65e88e7b6b0b1c65c5f34d4ab38e
FLAGSHIP_DETERMINISTIC_EVIDENCE_SHA ?= a6af3d3ca5cc7254abbc97b232a430e1be111c8ce66adb28f32b9ee23b47cf75
FLAGSHIP_INPUT ?= data/amazon_notify_flagship_logs.jsonl
FLAGSHIP_START ?= 2026-06-26T22:30:00Z
FLAGSHIP_END ?= 2026-06-26T23:32:21Z
SAMPLE_PROFILE_DIR ?= data/public_profile_contexts/payment_api_sample
FLAGSHIP_PROFILE_DIR ?= data/public_profile_contexts/amazon_notify_sample
PUBLIC_ARCHIVE ?= /tmp/ops-evidence-synthesis-public.zip
PUBLIC_SMOKE_EXTRA_ARGS ?= --expect-provider gemini-enterprise-agent-platform --expect-provider openai-gpt-oss-on-vertex --expect-provider mistral-agent-platform --expect-provider qwen-agent-platform --expect-provider gemma-agent-platform
GCS_REVIEW_PREFIX ?= gs://$(PROJECT_ID)-private-artifacts/precomputed_review_summaries
GCS_REVIEW_SHA ?= $(PUBLIC_EVIDENCE_SHA)
GCS_REVIEW_SOURCE ?= data/precomputed_review_summaries/$(GCS_REVIEW_SHA).json
REVIEW_ARGS ?= $(REVIEW_FROM_LOCAL_ARGS)
REVIEW_FROM_LOCAL_ARGS ?=
LOCAL_REVIEW_DB ?= workspace/local_review/payment_api.sqlite3
LOCAL_REVIEW_INPUT ?= data/sample_logs.jsonl
LOCAL_REVIEW_SERVICE ?= payment-api
LOCAL_REVIEW_ENVIRONMENT ?= prod
LOCAL_REVIEW_START ?= 2026-06-12T10:00:00Z
LOCAL_REVIEW_END ?= 2026-06-12T10:20:00Z
LOCAL_REVIEW_PROVIDER ?= local
LOCAL_REVIEW_PORT ?= 8097
CLOUDFLARE_WAF_ARGS ?=
BUDGET_GUARD_ARGS ?=

.PHONY: demo demo-flagship demo-sample review review-from-local gcs-review publish-gcs-review smoke-gcs-review show-public-review-url run-local-review show-local-review serve-local-review verify-precomputed verify-flagship verify-sample test coverage ci smoke-public smoke-demo-video deploy-public configure-cloudflare-waf configure-budget-guard archive-public

demo: demo-flagship

demo-flagship:
	PYTHONPATH=src $(PYTHON) scripts/generate_precomputed_review.py --input $(FLAGSHIP_INPUT) --db workspace/public_demo/amazon_notify_flagship.sqlite3 --service amazon-notify --environment prod --start $(FLAGSHIP_START) --end $(FLAGSHIP_END) --lookback-minutes 1440 --updated-at $(FLAGSHIP_END) --target-limit 6 --source-note "generated from committed public-safe amazon-notify fixture with deterministic local providers and sanitized source profile context" --source-context $(FLAGSHIP_PROFILE_DIR)/source_context_bundle.json --source-analysis $(FLAGSHIP_PROFILE_DIR)/source_analysis_bundle.json --profile-draft $(FLAGSHIP_PROFILE_DIR)/profile_draft.json --approved-profile $(FLAGSHIP_PROFILE_DIR)/approved_profile.json --profile-id amazon_notify_sample_source_approved --expected-evidence-sha $(FLAGSHIP_DETERMINISTIC_EVIDENCE_SHA) --expected-log-count 6506 --require-convergence --expected-convergence-score 0.6666666667

demo-sample:
	PYTHONPATH=src $(PYTHON) scripts/generate_precomputed_review.py --source-note "generated from public sample fixture with deterministic local providers and sanitized source profile context" --source-context $(SAMPLE_PROFILE_DIR)/source_context_bundle.json --source-analysis $(SAMPLE_PROFILE_DIR)/source_analysis_bundle.json --profile-draft $(SAMPLE_PROFILE_DIR)/profile_draft.json --approved-profile $(SAMPLE_PROFILE_DIR)/approved_profile.json --profile-id payment_api_sample_source_approved --expected-evidence-sha $(SAMPLE_EVIDENCE_SHA)

review review-from-local:
	@PROJECT_ID="$(PROJECT_ID)" PUBLIC_BASE_URL="$(PUBLIC_BASE_URL)" PYTHONPATH=src $(PYTHON) scripts/gcs_review_flow.py $(REVIEW_ARGS)

gcs-review: publish-gcs-review smoke-gcs-review show-public-review-url

publish-gcs-review:
	test -f $(GCS_REVIEW_SOURCE)
	gcloud storage cp $(GCS_REVIEW_SOURCE) $(GCS_REVIEW_PREFIX)/$(GCS_REVIEW_SHA).json

smoke-gcs-review:
	$(PYTHON) scripts/check_precomputed_review_url.py --base-url $(PUBLIC_BASE_URL) --evidence-sha $(GCS_REVIEW_SHA) --missing-evidence-sha $(RETIRED_EVIDENCE_SHA) $(PUBLIC_SMOKE_EXTRA_ARGS)

show-public-review-url:
	@echo "$(PUBLIC_BASE_URL)/ui/full-review-page?evidence_sha256=$(GCS_REVIEW_SHA)"

run-local-review:
	mkdir -p $(dir $(LOCAL_REVIEW_DB))
	rm -f $(LOCAL_REVIEW_DB)
	PYTHONPATH=src $(PYTHON) -m ops_evidence_synthesis.cli --db $(LOCAL_REVIEW_DB) run-case --input $(LOCAL_REVIEW_INPUT) --service $(LOCAL_REVIEW_SERVICE) --environment $(LOCAL_REVIEW_ENVIRONMENT) --start $(LOCAL_REVIEW_START) --end $(LOCAL_REVIEW_END) --provider $(LOCAL_REVIEW_PROVIDER) --review-base-url http://127.0.0.1:$(LOCAL_REVIEW_PORT)

show-local-review:
	PYTHONPATH=src $(PYTHON) -m ops_evidence_synthesis.cli --db $(LOCAL_REVIEW_DB) reviews --limit 5

serve-local-review:
	PYTHONPATH=src $(PYTHON) -m ops_evidence_synthesis.cli --db $(LOCAL_REVIEW_DB) serve --port $(LOCAL_REVIEW_PORT)

verify-precomputed: verify-sample verify-flagship

verify-sample:
	PYTHONPATH=src $(PYTHON) scripts/generate_precomputed_review.py --source-note "generated from public sample fixture with deterministic local providers and sanitized source profile context" --source-context $(SAMPLE_PROFILE_DIR)/source_context_bundle.json --source-analysis $(SAMPLE_PROFILE_DIR)/source_analysis_bundle.json --profile-draft $(SAMPLE_PROFILE_DIR)/profile_draft.json --approved-profile $(SAMPLE_PROFILE_DIR)/approved_profile.json --profile-id payment_api_sample_source_approved --expected-evidence-sha $(SAMPLE_EVIDENCE_SHA) --check

verify-flagship:
	PYTHONPATH=src $(PYTHON) scripts/generate_precomputed_review.py --input $(FLAGSHIP_INPUT) --db workspace/public_demo/amazon_notify_flagship.sqlite3 --service amazon-notify --environment prod --start $(FLAGSHIP_START) --end $(FLAGSHIP_END) --lookback-minutes 1440 --updated-at $(FLAGSHIP_END) --target-limit 6 --source-note "generated from committed public-safe amazon-notify fixture with deterministic local providers and sanitized source profile context" --source-context $(FLAGSHIP_PROFILE_DIR)/source_context_bundle.json --source-analysis $(FLAGSHIP_PROFILE_DIR)/source_analysis_bundle.json --profile-draft $(FLAGSHIP_PROFILE_DIR)/profile_draft.json --approved-profile $(FLAGSHIP_PROFILE_DIR)/approved_profile.json --profile-id amazon_notify_sample_source_approved --expected-evidence-sha $(FLAGSHIP_DETERMINISTIC_EVIDENCE_SHA) --expected-log-count 6506 --require-convergence --expected-convergence-score 0.6666666667 --check

test:
	$(PYTHON) -m pytest $(PYTEST_ARGS)

coverage:
	$(PYTHON) -m coverage erase
	$(PYTHON) -m coverage run -m pytest $(PYTEST_ARGS)
	$(PYTHON) -m coverage report

ci: verify-precomputed coverage

smoke-public:
	$(PYTHON) scripts/check_precomputed_review_url.py --base-url $(PUBLIC_BASE_URL) --evidence-sha $(PUBLIC_EVIDENCE_SHA) --missing-evidence-sha $(RETIRED_EVIDENCE_SHA) $(PUBLIC_SMOKE_EXTRA_ARGS)

smoke-demo-video:
	$(PYTHON) scripts/check_demo_video_path.py --base-url $(PUBLIC_BASE_URL)

deploy-public:
	scripts/deploy_public_demo.sh

configure-cloudflare-waf:
	$(PYTHON) scripts/configure_cloudflare_waf.py $(CLOUDFLARE_WAF_ARGS)

configure-budget-guard:
	$(PYTHON) scripts/configure_budget_fast_gcp_guard.py $(BUDGET_GUARD_ARGS)

archive-public:
	git archive --format=zip --output $(PUBLIC_ARCHIVE) HEAD
	ls -lh $(PUBLIC_ARCHIVE)
