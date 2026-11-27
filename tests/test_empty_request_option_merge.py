"""Request options laid over others keep them unless they say otherwise.

``headers`` and ``hooks`` merge whenever the options laid on top hold a
mapping, an empty one included. ``None`` means the option is absent, so the
options under it apply, as they do for requests. Every place that lays options
over others merges them the same way: a write's ``request_options`` over the
open ``write_options()`` blocks, nested blocks, a request through a view over
the view's options, and a view of a view.
"""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Callable
from textwrap import dedent
from typing import Any

import pytest

from eazyrest import API, JSONObject, UsageError, json_object
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


# `None` for an option is absent at every entry point, whether or not anything
# lies under it. The API below has its own timeout and a session header, so
# that the settings under a request can be seen.
SBASE = "https://absent-option.example/v1/"
SERVER = API(SBASE)
SERVER.timeout = 1.25
SERVER.total_timeout = None
SERVER.session.headers["X-Session"] = "s"
RAN: list[str] = []


@json_object
class Item(JSONObject):
    """Model whose queries and writes the tests send."""

    class_url = "/items/"

    id: int
    text: str


Item.register_api(SERVER)


def _ran(resp: Any, **kwargs: Any) -> None:
    """Response hook that records that it ran."""
    RAN.append("ran")


@pytest.fixture
def server(requests_mock: Any) -> Any:
    """Serve the model's collection, object, and writes without a redirect."""
    RAN.clear()
    item = {"id": 1, "text": "a"}
    requests_mock.get(
        SBASE + "items/",
        json={"results": [item], "next": SBASE + "items/?page=2"},
    )
    requests_mock.get(
        SBASE + "items/?page=2", json={"results": [], "next": None}
    )
    requests_mock.post(SBASE + "items/", json=item)
    for method in ("get", "put", "patch", "post"):
        requests_mock.register_uri(method, SBASE + "items/1/", json=item)
    requests_mock.delete(SBASE + "items/1/", status_code=204)
    return requests_mock


def _lazy_item() -> Item:
    """Return an object with a pending change to its text."""
    item = Item(json={"id": 1, "text": "a"}, write_mode="lazy")
    item.text = "b"
    return item


def _view(lower: dict[str, Any]) -> Any:
    """Return the API with ``lower`` as the options of a view over it."""
    return SERVER._scoped(lower)


def _save(mock: Any, lower: dict[str, Any], top: dict[str, Any]) -> Any:
    item = _lazy_item()
    with item.write_options(**lower):
        item.save(request_options=top)
    return mock.last_request


def _assign(mock: Any, lower: dict[str, Any], top: dict[str, Any]) -> Any:
    item = Item(json={"id": 1, "text": "a"}, write_mode="eager")
    with item.write_options(**lower), item.write_options(**top):
        item.text = "b"
    return mock.last_request


def _delete(mock: Any, lower: dict[str, Any], top: dict[str, Any]) -> Any:
    item = Item(json={"id": 1, "text": "a"})
    with item.write_options(**lower):
        item.delete(request_options=top)
    return mock.last_request


def _create(mock: Any, lower: dict[str, Any], top: dict[str, Any]) -> Any:
    Item.create(text="x", api=_view(lower), request_options=top)
    return mock.last_request


def _get_or_create(
    mock: Any, lower: dict[str, Any], top: dict[str, Any]
) -> Any:
    Item.get_or_create(id=1, api=_view(lower), request_options=top)
    return mock.last_request


def _filter(mock: Any, lower: dict[str, Any], top: dict[str, Any]) -> Any:
    list(Item.filter(api=_view(lower), request_options=top))
    return mock.last_request


def _all(mock: Any, lower: dict[str, Any], top: dict[str, Any]) -> Any:
    list(Item.all(api=_view(lower), request_options=top))
    return mock.last_request


def _get(mock: Any, lower: dict[str, Any], top: dict[str, Any]) -> Any:
    Item.get(id=1, api=_view(lower), request_options=top)
    return mock.last_request


def _get_page(mock: Any, lower: dict[str, Any], top: dict[str, Any]) -> Any:
    Item.get_page(
        url=SBASE + "items/?page=2", api=_view(lower), request_options=top
    )
    return mock.last_request


def _next_page(mock: Any, lower: dict[str, Any], top: dict[str, Any]) -> Any:
    Item.get_page(api=_view(lower), request_options=top).next_page()
    return mock.last_request


def _view_method(method: str) -> Merge:
    def send(mock: Any, lower: dict[str, Any], top: dict[str, Any]) -> Any:
        getattr(_view(lower), method)(SBASE + "items/1/", **top)
        return mock.last_request

    return send


def _view_of_view(
    mock: Any, lower: dict[str, Any], top: dict[str, Any]
) -> Any:
    _view(lower)._scoped(top).get(SBASE + "items/")
    return mock.last_request


def _direct_method(method: str) -> Merge:
    def send(mock: Any, lower: dict[str, Any], top: dict[str, Any]) -> Any:
        assert not lower
        getattr(SERVER, method)(SBASE + "items/1/", **top)
        return mock.last_request

    return send


# Entry points that send options laid over options under them.
LAYERED: dict[str, Merge] = {
    "save()": _save,
    "an assignment that writes": _assign,
    "delete()": _delete,
    "create()": _create,
    "get_or_create()": _get_or_create,
    "filter()": _filter,
    "all()": _all,
    "get()": _get,
    "get_page(url=)": _get_page,
    "next_page()": _next_page,
    "a view's get()": _view_method("get"),
    "a view's post()": _view_method("post"),
    "a view's put()": _view_method("put"),
    "a view's patch()": _view_method("patch"),
    "a view's delete()": _view_method("delete"),
    "a view of a view": _view_of_view,
}

# Entry points with only the API's own settings under the request.
DIRECT: dict[str, Merge] = {
    f"the API's {method}()": _direct_method(method)
    for method in ("get", "post", "put", "patch", "delete")
}

ENTRY_POINTS = {**LAYERED, **DIRECT}

# An option, a value for it under a request, and what the request shows of it.
LOWER_OPTIONS: dict[str, dict[str, Any]] = {
    "headers": {"headers": {"X-Base": "b"}},
    "hooks": {"hooks": {"response": [_ran]}},
    "auth": {"auth": ("user", "dummy")},
    "cookies": {"cookies": {"base": "b"}},
    "timeout": {"timeout": 5},
}


def _outcome(
    mock: Any,
    run: Merge,
    lower: dict[str, Any],
    top: dict[str, Any],
) -> dict[str, Any]:
    """Return what a request shows of the options that it was sent with."""
    RAN.clear()
    request = run(mock, lower, top)
    return {
        "timeout": request.timeout,
        "authorization": request.headers.get("Authorization") is not None,
        "cookie": request.headers.get("Cookie"),
        "x-base": request.headers.get("X-Base"),
        "hook": bool(RAN),
    }


OMITTED = {
    "timeout": 1.25,
    "authorization": False,
    "cookie": None,
    "x-base": None,
    "hook": False,
}


@pytest.mark.parametrize("run", ENTRY_POINTS.values(), ids=ENTRY_POINTS)
@pytest.mark.parametrize("option", LOWER_OPTIONS)
def test_none_with_nothing_under_it_is_the_option_omitted(
    server: Any, run: Merge, option: str
) -> None:
    """The API's own timeout applies, and no option is sent as ``None``."""
    assert _outcome(server, run, {}, {option: None}) == OMITTED


@pytest.mark.parametrize("run", LAYERED.values(), ids=LAYERED)
@pytest.mark.parametrize("option", LOWER_OPTIONS)
@pytest.mark.parametrize("both", [False, True], ids=["lower", "both"])
def test_none_under_an_option_is_the_option_omitted(
    server: Any, run: Merge, option: str, both: bool
) -> None:
    """A lower ``None`` is absent too, so the API's own timeout applies."""
    top = {option: None} if both else {}
    assert _outcome(server, run, {option: None}, top) == OMITTED


@pytest.mark.parametrize("run", LAYERED.values(), ids=LAYERED)
@pytest.mark.parametrize("option", LOWER_OPTIONS)
def test_none_over_a_value_keeps_the_value(
    server: Any, run: Merge, option: str
) -> None:
    """The request that carries ``None`` carries the value under it."""
    lower = LOWER_OPTIONS[option]
    seen = _outcome(server, run, lower, {option: None})

    assert seen == _outcome(server, run, lower, {})
    assert seen != OMITTED


@pytest.mark.parametrize("run", ENTRY_POINTS.values(), ids=ENTRY_POINTS)
def test_a_header_set_to_none_removes_the_sessions_header(
    server: Any, run: Merge
) -> None:
    """Only an option's own ``None`` is absent. A header's ``None`` stays."""
    run(server, {}, {})
    assert server.last_request.headers["X-Session"] == "s"

    run(server, {}, {"headers": {"X-Session": None}})
    assert "X-Session" not in server.last_request.headers


# The reads that follow a redirect. Each takes a client and the request's
# options, and returns the ids that it read.
READS: dict[str, Callable[[Any, dict[str, Any]], list[int]]] = {
    "filter()": lambda api, top: [
        i.id for i in Item.filter(api=api, request_options=top)
    ],
    "all()": lambda api, top: [
        i.id for i in Item.all(api=api, request_options=top)
    ],
    "get()": lambda api, top: [
        Item.get(id=1, api=api, request_options=top).id
    ],
    "get_page()": lambda api, top: [
        i.id for i in Item.get_page(api=api, request_options=top)
    ],
    "a request of the API or a view": lambda api, top: [
        o["id"] for o in api.get(SBASE + "items/", **top).json()["results"]
    ],
}


@pytest.mark.parametrize("read", READS.values(), ids=READS)
@pytest.mark.parametrize("under", [False, True], ids=["alone", "under"])
def test_none_for_allow_redirects_follows_a_redirect_as_omission_does(
    requests_mock: Any,
    read: Callable[[Any, dict[str, Any]], list[int]],
    under: bool,
) -> None:
    """The redirect is followed, as when the option is omitted."""
    requests_mock.get(
        SBASE + "items/", status_code=302, headers={"Location": "/v1/final/"}
    )
    requests_mock.get(
        SBASE + "final/", json={"results": [{"id": 1, "text": "a"}]}
    )
    none = {"allow_redirects": None}

    if under:
        assert read(_view(none), {}) == [1]
    else:
        assert read(SERVER, none) == [1]


def test_a_merge_leaves_out_every_option_that_is_none() -> None:
    """``None`` is absent in either argument, and the result holds none."""
    assert _merge_request_options({"timeout": None}, {}) == {}
    assert _merge_request_options({}, {"allow_redirects": None}) == {}
    assert _merge_request_options(
        {"timeout": None, "auth": "a"}, {"timeout": None, "cert": None}
    ) == {"auth": "a"}


@pytest.mark.parametrize("view", [False, True], ids=["direct", "view"])
def test_an_unknown_option_set_to_none_refuses_a_write_before_sending(
    requests_mock: Any, view: bool
) -> None:
    """An invalid keyword still reaches requests for its refusal."""
    url = SBASE + "items/"
    requests_mock.post(url, status_code=201)
    client = _view({}) if view else SERVER

    with pytest.raises(TypeError, match="unexpected keyword argument"):
        client.post(url, json={"id": 1}, not_a_request_option=None)

    assert not requests_mock.called


@pytest.mark.parametrize("wrapped", [False, True], ids=["stock", "wrapped"])
def test_request_options_are_independent_of_the_method_at_import(
    wrapped: bool,
) -> None:
    """A wrapper at import cannot drop the timeout or an invalid keyword."""
    script = dedent("""\
        import sys
        from contextlib import nullcontext
        from unittest.mock import patch

        import pytest
        import requests
        from requests_mock import Mocker

        stock = requests.Session.request

        def forward(self, *args, **kwargs):
            return stock(self, *args, **kwargs)

        wrapped = sys.argv[1] == "True"
        method = patch.object(requests.Session, "request", forward)
        with method if wrapped else nullcontext():
            from eazyrest import API

        url = "https://request-option.example/items/"
        api = API(url)
        api.timeout = 3.5
        api.total_timeout = None
        with Mocker() as mock:
            mock.get(url, json=[])
            mock.post(url, status_code=201)
            api.get(url, timeout=None)
            assert mock.last_request.timeout == 3.5

            with pytest.raises(TypeError, match="unexpected keyword argument"):
                api.post(url, json={"id": 1}, kwargs=None)
            assert mock.call_count == 1
        """)
    subprocess.run(
        [sys.executable, "-c", script, str(wrapped)], check=True, timeout=30
    )


@pytest.mark.parametrize("run", ENTRY_POINTS.values(), ids=ENTRY_POINTS)
def test_none_for_verify_leaves_the_sessions_setting_in_force(
    server: Any, monkeypatch: pytest.MonkeyPatch, run: Merge
) -> None:
    """A CA bundle in the environment does not replace the session's."""
    monkeypatch.setattr(SERVER.session, "verify", False)
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", "/environment/bundle.pem")

    run(server, {}, {"verify": None})

    assert server.last_request.verify is False


@pytest.mark.parametrize("run", ENTRY_POINTS.values(), ids=ENTRY_POINTS)
def test_none_for_proxies_leaves_the_sessions_proxies_in_force(
    server: Any, monkeypatch: pytest.MonkeyPatch, run: Merge
) -> None:
    """A proxy in the environment does not replace the session's."""
    monkeypatch.setitem(SERVER.session.proxies, "https", "http://session:1")
    monkeypatch.setenv("HTTPS_PROXY", "http://environment:2")

    run(server, {}, {"proxies": None})

    assert server.last_request.proxies["https"] == "http://session:1"


@pytest.mark.parametrize("run", LAYERED.values(), ids=LAYERED)
def test_a_header_set_to_none_removes_a_header_that_a_lower_option_set(
    server: Any, run: Merge
) -> None:
    """The header is dropped from the request, not sent as ``None``."""
    run(server, {"headers": {"X-Base": "b", "X-Keep": "k"}}, {})
    assert server.last_request.headers["X-Base"] == "b"

    run(
        server,
        {"headers": {"X-Base": "b", "X-Keep": "k"}},
        {"headers": {"X-Base": None}},
    )
    assert "X-Base" not in server.last_request.headers
    assert server.last_request.headers["X-Keep"] == "k"


@pytest.mark.parametrize("run", LAYERED.values(), ids=LAYERED)
@pytest.mark.parametrize(
    ("lower_name", "top_name"),
    [("X-Layer", b"x-layer"), (b"X-Layer", "x-layer")],
    ids=["text-below", "bytes-below"],
)
@pytest.mark.parametrize("value", [None, "new"], ids=["remove", "replace"])
def test_text_and_bytes_names_match_in_layered_headers(
    server: Any,
    run: Merge,
    lower_name: str | bytes,
    top_name: str | bytes,
    value: str | None,
) -> None:
    """The higher layer wins when both names identify the same header."""
    run(
        server,
        {"headers": {lower_name: "old"}},
        {"headers": {top_name: value}},
    )

    assert server.called
    assert all(
        request.headers.get("X-Layer") == value
        for request in server.request_history
    )


@pytest.mark.parametrize("run", ENTRY_POINTS.values(), ids=ENTRY_POINTS)
@pytest.mark.parametrize(
    ("session_name", "option_name"),
    [("X-Default", b"x-default"), (b"X-Default", "x-default")],
    ids=["text-in-session", "bytes-in-session"],
)
@pytest.mark.parametrize("value", [None, "new"], ids=["remove", "replace"])
def test_text_and_bytes_names_match_against_session_headers(
    server: Any,
    monkeypatch: pytest.MonkeyPatch,
    run: Merge,
    session_name: str | bytes,
    option_name: str | bytes,
    value: str | None,
) -> None:
    """A request overrides its session header without modifying the session."""
    session_headers: Any = SERVER.session.headers
    monkeypatch.setitem(session_headers, session_name, "old")

    run(server, {}, {"headers": {option_name: value}})

    assert server.called
    assert all(
        request.headers.get("X-Default") == value
        for request in server.request_history
    )
    assert session_headers[session_name] == "old"


def test_a_view_reuses_hooks_from_an_iterator(server: Any) -> None:
    """An iterator gives the view a hook for each of its requests."""
    view = _view({"hooks": {"response": iter([_ran])}})

    view.get(SBASE + "items/1/")
    view.get(SBASE + "items/1/")

    assert server.call_count == len(RAN) == 2


def test_a_query_reuses_hooks_from_an_iterator_on_later_pages(
    server: Any,
) -> None:
    """The separate next-page sender runs the query's hook too."""
    list(Item.all(request_options={"hooks": {"response": iter([_ran])}}))

    assert server.call_count == len(RAN) == 2


def test_get_or_create_reuses_hooks_between_the_lookup_and_creation(
    server: Any,
) -> None:
    """The POST uses the hooks from the same options as the lookup."""
    server.get(SBASE + "items/", json={"results": [], "next": None})

    _, created = Item.get_or_create(
        id=1, request_options={"hooks": {"response": iter([_ran])}}
    )

    assert created
    assert [request.method for request in server.request_history] == [
        "GET",
        "POST",
    ]
    assert len(RAN) == 2


def test_a_write_block_reuses_hooks_from_an_iterator(server: Any) -> None:
    """Both writes run the hook supplied to the block."""
    item = _lazy_item()
    with item.write_options(hooks={"response": iter([_ran])}):
        item.save()
        item.delete()

    assert server.call_count == len(RAN) == 2


def test_a_surviving_write_block_keeps_its_hooks(server: Any) -> None:
    """Rebuilding the block stack keeps the surviving block's hooks."""
    item = _lazy_item()

    def outer_hook(response: Any, **kwargs: Any) -> None:
        RAN.append("outer")

    outer = item.write_options(hooks={"response": outer_hook})
    outer.__enter__()
    with item.write_options(hooks={"response": iter([_ran])}):
        item.save()
        with pytest.raises(UsageError, match="block that began later"):
            outer.__exit__(None, None, None)
        item.delete()

    assert server.call_count == 2
    assert RAN == ["outer", "ran", "ran"]
