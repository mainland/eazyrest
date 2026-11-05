"""Helpers for collection endpoints that split their results into pages."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from typing import Any
from urllib.parse import urljoin

import requests

from ._errors import (
    LimitExceeded,
    PaginationError,
    ResponseShapeError,
)
from ._results import Results
from .api import (
    API,
    _origin,
    _redirect_destination,
    _redirect_origins,
    _response_hooks,
)

__all__ = ["follow_link_headers", "follow_next_links"]


def _next_link(resp: requests.Response) -> str | None:
    """Return the URL of the ``rel="next"`` link in a response's Link header.

    A link may have several relation types, as in ``rel="next last"``, and
    registered relation types compare case-insensitively, as RFC 8288 says.

    Args:
        resp: Response whose Link header is read.

    Returns:
        The URL of the first link whose relation types include ``next``, or
        ``None`` if there is none.
    """
    # requests parses the Link header into resp.links, keyed by the whole rel
    # value.
    for link in resp.links.values():
        if "next" in link.get("rel", "").lower().split():
            url: str | None = link.get("url")
            return url

    return None


class _PageReader:
    """Request the next pages of a collection, guarding their URLs."""

    def __init__(self, api: API, allow_other_origins: bool) -> None:
        """Prepare to read pages through ``api``.

        Args:
            api: Client used to request the next pages.
            allow_other_origins: Whether to follow next-page URLs, and
                redirects from page requests, whose origin differs from the
                API's base URL. A redirect must still lead to an origin that
                the API allows.
        """
        self.api = api
        self.allow_other_origins = allow_other_origins
        self.api_origin = _origin(api.base_url)
        self.read_urls: set[str] = set()
        """The URLs of the pages read so far."""
        self.pages = 1
        """The number of pages read so far, counting the first."""
        self.items = 0
        """The number of items yielded so far."""

    def count_items(self, items: list[Any]) -> list[Any]:
        """Count a page's items against the API's limit before yielding them.

        Args:
            items: Items of a page.

        Returns:
            ``items``.

        Raises:
            LimitExceeded: If yielding ``items`` would yield more than the
                API's ``max_items``.
        """
        self.items += len(items)
        limit = self.api.max_items
        if limit is not None and self.items > limit:
            raise LimitExceeded(
                "max_items", f"The collection has more than {limit} items"
            )
        return items

    def check_origin(self, url: str) -> None:
        """Reject a page or redirect URL before credentials can be sent.

        Args:
            url: Absolute URL of a page or of a redirect target.

        Raises:
            PaginationError: If ``url`` has an origin other than the API's base
                URL and other origins are not allowed.
        """
        if not self.allow_other_origins and _origin(url) != self.api_origin:
            raise PaginationError(
                f"Next-page URL {url!r} has a different origin from the "
                f"API's base URL {self.api.base_url!r}. Pass "
                "allow_other_origins=True to follow it."
            )

    def read(self, url: str) -> requests.Response:
        """Request the page at ``url``, a link resolved to an absolute URL.

        Args:
            url: Absolute URL of the page.

        Returns:
            Response for the page.

        Raises:
            PaginationError: If ``url`` names a page already read, or if it or
                a redirect has another origin and other origins are not
                allowed.
            LimitExceeded: If the page would be one more than the API's
                ``max_pages``.
        """
        if url in self.read_urls:
            raise PaginationError(
                f"Next-page URL {url!r} names a page that was already read, "
                "so following it would never end"
            )
        self.check_origin(url)
        limit = self.api.max_pages
        if limit is not None and self.pages >= limit:
            raise LimitExceeded(
                "max_pages", f"The collection has more than {limit} pages"
            )
        self.pages += 1

        def check_redirect(resp: requests.Response, **kwargs: Any) -> None:
            """Check redirects before requests follows them.

            The API checks every redirect too, as for any request. This hook
            adds the reader's own origin restriction.

            Args:
                resp: Response, which may be a redirect.
                kwargs: Other arguments that requests passes to response hooks,
                    which are ignored.

            Raises:
                PaginationError: If ``resp`` redirects to an origin other than
                    the API's base URL and other origins are not allowed. The
                    response is closed first.
            """
            target = self.api.session.get_redirect_target(resp)
            if not target or self.allow_other_origins:
                return
            destination = _redirect_destination(resp, target)
            if _redirect_origins(resp, target) != {self.api_origin}:
                resp.close()
                raise PaginationError(
                    f"Page request {resp.url!r} was redirected to "
                    f"{destination!r}, which has a different origin from the "
                    f"API's base URL {self.api.base_url!r}. Pass "
                    "allow_other_origins=True to follow it."
                )

        # Request hooks replace session hooks, so preserve the existing ones.
        # Run the guard after any hook that might change the response.
        hooks = [
            *_response_hooks(self.api.session.hooks.get("response")),
            check_redirect,
        ]
        resp = self.api.get(url, hooks={"response": hooks})
        # After a redirect, a page's links are relative to its final URL.
        self.read_urls.update((url, resp.url))
        return resp


def _count_of(payload: Any, count_key: str) -> int | None:
    """Return the collection size that a page reports.

    Args:
        payload: Decoded JSON payload of a page.
        count_key: Member of a page that holds the collection's size.

    Returns:
        The member's value, if the payload is a mapping and the value is an
        integer, and ``None`` otherwise.
    """
    if not isinstance(payload, Mapping):
        return None
    count = payload.get(count_key)
    if isinstance(count, int) and not isinstance(count, bool):
        return count
    return None


def follow_next_links(
    api: API,
    payload: Any,
    *,
    url: str | None = None,
    results_key: str = "results",
    next_key: str = "next",
    count_key: str = "count",
    allow_other_origins: bool = False,
) -> Results[Any]:
    """Iterate the items of a collection response, following next-page links.

    A list payload is an unpaginated collection, so its items are yielded
    directly. A mapping payload is one page. Its ``results_key`` member holds
    the page's items, and its ``next_key`` member holds the URL of the next
    page. On the last page, that member is ``None``, an empty string, or
    absent. The next page is requested only after the items of the current page
    have been consumed. A next-page URL that names a page already read raises
    ``PaginationError``, because following it would never end.

    A next-page URL is resolved against the URL of the page that holds it, as
    RFC 3986 resolves a relative reference. An absolute URL is used as is, a
    path such as ``/v1/items/?page=2`` keeps only the page's origin, and a
    query such as ``?page=2`` keeps the page's path.

    By default, a next-page URL and every redirect destination must have the
    same scheme, host, and port as the API's base URL. The session sends its
    headers and credentials with every request, so following a link to another
    origin could send them to another server, or over plain HTTP. A server
    behind a misconfigured reverse proxy can produce such links by naming its
    internal address or the wrong scheme. With ``allow_other_origins``, a
    redirect must still lead to the origin of the page request it answers, or
    to one of the API's ``redirect_origins``, as for every request.

    The defaults match the responses of Django REST Framework's
    ``PageNumberPagination``, ``LimitOffsetPagination``, and
    ``CursorPagination``. Return the iterator from ``collection_items()`` to
    read every page of a collection.

    The returned iterator's ``count`` is the first page's ``count_key``
    member, the number of items in the whole collection that
    ``PageNumberPagination`` and ``LimitOffsetPagination`` report, or ``None``
    if the page has none. ``filter()`` passes it on.

    Args:
        api: Client used to request the next pages.
        payload: Decoded JSON payload of the first response.
        url: URL of the first response, resolved against the API's base URL. If
            omitted, links in the first page are resolved against the base URL
            itself. Later pages are resolved against the URLs they were fetched
            from.
        results_key: Member of a page that holds the page's items.
        next_key: Member of a page that holds the next page's URL.
        count_key: Member of a page that holds the collection's size.
        allow_other_origins: Whether to follow next-page URLs, and redirects
            from page requests, whose origin differs from the API's base URL. A
            redirect must still lead to an origin that the API allows.

    Returns:
        Iterator over the JSON items of every page, in order.

    Raises:
        ResponseShapeError: If a page is not JSON, if a payload is neither a
            list nor a mapping, or if a page's ``results_key`` member is
            missing or is not a list.
        PaginationError: If a next-page URL names a page already read, or if
            a next-page URL or redirect has another origin and
            ``allow_other_origins`` is ``False``.
        RedirectError: If a redirect from a page request is refused, as for
            any request.
        LimitExceeded: If the collection has more pages than the API's
            ``max_pages`` or more items than its ``max_items``, or a page
            exceeds another of its limits.
    """
    count = _count_of(payload, count_key)

    def items() -> Iterator[Any]:
        """Yield the items of every page.

        Yields:
            JSON items from every page, in order.
        """
        page = payload
        reader = _PageReader(api, allow_other_origins)
        page_url = api.base_url if url is None else api.resolve_url(url)
        # Without url=, the first page's URL is unknown, and the base URL is
        # not a page.
        if url is not None:
            reader.read_urls.add(page_url)

        while True:
            if isinstance(page, list):
                yield from reader.count_items(page)
                return

            if not isinstance(page, Mapping):
                raise ResponseShapeError(
                    "Collection responses must be lists or page mappings, "
                    f"got {type(page).__name__}"
                )

            if results_key not in page:
                raise ResponseShapeError(
                    f"Collection page has no {results_key!r} member"
                )

            results = page[results_key]
            # A string would yield its characters, and a mapping its keys.
            if not isinstance(results, list):
                raise ResponseShapeError(
                    f"The {results_key!r} member of a collection page must be "
                    f"a list, got {type(results).__name__}"
                )
            yield from reader.count_items(results)

            next_url = page.get(next_key)
            # Some APIs mark the last page with an empty link. Resolving it
            # would name the current page again.
            if next_url is None or next_url == "":
                return

            resp = reader.read(urljoin(page_url, next_url))
            # After a redirect, a page's links are relative to its final URL.
            page_url = resp.url
            page = api.json(resp)

    return Results(items(), count)


def follow_link_headers(
    api: API,
    resp: requests.Response,
    *,
    allow_other_origins: bool = False,
) -> Results[Any]:
    """Iterate the items of a collection response, following Link headers.

    Each page's body is a JSON list of items, and its ``Link`` header names the
    next page with a ``next`` relation, as in ``rel="next"`` or ``rel="next
    last"``, as RFC 8288 defines and as APIs such as GitHub's send. A page
    without such a link is the last page. The next page is requested only after
    the items of the current page have been consumed. A link to a page already
    read raises ``PaginationError``, because following it would never end.

    A link is resolved against the URL of the page that holds it. As in
    ``follow_next_links()``, a next-page URL and every redirect destination
    must have the same scheme, host, and port as the API's base URL, because
    the session sends its headers and credentials with every request.

    Return the iterator from ``collection_items_from_response()`` to read every
    page of a collection.

    Args:
        api: Client used to request the next pages.
        resp: Response to the first page.
        allow_other_origins: Whether to follow next-page URLs, and redirects
            from page requests, whose origin differs from the API's base URL. A
            redirect must still lead to an origin that the API allows.

    Returns:
        Iterator over the JSON items of every page, in order. Its ``count`` is
        ``None``, because a Link header does not report the collection's size.

    Raises:
        ResponseShapeError: If a page's body is not a JSON list.
        PaginationError: If a next-page URL names a page already read, or if a
            next-page URL or redirect has another origin and
            ``allow_other_origins`` is ``False``.
        RedirectError: If a redirect from a page request is refused, as for
            any request.
        LimitExceeded: If the collection has more pages than the API's
            ``max_pages`` or more items than its ``max_items``, or a page
            exceeds another of its limits.
    """

    def items() -> Iterator[Any]:
        """Yield the items of every page.

        Yields:
            JSON items from every page, in order.
        """
        page = resp
        reader = _PageReader(api, allow_other_origins)
        reader.read_urls.add(page.url)

        while True:
            payload = api.json(page)
            if not isinstance(payload, list):
                raise ResponseShapeError(
                    "Pages linked by Link headers must be JSON lists, "
                    f"got {type(payload).__name__}"
                )

            yield from reader.count_items(payload)

            next_url = _next_link(page)
            if not next_url:
                return

            page = reader.read(urljoin(page.url, next_url))

    return Results(items())
