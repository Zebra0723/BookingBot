"""Learn the booking API by watching one real booking.

Run once, on a machine with a browser. It opens the club's booking site, hands
control to the user, and records everything the site does while they sign in
and book a court by hand. From that recording it writes:

  captured/session.har      the raw evidence, for auditing or re-analysis
  captured/recipe.json      the three requests the sniper replays
  captured/storage_state.json  a logged-in browser session for the UI fallback

Doing it this way means the bot never depends on endpoints I guessed. It
depends on endpoints the site demonstrably used.
"""

from __future__ import annotations

import logging
import sys
from dataclasses import dataclass
from datetime import date, datetime, time
from pathlib import Path

from . import har as har_reader
from .distill import distill
from .recipe import Recipe

log = logging.getLogger(__name__)

# A real desktop UA. The booking site may serve a different (and less
# automatable) experience to something that self-identifies as headless.
DESKTOP_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)


class DiscoveryError(RuntimeError):
    """User-facing discovery failure."""


@dataclass
class DiscoveryResult:
    har_path: Path
    recipe_path: Path
    state_path: Path
    report: list[str]
    recipe: Recipe


def _require_playwright():
    try:
        from playwright.sync_api import sync_playwright  # noqa: WPS433
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise DiscoveryError(
            "Playwright is not installed. Run:\n"
            "    pip install playwright\n"
            "    playwright install chromium"
        ) from exc
    return sync_playwright


def record(
    url: str,
    *,
    out_dir: Path,
    headless: bool = False,
    wait_for_enter: bool = True,
    timeout_ms: int = 900_000,
) -> Path:
    """Open a browser, record the session, return the HAR path.

    Headed by design: the user has to solve the login, any 2FA and the booking
    UI themselves. Trying to script that blind is exactly what this avoids.
    """
    sync_playwright = _require_playwright()
    out_dir.mkdir(parents=True, exist_ok=True)
    har_path = out_dir / "session.har"
    state_path = out_dir / "storage_state.json"

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=headless, args=["--disable-blink-features=AutomationControlled"])
        context = browser.new_context(
            user_agent=DESKTOP_UA,
            viewport={"width": 1440, "height": 900},
            locale="en-GB",
            timezone_id="Europe/London",
            record_har_path=str(har_path),
            record_har_content="embed",
        )
        page = context.new_page()
        page.set_default_timeout(timeout_ms)
        page.goto(url, wait_until="domcontentloaded")

        _banner()
        if wait_for_enter:
            try:
                input()
            except (EOFError, KeyboardInterrupt):
                print("\n  (input closed — finishing capture)", file=sys.stderr)

        try:
            context.storage_state(path=str(state_path))
        except Exception as exc:  # pragma: no cover - browser state edge case
            log.warning("could not save storage state: %s", exc)
        context.close()   # flushes the HAR
        browser.close()

    if not har_path.exists():
        raise DiscoveryError(f"no HAR written to {har_path}; capture failed")
    return har_path


def _banner() -> None:
    print(
        "\n"
        "  ┌────────────────────────────────────────────────────────────┐\n"
        "  │  RECORDING. In the browser window that just opened:        │\n"
        "  │                                                            │\n"
        "  │   1. Sign in  (start signed OUT, or the login call is       │\n"
        "  │      never made and cannot be captured)                     │\n"
        "  │   2. Go to court booking for your club                      │\n"
        "  │   3. Book ONE court, all the way to confirmation            │\n"
        "  │                                                            │\n"
        "  │  A real, completed booking is required — browsing alone     │\n"
        "  │  does not reveal the booking request. Cancel it afterwards  │\n"
        "  │  if you do not want it.                                     │\n"
        "  │                                                            │\n"
        "  │  Then press ENTER here.                                     │\n"
        "  └────────────────────────────────────────────────────────────┘\n",
        file=sys.stderr,
    )


def analyse(
    har_path: Path,
    *,
    out_dir: Path,
    booked_date: date | None,
    booked_time: time | None,
    base_url: str = "",
) -> DiscoveryResult:
    """Distill a recorded HAR into a recipe on disk."""
    exchanges = har_reader.parse(har_path)
    if not exchanges:
        raise DiscoveryError(
            f"{har_path} contains no requests. The capture did not record "
            f"anything — check the browser actually loaded the site."
        )
    result = distill(
        exchanges,
        booked_date=booked_date,
        booked_time=booked_time,
        base_url=base_url or _infer_base(exchanges),
        source=str(har_path),
    )
    recipe_path = result.recipe.save(out_dir / "recipe.json")
    return DiscoveryResult(
        har_path=har_path,
        recipe_path=recipe_path,
        state_path=out_dir / "storage_state.json",
        report=result.report,
        recipe=result.recipe,
    )


def _infer_base(exchanges) -> str:
    """Most common origin among non-asset requests."""
    from collections import Counter
    from urllib.parse import urlsplit

    counts: Counter[str] = Counter()
    for e in exchanges:
        if e.is_noise():
            continue
        parts = urlsplit(e.url)
        if parts.scheme and parts.netloc:
            counts[f"{parts.scheme}://{parts.netloc}"] += 1
    return counts.most_common(1)[0][0] if counts else ""
