PYTHON ?= python3

.PHONY: demo verify-precomputed test

demo:
	PYTHONPATH=src $(PYTHON) scripts/generate_precomputed_review.py

verify-precomputed:
	PYTHONPATH=src $(PYTHON) scripts/generate_precomputed_review.py --check

test:
	$(PYTHON) -m pytest
