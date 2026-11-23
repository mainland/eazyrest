"""``merge_response()`` is given copies of the object's JSON and the update.

An override may change what it is given, as one that sorts a list that the
server keeps sorted does. The object's JSON and the field's cached value must
still agree after the write, and a hook that changes its arguments and then
raises leaves the object as it was.
"""

from __future__ import annotations

from typing import Any

import pytest
import requests_mock

from eazyrest import API, JSONObject, json_object

BASE = "https://merge-copies.example/"
api = API(BASE)


@json_object
class Note(JSONObject):
    """A model whose server keeps the words sorted."""

    class_url = "/notes/"
    id: int
    title: str
    words: list[str]

    def merge_response(
        self,
        resp: Any,
        current: Any,
        sent: Any,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Sort the words of the current JSON in place."""
        merged = {**current, **sent}
        current["words"].sort()
        sent_words = sent.get("words")
        if sent_words is not None:
            sent_words.sort()
        merged["words"] = sorted(merged["words"])
        if merged["title"] == "boom":
            raise RuntimeError("the hook fails")
        return merged


Note.register_api(api)


def note(mode: Any = "lazy") -> Note:
    """Return a note whose words are not sorted yet."""
    return Note(
        json={"id": 1, "title": "t", "words": ["b", "a"]}, write_mode=mode
    )


def test_a_save_leaves_the_json_and_the_field_in_agreement() -> None:
    """A hook that sorts the current JSON in place does not change it."""
    obj = note()
    assert obj.words == ["b", "a"]
    obj.title = "x"
    with requests_mock.Mocker() as mocker:
        mocker.patch(BASE + "notes/1/", status_code=204)
        obj.save()
    assert obj.json["words"] == ["a", "b"]
    assert obj.words == ["a", "b"]


def test_an_eager_write_leaves_the_json_and_the_field_in_agreement() -> None:
    """The eager write gives the hook copies too."""
    obj = note("eager")
    assert obj.words == ["b", "a"]
    with requests_mock.Mocker() as mocker:
        mocker.patch(BASE + "notes/1/", status_code=204)
        obj.title = "x"
    assert obj.json["words"] == ["a", "b"]
    assert obj.words == ["a", "b"]


def test_a_full_save_leaves_the_json_and_the_field_in_agreement() -> None:
    """The ``PUT`` gives the hook copies too."""
    obj = note()
    assert obj.words == ["b", "a"]
    with requests_mock.Mocker() as mocker:
        mocker.put(BASE + "notes/1/", status_code=204)
        obj.save(full=True)
    assert obj.json["words"] == ["a", "b"]
    assert obj.words == ["a", "b"]


def test_a_hook_that_changes_its_arguments_and_raises_changes_nothing() -> (
    None
):
    """The object's JSON keeps its words as they were."""
    obj = note()
    obj.title = "boom"
    with requests_mock.Mocker() as mocker:
        mocker.patch(BASE + "notes/1/", status_code=204)
        with pytest.raises(RuntimeError):
            obj.save()
    assert obj.json["words"] == ["b", "a"]
