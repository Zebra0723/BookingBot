"""Command line entry point."""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time as _time
from datetime import date, datetime, time, timedelta
from pathlib import Path

from . import notify
from .config import Config, ConfigError, load as load_config
from . import doctor as doctor_mod
from .discover import DiscoveryError, analyse
from .http_client import BookingClient
from .recipe import Recipe, RecipeError
from .sniper import Sniper, choose_slot, next_actionable_release
from .timing import ClockSync

LOG_FORMAT = "%(asctime)s  %(levelname)-7s %(message)s"


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format=LOG_FORMAT,
        datefmt="%H:%M:%S",
    )


def _clock(args) -> ClockSync:
    return ClockSync(enabled=not getattr(args, "no_ntp", False))


def _parse_date(raw: str) -> date:
    try:
        return date.fromisoformat(raw)
    except ValueError:
        raise SystemExit(f"bad date {raw!r}; expected YYYY-MM-DD")


def _parse_time(raw: str) -> time:
    try:
        hh, mm = raw.split(":")
        return time(int(hh), int(mm))
    except (ValueError, TypeError):
        raise SystemExit(f"bad time {raw!r}; expected HH:MM")


# --- commands ---------------------------------------------------------------

def cmd_discover(args) -> int:
    out_dir = Path(args.out)
    har_path = Path(args.har)
    booked_date = _parse_date(args.booked_date) if args.booked_date else None
    booked_time = _parse_time(args.booked_time) if args.booked_time else None
    if booked_date is None:
        print(
            "\n  NOTE: without --booked-date the date cannot be turned into a\n"
            "  placeholder, so the recipe will only ever re-book that one day.\n"
            "  Re-run with --booked-date YYYY-MM-DD --booked-time HH:MM.\n",
            file=sys.stderr,
        )

    result = analyse(
        har_path, out_dir=out_dir,
        booked_date=booked_date, booked_time=booked_time,
        base_url=args.base_url or "",
    )
    print("\n".join("  " + line for line in result.report))
    problems = result.recipe.validate()
    print(f"\n  recipe -> {result.recipe_path}")
    print(f"  har    -> {result.har_path}")
    if result.secrets_path:
        names = ", ".join(f"DL_{n.upper()}" for n in sorted(result.secrets))
        print(f"  secret -> {result.secrets_path}  ({names}, mode 0600)")
        print(f"            load it before running:  source {result.secrets_path}")
    if problems:
        print("\n  NOT READY:")
        for p in problems:
            print(f"    - {p}")
        return 1
    print("\n  Recipe looks complete. Verify it with:  courtbot check --date YYYY-MM-DD")
    return 0


def cmd_doctor(args) -> int:
    """Report what a capture contains before a recipe is built from it."""
    from . import har as har_reader

    exchanges = har_reader.parse(Path(args.har))
    findings = doctor_mod.diagnose(
        exchanges,
        booked_date=_parse_date(args.booked_date) if args.booked_date else None,
        booked_time=_parse_time(args.booked_time) if args.booked_time else None,
    )
    print()
    for f in findings:
        print(f.render())
        print()
    usable, summary = doctor_mod.verdict(findings)
    print(f"  {summary}\n")
    return 0 if usable else 1


def cmd_plan(args) -> int:
    cfg = load_config(args.config)
    clock = _clock(args)
    clock.sync()
    now = clock.now()
    plan = next_actionable_release(cfg, now)
    print(f"  now:            {now.astimezone(cfg.window.tz):%a %d %b %Y %H:%M:%S %Z}")
    print(f"  clock offset:   {clock.offset:+.3f}s "
          f"({'NTP-synced' if clock.synced else 'NOT synced — using local clock'})")
    print(f"  window:         {cfg.window.advance_days} days, "
          f"release {cfg.window.release_time:%H:%M} {cfg.window.tz}")
    if plan is None:
        print("  next action:    none — no upcoming release opens a weekday in `targets`")
        return 1
    secs = clock.seconds_until(plan.release_at)
    print(f"  next release:   {plan.describe(cfg.window.tz)}")
    print(f"  that is:        {secs / 3600:.1f}h away")
    print(f"  will try:       {', '.join(t.strftime('%H:%M') for t in plan.target.times)}")
    if cfg.club.court_preference:
        print(f"  court order:    {', '.join(cfg.club.court_preference)}")
    return 0


def cmd_check(args) -> int:
    cfg = load_config(args.config)
    recipe = Recipe.load(cfg.recipe_path)
    problems = recipe.validate()
    if problems:
        print("  recipe problems:")
        for p in problems:
            print(f"    - {p}")
        return 1
    target = _parse_date(args.date) if args.date else cfg.window.target_date_for(date.today())
    client = BookingClient(recipe, timeout=cfg.attempts.request_timeout, extra_values={
        "club_id": cfg.club.club_id, "activity_id": cfg.club.activity_id,
        "duration": cfg.club.duration_minutes,
    })
    client.login(cfg.credentials())
    slots = client.availability(target)
    free = [s for s in slots if s.available]
    print(f"\n  {target:%a %d %b %Y}: {len(slots)} slots, {len(free)} free")
    for s in free[: args.limit]:
        print(f"    {s.label():24} id={s.slot_id}")
    if len(free) > args.limit:
        print(f"    … and {len(free) - args.limit} more")
    wanted = cfg.target_for_weekday(target.weekday())
    if wanted:
        pick = choose_slot(slots, wanted, cfg.club.court_preference)
        print(f"\n  would book: {pick.label() if pick else 'nothing (no wanted time free)'}")
    else:
        print(f"\n  note: {target:%A} is not in your `targets`, so the bot would skip it")
    return 0


def cmd_snipe(args) -> int:
    cfg = load_config(args.config)
    if args.dry_run:
        cfg = Config(**{**cfg.__dict__, "dry_run": True})
    recipe = Recipe.load(cfg.recipe_path)
    sniper = Sniper(cfg, recipe, clock=_clock(args))
    outcome = sniper.run(wait=not args.now, max_wait_seconds=args.max_wait * 3600)
    print(f"\n  {outcome.summary()}")
    if outcome.attempts:
        print(f"  fire lag {outcome.fire_lag_ms:+.1f}ms, "
              f"clock offset {outcome.clock_offset:+.3f}s")

    notify.announce(outcome, enabled=cfg.notify)
    return 0 if (outcome.booked or outcome.deferred) else 1


def cmd_calibrate(args) -> int:
    """Measure when slots actually appear.

    The published release time varies by club and David Lloyd has changed it
    before. Rather than trust a number, this watches the boundary and reports
    the instant the window really opened.
    """
    cfg = load_config(args.config)
    recipe = Recipe.load(cfg.recipe_path)
    clock = _clock(args)
    clock.sync()
    plan = next_actionable_release(cfg, clock.now(), horizon_days=args.horizon)
    if plan is None:
        print("  no upcoming release to calibrate against")
        return 1

    # Calibrate against the next date to open, whatever weekday it is.
    target = cfg.window.target_date_for(clock.now().astimezone(cfg.window.tz).date())
    expected = cfg.window.release_instant_for(target)
    start = expected - timedelta(minutes=args.before)
    stop = expected + timedelta(minutes=args.after)
    print(f"  watching {target:%a %d %b} from {start.astimezone(cfg.window.tz):%H:%M:%S} "
          f"to {stop.astimezone(cfg.window.tz):%H:%M:%S} "
          f"(expected {expected.astimezone(cfg.window.tz):%H:%M:%S})")

    client = BookingClient(recipe, timeout=cfg.attempts.request_timeout, extra_values={
        "club_id": cfg.club.club_id, "activity_id": cfg.club.activity_id,
        "duration": cfg.club.duration_minutes,
    })
    client.login(cfg.credentials())

    while clock.now() < start:
        _time.sleep(min(30.0, max(0.5, clock.seconds_until(start))))

    last = 0
    while clock.now() < stop:
        now = clock.now()
        try:
            free = sum(1 for s in client.availability(target) if s.available)
        except Exception as exc:  # noqa: BLE001
            free = -1
            logging.debug("poll failed: %s", exc)
        if free != last:
            delta = (now - expected).total_seconds()
            print(f"    {now.astimezone(cfg.window.tz):%H:%M:%S.%f}"[:-3] +
                  f"  free={free:<4} ({delta:+.1f}s vs expected)")
            if last == 0 and free > 0:
                print(f"\n  >>> window opened at "
                      f"{now.astimezone(cfg.window.tz):%H:%M:%S}, "
                      f"{delta:+.1f}s from your configured release_time.")
                if abs(delta) > 2:
                    print(f"  >>> update window.release_time in {args.config}")
                return 0
            last = free
        _time.sleep(args.interval)
    print("  window did not open during the watch period")
    return 1


def cmd_install(args) -> int:
    cfg = load_config(args.config)
    lead = timedelta(minutes=args.lead)
    fire = (datetime.combine(date.today(), cfg.window.release_time) - lead).time()
    project = Path.cwd().resolve()
    python = args.python or sys.executable
    label = args.label
    env_file = Path(args.env_file)

    # Secrets are sourced from one protected file rather than copied into the
    # plist. A plist in ~/Library/LaunchAgents is world-readable by default, and
    # duplicating a password into it means two places to rotate and one to
    # forget. Sourcing also covers refresh-token auth, where the secret is
    # written by `discover` and never typed at all.
    vars_needed = ", ".join(
        v for v in (cfg.username_env, cfg.password_env, cfg.refresh_token_env) if v
    )
    command = (
        f"cd {_sh(project)} && "
        f"[ -f {_sh(env_file)} ] && . {_sh(env_file)}; "
        f"exec {_sh(python)} -m courtbot snipe --config {_sh(project / args.config)}"
    )

    plist = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>{label}</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/zsh</string>
    <string>-c</string>
    <string>{_xml(command)}</string>
  </array>
  <key>WorkingDirectory</key><string>{project}</string>
  <key>StartCalendarInterval</key>
  <dict>
    <key>Hour</key><integer>{fire.hour}</integer>
    <key>Minute</key><integer>{fire.minute}</integer>
  </dict>
  <key>RunAtLoad</key><false/>
  <key>StandardOutPath</key><string>{project}/captured/launchd.out.log</string>
  <key>StandardErrorPath</key><string>{project}/captured/launchd.err.log</string>
</dict>
</plist>
"""
    out = Path(args.output or f"{label}.plist")
    out.write_text(plist)
    print(f"  wrote {out}")
    print(f"\n  Fires at {fire:%H:%M} local time ({args.lead} min before the "
          f"{cfg.window.release_time:%H:%M} release).")
    print(f"  Reads secrets from {env_file} — no credentials are stored in the plist.")

    if env_file.exists():
        print(f"  That file exists. Make sure it exports: {vars_needed}")
    else:
        print(f"\n  {env_file} does not exist yet. Create it with whichever your")
        print(f"  captured sign-in uses, then lock it down:")
        print(f"      mkdir -p {env_file.parent}")
        print(f"      cat > {env_file} <<'EOF'")
        print(f"      export {cfg.username_env}='you@example.com'")
        print(f"      export {cfg.password_env}='your-password'")
        print(f"      EOF")
        print(f"      chmod 600 {env_file}")

    print("\n  To install:")
    print(f"    cp {out} ~/Library/LaunchAgents/{label}.plist")
    print(f"    launchctl load ~/Library/LaunchAgents/{label}.plist")
    print("\n  Let the Mac wake for it, or it will miss the release:")
    print(f"    sudo pmset repeat wakeorpoweron MTWRFSU {fire:%H:%M}:00")
    print("    System Settings > Battery > Options > 'Wake for network access'")
    return 0


def _sh(value) -> str:
    """Single-quote a path for embedding in a shell command."""
    text = str(value)
    return "'" + text.replace("'", "'\\''") + "'"


def _xml(text: str) -> str:
    return (text.replace("&", "&amp;").replace("<", "&lt;")
                .replace(">", "&gt;").replace('"', "&quot;"))


# --- wiring -----------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="courtbot",
        description="Books a court the moment the booking window opens.",
    )
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument("--no-ntp", action="store_true",
                   help="skip NTP sync and trust the local clock")
    sub = p.add_subparsers(dest="command", required=True)

    d = sub.add_parser("discover", help="build the recipe from a captured session")
    d.add_argument("--har", required=True,
                   help="HAR of the app session in which you booked a court")
    d.add_argument("--out", default="captured", help="output directory")
    d.add_argument("--booked-date", help="the date you booked, YYYY-MM-DD")
    d.add_argument("--booked-time", help="the time you booked, HH:MM")
    d.add_argument("--base-url", help="override the inferred API origin")
    d.set_defaults(func=cmd_discover)

    doc = sub.add_parser("doctor", help="check whether a capture is usable")
    doc.add_argument("--har", required=True, help="the captured session to inspect")
    doc.add_argument("--booked-date", help="the date you booked, YYYY-MM-DD")
    doc.add_argument("--booked-time", help="the time you booked, HH:MM")
    doc.set_defaults(func=cmd_doctor)

    pl = sub.add_parser("plan", help="show the next release without doing anything")
    pl.add_argument("--config", default="config.yaml")
    pl.set_defaults(func=cmd_plan)

    c = sub.add_parser("check", help="log in and list availability, to verify the recipe")
    c.add_argument("--config", default="config.yaml")
    c.add_argument("--date", help="YYYY-MM-DD (default: furthest bookable date)")
    c.add_argument("--limit", type=int, default=20)
    c.set_defaults(func=cmd_check)

    s = sub.add_parser("snipe", help="wait for the release and book")
    s.add_argument("--config", default="config.yaml")
    s.add_argument("--now", action="store_true", help="do not wait; attempt immediately")
    s.add_argument("--dry-run", action="store_true", help="do everything except book")
    s.add_argument("--max-wait", type=float, default=1.0,
                   help="hours to block waiting for a release (default 1)")
    s.set_defaults(func=cmd_snipe)

    cal = sub.add_parser("calibrate", help="measure the real release time")
    cal.add_argument("--config", default="config.yaml")
    cal.add_argument("--before", type=float, default=3.0, help="minutes to start early")
    cal.add_argument("--after", type=float, default=5.0, help="minutes to keep watching")
    cal.add_argument("--interval", type=float, default=1.0, help="poll seconds")
    cal.add_argument("--horizon", type=int, default=21)
    cal.set_defaults(func=cmd_calibrate)

    i = sub.add_parser("install-launchd", help="write a macOS launchd job")
    i.add_argument("--config", default="config.yaml")
    i.add_argument("--lead", type=int, default=3, help="minutes before release to start")
    i.add_argument("--label", default="com.courtbot.chelsea")
    i.add_argument("--python", help="python interpreter to use")
    i.add_argument("--output", help="where to write the plist")
    i.add_argument("--env-file", default="captured/secrets.env",
                   help="file the job sources its secrets from")
    i.set_defaults(func=cmd_install)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    _setup_logging(args.verbose)
    try:
        return args.func(args)
    except (ConfigError, RecipeError, DiscoveryError) as exc:
        print(f"\n  error: {exc}\n", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\n  interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
