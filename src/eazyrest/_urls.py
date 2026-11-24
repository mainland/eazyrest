"""The reading of a URL that the server supplied, as requests prepares it."""

from __future__ import annotations

from typing import NamedTuple
from urllib.parse import SplitResult, urlsplit

_DEFAULT_PORTS = {"http": 80, "https": 443}


def _port_of(prepared: SplitResult, sent: str) -> int | None:
    """Return the port that a URL names, which can be 0.

    ``requests`` drops port 0 when it prepares a URL, but it sends a redirect's
    destination as it stands, so the adapter connects to port 0. Reading the
    prepared URL alone takes that port for the scheme's default.

    Args:
        prepared: The URL after ``requests`` prepared it.
        sent: The URL as it was before it was prepared.

    Returns:
        The prepared URL's port, or 0 if it has none and ``sent`` names 0, or
        ``None`` if neither names a port.
    """
    port = prepared.port
    if port is None:
        try:
            if urlsplit(sent).port == 0:
                return 0
        except ValueError:
            pass
    return port


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
    def from_prepared(
        cls, url: str, sendable: bool = True, sent: str | None = None
    ) -> _ServerURL:
        """Read a URL that requests has prepared.

        Args:
            url: The prepared URL.
            sendable: Whether the session has an adapter for the URL.
            sent: The URL as it was before it was prepared, if it was, so that
                a port of 0, which preparing drops, is read as 0.

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
        port = None
        if http:
            named = _port_of(parts, url if sent is None else sent)
            port = _DEFAULT_PORTS[scheme] if named is None else named
        return cls(url, scheme, host, port, sendable)

    @property
    def is_http(self) -> bool:
        """Whether the URL is an HTTP or HTTPS URL that names a host."""
        return self.host is not None

    @property
    def origin(self) -> tuple[str, str | None, int | None]:
        """The scheme, host, and port that requests reads from the URL."""
        return self.scheme, self.host, self.port

    @property
    def origin_text(self) -> str:
        """What a message quotes of an HTTP or HTTPS URL that it refuses.

        The text is built from the parts of the reading and not from the URL's
        text, so it holds no user, password, path, query, or fragment. It is
        ``scheme://host``, with ``:port`` unless the port is the scheme's
        default, and the host in brackets if it is an IPv6 address. Only an
        HTTP or HTTPS URL has an origin, so this is meaningful only if
        ``is_http`` is true.
        """
        host = self.host or ""
        if ":" in host:
            host = f"[{host}]"
        if self.port != _DEFAULT_PORTS.get(self.scheme):
            host = f"{host}:{self.port}"
        return f"{self.scheme}://{host}"
