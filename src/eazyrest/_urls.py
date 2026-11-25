"""The one reading of a URL, and the one rule for what a message shows of it.

Every URL that eazyrest parses is parsed here, as requests prepares it, and
every message, ``repr()``, and exception argument that names a URL builds the
name here, by where the URL came from. No other module parses a URL or decides
what is shown of one.
"""

from __future__ import annotations

import enum
from collections.abc import Collection
from typing import NamedTuple
from urllib.parse import SplitResult, parse_qsl, urljoin, urlparse, urlsplit

import requests
from requests.utils import requote_uri

_DEFAULT_PORTS = {"http": 80, "https": 443}

_Origin = tuple[str, str, int]
"""The scheme, host, and port of an HTTP or HTTPS URL."""


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
            The URL with its scheme, host, and port, read as ``_read_url()``
            reads them.

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
    def origin(self) -> _Origin | None:
        """The scheme, host, and port that requests reads from the URL.

        Only an HTTP or HTTPS URL that names a host has an origin. No origin
        matches anything, so ``_origin_matches()`` is the one place that
        compares one.
        """
        if self.host is None or self.port is None:
            return None
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


class Source(enum.Enum):
    """Where a URL came from, which decides what a message shows of it."""

    CALLER = "caller"
    """A URL passed to a request method, or an object's URL that eazyrest
    builds from the base URL, the class URL, and a key. A message shows its
    origin and path, and ``?...`` for a query."""
    CONFIG = "config"
    """The base URL. A message shows its origin and path. No message shows an
    entry of ``API.redirect_origins``, which can hold a credential."""
    SERVER = "server"
    """A URL of which any part a response chose: a ``Location``, the URL that a
    redirect reached, a next-page or previous-page link, or a URL read from a
    body. A message shows its origin."""


UNREADABLE = "<unreadable URL>"
"""What a message says for a URL that requests cannot prepare or send, or that
is not an HTTP or HTTPS URL, whatever its text."""


def _attempt(
    text: str | bytes, session: requests.Session | None
) -> tuple[_ServerURL | None, type[requests.RequestException] | None]:
    """Prepare a URL as requests will, and say how it failed if it did.

    Args:
        text: The URL.
        session: Session that would send a request to it, or ``None`` to skip
            the check for an adapter.

    Returns:
        The reading and ``None``, or ``None`` and the class of the error that
        requests raises for the URL, which is one of ``MissingSchema``,
        ``InvalidSchema``, and ``InvalidURL``.
    """
    sent = text.decode("utf-8", "replace") if isinstance(text, bytes) else text
    failure: type[requests.RequestException]
    request = requests.PreparedRequest()
    try:
        request.prepare_url(text, None)
        request.prepare_headers(None)
        request.prepare_auth(None)
        assert request.url is not None
        prepared = _ServerURL.from_prepared(request.url, sent=sent)
        if not prepared.scheme:
            failure = requests.exceptions.MissingSchema
        elif session is None:
            return prepared, None
        else:
            try:
                session.get_adapter(request.url)
            except requests.exceptions.InvalidSchema:
                if prepared.scheme in _DEFAULT_PORTS:
                    failure = requests.exceptions.InvalidSchema
                else:
                    return prepared._replace(sendable=False), None
            else:
                return prepared, None
    except requests.exceptions.MissingSchema:
        failure = requests.exceptions.MissingSchema
    except requests.exceptions.InvalidSchema:
        failure = requests.exceptions.InvalidSchema
    except (ValueError, requests.RequestException):
        failure = requests.exceptions.InvalidURL
    return None, failure


def _read_url(
    text: str | bytes, session: requests.Session | None = None
) -> _ServerURL | None:
    """Read a URL as requests prepares it, or ``None`` if requests cannot.

    This takes the steps that requests takes before it sends a request to the
    URL: ``prepare_url()``, the Basic authentication that it takes from the
    URL's user and password, and the session's choice of adapter. No rule says
    which text of a URL is a host and which a password, so the only reading to
    trust is requests'. A check of a URL's origin, and a name for a message,
    must use the result and not the text.

    The errors of requests, urllib3, and ``urllib.parse`` end here, so no
    caller raises inside a handler and chains an error that quotes the URL.

    Args:
        text: The URL, as a string or as ``bytes``, which requests decodes as
            UTF-8.
        session: Session that would send a request to the URL, or ``None`` to
            skip the check for an adapter.

    Returns:
        The prepared URL with its scheme, host, and port, or ``None`` if any
        step fails, or if the scheme is HTTP or HTTPS and the session has no
        adapter for the URL, as for a URL that begins with a control character,
        which ``urlsplit()`` reads as HTTP and requests does not, or if the URL
        has no scheme. A URL whose scheme is not HTTP or HTTPS has no host and
        port in the result. Its ``sendable`` says whether the session has an
        adapter for it.
    """
    return _attempt(text, session)[0]


def _unreadable_error(
    text: str | bytes, session: requests.Session | None = None
) -> requests.RequestException:
    """Return the error that requests raises for a URL it cannot send.

    The class is the one that requests raises, ``MissingSchema``,
    ``InvalidSchema``, or ``InvalidURL``, so code that catches it keeps
    working. The message is built here and quotes none of the URL, and the
    error has no cause or context, so its chain quotes none either. The caller
    raises the result outside any handler.

    Args:
        text: The URL.
        session: Session that would send a request to it, or ``None``.

    Returns:
        The error to raise. It is ``InvalidSchema`` for a URL that is readable
        but has no adapter, and ``InvalidURL`` for any other URL that requests
        reads but a parser of ``urllib.parse`` does not.
    """
    reading, failure = _attempt(text, session)
    if failure is None:
        failure = (
            requests.exceptions.InvalidSchema
            if reading is not None and not reading.sendable
            else requests.exceptions.InvalidURL
        )
    named = _shown(None, Source.CALLER)
    if failure is requests.exceptions.MissingSchema:
        return failure(f"Invalid URL {named}: no scheme was supplied")
    if failure is requests.exceptions.InvalidSchema:
        return failure(
            f"Invalid URL {named}: no connection adapter was found for its "
            "scheme"
        )
    return failure(f"Invalid URL {named}: requests cannot parse it")


def _shown(
    url: _ServerURL | None, source: Source, *, quoted: bool = True
) -> str:
    """Return what a message quotes of a URL.

    The text is built from the parts of requests' reading, which holds only
    printable ASCII because requests percent-encodes a path and a query, so it
    never holds the URL's user or password, or the text of a URL that requests
    reads differently.

    Args:
        url: The reading, from ``_read_url()``, or ``None``.
        source: Where the URL came from. A server's URL shows its origin, a
            configured URL its origin and path, and a caller's URL its origin
            and path, with ``?...`` in place of a query.
        quoted: Whether to quote the text with ``repr()``, as a message does
            for a URL inside a sentence. A name that follows a method, as in
            ``GET https://example.com/items``, is not quoted.

    Returns:
        The text, or ``UNREADABLE`` for ``None``, for a URL whose scheme is not
        HTTP or HTTPS, and for text that is not printable ASCII. That phrase is
        never quoted.
    """
    if url is None or not url.is_http:
        return UNREADABLE
    text = url.origin_text
    if source is not Source.SERVER:
        parts = urlsplit(url.url)
        text += parts.path
        if source is Source.CALLER and parts.query:
            text += "?..."
    if not text.isascii() or not text.isprintable() or " " in text:
        return UNREADABLE
    return repr(text) if quoted else text


def _request_name(
    method: str | None, asked: str, page: str | None = None
) -> str:
    """Return the name of a request for a message.

    Args:
        method: The request's method, as ``"GET"``, or ``None`` if unknown.
        asked: The URL as ``_shown()`` returns it unquoted.
        page: Which page of a collection the request is for, as ``"page 3"``,
            if it is for one that a link named, or ``None``.

    Returns:
        The method and the URL, as ``GET https://example.com/items``, or the
        method and the page, as ``GET page 3 of https://example.com``.
    """
    if page is None:
        return f"{method} {asked}"
    return f"{method} {page} of {asked}"


def _asked_text(
    url: str | bytes, session: requests.Session | None, source: Source
) -> str:
    """Return what a name shows of a URL, as ``_shown()`` returns it unquoted.

    Args:
        url: The URL.
        session: Session that would send a request to it, or ``None``.
        source: Where the URL came from.

    Returns:
        The text that follows a method in the name of a request.
    """
    return _shown(_read_url(url, session), source, quoted=False)


def _origin(text: str) -> _Origin | None:
    """Return the origin of an absolute URL, as requests would send it.

    Args:
        text: Absolute URL.

    Returns:
        Lowercased scheme, host, and port of ``text``, with the scheme's
        default port if it names none, or ``None`` if its scheme is not
        ``http`` or ``https``.

    Raises:
        requests.RequestException: If requests cannot prepare the URL, as
            ``_unreadable_error()`` describes it.
    """
    # Requests and urlsplit disagree about backslashes in an authority, so the
    # URL that requests would send is read, before it attaches credentials.
    reading = _read_url(text)
    if reading is None:
        raise _unreadable_error(text)
    return reading.origin


def _base_origins(base_url: str) -> set[_Origin]:
    """Return the origins that a request to a base URL may reach by itself.

    Args:
        base_url: Absolute URL of a request, or an API's base URL.

    Returns:
        A set of the URL's origin, or an empty set if its scheme is not
        ``http`` or ``https``, since such a URL has no origin to allow.

    Raises:
        requests.RequestException: If requests cannot prepare the URL, as for
            ``_origin()``.
    """
    origin = _origin(base_url)
    return set() if origin is None else {origin}


def _origin_matches(
    origin: _Origin | None, allowed: Collection[_Origin]
) -> bool:
    """Return whether an origin is one of those allowed.

    This is the one comparison of origins. A URL with no origin matches
    nothing, not even another URL with none, so that a base URL of another
    scheme never allows a link or a redirect to another host.

    Args:
        origin: A reading's origin, which is ``None`` for a URL of a scheme
            other than ``http`` and ``https``.
        allowed: The origins that are allowed.

    Returns:
        Whether ``origin`` is in ``allowed``.
    """
    return origin is not None and origin in allowed


def _is_absolute(uri: str | bytes) -> bool:
    """Return whether a request URI names a scheme or a host.

    Args:
        uri: Relative or absolute request URI.

    Returns:
        Whether the URI is absolute, as ``urlparse()`` reads it.

    Raises:
        requests.RequestException: If ``urlparse()`` rejects the URI, which
            is ``InvalidURL`` with a message that quotes none of it.
    """
    try:
        parts = urlparse(uri)
    except ValueError:
        parts = None
    if parts is None:
        raise _unreadable_error(uri)
    return bool(parts.scheme or parts.netloc)


def _join(base: str, link: str) -> str:
    """Resolve a link against the URL of the page that holds it.

    Args:
        base: URL of the page.
        link: Link, absolute or relative.

    Returns:
        The absolute URL.

    Raises:
        ValueError: If ``urljoin()`` cannot parse either. Its message can quote
            the text, so a caller raises its own error after the handler ends
            and attaches none.
    """
    return urljoin(base, link)


def _query_names(text: str) -> set[str]:
    """Return the names in the query of a URL, as requests sends it.

    Args:
        text: Absolute URL of a page.

    Returns:
        The names of the query's parameters, including those with empty values,
        or an empty set if requests cannot prepare the URL.
    """
    reading = _read_url(text)
    if reading is None:
        return set()
    return {name for name, _ in parse_qsl(urlsplit(reading.url).query, True)}


def _redirect_destination(resp: requests.Response, target: str) -> str:
    """Return the URL to which requests follows a redirect.

    This repeats the steps of ``requests.Session.resolve_redirects()``.
    requests sends the resulting string without preparing it again, so an
    origin check must examine exactly this string, as ``_prepare_redirect()``
    does. In particular, requests percent-encodes a backslash in an absolute
    target, which moves the end of its userinfo, so ``urljoin()`` of the raw
    target can name a different host.

    Args:
        resp: Redirect response.
        target: Redirect target, from ``Session.get_redirect_target()``.

    Returns:
        Absolute URL of the redirected request.

    Raises:
        ValueError: If the target cannot be parsed or joined, as for
            ``_join()``.
    """
    url = target
    if url.startswith("//"):
        url = f"{urlparse(resp.url).scheme}:{url}"
    parsed = urlparse(url)
    url = parsed.geturl()
    if not parsed.netloc:
        return urljoin(resp.url, requote_uri(url))
    return requote_uri(url)


def _prepare_redirect(
    session: requests.Session, resp: requests.Response, target: str
) -> tuple[_ServerURL, _ServerURL] | None:
    r"""Read the destination of a redirect as requests sends the request.

    A redirect is safe only if every reading of its target names an allowed
    origin. The destination is read as requests reads it. The target joined
    onto the request's URL without percent-encoding is read too, since a proxy,
    or a client that treats a backslash as a slash, as browsers do, may read it
    differently: ``https://a.test\@b.test/`` names ``a.test`` to a browser but
    ``b.test`` to ``urlsplit()``.

    Args:
        session: Session that follows the redirect.
        resp: Redirect response.
        target: Redirect target, from ``Session.get_redirect_target()``.

    Returns:
        The destination and the joined reading, or ``None`` if requests cannot
        prepare either.

    Raises:
        ValueError: If the target cannot be joined onto the request's URL.
    """
    destination = _read_url(_redirect_destination(resp, target), session)
    joined = _read_url(urljoin(resp.url, target), session)
    if destination is None or joined is None:
        return None
    return destination, joined


def _refusal(entry: str) -> str | None:
    """Say why an entry of ``API.redirect_origins`` is no origin.

    Args:
        entry: A string that should be an absolute ``http`` or ``https`` URL.

    Returns:
        ``None`` if ``entry`` is an ``http`` or ``https`` URL with a host, else
        a clause that says which check failed, as the message of
        ``_redirect_origin()`` ends. It quotes nothing of ``entry``.
    """
    reading, failure = _attempt(entry, None)
    if reading is not None and reading.is_http:
        return None
    if failure is requests.exceptions.MissingSchema:
        return "it has no scheme"
    if reading is not None:
        if reading.scheme in _DEFAULT_PORTS:
            return "it has no host"
        return "its scheme is not http or https"
    # Requests refuses an HTTP or HTTPS URL with no host as it refuses one with
    # a bad port, so ask the parser. Record what it says here and decide after
    # the handler, so that no error is chained.
    scheme = hostname = None
    try:
        parts = urlsplit(entry)
    except ValueError:
        pass
    else:
        scheme, hostname = parts.scheme.lower(), parts.hostname
    if scheme in _DEFAULT_PORTS and not hostname:
        return "it has no host"
    return "requests cannot parse it"


def _redirect_origin(position: int | None, entry: str) -> _Origin:
    """Return the origin of an entry of ``API.redirect_origins``.

    Args:
        position: Position of ``entry`` in a tuple or a list, or ``None`` for a
            frozenset, whose iteration order is no position.
        entry: A string that should be an absolute ``http`` or ``https`` URL.

    Returns:
        The scheme, host, and port of ``entry``, as ``_origin()`` gives them.

    Raises:
        ValueError: If ``entry`` is no ``http`` or ``https`` URL with a host
            that requests can prepare. The error names the attribute and the
            position, if any, and says which check failed: that the entry has
            no scheme, that its scheme is not ``http`` or ``https``, that it
            has no host, or that requests cannot parse it. It quotes nothing of
            ``entry``, whose text can read as a user and password, and it has
            no cause or context.
    """
    why = _refusal(entry)
    if why is not None:
        where = (
            "An entry of API.redirect_origins"
            if position is None
            else f"API.redirect_origins[{position}]"
        )
        raise ValueError(
            f"{where} must be an absolute http or https URL with a host, "
            f'such as "https://files.example.com", but {why}'
        )
    reading = _read_url(entry)
    assert reading is not None
    origin = reading.origin
    assert origin is not None
    return origin
