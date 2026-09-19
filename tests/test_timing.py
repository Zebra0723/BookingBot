"""Booking-window arithmetic. Getting this wrong books the wrong day, or the
right day an hour late — both of which lose the court."""

from datetime import date, datetime, time, timedelta, timezone

import pytest

from courtbot.timing import LONDON, BookingWindow, ClockSync, _to_utc

W = BookingWindow()


def test_nine_day_rule_matches_published_example():
    # David Lloyd: "booking on a Monday you can book for the following Wednesday".
    monday = date(2026, 9, 21)
    target = W.target_date_for(monday)
    assert target == date(2026, 9, 30)
    assert target.strftime("%A") == "Wednesday"


def test_release_day_is_the_inverse_of_target_date():
    for offset in range(0, 40):
        d = date(2026, 1, 1) + timedelta(days=offset)
        assert W.release_day_for(W.target_date_for(d)) == d


@pytest.mark.parametrize(
    "target,expected_utc_hour,expected_zone",
    [
        # 8am local on a GMT day is 8am UTC; on a BST day it is 7am UTC.
        (date(2026, 3, 28) + timedelta(days=9), 8, "GMT"),
        (date(2026, 3, 29) + timedelta(days=9), 7, "BST"),
        (date(2026, 10, 24) + timedelta(days=9), 7, "BST"),
        (date(2026, 10, 25) + timedelta(days=9), 8, "GMT"),
    ],
)
def test_release_instant_across_dst_boundaries(target, expected_utc_hour, expected_zone):
    instant = W.release_instant_for(target)
    assert instant.hour == expected_utc_hour
    assert instant.astimezone(LONDON).tzname() == expected_zone
    assert instant.astimezone(LONDON).hour == 8


def test_release_time_inside_the_spring_forward_gap_does_not_vanish():
    # 01:30 does not exist on 29 March 2026 — the clocks jump 01:00 -> 02:00.
    gap = BookingWindow(advance_days=9, release_time=time(1, 30))
    instant = gap.release_instant_for(date(2026, 3, 29) + timedelta(days=9))
    local = instant.astimezone(LONDON)
    assert local.date() == date(2026, 3, 29)
    # It must land just after the gap, not silently an hour early.
    assert (local.hour, local.minute) == (2, 30)


def test_fall_back_ambiguity_picks_the_earlier_instant():
    amb = BookingWindow(advance_days=9, release_time=time(1, 30))
    instant = amb.release_instant_for(date(2026, 10, 25) + timedelta(days=9))
    # 01:30 happens twice; the first (still BST) is 00:30 UTC.
    assert instant == datetime(2026, 10, 25, 0, 30, tzinfo=timezone.utc)


def test_next_release_skips_one_that_has_already_passed():
    # 09:00 London on 19 Sep is after that day's 08:00 release.
    now = datetime(2026, 9, 19, 9, 0, tzinfo=LONDON).astimezone(timezone.utc)
    instant, target = W.next_release(now)
    assert instant.astimezone(LONDON).date() == date(2026, 9, 20)
    assert target == date(2026, 9, 29)


def test_next_release_returns_today_when_still_ahead():
    now = datetime(2026, 9, 19, 6, 0, tzinfo=LONDON).astimezone(timezone.utc)
    instant, target = W.next_release(now)
    assert instant.astimezone(LONDON).date() == date(2026, 9, 19)
    assert target == date(2026, 9, 28)


def test_next_release_is_never_in_the_past():
    for hour in range(0, 24):
        now = datetime(2026, 9, 19, hour, 0, tzinfo=LONDON).astimezone(timezone.utc)
        instant, _ = W.next_release(now)
        assert instant >= now


def test_is_bookable_flips_exactly_at_the_release_instant():
    target = date(2026, 9, 28)
    release = W.release_instant_for(target)
    assert W.is_bookable(target, release)
    assert W.is_bookable(target, release + timedelta(milliseconds=1))
    assert not W.is_bookable(target, release - timedelta(milliseconds=1))


def test_to_utc_roundtrips_for_ordinary_times():
    wall = datetime(2026, 6, 1, 8, 0)
    assert _to_utc(wall, LONDON).astimezone(LONDON).replace(tzinfo=None) == wall


def test_clock_sync_disabled_reports_unsynced_and_zero_offset():
    c = ClockSync(enabled=False).sync()
    assert c.offset == 0.0
    assert c.synced is False


def test_clock_sync_survives_an_unreachable_server_within_budget():
    import time as t
    started = t.monotonic()
    # 203.0.113.0/24 is TEST-NET-3: reserved, guaranteed not to answer.
    c = ClockSync(server="203.0.113.1", samples=4, timeout=0.3, budget=2.0).sync()
    assert c.synced is False
    assert c.offset == 0.0
    assert t.monotonic() - started < 5.0, "sync must not block past its budget"


# --- the 9-day window straddling a clock change -----------------------------
# Which zone applies is decided by the day the release happens, not the day
# being booked. Four windows a year span a change, and anchoring on the wrong
# end would fire an hour early or an hour late on each of them.

@pytest.mark.parametrize(
    "release_day,release_zone,target_zone,expected_utc_hour",
    [
        # Release in GMT, the court date lands after the spring change in BST.
        (date(2026, 3, 20), "GMT", "BST", 8),
        (date(2026, 3, 28), "GMT", "BST", 8),
        # Release in BST, the court date lands after the autumn change in GMT.
        (date(2026, 10, 16), "BST", "GMT", 7),
        (date(2026, 10, 24), "BST", "GMT", 7),
    ],
)
def test_release_follows_the_release_day_not_the_booked_day(
    release_day, release_zone, target_zone, expected_utc_hour
):
    target = W.target_date_for(release_day)
    instant = W.release_instant_for(target)

    # The window really does straddle a change, or this test proves nothing.
    booked_zone = datetime.combine(target, time(12, 0), tzinfo=LONDON).tzname()
    assert booked_zone == target_zone
    assert instant.astimezone(LONDON).tzname() == release_zone
    assert booked_zone != release_zone

    # 08:00 local on the release day, whatever the booked day is doing.
    assert instant.astimezone(LONDON).hour == 8
    assert instant.astimezone(LONDON).date() == release_day
    assert instant.hour == expected_utc_hour


def test_every_release_in_a_year_is_eight_local_on_its_own_release_day():
    day = date(2026, 1, 1)
    while day < date(2027, 1, 1):
        local = W.release_instant_for(W.target_date_for(day)).astimezone(LONDON)
        assert (local.date(), local.hour, local.minute) == (day, 8, 0), day
        day += timedelta(days=1)
