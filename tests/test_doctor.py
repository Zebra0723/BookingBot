"""Capture diagnostics.

Capturing the app's traffic is the step most likely to fail, and it fails
quietly. These cover the four ways a capture goes wrong, because a doctor that
passes a broken capture is worse than no doctor at all.
"""

import json
from datetime import date, time

import pytest

from courtbot.distill import Exchange
from courtbot.doctor import FAIL, OK, WARN, diagnose, verdict

B = "https://api.davidlloyd.co.uk"
BOOKED = date(2026, 10, 3)


def ex(method, url, body=None, status=200, resp=None, headers=None, rtype="xhr", error=""):
    return Exchange(
        method=method, url=url,
        headers=headers or {"content-type": "application/json"},
        body=json.dumps(body) if isinstance(body, dict) else body,
        status=status, response_text=json.dumps(resp) if resp is not None else "",
        resource_type=rtype, error=error,
    )


LOGIN = ex("POST", f"{B}/v2/auth/login", {"username": "a@b.com", "password": "p"},
           200, {"data": {"accessToken": "eyJa.bbbbbbbbbbbbbbbb.c"}})
AVAIL = ex("GET", f"{B}/v2/availability?date=2026-10-03", None, 200,
           {"data": {"slots": [{"id": "s1", "startTime": "10:00",
                                "courtName": "Court 1", "available": True}]}})
BOOK = ex("POST", f"{B}/v2/bookings",
          {"slotId": "s1", "date": "2026-10-03", "startTime": "10:00"}, 201, {"ok": 1})


def levels(findings, title_contains):
    return [f.level for f in findings if title_contains.lower() in f.title.lower()]


def test_a_complete_capture_passes_cleanly():
    findings = diagnose([LOGIN, AVAIL, BOOK], booked_date=BOOKED, booked_time=time(10, 0))
    assert all(f.level == OK for f in findings), [f.title for f in findings if f.level != OK]
    usable, summary = verdict(findings)
    assert usable and "complete" in summary


def test_an_empty_capture_is_reported_as_a_routing_problem():
    findings = diagnose([])
    assert findings[0].level == FAIL
    assert "proxy" in findings[0].detail
    assert verdict(findings)[0] is False


def test_certificate_pinning_is_named_explicitly():
    pinned = [
        ex("GET", f"{B}/v2/auth/login", None, 0, None,
           error="Client TLS handshake failed: certificate verify failed"),
        ex("GET", f"{B}/v2/availability", None, 0, None, error="Client TLS handshake failed"),
    ]
    findings = diagnose(pinned, booked_date=BOOKED)
    assert levels(findings, "could not be decrypted") == [FAIL]
    assert any("pinning" in f.detail for f in findings)
    assert verdict(findings)[0] is False


def test_a_failed_request_is_not_counted_as_a_found_endpoint():
    """The worst false positive available: telling someone a dead capture is fine."""
    dead = [ex("GET", f"{B}/v2/availability?date=2026-10-03", None, 0, None,
               error="TLS handshake failed")]
    findings = diagnose(dead, booked_date=BOOKED)
    assert levels(findings, "availability") == [FAIL]


def test_browsing_without_booking_is_caught():
    findings = diagnose([LOGIN, AVAIL], booked_date=BOOKED)
    assert levels(findings, "booking request") == [FAIL]
    assert any("confirm a booking" in f.detail for f in findings)


def test_an_already_signed_in_app_is_told_to_sign_out_first():
    authed = [
        ex("GET", f"{B}/v2/availability?date=2026-10-03", None, 200,
           {"data": {"slots": [{"id": "s1", "startTime": "10:00"}]}},
           headers={"content-type": "application/json", "authorization": "Bearer eyJabc"}),
        BOOK,
    ]
    findings = diagnose(authed, booked_date=BOOKED)
    assert levels(findings, "bearer token") == [WARN]
    assert any("Sign OUT" in f.detail for f in findings)


def test_a_missing_booked_date_is_only_a_warning():
    findings = diagnose([LOGIN, AVAIL, BOOK])
    assert levels(findings, "booked-date") == [WARN]
    assert verdict(findings)[0] is True   # usable, just less re-aimable


def test_a_wrong_booked_date_is_flagged():
    findings = diagnose([LOGIN, AVAIL, BOOK], booked_date=date(2030, 1, 1))
    assert levels(findings, "booked date does not appear") == [WARN]


def test_a_wrong_booked_time_is_flagged():
    findings = diagnose([LOGIN, AVAIL, BOOK], booked_date=BOOKED, booked_time=time(23, 0))
    assert levels(findings, "booked time does not appear") == [WARN]


def test_analytics_only_capture_is_reported_as_no_api():
    noise = [ex("GET", "https://www.google-analytics.com/collect", None, 200, {"ok": 1})]
    findings = diagnose(noise, booked_date=BOOKED)
    assert levels(findings, "No JSON responses") == [FAIL]


def test_findings_render_without_crashing():
    for f in diagnose([LOGIN, AVAIL, BOOK], booked_date=BOOKED):
        assert f.render().strip()
