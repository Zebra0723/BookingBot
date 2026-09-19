"""Selection and planning. Both are pure, so they are tested against a clock
that does not tick."""

from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

import pytest

from courtbot.config import AttemptPolicy, Club, Config, Target
from courtbot.http_client import Slot
from courtbot.sniper import choose_slot, next_actionable_release, slot_key
from courtbot.timing import LONDON, BookingWindow

SAT = 5


def slot(t, court="Court 1", available=True, sid=None):
    hh, mm = t.split(":")
    return Slot(start=time(int(hh), int(mm)), court=court, available=available,
                slot_id=sid or f"{t}-{court}")


def make_config(times, *, weekday=SAT, courts=()):
    return Config(
        club=Club(name="Test", court_preference=tuple(courts)),
        window=BookingWindow(advance_days=9, release_time=time(8, 0), tz=LONDON),
        targets=(Target(weekday, "saturday", tuple(
            time(int(x.split(":")[0]), int(x.split(":")[1])) for x in times)),),
        attempts=AttemptPolicy(),
        base_url="", recipe_path=Path("r"), state_path=Path("s"),
    )


# --- choose_slot ------------------------------------------------------------

def test_picks_the_first_preference_when_free():
    t = make_config(["10:00", "11:00"]).targets[0]
    assert choose_slot([slot("11:00"), slot("10:00")], t).start == time(10, 0)


def test_falls_back_in_declared_order():
    t = make_config(["10:00", "11:00", "09:00"]).targets[0]
    got = choose_slot([slot("10:00", available=False), slot("09:00"), slot("11:00")], t)
    assert got.start == time(11, 0)


def test_never_picks_an_unavailable_slot():
    t = make_config(["10:00"]).targets[0]
    assert choose_slot([slot("10:00", available=False)], t) is None


def test_ignores_times_not_asked_for():
    t = make_config(["10:00"]).targets[0]
    assert choose_slot([slot("14:00"), slot("15:00")], t) is None


def test_time_preference_outranks_court_preference():
    # A worse court at the preferred time beats a favourite court an hour later.
    cfg = make_config(["10:00", "11:00"], courts=("Court 5",))
    got = choose_slot([slot("11:00", "Court 5"), slot("10:00", "Court 1")],
                      cfg.targets[0], cfg.club.court_preference)
    assert got.start == time(10, 0) and got.court == "Court 1"


def test_court_preference_breaks_ties_at_the_same_time():
    cfg = make_config(["10:00"], courts=("Court 5", "Court 3"))
    got = choose_slot([slot("10:00", "Court 1"), slot("10:00", "Court 3"),
                       slot("10:00", "Court 5")], cfg.targets[0], cfg.club.court_preference)
    assert got.court == "Court 5"


def test_court_preference_matches_case_insensitively():
    cfg = make_config(["10:00"], courts=("court 3",))
    got = choose_slot([slot("10:00", "Court 1"), slot("10:00", "Court 3")],
                      cfg.targets[0], cfg.club.court_preference)
    assert got.court == "Court 3"


def test_slots_with_an_unparseable_time_are_skipped():
    t = make_config(["10:00"]).targets[0]
    bad = Slot(start=None, court="X", slot_id="x", available=True)
    assert choose_slot([bad], t) is None


def test_empty_availability_is_handled():
    assert choose_slot([], make_config(["10:00"]).targets[0]) is None


# --- next_actionable_release ------------------------------------------------

def at(y, m, d, hh, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=LONDON).astimezone(timezone.utc)


def test_finds_todays_release_when_it_opens_a_wanted_weekday():
    # 19 Sep 2026 is a Saturday; +9 days is Monday 28th, which we do not want.
    # The Saturday we want (3 Oct) opens on Thursday 24 Sep.
    cfg = make_config(["10:00"], weekday=SAT)
    plan = next_actionable_release(cfg, at(2026, 9, 19, 6))
    assert plan.target_date.weekday() == SAT
    assert plan.target_date == date(2026, 10, 3)
    assert plan.release_at == cfg.window.release_instant_for(date(2026, 10, 3))


def test_release_is_always_nine_days_before_its_target():
    cfg = make_config(["10:00"], weekday=SAT)
    plan = next_actionable_release(cfg, at(2026, 9, 19, 6))
    local_release_day = plan.release_at.astimezone(LONDON).date()
    assert plan.target_date - local_release_day == timedelta(days=9)


def test_skips_a_release_that_has_already_happened_today():
    cfg = make_config(["10:00"], weekday=SAT)
    before = next_actionable_release(cfg, at(2026, 9, 24, 7, 59))
    after = next_actionable_release(cfg, at(2026, 9, 24, 8, 1))
    assert before.target_date == date(2026, 10, 3)
    assert after.target_date == date(2026, 10, 10)


def test_returns_none_when_no_release_in_the_horizon_matches():
    cfg = make_config(["10:00"], weekday=SAT)
    assert next_actionable_release(cfg, at(2026, 9, 19, 6), horizon_days=2) is None


def test_plan_is_flagged_as_today_only_when_it_is():
    cfg = make_config(["10:00"], weekday=SAT)
    assert next_actionable_release(cfg, at(2026, 9, 24, 6)).is_today is True
    assert next_actionable_release(cfg, at(2026, 9, 23, 6)).is_today is False


@pytest.mark.parametrize("weekday", range(7))
def test_every_weekday_is_reachable_within_the_horizon(weekday):
    cfg = make_config(["10:00"], weekday=weekday)
    plan = next_actionable_release(cfg, at(2026, 9, 19, 6))
    assert plan is not None
    assert plan.target_date.weekday() == weekday


# --- slot identity ----------------------------------------------------------

def test_slot_key_uses_the_id_when_there_is_one():
    assert slot_key(slot("10:00", "Court 1", sid="abc")) == "abc"


def test_slot_key_falls_back_to_time_and_court_when_there_is_no_id():
    # Regression: an API with no slot id gave every slot the same empty key, so
    # excluding one failed slot excluded the whole grid after a single attempt.
    a = Slot(start=time(10, 0), court="Court 1", slot_id="", available=True)
    b = Slot(start=time(11, 0), court="Court 1", slot_id="", available=True)
    c = Slot(start=time(10, 0), court="Court 3", slot_id="", available=True)
    assert len({slot_key(a), slot_key(b), slot_key(c)}) == 3


def test_slot_key_is_stable_for_the_same_slot():
    a = Slot(start=time(10, 0), court="Court 1", slot_id="", available=True)
    b = Slot(start=time(10, 0), court="Court 1", slot_id="", available=False)
    assert slot_key(a) == slot_key(b)
