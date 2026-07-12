from __future__ import annotations

import importlib.util
import os
import shutil
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")
sync_api = pytest.importorskip("playwright.sync_api")

from fastapi.testclient import TestClient

from ops_evidence_synthesis.api import app
from ops_evidence_synthesis.local_first import build_bundle_from_sanitized, sanitize_input


ROOT = Path(__file__).resolve().parents[1]


def _code_profile_review_html() -> str:
    path = ROOT / "scripts" / "gcs_review_flow.py"
    spec = importlib.util.spec_from_file_location("gcs_review_flow_browser_test", path)
    assert spec is not None
    assert spec.loader is not None
    script = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(script)
    focused_profile = {
        "schema_version": "focused_operational_profile.v1",
        "system_label": "stream-runtime",
        "system_summary": {
            "system_type": "streaming_service",
            "primary_purpose": "Keep a public live stream available.",
        },
        "observability_contract": {
            "metrics": [
                {
                    "metric_name": "publish_gap_seconds",
                    "meaning": "Time since last successful publish.",
                    "healthy_direction": "decrease",
                }
            ]
        },
        "profile_limits": {
            "source_context_is_incident_evidence": False,
            "runtime_claims_require_evidence_id": True,
            "approval_required_before_explicit_profile": True,
            "raw_source_sent_to_provider": False,
            "raw_logs_sent_to_provider": False,
        },
        "human_review_required": ["Is zero publish gap healthy?"],
    }
    review_form = script._render_code_profile_review_form(
        run_id="browser-review-run",
        code_profile_id="browser-profile-id",
        code_profile_url="http://example.test/code-profiles/browser-profile-id/",
        focused_profile=focused_profile,
        interpretation={},
    )
    return script._render_code_profile_html(
        title="Code Profile Review",
        code_profile_url="http://example.test/code-profiles/browser-profile-id/",
        code_profile_report_url="http://example.test/code-profiles/browser-profile-id/report.md",
        markdown="# Gemini Pro Code Profile\n\n## Gemini Questions For Human Approval\n\nAnswer the question below.\n",
        review_form=review_form,
    )


def _redaction_fixture_bundle(tmp_path: Path) -> dict[str, object]:
    out = tmp_path / "browser_local_first"
    sanitize_input(ROOT / "sample_logs" / "redaction_fixture.jsonl", out)
    return build_bundle_from_sanitized(
        out / "sanitized_events.jsonl",
        service="unknown-sample",
        environment="prod",
        start="2026-06-16T00:00:00Z",
        end="2026-06-16T18:00:00Z",
        profile_name="generic",
        out_path=out / "evidence_bundle.json",
    )


def test_root_page_renders_in_real_browser(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OES_STORE", "sqlite")
    monkeypatch.setenv("OES_DB_PATH", str(tmp_path / "browser.sqlite3"))
    client = TestClient(app)
    assert client.head("/").status_code == 200
    html = client.get("/").text
    console_errors: list[str] = []

    with sync_api.sync_playwright() as playwright:
        executable_path = (
            os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE")
            or shutil.which("chromium")
            or shutil.which("chromium-browser")
            or shutil.which("google-chrome")
        )
        launch_kwargs = {"executable_path": executable_path} if executable_path else {}
        try:
            browser = playwright.chromium.launch(**launch_kwargs)
        except Exception as exc:
            pytest.skip(f"Chromium is not installed for Playwright: {exc}")
        page = browser.new_page(viewport={"width": 1366, "height": 900})
        page.on("console", lambda message: console_errors.append(message.text) if message.type == "error" else None)
        page.set_content(html, wait_until="domcontentloaded")

        assert page.locator("text=Upload Sanitized Evidence Bundle").is_visible()
        assert page.locator("#artifact-drop-zone").is_visible()
        assert page.locator("text=No Evidence Bundle selected").is_visible()
        assert page.locator("text=Raw logs, raw source files").is_visible()
        assert console_errors == []
        browser.close()


def test_write_token_retry_prompts_when_stored_token_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OES_STORE", "sqlite")
    monkeypatch.setenv("OES_DB_PATH", str(tmp_path / "browser-token.sqlite3"))
    html = TestClient(app).get("/").text
    requests_seen: list[str] = []

    with sync_api.sync_playwright() as playwright:
        executable_path = (
            os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE")
            or shutil.which("chromium")
            or shutil.which("chromium-browser")
            or shutil.which("google-chrome")
        )
        launch_kwargs = {"executable_path": executable_path} if executable_path else {}
        try:
            browser = playwright.chromium.launch(**launch_kwargs)
        except Exception as exc:
            pytest.skip(f"Chromium is not installed for Playwright: {exc}")
        page = browser.new_page(viewport={"width": 1024, "height": 768})
        page.route("http://example.test/", lambda route: route.fulfill(status=200, body=html, content_type="text/html"))

        def write_route(route: sync_api.Route) -> None:
            token = route.request.headers.get("x-oes-write-token", "")
            requests_seen.append(token)
            if token == "old-token":
                route.fulfill(status=403, json={"detail": "write token rejected"})
            else:
                route.fulfill(status=200, json={"ok": token == "new-token"})

        page.route("http://example.test/write-test", write_route)
        page.goto("http://example.test/", wait_until="domcontentloaded")
        page.evaluate("localStorage.setItem('oes.write_token', 'old-token')")
        page.evaluate(
            """() => {
              window.__writeStatus = "pending";
              fetch('/write-test', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: '{}'})
                .then((response) => { window.__writeStatus = response.status; })
                .catch(() => { window.__writeStatus = "failed"; });
            }"""
        )

        page.wait_for_selector("#write-token-dialog")
        assert "rejected" in page.locator("#write-token-message").inner_text()
        page.locator("#write-token-input").fill("new-token")
        page.locator("#write-token-dialog button[type='submit']").click()
        page.wait_for_function("window.__writeStatus === 200")

        assert requests_seen == ["old-token", "new-token"]
        assert page.evaluate("localStorage.getItem('oes.write_token')") == "new-token"
        browser.close()


def test_write_token_prompt_appears_before_first_write_without_stored_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OES_STORE", "sqlite")
    monkeypatch.setenv("OES_DB_PATH", str(tmp_path / "browser-token-first.sqlite3"))
    html = TestClient(app).get("/").text
    requests_seen: list[str] = []

    with sync_api.sync_playwright() as playwright:
        executable_path = (
            os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE")
            or shutil.which("chromium")
            or shutil.which("chromium-browser")
            or shutil.which("google-chrome")
        )
        launch_kwargs = {"executable_path": executable_path} if executable_path else {}
        try:
            browser = playwright.chromium.launch(**launch_kwargs)
        except Exception as exc:
            pytest.skip(f"Chromium is not installed for Playwright: {exc}")
        page = browser.new_page(viewport={"width": 1024, "height": 768})
        page.route("http://example.test/", lambda route: route.fulfill(status=200, body=html, content_type="text/html"))

        def write_route(route: sync_api.Route) -> None:
            requests_seen.append(route.request.headers.get("x-oes-write-token", ""))
            route.fulfill(status=200, json={"ok": True})

        page.route("http://example.test/write-test", write_route)
        page.goto("http://example.test/", wait_until="domcontentloaded")
        page.evaluate("localStorage.removeItem('oes.write_token')")
        page.evaluate(
            """() => {
              window.__writeStatus = "pending";
              fetch('/write-test', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: '{}'})
                .then((response) => { window.__writeStatus = response.status; })
                .catch(() => { window.__writeStatus = "failed"; });
            }"""
        )

        page.wait_for_selector("#write-token-dialog")
        assert "continue" in page.locator("#write-token-message").inner_text()
        assert requests_seen == []
        page.locator("#write-token-input").fill("new-token")
        page.locator("#write-token-dialog button[type='submit']").click()
        page.wait_for_function("window.__writeStatus === 200")

        assert requests_seen == ["new-token"]
        assert page.evaluate("localStorage.getItem('oes.write_token')") == "new-token"
        browser.close()


def test_generate_refined_plan_requires_inline_write_token_before_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OES_STORE", "sqlite")
    monkeypatch.setenv("OES_DB_PATH", str(tmp_path / "browser-planner-token.sqlite3"))
    bundle = _redaction_fixture_bundle(tmp_path)
    client = TestClient(app)
    uploaded = client.post("/bundles/upload", json={"bundle": bundle})
    assert uploaded.status_code == 200, uploaded.text
    html = client.get(f"/?evidence_sha256={bundle['evidence_sha256']}&full=1").text
    requests_seen: list[str] = []

    with sync_api.sync_playwright() as playwright:
        executable_path = (
            os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE")
            or shutil.which("chromium")
            or shutil.which("chromium-browser")
            or shutil.which("google-chrome")
        )
        launch_kwargs = {"executable_path": executable_path} if executable_path else {}
        try:
            browser = playwright.chromium.launch(**launch_kwargs)
        except Exception as exc:
            pytest.skip(f"Chromium is not installed for Playwright: {exc}")
        page = browser.new_page(viewport={"width": 1366, "height": 900})
        page.route("http://example.test/", lambda route: route.fulfill(status=200, body=html, content_type="text/html"))

        def planner_route(route: sync_api.Route) -> None:
            requests_seen.append(route.request.headers.get("x-oes-write-token", ""))
            route.fulfill(
                status=200,
                json={
                    "plan": {"schema_version": "evidence_request_plan.v1", "plan_id": "PLAN-TEST"},
                    "collection_instructions_markdown": "Updated collection notes",
                },
            )

        page.route("http://example.test/evidence-requests/plan", planner_route)
        page.goto("http://example.test/", wait_until="domcontentloaded")
        page.evaluate("localStorage.removeItem('oes.write_token')")
        page.locator("#evidence-request-planner > details > summary").click()

        assert requests_seen == []
        assert page.locator("#planner-refine-button").is_disabled()
        assert "Enter write token" in page.locator("#planner-write-token-status").inner_text()
        assert "After generation, output appears" in page.locator("#planner-result-message").inner_text()
        assert "Not generated in this browser yet." in page.locator("#planner-output-stamp").inner_text()

        page.locator("#planner-write-token-input").fill("planner-token")
        page.wait_for_function("document.getElementById('planner-refine-button')?.disabled === false")
        page.locator("#planner-refine-button").click()
        page.wait_for_function("document.getElementById('planner-refine-progress-step')?.textContent === 'Complete'")

        assert requests_seen == ["planner-token"]
        assert "Updated collection notes" in page.locator("#planner-collection-markdown").inner_text()
        assert page.locator("#planner-collection-markdown").get_attribute("data-output-changed") == "true"
        assert "Collection Instructions changed below" in page.locator("#planner-refine-status").inner_text()
        assert "Collection Instructions changed" in page.locator("#planner-result-message").inner_text()
        assert "Updated at" in page.locator("#planner-output-stamp").inner_text()

        page.locator("#planner-refine-button").click()
        page.wait_for_function("document.getElementById('planner-refine-status')?.textContent.includes('already current')")

        assert requests_seen == ["planner-token", "planner-token"]
        assert page.locator("#planner-collection-markdown").get_attribute("data-output-changed") == "false"
        assert "No Collection Instructions text changed" in page.locator("#planner-result-message").inner_text()
        assert "Generated with no text changes" in page.locator("#planner-output-stamp").inner_text()
        browser.close()


def test_planner_panel_stays_left_of_sticky_drawer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OES_STORE", "sqlite")
    monkeypatch.setenv("OES_DB_PATH", str(tmp_path / "browser-layout.sqlite3"))
    bundle = _redaction_fixture_bundle(tmp_path)
    client = TestClient(app)
    uploaded = client.post("/bundles/upload", json={"bundle": bundle})
    assert uploaded.status_code == 200, uploaded.text
    html = client.get(f"/?evidence_sha256={bundle['evidence_sha256']}&full=1").text

    with sync_api.sync_playwright() as playwright:
        executable_path = (
            os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE")
            or shutil.which("chromium")
            or shutil.which("chromium-browser")
            or shutil.which("google-chrome")
        )
        launch_kwargs = {"executable_path": executable_path} if executable_path else {}
        try:
            browser = playwright.chromium.launch(**launch_kwargs)
        except Exception as exc:
            pytest.skip(f"Chromium is not installed for Playwright: {exc}")
        page = browser.new_page(viewport={"width": 1366, "height": 900})
        page.set_content(html, wait_until="domcontentloaded")
        page.locator("#evidence-request-planner > details > summary").click()

        layout = page.evaluate(
            """() => {
              const rect = (selector) => {
                const node = document.querySelector(selector);
                const box = node.getBoundingClientRect();
                return {
                  left: box.left,
                  right: box.right,
                  width: box.width,
                  scrollWidth: node.scrollWidth,
                  clientWidth: node.clientWidth,
                };
              };
              return {
                planner: rect("#evidence-request-planner"),
                details: rect("#evidence-request-planner details"),
                drawer: rect(".drawer"),
              };
            }"""
        )

        assert layout["planner"]["right"] <= layout["drawer"]["left"]
        assert layout["details"]["scrollWidth"] <= layout["details"]["clientWidth"] + 1
        browser.close()


def test_code_profile_review_completes_normalize_preview_and_approval_in_browser() -> None:
    html = _code_profile_review_html()
    requests_seen: list[dict[str, object]] = []
    preview_count = 0

    with sync_api.sync_playwright() as playwright:
        executable_path = (
            os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE")
            or shutil.which("chromium")
            or shutil.which("chromium-browser")
            or shutil.which("google-chrome")
        )
        launch_kwargs = {"executable_path": executable_path} if executable_path else {}
        try:
            browser = playwright.chromium.launch(**launch_kwargs)
        except Exception as exc:
            pytest.skip(f"Chromium is not installed for Playwright: {exc}")
        page = browser.new_page(viewport={"width": 1366, "height": 1000})
        page.route(
            "http://example.test/code-profiles/browser-profile-id/",
            lambda route: route.fulfill(status=200, body=html, content_type="text/html"),
        )

        def normalize_route(route: sync_api.Route) -> None:
            payload = route.request.post_data_json
            requests_seen.append(
                {
                    "step": "normalize",
                    "token": route.request.headers.get("x-oes-write-token", ""),
                    "payload": payload,
                }
            )
            route.fulfill(
                status=200,
                json={
                    "status": "candidate_patch_ready",
                    "patch": {
                        "schema_version": "operational_profile_review_patch.v1",
                        "system_summary_overrides": {},
                        "metric_semantics_overrides": [
                            {
                                "metric_name": "publish_gap_seconds",
                                "meaning": "Seconds since the last successful publication.",
                                "healthy_direction": "decrease",
                                "zero_behavior": "healthy",
                                "increase_behavior": "suspicious",
                                "decrease_behavior": "healthy",
                                "reason": "Human-confirmed semantics.",
                                "provenance": "human_answer",
                            }
                        ],
                        "component_role_overrides": [],
                        "log_source_overrides": [],
                        "confirmed_user_outcomes": [],
                        "ignored_component_ids": [],
                        "approved_collectors": [],
                        "unresolved_questions": [],
                    },
                    "normalization": {"provider_id": "gemini-enterprise-agent-platform"},
                    "change_summary": {"metric_semantics": 1},
                },
            )

        def preview_route(route: sync_api.Route) -> None:
            nonlocal preview_count
            preview_count += 1
            payload = route.request.post_data_json
            requests_seen.append(
                {
                    "step": "preview",
                    "token": route.request.headers.get("x-oes-write-token", ""),
                    "payload": payload,
                }
            )
            route.fulfill(
                status=200,
                json={
                    "status": "ready_for_human_re_review",
                    "reviewed_patch_sha256": str(preview_count) * 64,
                    "answer_count": 1,
                    "unresolved_question_count": 0,
                    "change_summary": {"metric_semantics": 1},
                    "interpreted_profile": {
                        "status": "candidate_interpretation",
                        "system_profile": {"purpose": "Keep a public live stream available."},
                        "metric_semantics": {
                            "publish_gap_seconds": {
                                "zero_behavior": "healthy",
                                "increase_behavior": "suspicious",
                            }
                        },
                    },
                },
            )

        def approve_route(route: sync_api.Route) -> None:
            payload = route.request.post_data_json
            requests_seen.append(
                {
                    "step": "approve",
                    "token": route.request.headers.get("x-oes-write-token", ""),
                    "payload": payload,
                }
            )
            route.fulfill(
                status=200,
                json={
                    "status": "approved",
                    "approved_profile_sha256": "a" * 64,
                    "approved_profile": {
                        "schema_version": "approved_operational_profile.v1",
                        "status": "approved",
                        "review_policy": {"source_access_after_approval": "disabled"},
                    },
                },
            )

        page.route("http://example.test/profile-reviews/normalize", normalize_route)
        page.route("http://example.test/profile-reviews/preview", preview_route)
        page.route("http://example.test/profile-reviews/approve", approve_route)
        page.goto(
            "http://example.test/code-profiles/browser-profile-id/",
            wait_until="domcontentloaded",
        )

        page.locator("#review-question-1").fill(
            "Zero is healthy. Increasing values indicate a publication gap."
        )
        page.locator("#profile-matches-deployment").check()
        page.locator("#deployment-period-confirmed").check()
        page.locator("#log-scope-confirmed").check()
        page.locator("#reviewer").fill("operator-1")
        page.locator("#decision").select_option("approved")
        page.locator("#approval-note").fill("Confirmed against the deployed runtime.")
        page.locator("#profile-review-write-token").fill("browser-write-token")

        page.locator("#approve-profile-review").click()
        assert "Normalize with Gemini first" in page.locator("#review-form-status").inner_text()
        assert requests_seen == []

        page.locator("#normalize-profile-review").click()
        page.wait_for_function(
            "document.getElementById('review-form-status')?.textContent.includes('Candidate patch ready')"
        )
        assert page.locator("#preview-profile-review").is_enabled()
        assert '"zero_behavior": "healthy"' in page.locator("#profile-patch-output").input_value()

        normalize_request = requests_seen[0]
        assert normalize_request["step"] == "normalize"
        assert normalize_request["token"] == "browser-write-token"
        normalize_payload = normalize_request["payload"]
        assert isinstance(normalize_payload, dict)
        assert normalize_payload["human_review"]["answers"][0]["answer"].startswith("Zero is healthy")

        page.locator("#preview-profile-review").click()
        page.wait_for_function(
            "document.getElementById('review-form-status')?.textContent.includes('interpretation rebuilt')"
        )
        assert page.locator("#interpretation-review-confirmed").is_enabled()
        assert '"candidate_interpretation"' in page.locator("#interpreted-profile-preview").input_value()

        page.locator("#approve-profile-review").click()
        assert "Step 2 is required" in page.locator("#review-form-status").inner_text()
        assert [request["step"] for request in requests_seen] == ["normalize", "preview"]

        edited_patch = page.locator("#profile-patch-output").input_value().replace(
            "Human-confirmed semantics.",
            "Operator-confirmed semantics.",
        )
        page.locator("#profile-patch-output").fill(edited_patch)
        page.locator("#approve-profile-review").click()
        assert "Step 1 is required" in page.locator("#review-form-status").inner_text()
        assert [request["step"] for request in requests_seen] == ["normalize", "preview"]

        page.locator("#preview-profile-review").click()
        page.wait_for_function(
            "document.getElementById('review-form-status')?.textContent.includes('interpretation rebuilt')"
        )
        page.locator("#interpretation-review-confirmed").check()
        page.locator("#approve-profile-review").click()
        page.wait_for_function(
            "document.getElementById('review-form-status')?.textContent.includes('Approved profile frozen')"
        )

        assert [request["step"] for request in requests_seen] == [
            "normalize",
            "preview",
            "preview",
            "approve",
        ]
        final_request = requests_seen[-1]
        assert final_request["token"] == "browser-write-token"
        final_payload = final_request["payload"]
        assert isinstance(final_payload, dict)
        assert final_payload["interpretation_review_confirmed"] is True
        assert final_payload["reviewed_patch_sha256"] == "2" * 64
        assert (
            final_payload["accepted_patch"]["metric_semantics_overrides"][0]["reason"]
            == "Operator-confirmed semantics."
        )
        assert page.locator("#download-approved-profile").is_enabled()
        assert page.locator("#copy-approve-command").is_enabled()
        assert '"source_access_after_approval": "disabled"' in page.locator(
            "#approved-profile-output"
        ).input_value()
        stored_values = page.evaluate("Object.values(localStorage)")
        assert all("browser-write-token" not in value for value in stored_values)
        browser.close()
