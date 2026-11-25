"""One rule says which request options a query or a write may set.

A caller may set the options that requests takes, but not a body or response
streaming, and not the query parameters of a query. A write takes ``params``.
The checks run in one order for both kinds of request.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from eazyrest import API, JSONObject, json_object
from eazyrest._models import _checked_request_options

BASE = "https://example.com/v1/"


@json_object
class Note(JSONObject):
    """Model whose requests the tests send."""

    class_url = "/notes/"

    id: int
    text: str


Note.register_api(API(BASE))

Call = Callable[[dict[str, Any]], object]


def _note() -> Note:
    """Return a note that can be written."""
    return Note(json={"id": 1, "text": "a"})


QUERIES: dict[str, Call] = {
    "filter": lambda options: Note.filter(request_options=options),
    "all": lambda options: Note.all(request_options=options),
    "get": lambda options: Note.get(request_options=options),
    "get_page": lambda options: Note.get_page(request_options=options),
    "get_or_create": lambda options: Note.get_or_create(
        id=1, request_options=options
    ),
}

WRITES: dict[str, Call] = {
    "save": lambda options: _changed().save(request_options=options),
    "delete": lambda options: _note().delete(request_options=options),
    "create": lambda options: Note.create(id=2, request_options=options),
    "write_options": lambda options: (
        _note().write_options(**options).__enter__()
    ),
}


def _changed() -> Note:
    """Return a note with a pending change."""
    note = _note()
    note.set_write_mode("lazy")
    note.text = "b"
    return note


@pytest.mark.parametrize("call", QUERIES.values(), ids=QUERIES)
@pytest.mark.parametrize(
    ("options", "match"),
    [
        ({"json": {}}, "cannot set json, because a query sends no body"),
        ({"data": "x"}, "cannot set data, because a query sends no body"),
        ({"files": {}}, "cannot set files, because a query sends no body"),
        ({"params": {"a": 1}}, "cannot set params"),
        ({"stream": True}, "Queries read their responses"),
        ({"stream": True, "json": {}}, "cannot set json"),
        ({"stream": True, "params": {}}, "cannot set params"),
        ({"method": "POST"}, "cannot set method"),
        ([("headers", {})], "must be a mapping"),
        ({"headers": []}, "headers request option must be a mapping"),
    ],
)
def test_a_query_refuses_what_it_cannot_send(
    requests_mock: Any, call: Call, options: Any, match: str
) -> None:
    """The error is raised before any request is sent."""
    with pytest.raises(TypeError, match=match):
        call(options)

    assert requests_mock.call_count == 0


@pytest.mark.parametrize("call", WRITES.values(), ids=WRITES)
@pytest.mark.parametrize(
    ("options", "match"),
    [
        ({"json": {}}, "cannot set json, because the write builds"),
        ({"data": "x"}, "cannot set data, because the write builds"),
        ({"files": {}}, "cannot set files, because the write builds"),
        ({"stream": True}, "Model writes cannot stream"),
        ({"stream": True, "json": {}}, "cannot set json"),
        ({"method": "POST"}, "cannot set method"),
        ({"headers": []}, "headers request option must be a mapping"),
    ],
)
def test_a_write_refuses_what_it_cannot_send(
    requests_mock: Any, call: Call, options: Any, match: str
) -> None:
    """The error is raised before any request is sent."""
    with pytest.raises(TypeError, match=match):
        call(options)

    assert requests_mock.call_count == 0


@pytest.mark.parametrize("kind", ["query", "write"])
@pytest.mark.parametrize(
    "options",
    [
        {},
        {"timeout": 5},
        {"headers": {"X": "1"}},
        {"allow_redirects": False, "verify": False},
    ],
)
def test_both_kinds_take_the_options_that_requests_takes(
    kind: Any, options: dict[str, Any]
) -> None:
    """A copy of the options comes back."""
    assert _checked_request_options(options, kind) == options
    assert _checked_request_options(options, kind) is not options


def test_only_a_write_takes_params() -> None:
    """A query builds its own parameters."""
    assert _checked_request_options({"params": {"a": 1}}, "write") == {
        "params": {"a": 1}
    }
    with pytest.raises(TypeError, match="cannot set params"):
        _checked_request_options({"params": {"a": 1}}, "query")


def test_a_false_stream_is_dropped_by_a_query_and_kept_by_a_write() -> None:
    """Asking for no streaming is what a query does anyway."""
    assert _checked_request_options({"stream": False}, "query") == {}
    assert _checked_request_options({"stream": False}, "write") == {
        "stream": False
    }


@pytest.mark.parametrize("kind", ["query", "write"])
def test_no_options_give_an_empty_copy(kind: Any) -> None:
    """``None`` means no options."""
    assert _checked_request_options(None, kind) == {}
