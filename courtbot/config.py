"""Configuration loading and validation.

Everything site-specific lives in YAML so the bot can be re-pointed at another
club, activity or release time without code changes. Credentials never appear
in the file: they are read from the environment (or the macOS keychain via a
launchd wrapper) and the loader rejects a config that tries to inline them.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from datetime import time
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml

from .timing import BookingWindow

WEEKDAYS = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
}

_TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")

# Keys that must never be committed to the config file.
_FORBIDDEN_KEYS = {"password", "passwd", "secret", "token", "pin"}


class ConfigError(ValueError):
    """Raised for a config the bot cannot act on. Message is user-facing."""


def _parse_time(raw: object, where: str) -> time:
    if not isinstance(raw, str) or not _TIME_RE.match(raw.strip()):
        raise ConfigError(f"{where}: expected a 24-hour time like '08:00', got {raw!r}")
    hh, mm = raw.strip().split(":")
    return time(int(hh), int(mm))


@dataclass(frozen=True)
class Target:
    """A day-of-week the user wants, and the start times to try, best first."""

    weekday: int
    weekday_name: str
    times: tuple[time, ...]

    def __post_init__(self) -> None:
        if not self.times:
            raise ConfigError(f"target {self.weekday_name}: needs at least one time")


@dataclass(frozen=True)
class AttemptPolicy:
    """How hard to push at the release moment.

    `prefire_ms` sends the first request slightly *before* the release instant so
    it lands at the server just as the window opens, absorbing network latency.
    Kept small: too eager and the server rejects it as early, wasting the slot.

    Attempts are deliberately sequential. Firing booking requests in parallel
    risks two of them succeeding, which leaves you holding two courts and
    probably a cancellation fee; and it would put load on the club's servers
    without winning slots any sooner, since the contended resource is the slot,
    not the connection. Speed comes from the pre-warmed connection and the
    prefire, not from volume.
    """

    prefire_ms: int = 250
    retry_for_seconds: float = 45.0
    retry_interval_ms: int = 400
    request_timeout: float = 10.0

    def __post_init__(self) -> None:
        if not 0 <= self.prefire_ms <= 5_000:
            raise ConfigError("attempts.prefire_ms must be between 0 and 5000")
        if self.retry_for_seconds <= 0:
            raise ConfigError("attempts.retry_for_seconds must be positive")
        if self.retry_interval_ms < 50:
            raise ConfigError(
                "attempts.retry_interval_ms below 50 would poll the club's API "
                "more than 20x a second; keep it at 50 or above"
            )


@dataclass(frozen=True)
class Club:
    name: str
    activity: str = "tennis"
    duration_minutes: int = 60
    club_id: str = ""            # discovered, not guessed
    activity_id: str = ""        # discovered, not guessed
    court_preference: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.duration_minutes not in (30, 60, 90, 120):
            raise ConfigError("club.duration_minutes must be 30, 60, 90 or 120")


@dataclass(frozen=True)
class Credentials:
    username: str
    password: str

    @staticmethod
    def from_env(user_var: str, pass_var: str) -> "Credentials":
        user = os.environ.get(user_var, "").strip()
        pwd = os.environ.get(pass_var, "")
        if not user or not pwd:
            raise ConfigError(
                f"Missing credentials. Set {user_var} and {pass_var} in the "
                f"environment (see README: 'Credentials'). They are never read "
                f"from the config file."
            )
        return Credentials(user, pwd)


@dataclass(frozen=True)
class Config:
    club: Club
    window: BookingWindow
    targets: tuple[Target, ...]
    attempts: AttemptPolicy
    base_url: str
    recipe_path: Path
    state_path: Path
    username_env: str = "DL_USERNAME"
    password_env: str = "DL_PASSWORD"
    notify: bool = True
    dry_run: bool = False
    extras: dict = field(default_factory=dict)

    def credentials(self) -> Credentials:
        return Credentials.from_env(self.username_env, self.password_env)

    def target_for_weekday(self, weekday: int) -> Target | None:
        for t in self.targets:
            if t.weekday == weekday:
                return t
        return None


def _reject_inline_secrets(raw: dict, path: str = "") -> None:
    for key, value in raw.items():
        here = f"{path}.{key}" if path else str(key)
        if str(key).lower() in _FORBIDDEN_KEYS and value:
            raise ConfigError(
                f"{here} is set in the config file. Credentials must come from "
                f"the environment instead — remove it and export "
                f"DL_USERNAME / DL_PASSWORD (see README)."
            )
        if isinstance(value, dict):
            _reject_inline_secrets(value, here)


def load(path: str | Path) -> Config:
    path = Path(path)
    if not path.exists():
        raise ConfigError(
            f"No config at {path}. Copy config.example.yaml to {path.name} and "
            f"edit it."
        )
    raw = yaml.safe_load(path.read_text()) or {}
    if not isinstance(raw, dict):
        raise ConfigError(f"{path}: expected a YAML mapping at the top level")
    _reject_inline_secrets(raw)

    club_raw = raw.get("club") or {}
    if not club_raw.get("name"):
        raise ConfigError("club.name is required")
    club = Club(
        name=str(club_raw["name"]),
        activity=str(club_raw.get("activity", "tennis")).lower(),
        duration_minutes=int(club_raw.get("duration_minutes", 60)),
        club_id=str(club_raw.get("club_id", "") or ""),
        activity_id=str(club_raw.get("activity_id", "") or ""),
        court_preference=tuple(str(c) for c in club_raw.get("court_preference", [])),
    )

    win_raw = raw.get("window") or {}
    tz_name = str(win_raw.get("timezone", "Europe/London"))
    try:
        tz = ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ConfigError(f"window.timezone: unknown zone {tz_name!r}") from exc
    window = BookingWindow(
        advance_days=int(win_raw.get("advance_days", 9)),
        release_time=_parse_time(win_raw.get("release_time", "08:00"), "window.release_time"),
        tz=tz,
    )
    if not 1 <= window.advance_days <= 30:
        raise ConfigError("window.advance_days must be between 1 and 30")

    targets_raw = raw.get("targets") or []
    if not targets_raw:
        raise ConfigError(
            "targets is empty — add at least one weekday and the start times to "
            "try, e.g.\n  targets:\n    - weekday: saturday\n      times: ['10:00', '11:00']"
        )
    targets: list[Target] = []
    seen: set[int] = set()
    for i, t in enumerate(targets_raw):
        name = str(t.get("weekday", "")).strip().lower()
        if name not in WEEKDAYS:
            raise ConfigError(
                f"targets[{i}].weekday: {name!r} is not a weekday name"
            )
        if WEEKDAYS[name] in seen:
            raise ConfigError(
                f"targets[{i}]: {name} appears twice — merge the times into one entry"
            )
        seen.add(WEEKDAYS[name])
        times = tuple(
            _parse_time(x, f"targets[{i}].times[{j}]")
            for j, x in enumerate(t.get("times") or [])
        )
        targets.append(Target(WEEKDAYS[name], name, times))

    attempts = AttemptPolicy(**(raw.get("attempts") or {}))

    base_url = str(raw.get("base_url", "")).strip().rstrip("/")
    if base_url and not base_url.startswith("https://"):
        raise ConfigError("base_url must be https://")

    return Config(
        club=club,
        window=window,
        targets=tuple(targets),
        attempts=attempts,
        base_url=base_url,
        recipe_path=Path(raw.get("recipe_path", "captured/recipe.json")),
        state_path=Path(raw.get("state_path", "captured/storage_state.json")),
        username_env=str(raw.get("username_env", "DL_USERNAME")),
        password_env=str(raw.get("password_env", "DL_PASSWORD")),
        notify=bool(raw.get("notify", True)),
        dry_run=bool(raw.get("dry_run", False)),
        extras=raw.get("extras") or {},
    )
