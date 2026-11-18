"""Helpers for collection endpoints that split their results into pages."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator, Mapping
from typing import Any, Generic, TypeVar
from urllib.parse import urljoin

import requests
from requests.utils import parse_header_links

from ._errors import (
    LimitExceeded,
    PaginationError,
    ResponseShapeError,
    UsageError,
    _asked_url,
    _redact_url,
)
from ._protocols import Client, Response
from ._results import Results
from .api import (
    _UNPARSABLE_PAGE_URL,
    _as_api,
    _as_sender,
    _check_final_response,
    _origin,
    _page_url_subject,
)

__all__ = [
    "Page",
    "follow_link_headers",
    "follow_next_links",
    "link_headers_page",
    "next_links_page",
]

T = TypeVar("T")


def _link(resp: Response, rels: frozenset[str]) -> str | None:
    """Return the URL of a link with one of ``rels`` in a Link header.

    A link may have several relation types, as in ``rel="next last"``, and
    registered relation types compare case-insensitively, as RFC 8288 says.

    Args:
        resp: Response whose Link header is read.
        rels: Relation types to look for, in lowercase.

    Returns:
        The URL of the first link whose relation types include one of
        ``rels``, or ``None`` if there is none.
    """
    header = resp.headers.get("Link")
    if not header:
        return None
    # As requests' Response.links does, key the links by their whole rel
    # value, so that a later link with the same value replaces an earlier one.
    links: dict[str, dict[str, str]] = {}
    for link in parse_header_links(header):
        key = link.get("rel") or link.get("url")
        if key is not None:
            links[key] = link
    for link in links.values():
        if rels.intersection(link.get("rel", "").lower().split()):
            url: str | None = link.get("url")
            return url

    return None


def _page_link(page: Mapping[str, Any], key: str, base: str) -> str | None:
    """Return the absolute URL of a page's link to another page.

    Args:
        page: Decoded JSON page.
        key: Member of the page that holds the link.
        base: URL of the page, against which a relative link is resolved.

    Returns:
        The link resolved against ``base``, or ``None`` if the member is
        absent, null, or empty. Some APIs mark the last page with an empty
        link, which resolving would turn into the current page.

    Raises:
        ResponseShapeError: If the member holds something other than a string.
    """
    link = page.get(key)
    if link is None or link == "":
        return None
    if not isinstance(link, str):
        raise ResponseShapeError(
            f"The {key!r} member of a collection page must be a URL, got "
            f"{type(link).__name__}"
        )
    return _resolve(base, link)


def _resolve(base: str, link: str) -> str:
    """Resolve a page link against the URL of the page that holds it.

    Args:
        base: URL of the page.
        link: Link, absolute or relative.

    Returns:
        The absolute URL.

    Raises:
        PaginationError: If the link cannot be parsed. The message quotes none
            of it, since the server chose it and no rule says which of its text
            is a host and which a password, and no error is attached, since the
            one that rejected the link can quote it.
    """
    try:
        return urljoin(base, link)
    except ValueError:
        pass
    raise PaginationError(_UNPARSABLE_PAGE_URL) from None


def _page_results(page: Any, results_key: str) -> list[Any] | None:
    """Return a page's items, checking the page's shape.

    Args:
        page: Decoded JSON payload of a page.
        results_key: Member of a page mapping that holds its items.

    Returns:
        The items of a page mapping, or ``None`` for a list payload, which is
        an unpaginated collection.

    Raises:
        ResponseShapeError: If the payload is neither a list nor a mapping, or
            its ``results_key`` member is missing or is not a list.
    """
    if isinstance(page, list):
        return None
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
            f"The {results_key!r} member of a collection page must be a list, "
            f"got {type(results).__name__}"
        )
    return results


def _check_page_origin(
    api: Client, url: str, from_server: bool = False
) -> None:
    """Reject a page URL whose origin is not the API's base URL's.

    The check is the one that the request makes before it sends, so a link that
    cannot be requested is refused before it counts as a page.

    Args:
        api: Client that would request the page.
        url: Absolute URL of the page.
        from_server: Whether a response supplied ``url``, as a next-page link.

    Raises:
        PaginationError: If ``url`` has another origin, or requests cannot
            prepare it. The message of the second quotes none of it, and
            attaches no error, as for ``_resolve()``. The message of the first
            quotes the URL's origin alone if ``from_server``, and otherwise the
            link as requests prepares it, without its user and password. It
            says only that the URL is not an HTTP or HTTPS URL if it is not.
            The page's link is on ``Page.next_url``.
    """
    sender, _ = _as_sender(api)
    sender._check_page_destination(
        url, {_origin(sender.base_url)}, from_server
    )


def _request_page(
    api: Client,
    url: str,
    allow_other_origins: bool,
    from_server: bool = False,
) -> requests.Response:
    """Request the page at ``url``, checking its origin and redirects.

    The request goes to the API's private sender and not to ``get()``, with the
    options of a scoped view. Unless ``allow_other_origins`` is set, the page
    and its redirects may reach the API's origin alone. Otherwise the page may
    have any origin, and its redirects follow the rule of any other request.

    Args:
        api: Client that sends the request.
        url: URL of the page, absolute or relative to the API's base URL.
        allow_other_origins: Whether the page and its redirects may have an
            origin other than the API's base URL. A redirect must still lead
            to an origin that the API allows.
        from_server: Whether a response supplied ``url``, as a next-page link,
            so that a message quotes its origin alone.

    Returns:
        Response for the page.

    Raises:
        PaginationError: If ``url`` or a redirect has another origin and other
            origins are not allowed, or if requests cannot prepare ``url`` or
            has no adapter to send it, so that nothing was sent. The message of
            the second quotes none of the link, and no error is attached.
        RedirectError: If a redirect is refused, as for any request, or the
            response is a redirect that was not followed.
        requests.RequestException: If the request fails, as for any request.
    """
    sender, options = _as_sender(api)
    own = None if allow_other_origins else {_origin(sender.base_url)}
    resp = sender._send_page(
        url, options, from_server=from_server, initial=own, redirects=own
    )
    return _check_final_response(resp, "the page has no response to read")


class _PageReader:
    """Request the next pages of a collection, guarding their URLs."""

    def __init__(self, api: Client, allow_other_origins: bool) -> None:
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
            subject = _page_url_subject(
                _as_api(self.api)._read_server_url(url),
                "Next-page URL",
                from_server=True,
            )
            raise PaginationError(
                f"{subject} names a page that was already read, so "
                "following it would never end"
            )
        if not self.allow_other_origins:
            _check_page_origin(self.api, url, from_server=True)
        limit = self.api.max_pages
        if limit is not None and self.pages >= limit:
            raise LimitExceeded(
                "max_pages", f"The collection has more than {limit} pages"
            )
        self.pages += 1
        resp = _request_page(
            self.api, url, self.allow_other_origins, from_server=True
        )
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
    api: Client,
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
        PaginationError: If a next-page URL names a page already read, or if a
            next-page URL or a redirect that the request follows has another
            origin and ``allow_other_origins`` is ``False``.
        RedirectError: If a redirect from a page request is refused, as for any
            request, or if the request does not follow redirects and a page
            request is redirected.
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
            results = _page_results(page, results_key)
            if results is None:
                yield from reader.count_items(page)
                return
            yield from reader.count_items(results)

            next_url = _page_link(page, next_key, page_url)
            if next_url is None:
                return

            resp = reader.read(next_url)
            # After a redirect, a page's links are relative to its final URL.
            page_url = resp.url
            page = api.json(resp)

    return Results(items(), count)


def follow_link_headers(
    api: Client,
    resp: Response,
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
            next-page URL or a redirect that the request follows has another
            origin and ``allow_other_origins`` is ``False``.
        RedirectError: If a redirect from a page request is refused, as for any
            request, or if the request does not follow redirects and a page
            request is redirected.
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

            next_url = _link(page, frozenset({"next"}))
            if not next_url:
                return

            page = reader.read(_resolve(page.url, next_url))

    return Results(items())


def _follow_pages(
    api: Client,
    first: Page[Any],
    read_page: Callable[[Response], Page[Any]],
) -> Results[Any]:
    """Iterate the items of a page and of the pages that it links to.

    The next page is requested only after the items of the current page have
    been consumed. The limits, the check for a link to a page already read, and
    the origin check are those of ``follow_next_links()``, and ``max_pages``
    and ``max_items`` count the whole traversal. A page without a way to read
    others ends the traversal after its items.

    Args:
        api: Client that requests the later pages.
        first: The first page.
        read_page: Reads a page from the response that requested it.

    Returns:
        Iterator over the items of every page, in order. Its ``count`` is the
        first page's.

    Raises:
        PaginationError: If a next-page URL names a page already read, or has
            another origin and the pages do not allow other origins.
        LimitExceeded: If the pages hold more items than the API's
            ``max_items``, or are more than its ``max_pages``.
    """

    def items() -> Iterator[Any]:
        """Yield the items of every page.

        Yields:
            Items from every page, in order.
        """
        page = first
        reader = _PageReader(api, first._allow_other_origins)
        reader.read_urls.add(first.url)
        while True:
            yield from reader.count_items(page.items)
            if page._source is None or page.next_url is None:
                return
            page = read_page(reader.read(page.next_url))

    return Results(items(), first.count)


class _Trail:
    """The pages read in one direction from a first page."""

    def __init__(self, urls: frozenset[str], pages: int) -> None:
        """Record the pages read in one direction.

        Args:
            urls: URLs of the pages, as requested and after redirects.
            pages: Number of pages, counting the first.
        """
        self.urls = urls
        self.pages = pages


class _PageSource(Generic[T]):
    """Reads the page at a URL, as the pages that it returns link to it."""

    @property
    def api(self) -> Client:
        """Client that requests the pages, which never changes."""
        raise NotImplementedError

    def read(self, url: str, allow_other_origins: bool) -> Page[T]:
        """Request and read the page at ``url``.

        Args:
            url: Absolute URL of the page.
            allow_other_origins: Whether the page may have another origin.

        Returns:
            The page.
        """
        raise NotImplementedError


class Page(Generic[T]):
    """One page of a collection, with links to the pages around it.

    Iterating a page yields its items. ``next_page()`` and ``previous_page()``
    request the pages that it links to. A link is checked as
    ``follow_next_links()`` checks one: its origin must be the API's base
    URL's unless the page allows other origins, a link to a page already read
    in the same direction raises ``PaginationError``, and reading more pages
    in one direction than the API's ``max_pages`` raises ``LimitExceeded``.

    A page without items is false, even when it links to another page, so
    loop over pages with ``while page is not None``.
    """

    items: list[T]
    """The page's items."""

    url: str
    """Absolute URL of the page, after any redirect."""

    count: int | None
    """Size of the whole collection, if the page reports it."""

    next_url: str | None
    """Absolute URL of the next page, or ``None`` on the last page."""

    def __init__(
        self,
        items: Iterable[T],
        *,
        url: str,
        count: int | None = None,
        next_url: str | None = None,
        previous_url: str | None = None,
    ) -> None:
        """Create a page.

        A page that a collection hook builds this way cannot read other
        pages. Build one with ``next_links_page()`` or ``link_headers_page()``
        to navigate.

        Args:
            items: The page's items.
            url: Absolute URL of the page.
            count: Size of the whole collection, if known.
            next_url: Absolute URL of the next page, if any.
            previous_url: Absolute URL of the previous page, if any.
        """
        self.items = list(items)
        self.url = url
        self.count = count
        self.next_url = next_url
        self._previous_url = previous_url
        self._previous_link: Callable[[], str | None] | None = None
        self._source: _PageSource[T] | None = None
        self._asked_url: str | None = None
        """URL that was asked for to read the page, if it was read."""
        self._allow_other_origins = False
        self._forward = _Trail(frozenset({url}), 1)
        self._backward = _Trail(frozenset({url}), 1)

    @property
    def previous_url(self) -> str | None:
        """Absolute URL of the previous page, or ``None`` on the first page.

        A page that a reader built resolves the link when it is first needed,
        so a page whose previous link is malformed can be read and iterated,
        and the link's error is raised here and by ``previous_page()``.
        """
        if self._previous_link is not None:
            self._previous_url = self._previous_link()
            self._previous_link = None
        return self._previous_url

    @previous_url.setter
    def previous_url(self, url: str | None) -> None:
        self._previous_link = None
        self._previous_url = url

    def _defer_previous(self, link: Callable[[], str | None]) -> None:
        """Resolve the previous page's URL when it is first read.

        Args:
            link: Function that returns the URL, or raises the error that a
                malformed link deserves.
        """
        self._previous_link = link

    def __iter__(self) -> Iterator[T]:
        """Iterate the page's items.

        Returns:
            Iterator over ``items``.
        """
        return iter(self.items)

    def __len__(self) -> int:
        """Return the number of items on the page.

        Returns:
            Number of items.
        """
        return len(self.items)

    def __repr__(self) -> str:
        """Return the page's URL and size.

        The URL leaves out any user and password, as an error message does,
        since a representation reaches logs in the same way. A page that was
        read shows the URL that eazyrest was asked to send, which is the page
        link or the URL that the caller gave, and not the URL that a redirect
        chose. A page that was built by hand shows its ``url``.

        Returns:
            Representation of the page.
        """
        asked = self._asked_url
        url = _redact_url(self.url) if asked is None else asked
        return f"<Page url={url!r} items={len(self.items)}>"

    def next_page(self) -> Page[T] | None:
        """Request the next page.

        Returns:
            The next page, or ``None`` if this is the last page.

        Raises:
            UsageError: If the page was built without a way to read others.
            PaginationError: If the link names a page already read going
                forward, or its origin is not allowed.
            LimitExceeded: If reading it would read more than the API's
                ``max_pages`` going forward.
        """
        return self._adjacent(self.next_url, forward=True)

    def previous_page(self) -> Page[T] | None:
        """Request the previous page.

        Returns:
            The previous page, or ``None`` if this is the first page.

        Raises:
            UsageError: If the page was built without a way to read others.
            PaginationError: If the link names a page already read going
                back, or its origin is not allowed.
            LimitExceeded: If reading it would read more than the API's
                ``max_pages`` going back.
        """
        return self._adjacent(self.previous_url, forward=False)

    def _adjacent(self, url: str | None, *, forward: bool) -> Page[T] | None:
        """Request the page at a link, guarding against endless traversals.

        Args:
            url: Absolute URL of the page, or ``None``.
            forward: Whether the link is the next page's.

        Returns:
            The page, or ``None`` if ``url`` is ``None``.

        Raises:
            UsageError: If the page has no source.
            PaginationError: If ``url`` names a page already read in that
                direction.
            LimitExceeded: If the page would be one more than ``max_pages``.
        """
        if url is None:
            return None
        if self._source is None:
            raise UsageError(
                "This page cannot read other pages. Build it with "
                "next_links_page() or link_headers_page()."
            )
        trail = self._forward if forward else self._backward
        if url in trail.urls:
            subject = _page_url_subject(
                _as_api(self._source.api)._read_server_url(url),
                "Page URL",
                from_server=True,
            )
            raise PaginationError(
                f"{subject} names a page that was already read, so "
                "following it would never end"
            )
        limit = self._source.api.max_pages
        if limit is not None and trail.pages >= limit:
            raise LimitExceeded(
                "max_pages", f"The collection has more than {limit} pages"
            )
        page = self._source.read(url, self._allow_other_origins)
        extended = _Trail(trail.urls | {url, page.url}, trail.pages + 1)
        if forward:
            page._forward = extended
        else:
            page._backward = extended
        return page


def _check_page_size(api: Client, items: list[Any]) -> None:
    """Reject a page with more items than the API allows in a collection.

    Args:
        api: Client whose ``max_items`` applies.
        items: Items of the page.

    Raises:
        LimitExceeded: If the page has more than ``max_items`` items.
    """
    limit = api.max_items
    if limit is not None and len(items) > limit:
        raise LimitExceeded(
            "max_items", f"The collection page has more than {limit} items"
        )


class _JSONPageSource(_PageSource[Any]):
    """Reads pages of JSON items with one of the page helpers."""

    def __init__(
        self,
        api: Client,
        parse: Callable[[Response], Page[Any]],
        allow_other_origins: bool,
    ) -> None:
        """Read pages through ``api`` with ``parse``.

        Args:
            api: Client that requests the pages.
            parse: Reads a page from its response.
            allow_other_origins: Whether pages may have other origins.
        """
        self._api = api
        self._parse = parse
        self._allow_other_origins = allow_other_origins

    @property
    def api(self) -> Client:
        """Client that requests the pages."""
        return self._api

    def read(self, url: str, allow_other_origins: bool) -> Page[Any]:
        """Request and read the page at ``url``.

        Args:
            url: Absolute URL of the page.
            allow_other_origins: Whether the page may have another origin.

        Returns:
            The page.
        """
        return self.page(
            _request_page(self.api, url, allow_other_origins, from_server=True)
        )

    def page(self, resp: Response) -> Page[Any]:
        """Read a page from its response and attach this source.

        Args:
            resp: Response for the page.

        Returns:
            The page, which can read the pages it links to.
        """
        page = self._parse(resp)
        _check_page_size(self.api, page.items)
        page._source = self
        page._asked_url = _asked_url(resp)
        page._allow_other_origins = self._allow_other_origins
        return page


def next_links_page(
    api: Client,
    resp: Response,
    *,
    results_key: str = "results",
    next_key: str = "next",
    previous_key: str = "previous",
    count_key: str = "count",
    allow_other_origins: bool = False,
) -> Page[Any]:
    """Read one page of a collection whose pages link to each other.

    The defaults match Django REST Framework's paginators: a page is a
    mapping whose ``results`` member holds its items, whose ``next`` and
    ``previous`` members hold the URLs of the pages around it, and whose
    ``count`` member holds the size of the whole collection. A list payload
    is a single unpaginated page without links. Links are resolved against
    the page's URL, and checked when they are followed, as for
    ``follow_next_links()``.

    Return the page from ``collection_page()`` to navigate a model's
    collection with ``get_page()``.

    Args:
        api: Client that requests the other pages.
        resp: Response for the page.
        results_key: Member of a page that holds its items.
        next_key: Member of a page that holds the next page's URL.
        previous_key: Member of a page that holds the previous page's URL.
        count_key: Member of a page that holds the collection's size.
        allow_other_origins: Whether linked pages, and redirects from their
            requests, may have an origin other than the API's base URL.

    Returns:
        The page of JSON items.

    Raises:
        ResponseShapeError: If the page is not JSON, is neither a list nor a
            mapping, has no list of items, or holds a link that is not a URL.
        LimitExceeded: If the page has more items than the API's
            ``max_items``, or exceeds another of its limits.
    """

    def parse(resp: Response) -> Page[Any]:
        """Read a page from its response.

        Args:
            resp: Response for the page.

        Returns:
            The page.
        """
        payload = api.json(resp)
        results = _page_results(payload, results_key)
        if results is None:
            return Page(payload, url=resp.url)
        page = Page(
            results,
            url=resp.url,
            count=_count_of(payload, count_key),
            next_url=_page_link(payload, next_key, resp.url),
        )
        page._defer_previous(
            lambda: _page_link(payload, previous_key, resp.url)
        )
        return page

    return _JSONPageSource(api, parse, allow_other_origins).page(resp)


def link_headers_page(
    api: Client,
    resp: Response,
    *,
    allow_other_origins: bool = False,
) -> Page[Any]:
    """Read one page of a collection whose pages link in Link headers.

    The page's body is a JSON list of its items, and its ``Link`` header names
    the next page with a ``next`` relation and the previous page with ``prev``
    or ``previous``, as RFC 8288 defines and as APIs such as GitHub's send.
    Links are resolved against the page's URL, and checked when they are
    followed, as for ``follow_link_headers()``. ``count`` is ``None``.

    Args:
        api: Client that requests the other pages.
        resp: Response for the page.
        allow_other_origins: Whether linked pages, and redirects from their
            requests, may have an origin other than the API's base URL.

    Returns:
        The page of JSON items.

    Raises:
        ResponseShapeError: If the page's body is not a JSON list.
        LimitExceeded: If the page has more items than the API's
            ``max_items``, or exceeds another of its limits.
    """

    def parse(resp: Response) -> Page[Any]:
        """Read a page from its response.

        Args:
            resp: Response for the page.

        Returns:
            The page.
        """
        payload = api.json(resp)
        if not isinstance(payload, list):
            raise ResponseShapeError(
                "Pages linked by Link headers must be JSON lists, "
                f"got {type(payload).__name__}"
            )
        link = _link(resp, frozenset({"next"}))
        page = Page(
            payload,
            url=resp.url,
            next_url=_resolve(resp.url, link) if link else None,
        )

        def previous() -> str | None:
            """Resolve the previous page's link.

            Returns:
                The absolute URL, or ``None`` if the header has none.
            """
            link = _link(resp, frozenset({"prev", "previous"}))
            return _resolve(resp.url, link) if link else None

        page._defer_previous(previous)
        return page

    return _JSONPageSource(api, parse, allow_other_origins).page(resp)
