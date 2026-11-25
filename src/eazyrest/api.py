"""HTTP API client primitives for eazyrest."""

from __future__ import annotations

import inspect
import time
from collections.abc import Callable, Collection, Mapping
from http.cookiejar import CookieJar
from types import MappingProxyType
from typing import Any, Literal, Self, TypeAlias

import requests
import requests.adapters
import requests.cookies
from requests.structures import CaseInsensitiveDict
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
    _DEFAULT_MAX_JSON_DEPTH,
    LimitExceeded,
    PaginationError,
    RedirectError,
    _api_error,
    _is_failure,
    _is_unfollowed_redirect,
    _PageLimit,
    _request_budget,
    _response_json,
    _response_name,
)
from ._protocols import Client, Response
from ._urls import (
    Source,
    _asked_text,
    _base_origins,
    _is_absolute,
    _Origin,
    _origin_matches,
    _prepare_redirect,
    _query_names,
    _read_url,
    _redirect_origin,
    _request_name,
    _ServerURL,
    _shown,
    _unreadable_error,
)
from .write_mode import WriteMode, validate_write_mode

__all__ = ["API"]

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


def _takes_kwargs(method: Any) -> bool:
    """Return whether a method has a ``**kwargs`` parameter.

    Args:
        method: Method to inspect, also a ``classmethod`` or ``staticmethod``
            object.

    Returns:
        Whether it takes any keyword argument. A callable whose signature
        cannot be read, such as a builtin, is taken to.
    """
    if isinstance(method, (classmethod, staticmethod)):
        method = method.__func__
    try:
        parameters = inspect.signature(method).parameters
    except (TypeError, ValueError):
        return True
    return any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in parameters.values()
    )


def _leaves_allowed_origins(
    prepared: tuple[_ServerURL, ...],
    allowed: Collection[_Origin],
) -> bool:
    """Return whether any reading of a destination names an origin not allowed.

    A page's first destination and each redirect are checked by this.

    Args:
        prepared: The readings of the destination, which are the two that
            ``_prepare_redirect()`` returned for a redirect, and the one that
            ``_read_url()`` returned for a page's URL.
        allowed: The origins that the destination may have.

    Returns:
        Whether any reading's origin is not in ``allowed``. A reading with no
        origin, as for a scheme other than ``http`` and ``https``, leaves the
        allowed origins.
    """
    return not all(
        _origin_matches(reading.origin, allowed) for reading in prepared
    )


def _permitted_origins(
    base_url: str,
    redirect_origins: tuple[str, ...] | list[str] | frozenset[str],
) -> set[_Origin]:
    """Return the origins that a request starting at ``base_url`` may reach.

    Every request reads ``API.redirect_origins`` here and nowhere else.

    Args:
        base_url: Absolute URL of the request, or an API's base URL.
        redirect_origins: Further origins that the API allows, as URLs.

    Returns:
        The origin of ``base_url``, if its scheme is ``http`` or ``https``, and
        the origins of ``redirect_origins``.

    Raises:
        TypeError: If ``redirect_origins`` is not a tuple, a list, or a
            frozenset, or holds anything but strings. A string or bytes would
            be read as a collection of its characters or its integers, and a
            type checker takes a string for a collection of strings.
        ValueError: If a string in ``redirect_origins`` is no absolute ``http``
            or ``https`` URL with a host.
    """
    if not isinstance(redirect_origins, (tuple, list, frozenset)):
        raise TypeError(
            "API.redirect_origins must be a collection of origins, a tuple, "
            "list, or frozenset of URLs such as "
            '["https://cdn.example.com"], not a '
            f"{type(redirect_origins).__name__}"
        )
    for origin in redirect_origins:
        if not isinstance(origin, str):
            raise TypeError(
                "API.redirect_origins must be a collection of origins, such "
                f'as "https://cdn.example.com", but it holds a '
                f"{type(origin).__name__}"
            )
    return {
        *_base_origins(base_url),
        *(
            _redirect_origin(
                None if isinstance(redirect_origins, frozenset) else index,
                entry,
            )
            for index, entry in enumerate(redirect_origins)
        ),
    }


_UNPARSABLE_PAGE_URL = "A page URL cannot be parsed, so nothing was sent."


def _page_url_subject(
    prepared: _ServerURL | None,
    label: str,
    source: Source,
    page: str | None = None,
) -> str:
    """Name a page request in a message.

    Args:
        prepared: The URL as requests prepares it, or ``None`` if requests
            cannot.
        label: What the message calls a page URL that the caller gave, such as
            ``"Page URL"``.
        source: Where the URL came from. A link that a response supplied is
            ``Source.SERVER``, and the caller's URL for the first page is
            ``Source.CALLER``.
        page: Which page a link names, as ``"page 3"``, or ``None`` if the
            position is not known.

    Returns:
        For a server's link, the request for the page by its position, with the
        link's origin, as ``The request for page 3 of 'https://example.com'``.
        Otherwise ``label`` with what ``_shown()`` shows of the URL.
    """
    shown = _shown(prepared, source)
    if source is Source.SERVER:
        return f"The request for {page or 'a page'} of {shown}"
    return f"{label} {shown}"


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
    combined per event, with ``base``'s first. Both are merged whenever
    ``override`` holds a mapping, an empty one included. A value of ``None``
    means the option is absent, so ``override``'s ``None`` keeps ``base``'s
    value, as requests does with the options of a session. For any other
    option, ``override``'s value wins. A header whose value is ``None`` is
    kept, so requests still removes that header from the session's.

    Args:
        base: Options that apply unless ``override`` sets them.
        override: Options that take precedence.

    Returns:
        The merged options, as a new dict.
    """
    merged = {**base, **override}
    for name in ("headers", "hooks"):
        if override.get(name) is None and base.get(name) is not None:
            merged[name] = base[name]
    if base.get("headers") is not None and override.get("headers") is not None:
        headers: CaseInsensitiveDict[Any] = CaseInsensitiveDict(
            base["headers"]
        )
        headers.update(override["headers"])
        merged["headers"] = headers
    if base.get("hooks") is not None and override.get("hooks") is not None:
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
    if _is_unfollowed_redirect(resp.status_code):
        resp.close()
        raise RedirectError(
            f"{_response_name(resp)} returned status {resp.status_code}, so "
            f"{consequence}",
            response=resp,
        )
    return resp


def _record_asked_url(
    resp: requests.Response, asked: str, page: str | None = None
) -> None:
    """Record the URL that was asked for on a response and its history.

    Messages about the response then name the request by this URL, wherever
    they are raised, and not by a URL that a redirect or a hook chose.

    Args:
        resp: The response that a request returned.
        asked: What a message shows of the URL that the request was asked to
            send, as ``_shown()`` returns it unquoted.
        page: Which page of a collection a link named, as ``"page 3"``, or
            ``None``.
    """
    for hop in (*resp.history, resp):
        hop._asked_url = asked  # type: ignore[attr-defined]
        hop._asked_page = page  # type: ignore[attr-defined]


# The methods whose requests follow redirects. A redirect answers any other
# method without the effect that the request asked for, or with an effect that
# the server performed elsewhere, so a write never follows one.
_FOLLOWING_METHODS = frozenset({"GET"})


def _carries_body(kwargs: dict[str, Any]) -> bool:
    """Return whether requests sends a body with these request arguments.

    requests sends a body for any ``json`` that is not ``None``, including an
    empty one, and for ``data`` or ``files`` that is not empty. An empty
    mapping, string, or list is no body, but a stream is one.

    Args:
        kwargs: Keyword arguments of the request, after the client's options
            were merged into them.

    Returns:
        Whether the request carries a body.
    """
    return (
        kwargs.get("json") is not None
        or bool(kwargs.get("data"))
        or bool(kwargs.get("files"))
    )


def _redirected_write(
    resp: requests.Response, name: str, what: str = "a write"
) -> RedirectError:
    """Describe a request that the server answered with a redirect.

    Args:
        resp: The 3xx response. It is closed, and its requests lose their
            response hooks, so that the error pickles.
        name: The request's method and URL, as for ``_Budget.request``.
        what: The kind of request, which the message says does not follow a
            redirect.

    Returns:
        The error to raise. The message does not quote the ``Location``, which
        stays on ``resp``.
    """
    _drop_response_hooks(resp)
    resp.close()
    return RedirectError(
        f"{name} was answered with status {resp.status_code}, a redirect, "
        f"which {what} does not follow, so it was not resent. The response "
        "of this error holds the Location header. Send the request to the "
        "URL that the server expects, for example by setting "
        "API.trailing_slash.",
        response=resp,
    )


# Bytes to read from a response body at a time. read1() returns as soon as
# any bytes arrive, so a slow body cannot hold a read open past the deadline
# for longer than one socket timeout.
_READ_SIZE = 64 * 1024


def _unquotable_redirect(
    resp: requests.Response, problem: str, name: str
) -> RedirectError:
    """Describe a redirect whose ``Location`` the message cannot quote.

    The message quotes no part of the header. Where a parser rejects the
    header, no rule says which of its text is the host and which is a password,
    so any part of it might be either. The header stays on ``resp``.

    Args:
        resp: Response that holds the ``Location``.
        problem: What is wrong with the location, completing "to a location
            that".
        name: The request's method and URL, as for ``_Budget.request``.

    Returns:
        The error to raise. ``resp`` is closed first.
    """
    resp.close()
    return RedirectError(
        f"{name} was redirected with status {resp.status_code} to a location "
        f"that {problem}, so it was not followed. The response of this error "
        "holds the Location header.",
        response=resp,
    )


def _unpreparable_redirect(
    resp: requests.Response, name: str
) -> RedirectError:
    """Describe an unfollowed redirect that requests cannot prepare.

    The message quotes no part of the ``Location``. A target that requests
    cannot prepare has no URL that the message could quote without naming a
    password or a host that a reader would take for the destination, whatever a
    parser makes of its text.

    Args:
        resp: Final response, a redirect that requests does not follow.
        name: The request's method and URL, as for ``_Budget.request``.

    Returns:
        The error to raise instead of the one that requests raised while it
        prepared ``Response.next``. ``resp`` is closed first, and its requests
        lose their response hooks, so that the error pickles.
    """
    _drop_response_hooks(resp)
    return _unquotable_redirect(resp, "requests cannot prepare", name)


class _Budget:
    """The limits of one request: its body bytes and its total time."""

    def __init__(
        self,
        max_bytes: int | None,
        total_timeout: float | None,
        streamed: bool = False,
        request: str | None = None,
    ) -> None:
        """Start the budget of a request.

        Args:
            max_bytes: Greatest decoded size of all the bodies that the request
                keeps, those of its redirects, of the challenges that an
                authentication handler answers, and of its final response, or
                ``None`` for no limit.
            total_timeout: Seconds that the request may take, including
                redirects and reading bodies, or ``None`` for no limit.
            streamed: Whether the caller reads the body of a successful final
                response, so that the adapter leaves it unread.
            request: Name of the request, as ``"GET http://host/path?..."``,
                for the URL as the caller gave it, resolved against the base
                URL, and shown as ``_shown()`` shows a caller's URL. An error
                names the request by it, and never by a URL that a response
                supplied. ``None`` if unknown.
        """
        self.max_bytes = max_bytes
        self.request = request
        self.held = 0
        """Bytes of the bodies that the request holds so far."""
        self.total_timeout = total_timeout
        self.streamed = streamed
        self.unfollowed: requests.Response | None = None
        """Final response that is a redirect that requests does not follow."""
        self.deadline = (
            None if total_timeout is None else time.monotonic() + total_timeout
        )

    def remaining(self) -> float | None:
        """Return the time that remains of the request.

        This is the one place that computes it, as the deadline less the clock.
        A caller applies its own floor, since a socket timeout of 0 does not
        wait at all while a wait of 0 is no wait.

        Returns:
            The seconds until the deadline, which is not above 0 once it has
            passed, or ``None`` if the request has no deadline.
        """
        if self.deadline is None:
            return None
        return self.deadline - time.monotonic()

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
        remaining = self.remaining()
        if remaining is None:
            return timeout
        remaining = max(remaining, 0.001)
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

    def _name(self, resp: requests.Response) -> str:
        """Return the method and URL that an error names for a response.

        Args:
            resp: Response being handled.

        Returns:
            The request's method and URL, as for ``request``, or, for a budget
            without one, what ``_response_name()`` gives for ``resp``.
        """
        if self.request is not None:
            return self.request
        return _response_name(resp)

    def check_time(self, resp: requests.Response | None = None) -> None:
        """Raise if the request has run out of time.

        Args:
            resp: Response being handled, if any, which is closed first. An
                error names the request of the response, or else the request
                that the budget bounds.

        Raises:
            LimitExceeded: If the deadline has passed.
        """
        remaining = self.remaining()
        if remaining is not None and remaining <= 0:
            where = self.request or "The request"
            if resp is not None:
                resp.close()
                where = self._name(resp)
            raise LimitExceeded(
                "total_timeout",
                f"{where} took at least {self.total_timeout} seconds",
            )

    def read_body(self, resp: requests.Response, held: bool = True) -> None:
        """Read a streamed response's body within the budget.

        The body is read into the response, so ``resp.content`` and
        ``resp.json()`` use it as if requests had read it. A request keeps the
        body of each redirect in ``resp.history``, so the bodies of one request
        count against one size limit. The length that a response declares is
        not checked for a response that has no body, a 1xx, 204, or 304
        response or the response to a ``HEAD`` request, which may declare the
        length of the entity that it describes. A session hook can send a
        ``HEAD`` request while a request of the API is in flight.

        Args:
            resp: Response sent with ``stream=True``, whose body is unread.
            held: Whether the request keeps the body, so that it counts against
                the size limit of the bodies the request holds. A response that
                a retry discards does not.

        Raises:
            LimitExceeded: If the body, with the bodies read before it, is
                larger than the limit, or reading it passes the deadline. The
                response is closed first.
        """
        # requests marks a body that it has not read with False.
        if getattr(resp, "_content", None) is not False:
            return

        allowed = (
            None
            if self.max_bytes is None
            else self.max_bytes - (self.held if held else 0)
        )
        # A response without a body, by its status or its request, may still
        # declare the length of the entity that it describes, as a 304 does.
        # urllib3 reads no body for it, whatever it declares.
        declared = (
            ""
            if resp.status_code < 200
            or resp.status_code in (204, 304)
            or resp.request.method == "HEAD"
            else resp.headers.get("Content-Length", "")
        )
        # A length longer than the limit's has more digits than int() reads
        # quickly, and str.isdigit() also accepts digits that int() refuses.
        if (
            allowed is not None
            and declared.isascii()
            and declared.isdigit()
            and (len(declared) > len(str(allowed)) or int(declared) > allowed)
        ):
            self._too_large(resp, held)

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
                if allowed is not None and size > allowed:
                    self._too_large(resp, held)
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

        if held:
            self.held += size
        resp._content = b"".join(chunks)
        resp._content_consumed = True  # type: ignore[attr-defined]
        release = getattr(raw, "release_conn", None)
        if release is not None:
            release()

    def _too_large(self, resp: requests.Response, held: bool) -> None:
        """Close a response whose body is too large, and raise.

        Args:
            resp: Response whose body exceeds the limit.
            held: Whether the request keeps the body, so that the bodies read
                before it count.

        Raises:
            LimitExceeded: Always.
        """
        resp.close()
        where = self._name(resp)
        if held and self.held:
            raise LimitExceeded(
                "max_response_bytes",
                f"The response to {where}, with the {self.held} bytes of "
                f"bodies that the request read before it, is larger than "
                f"{self.max_bytes} bytes",
            )
        raise LimitExceeded(
            "max_response_bytes",
            f"The response to {where} is larger than {self.max_bytes} bytes",
        )


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
                wrapped.url = ""
                wrapped.request = requests.PreparedRequest()
                wrapped.request.method = method
            budget.check_time(wrapped)

        try:
            retry = super().increment(
                method, url, response, error, _pool, _stacktrace
            )
        except MaxRetryError:
            # With raise_on_status=False, urllib3 returns the unread response
            # to Requests, and the adapter reads it under the same budget.
            if (
                budget is not None
                and wrapped is not None
                and self.raise_on_status
            ):
                budget.read_body(wrapped, held=False)
            raise
        if budget is not None and wrapped is not None:
            budget.read_body(wrapped, held=False)
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
        remaining = max(budget.remaining() or 0.0, 0.0)
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
        budget = _request_budget.get()
        resp = super().send(request, stream, timeout, verify, cert, proxies)
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

    A write or any request with a body follows no redirect. A request with any
    method but ``GET``, or with a non-empty ``data`` or ``files`` or a ``json``
    that is not ``None``, is sent with ``allow_redirects=False``, whatever the
    caller passes, and a 3xx response to it raises ``RedirectError``, whose
    ``response`` holds the ``Location``. Django's ``APPEND_SLASH`` answers a
    URL that lacks its trailing slash with such a redirect.

    A ``GET`` without a body follows a redirect only to the origin of the
    request it answers, or to an origin in ``redirect_origins``. Requests drops
    an ``Authorization`` header when a redirect leaves the host, but it sends
    every other header, such as an API key or a CSRF token. A redirect to
    another origin raises ``RedirectError`` before anything is sent there.
    ``RedirectError`` is a ``requests.HTTPError``, whose ``response`` is the
    redirect. A raw ``GET`` sent with ``allow_redirects=False`` follows
    nothing, so it returns a redirect as it is, without these checks, unless
    requests raises ``ValueError`` or ``UnicodeError`` while it prepares
    ``Response.next`` for it, which raises ``RedirectError``. Any other
    exception that requests raises there, such as ``TooManyRedirects`` when
    ``Session.max_redirects`` is 0, comes through unchanged.

    The class has ``get()``, ``post()``, ``patch()``, ``put()``, and
    ``delete()``, and sends no ``HEAD`` request. A ``HEAD`` that is sent
    through ``session.head()`` skips these checks, and follows a redirect to
    another origin with the session's headers if it is told to follow.
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
    """Greatest size of the response bodies of one request, or ``None``.

    The limit covers all the bodies that one request reads, decompressed, those
    of the redirects that requests follows, of the 401 challenges that an
    authentication handler answers, and the final one, which requests keeps in
    ``resp.history``, so one request never holds more than the limit in total.
    A challenge and a final body that together exceed it raise
    ``LimitExceeded``, though each is within it alone. A larger body raises
    ``LimitExceeded`` as soon as it is seen to be too large, before it is
    decoded. The adapter reads each body before any response hook runs, so an
    authentication handler such as ``requests.auth.HTTPDigestAuth`` cannot read
    a challenge's body past the limit. A request sent with ``stream=True``,
    which reads its own body, is limited only in the bodies of redirects,
    followed or not, of a challenge, and of a failed response, which eazyrest
    reads.
    """

    max_json_depth: int | None = _DEFAULT_MAX_JSON_DEPTH
    """Greatest nesting depth of arrays and objects in a response's JSON.

    eazyrest's conversions recurse through nested values, so a deeper value
    raises ``LimitExceeded`` instead of exhausting Python's recursion limit.
    ``None`` removes the limit, so JSON as deep as Python's decoder accepts
    reaches the conversions, whose recursion it can exceed.
    """

    max_pages: int | None = 1000
    """Greatest number of pages that one pagination traversal reads.

    ``filter()``, ``all()``, ``follow_next_links()``, and
    ``follow_link_headers()`` count the first page too, and raise
    ``LimitExceeded`` rather than read more, so a server that links new pages
    without end cannot keep a client reading. It counts the pages that
    eazyrest requests. A hook that requests its own pages is not bounded by
    it. ``None`` removes the limit.
    """

    max_items: int | None = 100_000
    """Greatest number of items that one pagination traversal yields.

    A traversal raises ``LimitExceeded`` rather than yield more, whichever
    hook gave the items. ``None`` removes the limit.
    """

    list_lookup_style: Literal["comma", "repeat"] = "comma"
    """How a lookup that takes several values sends them.

    A lookup whose name ends in ``__in`` or ``__range`` sends its values as one
    comma-separated parameter, ``?id__in=1,2``, which django-filter reads, or,
    with ``"repeat"``, as the parameter repeated, ``?id__in=1&id__in=2``, which
    ``QueryDict.getlist()`` reads. Set it on an instance or in a subclass. A
    field's own parameter, such as ``tag=[a, b]``, is always repeated. Any
    other value raises ``ValueError`` when a query is encoded.
    """

    trailing_slash: bool = True
    """Whether object URLs end with a slash, as in ``/users/1/``.

    Django REST Framework's routers use a trailing slash. Set this to
    ``False``, on an instance or in a subclass, for an API whose object URLs
    have none, as in ``/users/1``.
    """

    redirect_origins: tuple[str, ...] | list[str] | frozenset[str] = ()
    """Other origins to which a redirect may lead, as URLs.

    By default, a redirect must lead to the scheme, host, and port of the
    request it answers, so a redirect from ``http`` to ``https`` is refused
    too. Add an origin here, such as ``"https://cdn.example.com"``, on an
    instance or in a subclass, to follow redirects to it. Only the scheme,
    host, and port of each URL count. Every header and any body of the
    redirected request is sent to these origins. Give a tuple, a list, or a
    frozenset of absolute ``http`` or ``https`` URLs, each with a host. Any
    other value, such as a single string, bytes, a set, or a list that holds
    anything but strings, raises ``TypeError`` before a request is sent. A
    string that is no such URL, as ``"files.example.com"``, ``"https://"``, or
    ``"ftp://files.example.com"``, raises ``ValueError`` naming the attribute
    and the entry's position, if it is in a tuple or a list, and saying which
    check failed. The message does not quote the entry.
    """

    default_write_mode: WriteMode
    """Default write mode inherited by newly created model instances."""

    _request_options: Mapping[str, Any] = MappingProxyType({})
    """Request options that every request sends, which an API has none of.

    A scoped view of the API has its own, so a helper that sends a request
    through either reads the same attribute.
    """

    def __init_subclass__(cls, **kwargs: Any) -> None:
        """Check that an override of ``raise_for_response()`` takes kwargs.

        Args:
            **kwargs: Keyword arguments for ``object.__init_subclass__()``.

        Raises:
            TypeError: If the class overrides ``raise_for_response()``, also
                through a mixin, with a method that takes no ``**kwargs``,
                which later releases may pass.
        """
        super().__init_subclass__(**kwargs)
        owner = next(
            base for base in cls.__mro__ if "raise_for_response" in vars(base)
        )
        if owner is not API and not _takes_kwargs(
            vars(owner)["raise_for_response"]
        ):
            raise TypeError(
                f"{owner.__name__}.raise_for_response() must accept "
                "**kwargs, which later releases may pass to it"
            )

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

        Raises:
            requests.RequestException: An ``InvalidURL`` if ``urlparse()``
                rejects ``url``. The message quotes none of it.
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
        or mounted adapters. The replaced session is closed. A first session
        sends ``Accept: application/json``, which a request may override.

        Args:
            verify: Whether TLS certificates should be verified, or the path of
                a CA bundle to verify them with. If omitted, the new session
                keeps the replaced session's setting, and a first session
                verifies certificates.
            kwargs: If present, these are forwarded to a new
                ``requests.adapters.HTTPAdapter`` and mounted on the session.
                This allows for configuring connection pooling parameters,
                e.g., ``pool_connections`` and ``pool_maxsize``.

        Raises:
            requests.RequestException: If requests cannot prepare
                ``base_url``, as ``MissingSchema`` or ``InvalidURL``. The
                message quotes none of it.
        """
        session = requests.Session()

        old_session: requests.Session | None = getattr(self, "session", None)
        if old_session is not None:
            for name in _SESSION_SETTINGS:
                setattr(session, name, getattr(old_session, name))
            old_session.close()
        else:
            # A server that negotiates content, such as Django REST
            # Framework's browsable API, would otherwise answer with HTML.
            session.headers["Accept"] = "application/json"

        if verify is not None:
            session.verify = verify

        reading = _read_url(self.base_url)
        if reading is None:
            raise _unreadable_error(self.base_url)
        scheme = reading.scheme + "://"
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

    def raise_for_response(self, resp: Response, **kwargs: Any) -> None:
        """Raise an exception if ``resp`` represents a failed request.

        The default raises ``NotFound`` for a 404 and ``APIError`` for any
        other failure. Both are ``requests.HTTPError``, and they hold the
        response's JSON body as ``data``. Subclasses can override this hook to
        translate API-specific error payloads into exception types of their
        own. An override should raise a subclass of ``APIError``, and of
        ``NotFound`` for a 404, because eazyrest relies on ``NotFound`` being
        ``DoesNotExist`` when it loads an object that the server deleted, and
        callers catch ``APIError``.

        An override must accept ``**kwargs``, which later releases may pass,
        or the subclass raises ``TypeError`` when it is defined.

        Args:
            resp: Response object to validate.
            **kwargs: Keyword arguments that later releases may pass, which
                an override must accept.

        Raises:
            NotFound: If the response status is 404.
            APIError: If the response status indicates another failure.
            LimitExceeded: If the error body exceeds ``max_json_depth``.
        """
        if not _is_failure(resp.status_code):
            return
        raise _api_error(resp, self.max_json_depth)

    def json(self, resp: Response) -> Any:
        """Decode response JSON with this client's depth and number policy.

        Use this in collection, bulk-lookup, and error hooks that decode a
        response themselves. Request methods already enforce body limits.

        Args:
            resp: Response whose body has been read by this client.

        Returns:
            Decoded JSON value.

        Raises:
            ResponseShapeError: If the body is not valid JSON with finite,
                representable numbers, or does not decode as UTF-8 or as the
                charset that the response declares.
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

        Raises:
            requests.RequestException: An ``InvalidURL`` if ``urlparse()``
                rejects the URI, as for a host that Unicode normalization
                changes. The message quotes none of the URI.
        """
        if _is_absolute(uri):
            return uri

        return f"{self.base_url.rstrip('/')}/{uri.lstrip('/')}"

    def _read_server_url(self, url: str) -> _ServerURL | None:
        """Read a URL that a server supplied, as this session will send it.

        Pagination checks the origin of a next-page link and names it in a
        message, so it needs the reading that requests will act on, which
        depends on this API's session, and not the text of the link.

        Args:
            url: Absolute URL, such as a link to another page.

        Returns:
            The reading that ``_read_url()`` gives for this API's session, or
            ``None`` if requests cannot prepare the URL.
        """
        return _read_url(url, self.session)

    def _repeated_params(self, url: str) -> dict[str, None]:
        """Name the session parameters that the query of ``url`` already has.

        A server builds a next-page link from the URL that it received, so the
        link carries the session's parameters, which requests would add again
        to each page. Pagination passes this result as ``params=``, where a
        parameter given as ``None`` is left out when requests merges them.

        Args:
            url: Absolute URL of a page.

        Returns:
            A mapping from each session parameter that the query of ``url``
            names to ``None``. It is empty if the session's parameters are not
            a mapping.
        """
        session_params = self.session.params
        if not isinstance(session_params, Mapping):
            return {}
        query = _query_names(url)
        return {name: None for name in session_params if name in query}

    def _scoped(self, options: Mapping[str, Any]) -> _ScopedAPI:
        """Return a view of this client that sends ``options`` with requests.

        Args:
            options: Request options, as for ``requests.Session.request()``.

        Returns:
            The view, which sends every request through this client.
        """
        return _ScopedAPI(self, dict(options))

    def _send(
        self,
        method: str,
        send: Callable[..., requests.Response],
        uri: str,
        *args: Any,
        **kwargs: Any,
    ) -> requests.Response:
        """Send a request with this client's defaults and check the response.

        Args:
            method: The request's method, as ``"GET"``, which an error names.
            send: Session method that sends the request with that method.
            uri: Relative or absolute request URI.
            *args: Positional arguments forwarded to ``send``.
            **kwargs: Keyword arguments forwarded to ``send``.

        Returns:
            Validated HTTP response.

        Raises:
            RedirectError: If the request is a write or carries a body and the
                response is a redirect, or a redirect leads to an origin that
                is not allowed, or the final response is a redirect that is not
                followed and requests raises ``ValueError`` or ``UnicodeError``
                while it prepares the next request for it.
            requests.HTTPError: If ``raise_for_response()`` raises it for the
                response.
            ValueError: If requests raises it, unless the final response is a
                redirect that requests does not follow.
            requests.RequestException: A ``MissingSchema``, ``InvalidSchema``,
                or ``InvalidURL`` if requests cannot prepare the URL or has no
                adapter for it, so that nothing was sent. Its message quotes
                none of the URL, and it has no ``__cause__`` or
                ``__context__``.
            TypeError: If ``redirect_origins`` is not a tuple, a list, or a
                frozenset of strings. The request is not sent.
            ValueError: If a string in ``redirect_origins`` is no absolute
                ``http`` or ``https`` URL with a host. The request is not sent.
        """
        return self._transmit(method, send, uri, args, kwargs, None)

    def _send_page(
        self,
        url: str,
        options: Mapping[str, Any],
        *,
        from_server: bool,
        initial: Collection[_Origin] | None,
        redirects: Collection[_Origin] | None,
        page: str | None = None,
    ) -> requests.Response:
        """Send a page request with this client's defaults and policy.

        Pagination calls this instead of ``get()``, so an override of ``get()``
        does not see a page request. The request is sent through the session
        with the options that a scoped view adds, and the session's hooks, its
        authentication, and this client's limits apply to it as to any request.

        This method resolves ``url``, prepares it as the session will send it,
        and checks the destination and every redirect against the same origins.

        Args:
            url: Relative or absolute URL of the page.
            options: Request options to send, as ``requests.Session.get()``
                takes them, which a query's scoped view holds.
            from_server: Whether a response supplied ``url``, as a next-page
                link, so that a message quotes its origin alone and names the
                request by its position.
            initial: The origins that the page's URL may have, or ``None`` if
                it may have any.
            redirects: The origins that a redirect of the request may reach, or
                ``None`` to apply the rule of any other request: the request's
                own origin and ``redirect_origins``.
            page: Which page the link names, as ``"page 3"``, or ``None`` if
                the position is not known.

        Returns:
            The page's response.

        Raises:
            PaginationError: If the URL's origin is not in ``initial``, or a
                redirect leaves ``redirects``, or requests cannot prepare the
                URL or has no adapter to send it, so that nothing was sent. The
                message of the last two quotes none of the link.
            RedirectError: If a redirect is refused, as for any request, or the
                response is a redirect that was not followed.
            requests.RequestException: If the request fails, as for any
                request.
            TypeError: If ``redirect_origins`` is not a tuple, a list, or a
                frozenset of strings. The request is not sent.
            ValueError: If a string in ``redirect_origins`` is no absolute
                ``http`` or ``https`` URL with a host. The request is not sent.
        """
        url = self.resolve_url(url)
        # Prepare the link as requests will, before anything is sent, so that
        # an error that requests raises for the link, which can quote it, is
        # not the request's. An error from the request itself, or from a
        # session hook, keeps its own type.
        prepared = self._check_page_destination(
            url, initial, from_server, page
        )
        if prepared is None or not prepared.sendable:
            raise PaginationError(
                "A page URL cannot be requested, so nothing was sent."
            )
        source = Source.SERVER if from_server else Source.CALLER
        subject = _page_url_subject(prepared, "Page request", source, page)

        def refusal() -> Exception:
            """Return the error for a redirect to another origin.

            Returns:
                An error whose message names the page request and quotes no
                part of the redirect's target.
            """
            return PaginationError(
                f"{subject} was redirected to a different origin "
                f"from the API's base URL {self._config_url(self.base_url)}. "
                "Pass allow_other_origins=True to follow it."
            )

        limit = None if redirects is None else _PageLimit(redirects, refusal)
        kwargs = _merge_request_options(
            options, {"params": self._repeated_params(url)}
        )
        return self._transmit(
            "GET",
            self.session.get,
            url,
            (),
            kwargs,
            limit,
            source=source,
            page=page,
        )

    def _config_url(self, url: str) -> str:
        """Return what a message quotes of a configured URL.

        Args:
            url: A URL that the configuration holds, such as ``base_url``.

        Returns:
            The URL's origin and path, quoted, as ``_shown()`` returns it.
        """
        return _shown(_read_url(url, self.session), Source.CONFIG)

    def _check_page_destination(
        self,
        url: str,
        initial: Collection[_Origin] | None,
        from_server: bool,
        page: str | None = None,
    ) -> _ServerURL | None:
        """Check the origin of a page's URL as a redirect's is checked.

        The URL is read once, as requests prepares it, and the origin and the
        message both come from that reading.

        Args:
            url: Absolute URL of the page.
            initial: The origins that the URL may have, or ``None`` if it may
                have any.
            from_server: Whether a response supplied ``url``, as a next-page
                link.
            page: Which page the link names, as ``"page 3"``, or ``None``.

        Returns:
            The reading of ``url``, or ``None`` if requests cannot prepare it
            and ``initial`` is ``None``.

        Raises:
            PaginationError: If ``initial`` is not ``None`` and requests cannot
                prepare ``url``, or its origin is not in ``initial``. The
                message of the first quotes none of the link and attaches no
                error, as for ``_resolve()``. The message of the second names
                the page by its position and the link's origin if
                ``from_server``, and otherwise quotes the URL as a caller's.
        """
        prepared = self._read_server_url(url)
        if initial is None:
            return prepared
        if prepared is None:
            raise PaginationError(_UNPARSABLE_PAGE_URL)
        if _leaves_allowed_origins((prepared,), initial):
            source = Source.SERVER if from_server else Source.CALLER
            subject = _page_url_subject(prepared, "Page URL", source, page)
            raise PaginationError(
                f"{subject} has a different origin from the API's base URL "
                f"{self._config_url(self.base_url)}. "
                "Pass allow_other_origins=True to follow it."
            )
        return prepared

    def _transmit(
        self,
        method: str,
        send: Callable[..., requests.Response],
        uri: str,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
        limit: _PageLimit | None,
        *,
        source: Source = Source.CALLER,
        page: str | None = None,
    ) -> requests.Response:
        """Send a request, checking its redirects, and check the response.

        Args:
            method: The request's method, as ``"GET"``, which an error names.
            send: Session method that sends the request with that method.
            uri: Relative or absolute request URI.
            args: Positional arguments forwarded to ``send``.
            kwargs: Keyword arguments forwarded to ``send``.
            limit: The limit of a page request on the origins of its redirects,
                or ``None`` for the rule of any other request.
            source: Where the URL came from. A request for a link that a server
                supplied is ``Source.SERVER``.
            page: Which page of a collection a link names, as ``"page 3"``, or
                ``None``.

        Returns:
            Validated HTTP response.

        Raises:
            RedirectError: If the request is a write or carries a body and the
                response is a redirect, or a redirect leads to an origin that
                is not allowed, or the final response is a redirect that is not
                followed and requests raises ``ValueError`` or ``UnicodeError``
                while it prepares the next request for it.
            requests.HTTPError: If ``raise_for_response()`` raises it for the
                response.
            ValueError: If requests raises it, unless the final response is a
                redirect that requests does not follow.
        """
        kwargs.setdefault("timeout", self.timeout)
        # A request that streams reads its own body, but not the bodies of
        # redirects, which requests reads, or of a failure, which the error
        # reads, so those are bounded too.
        streamed = bool(kwargs.get("stream"))
        url = self.resolve_url(uri)
        # Prepare the URL as requests will, before anything is sent, so that an
        # error that requests raises for it, which can quote it, is not the
        # request's. An error from the request itself keeps its own type.
        reading = _read_url(url, self.session)
        if reading is None or not reading.sendable:
            raise _unreadable_error(url, self.session)
        asked = _shown(reading, source, quoted=False)
        name = _request_name(method, asked, page)
        budget = _Budget(
            self.max_response_bytes, self.total_timeout, streamed, name
        )
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

        write = method.upper() not in _FOLLOWING_METHODS
        # requests resends a body only for a 307 or 308, and drops it for a
        # 301, 302, or 303 even from a GET, so a request that carries one
        # follows no redirect, as a write does.
        no_follow = write or _carries_body(kwargs)
        what = "a write" if write else "a request with a body"
        if no_follow:
            # Such a request follows no redirect, whatever the caller passed.
            kwargs["allow_redirects"] = False
        kwargs["hooks"] = self._guard_redirects(
            url,
            kwargs.get("hooks"),
            budget,
            follow=bool(kwargs.get("allow_redirects", True)),
            limit=limit,
            source=source,
            page=page,
        )
        token = _request_budget.set(budget)
        rejected: requests.Response | None = None
        try:
            try:
                resp = send(url, *args, **kwargs)
            except (requests.RequestException, ValueError) as err:
                # Session.send() stores the response's cookies, and those of
                # the responses that a hook put in its history, before it
                # prepares Response.next. So the error of that preparation
                # loses no cookie.
                if (
                    isinstance(err, ValueError)
                    and (unfollowed := budget.unfollowed) is not None
                ):
                    rejected = unfollowed
                else:
                    if isinstance(err, requests.RequestException):
                        _drop_response_hooks(err.response, err.request)
                    raise
            if rejected is not None:
                # The error that rejected the header can quote it, so none is
                # attached.
                if no_follow:
                    raise _redirected_write(rejected, name, what) from None
                raise _unpreparable_redirect(rejected, name) from None
            _drop_response_hooks(resp)
            _record_asked_url(resp, asked, page)
            if no_follow and _is_unfollowed_redirect(resp.status_code):
                raise _redirected_write(resp, name, what)
            budget.check_time(resp)
            if not streamed or _is_failure(resp.status_code):
                budget.read_body(resp)
            return self._check_response(resp)
        finally:
            _request_budget.reset(token)

    def _guard_redirects(
        self,
        url: str,
        hooks: dict[str, Any] | None,
        budget: _Budget | None = None,
        follow: bool = True,
        limit: _PageLimit | None = None,
        source: Source = Source.CALLER,
        page: str | None = None,
    ) -> dict[str, Any]:
        """Return request hooks that refuse unsafe redirects.

        The hook checks every redirect once, against the request's origin and
        ``redirect_origins``, or against ``limit``'s origins if a page request
        passes one.

        Args:
            url: Absolute URL of the request.
            hooks: Hooks passed with the request, if any.
            budget: Limits of the request, which each redirect's body and the
                time to reach it must respect.
            follow: Whether requests follows redirects. If not, the hook
                refuses nothing, because no redirect is followed, and a caller
                that cannot accept an unfollowed redirect refuses it itself. It
                records a final redirect on ``budget``, so that ``_send()`` can
                tell a ``ValueError`` that requests raises while it prepares
                ``Response.next`` for the redirect from any other.
            limit: The limit of a page request, which replaces the allowed
                origins and supplies the error for a redirect outside them.
            source: Where ``url`` came from.
            page: Which page of a collection a link names, as ``"page 3"``, or
                ``None``.

        Returns:
            ``hooks``, or the session's hooks if ``hooks`` has no response
            hooks, with a final response hook that checks each redirect before
            requests follows it.
        """
        # Every request checks the attribute, also one that a page request's
        # own limit replaces, so that a value that is refused is refused
        # whatever the request.
        permitted = _permitted_origins(url, self.redirect_origins)
        allowed = permitted if limit is None else set(limit.origins)

        asked = _asked_text(url, self.session, source)

        def name(resp: requests.Response) -> str:
            """Return the request that a message names.

            Args:
                resp: Response, whose request has the method.

            Returns:
                The method of ``resp``'s request and what a message shows of
                the URL that was asked for.
            """
            return _request_name(resp.request.method, asked, page)

        def check_redirect(resp: requests.Response, **kwargs: Any) -> None:
            """Refuse a redirect that is not safe to follow.

            Args:
                resp: Response, which may be a redirect.
                kwargs: Arguments that requests passes to response hooks.

            Raises:
                RedirectError: If requests follows the redirect, and its
                    ``Location`` cannot be parsed or prepared, or it leads to
                    an origin that is not allowed. The message names the
                    request by the URL that was asked for, and quotes no part
                    of the ``Location``. The response is closed first.
                Exception: The error that ``limit`` supplies, instead of
                    ``RedirectError``, for a redirect outside its origins.
            """
            if not follow:
                if resp.is_redirect and budget is not None:
                    budget.unfollowed = resp
                return
            try:
                target = self.session.get_redirect_target(resp)
                # requests follows no redirect to an empty target.
                if not target:
                    return
                prepared = _prepare_redirect(self.session, resp, target)
            except ValueError:
                pass
            else:
                if prepared is None:
                    # requests cannot prepare the target, as for a port that is
                    # not a number.
                    raise _unquotable_redirect(
                        resp, "requests cannot prepare", name(resp)
                    )
                if not _leaves_allowed_origins(prepared, allowed):
                    if budget is not None:
                        # requests reads a redirect's body before following it.
                        budget.check_time(resp)
                        budget.read_body(resp)
                    return

                resp.close()
                if limit is not None:
                    raise limit.refusal()
                raise RedirectError(
                    f"{name(resp)} was redirected with status "
                    f"{resp.status_code} to another origin, so it was not "
                    "followed. The response of this error holds the Location "
                    "header. Add its origin to API.redirect_origins to "
                    "follow it.",
                    response=resp,
                )
            # The error that rejected the header can quote it, so none is
            # attached.
            raise _unquotable_redirect(
                resp, "cannot be parsed", name(resp)
            ) from None

        # requests uses a request's response hooks instead of the session's, so
        # keep the session's when the request has none. Run the guard last,
        # after any hook that might change the response.
        merged = dict(hooks or {})
        response = merged.get("response") or self.session.hooks.get("response")
        merged["response"] = [*_response_hooks(response), check_redirect]
        return merged

    def get(self, uri: str, *args: Any, **kwargs: Any) -> requests.Response:
        """Issue a ``GET`` request.

        Args:
            uri: Relative or absolute request URI.
            *args: Positional arguments forwarded to ``requests.Session.get``.
            **kwargs: Keyword arguments forwarded to ``requests.Session.get``.

        Returns:
            Validated HTTP response.

        Raises:
            APIError: If the status reports a failure, or ``NotFound`` for a
                404, as ``raise_for_response()`` raises them.
            RedirectError: If a redirect leads to an origin that is not
                allowed, or the final response is a redirect that is not
                followed and requests raises ``ValueError`` or ``UnicodeError``
                while it prepares the next request for it.
            LimitExceeded: If the request exceeds ``total_timeout``, or the
                response exceeds ``max_response_bytes``.
            requests.RequestException: If the request fails to send, as
                ``requests.ConnectionError`` does.
            TypeError: If ``redirect_origins`` is not a tuple, a list, or a
                frozenset of strings. The request is not sent.
            ValueError: If a string in ``redirect_origins`` is no absolute
                ``http`` or ``https`` URL with a host. The request is not sent.
        """
        return self._send("GET", self.session.get, uri, *args, **kwargs)

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

        Raises:
            APIError: As for ``get()``.
            RedirectError: If the response is a redirect, since the request
                follows none, and as for ``get()``.
            LimitExceeded: As for ``get()``.
            requests.RequestException: As for ``get()``.
            TypeError: As for ``get()``.
        """
        return self._send("POST", self.session.post, uri, *args, **kwargs)

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

        Raises:
            APIError: As for ``get()``.
            RedirectError: If the response is a redirect, since the request
                follows none, and as for ``get()``.
            LimitExceeded: As for ``get()``.
            requests.RequestException: As for ``get()``.
            TypeError: As for ``get()``.
        """
        return self._send("PATCH", self.session.patch, uri, *args, **kwargs)

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

        Raises:
            APIError: As for ``get()``.
            RedirectError: If the response is a redirect, since the request
                follows none, and as for ``get()``.
            LimitExceeded: As for ``get()``.
            requests.RequestException: As for ``get()``.
            TypeError: As for ``get()``.
        """
        return self._send("PUT", self.session.put, uri, *args, **kwargs)

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

        Raises:
            APIError: As for ``get()``.
            RedirectError: If the response is a redirect, since the request
                follows none, and as for ``get()``.
            LimitExceeded: As for ``get()``.
            requests.RequestException: As for ``get()``.
            TypeError: As for ``get()``.
        """
        return self._send("DELETE", self.session.delete, uri, *args, **kwargs)


class _ScopedAPI:
    """A view of an ``API`` that sends request options with each request.

    A query with ``request_options=`` passes this to its hooks as ``api``, in
    place of the ``API``. Each request method merges the call's options over
    the view's and sends the request with the ``API``'s method of the same
    name, so a subclass of ``API`` that overrides ``get()`` sees every request
    that a hook sends through the view. Pagination reads a page without it: it
    sends the page's request through the ``API`` and adds the view's options.
    The view holds no state that a request changes. It adds its options and
    nothing else, so a hook that sends ``allow_redirects=False`` reads the
    redirect itself, as it does without options.

    The view has the members of ``Client``. Reading any other attribute reads
    the ``API``'s, so ``api.session`` and a subclass's helpers work, but a
    helper that sends requests through its own ``self`` does not send the
    options. Assigning an attribute raises ``AttributeError``. It is not an
    ``API``, so ``isinstance(api, API)`` is false.
    """

    __slots__ = ("_api", "_request_options")

    _api: API
    _request_options: Mapping[str, Any]

    def __init__(self, api: API, options: Mapping[str, Any]) -> None:
        """Wrap ``api`` with the options that its requests send.

        Args:
            api: The client that sends the requests.
            options: Request options that every request sends.
        """
        object.__setattr__(self, "_api", api)
        object.__setattr__(self, "_request_options", MappingProxyType(options))

    def __setattr__(self, name: str, value: Any) -> None:
        """Refuse to assign an attribute, which the ``API`` would not see.

        Args:
            name: Attribute name.
            value: Value that was assigned.

        Raises:
            AttributeError: Always.
        """
        raise AttributeError(
            f"cannot assign {name!r} on a request-scoped view of an API. "
            "Assign it on the API"
        )

    def __getattr__(self, name: str) -> Any:
        """Read an attribute that the view does not define from the ``API``.

        Args:
            name: Attribute name.

        Returns:
            The ``API``'s attribute.
        """
        return getattr(object.__getattribute__(self, "_api"), name)

    def __reduce__(self) -> tuple[Any, ...]:
        """Rebuild the view from its ``API`` and a plain dict of its options.

        Copying, deep-copying, and pickling use this, since the options are
        held in a read-only mapping that cannot be pickled. A deep copy
        deep-copies the ``API``, as for any object that holds one.

        Returns:
            The class and the arguments that build an equal view.
        """
        return (_ScopedAPI, (self._api, dict(self._request_options)))

    def __repr__(self) -> str:
        """Return the view's API.

        Returns:
            Representation of the view.
        """
        return f"<request-scoped view of {self._api!r}>"

    @property
    def base_url(self) -> str:
        """The API's base URL."""
        return self._api.base_url

    @property
    def total_timeout(self) -> float | None:
        """The API's ``total_timeout``."""
        return self._api.total_timeout

    @property
    def max_response_bytes(self) -> int | None:
        """The API's ``max_response_bytes``."""
        return self._api.max_response_bytes

    @property
    def max_json_depth(self) -> int | None:
        """The API's ``max_json_depth``."""
        return self._api.max_json_depth

    @property
    def max_pages(self) -> int | None:
        """The API's ``max_pages``."""
        return self._api.max_pages

    @property
    def max_items(self) -> int | None:
        """The API's ``max_items``."""
        return self._api.max_items

    @property
    def list_lookup_style(self) -> Literal["comma", "repeat"]:
        """The API's ``list_lookup_style``."""
        return self._api.list_lookup_style

    def resolve_url(self, uri: str) -> str:
        """Resolve a request URI against the API's ``base_url``.

        Args:
            uri: Relative or absolute request URI.

        Returns:
            Fully resolved request URL.
        """
        return self._api.resolve_url(uri)

    def json(self, resp: Response) -> Any:
        """Decode a response's JSON with the API's depth limit.

        Args:
            resp: Response that the API returned.

        Returns:
            The decoded JSON value.
        """
        return self._api.json(resp)

    def _scoped(self, options: Mapping[str, Any]) -> _ScopedAPI:
        """Return a view of the same API with more options.

        Args:
            options: Request options that take precedence over this view's.

        Returns:
            The view, whose options are these merged over this view's.
        """
        return _ScopedAPI(
            self._api, _merge_request_options(self._request_options, options)
        )

    def _send(
        self,
        send: Callable[..., requests.Response],
        uri: str,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
    ) -> requests.Response:
        """Send a request with the API's method, adding this view's options.

        Args:
            send: The API's method for the request's HTTP method.
            uri: Relative or absolute request URI.
            args: Positional arguments for the method.
            kwargs: Keyword arguments for the method, which take precedence
                over this view's options.

        Returns:
            Validated HTTP response.
        """
        return send(
            uri, *args, **_merge_request_options(self._request_options, kwargs)
        )

    def get(self, uri: str, *args: Any, **kwargs: Any) -> requests.Response:
        """Issue a ``GET`` request through the API with this view's options.

        Args:
            uri: Relative or absolute request URI.
            *args: Positional arguments forwarded to ``API.get``.
            **kwargs: Keyword arguments forwarded to ``API.get``.

        Returns:
            Validated HTTP response.
        """
        return self._send(self._api.get, uri, args, kwargs)

    def post(self, uri: str, *args: Any, **kwargs: Any) -> requests.Response:
        """Issue a ``POST`` request through the API with this view's options.

        Args:
            uri: Relative or absolute request URI.
            *args: Positional arguments forwarded to ``API.post``.
            **kwargs: Keyword arguments forwarded to ``API.post``.

        Returns:
            Validated HTTP response.
        """
        return self._send(self._api.post, uri, args, kwargs)

    def patch(self, uri: str, *args: Any, **kwargs: Any) -> requests.Response:
        """Issue a ``PATCH`` request through the API with this view's options.

        Args:
            uri: Relative or absolute request URI.
            *args: Positional arguments forwarded to ``API.patch``.
            **kwargs: Keyword arguments forwarded to ``API.patch``.

        Returns:
            Validated HTTP response.
        """
        return self._send(self._api.patch, uri, args, kwargs)

    def put(self, uri: str, *args: Any, **kwargs: Any) -> requests.Response:
        """Issue a ``PUT`` request through the API with this view's options.

        Args:
            uri: Relative or absolute request URI.
            *args: Positional arguments forwarded to ``API.put``.
            **kwargs: Keyword arguments forwarded to ``API.put``.

        Returns:
            Validated HTTP response.
        """
        return self._send(self._api.put, uri, args, kwargs)

    def delete(self, uri: str, *args: Any, **kwargs: Any) -> requests.Response:
        """Issue a ``DELETE`` request through the API with this view's options.

        Args:
            uri: Relative or absolute request URI.
            *args: Positional arguments forwarded to ``API.delete``.
            **kwargs: Keyword arguments forwarded to ``API.delete``.

        Returns:
            Validated HTTP response.
        """
        return self._send(self._api.delete, uri, args, kwargs)


_AnyAPI: TypeAlias = API | _ScopedAPI
"""An ``API``, or a scoped view that sends through one."""


def _as_client(api: Client) -> _AnyAPI:
    """Return a client that a hook or a caller passed back to eazyrest.

    ``API`` and its scoped view are the only clients there are so far, so every
    ``Client`` that eazyrest passes to a hook is one of them.

    Args:
        api: Client that a hook or a caller passed to eazyrest.

    Returns:
        ``api``.

    Raises:
        TypeError: If ``api`` is neither an ``API`` nor a scoped view of one.
    """
    if not isinstance(api, API | _ScopedAPI):
        raise TypeError(
            "eazyrest sends requests only through an API, not "
            f"{type(api).__name__}"
        )
    return api


def _as_api(api: Client) -> API:
    """Return the ``API`` that sends a client's requests.

    Args:
        api: An ``API``, or a scoped view of one.

    Returns:
        ``api``, or the ``API`` that a view sends through.

    Raises:
        TypeError: If ``api`` is neither.
    """
    api = _as_client(api)
    return api._api if isinstance(api, _ScopedAPI) else api


def _as_sender(api: Client) -> tuple[API, Mapping[str, Any]]:
    """Return the ``API`` that sends a client's requests, and their options.

    Args:
        api: An ``API``, or a scoped view of one.

    Returns:
        ``api`` and no options, or the ``API`` that a view sends through and
        the view's merged request options.

    Raises:
        TypeError: If ``api`` is neither.
    """
    api = _as_client(api)
    if isinstance(api, _ScopedAPI):
        return api._api, api._request_options
    return api, {}


def _registered_api(api: Client) -> API:
    """Return an ``API`` that a class registers.

    Args:
        api: The client to register.

    Returns:
        ``api``.

    Raises:
        TypeError: If ``api`` is not an ``API``. A scoped view is not, since
            the registry holds the one ``API`` that sends requests.
    """
    if not isinstance(api, API):
        raise TypeError(
            "eazyrest sends requests only through an API, not "
            f"{type(api).__name__}"
        )
    return api
