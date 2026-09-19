"""Replay a captured recipe over HTTP.

This is the fast path. A browser needs seconds to load the booking page and
render a slot grid; three HTTP calls against a pre-warmed TLS connection need
tens of milliseconds, and at 08:00:00 that difference is the whole game.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime, time
from typing import Any

import requests
from requests.adapters import HTTPAdapter

from .recipe import Recipe, RecipeError, Step, extract_path

log = logging.getLogger(__name__)

_ISO_TIME = re.compile(r"(?:^|T)([01]?\d|2[0-3])[:.]([0-5]\d)")


@dataclass
class Slot:
    """One bookable slot, normalised out of whatever shape the API returned."""

    start: time | None
    court: str
    slot_id: str
    available: bool
    raw: dict = field(default_factory=dict)

    def label(self) -> str:
        t = self.start.strftime("%H:%M") if self.start else "??:??"
        return f"{t}{' ' + self.court if self.court else ''}"


@dataclass
class BookingResult:
    ok: bool
    status: int
    detail: str
    slot: Slot | None = None
    response: Any = None

    def __str__(self) -> str:
        if self.ok and self.slot:
            return f"BOOKED {self.slot.label()} (HTTP {self.status})"
        return f"failed ({self.status}): {self.detail}"


def parse_slot_time(value: Any) -> time | None:
    """Pull a start time out of '10:00', '10:00:00' or an ISO timestamp."""
    if isinstance(value, time):
        return value
    if isinstance(value, (int, float)):
        # Epoch seconds or milliseconds.
        ts = float(value)
        if ts > 1e11:
            ts /= 1000.0
        try:
            return datetime.fromtimestamp(ts).time().replace(second=0, microsecond=0)
        except (OSError, OverflowError, ValueError):
            return None
    if not isinstance(value, str):
        return None
    m = _ISO_TIME.search(value)
    if not m:
        return None
    return time(int(m.group(1)), int(m.group(2)))


class BookingClient:
    """Executes a Recipe. One instance per booking attempt run."""

    def __init__(
        self,
        recipe: Recipe,
        *,
        timeout: float = 10.0,
        dry_run: bool = False,
        extra_values: dict[str, Any] | None = None,
    ) -> None:
        self.recipe = recipe
        self.timeout = timeout
        self.dry_run = dry_run
        self.values: dict[str, Any] = dict(extra_values or {})
        self.session = requests.Session()
        # One healthy keep-alive connection is all we need; a big pool just
        # means more handshakes at the worst possible moment.
        adapter = HTTPAdapter(pool_connections=4, pool_maxsize=8, max_retries=0)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)
        self.authenticated = False

    # -- plumbing ---------------------------------------------------------

    def _send(self, step: Step, values: dict[str, Any]) -> requests.Response:
        url, headers, body = step.render(values)
        kwargs: dict[str, Any] = {"headers": headers, "timeout": self.timeout}
        if body is not None:
            ctype = (step.content_type or "").lower()
            if "json" in ctype:
                headers.setdefault("content-type", "application/json")
                kwargs["data"] = body.encode("utf-8")
            else:
                headers.setdefault("content-type", ctype or "application/x-www-form-urlencoded")
                kwargs["data"] = body.encode("utf-8")
        log.debug("%s %s", step.method, url)
        return self.session.request(step.method, url, **kwargs)

    def _absorb(self, step: Step, resp: requests.Response) -> dict[str, Any]:
        """Pull `extract` values out of a response into the value bag."""
        if not step.extract:
            return {}
        try:
            payload = resp.json()
        except ValueError:
            log.warning("%s: response is not JSON; cannot extract %s",
                        step.name, list(step.extract))
            return {}
        got: dict[str, Any] = {}
        for name, path in step.extract.items():
            value = extract_path(payload, path)
            if value is None:
                log.warning("%s: nothing at `%s` for `%s`", step.name, path, name)
            else:
                got[name] = value
        self.values.update(got)
        return got

    def prewarm(self) -> bool:
        """Open the TLS connection before the race so the first real request
        is not paying for a handshake. Failure here is not fatal."""
        target = self.recipe.base_url or (self.recipe.availability.url if self.recipe.availability else "")
        if not target:
            return False
        try:
            self.session.get(self.recipe.base_url or target, timeout=self.timeout)
            log.info("connection pre-warmed against %s", self.recipe.base_url or target)
            return True
        except requests.RequestException as exc:
            log.debug("pre-warm failed (harmless): %s", exc)
            return False

    # -- the three steps --------------------------------------------------

    def login(self, username: str, password: str) -> None:
        step = self.recipe.login
        self.values.update({"username": username, "password": password})
        if step is None:
            log.info("recipe has no login step; assuming cookie auth from storage state")
            self.authenticated = True
            return
        resp = self._send(step, self.values)
        if not step.ok(resp.status_code):
            raise RecipeError(
                f"login failed with HTTP {resp.status_code}. Either the credentials "
                f"in the environment are wrong, or the login endpoint has changed "
                f"and you need to re-run `courtbot discover`.\n"
                f"  response: {resp.text[:300]}"
            )
        self._absorb(step, resp)
        for extra in self.recipe.preflight:
            try:
                self._absorb(extra, self._send(extra, self.values))
            except (requests.RequestException, RecipeError) as exc:
                log.warning("preflight step %s failed: %s", extra.name, exc)
        self.authenticated = True
        log.info("authenticated as %s", username)

    def availability(self, target: date) -> list[Slot]:
        step = self.recipe.availability
        if step is None:
            raise RecipeError("recipe has no availability step; re-run discovery")
        values = dict(self.values, date=target.isoformat(), date_iso=target.isoformat())
        resp = self._send(step, values)
        if not step.ok(resp.status_code):
            raise RecipeError(
                f"availability lookup failed with HTTP {resp.status_code}: "
                f"{resp.text[:300]}"
            )
        try:
            payload = resp.json()
        except ValueError as exc:
            raise RecipeError(
                "availability response was not JSON — the recipe may be pointing "
                "at an HTML page rather than the API"
            ) from exc
        return self._read_slots(payload)

    def _read_slots(self, payload: Any) -> list[Slot]:
        m = self.recipe.slots
        raw = extract_path(payload, m.list_path) if m.list_path else payload
        if not isinstance(raw, list):
            raise RecipeError(
                f"no slot list at `{m.list_path}` in the availability response. "
                f"Fix `slots.list_path` in the recipe."
            )
        slots: list[Slot] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            available = True
            if m.available_field:
                available = item.get(m.available_field) == m.available_value
            slots.append(
                Slot(
                    start=parse_slot_time(item.get(m.time_field)),
                    court=str(item.get(m.court_field, "") or "") if m.court_field else "",
                    slot_id=str(item.get(m.id_field, "") or "") if m.id_field else "",
                    available=bool(available),
                    raw=item,
                )
            )
        return slots

    def book(self, slot: Slot, target: date) -> BookingResult:
        step = self.recipe.book
        if step is None:
            raise RecipeError("recipe has no booking step; re-run discovery")
        values = dict(
            self.values,
            date=target.isoformat(),
            time=slot.start.strftime("%H:%M") if slot.start else "",
            slot_id=slot.slot_id,
            court_id=slot.court,
        )
        # Anything the booking step still needs may live on the slot itself.
        for name in step.placeholders():
            if name not in values and name in slot.raw:
                values[name] = slot.raw[name]

        if self.dry_run:
            url, _, body = step.render(values)
            log.info("DRY RUN — would send %s %s body=%s", step.method, url, body)
            return BookingResult(True, 0, "dry run — nothing was sent", slot)

        resp = self._send(step, values)
        ok = step.ok(resp.status_code)
        try:
            payload = resp.json()
        except ValueError:
            payload = resp.text[:500]
        return BookingResult(
            ok=ok,
            status=resp.status_code,
            detail="booked" if ok else str(payload)[:300],
            slot=slot,
            response=payload,
        )
