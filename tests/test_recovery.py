"""Session recovery.

Pre-warming logs in ~90 seconds before the release so the connection is hot
when the window opens. A short-lived token can lapse inside that gap, and
retrying a dead session for 45 seconds would burn the whole release.
"""

from datetime import date, time
from pathlib import Path

import pytest

from courtbot.config import AttemptPolicy, Club, Config, Target
from courtbot.http_client import AuthExpired, BookingResult, Slot
from courtbot.recipe import Recipe, SlotMapping, Step
from courtbot.sniper import Sniper
from courtbot.timing import LONDON, BookingWindow


class StubClient:
    """A booking client that fails in a scripted way."""

    def __init__(self, *, expire_times=0, book_ok=True):
        self.expire_times = expire_times
        self.book_ok = book_ok
        self.logins = 0
        self.availability_calls = 0
        self.book_calls = 0

    def prewarm(self):
        return True

    def login(self, credentials):
        self.logins += 1

    def availability(self, target):
        self.availability_calls += 1
        if self.expire_times > 0:
            self.expire_times -= 1
            raise AuthExpired("HTTP 401")
        return [Slot(start=time(10, 0), court="Court 1", slot_id="s1", available=True)]

    def book(self, slot, target):
        self.book_calls += 1
        if self.book_ok:
            return BookingResult(True, 201, "booked", slot)
        return BookingResult(False, 409, "taken", slot)


@pytest.fixture
def recipe():
    return Recipe(
        base_url="https://api.example.com",
        login=Step("login", "POST", "/login", extract={"token": "data.token"}),
        availability=Step("availability", "GET", "/a?date={date}"),
        book=Step("book", "POST", "/b", body='{"slotId":"{slot_id}"}'),
        slots=SlotMapping(list_path="slots", time_field="startTime", id_field="id"),
    )


@pytest.fixture
def config():
    target = date.today()
    return Config(
        club=Club(name="Stub"),
        window=BookingWindow(advance_days=9, release_time=time(0, 0), tz=LONDON),
        targets=(Target((target.weekday() + 9) % 7, "t", (time(10, 0),)),),
        attempts=AttemptPolicy(retry_for_seconds=5, retry_interval_ms=50),
        base_url="https://api.example.com",
        recipe_path=Path("r"),
    )


@pytest.fixture(autouse=True)
def creds(monkeypatch):
    monkeypatch.setenv("DL_USERNAME", "u")
    monkeypatch.setenv("DL_PASSWORD", "p")


def run(config, recipe, client):
    from courtbot.timing import ClockSync
    return Sniper(config, recipe, clock=ClockSync(enabled=False),
                  client=client).run(wait=False)


def test_an_expired_session_is_re_authenticated_once(config, recipe):
    client = StubClient(expire_times=1)
    outcome = run(config, recipe, client)
    assert outcome.booked, outcome.summary()
    assert client.logins == 2, "should log in again after the session lapsed"
    assert any("re-authenticating" in a.note for a in outcome.attempts)


def test_it_does_not_re_authenticate_forever(config, recipe):
    # A permanently bad credential must fail fast, not spin for the whole window.
    client = StubClient(expire_times=99)
    outcome = run(config, recipe, client)
    assert not outcome.booked
    assert client.logins == 2, "one initial login plus exactly one retry"
    assert "authentication" in outcome.note


def test_a_healthy_session_never_re_authenticates(config, recipe):
    client = StubClient(expire_times=0)
    outcome = run(config, recipe, client)
    assert outcome.booked
    assert client.logins == 1


def test_a_rejected_booking_does_not_trigger_re_authentication(config, recipe):
    # A 409 means someone else took the slot; the session is fine.
    client = StubClient(expire_times=0, book_ok=False)
    outcome = run(config, recipe, client)
    assert not outcome.booked
    assert client.logins == 1
    # Having tried the only slot, it must not keep hammering it.
    assert client.book_calls == 1
