"""A UUID key is read from four spellings and no other."""

from __future__ import annotations

import uuid
from typing import Any
from urllib.parse import unquote, urlsplit

import pytest
import requests_mock

from eazyrest import API, FieldConversionError, JSONObject, json_object

api = API("https://uuid.example/")
TARGET = uuid.UUID("abcdef12-1234-5678-1234-567812345abc")
CANONICAL = str(TARGET)


@json_object
class Doc(JSONObject):
    """A model with a UUID key."""

    class_url = "/docs/"
    id: uuid.UUID


@json_object
class Holder(JSONObject):
    """A model that relates to documents by key."""

    class_url = "/holders/"
    id: int
    doc: Doc | None
    docs: list[Doc]


Doc.register_api(api)
Holder.register_api(api)

ADMITTED = {
    "hyphenated": CANONICAL,
    "hyphenated_upper": CANONICAL.upper(),
    "compact": TARGET.hex,
    "compact_upper": TARGET.hex.upper(),
    "braced": "{" + CANONICAL + "}",
    "braced_upper": "{" + CANONICAL.upper() + "}",
    "urn": "urn:uuid:" + CANONICAL,
    "urn_upper_digits": "urn:uuid:" + CANONICAL.upper(),
}
HEX = TARGET.hex
REFUSED = {
    "hex_prefix": "0x" + HEX[2:],
    "plus_sign": "+" + HEX[1:],
    "minus_sign": "-" + HEX[1:],
    "underscore": HEX[:15] + "_" + HEX[16:],
    "hyphens_elsewhere": "-".join(HEX[i : i + 4] for i in range(0, 32, 4)),
    "leading_space": " " + HEX[1:],
    "trailing_space": HEX[:-1] + " ",
    "trailing_newline": HEX[:-1] + "\n",
    "fullwidth_digit": HEX[:-1] + chr(0xFF17),
    "arabic_indic_digit": HEX[:-1] + chr(0x0667),
    "urn_inside": HEX[:6] + "urn:" + HEX[10:],
    "braced_compact": "{" + HEX + "}",
    "one_brace": "{" + CANONICAL,
    "uppercase_urn_prefix": "URN:UUID:" + CANONICAL,
    "urn_without_hyphens": "urn:uuid:" + HEX,
    "empty": "",
}


@pytest.mark.parametrize("text", ADMITTED.values(), ids=ADMITTED.keys())
def test_a_uuid_key_reads_each_of_four_spellings(text: str) -> None:
    """Every route reads the spelling as the one UUID."""
    assert Doc(id=text).pk == TARGET
    assert Doc(json={"id": text}).pk == TARGET
    assert Doc(json={"id": text}).url == f"/docs/{CANONICAL}/"
    holder = Holder(json={"id": 1, "doc": text, "docs": [text]})
    assert holder.doc == Doc(id=TARGET)
    assert holder.docs == [Doc(id=TARGET)]
    with requests_mock.Mocker() as mocker:
        mocker.get(requests_mock.ANY, json=[])
        list(Doc.filter(id=text))
    assert unquote(urlsplit(mocker.request_history[0].url).query) == (
        f"id={CANONICAL}"
    )


def _refused(call: Any, *named: str) -> None:
    """Assert that a call refuses its value before any request."""
    with requests_mock.Mocker() as mocker:
        mocker.get(requests_mock.ANY, json=[])
        mocker.post(requests_mock.ANY, json={"id": 1})
        with pytest.raises(FieldConversionError) as caught:
            call()
        assert mocker.call_count == 0
    for name in named:
        assert name in str(caught.value)


@pytest.mark.parametrize("text", REFUSED.values(), ids=REFUSED.keys())
def test_a_uuid_key_refuses_every_other_spelling(text: str) -> None:
    """Strings that uuid.UUID reads are refused unless they are one of four."""
    _refused(lambda: Doc(id=text), "Doc.id")
    _refused(lambda: Doc(json={"id": text}), "Doc.id")
    _refused(lambda: list(Doc.filter(id=text)), "Doc.id")
    _refused(lambda: list(Doc.filter(pk__in=[text])), "Doc.id")
    _refused(lambda: Holder.create(doc=text), "Holder.doc")
    _refused(lambda: list(Holder.filter(doc=text)), "Holder.doc")
    holder = Holder(json={"id": 1, "doc": text, "docs": [text]})
    _refused(lambda: holder.doc, "Holder.doc")
    _refused(lambda: holder.docs, "Holder.docs")
