#!/usr/bin/env python3
"""Capture the public demo screens used by the hackathon video.

The script never starts a model run. It only opens public read-only pages and
captures deterministic viewport screenshots at the sections named below.
"""

from __future__ import annotations

from pathlib import Path

from playwright.sync_api import Browser, Page, sync_playwright


ROOT = Path(__file__).resolve().parents[1]
SCREENSHOTS = ROOT / "assets" / "screenshots"
SLIDES = ROOT / "assets" / "slides"
OVERLAYS = ROOT / "assets" / "overlays"
CHROMIUM = "/snap/bin/chromium"
VIEWPORT = {"width": 1920, "height": 1080}

RUNTIME_PROFILE = (
    "https://ops-evidence.yukimurata0421.dev/code-profiles/"
    "31dd5326f0e9e052697975e7174d9de6ebf7c2fde58625cb96ce41f29faab621/"
)
RUNTIME_REVIEW = (
    "https://ops-evidence.yukimurata0421.dev/ui/full-review-page?evidence_sha256="
    "ab18d62c4e628e190345fa218834ca74276f556191d2f068a969f7922945a471"
)
FAST_REVIEW = "https://ops-evidence.yukimurata0421.dev/ui/fast-gcp-review"
VERIFIED_FAST_REVIEW = (
    "https://ops-evidence.yukimurata0421.dev/ui/full-review-page?evidence_sha256="
    "2641cb5fe5850d006864dec4aad3b3d2539e9efcef3753b43d5624f8b6e5136b"
)
RESCORE = (
    "https://ops-evidence.yukimurata0421.dev/ui/rescore-demo"
    "?id=amazon-notify-more-data-rescore"
)


def prepare(page: Page, url: str) -> None:
    page.goto(url, wait_until="networkidle", timeout=60_000)
    page.add_style_tag(
        content="""
        *, *::before, *::after {
          animation: none !important;
          caret-color: transparent !important;
          scroll-behavior: auto !important;
          transition: none !important;
        }
        """
    )


def capture_top(page: Page, filename: str, url: str) -> None:
    prepare(page, url)
    page.evaluate("window.scrollTo(0, 0)")
    page.screenshot(path=str(SCREENSHOTS / filename), full_page=False)


def capture_at_text(
    page: Page,
    filename: str,
    url: str,
    text: str,
    *,
    offset: int = -150,
) -> None:
    prepare(page, url)
    locator = page.get_by_text(text, exact=False).first
    locator.wait_for(state="visible", timeout=30_000)
    locator.scroll_into_view_if_needed()
    page.evaluate("offset => window.scrollBy(0, offset)", offset)
    page.screenshot(path=str(SCREENSHOTS / filename), full_page=False)


def capture_at_heading(
    page: Page,
    filename: str,
    url: str,
    text: str,
    *,
    offset: int = -150,
) -> None:
    prepare(page, url)
    locator = page.get_by_role("heading", name=text, exact=True).last
    locator.wait_for(state="visible", timeout=30_000)
    locator.scroll_into_view_if_needed()
    page.evaluate("offset => window.scrollBy(0, offset)", offset)
    page.wait_for_timeout(300)
    page.screenshot(path=str(SCREENSHOTS / filename), full_page=False)


def capture_rescore(page: Page) -> None:
    prepare(page, RESCORE)
    console = page.get_by_text("Rescore console", exact=False).first
    console.scroll_into_view_if_needed()
    page.evaluate("window.scrollBy(0, -130)")
    page.screenshot(path=str(SCREENSHOTS / "18-rescore-before.png"), full_page=False)
    page.get_by_role("button", name="After more data").click()
    page.locator('[data-phase-view="after"]').wait_for(state="visible", timeout=10_000)
    page.wait_for_timeout(1_000)
    page.evaluate("window.scrollBy(0, 1); window.scrollBy(0, -1)")
    page.screenshot(path=str(SCREENSHOTS / "19-rescore-after.png"), full_page=False)


def capture_runtime_sections(browser: Browser) -> None:
    """Capture the live review sections at 125% equivalent browser scaling."""
    context = browser.new_context(
        viewport={"width": 1536, "height": 864},
        device_scale_factor=1.25,
    )
    page = context.new_page()
    prepare(page, RUNTIME_REVIEW)
    trace_heading = page.get_by_role(
        "heading", name="A guarded autonomous investigation loop.", exact=True
    ).last
    trace_heading.wait_for(state="visible", timeout=30_000)
    trace_heading.evaluate(
        "element => window.scrollTo(0, element.getBoundingClientRect().top + window.scrollY - 86)"
    )
    page.wait_for_timeout(1_000)
    page.screenshot(path=str(SCREENSHOTS / "11-runtime-agent-trace.png"), full_page=False)

    prepare(page, RUNTIME_REVIEW)
    target_heading = page.get_by_role(
        "heading", name="Every target carries its own evidence and gate.", exact=True
    ).last
    target_heading.wait_for(state="visible", timeout=30_000)
    target_heading.evaluate(
        "element => window.scrollTo(0, element.getBoundingClientRect().top + window.scrollY - 86)"
    )
    page.get_by_text("youtube_health", exact=True).first.click()
    page.wait_for_timeout(1_000)
    page.screenshot(path=str(SCREENSHOTS / "12-runtime-target.png"), full_page=False)
    context.close()


def capture_code_profile_system_reading(browser: Browser) -> None:
    """Capture the actual public page at 160% equivalent browser scaling."""
    context = browser.new_context(
        viewport={"width": 1200, "height": 675},
        device_scale_factor=1.6,
    )
    page = context.new_page()
    prepare(page, RUNTIME_PROFILE)
    heading = page.get_by_role("heading", name="Gemini System Reading", exact=True).last
    heading.wait_for(state="visible", timeout=30_000)
    heading.evaluate(
        "element => window.scrollTo(0, element.getBoundingClientRect().top + window.scrollY - 55)"
    )
    page.evaluate("window.scrollBy(0, 1); window.scrollBy(0, -1)")
    page.wait_for_timeout(1_000)
    page.screenshot(
        path=str(SCREENSHOTS / "14-code-profile-system-reading.png"),
        full_page=False,
    )
    context.close()


def capture_code_profile_questions(browser: Browser) -> None:
    """Stage representative human answers in-browser without saving or API calls."""
    context = browser.new_context(
        viewport={"width": 1200, "height": 675},
        device_scale_factor=1.6,
    )
    page = context.new_page()
    prepare(page, RUNTIME_PROFILE)
    answers = [
        "Keep 24/7 YouTube Live delivery fresh: ADS-B visuals must update and program audio must remain audible.",
        "adsb-streamnew-youtube-stream.service -> FFmpeg -> YouTube Live is the main delivery path; watchdog and recovery services support it.",
        "A controlled transient restart is acceptable only when delivery recovers quickly. Repeated restarts or any viewer-visible interruption are harmful.",
        "stream_engine_ffmpeg_restart_count and watchdog_timeline_anomaly_count are zero-is-good. Freshness age and publish gaps become suspicious as they increase.",
        "Viewer-impact evidence includes stale video, silent audio, failed publish, or an unavailable stream. Lifecycle-only messages are diagnostic context.",
        "The media runner, recovery controller, watchdog, collector, and worker roles match this deployment.",
        "Make restart count, anomaly count, freshness age, and publish-gap direction explicit; do not infer incident impact from restart activity alone.",
        "Approve only for the matching deployment window, after preserving the human-gated user-impact requirement.",
    ]
    questions = page.locator("[data-review-question]")
    for index, answer in enumerate(answers):
        questions.nth(index).fill(answer)
    page.locator("#reviewer").fill("Demo SRE reviewer")
    page.locator("#decision").select_option("approved")
    page.locator("#approval-note").fill(
        "Approved for this stream_v3 deployment window; user-impact evidence remains required."
    )
    for checkbox_id in (
        "#profile-matches-deployment",
        "#deployment-period-confirmed",
        "#log-scope-confirmed",
    ):
        page.locator(checkbox_id).check()
    heading = page.get_by_role(
        "heading", name="Gemini Questions For Human Approval", exact=True
    ).last
    heading.evaluate(
        "element => window.scrollTo(0, element.getBoundingClientRect().top + window.scrollY - 45)"
    )
    page.evaluate("window.scrollBy(0, 1); window.scrollBy(0, -1)")
    page.wait_for_timeout(1_000)
    page.screenshot(
        path=str(SCREENSHOTS / "15-code-profile-human-questions.png"),
        full_page=False,
    )
    context.close()


def capture_verified_fast_review(browser: Browser) -> None:
    """Capture the verified result at 125% equivalent browser scaling."""
    context = browser.new_context(
        viewport={"width": 1536, "height": 864},
        device_scale_factor=1.25,
    )
    page = context.new_page()
    prepare(page, VERIFIED_FAST_REVIEW)
    heading = page.locator("h1").first
    heading.wait_for(state="visible", timeout=30_000)
    heading.evaluate(
        "element => window.scrollTo(0, element.getBoundingClientRect().top + window.scrollY - 82)"
    )
    page.evaluate("window.scrollBy(0, 1); window.scrollBy(0, -1)")
    page.wait_for_timeout(1_000)
    page.screenshot(
        path=str(SCREENSHOTS / "17-verified-fast-review.png"),
        full_page=False,
    )
    context.close()


def render_slides(page: Page) -> None:
    for slide in sorted(SLIDES.glob("*.svg")):
        page.goto(slide.as_uri(), wait_until="load", timeout=30_000)
        page.screenshot(
            path=str(SCREENSHOTS / f"{slide.stem}.png"),
            full_page=False,
        )


def render_overlays(page: Page) -> None:
    page.set_viewport_size({"width": 1920, "height": 320})
    for overlay in sorted(OVERLAYS.glob("*.svg")):
        page.goto(overlay.as_uri(), wait_until="load", timeout=30_000)
        page.screenshot(
            path=str(SCREENSHOTS / f"{overlay.stem}.png"),
            full_page=False,
            omit_background=True,
        )
    page.set_viewport_size(VIEWPORT)


def main() -> None:
    SCREENSHOTS.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            executable_path=CHROMIUM,
            headless=True,
            args=["--no-sandbox"],
        )
        page = browser.new_page(viewport=VIEWPORT, device_scale_factor=1)

        render_slides(page)
        render_overlays(page)
        capture_top(page, "10-runtime-review-hero.png", RUNTIME_REVIEW)
        capture_runtime_sections(browser)
        capture_top(page, "13-code-profile-top.png", RUNTIME_PROFILE)
        capture_code_profile_system_reading(browser)
        capture_code_profile_questions(browser)
        capture_top(page, "16-fast-gcp-review.png", FAST_REVIEW)
        capture_verified_fast_review(browser)
        capture_rescore(page)
        browser.close()

    print(f"captured={len(list(SCREENSHOTS.glob('*.png')))} directory={SCREENSHOTS}")


if __name__ == "__main__":
    main()
