"""Every write of an object sends the options of its open blocks.

``save()``, ``save(full=True)``, ``delete()``, and an assignment that writes at
once send the headers of the open ``write_options()`` blocks, with those of the
call's ``request_options`` over them.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from eazyrest import API, JSONObject, json_object

BASE = "https://example.com/v1/"
URL = BASE + "notes/1/"


@json_object
class Note(JSONObject):
    """Model whose writes the tests send."""

    class_url = "/notes/"

    id: int
    text: str


Note.register_api(API(BASE))


def _lazy() -> Note:
    """Return a note whose assignments are saved by ``save()``."""
    note = Note(json={"id": 1, "text": "a"}, write_mode="lazy")
    note.text = "b"
    return note


def _save(note: Note, options: dict[str, Any]) -> None:
    """Save the pending change."""
    note.save(request_options=options)


def _save_full(note: Note, options: dict[str, Any]) -> None:
    """Save every field."""
    note.save(full=True, request_options=options)


def _delete(note: Note, options: dict[str, Any]) -> None:
    """Delete the note."""
    note.delete(request_options=options)


WRITES: dict[str, tuple[str, Callable[[Note, dict[str, Any]], None]]] = {
    "save": ("PATCH", _save),
    "save(full=True)": ("PUT", _save_full),
    "delete": ("DELETE", _delete),
}


@pytest.mark.parametrize(("method", "write"), WRITES.values(), ids=WRITES)
def test_a_write_sends_the_headers_of_the_block_and_of_the_call(
    requests_mock: Any, method: str, write: Callable[..., None]
) -> None:
    """The call's header is sent with the block's, and wins for one name."""
    mocked = requests_mock.register_uri(
        method, URL, json={"id": 1, "text": "b"}
    )
    note = _lazy()

    with note.write_options(headers={"X-Block": "b", "X-Both": "block"}):
        write(note, {"headers": {"X-Call": "c", "X-Both": "call"}})

    headers = mocked.last_request.headers
    assert (headers["X-Block"], headers["X-Call"]) == ("b", "c")
    assert headers["X-Both"] == "call"


@pytest.mark.parametrize(("method", "write"), WRITES.values(), ids=WRITES)
def test_a_write_outside_a_block_sends_only_the_calls_headers(
    requests_mock: Any, method: str, write: Callable[..., None]
) -> None:
    """With no block open, the block's header is not sent."""
    mocked = requests_mock.register_uri(
        method, URL, json={"id": 1, "text": "b"}
    )
    note = _lazy()
    with note.write_options(headers={"X-Block": "b"}):
        pass

    write(note, {"headers": {"X-Call": "c"}})

    headers = mocked.last_request.headers
    assert headers["X-Call"] == "c"
    assert "X-Block" not in headers


def test_an_eager_assignment_sends_the_headers_of_the_block(
    requests_mock: Any,
) -> None:
    """An assignment has no options of its own, so it sends the block's."""
    mocked = requests_mock.patch(URL, json={"id": 1, "text": "b"})
    note = Note(json={"id": 1, "text": "a"}, write_mode="eager")

    with note.write_options(headers={"X-Block": "b"}):
        note.text = "b"

    assert mocked.last_request.headers["X-Block"] == "b"


def test_nested_blocks_send_both_blocks_headers(requests_mock: Any) -> None:
    """Each open block adds its headers, for every kind of write."""
    patch = requests_mock.patch(URL, json={"id": 1, "text": "b"})
    note = _lazy()

    with (
        note.write_options(headers={"X-Outer": "1"}),
        note.write_options(headers={"X-Inner": "2"}),
    ):
        note.save()

    headers = patch.last_request.headers
    assert (headers["X-Outer"], headers["X-Inner"]) == ("1", "2")


@pytest.mark.parametrize(("method", "write"), WRITES.values(), ids=WRITES)
def test_a_write_refuses_options_that_it_cannot_send(
    requests_mock: Any, method: str, write: Callable[..., None]
) -> None:
    """The call's options are checked before anything is sent."""
    requests_mock.register_uri(method, URL, json={"id": 1, "text": "b"})
    note = _lazy()

    with pytest.raises(TypeError, match="cannot set json"):
        write(note, {"json": {}})

    assert requests_mock.call_count == 0
