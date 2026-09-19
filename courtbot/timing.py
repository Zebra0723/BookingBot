"""Clock and booking-window arithmetic.

Court release is a race: slots for a date appear at a fixed wall-clock moment
and the good ones are gone within seconds. Two things therefore matter here and
are kept free of any network or site specifics so they can be tested directly:

  1. Which date opens at the next release, given the club's advance window.
  2. The exact UTC instant of that release, correct across BST/GMT.

The local clock is not trusted. `ClockSync` measures drift against NTP so the
bot fires on true time rather than on a machine that is half a second slow.
"""

from __future__ import annotations

import logging
import socket
import statistics
import struct
import time as _time
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)

LONDON = ZoneInfo("Europe/London")

# NTP counts seconds from 1900; UNIX counts from 1970.
_NTP_EPOCH_DELTA = 2_208_988_800


@dataclass(frozen=True)
class BookingWindow:
    """The club's booking rules.

    Harbour Club Chelsea publishes "9 days in advance online". Booking on a
    Monday reaches the following Wednesday, which is exactly +9 days, so the
    advance count includes neither endpoint adjustment nor a rounding rule.

    The release *time* differs by club and David Lloyd has moved it before, so
    it stays configurable. `courtbot calibrate` measures the real one.
    """

    advance_days: int = 9
    release_time: time = time(8, 0)
    tz: ZoneInfo = LONDON

    def target_date_for(self, release_day: date) -> date:
        """The furthest date that becomes bookable at `release_day`'s release."""
        return release_day + timedelta(days=self.advance_days)

    def release_day_for(self, target: date) -> date:
        """The day whose release opens `target`."""
        return target - timedelta(days=self.advance_days)

    def release_instant_for(self, target: date) -> datetime:
        """The exact UTC instant at which `target` becomes bookable."""
        wall = datetime.combine(self.release_day_for(target), self.release_time)
        return _to_utc(wall, self.tz)

    def next_release(self, now: datetime) -> tuple[datetime, date]:
        """The next release instant at or after `now`, and the date it opens.

        Returns the release we can still act on. If today's release has already
        passed, this is tomorrow's, which opens the day after the furthest date
        currently bookable.
        """
        now_utc = now.astimezone(timezone.utc)
        today_local = now_utc.astimezone(self.tz).date()

        # Walk forward from today; one step is enough in practice, but looping
        # keeps this correct if a release instant is skipped by a DST gap.
        for offset in range(0, 3):
            release_day = today_local + timedelta(days=offset)
            target = self.target_date_for(release_day)
            instant = self.release_instant_for(target)
            if instant >= now_utc:
                return instant, target
        raise RuntimeError("no upcoming release found")  # pragma: no cover

    def is_bookable(self, target: date, now: datetime) -> bool:
        """Whether `target` is inside the open booking window at `now`."""
        now_utc = now.astimezone(timezone.utc)
        if target < now_utc.astimezone(self.tz).date():
            return False
        return self.release_instant_for(target) <= now_utc


def _to_utc(wall: datetime, tz: ZoneInfo) -> datetime:
    """Attach `tz` to a naive wall-clock time and convert to UTC.

    Handles the two awkward cases so a DST weekend cannot silently move a
    release by an hour:

    - Gap (clocks spring forward): the wall time does not exist. Round-tripping
      through UTC exposes this, and we take the first instant after the gap.
    - Fold (clocks fall back): the wall time happens twice. `fold=0` picks the
      first, i.e. still on summer time, which is the earlier of the two.
    """
    aware = wall.replace(tzinfo=tz, fold=0)
    utc = aware.astimezone(timezone.utc)
    if utc.astimezone(tz).replace(tzinfo=None) != wall:
        # Non-existent wall time. Its UTC image lands after the gap; use that.
        shifted = (wall + timedelta(hours=1)).replace(tzinfo=tz, fold=0)
        return shifted.astimezone(timezone.utc)
    return utc


def sntp_offset(server: str = "pool.ntp.org", timeout: float = 3.0) -> float:
    """Seconds to add to the local clock to get true time.

    A single SNTP round trip. Raises OSError if the server cannot be reached;
    callers decide whether to degrade to the bare local clock.
    """
    packet = bytearray(48)
    packet[0] = 0x1B  # LI=0, VN=3, Mode=3 (client)

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(timeout)
    try:
        t0 = _time.time()
        sock.sendto(bytes(packet), (server, 123))
        data, _ = sock.recvfrom(48)
        t3 = _time.time()
    finally:
        sock.close()

    if len(data) < 48:
        raise OSError(f"short NTP reply: {len(data)} bytes")

    recv_s, recv_f = struct.unpack("!II", data[32:40])
    xmit_s, xmit_f = struct.unpack("!II", data[40:48])
    t1 = (recv_s - _NTP_EPOCH_DELTA) + recv_f / 2**32
    t2 = (xmit_s - _NTP_EPOCH_DELTA) + xmit_f / 2**32

    return ((t1 - t0) + (t2 - t3)) / 2


@dataclass
class ClockSync:
    """Local clock corrected against NTP.

    Sampling several times and taking the median discards the one round trip
    that happened to hit a slow path. `offset` is additive: true = local +
    offset. When NTP is unreachable the offset stays 0.0 and `synced` is False,
    which the caller should surface rather than silently trusting the clock.

    Syncing is hard-bounded by `budget`. An unreachable NTP server costs one
    timeout per sample, and spending that on the approach to a release is far
    worse than running on a slightly imprecise clock: being 20ms early is a
    rounding error, being 12 seconds late loses the court.
    """

    server: str = "pool.ntp.org"
    samples: int = 4
    timeout: float = 1.5
    budget: float = 5.0
    enabled: bool = True
    offset: float = 0.0
    synced: bool = False
    spread: float = 0.0
    _measurements: list[float] = field(default_factory=list)

    def sync(self) -> "ClockSync":
        if not self.enabled:
            log.debug("clock sync disabled; using the local clock as-is")
            return self
        got: list[float] = []
        started = _time.monotonic()
        for _ in range(self.samples):
            if _time.monotonic() - started > self.budget:
                log.debug("NTP budget of %.1fs spent; stopping early", self.budget)
                break
            try:
                got.append(sntp_offset(self.server, timeout=self.timeout))
            except OSError as exc:
                log.debug("NTP sample failed: %s", exc)
        self._measurements = got
        if got:
            self.offset = statistics.median(got)
            self.spread = max(got) - min(got)
            self.synced = True
            log.info(
                "clock synced: offset %+.3fs (spread %.3fs, %d/%d samples)",
                self.offset, self.spread, len(got), self.samples,
            )
        else:
            self.offset = 0.0
            self.synced = False
            log.warning(
                "NTP unreachable (%s); firing on the uncorrected local clock",
                self.server,
            )
        return self

    def now(self) -> datetime:
        """True current time, UTC."""
        return datetime.fromtimestamp(_time.time() + self.offset, tz=timezone.utc)

    def seconds_until(self, instant: datetime) -> float:
        return (instant.astimezone(timezone.utc) - self.now()).total_seconds()
