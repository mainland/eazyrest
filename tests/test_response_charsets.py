"""A JSON body is decoded as UTF-8, UTF-16, or UTF-32, and as nothing else.

A response declares its charset by name, and a name that a server chooses must
not reach the codec registry. That registry decodes by any codec that Python
ships, some of which take time quadratic in the size of a body, and it caches
each name that it finds. Only the six names of UTF-16 and UTF-32 change the
decoding. A body that declares any other charset, or none, is UTF-8.
"""

from __future__ import annotations

import codecs
import encodings
import pkgutil
from collections.abc import Callable
from typing import Any

import pytest
import requests
import requests_mock

from eazyrest import API, APIError, JSONObject, ResponseShapeError, json_object
from eazyrest._errors import _body_text

BASE = "https://charsets.example/"
api = API(BASE)


@json_object
class City(JSONObject):
    """A model that the tests load, list, and create."""

    class_url = "/cities/"
    id: int
    name: str


City.register_api(api)

TEXT = '{"id": 1, "name": "Zürich"}'
UTF_8 = TEXT.encode()
CHARSETS = [
    f"utf-{width}{order}"
    for width in ("16", "32")
    for order in ("", "be", "le")
]


def _codec_names() -> list[str]:
    """Return every codec name and alias that ``encodings`` registers."""
    names = {
        info.name
        for info in pkgutil.iter_modules(encodings.__path__)
        if not info.name.startswith("_") and info.name not in {"aliases"}
    }
    names |= set(encodings.aliases.aliases)
    names.add("undefined")
    names.add("punycode")
    return sorted(names)


def _is_listed(name: str) -> bool:
    """Return whether ``name`` is one of the six names that are decoded."""
    return name.lower() in CHARSETS


OTHER_NAMES = [name for name in _codec_names() if not _is_listed(name)]


def _serve(
    mocker: Any, body: bytes, content_type: str | None, status: int = 200
) -> None:
    """Answer every request with ``body``, and the status for ``/broken/``."""
    headers = {} if content_type is None else {"Content-Type": content_type}
    mocker.register_uri(
        requests_mock.ANY,
        requests_mock.ANY,
        content=body,
        headers=headers,
    )
    mocker.register_uri(
        requests_mock.ANY,
        BASE + "broken/",
        content=body,
        headers=headers,
        status_code=500,
    )


def _failed_status() -> object:
    """Return the JSON that the error of a failed status holds."""
    try:
        api.get("/broken/")
    except APIError as err:
        return err.data["name"] if isinstance(err.data, dict) else None
    raise AssertionError("The failed status raised nothing")


LISTING = f"[{TEXT}]"
# Each entry point, with the JSON that it reads and what it returns.
ENTRY_POINTS: dict[str, tuple[str, Callable[[], object]]] = {
    "json": (TEXT, lambda: api.json(api.get("/cities/1/"))["name"]),
    "load": (TEXT, lambda: City(id=1).name),
    "filter": (LISTING, lambda: next(iter(City.filter())).name),
    "get_page": (LISTING, lambda: City.get_page().items[0].name),
    "write": (TEXT, lambda: City.create(name="x").name),
    "failed status": (TEXT, _failed_status),
}


def test_the_charsets_that_are_decoded_are_the_six_of_utf_16_and_utf_32() -> (
    None
):
    """Every other name that ``encodings`` registers reads as UTF-8."""
    resp = requests.Response()
    resp._content = UTF_8
    decoded = []
    for name in _codec_names():
        resp.headers["Content-Type"] = f"application/json; charset={name}"
        try:
            text = _body_text(resp)
        except ValueError:
            text = None
        if text == TEXT:
            decoded.append(name)
    assert decoded == OTHER_NAMES


@pytest.mark.parametrize("name", OTHER_NAMES)
def test_a_charset_that_is_not_listed_reads_the_body_as_utf_8(
    name: str,
) -> None:
    """The name is ignored, whatever codec it names."""
    resp = requests.Response()
    resp._content = UTF_8
    resp.headers["Content-Type"] = f"application/json; charset={name}"

    assert _body_text(resp) == TEXT


@pytest.mark.parametrize(
    ("text", "read"), ENTRY_POINTS.values(), ids=ENTRY_POINTS.keys()
)
@pytest.mark.parametrize(
    "content_type",
    [
        "application/json; charset=zip",
        "application/json; charset=punycode",
        "application/json; charset=undefined",
        "application/json; charset=utf8",
        "application/json; charset=us-ascii",
        "application/json; charset=",
        "application/json",
        "text/plain",
        None,
    ],
)
def test_a_body_with_any_other_charset_is_read_as_utf_8_at_every_entry_point(
    text: str, read: Callable[[], object], content_type: str | None
) -> None:
    """Each entry point reads the name that the body holds."""
    with requests_mock.Mocker() as mocker:
        _serve(mocker, text.encode(), content_type)
        assert read() == "Zürich"


@pytest.mark.parametrize("charset", CHARSETS)
@pytest.mark.parametrize(
    "spelling",
    [str.lower, str.upper, str.title, lambda name: f'"{name}"'],
    ids=["lower", "upper", "title", "quoted"],
)
def test_each_listed_charset_decodes_in_any_case(
    charset: str, spelling: Callable[[str], str]
) -> None:
    """The six names are matched without regard to case."""
    codec = charset.replace("utf-", "utf_")
    resp = requests.Response()
    resp._content = TEXT.encode(codec)
    resp.headers["Content-Type"] = (
        f"application/json; charset={spelling(charset)}"
    )

    assert _body_text(resp) == TEXT


@pytest.mark.parametrize("charset", CHARSETS)
@pytest.mark.parametrize(
    ("text", "read"), ENTRY_POINTS.values(), ids=ENTRY_POINTS.keys()
)
def test_a_body_in_a_listed_charset_is_read_at_every_entry_point(
    text: str, read: Callable[[], object], charset: str
) -> None:
    """Each entry point decodes the body in the charset that it declares."""
    body = text.encode(charset.replace("utf-", "utf_"))
    with requests_mock.Mocker() as mocker:
        _serve(mocker, body, f"application/json; charset={charset}")
        assert read() == "Zürich"


@pytest.mark.parametrize("charset", ["utf-16", "utf-32"])
def test_a_byte_order_mark_picks_the_order_of_a_listed_charset(
    charset: str,
) -> None:
    """The mark is read, and dropped, as the codec reads it."""
    codec = charset.replace("utf-", "utf_")
    resp = requests.Response()
    resp._content = TEXT.encode(codec)
    resp.headers["Content-Type"] = f"application/json; charset={charset}"
    assert resp.content[:2] in {b"\xff\xfe", b"\xfe\xff"}

    assert _body_text(resp) == TEXT


def test_a_byte_order_mark_is_dropped_from_a_body_that_is_utf_8() -> None:
    """A body that declares UTF-8, or nothing, may begin with the mark."""
    for content_type in ("application/json; charset=utf-8", "text/plain"):
        resp = requests.Response()
        resp._content = b"\xef\xbb\xbf" + UTF_8
        resp.headers["Content-Type"] = content_type
        assert _body_text(resp) == TEXT


@pytest.mark.parametrize(
    ("text", "read"), ENTRY_POINTS.values(), ids=ENTRY_POINTS.keys()
)
def test_a_latin_1_body_declared_iso_8859_1_is_not_json(
    text: str, read: Callable[[], object]
) -> None:
    """The declared name is not honored, so the bytes are not UTF-8."""
    body = text.encode("latin-1")
    with requests_mock.Mocker() as mocker:
        _serve(mocker, body, "application/json; charset=ISO-8859-1")
        if read is _failed_status:
            assert read() is None
        else:
            with pytest.raises(ResponseShapeError, match="does not hold JSON"):
                read()


@pytest.mark.parametrize("charset", CHARSETS)
def test_a_body_that_does_not_decode_in_its_listed_charset_is_no_json(
    charset: str,
) -> None:
    """A body of three bytes is no text in any of the six charsets."""
    resp = requests.Response()
    resp._content = b"\xff\xfe\xfd"
    resp.headers["Content-Type"] = f"application/json; charset={charset}"

    with pytest.raises(ValueError, match="not valid text"):
        _body_text(resp)


def test_a_declared_name_never_reaches_the_codec_registry() -> None:
    """A search function never sees the name of a charset that is read."""
    seen: list[str] = []

    def search(name: str) -> None:
        seen.append(name)

    codecs.register(search)
    try:
        resp = requests.Response()
        resp._content = UTF_8
        resp.headers["Content-Type"] = (
            "application/json; charset=never-registered-before-1234"
        )
        assert _body_text(resp) == TEXT
    finally:
        codecs.unregister(search)

    assert not any("never-registered" in name for name in seen)
