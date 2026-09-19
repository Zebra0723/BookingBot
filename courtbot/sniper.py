"""Orchestration: decide what to book, wait for the release, take it.

Exactly one new date opens at each release, so the bot's job each morning is
narrow: work out which date that is, check whether the user wants that weekday,
and if so be first in the queue for their preferred time.

The selection and planning logic here is deliberately free of I/O so it can be
tested against a clock that does not tick.
"""

from __future__ import annotations

import logging
import time as _time
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone

from .config import Config, Target
from .http_client import AuthExpired, BookingClient, BookingResult, Slot
from .recipe import Recipe, RecipeError
from .timing import ClockSync

log = logging.getLogger(__name__)


@dataclass
class Plan:
    """What the next actionable release is."""

    release_at: datetime
    target_date: date
    target: Target
    is_today: bool

    def describe(self, tz) -> str:
        local = self.release_at.astimezone(tz)
        return (
            f"{self.target_date:%a %d %b %Y} opens at "
            f"{local:%H:%M:%S %Z} on {local:%a %d %b}"
        )


@dataclass
class Attempt:
    at: datetime
    slot: Slot | None
    result: BookingResult | None
    note: str = ""


@dataclass
class SnipeOutcome:
    booked: bool
    target_date: date | None
    attempts: list[Attempt] = field(default_factory=list)
    result: BookingResult | None = None
    fire_lag_ms: float = 0.0
    clock_offset: float = 0.0
    clock_synced: bool = True
    note: str = ""
    # True when the run stopped because the release is too far off to wait for.
    deferred: bool = False
    plan: "Plan | None" = None

    def summary(self) -> str:
        if self.deferred and self.plan:
            return f"Nothing to do yet — {self.note}"
        if self.booked and self.result and self.result.slot:
            return (
                f"Booked {self.result.slot.label()} on "
                f"{self.target_date:%a %d %b} after {len(self.attempts)} attempt(s)"
            )
        if self.target_date is None:
            return self.note or "nothing to do"
        return (
            f"No court booked for {self.target_date:%a %d %b} "
            f"after {len(self.attempts)} attempt(s). {self.note}".strip()
        )


def slot_key(slot: Slot) -> str:
    """A stable identity for a slot.

    Not every API exposes a slot id. Falling back to the bare `slot_id` would
    make every slot share the empty string, so excluding one failed slot would
    exclude the entire grid and the run would give up after a single attempt.
    """
    if slot.slot_id:
        return slot.slot_id
    start = slot.start.strftime("%H:%M") if slot.start else "??"
    return f"{start}|{slot.court}"


def choose_slot(
    slots: list[Slot],
    target: Target,
    court_preference: tuple[str, ...] = (),
) -> Slot | None:
    """Pick the best available slot.

    Preference is strictly ordered: the user's first listed time beats their
    second even if the second is on a nicer court. Court preference only breaks
    ties between slots at the same time.
    """
    available = [s for s in slots if s.available and s.start is not None]
    if not available:
        return None

    def court_rank(slot: Slot) -> int:
        if not court_preference:
            return 0
        for i, name in enumerate(court_preference):
            if name.lower() in slot.court.lower():
                return i
        return len(court_preference)

    for wanted in target.times:
        same_time = [s for s in available if s.start == wanted]
        if same_time:
            return sorted(same_time, key=court_rank)[0]
    return None


def next_actionable_release(
    config: Config, now: datetime, *, horizon_days: int = 21
) -> Plan | None:
    """The next release that opens a weekday the user actually wants."""
    window = config.window
    today_local = now.astimezone(window.tz).date()
    for offset in range(0, horizon_days):
        release_day = today_local + timedelta(days=offset)
        target_date = window.target_date_for(release_day)
        instant = window.release_instant_for(target_date)
        if instant < now.astimezone(timezone.utc):
            continue
        wanted = config.target_for_weekday(target_date.weekday())
        if wanted:
            return Plan(
                release_at=instant,
                target_date=target_date,
                target=wanted,
                is_today=(release_day == today_local),
            )
    return None


def sleep_until(clock: ClockSync, instant: datetime, *, spin_ms: float = 60.0) -> float:
    """Block until `instant` on the corrected clock; return overshoot in ms.

    Coarse sleeping is accurate to roughly a millisecond but can overshoot under
    load, so the last fraction is spun. Spinning the whole wait would burn a CPU
    for minutes, which on a laptop means fans and thermal throttling at the one
    moment we need the machine responsive.
    """
    spin = spin_ms / 1000.0
    while True:
        remaining = clock.seconds_until(instant)
        if remaining <= spin:
            break
        _time.sleep(min(remaining - spin, 30.0))
    while clock.seconds_until(instant) > 0:
        pass
    return -clock.seconds_until(instant) * 1000.0


class Sniper:
    def __init__(
        self,
        config: Config,
        recipe: Recipe,
        *,
        clock: ClockSync | None = None,
        client: BookingClient | None = None,
    ) -> None:
        self.config = config
        self.recipe = recipe
        self.clock = clock or ClockSync()
        self._client = client

    def _make_client(self) -> BookingClient:
        if self._client is not None:
            return self._client
        return BookingClient(
            self.recipe,
            timeout=self.config.attempts.request_timeout,
            dry_run=self.config.dry_run,
            extra_values={
                "club_id": self.config.club.club_id,
                "activity_id": self.config.club.activity_id,
                "duration": self.config.club.duration_minutes,
            },
        )

    def run(
        self,
        *,
        wait: bool = True,
        now: datetime | None = None,
        max_wait_seconds: float = 3_600.0,
    ) -> SnipeOutcome:
        """Plan, wait for the release, and take the best slot available.

        `max_wait_seconds` caps how long the process will block. Only one date
        opens per release, so if the user only wants Saturdays the next useful
        release can be a week away — and a process that sleeps for a week is a
        process that dies to a reboot or a closed laptop lid. Past the cap the
        run reports when to come back and exits, leaving the scheduling to
        launchd, which survives sleep and restarts.
        """
        problems = self.recipe.validate()
        if problems:
            raise RecipeError(
                "recipe is not usable yet:\n  - " + "\n  - ".join(problems)
            )

        self.clock.sync()
        current = now or self.clock.now()
        plan = next_actionable_release(self.config, current)
        if plan is None:
            return SnipeOutcome(
                booked=False, target_date=None,
                note="no upcoming release opens a weekday listed in `targets`",
                clock_offset=self.clock.offset, clock_synced=self.clock.synced,
            )

        log.info("plan: %s", plan.describe(self.config.window.tz))
        log.info("wanting %s in order",
                 ", ".join(t.strftime("%H:%M") for t in plan.target.times))

        seconds_out = self.clock.seconds_until(plan.release_at)
        if wait and seconds_out > max_wait_seconds:
            note = (
                f"next release that opens a wanted weekday is "
                f"{plan.describe(self.config.window.tz)} "
                f"({seconds_out / 3600:.1f}h away, over the {max_wait_seconds / 3600:.1f}h "
                f"wait cap). Run again nearer the time, or let the launchd job handle it."
            )
            log.info("%s", note)
            return SnipeOutcome(
                booked=False, target_date=plan.target_date, note=note,
                deferred=True, plan=plan,
                clock_offset=self.clock.offset, clock_synced=self.clock.synced,
            )

        client = self._make_client()
        creds = self.config.credentials()

        # Pre-warm well before the release: authenticate and open the TLS
        # connection so the first request at T-0 is the booking traffic itself.
        if wait and seconds_out > 120:
            log.info("release in %.0fs — sleeping until 90s out to log in", seconds_out)
            sleep_until(self.clock, plan.release_at - timedelta(seconds=90))

        client.prewarm()
        client.login(creds.username, creds.password)

        lag = 0.0
        if wait:
            fire_at = plan.release_at - timedelta(
                milliseconds=self.config.attempts.prefire_ms
            )
            remaining = self.clock.seconds_until(fire_at)
            if remaining > 0:
                log.info("armed; firing in %.2fs (prefire %dms)",
                         remaining, self.config.attempts.prefire_ms)
                lag = sleep_until(self.clock, fire_at)

        outcome = self._attack(client, plan)
        outcome.plan = plan
        outcome.fire_lag_ms = lag
        outcome.clock_offset = self.clock.offset
        outcome.clock_synced = self.clock.synced
        return outcome

    def _attack(self, client: BookingClient, plan: Plan) -> SnipeOutcome:
        policy = self.config.attempts
        outcome = SnipeOutcome(booked=False, target_date=plan.target_date)
        deadline = self.clock.now() + timedelta(seconds=policy.retry_for_seconds)
        tried_slot_ids: set[str] = set()
        relogins = 0

        while self.clock.now() < deadline:
            now = self.clock.now()
            try:
                slots = client.availability(plan.target_date)
            except AuthExpired as exc:
                # Pre-warming logs in ~90s early, so a short-lived token can
                # lapse before the release. Retrying a dead session never
                # recovers; one re-login does.
                if relogins >= 1:
                    outcome.attempts.append(
                        Attempt(now, None, None, f"session expired again: {exc}"))
                    outcome.note = "authentication kept failing"
                    return outcome
                relogins += 1
                log.warning("session expired — logging in again")
                outcome.attempts.append(Attempt(now, None, None, "re-authenticating"))
                try:
                    creds = self.config.credentials()
                    client.login(creds.username, creds.password)
                except Exception as relogin_exc:  # noqa: BLE001
                    outcome.note = f"could not re-authenticate: {relogin_exc}"
                    return outcome
                continue
            except Exception as exc:  # noqa: BLE001 - reported, then retried
                outcome.attempts.append(Attempt(now, None, None, f"availability error: {exc}"))
                log.warning("availability failed: %s", exc)
                _time.sleep(policy.retry_interval_ms / 1000.0)
                continue

            candidates = [s for s in slots if slot_key(s) not in tried_slot_ids]
            slot = choose_slot(candidates, plan.target, self.config.club.court_preference)
            if slot is None:
                free = sorted(
                    {s.start.strftime("%H:%M") for s in slots if s.available and s.start}
                )
                outcome.attempts.append(
                    Attempt(now, None, None,
                            f"none of the wanted times free (open: {', '.join(free) or 'none'})")
                )
                log.info("no wanted time free; open slots: %s", ", ".join(free) or "none")
                _time.sleep(policy.retry_interval_ms / 1000.0)
                continue

            log.info("attempting %s", slot.label())
            try:
                result = client.book(slot, plan.target_date)
            except Exception as exc:  # noqa: BLE001 - one bad slot must not end the run
                outcome.attempts.append(Attempt(now, slot, None, f"book error: {exc}"))
                tried_slot_ids.add(slot_key(slot))
                continue

            outcome.attempts.append(Attempt(now, slot, result))
            if result.ok:
                outcome.booked = True
                outcome.result = result
                log.info("SUCCESS: %s", result)
                return outcome

            log.warning("booking %s rejected: %s", slot.label(), result.detail)
            # Someone else took it, or it was never really free. Do not retry
            # the same slot: fall through to the next preference.
            tried_slot_ids.add(slot_key(slot))
            _time.sleep(policy.retry_interval_ms / 1000.0)

        outcome.note = f"gave up after {policy.retry_for_seconds:.0f}s"
        return outcome
