"""Full pipeline against a mock club: record -> distill -> wait -> book.

This is the closest thing to proof available without access to the real system.
It exercises the parts that only break under time pressure: the release gate,
losing a slot to a rival, and the fallback to the next preference.
"""

from datetime import datetime, time, timedelta, timezone
from pathlib import Path

import pytest
import requests

from courtbot.config import AttemptPolicy, Club, Config, Target
from courtbot.distill import Exchange, distill
from courtbot.sniper import Sniper
from courtbot.timing import LONDON, BookingWindow, ClockSync
from mock_club import COURTS, PASSWORD, USERNAME, ClubState, serve


def _record(session, response, sink):
    sink.append(Exchange(
        method=response.request.method, url=response.request.url,
        headers={k.lower(): v for k, v in response.request.headers.items()},
        body=response.request.body.decode() if isinstance(response.request.body, bytes)
        else response.request.body,
        status=response.status_code, response_text=response.text, resource_type="xhr",
    ))
    return response


@pytest.fixture
def club(request):
    """A mock club whose release is a few seconds out, plus a matching window."""
    seconds_out = getattr(request, "param", {}).get("seconds_out", 4)
    rivals = getattr(request, "param", {}).get("rivals", False)

    now_lon = datetime.now(LONDON)
    release_wall = (now_lon + timedelta(seconds=seconds_out)).time().replace(microsecond=0)
    window = BookingWindow(advance_days=9, release_time=release_wall, tz=LONDON)
    target = now_lon.date() + timedelta(days=9)
    release_at = window.release_instant_for(target)

    takes = [f"{target.isoformat()}-1900-{c.replace(' ', '')}" for c in COURTS] if rivals else []
    state = ClubState(release_at, advance_days=9,
                      rival_after=0.0 if rivals else None, rival_takes=takes)
    server = serve(state, port=0)
    base = f"http://127.0.0.1:{server.server_address[1]}"
    yield state, window, target, base, server
    server.shutdown()


@pytest.fixture
def recipe(club):
    """Recipe distilled from a scripted 'manual' booking on an already-open date."""
    state, _, _, base, _ = club
    sink, session = [], requests.Session()
    resp = _record(session, session.post(f"{base}/api/v2/auth/login",
                   json={"username": USERNAME, "password": PASSWORD}), sink)
    session.headers["authorization"] = f"Bearer {resp.json()['data']['accessToken']}"
    open_date = datetime.now(LONDON).date() + timedelta(days=8)
    _record(session, session.get(f"{base}/api/v2/availability",
            params={"date": open_date.isoformat()}), sink)
    _record(session, session.post(f"{base}/api/v2/bookings", json={
        "slotId": state.slot_id(open_date, 10, "Court 3"),
        "date": open_date.isoformat(), "startTime": "10:00"}), sink)

    result = distill(sink, booked_date=open_date, booked_time=time(10, 0), base_url=base)
    assert result.recipe.validate() == [], result.report
    return result.recipe


def make_config(window, target, base, times, courts=()):
    return Config(
        club=Club(name="Mock", court_preference=tuple(courts)),
        window=window,
        targets=(Target(target.weekday(), "target", tuple(times)),),
        attempts=AttemptPolicy(prefire_ms=200, retry_for_seconds=15, retry_interval_ms=150),
        base_url=base, recipe_path=Path("r"),
    )


@pytest.fixture(autouse=True)
def creds(monkeypatch):
    monkeypatch.setenv("DL_USERNAME", USERNAME)
    monkeypatch.setenv("DL_PASSWORD", PASSWORD)


def test_target_date_is_not_bookable_before_release(club, recipe):
    state, _, target, base, _ = club
    token = requests.post(f"{base}/api/v2/auth/login",
                          json={"username": USERNAME, "password": PASSWORD}
                          ).json()["data"]["accessToken"]
    body = requests.get(f"{base}/api/v2/availability", params={"date": target.isoformat()},
                        headers={"authorization": f"Bearer {token}"}).json()
    assert body["data"]["slots"] == []


def test_books_first_preference_at_the_release_moment(club, recipe):
    _, window, target, base, _ = club
    cfg = make_config(window, target, base,
                      [time(19, 0), time(10, 0)], courts=("Court 5",))
    outcome = Sniper(cfg, recipe, clock=ClockSync(enabled=False)).run(wait=True)

    assert outcome.booked, outcome.summary()
    assert outcome.result.slot.start == time(19, 0)
    assert outcome.result.slot.court == "Court 5"
    # It must not have succeeded before the window legitimately opened.
    assert outcome.attempts[-1].at >= window.release_instant_for(target) - timedelta(seconds=1)


@pytest.mark.parametrize("club", [{"rivals": True, "seconds_out": 4}], indirect=True)
def test_falls_back_when_rivals_take_the_first_preference(club, recipe):
    _, window, target, base, _ = club
    cfg = make_config(window, target, base, [time(19, 0), time(10, 0), time(11, 0)])
    outcome = Sniper(cfg, recipe, clock=ClockSync(enabled=False)).run(wait=True)

    assert outcome.booked, outcome.summary()
    assert outcome.result.slot.start == time(10, 0), "should have fallen back"


def test_dry_run_books_nothing(club, recipe):
    state, window, target, base, _ = club
    cfg = make_config(window, target, base, [time(19, 0)])
    cfg = Config(**{**cfg.__dict__, "dry_run": True})
    # The recipe fixture books once for real during discovery, so compare
    # against that baseline rather than an empty club.
    before, log_before = set(state.booked), len(state.booking_log)
    outcome = Sniper(cfg, recipe, clock=ClockSync(enabled=False)).run(wait=True)

    assert outcome.booked              # reported as a would-be success
    assert state.booked == before      # but nothing new was actually reserved
    assert len(state.booking_log) == log_before


def test_a_far_off_release_defers_instead_of_sleeping(club, recipe):
    """A process that sleeps for days dies to a reboot; it must hand back."""
    _, window, target, base, _ = club
    # Want a weekday that is not the one opening imminently.
    other = (target.weekday() + 3) % 7
    cfg = Config(**{**make_config(window, target, base, [time(10, 0)]).__dict__,
                    "targets": (Target(other, "other", (time(10, 0),)),)})
    started = datetime.now(timezone.utc)
    outcome = Sniper(cfg, recipe, clock=ClockSync(enabled=False)).run(
        wait=True, max_wait_seconds=60)

    assert outcome.deferred and not outcome.booked
    assert "wait cap" in outcome.note
    assert (datetime.now(timezone.utc) - started).total_seconds() < 15


def test_reports_cleanly_when_no_wanted_weekday_is_ever_targeted(club, recipe):
    _, window, target, base, _ = club
    cfg = make_config(window, target, base, [time(10, 0)])
    cfg = Config(**{**cfg.__dict__, "targets": ()})
    outcome = Sniper(cfg, recipe, clock=ClockSync(enabled=False)).run(wait=False)
    assert not outcome.booked and outcome.target_date is None
