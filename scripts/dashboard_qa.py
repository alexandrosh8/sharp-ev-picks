"""Playwright sweep for the sharp-ev-picks dashboard.

The live sweep performs read-only GETs. A second isolated-origin suite serves
that same dashboard HTML with mocked API responses and exercises qualification,
deep-link/modal behavior, stale-cache retention, CSV safety, and score entry.
It runs inside the Playwright container via ``scripts/dashboard_qa.sh`` or from
the project venv after Chromium is installed.

Environment:
    DASHQA_BASE_URL   live app URL (default http://app:8000)
    DASHQA_OUT        artifact directory (default /out)
    DASHQA_HTML       optional local dashboard HTML for mock-only runs
    DASHQA_MOCK_ONLY  set to 1 to skip the live sweep
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime
import gzip
import json
import os
import sys
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from playwright.async_api import Browser, Page, Route, async_playwright

TABS = ["today", "edges", "radar", "lab", "sources"]
BASE = os.environ.get("DASHQA_BASE_URL", "http://app:8000").rstrip("/")
OUT = Path(os.environ.get("DASHQA_OUT", "/out"))
MOCK_ONLY = os.environ.get("DASHQA_MOCK_ONLY") == "1"
MOCK_ORIGIN = "http://sharp-dashboard.test"


def _iso(offset_minutes: int = 0) -> str:
    now = datetime.datetime.now(datetime.UTC) + datetime.timedelta(minutes=offset_minutes)
    return now.isoformat().replace("+00:00", "Z")


def _pick(index: int) -> dict[str, Any]:
    valid = index <= 7
    starts_at = _iso(180 + index)
    revalidated_at = _iso(-1)
    structural_sane: bool | None = True
    if index == 8:
        starts_at = _iso(-60)
    elif index == 9:
        revalidated_at = _iso(60)
    elif index == 10:
        structural_sane = None
    pick: dict[str, Any] = {
        "id": index,
        "event_id": 1000 + index,
        "event": "=2+2 vs Safe" if index == 1 else f"Mock Home {index} vs Mock Away {index}",
        "league": "Mock League",
        "country": "QA",
        "sport": "soccer",
        "sport_label": "Football",
        "market": "h2h",
        "selection": f"Mock Home {index}",
        "decimal_odds": "2.10",
        "edge": "0.08",
        "current_edge": "0.08",
        "edge_floor": "0.03",
        "ev": "0.10",
        "status": "alerted",
        "tier": "premium",
        "starts_at": starts_at,
        "revalidated_at": revalidated_at,
        "anchor_type": "sharp",
        "anchor_match_confidence": "0.99",
        "anchor_match_method": "canonical",
        "scraped_score": "2-1" if index == 8 else None,
    }
    if structural_sane is not None:
        pick["structural_sane"] = structural_sane
    assert valid or index in {8, 9, 10}
    return pick


def _health(mode: str) -> tuple[int, dict[str, Any]]:
    body: dict[str, Any] = {
        "status": "ok",
        "mode": "picks-only",
        "polls": {"soccer": {"finished_at": _iso(-1), "per_market": {"h2h": 10}}},
        "newest_poll_age_seconds": 30,
        "poll_interval_seconds": 60,
        "max_odds_age_seconds": 2700,
        "poll_max_age_seconds": 180,
        "value_min_edge": 0.03,
        "value_volume_min_edge": 0.01,
        "odds_source": "mock",
        "proxy_pool": None,
    }
    if mode == "cold":
        body["polls"] = {}
        body["newest_poll_age_seconds"] = None
    elif mode == "mismatch":
        body["status"] = "degraded"  # Deliberate HTTP/body disagreement.
    return 200, body


async def _read_dashboard_source(api: Any) -> str:
    configured = os.environ.get("DASHQA_HTML")
    if configured:
        return Path(configured).read_text()
    if not str(__file__).startswith("<"):
        local = Path(__file__).resolve().parents[1] / "app" / "api" / "dashboard.html"
        if local.is_file():
            return local.read_text()
    response = await api.get(f"{BASE}/", timeout=15000)
    return await response.text()


async def _live_sweep(
    browser: Browser,
    report: list[str],
    hard_failures: list[str],
    console_errors: list[str],
    page_errors: list[str],
    request_failures: list[str],
) -> None:
    page = await browser.new_page(viewport={"width": 1440, "height": 900})
    page.on("console", lambda m: console_errors.append(m.text) if m.type == "error" else None)
    page.on("pageerror", lambda e: page_errors.append(f"{e}"))
    page.on(
        "requestfailed", lambda request: request_failures.append(f"{request.url} {request.failure}")
    )
    await page.goto(f"{BASE}/", wait_until="networkidle", timeout=60000)
    await page.wait_for_timeout(4000)

    for tab in TABS:
        try:
            await page.locator(f'[data-testid="rail-nav-{tab}"]').first.click(timeout=6000)
            await page.wait_for_timeout(1800)
            panel = page.locator(f"#view-{tab}")
            await panel.wait_for(state="visible", timeout=6000)
            panel_text = (await panel.inner_text()).strip()
            await page.screenshot(path=str(OUT / f"tab_{tab}.png"))
            if not panel_text:
                hard_failures.append(f"tab {tab}: rendered but root panel is EMPTY")
                report.append(f"tab {tab}: FAIL empty root panel #view-{tab}")
            else:
                report.append(f"tab {tab}: OK (panel text {len(panel_text)} chars)")
        except Exception as exc:
            hard_failures.append(f"tab {tab}: {type(exc).__name__}: {exc}")
            report.append(f"tab {tab}: FAIL {type(exc).__name__}: {exc}")
            with contextlib.suppress(Exception):
                await page.screenshot(path=str(OUT / f"tab_{tab}_fail.png"))

    await page.set_viewport_size({"width": 390, "height": 844})
    with contextlib.suppress(Exception):
        await page.locator(
            '[data-testid="dock-nav-today"], [data-testid="rail-nav-today"]'
        ).first.click(timeout=6000)
    await page.wait_for_timeout(1500)
    await page.screenshot(path=str(OUT / "tab_today_mobile.png"))
    overflow = await page.evaluate(
        "Math.max(document.body.scrollWidth, document.documentElement.scrollWidth) "
        "> window.innerWidth"
    )
    report.append(f"mobile_overflow_390px: {'YES' if overflow else 'no'}")
    if overflow:
        hard_failures.append("mobile horizontal overflow at 390px")
    await page.close()


async def _mocked_regressions(
    browser: Browser,
    dashboard_html: str,
    report: list[str],
    hard_failures: list[str],
) -> None:
    state = {"premium_failure": False, "health_mode": "normal"}
    premium = [_pick(index) for index in range(1, 11)]
    performance = {
        "n_sharp_close": 0,
        "min_headline_n": 50,
        "sharp_status": "insufficient",
        "roi_status": "insufficient",
        "by_sport": {},
    }

    async def json_response(route: Route, payload: Any, status: int = 200) -> None:
        await route.fulfill(
            status=status,
            body=json.dumps(payload),
            headers={"content-type": "application/json; charset=utf-8"},
        )

    async def handler(route: Route) -> None:
        parsed = urlparse(route.request.url)
        path = parsed.path
        query = parse_qs(parsed.query)
        if path == "/":
            await route.fulfill(status=200, body=dashboard_html, content_type="text/html")
        elif path == "/picks":
            tier = query.get("tier", [""])[0]
            if tier == "premium" and state["premium_failure"]:
                await json_response(route, {"detail": "mock premium failure"}, status=500)
            else:
                await json_response(route, premium if tier == "premium" else [])
        elif path == "/games":
            await json_response(route, [])
        elif path == "/performance":
            await json_response(route, performance)
        elif path == "/health":
            status, payload = _health(str(state["health_mode"]))
            await json_response(route, payload, status=status)
        elif path.startswith("/events/") and path.endswith("/result"):
            await json_response(route, {"settled": 2, "skipped": 0})
        elif path == "/sw.js":
            await route.fulfill(status=200, body="", content_type="application/javascript")
        elif path == "/manifest.webmanifest":
            await json_response(route, {"name": "QA", "start_url": "/"})
        else:
            await route.fulfill(status=204, body="")

    context = await browser.new_context(
        viewport={"width": 1440, "height": 900},
        accept_downloads=True,
        service_workers="block",
    )
    await context.route(f"{MOCK_ORIGIN}/**", handler)
    page: Page = await context.new_page()
    page_errors: list[str] = []
    mock_console_errors: list[str] = []
    page.on("pageerror", lambda error: page_errors.append(str(error)))
    page.on(
        "console",
        lambda message: (
            mock_console_errors.append(message.text) if message.type == "error" else None
        ),
    )

    stage = "deep-link"
    try:
        # Cold navigation to a nested route must open the real pick after data arrives.
        await page.goto(f"{MOCK_ORIGIN}/#/edges/1", wait_until="networkidle", timeout=30000)
        await page.locator("#edge-detail.open").wait_for(state="visible", timeout=10000)
        assert await page.locator("#edge-detail").get_attribute("aria-modal") == "true"
        assert await page.locator("#edge-detail").get_attribute("aria-hidden") == "false"
        assert (
            await page.evaluate("document.activeElement && document.activeElement.id")
            == "edge-back"
        )

        stage = "qualified-kpi"
        # Seven valid rows qualify; the panel is capped at five but the KPI is not.
        assert await page.locator("#actionable-count").inner_text() == "7"
        assert await page.locator("#today-stats .stat").first.locator(".sv").inner_text() == "7"
        assert await page.locator("#actionable-now .kickoff-row").count() == 5

        stage = "modal-focus"
        # Focus cycles inside the modal and the Back action restores a stable target.
        await page.evaluate(
            """() => {
              const detail = document.querySelector('#edge-detail');
              const nodes = Array.from(detail.querySelectorAll(
                'a[href], button:not([disabled]), input:not([disabled]), '
                + 'select:not([disabled]), textarea:not([disabled]), summary, '
                + '[tabindex]:not([tabindex="-1"])'
              )).filter((el) => el.offsetParent !== null);
              nodes[nodes.length - 1].focus();
            }"""
        )
        await page.keyboard.press("Tab")
        focused_after_tab = await page.evaluate(
            "document.activeElement && "
            "(document.activeElement.id || document.activeElement.tagName)"
        )
        assert focused_after_tab == "edge-back", f"Tab ended on {focused_after_tab!r}"
        await page.locator("#edge-back").click()
        await page.wait_for_url(f"{MOCK_ORIGIN}/#/edges")
        await page.wait_for_function(
            "document.querySelector('#edge-detail').getAttribute('aria-hidden') === 'true'"
        )

        stage = "csv"
        # CSV output neutralizes a user-controlled leading '=' formula cell.
        async with page.expect_download(timeout=10000) as download_info:
            await page.locator("#eq-export").click()
        download = await download_info.value
        download_path = await download.path()
        assert download_path is not None
        csv_text = Path(download_path).read_text()
        assert "'=2+2 vs Safe" in csv_text

        stage = "tier-cache"
        # A tier refresh failure keeps cached rows visible but removes qualification.
        state["premium_failure"] = True
        await page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
        await page.locator("#offline-banner").wait_for(state="visible", timeout=10000)
        await page.wait_for_function(
            "document.querySelector('#actionable-count').textContent === '—'"
        )
        assert "showing the last loaded rows" in await page.locator("#offline-banner").inner_text()
        assert await page.locator("#edge-list .edge-row").count() == 10
        partial_failure_pill = await page.locator("#pill-text").inner_text()
        assert partial_failure_pill.startswith("Data refresh degraded ·")
        assert "Source Degraded" not in partial_failure_pill

        stage = "health-fail-closed"
        # Cold-start and HTTP/body-mismatched health are both non-actionable.
        state["premium_failure"] = False
        state["health_mode"] = "cold"
        await page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
        await page.wait_for_function(
            "document.querySelector('#offline-banner').textContent"
            ".includes('no completed poll cycle yet')"
        )
        assert await page.locator("#actionable-count").inner_text() == "—"
        assert (await page.locator("#pill-text").inner_text()).startswith("Health unverified ·")
        state["health_mode"] = "mismatch"
        await page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
        await page.wait_for_function(
            "document.querySelector('#offline-banner').textContent"
            ".includes('Could not verify health')"
        )
        assert await page.locator("#actionable-count").inner_text() == "—"
        assert (await page.locator("#pill-text").inner_text()).startswith("Health unknown ·")

        stage = "score-form"
        # Closed-pick score entry is prefilled and native Enter submits the form.
        state["health_mode"] = "normal"
        await page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
        await page.wait_for_function(
            "document.querySelector('#pill-text').textContent.startsWith('Verified ·')"
        )
        await page.evaluate("location.hash = '#/edges/8'")
        await page.locator(".result-form").wait_for(state="visible", timeout=10000)
        assert await page.locator('[id^="result-home-"]').input_value() == "2"
        assert await page.locator('[id^="result-away-"]').input_value() == "1"

        stage = "closed-dirty-score-form-refresh"
        # Abandoning a dirty score form must not pause polling after the drawer closes.
        await page.locator('[id^="result-away-"]').fill("4")
        assert await page.locator(".result-form").get_attribute("data-dirty") == "true"
        await page.locator("#edge-back").click()
        await page.wait_for_url(f"{MOCK_ORIGIN}/#/edges")
        await page.wait_for_function(
            "document.querySelector('#edge-detail').getAttribute('aria-hidden') === 'true'"
        )
        async with page.expect_request(
            lambda request: (
                urlparse(request.url).path == "/picks"
                and parse_qs(urlparse(request.url).query).get("tier") == ["premium"]
            ),
            timeout=10000,
        ):
            await page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
        await page.wait_for_load_state("networkidle")

        stage = "score-form-submit"
        await page.evaluate("location.hash = '#/edges/8'")
        await page.locator(".result-form").wait_for(state="visible", timeout=10000)
        assert await page.locator('[id^="result-home-"]').input_value() == "2"
        assert await page.locator('[id^="result-away-"]').input_value() == "1"
        await page.locator('[id^="result-away-"]').focus()
        await page.keyboard.press("Enter")
        await page.wait_for_function(
            "document.querySelector('.result-note').textContent"
            ".includes('Result recorded — 2 picks settled.')"
        )

        stage = "mobile"
        # Mobile heading, font size, and target floor are runtime-verifiable.
        await page.locator("#edge-back").click()
        await page.set_viewport_size({"width": 390, "height": 844})
        await page.locator('[data-testid="dock-nav-today"]').click()
        assert await page.locator(".topbar-brand h1").is_visible()
        assert (
            await page.locator("#eq-search").evaluate("el => getComputedStyle(el).fontSize")
            == "16px"
        )
        logout_height = await page.locator("#logout-btn").evaluate(
            "el => el.getBoundingClientRect().height"
        )
        assert logout_height >= 44

        await page.screenshot(path=str(OUT / "mocked_regressions.png"), full_page=True)
        if page_errors:
            raise AssertionError(f"mock page errors: {page_errors}")
        unexpected_console = [
            message
            for message in mock_console_errors
            if "status of 500" not in message and "500 (Internal Server Error)" not in message
        ]
        if unexpected_console:
            raise AssertionError(f"mock console errors: {unexpected_console}")
        report.append("mocked_regressions: PASS")
    except Exception as exc:
        hard_failures.append(f"mocked regressions [{stage}]: {type(exc).__name__}: {exc}")
        report.append(f"mocked_regressions: FAIL [{stage}] {type(exc).__name__}: {exc}")
        with contextlib.suppress(Exception):
            await page.screenshot(path=str(OUT / "mocked_regressions_fail.png"), full_page=True)
    finally:
        await context.close()


async def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    report: list[str] = [
        "sharp-ev-picks dashboard QA report",
        f"generated_utc: {datetime.datetime.now(datetime.UTC).isoformat()}",
        f"base_url: {BASE}",
    ]
    hard_failures: list[str] = []
    console_errors: list[str] = []
    page_errors: list[str] = []
    request_failures: list[str] = []

    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        api = await pw.request.new_context()
        dashboard_html = ""
        try:
            if not MOCK_ONLY:
                response = await api.get(f"{BASE}/health", timeout=15000)
                report.append(f"health: HTTP {response.status} {(await response.text())[:500]}")
            dashboard_html = await _read_dashboard_source(api)
            if 'id="edge-list"' not in dashboard_html:
                raise RuntimeError("dashboard source was not returned")
            dashboard_bytes = dashboard_html.encode("utf-8")
            report.append(
                "dashboard_payload: "
                f"{len(dashboard_bytes)} bytes raw / "
                f"{len(gzip.compress(dashboard_bytes, compresslevel=9))} bytes gzip-9"
            )
        except Exception as exc:
            report.append(f"dashboard_source: UNAVAILABLE {type(exc).__name__}: {exc}")
            hard_failures.append("dashboard source unavailable for mocked regressions")
        finally:
            await api.dispose()

        if not MOCK_ONLY:
            try:
                await _live_sweep(
                    browser,
                    report,
                    hard_failures,
                    console_errors,
                    page_errors,
                    request_failures,
                )
            except Exception as exc:
                hard_failures.append(f"live sweep: {type(exc).__name__}: {exc}")
                report.append(f"live_sweep: FAIL {type(exc).__name__}: {exc}")
        if dashboard_html:
            await _mocked_regressions(browser, dashboard_html, report, hard_failures)
        await browser.close()

    report.append(f"console_errors: {len(console_errors)}")
    report.extend(f"  console: {item}" for item in console_errors[:12])
    report.append(f"pageerrors: {len(page_errors)}")
    report.extend(f"  pageerror: {item}" for item in page_errors[:12])
    report.append(f"request_failures (informational): {len(request_failures)}")
    report.extend(f"  requestfailed: {item}" for item in request_failures[:8])
    if console_errors:
        hard_failures.append(f"{len(console_errors)} console error(s)")
    if page_errors:
        hard_failures.append(f"{len(page_errors)} pageerror(s)")

    report.append(f"RESULT: {'FAIL' if hard_failures else 'PASS'}")
    report.extend(f"  failure: {failure}" for failure in hard_failures)
    output = "\n".join(report) + "\n"
    (OUT / "report.txt").write_text(output)
    print(output, end="")
    return 1 if hard_failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
