"""Assigning an equal related object sends nothing, in any spelling."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
import requests_mock

from eazyrest import API, JSONObject, json_object

api = API("https://assign.example/")
TARGET = uuid.UUID("abcdef12-1234-5678-1234-567812345abc")
OTHER = uuid.UUID("abcdef12-1234-5678-1234-567812345abd")
LOWER = str(TARGET)
UPPER = LOWER.upper()
COMPACT = TARGET.hex.upper()


@json_object
class Doc(JSONObject):
    """A related model with a UUID key."""

    class_url = "/docs/"
    id: uuid.UUID


@json_object
class Holder(JSONObject):
    """A model that relates to documents by key."""

    class_url = "/holders/"
    id: int
    doc: Doc | None
    docs: list[Doc]
    docset: set[Doc]


Doc.register_api(api)
Holder.register_api(api)


def holder(spelling: str, mode: Any = "lazy") -> Holder:
    """Return a holder whose server spells each key as given."""
    return Holder(
        json={
            "id": 1,
            "doc": spelling,
            "docs": [spelling],
            "docset": [spelling],
        },
        write_mode=mode,
    )


def sent(assign: Any, mode: Any) -> list[Any]:
    """Return the bodies of the writes that an assignment and a save send."""
    with requests_mock.Mocker() as mocker:
        mocker.patch(requests_mock.ANY, json={})
        assign(mode)
        return [r.json() for r in mocker.request_history]


ASSIGNMENTS = {
    "doc": lambda h, key: setattr(h, "doc", Doc(id=key)),
    "docs": lambda h, key: setattr(h, "docs", [Doc(id=key)]),
    "docset": lambda h, key: setattr(h, "docset", {Doc(id=key)}),
}


@pytest.mark.parametrize("mode", ["lazy", "eager"])
@pytest.mark.parametrize("spelling", [UPPER, COMPACT, LOWER])
@pytest.mark.parametrize("field", ASSIGNMENTS)
def test_an_equal_object_is_not_a_change_whatever_the_server_spelled(
    field: str, spelling: str, mode: str
) -> None:
    """The key is compared as it is built."""
    obj = holder(spelling, mode)

    def assign(_: Any) -> None:
        ASSIGNMENTS[field](obj, TARGET)
        obj.save()

    assert sent(assign, mode) == []


@pytest.mark.parametrize("mode", ["lazy", "eager"])
@pytest.mark.parametrize("field", ASSIGNMENTS)
def test_a_different_key_is_still_sent(field: str, mode: str) -> None:
    """Only the same key sends nothing."""
    obj = holder(UPPER, mode)

    def assign(_: Any) -> None:
        ASSIGNMENTS[field](obj, OTHER)
        obj.save()

    expected = str(OTHER)
    body = sent(assign, mode)
    assert len(body) == 1
    value = body[0][field]
    assert value in (expected, [expected])


def test_reassigning_the_field_that_was_read_sends_nothing() -> None:
    """The object that the field returns equals the stored key."""
    obj = holder(UPPER)

    def assign(_: Any) -> None:
        obj.doc = obj.doc
        obj.docs = obj.docs
        obj.docset = obj.docset
        obj.save()

    assert sent(assign, "lazy") == []
