"""What is written is plain JSON before anything checks or stores it.

A value that a caller gives to be written, and each mapping key in it, is
reduced to ``str``, ``int``, ``float``, ``bool``, or ``None`` once, where it is
encoded. The checks of an integer's digits and of a float's finiteness then see
the plain value, which no override of ``str()`` or ``bit_length()`` changes,
and the JSON that an object keeps holds the plain keys.
"""

from __future__ import annotations

import enum
from collections.abc import Callable
from typing import Any

import pytest
import requests_mock
from typing_extensions import TypedDict

from eazyrest import API, FieldConversionError, JSONObject, json_object

BASE = "https://written-reduction.example/"
LIMIT = 4300
BIG = 10**5000


class Mixed(int, enum.Enum):
    """An enum of integers whose ``str()`` is the member's name."""

    HUGE = BIG
    LARGEST = 10 ** (LIMIT + 1)
    FITS = 10 ** (LIMIT - 1)


class Named(int):
    """An integer with its own ``__str__()``."""

    def __str__(self) -> str:
        """Return text that is no number."""
        return "named"


class Short(int):
    """An integer that reports no bits."""

    def bit_length(self) -> int:
        """Report no bits."""
        return 0


class Color(str, enum.Enum):  # noqa: UP042
    """An enum of text."""

    RED = "red"


class Entry(TypedDict, total=False):
    """A mapping with one declared key."""

    name: str


@json_object
class Doc(JSONObject):
    """A model with fields of every shape that a value is written to."""

    class_url = "/docs/"
    id: int
    n: int
    anything: Any
    rows: list[int]
    table: dict[str, int]
    typed: Entry
    name: str


Doc.register_api(API(BASE))

BODY = {
    "id": 1,
    "n": 0,
    "anything": 0,
    "rows": [],
    "table": {},
    "typed": {},
    "name": "",
}

WIDE: dict[str, Callable[[int], int]] = {
    "an enum member": lambda n: Mixed.HUGE if n == BIG else Mixed.LARGEST,
    "an integer with its own str()": Named,
    "an integer with its own bit_length()": Short,
}


def _assign(field: str, value: Any) -> Doc:
    doc = Doc(id=1, write_mode="lazy")
    setattr(doc, field, [value] if field == "rows" else value)
    return doc


def _change_in_place(field: str, value: Any) -> Doc:
    doc = Doc(id=1, write_mode="lazy")
    doc.rows.append(value)
    doc.save()
    return doc


ENTRIES: dict[str, Callable[[Any], Any]] = {
    "assignment to an int": lambda v: _assign("n", v),
    "assignment to Any": lambda v: _assign("anything", v),
    "assignment to a list": lambda v: _assign("rows", v),
    "change in place": lambda v: _change_in_place("rows", v),
    "create": lambda v: Doc.create(n=v),
    "create of a list": lambda v: Doc.create(rows=[v]),
    "get_or_create defaults": lambda v: Doc.get_or_create(
        id=5, defaults={"n": v}
    ),
}


@pytest.mark.parametrize("entry", ENTRIES.values(), ids=ENTRIES)
@pytest.mark.parametrize("make", WIDE.values(), ids=WIDE)
@pytest.mark.parametrize("digits", [LIMIT + 1, LIMIT + 700])
def test_an_integer_of_too_many_digits_is_refused_whatever_its_class(
    entry: Callable[[Any], Any],
    make: Callable[[int], int],
    digits: int,
) -> None:
    """The refusal names the field, and nothing is sent or queued."""
    value = make(10 ** (digits - 1))
    with requests_mock.Mocker() as mocker:
        mocker.get(BASE + "docs/1/", json=BODY)
        mocker.get(BASE + "docs/", json=[])
        mocker.patch(BASE + "docs/1/", json={})
        mocker.post(BASE + "docs/", json={"id": 5})
        with pytest.raises(FieldConversionError, match=r"Doc\.\w+"):
            entry(value)
        assert [
            r.method for r in mocker.request_history if r.method != "GET"
        ] == []


def test_an_enum_member_of_too_many_digits_is_refused() -> None:
    """The member's own str() hides its digits from no check."""
    with pytest.raises(FieldConversionError, match=r"Doc\.n"):
        _assign("n", Mixed.LARGEST)


@pytest.mark.parametrize("entry", ENTRIES.values(), ids=ENTRIES)
def test_an_integer_of_the_most_digits_that_python_writes_is_accepted(
    entry: Callable[[Any], Any],
) -> None:
    """The bound is the same for an enum member as for a plain integer."""
    with requests_mock.Mocker() as mocker:
        mocker.get(BASE + "docs/1/", json=BODY)
        mocker.get(BASE + "docs/", json=[])
        mocker.patch(BASE + "docs/1/", json={})
        mocker.post(BASE + "docs/", json={"id": 5})
        entry(Mixed.FITS)


KEYED: dict[str, Callable[[Any], Doc]] = {
    "assignment to a mapping": lambda k: _assign("table", {k: 1}),
    "assignment to a typed dict": lambda k: _assign("typed", {k: "x"}),
    "assignment to Any": lambda k: _assign("anything", {k: 1}),
    "create": lambda k: Doc.create(table={k: 1}, typed={k: "x"}),
    "get_or_create defaults": lambda k: Doc.get_or_create(
        id=5, defaults={"table": {k: 1}}
    )[0],
}


@pytest.mark.parametrize("entry", KEYED.values(), ids=KEYED)
def test_a_mapping_key_of_a_subclass_is_kept_as_the_plain_text(
    entry: Callable[[Any], Doc],
) -> None:
    """The JSON that the object keeps holds ``str`` keys only."""
    with requests_mock.Mocker() as mocker:
        mocker.get(BASE + "docs/1/", json=BODY)
        mocker.get(BASE + "docs/", json=[])
        mocker.patch(BASE + "docs/1/", json={})
        mocker.post(BASE + "docs/", json={"id": 5})
        doc = entry(Color.RED)
        stored = [
            key
            for field in ("table", "typed", "anything")
            if isinstance(value := doc.json.get(field), dict)
            for key in value
        ]
    assert stored
    assert all(type(key) is str for key in stored)


def test_a_mapping_key_of_a_subclass_changed_in_place_is_queued_as_text() -> (
    None
):
    """A key added to a mapping that was read is stored as plain text."""
    with requests_mock.Mocker() as mocker:
        mocker.get(BASE + "docs/1/", json=BODY)
        mocker.patch(BASE + "docs/1/", json={})
        doc = Doc(id=1, write_mode="lazy")
        doc.table[Color.RED] = 1
        doc.save()
        assert mocker.last_request.json() == {"table": {"red": 1}}
    assert all(type(key) is str for key in doc.json["table"])


EXTRAS: dict[str, Any] = {
    "an enum member of too many digits": Mixed.LARGEST,
    "an integer with its own str()": Named(10**LIMIT),
    "an integer with its own bit_length()": Short(10**LIMIT),
}


@pytest.mark.parametrize("extra", EXTRAS.values(), ids=EXTRAS)
@pytest.mark.parametrize(
    "files", [None, {"upload": b"x"}], ids=["json", "form"]
)
def test_an_extra_that_json_cannot_write_is_refused_whatever_its_class(
    extra: Any, files: Any
) -> None:
    """An extra of a subclass is read as its plain value, as a field is."""
    with requests_mock.Mocker() as mocker:
        mocker.post(BASE + "docs/", json={"id": 5})
        with pytest.raises(ValueError, match=r"\[key\]"):
            Doc.create(extra=extra, files=files)
        assert mocker.call_count == 0
