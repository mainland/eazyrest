"""Exceptions that eazyrest raises for its own policies."""

from __future__ import annotations

import codecs
import contextvars
import json
import math
from collections.abc import Callable, Collection, Iterator
from dataclasses import dataclass
from email.message import Message
from typing import TYPE_CHECKING, Any, NoReturn, cast

import requests

from ._protocols import Response
from ._urls import Source, _asked_text, _request_name

if TYPE_CHECKING:
    from .api import _Budget


class EazyrestError(Exception):
    """Base class of the exceptions that eazyrest defines.

    Each subclass that replaces an exception of another type, such as
    ``requests.HTTPError`` or ``ValueError``, also derives from that type, so
    code that catches the other type still catches it.

    Every subclass pickles, with its attributes, so that an error raised in
    another process, as by a ``concurrent.futures.ProcessPoolExecutor``,
    reaches the caller with its type. There are two exceptions. An error that
    holds the request of a raw ``API`` request whose body is a stream, such as
    an open file, does not pickle, because the request keeps the stream and a
    stream does not pickle. Model writes encode their bodies to bytes, so their
    errors pickle. A ``FieldConversionError`` whose model pickle cannot find by
    its module and name, as a model that a function defines, does not pickle
    either.
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

    The message names the method, the URL's origin and path with ``?...`` in
    place of a query, and the status, and the members of a JSON object body
    other than ``detail``, such as the fields that a Django REST Framework
    validation error names. It does not quote the body, whose messages can
    quote the values that were sent. The parsed body is in ``data``.
    """

    response: requests.Response
    """The response whose status reports the failure, which an ``APIError``
    always has, although requests types ``HTTPError.response`` as optional."""

    data: object
    """The response's JSON body, or ``None`` if the body is not JSON."""

    def __init__(
        self, message: str, *, response: Response, data: object
    ) -> None:
        """Describe a failed request.

        Args:
            message: Description of the failure.
            response: Response whose status reports the failure.
            data: The response's JSON body, or ``None``.
        """
        # requests types response= as its own Response, but stores any value.
        super().__init__(message, response=cast(requests.Response, response))
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

    response: requests.Response | None
    """The response whose JSON is refused. It may be ``None``, as when the
    error is raised from decoded JSON in a hook such as ``object_json()``, so
    test it before use."""

    def __init__(
        self, *args: object, response: Response | None = None
    ) -> None:
        """Describe a response that is refused.

        Args:
            *args: The message, as ``Exception`` takes it.
            response: The response whose JSON is refused, if one is in scope.
        """
        super().__init__(*args)
        # The protocol types any HTTP library's response, and requests' type is
        # the one that API returns, as APIError.response has it.
        self.response = cast("requests.Response | None", response)


class UsageError(EazyrestError, RuntimeError):
    """Raised when eazyrest is used in a way it does not support.

    For example, a model has no registered API, declares no ``class_url``, or
    has no primary-key field and is asked to write or load, a page that cannot
    read other pages is asked for one, or a ``write_options()`` block ends
    while a later block of the object is still open.
    """


_DEFAULT_MAX_JSON_DEPTH = 128
"""The default of ``API.max_json_depth``, the greatest depth of a response."""


def _json_depth(value: object, limit: int | None = None) -> int:
    """Return the nesting depth of a JSON value.

    This is the one place that counts the depth of a JSON value, whether a
    response holds it or a caller gives it to be written.

    Args:
        value: Decoded JSON value.
        limit: Depth at which to stop counting, or ``None`` to count the whole
            value. The walk ends at the first level deeper than ``limit``, so a
            value that holds itself ends it, and the walk needs no more memory
            than ``limit`` levels allow.

    Returns:
        The number of nested arrays and objects on the deepest path, so 0 for a
        scalar and 1 for a flat array. With a ``limit``, a depth over it is
        reported as ``limit + 1``.
    """
    # The stack holds one iterator per open array or object, so memory grows
    # with the nesting depth, not with the number of values at each level.
    stack: list[Iterator[object]] = [iter((value,))]
    depth = 0
    while stack:
        for child in stack[-1]:
            if isinstance(child, dict):
                stack.append(iter(child.values()))
            elif isinstance(child, list):
                stack.append(iter(child))
            else:
                continue
            depth = max(depth, len(stack) - 1)
            if limit is not None and depth > limit:
                return depth
            break
        else:
            stack.pop()
    return depth


def _response_json(resp: Response, max_depth: int | None = None) -> object:
    """Return the JSON that a response's body holds.

    Args:
        resp: Response whose body is decoded.
        max_depth: Greatest nesting depth of arrays and objects to accept, or
            ``None`` for no limit.

    Returns:
        The decoded JSON value.

    Raises:
        ResponseShapeError: If the body is not JSON, or is not text in the
            charset that the response declares, or in UTF-8 if it declares
            none. The message does not quote the body.
        LimitExceeded: If the JSON nests more deeply than ``max_depth``, or
            too deeply for Python's decoder.
    """
    where = (
        f"The response to {_response_name(resp)}, with status "
        f"{resp.status_code},"
    )
    try:
        value = json.loads(
            _body_text(resp),
            parse_constant=_invalid_json_constant,
            parse_float=_finite_float,
        )
    except ValueError as err:
        raise ResponseShapeError(
            f"{where} does not hold JSON", response=resp
        ) from err
    except RecursionError as err:
        raise LimitExceeded(
            "max_json_depth", f"{where} holds JSON nested too deeply to decode"
        ) from err

    if max_depth is not None and _json_depth(value, max_depth) > max_depth:
        raise LimitExceeded(
            "max_json_depth",
            f"{where} holds JSON nested more than {max_depth} levels deep",
        )
    return value


def _body_text(resp: Response) -> str:
    """Decode a response's body strictly, as the response declares.

    JSON that systems exchange is UTF-8, by RFC 8259, section 8.1, and a body
    that is not valid in its charset must not read as other text, which
    ``requests`` would do by replacing each bad byte with U+FFFD, or by
    guessing the charset of a body that declares none.

    Args:
        resp: Response whose body is decoded.

    Returns:
        The body as text. An initial byte order mark is dropped from a body
        that is UTF-8.

    Raises:
        ValueError: If the body does not decode in the charset that the
            response declares, UTF-8 if it declares none, or the charset is
            unknown.
    """
    header = Message()
    header["Content-Type"] = resp.headers.get("Content-Type", "")
    charset = header.get_content_charset() or "utf-8"
    try:
        codec = codecs.lookup(charset)
    except LookupError as err:
        raise ValueError("The response declares an unknown charset") from err
    if codec.name == "utf-8":
        charset = "utf-8-sig"
    try:
        return resp.content.decode(charset)
    except UnicodeDecodeError as err:
        raise ValueError("The body is not valid text") from err


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


def _is_failure(status_code: int) -> bool:
    """Return whether a status reports a failed request.

    A 4xx or 5xx status does, as for ``requests.Response.ok``.

    Args:
        status_code: HTTP status code.

    Returns:
        Whether the status is a client or server error.
    """
    return 400 <= status_code < 600


_request_budget: contextvars.ContextVar[_Budget | None] = (
    contextvars.ContextVar("_request_budget", default=None)
)
"""Budget active while Requests and its adapter handle one API request."""


@dataclass(frozen=True)
class _PageLimit:
    """The origins that the redirects of one page request may reach."""

    origins: Collection[tuple[str, str | None, int | None]]
    """The origins that a redirect of that request may reach."""
    refusal: Callable[[], Exception]
    """Return the error to raise for a redirect outside ``origins``."""


def _asked_url(resp: Response) -> str | None:
    """Return the URL that eazyrest was asked to send for a response.

    Args:
        resp: Response, which an ``API`` request returned or recorded in its
            history.

    Returns:
        What a message shows of that URL, as ``_shown()`` returns it unquoted,
        or ``None`` if ``resp`` did not come from an ``API`` request.
    """
    return cast("str | None", getattr(resp, "_asked_url", None))


def _asked_page(resp: Response) -> str | None:
    """Return which page of a collection a response's request was for.

    Args:
        resp: Response, as for ``_asked_url()``.

    Returns:
        The page, as ``"page 3"``, if a link named the page that the request
        asked for, or ``None``.
    """
    return cast("str | None", getattr(resp, "_asked_page", None))


def _response_name(resp: Response) -> str:
    """Return the request that a message names for a response.

    The name is the method and what a message shows of the URL that eazyrest
    was asked to send, never the URL that a redirect or a response hook chose,
    since a server decides those, and a signed download URL carries its
    credential in the query.

    Args:
        resp: Response whose request the message names.

    Returns:
        The method and URL. A response that did not come from an ``API``
        request, and that no request is in flight to name, is named by its own
        URL, as a server's.
    """
    method = resp.request.method
    asked = _asked_url(resp)
    if asked is not None:
        return _request_name(method, asked, _asked_page(resp))
    budget = _request_budget.get()
    if budget is not None and budget.request is not None:
        return budget.request
    return _request_name(method, _asked_text(resp.url, None, Source.SERVER))


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


_SHOWN_LENGTH = 40
"""The most characters of a reason phrase or a body's name that a message
shows, for text that a server chose."""


def _api_error(resp: Response, max_depth: int | None = None) -> APIError:
    """Return the exception for a response whose status reports a failure.

    Args:
        resp: Failed response.
        max_depth: Greatest JSON nesting depth to decode, as for
            ``_response_json()``.

    Returns:
        ``NotFound`` for a 404 and ``APIError`` otherwise, with the body's
        JSON, if any, as ``data``. The message holds the status, the reason
        phrase cut to 40 characters, and the request.
    """
    data: object = None
    if resp.content:
        try:
            data = _response_json(resp, max_depth)
        except ResponseShapeError:
            data = None

    kind = "Client" if 400 <= resp.status_code < 500 else "Server"
    # The reason phrase is chosen by the server, as the body's names are, and a
    # status line can be 65,536 bytes long, so it is bounded too.
    reason = str(resp.reason)
    if len(reason) > _SHOWN_LENGTH:
        reason = reason[:_SHOWN_LENGTH] + "..."
    message = (
        f"{resp.status_code} {kind} Error: {_printable(reason)} for "
        f"{_response_name(resp)}"
    )
    if isinstance(data, dict):
        # A server chooses the names, so bound them, and quote them, so that
        # a newline or an escape sequence reaches a log as text.
        names = sorted(str(name) for name in data if name != "detail")
        shown = [
            repr(
                name
                if len(name) <= _SHOWN_LENGTH
                else name[:_SHOWN_LENGTH] + "..."
            )
            for name in names[:10]
        ]
        if len(names) > 10:
            shown.append(f"and {len(names) - 10} more")
        if shown:
            message += f" (body names {', '.join(shown)})"
    error_type = NotFound if resp.status_code == 404 else APIError
    return error_type(message, response=resp, data=data)
