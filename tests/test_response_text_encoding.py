"""A JSON body is decoded strictly, as UTF-8, UTF-16, or UTF-32.

A body that does not decode must not read as other text. requests replaces each
bad byte with U+FFFD, or guesses an encoding for a body that declares none, so
that the replacement character would be written back by ``save(full=True)``.
"""

from __future__ import annotations

from typing import Any

import pytest
import requests_mock

from eazyrest import API, JSONObject, ResponseShapeError, json_object

BASE = "https://encodings.example/"
api = API(BASE)


@json_object
class City(JSONObject):
    """A model whose name holds non-ASCII text."""

    class_url = "/cities/"
    id: int
    name: str


City.register_api(api)

LATIN_1 = b'{"id": 1, "name": "Z\xfcrich"}'
UTF_8 = '{"id": 1, "name": "Zürich"}'.encode()

BAD_BODIES = {
    "Latin-1 bytes": LATIN_1,
    "a cut UTF-8 sequence": UTF_8[:-3] + b"\xc3",
}
TYPES = {
    "json": "application/json",
    "json with utf-8": "application/json; charset=utf-8",
    "json with latin-1": "application/json; charset=iso-8859-1",
    "text with latin-1": "text/plain; charset=latin-1",
    "a structured type": "application/vnd.api+json",
    "no type": None,
    "text": "text/plain",
}


def serve(mocker: Any, body: bytes, content_type: str | None) -> None:
    """Answer every request with a body and a content type."""
    headers = {} if content_type is None else {"Content-Type": content_type}
    mocker.register_uri(
        requests_mock.ANY,
        requests_mock.ANY,
        content=body,
        headers=headers,
    )


@pytest.mark.parametrize("content_type", TYPES.values(), ids=TYPES.keys())
@pytest.mark.parametrize("body", BAD_BODIES.values(), ids=BAD_BODIES.keys())
def test_a_body_that_is_not_valid_utf_8_is_no_json(
    body: bytes, content_type: str | None
) -> None:
    """The error is the one of a body that holds no JSON, with the response."""
    with requests_mock.Mocker() as mocker:
        serve(mocker, body, content_type)
        with pytest.raises(ResponseShapeError, match="does not hold JSON"):
            api.json(api.get("/cities/1/"))
        with pytest.raises(ResponseShapeError, match="does not hold JSON"):
            City(id=1).name  # noqa: B018
        with pytest.raises(ResponseShapeError, match="does not hold JSON"):
            list(City.filter())


def test_a_body_that_does_not_decode_is_never_written_back() -> None:
    """``save(full=True)`` has no replacement character to write."""
    with requests_mock.Mocker() as mocker:
        serve(mocker, UTF_8, "application/json")
        city = City(id=1)
        assert city.name == "Zürich"
        serve(mocker, LATIN_1, "application/json")
        city.refresh()
        with pytest.raises(ResponseShapeError):
            city.name  # noqa: B018
        assert all(
            b"\xef\xbf\xbd" not in (r.body or b"")
            for r in mocker.request_history
        )


GOOD = {
    "UTF-8 without a type": (UTF_8, None),
    "UTF-8 with a BOM": (b"\xef\xbb\xbf" + UTF_8, "application/vnd.api+json"),
    "UTF-8 declared": (UTF_8, "application/json; charset=UTF-8"),
    "UTF-16 declared": (
        UTF_8.decode().encode("utf-16"),
        'application/json; charset="utf-16"',
    ),
}


@pytest.mark.parametrize(("body", "content_type"), GOOD.values(), ids=GOOD)
def test_a_body_in_its_declared_charset_reads(
    body: bytes, content_type: str | None
) -> None:
    """A body that decodes in UTF-8, or in UTF-16 that it names, reads."""
    with requests_mock.Mocker() as mocker:
        serve(mocker, body, content_type)
        assert api.json(api.get("/cities/1/"))["name"] == "Zürich"
        assert City(id=1).name == "Zürich"
