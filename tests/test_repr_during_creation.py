"""The object that a creation's hook reads shows no key, as ``...``."""

from __future__ import annotations

from typing import Any

import requests_mock

from eazyrest import API, JSONObject, json_object

api = API("https://repr.example/")
SEEN: list[str] = []


@json_object
class Note(JSONObject):
    """A model whose hook records the representation of its object."""

    class_url = "/notes/"
    id: int
    text: str

    def merge_response(  # type: ignore[no-untyped-def]
        self, resp, current, sent, **kwargs
    ):
        """Record ``repr(self)``, then merge."""
        SEEN.append(repr(self))
        return super().merge_response(resp, current, sent, **kwargs)


Note.register_api(api)


def test_the_hook_object_shows_three_dots_for_its_key() -> None:
    """No key is known yet, so none is shown, and none is asked for."""
    SEEN.clear()
    with requests_mock.Mocker() as mocker:
        mocker.post("https://repr.example/notes/", json={"id": 5, "text": "t"})
        note: Any = Note.create(text="t")
    assert SEEN == ["<Note id=...>"]
    assert repr(note) == "<Note id=5>"
