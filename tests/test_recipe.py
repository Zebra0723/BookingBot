import json
import pytest

from courtbot.recipe import Recipe, RecipeError, SlotMapping, Step, extract_path, render

DOC = {
    "data": {"accessToken": "tok-123", "nested": {"deep": [{"id": 7}]}},
    "slots": [{"startTime": "10:00", "id": "a"}, {"startTime": "11:00", "id": "b"}],
}


@pytest.mark.parametrize("path,expected", [
    ("data.accessToken", "tok-123"),
    ("slots[0].id", "a"),
    ("slots[-1].id", "b"),
    ("slots[*].startTime", ["10:00", "11:00"]),
    ("data.nested.deep[0].id", 7),
])
def test_extract_path_resolves(path, expected):
    assert extract_path(DOC, path) == expected


@pytest.mark.parametrize("path", [
    "nope", "data.missing", "slots[9].id", "data.accessToken.deeper", "slots[*].absent.x",
])
def test_extract_path_returns_none_rather_than_raising(path):
    # A drifted recipe must degrade to "not found" mid-race, never crash.
    assert extract_path(DOC, path) in (None, [None, None])


def test_render_substitutes_and_url_encodes():
    assert render("/b/{date}", {"date": "2026-09-30"}) == "/b/2026-09-30"
    assert render("{q}", {"q": "a b&c"}, url_encode=True) == "a%20b%26c"


def test_render_raises_on_missing_placeholder():
    with pytest.raises(RecipeError, match="placeholder"):
        render("/b/{date}/{time}", {"date": "x"})


def test_step_reports_placeholders_from_url_body_and_headers():
    s = Step("book", "POST", "/b/{date}", {"X-Court": "{court_id}"}, '{"t":"{time}"}')
    assert s.placeholders() == {"date", "court_id", "time"}


def test_step_success_defaults_to_any_2xx():
    s = Step("x", "GET", "/")
    assert s.ok(200) and s.ok(201) and s.ok(204)
    assert not s.ok(409) and not s.ok(500)


def test_step_honours_explicit_success_status():
    s = Step("x", "POST", "/", success_status=(409,))
    assert s.ok(409) and not s.ok(200)


def test_roundtrip_through_disk_preserves_the_recipe(tmp_path):
    r = Recipe(
        base_url="https://api.example.com",
        login=Step("login", "POST", "/login", body='{"u":"{username}"}',
                   extract={"token": "data.accessToken"}),
        availability=Step("availability", "GET", "/a?date={date}"),
        book=Step("book", "POST", "/b", body='{"slotId":"{slot_id}"}'),
        slots=SlotMapping(list_path="data.slots", time_field="startTime", id_field="id"),
    )
    path = r.save(tmp_path / "recipe.json")
    back = Recipe.load(path)
    assert back.to_dict() == r.to_dict()
    assert back.login.extract == {"token": "data.accessToken"}


def test_validate_flags_a_recipe_missing_the_booking_step():
    r = Recipe(availability=Step("availability", "GET", "/a"))
    assert any("booking step" in p for p in r.validate())


def test_validate_flags_an_unsatisfiable_placeholder():
    r = Recipe(
        availability=Step("availability", "GET", "/a"),
        book=Step("book", "POST", "/b", body='{"x":"{mystery_value}"}'),
    )
    assert any("mystery_value" in p for p in r.validate())


def test_validate_accepts_a_placeholder_supplied_by_an_earlier_extract():
    r = Recipe(
        availability=Step("availability", "GET", "/a", extract={"basket": "data.basketId"}),
        book=Step("book", "POST", "/b", body='{"b":"{basket}"}'),
    )
    assert r.validate() == []


def test_load_gives_an_actionable_error_when_absent(tmp_path):
    with pytest.raises(RecipeError, match="courtbot discover"):
        Recipe.load(tmp_path / "nope.json")


def test_load_rejects_malformed_json(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("{not json")
    with pytest.raises(RecipeError, match="not valid JSON"):
        Recipe.load(p)
