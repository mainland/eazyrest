"""Assigning ``None`` over an embedded object whose key is ``null`` is sent.

A server may embed a placeholder, as ``{"id": null, "name": "anon"}``, which
the field cannot read, since the object has no key. Assigning ``None`` clears
it, and the clearing is a change like any other.
"""

from __future__ import annotations

from typing import Any

import pytest
import requests_mock

from eazyrest import API, FieldConversionError, JSONObject, json_object

BASE = "https://placeholder.example/"
api = API(BASE)


@json_object
class Person(JSONObject):
    """A related model."""

    class_url = "/people/"
    id: int
    name: str


@json_object
class Card(JSONObject):
    """A model whose owner may be cleared."""

    class_url = "/cards/"
    id: int
    owner: Person | None


Person.register_api(api)
Card.register_api(api)

PLACEHOLDERS = {
    "null key": {"id": None, "name": "anon"},
    "no key": {"name": "anon"},
    "a key": {"id": 7, "name": "a"},
}


@pytest.mark.parametrize("mode", ["lazy", "eager"])
@pytest.mark.parametrize("owner", sorted(PLACEHOLDERS))
def test_none_over_an_embedded_object_is_sent(owner: str, mode: Any) -> None:
    """The ``PATCH`` clears the owner, whatever the placeholder's key."""
    card = Card(json={"id": 1, "owner": PLACEHOLDERS[owner]}, write_mode=mode)
    with requests_mock.Mocker() as mocker:
        mocker.patch(BASE + "cards/1/", json={})
        card.owner = None
        card.save()
    assert [r.json() for r in mocker.request_history] == [{"owner": None}]
    assert card.owner is None


def test_a_placeholder_that_was_not_replaced_is_still_refused() -> None:
    """Reading the field raises while the placeholder stays."""
    card = Card(json={"id": 1, "owner": PLACEHOLDERS["null key"]})
    with pytest.raises(FieldConversionError):
        _ = card.owner
