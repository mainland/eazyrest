"""A set given to a sequence relation's parameter is sent in sorted order."""

from __future__ import annotations

import subprocess
import sys
from typing import Any
from urllib.parse import unquote, urlsplit

import requests_mock

from eazyrest import API, JSONObject, json_object

api = API("https://sorted.example/")


@json_object(pk="slug")
class Tag(JSONObject):
    """A model with a text key."""

    class_url = "/tags/"
    slug: str


@json_object
class Item(JSONObject):
    """A model with a list of related objects and a plain list."""

    class_url = "/items/"
    id: int
    tags: list[Tag]
    scores: list[int]


Tag.register_api(api)
Item.register_api(api)


def query(**params: Any) -> str:
    """Run a filter and return the decoded query that it sent."""
    with requests_mock.Mocker() as mocker:
        mocker.get(requests_mock.ANY, json=[])
        list(Item.filter(**params))
    return unquote(urlsplit(mocker.request_history[0].url).query)


def test_a_set_of_objects_is_sent_sorted_by_key() -> None:
    """The order does not depend on the hashes."""
    tags = {Tag(slug=name) for name in "dbcae"}
    assert query(tags=tags) == "&".join(f"tags={name}" for name in "abcde")
    assert query(tags=frozenset(tags)) == query(tags=tags)


def test_a_set_of_plain_values_is_sent_sorted() -> None:
    """A set for a sequence field of plain values is ordered too."""
    assert query(scores={3, 1, 2}) == "scores=1&scores=2&scores=3"


def test_a_list_keeps_its_order() -> None:
    """Only a set, which has no order, is sorted."""
    assert query(scores=[3, 1, 2]) == "scores=3&scores=1&scores=2"


def test_the_url_is_the_same_in_every_process() -> None:
    """Another hash seed does not change the query."""
    code = (
        "import requests_mock\n"
        "from eazyrest import API, JSONObject, json_object\n"
        "@json_object(pk='slug')\n"
        "class Tag(JSONObject):\n"
        "    class_url = '/tags/'\n"
        "    slug: str\n"
        "@json_object\n"
        "class Item(JSONObject):\n"
        "    class_url = '/items/'\n"
        "    id: int\n"
        "    tags: list[Tag]\n"
        "api = API('https://sorted.example/')\n"
        "Tag.register_api(api)\n"
        "Item.register_api(api)\n"
        "with requests_mock.Mocker() as m:\n"
        "    m.get(requests_mock.ANY, json=[])\n"
        "    list(Item.filter(tags={Tag(slug=s) for s in 'dbcae'}))\n"
        "    print(m.request_history[0].url)\n"
    )
    seen = set()
    for seed in ("1", "2", "3", "4"):
        done = subprocess.run(
            [sys.executable, "-c", code],
            env={"PYTHONHASHSEED": seed, "PYTHONPATH": ":".join(sys.path)},
            capture_output=True,
            text=True,
            check=True,
        )
        seen.add(done.stdout.strip())
    assert len(seen) == 1
