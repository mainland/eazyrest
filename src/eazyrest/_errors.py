"""Exceptions that eazyrest raises for its own policies."""

from __future__ import annotations

import contextvars
import math
import re
from typing import TYPE_CHECKING, Any, NoReturn, cast

import requests

from ._protocols import Response
from ._urls import _ServerURL

if TYPE_CHECKING:
    from .api import _Budget


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


def _response_json(resp: Response, max_depth: int | None = None) -> object:
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
        f"The response to {resp.request.method} {_response_url(resp)}, with "
        f"status {resp.status_code},"
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


def _is_failure(status_code: int) -> bool:
    """Return whether a status reports a failed request.

    A 4xx or 5xx status does, as for ``requests.Response.ok``.

    Args:
        status_code: HTTP status code.

    Returns:
        Whether the status is a client or server error.
    """
    return 400 <= status_code < 600


_USERINFO = re.compile(
    r"^(?:((?:https?:)?//)[^/?#\\]*@"
    r"|(?!https?:)([A-Za-z][A-Za-z0-9+.-]*://)[^/?#]*@)",
    re.IGNORECASE,
)
_URL_NOISE = str.maketrans("", "", "\t\r\n")
_URL_LEADING = re.compile(r"^[\s\x00-\x20]+")


def _redact_url(url: object) -> str:
    """Return a URL, without any user and password, to quote in a message.

    An error message quotes the URL that the caller gave for a request, with
    its query, and a URL such as ``http://user:pass@host/`` would put the
    credentials into a log.

    Args:
        url: URL that requests prepares, as a string or as ``bytes``, which are
            decoded as UTF-8 as requests decodes them, or ``None``. It may lack
            a scheme, as a ``Location`` header that begins with ``//`` does.

    Returns:
        The URL as it is, but for the user and password before the host, which
        are left out, so ``http://user:pass@host/x?q=1`` becomes
        ``http://host/x?q=1`` and ``//user:pass@host/x`` becomes ``//host/x``.
        For ``http``, ``https``, and a URL without a scheme, which requests
        joins onto the scheme of its base, the user and password end at a
        backslash, as urllib3 ends the authority there, so that the host named
        is the one that requests connects to once it has prepared the URL. A
        raw ``Location`` that it has not prepared may name another host. For
        other schemes they end at the last ``@`` before a ``/``, ``?``, or
        ``#``. The match reads the URL as requests and ``urlsplit()`` do, which
        ignore leading control characters, spaces, and Unicode whitespace such
        as a no-break space, and remove tabs, carriage returns, and line feeds.
        A URL that has a user and password is quoted in that form, so
        ``http://user:pass@host/x`` with a form feed before it becomes
        ``http://host/x``.
    """
    text, normal = _normalized_url(url)
    match = _USERINFO.match(normal)
    if match is None:
        return text
    return (match.group(1) or match.group(2)) + normal[match.end() :]


_UNSHOWN_URL = "<URL not shown>"

_request_budget: contextvars.ContextVar[_Budget | None] = (
    contextvars.ContextVar("_request_budget", default=None)
)
"""Budget active while Requests and its adapter handle one API request."""


def _first_url(resp: Response) -> str:
    """Return the URL that was sent first for the request of a response.

    Args:
        resp: Response, after requests has set its ``history``.

    Returns:
        The URL of the first response of the chain that led to ``resp``, which
        is the URL of ``resp`` itself if no redirect led to it. A challenge
        that an authentication handler answered by resending the request is in
        the history too, with the URL that was given.
    """
    return resp.history[0].url if resp.history else resp.url


def _response_url(resp: Response, sent: str | None = None) -> str:
    """Return the URL of a response, to quote in a message.

    A redirect's ``Location`` is the server's choice, and a signed download URL
    carries its credential in the query. So the URL that the client sent first
    for a request, which the caller gave or a page linked, is quoted by
    ``_redact_url()``, and any other URL of a response is quoted by its origin
    and path, built from the parts of the URL that requests sent, as for a
    redirect that is refused.

    Args:
        resp: Response whose URL the message names.
        sent: The URL that was sent first for the request, as requests prepared
            it, which a response hook knows from the request's budget because
            requests has not set the history yet. By default, the first URL of
            the active request's budget, so that every message of one request
            agrees even if a hook replaced the response or changed its URL, or,
            with no request active, the URL that the history of ``resp`` starts
            with.

    Returns:
        The URL without its user and password if it is the URL that was sent
        first. Otherwise its origin and path alone, or, if requests could not
        have sent the URL, which cannot be read as an HTTP or HTTPS URL, a text
        that names none of it.
    """
    if sent is None:
        budget = _request_budget.get()
        if budget is not None and budget.first_url is not None:
            sent = budget.first_url
        else:
            sent = _first_url(resp)
    if resp.url == sent:
        return _redact_url(resp.url)
    return _redirected_url(resp.url)


def _redirected_url(url: str) -> str:
    """Return a URL that the server chose, to quote in a message.

    Args:
        url: The URL, as requests sent it.

    Returns:
        Its origin and path, built from the parts that requests reads, or a
        text that names none of it if it cannot be read as an HTTP or HTTPS
        URL.
    """
    try:
        prepared = _ServerURL.from_prepared(str(url))
    except ValueError:
        return _UNSHOWN_URL
    if not prepared.is_http:
        return _UNSHOWN_URL
    return prepared.origin_and_path


def _normalized_url(url: object) -> tuple[str, str]:
    """Return a URL as text, and as requests and ``urlsplit()`` read it.

    Args:
        url: URL, as for ``_redact_url()``.

    Returns:
        The URL as text, and that text without leading control characters,
        spaces, and Unicode whitespace, and without tabs, carriage returns, and
        line feeds.
    """
    if isinstance(url, bytes):
        text = url.decode("utf-8", "replace")
    else:
        text = str(url)
    return text, _URL_LEADING.sub("", text.translate(_URL_NOISE))


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


def _api_error(resp: Response, max_depth: int | None = None) -> APIError:
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
        f"{resp.request.method} {_response_url(resp)}"
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
