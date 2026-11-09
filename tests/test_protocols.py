"""Tests that hooks and helpers use only what the protocols promise."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any, cast

import pytest
from requests.structures import CaseInsensitiveDict

from eazyrest import (
    API,
    APIError,
    LimitExceeded,
    NotFound,
    Response,
    follow_link_headers,
    follow_next_links,
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
