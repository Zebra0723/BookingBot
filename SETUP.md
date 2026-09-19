# Setup, step by step

Everything below is run on the **Mac**, except where it says iPad. Allow about
40 minutes, plus one morning for `calibrate`.

You will need: the Mac and the iPad on the **same Wi-Fi network**, and your
David Lloyd membership login.

---

## Step 0 — Get the code

```bash
git clone https://github.com/Zebra0723/Available1.git courtbot
cd courtbot
git checkout claude/beautiful-faraday-l7djys
```

Then set up Python. macOS ships with Python 3, but check it is 3.11 or newer:

```bash
python3 --version          # need 3.11+
```

If it is older, `brew install python@3.12` and use `python3.12` below.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Check it runs:

```bash
python -m courtbot --help
```

> Every later command assumes `source .venv/bin/activate` has been run in that
> terminal. If you open a new tab, run it again.

---

## Step 1 — Install Proxyman on the Mac

1. Download from <https://proxyman.io>, drag to Applications, open it.
2. On first launch it asks to install a helper tool and its root certificate on
   the Mac. Allow both, and enter your Mac password when prompted.
3. Leave Proxyman running. It listens on port **9090** by default.

Find the Mac's IP address on the Wi-Fi network — you need it in the next step:

```bash
ipconfig getifaddr en0          # e.g. 192.168.1.42
```

If that prints nothing, you are on Ethernet or a second adapter; try `en1`.

---

## Step 2 — Point the iPad at the Mac

**On the iPad:**

1. Settings → Wi-Fi → tap the ⓘ next to your network.
2. Scroll down → **Configure Proxy** → **Manual**.
3. Server: the IP from Step 1 (e.g. `192.168.1.42`). Port: `9090`.
   Leave authentication off. Tap **Save**.

You should now see the iPad's traffic appearing in Proxyman. If nothing
appears, see *Troubleshooting* at the bottom.

---

## Step 3 — Install and trust Proxyman's certificate on the iPad

This is the step people get wrong. Installing is **not** the same as trusting,
and without trusting, everything stays encrypted and unreadable.

**On the Mac:** Proxyman menu → **Certificate** → **Install Certificate on iOS
Device** → **Physical Device…**. It shows instructions; follow them alongside
these.

**On the iPad:**

1. Open Safari and go to **`http://proxy.man/ssl`**
2. It offers a configuration profile — tap **Allow**, then **Close**.
3. Settings → **General** → **VPN & Device Management** → tap the downloaded
   profile → **Install** (top right) → enter passcode → **Install** again.
4. **Now the part that matters:**
   Settings → **General** → **About** → scroll to the bottom →
   **Certificate Trust Settings** → toggle **Proxyman CA** **ON** → Continue.

**Verify it worked:** browse to any HTTPS site in Safari on the iPad. In
Proxyman you should see the request with readable contents, not
`<encrypted>` or a TLS error. If you see errors, the trust toggle is off.

---

## Step 4 — Capture a real booking

**Sign out of the David Lloyd app on the iPad first.** A signed-in app never
puts its credentials on the wire, so the bot would have no way to authenticate
itself. This is the second most common mistake.

Then:

1. On the Mac, in Proxyman, clear the session so the capture stays small:
   **Edit → Clear Session** (⌘K).
2. On the iPad, open the David Lloyd app and **sign in**.
3. Go to court booking, choose **Harbour Club Chelsea**, and open the
   availability for a date you can actually book.
4. **Book one court, all the way to confirmation.**
   Browsing is not enough — the booking request only exists if you book.
5. **Write down the exact date and time you booked.** You need both in the next
   step, in the form `2026-10-03` and `10:00`.
6. Cancel the booking in the app afterwards if you do not want it. Cancelling
   does not affect the capture.

**Export:** on the Mac, Proxyman → **File → Export → HAR…** (or right-click in
the session list → Export → HAR). Save it somewhere easy, e.g.
`~/Downloads/session.har`.

---

## Step 5 — Check the capture before building on it

```bash
python -m courtbot doctor \
  --har ~/Downloads/session.har \
  --booked-date 2026-10-03 \
  --booked-time 10:00
```

Replace the date and time with what you actually booked.

You want every line to read `[PASS]`. What the failures mean:

| What it says | What to do |
|---|---|
| `The capture is empty` | The iPad was not routed through the proxy. Redo Step 2. |
| `request(s) could not be decrypted` | The certificate is not trusted. Redo Step 3.4. If it names the club's own API host and the trust toggle *is* on, the app pins certificates and this route is closed — stop here and tell me. |
| `No JSON responses` | Same as above, or the app was never used during the capture. |
| `No sign-in request, but the app sent a bearer token` | You were already signed in. Sign out, clear the session, capture again. |
| `No booking request` | You browsed but did not confirm a booking. Capture again and book for real. |
| `No availability request` | Open the court booking screen and let it load before booking. |
| `The booked date does not appear anywhere` | Usually `--booked-date` is wrong. Check what you actually booked. |

Do not continue until `doctor` says `capture looks complete`.

---

## Step 6 — Build the recipe

```bash
python -m courtbot discover \
  --har ~/Downloads/session.har \
  --booked-date 2026-10-03 \
  --booked-time 10:00
```

It prints what it found and writes `captured/recipe.json`.

If the app authenticated with a **refresh token** rather than a password, it
also writes `captured/secrets.env` (readable only by you). It will say so.

---

## Step 7 — Undo the iPad proxy

Do this now, before you forget. If you leave it set, the iPad loses internet
whenever the Mac is asleep or Proxyman is closed.

**On the iPad:** Settings → Wi-Fi → ⓘ → Configure Proxy → **Off** → Save.

You can leave the certificate installed; it does nothing on its own. (To remove
it later: Settings → General → VPN & Device Management → the profile → Remove.)

---

## Step 8 — Credentials

Keep them in one protected file. Both the interactive commands and the
scheduled job read the same file, so there is nothing to keep in sync.

**If Step 6 already wrote `captured/secrets.env`** (refresh-token app), it is
done — skip to Step 9.

**Otherwise**, create it:

```bash
mkdir -p captured
cat > captured/secrets.env <<'EOF'
export DL_USERNAME='you@example.com'
export DL_PASSWORD='your-password'
EOF
chmod 600 captured/secrets.env
```

Load it in whichever terminal you are working in:

```bash
source captured/secrets.env
```

You only need whichever your captured sign-in actually used. If you get it
wrong, `courtbot` names the exact variable it wanted.

> `captured/` is gitignored, so this never reaches the repository. `chmod 600`
> makes it readable only by you — worth doing even on your own machine.

## Step 9 — Configure what you want booked

```bash
cp config.example.yaml config.yaml
open -e config.yaml
```

The part that matters:

```yaml
targets:
  - weekday: saturday
    times: ["10:00", "11:00", "09:00"]
  - weekday: sunday
    times: ["10:00", "09:00"]
```

Times are tried **strictly in the order listed** — it takes 10:00 if free, else
11:00, else 09:00. Add a `court_preference` only if you care; it just breaks
ties between courts at the same time, it never overrides a time preference.

Leave `release_time: "08:00"` for now. Step 11 verifies it.

---

## Step 10 — Verify against the real system

```bash
python -m courtbot plan
```

Shows the next release that opens a weekday you asked for, and how far off it
is. No calls to the club.

```bash
python -m courtbot check --date 2026-09-28
```

Signs in and lists live availability. **Use a date already inside the booking
window** — a few days out, not 9 — otherwise it correctly shows zero slots
because that date has not been released yet.

If `check` lists real courts, the recipe works.

```bash
python -m courtbot snipe --dry-run
```

A full run that books nothing. It waits for the release if one is within an
hour, so run this on a morning shortly before 08:00 for the most realistic
result — or just confirm it plans correctly and move on.

---

## Step 11 — Measure the real release time

Do this **once**, on a morning, starting a few minutes before you think courts
release. Around **07:25** is safe for an 08:00 release.

```bash
python -m courtbot calibrate
```

It watches the boundary and prints the moment slots actually appear:

```
>>> window opened at 07:30:02, -1798.0s from your configured release_time.
>>> update window.release_time in config.yaml
```

If it says that, edit `config.yaml` and set `release_time` to what it measured
(seconds are allowed, e.g. `"07:30:02"`). If it opens within a couple of
seconds of 08:00, leave it alone.

This matters because David Lloyd publishes **7:30am** for a named list of clubs
and **8am** generally, and Chelsea is in neither list explicitly. Guessing
wrong means being 30 minutes late, every time.

---

## Step 12 — Schedule it

```bash
python -m courtbot install-launchd --lead 3
```

This writes `com.courtbot.chelsea.plist`, set to fire 3 minutes before your
configured release time. It reads your credentials from
`captured/secrets.env` — **no password is copied into the plist**, which
matters because files in `~/Library/LaunchAgents` are readable by anything
running as you.

Install it:

```bash
cp com.courtbot.chelsea.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.courtbot.chelsea.plist
```

**Let the Mac wake up for it**, or it will simply miss the release:

```bash
sudo pmset repeat wakeorpoweron MTWRFSU 07:57:00
```

Use a time a couple of minutes before the job fires. Also turn on System
Settings → Battery → Options → *Wake for network access*.

**Check it is loaded:**

```bash
launchctl list | grep courtbot
```

**Test it right now** without waiting for the morning:

```bash
launchctl start com.courtbot.chelsea
sleep 10 && cat captured/launchd.err.log
```

It will most likely report that the next release is too far off to wait for —
that is a correct, successful run, and proves the job can start, find your
credentials and read the config.

**Logs**, after it has run for real:

```bash
cat captured/launchd.out.log
cat captured/history.log          # one line per run, booked or not
```

**To stop it:**

```bash
launchctl unload ~/Library/LaunchAgents/com.courtbot.chelsea.plist
sudo pmset repeat cancel
```

## Rehearse any time

None of this touches the club:

```bash
python tools/rehearse.py            # drives the whole CLI against a mock
python -m pytest tests/ -q          # the test suite
```

---

## Troubleshooting

**Nothing appears in Proxyman when the iPad browses.**
Mac and iPad on the same Wi-Fi? Double-check the IP (`ipconfig getifaddr en0`)
— it changes when you rejoin a network. Check macOS firewall: System Settings →
Network → Firewall → Options → allow Proxyman.

**Proxyman shows the requests but the contents are unreadable.**
The certificate is installed but not trusted. Step 3.4 — the toggle under
Settings → General → About → Certificate Trust Settings.

**The app refuses to load anything at all while the proxy is on.**
That is certificate pinning. Run `doctor` on whatever you captured to confirm;
if it names the club's API host, this approach cannot work and there is no
workaround worth pursuing.

**`courtbot check` returns zero slots.**
Almost always the date. Use one a few days out, inside the window, not 9 days.

**`login failed with HTTP 401`.**
Wrong credentials, or the recipe captured a session that has since expired.
Re-export and re-run `discover`.

**It booked nothing and said "none of the wanted times free".**
Working correctly — those slots were gone. It lists what *was* open; consider
widening `times` in your config.

**Anything else:** run with `-v` for debug logging, e.g.
`python -m courtbot -v snipe --dry-run`.
