"""One rule gives the text of a scalar sent outside a JSON body.

A query and a multipart form send a boolean as ``true`` or ``false``, and any
other scalar as the text of its plain value, whatever a subclass's own
``__str__()`` says.
"""

import enum
import re
from typing import Any
from urllib.parse import parse_qsl, urlsplit

import pytest
import requests_mock

from eazyrest import API, JSONObject, json_object

BASE = "https://scalar-text.example/"
api = API(BASE)


class Level(int, enum.Enum):
    """An enum that mixes in ``int``."""

    LOW = 1


class Shout(str):
    """A string whose own text is not its value."""

    def __str__(self) -> str:
        """Return text that is not the value."""
        return "SHOUT"


class Loud(int):
    """An integer whose own text is not its value."""

    def __str__(self) -> str:
        """Return text that is not the value."""
        return "LOUD"


class Thick(float):
    """A float whose own text is not its value."""

    def __str__(self) -> str:
        """Return text that is not the value."""
        return "THICK"


@json_object
class Thing(JSONObject):
    """A model with a field of each scalar type."""

    class_url = "/things/"
    id: int
    flag: bool
    level: Level
    name: str
    size: int
    ratio: float


Thing.register_api(api)

# The field, a value for it, and the text that is sent.
CASES: list[tuple[str, Any, str]] = [
    ("flag", True, "true"),
    ("flag", False, "false"),
    ("level", Level.LOW, "1"),
    ("name", "plain", "plain"),
    ("name", Shout("quiet"), "quiet"),
    ("size", 0, "0"),
    ("size", -3, "-3"),
    ("size", Loud(3), "3"),
    ("size", 10**20, "100000000000000000000"),
    ("ratio", 2.5, "2.5"),
    ("ratio", 0.1, "0.1"),
    ("ratio", 1e100, "1e+100"),
    ("ratio", Thick(2.5), "2.5"),
]


def _id(case: tuple[str, Any, str]) -> str:
    return f"{case[0]}-{case[2]}"


def sent_in_a_query(field: str, value: Any) -> str:
    """Return the text that a query on a field sends for a value."""
    with requests_mock.Mocker() as mocker:
        mocker.get(BASE + "things/", json=[])
        list(Thing.filter(**{field: value}))
        query = parse_qsl(urlsplit(mocker.request_history[-1].url).query)
    assert [name for name, _ in query] == [field]
    return str(query[0][1])


def sent_in_a_form(field: str, value: Any) -> str:
    """Return the text that a multipart write sends for a field's value."""
    with requests_mock.Mocker() as mocker:
        mocker.post(BASE + "things/", json={"id": 1})
        Thing.create(files={"upload": b"x"}, **{field: value})
        body = mocker.request_history[-1].body
    assert isinstance(body, bytes)
    found = re.search(
        rb'name="' + field.encode() + rb'"\r\n\r\n(.*?)\r\n--', body, re.DOTALL
    )
    assert found is not None
    return found.group(1).decode()


@pytest.mark.parametrize("case", CASES, ids=_id)
def test_a_query_sends_the_text_of_the_plain_value(
    case: tuple[str, Any, str],
) -> None:
    """A query sends a boolean as JSON does, and others as plain text."""
    field, value, text = case
    assert sent_in_a_query(field, value) == text


@pytest.mark.parametrize("case", CASES, ids=_id)
def test_a_form_sends_the_text_of_the_plain_value(
    case: tuple[str, Any, str],
) -> None:
    """A multipart write sends the same text as a query for one value."""
    field, value, text = case
    assert sent_in_a_form(field, value) == text
