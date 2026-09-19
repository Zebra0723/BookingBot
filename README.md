# courtbot

Books a court at **Harbour Club Chelsea** the moment the booking window opens.

Courts release 9 days ahead at a fixed time each morning and the good slots go
within seconds. This sits on that moment: it wakes before the release, holds an
authenticated connection open, fires the instant the window opens, and walks
down your list of preferred times until something sticks.

---

## What this talks to

Court booking is **app-only** — there is no web booking page — and the Mac App
Store build is the iOS app in compatibility mode, which exposes almost nothing
to macOS Accessibility. So neither the app's UI nor a browser is a workable
target.

They don't need to be. The app is built on PhoneGap/Cordova — a web app in a
native wrapper — so underneath it speaks plain HTTPS to a REST API. `courtbot`
talks to that API directly: more reliable than any UI, and fast enough to win a
race decided in milliseconds.

**The bot is not told what the endpoints are — it learns them.** You book once
by hand with the app's traffic being recorded; it picks out the requests that
matter and replaces the volatile parts (date, time, slot id, auth token) with
placeholders. After that it can aim the same requests at any slot on any day.

That matters because the API is private and undocumented. Anything hardcoded
here would be a guess, and would break the first time they shipped a change.

---

## Setup

### 1. Install

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Capture the app's traffic

You need the iPad's traffic going through a proxy on the Mac.

1. Install [Proxyman](https://proxyman.io) on the Mac (free tier is fine).
2. Proxyman → *Certificate* → *Install on iOS device*, and follow it on the
   iPad. The certificate must be **installed _and_ trusted** — Settings →
   General → About → Certificate Trust Settings, toggle it on. Installing
   alone is not enough and is the single most common mistake.
3. Point the iPad's Wi-Fi at the Mac's proxy.
4. **Sign out of the app first.** A signed-in app never puts its credentials on
   the wire, and the bot then has no way to authenticate itself.
5. Sign in, open court booking for Harbour Club Chelsea, and **book one court
   all the way to confirmation**. Browsing is not enough — the booking request
   only exists if you actually book. Cancel it afterwards if you don't want it.
6. Export the session as HAR.

### 3. Check the capture before building anything on it

```bash
python -m courtbot doctor --har ~/Downloads/session.har \
  --booked-date 2026-10-03 --booked-time 10:00
```

This reports what the capture actually contains — the sign-in, the availability
call, the booking — and names what's missing. It also detects certificate
pinning, which is the one failure that closes this route entirely. Fix anything
it flags before going further.

### 4. Build the recipe

```bash
python -m courtbot discover --har ~/Downloads/session.har \
  --booked-date 2026-10-03 --booked-time 10:00
```

Writes `captured/recipe.json`. If the app authenticated with a **refresh token**
rather than a password, that token is kept out of the recipe and written to
`captured/secrets.env` (mode 0600) — `source` it before running.

### 5. Credentials

Never in the config file; the loader refuses to start if it finds a secret there.

```bash
export DL_USERNAME='you@example.com'
export DL_PASSWORD='...'
# or, for a refresh-token app:
source captured/secrets.env
```

You only need whichever the captured sign-in actually uses. If it's missing,
`courtbot` names the exact variable.

### 6. Configure

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

### 7. Verify

```bash
python -m courtbot check --date 2026-10-03   # lists real availability
python -m courtbot plan                      # when is the next release?
python -m courtbot snipe --dry-run           # full run, books nothing
```

### 8. Find the real release time

The published time is inconsistent: David Lloyd documents **7:30am** for a named
list of clubs and **8am** generally, and Chelsea is in neither list explicitly.
Measure it rather than guess:

```bash
python -m courtbot calibrate
```

It watches the boundary and reports the second the window actually opened.
**Do this once before relying on the bot.**

### 9. Schedule it

```bash
python -m courtbot install-launchd --lead 3
```

Follow the printed instructions. A sleeping Mac will miss the run unless you
also let it wake:

```bash
sudo pmset repeat wakeorpoweron MTWRFSU 07:57:00
```

---

## Commands

| command | what it does |
|---|---|
| `doctor` | check whether a capture is usable, before building on it |
| `discover` | turn a capture into a recipe |
| `plan` | show the next actionable release; no calls to the club |
| `check` | sign in and list live availability — verifies the recipe |
| `snipe` | wait for the release and book (`--dry-run`, `--now`) |
| `calibrate` | measure the true release time |
| `install-launchd` | write a macOS scheduled job |

---

## Rehearsing without the real club

A mock booking API ships with the project. It enforces a release gate, refuses
double bookings, and can simulate rivals taking slots the instant they open.

```bash
python -m pytest tests/ -q          # 152 tests
python tools/rehearse.py            # drives the real CLI end to end
python tools/rehearse.py --basket   # same, with a two-step basket flow
```

The rehearsal runs two rounds against fresh mock clubs: a dry run that must
leave the club untouched, then a live run that must produce a confirmed
booking. Worth running before your first real 08:00.

---

## Design notes

**BST vs GMT.** The release time is local London time, and which offset applies
is decided by **the day the release happens** — not the day being booked. Those
differ: four windows a year straddle a clock change, so a release on a GMT
morning can open a court date that lands in BST. Anchoring on the wrong end
would fire an hour out on each of them. There's a test that walks every day of
a year and asserts each release lands at 08:00 local on its own release day.

**Clock.** The local clock is not trusted; it's corrected against NTP, because
firing 300ms late is a lost court. The sync is hard-bounded — an unreachable
time server costs one timeout per sample, and spending that on the approach to
a release is far worse than a slightly imprecise clock.

**Prefire.** The first request goes out ~250ms *before* the release so it
arrives as the window opens. Tune `attempts.prefire_ms` for your connection.

**Session recovery.** Sign-in happens ~90 seconds early so the connection is
warm. A short-lived token can lapse in that gap, so a rejected session triggers
exactly one re-login — enough to recover, not enough to spin on a bad password
while the release goes by.

**Multi-step booking.** Some systems book in stages (add to basket, then
confirm at checkout). Discovery detects that and records the whole chain,
wiring each step's output into the next, so a basket id created in step one
reaches the confirmation as a placeholder rather than the stale literal it was
captured with.

**It will not hammer the club.** Attempts are sequential and stop the moment a
booking succeeds. Parallel booking requests are deliberately unsupported: two
that both succeed leave you holding two courts.

**Secrets.** Captured passwords and tokens are stripped from the recipe and
replaced with placeholders, so it is safe to read and diff. `captured/` is
gitignored — it holds live credentials and your booking history.

---

## Limits and honesty

- **Nothing here has run against the real David Lloyd API.** It was developed
  against a mock, because the club's domains are unreachable from the machine
  it was written on. The logic is tested; the endpoints are unknown until you
  run `discover`.
- **The release time is unverified.** Run `calibrate` before trusting it.
- **If the app pins certificates, this route is closed.** Proxyman will fail to
  decrypt and `doctor` will say so plainly. Cordova apps usually don't pin, but
  I could not verify it for this one.
- Automated access is very likely against the club's terms of use. This books
  your own court on your own membership at a human rate, but that's your call,
  and an account is a thing a club can suspend.
