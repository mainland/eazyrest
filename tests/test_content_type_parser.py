"""The charset of a response is read by one parser that never raises.

``email.message`` parses a ``Content-Type`` in time quadratic in its length on
Python 3.11 to 3.13 and raises ``TypeError`` for the continuation form ``a*=1;
a*0=2`` on 3.11 and 3.12. The library reads the one parameter that it needs
with a scan of its own, and refuses a header that no server sends.
"""

from __future__ import annotations

import ast
import inspect
import time
from collections.abc import Callable

import pytest
import requests_mock
from hypothesis import given
from hypothesis import strategies as st
from test_response_charsets import (
    ENTRY_POINTS,
    _failed_status,
    _serve,
)

from eazyrest import _errors
from eazyrest._errors import _declared_charset

# A header and the charset that it declares, or None if it declares none.
HEADERS = {
    "bare": ("application/json; charset=utf-16", "utf-16"),
    "spaced": ("application/json ;  charset = utf-16 ", "utf-16"),
    "upper-case name": ("application/json; CHARSET=UTF-16LE", "UTF-16LE"),
    "quoted": ('application/json; charset="utf-16be"', "utf-16be"),
    "escaped": (r'application/json; charset="utf\-16"', "utf-16"),
    "quoted semicolon": (
        'application/json; a="x;charset=utf-16"; charset=utf-32',
        "utf-32",
    ),
    "after other parameters": ("text/x; a=1; b=2; charset=utf-32", "utf-32"),
    "the first of two": ("a/b; charset=utf-8; charset=utf-16", "utf-8"),
    "none": ("application/json", None),
    "empty": ("", None),
    "no value": ("a/b; charset", None),
    "empty value": ("a/b; charset=", None),
    "an empty value then a value": ("a/b; charset=; charset=utf-16", "utf-16"),
    "unterminated quote": ('a/b; charset="utf-16', None),
    "a later charset after an unterminated quote": (
        'a/b; a="x; charset=utf-16',
        None,
    ),
    "a space before a quote": ('a/b; charset= "utf-16"', "utf-16"),
    "text after a quote": ('a/b; charset="utf-16"x; c=1', None),
    "not a token": ("a/b; charset=u tf", None),
    "extended form": ("a/b; charset*=utf-16''x", None),
    "continuation": ("a/b; charset*0=utf; charset*1=-16", None),
    "a name that is no token": ("a/b; char set=utf-16", None),
    "only the type": ("charset=utf-16", None),
    "empty parameters": ("a/b;;;charset=utf-16", "utf-16"),
}


@pytest.mark.parametrize(
    ("header", "charset"), HEADERS.values(), ids=HEADERS.keys()
)
def test_the_parser_reads_the_charset_of_a_header(
    header: str, charset: str | None
) -> None:
    """The first ``charset`` parameter of the form that is read."""
    assert _declared_charset(header) == charset


@given(st.text())
def test_the_parser_never_raises_for_any_text(header: str) -> None:
    """A server may send any text, and none raises."""
    _declared_charset(header)


ATOMS = [
    "charset", "charset*", "charset*0", "*", "=", '"', "'", ";", " ", "\\",
    "utf-16", "a", "\x00", "é", "\r\n ", "\t", "%00", "''",
]  # fmt: skip


@given(st.lists(st.sampled_from(ATOMS), max_size=40).map("".join))
def test_the_parser_never_raises_for_the_pieces_of_a_header(
    header: str,
) -> None:
    """Text built from the pieces of a header, which random text rarely is."""
    _declared_charset("application/json; " + header)


# Headers that raised ``TypeError`` or took long with ``email.message``.
HOSTILE = [
    "application/json; a*=1; a*0=2",
    "text/html; x*0*=a; x*=b",
    "application/json; charset*=x; charset*0=y",
    "application/json; charset*0=utf; charset*=8",
    "application/json; charset*=utf-8''utf-8",
    "application/json; charset*=%00''utf-8",
    "application/json; charset*0*=utf-8''utf; charset*1=-8",
    "application/json; charset*99999999999999999999999=x",
    "application/json; charset=\x00",
    "application/json; charset=" + "a" * 4000,
    "application/json; charset=../../x",
    'application/json; charset="',
    "application/json;;;charset=utf-8",
    "; charset=utf-8",
    "application/json; charset=utf-8; charset=zip",
    "application/json; charset*=a; charset=b",
    "application/json; charset*0=a; charset*0*=b",
    "application/json; " + "a*=1; a*0=2; " * 100,
    "application/json" + "; " * 2000,
    "application/json; " + "\r\n " * 1000,
]


@pytest.mark.parametrize(
    ("text", "read"), ENTRY_POINTS.values(), ids=ENTRY_POINTS.keys()
)
@pytest.mark.parametrize("header", HOSTILE)
def test_a_hostile_header_is_read_as_utf_8_at_every_entry_point(
    text: str, read: Callable[[], object], header: str
) -> None:
    """No entry point raises anything but the error of the body or status."""
    assert len(header) <= 4096
    with requests_mock.Mocker() as mocker:
        _serve(mocker, text.encode(), header)
        assert read() == "Zürich"


@pytest.mark.parametrize(
    ("text", "read"), ENTRY_POINTS.values(), ids=ENTRY_POINTS.keys()
)
@pytest.mark.parametrize(
    ("length", "readable"), [(4096, True), (4097, False), (6 * 2**20, False)]
)
def test_a_content_type_is_read_up_to_4_kib(
    text: str, read: Callable[[], object], length: int, readable: bool
) -> None:
    """A header of 4096 characters reads, and one of 4097 holds no JSON."""
    header = "application/json; x=".ljust(length, "a")
    assert len(header) == length
    with requests_mock.Mocker() as mocker:
        _serve(mocker, text.encode(), header)
        if readable:
            assert read() == "Zürich"
        elif read is _failed_status:
            assert read() is None
        else:
            with pytest.raises(
                _errors.ResponseShapeError, match="does not hold JSON"
            ):
                read()


def _seconds(header: str) -> float:
    """Return the time that the parser takes for ``header``."""
    start = time.perf_counter()
    _declared_charset(header)
    return time.perf_counter() - start


SHAPES = {
    "semicolons": "; a=1",
    "quoted semicolons": '; a="x;',
    "equals signs": ";=",
    "backslashes": '; a="\\\\',
}


@pytest.mark.parametrize("unit", SHAPES.values(), ids=SHAPES.keys())
def test_the_parser_takes_time_linear_in_the_length_of_a_header(
    unit: str,
) -> None:
    """A header ten times longer takes far less than 100 times as long."""
    count = 6 * 2**20 // (10 * len(unit))
    small = "application/json" + unit * count
    large = "application/json" + unit * (10 * count)
    assert len(large) > 6 * 2**20 - 100
    base = min(_seconds(small) for _ in range(3))
    big = min(_seconds(large) for _ in range(2))
    assert big < 40 * max(base, 1e-4)


def test_the_module_does_not_import_the_email_package() -> None:
    """The parser of ``email`` is not used, so its cost and errors are not."""
    tree = ast.parse(inspect.getsource(_errors))
    imported = {
        alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {
        (node.module or "").split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.level == 0
    }
    assert "email" not in imported
