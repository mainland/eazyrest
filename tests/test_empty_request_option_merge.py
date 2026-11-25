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


def _noop(resp: Any, **kwargs: Any) -> None:
    """Response hook that does nothing."""


UNMERGEABLE = {
    "headers as a list of pairs": ("headers", [("X-Top", "t")]),
    "headers as an empty list": ("headers", []),
    "headers as a string": ("headers", "X-Top: t"),
    "hooks as an empty list": ("hooks", []),
    "hooks as a list of hooks": ("hooks", [_noop]),
    "hooks as a callable": ("hooks", _noop),
}

# What the base holds, so that a view or block has options of that name or has
# none of them.
BASES = {
    "a base that has the option": {
        "headers": {"X-Base": "b"},
        "hooks": {"response": [_noop]},
    },
    "a base that has no option": {"timeout": 7},
}


@pytest.mark.parametrize("merge", MERGES.values(), ids=MERGES)
@pytest.mark.parametrize("base", BASES.values(), ids=BASES)
@pytest.mark.parametrize(
    ("name", "value"), UNMERGEABLE.values(), ids=UNMERGEABLE
)
def test_a_value_that_cannot_merge_raises_a_type_error_that_names_the_option(
    requests_mock: Any,
    merge: Merge,
    base: dict[str, Any],
    name: str,
    value: Any,
) -> None:
    """The refusal is the same whether or not the options under it have it."""
    with pytest.raises(TypeError, match=rf"The {name} request option"):
        merge(requests_mock, base, {name: value})
    assert not requests_mock.called


@pytest.mark.parametrize("name", ["headers", "hooks"])
@pytest.mark.parametrize("value", [[], [("X", "y")]], ids=["empty", "pairs"])
def test_the_base_of_a_merge_is_checked_too(name: str, value: Any) -> None:
    """A value that cannot merge is refused on either side."""
    with pytest.raises(TypeError, match=rf"The {name} request option"):
        _merge_request_options({name: value}, {})
    with pytest.raises(TypeError, match=rf"The {name} request option"):
        _merge_request_options({}, {name: value})


def test_a_view_refuses_options_that_cannot_merge_when_it_is_made() -> None:
    """No request is needed to find the option that cannot be merged."""
    with pytest.raises(TypeError, match=r"The hooks request option"):
        api._scoped({"hooks": []})


# An option and a value under it, so that None over the value can be seen to
# leave the request as it was.
ABSENT_OPTIONS: dict[str, dict[str, Any]] = {
    "headers": {"headers": {"X-Base": "b"}},
    "hooks": {"hooks": {"response": [_noop]}},
    "auth": {"auth": ("user", "dummy")},
    "cookies": {"cookies": {"base": "b"}},
    "timeout": {"timeout": 5},
}


@pytest.mark.parametrize("merge", MERGES.values(), ids=MERGES)
@pytest.mark.parametrize("option", ABSENT_OPTIONS)
def test_none_for_any_option_leaves_the_options_under_it_in_force(
    requests_mock: Any, merge: Merge, option: str
) -> None:
    """The request that carries ``None`` is the one that carries nothing."""
    base = ABSENT_OPTIONS[option]
    with_none = merge(requests_mock, base, {option: None})
    without = merge(requests_mock, base, {})

    assert with_none.headers == without.headers
    assert with_none.timeout == without.timeout
    assert with_none.timeout is not None or option != "timeout"
    assert option != "auth" or "Authorization" in with_none.headers
    assert option != "cookies" or "base=b" in with_none.headers["Cookie"]


@pytest.mark.parametrize("option", ["auth", "cookies", "timeout", "verify"])
@pytest.mark.parametrize(
    "value", [{}, [], {"a": "b"}, ["a"], 3], ids=lambda v: repr(v)
)
def test_an_option_that_does_not_merge_is_replaced_by_any_value(
    option: str, value: Any
) -> None:
    """Only ``None`` is absent. Another value takes the place of the base's."""
    merged = _merge_request_options({option: "base"}, {option: value})

    assert merged[option] == value
    assert _merge_request_options({option: "base"}, {option: None}) == {
        option: "base"
    }


@pytest.mark.parametrize("method", ["get", "post", "put", "patch", "delete"])
@pytest.mark.parametrize(
    ("name", "value"), UNMERGEABLE.values(), ids=UNMERGEABLE
)
def test_a_request_without_a_view_refuses_a_value_that_cannot_merge(
    requests_mock: Any, method: str, name: str, value: Any
) -> None:
    """A client's own request raises the error that a view's does."""
    with pytest.raises(TypeError, match=rf"The {name} request option"):
        getattr(api, method)(URL, **{name: value})
    assert not requests_mock.called
