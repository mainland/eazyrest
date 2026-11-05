"""HTTP API client primitives for eazyrest."""

from __future__ import annotations

import sys
from collections.abc import Callable, Collection
from http.cookiejar import CookieJar
from typing import Any
from urllib.parse import urljoin, urlparse, urlsplit

import requests
import urllib3
from requests.utils import requote_uri

from ._errors import RedirectError
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
    raise RedirectError(
        f"{method} {resp.url} was redirected with status {resp.status_code} "
        f"to {target}, which requests follows "
        + ("as a GET" if changed else "without the request body")
        + f", so the {method} request would not be performed. Only 307 "
        "and 308 redirects resend a request unchanged. Send the request to "
        "the URL that the server expects, for example by setting "
        "API.trailing_slash.",
        response=resp,
    )


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
    the redirect.
    """

    base_url: str
    """Base API URL."""

    session: requests.Session
    """Session object for API requests."""

    timeout: float | tuple[float, float] | None = 10.0
    """Default request timeout in seconds, as requests accepts it.

    A pair gives the connect and read timeouts separately, and ``None`` waits
    forever. A request's own ``timeout`` argument takes precedence.
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
                otherwise uses. When ``False``, urllib3's
                ``InsecureRequestWarning`` is suppressed for the whole process.
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
    def cookies(self) -> CookieJar:
        """Return cookies currently used by the session."""
        return self.session.cookies

    @cookies.setter
    def cookies(self, cookies: CookieJar) -> None:
        """Merge cookies into the active session.

        Args:
            cookies: Cookie jar to merge into the session cookie store.
        """
        self.session.cookies.update(cookies)

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
                verifies certificates. When ``False``, urllib3's
                ``InsecureRequestWarning`` is suppressed for the whole process.
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
            if verify is False:
                urllib3.disable_warnings(
                    urllib3.exceptions.InsecureRequestWarning
                )

        # Adapt session for multiple connections
        if len(kwargs) != 0:
            url = urlparse(self.base_url)

            adapter = requests.adapters.HTTPAdapter(**kwargs)
            session.mount(url.scheme + "://", adapter)

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
        if resp.ok:
            return resp

        self.raise_for_response(resp)
        return resp

    def raise_for_response(self, resp: requests.Response) -> None:
        """Raise an exception if ``resp`` represents a failed request.

        Subclasses can override this hook to translate API-specific error
        payloads into richer exception types while preserving the default
        ``requests`` behavior for successful responses.

        Args:
            resp: Response object to validate.

        Raises:
            requests.HTTPError: If the response status indicates failure.
        """
        resp.raise_for_status()

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
        kwargs.setdefault("timeout", self.timeout)
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
        kwargs["hooks"] = self._guard_redirects(url, kwargs.get("hooks"))
        resp = send(url, *args, **kwargs)
        self._check_redirects(resp)
        return self._check_response(resp)

    def _guard_redirects(
        self, url: str, hooks: dict[str, Any] | None
    ) -> dict[str, Any]:
        """Return request hooks that refuse unsafe redirects.

        Args:
            url: Absolute URL of the request.
            hooks: Hooks passed with the request, if any.

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
                kwargs: Other arguments that requests passes to response hooks,
                    which are ignored.

            Raises:
                RedirectError: If ``resp`` redirects to an origin that is not
                    allowed, or if requests would follow it without the
                    request's body. The response is closed first.
            """
            target = self.session.get_redirect_target(resp)
            # requests follows no redirect to an empty target.
            if not target:
                return

            destination = _redirect_destination(resp, target)
            if _redirect_origins(resp, target) <= allowed:
                _check_write_redirect(resp, target)
                return

            resp.close()
            raise RedirectError(
                f"{resp.request.method} {resp.url} was redirected with status "
                f"{resp.status_code} to {destination}, whose origin differs "
                "from the request's, so it was not followed. Add the origin "
                "to API.redirect_origins to follow it.",
                response=resp,
            )

        # requests uses a request's response hooks instead of the session's, so
        # keep the session's when the request has none. Run the guard last,
        # after any hook that might change the response.
        merged = dict(hooks or {})
        response = merged.get("response") or self.session.hooks.get("response")
        merged["response"] = [*_response_hooks(response), check_redirect]
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
        if not resp.history:
            return

        redirect = resp.history[0]
        sent = (redirect.request.method or "").upper()
        dropped = redirect.request.body is not None and any(
            hop.status_code not in _BODY_PRESERVING_REDIRECTS
            for hop in resp.history
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
