PYTHON ?= $(shell if [ -x .venv/bin/python ]; then echo .venv/bin/python; else echo python3; fi)
PYTEST_ARGS ?=
PUBLIC_BASE_URL ?= https://ops-evidence.yukimurata0421.dev
PUBLIC_EVIDENCE_SHA ?= 345430d258752cefef81bfb587b4c210799d02bfc849e0a7ac5dc4c48fddb1d6
RETIRED_EVIDENCE_SHA ?= 5d0b5a918de1f99852498da2c8558d14993fe33b2259d23ac0ece59a900b48d9
SAMPLE_EVIDENCE_SHA ?= a7da502659d7af556b71f341ff098be6460a41b844761c3fff96339d58f46208
FLAGSHIP_DETERMINISTIC_EVIDENCE_SHA ?= 3ee1f95fe1567c8b8bdbf3630100a52a24c7a76450d8b22afffc397c6a7df19d
FLAGSHIP_INPUT ?= data/amazon_notify_flagship_logs.jsonl
FLAGSHIP_START ?= 2026-06-26T22:30:00Z
FLAGSHIP_END ?= 2026-06-26T23:32:21Z
SAMPLE_PROFILE_DIR ?= data/public_profile_contexts/payment_api_sample
FLAGSHIP_PROFILE_DIR ?= data/public_profile_contexts/amazon_notify_sample
PUBLIC_ARCHIVE ?= /tmp/ops-evidence-synthesis-public.zip
PUBLIC_SMOKE_EXTRA_ARGS ?= --expect-provider gemini-enterprise-agent-platform --expect-provider openai-gpt-oss-on-vertex --expect-provider mistral-agent-platform --expect-provider qwen-agent-platform --expect-provider gemma-agent-platform

.PHONY: demo demo-flagship demo-sample verify-precomputed verify-flagship verify-sample test ci smoke-public deploy-public archive-public

demo: demo-flagship

demo-flagship:
	PYTHONPATH=src $(PYTHON) scripts/generate_precomputed_review.py --input $(FLAGSHIP_INPUT) --db workspace/public_demo/amazon_notify_flagship.sqlite3 --service amazon-notify --environment prod --start $(FLAGSHIP_START) --end $(FLAGSHIP_END) --lookback-minutes 1440 --updated-at $(FLAGSHIP_END) --target-limit 6 --source-note "generated from committed public-safe amazon-notify fixture with deterministic local providers and sanitized source profile context" --source-context $(FLAGSHIP_PROFILE_DIR)/source_context_bundle.json --source-analysis $(FLAGSHIP_PROFILE_DIR)/source_analysis_bundle.json --profile-draft $(FLAGSHIP_PROFILE_DIR)/profile_draft.json --approved-profile $(FLAGSHIP_PROFILE_DIR)/approved_profile.json --profile-id amazon_notify_sample_source_approved --expected-evidence-sha $(FLAGSHIP_DETERMINISTIC_EVIDENCE_SHA) --expected-log-count 6506 --require-convergence --expected-convergence-score 0.6666666667

demo-sample:
	PYTHONPATH=src $(PYTHON) scripts/generate_precomputed_review.py --source-note "generated from public sample fixture with deterministic local providers and sanitized source profile context" --source-context $(SAMPLE_PROFILE_DIR)/source_context_bundle.json --source-analysis $(SAMPLE_PROFILE_DIR)/source_analysis_bundle.json --profile-draft $(SAMPLE_PROFILE_DIR)/profile_draft.json --approved-profile $(SAMPLE_PROFILE_DIR)/approved_profile.json --profile-id payment_api_sample_source_approved --expected-evidence-sha $(SAMPLE_EVIDENCE_SHA)

verify-precomputed: verify-sample verify-flagship

verify-sample:
	PYTHONPATH=src $(PYTHON) scripts/generate_precomputed_review.py --source-note "generated from public sample fixture with deterministic local providers and sanitized source profile context" --source-context $(SAMPLE_PROFILE_DIR)/source_context_bundle.json --source-analysis $(SAMPLE_PROFILE_DIR)/source_analysis_bundle.json --profile-draft $(SAMPLE_PROFILE_DIR)/profile_draft.json --approved-profile $(SAMPLE_PROFILE_DIR)/approved_profile.json --profile-id payment_api_sample_source_approved --expected-evidence-sha $(SAMPLE_EVIDENCE_SHA) --check

verify-flagship:
	PYTHONPATH=src $(PYTHON) scripts/generate_precomputed_review.py --input $(FLAGSHIP_INPUT) --db workspace/public_demo/amazon_notify_flagship.sqlite3 --service amazon-notify --environment prod --start $(FLAGSHIP_START) --end $(FLAGSHIP_END) --lookback-minutes 1440 --updated-at $(FLAGSHIP_END) --target-limit 6 --source-note "generated from committed public-safe amazon-notify fixture with deterministic local providers and sanitized source profile context" --source-context $(FLAGSHIP_PROFILE_DIR)/source_context_bundle.json --source-analysis $(FLAGSHIP_PROFILE_DIR)/source_analysis_bundle.json --profile-draft $(FLAGSHIP_PROFILE_DIR)/profile_draft.json --approved-profile $(FLAGSHIP_PROFILE_DIR)/approved_profile.json --profile-id amazon_notify_sample_source_approved --expected-evidence-sha $(FLAGSHIP_DETERMINISTIC_EVIDENCE_SHA) --expected-log-count 6506 --require-convergence --expected-convergence-score 0.6666666667 --check

test:
	$(PYTHON) -m pytest $(PYTEST_ARGS)

ci: verify-precomputed test

smoke-public:
	$(PYTHON) scripts/check_precomputed_review_url.py --base-url $(PUBLIC_BASE_URL) --evidence-sha $(PUBLIC_EVIDENCE_SHA) --missing-evidence-sha $(RETIRED_EVIDENCE_SHA) $(PUBLIC_SMOKE_EXTRA_ARGS)

deploy-public:
	scripts/deploy_public_demo.sh

archive-public:
	git archive --format=zip --output $(PUBLIC_ARCHIVE) HEAD
	ls -lh $(PUBLIC_ARCHIVE)
