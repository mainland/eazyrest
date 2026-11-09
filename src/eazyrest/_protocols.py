"""The interfaces of the responses and clients that eazyrest's hooks receive.

The hooks and helpers are typed against these protocols rather than the
requests classes, so that a later release can send requests with another
HTTP library without changing their signatures. ``API`` and the responses
it returns satisfy them.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Protocol

__all__ = ["Client", "Request", "Response"]


class Request(Protocol):
    """The request that a response answers."""

    @property
    def method(self) -> str | None:
        """The request's HTTP method, such as ``"GET"``."""
        ...


class Response(Protocol):
    """An HTTP response, as eazyrest's hooks and helpers receive it.

    This is what eazyrest guarantees of a response, whatever HTTP library
    sends the request. ``API`` returns a ``requests.Response``, which has
    these members and more. A hook that uses another of its members, such as
    ``ok`` or ``links``, depends on requests.
    """

    @property
    def status_code(self) -> int:
        """The response's HTTP status code."""
        ...

    @property
    def reason(self) -> str:
        """The status line's reason phrase, such as ``"Not Found"``."""
        ...

    @property
    def headers(self) -> Mapping[str, str]:
        """The response's headers, whose names compare case-insensitively."""
        ...

    @property
    def content(self) -> bytes:
        """The response's body."""
        ...

    @property
    def text(self) -> str:
        """The response's body, decoded as text."""
        ...

    @property
    def url(self) -> str:
        """The absolute URL of the response, after any redirect."""
        ...

    @property
    def request(self) -> Request:
        """The request that the response answers."""
        ...

    @property
    def history(self) -> Sequence[Response]:
        """The redirects that led to the response, oldest first."""
        ...

    def json(self, **kwargs: Any) -> Any:
        """Decode the body as JSON.

        Prefer ``Client.json()``, which applies the client's limits.

        Args:
            **kwargs: Keyword arguments for ``json.loads()``.

        Returns:
            The decoded JSON value.
        """
        ...


class Client(Protocol):
    """The client that eazyrest passes to hooks as ``api``.

    A hook sends requests through it, such as for later pages or a bulk
    lookup, and reads its URL and limits. ``API`` satisfies it, and so does
    the request-scoped copy of an ``API`` that a query with
    ``request_options=`` passes. eazyrest's helpers accept only eazyrest's own
    clients, so this protocol describes what a hook may use rather than an
    interface to implement.
    """

    @property
    def base_url(self) -> str:
        """The URL that request URIs are resolved against."""
        ...

    @property
    def total_timeout(self) -> float | None:
        """Seconds that a request may take in all, or ``None``."""
        ...

    @property
    def max_response_bytes(self) -> int | None:
        """Greatest size of a response body, or ``None``."""
        ...

    @property
    def max_json_depth(self) -> int | None:
        """Greatest nesting depth of a response's JSON, or ``None``."""
        ...

    @property
    def max_pages(self) -> int | None:
        """Greatest number of pages one traversal reads, or ``None``."""
        ...

    @property
    def max_items(self) -> int | None:
        """Greatest number of items one traversal yields, or ``None``."""
        ...

    def resolve_url(self, uri: str) -> str:
        """Resolve a request URI against ``base_url``.

        Args:
            uri: Relative or absolute request URI.

        Returns:
            The absolute URL.
        """
        ...

    def json(self, resp: Response) -> Any:
        """Decode a response's JSON with the client's depth limit.

        Args:
            resp: Response that the client returned.

        Returns:
            The decoded JSON value.
        """
        ...

    def get(self, uri: str, *args: Any, **kwargs: Any) -> Response:
        """Send a ``GET`` request.

        Args:
            uri: Relative or absolute request URI.
            *args: Positional arguments for the request.
            **kwargs: Request options for the request.

        Returns:
            The successful response.
        """
        ...

    def post(self, uri: str, *args: Any, **kwargs: Any) -> Response:
        """Send a ``POST`` request.

        Args:
            uri: Relative or absolute request URI.
            *args: Positional arguments for the request.
            **kwargs: Request options for the request.

        Returns:
            The successful response.
        """
        ...

    def put(self, uri: str, *args: Any, **kwargs: Any) -> Response:
        """Send a ``PUT`` request.

        Args:
            uri: Relative or absolute request URI.
            *args: Positional arguments for the request.
            **kwargs: Request options for the request.

        Returns:
            The successful response.
        """
        ...

    def patch(self, uri: str, *args: Any, **kwargs: Any) -> Response:
        """Send a ``PATCH`` request.

        Args:
            uri: Relative or absolute request URI.
            *args: Positional arguments for the request.
            **kwargs: Request options for the request.

        Returns:
            The successful response.
        """
        ...

    def delete(self, uri: str, *args: Any, **kwargs: Any) -> Response:
        """Send a ``DELETE`` request.

        Args:
            uri: Relative or absolute request URI.
            *args: Positional arguments for the request.
            **kwargs: Request options for the request.

        Returns:
            The successful response.
        """
        ...
