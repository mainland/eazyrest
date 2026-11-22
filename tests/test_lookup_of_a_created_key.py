"""The lookup of get_or_create() spells the key as the creation does."""

from __future__ import annotations

import uuid
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest
import requests_mock

from eazyrest import API, JSONObject, json_object

api = API("https://lookup.example/")
LOWER = "abcdef12-1234-5678-1234-567812345abc"
UPPER = LOWER.upper()
COMPACT = LOWER.replace("-", "").upper()


@json_object(field_map={"id": "docId"}, write_map={"id": "doc_id"})
class Doc(JSONObject):
    """A UUID key that reads as docId and writes as doc_id."""

    class_url = "/docs/"
    id: uuid.UUID
    name: str


Doc.register_api(api)


def run(call: Any) -> tuple[dict[str, list[str]], Any]:
    """Return the lookup's query and the POST body of one call."""
    with requests_mock.Mocker() as mocker:
        mocker.get("https://lookup.example/docs/", json=[])
        mocker.post(
            "https://lookup.example/docs/", json={"docId": LOWER, "name": "a"}
        )
        call()
        get, post = mocker.request_history
    return parse_qs(urlparse(get.url).query), post.json()


@pytest.mark.parametrize("spelling", [UPPER, COMPACT])
@pytest.mark.parametrize("name", ["id", "pk", "docId", "doc_id"])
def test_every_name_of_the_key_looks_up_and_creates_the_one_spelling(
    name: str, spelling: str
) -> None:
    """The lookup and the POST hold the key as it is built."""
    query, body = run(
        lambda: Doc.get_or_create(params={name: spelling, "name": "a"})
    )
    assert query["docId" if name == "id" else name] == [LOWER]
    assert body == {"doc_id": LOWER, "name": "a"}


def test_the_key_in_params_is_looked_up_as_built() -> None:
    """A name in params is a name given to the lookup."""
    query, _ = run(lambda: Doc.get_or_create(params={"docId": UPPER}))
    assert query["docId"] == [LOWER]


def test_filter_sends_the_key_under_its_json_name_as_given() -> None:
    """An ordinary query parameter is sent as it is."""
    with requests_mock.Mocker() as mocker:
        mocker.get("https://lookup.example/docs/", json=[])
        list(Doc.filter(docId=UPPER))
        list(Doc.filter(doc_id=UPPER))
    sent = [parse_qs(urlparse(r.url).query) for r in mocker.request_history]
    assert sent == [{"docId": [UPPER]}, {"doc_id": [UPPER]}]
