"""Exceptions that eazyrest raises for its own policies."""

from __future__ import annotations

import math
from typing import NoReturn

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


def _json_depth(value: object) -> int:
    """Return the nesting depth of a JSON value.

    Args:
        value: Decoded JSON value.

    Returns:
        The number of nested arrays and objects on the deepest path, so 0 for
        a scalar and 1 for a flat array.
    """
    depth = 0
    pending = [(value, 1)]
    while pending:
        node, level = pending.pop()
        if isinstance(node, dict):
            children: list[object] = list(node.values())
        elif isinstance(node, list):
            children = node
        else:
            continue
        depth = max(depth, level)
        pending.extend((child, level + 1) for child in children)
    return depth


def _response_json(
    resp: requests.Response, max_depth: int | None = None
) -> object:
    """Return the JSON that a response's body holds.

    Args:
        resp: Response whose body is decoded.
        max_depth: Greatest nesting depth of arrays and objects to accept, or
            ``None`` for no limit.

    Returns:
        The decoded JSON value.

    Raises:
        ResponseShapeError: If the body is not JSON. The message does not
            quote the body.
        LimitExceeded: If the JSON nests more deeply than ``max_depth``, or
            too deeply for Python's decoder.
    """
    where = (
        f"The response to {resp.request.method} {resp.url}, with status "
        f"{resp.status_code},"
    )
    try:
        value = resp.json(
            parse_constant=_invalid_json_constant, parse_float=_finite_float
        )
    except ValueError as err:
        raise ResponseShapeError(f"{where} does not hold JSON") from err
    except RecursionError as err:
        raise LimitExceeded(
            "max_json_depth", f"{where} holds JSON nested too deeply to decode"
        ) from err

    if max_depth is not None and _json_depth(value) > max_depth:
        raise LimitExceeded(
            "max_json_depth",
            f"{where} holds JSON nested more than {max_depth} levels deep",
        )
    return value


def _invalid_json_constant(value: str) -> NoReturn:
    """Reject the non-finite constants that Python's decoder accepts.

    Args:
        value: Constant spelling, which is not included in the error.

    Raises:
        ValueError: Always, because JSON has no non-finite numbers.
    """
    raise ValueError("JSON numbers must be finite")


def _finite_float(value: str) -> float:
    """Decode a JSON number without overflowing to infinity.

    Args:
        value: JSON floating-point number.

    Returns:
        Its finite Python value.

    Raises:
        ValueError: If the number is not representable as a finite float.
    """
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("JSON numbers must be finite")
    return number


class LimitExceeded(EazyrestError):
    """Raised when an operation exceeds one of the API's resource limits.

    The limits bound what a server can make the client spend: the size of a
    response, the time a request takes, the nesting depth of its JSON, and the
    pages and items of a collection. Each is an attribute of ``API``.
    """

    limit: str
    """Name of the ``API`` attribute that sets the exceeded limit."""

    def __init__(self, limit: str, message: str) -> None:
        """Describe an exceeded limit.

        Args:
            limit: Name of the ``API`` attribute that sets the limit.
            message: Description of what exceeded it.
        """
        super().__init__(f"{message}, which exceeds API.{limit}")
        self.limit = limit
