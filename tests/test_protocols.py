"""Tests that hooks and helpers use only what the protocols promise."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any, cast

import pytest
from requests.structures import CaseInsensitiveDict
from typing_extensions import Self

from eazyrest import (
    API,
    APIError,
    Client,
    JSONObject,
    LimitExceeded,
    NotFound,
    Response,
    follow_link_headers,
    follow_next_links,
    json_object,
    link_headers_page,
    next_links_page,
)


class _Request:
    """A request with only the members of ``eazyrest.Request``."""

    def __init__(self, method: str) -> None:
        """Record the method.

        Args:
            method: HTTP method.
        """
        self.method = method


class _Response:
    """A response with only the members of ``eazyrest.Response``.

    It is not a ``requests.Response``, so a hook or helper that uses
    another member of one fails with it.
    """

    def __init__(
        self,
        body: object,
        *,
        url: str,
        status_code: int = 200,
        reason: str = "OK",
        headers: Mapping[str, str] | None = None,
    ) -> None:
        """Describe a response to a ``GET``.

        Args:
            body: JSON value of the body.
            url: Absolute URL of the response.
            status_code: HTTP status code.
            reason: Reason phrase.
            headers: Response headers.
        """
        self.status_code = status_code
        self.reason = reason
        self.headers: Mapping[str, str] = CaseInsensitiveDict(headers or {})
        self.content = json.dumps(body).encode()
        self.text = self.content.decode()
        self.url = url
        self.request = _Request("GET")
        self.history: Sequence[Response] = ()

    def json(self, **kwargs: Any) -> Any:
        """Decode the body.

        Args:
            **kwargs: Keyword arguments for ``json.loads()``.

        Returns:
            The decoded body.
        """
        return json.loads(self.text, **kwargs)


def test_raise_for_response_reads_only_the_protocol() -> None:
    """The default error hook builds its error from the protocol's members."""
    api = API("https://example.com/v1/")
    resp = _Response(
        {"detail": "gone", "name": ["bad"]},
        url="https://example.com/v1/items/1/",
        status_code=404,
        reason="Not Found",
    )

    with pytest.raises(NotFound) as caught:
        api.raise_for_response(resp)

    assert str(caught.value) == (
        "404 Client Error: Not Found for GET "
        "https://example.com/v1/items/1/ (body names 'name')"
    )
    # The error holds the protocol response it was given.
    assert cast(object, caught.value.response) is resp
    assert caught.value.data == {"detail": "gone", "name": ["bad"]}


def test_raise_for_response_passes_a_success() -> None:
    """A success status raises nothing, and a 5xx raises ``APIError``."""
    api = API("https://example.com/v1/")
    api.raise_for_response(_Response([], url="https://example.com/v1/"))

    with pytest.raises(APIError, match="503 Server Error"):
        api.raise_for_response(
            _Response(
                None,
                url="https://example.com/v1/",
                status_code=503,
                reason="Service Unavailable",
            )
        )


def test_json_reads_only_the_protocol() -> None:
    """``API.json()`` decodes any response with its depth limit."""
    api = API("https://example.com/v1/")
    api.max_json_depth = 1

    assert api.json(_Response([1], url="https://example.com/v1/")) == [1]
    with pytest.raises(LimitExceeded, match="max_json_depth"):
        api.json(_Response([[1]], url="https://example.com/v1/"))


def test_link_headers_read_only_the_protocol(requests_mock: Any) -> None:
    """The Link-header helpers read a first page from the protocol."""
    api = API("https://example.com/v1/")
    requests_mock.get("https://example.com/v1/items/?page=2", json=[{"id": 2}])

    def first() -> _Response:
        """Return the first page, whose Link header names the second.

        Returns:
            The first page.
        """
        return _Response(
            [{"id": 1}],
            url="https://example.com/v1/items/?page=1",
            headers={"link": '<?page=2>; rel="next"'},
        )

    page = link_headers_page(api, first())
    assert page.next_url == "https://example.com/v1/items/?page=2"
    second = page.next_page()
    assert second is not None
    assert second.items == [{"id": 2}]
    assert list(follow_link_headers(api, first())) == [{"id": 1}, {"id": 2}]


def test_next_links_read_only_the_protocol(requests_mock: Any) -> None:
    """The next-link helpers read a first page from the protocol."""
    api = API("https://example.com/v1/")
    requests_mock.get(
        "https://example.com/v1/items/?page=2",
        json={"results": [{"id": 2}], "next": None, "previous": None},
    )
    resp = _Response(
        {"results": [{"id": 1}], "next": "?page=2", "count": 2},
        url="https://example.com/v1/items/",
    )

    page = next_links_page(api, resp)
    assert page.count == 2
    second = page.next_page()
    assert second is not None
    assert second.items == [{"id": 2}]
    items = follow_next_links(api, api.json(resp), url=resp.url)
    assert list(items) == [{"id": 1}, {"id": 2}]


class _NotAnAPI:
    """A client that is not an ``API``, with the URL and limits of one."""

    base_url = "https://example.com/v1/"
    max_pages = None
    max_items = None


def test_api_is_a_client() -> None:
    """``API`` satisfies ``Client``, which mypy checks here."""
    client: Client = API("https://example.com/v1/")

    assert client.resolve_url("items/") == "https://example.com/v1/items/"


@json_object
class _BulkItem(JSONObject):
    """Model whose bulk hooks are annotated as a downstream override would."""

    class_url = "/items/"

    id: int

    @classmethod
    def bulk_get_by_pks(
        cls, pks: set[Any], *, api: Client, **kwargs: Any
    ) -> dict[Any, Self]:
        """Load objects by key, returning a ``dict`` of this class.

        Args:
            pks: Primary keys to load.
            api: The client to load them with.
            **kwargs: Context for later releases.

        Returns:
            The objects, by primary key.
        """
        return {pk: cls(id=pk, api=api) for pk in pks}

    @classmethod
    def bulk_get_by_urls(
        cls, urls: set[str], *, api: Client, **kwargs: Any
    ) -> Mapping[str, Self]:
        """Load objects by URL, returning what the default hook returns.

        Args:
            urls: URLs to load.
            api: The client to load them with.
            **kwargs: Context for later releases.

        Returns:
            The objects, by URL.
        """
        return super().bulk_get_by_urls(urls, api=api, **kwargs)


def test_bulk_hooks_may_return_a_dict_of_their_class() -> None:
    """An override may narrow the ``Mapping`` of its model to a ``dict``.

    The hooks return ``Mapping[..., Self]``, whose values are covariant, so
    the overrides above type-check under mypy, which checks this file, and
    under Pyright, whose override check compares the return types.
    """
    api = API("https://example.com/v1/")
    items: Mapping[Any, _BulkItem] = _BulkItem.bulk_get_by_pks({1}, api=api)

    assert items[1].pk == 1


def test_hooks_receive_a_client_they_can_pass_back(requests_mock: Any) -> None:
    """A hook's ``api`` goes back to the constructor and the query methods."""
    seen: list[Client] = []

    @json_object
    class Item(JSONObject):
        class_url = "/items/"

        id: int

        @classmethod
        def collection_items(
            cls, payload: Any, *, api: Client, **kwargs: Any
        ) -> list[Any]:
            seen.append(api)
            return [cls(json=item, api=api).json for item in payload]

    api = API("https://example.com/v1/")
    requests_mock.get("https://example.com/v1/items/", json=[{"id": 1}])

    assert [item.id for item in Item.filter(api=api)] == [1]
    assert seen == [api]


def test_only_an_api_sends_requests() -> None:
    """A client that is not an ``API`` is refused where it would be used."""

    @json_object
    class Item(JSONObject):
        class_url = "/items/"

        id: int

    other: Any = _NotAnAPI()
    with pytest.raises(TypeError, match="only through an API, not _NotAnAPI"):
        Item(json={"id": 1}, api=other)
    with pytest.raises(TypeError, match="only through an API, not _NotAnAPI"):
        Item.filter(api=other)
    with pytest.raises(TypeError, match="only through an API, not _NotAnAPI"):
        Item.get_page(api=other)
    with pytest.raises(TypeError, match="only through an API, not _NotAnAPI"):
        Item.create(id=1, api=other)
    page = {"results": [], "next": "https://example.com/v1/items/?page=2"}
    with pytest.raises(TypeError, match="only through an API, not _NotAnAPI"):
        list(follow_next_links(other, page))
