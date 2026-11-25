"""A keyword of the constructor writes what an assignment of it writes.

Each keyword is an assignment, so the constructor converts its value once and
applies what the conversion gave. A one-shot iterable, which the conversion
uses up, is written as its items, and a conversion's ``encode()`` runs once.
"""

from __future__ import annotations

import ipaddress
from collections.abc import Callable, Mapping
from typing import Any, ClassVar

import pytest
import requests_mock

from eazyrest import API, Conversion, JSONObject, json_object

BASE = "https://constructor-parity.example/"

CALLS: list[Any] = []


def _encode(value: Any, cls: type[Any]) -> str:
    """Encode an address and count the call."""
    CALLS.append(value)
    return str(value)


class Base(JSONObject):
    """Models served by the mocked API."""

    conversions: ClassVar[Mapping[Any, Conversion]] = {
        ipaddress.IPv4Address: Conversion(
            decode=lambda value, cls: cls(value), encode=_encode
        )
    }


Base.register_api(API(BASE))


@json_object
class Tag(Base):
    """A related model."""

    class_url = "/tags/"
    id: int


@json_object(write_only={"secrets"})
class Post(Base):
    """A model with a field of each kind that a keyword may write."""

    class_url = "/posts/"
    id: int
    labels: list[str]
    marks: set[int]
    tags: list[Tag]
    owner: Tag | None
    hosts: list[ipaddress.IPv4Address]
    secrets: list[str]
    note: str


BODY = {
    "id": 1,
    "labels": ["x"],
    "marks": [],
    "tags": [9],
    "owner": None,
    "hosts": [],
    "note": "",
}

VALUES: dict[str, Callable[[], dict[str, Any]]] = {
    "a list": lambda: {"labels": ["a", "b"]},
    "a tuple": lambda: {"labels": ("a", "b")},
    "a generator": lambda: {"labels": (s for s in ["a", "b"])},
    "an iterator": lambda: {"labels": iter(["a", "b"])},
    "a map": lambda: {"labels": map(str, [1, 2])},
    "a set": lambda: {"marks": {3, 1, 2}},
    "a generator for a set": lambda: {"marks": (n for n in [3, 1, 2])},
    "a scalar": lambda: {"note": "n"},
    "a generator of related objects": lambda: {
        "tags": (Tag(id=i) for i in (1, 2))
    },
    "a list of related objects": lambda: {"tags": [Tag(id=1), Tag(id=2)]},
    "a related object": lambda: {"owner": Tag(id=4)},
    "a bare key": lambda: {"owner": 4},
    "a null": lambda: {"owner": None},
    "a generator for a write-only field": lambda: {
        "secrets": (s for s in ["p", "q"])
    },
    "a list for a write-only field": lambda: {"secrets": ["p", "q"]},
    "a generator through a conversion": lambda: {
        "hosts": (ipaddress.IPv4Address(a) for a in ("10.0.0.1", "10.0.0.2"))
    },
}


def _write(
    how: str, mode: str, values: dict[str, Any]
) -> tuple[list[tuple[str, str, Any]], dict[str, Any], int]:
    """Write the values by the constructor or by assignments.

    Returns:
        The requests sent, the object's JSON and the values that it reads for
        the fields written, and how many times a conversion's ``encode()`` ran.
    """
    CALLS.clear()
    with requests_mock.Mocker() as mocker:
        mocker.get(BASE + "posts/1/", json=BODY)
        mocker.patch(BASE + "posts/1/", json={})
        if how == "constructor":
            post = Post(id=1, write_mode=mode, **values)  # type: ignore[arg-type]
        else:
            post = Post(id=1, write_mode=mode)  # type: ignore[arg-type]
            for name, value in values.items():
                setattr(post, name, value)
        if mode == "lazy":
            post.save()
        sent = [
            (r.method, r.path, r.json() if r.body else None)
            for r in mocker.request_history
        ]
        read = {"json": post.json} | {
            name: getattr(post, name) for name in values if name != "secrets"
        }
    return sent, read, len(CALLS)


@pytest.mark.parametrize("mode", ["lazy", "eager"])
@pytest.mark.parametrize("make", VALUES.values(), ids=VALUES)
def test_a_keyword_writes_what_an_assignment_of_its_value_writes(
    make: Callable[[], dict[str, Any]], mode: str
) -> None:
    """The requests, the object's JSON, and the values read are the same."""
    by_keyword = _write("constructor", mode, make())
    by_assignment = _write("assignment", mode, make())
    assert by_keyword[0] == by_assignment[0]
    assert by_keyword[1] == by_assignment[1]


@pytest.mark.parametrize("mode", ["lazy", "eager"])
def test_a_one_shot_iterable_keyword_is_written_as_its_items(
    mode: str,
) -> None:
    """The items are sent, where an empty list once was."""
    sent, _, _ = _write(
        "constructor", mode, {"labels": (s for s in ["a", "b"])}
    )
    assert sent[-1] == ("PATCH", "/posts/1/", {"labels": ["a", "b"]})


@pytest.mark.parametrize("mode", ["lazy", "eager"])
def test_a_conversion_encodes_a_keyword_once(mode: str) -> None:
    """The constructor converts each keyword once, as an assignment does."""
    values = {
        "hosts": [
            ipaddress.IPv4Address("10.0.0.1"),
            ipaddress.IPv4Address("10.0.0.2"),
        ]
    }
    _, _, by_keyword = _write("constructor", mode, values)
    _, _, by_assignment = _write("assignment", mode, values)
    assert by_keyword == by_assignment


SENT: dict[str, tuple[Callable[[], dict[str, Any]], dict[str, Any]]] = {
    "a generator for a write-only field": (
        lambda: {"secrets": (s for s in ["p", "q"])},
        {"secrets": ["p", "q"]},
    ),
    "a set": (lambda: {"marks": {3, 1, 2}}, {"marks": [1, 2, 3]}),
    "a generator of related objects": (
        lambda: {"tags": (Tag(id=i) for i in (1, 2))},
        {"tags": [1, 2]},
    ),
    "a related object": (lambda: {"owner": Tag(id=4)}, {"owner": 4}),
    "a scalar": (lambda: {"note": "n"}, {"note": "n"}),
}


@pytest.mark.parametrize("mode", ["lazy", "eager"])
@pytest.mark.parametrize(("make", "body"), SENT.values(), ids=SENT)
def test_a_keyword_sends_the_body_of_its_value(
    make: Callable[[], dict[str, Any]], body: dict[str, Any], mode: str
) -> None:
    """The write that a keyword makes holds the converted value."""
    sent, _, _ = _write("constructor", mode, make())
    assert sent[-1] == ("PATCH", "/posts/1/", body)
