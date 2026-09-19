# courtbot

Books a court at **Harbour Club Chelsea** the moment the booking window opens.

Courts release 9 days ahead at a fixed time each morning and the good slots go
within seconds. This sits on that moment: it wakes before the release, holds an
authenticated connection open, fires the instant the window opens, and walks
down your list of preferred times until something sticks.

---

## Why this does not drive the app

Harbour Club / David Lloyd ships an iOS app, and on an Apple Silicon Mac the
Mac App Store version is that same iOS app running in compatibility mode. Those
apps are close to un-automatable from macOS: they expose almost nothing to the
Accessibility APIs, so AppleScript and friends see one opaque window with no
buttons in it.

Fortunately they don't need to be driven. The app is built on PhoneGap/Cordova
— a web app in a native wrapper — so underneath it is talking plain HTTPS to a
REST API. `courtbot` talks to that API directly, which is both far more
reliable and fast enough to win a race that is decided in milliseconds.

---

## How it works

```
  discover  ──▶  one real booking, recorded  ──▶  captured/recipe.json
                                                        │
  snipe     ──▶  wait for release ──▶ replay recipe ──▶ booked
```

**The bot is not told what the endpoints are — it learns them.** You make one
booking by hand with recording on; it watches the traffic, picks out the three
requests that matter (log in, list availability, book), and replaces the
volatile parts — date, time, slot id, auth token — with placeholders. After
that it can aim the same requests at any slot on any day.

This matters because David Lloyd's API is private and undocumented. Anything
hardcoded here would be a guess, and would break the first time they shipped a
change. A recipe you can re-capture in two minutes will not.

---

## Setup

### 1. Install

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium          # only needed for `discover`
```

### 2. Credentials

Never put these in the config file — the loader refuses to start if it finds a
password there.

```bash
export DL_USERNAME='you@example.com'
export DL_PASSWORD='...'
```

### 3. Capture the booking flow

Two routes. **Try A first**; use B if court booking turns out to be app-only.

**A — the members' website**, if it can book courts:

```bash
python -m courtbot discover \
  --url 'https://www.harbourclub.com/account/login/' \
  --booked-date 2026-10-03 --booked-time 10:00
```

A browser opens. Sign in (**start signed out**, or the login request never
happens and can't be captured), go to court booking, and **book one court all
the way to confirmation**. Browsing is not enough — the booking request only
exists if you actually book. Cancel it afterwards if you don't want it. Then
press Enter.

**B — capture the iPad app instead**, if booking is app-only:

1. Install [Proxyman](https://proxyman.io) on the Mac (free tier is fine).
2. Proxyman → *Certificate* → *Install on iOS device*, and follow it on the
   iPad (install **and trust** the profile in Settings → General → About →
   Certificate Trust Settings).
3. Point the iPad's Wi-Fi at the Mac's proxy.
4. Make one real booking in the app.
5. Export the session as HAR, then:

```bash
python -m courtbot discover --har ~/Downloads/session.har \
  --booked-date 2026-10-03 --booked-time 10:00
```

> If the app uses certificate pinning, Proxyman will show TLS failures and this
> route is closed. Cordova apps usually don't pin, so it's likely to work — but
> it's the one step I couldn't verify for you.

Either way you end up with `captured/recipe.json`. Discovery prints what it
found and flags anything it couldn't work out.

### 4. Configure

```bash
cp config.example.yaml config.yaml
```

Set the weekdays you want and your preferred start times, best first:

```yaml
targets:
  - weekday: saturday
    times: ["10:00", "11:00", "09:00"]
```

Exactly one new date opens per release, so a weekday listed here is attempted
only on the morning it opens.

### 5. Verify before trusting it

```bash
python -m courtbot check --date 2026-10-03   # lists real availability
python -m courtbot plan                      # when is the next release?
python -m courtbot snipe --dry-run --now     # full run, books nothing
```

### 6. Find the real release time

The published time is inconsistent: David Lloyd documents **7:30am** for a named
list of clubs and **8am** generally, and Chelsea is in neither list explicitly.
Rather than guess, measure it:

```bash
python -m courtbot calibrate
```

It watches the boundary and reports the second the window actually opened, and
tells you if `window.release_time` needs changing. **Do this once before relying
on the bot.**

### 7. Schedule it

```bash
python -m courtbot install-launchd --lead 3
```

Then follow the printed instructions. Note that a sleeping Mac will miss the
run unless you also allow it to wake:

```bash
sudo pmset repeat wakeorpoweron MTWRFSU 07:57:00
```

---

## Commands

| command | what it does |
|---|---|
| `discover` | record a real booking, write the recipe |
| `plan` | show the next actionable release; no network calls to the club |
| `check` | log in and list live availability — verifies the recipe |
| `snipe` | wait for the release and book (`--dry-run`, `--now`) |
| `calibrate` | measure the true release time |
| `selectors` | check browser-fallback configuration |
| `install-launchd` | write a macOS scheduled job |

---

## Rehearsing without the real club

A mock booking API ships with the project. It enforces a release gate, refuses
double bookings, and can simulate rivals taking slots the instant they open.

```bash
python tools/mock_club.py --port 8765 --release-in 60
```

The test suite drives the whole pipeline against it:

```bash
python -m pytest tests/ -q      # 126 tests
```

And a dress rehearsal drives the **real CLI** — the same commands you will
type, as subprocesses, with the same config file and exit codes:

```bash
python tools/rehearse.py            # single-request booking flow
python tools/rehearse.py --basket   # two-step basket flow
```

It runs two rounds against fresh mock clubs: a dry run that must leave the club
untouched, then a live run that must produce a confirmed booking. Worth running
before your first real 08:00.

---

## Design notes

**Clock.** The local clock is not trusted; it's corrected against NTP, because
firing 300ms late is a lost court. NTP sync is hard-bounded — an unreachable
time server costs one timeout per sample, and spending that on the approach to
a release is far worse than a slightly imprecise clock.

**Prefire.** The first request goes out ~250ms *before* the release so it
arrives as the window opens. Tune `attempts.prefire_ms` for your connection.

**It will not hammer the club.** Attempts are sequential and stop the moment
a booking succeeds. Parallel booking requests are deliberately not supported:
two that both succeed leave you holding two courts.

**Multi-step booking.** Some leisure systems book in stages (add to basket,
then confirm at checkout). Discovery detects that and records the whole chain,
wiring each step's output into the next — a basket id created in step one
reaches the confirmation in step two as a placeholder, not as the stale literal
it was captured with. Single-request flows are left as one step.

**Session recovery.** Login happens ~90 seconds before the release so the
connection is hot when the window opens. A short-lived token can lapse inside
that gap, so a rejected session triggers exactly one re-login — enough to
recover, not enough to spin on a bad password while the release goes by.

**Fallback.** If the HTTP path is ever blocked (a WAF, a browser-bound token),
`snipe --browser-fallback` replays the booking through a real browser using the
session saved during discovery. Slower, but it survives changes the fast path
doesn't.

**Secrets.** Captured tokens and credentials are stripped from the recipe and
replaced with placeholders; the recipe is safe to read and diff. `captured/` is
gitignored because it holds cookies and your booking history.

---

## Limits and honesty

- **Nothing here has run against the real Harbour Club API.** It was developed
  against a mock, because the club's domains are unreachable from the machine it
  was written on. The logic is tested; the endpoints are unknown until you run
  `discover`.
- **The release time is unverified.** Run `calibrate` before trusting it.
- **Whether court booking exists on the website at all is unconfirmed** — David
  Lloyd's own pages push the app. Route B exists for that reason.
- Automated access is very likely against the club's terms of use. This books
  your own court on your own membership at a normal human rate, but that is
  your call to make, and an account is a thing a club can suspend.
