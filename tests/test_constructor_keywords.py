"""A constructor refuses every keyword before it sends a request.

Each keyword of the constructor is an assignment, and an assignment converts
its value before it loads the object. The constructor checks every name and
converts every value before its first assignment, so that a keyword that is
refused after others leaves no ``GET`` or ``PATCH`` behind it.
"""

from __future__ import annotations

from collections.abc import Callable
from functools import cached_property
from typing import Any, ClassVar, Optional

import pytest
import requests_mock

from eazyrest import (
    API,
    FieldConversionError,
    JSONObject,
    UsageError,
    json_object,
)

BASE = "https://constructor-keywords.example/"


class Base(JSONObject):
    """Models served by the mocked API."""


Base.register_api(API(BASE))


@json_object
class User(Base):
    """A related model."""

    class_url = "/users/"
    id: int


@json_object(read_only={"stamp"}, write_only={"secret"})
class Todo(Base):
    """A model with every kind of field that a keyword may name."""

    class_url = "/todos/"
    id: int
    title: str
    done: bool
    stamp: str
    secret: str
    owner: Optional[User]  # noqa: UP045


@json_object
class Region(Base):
    """A model with names that are no fields."""

    class_url = "/regions/"
    id: str
    label: str
    limit = 10

    @property
    def code(self) -> str:
        """Return a code that the program keeps."""
        return str(self.__dict__.get("_code"))

    @code.setter
    def code(self, value: str) -> None:
        self._code = value

    @cached_property
    def slow(self) -> str:
        """Return the key in capitals."""
        return self.id.upper()

    def remember(self, note: str) -> None:
        """Keep a public attribute of the program's own."""
        self.note = note


BODY = {"id": 1, "title": "t", "done": False, "stamp": "s", "owner": 7}

REFUSED: dict[str, tuple[dict[str, Any], type[Exception], str]] = {
    "a name that is no field": (
        {"tilte": "typo"},
        AttributeError,
        "no field of Todo",
    ),
    "a read-only field": ({"stamp": "x"}, AttributeError, "read-only"),
    "a value that cannot convert": (
        {"owner": "7"},
        FieldConversionError,
        r"Todo\.owner",
    ),
    "a value that cannot convert in a write-only field": (
        {"secret": object()},
        FieldConversionError,
        r"Todo\.secret",
    ),
    "a second key": ({"pk": 2}, AttributeError, "cannot change"),
}


@pytest.mark.parametrize("position", ["first", "last"])
@pytest.mark.parametrize("mode", ["lazy", "eager"])
@pytest.mark.parametrize(
    ("refused", "error", "named"), REFUSED.values(), ids=REFUSED
)
def test_a_keyword_that_is_refused_leaves_no_request_behind_any_other(
    refused: dict[str, Any],
    error: type[Exception],
    named: str,
    mode: str,
    position: str,
) -> None:
    """Nothing is sent, whichever keyword is refused and wherever it is."""
    good: dict[str, Any] = {"done": True, "title": "x"}
    keywords = (
        {**refused, "id": 1, **good}
        if position == "first"
        else {"id": 1, **good, **refused}
    )
    with requests_mock.Mocker() as mocker:
        mocker.get(BASE + "todos/1/", json=BODY)
        mocker.patch(BASE + "todos/1/", json={})
        with pytest.raises(error, match=named):
            Todo(write_mode=mode, **keywords)  # type: ignore[arg-type]
        assert mocker.call_count == 0


def test_keywords_that_are_all_taken_are_assigned_in_order() -> None:
    """The checks refuse nothing that an assignment takes."""
    with requests_mock.Mocker() as mocker:
        mocker.get(BASE + "todos/1/", json=BODY)
        mocker.patch(BASE + "todos/1/", json={})
        todo = Todo(id=1, done=True, title="x", secret="s", write_mode="eager")
        assert [
            (r.method, r.json() if r.body else None)
            for r in mocker.request_history
        ] == [
            ("GET", None),
            ("PATCH", {"done": True}),
            ("PATCH", {"title": "x"}),
            ("PATCH", {"secret": "s"}),
        ]
    assert todo.title == "x"


def test_a_keyless_model_refuses_a_keyword_before_any_other_check() -> None:
    """The refusal of a keyword for a model without a key comes first."""

    @json_object
    class Entry(Base):
        """A model without a key."""

        name: str

    with pytest.raises(UsageError):
        Entry(name="a")


NAMES: dict[str, Callable[[Region], Any]] = {
    "a property with a setter": lambda r: setattr(r, "code", "N"),
    "a public attribute set in a method": lambda r: r.remember("x"),
    "a class attribute": lambda r: setattr(r, "limit", 5),
    "a misspelled field": lambda r: setattr(r, "lbael", "x"),
}


@pytest.mark.parametrize("assign", NAMES.values(), ids=NAMES)
def test_an_assignment_to_a_name_that_is_no_field_says_so(
    assign: Callable[[Region], Any],
) -> None:
    """The message does not claim that a property has no attribute."""
    region = Region(json={"id": "north", "label": "N"})
    with pytest.raises(
        AttributeError, match=r"is no field of Region"
    ) as caught:
        assign(region)
    assert "begins with an underscore" in str(caught.value)


def test_the_names_that_may_be_assigned_are() -> None:
    """A field, pk, and a name that begins with an underscore."""
    region = Region(json={"id": "north", "label": "N"})
    region.label = "M"
    region.pk = "north"
    region._note = "x"  # type: ignore[attr-defined]
    assert region.__dict__["_note"] == "x"
    assert region.slow == "NORTH"


class _Audited(Base):
    """A base whose assignments are recorded."""

    seen: ClassVar[list[str]] = []

    def __setattr__(self, name: str, value: Any) -> None:
        if not name.startswith("_"):
            self.seen.append(name)
        super().__setattr__(name, value)


@json_object
class Audited(_Audited):
    """A model whose class defines ``__setattr__()``."""

    class_url = "/audited/"
    id: int
    name: str


def test_a_constructor_keyword_does_not_pass_through_setattr() -> None:
    """A model's ``__setattr__()`` sees an assignment and no keyword."""
    obj = Audited(json={"id": 1, "name": "a"}, write_mode="lazy")
    Audited.seen.clear()
    obj.name = "b"
    assert Audited.seen == ["name"]

    Audited.seen.clear()
    obj = Audited(json={"id": 1, "name": "a"}, name="b", write_mode="lazy")
    assert Audited.seen == []
    assert obj.name == "b"

    Audited.seen.clear()
    Audited(id=1, json={"id": 1, "name": "a"}, write_mode="lazy")
    assert Audited.seen == []
