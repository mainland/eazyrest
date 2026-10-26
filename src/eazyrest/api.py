"""HTTP API client primitives for eazyrest."""

from __future__ import annotations

import sys
from collections.abc import Callable
from http.cookiejar import CookieJar
from typing import Any
from urllib.parse import urlparse

import requests
import urllib3

from .write_mode import WriteMode, validate_write_mode

if sys.version_info >= (3, 11):
    from typing import Self
else:
    from typing_extensions import Self

# The configuration that reset_session() copies to a new session. These are
# the attributes of requests.Session.__attrs__ other than adapters, which hold
# the connection pools.
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


class API:
    """HTTP client wrapper for REST APIs.

    The class wraps a ``requests.Session`` and automatically joins
    request paths against ``base_url`` while applying a default timeout.

    Requests follows redirects, and it resends a ``POST`` answered with
    a 301 or 302, and any other request answered with a 302 or 303, as a
    ``GET``. The server has then not performed the original request, so
    a request whose method a redirect changes raises
    ``requests.HTTPError`` instead of returning the response to the
    ``GET``. Redirects that keep the method, such as 307 and 308, are
    followed.
    """

    base_url: str
    """Base API URL."""

    session: requests.Session
    """Session object for API requests."""

    timeout: float | tuple[float, float] | None = 10.0
    """Default request timeout in seconds, as requests accepts it.

    A pair gives the connect and read timeouts separately, and ``None``
    waits forever. A request's own ``timeout`` argument takes
    precedence.
    """

    trailing_slash: bool = True
    """Whether object URLs end with a slash, as in ``/users/1/``.

    Django REST Framework's routers use a trailing slash. Set this to
    ``False``, on an instance or in a subclass, for an API whose object
    URLs have none, as in ``/users/1``.
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
            verify: Whether TLS certificates should be verified, or the path
                of a CA bundle to verify them with, as for requests. When
                ``False``, urllib3's ``InsecureRequestWarning`` is suppressed
                for the whole process.
            kwargs: If present, these are forwarded to a new
              ``requests.adapters.HTTPAdapter`` and mounted on the session.
              This allows for configuring connection pooling parameters, e.g.,
              ``pool_connections`` and ``pool_maxsize``, and retries, with
              ``max_retries``. To change the request timeout, set the
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
            verify: Whether TLS certificates should be verified, or the path
                of a CA bundle to verify them with. If omitted, the new
                session keeps the replaced session's setting, and a first
                session verifies certificates. When ``False``, urllib3's
                ``InsecureRequestWarning`` is suppressed for the whole process.
            kwargs: If present, these are forwarded to a new
              ``requests.adapters.HTTPAdapter`` and mounted on the session.
              This allows for configuring connection pooling parameters, e.g.,
              ``pool_connections`` and ``pool_maxsize``.
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
        """Return this API client for use as a context manager."""
        return self

    def __exit__(self, *args: object) -> None:
        """Close the session when leaving a context-manager block."""
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
            requests.HTTPError: If a redirect changed the request method, or
                if ``raise_for_response()`` raises it for the response.
        """
        kwargs.setdefault("timeout", self.timeout)
        if self.session.proxies:
            # requests lets proxy environment variables override session
            # proxies, but not proxies passed with the request. requests adds
            # environment proxies to the dict it receives, so pass a copy.
            kwargs.setdefault("proxies", dict(self.session.proxies))

        resp = send(self.resolve_url(uri), *args, **kwargs)
        self._check_redirects(resp)
        return self._check_response(resp)

    @staticmethod
    def _check_redirects(resp: requests.Response) -> None:
        """Reject a response to a request whose method a redirect changed.

        Args:
            resp: Final response, after requests followed any redirects.

        Raises:
            requests.HTTPError: If requests sent the final request with a
                different method from the original request.
        """
        if not resp.history:
            return

        redirect = resp.history[0]
        sent = redirect.request.method
        if resp.request.method != sent:
            raise requests.HTTPError(
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
            *args: Positional arguments forwarded to
                ``requests.Session.put``.
            **kwargs: Keyword arguments forwarded to
                ``requests.Session.put``.

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
