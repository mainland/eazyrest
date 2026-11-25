"""An assignment is refused the same way at every entry point.

An attribute assignment, an assignment to ``pk``, and a constructor keyword
refuse the same values of the same fields with the same error, and send no
request, since each checks the value before it reads anything.
"""

from __future__ import annotations

import datetime
from collections import UserDict
from collections.abc import Callable
from typing import Any

import pytest
import requests_mock

from eazyrest import API, JSONObject, json_object

BASE = "https://assignment-refusals.example/"


class Base(JSONObject):
    """Models served by the mocked API."""


Base.register_api(API(BASE))


@json_object
class Owner(Base):
    """A related model."""

    class_url = "/owners/"
    id: int


@json_object
class Other(Base):
    """Another related model."""

    class_url = "/others/"
    id: int


@json_object(write_only={"secret"}, read_only={"stamp"})
class Thing(Base):
    """A model with a field of every kind that an assignment may name."""

    class_url = "/things/"
    id: int
    name: str
    tags: list[str]
    owner: Owner
    owners: list[Owner]
    when: datetime.datetime
    data: Any
    secret: str
    stamp: int


@json_object
class Anonymous(Base):
    """A model without a key field."""

    class_url = "/anonymous/"
    name: str


def _deep() -> Any:
    value: Any = 0
    for _ in range(128):
        value = [value]
    return value


REFUSED: list[tuple[str, Callable[[], Any]]] = [
    ("name", object),
    ("name", lambda: float("nan")),
    ("tags", lambda: "abc"),
    ("tags", lambda: 5),
    ("tags", lambda: {"a", "b"}),
    ("tags", lambda: UserDict(a=1)),
    ("owner", lambda: {"id": 2}),
    ("owner", lambda: Other(id=2)),
    ("owner", lambda: 2.5),
    ("owners", lambda: [None]),
    ("owners", lambda: Owner(id=3)),
    ("when", lambda: datetime.timedelta(1)),
    ("data", _deep),
    ("data", lambda: {1: 2}),
    ("secret", object),
    ("stamp", lambda: 2),
    ("id", lambda: 2),
    ("id", lambda: "1"),
    ("pk", lambda: 2),
    ("pk", lambda: "2"),
    ("pk", lambda: {"id": 1}),
]


def _outcome(action: Callable[[], Any]) -> str:
    try:
        action()
    except Exception as err:  # noqa: BLE001
        return f"{type(err).__name__}: {err}"
    return "accepted"


@pytest.mark.parametrize(
    ("field", "make"),
    REFUSED,
    ids=[f"{field}-{index}" for index, (field, _) in enumerate(REFUSED)],
)
def test_a_refused_value_is_refused_alike_by_assignment_and_constructor(
    field: str, make: Callable[[], Any]
) -> None:
    """Both entry points raise the same error, and send nothing."""
    value = make()
    with requests_mock.Mocker() as mock:
        thing = Thing(id=1)
        if field == "id":
            built = _outcome(lambda: Thing(pk=1, **{field: value}))
        else:
            built = _outcome(lambda: Thing(id=1, **{field: value}))
        assigned = _outcome(lambda: setattr(thing, field, value))
        assert mock.call_count == 0
    assert assigned != "accepted"
    assert built == assigned


def test_a_refused_keyword_after_a_good_one_sends_nothing() -> None:
    """The keywords before a refused one send nothing."""
    with requests_mock.Mocker() as mock:
        with pytest.raises(AttributeError, match="read-only"):
            Thing(id=1, name="a", stamp=2)
        assert mock.call_count == 0


@pytest.mark.parametrize("field", ["name", "pk"])
def test_a_keyless_model_refuses_every_assignment(field: str) -> None:
    """A model without a key field has no object to assign to."""
    with requests_mock.Mocker() as mock:
        thing = Anonymous(json={"name": "a"})
        assert _outcome(lambda: setattr(thing, field, "b")) != "accepted"
        keywords: dict[str, Any] = {field: "b"}
        assert _outcome(lambda: Anonymous(json={}, **keywords)) != "accepted"
        assert mock.call_count == 0
