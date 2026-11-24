"""A stored key that cannot be built is no key that an assignment equals.

A server that sends ``7.0`` for an ``int`` key gives a field that raises when
it is read, since a float is no ``int`` key. JSON compares ``7.0`` and ``7``
equal, so assigning the key ``7`` was taken for no change, nothing was sent,
and the JSON kept ``7.0``. A stored key that its type cannot build equals no
key that a program writes.
"""

from __future__ import annotations

from typing import Any

import pytest
import requests_mock

from eazyrest import API, FieldConversionError, JSONObject, json_object

BASE = "https://unbuildable.example/"


class Base(JSONObject):
    """Models served by the mocked API."""


Base.register_api(API(BASE))


@json_object
class User(Base):
    """A related model with an ``int`` key."""

    class_url = "/users/"
    id: int
    name: str


@json_object
class Post(Base):
    """A model with a relation, a sequence of relations, and a set."""

    class_url = "/posts/"
    id: int
    title: str
    owner: User
    readers: list[User]
    watchers: set[User]


STORED: dict[str, tuple[str, Any]] = {
    "a bare float": ("owner", 7.0),
    "an embedded float key": ("owner", {"id": 7.0, "name": "a"}),
    "a bare float in a sequence": ("readers", [7.0]),
    "an embedded float key in a sequence": ("readers", [{"id": 7.0}]),
    "a bare float in a set": ("watchers", [7.0]),
}
NEW: dict[str, Any] = {
    "the key": 7,
    "an object": User(id=7),
}


def given(field: str, new: Any) -> Any:
    """Return the value to assign, for a field that holds one or many."""
    return new if field == "owner" else [new]


def expected(field: str) -> Any:
    """Return what the write sends for the key 7."""
    return 7 if field == "owner" else [7]


def post(field: str, stored: Any, mode: str) -> Post:
    """Return a post that has read a stored value for one of its fields."""
    base: dict[str, Any] = {"id": 1, "title": "t", "owner": 7, "readers": []}
    base["watchers"] = []
    return Post(
        json={**base, field: stored},
        write_mode=mode,  # type: ignore[arg-type]
    )


@pytest.mark.parametrize("mode", ["lazy", "eager"])
@pytest.mark.parametrize("new", NEW.values(), ids=NEW.keys())
@pytest.mark.parametrize(("field", "stored"), STORED.values(), ids=STORED)
def test_a_key_assigned_over_a_stored_float_is_sent(
    field: str, stored: Any, new: Any, mode: str
) -> None:
    """The write is sent, the JSON holds the key, and the field reads."""
    obj = post(field, stored, mode)
    with pytest.raises(FieldConversionError):
        getattr(obj, field)
    with requests_mock.Mocker() as mocker:
        mocker.patch(BASE + "posts/1/", json={})
        setattr(obj, field, given(field, new))
        obj.save()
        assert [r.json() for r in mocker.request_history] == [
            {field: expected(field)}
        ]
    value = getattr(obj, field)
    assert value == (
        User(id=7) if field == "owner" else type(value)([User(id=7)])
    )


@pytest.mark.parametrize(("field", "stored"), STORED.values(), ids=STORED)
def test_the_same_stored_float_is_no_change(field: str, stored: Any) -> None:
    """A key that cannot be built equals itself, so a reread is no change."""
    obj = post(field, stored, "lazy")
    obj.update_from_json({field: stored})
    with pytest.raises(FieldConversionError):
        getattr(obj, field)


def test_a_response_that_respells_a_key_drops_the_cached_object() -> None:
    """The field then raises, as a fresh object with that JSON does."""
    obj = post("owner", 7, "lazy")
    owner = obj.owner
    assert owner.pk == 7
    with requests_mock.Mocker() as mocker:
        mocker.patch(BASE + "posts/1/", json={"owner": 7.0})
        obj.title = "x"
        obj.save()
    with pytest.raises(FieldConversionError):
        obj.owner  # noqa: B018
    with pytest.raises(FieldConversionError):
        Post(json=obj.json).owner  # noqa: B018


def test_a_stored_key_that_is_none_still_compares_as_none() -> None:
    """A ``null`` is no unbuildable key, so assigning ``None`` is no change."""
    obj = Post(json={"id": 1, "title": "t", "owner": None}, write_mode="lazy")
    with requests_mock.Mocker() as mocker:
        obj.owner = None  # type: ignore[assignment]
        obj.save()
        assert mocker.call_count == 0


@pytest.mark.parametrize("stored", [7.0, {"id": 7.0, "name": "a"}])
def test_none_assigned_over_a_stored_float_is_sent(stored: Any) -> None:
    """A key that cannot be built is not ``None``, so ``None`` is a change."""
    obj = post("owner", stored, "lazy")
    with requests_mock.Mocker() as mocker:
        mocker.patch(BASE + "posts/1/", json={})
        obj.owner = None  # type: ignore[assignment]
        obj.save()
        assert [r.json() for r in mocker.request_history] == [{"owner": None}]
