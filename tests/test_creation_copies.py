"""A creation and a load keep what they promise about the JSON they hold."""

from __future__ import annotations

import uuid
from typing import Any

import requests_mock

from eazyrest import API, JSONObject, json_object

api = API("https://copies.example/")
SPELLING = "ABCDEF12-1234-5678-1234-567812345ABC"


@json_object
class Basket(JSONObject):
    """A model whose field keeps any JSON, and whose hook changes its input."""

    class_url = "/baskets/"
    id: int
    items: Any

    def merge_response(  # type: ignore[no-untyped-def]
        self, resp, current, sent, **kwargs
    ):
        """Change the list that the creation sent, then merge."""
        sent["items"].append("added by the hook")
        return super().merge_response(resp, current, sent, **kwargs)


@json_object
class Doc(JSONObject):
    """A model keyed by a UUID."""

    class_url = "/docs/"
    id: uuid.UUID
    title: str


Basket.register_api(api)
Doc.register_api(api)


def test_a_hook_that_changes_what_it_is_sent_leaves_the_callers_list() -> None:
    """The caller's own list is not the one that the hook gets."""
    mine = ["a"]
    with requests_mock.Mocker() as mocker:
        mocker.post("https://copies.example/baskets/", json={"id": 1})
        basket = Basket.create(items=mine)
    assert mine == ["a"]
    assert basket.items == ["a", "added by the hook"]
    mine.append("b")
    assert basket.items == ["a", "added by the hook"]


def test_a_load_keeps_the_spelling_of_the_key_that_the_server_gave() -> None:
    """A created object's JSON keeps the key as the response spelled it."""
    with requests_mock.Mocker() as mocker:
        mocker.post("https://copies.example/docs/", json={"id": SPELLING})
        doc = Doc.create()
        assert doc.json == {"id": SPELLING}
        mocker.get(
            "https://copies.example/docs/" + SPELLING.lower() + "/",
            json={"title": "t"},
        )
        assert doc.title == "t"
    assert doc.json == {"id": SPELLING, "title": "t"}
    assert doc.pk == uuid.UUID(SPELLING)
