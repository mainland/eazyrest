"""The reading of a URL that the server supplied, as requests prepares it."""

from __future__ import annotations

from typing import NamedTuple
from urllib.parse import urlsplit

_DEFAULT_PORTS = {"http": 80, "https": 443}


class _ServerURL(NamedTuple):
    """A URL that the server supplied, as requests prepares it."""

    url: str
    """The prepared URL, with its user and password."""
    scheme: str
    """Lowercased scheme."""
    host: str | None
    """Host of an HTTP or HTTPS URL, or ``None`` for another scheme."""
    port: int | None
    """Port of an HTTP or HTTPS URL, or its scheme's default, else ``None``."""
    sendable: bool
    """Whether the session has an adapter for the URL."""

    @classmethod
    def from_prepared(cls, url: str, sendable: bool = True) -> _ServerURL:
        """Read a URL that requests has prepared.

        Args:
            url: The prepared URL.
            sendable: Whether the session has an adapter for the URL.

        Returns:
            The URL with its scheme, host, and port, read as
            ``_prepare_server_url()`` reads them.

        Raises:
            ValueError: If the URL's port is not a number or is out of range.
        """
        parts = urlsplit(url)
        scheme = parts.scheme.lower()
        http = scheme in _DEFAULT_PORTS
        host = parts.hostname if http else None
        port = parts.port or _DEFAULT_PORTS[scheme] if http else None
        return cls(url, scheme, host, port, sendable)

    @property
    def is_http(self) -> bool:
        """Whether the URL is an HTTP or HTTPS URL that names a host."""
        return self.host is not None

    @property
    def scheme_name(self) -> str | None:
        """What a message calls a URL that is not HTTP or HTTPS, else ``None``.

        Such a URL has no user and password to leave out of a message, and its
        text can read as one, so a message names its scheme alone.
        """
        if self.is_http:
            return None
        return f"a location whose scheme is {self.scheme}:"

    @property
    def origin(self) -> tuple[str, str | None, int | None]:
        """The scheme, host, and port that requests reads from the URL."""
        return self.scheme, self.host, self.port

    @property
    def origin_text(self) -> str:
        """What a message quotes of a URL that it refuses: its origin.

        A message builds its text from the parts of the reading, never from the
        URL's text, so it can hold no user, password, query, or fragment, and
        no text that a parser might read as one. The URL itself stays on the
        error's response, or, for a page link, on the page.

        The origin is ``scheme://host``, with ``:port`` unless the port is the
        scheme's default and the host in brackets if it is an IPv6 address. The
        host is the one that requests connects to. It holds no character that
        is not printable, since requests rejects or encodes such a character in
        a host. A URL that is not HTTP or HTTPS has no origin, so this is
        ``scheme_name``.
        """
        if self.host is None:
            return str(self.scheme_name)
        host = f"[{self.host}]" if ":" in self.host else self.host
        if self.port != _DEFAULT_PORTS.get(self.scheme):
            host = f"{host}:{self.port}"
        return f"{self.scheme}://{host}"

    @property
    def origin_and_path(self) -> str:
        """What a message quotes of a write redirect's target.

        The path is the advice, as a missing trailing slash is. It is as
        requests prepares it, which percent-encodes any character that is not
        printable, and it ends before any query or fragment. Only an HTTP or
        HTTPS URL has a path to quote, and a write redirect reaches a message
        only on an origin that the request allows, which is one.
        """
        return self.origin_text + urlsplit(self.url).path
