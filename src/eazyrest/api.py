"""HTTP API client primitives for eazyrest."""

from __future__ import annotations

import contextvars
import copy
import sys
import time
from collections.abc import Callable, Collection, Mapping
from http.cookiejar import CookieJar
from types import MappingProxyType
from typing import Any
from urllib.parse import urljoin, urlparse, urlsplit, urlunsplit

import requests
from requests.structures import CaseInsensitiveDict
from requests.utils import requote_uri
from urllib3.exceptions import (
    DecodeError,
    MaxRetryError,
    ProtocolError,
    ReadTimeoutError,
    SSLError,
)
from urllib3.response import BaseHTTPResponse
from urllib3.util import Retry, Timeout

from ._errors import (
    LimitExceeded,
    RedirectError,
    _api_error,
    _is_failure,
    _printable,
    _response_json,
)
from .write_mode import WriteMode, validate_write_mode

__all__ = ["API"]

if sys.version_info >= (3, 11):
    from typing import Self
else:
    from typing_extensions import Self

# The configuration that reset_session() copies to a new session. These are the
# attributes of requests.Session.__attrs__ other than adapters, which hold the
# connection pools.
_SESSION_SETTINGS = (
    "headers",
    "cookies",
    "auth",
    "proxies",
    "hooks",
    "params",
    "verify",
    "cert",
    "stream",
    "trust_env",
    "max_redirects",
)

_DEFAULT_PORTS = {"http": 80, "https": 443}


def _origin(url: str) -> tuple[str, str | None, int | None]:
    """Return the scheme, host, and port of an absolute URL.

    Args:
        url: Absolute URL.

    Returns:
        Lowercased scheme, host, and port of ``url`` as Requests would send it.
        If ``url`` names no port, the port is the scheme's default port, or
        ``None`` if the scheme has no known default.
    """
    # Requests and urlsplit disagree about backslashes in an authority. Check
    # the URL Requests would send, before attaching session credentials.
    request = requests.PreparedRequest()
    request.prepare_url(url, None)
    assert request.url is not None
    parts = urlsplit(request.url)
    scheme = parts.scheme.lower()
    return scheme, parts.hostname, parts.port or _DEFAULT_PORTS.get(scheme)


def _canonical_url(url: str) -> str:
    """Return an absolute URL in one spelling for each resource it names.

    The URL is prepared as Requests would send it, and its scheme and host
    are lowercased, a default port is dropped, and a fragment is removed, so
    that two spellings of one hyperlink compare equal.

    Args:
        url: Absolute URL.

    Returns:
        The URL in its canonical spelling.
    """
    request = requests.PreparedRequest()
    request.prepare_url(url, None)
    assert request.url is not None
    parts = urlsplit(request.url)
    scheme = parts.scheme.lower()
    host = parts.hostname or ""
    if ":" in host:
        host = f"[{host}]"
    if parts.port is not None and parts.port != _DEFAULT_PORTS.get(scheme):
        host = f"{host}:{parts.port}"
    userinfo, _, _ = parts.netloc.rpartition("@")
    netloc = f"{userinfo}@{host}" if userinfo else host
    return urlunsplit((scheme, netloc, parts.path or "/", parts.query, ""))


# Redirects that make requests resend the request body. requests drops the body
# of a request that it follows through any other redirect.
_BODY_PRESERVING_REDIRECTS = frozenset({307, 308})


def _redirect_destination(resp: requests.Response, target: str) -> str:
    """Return the URL to which requests follows a redirect.

    This repeats the steps of ``requests.Session.resolve_redirects()``.
    requests sends the resulting string without preparing it again, so an
    origin check must examine exactly this string. In particular, requests
    percent-encodes a backslash in an absolute target, which moves the end of
    its userinfo, so ``urljoin()`` of the raw target can name a different host.

    Args:
        resp: Redirect response.
        target: Redirect target, from ``Session.get_redirect_target()``.

    Returns:
        Absolute URL of the redirected request.
    """
    url = target
    if url.startswith("//"):
        url = f"{urlparse(resp.url).scheme}:{url}"
    parsed = urlparse(url)
    url = parsed.geturl()
    if not parsed.netloc:
        return urljoin(resp.url, requote_uri(url))
    return requote_uri(url)


def _redirect_origins(
    resp: requests.Response, target: str
) -> set[tuple[str, str | None, int | None]]:
    r"""Return the origins that a redirect can be taken to name.

    The connection that requests opens for a redirect uses ``urlparse()`` of
    the URL that ``_redirect_destination()`` returns. A proxy, or a client that
    treats a backslash as a slash, as browsers do, may read the target
    differently, so ``https://a.test\@b.test/`` names ``b.test`` to requests
    but ``a.test`` to a browser. A redirect is safe only if every reading names
    an allowed origin.

    Args:
        resp: Redirect response.
        target: Redirect target, from ``Session.get_redirect_target()``.

    Returns:
        The origins of the redirect under each reading. A reading that fails
        gives an origin with no host, which no allowed origin matches.
    """
    destination = _redirect_destination(resp, target)
    origins: set[tuple[str, str | None, int | None]] = set()
    try:
        parts = urlsplit(destination)
        scheme = parts.scheme.lower()
        origins.add(
            (scheme, parts.hostname, parts.port or _DEFAULT_PORTS.get(scheme))
        )
    except ValueError:
        origins.add(("", None, None))
    for url in (destination, urljoin(resp.url, target)):
        try:
            origins.add(_origin(url))
        except (ValueError, requests.RequestException):
            origins.add(("", None, None))
    return origins


def _drop_response_hooks(resp: Any, request: Any = None) -> None:
    """Remove the response hooks from the requests of a response.

    A sent request's response hooks have run. They can hold objects that
    cannot be pickled, such as eazyrest's redirect guards, which are local
    functions, and the thread-local state of ``requests.auth.HTTPDigestAuth``,
    so a response that keeps them, or an error that holds such a response or
    request, could not reach another process.

    Args:
        resp: Response, with its history, or ``None``.
        request: A request that an error holds, or ``None``.
    """
    requests_sent = [request]
    if isinstance(resp, requests.Response):
        requests_sent += [hop.request for hop in (*resp.history, resp)]
    for sent in requests_sent:
        hooks = getattr(sent, "hooks", None)
        if isinstance(hooks, dict) and hooks.get("response"):
            hooks["response"] = []


def _response_hooks(hooks: Any) -> list[Callable[..., Any]]:
    """Return response hooks as a list, as requests accepts them.

    Args:
        hooks: A response hook, a sequence of them, or ``None``.

    Returns:
        A new list of the hooks.
    """
    if hooks is None:
        return []
    if callable(hooks):
        return [hooks]
    return list(hooks)


def _merge_request_options(
    base: Mapping[str, Any], override: Mapping[str, Any]
) -> dict[str, Any]:
    """Return request options with ``override`` applied over ``base``.

    Headers are merged, matching names without regard to case, and hooks are
    combined per event, with ``base``'s first. For any other option,
    ``override``'s value wins. A header whose value is ``None`` is kept, so
    requests still removes that header from the session's.

    Args:
        base: Options that apply unless ``override`` sets them.
        override: Options that take precedence.

    Returns:
        The merged options, as a new dict.
    """
    merged = {**base, **override}
    if base.get("headers") and override.get("headers"):
        headers: CaseInsensitiveDict[Any] = CaseInsensitiveDict(
            base["headers"]
        )
        headers.update(override["headers"])
        merged["headers"] = headers
    if base.get("hooks") and override.get("hooks"):
        merged["hooks"] = {
            event: [
                *_response_hooks(base["hooks"].get(event)),
                *_response_hooks(override["hooks"].get(event)),
            ]
            for event in {*base["hooks"], *override["hooks"]}
        }
    return merged


def _check_final_response(
    resp: requests.Response, consequence: str
) -> requests.Response:
    """Refuse a final response that is a redirect.

    A request that expects a result, such as a model write or a query, has
    none when it ends with a redirect that was not followed, as with
    ``allow_redirects=False``.

    Args:
        resp: Response that the API accepted.
        consequence: What the redirect means for the caller, ending the
            message, such as "the model write was not confirmed".

    Returns:
        ``resp``, when its status is not a redirect.

    Raises:
        RedirectError: If the response has a 3xx status. The response is
            closed first.
    """
    if 300 <= resp.status_code < 400:
        resp.close()
        raise RedirectError(
            f"{resp.request.method} {resp.url} returned status "
            f"{resp.status_code}, so {consequence}",
            response=resp,
        )
    return resp


def _redirected_method(method: str, status: int) -> str:
    """Return the method with which requests follows a redirect.

    This repeats ``requests.Session.rebuild_method()``.

    Args:
        method: Method of the redirected request, in upper case.
        status: Status code of the redirect.

    Returns:
        Method of the request that requests sends next.
    """
    if status == 303 and method != "HEAD":
        return "GET"
    if status == 302 and method != "HEAD":
        return "GET"
    if status == 301 and method == "POST":
        return "GET"
    return method


def _check_write_redirect(resp: requests.Response, target: str) -> None:
    """Refuse a redirect that would not resend the request it answers.

    requests follows a 301, 302, or 303 without the request body. It changes a
    ``POST`` to a ``GET`` for a 301 or 302, and any method but ``HEAD`` to a
    ``GET`` for a 302 or 303, but it resends a ``PATCH`` or ``PUT`` answered
    with a 301 with its method and without its body. A server that answered
    the resent request would report success for a request it never received.

    Args:
        resp: Redirect response.
        target: Redirect target.

    Raises:
        RedirectError: If requests would follow ``resp`` with another method,
            or without the body of the request it answers. The response is
            closed first.
    """
    request = resp.request
    method = (request.method or "").upper()
    if resp.status_code in _BODY_PRESERVING_REDIRECTS:
        return
    changed = _redirected_method(method, resp.status_code) != method
    if not changed and request.body is None:
        return

    resp.close()
    if resp.status_code == 303:
        # A 303 answers a request that the server performed, and points to
        # its result, so retrying the request would repeat it.
        raise RedirectError(
            f"{method} {resp.url} was answered with status 303, which says "
            f"that the server performed it and points to its result at "
            f"{_printable(target)}, which was not read. Do not retry the "
            f"{method}.",
            response=resp,
        )
    raise RedirectError(
        f"{method} {resp.url} was redirected with status {resp.status_code} "
        f"to {_printable(target)}, which requests follows "
        + ("as a GET" if changed else "without the request body")
        + f", so the {method} request would not be performed. Only 307 "
        "and 308 redirects resend a request unchanged. Send the request to "
        "the URL that the server expects, for example by setting "
        "API.trailing_slash.",
        response=resp,
    )


# Bytes to read from a response body at a time. read1() returns as soon as
# any bytes arrive, so a slow body cannot hold a read open past the deadline
# for longer than one socket timeout.
_READ_SIZE = 64 * 1024


class _Budget:
    """The limits of one request: its body size and its total time."""

    def __init__(
        self,
        max_bytes: int | None,
        total_timeout: float | None,
        streamed: bool = False,
    ) -> None:
        """Start the budget of a request.

        Args:
            max_bytes: Greatest decoded body size of each response, or
                ``None`` for no limit.
            total_timeout: Seconds that the request may take, including
                redirects and reading bodies, or ``None`` for no limit.
            streamed: Whether the caller reads the body of a successful final
                response, so that the adapter leaves it unread.
        """
        self.max_bytes = max_bytes
        self.total_timeout = total_timeout
        self.streamed = streamed
        self.deadline = (
            None if total_timeout is None else time.monotonic() + total_timeout
        )

    def timeout(self, timeout: Any) -> Any:
        """Return a request timeout that ends no later than the deadline.

        Args:
            timeout: Timeout that the request would otherwise use, as requests
                accepts it, also a ``urllib3.Timeout``.

        Returns:
            ``timeout``, with each part reduced to the time remaining. A part
            that waits without a limit becomes the time remaining, as does a
            part of a ``urllib3.Timeout`` that is unset, when the socket's
            default timeout has no limit.
        """
        if self.deadline is None:
            return timeout
        remaining = max(self.deadline - time.monotonic(), 0.001)
        if timeout is None:
            return remaining
        if isinstance(timeout, Timeout):

            def cap(part: Any) -> float:
                """Return a timeout part that ends by the deadline."""
                part = Timeout.resolve_default_timeout(part)
                return remaining if part is None else min(part, remaining)

            # urllib3 keeps the parts in private attributes, and its properties
            # raise or combine them with the total.
            total = Timeout.resolve_default_timeout(timeout.total)
            return Timeout(
                total=None if total is None else min(total, remaining),
                connect=cap(timeout._connect),
                read=cap(timeout._read),
            )
        if isinstance(timeout, tuple):
            return tuple(
                remaining if part is None else min(part, remaining)
                for part in timeout
            )
        return min(timeout, remaining)

    def check_time(self, resp: requests.Response | None = None) -> None:
        """Raise if the request has run out of time.

        Args:
            resp: Response being handled, if any, which is closed first.

        Raises:
            LimitExceeded: If the deadline has passed.
        """
        if self.deadline is not None and time.monotonic() >= self.deadline:
            where = "The request"
            if resp is not None:
                resp.close()
                where = f"{resp.request.method} {resp.url}"
            raise LimitExceeded(
                "total_timeout",
                f"{where} took at least {self.total_timeout} seconds",
            )

    def read_body(self, resp: requests.Response) -> None:
        """Read a streamed response's body within the budget.

        The body is read into the response, so ``resp.content`` and
        ``resp.json()`` use it as if requests had read it.

        Args:
            resp: Response sent with ``stream=True``, whose body is unread.

        Raises:
            LimitExceeded: If the body is larger than the limit, or reading it
                passes the deadline. The response is closed first.
        """
        # requests marks a body that it has not read with False.
        if getattr(resp, "_content", None) is not False:
            return

        declared = resp.headers.get("Content-Length", "")
        # A length longer than the limit's has more digits than int() reads
        # quickly, and str.isdigit() also accepts digits that int() refuses.
        if (
            self.max_bytes is not None
            and declared.isascii()
            and declared.isdigit()
            and (
                len(declared) > len(str(self.max_bytes))
                or int(declared) > self.max_bytes
            )
        ):
            self._too_large(resp)

        raw = resp.raw
        read = getattr(raw, "read1", None) or raw.read
        chunks = []
        size = 0
        try:
            while True:
                self.check_time(resp)
                chunk = read(_READ_SIZE, decode_content=True)
                if not chunk:
                    break
                size += len(chunk)
                if self.max_bytes is not None and size > self.max_bytes:
                    self._too_large(resp)
                chunks.append(chunk)
        except (ProtocolError, DecodeError, ReadTimeoutError, SSLError) as err:
            if isinstance(err, ReadTimeoutError):
                # The socket timeout is at most the time that remains, so a
                # read that timed out at the deadline ran out of total time.
                self.check_time(resp)
            resp.close()
            error_type: type[requests.RequestException]
            if isinstance(err, ProtocolError):
                error_type = requests.exceptions.ChunkedEncodingError
            elif isinstance(err, DecodeError):
                error_type = requests.exceptions.ContentDecodingError
            elif isinstance(err, ReadTimeoutError):
                error_type = requests.ConnectionError
            else:
                error_type = requests.exceptions.SSLError
            raise error_type(err, request=resp.request, response=resp) from err
        self.check_time(resp)

        resp._content = b"".join(chunks)
        resp._content_consumed = True  # type: ignore[attr-defined]
        release = getattr(raw, "release_conn", None)
        if release is not None:
            release()

    def _too_large(self, resp: requests.Response) -> None:
        """Close a response whose body is too large, and raise.

        Args:
            resp: Response whose body exceeds the limit.

        Raises:
            LimitExceeded: Always.
        """
        resp.close()
        raise LimitExceeded(
            "max_response_bytes",
            f"The response to {resp.request.method} {resp.url} is larger "
            f"than {self.max_bytes} bytes",
        )


_request_budget: contextvars.ContextVar[_Budget | None] = (
    contextvars.ContextVar("_request_budget", default=None)
)
"""Budget active while Requests and its adapter handle one API request."""


class _BudgetRetry(Retry):
    """Apply the active request's budget to urllib3's internal retries."""

    @classmethod
    def wrap(cls, retry: Retry) -> Self:
        """Copy a retry configuration without changing its policy.

        Args:
            retry: Configuration that the HTTP adapter accepted.

        Returns:
            The same settings, with budget checks around retry work.
        """
        return cls(
            total=retry.total,
            connect=retry.connect,
            read=retry.read,
            redirect=retry.redirect,
            status=retry.status,
            other=retry.other,
            allowed_methods=retry.allowed_methods,
            status_forcelist=retry.status_forcelist,
            backoff_factor=retry.backoff_factor,
            backoff_max=retry.backoff_max,
            retry_after_max=retry.retry_after_max,
            raise_on_redirect=retry.raise_on_redirect,
            raise_on_status=retry.raise_on_status,
            history=retry.history,
            respect_retry_after_header=retry.respect_retry_after_header,
            remove_headers_on_redirect=retry.remove_headers_on_redirect,
            backoff_jitter=retry.backoff_jitter,
        )

    def increment(
        self,
        method: str | None = None,
        url: str | None = None,
        response: BaseHTTPResponse | None = None,
        error: Exception | None = None,
        _pool: Any = None,
        _stacktrace: Any = None,
    ) -> Self:
        """Bound an intermediate response before urllib3 drains its body.

        Args:
            method: Request method.
            url: Request URL.
            response: Response that would cause a retry, if any.
            error: Transport failure that would cause a retry, if any.
            _pool: Connection pool, forwarded to urllib3.
            _stacktrace: Transport failure's traceback, forwarded to urllib3.

        Returns:
            Retry configuration for the next attempt.

        Raises:
            LimitExceeded: If the response or request exceeds its budget.
            MaxRetryError: If the retry policy has been exhausted.
        """
        budget = _request_budget.get()
        wrapped = None
        if budget is not None:
            if response is not None:
                wrapped = requests.Response()
                wrapped.raw = response
                wrapped.status_code = response.status
                wrapped.headers.update(response.headers)
                wrapped.url = url or ""
                wrapped.request = requests.PreparedRequest()
                wrapped.request.method = method
            budget.check_time(wrapped)

        try:
            retry = super().increment(
                method, url, response, error, _pool, _stacktrace
            )
        except MaxRetryError:
            # With raise_on_status=False, urllib3 returns the unread response
            # to Requests, and _send() reads it under the same budget.
            if (
                budget is not None
                and wrapped is not None
                and self.raise_on_status
            ):
                budget.read_body(wrapped)
            raise
        if budget is not None and wrapped is not None:
            budget.read_body(wrapped)
        return retry

    def sleep(self, response: BaseHTTPResponse | None = None) -> None:
        """Wait between attempts without sleeping past the request deadline.

        Args:
            response: Response whose Retry-After header may set the delay.

        Raises:
            LimitExceeded: If the request deadline is reached.
        """
        budget = _request_budget.get()
        if budget is None or budget.deadline is None:
            super().sleep(response)
            return
        budget.check_time()
        delay = (
            self.get_retry_after(response)
            if self.respect_retry_after_header and response is not None
            else None
        )
        if not delay:
            delay = self.get_backoff_time()
        remaining = max(budget.deadline - time.monotonic(), 0.0)
        if delay > 0:
            time.sleep(min(delay, remaining))
        budget.check_time()


class _HTTPAdapter(requests.adapters.HTTPAdapter):
    """Requests adapter whose standard retry policy obeys the API budget."""

    def __init__(self, **kwargs: Any) -> None:
        """Configure connection pools and budget-aware retries.

        Args:
            kwargs: Arguments accepted by Requests' HTTPAdapter.
        """
        super().__init__(**kwargs)
        self.max_retries = _BudgetRetry.wrap(self.max_retries)

    def send(
        self,
        request: requests.PreparedRequest,
        stream: bool = False,
        timeout: Any = None,
        verify: Any = True,
        cert: Any = None,
        proxies: Any = None,
    ) -> requests.Response:
        """Send a request, and bound the response before any hook sees it.

        Requests runs its response hooks on the response that this returns, and
        an authentication handler such as ``HTTPDigestAuth`` reads the body of
        a 401 challenge there. Reading it here first, within the active budget,
        keeps the size and time limits true for every handler. A body that the
        caller asked to stream stays unread when the status reports neither a
        failure nor a redirect. Requests reads the body of a redirect itself,
        without a limit, even when it does not follow the redirect, so that it
        can prepare ``Response.next``.

        Args:
            request: Prepared request to send.
            stream: Whether to leave the body of a successful response unread,
                as for Requests.
            timeout: Timeout, as for Requests.
            verify: Certificate verification, as for Requests.
            cert: Client certificate, as for Requests.
            proxies: Proxies for the request, as for Requests.

        Returns:
            The response.

        Raises:
            LimitExceeded: If the response body is larger than the limit, or
                the request has run out of time.
        """
        resp = super().send(request, stream, timeout, verify, cert, proxies)
        budget = _request_budget.get()
        if budget is not None:
            budget.check_time(resp)
            if (
                not budget.streamed
                or _is_failure(resp.status_code)
                or resp.is_redirect
            ):
                budget.read_body(resp)
        return resp


class API:
    """HTTP client wrapper for REST APIs.

    The class wraps a ``requests.Session`` and automatically joins request
    paths against ``base_url`` while applying a default timeout.

    Requests follows redirects, but only a 307 or 308 redirect keeps the
    request's method and body. It resends a ``POST`` answered with a 301 or
    302, and any other request answered with a 302 or 303, as a ``GET``, and it
    resends a request answered with a 301 without its body. The server then
    never receives the original request, so a redirect that would change the
    method or drop a body raises ``RedirectError`` before it is followed.
    Django's ``APPEND_SLASH`` answers a URL that lacks its trailing slash with
    such a redirect.

    A redirect is followed only to the origin of the request it answers, or to
    an origin in ``redirect_origins``. Requests drops an ``Authorization``
    header when a redirect leaves the host, but it sends every other header,
    such as an API key or a CSRF token, and a 307 or 308 resends the body. A
    redirect to another origin raises ``RedirectError`` before anything is sent
    there. ``RedirectError`` is a ``requests.HTTPError``, whose ``response`` is
    the redirect. A raw request sent with ``allow_redirects=False`` follows
    nothing, so it returns a redirect as it is, without these checks, unless
    its ``Location`` cannot be parsed, which raises ``RedirectError``.
    """

    base_url: str
    """Base API URL."""

    session: requests.Session
    """Session object for API requests."""

    timeout: float | tuple[float, float] | None = 10.0
    """Default request timeout in seconds, as requests accepts it.

    This bounds the wait to connect and the wait for each read from the
    socket, not the whole request. A server that keeps sending a byte now and
    then never reaches it. ``total_timeout`` bounds the whole request. A pair
    gives the connect and read timeouts separately, and ``None`` waits forever.
    A request's own ``timeout`` argument takes precedence.
    """

    total_timeout: float | None = 300.0
    """Seconds that a request may take in all, or ``None`` for no limit.

    The time includes redirects, retries, retry delays, and reading response
    bodies with the adapters this client creates. It is checked between the
    reads of a body and between attempts, so a body that arrives slowly
    overruns it by at most one ``timeout``, but response headers, or the
    framing of a chunked or compressed body, that a server sends byte by byte
    can overrun it without bound. A request that overruns it raises
    ``LimitExceeded``. Each request of an operation that sends several, such as
    reading the pages of a collection, has its own. The adapter reads a
    response's body before any response hook runs, so the limit also holds for
    the body of a challenge that an authentication handler such as
    ``requests.auth.HTTPDigestAuth`` reads. Custom adapters and application
    hooks control their own work.
    """

    max_response_bytes: int | None = 16 * 1024 * 1024
    """Greatest size of a response body, decompressed, or ``None``.

    A larger body raises ``LimitExceeded`` as soon as it is seen to be too
    large, before it is decoded. The adapter reads each body before any
    response hook runs, so an authentication handler such as
    ``requests.auth.HTTPDigestAuth`` cannot read a challenge's body past the
    limit. A request sent with ``stream=True``, which reads its own body, is
    limited only in the bodies of redirects, of a challenge, and of a failed
    response, which eazyrest reads.
    """

    max_json_depth: int | None = 128
    """Greatest nesting depth of arrays and objects in a response's JSON.

    eazyrest's conversions recurse through nested values, so a deeper value
    raises ``LimitExceeded`` instead of exhausting Python's recursion limit.
    ``None`` removes the limit, but Python's decoder still fails for JSON
    nested more deeply than about a thousand levels.
    """

    max_pages: int | None = 1000
    """Greatest number of pages that one pagination traversal reads.

    ``follow_next_links()`` and ``follow_link_headers()`` count the first page
    too, and raise ``LimitExceeded`` rather than read more, so a server that
    links new pages without end cannot keep a client reading. ``None`` removes
    the limit.
    """

    max_items: int | None = 100_000
    """Greatest number of items that one pagination traversal yields.

    A traversal raises ``LimitExceeded`` rather than yield more. ``None``
    removes the limit.
    """

    trailing_slash: bool = True
    """Whether object URLs end with a slash, as in ``/users/1/``.

    Django REST Framework's routers use a trailing slash. Set this to
    ``False``, on an instance or in a subclass, for an API whose object URLs
    have none, as in ``/users/1``.
    """

    redirect_origins: Collection[str] = ()
    """Other origins to which a redirect may lead, as URLs.

    By default, a redirect must lead to the scheme, host, and port of the
    request it answers, so a redirect from ``http`` to ``https`` is refused
    too. Add an origin here, such as ``"https://cdn.example.com"``, on an
    instance or in a subclass, to follow redirects to it. Only the scheme,
    host, and port of each URL count. Every header and any body of the
    redirected request is sent to these origins.
    """

    default_write_mode: WriteMode
    """Default write mode inherited by newly created model instances."""

    _request_options: Mapping[str, Any] = MappingProxyType({})
    """Request options that every request of this client sends."""

    _request_scoped: bool = False
    """Whether this client is a request-scoped copy made by ``_scoped()``."""

    def __init__(
        self,
        url: str,
        proxy: str | None = None,
        default_write_mode: WriteMode = "lazy",
        verify: bool | str = True,
        **kwargs: Any,
    ):
        """Initialize an API client.

        Args:
            url: Base URL for the API.
            proxy: Optional proxy URL for both HTTP and HTTPS traffic.
            default_write_mode: Default model write mode used when a
                ``JSONObject`` instance does not override it explicitly.
            verify: Whether TLS certificates should be verified, or the path of
                a CA bundle to verify them with, as for requests. A path or
                ``False`` takes precedence over the ``REQUESTS_CA_BUNDLE`` and
                ``CURL_CA_BUNDLE`` environment variables, which requests
                otherwise uses. When ``False``, urllib3 warns with an
                ``InsecureRequestWarning`` for each request, unless a warnings
                filter ignores it.
            kwargs: If present, these are forwarded to a new
                ``requests.adapters.HTTPAdapter`` and mounted on the session.
                This allows for configuring connection pooling parameters,
                e.g., ``pool_connections`` and ``pool_maxsize``, and retries,
                with ``max_retries``. To change the request timeout, set the
                ``timeout`` attribute instead.
        """
        self.base_url = url
        self.default_write_mode = validate_write_mode(default_write_mode)

        self.reset_session(verify=verify, **kwargs)

        if proxy is not None:
            self.set_proxy(proxy)

    @property
    def cookies(self) -> requests.cookies.RequestsCookieJar:
        """The session's cookie jar.

        Assigning a jar replaces the session's cookies with a copy of its
        cookies. To add cookies, call ``api.cookies.update(jar)`` or
        ``api.cookies.set(name, value)``.
        """
        return self.session.cookies

    @cookies.setter
    def cookies(self, cookies: CookieJar) -> None:
        """Replace the session's cookies.

        Args:
            cookies: Cookie jar whose cookies the session uses instead of its
                own.
        """
        jar = requests.cookies.RequestsCookieJar()
        jar.update(cookies)
        self.session.cookies = jar

    def set_proxy(self, proxy: str) -> None:
        """Configure an HTTP/HTTPS proxy for subsequent requests.

        The proxy takes precedence over proxy environment variables such as
        ``HTTPS_PROXY``.

        Args:
            proxy: Proxy URL to apply to both ``http`` and ``https`` schemes.
        """
        proxies = {"http": proxy, "https": proxy}

        self.session.proxies.update(proxies)

    def reset_session(
        self, verify: bool | str | None = None, **kwargs: Any
    ) -> None:
        """Replace the underlying ``requests.Session``.

        The new session keeps the configuration of the session it replaces,
        including its headers, authentication, cookies, proxies, query
        parameters, hooks, and TLS settings. It does not keep connection pools
        or mounted adapters. The replaced session is closed.

        Args:
            verify: Whether TLS certificates should be verified, or the path of
                a CA bundle to verify them with. If omitted, the new session
                keeps the replaced session's setting, and a first session
                verifies certificates.
            kwargs: If present, these are forwarded to a new
                ``requests.adapters.HTTPAdapter`` and mounted on the session.
                This allows for configuring connection pooling parameters,
                e.g., ``pool_connections`` and ``pool_maxsize``.
        """
        session = requests.Session()

        old_session: requests.Session | None = getattr(self, "session", None)
        if old_session is not None:
            for name in _SESSION_SETTINGS:
                setattr(session, name, getattr(old_session, name))
            old_session.close()

        if verify is not None:
            session.verify = verify

        scheme = urlparse(self.base_url).scheme + "://"
        for prefix in ("http://", "https://"):
            session.adapters[prefix].close()
            session.mount(
                prefix,
                _HTTPAdapter(**kwargs) if prefix == scheme else _HTTPAdapter(),
            )

        self.session = session

    def close(self) -> None:
        """Close the underlying HTTP session."""
        self.session.close()

    def __enter__(self) -> Self:
        """Return this API client for use as a context manager.

        Returns:
            This API client.
        """
        return self

    def __exit__(self, *args: object) -> None:
        """Close the session when leaving a context-manager block.

        Args:
            args: Exception type, value, and traceback of the block, which are
                ignored.
        """
        self.close()

    def _check_response(self, resp: requests.Response) -> requests.Response:
        """Validate an HTTP response.

        Args:
            resp: Response object to validate.

        Returns:
            The same response when status code indicates success.

        Raises:
            requests.HTTPError: If the response status indicates failure.
        """
        if not _is_failure(resp.status_code):
            return resp

        self.raise_for_response(resp)
        return resp

    def raise_for_response(self, resp: requests.Response) -> None:
        """Raise an exception if ``resp`` represents a failed request.

        The default raises ``NotFound`` for a 404 and ``APIError`` for any
        other failure. Both are ``requests.HTTPError``, and they hold the
        response's JSON body as ``data``. Subclasses can override this hook to
        translate API-specific error payloads into exception types of their
        own. An override should raise a subclass of ``APIError``, and of
        ``NotFound`` for a 404, because eazyrest relies on ``NotFound`` being
        ``DoesNotExist`` when it loads an object that the server deleted, and
        callers catch ``APIError``.

        Args:
            resp: Response object to validate.

        Raises:
            NotFound: If the response status is 404.
            APIError: If the response status indicates another failure.
            LimitExceeded: If the error body exceeds ``max_json_depth``.
        """
        if not _is_failure(resp.status_code):
            return
        raise _api_error(resp, self.max_json_depth)

    def json(self, resp: requests.Response) -> Any:
        """Decode response JSON with this client's depth and number policy.

        Use this in collection, bulk-lookup, and error hooks that decode a
        response themselves. Request methods already enforce body limits.

        Args:
            resp: Response whose body has been read by this client.

        Returns:
            Decoded JSON value.

        Raises:
            ResponseShapeError: If the body is not valid JSON with finite,
                representable numbers.
            LimitExceeded: If JSON nesting exceeds ``max_json_depth``.
        """
        return _response_json(resp, self.max_json_depth)

    def resolve_url(self, uri: str) -> str:
        """Resolve a request URI against ``base_url``.

        Absolute URIs are returned unchanged. Relative paths are appended to
        ``base_url`` without letting leading slashes discard any existing path
        prefix on the base URL.

        Args:
            uri: Relative or absolute request URI.

        Returns:
            Fully resolved request URL.
        """
        parsed = urlparse(uri)
        if parsed.scheme or parsed.netloc:
            return uri

        return f"{self.base_url.rstrip('/')}/{uri.lstrip('/')}"

    def _scoped(self, options: Mapping[str, Any]) -> Self:
        """Return a copy of this client that sends ``options`` with requests.

        The copy shares this client's session, so its cookies, adapters, and
        configuration are the same. A request that the copy sends with its own
        options merges them over ``options``. The copy expects a final
        response to every request, so a redirect that is not followed raises.

        Args:
            options: Request options, as for ``requests.Session.request()``.

        Returns:
            The request-scoped copy.
        """
        scoped = copy.copy(self)
        scoped._request_options = _merge_request_options(
            self._request_options, options
        )
        scoped._request_scoped = True
        return scoped

    def _send(
        self,
        send: Callable[..., requests.Response],
        uri: str,
        *args: Any,
        **kwargs: Any,
    ) -> requests.Response:
        """Send a request with this client's defaults and check the response.

        Args:
            send: Session method that sends the request.
            uri: Relative or absolute request URI.
            *args: Positional arguments forwarded to ``send``.
            **kwargs: Keyword arguments forwarded to ``send``.

        Returns:
            Validated HTTP response.

        Raises:
            RedirectError: If a redirect leads to an origin that is not
                allowed, or if requests would follow a redirect without the
                request's body or with another method.
            requests.HTTPError: If ``raise_for_response()`` raises it for the
                response.
        """
        if self._request_options:
            kwargs = _merge_request_options(self._request_options, kwargs)
        kwargs.setdefault("timeout", self.timeout)
        # A request that streams reads its own body, but not the bodies of
        # redirects, which requests reads, or of a failure, which the error
        # reads, so those are bounded too.
        streamed = bool(kwargs.get("stream"))
        budget = _Budget(self.max_response_bytes, self.total_timeout, streamed)
        kwargs["stream"] = True
        kwargs["timeout"] = budget.timeout(kwargs["timeout"])
        if self.session.verify is not True:
            # With trust_env, requests replaces a verify setting of True with
            # the CA bundle that REQUESTS_CA_BUNDLE or CURL_CA_BUNDLE names,
            # and then prefers it over the session's setting. Only a setting
            # passed with the request is kept.
            kwargs.setdefault("verify", self.session.verify)
        if self.session.proxies:
            # requests lets proxy environment variables override session
            # proxies, but not proxies passed with the request. requests adds
            # environment proxies to the dict it receives, so pass a copy.
            kwargs.setdefault("proxies", dict(self.session.proxies))

        url = self.resolve_url(uri)
        kwargs["hooks"] = self._guard_redirects(
            url,
            kwargs.get("hooks"),
            budget,
            follow=bool(kwargs.get("allow_redirects", True)),
        )
        token = _request_budget.set(budget)
        try:
            try:
                resp = send(url, *args, **kwargs)
            except requests.RequestException as err:
                _drop_response_hooks(err.response, err.request)
                raise
            _drop_response_hooks(resp)
            self._check_redirects(resp)
            budget.check_time(resp)
            if not streamed or _is_failure(resp.status_code):
                budget.read_body(resp)
            resp = self._check_response(resp)
            if self._request_scoped:
                return _check_final_response(
                    resp, "the request has no response to read"
                )
            return resp
        finally:
            _request_budget.reset(token)

    def _guard_redirects(
        self,
        url: str,
        hooks: dict[str, Any] | None,
        budget: _Budget | None = None,
        follow: bool = True,
    ) -> dict[str, Any]:
        """Return request hooks that refuse unsafe redirects.

        Args:
            url: Absolute URL of the request.
            hooks: Hooks passed with the request, if any.
            budget: Limits of the request, which each redirect's body and the
                time to reach it must respect.
            follow: Whether requests follows redirects. If not, the hook
                refuses only a ``Location`` that requests cannot prepare
                ``Response.next`` for. The hook runs the preparation itself, so
                that the ``ValueError`` or ``UnicodeError`` that any of its
                steps raises, for instance on a port that is out of range,
                becomes a ``RedirectError``. Otherwise it refuses nothing,
                because no redirect is followed, and a caller that cannot
                accept an unfollowed redirect refuses it itself.

        Returns:
            ``hooks``, or the session's hooks if ``hooks`` has no response
            hooks, with a final response hook that checks each redirect before
            requests follows it.
        """
        allowed = {_origin(url), *map(_origin, self.redirect_origins)}

        def check_redirect(resp: requests.Response, **kwargs: Any) -> None:
            """Refuse a redirect that is not safe to follow.

            Args:
                resp: Response, which may be a redirect.
                kwargs: Arguments that requests passes to response hooks, which
                    are those it sends the request with. They are passed on to
                    prepare an unfollowed redirect's next request.

            Raises:
                RedirectError: If the ``Location`` of ``resp`` cannot be
                    parsed, or requests cannot prepare the next request for it
                    without following it, or if requests follows the redirect,
                    and it leads to an origin that is not allowed or requests
                    would follow it without the request's body. The response is
                    closed first.
            """
            try:
                target = self.session.get_redirect_target(resp)
                # requests follows no redirect to an empty target.
                if not target:
                    return
                destination = _redirect_destination(resp, target)
                origins = _redirect_origins(resp, target)
                if not follow:
                    # Session.send() prepares Response.next in this way when it
                    # does not follow redirects. Each step, such as the
                    # comparison of ports to decide whether to strip
                    # credentials, can raise a bare ValueError or UnicodeError
                    # that no parse of the target here anticipates. Run the
                    # preparation so that any such failure is refused here. It
                    # sends nothing, and the repeat reads no more of the body
                    # and closes the response, as requests does next.
                    next(
                        self.session.resolve_redirects(
                            resp,
                            resp.request,
                            yield_requests=True,
                            **kwargs,
                        ),
                        None,
                    )
            except ValueError as err:
                resp.close()
                raise RedirectError(
                    f"{resp.request.method} {resp.url} was redirected with "
                    f"status {resp.status_code} to a location that cannot be "
                    "parsed, so it was not followed",
                    response=resp,
                ) from err
            if not follow:
                return
            if origins <= allowed:
                _check_write_redirect(resp, target)
                if budget is not None:
                    # requests reads a redirect's body before following it.
                    budget.check_time(resp)
                    budget.read_body(resp)
                return

            resp.close()
            raise RedirectError(
                f"{resp.request.method} {resp.url} was redirected with status "
                f"{resp.status_code} to {_printable(destination)}, whose "
                "origin differs "
                "from the request's, so it was not followed. Add the origin "
                "to API.redirect_origins to follow it.",
                response=resp,
            )

        # requests uses a request's response hooks instead of the session's, so
        # keep the session's when the request has none. Run the guard last,
        # after any hook that might change the response.
        merged = dict(hooks or {})
        response = merged.get("response") or self.session.hooks.get("response")
        merged["response"] = [
            *_response_hooks(response),
            check_redirect,
        ]
        return merged

    @staticmethod
    def _check_redirects(resp: requests.Response) -> None:
        """Reject a response to a request that a redirect changed.

        The response hook refuses such a redirect before requests follows it.
        This check is a second line of defense, in case a hook was bypassed.

        Args:
            resp: Final response, after requests followed any redirects.

        Raises:
            RedirectError: If requests followed a redirect that dropped
                the original request's body or changed its method.
        """
        # requests also records in the history a 401 that an authentication
        # handler, such as HTTPDigestAuth, answers by resending the request
        # unchanged, which is not a redirect.
        hops = [hop for hop in resp.history if hop.is_redirect]
        if not hops:
            return

        redirect = hops[0]
        sent = (redirect.request.method or "").upper()
        dropped = redirect.request.body is not None and any(
            hop.status_code not in _BODY_PRESERVING_REDIRECTS for hop in hops
        )
        if dropped or resp.request.method != redirect.request.method:
            raise RedirectError(
                f"{sent} {redirect.url} was redirected with status "
                f"{redirect.status_code}, and requests resent it to "
                f"{resp.url} as {resp.request.method}, so the {sent} request "
                "was not performed",
                response=redirect,
            )

    def get(self, uri: str, *args: Any, **kwargs: Any) -> requests.Response:
        """Issue a ``GET`` request.

        Args:
            uri: Relative or absolute request URI.
            *args: Positional arguments forwarded to ``requests.Session.get``.
            **kwargs: Keyword arguments forwarded to ``requests.Session.get``.

        Returns:
            Validated HTTP response.
        """
        return self._send(self.session.get, uri, *args, **kwargs)

    def post(self, uri: str, *args: Any, **kwargs: Any) -> requests.Response:
        """Issue a ``POST`` request.

        A body passed as ``json=`` must already be JSON. Encode Python values,
        such as a ``datetime``, as fields encode them with ``encode_json()``.

        Args:
            uri: Relative or absolute request URI.
            *args: Positional arguments forwarded to ``requests.Session.post``.
            **kwargs: Keyword arguments forwarded to ``requests.Session.post``.

        Returns:
            Validated HTTP response.
        """
        return self._send(self.session.post, uri, *args, **kwargs)

    def patch(self, uri: str, *args: Any, **kwargs: Any) -> requests.Response:
        """Issue a ``PATCH`` request.

        A body passed as ``json=`` must already be JSON. Encode Python values,
        such as a ``datetime``, as fields encode them with ``encode_json()``.

        Args:
            uri: Relative or absolute request URI.
            *args: Positional arguments forwarded to
                ``requests.Session.patch``.
            **kwargs: Keyword arguments forwarded to
                ``requests.Session.patch``.

        Returns:
            Validated HTTP response.
        """
        return self._send(self.session.patch, uri, *args, **kwargs)

    def put(self, uri: str, *args: Any, **kwargs: Any) -> requests.Response:
        """Issue a ``PUT`` request.

        A body passed as ``json=`` must already be JSON. Encode Python values,
        such as a ``datetime``, as fields encode them with ``encode_json()``.

        Args:
            uri: Relative or absolute request URI.
            *args: Positional arguments forwarded to ``requests.Session.put``.
            **kwargs: Keyword arguments forwarded to ``requests.Session.put``.

        Returns:
            Validated HTTP response.
        """
        return self._send(self.session.put, uri, *args, **kwargs)

    def delete(self, uri: str, *args: Any, **kwargs: Any) -> requests.Response:
        """Issue a ``DELETE`` request.

        Args:
            uri: Relative or absolute request URI.
            *args: Positional arguments forwarded to
                ``requests.Session.delete``.
            **kwargs: Keyword arguments forwarded to
                ``requests.Session.delete``.

        Returns:
            Validated HTTP response.
        """
        return self._send(self.session.delete, uri, *args, **kwargs)
