PYTHON ?= python3
PUBLIC_BASE_URL ?= https://ops-evidence-api-vn3uyu4gia-an.a.run.app
PUBLIC_EVIDENCE_SHA ?= 5d0b5a918de1f99852498da2c8558d14993fe33b2259d23ac0ece59a900b48d9

.PHONY: demo verify-precomputed test ci smoke-public

demo:
	PYTHONPATH=src $(PYTHON) scripts/generate_precomputed_review.py

verify-precomputed:
	PYTHONPATH=src $(PYTHON) scripts/generate_precomputed_review.py --check

test:
	$(PYTHON) -m pytest

ci: verify-precomputed test

smoke-public:
	$(PYTHON) scripts/check_precomputed_review_url.py --base-url $(PUBLIC_BASE_URL) --evidence-sha $(PUBLIC_EVIDENCE_SHA)
