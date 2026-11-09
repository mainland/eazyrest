"""Helpers for collection endpoints that split their results into pages."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator, Mapping
from typing import Any, Generic, TypeVar
from urllib.parse import parse_qsl, urljoin, urlsplit

import requests
from requests.utils import parse_header_links

from ._errors import (
    LimitExceeded,
    PaginationError,
    ResponseShapeError,
    UsageError,
    _redact_url,
)
from ._protocols import Client, Response
from ._results import Results
from .api import (
    _as_api,
    _check_final_response,
    _origin,
    _redirect_destination,
    _redirect_origins,
    _response_hooks,
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
        PaginationError: If the link cannot be parsed.
    """
    try:
        return urljoin(base, link)
    except ValueError as err:
        raise PaginationError(
            f"Page URL {_redact_url(link)!r} cannot be parsed"
        ) from err


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


def _check_page_origin(api: Client, url: str) -> None:
    """Reject a page URL whose origin is not the API's base URL's.

    Args:
        api: Client that would request the page.
        url: Absolute URL of the page.

    Raises:
        PaginationError: If ``url`` has another origin.
    """
    try:
        origin = _origin(url)
    except ValueError as err:
        raise PaginationError(
            f"Page URL {_redact_url(url)!r} cannot be parsed"
        ) from err
    if origin != _origin(api.base_url):
        raise PaginationError(
            f"Page URL {_redact_url(url)!r} has a different origin from the "
            f"API's base URL {_redact_url(api.base_url)!r}. Pass "
            "allow_other_origins=True to follow it."
        )


def _request_page(
    api: Client, url: str, allow_other_origins: bool
) -> requests.Response:
    """Request the page at ``url``, checking its origin and redirects.

    Args:
        api: Client that sends the request.
        url: Absolute URL of the page.
        allow_other_origins: Whether the page and its redirects may have an
            origin other than the API's base URL. A redirect must still lead
            to an origin that the API allows.

    Returns:
        Response for the page.

    Raises:
        PaginationError: If ``url`` or a redirect has another origin and other
            origins are not allowed.
        RedirectError: If a redirect is refused, as for any request, or the
            response is a redirect that was not followed.
    """
    api = _as_api(api)
    api_origin = _origin(api.base_url)
    if not allow_other_origins:
        _check_page_origin(api, url)
    follow = bool(api._request_options.get("allow_redirects", True))

    def check_redirect(resp: requests.Response, **kwargs: Any) -> None:
        """Check redirects before requests follows them.

        The API checks every redirect too, as for any request. This hook adds
        the page's own origin restriction.

        Args:
            resp: Response, which may be a redirect.
            kwargs: Other arguments that requests passes to response hooks,
                which are ignored.

        Raises:
            PaginationError: If ``resp`` redirects to an origin other than the
                API's base URL and other origins are not allowed. The response
                is closed first. A client that does not follow redirects never
                raises it, because its page requests would not follow the
                redirect even with ``allow_other_origins=True``, and the
                unfollowed redirect raises ``RedirectError`` instead.
        """
        if not follow:
            return
        try:
            target = api.session.get_redirect_target(resp)
            if not target or allow_other_origins:
                return
            destination = _redirect_destination(resp, target)
            origins = _redirect_origins(resp, target)
        except ValueError:
            # The API's own guard refuses a location that cannot be parsed.
            return
        if origins != {api_origin}:
            resp.close()
            raise PaginationError(
                f"Page request {_redact_url(resp.url)!r} was redirected to "
                f"{_redact_url(destination)!r}, which has a different origin "
                f"from the API's base URL {_redact_url(api.base_url)!r}. Pass "
                "allow_other_origins=True to follow it."
            )

    # Request hooks replace session hooks, so preserve the existing ones,
    # unless the API's request options carry hooks, which replace them as they
    # do for the first page. Run the guard after any hook that might change
    # the response.
    session_hooks = (
        None
        if api._request_options.get("hooks")
        else api.session.hooks.get("response")
    )
    hooks = [*_response_hooks(session_hooks), check_redirect]
    # A link carries the query that the server received, with the session's
    # parameters, which requests would add again to each page. A parameter
    # given as None is left out when requests merges them.
    session_params = api.session.params
    query = {name for name, _ in parse_qsl(urlsplit(url).query, True)}
    params = (
        {name: None for name in session_params if name in query}
        if isinstance(session_params, Mapping)
        else {}
    )
    try:
        resp = api.get(url, params=params, hooks={"response": hooks})
    except (
        requests.exceptions.InvalidURL,
        requests.exceptions.MissingSchema,
    ) as err:
        raise PaginationError(
            f"Page URL {_redact_url(url)!r} cannot be requested"
        ) from err
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
            raise PaginationError(
                f"Next-page URL {_redact_url(url)!r} names a page that was "
                "already read, "
                "so following it would never end"
            )
        if not self.allow_other_origins:
            _check_page_origin(self.api, url)
        limit = self.api.max_pages
        if limit is not None and self.pages >= limit:
            raise LimitExceeded(
                "max_pages", f"The collection has more than {limit} pages"
            )
        self.pages += 1
        resp = _request_page(self.api, url, self.allow_other_origins)
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

    previous_url: str | None
    """Absolute URL of the previous page, or ``None`` on the first page."""

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
        self.previous_url = previous_url
        self._source: _PageSource[T] | None = None
        self._allow_other_origins = False
        self._forward = _Trail(frozenset({url}), 1)
        self._backward = _Trail(frozenset({url}), 1)

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
        since a representation reaches logs in the same way.

        Returns:
            Representation of the page.
        """
        return f"<Page url={_redact_url(self.url)!r} items={len(self.items)}>"

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
            raise PaginationError(
                f"Page URL {_redact_url(url)!r} names a page that was already "
                "read, so "
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
        return self.page(_request_page(self.api, url, allow_other_origins))

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
        return Page(
            results,
            url=resp.url,
            count=_count_of(payload, count_key),
            next_url=_page_link(payload, next_key, resp.url),
            previous_url=_page_link(payload, previous_key, resp.url),
        )

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
        links = {
            name: (_resolve(resp.url, link) if link else None)
            for name, link in (
                ("next", _link(resp, frozenset({"next"}))),
                ("previous", _link(resp, frozenset({"prev", "previous"}))),
            )
        }
        return Page(
            payload,
            url=resp.url,
            next_url=links["next"],
            previous_url=links["previous"],
        )

    return _JSONPageSource(api, parse, allow_other_origins).page(resp)
