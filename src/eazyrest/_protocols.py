"""The interface of the responses that eazyrest's hooks receive.

The hooks and helpers are typed against these protocols rather than the
requests classes, so that a later release can send requests with another
HTTP library without changing their signatures. The responses that ``API``
returns satisfy them.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Protocol

__all__ = ["Request", "Response"]


class Request(Protocol):
    """The request that a response answers."""

    @property
    def method(self) -> str | None:
        """The request's HTTP method, such as ``"GET"``."""


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

    @property
    def reason(self) -> str:
        """The status line's reason phrase, such as ``"Not Found"``."""

    @property
    def headers(self) -> Mapping[str, str]:
        """The response's headers, whose names compare case-insensitively."""

    @property
    def content(self) -> bytes:
        """The response's body."""

    @property
    def text(self) -> str:
        """The response's body, decoded as text."""

    @property
    def url(self) -> str:
        """The absolute URL of the response, after any redirect."""

    @property
    def request(self) -> Request:
        """The request that the response answers."""

    @property
    def history(self) -> Sequence[Response]:
        """The redirects that led to the response, oldest first."""

    def json(self, **kwargs: Any) -> Any:
        """Decode the body as JSON.

        Prefer ``API.json()``, which applies the client's limits.

        Args:
            **kwargs: Keyword arguments for ``json.loads()``.

        Returns:
            The decoded JSON value.
        """
