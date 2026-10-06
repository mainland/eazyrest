"""Helpers for collection endpoints that split their results into pages."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from typing import Any

from .api import API


def follow_next_links(
    api: API,
    payload: Any,
    *,
    results_key: str = "results",
    next_key: str = "next",
) -> Iterator[Any]:
    """Yield the items of a collection response, following next-page links.

    A list payload is an unpaginated collection, so its items are yielded
    directly. A mapping payload is one page. Its ``results_key`` member holds
    the page's items, and its ``next_key`` member holds the URL of the next
    page, or ``None`` on the last page. The next page is requested only after
    the items of the current page have been consumed.

    The defaults match the responses of Django REST Framework's
    ``PageNumberPagination``, ``LimitOffsetPagination``, and
    ``CursorPagination``. Return the iterator from ``collection_items()`` to
    read every page of a collection.

    Args:
        api: Client used to request the next pages. A relative next-page URL
            is resolved against its base URL.
        payload: Decoded JSON payload of the first response.
        results_key: Member of a page that holds the page's items.
        next_key: Member of a page that holds the next page's URL.

    Yields:
        JSON items from every page, in order.

    Raises:
        TypeError: If a payload is neither a list nor a mapping.
        ValueError: If a page has no ``results_key`` member.
    """
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
        if next_url is None:
            return

        payload = api.get(next_url).json()
