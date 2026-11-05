"""Exceptions that eazyrest raises for its own policies."""

from __future__ import annotations

import math
from typing import Any, NoReturn

import requests


def _printable(text: object) -> str:
    """Return text that a server chose, with its control characters escaped.

    Args:
        text: Text, such as a reason phrase.

    Returns:
        The text, with each character that is not printable escaped as
        ``repr()`` escapes it.
    """
    return "".join(
        char if char.isprintable() else repr(char)[1:-1] for char in str(text)
    )


class EazyrestError(Exception):
    """Base class of the exceptions that eazyrest defines.

    Each subclass that replaces an exception of another type, such as
    ``requests.HTTPError`` or ``ValueError``, also derives from that type, so
    code that catches the other type still catches it.

    Every subclass pickles, with its attributes, so that an error raised in
    another process, as by a ``concurrent.futures.ProcessPoolExecutor``,
    reaches the caller with its type. The exception is an error that holds the
    request of a raw ``API`` request whose body is a stream, such as an open
    file, because the request keeps the stream and a stream does not pickle.
    Model writes encode their bodies to bytes, so their errors pickle.
    """

    def __reduce__(self) -> tuple[Any, ...]:
        """Return how to rebuild this exception without its constructor.

        A subclass's constructor may take other arguments than ``args``.

        Returns:
            A function that rebuilds the exception from its type, ``args``, and
            attributes, and those three.
        """
        return (_rebuild, (type(self), self.args, dict(vars(self))))


def _rebuild(
    cls: type[EazyrestError], args: tuple[Any, ...], state: dict[str, Any]
) -> EazyrestError:
    """Rebuild a pickled eazyrest exception.

    Args:
        cls: The exception's type.
        args: Its ``args``.
        state: Its attributes.

    Returns:
        The exception.
    """
    error = cls.__new__(cls)
    Exception.__init__(error, *args)
    vars(error).update(state)
    return error


class DoesNotExist(EazyrestError):
    """Raised when no object matches a query that expects one result."""


class MultipleObjectsReturned(EazyrestError):
    """Raised when a query expected one object but got multiple results."""


class PrefetchNotSupported(EazyrestError):
    """Raised when explicit prefetch is requested for an unsupported model."""


class APIError(EazyrestError, requests.HTTPError):
    """Raised for a response whose status reports a failed request.

    ``API.raise_for_response()`` raises it, or ``NotFound`` for a 404, in
    place of requests' ``HTTPError``, which it also is. The ``response``
    attribute holds the response.

    The message names the method, URL, and status, and the members of a JSON
    object body other than ``detail``, such as the fields that a Django REST
    Framework validation error names. It does not quote the body, whose
    messages can quote the values that were sent. The parsed body is in
    ``data``.
    """

    response: requests.Response
    """The response whose status reports the failure, which an ``APIError``
    always has, although requests types ``HTTPError.response`` as optional."""

    data: object
    """The response's JSON body, or ``None`` if the body is not JSON."""

    def __init__(
        self, message: str, *, response: requests.Response, data: object
    ) -> None:
        """Describe a failed request.

        Args:
            message: Description of the failure.
            response: Response whose status reports the failure.
            data: The response's JSON body, or ``None``.
        """
        super().__init__(message, response=response)
        self.data = data


class NotFound(APIError, DoesNotExist):
    """Raised for a response with status 404.

    It is also ``DoesNotExist``, so ``except DoesNotExist`` catches an object
    that a lazy load finds deleted, as it catches a lookup that matches
    nothing.
    """


class RedirectError(EazyrestError, requests.HTTPError):
    """Raised for an unsafe redirect or an unconfirmed model write.

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


def _api_error(
    resp: requests.Response, max_depth: int | None = None
) -> APIError:
    """Return the exception for a response whose status reports a failure.

    Args:
        resp: Failed response.
        max_depth: Greatest JSON nesting depth to decode, as for
            ``_response_json()``.

    Returns:
        ``NotFound`` for a 404 and ``APIError`` otherwise, with the body's
        JSON, if any, as ``data``.
    """
    data: object = None
    if resp.content:
        try:
            data = _response_json(resp, max_depth)
        except ResponseShapeError:
            data = None

    kind = "Client" if 400 <= resp.status_code < 500 else "Server"
    message = (
        f"{resp.status_code} {kind} Error: {_printable(resp.reason)} for "
        f"{resp.request.method} {resp.url}"
    )
    if isinstance(data, dict):
        # A server chooses the names, so bound them, and quote them, so that
        # a newline or an escape sequence reaches a log as text.
        names = sorted(str(name) for name in data if name != "detail")
        shown = [
            repr(name if len(name) <= 40 else name[:40] + "...")
            for name in names[:10]
        ]
        if len(names) > 10:
            shown.append(f"and {len(names) - 10} more")
        if shown:
            message += f" (body names {', '.join(shown)})"
    error_type = NotFound if resp.status_code == 404 else APIError
    return error_type(message, response=resp, data=data)
