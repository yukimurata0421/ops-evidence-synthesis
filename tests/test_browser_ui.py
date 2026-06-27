from __future__ import annotations

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


def _secret_heavy_bundle(tmp_path: Path) -> dict[str, object]:
    out = tmp_path / "browser_local_first"
    sanitize_input(ROOT / "sample_logs" / "secret_heavy.jsonl", out)
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
    bundle = _secret_heavy_bundle(tmp_path)
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
    bundle = _secret_heavy_bundle(tmp_path)
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
