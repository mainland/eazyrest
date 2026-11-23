"""A name that is no field is sent as given, whatever it is.

The name ``api`` reads as a class attribute that raises when the class has no
registered API, so finding out whether a name is a field must not run it.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
import requests_mock as requests_mock_module

from eazyrest import API, JSONObject, json_object

URL = "https://example.com/v1/things/"
ITEM = {"id": 1, "title": "t"}


@json_object
class Thing(JSONObject):
    """A model with no registered API, always given ``api=``."""

    class_url = "/things/"

    id: int
    title: str


@pytest.fixture
def client() -> API:
    """Return the API that each call is given."""
    return API("https://example.com/v1/")


@pytest.fixture
def server() -> Any:
    """Serve one object, and accept a creation and an update."""
    with requests_mock_module.Mocker() as mocker:
        mocker.get(URL, json=[ITEM])
        mocker.post(URL, json=ITEM)
        mocker.patch(f"{URL}1/", json={})
        mocker.put(f"{URL}1/", json={})
        yield mocker


def query_of(request: Any) -> dict[str, list[str]]:
    """Return the decoded query string of a request."""
    return parse_qs(urlsplit(request.url).query)


LOOKUPS: list[Callable[[API], Any]] = [
    lambda api: list(Thing.filter(api=api, params={"api": "v2"})),
    lambda api: Thing.get(api=api, params={"api": "v2"}),
    lambda api: Thing.get_page(api=api, params={"api": "v2"}),
    lambda api: Thing.get_or_create(api=api, params={"api": "v2"}),
]


@pytest.mark.parametrize("lookup", LOOKUPS)
def test_a_query_parameter_named_api_is_sent(
    lookup: Callable[[API], Any], client: API, server: Any
) -> None:
    """The parameter ``api`` is an ordinary name, sent as given."""
    lookup(client)

    assert [query_of(r) for r in server.request_history] == [{"api": ["v2"]}]


def test_a_creation_payload_named_api_is_sent_by_get_or_create(
    client: API, server: Any
) -> None:
    """``params=`` and ``defaults=`` may name ``api``, and both are sent."""
    server.get(URL, json=[])

    Thing.get_or_create(api=client, params={"api": "v2"})
    Thing.get_or_create(api=client, defaults={"api": "v3"}, title="t")

    posted = [r.json() for r in server.request_history if r.method == "POST"]
    assert posted == [{"api": "v2"}, {"api": "v3", "title": "t"}]


def test_a_field_named_api_is_sent_by_create(client: API, server: Any) -> None:
    """``fields=`` may name ``api``, and a keyword cannot."""
    Thing.create(api=client, fields={"api": "v2"}, title="t")

    assert server.last_request.json() == {"api": "v2", "title": "t"}


def test_an_upload_named_api_is_sent_by_create(
    client: API, server: Any
) -> None:
    """A file may be uploaded under the form key ``api``."""
    Thing.create(api=client, fields={"title": "t"}, files={"api": b"data"})

    body = server.last_request.body
    assert b'name="api"' in body
    assert b"data" in body


def test_an_upload_named_api_is_sent_by_save(client: API, server: Any) -> None:
    """``save(files=)`` may upload under the form key ``api``."""
    thing = Thing(json=ITEM, api=client)

    thing.save(files={"api": b"data"})

    body = server.last_request.body
    assert b'name="api"' in body
    assert b"data" in body


@pytest.mark.parametrize("method", ["filter", "get", "get_page"])
def test_prefetching_the_name_api_is_refused_as_no_field(
    method: str, client: API, server: Any
) -> None:
    """The name ``api`` is no field, so it cannot be prefetched."""
    with pytest.raises(ValueError, match="not a JSON-backed field"):
        getattr(Thing, method)(api=client, prefetch="api")

    assert server.call_count == 0
