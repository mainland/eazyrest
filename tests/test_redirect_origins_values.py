"""``API.redirect_origins`` is a tuple, list, or frozenset of strings.

Every request reads the attribute in one place before it is sent, so a value of
any other kind raises ``TypeError`` naming the attribute and the type found,
whatever request meets it. A value that Python would read as something else,
such as the integers of a ``bytearray`` or the characters of a string, is
refused rather than read.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import Any

import pytest
import requests_mock

from eazyrest import API, link_headers_page

BASE = "https://api.example.com/"
ORIGIN = "https://cdn.example.com"


def generated() -> Iterator[str]:
    """Return a generator of origins, which a request would use up."""
    yield ORIGIN


REFUSED: dict[str, tuple[Any, str]] = {
    "str": (ORIGIN, "str"),
    "bytes": (ORIGIN.encode(), "bytes"),
    "bytearray": (bytearray(ORIGIN.encode()), "bytearray"),
    "a list that holds an int": ([7], "int"),
    "a tuple that holds bytes": ((ORIGIN.encode(),), "bytes"),
    "a set": ({ORIGIN}, "set"),
    "a generator": (generated(), "generator"),
    "a dict": ({ORIGIN: 1}, "dict"),
    "None": (None, "NoneType"),
    "an int": (3, "int"),
}
ACCEPTED: dict[str, Any] = {
    "a tuple": (ORIGIN,),
    "a list": [ORIGIN],
    "a frozenset": frozenset({ORIGIN}),
    "an empty tuple": (),
}


def first_page(api: API) -> Any:
    """Request the first page of a collection."""
    return link_headers_page(api, api.get("/items/"))


REQUESTS: dict[str, Callable[[API], Any]] = {
    "get": lambda api: api.get("/items/"),
    "post": lambda api: api.post("/items/", json={}),
    "patch": lambda api: api.patch("/items/1/", json={}),
    "put": lambda api: api.put("/items/1/", json={}),
    "delete": lambda api: api.delete("/items/1/"),
    "first page": first_page,
}


def serve(mocker: Any) -> None:
    """Answer every request of the mocked API."""
    mocker.register_uri(
        requests_mock.ANY,
        requests_mock.ANY,
        json=[],
        headers={"Link": f'<{BASE}items/?p=2>; rel="next"'},
    )


@pytest.mark.parametrize("request_", REQUESTS.values(), ids=REQUESTS.keys())
@pytest.mark.parametrize(
    ("value", "found"), REFUSED.values(), ids=REFUSED.keys()
)
def test_a_value_of_another_kind_is_refused_before_the_request(
    request_: Callable[[API], Any], value: Any, found: str
) -> None:
    """The error names the attribute and the type, and nothing is sent."""
    api = API(BASE)
    api.redirect_origins = value
    with requests_mock.Mocker() as mocker:
        serve(mocker)
        with pytest.raises(TypeError, match="redirect_origins") as caught:
            request_(api)
        assert mocker.call_count == 0
    assert found in str(caught.value)


@pytest.mark.parametrize(
    ("value", "found"), REFUSED.values(), ids=REFUSED.keys()
)
def test_a_next_page_refuses_a_value_of_another_kind(
    value: Any, found: str
) -> None:
    """A page that has a link to follow reads the attribute as well."""
    api = API(BASE)
    with requests_mock.Mocker() as mocker:
        serve(mocker)
        page = first_page(api)
        api.redirect_origins = value
        before = mocker.call_count
        with pytest.raises(TypeError, match="redirect_origins"):
            page.next_page()
        assert mocker.call_count == before


@pytest.mark.parametrize("request_", REQUESTS.values(), ids=REQUESTS.keys())
@pytest.mark.parametrize("value", ACCEPTED.values(), ids=ACCEPTED.keys())
def test_a_tuple_list_or_frozenset_of_strings_is_read(
    request_: Callable[[API], Any], value: Any
) -> None:
    """Each collection that the attribute allows sends the request."""
    api = API(BASE)
    api.redirect_origins = value
    with requests_mock.Mocker() as mocker:
        serve(mocker)
        request_(api)
        assert mocker.call_count == 1


def test_a_subclass_attribute_is_read_at_each_request() -> None:
    """A class attribute is checked when a request is made."""

    class Subclassed(API):
        """An API whose subclass sets the attribute."""

        redirect_origins = {ORIGIN}  # type: ignore[assignment]  # noqa: RUF012

    api = Subclassed(BASE)
    with requests_mock.Mocker() as mocker:
        serve(mocker)
        with pytest.raises(TypeError, match="redirect_origins"):
            api.get("/items/")
        assert mocker.call_count == 0
