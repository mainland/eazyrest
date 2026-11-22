"""A collection of relations holds relations, so no item is null."""

from __future__ import annotations

from collections import deque
from typing import Any

import pytest
import requests_mock

from eazyrest import API, FieldConversionError, JSONObject, json_object

api = API("https://null.example/")


@json_object
class Person(JSONObject):
    """A related model."""

    class_url = "/people/"
    id: int


@json_object
class Team(JSONObject):
    """A model with a collection of relations of every kind."""

    class_url = "/teams/"
    id: int
    as_list: list[Person]
    as_tuple: tuple[Person, ...]
    as_deque: deque[Person]
    as_set: set[Person]
    as_frozenset: frozenset[Person]


Person.register_api(api)
Team.register_api(api)

FIELDS = ["as_list", "as_tuple", "as_deque", "as_set", "as_frozenset"]
SEQUENCES = ["as_list", "as_tuple", "as_deque"]


def team(**over: Any) -> Team:
    """Return a team read from JSON, with the given members replaced."""
    body: dict[str, Any] = {"id": 1, **{field: [3] for field in FIELDS}}
    return Team(json={**body, **over})


def refused(call: Any, field: str, position: str | None = None) -> None:
    """Assert that a call raises for a null item and sends nothing."""
    with requests_mock.Mocker() as mocker:
        mocker.get(requests_mock.ANY, json=[])
        mocker.post(requests_mock.ANY, json={"id": 2})
        mocker.patch(requests_mock.ANY, json={})
        mocker.put(requests_mock.ANY, json={})
        with pytest.raises(FieldConversionError) as caught:
            call()
        assert mocker.call_count == 0
    message = str(caught.value)
    assert f"Team.{field}" in message
    assert "null" in message
    if position is not None:
        assert position in message


@pytest.mark.parametrize("field", FIELDS)
def test_a_null_item_is_refused_when_the_field_is_read(field: str) -> None:
    """The position is the item's place in the JSON."""
    refused(lambda: getattr(team(**{field: [3, None]}), field), field, "[1]")
    refused(lambda: getattr(team(**{field: [None]}), field), field, "[0]")


@pytest.mark.parametrize("field", FIELDS)
def test_a_null_item_is_refused_on_assignment(field: str) -> None:
    """Nothing is sent, and the field keeps its value."""
    obj = team()
    refused(
        lambda: setattr(obj, field, [Person(id=4), None]),
        field,
        "[1]" if field in SEQUENCES else None,
    )
    assert getattr(obj, field) == getattr(team(), field)
    obj.save()


@pytest.mark.parametrize("field", FIELDS)
def test_a_null_item_is_refused_on_create(field: str) -> None:
    """The item's position is named for a sequence."""
    refused(
        lambda: Team.create(fields={field: [None]}),
        field,
        "[0]" if field in SEQUENCES else None,
    )


@pytest.mark.parametrize("field", ["as_list", "as_deque", "as_set"])
def test_a_null_item_added_in_place_is_refused_on_save(field: str) -> None:
    """A change made in place is encoded when the object is saved."""
    obj = team()
    items = getattr(obj, field)
    (items.add if field == "as_set" else items.append)(None)
    refused(obj.save, field)


@pytest.mark.parametrize("field", FIELDS)
def test_a_null_item_in_the_json_is_refused_by_a_full_save(
    field: str,
) -> None:
    """A full save reads each field of the object it sends."""
    refused(lambda: team(**{field: [None]}).save(full=True), field)
