"""Multi-step (basket) booking flows.

Many leisure systems book in stages: add to basket, then confirm at checkout.
A recipe that captured only the last request would replay a stale basket id
forever, so the distiller has to detect the chain and wire the steps together.
"""

import json
from datetime import date, time, timedelta

import pytest
import requests

from courtbot.distill import Exchange, distill, placeholder_name, wire_chain
from courtbot.recipe import Step
from courtbot.sniper import Sniper
from courtbot.timing import LONDON, BookingWindow, ClockSync
from mock_club import PASSWORD, USERNAME, ClubState, serve

from test_e2e import make_config  # reuse the shared config builder

TOKEN = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiI5OSJ9.sIgNaTuRe12345"
BASE = "https://api.example.com"
BOOKED = date(2026, 9, 30)


def ex(method, url, body=None, status=200, resp=None, rtype="xhr"):
    return Exchange(
        method=method, url=url, headers={"content-type": "application/json"},
        body=json.dumps(body) if isinstance(body, (dict, list)) else body,
        status=status, response_text=json.dumps(resp) if resp is not None else "",
        resource_type=rtype,
    )


@pytest.fixture
def chain_recipe():
    capture = [
        ex("POST", f"{BASE}/auth/login", {"username": "u", "password": "p"}, 200,
           {"data": {"accessToken": TOKEN}}),
        ex("GET", f"{BASE}/availability?date=2026-09-30", None, 200,
           {"data": {"slots": [{"id": "s-77", "startTime": "10:00",
                                "courtName": "Court 3", "available": True}]}}),
        ex("POST", f"{BASE}/basket", {"slotId": "s-77"}, 201,
           {"data": {"basketId": "BSK-abc123def456"}}),
        ex("POST", f"{BASE}/basket/BSK-abc123def456/checkout", {}, 201,
           {"data": {"bookingRef": "BK-1"}}),
    ]
    return distill(capture, booked_date=BOOKED, booked_time=time(10, 0),
                   base_url=BASE)


def test_detects_the_two_step_flow(chain_recipe):
    assert len(chain_recipe.recipe.book_chain) == 2
    assert chain_recipe.recipe.booking_steps()[0].url.endswith("/basket")


def test_basket_id_is_wired_from_the_first_step_to_the_second(chain_recipe):
    first, second = chain_recipe.recipe.book_chain
    # The literal basket id must not survive — it would be replayed stale.
    assert "BSK-abc123def456" not in second.url
    assert "{basket_id}" in second.url
    assert first.extract == {"basket_id": "data.basketId"}


def test_slot_id_is_still_templated_in_the_first_step(chain_recipe):
    assert json.loads(chain_recipe.recipe.book_chain[0].body)["slotId"] == "{slot_id}"


def test_a_wired_chain_validates(chain_recipe):
    assert chain_recipe.recipe.validate() == []


def test_an_unwired_chain_is_reported_as_unsatisfiable():
    r = distill([
        ex("POST", f"{BASE}/login", {"username": "u", "password": "p"}, 200,
           {"accessToken": TOKEN}),
        ex("GET", f"{BASE}/availability?date=2026-09-30", None, 200,
           {"slots": [{"id": "s-77", "startTime": "10:00"}]}),
        ex("POST", f"{BASE}/bookings", {"slotId": "s-77"}, 201, {"ok": 1}),
    ], booked_date=BOOKED, booked_time=time(10, 0)).recipe
    r.book.body = '{"ref":"{never_produced}"}'
    assert any("never_produced" in p for p in r.validate())


def test_single_step_flows_are_left_alone(chain_recipe):
    r = distill([
        ex("POST", f"{BASE}/login", {"username": "u", "password": "p"}, 200,
           {"accessToken": TOKEN}),
        ex("GET", f"{BASE}/availability?date=2026-09-30", None, 200,
           {"slots": [{"id": "s-77", "startTime": "10:00"}]}),
        ex("POST", f"{BASE}/bookings", {"slotId": "s-77", "date": "2026-09-30"}, 201, {"ok": 1}),
    ], booked_date=BOOKED, booked_time=time(10, 0)).recipe
    assert r.book_chain == []
    assert r.book is not None


def test_a_post_before_availability_is_not_part_of_the_chain():
    # A consent POST fired on page load must not be mistaken for booking.
    r = distill([
        ex("POST", f"{BASE}/login", {"username": "u", "password": "p"}, 200,
           {"accessToken": TOKEN}),
        ex("POST", f"{BASE}/booking-preferences", {"optIn": True}, 200, {"ok": 1}),
        ex("GET", f"{BASE}/availability?date=2026-09-30", None, 200,
           {"slots": [{"id": "s-77", "startTime": "10:00"}]}),
        ex("POST", f"{BASE}/bookings", {"slotId": "s-77", "date": "2026-09-30"}, 201, {"ok": 1}),
    ], booked_date=BOOKED, booked_time=time(10, 0)).recipe
    urls = [s.url for s in r.booking_steps()]
    assert not any("preferences" in u for u in urls)


def test_short_values_are_not_wired_by_coincidence():
    # "60" appearing in both a response and a later body is a coincidence.
    a = Step("a", "POST", "/a")
    b = Step("b", "POST", "/b", body='{"duration":60}')
    pairs = [(a, ex("POST", "/a", None, 200, {"duration": 60})),
             (b, ex("POST", "/b", {"duration": 60}, 201, {"ok": 1}))]
    wire_chain(pairs)
    assert a.extract == {}
    assert b.body == '{"duration":60}'


@pytest.mark.parametrize("path,expected", [
    ("data.basketId", "basket_id"),
    ("reservationToken", "reservation_token"),
    ("data.items[0].ref", "ref"),
])
def test_placeholder_names_are_readable(path, expected):
    assert placeholder_name(path, set()) == expected


def test_placeholder_names_never_shadow_runtime_values():
    # Shadowing `date` would silently misdirect the booking request.
    assert placeholder_name("meta.date", set()) != "date"
    assert placeholder_name("x.slotId", set()) != "slot_id"


def test_placeholder_names_are_unique():
    taken = set()
    a = placeholder_name("one.ref", taken); taken.add(a)
    b = placeholder_name("two.ref", taken)
    assert a != b


# --- end to end against the mock's basket flow ------------------------------

def test_books_through_the_basket_flow_at_release(monkeypatch):
    monkeypatch.setenv("DL_USERNAME", USERNAME)
    monkeypatch.setenv("DL_PASSWORD", PASSWORD)

    from datetime import datetime
    now_lon = datetime.now(LONDON)
    release_wall = (now_lon + timedelta(seconds=4)).time().replace(microsecond=0)
    window = BookingWindow(9, release_wall, LONDON)
    target = now_lon.date() + timedelta(days=9)
    state = ClubState(window.release_instant_for(target), advance_days=9)
    server = serve(state, port=0)
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        # Record a manual booking through the basket route on an open date.
        sink, session = [], requests.Session()

        def rec(r):
            sink.append(Exchange(
                r.request.method, r.request.url,
                {k.lower(): v for k, v in r.request.headers.items()},
                r.request.body.decode() if isinstance(r.request.body, bytes) else r.request.body,
                r.status_code, r.text, "xhr"))
            return r

        tok = rec(session.post(f"{base}/api/v2/auth/login",
                  json={"username": USERNAME, "password": PASSWORD})
                  ).json()["data"]["accessToken"]
        session.headers["authorization"] = f"Bearer {tok}"
        open_date = now_lon.date() + timedelta(days=8)
        rec(session.get(f"{base}/api/v2/availability", params={"date": open_date.isoformat()}))
        sid = state.slot_id(open_date, 10, "Court 3")
        basket = rec(session.post(f"{base}/api/v2/basket", json={"slotId": sid})
                     ).json()["data"]["basketId"]
        rec(session.post(f"{base}/api/v2/basket/{basket}/checkout", json={}))

        recipe = distill(sink, booked_date=open_date, booked_time=time(10, 0),
                         base_url=base).recipe
        assert len(recipe.book_chain) == 2, "expected the basket flow to be detected"
        assert recipe.validate() == []

        cfg = make_config(window, target, base, [time(19, 0)])
        outcome = Sniper(cfg, recipe, clock=ClockSync(enabled=False)).run(wait=True)

        assert outcome.booked, outcome.summary()
        assert outcome.result.slot.start == time(19, 0)
        # The confirmed booking must exist on the club side, not just locally.
        assert any(b["slot"].endswith("1900-Court1") or "1900" in b["slot"]
                   for b in state.booking_log)
    finally:
        server.shutdown()
