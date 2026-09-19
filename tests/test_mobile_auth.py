"""Mobile authentication.

A phone app usually signs in once and then renews a long-lived refresh token,
so a capture often shows a renewal rather than a password exchange. Both have
to work, and neither may leave a secret in the recipe.
"""

import json
import stat
from datetime import date, time

import pytest

from courtbot.discover import analyse
from courtbot.distill import Exchange, distill
from courtbot.har import parse as parse_har

B = "https://api.davidlloyd.co.uk"
BOOKED = date(2026, 10, 3)
REFRESH = "rt_9f8e7d6c5b4a39281706aBcDeF0123456789"
ACCESS = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiI5OSJ9.sIgNaTuRe123"


def ex(method, url, body=None, status=200, resp=None, ctype="application/json"):
    return Exchange(
        method=method, url=url, headers={"content-type": ctype},
        body=json.dumps(body) if isinstance(body, dict) else body,
        status=status, response_text=json.dumps(resp) if resp is not None else "",
        resource_type="xhr",
    )


AVAIL = ex("GET", f"{B}/v2/availability?date=2026-10-03", None, 200,
           {"data": {"slots": [{"id": "s1", "startTime": "10:00",
                                "courtName": "Court 1", "available": True}]}})
BOOK = ex("POST", f"{B}/v2/bookings",
          {"slotId": "s1", "date": "2026-10-03", "startTime": "10:00"}, 201, {"ok": 1})


def distil(auth_exchange):
    return distill([auth_exchange, AVAIL, BOOK],
                   booked_date=BOOKED, booked_time=time(10, 0), base_url=B)


def test_a_json_refresh_grant_is_recognised_as_the_auth_step():
    d = distil(ex("POST", f"{B}/v2/oauth/token",
                  {"grant_type": "refresh_token", "refresh_token": REFRESH},
                  200, {"data": {"accessToken": ACCESS}}))
    assert d.recipe.login is not None
    assert d.recipe.login.url.endswith("/oauth/token")
    assert d.recipe.validate() == []


def test_the_refresh_token_never_reaches_the_recipe():
    d = distil(ex("POST", f"{B}/v2/oauth/token",
                  {"grant_type": "refresh_token", "refresh_token": REFRESH},
                  200, {"data": {"accessToken": ACCESS}}))
    assert REFRESH not in json.dumps(d.recipe.to_dict())
    assert json.loads(d.recipe.login.body)["refresh_token"] == "{refresh_token}"
    # grant_type is not a secret and must survive intact, or the call breaks.
    assert json.loads(d.recipe.login.body)["grant_type"] == "refresh_token"


def test_the_refresh_token_is_handed_back_for_safe_storage():
    d = distil(ex("POST", f"{B}/v2/oauth/token",
                  {"grant_type": "refresh_token", "refresh_token": REFRESH},
                  200, {"data": {"accessToken": ACCESS}}))
    assert d.secrets == {"refresh_token": REFRESH}


def test_a_form_encoded_oauth_grant_is_handled():
    d = distil(ex("POST", f"{B}/v2/oauth/token",
                  f"grant_type=refresh_token&refresh_token={REFRESH}&client_id=app",
                  200, {"access_token": ACCESS},
                  ctype="application/x-www-form-urlencoded"))
    assert d.recipe.login is not None
    assert "{refresh_token}" in d.recipe.login.body
    assert REFRESH not in d.recipe.login.body
    assert "client_id=app" in d.recipe.login.body   # not a secret; keep it
    assert d.secrets["refresh_token"] == REFRESH


def test_a_password_sign_in_still_works_and_yields_no_stored_secret():
    d = distil(ex("POST", f"{B}/v2/auth/login",
                  {"username": "me@example.com", "password": "hunter2"},
                  200, {"data": {"accessToken": ACCESS}}))
    body = json.loads(d.recipe.login.body)
    assert body == {"username": "{username}", "password": "{password}"}
    # The password is known to the user; storing it would be gratuitous.
    assert d.secrets == {}


def test_the_report_says_which_kind_of_sign_in_was_captured():
    d = distil(ex("POST", f"{B}/v2/oauth/token",
                  {"grant_type": "refresh_token", "refresh_token": REFRESH},
                  200, {"data": {"accessToken": ACCESS}}))
    assert any("refresh-token renewal" in line for line in d.report)


def test_discover_writes_secrets_to_a_protected_file(tmp_path):
    har = {"log": {"entries": [{
        "startedDateTime": "2026-09-19T08:00:00Z", "_resourceType": "xhr",
        "request": {"method": "POST", "url": f"{B}/v2/oauth/token",
                    "headers": [{"name": "content-type", "value": "application/json"}],
                    "postData": {"text": json.dumps(
                        {"grant_type": "refresh_token", "refresh_token": REFRESH})}},
        "response": {"status": 200, "content": {
            "text": json.dumps({"data": {"accessToken": ACCESS}})}}}]}}
    for e in (AVAIL, BOOK):
        har["log"]["entries"].append({
            "startedDateTime": "2026-09-19T08:00:01Z", "_resourceType": "xhr",
            "request": {"method": e.method, "url": e.url,
                        "headers": [{"name": "content-type", "value": "application/json"}],
                        **({"postData": {"text": e.body}} if e.body else {})},
            "response": {"status": e.status, "content": {"text": e.response_text}}})

    har_path = tmp_path / "s.har"
    har_path.write_text(json.dumps(har))
    result = analyse(har_path, out_dir=tmp_path, booked_date=BOOKED, booked_time=time(10, 0))

    assert result.secrets_path and result.secrets_path.exists()
    written = result.secrets_path.read_text()
    assert f"export DL_REFRESH_TOKEN='{REFRESH}'" in written
    # Readable only by the owner — it is a live credential.
    assert stat.S_IMODE(result.secrets_path.stat().st_mode) == 0o600
    # And it is absent from the recipe that sits beside it.
    assert REFRESH not in result.recipe_path.read_text()
