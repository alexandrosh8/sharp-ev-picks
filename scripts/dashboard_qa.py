"""Playwright sweep for the SignalDesk dashboard (read-only GETs).

Runs INSIDE the mcr.microsoft.com/playwright/python container — do not run on
the host (the dev container lacks browser system libs). Driven by
scripts/dashboard_qa.sh, which pipes this file to `docker exec -i <qa> python -`.

Environment:
    DASHQA_BASE_URL  base URL of the app (default http://app:8000)
    DASHQA_OUT       output dir inside the container (default /out)

Writes per-tab screenshots + report.txt to DASHQA_OUT. Exits nonzero if any
tab fails to render / has an empty root panel, any console error or pageerror
occurs, or horizontal overflow is detected at 390px.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime
import os
import sys
from pathlib import Path

from playwright.async_api import async_playwright

TABS = ["today", "edges", "radar", "lab", "sources"]
BASE = os.environ.get("DASHQA_BASE_URL", "http://app:8000").rstrip("/")
OUT = Path(os.environ.get("DASHQA_OUT", "/out"))


async def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    report: list[str] = [
        "SignalDesk dashboard QA report",
        f"generated_utc: {datetime.datetime.now(datetime.UTC).isoformat()}",
        f"base_url: {BASE}",
    ]
    hard_failures: list[str] = []
    console_errors: list[str] = []
    page_errors: list[str] = []
    request_failures: list[str] = []

    async with async_playwright() as pw:
        # /health status (read-only GET, recorded for context).
        try:
            api = await pw.request.new_context()
            resp = await api.get(f"{BASE}/health", timeout=15000)
            body = (await resp.text())[:500]
            report.append(f"health: HTTP {resp.status} {body}")
            await api.dispose()
        except Exception as exc:
            report.append(f"health: UNREACHABLE {type(exc).__name__}")

        browser = await pw.chromium.launch()
        page = await browser.new_page(viewport={"width": 1440, "height": 900})
        page.on(
            "console",
            lambda m: console_errors.append(m.text) if m.type == "error" else None,
        )
        page.on("pageerror", lambda e: page_errors.append(f"{e}"))
        page.on(
            "requestfailed",
            lambda r: request_failures.append(f"{r.url} {r.failure}"),
        )

        await page.goto(f"{BASE}/", wait_until="networkidle", timeout=60000)
        await page.wait_for_timeout(4000)

        for tab in TABS:
            try:
                await page.locator(f'[data-testid="rail-nav-{tab}"]').first.click(timeout=6000)
                await page.wait_for_timeout(1800)
                panel = page.locator(f"#view-{tab}")
                await panel.wait_for(state="visible", timeout=6000)
                text = (await panel.inner_text()).strip()
                await page.screenshot(path=str(OUT / f"tab_{tab}.png"))
                if not text:
                    hard_failures.append(f"tab {tab}: rendered but root panel is EMPTY")
                    report.append(f"tab {tab}: FAIL empty root panel #view-{tab}")
                else:
                    report.append(f"tab {tab}: OK (panel text {len(text)} chars)")
            except Exception as exc:
                hard_failures.append(f"tab {tab}: {type(exc).__name__}: {exc}")
                report.append(f"tab {tab}: FAIL {type(exc).__name__}: {exc}")
                with contextlib.suppress(Exception):
                    await page.screenshot(path=str(OUT / f"tab_{tab}_fail.png"))

        # Mobile pass: 390px, Today tab, horizontal-overflow check.
        await page.set_viewport_size({"width": 390, "height": 844})
        with contextlib.suppress(Exception):
            nav = page.locator('[data-testid="dock-nav-today"], [data-testid="rail-nav-today"]')
            await nav.first.click(timeout=6000)
        await page.wait_for_timeout(1500)
        await page.screenshot(path=str(OUT / "tab_today_mobile.png"))
        overflow = await page.evaluate(
            "Math.max(document.body.scrollWidth,"
            " document.documentElement.scrollWidth) > window.innerWidth"
        )
        report.append(f"mobile_overflow_390px: {'YES' if overflow else 'no'}")
        if overflow:
            hard_failures.append("mobile horizontal overflow at 390px")

        await browser.close()

    report.append(f"console_errors: {len(console_errors)}")
    report.extend(f"  console: {t}" for t in console_errors[:12])
    report.append(f"pageerrors: {len(page_errors)}")
    report.extend(f"  pageerror: {t}" for t in page_errors[:12])
    report.append(f"request_failures (informational): {len(request_failures)}")
    report.extend(f"  requestfailed: {t}" for t in request_failures[:8])

    if console_errors:
        hard_failures.append(f"{len(console_errors)} console error(s)")
    if page_errors:
        hard_failures.append(f"{len(page_errors)} pageerror(s)")

    report.append(f"RESULT: {'FAIL' if hard_failures else 'PASS'}")
    report.extend(f"  failure: {f}" for f in hard_failures)

    text = "\n".join(report) + "\n"
    (OUT / "report.txt").write_text(text)
    print(text, end="")
    return 1 if hard_failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
