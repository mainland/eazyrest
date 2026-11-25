"""A charset that names no text encoding is no JSON, as any bad charset is.

``codecs.lookup()`` finds codecs such as ``zip`` and ``rot13`` that decode
bytes to bytes or text to text, and they refuse a body only when it decodes.
The body must then read as one that holds no JSON, not raise ``LookupError``.
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
    """A model that the tests load and list."""

    class_url = "/cities/"
    id: int
    name: str


City.register_api(api)

BODY = b'{"id": 1, "name": "Oslo"}'


def _codec_names() -> list[str]:
    """Return every codec name and alias that ``encodings`` registers."""
    names = {
        info.name
        for info in pkgutil.iter_modules(encodings.__path__)
        if not info.name.startswith("_") and info.name not in {"aliases"}
    }
    names |= set(encodings.aliases.aliases)
    names.add("undefined")
    return sorted(names)


def _no_text_decoding(name: str) -> bool:
    """Return whether decoding a body in ``name`` raises a codec error."""
    try:
        codecs.lookup(name)
    except LookupError:
        return False
    try:
        BODY.decode(name)
    except (LookupError, UnicodeError):
        return True
    except Exception:  # noqa: BLE001
        return False
    return False


NAMES = [name for name in _codec_names() if _no_text_decoding(name)]


def _serve(mocker: Any, charset: str, status: int = 200) -> None:
    """Answer every request with a JSON body that declares ``charset``."""
    mocker.register_uri(
        requests_mock.ANY,
        requests_mock.ANY,
        content=BODY,
        status_code=status,
        headers={"Content-Type": f"application/json; charset={charset}"},
    )


READS: dict[str, Callable[[], object]] = {
    "json": lambda: api.json(api.get("/cities/1/")),
    "load": lambda: City(id=1).name,
    "filter": lambda: list(City.filter()),
    "get_page": lambda: City.get_page(),
}


def test_the_codecs_that_decode_to_no_text_are_found() -> None:
    """The codecs that refuse a body include the usual ones."""
    assert {"zip", "base64", "rot13", "undefined"} <= set(NAMES)


@pytest.mark.parametrize("charset", NAMES)
@pytest.mark.parametrize("read", READS.values(), ids=READS.keys())
def test_a_charset_that_is_no_text_encoding_holds_no_json(
    read: Callable[[], object], charset: str
) -> None:
    """Each read raises the error of a body that holds no JSON."""
    with requests_mock.Mocker() as mocker:
        _serve(mocker, charset)
        with pytest.raises(ResponseShapeError, match="does not hold JSON"):
            read()


@pytest.mark.parametrize("charset", NAMES)
def test_a_failed_status_with_such_a_charset_raises_api_error(
    charset: str,
) -> None:
    """The error of the status is raised, not an error of the charset."""
    with requests_mock.Mocker() as mocker:
        _serve(mocker, charset, status=500)
        with pytest.raises(APIError):
            api.get("/cities/1/")


@pytest.mark.parametrize("charset", NAMES)
def test_decoding_such_a_body_raises_value_error_and_nothing_else(
    charset: str,
) -> None:
    """The decoding raises the error that its callers translate."""
    resp = requests.Response()
    resp._content = BODY
    resp.headers["Content-Type"] = f"application/json; charset={charset}"

    with pytest.raises(ValueError, match=r"charset|not valid text"):
        _body_text(resp)
