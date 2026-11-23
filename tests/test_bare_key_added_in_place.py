"""A bare key added in place to a collection of relations is refused."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
import requests_mock

from eazyrest import API, FieldConversionError, JSONObject, json_object

api = API("https://bare.example/")


@json_object(pk="slug")
class Label(JSONObject):
    """A model with a text key."""

    class_url = "/labels/"
    slug: str


@json_object
class Note(JSONObject):
    """A model with a list, a deque, and a set of labels."""

    class_url = "/notes/"
    id: int
    labels: list[Label]
    queue: list[Label]
    group: set[Label]


Label.register_api(api)
Note.register_api(api)

BODY = {"id": 1, "labels": ["a"], "queue": ["a"], "group": ["a"]}

ADDS: dict[str, Callable[[Note], Any]] = {
    "append": lambda n: n.labels.append("b"),  # type: ignore[arg-type]
    "insert": lambda n: n.labels.insert(0, "b"),  # type: ignore[arg-type]
    "extend": lambda n: n.labels.extend(["b"]),  # type: ignore[list-item]
    "item": lambda n: n.labels.__setitem__(0, "b"),  # type: ignore[call-overload]
}


@pytest.mark.parametrize("add", ADDS)
@pytest.mark.parametrize("full", [False, True])
def test_a_bare_key_in_a_sequence_names_the_field_and_position(
    add: str, full: bool
) -> None:
    """The error comes before any request and names the position."""
    note = Note(json=BODY)
    ADDS[add](note)
    index = "[0]" if add in ("insert", "item") else "[1]"
    with requests_mock.Mocker() as mocker:
        with pytest.raises(
            FieldConversionError, match=r"Note\.labels"
        ) as caught:
            note.save(full=full)
        assert mocker.call_count == 0
    assert index in str(caught.value)
    assert "related object" in str(caught.value)


@pytest.mark.parametrize("full", [False, True])
def test_a_bare_key_in_a_set_names_the_field(full: bool) -> None:
    """The set has no position to name."""
    note = Note(json=BODY)
    note.group.add("b")  # type: ignore[arg-type]
    with requests_mock.Mocker() as mocker:
        with pytest.raises(FieldConversionError, match=r"Note\.group"):
            note.save(full=full)
        assert mocker.call_count == 0


def test_a_related_object_added_in_place_is_sent() -> None:
    """An object is the item to add."""
    note = Note(json=BODY)
    note.labels.append(Label(slug="b"))
    note.group.add(Label(slug="b"))
    with requests_mock.Mocker() as mocker:
        mocker.patch("https://bare.example/notes/1/", json={})
        note.save()
    assert mocker.request_history[0].json() == {
        "labels": ["a", "b"],
        "group": ["a", "b"],
    }


def test_a_bare_key_that_is_assigned_is_still_taken() -> None:
    """An assignment reads the key as an object."""
    note = Note(json=BODY)
    note.labels = ["b"]  # type: ignore[list-item]
    assert note.labels == [Label(slug="b")]
