"""Helpers for collection endpoints that split their results into pages."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from typing import Any
from urllib.parse import urljoin, urlsplit

import requests

from .api import API

_DEFAULT_PORTS = {"http": 80, "https": 443}


def _origin(url: str) -> tuple[str, str | None, int | None]:
    """Return the scheme, host, and port of an absolute URL."""
    # Requests and urlsplit disagree about backslashes in an authority. Check
    # the URL Requests would send, before attaching session credentials.
    request = requests.PreparedRequest()
    request.prepare_url(url, None)
    assert request.url is not None
    parts = urlsplit(request.url)
    scheme = parts.scheme.lower()
    return scheme, parts.hostname, parts.port or _DEFAULT_PORTS.get(scheme)


def follow_next_links(
    api: API,
    payload: Any,
    *,
    url: str | None = None,
    results_key: str = "results",
    next_key: str = "next",
    allow_other_origins: bool = False,
) -> Iterator[Any]:
    """Yield the items of a collection response, following next-page links.

    A list payload is an unpaginated collection, so its items are yielded
    directly. A mapping payload is one page. Its ``results_key`` member holds
    the page's items, and its ``next_key`` member holds the URL of the next
    page. On the last page, that member is ``None``, an empty string, or
    absent. The next page is requested only after the items of the current
    page have been consumed. A next-page URL that names a page already read
    raises ``ValueError``, because following it would never end.

    A next-page URL is resolved against the URL of the page that holds it, as
    RFC 3986 resolves a relative reference. An absolute URL is used as is, a
    path such as ``/v1/items/?page=2`` keeps only the page's origin, and a
    query such as ``?page=2`` keeps the page's path.

    By default, a next-page URL and every redirect destination must have the
    same scheme, host, and port as the API's base URL. The session sends its
    headers and credentials with every request, so following a link to another
    origin could send them to another server, or over plain HTTP. A server
    behind a misconfigured reverse proxy can produce such links by naming its
    internal address or the wrong scheme.

    The defaults match the responses of Django REST Framework's
    ``PageNumberPagination``, ``LimitOffsetPagination``, and
    ``CursorPagination``. Return the iterator from ``collection_items()`` to
    read every page of a collection.

    Args:
        api: Client used to request the next pages.
        payload: Decoded JSON payload of the first response.
        url: URL of the first response, resolved against the API's base URL.
            If omitted, links in the first page are resolved against the base
            URL itself. Later pages are resolved against the URLs they were
            fetched from.
        results_key: Member of a page that holds the page's items.
        next_key: Member of a page that holds the next page's URL.
        allow_other_origins: Whether to follow next-page URLs and redirects
            whose origin differs from the API's base URL.

    Yields:
        JSON items from every page, in order.

    Raises:
        TypeError: If a payload is neither a list nor a mapping.
        ValueError: If a page has no ``results_key`` member, if a next-page
            URL names a page already read, or if a next-page URL or redirect
            has another origin and ``allow_other_origins`` is ``False``.
    """
    api_origin = _origin(api.base_url)
    page_url = api.base_url if url is None else api.resolve_url(url)
    # The URLs of the pages read so far. Without url=, the first page's URL
    # is unknown, and the base URL is not a page.
    read_urls: set[str] = set() if url is None else {page_url}

    def check_origin(next_url: str) -> None:
        """Reject a page or redirect URL before credentials can be sent."""
        if not allow_other_origins and _origin(next_url) != api_origin:
            raise ValueError(
                f"Next-page URL {next_url!r} has a different origin from the "
                f"API's base URL {api.base_url!r}. Pass "
                "allow_other_origins=True to follow it."
            )

    def check_redirect(resp: requests.Response, **kwargs: Any) -> None:
        """Check redirects before requests follows them."""
        target = api.session.get_redirect_target(resp)
        if target is not None:
            try:
                check_origin(urljoin(resp.url, target))
            except ValueError:
                resp.close()
                raise

    while True:
        if isinstance(payload, list):
            yield from payload
            return

        if not isinstance(payload, Mapping):
            raise TypeError(
                "Collection responses must be lists or page mappings, "
                f"got {type(payload).__name__}"
            )

        if results_key not in payload:
            raise ValueError(f"Collection page has no {results_key!r} member")

        yield from payload[results_key]

        next_url = payload.get(next_key)
        # Some APIs mark the last page with an empty link. Resolving it would
        # name the current page again.
        if next_url is None or next_url == "":
            return

        next_url = urljoin(page_url, next_url)
        if next_url in read_urls:
            raise ValueError(
                f"Next-page URL {next_url!r} names a page that was already "
                "read, so following it would never end"
            )
        check_origin(next_url)

        # Request hooks replace session hooks, so preserve the existing ones.
        # Run the guard last, after any hook that might change the response.
        hooks = [*api.session.hooks.get("response", []), check_redirect]
        resp = api.get(next_url, hooks={"response": hooks})
        # After a redirect, a page's links are relative to its final URL.
        page_url = resp.url
        read_urls.update((next_url, page_url))
        payload = resp.json()
