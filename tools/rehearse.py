"""Dress rehearsal: drive the real CLI end to end against the mock club.

The unit tests exercise the library; this exercises the thing you will actually
type. It stands up a mock club whose booking window opens in a few seconds,
records a HAR of a booking made against it, then runs `discover`, `plan`,
`check`, a dry run and a live snipe as subprocesses — the same commands, the
same config file, the same exit codes.

Worth running before your first real 08:00, and again after any change.

    python tools/rehearse.py
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

from mock_club import PASSWORD, USERNAME, ClubState, serve  # noqa: E402

from courtbot.timing import LONDON, BookingWindow  # noqa: E402

GREEN, RED, DIM, RESET = "\033[32m", "\033[31m", "\033[2m", "\033[0m"


def har_entry(response, started: datetime) -> dict:
    body = response.request.body
    if isinstance(body, bytes):
        body = body.decode()
    return {
        "startedDateTime": started.isoformat().replace("+00:00", "Z"),
        "_resourceType": "xhr",
        "request": {
            "method": response.request.method,
            "url": response.request.url,
            "headers": [{"name": k.lower(), "value": v}
                        for k, v in response.request.headers.items()],
            **({"postData": {"mimeType": "application/json", "text": body}} if body else {}),
        },
        "response": {"status": response.status_code,
                     "content": {"mimeType": "application/json", "text": response.text}},
    }


def record_manual_booking(base: str, state, open_date, har_path: Path, *, basket: bool) -> None:
    """Script the booking a human would make during `discover`, as a HAR."""
    entries, session = [], requests.Session()

    def go(response):
        entries.append(har_entry(response, datetime.now(timezone.utc)))
        return response

    token = go(session.post(f"{base}/api/v2/auth/login",
               json={"username": USERNAME, "password": PASSWORD})
               ).json()["data"]["accessToken"]
    session.headers["authorization"] = f"Bearer {token}"
    go(session.get(f"{base}/api/v2/availability", params={"date": open_date.isoformat()}))

    slot = state.slot_id(open_date, 10, "Court 3")
    if basket:
        bid = go(session.post(f"{base}/api/v2/basket", json={"slotId": slot})
                 ).json()["data"]["basketId"]
        go(session.post(f"{base}/api/v2/basket/{bid}/checkout", json={}))
    else:
        go(session.post(f"{base}/api/v2/bookings", json={
            "slotId": slot, "date": open_date.isoformat(), "startTime": "10:00"}))

    har_path.parent.mkdir(parents=True, exist_ok=True)
    har_path.write_text(json.dumps({"log": {"version": "1.2", "entries": entries}}))


def write_config(path: Path, base: str, window: BookingWindow, target, recipe: Path) -> None:
    weekday = ["monday", "tuesday", "wednesday", "thursday",
               "friday", "saturday", "sunday"][target.weekday()]
    path.write_text(f"""
club:
  name: "Rehearsal Club"
  activity: tennis
  duration_minutes: 60
window:
  advance_days: {window.advance_days}
  release_time: "{window.release_time:%H:%M:%S}"
  timezone: Europe/London
targets:
  - weekday: {weekday}
    times: ["19:00", "10:00"]
attempts:
  prefire_ms: 200
  retry_for_seconds: 10
  retry_interval_ms: 200
base_url: "{base}"
recipe_path: "{recipe}"
notify: false
""".lstrip())


def run(label: str, args: list[str], *, expect: int = 0, env=None) -> bool:
    t0 = datetime.now(timezone.utc)
    print(f"\n{DIM}[{t0:%H:%M:%S}] $ python -m courtbot {' '.join(args)}{RESET}")
    proc = subprocess.run(
        [sys.executable, "-m", "courtbot", *args],
        cwd=ROOT, capture_output=True, text=True, env=env, timeout=180,
    )
    out = (proc.stdout + proc.stderr).rstrip()
    for line in out.splitlines():
        print(f"  {line}")
    ok = proc.returncode == expect
    took = (datetime.now(timezone.utc) - t0).total_seconds()
    print(f"  {GREEN + 'PASS' if ok else RED + 'FAIL'}{RESET} "
          f"{label} (exit {proc.returncode}, expected {expect}, {took:.1f}s)")
    return ok


def one_round(label: str, *, dry: bool, basket: bool, seconds_out: float,
              env: dict) -> list[tuple[str, bool]]:
    """Stand up a fresh mock and drive one release through the CLI.

    Each round gets its own club because a release happens once. A dry run and
    a live run therefore cannot share one — the dry run would consume the
    release and leave the live run deferring to next week.
    """
    now_lon = datetime.now(LONDON)
    release_wall = (now_lon + timedelta(seconds=seconds_out)).time().replace(microsecond=0)
    window = BookingWindow(advance_days=9, release_time=release_wall, tz=LONDON)
    target = now_lon.date() + timedelta(days=9)
    open_date = now_lon.date() + timedelta(days=8)

    state = ClubState(window.release_instant_for(target), advance_days=9)
    server = serve(state, port=0)
    base = f"http://127.0.0.1:{server.server_address[1]}"

    t_start = datetime.now(timezone.utc)
    print(f"\n{'=' * 58}\n  ROUND: {label}")
    print(f"  club     {base}")
    print(f"  release  {window.release_instant_for(target).astimezone(LONDON):%H:%M:%S}"
          f"  for {target:%a %d %b}")
    print("=" * 58)

    results: list[tuple[str, bool]] = []
    try:
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            har, recipe, config = tmp / "session.har", tmp / "recipe.json", tmp / "config.yaml"
            record_manual_booking(base, state, open_date, har, basket=basket)
            write_config(config, base, window, target, recipe)

            results.append(("discover builds a recipe from the HAR", run("discover", [
                "discover", "--har", str(har), "--out", str(tmp),
                "--booked-date", open_date.isoformat(), "--booked-time", "10:00",
                "--base-url", base], env=env)))
            if basket:
                chain = json.loads(recipe.read_text()).get("book_chain") or []
                results.append(("the basket flow was captured as a chain", len(chain) == 2))

            results.append(("plan reports the next release", run(
                "plan", ["--no-ntp", "plan", "--config", str(config)], env=env)))
            results.append(("check lists live availability", run("check", [
                "--no-ntp", "check", "--config", str(config),
                "--date", open_date.isoformat()], env=env)))

            baseline = len(state.booking_log)
            snipe_args = ["--no-ntp", "snipe", "--config", str(config)]
            if dry:
                snipe_args.append("--dry-run")
            results.append((
                f"snipe waits for the release and {'reports' if dry else 'books'}",
                run("snipe", snipe_args, env=env),
            ))

            if dry:
                results.append(("the dry run left the club untouched",
                                len(state.booking_log) == baseline))
            else:
                confirmed = [b for b in state.booking_log
                             if b["slot"].startswith(target.isoformat())]
                results.append((f"a booking was confirmed for {target:%a %d %b}",
                                bool(confirmed)))
                if confirmed:
                    print(f"\n  {DIM}club recorded: {confirmed[0]['ref']} "
                          f"-> {confirmed[0]['slot']}{RESET}")
    finally:
        server.shutdown()
    return results


def main() -> int:
    ap = argparse.ArgumentParser(description="Rehearse the CLI against a mock club")
    ap.add_argument("--basket", action="store_true",
                    help="rehearse a two-step basket flow instead of a single request")
    ap.add_argument("--seconds-out", type=float, default=30.0,
                    help="seconds until each round's mock release")
    args = ap.parse_args()

    env = {**dict(os.environ), "DL_USERNAME": USERNAME, "DL_PASSWORD": PASSWORD}
    print(f"flow: {'basket (2 steps)' if args.basket else 'single request'}")

    results = one_round("dry run — everything except the booking",
                        dry=True, basket=args.basket,
                        seconds_out=args.seconds_out, env=env)
    results += one_round("live — books for real against the mock",
                         dry=False, basket=args.basket,
                         seconds_out=args.seconds_out, env=env)

    print("\n" + "-" * 58)
    for label, ok in results:
        print(f"  {GREEN + 'PASS' if ok else RED + 'FAIL'}{RESET}  {label}")
    failed = [l for l, ok in results if not ok]
    print("-" * 58)
    if failed:
        print(f"{RED}{len(failed)} step(s) failed{RESET}")
        return 1
    print(f"{GREEN}Rehearsal passed — the CLI works end to end.{RESET}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
