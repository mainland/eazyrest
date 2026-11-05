"""Exceptions that eazyrest raises for its own policies."""

from __future__ import annotations

import requests


class EazyrestError(Exception):
    """Base class of the exceptions that eazyrest defines.

    Each subclass that replaces an exception of another type, such as
    ``requests.HTTPError`` or ``ValueError``, also derives from that type, so
    code that catches the other type still catches it.
    """


class RedirectError(EazyrestError, requests.HTTPError):
    """Raised when a redirect is refused before it is followed.

    A redirect is refused if it leads to an origin that the API does not
    allow, or if requests would follow it without the request's body or with
    another method. The ``response`` attribute holds the redirect.
    """


class PaginationError(EazyrestError, ValueError):
    """Raised when following a collection's pages is refused.

    A next-page link or a redirect from a page request may lead to another
    origin, or name a page already read, which would never end.
    """


class ResponseShapeError(EazyrestError, TypeError, ValueError):
    """Raised when a response's JSON does not have the expected shape.

    For example, an object's response holds a list, or a collection page has
    no list of results.
    """


class UsageError(EazyrestError, RuntimeError):
    """Raised when eazyrest is used in a way it does not support.

    For example, a model has no registered API, or a lazy related collection
    is iterated a second time.
    """


def _response_json(resp: requests.Response) -> object:
    """Return the JSON that a response's body holds.

    Args:
        resp: Response whose body is decoded.

    Returns:
        The decoded JSON value.

    Raises:
        ResponseShapeError: If the body is not JSON. The message does not
            quote the body.
    """
    try:
        return resp.json()
    except requests.JSONDecodeError as err:
        raise ResponseShapeError(
            f"The response to {resp.request.method} {resp.url}, with status "
            f"{resp.status_code}, does not hold JSON"
        ) from err
