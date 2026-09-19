"""Diagnose a captured session before trusting it.

Capturing the iOS app's traffic is the step most likely to go wrong, and the
failures are quiet: a proxy the device never actually used, a certificate the
device refused, or a session where the user browsed but never confirmed a
booking. All three produce a HAR that looks plausible and a recipe that fails
at 08:00.

This reports what the capture actually contains, in the order that matters,
so a bad capture is caught at the desk.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import date, time
from urllib.parse import urlsplit

from .distill import (
    Exchange,
    _looks_like_availability,
    _looks_like_booking,
    _looks_like_login,
    date_variants,
    find_slot_list,
    find_token_path,
    time_variants,
)

OK, WARN, FAIL = "ok", "warn", "fail"


@dataclass
class Finding:
    level: str
    title: str
    detail: str

    def render(self) -> str:
        mark = {OK: "PASS", WARN: "WARN", FAIL: "FAIL"}[self.level]
        body = "\n".join(f"        {line}" for line in self.detail.splitlines())
        return f"  [{mark}]  {self.title}\n{body}" if self.detail else f"  [{mark}]  {self.title}"


def _host(url: str) -> str:
    return urlsplit(url).netloc or "?"


def diagnose(
    exchanges: list[Exchange],
    *,
    booked_date: date | None = None,
    booked_time: time | None = None,
) -> list[Finding]:
    out: list[Finding] = []
    signal = [e for e in exchanges if not e.is_noise()]
    # A request that never completed carries no evidence. Counting one as a
    # "found" endpoint is the worst possible false positive here: it tells
    # someone their pinned capture is fine.
    usable = [e for e in signal if not e.error and e.status]

    # 1. Did anything get captured at all?
    if not exchanges:
        out.append(Finding(FAIL, "The capture is empty", (
            "No requests were recorded. The device was not routed through the\n"
            "proxy. Check the Wi-Fi proxy settings on the iPad and that the\n"
            "proxy is listening on the Mac's LAN address, not just localhost."
        )))
        return out
    out.append(Finding(OK, f"{len(exchanges)} requests captured",
                       f"{len(signal)} remain after dropping images, fonts and analytics"))

    # 2. Did TLS interception actually work?
    failed = [e for e in exchanges if e.error or e.status == 0]
    if failed:
        hosts = Counter(_host(e.url) for e in failed)
        worst = ", ".join(f"{h} ({n})" for h, n in hosts.most_common(4))
        out.append(Finding(FAIL, f"{len(failed)} request(s) could not be decrypted", (
            f"Affected hosts: {worst}\n"
            "This is what certificate pinning looks like. If the club's own API\n"
            "host is in that list, the app refuses an intercepted connection and\n"
            "this route is closed — capture is not possible without patching the\n"
            "app, which is out of scope here."
        )))

    # 3. Is there an API here, or only tracking?
    json_ex = [e for e in signal if e.json_response is not None]
    if not json_ex:
        out.append(Finding(FAIL, "No JSON responses in the capture", (
            "Nothing that looks like an API was recorded. Either the certificate\n"
            "was not trusted on the device (Settings > General > About >\n"
            "Certificate Trust Settings — it must be toggled on, not merely\n"
            "installed), or the app was never used during the capture."
        )))
    else:
        hosts = Counter(_host(e.url) for e in json_ex)
        listed = "\n".join(f"{n:>4}  {h}" for h, n in hosts.most_common(6))
        out.append(Finding(OK, f"{len(json_ex)} JSON responses from {len(hosts)} host(s)", listed))

    # 4. The three requests that matter.
    login = max(usable, key=lambda e: _looks_like_login(e), default=None)
    if login and _looks_like_login(login) >= 5:
        token = find_token_path(login.json_response)
        out.append(Finding(OK, "Found the sign-in request",
                           f"{login.method} {login.url[:90]}\n" +
                           (f"token at `{token}`" if token else
                            "no token in the reply — auth is probably cookie-based")))
    else:
        bearer = [e for e in usable
                  if any(k.lower() == "authorization" for k in e.headers)]
        if bearer:
            out.append(Finding(WARN, "No sign-in request, but the app sent a bearer token", (
                "The app was already signed in, so the credentials exchange was\n"
                "never on the wire. Sign OUT in the app, start a fresh capture,\n"
                "and sign back in — otherwise the bot cannot authenticate itself."
            )))
        else:
            out.append(Finding(FAIL, "No sign-in request and no bearer token", (
                "Nothing in this capture shows how the app authenticates."
            )))

    avail = max(usable, key=lambda e: _looks_like_availability(e, booked_date), default=None)
    found = find_slot_list(avail.json_response) if avail else None
    if avail and _looks_like_availability(avail, booked_date) >= 4 and found:
        out.append(Finding(OK, "Found the availability request",
                           f"{avail.method} {avail.url[:90]}\n"
                           f"{len(found[1])} slots at `{found[0]}`"))
    else:
        out.append(Finding(FAIL, "No availability request", (
            "Nothing returned a list of slots. Open the court booking screen for\n"
            "your club and let it load before booking."
        )))

    booking = [e for e in usable if _looks_like_booking(e, booked_date) >= 5]
    if booking:
        chain = "\n".join(f"{e.method} {e.url[:86]} -> {e.status}" for e in booking)
        level = OK if any(200 <= e.status < 300 for e in booking) else WARN
        out.append(Finding(level, f"Found {len(booking)} booking request(s)", chain))
    else:
        out.append(Finding(FAIL, "No booking request", (
            "Browsing availability is not enough — the booking request only\n"
            "exists if you actually confirm a booking. Make one for real during\n"
            "the capture; cancel it afterwards if you do not want it."
        )))

    # 5. Can the capture be re-aimed at another slot?
    if booked_date:
        hay = "\n".join(f"{e.url}{e.body or ''}" for e in usable)
        if any(v in hay for v in date_variants(booked_date)):
            out.append(Finding(OK, "The booked date appears in the traffic",
                               "so the recipe can be re-aimed at another day"))
        else:
            out.append(Finding(WARN, "The booked date does not appear anywhere", (
                "Either --booked-date is wrong, or the app refers to slots only by\n"
                "an opaque id. The second case still works, but check the recipe's\n"
                "availability request by hand."
            )))
        if booked_time and not any(v in hay for v in time_variants(booked_time)):
            out.append(Finding(WARN, "The booked time does not appear anywhere",
                               "Check --booked-time matches the slot you actually took."))
    else:
        out.append(Finding(WARN, "No --booked-date given",
                           "Pass it so the date can be turned into a placeholder."))

    return out


def verdict(findings: list[Finding]) -> tuple[bool, str]:
    fails = [f for f in findings if f.level == FAIL]
    warns = [f for f in findings if f.level == WARN]
    if fails:
        return False, f"{len(fails)} blocking problem(s) — this capture will not produce a working bot"
    if warns:
        return True, f"usable, with {len(warns)} thing(s) worth checking"
    return True, "capture looks complete"
