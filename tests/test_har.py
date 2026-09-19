import base64
import json

import pytest

from courtbot.har import parse


def _har(entries):
    return {"log": {"entries": entries}}


def _entry(url, started, *, body=None, resp="", b64=False, rtype="xhr", status=200):
    content = {"text": base64.b64encode(resp.encode()).decode(), "encoding": "base64"} if b64 \
        else {"text": resp}
    return {
        "startedDateTime": started, "_resourceType": rtype,
        "request": {"method": "POST" if body else "GET", "url": url,
                    "headers": [{"name": "content-type", "value": "application/json"}],
                    **({"postData": {"text": body}} if body else {})},
        "response": {"status": status, "content": content},
    }


def test_parses_and_orders_chronologically(tmp_path):
    p = tmp_path / "s.har"
    p.write_text(json.dumps(_har([
        _entry("https://x/second", "2026-09-19T08:00:02.000Z"),
        _entry("https://x/first", "2026-09-19T08:00:01.000Z"),
    ])))
    assert [e.url.rsplit("/", 1)[1] for e in parse(p)] == ["first", "second"]


def test_decodes_base64_response_bodies(tmp_path):
    p = tmp_path / "s.har"
    p.write_text(json.dumps(_har([
        _entry("https://x/a", "2026-09-19T08:00:00Z", resp='{"ok":true}', b64=True)
    ])))
    assert parse(p)[0].json_response == {"ok": True}


def test_tolerates_undecodable_base64(tmp_path):
    p = tmp_path / "s.har"
    har = _har([_entry("https://x/a", "2026-09-19T08:00:00Z")])
    har["log"]["entries"][0]["response"]["content"] = {"text": "!!!", "encoding": "base64"}
    p.write_text(json.dumps(har))
    assert parse(p)[0].response_text == ""


def test_classifies_assets_as_noise(tmp_path):
    p = tmp_path / "s.har"
    p.write_text(json.dumps(_har([
        _entry("https://x/app.js", "2026-09-19T08:00:00Z", rtype="script"),
        _entry("https://x/api/book", "2026-09-19T08:00:01Z", rtype="xhr"),
    ])))
    assert [e.is_noise() for e in parse(p)] == [True, False]


def test_missing_file_and_bad_json_are_reported(tmp_path):
    with pytest.raises(FileNotFoundError):
        parse(tmp_path / "absent.har")
    bad = tmp_path / "bad.har"
    bad.write_text("nonsense")
    with pytest.raises(ValueError, match="not a valid HAR"):
        parse(bad)
