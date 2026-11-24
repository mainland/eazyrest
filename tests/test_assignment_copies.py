"""An assignment keeps a copy of a value that encodes as itself.

A list assigned to a field that keeps any JSON encodes as the caller's own
list. The queued update must not follow a change that the caller makes to the
list after the assignment, for a field and for a write-only one. A hook of
``merge_response()`` that changes the update that it is given and then raises
leaves the update pending as it was.
"""

from __future__ import annotations

from typing import Any

import pytest
import requests_mock

from eazyrest import API, JSONObject, json_object

BASE = "https://assignment-copies.example/"
api = API(BASE)


@json_object(write_only={"hidden"})
class Note(JSONObject):
    """A model with a field that keeps its JSON, and a write-only one."""

    class_url = "/notes/"
    id: int
    title: str
    words: Any
    hidden: Any

    def merge_response(
        self, resp: Any, current: Any, sent: Any, **kwargs: Any
    ) -> dict[str, Any]:
        """Change the update that the hook is given, and refuse a title."""
        for value in sent.values():
            if isinstance(value, list):
                value.append("changed by the hook")
        if sent.get("title") == "boom":
            raise RuntimeError("the hook fails")
        return {**current, **sent}


Note.register_api(api)


def note() -> Note:
    """Return a note that has read its fields."""
    return Note(json={"id": 1, "title": "t", "words": ["a"]})


@pytest.mark.parametrize("field", ["words", "hidden"])
def test_a_list_changed_after_its_assignment_is_not_what_save_sends(
    field: str,
) -> None:
    """The update holds the list as it was assigned."""
    obj = note()
    value = ["x", "y"]
    setattr(obj, field, value)
    value.append("late")
    with requests_mock.Mocker() as mocker:
        mocker.patch(BASE + "notes/1/", json={})
        obj.save()
        assert mocker.last_request.json() == {field: ["x", "y"]}


@pytest.mark.parametrize("field", ["words", "hidden"])
def test_a_list_changed_after_an_eager_assignment_is_not_sent_again(
    field: str,
) -> None:
    """The write that an eager assignment sends holds the list as assigned."""
    obj = Note(
        json={"id": 1, "title": "t", "words": ["a"]}, write_mode="eager"
    )
    value = ["x", "y"]
    with requests_mock.Mocker() as mocker:
        mocker.patch(BASE + "notes/1/", json={})
        setattr(obj, field, value)
        value.append("late")
        assert mocker.last_request.json() == {field: ["x", "y"]}


@pytest.mark.parametrize("field", ["words", "hidden"])
def test_a_hook_that_changes_the_update_and_raises_changes_nothing(
    field: str,
) -> None:
    """The pending update is as it was assigned, and the next save sends it."""
    obj = note()
    setattr(obj, field, ["x", "y"])
    obj.title = "boom"
    with requests_mock.Mocker() as mocker:
        mocker.patch(BASE + "notes/1/", json={})
        with pytest.raises(RuntimeError):
            obj.save()
        assert obj._pending_updates[field] == ["x", "y"]
        obj.title = "fine"
        obj.save()
        assert mocker.last_request.json()[field] == ["x", "y"]
