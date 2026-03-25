"""HTTP API client primitives for eazyrest."""

from http.cookiejar import CookieJar
from typing import Any
from urllib.parse import urlparse

import certifi
import requests
import urllib3


class API:
    """HTTP client wrapper for REST APIs.

    The class wraps a ``requests.Session`` and automatically joins request
    paths against ``base_url`` while applying a default timeout.
    """

    base_url: str
    """Base API URL."""

    session: requests.Session
    """Session object for API requests."""

    timeout: float | None = 10.0
    """Default request timeout."""

    def __init__(
        self,
        url: str,
        proxy: str | None = None,
        **kwargs: Any,
    ):
        """Initialize an API client.

        Args:
            url: Base URL for the API.
            proxy: Optional proxy URL for both HTTP and HTTPS traffic.
            kwargs: If present, these are forwarded to a new
              ``requests.adapters.HTTPAdapter`` and mounted on the session.
              This allows for configuring connection pooling parameters, e.g.,
              ``pool_connections`` and ``pool_maxsize``.
        """
        self.base_url = url

        self.reset_session(**kwargs)

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

        Args:
            proxy: Proxy URL to apply to both ``http`` and ``https`` schemes.
        """
        proxies = {"http": proxy, "https": proxy}

        self.session.proxies.update(proxies)

    def reset_session(self, verify: bool = True, **kwargs: Any) -> None:
        """Create and configure a fresh underlying ``requests.Session``.

        Args:
            verify: Whether TLS certificates should be verified.
            kwargs: If present, these are forwarded to a new
              ``requests.adapters.HTTPAdapter`` and mounted on the session.
              This allows for configuring connection pooling parameters, e.g.,
              ``pool_connections`` and ``pool_maxsize``.
        """
        self.session = requests.Session()

        if verify:
            self.session.verify = certifi.where()
        else:
            self.session.verify = False
            urllib3.disable_warnings()

        # Adapt session for multiple connections
        if len(kwargs) != 0:
            url = urlparse(self.base_url)

            adapter = requests.adapters.HTTPAdapter(**kwargs)
            self.session.mount(url.scheme + "://", adapter)

    def close(self) -> None:
        """Close the underlying HTTP session."""
        self.session.close()

    def _check_response(self, resp: requests.Response) -> requests.Response:
        """Validate an HTTP response.

        Args:
            resp: Response object to validate.

        Returns:
            The same response when status code indicates success.

        Raises:
            requests.HTTPError: If the response status indicates failure.
        """
        resp.raise_for_status()
        return resp

    def _resolve_url(self, uri: str) -> str:
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

    def get(self, uri: str, *args: Any, **kwargs: Any) -> requests.Response:
        """Issue a ``GET`` request.

        Args:
            uri: Relative or absolute request URI.
            *args: Positional arguments forwarded to ``requests.Session.get``.
            **kwargs: Keyword arguments forwarded to ``requests.Session.get``.

        Returns:
            Validated HTTP response.
        """
        kwargs.setdefault("timeout", self.timeout)
        req = self.session.get(self._resolve_url(uri), *args, **kwargs)
        return self._check_response(req)

    def post(self, uri: str, *args: Any, **kwargs: Any) -> requests.Response:
        """Issue a ``POST`` request.

        Args:
            uri: Relative or absolute request URI.
            *args: Positional arguments forwarded to ``requests.Session.post``.
            **kwargs: Keyword arguments forwarded to ``requests.Session.post``.

        Returns:
            Validated HTTP response.
        """
        kwargs.setdefault("timeout", self.timeout)
        req = self.session.post(self._resolve_url(uri), *args, **kwargs)
        return self._check_response(req)

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
        kwargs.setdefault("timeout", self.timeout)
        req = self.session.patch(self._resolve_url(uri), *args, **kwargs)
        return self._check_response(req)

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
        kwargs.setdefault("timeout", self.timeout)
        req = self.session.delete(self._resolve_url(uri), *args, **kwargs)
        return self._check_response(req)
