from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_deploy_public_demo_preserves_fast_review_runtime_contract() -> None:
    script = (ROOT / "scripts" / "deploy_public_demo.sh").read_text(encoding="utf-8")

    assert 'FAST_GCP_REVIEW_SAMPLE_ROWS="${FAST_GCP_REVIEW_SAMPLE_ROWS:-2000}"' in script
    assert 'PUBLIC_FAST_GCP_REVIEW_CACHE_SECONDS="${PUBLIC_FAST_GCP_REVIEW_CACHE_SECONDS:-3600}"' in script
    assert 'PUBLIC_FAST_GCP_REVIEW_DAILY_LIMIT="${PUBLIC_FAST_GCP_REVIEW_DAILY_LIMIT:-12}"' in script
    assert 'PUBLIC_FAST_GCP_REVIEW_CLIENT_DAILY_LIMIT="${PUBLIC_FAST_GCP_REVIEW_CLIENT_DAILY_LIMIT:-2}"' in script
    assert 'PUBLIC_FAST_GCP_REVIEW_MAX_INSTANCES="${PUBLIC_FAST_GCP_REVIEW_MAX_INSTANCES:-1}"' in script
    assert 'PUBLIC_FAST_GCP_REVIEW_CONCURRENCY="${PUBLIC_FAST_GCP_REVIEW_CONCURRENCY:-5}"' in script
    assert "OES_FAST_GCP_GEMINI_MODEL=gemini-3.1-flash-lite" in script
    assert "OES_FAST_GCP_REVIEW_SAMPLE_ROWS=${FAST_GCP_REVIEW_SAMPLE_ROWS}" in script
    assert "OES_PUBLIC_FAST_GCP_REVIEW_CACHE_SECONDS=${PUBLIC_FAST_GCP_REVIEW_CACHE_SECONDS}" in script
    assert "OES_PUBLIC_FAST_GCP_REVIEW_DAILY_LIMIT=${PUBLIC_FAST_GCP_REVIEW_DAILY_LIMIT}" in script
    assert "OES_PUBLIC_FAST_GCP_REVIEW_CLIENT_DAILY_LIMIT=${PUBLIC_FAST_GCP_REVIEW_CLIENT_DAILY_LIMIT}" in script


def test_deploy_public_demo_keeps_ci_secret_scan_digest_and_smoke_gates() -> None:
    script = (ROOT / "scripts" / "deploy_public_demo.sh").read_text(encoding="utf-8")

    assert 'make PYTHON="${PYTHON_BIN}" ci' in script
    assert "gitleaks detect --source . --no-banner" in script
    assert "gcloud artifacts docker images describe" in script
    assert 'if [[ -z "${DIGEST_IMAGE_URI}" ]]; then' in script
    assert 'if [[ "${DEPLOYED_IMAGE}" != "${DIGEST_IMAGE_URI}" ]]; then' in script
    assert 'if [[ "${TRAFFIC_REVISION}" != "${READY_REVISION}" || "${TRAFFIC_PERCENT}" != "100" ]]; then' in script
    assert 'make PYTHON="${PYTHON_BIN}" PUBLIC_BASE_URL="${PUBLIC_BASE_URL}" smoke-public' in script


def test_generate_precomputed_review_script_records_public_review_safety_terms() -> None:
    script = (ROOT / "scripts" / "generate_precomputed_review_from_multi_run.py").read_text(encoding="utf-8")

    assert "Excluded from convergence denominator." in script
    assert "Provider convergence can create high-priority validation work" in script
    assert "requires enough runtime evidence" in script
    assert "incident promotion is not auto-accepted" in script
    assert "Incident gate signal is a graph-level support signal" in script
    assert "Convergence score = claimed successful providers / all successful providers" in script
