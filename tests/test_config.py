import textwrap
from datetime import time

import pytest

from courtbot.config import AttemptPolicy, Club, ConfigError, load

GOOD = """
club:
  name: "Harbour Club Chelsea"
  activity: tennis
  duration_minutes: 60
window:
  advance_days: 9
  release_time: "08:00"
  timezone: Europe/London
targets:
  - weekday: saturday
    times: ["10:00", "11:00"]
"""


def write(tmp_path, text):
    p = tmp_path / "config.yaml"
    p.write_text(textwrap.dedent(text))
    return p


def test_loads_a_good_config(tmp_path):
    c = load(write(tmp_path, GOOD))
    assert c.club.name == "Harbour Club Chelsea"
    assert c.window.advance_days == 9
    assert c.window.release_time == time(8, 0)
    assert c.targets[0].weekday == 5
    assert c.targets[0].times == (time(10, 0), time(11, 0))


def test_time_preference_order_is_preserved(tmp_path):
    c = load(write(tmp_path, GOOD.replace('["10:00", "11:00"]', '["19:00", "07:00", "12:00"]')))
    assert [t.strftime("%H:%M") for t in c.targets[0].times] == ["19:00", "07:00", "12:00"]


def test_rejects_a_password_in_the_file(tmp_path):
    # Credentials in a committed config is the mistake worth hard-failing on.
    p = write(tmp_path, GOOD + '\npassword: "hunter2"\n')
    with pytest.raises(ConfigError, match="environment"):
        load(p)


def test_rejects_a_nested_secret(tmp_path):
    p = write(tmp_path, GOOD + '\nextras:\n  api:\n    token: "abc"\n')
    with pytest.raises(ConfigError, match="environment"):
        load(p)


def test_missing_file_names_the_fix(tmp_path):
    with pytest.raises(ConfigError, match="config.example.yaml"):
        load(tmp_path / "absent.yaml")


def test_rejects_an_unknown_weekday(tmp_path):
    with pytest.raises(ConfigError, match="not a weekday"):
        load(write(tmp_path, GOOD.replace("saturday", "caturday")))


def test_rejects_a_duplicate_weekday(tmp_path):
    with pytest.raises(ConfigError, match="appears twice"):
        load(write(tmp_path, GOOD + '  - weekday: saturday\n    times: ["09:00"]\n'))


def test_rejects_a_malformed_time(tmp_path):
    with pytest.raises(ConfigError, match="24-hour time"):
        load(write(tmp_path, GOOD.replace('"10:00"', '"25:99"')))


def test_rejects_empty_targets(tmp_path):
    with pytest.raises(ConfigError, match="targets is empty"):
        load(write(tmp_path, GOOD.split("targets:")[0]))


def test_rejects_an_unknown_timezone(tmp_path):
    with pytest.raises(ConfigError, match="unknown zone"):
        load(write(tmp_path, GOOD.replace("Europe/London", "Mars/Olympus")))


def test_rejects_plain_http_base_url(tmp_path):
    with pytest.raises(ConfigError, match="https"):
        load(write(tmp_path, GOOD + '\nbase_url: "http://insecure.example.com"\n'))


def test_credentials_come_from_the_environment(tmp_path, monkeypatch):
    c = load(write(tmp_path, GOOD))
    monkeypatch.setenv("DL_USERNAME", "me@example.com")
    monkeypatch.setenv("DL_PASSWORD", "pw")
    assert c.credentials().username == "me@example.com"


def test_missing_credentials_are_reported_clearly(tmp_path, monkeypatch):
    c = load(write(tmp_path, GOOD))
    monkeypatch.delenv("DL_USERNAME", raising=False)
    monkeypatch.delenv("DL_PASSWORD", raising=False)
    with pytest.raises(ConfigError, match="DL_USERNAME"):
        c.credentials()


def test_attempt_policy_rejects_an_absurd_prefire():
    with pytest.raises(ConfigError, match="prefire_ms"):
        AttemptPolicy(prefire_ms=60_000)


def test_attempt_policy_rejects_a_polling_rate_that_would_hammer_the_club():
    with pytest.raises(ConfigError, match="20x a second"):
        AttemptPolicy(retry_interval_ms=5)


def test_attempt_policy_rejects_a_non_positive_retry_budget():
    with pytest.raises(ConfigError, match="positive"):
        AttemptPolicy(retry_for_seconds=0)


def test_club_rejects_an_unsupported_duration():
    with pytest.raises(ConfigError, match="duration_minutes"):
        Club(name="x", duration_minutes=45)
