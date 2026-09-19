"""The distiller turns one observed booking into a reusable recipe. Its job is
to leave nothing session-specific behind: a stale token or a captured slot id
would make the bot fail silently at 08:00."""

import json
from datetime import date, time

import pytest

from courtbot.distill import Exchange, distill, find_slot_list, find_token_path, guess_slot_mapping

TOKEN = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiI5OTExIn0.sIgNaTuRe123456"
BASE = "https://api.example.com"
BOOKED_DATE = date(2026, 9, 30)
BOOKED_TIME = time(10, 0)


def ex(method, url, body=None, status=200, resp=None, headers=None, rtype="xhr"):
    return Exchange(
        method=method, url=url,
        headers=headers or {"content-type": "application/json"},
        body=json.dumps(body) if isinstance(body, (dict, list)) else body,
        status=status,
        response_text=json.dumps(resp) if resp is not None else "",
        resource_type=rtype,
    )


@pytest.fixture
def capture():
    auth = {"content-type": "application/json", "authorization": f"Bearer {TOKEN}"}
    return [
        ex("GET", f"{BASE}/app.js", rtype="script"),
        ex("GET", "https://www.google-analytics.com/collect"),
        ex("POST", f"{BASE}/auth/login",
           {"username": "me@example.com", "password": "hunter2"},
           200, {"data": {"accessToken": TOKEN}}),
        ex("GET", f"{BASE}/availability?date=2026-09-30&clubId=42", None, 200,
           {"data": {"slots": [
               {"id": "s-1", "startTime": "09:00", "courtName": "Court 1", "available": False},
               {"id": "s-2", "startTime": "10:00", "courtName": "Court 3", "available": True},
           ]}}, headers=auth),
        ex("POST", f"{BASE}/bookings",
           {"slotId": "s-2", "court": "Court 3", "date": "2026-09-30", "startTime": "10:00"},
           201, {"data": {"bookingRef": "BK-1"}}, headers=auth),
    ]


@pytest.fixture
def recipe(capture):
    return distill(capture, booked_date=BOOKED_DATE, booked_time=BOOKED_TIME,
                   base_url=BASE).recipe


def test_drops_assets_and_analytics(capture):
    d = distill(capture, booked_date=BOOKED_DATE, booked_time=BOOKED_TIME)
    assert d.considered == 5 and d.kept == 3


def test_identifies_all_three_steps(recipe):
    assert recipe.login.url.endswith("/auth/login")
    assert "/availability" in recipe.availability.url
    assert recipe.book.url.endswith("/bookings")
    assert recipe.validate() == []


def test_captured_credentials_become_placeholders(recipe):
    assert "hunter2" not in (recipe.login.body or "")
    assert "{password}" in recipe.login.body
    assert "{username}" in recipe.login.body


def test_no_stale_token_survives_anywhere(recipe):
    assert TOKEN not in json.dumps(recipe.to_dict())
    assert recipe.availability.headers["authorization"] == "Bearer {token}"
    assert recipe.book.headers["authorization"] == "Bearer {token}"
    assert recipe.login.extract == {"token": "data.accessToken"}


def test_date_and_time_are_templated(recipe):
    assert "date={date}" in recipe.availability.url
    body = json.loads(recipe.book.body)
    assert body["date"] == "{date}"
    assert body["startTime"] == "{time}"


def test_captured_slot_id_is_templated_not_frozen(recipe):
    # Left literal, the bot would rebook the same dead slot every morning.
    body = json.loads(recipe.book.body)
    assert body["slotId"] == "{slot_id}"
    assert body["court"] == "{court_id}"


def test_slot_identifier_templating_precedes_date_templating():
    # An id embedding the date must survive intact.
    cap = [
        ex("POST", f"{BASE}/login", {"username": "u", "password": "p"}, 200,
           {"accessToken": TOKEN}),
        ex("GET", f"{BASE}/avail?date=2026-09-30", None, 200,
           {"slots": [{"id": "2026-09-30-1000-C3", "startTime": "10:00", "courtName": "C3"}]}),
        ex("POST", f"{BASE}/book", {"slotId": "2026-09-30-1000-C3", "date": "2026-09-30"},
           201, {"ok": 1}),
    ]
    r = distill(cap, booked_date=BOOKED_DATE, booked_time=BOOKED_TIME).recipe
    assert json.loads(r.book.body)["slotId"] == "{slot_id}"


def test_volatile_headers_are_not_replayed(capture):
    capture[3].headers["content-length"] = "123"
    capture[3].headers["cookie"] = "sid=abc"
    r = distill(capture, booked_date=BOOKED_DATE, booked_time=BOOKED_TIME).recipe
    assert "content-length" not in r.availability.headers
    assert "cookie" not in r.availability.headers


def test_reports_when_the_booking_step_was_never_observed(capture):
    d = distill(capture[:-1], booked_date=BOOKED_DATE, booked_time=BOOKED_TIME)
    assert any("booking: NOT FOUND" in line for line in d.report)
    assert d.recipe.validate()


def test_reports_when_login_was_never_observed(capture):
    d = distill(capture[3:], booked_date=BOOKED_DATE, booked_time=BOOKED_TIME)
    assert any("login: NOT FOUND" in line for line in d.report)


def test_warns_when_the_date_could_not_be_templated():
    cap = [ex("POST", f"{BASE}/bookings", {"slotRef": "opaque"}, 201, {"ok": 1})]
    d = distill(cap, booked_date=BOOKED_DATE, booked_time=BOOKED_TIME)
    assert any("WARNING" in line for line in d.report)


def test_a_cancellation_endpoint_is_not_mistaken_for_booking():
    cap = [
        ex("POST", f"{BASE}/login", {"username": "u", "password": "p"}, 200, {"accessToken": TOKEN}),
        ex("GET", f"{BASE}/avail?date=2026-09-30", None, 200,
           {"slots": [{"id": "s-2", "startTime": "10:00"}]}),
        ex("POST", f"{BASE}/bookings/cancel", {"id": "old"}, 200, {"ok": 1}),
    ]
    d = distill(cap, booked_date=BOOKED_DATE, booked_time=BOOKED_TIME)
    assert d.recipe.book is None or "cancel" not in d.recipe.book.url


def test_find_token_path_prefers_named_keys_then_jwt_shape():
    assert find_token_path({"data": {"accessToken": "x" * 20}}) == "data.accessToken"
    assert find_token_path({"blob": "eyJhbGci.payloadpayload.sig"}) == "blob"
    assert find_token_path({"short": "abc"}) is None


def test_find_slot_list_picks_the_longest_list_of_objects():
    doc = {"meta": [{"a": 1}], "data": {"slots": [{"b": 1}, {"b": 2}, {"b": 3}]}}
    path, items = find_slot_list(doc)
    assert path == "data.slots" and len(items) == 3


def test_guess_slot_mapping_finds_the_obvious_fields():
    m, _ = guess_slot_mapping([
        {"id": "s1", "startTime": "10:00", "endTime": "11:00",
         "courtName": "Court 1", "available": True}
    ])
    assert (m.time_field, m.court_field, m.id_field, m.available_field) == \
("startTime", "courtName", "id", "available")


def test_guess_slot_mapping_does_not_pick_the_end_time():
    m, _ = guess_slot_mapping([{"endTime": "11:00", "startTime": "10:00"}])
    assert m.time_field == "startTime"
