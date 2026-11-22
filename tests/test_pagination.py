"""Tests for collection pagination helpers."""

from __future__ import annotations

import ast
import copy
import pathlib
import pickle
import re
import socket
import threading
import traceback
from typing import Any

import pytest
import requests

from eazyrest import (
    API,
    Client,
    JSONObject,
    LimitExceeded,
    Page,
    PaginationError,
    RedirectError,
    Response,
    ResponseShapeError,
    Results,
    UsageError,
    follow_link_headers,
    follow_next_links,
    json_object,
    link_headers_page,
    next_links_page,
    pagination,
)
from eazyrest.api import _as_api, _prepare_server_url


class PagedBase(JSONObject):
    """Base object for models with paginated collection responses."""

    @classmethod
    def collection_items(
        cls, payload: Any, *, api: Client, **kwargs: Any
    ) -> Any:
        """Read every page of a collection response.

        Args:
            payload: Decoded JSON body of the collection's first page.
            api: The query's client.

        Returns:
            An iterator over the items of every page, following next links.
        """
        return follow_next_links(api, payload)


PagedBase.register_api(API("https://example.com/v1/"))


@json_object
class Item(PagedBase):
    """Model whose collection endpoint is paginated."""

    class_url = "/items/"

    id: int


class ResponsePagedBase(JSONObject):
    """Base object whose collection hook reads the response."""

    @classmethod
    def collection_items_from_response(
        cls, resp: Response, *, api: Client, **kwargs: Any
    ) -> Any:
        """Read every page, resolving links against the first page's URL.

        Args:
            resp: Response for the collection's first page.
            api: The query's client.

        Returns:
            An iterator over the items of every page, following next links.
        """
        return follow_next_links(api, resp.json(), url=resp.url)


ResponsePagedBase.register_api(API("https://example.com/v1/"))


@json_object
class Entry(ResponsePagedBase):
    """Model whose collection pages link to each other by query only."""

    class_url = "/entries/"

    id: int


class LinkPagedBase(JSONObject):
    """Base object whose collection pages are linked by Link headers."""

    @classmethod
    def collection_items_from_response(
        cls, resp: Response, *, api: Client, **kwargs: Any
    ) -> Any:
        """Read every page named by a Link header.

        Args:
            resp: Response for the collection's first page.
            api: The query's client.

        Returns:
            An iterator over the items of every page, following ``Link``
            headers.
        """
        return follow_link_headers(api, resp)


LinkPagedBase.register_api(API("https://example.com/v1/"))


@json_object
class LinkedItem(LinkPagedBase):
    """Model whose collection endpoint names its next page in a header."""

    class_url = "/linked/"

    id: int


LINKED = "https://example.com/v1/linked/"


@json_object
class Owner(PagedBase):
    """Related model whose bulk lookup sends no request."""

    class_url = "/owners/"

    id: int

    @classmethod
    def bulk_get_by_pks(
        cls, pks: set[Any], *, api: Client | None = None, **kwargs: Any
    ) -> dict[Any, Owner]:
        """Return an object for each primary key.

        Args:
            pks: Primary keys to look up.
            api: API to look the objects up with, which is ignored.

        Returns:
            A new ``Owner`` for each key in ``pks``, keyed by primary key.
        """
        del api
        return {pk: cls(json={"id": pk}) for pk in pks}


@json_object
class OwnedItem(PagedBase):
    """Paginated model with a prefetchable related field."""

    class_url = "/owned-items/"
    prefetch_batch_size = 2

    id: int
    owner: Owner


def test_list_payload_is_one_unpaginated_page(requests_mock: Any) -> None:
    """A list payload yields its items without further requests."""
    api = API("https://example.com/v1/")

    items = list(follow_next_links(api, [{"id": 1}, {"id": 2}]))

    assert items == [{"id": 1}, {"id": 2}]
    assert requests_mock.call_count == 0


def test_next_page_is_requested_only_when_reached(requests_mock: Any) -> None:
    """Each next page is requested after the previous page is consumed."""
    api = API("https://example.com/v1/")
    requests_mock.get(
        "https://example.com/v1/items/?page=2",
        json={"next": None, "results": [{"id": 3}]},
    )
    first_page = {
        "next": "https://example.com/v1/items/?page=2",
        "results": [{"id": 1}, {"id": 2}],
    }

    items = follow_next_links(api, first_page)
    first_two = [next(items), next(items)]
    calls_before_next_page = requests_mock.call_count
    rest = list(items)

    assert first_two == [{"id": 1}, {"id": 2}]
    assert calls_before_next_page == 0
    assert rest == [{"id": 3}]
    assert requests_mock.call_count == 1


@pytest.mark.parametrize("allow_other_origins", [False, True])
def test_unparsable_next_link_raises_pagination_error(
    requests_mock: Any, allow_other_origins: bool
) -> None:
    """A next link that cannot be parsed raises ``PaginationError``.

    Args:
        allow_other_origins: Whether links to other origins are followed.
    """
    api = API("https://example.com/v1/")
    first_page = {"next": "http://[::1/items/", "results": [{"id": 1}]}

    with pytest.raises(PaginationError, match="cannot be parsed"):
        list(
            follow_next_links(
                api, first_page, allow_other_origins=allow_other_origins
            )
        )

    assert requests_mock.call_count == 0


@pytest.mark.parametrize("allow_other_origins", [False, True])
def test_unrequestable_next_link_raises_pagination_error(
    requests_mock: Any, allow_other_origins: bool
) -> None:
    """A link that requests cannot send raises ``PaginationError``.

    Its port is out of range, which the origin check finds, and requests,
    when links to other origins are followed.

    Args:
        allow_other_origins: Whether links to other origins are followed.
    """
    api = API("https://example.com/v1/")
    first_page = {"next": "https://example.com:99999/x", "results": []}

    with pytest.raises(PaginationError, match="cannot be") as raised:
        list(
            follow_next_links(
                api, first_page, allow_other_origins=allow_other_origins
            )
        )

    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert requests_mock.call_count == 0


PARSED = "A page URL cannot be parsed, so nothing was sent."
REQUESTED = "A page URL cannot be requested, so nothing was sent."

# Links that a server can send, with the text of each after its scheme that no
# message may quote, and the message that a client gives for it when it refuses
# links to other origins and when it allows them.
UNUSABLE_LINKS = [
    ("http://alice:hunter2#@api.test/x", PARSED, REQUESTED),
    ("http://alice:hunter2@api.test:bad/x", PARSED, REQUESTED),
    ("http://alice:hunter2@h\u2100/x", PARSED, PARSED),
    ("http://[alice:hunter2@api.test/x", PARSED, PARSED),
]
SECRETS = ("alice", "hunter2", "h\u2100", "[alice", "bad/x")


def _assert_error_quotes_no_link(error: PaginationError, message: str) -> None:
    """Check that nothing a log prints for a page error quotes a link.

    Args:
        error: The error that refused the link.
        message: The message that the error must have.
    """
    assert str(error) == message
    assert error.args == (message,)
    assert error.__cause__ is None
    assert error.__context__ is None
    for text in (
        str(error),
        repr(error),
        "".join(traceback.format_exception(error)),
    ):
        for secret in SECRETS:
            assert secret not in text


@pytest.mark.parametrize("allow_other_origins", [False, True])
@pytest.mark.parametrize(("link", "parsed", "requested"), UNUSABLE_LINKS)
def test_next_link_that_cannot_be_used_is_not_quoted_or_chained(
    requests_mock: Any,
    link: str,
    parsed: str,
    requested: str,
    allow_other_origins: bool,
) -> None:
    """The error for a next link quotes nothing of it and has no cause.

    The rejecting error can quote the link, as the one for a port that is not a
    number does, and a traceback prints it.

    Args:
        link: The next link in the page's JSON.
        parsed: The message when links to other origins are refused.
        requested: The message when links to other origins are allowed.
        allow_other_origins: Whether links to other origins are followed.
    """
    api = API("http://api.test/")
    first_page = {"next": link, "results": []}

    with pytest.raises(PaginationError) as raised:
        list(
            follow_next_links(
                api,
                first_page,
                url="http://api.test/p",
                allow_other_origins=allow_other_origins,
            )
        )

    _assert_error_quotes_no_link(
        raised.value, requested if allow_other_origins else parsed
    )
    assert requests_mock.call_count == 0


def test_next_link_without_a_host_is_refused_without_quoting_it(
    requests_mock: Any,
) -> None:
    """A link with no host is refused as another origin, unquoted."""
    api = API("http://api.test/")
    first_page = {
        "next": "mailto:alice:hunter2@h.test/x",
        "results": [],
    }

    with pytest.raises(PaginationError) as raised:
        list(follow_next_links(api, first_page, url="http://api.test/p"))

    assert str(raised.value) == (
        "Page URL, which is not an HTTP or HTTPS URL, has a different origin "
        "from the API's base URL 'http://api.test/'. Pass "
        "allow_other_origins=True to follow it."
    )
    assert "alice" not in repr(raised.value)
    assert requests_mock.call_count == 0


def test_next_link_with_a_host_is_quoted_by_its_origin_alone(
    requests_mock: Any,
) -> None:
    """A link to another origin is quoted by the origin that requests reads."""
    api = API("http://api.test/")
    first_page = {
        "next": "https://alice:hunter2@H.test:8443/x[1]",
        "results": [],
    }

    with pytest.raises(PaginationError) as raised:
        list(follow_next_links(api, first_page, url="http://api.test/p"))

    assert str(raised.value).startswith("Page URL 'https://h.test:8443' has a")
    assert "hunter2" not in repr(raised.value)
    assert "x%5B1%5D" not in repr(raised.value)


@pytest.mark.parametrize("allow_other_origins", [False, True])
@pytest.mark.parametrize(("link", "parsed", "requested"), UNUSABLE_LINKS)
def test_page_object_link_that_cannot_be_used_is_not_quoted_or_chained(
    requests_mock: Any,
    link: str,
    parsed: str,
    requested: str,
    allow_other_origins: bool,
) -> None:
    """A page's link, read by ``Page.next_page()``, is refused the same way.

    Args:
        link: The next link in the page's JSON.
        parsed: The message when links to other origins are refused.
        requested: The message when links to other origins are allowed.
        allow_other_origins: Whether links to other origins are followed.
    """
    api = API("http://api.test/")
    requests_mock.get(
        "http://api.test/p", json={"next": link, "results": [{"id": 1}]}
    )

    def read_next_page() -> None:
        # A link that urljoin() rejects fails when the page is built, and any
        # other when the page reads it.
        page = next_links_page(
            api, api.get("/p"), allow_other_origins=allow_other_origins
        )
        page.next_page()

    with pytest.raises(PaginationError) as raised:
        read_next_page()

    _assert_error_quotes_no_link(
        raised.value, requested if allow_other_origins else parsed
    )


READ_LINKS = [
    "next links",
    "next page",
    "previous page",
    "link headers",
    "link header page",
    "link header previous page",
]


def _read_link(
    requests_mock: Any, how: str, link: str, allow_other_origins: bool
) -> object:
    """Follow a link that a server sends, from a page at ``/p``.

    Args:
        requests_mock: The mock that serves the page.
        how: How the link is read, as an item of ``READ_LINKS``.
        link: The link.
        allow_other_origins: Whether links to other origins are followed.

    Returns:
        What reading the link gives.
    """
    api = API("http://api.test/")
    if how == "next links":
        return list(
            follow_next_links(
                api,
                {"next": link, "results": []},
                url="http://api.test/p",
                allow_other_origins=allow_other_origins,
            )
        )
    if "header" in how:
        rel = "prev" if "previous" in how else "next"
        requests_mock.get(
            "http://api.test/p",
            json=[],
            headers={"Link": f'<{link}>; rel="{rel}"'},
        )
        resp = api.get("/p")
        if how == "link headers":
            return list(
                follow_link_headers(
                    api, resp, allow_other_origins=allow_other_origins
                )
            )
        page = link_headers_page(
            api, resp, allow_other_origins=allow_other_origins
        )
    else:
        key = "previous" if "previous" in how else "next"
        requests_mock.get("http://api.test/p", json={key: link, "results": []})
        page = next_links_page(
            api, api.get("/p"), allow_other_origins=allow_other_origins
        )
    return page.previous_page() if "previous" in how else page.next_page()


@pytest.mark.parametrize("allow_other_origins", [False, True])
@pytest.mark.parametrize("how", READ_LINKS)
@pytest.mark.parametrize(
    "link",
    [
        "\x01https://alice:hunter2#@h.test/",
        "\x01https://alice:hunter2\\@h.test/",
        "\x01https://alice:hunter2\\@",
        "\x08https://alice:hunter2@h.test/",
        "http://alice:hunter2\u2215@api.test/p2",
        "http://alice:hunter2@api.test:bad/x",
    ],
)
def test_link_that_requests_cannot_prepare_is_not_quoted_or_chained(
    requests_mock: Any, how: str, link: str, allow_other_origins: bool
) -> None:
    """A link that requests rejects raises ``PaginationError``, quoting none.

    The errors that requests raises for such a link quote it, or its password,
    so none is raised or chained, whichever origins are allowed.

    Args:
        how: How the link is read, as an item of ``READ_LINKS``.
        link: The link, which requests cannot prepare as an HTTP URL.
        allow_other_origins: Whether links to other origins are followed.
    """
    with pytest.raises(PaginationError) as raised:
        _read_link(requests_mock, how, link, allow_other_origins)

    _assert_error_quotes_no_link(
        raised.value, REQUESTED if allow_other_origins else PARSED
    )
    assert all(
        request.url == "http://api.test/p"
        for request in requests_mock.request_history
    )


@pytest.mark.parametrize("how", READ_LINKS)
@pytest.mark.parametrize(
    "link",
    [
        "ftp://alice:hunter2@h.test/p2",
        "FTP://alice:hunter2@/x",
        "mailto:alice:hunter2@h.test",
    ],
)
def test_link_that_is_not_http_is_refused_without_quoting_it(
    requests_mock: Any, how: str, link: str
) -> None:
    """A link that is not HTTP is refused as another origin, unquoted.

    Its text can read as a user and password, which no rule can tell apart.

    Args:
        how: How the link is read, as an item of ``READ_LINKS``.
        link: The link.
    """
    with pytest.raises(PaginationError) as raised:
        _read_link(requests_mock, how, link, False)

    _assert_error_quotes_no_link(
        raised.value,
        "Page URL, which is not an HTTP or HTTPS URL, has a different "
        "origin from the API's base URL 'http://api.test/'. Pass "
        "allow_other_origins=True to follow it.",
    )


@pytest.mark.parametrize("how", READ_LINKS)
@pytest.mark.parametrize(
    "link",
    [
        "ftp://alice:hunter2@h.test/p2",
        "FTP://alice:hunter2@/x",
        "mailto:alice:hunter2@h.test",
        "alice:hunter2@h.test",
    ],
)
def test_link_that_requests_cannot_send_is_not_quoted_if_origins_are_allowed(
    requests_mock: Any, how: str, link: str
) -> None:
    """A link with no adapter to send it is refused before it is requested.

    requests has no adapter for a scheme other than HTTP and HTTPS, and its
    error for that quotes the link.

    Args:
        how: How the link is read, as an item of ``READ_LINKS``.
        link: The link.
    """
    with pytest.raises(PaginationError) as raised:
        _read_link(requests_mock, how, link, True)

    _assert_error_quotes_no_link(raised.value, REQUESTED)


SIGNED_LINK = "https://other.test/p?token=SECRETTOKEN&sig=SECRETSIG#SECRETFRAG"
SIGNED_SECRETS = ("SECRETTOKEN", "SECRETSIG", "SECRETFRAG", "token=")


def _printed_by(error: BaseException) -> list[str]:
    """Return every text that logging an error prints from it.

    Args:
        error: The error.

    Returns:
        Its message, representation, arguments, and formatted traceback.
    """
    return [
        str(error),
        repr(error),
        repr(error.args),
        "".join(traceback.format_exception(error)),
    ]


@pytest.mark.parametrize("how", READ_LINKS)
def test_server_link_to_another_origin_is_quoted_by_its_origin_alone(
    requests_mock: Any, how: str
) -> None:
    """A link that the server sent and eazyrest refuses leaves out its query.

    Args:
        how: How the link is read, as an item of ``READ_LINKS``.
    """
    with pytest.raises(PaginationError) as raised:
        _read_link(requests_mock, how, SIGNED_LINK, False)

    assert str(raised.value) == (
        "Page URL 'https://other.test' has a different origin from the API's "
        "base URL 'http://api.test/'. Pass allow_other_origins=True to "
        "follow it."
    )
    for text in _printed_by(raised.value):
        for secret in SIGNED_SECRETS:
            assert secret not in text


def test_caller_link_to_another_origin_is_quoted_with_its_query(
    requests_mock: Any,
) -> None:
    """A page URL that the caller gave is quoted with its query and no user."""
    link = "https://alice:hunter2@other.example/p?cursor=KEPT"

    with pytest.raises(PaginationError) as raised:
        Item.get_page(url=link)

    message = str(raised.value)
    assert message.startswith(
        "Page URL 'https://other.example/p?cursor=KEPT' has a different "
        "origin from the API's base URL "
    )
    for text in _printed_by(raised.value):
        assert "hunter2" not in text
        assert "alice" not in text
    assert requests_mock.call_count == 0


@pytest.mark.parametrize(
    ("link", "quoted"),
    [
        ("https://[2001:DB8::1]:8443/p?q=X", "https://[2001:db8::1]:8443"),
        ("http://Other.TEST:8080/p", "http://other.test:8080"),
        ("https://b\u00fccher.test/p", "https://xn--bcher-kva.test"),
        ("https://alice:hunter2@other.test/p", "https://other.test"),
        ("https://alice:hunter2@H.test:8443/x[1]", "https://h.test:8443"),
    ],
)
def test_server_link_to_another_origin_is_quoted_as_requests_reads_it(
    requests_mock: Any, link: str, quoted: str
) -> None:
    """The quoted origin is the one that requests reads, without a user.

    The host is lower case, an international name is its ASCII form, and an
    IPv6 address is in brackets.

    Args:
        link: The link.
        quoted: The origin that the message quotes.
    """
    with pytest.raises(PaginationError) as raised:
        _read_link(requests_mock, "next links", link, False)

    assert str(raised.value).startswith(f"Page URL {quoted!r} has a")
    for text in _printed_by(raised.value):
        assert "hunter2" not in text
        assert "alice" not in text


def test_next_link_to_a_page_already_read_is_quoted_by_its_origin_alone(
    requests_mock: Any,
) -> None:
    """A cycle's message quotes the origin of the link that closes it."""
    api = API("http://api.test/")
    link = "http://alice:hunter2@api.test/p?cursor=KEPT"
    first_page = {"next": link, "results": []}

    with pytest.raises(PaginationError) as raised:
        list(follow_next_links(api, first_page, url=link))

    assert str(raised.value) == (
        "Next-page URL 'http://api.test' names a page that was "
        "already read, so following it would never end"
    )
    for text in _printed_by(raised.value):
        assert "hunter2" not in text
        assert "KEPT" not in text


def test_next_link_to_an_unpreparable_page_already_read_is_not_quoted(
    requests_mock: Any,
) -> None:
    """A cycle through a URL that requests cannot prepare quotes none of it."""
    api = API("http://api.test/")
    link = "http://alice:hunter2@api.test:bad/p?cursor=SECRETCURSOR"
    first_page = {"next": link, "results": []}

    with pytest.raises(PaginationError) as raised:
        list(follow_next_links(api, first_page, url=link))

    assert str(raised.value) == (
        "Next-page URL names a page that was already read, so following it "
        "would never end"
    )
    for text in _printed_by(raised.value):
        for secret in ("alice", "hunter2", "SECRET"):
            assert secret not in text


def test_page_link_to_a_page_already_read_is_quoted_by_its_origin_alone(
    requests_mock: Any,
) -> None:
    """``Page.next_page()`` reports a cycle by the origin of its link."""
    api = API("http://api.test/")
    link = "http://api.test/p?cursor=SECRETCURSOR"
    requests_mock.get(link, json={"next": link, "results": [{"id": 1}]})
    page = next_links_page(api, api.get(link))

    with pytest.raises(PaginationError) as raised:
        page.next_page()

    assert str(raised.value) == (
        "Page URL 'http://api.test' names a page that was already read, so "
        "following it would never end"
    )
    for text in _printed_by(raised.value):
        assert "SECRETCURSOR" not in text


def test_page_redirected_to_a_signed_url_names_no_part_of_it(
    requests_mock: Any,
) -> None:
    """A page request's redirect to another origin is named by the link."""
    api = API("https://api.test/")
    requests_mock.get(
        "https://api.test/p",
        status_code=302,
        headers={
            "Location": "https://files.test/obj?sig=SECRETSIG#SECRETFRAG"
        },
    )
    first_page = {"next": "https://api.test/p", "results": []}

    with pytest.raises(PaginationError) as raised:
        list(follow_next_links(api, first_page))

    assert str(raised.value) == (
        "Page request 'https://api.test/p' was redirected to a different "
        "origin from the API's base URL 'https://api.test/'. Pass "
        "allow_other_origins=True to follow it."
    )
    for text in _printed_by(raised.value):
        assert "SECRET" not in text


def _serve_link_header(link: bytes) -> tuple[socket.socket, threading.Thread]:
    """Serve one JSON page whose ``Link`` header holds raw bytes.

    Args:
        link: Bytes of the ``Link`` header's value.

    Returns:
        The listening socket, and the thread that serves one request.
    """

    def serve(listener: socket.socket) -> None:
        conn, _ = listener.accept()
        with conn:
            conn.recv(65536)
            conn.sendall(
                b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                b"Link: " + link + b"\r\nContent-Length: 2\r\n"
                b"Connection: close\r\n\r\n[]"
            )

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    # A request that never connects ends the server's wait, not the run.
    listener.settimeout(10)
    thread = threading.Thread(target=serve, args=(listener,))
    thread.start()
    return listener, thread


@pytest.mark.parametrize(
    "link",
    [
        "http://alice:hunter2@h\u2100/x".encode(),
        b"http://[alice:hunter2@h\xe5/x",
        b"http://[alice:hunter2@api.test/x",
    ],
)
def test_link_header_that_cannot_be_parsed_is_not_quoted_or_chained(
    link: bytes,
) -> None:
    """A ``Link`` header's bytes appear in no part of the page error.

    Args:
        link: The bytes of the link in the header, which may not be UTF-8.
    """
    listener, thread = _serve_link_header(b"<" + link + b'>; rel="next"')
    with listener:
        api = API(f"http://127.0.0.1:{listener.getsockname()[1]}/")
        try:
            resp = api.get("/p")
        finally:
            thread.join()

    with pytest.raises(PaginationError) as raised:
        link_headers_page(api, resp).next_page()

    _assert_error_quotes_no_link(raised.value, PARSED)


@pytest.mark.parametrize("allow_other_origins", [False, True])
@pytest.mark.parametrize(
    "location", ["/v1/items/3", "https://other.example/v1/items/3"]
)
def test_unfollowed_page_redirect_raises_redirect_error(
    requests_mock: Any, allow_other_origins: bool, location: str
) -> None:
    """A page redirect that is not followed raises ``RedirectError``.

    The origin of the target does not matter, because nothing is followed, so
    the error does not advise an option that would change nothing.

    Args:
        allow_other_origins: Whether links to other origins are followed.
        location: The redirect's target, on the page's origin or another.
    """
    api = API("https://example.com/v1/")
    requests_mock.get(
        "https://example.com/v1/items/2",
        status_code=302,
        headers={"Location": location},
    )
    first_page = {"next": "https://example.com/v1/items/2", "results": []}

    with pytest.raises(RedirectError, match="returned status 302"):
        list(
            follow_next_links(
                api._scoped({"allow_redirects": False}),
                first_page,
                allow_other_origins=allow_other_origins,
            )
        )


@pytest.mark.parametrize("allow_other_origins", [False, True])
def test_unparsable_page_redirect_raises_redirect_error(
    requests_mock: Any, allow_other_origins: bool
) -> None:
    """A page redirected to a location that cannot be parsed raises.

    The API's own guard refuses it, as for any request.

    Args:
        allow_other_origins: Whether links to other origins are followed.
    """
    api = API("https://example.com/v1/")
    requests_mock.get(
        "https://example.com/v1/items/2",
        status_code=302,
        headers={"Location": "http://[::1/x"},
    )
    first_page = {"next": "https://example.com/v1/items/2", "results": []}

    with pytest.raises(RedirectError, match="cannot be parsed"):
        list(
            follow_next_links(
                api, first_page, allow_other_origins=allow_other_origins
            )
        )


def test_page_redirected_to_a_target_without_an_authority_quotes_none_of_it(
    requests_mock: Any,
) -> None:
    """A page redirected to ``mailto:`` text quotes none of it."""
    api = API("https://example.com/v1/")
    requests_mock.get(
        "https://example.com/v1/items/2",
        status_code=302,
        headers={"Location": "mailto:alice:hunter2@h.test/x"},
    )
    first_page = {"next": "https://example.com/v1/items/2", "results": []}

    with pytest.raises(PaginationError, match="different origin") as raised:
        list(follow_next_links(api, first_page))

    assert "mailto" not in str(raised.value)
    assert "alice" not in str(raised.value)
    assert "hunter2" not in str(raised.value)


@pytest.mark.parametrize(
    "location",
    [
        "http://alice:hunter2#@h.test/",
        "https://h.test:hunter2/",
        "http://alice:hunter2#@h.test:abc/x",
        "https://alice:hunter2\u2215@h.test/",
    ],
)
def test_page_redirected_to_a_target_that_requests_cannot_prepare_quotes_none(
    requests_mock: Any, location: str
) -> None:
    """The API's guard refuses such a target, and its message quotes none.

    The page's own check leaves the target to the guard, whose error holds the
    header on its response.

    Args:
        location: The redirect's ``Location`` header.
    """
    api = API("https://api.test/")
    requests_mock.get(
        "https://api.test/p",
        status_code=302,
        headers={"Location": location},
    )
    first_page = {"next": "https://api.test/p", "results": []}

    with pytest.raises(RedirectError, match="cannot") as raised:
        list(follow_next_links(api, first_page))

    for text in (str(raised.value), repr(raised.value)):
        assert "alice" not in text
        assert "hunter2" not in text
        assert "h.test" not in text
    assert raised.value.response is not None
    assert raised.value.response.headers["Location"] == location


def test_page_redirected_to_another_origin_quotes_no_part_of_it(
    requests_mock: Any,
) -> None:
    """A page redirected to another origin quotes no part of the target."""
    api = API("https://api.test/")
    requests_mock.get(
        "https://api.test/p",
        status_code=302,
        headers={"Location": "http://Alice:hunter2@H.TEST:8080/x[1]"},
    )
    first_page = {"next": "https://api.test/p", "results": []}

    with pytest.raises(PaginationError, match="different origin") as raised:
        list(follow_next_links(api, first_page))

    assert "h.test" not in str(raised.value)
    assert "hunter2" not in repr(raised.value)


def test_session_parameters_are_not_repeated_on_links(
    requests_mock: Any,
) -> None:
    """A link that carries a session parameter is requested as it is.

    A server builds each link from the URL it received, which already holds
    the session's parameters, so adding them again would repeat them on
    every page until the URL grew too long.
    """
    api = API("https://example.com/v1/")
    api.session.params = {"api_key": "k"}
    for page, following in ((2, 3), (3, None)):
        requests_mock.get(
            f"https://example.com/v1/items/?api_key=k&page={page}",
            complete_qs=True,
            json={
                "next": following
                and f"https://example.com/v1/items/?api_key=k&page={following}",
                "results": [{"id": page}],
            },
        )
    first_page = {
        "next": "https://example.com/v1/items/?api_key=k&page=2",
        "results": [{"id": 1}],
    }

    items = list(follow_next_links(api, first_page))

    assert items == [{"id": 1}, {"id": 2}, {"id": 3}]
    assert [request.qs for request in requests_mock.request_history] == [
        {"api_key": ["k"], "page": ["2"]},
        {"api_key": ["k"], "page": ["3"]},
    ]


def test_path_next_link_resolves_against_origin(requests_mock: Any) -> None:
    """A next-page path replaces the page's path, as in RFC 3986."""
    api = API("https://example.com/v1/")
    requests_mock.get(
        "https://example.com/v1/items/?page=2",
        json={"next": None, "results": [{"id": 2}]},
    )

    items = list(
        follow_next_links(
            api, {"next": "/v1/items/?page=2", "results": [{"id": 1}]}
        )
    )

    assert items == [{"id": 1}, {"id": 2}]


def test_query_next_link_resolves_against_page_url(
    requests_mock: Any,
) -> None:
    """A query-only link keeps the path of the page that holds it."""
    api = API("https://example.com/v1/")
    requests_mock.get(
        "https://example.com/v1/items/?page=2",
        json={"next": "?page=3", "results": [{"id": 2}]},
    )
    requests_mock.get(
        "https://example.com/v1/items/?page=3",
        json={"next": None, "results": [{"id": 3}]},
    )

    items = list(
        follow_next_links(
            api,
            {"next": "?page=2", "results": [{"id": 1}]},
            url="/items/",
        )
    )

    assert items == [{"id": 1}, {"id": 2}, {"id": 3}]


def test_next_link_after_redirect_resolves_against_final_url(
    requests_mock: Any,
) -> None:
    """A redirected page's links are relative to the URL it moved to."""
    api = API("https://example.com/v1/")
    responses: list[str] = []

    def record_response(resp: requests.Response, **kwargs: Any) -> None:
        """Record the responses seen by the existing session hook.

        Args:
            resp: Response that the session received.
            kwargs: Other arguments that Requests passes to response hooks,
                which are ignored.
        """
        responses.append(resp.url)

    api.session.hooks["response"].append(record_response)
    requests_mock.get(
        "https://example.com/v1/items/?page=2",
        status_code=301,
        headers={"Location": "https://example.com/v2/items/?page=2"},
    )
    requests_mock.get(
        "https://example.com/v2/items/?page=2",
        json={"next": "?page=3", "results": [{"id": 2}]},
    )
    requests_mock.get(
        "https://example.com/v2/items/?page=3",
        json={"next": None, "results": [{"id": 3}]},
    )

    items = list(
        follow_next_links(
            api,
            {
                "next": "https://example.com/v1/items/?page=2",
                "results": [{"id": 1}],
            },
        )
    )

    assert items == [{"id": 1}, {"id": 2}, {"id": 3}]
    assert responses == [
        "https://example.com/v1/items/?page=2",
        "https://example.com/v2/items/?page=2",
        "https://example.com/v2/items/?page=3",
    ]


def test_first_page_links_resolve_against_base_url_by_default(
    requests_mock: Any,
) -> None:
    """Without ``url``, the base URL stands in for the first page's URL."""
    api = API("https://example.com/v1/")
    requests_mock.get(
        "https://example.com/v1/items/?page=2",
        json={"next": None, "results": [{"id": 2}]},
    )

    items = list(
        follow_next_links(
            api, {"next": "items/?page=2", "results": [{"id": 1}]}
        )
    )

    assert items == [{"id": 1}, {"id": 2}]


def test_custom_page_keys(requests_mock: Any) -> None:
    """Pages may name their items and next link differently."""
    api = API("https://example.com/v1/")
    requests_mock.get(
        "https://example.com/v1/items/?cursor=b",
        json={"data": [{"id": 2}]},
    )

    items = list(
        follow_next_links(
            api,
            {"data": [{"id": 1}], "links": "/v1/items/?cursor=b"},
            results_key="data",
            next_key="links",
        )
    )

    assert items == [{"id": 1}, {"id": 2}]


@pytest.mark.parametrize("last", [None, ""])
def test_empty_next_link_ends_iteration(
    requests_mock: Any, last: str | None
) -> None:
    """A null or empty next link marks the last page.

    Args:
        last: Value of the first page's ``"next"`` key, ``None`` or the empty
            string.
    """
    api = API("https://example.com/v1/")

    items = list(follow_next_links(api, {"results": [1], "next": last}))

    assert items == [1]
    assert requests_mock.call_count == 0


def test_next_link_to_current_page_raises_value_error(
    requests_mock: Any,
) -> None:
    """A page that links to itself is not read again."""
    api = API("https://example.com/v1/")
    page = {"results": [1], "next": "?page=1"}

    items = follow_next_links(api, page, url="/items/?page=1")

    assert next(items) == 1
    with pytest.raises(ValueError, match="already read"):
        next(items)
    assert requests_mock.call_count == 0


def test_next_link_cycle_raises_value_error(requests_mock: Any) -> None:
    """Pages that link back to an earlier page are not read again."""
    api = API("https://example.com/v1/")
    requests_mock.get(
        "https://example.com/v1/items/?page=2",
        json={"results": [2], "next": "?page=1"},
    )
    requests_mock.get(
        "https://example.com/v1/items/?page=1",
        json={"results": [1], "next": "?page=2"},
    )

    items = follow_next_links(
        api, {"results": [1], "next": "?page=2"}, url="/items/"
    )

    assert [next(items), next(items), next(items)] == [1, 2, 1]
    with pytest.raises(ValueError, match="already read"):
        next(items)
    assert requests_mock.call_count == 2


def test_page_without_results_raises_value_error() -> None:
    """A page mapping must hold its items under the results key."""
    api = API("https://example.com/v1/")

    with pytest.raises(ValueError, match="'results'"):
        list(follow_next_links(api, {"next": None}))


@pytest.mark.parametrize("results", ["abc", {"name": 1}, None, 3])
def test_page_results_must_be_a_list(results: Any) -> None:
    """A page whose results are not a list raises before yielding items.

    Args:
        results: Malformed value of the page's ``results`` member.
    """
    api = API("https://example.com/v1/")

    with pytest.raises(TypeError, match="'results' member"):
        list(follow_next_links(api, {"next": None, "results": results}))


def test_other_payload_raises_type_error() -> None:
    """Only lists and page mappings are collection responses."""
    api = API("https://example.com/v1/")

    with pytest.raises(TypeError, match="str"):
        list(follow_next_links(api, "items"))


def test_filter_reads_every_page_lazily(requests_mock: Any) -> None:
    """``filter()`` with the helper streams objects across pages."""
    requests_mock.get(
        "https://example.com/v1/items/",
        json={
            "next": "https://example.com/v1/items/?page=2",
            "results": [{"id": 1}],
        },
    )
    requests_mock.get(
        "https://example.com/v1/items/?page=2",
        json={"next": None, "results": [{"id": 2}]},
    )

    items = Item.filter()
    first = next(items)
    calls_after_first = requests_mock.call_count

    assert first.id == 1
    assert calls_after_first == 1
    assert [item.id for item in items] == [2]
    assert requests_mock.call_count == 2


@pytest.mark.parametrize(
    "next_url",
    [
        # TLS terminated at a proxy that Django does not trust.
        "http://example.com/v1/items/?page=2",
        # A proxy that does not pass the original Host header through.
        "http://127.0.0.1:8000/v1/items/?page=2",
        "https://example.com:8443/v1/items/?page=2",
    ],
)
def test_next_link_to_other_origin_raises_value_error(
    requests_mock: Any, next_url: str
) -> None:
    """A next-page URL on another origin is rejected before any request.

    Args:
        next_url: Absolute next-page URL whose scheme, host, or port differs
            from the API's.
    """
    api = API("https://example.com/v1/")

    with pytest.raises(ValueError, match="different origin"):
        list(follow_next_links(api, {"next": next_url, "results": []}))

    assert requests_mock.call_count == 0


def test_next_link_origin_ignores_explicit_default_port(
    requests_mock: Any,
) -> None:
    """``https://host:443`` has the same origin as ``https://host``."""
    api = API("https://Example.com/v1/")
    requests_mock.get(
        "https://example.com:443/v1/items/?page=2",
        json={"next": None, "results": [{"id": 2}]},
    )

    items = list(
        follow_next_links(
            api,
            {
                "next": "https://example.com:443/v1/items/?page=2",
                "results": [{"id": 1}],
            },
        )
    )

    assert items == [{"id": 1}, {"id": 2}]


@pytest.mark.parametrize("redirect", [False, True])
@pytest.mark.parametrize(
    "target",
    [
        r"https://other.example.net\@example.com/items/?page=2",
        r"//other.example.net\@example.com/items/?page=2",
    ],
)
def test_ambiguous_authority_cannot_bypass_origin_check(
    requests_mock: Any, target: str, redirect: bool
) -> None:
    """Check the destination Requests uses before sending credentials.

    Args:
        target: URL with an ambiguous authority that Requests sends to another
            origin.
        redirect: Whether ``target`` arrives as a redirect from a same-origin
            page rather than as the next link itself.
    """
    api = API("https://example.com/v1/")
    api.session.headers.update(
        {"Authorization": "Bearer test-token", "X-API-Key": "test-token"}
    )
    # Catch any escaped request, including one whose URL Requests normalizes.
    requests_mock.get(re.compile(".*"), json={"next": None, "results": []})
    if redirect:
        requests_mock.get(
            "https://example.com/v1/items/?page=2",
            status_code=302,
            headers={"Location": target},
        )
        next_url = "/v1/items/?page=2"
    else:
        next_url = target

    with pytest.raises(ValueError, match="different origin"):
        list(follow_next_links(api, {"next": next_url, "results": []}))

    assert requests_mock.call_count == int(redirect)
    if redirect:
        assert requests_mock.last_request.url == (
            "https://example.com/v1/items/?page=2"
        )


@pytest.mark.parametrize("redirect", [False, True])
def test_allow_other_origins_follows_next_link(
    requests_mock: Any, redirect: bool
) -> None:
    """The opt-in permits both links and redirects to another server.

    A redirect also needs the API to allow its origin, as every redirect does.

    Args:
        redirect: Whether the other server is reached by a redirect from a
            same-origin page rather than by the next link itself.
    """
    api = API("https://example.com/v1/")
    next_url = "https://cdn.example.net/items/?page=2"
    if redirect:
        api.redirect_origins = ["https://cdn.example.net"]
        requests_mock.get(
            "https://example.com/v1/items/?page=2",
            status_code=302,
            headers={"Location": next_url},
        )
        next_url = "https://example.com/v1/items/?page=2"
    requests_mock.get(
        "https://cdn.example.net/items/?page=2",
        json={"next": None, "results": [{"id": 2}]},
    )

    items = list(
        follow_next_links(
            api,
            {
                "next": next_url,
                "results": [{"id": 1}],
            },
            allow_other_origins=True,
        )
    )

    assert items == [{"id": 1}, {"id": 2}]


def test_allow_other_origins_still_checks_redirects(
    requests_mock: Any,
) -> None:
    """The opt-in does not admit a redirect to an origin the API refuses."""
    api = API("https://example.com/v1/")
    requests_mock.get(
        "https://example.com/v1/items/?page=2",
        status_code=302,
        headers={"Location": "https://cdn.example.net/items/?page=2"},
    )
    cdn = requests_mock.get(
        "https://cdn.example.net/items/?page=2",
        json={"next": None, "results": [{"id": 2}]},
    )

    items = follow_next_links(
        api,
        {
            "next": "https://example.com/v1/items/?page=2",
            "results": [{"id": 1}],
        },
        allow_other_origins=True,
    )

    assert next(items) == {"id": 1}
    with pytest.raises(requests.HTTPError, match="redirect_origins"):
        next(items)
    assert not cdn.called


@pytest.mark.parametrize(
    "target",
    [
        "https://other.example.net/items/?page=2",
        "http://example.com/v1/items/?page=2",
        "https://example.com:8443/v1/items/?page=2",
    ],
)
def test_redirect_to_other_origin_is_rejected_before_sending_credentials(
    requests_mock: Any, target: str
) -> None:
    """Every redirect hop must pass the origin check before it is sent.

    Args:
        target: URL on another origin that the second redirect names.
    """
    api = API("https://example.com/v1/")
    api.session.headers["X-API-Key"] = "test-token"
    requests_mock.get(
        "https://example.com/v1/items/?page=2",
        status_code=302,
        headers={"Location": "/v2/items/?page=2"},
    )
    requests_mock.get(
        "https://example.com/v2/items/?page=2",
        status_code=302,
        headers={"Location": target},
    )
    other_origin = requests_mock.get(
        target, json={"next": None, "results": []}
    )

    with pytest.raises(ValueError, match="different origin"):
        list(
            follow_next_links(
                api, {"next": "/v1/items/?page=2", "results": []}
            )
        )

    assert requests_mock.call_count == 2
    assert not other_origin.called


def test_filter_with_api_reads_every_page_with_it(requests_mock: Any) -> None:
    """``collection_items()`` reads later pages with the query's API."""
    tenant = API("https://tenant.example.com/v1/")
    requests_mock.get(
        "https://tenant.example.com/v1/items/",
        json={
            "results": [{"id": 1}],
            "next": "https://tenant.example.com/v1/items/?page=2",
        },
    )
    requests_mock.get(
        "https://tenant.example.com/v1/items/?page=2",
        json={"results": [{"id": 2}], "next": None},
    )

    items = list(Item.filter(api=tenant))

    assert [item.id for item in items] == [1, 2]
    assert all(item.api is tenant for item in items)
    assert requests_mock.call_count == 2
    assert {r.hostname for r in requests_mock.request_history} == {
        "tenant.example.com"
    }
    assert Item.api is not tenant


def test_collection_hook_receives_the_response(requests_mock: Any) -> None:
    """A query-only next link resolves against the first page's URL."""
    requests_mock.get(
        "https://example.com/v1/entries/?kind=a",
        json={"results": [{"id": 1}], "next": "?kind=a&page=2"},
    )
    second = requests_mock.get(
        "https://example.com/v1/entries/?kind=a&page=2",
        json={"results": [{"id": 2}], "next": None},
    )

    entries = list(Entry.filter(kind="a"))

    assert [entry.id for entry in entries] == [1, 2]
    assert second.call_count == 1


def test_link_headers_are_followed_lazily(requests_mock: Any) -> None:
    """Absolute and relative next links are followed only when reached."""
    requests_mock.get(
        LINKED,
        json=[{"id": 1}],
        headers={
            "Link": f'<{LINKED}?page=2>; rel="next", '
            f'<{LINKED}?page=3>; rel="last"'
        },
    )
    requests_mock.get(
        LINKED + "?page=2",
        json=[{"id": 2}],
        headers={"Link": '</v1/linked/?page=3>; rel="next"'},
    )
    requests_mock.get(LINKED + "?page=3", json=[{"id": 3}])

    items = LinkedItem.filter()
    first = next(items)

    assert first.id == 1
    assert requests_mock.call_count == 1
    assert [item.id for item in items] == [2, 3]
    assert requests_mock.call_count == 3


@pytest.mark.parametrize(
    "rel", ['"next last"', '"last next"', "next", '"NEXT"', '"Next prev"']
)
def test_link_headers_find_next_among_relation_types(
    requests_mock: Any, rel: str
) -> None:
    """A link is followed when ``next`` is one of its relation types.

    Args:
        rel: Value of the link's ``rel`` parameter.
    """
    requests_mock.get(
        LINKED,
        json=[{"id": 1}],
        headers={"Link": f"<{LINKED}?page=2>; rel={rel}"},
    )
    requests_mock.get(LINKED + "?page=2", json=[{"id": 2}])

    assert [item.id for item in LinkedItem.filter()] == [1, 2]
    assert requests_mock.call_count == 2


@pytest.mark.parametrize(
    ("link", "error", "match"),
    [
        (
            '<https://other.example.com/linked/?page=2>; rel="next"',
            ValueError,
            "different origin",
        ),
        (f'<{LINKED}>; rel="next"', ValueError, "already read"),
    ],
)
def test_link_headers_reject_unsafe_links(
    requests_mock: Any, link: str, error: type[Exception], match: str
) -> None:
    """A link to another origin or to a page already read raises.

    Args:
        link: ``Link`` header of the first page.
        error: Exception type that reading the collection raises.
        match: Pattern that the exception's message matches.
    """
    requests_mock.get(LINKED, json=[{"id": 1}], headers={"Link": link})

    with pytest.raises(error, match=match):
        list(LinkedItem.filter())

    assert requests_mock.call_count == 1


def test_link_header_pages_must_be_lists(requests_mock: Any) -> None:
    """A page body that is not a list raises ``TypeError``."""
    requests_mock.get(LINKED, json={"results": []})

    with pytest.raises(TypeError, match="must be JSON lists"):
        list(LinkedItem.filter())


def test_prefetch_reads_ahead_across_pages(requests_mock: Any) -> None:
    """A prefetch batch fills from later pages before the first object."""
    requests_mock.get(
        "https://example.com/v1/owned-items/",
        json={
            "next": "https://example.com/v1/owned-items/?page=2",
            "results": [{"id": 1, "owner": 10}],
        },
    )
    requests_mock.get(
        "https://example.com/v1/owned-items/?page=2",
        json={"next": None, "results": [{"id": 2, "owner": 20}]},
    )

    items = OwnedItem.filter(prefetch="owner")
    first = next(items)

    assert requests_mock.call_count == 2
    assert first.owner.pk == 10
    assert [item.owner.pk for item in items] == [20]
    assert requests_mock.call_count == 2


def test_single_callable_session_hook_is_kept(requests_mock: Any) -> None:
    """A session hook given as one callable, as requests allows, still runs."""
    api = API("https://example.com/v1/")
    seen: list[str] = []

    def record(resp: requests.Response, **kwargs: Any) -> None:
        seen.append(resp.url)

    api.session.hooks["response"] = record
    requests_mock.get(
        "https://example.com/v1/items/?page=2",
        json={"next": None, "results": [{"id": 2}]},
    )

    items = list(
        follow_next_links(
            api, {"next": "/v1/items/?page=2", "results": [{"id": 1}]}
        )
    )

    assert items == [{"id": 1}, {"id": 2}]
    assert seen == ["https://example.com/v1/items/?page=2"]


@pytest.mark.parametrize(
    "target",
    [
        "https://example.com\\@evil.example.net/items/?page=2",
        "//example.com\\@evil.example.net/items/?page=2",
    ],
)
def test_backslash_redirect_cannot_leave_api_origin(
    requests_mock: Any, target: str
) -> None:
    """A page redirect cannot hide another host behind a backslash.

    Args:
        target: Redirect target that requests sends to another host.
    """
    api = API("https://example.com/v1/")
    api.session.headers["X-API-Key"] = "test-token"
    requests_mock.get(
        "https://example.com/v1/items/?page=2",
        status_code=302,
        headers={"Location": target},
    )
    evil = requests_mock.get(
        re.compile(r"https://.*evil\.example\.net/.*"),
        json={"next": None, "results": []},
    )

    with pytest.raises(ValueError, match="different origin"):
        list(
            follow_next_links(
                api, {"next": "/v1/items/?page=2", "results": []}
            )
        )

    assert not evil.called


# Responses in the shapes that Django REST Framework's paginators produce, for
# a collection of three items in pages of two.
_PAGE_NUMBER = [
    (
        "https://example.com/v1/items/",
        {
            "count": 3,
            "next": "https://example.com/v1/items/?page=2",
            "previous": None,
            "results": [{"id": 1}, {"id": 2}],
        },
    ),
    (
        "https://example.com/v1/items/?page=2",
        {
            "count": 3,
            "next": None,
            "previous": "https://example.com/v1/items/",
            "results": [{"id": 3}],
        },
    ),
]
_LIMIT_OFFSET = [
    (
        "https://example.com/v1/items/?limit=2",
        {
            "count": 3,
            "next": "https://example.com/v1/items/?limit=2&offset=2",
            "previous": None,
            "results": [{"id": 1}, {"id": 2}],
        },
    ),
    (
        "https://example.com/v1/items/?limit=2&offset=2",
        {
            "count": 3,
            "next": None,
            "previous": "https://example.com/v1/items/?limit=2",
            "results": [{"id": 3}],
        },
    ),
]
_CURSOR = [
    (
        "https://example.com/v1/items/",
        {
            "next": "https://example.com/v1/items/?cursor=cD0y",
            "previous": None,
            "results": [{"id": 1}, {"id": 2}],
        },
    ),
    (
        "https://example.com/v1/items/?cursor=cD0y",
        {
            "next": None,
            "previous": "https://example.com/v1/items/?cursor=cj0xJnA9Mw%3D%3D",
            "results": [{"id": 3}],
        },
    ),
]


@pytest.mark.parametrize(
    "pages",
    [
        pytest.param(_PAGE_NUMBER, id="PageNumberPagination"),
        pytest.param(_LIMIT_OFFSET, id="LimitOffsetPagination"),
        pytest.param(_CURSOR, id="CursorPagination"),
    ],
)
def test_drf_paginators_are_read_in_full(
    requests_mock: Any, pages: list[tuple[str, dict[str, Any]]]
) -> None:
    """Every page shape that DRF's paginators produce reads every item.

    Args:
        pages: URL and body of each page, in order.
    """
    api = API("https://example.com/v1/")
    for url, body in pages[1:]:
        requests_mock.get(url, json=body, complete_qs=True)
    (first_url, first_page) = pages[0]

    items = list(follow_next_links(api, first_page, url=first_url))

    assert items == [{"id": 1}, {"id": 2}, {"id": 3}]
    assert requests_mock.call_count == len(pages) - 1


def test_filter_reports_the_collection_count(requests_mock: Any) -> None:
    """``filter()`` passes on the first page's ``count``, and stays lazy."""
    requests_mock.get(
        "https://example.com/v1/items/",
        json={
            "count": 3,
            "next": "https://example.com/v1/items/?page=2",
            "results": [{"id": 1}, {"id": 2}],
        },
    )
    second = requests_mock.get(
        "https://example.com/v1/items/?page=2",
        json={"count": 3, "next": None, "results": [{"id": 3}]},
    )

    items = Item.filter()

    assert isinstance(items, Results)
    assert items.count == 3
    assert not second.called
    assert [item.id for item in items] == [1, 2, 3]


@pytest.mark.parametrize(
    "payload", [[{"id": 1}], {"next": None, "results": []}, {"count": "3"}]
)
def test_count_is_none_when_the_response_has_none(payload: Any) -> None:
    """A list, a page without ``count``, or a non-integer count gives None.

    Args:
        payload: First response of the collection.
    """
    api = API("https://example.com/v1/")

    assert follow_next_links(api, payload).count is None


def test_count_comes_from_a_custom_hook(requests_mock: Any) -> None:
    """Any iterable with an integer ``count`` gives ``filter()`` its count."""

    class Counted:
        count = 42

        def __init__(self, items: list[Any]) -> None:
            self.items = items

        def __iter__(self) -> Any:
            return iter(self.items)

    @json_object
    class Custom(PagedBase):
        class_url = "/custom/"

        id: int

        @classmethod
        def collection_items(
            cls, payload: Any, *, api: Client, **kwargs: Any
        ) -> Any:
            return Counted(payload["data"])

    requests_mock.get(
        "https://example.com/v1/custom/", json={"data": [{"id": 1}]}
    )

    items = Custom.filter()

    assert items.count == 42
    assert [item.id for item in items] == [1]
    assert Custom.all().count == 42


def test_scoped_option_hooks_replace_session_hooks_on_every_page(
    requests_mock: Any,
) -> None:
    """Option hooks replace the session's, on later pages as on the first."""
    api = API("https://example.com/v1/")
    seen: list[str] = []
    api.session.hooks["response"] = [lambda resp, **_: seen.append("session")]
    scoped = api._scoped(
        {"hooks": {"response": [lambda resp, **_: seen.append("option")]}}
    )
    requests_mock.get(
        "https://example.com/v1/items/?page=2",
        json={"next": None, "results": [{"id": 2}]},
    )

    items = list(
        follow_next_links(
            scoped, {"next": "/v1/items/?page=2", "results": [{"id": 1}]}
        )
    )

    assert items == [{"id": 1}, {"id": 2}]
    assert seen == ["option"]


def test_unfollowed_redirect_of_a_later_page_raises(
    requests_mock: Any,
) -> None:
    """A page answered with a redirect that is not followed has no items."""
    api = API("https://example.com/v1/")
    requests_mock.get(
        "https://example.com/v1/items/?page=2",
        status_code=304,
        json={"next": None, "results": [{"id": 2}]},
    )

    items = follow_next_links(
        api, {"next": "/v1/items/?page=2", "results": [{"id": 1}]}
    )

    assert next(items) == {"id": 1}
    with pytest.raises(RedirectError, match="status 304"):
        next(items)


def test_class_api_is_the_registered_api_inside_a_hook(
    requests_mock: Any,
) -> None:
    """``cls.api`` in a hook is the registered API, not the query's."""
    tenant = API("https://tenant.example.com/v1/")
    seen: list[tuple[API, Client]] = []

    @json_object
    class Probe(PagedBase):
        class_url = "/probes/"

        id: int

        @classmethod
        def collection_items(
            cls, payload: Any, *, api: Client, **kwargs: Any
        ) -> Any:
            seen.append((cls.api, api))
            return payload

    requests_mock.get("https://tenant.example.com/v1/probes/", json=[])

    list(Probe.filter(api=tenant))

    assert seen == [(Probe.api, tenant)]
    assert Probe.api is not tenant


def test_class_api_is_the_registered_api_in_a_lazy_hook(
    requests_mock: Any,
) -> None:
    """A hook's generator sees the registered API as ``cls.api`` too."""
    tenant = API("https://tenant.example.com/v1/")
    seen: list[tuple[API, Client]] = []

    @json_object
    class Probe(PagedBase):
        class_url = "/probes/"

        id: int

        @classmethod
        def collection_items(
            cls, payload: Any, *, api: Client, **kwargs: Any
        ) -> Any:
            for item in payload:
                seen.append((cls.api, api))
                yield item

    requests_mock.get(
        "https://tenant.example.com/v1/probes/", json=[{"id": 1}]
    )

    list(Probe.filter(api=tenant))

    assert seen == [(Probe.api, tenant)]
    assert Probe.api is not tenant


def test_class_api_is_the_registered_api_in_the_page_hook(
    requests_mock: Any,
) -> None:
    """``cls.api`` in ``collection_page()`` is the registered API."""
    tenant = API("https://tenant.example.com/v1/")
    seen: list[tuple[API, Client]] = []

    @json_object
    class Probe(PagedBase):
        class_url = "/probes/"

        id: int

        @classmethod
        def collection_page(
            cls, resp: Any, *, api: Client, **kwargs: Any
        ) -> Page[Any]:
            seen.append((cls.api, api))
            return super().collection_page(resp, api=api, **kwargs)

    requests_mock.get(
        "https://tenant.example.com/v1/probes/", json=[{"id": 1}]
    )

    Probe.get_page(api=tenant)

    assert seen == [(Probe.api, tenant)]
    assert Probe.api is not tenant


def test_query_request_options_reach_later_pages(requests_mock: Any) -> None:
    """A query's options reach every page that its hook requests."""
    first = requests_mock.get(
        "https://example.com/v1/items/",
        json={"results": [{"id": 1}], "next": "/v1/items/?page=2"},
    )
    second = requests_mock.get(
        "https://example.com/v1/items/?page=2",
        json={"results": [{"id": 2}], "next": None},
    )

    items = list(Item.filter(request_options={"headers": {"X-Trace": "t"}}))

    assert [item.id for item in items] == [1, 2]
    assert first.last_request.headers["X-Trace"] == "t"
    assert second.last_request.headers["X-Trace"] == "t"
    assert all(item._api_override is None for item in items)


def test_scoped_query_sends_every_request_through_its_api(
    requests_mock: Any,
) -> None:
    """A hook's own request goes through ``get()``, and a page does not."""

    class Counting(API):
        """An API that counts the requests that reach ``get()``."""

        calls = 0
        """Requests so far, kept on the instance by ``get()``."""

        def get(
            self, uri: str, *args: Any, **kwargs: Any
        ) -> requests.Response:
            self.calls += 1
            return super().get(uri, *args, **kwargs)

    @json_object
    class Probe(PagedBase):
        class_url = "/probes/"

        id: int

        @classmethod
        def collection_items(
            cls, payload: Any, *, api: Client, **kwargs: Any
        ) -> Any:
            api.get("/extra/")
            return super().collection_items(payload, api=api, **kwargs)

    api = Counting("https://example.com/v1/")
    requests_mock.get(
        "https://example.com/v1/probes/",
        json={"results": [{"id": 1}], "next": "/v1/probes/?page=2"},
    )
    requests_mock.get(
        "https://example.com/v1/probes/?page=2",
        json={"results": [{"id": 2}], "next": None},
    )
    extra = requests_mock.get("https://example.com/v1/extra/", json={})

    items = list(
        Probe.filter(api=api, request_options={"headers": {"X-Trace": "t"}})
    )

    assert [item.id for item in items] == [1, 2]
    assert api.calls == 2
    assert extra.last_request.headers["X-Trace"] == "t"
    assert requests_mock.last_request.headers["X-Trace"] == "t"


def test_object_built_by_a_scoped_hook_keeps_the_view(
    requests_mock: Any,
) -> None:
    """An object that a hook builds with its ``api`` sends the options too."""
    seen: list[Client] = []

    @json_object
    class Probe(PagedBase):
        class_url = "/probes/"

        id: int
        name: str

        @classmethod
        def collection_items(
            cls, payload: Any, *, api: Client, **kwargs: Any
        ) -> Any:
            seen.append(api)
            return payload

    requests_mock.get("https://example.com/v1/probes/", json=[])
    get = requests_mock.get(
        "https://example.com/v1/probes/1/", json={"id": 1, "name": "a"}
    )
    list(Probe.filter(request_options={"headers": {"X-Trace": "t"}}))
    (view,) = seen

    built = Probe(id=1, api=view)

    assert not isinstance(view, API)
    assert built.name == "a"
    assert get.last_request.headers["X-Trace"] == "t"


@json_object
class Scoped(PagedBase):
    """Model for objects that are built with a request-scoped view."""

    class_url = "/scoped/"

    id: int


@pytest.mark.parametrize(
    "duplicate",
    [copy.deepcopy, lambda obj: pickle.loads(pickle.dumps(obj))],
    ids=["deepcopy", "pickle"],
)
def test_object_built_with_a_scoped_view_can_be_copied_and_pickled(
    duplicate: Any,
) -> None:
    """An object that keeps a view survives a deep copy and a pickle.

    Args:
        duplicate: Deep-copies the object, or round-trips it through pickle.
    """
    view = API("https://example.com/v1/")._scoped({"timeout": 3})
    built = Scoped(json={"id": 1}, api=view)

    again = duplicate(built)

    assert again.id == 1
    assert dict(again.api._request_options) == {"timeout": 3}


@pytest.mark.parametrize("link", [5, ["/v1/items/?page=2"], {"href": "x"}])
def test_next_link_that_is_not_a_string_is_a_shape_error(link: Any) -> None:
    """A next link that is not a URL fails as a misshapen page.

    Args:
        link: Next-page member that is neither a URL nor empty.
    """
    api = API("https://example.com/v1/")
    items = follow_next_links(api, {"next": link, "results": [{"id": 1}]})

    assert next(items) == {"id": 1}
    with pytest.raises(ResponseShapeError, match="must be a URL"):
        next(items)


def _page(api: API, requests_mock: Any, url: str, **body: Any) -> Page[Any]:
    """Return the page that a mocked ``url`` answers with ``body``.

    Args:
        api: Client to read the page with.
        requests_mock: Mocker that serves the page.
        url: Absolute URL of the page.
        **body: Members of the JSON page.

    Returns:
        The page, read with ``next_links_page()``.
    """
    requests_mock.get(url, json=body)
    return next_links_page(api, api.get(url))


def test_next_links_page_navigates_both_ways(requests_mock: Any) -> None:
    """A page's links are resolved, followed, and followed back."""
    api = API("https://example.com/v1/")
    requests_mock.get(
        "https://example.com/v1/items/?page=2",
        json={
            "count": 3,
            "results": [{"id": 3}],
            "next": None,
            "previous": "?page=1",
        },
    )
    requests_mock.get(
        "https://example.com/v1/items/?page=1",
        json={
            "count": 3,
            "results": [{"id": 1}, {"id": 2}],
            "next": "/v1/items/?page=2",
            "previous": "",
        },
    )

    first = next_links_page(api, api.get("/items/?page=1"))

    assert list(first) == [{"id": 1}, {"id": 2}]
    assert len(first) == 2
    assert first.count == 3
    assert first.previous_url is None
    assert first.previous_page() is None
    assert first.next_url == "https://example.com/v1/items/?page=2"
    second = first.next_page()
    assert second is not None
    assert second.items == [{"id": 3}]
    assert second.next_page() is None
    back = second.previous_page()
    assert back is not None
    assert back.items == first.items
    assert "items=2" in repr(back)


def test_page_repr_leaves_out_the_url_credentials(
    requests_mock: Any,
) -> None:
    """A page's ``repr()`` does not quote the user and password of its URL."""
    api = API("https://user:dummy-pass@example.com/v1/")
    requests_mock.get(
        "https://user:dummy-pass@example.com/v1/items/",
        json={"results": [{"id": 1}], "next": None},
    )

    page = next_links_page(api, api.get("/items/"))

    assert page.url is not None
    assert "dummy-pass" in page.url
    assert repr(page) == "<Page url='https://example.com/v1/items/' items=1>"


def test_list_payload_is_a_page_without_links(requests_mock: Any) -> None:
    """A list payload is one page with no links and no count."""
    api = API("https://example.com/v1/")
    requests_mock.get("https://example.com/v1/items/", json=[{"id": 1}])

    page = next_links_page(api, api.get("/items/"))

    assert page.items == [{"id": 1}]
    assert page.count is None
    assert page.next_page() is None
    assert page.previous_page() is None


def test_page_navigation_refuses_cycles_and_too_many_pages(
    requests_mock: Any,
) -> None:
    """A next link back to a page read going forward, or too many, raises."""
    api = API("https://example.com/v1/")
    requests_mock.get(
        "https://example.com/v1/items/?page=2",
        json={"results": [], "next": "/v1/items/?page=1"},
    )
    first = _page(
        api,
        requests_mock,
        "https://example.com/v1/items/?page=1",
        results=[],
        next="/v1/items/?page=2",
    )
    second = first.next_page()
    assert second is not None

    with pytest.raises(PaginationError, match="already read"):
        second.next_page()

    api.max_pages = 1
    with pytest.raises(LimitExceeded, match="more than 1 pages"):
        first.next_page()


def test_page_link_to_another_origin_is_refused(requests_mock: Any) -> None:
    """A link to another origin is refused before anything is sent there."""
    api = API("https://example.com/v1/")
    other = requests_mock.get("https://other.example/items/", json=[])
    page = _page(
        api,
        requests_mock,
        "https://example.com/v1/items/",
        results=[],
        next="https://other.example/items/",
    )

    with pytest.raises(PaginationError, match="different origin"):
        page.next_page()

    assert not other.called


@pytest.mark.parametrize("space", ["\xa0", "\u3000", "\x85", "\u2028"])
def test_origin_error_leaves_out_base_url_credentials_after_unicode_space(
    requests_mock: Any, space: str
) -> None:
    """A base URL after Unicode whitespace is quoted without credentials.

    Args:
        space: Unicode whitespace character before the base URL.
    """
    api = API(space + "https://user:dummy-pass@example.com/v1/")
    page = _page(
        api,
        requests_mock,
        "https://example.com/v1/items/",
        results=[],
        next="https://other.example/items/",
    )

    with pytest.raises(PaginationError, match="different origin") as raised:
        page.next_page()

    assert "dummy-pass" not in str(raised.value)
    assert "dummy-pass" not in repr(raised.value)


def test_page_larger_than_max_items_is_refused(requests_mock: Any) -> None:
    """A single page with more items than ``max_items`` raises."""
    api = API("https://example.com/v1/")
    api.max_items = 1

    with pytest.raises(LimitExceeded, match="more than 1 items"):
        _page(
            api,
            requests_mock,
            "https://example.com/v1/items/",
            results=[{"id": 1}, {"id": 2}],
        )


def test_built_page_cannot_read_other_pages() -> None:
    """A page that a hook builds itself has no way to read its links."""
    page = Page([1], url="https://example.com/v1/items/", next_url="x")

    with pytest.raises(UsageError, match="cannot read other pages"):
        page.next_page()


def test_link_headers_page_reads_next_and_previous(requests_mock: Any) -> None:
    """``rel="next"`` and ``rel="prev"`` name the pages around a page."""
    api = API("https://example.com/v1/")
    requests_mock.get(
        "https://example.com/v1/items/?page=2",
        json=[{"id": 2}],
        headers={"Link": '</v1/items/?page=1>; rel="prev first"'},
    )
    requests_mock.get(
        "https://example.com/v1/items/?page=1",
        json=[{"id": 1}],
        headers={"Link": '<?page=2>; rel="next", <>; rel="previous"'},
    )

    first = link_headers_page(api, api.get("/items/?page=1"))

    assert first.items == [{"id": 1}]
    assert first.count is None
    assert first.previous_url is None
    second = first.next_page()
    assert second is not None
    assert second.items == [{"id": 2}]
    assert second.previous_url == "https://example.com/v1/items/?page=1"
    assert second.next_url is None


def test_link_headers_page_must_be_a_list(requests_mock: Any) -> None:
    """A Link-header page whose body is not a JSON list is misshapen."""
    api = API("https://example.com/v1/")
    requests_mock.get("https://example.com/v1/items/", json={"results": []})

    with pytest.raises(ResponseShapeError, match="JSON lists"):
        link_headers_page(api, api.get("/items/"))


def _serve_two_pages(requests_mock: Any, path: str) -> None:
    """Serve a two-page DRF collection at ``path``.

    Args:
        requests_mock: Mocker that serves the pages.
        path: Path of the collection under the API's base URL.
    """
    base = f"https://example.com/v1{path}"
    requests_mock.get(
        base,
        json={
            "count": 3,
            "results": [{"id": 1, "owner": 7}, {"id": 2, "owner": 8}],
            "next": f"{base}?page=2",
            "previous": None,
        },
    )
    requests_mock.get(
        f"{base}?page=2",
        json={
            "count": 3,
            "results": [{"id": 3, "owner": 7}],
            "next": None,
            "previous": base,
        },
    )


def test_get_page_reads_and_navigates_pages_of_objects(
    requests_mock: Any,
) -> None:
    """``get_page()`` returns objects, and its links return objects too."""
    _serve_two_pages(requests_mock, "/items/")

    first = Item.get_page(request_options={"headers": {"X-Trace": "t"}})

    assert [item.id for item in first] == [1, 2]
    assert all(isinstance(item, Item) for item in first)
    assert first.count == 3
    assert first.previous_page() is None
    second = first.next_page()
    assert second is not None
    assert [item.id for item in second] == [3]
    assert all(item._api_override is None for item in second)
    back = second.previous_page()
    assert back is not None
    assert [item.id for item in back] == [1, 2]
    assert all(
        request.headers["X-Trace"] == "t"
        for request in requests_mock.request_history
    )


_MALFORMED_PREVIOUS = [
    (5, ResponseShapeError, "must be a URL"),
    ("http://[bad", PaginationError, "cannot be parsed"),
]


@pytest.mark.parametrize(("previous", "error", "message"), _MALFORMED_PREVIOUS)
def test_filter_reads_a_page_whose_previous_link_is_malformed(
    requests_mock: Any, previous: Any, error: type[Exception], message: str
) -> None:
    """``filter()`` never follows ``previous``, so it does not check it.

    ``previous_page()`` follows it, and refuses it.
    """
    requests_mock.get(
        "https://example.com/v1/items/",
        json={
            "count": 1,
            "results": [{"id": 1}],
            "next": None,
            "previous": previous,
        },
    )

    assert [item.id for item in Item.filter()] == [1]
    page = Item.get_page()
    assert [item.id for item in page] == [1]
    with pytest.raises(error, match=message):
        page.previous_page()
    with pytest.raises(error, match=message):
        page.previous_url  # noqa: B018


def test_filter_refuses_a_page_whose_next_link_is_malformed(
    requests_mock: Any,
) -> None:
    """A malformed ``next`` still fails, as the link ``filter()`` follows."""
    requests_mock.get(
        "https://example.com/v1/items/",
        json={"results": [{"id": 1}], "next": 5, "previous": None},
    )

    with pytest.raises(ResponseShapeError, match="must be a URL"):
        list(Item.filter())


def test_link_headers_page_reads_with_a_malformed_previous_link(
    requests_mock: Any,
) -> None:
    """A previous link that cannot be parsed fails when it is first needed."""
    api = API("https://example.com/v1/")
    requests_mock.get(
        "https://example.com/v1/items/",
        json=[{"id": 1}],
        headers={"Link": '<http://[bad>; rel="prev"'},
    )

    page = link_headers_page(api, api.get("/items/"))

    assert page.items == [{"id": 1}]
    with pytest.raises(PaginationError, match="cannot be parsed"):
        page.previous_page()


def test_get_page_refuses_a_hand_built_page_over_max_items(
    requests_mock: Any,
) -> None:
    """The size check holds for a page that a ``collection_page()`` builds."""
    api = API("https://api.test/")
    api.max_items = 2
    requests_mock.get(
        "https://api.test/users/", json=[{"id": n} for n in range(5)]
    )

    @json_object
    class User(JSONObject):
        class_url = "/users/"

        id: int

        @classmethod
        def collection_page(
            cls, resp: Response, *, api: Client, **kwargs: Any
        ) -> Page[Any]:
            return Page(api.json(resp), url=resp.url)

    User.register_api(api)

    with pytest.raises(LimitExceeded, match="more than 2 items"):
        User.get_page()


def test_next_page_remembers_where_a_redirect_ended(
    requests_mock: Any,
) -> None:
    """A link to the URL that a redirect reached is a cycle, read once."""
    api = API("https://api.test/")
    requests_mock.get(
        "https://api.test/b",
        status_code=302,
        headers={"Location": "https://api.test/c"},
    )
    requests_mock.get(
        "https://api.test/c",
        json={"results": [{"id": 2}], "next": "https://api.test/c"},
    )
    requests_mock.get(
        "https://api.test/a",
        json={"results": [{"id": 1}], "next": "https://api.test/b"},
    )
    first = next_links_page(api, api.get("/a"))
    second = first.next_page()
    assert second is not None
    reads = requests_mock.call_count

    with pytest.raises(PaginationError, match="already read"):
        second.next_page()

    assert requests_mock.call_count == reads


def test_get_page_prefetches_each_page(requests_mock: Any) -> None:
    """Prefetch applies to each page that navigation reads."""
    _serve_two_pages(requests_mock, "/owned-items/")

    first = OwnedItem.get_page(prefetch="owner")
    second = first.next_page()

    assert second is not None
    assert [item.owner.id for item in first] == [7, 8]
    assert [item.owner.id for item in second] == [7]
    assert requests_mock.call_count == 2


def test_get_page_fetches_a_page_url(requests_mock: Any) -> None:
    """A saved page URL fetches that page, but not with query parameters."""
    _serve_two_pages(requests_mock, "/items/")
    other = requests_mock.get("https://other.example/items/", json=[])

    page = Item.get_page(url="/items/?page=2")

    assert [item.id for item in page] == [3]
    with pytest.raises(TypeError, match="not both"):
        Item.get_page(url="/items/?page=2", id=3)
    with pytest.raises(PaginationError, match="different origin"):
        Item.get_page(url="https://other.example/items/")
    assert not other.called


def test_collection_page_hook_reads_link_headers(requests_mock: Any) -> None:
    """A model reads Link-header pages by returning ``link_headers_page()``."""
    seen: list[tuple[API, Client]] = []
    tenant = API("https://example.com/v1/")

    @json_object
    class Linked(PagedBase):
        class_url = "/linked/"

        id: int

        @classmethod
        def collection_page(
            cls, resp: Response, *, api: Client, **kwargs: Any
        ) -> Page[Any]:
            seen.append((cls.api, api))
            return link_headers_page(api, resp)

    requests_mock.get(
        "https://example.com/v1/linked/",
        json=[{"id": 1}],
        headers={"Link": '<?page=2>; rel="next"'},
    )
    requests_mock.get(
        "https://example.com/v1/linked/?page=2",
        json=[{"id": 2}],
        headers={"Link": '</v1/linked/>; rel="prev"'},
    )

    page = Linked.get_page(api=tenant)
    second = page.next_page()

    assert second is not None
    assert [item.id for item in second] == [2]
    assert all(item.api is tenant for item in second)
    assert seen == [(Linked.api, tenant), (Linked.api, tenant)]
    assert Linked.api is not tenant


class PageHookBase(JSONObject):
    """Base object that overrides only the page hook, for Link headers."""

    @classmethod
    def collection_page(
        cls, resp: Response, *, api: Client, **kwargs: Any
    ) -> Page[Any]:
        """Read a page and the pages that it links to by ``Link`` headers.

        Args:
            resp: Response for the page.
            api: The query's client.

        Returns:
            The page of JSON items.
        """
        return link_headers_page(api, resp)


PageHookBase.register_api(API("https://example.com/v1/"))


@json_object
class Hooked(PageHookBase):
    """Model whose pages are linked by headers and read by the page hook."""

    class_url = "/hooked/"

    id: int


HOOKED = "https://example.com/v1/hooked/"


def _serve_hooked_pages(requests_mock: Any, count: int = 3) -> None:
    """Serve ``count`` pages of one item each, linked by headers.

    Args:
        requests_mock: The mocker that serves the pages.
        count: Number of pages.
    """
    for number in range(1, count + 1):
        headers = (
            {"Link": f'<{HOOKED}?page={number + 1}>; rel="next"'}
            if number < count
            else {}
        )
        url = HOOKED if number == 1 else f"{HOOKED}?page={number}"
        requests_mock.get(url, json=[{"id": number}], headers=headers)


def test_page_hook_alone_reads_every_page_through_filter_and_all(
    requests_mock: Any,
) -> None:
    """A class that overrides only ``collection_page()`` reads all pages."""
    _serve_hooked_pages(requests_mock)

    assert [item.id for item in Hooked.filter()] == [1, 2, 3]
    assert [item.id for item in Hooked.all()] == [1, 2, 3]
    assert [item.id for item in Hooked.get_page()] == [1]


def test_page_hook_reads_the_next_page_only_when_reached(
    requests_mock: Any,
) -> None:
    """Reading through the page hook is lazy, as following links is."""
    _serve_hooked_pages(requests_mock)

    items = Hooked.filter()

    assert requests_mock.call_count == 1
    assert next(items).id == 1
    assert requests_mock.call_count == 1
    assert [item.id for item in items] == [2, 3]
    assert requests_mock.call_count == 3


def test_default_page_hook_reads_pages_lazily_and_reports_the_count(
    requests_mock: Any,
) -> None:
    """A class with no override reads pages of Django REST Framework lazily."""

    @json_object
    class Plain(JSONObject):
        class_url = "/plain/"

        id: int

    Plain.register_api(API("https://example.com/v1/"))
    requests_mock.get(
        "https://example.com/v1/plain/",
        json={"count": 2, "next": "?page=2", "results": [{"id": 1}]},
    )
    requests_mock.get(
        "https://example.com/v1/plain/?page=2",
        json={"count": 2, "next": None, "results": [{"id": 2}]},
    )

    items = Plain.filter()

    assert items.count == 2
    assert next(items).id == 1
    assert requests_mock.call_count == 1
    assert [item.id for item in items] == [2]


def test_page_hook_traversal_refuses_a_link_to_a_page_already_read(
    requests_mock: Any,
) -> None:
    """A cycle of links raises ``PaginationError``, not an endless read."""
    requests_mock.get(
        HOOKED,
        json=[{"id": 1}],
        headers={"Link": f'<{HOOKED}?p=2>; rel="next"'},
    )
    requests_mock.get(
        HOOKED + "?p=2",
        json=[{"id": 2}],
        headers={"Link": f'<{HOOKED}>; rel="next"'},
    )

    items = Hooked.filter()

    assert next(items).id == 1
    assert next(items).id == 2
    with pytest.raises(PaginationError, match="already read"):
        next(items)


def test_page_hook_traversal_refuses_a_link_to_another_origin(
    requests_mock: Any,
) -> None:
    """A page that links to another origin is not requested."""
    requests_mock.get(
        HOOKED,
        json=[{"id": 1}],
        headers={"Link": '<https://other.example/hooked/>; rel="next"'},
    )
    other = requests_mock.get("https://other.example/hooked/", json=[])

    items = Hooked.filter()

    assert next(items).id == 1
    with pytest.raises(PaginationError, match="different origin"):
        next(items)
    assert not other.called


def test_page_hook_traversal_counts_pages_and_items_together(
    requests_mock: Any,
) -> None:
    """``max_pages`` and ``max_items`` bound the whole traversal."""
    _serve_hooked_pages(requests_mock)
    paged = API("https://example.com/v1/")
    paged.max_pages = 2
    capped = API("https://example.com/v1/")
    capped.max_items = 2

    with pytest.raises(LimitExceeded, match="more than 2 pages"):
        list(Hooked.filter(api=paged))
    with pytest.raises(LimitExceeded, match="more than 2 items"):
        list(Hooked.filter(api=capped))


def test_page_built_without_a_source_ends_the_traversal(
    requests_mock: Any,
) -> None:
    """A page that a hook builds by hand yields its items and ends."""

    @json_object
    class Built(JSONObject):
        class_url = "/built/"

        id: int

        @classmethod
        def collection_page(
            cls, resp: Response, *, api: Client, **kwargs: Any
        ) -> Page[Any]:
            return Page(
                api.json(resp), url=resp.url, count=7, next_url="?page=2"
            )

    Built.register_api(API("https://example.com/v1/"))
    requests_mock.get("https://example.com/v1/built/", json=[{"id": 1}])

    items = Built.filter()

    assert items.count == 7
    assert [item.id for item in items] == [1]
    assert requests_mock.call_count == 1


def test_collection_items_override_still_receives_the_payload(
    requests_mock: Any,
) -> None:
    """A class that overrides ``collection_items()`` keeps its override."""
    payloads: list[Any] = []

    @json_object
    class Wrapped(JSONObject):
        class_url = "/wrapped/"

        id: int

        @classmethod
        def collection_items(
            cls, payload: Any, *, api: Client, **kwargs: Any
        ) -> Any:
            payloads.append(payload)
            return payload["rows"]

        @classmethod
        def collection_page(
            cls, resp: Response, *, api: Client, **kwargs: Any
        ) -> Page[Any]:
            raise AssertionError("the override replaces the page hook")

    Wrapped.register_api(API("https://example.com/v1/"))
    requests_mock.get(
        "https://example.com/v1/wrapped/", json={"rows": [{"id": 1}]}
    )

    assert [item.id for item in Wrapped.filter()] == [1]
    assert payloads == [{"rows": [{"id": 1}]}]


def test_collection_items_from_response_override_still_replaces_the_default(
    requests_mock: Any,
) -> None:
    """A class that overrides the response hook is read through it alone."""

    @json_object
    class Custom(JSONObject):
        class_url = "/custom/"

        id: int

        @classmethod
        def collection_items_from_response(
            cls, resp: Response, *, api: Client, **kwargs: Any
        ) -> Any:
            return api.json(resp)["rows"]

        @classmethod
        def collection_page(
            cls, resp: Response, *, api: Client, **kwargs: Any
        ) -> Page[Any]:
            raise AssertionError("the override replaces the page hook")

    Custom.register_api(API("https://example.com/v1/"))
    requests_mock.get(
        "https://example.com/v1/custom/", json={"rows": [{"id": 1}]}
    )

    assert [item.id for item in Custom.filter()] == [1]


def test_collection_page_hook_must_return_a_page(requests_mock: Any) -> None:
    """A hook that returns something other than a page fails clearly."""

    @json_object
    class Wrong(PagedBase):
        class_url = "/wrong/"

        id: int

        @classmethod
        def collection_page(
            cls, resp: Response, *, api: Client, **kwargs: Any
        ) -> Any:
            return api.json(resp)

    requests_mock.get("https://example.com/v1/wrong/", json=[{"id": 1}])

    with pytest.raises(TypeError, match="must return a Page"):
        Wrong.get_page()


def test_page_built_by_a_hook_holds_objects_but_cannot_navigate(
    requests_mock: Any,
) -> None:
    """A hook may build its own page, which then cannot read its links."""

    @json_object
    class Built(PagedBase):
        class_url = "/built/"

        id: int

        @classmethod
        def collection_page(
            cls, resp: Response, *, api: Client, **kwargs: Any
        ) -> Page[Any]:
            return Page(api.json(resp), url=resp.url, next_url="?page=2")

    requests_mock.get("https://example.com/v1/built/", json=[{"id": 1}])

    page = Built.get_page()

    assert [item.id for item in page] == [1]
    with pytest.raises(UsageError, match="cannot read other pages"):
        page.next_page()


@json_object
class Team(JSONObject):
    """Model of a team, which has its own endpoint."""

    class_url = "/teams/"

    id: int
    name: str


@json_object
class LeaderboardEntry(JSONObject):
    """Model without a key, read whole from a paginated endpoint."""

    rank: int
    points: float
    team: Team

    @classmethod
    def collection_items(
        cls, payload: Any, *, api: Client, **kwargs: Any
    ) -> Any:
        """Read every page of a collection response.

        Args:
            payload: Decoded JSON body of the collection's first page.
            api: The query's client.

        Returns:
            An iterator over the items of every page, following next links.
        """
        return follow_next_links(api, payload)


def test_a_model_without_a_key_is_read_from_the_items_of_an_endpoint(
    requests_mock: Any,
) -> None:
    """An object without a key reads its fields from the item it is built on.

    Its related object, of a model with a key, is embedded or named by its key.
    A key alone loads from the object's own URL when a field is read.
    """
    api = API("https://example.com/v1/")
    Team.register_api(api)
    LeaderboardEntry.register_api(api)
    first = requests_mock.get(
        "https://example.com/v1/leaderboard/",
        json={
            "count": 3,
            "next": "https://example.com/v1/leaderboard/?page=2",
            "previous": None,
            "results": [
                {"rank": 1, "points": 9.5, "team": 7},
                {"rank": 2, "points": 8.0, "team": 8},
            ],
        },
    )
    second = requests_mock.get(
        "https://example.com/v1/leaderboard/?page=2",
        json={
            "count": 3,
            "next": None,
            "previous": "https://example.com/v1/leaderboard/",
            "results": [
                {
                    "rank": 3,
                    "points": 4.25,
                    "team": {"id": 9, "name": "Greens"},
                }
            ],
        },
    )
    team = requests_mock.get(
        "https://example.com/v1/teams/7/", json={"id": 7, "name": "Reds"}
    )

    response = api.get("/leaderboard/")
    entries = [
        LeaderboardEntry(json=item)
        for item in LeaderboardEntry.collection_items(response.json(), api=api)
    ]

    assert first.call_count == 1
    assert second.call_count == 1
    assert [entry.rank for entry in entries] == [1, 2, 3]
    assert [entry.points for entry in entries] == [9.5, 8.0, 4.25]
    assert all(entry.pk is None for entry in entries)
    assert [entry.team.pk for entry in entries] == [7, 8, 9]
    assert team.call_count == 0
    assert entries[2].team.name == "Greens"
    assert team.call_count == 0
    assert entries[0].team.name == "Reds"
    assert team.call_count == 1
    assert team.last_request.url == "https://example.com/v1/teams/7/"
    with pytest.raises(ValueError, match="has no primary-key field"):
        _ = entries[0].url


def test_page_request_redirected_twice_is_named_by_its_link(
    requests_mock: Any,
) -> None:
    """A redirect from a redirect is named by the page link.

    The link is quoted with its query, since it holds the state that a caller
    needs, but the URL that a redirect chose is not.
    """
    api = API("https://api.test/")
    requests_mock.get(
        "https://api.test/p",
        status_code=302,
        headers={"Location": "https://api.test/q?sig=SECRETSIG#SECRETFRAG"},
    )
    requests_mock.get(
        "https://api.test/q",
        status_code=302,
        headers={"Location": "https://files.test/z?sig=SECRETSIG"},
    )
    first_page = {"next": "https://api.test/p?cursor=KEPT", "results": []}

    with pytest.raises(PaginationError) as raised:
        list(follow_next_links(api, first_page))

    assert str(raised.value) == (
        "Page request 'https://api.test/p?cursor=KEPT' was redirected to a "
        "different origin from the API's base URL 'https://api.test/'. Pass "
        "allow_other_origins=True to follow it."
    )


def test_page_link_that_is_redirected_away_is_named_with_its_query(
    requests_mock: Any,
) -> None:
    """A page link that no redirect reached is quoted with its query."""
    api = API("https://api.test/")
    requests_mock.get(
        "https://api.test/p",
        status_code=302,
        headers={"Location": "https://files.test/z?sig=SECRETSIG"},
    )
    first_page = {"next": "https://api.test/p?cursor=KEPT", "results": []}

    with pytest.raises(PaginationError) as raised:
        list(follow_next_links(api, first_page))

    assert str(raised.value).startswith(
        "Page request 'https://api.test/p?cursor=KEPT' was redirected to a "
        "different origin "
    )
    assert "SECRETSIG" not in str(raised.value)


def test_page_link_that_fails_keeps_its_query(requests_mock: Any) -> None:
    """The error for a page link that no redirect reached names its query."""
    api = API("https://api.test/")
    requests_mock.get("https://api.test/p", status_code=500)
    first_page = {"next": "https://api.test/p?cursor=KEPT", "results": []}

    with pytest.raises(requests.HTTPError) as raised:
        list(follow_next_links(api, first_page))

    assert str(raised.value) == (
        "500 Server Error: None for GET https://api.test/p?cursor=KEPT"
    )


def test_page_link_that_a_redirect_reached_and_failed_is_named_by_the_link(
    requests_mock: Any,
) -> None:
    """The failure of the URL that a redirect chose names the page link."""
    api = API("https://api.test/")
    requests_mock.get(
        "https://api.test/p",
        status_code=302,
        headers={"Location": "https://api.test/q?sig=SECRETSIG"},
    )
    requests_mock.get("https://api.test/q", status_code=500)
    first_page = {"next": "https://api.test/p?cursor=KEPT", "results": []}

    with pytest.raises(requests.HTTPError) as raised:
        list(follow_next_links(api, first_page))

    assert str(raised.value) == (
        "500 Server Error: None for GET https://api.test/p?cursor=KEPT"
    )


def test_page_repr_after_a_redirect_shows_the_url_that_was_asked_for(
    requests_mock: Any,
) -> None:
    """A page that a redirect reached shows the URL that was asked for."""
    api = API("https://api.test/")
    api.redirect_origins = ["https://files.test"]
    requests_mock.get(
        "https://api.test/items/",
        status_code=302,
        headers={"Location": "https://files.test/o?sig=SECRETSIG"},
    )
    requests_mock.get(
        "https://files.test/o",
        json={"results": [1], "next": "https://files.test/o?cursor=2"},
    )

    page = next_links_page(api, api.get("items/"), allow_other_origins=True)

    assert page.url == "https://files.test/o?sig=SECRETSIG"
    assert repr(page) == "<Page url='https://api.test/items/' items=1>"


def test_page_repr_keeps_the_query_of_a_url_that_was_not_redirected(
    requests_mock: Any,
) -> None:
    """A page that was read from a link, or built by hand, shows its query."""
    api = API("https://api.test/")
    requests_mock.get(
        "https://api.test/items/",
        json={"results": [1], "next": "https://api.test/items/?page=2"},
    )
    requests_mock.get(
        "https://api.test/items/?page=2", json={"results": [2], "next": None}
    )

    first = next_links_page(api, api.get("items/"))
    second = first.next_page()

    assert second is not None
    assert (
        repr(second) == "<Page url='https://api.test/items/?page=2' items=1>"
    )
    assert repr(Page([1], url="https://api.test/a?k=1")) == (
        "<Page url='https://api.test/a?k=1' items=1>"
    )


def test_model_page_repr_after_a_redirect_shows_the_url_that_was_asked_for(
    requests_mock: Any,
) -> None:
    """A model's page that a redirect reached shows the URL asked for."""
    api = API("https://api.test/")
    requests_mock.get(
        "https://api.test/users/",
        status_code=302,
        headers={"Location": "/u2/?sig=SECRETSIG"},
    )
    requests_mock.get(
        "https://api.test/u2/",
        json={"results": [{"id": 1}], "next": "https://api.test/u2/?page=2"},
    )
    requests_mock.get(
        "https://api.test/u2/?page=2",
        json={"results": [{"id": 2}], "next": None},
    )

    @json_object
    class User(JSONObject):
        class_url = "/users/"

        id: int

        @classmethod
        def collection_page(
            cls, resp: Response, *, api: Client, **kwargs: Any
        ) -> Page[Any]:
            return next_links_page(api, resp)

    first = User.get_page(api=api)
    second = first.next_page()

    assert repr(first) == "<Page url='https://api.test/users/' items=1>"
    assert second is not None
    assert repr(second) == "<Page url='https://api.test/u2/?page=2' items=1>"


def test_model_page_built_by_a_hook_after_a_redirect_shows_the_url_asked_for(
    requests_mock: Any,
) -> None:
    """A page that a model's hook builds from a redirected response is quoted.

    The hook builds a new page, so the page keeps what it reads from the
    response that it was built from.
    """
    api = API("https://api.test/")
    requests_mock.get(
        "https://api.test/built/",
        status_code=302,
        headers={"Location": "/b2/?sig=SECRETSIG"},
    )
    requests_mock.get("https://api.test/b2/", json=[{"id": 1}])

    @json_object
    class Built(JSONObject):
        class_url = "/built/"

        id: int

        @classmethod
        def collection_page(
            cls, resp: Response, *, api: Client, **kwargs: Any
        ) -> Page[Any]:
            return Page(api.json(resp), url=resp.url)

    page = Built.get_page(api=api)

    assert repr(page) == "<Page url='https://api.test/built/' items=1>"


def test_model_page_repr_keeps_the_url_that_the_caller_gave(
    requests_mock: Any,
) -> None:
    """A model's page that no redirect reached shows its query."""
    api = API("https://api.test/")
    requests_mock.get("https://api.test/users/", json=[{"id": 1}])

    @json_object
    class User(JSONObject):
        class_url = "/users/"

        id: int

        @classmethod
        def collection_page(
            cls, resp: Response, *, api: Client, **kwargs: Any
        ) -> Page[Any]:
            return Page(api.json(resp), url=resp.url)

    page = User.get_page(api=api, url="/users/?q=KEPT")

    assert repr(page) == "<Page url='https://api.test/users/?q=KEPT' items=1>"


class _ForwardingAPI(API):
    """API whose ``get()`` forwards its keywords, as a subclass may."""

    def get(self, uri: str, *args: Any, **kwargs: Any) -> requests.Response:
        """Forward the request.

        Args:
            uri: Relative or absolute request URI.
            *args: Positional arguments for the request.
            **kwargs: Keyword arguments for the request.

        Returns:
            The response.
        """
        return super().get(uri, *args, **kwargs)


def test_page_through_a_scoped_view_limits_redirects_to_the_api_origin(
    requests_mock: Any,
) -> None:
    """A page request through a scoped view is limited to the API's origin.

    A page's redirect may reach the API's origin alone, though
    ``redirect_origins`` allows another for a request that is not a page's.
    """
    api = _ForwardingAPI("https://api.test/")
    api.redirect_origins = ["https://other.test"]
    requests_mock.get(
        "https://api.test/p",
        status_code=302,
        headers={"Location": "https://other.test/x"},
    )
    target = requests_mock.get("https://other.test/x", json={"results": []})
    view = api._scoped({"timeout": 3})
    first_page = {"next": "https://api.test/p", "results": []}

    with pytest.raises(PaginationError, match="different origin"):
        list(follow_next_links(view, first_page))

    assert not target.called


def test_other_origin_page_redirects_follow_the_apis_allowed_origins(
    requests_mock: Any,
) -> None:
    """With ``allow_other_origins``, a redirect is checked as any other is."""
    api = API("https://api.test/")
    api.redirect_origins = ["https://cdn.test"]
    requests_mock.get(
        "https://api.test/ok",
        status_code=302,
        headers={"Location": "https://cdn.test/page"},
    )
    requests_mock.get("https://cdn.test/page", json={"results": [{"id": 1}]})
    requests_mock.get(
        "https://api.test/bad",
        status_code=302,
        headers={"Location": "https://evil.test/page"},
    )
    evil = requests_mock.get("https://evil.test/page", json={"results": []})

    followed = list(
        follow_next_links(
            api,
            {"next": "https://api.test/ok", "results": []},
            allow_other_origins=True,
        )
    )
    with pytest.raises(RedirectError, match="redirect_origins"):
        list(
            follow_next_links(
                api,
                {"next": "https://api.test/bad", "results": []},
                allow_other_origins=True,
            )
        )

    assert followed == [{"id": 1}]
    assert not evil.called


def test_refused_page_redirect_runs_session_hooks_once_per_response(
    requests_mock: Any,
) -> None:
    """A page's redirect is checked once, after the session's hooks ran."""
    api = API("https://api.test/")
    calls: list[int] = []

    def count(resp: requests.Response, **kwargs: Any) -> None:
        calls.append(resp.status_code)

    api.session.hooks["response"].append(count)
    requests_mock.get(
        "https://api.test/p",
        status_code=302,
        headers={"Location": "https://other.test/x"},
    )

    with pytest.raises(PaginationError, match="different origin"):
        list(
            follow_next_links(
                api, {"next": "https://api.test/p", "results": []}
            )
        )

    assert calls == [302]


def test_page_origin_limit_applies_to_every_redirect_hop(
    requests_mock: Any,
) -> None:
    """A page's second redirect may not leave the API's origin either."""
    api = API("https://api.test/")
    api.redirect_origins = ["https://other.test"]
    requests_mock.get(
        "https://api.test/p",
        status_code=302,
        headers={"Location": "https://api.test/q"},
    )
    requests_mock.get(
        "https://api.test/q",
        status_code=302,
        headers={"Location": "https://other.test/x"},
    )
    target = requests_mock.get("https://other.test/x", json={"results": []})

    with pytest.raises(PaginationError, match="different origin"):
        list(
            follow_next_links(
                api, {"next": "https://api.test/p", "results": []}
            )
        )

    assert not target.called


@pytest.mark.parametrize("scoped", [False, True])
@pytest.mark.parametrize(
    "url",
    [
        "https://alice:hunter2@api.test:8443/x?a=1",
        "HTTP://API.TEST/x",
        "ftp://api.test/x",
        "http://alice:hunter2#@api.test/x",
        "http://alice:hunter2@api.test:bad/x",
        "\x01https://api.test/x",
        "/relative",
    ],
)
def test_api_reads_a_server_url_as_its_session_sends_it(
    url: str, scoped: bool
) -> None:
    """An API and a scoped view of it read a URL as the session does."""
    api = API("https://api.test/")
    client = api._scoped({"timeout": 3}) if scoped else api

    assert _as_api(client)._read_server_url(url) == _prepare_server_url(
        api.session, url
    )


@pytest.mark.parametrize("scoped", [False, True])
@pytest.mark.parametrize(
    ("session_params", "url", "expected"),
    [
        ({"api_key": "k"}, "https://api.test/x?api_key=k&page=2", ["api_key"]),
        ({"api_key": "k"}, "https://api.test/x?page=2", []),
        ({"a": "1", "b": "2"}, "https://api.test/x?b=3", ["b"]),
        ({"api_key": "k"}, "https://api.test/x?api_key=", ["api_key"]),
        ({}, "https://api.test/x?api_key=k", []),
        ([("api_key", "k")], "https://api.test/x?api_key=k", []),
        (None, "https://api.test/x?api_key=k", []),
    ],
)
def test_api_names_session_params_that_a_url_query_carries(
    session_params: Any, url: str, expected: list[str], scoped: bool
) -> None:
    """Only a mapping's names that the query carries are left out."""
    api = API("https://api.test/")
    api.session.params = session_params
    client = api._scoped({"timeout": 3}) if scoped else api

    result = _as_api(client)._repeated_params(url)

    assert result == dict.fromkeys(expected)


def test_pagination_module_reads_no_session_attribute() -> None:
    """Pagination reaches the session only through the API's methods."""
    source = pathlib.Path(pagination.__file__).read_text(encoding="utf-8")
    reads = [
        node.lineno
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Attribute) and node.attr == "session"
    ]

    assert reads == []


class _CountingAPI(API):
    """API that counts the requests that reach its ``get()``."""

    gets = 0
    """Requests so far, kept on the instance by ``get()``."""

    def get(self, uri: str, *args: Any, **kwargs: Any) -> requests.Response:
        """Count the request and forward it.

        Args:
            uri: Relative or absolute request URI.
            *args: Positional arguments for the request.
            **kwargs: Keyword arguments for the request.

        Returns:
            The response.
        """
        self.gets += 1
        return super().get(uri, *args, **kwargs)


def test_override_of_get_is_not_called_for_a_page_request(
    requests_mock: Any,
) -> None:
    """Pagination sends a page request without ``get()``."""
    api = _CountingAPI("https://api.test/")
    requests_mock.get("https://api.test/two", json={"results": [2]})

    items = list(
        follow_next_links(api, {"next": "/two", "results": [1]}, url="/one")
    )

    assert items == [1, 2]
    assert api.gets == 0


def test_override_of_get_sees_a_query_request_but_no_page_request(
    requests_mock: Any,
) -> None:
    """A model's first request goes through ``get()``, a next page does not."""

    @json_object
    class Probe(PagedBase):
        class_url = "/probes/"

        id: int

    api = _CountingAPI("https://api.test/")
    requests_mock.get(
        "https://api.test/probes/",
        json={"results": [{"id": 1}], "next": "/probes/?page=2"},
    )
    requests_mock.get(
        "https://api.test/probes/?page=2",
        json={"results": [{"id": 2}], "next": None},
    )

    items = list(Probe.filter(api=api))

    assert [item.id for item in items] == [1, 2]
    assert api.gets == 1


def test_override_of_get_sees_no_request_of_a_page_by_url_or_a_previous_page(
    requests_mock: Any,
) -> None:
    """``get_page(url=...)`` and ``previous_page()`` skip ``get()``."""

    @json_object
    class Probe(PagedBase):
        class_url = "/probes/"

        id: int

    api = _CountingAPI("https://api.test/")
    requests_mock.get(
        "https://api.test/probes/",
        json={"results": [{"id": 1}], "next": None, "previous": None},
    )
    requests_mock.get(
        "https://api.test/probes/?page=2",
        json={
            "results": [{"id": 2}],
            "next": None,
            "previous": "/probes/",
        },
    )

    second = Probe.get_page(url="/probes/?page=2", api=api)
    first = second.previous_page()

    assert [item.id for item in second] == [2]
    assert first is not None
    assert [item.id for item in first] == [1]
    assert api.gets == 0


@pytest.mark.parametrize("rewrite", ["?x=1", "/", "extra/"])
def test_page_redirect_limit_holds_for_each_redirect_whatever_get_does(
    requests_mock: Any, rewrite: str
) -> None:
    """A page redirect to another origin is refused, though the API allows it.

    An override of ``get()`` that rewrites the URL of a request does not loosen
    the limit, since pagination does not send the page through it.

    Args:
        requests_mock: Mock transport.
        rewrite: Text that the override adds to the URL.
    """

    class Rewriting(API):
        """API whose ``get()`` rewrites each request's URL."""

        def get(
            self, uri: str, *args: Any, **kwargs: Any
        ) -> requests.Response:
            """Forward the request with a changed URL.

            Args:
                uri: Relative or absolute request URI.
                *args: Positional arguments for the request.
                **kwargs: Keyword arguments for the request.

            Returns:
                The response.
            """
            return super().get(f"{uri}{rewrite}", *args, **kwargs)

    api = Rewriting("https://api.test/")
    api.redirect_origins = ["https://other.test"]
    requests_mock.get("https://api.test/p", json={"results": [], "next": "/q"})
    requests_mock.get(
        "https://api.test/q",
        status_code=302,
        headers={"Location": "https://api.test/r"},
    )
    requests_mock.get(
        "https://api.test/r",
        status_code=302,
        headers={"Location": "https://other.test/x"},
    )
    target = requests_mock.get("https://other.test/x", json={"results": []})

    with pytest.raises(PaginationError, match="different origin"):
        list(follow_next_links(api, {"next": "/q", "results": []}))

    assert not target.called


def test_session_response_hook_sees_a_page_through_a_direct_session_override(
    requests_mock: Any,
) -> None:
    """A session hook runs for a page that a direct-session ``get()`` skips."""

    class Direct(API):
        """API whose ``get()`` sends through the session."""

        def get(
            self, uri: str, *args: Any, **kwargs: Any
        ) -> requests.Response:
            """Send the request through the session.

            Args:
                uri: Relative or absolute request URI.
                *args: Positional arguments for the request.
                **kwargs: Keyword arguments for the request.

            Returns:
                The response.
            """
            return self.session.get(uri, *args, **kwargs)

    api = Direct("https://api.test/")
    statuses: list[int] = []
    api.session.hooks["response"] = [
        lambda resp, **kwargs: statuses.append(resp.status_code)
    ]
    requests_mock.get(
        "https://api.test/two", json={"next": None, "results": [2]}
    )

    items = list(follow_next_links(api, {"next": "/two", "results": [1]}))

    assert items == [1, 2]
    assert statuses == [200]


def test_scoped_view_request_options_reach_a_page_request(
    requests_mock: Any,
) -> None:
    """A page request carries the options of the query's view."""

    class Dropping(API):
        """API whose ``get()`` forwards no option."""

        def get(
            self, uri: str, *args: Any, **kwargs: Any
        ) -> requests.Response:
            """Send the request without the options it was given.

            Args:
                uri: Relative or absolute request URI.
                *args: Positional arguments for the request.
                **kwargs: Keyword arguments for the request.

            Returns:
                The response.
            """
            return super().get(uri)

    api = Dropping("https://api.test/")
    requests_mock.get(
        "https://api.test/two", json={"next": None, "results": [2]}
    )
    view = api._scoped({"headers": {"Accept-Language": "en"}, "timeout": 7})

    items = list(follow_next_links(view, {"next": "/two", "results": [1]}))

    assert items == [1, 2]
    assert requests_mock.last_request.headers["Accept-Language"] == "en"
    assert requests_mock.last_request.timeout == 7
