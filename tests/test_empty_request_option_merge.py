"""Request options laid over others keep them unless they say otherwise.

``headers`` and ``hooks`` merge whenever the options laid on top hold a
mapping, an empty one included. ``None`` means the option is absent, so the
options under it apply, as they do for requests. Every place that lays options
over others merges them the same way: a write's ``request_options`` over the
open ``write_options()`` blocks, nested blocks, a request through a view over
the view's options, and a view of a view.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from eazyrest import API, JSONObject, json_object
from eazyrest.api import _merge_request_options

BASE = "https://option-merge.example/v1/"
URL = BASE + "notes/1/"
api = API(BASE)


@json_object
class Note(JSONObject):
    """Model whose writes the tests send."""

    class_url = "/notes/"

    id: int
    text: str


Note.register_api(api)

Merge = Callable[[Any, dict[str, Any], dict[str, Any]], Any]


def _call_over_block(
    requests_mock: Any, base: dict[str, Any], top: dict[str, Any]
) -> Any:
    """Save with ``top`` as the call's options inside a block of ``base``."""
    mocked = requests_mock.patch(URL, json={"id": 1, "text": "b"})
    note = Note(json={"id": 1, "text": "a"}, write_mode="lazy")
    note.text = "b"
    with note.write_options(**base):
        note.save(request_options=top)
    return mocked.last_request


def _nested_blocks(
    requests_mock: Any, base: dict[str, Any], top: dict[str, Any]
) -> Any:
    """Save inside a block of ``top`` that is inside a block of ``base``."""
    mocked = requests_mock.patch(URL, json={"id": 1, "text": "b"})
    note = Note(json={"id": 1, "text": "a"}, write_mode="lazy")
    note.text = "b"
    with note.write_options(**base), note.write_options(**top):
        note.save()
    return mocked.last_request


def _request_over_view(
    requests_mock: Any, base: dict[str, Any], top: dict[str, Any]
) -> Any:
    """Send a request with ``top`` through a view with ``base``."""
    mocked = requests_mock.get(URL, json={})
    api._scoped(base).get(URL, **top)
    return mocked.last_request


def _view_of_a_view(
    requests_mock: Any, base: dict[str, Any], top: dict[str, Any]
) -> Any:
    """Send a request through a view of a view, with ``top`` on the inner."""
    mocked = requests_mock.get(URL, json={})
    api._scoped(base)._scoped(top).get(URL)
    return mocked.last_request


MERGES: dict[str, Merge] = {
    "a call over a block": _call_over_block,
    "nested blocks": _nested_blocks,
    "a request over a view": _request_over_view,
    "a view of a view": _view_of_a_view,
}

HEADERS = {
    "an empty mapping": ({}, {"X-Base": "b"}),
    "None": (None, {"X-Base": "b"}),
    "a mapping": ({"X-Top": "t"}, {"X-Base": "b", "X-Top": "t"}),
    "a mapping that names a header of the base": (
        {"x-base": "t"},
        {"X-Base": "t"},
    ),
}


@pytest.mark.parametrize("merge", MERGES.values(), ids=MERGES)
@pytest.mark.parametrize(("top", "expected"), HEADERS.values(), ids=HEADERS)
def test_headers_are_merged_when_the_top_options_hold_a_mapping(
    requests_mock: Any,
    merge: Merge,
    top: dict[str, str] | None,
    expected: dict[str, str],
) -> None:
    """An empty mapping adds nothing, and ``None`` is absent."""
    request = merge(
        requests_mock, {"headers": {"X-Base": "b"}}, {"headers": top}
    )

    for name, value in expected.items():
        assert request.headers[name] == value


@pytest.mark.parametrize("merge", MERGES.values(), ids=MERGES)
@pytest.mark.parametrize("top", [{}, None], ids=["empty", "none"])
def test_if_match_of_a_block_survives_empty_headers(
    requests_mock: Any, merge: Merge, top: dict[str, Any] | None
) -> None:
    """A precondition that a block sets is still sent."""
    request = merge(
        requests_mock,
        {"headers": {"If-Match": '"v1"'}},
        {"headers": top},
    )

    assert request.headers["If-Match"] == '"v1"'


HOOKS = {
    "an empty mapping": ({}, ["base"]),
    "None": (None, ["base"]),
    "a mapping": ({"response": ["top"]}, ["base", "top"]),
}


@pytest.mark.parametrize("merge", MERGES.values(), ids=MERGES)
@pytest.mark.parametrize(("top", "expected"), HOOKS.values(), ids=HOOKS)
def test_hooks_are_merged_when_the_top_options_hold_a_mapping(
    requests_mock: Any,
    merge: Merge,
    top: dict[str, list[str]] | None,
    expected: list[str],
) -> None:
    """A hook of the base still runs, and the top's runs after it."""
    calls: list[str] = []

    def hook(name: str) -> Callable[..., None]:
        def run(resp: Any, **kwargs: Any) -> None:
            calls.append(name)

        return run

    top_hooks = (
        None
        if top is None
        else {event: [hook(n) for n in names] for event, names in top.items()}
    )
    merge(
        requests_mock,
        {"hooks": {"response": [hook("base")]}},
        {"hooks": top_hooks},
    )

    assert calls == expected


def test_headers_that_differ_in_case_are_one_header() -> None:
    """The top's value replaces the base's for the same name."""
    merged = _merge_request_options(
        {"headers": {"X-Base": "b", "X-Keep": "k"}},
        {"headers": {"x-base": "t"}},
    )

    assert dict(merged["headers"].lower_items()) == {
        "x-base": "t",
        "x-keep": "k",
    }
