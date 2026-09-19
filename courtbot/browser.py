"""Fallback: drive the real booking UI in a browser.

The HTTP path is faster and preferred, but it can be defeated — a WAF that
fingerprints non-browser clients, a token bound to a browser session, or a
booking flow with a server-rendered step. When that happens, replaying the UI
in a real browser still works; it just costs seconds.

Selectors live in config because they belong to a site this code has never
seen. `courtbot selectors` prints what the saved page actually contains so the
user can fill them in without reading the recipe by hand.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, time
from pathlib import Path

log = logging.getLogger(__name__)


class BrowserError(RuntimeError):
    """User-facing browser-fallback failure."""


@dataclass
class BrowserSelectors:
    """How to find things on the booking page.

    `booking_url` may contain {date}; the rest are CSS selectors. `slot` should
    match every bookable slot element, and its text is matched against the
    wanted time.
    """

    booking_url: str = ""
    slot: str = ""
    slot_time_attr: str = ""     # read the time from an attribute instead of text
    confirm: str = ""
    success: str = ""
    cookie_accept: str = ""

    @staticmethod
    def from_config(extras: dict) -> "BrowserSelectors":
        raw = (extras or {}).get("browser") or {}
        return BrowserSelectors(
            booking_url=str(raw.get("booking_url", "")),
            slot=str(raw.get("slot", "")),
            slot_time_attr=str(raw.get("slot_time_attr", "")),
            confirm=str(raw.get("confirm", "")),
            success=str(raw.get("success", "")),
            cookie_accept=str(raw.get("cookie_accept", "")),
        )

    def usable(self) -> list[str]:
        missing = [
            name for name, value in (
                ("extras.browser.booking_url", self.booking_url),
                ("extras.browser.slot", self.slot),
                ("extras.browser.confirm", self.confirm),
            ) if not value
        ]
        return missing


def book_via_ui(
    target: date,
    wanted: list[time],
    selectors: BrowserSelectors,
    *,
    storage_state: Path,
    headless: bool = True,
    timeout_ms: int = 30_000,
    dry_run: bool = False,
) -> tuple[bool, str]:
    """Try to book one of `wanted` on `target`. Returns (booked, detail)."""
    missing = selectors.usable()
    if missing:
        raise BrowserError(
            "browser fallback is not configured. Set " + ", ".join(missing) +
            " in your config (run `courtbot selectors` for help)."
        )
    if not storage_state.exists():
        raise BrowserError(
            f"no saved browser session at {storage_state}. Run `courtbot discover` "
            f"first — it saves one."
        )
    try:
        from playwright.sync_api import TimeoutError as PWTimeout, sync_playwright
    except ImportError as exc:
        raise BrowserError("Playwright is not installed (pip install playwright)") from exc

    url = selectors.booking_url.replace("{date}", target.isoformat())
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=headless)
        context = browser.new_context(
            storage_state=str(storage_state), locale="en-GB", timezone_id="Europe/London",
        )
        page = context.new_page()
        page.set_default_timeout(timeout_ms)
        try:
            page.goto(url, wait_until="domcontentloaded")
            if selectors.cookie_accept:
                try:
                    page.click(selectors.cookie_accept, timeout=3_000)
                except PWTimeout:
                    pass  # banner not shown; fine

            page.wait_for_selector(selectors.slot, timeout=timeout_ms)
            elements = page.query_selector_all(selectors.slot)
            if not elements:
                return False, f"no elements matched `{selectors.slot}`"

            for want in wanted:
                label = want.strftime("%H:%M")
                for el in elements:
                    text = (
                        el.get_attribute(selectors.slot_time_attr)
                        if selectors.slot_time_attr else el.inner_text()
                    ) or ""
                    if label not in text:
                        continue
                    if dry_run:
                        return True, f"dry run — would click slot matching {label}"
                    el.click()
                    page.click(selectors.confirm)
                    if selectors.success:
                        try:
                            page.wait_for_selector(selectors.success, timeout=timeout_ms)
                        except PWTimeout:
                            return False, f"clicked {label} but no success marker appeared"
                    return True, f"booked {label} via the UI"
            return False, "none of the wanted times were present on the page"
        finally:
            context.close()
            browser.close()
