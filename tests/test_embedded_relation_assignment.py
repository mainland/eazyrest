"""Assigning an embedded relation its own object sends nothing."""

from __future__ import annotations

from typing import Any

import pytest
import requests_mock

from eazyrest import API, JSONObject, json_object

api = API("https://embedded.example/")


@json_object
class Person(JSONObject):
    """A related model."""

    class_url = "/people/"
    id: int
    name: str


@json_object
class Card(JSONObject):
    """A model with an owner, a list, and a set of people."""

    class_url = "/cards/"
    id: int
    owner: Person
    people: list[Person]
    crowd: set[Person]


Person.register_api(api)
Card.register_api(api)

BODY = {
    "id": 1,
    "owner": {"id": 7, "name": "a"},
    "people": [{"id": 7, "name": "a"}, {"id": 8, "name": "b"}],
    "crowd": [{"id": 7, "name": "a"}],
}


@pytest.mark.parametrize("mode", ["lazy", "eager"])
@pytest.mark.parametrize("field", ["owner", "people", "crowd"])
def test_assigning_a_field_its_own_value_sends_nothing(
    field: str, mode: Any
) -> None:
    """The objects stay, and no request is sent."""
    card = Card(json=BODY, write_mode=mode)
    value = getattr(card, field)
    held = [value] if field == "owner" else list(value)
    with requests_mock.Mocker() as mocker:
        setattr(card, field, getattr(card, field))
        card.save()
        assert mocker.call_count == 0
    now = getattr(card, field)
    for before, after in zip(
        held, [now] if field == "owner" else now, strict=True
    ):
        assert after is before


def test_unsaved_changes_of_an_embedded_object_stay() -> None:
    """The object that the field holds is not replaced by a new one."""
    card = Card(json=BODY)
    owner = card.owner
    owner.name = "changed"
    card.owner = card.owner
    assert card.owner is owner
    assert card.owner.name == "changed"


@pytest.mark.parametrize("mode", ["lazy", "eager"])
def test_another_key_is_still_sent(mode: Any) -> None:
    """Only the same key is no change."""
    card = Card(json=BODY, write_mode=mode)
    with requests_mock.Mocker() as mocker:
        mocker.patch("https://embedded.example/cards/1/", json={})
        card.owner = Person(id=9)
        card.people = [Person(id=7)]
        card.save()
    sent = [r.json() for r in mocker.request_history]
    if mode == "eager":
        assert sent == [{"owner": 9}, {"people": [7]}]
    else:
        assert sent == [{"owner": 9, "people": [7]}]
