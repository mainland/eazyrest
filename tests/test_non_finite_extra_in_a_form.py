"""An extra that JSON cannot write is refused in a form as in a JSON body.

A float that is not finite, and an integer of more digits than Python writes as
text, raise ``ValueError`` before any request, whether the extra is sent in a
JSON body or as a field of a form that also uploads files. The message names
the value as ``[key]``, and an item of a list as ``[key][2]``.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from typing import Any

import pytest
import requests_mock

from eazyrest import API, JSONObject, json_object

BASE = "https://form-extras.example/"


@json_object
class Item(JSONObject):
    """A model that is created with extras."""

    class_url = "/items/"
    id: int


Item.register_api(API(BASE))

FILES: dict[str, dict[str, Any] | None] = {
    "no files": None,
    "files": {"upload": b"x"},
}
CREATIONS: dict[str, Callable[[Any, Any], Any]] = {
    "create": lambda extra, files: Item.create(extra=extra, files=files),
    "create by fields": lambda extra, files: Item.create(
        fields={"extra": extra}, files=files
    ),
}
NO_FILES = {
    "get_or_create defaults": lambda extra: Item.get_or_create(
        id=1, defaults={"extra": extra}
    ),
}
REFUSED: dict[str, Any] = {
    "nan": math.nan,
    "inf": math.inf,
    "negative inf": -math.inf,
    "a list that holds nan": [1, math.nan],
    "an integer of too many digits": 10**4300,
}
ACCEPTED: dict[str, Any] = {
    "a float": 1.5,
    "zero": 0.0,
    "an integer of the most digits": 10**4299,
    "a list of floats": [1.5, 2.5],
}


@pytest.mark.parametrize("files", FILES.values(), ids=FILES)
@pytest.mark.parametrize("creation", CREATIONS.values(), ids=CREATIONS)
@pytest.mark.parametrize("extra", REFUSED.values(), ids=REFUSED)
def test_an_extra_that_json_cannot_write_is_refused_before_any_request(
    extra: Any, creation: Callable[[Any, Any], Any], files: Any
) -> None:
    """The error is a ``ValueError`` that names the value as ``[key]``."""
    with requests_mock.Mocker() as mocker:
        mocker.get(BASE + "items/", json=[])
        mocker.post(BASE + "items/", json={"id": 1})
        with pytest.raises(ValueError, match=r"\[key\]"):
            creation(extra, files)
        assert [
            r.method for r in mocker.request_history if r.method == "POST"
        ] == []


@pytest.mark.parametrize("files", FILES.values(), ids=FILES)
@pytest.mark.parametrize("creation", CREATIONS.values(), ids=CREATIONS)
@pytest.mark.parametrize("extra", ACCEPTED.values(), ids=ACCEPTED)
def test_an_extra_that_json_can_write_is_sent(
    extra: Any, creation: Callable[[Any, Any], Any], files: Any
) -> None:
    """The bound is the same with and without files."""
    with requests_mock.Mocker() as mocker:
        mocker.get(BASE + "items/", json=[])
        mocker.post(BASE + "items/", json={"id": 1})
        creation(extra, files)
        assert mocker.last_request.method == "POST"


@pytest.mark.parametrize("extra", REFUSED.values(), ids=REFUSED)
def test_get_or_create_refuses_an_extra_that_json_cannot_write(
    extra: Any,
) -> None:
    """Its creation is a JSON body, so the rule holds there too."""
    with requests_mock.Mocker() as mocker:
        mocker.get(BASE + "items/", json=[])
        mocker.post(BASE + "items/", json={"id": 1})
        with pytest.raises(ValueError, match=r"\[key\]"):
            NO_FILES["get_or_create defaults"](extra)
        assert mocker.call_count == 0


ITEMS = {
    "a float that is not finite": [1.0, 2.0, math.nan],
    "an integer of too many digits": [1, 2, 10**4300],
}


@pytest.mark.parametrize("creation", CREATIONS.values(), ids=CREATIONS)
@pytest.mark.parametrize("extra", ITEMS.values(), ids=ITEMS)
def test_a_form_names_the_place_of_a_refused_list_item_as_a_body_does(
    extra: Any, creation: Callable[[Any, Any], Any]
) -> None:
    """The third item is ``[key][2]``, with or without files."""
    messages = []
    for files in FILES.values():
        with requests_mock.Mocker() as mocker:
            mocker.post(BASE + "items/", json={"id": 1})
            with pytest.raises(ValueError, match=r"\[key\]\[2\]") as caught:
                creation(extra, files)
            assert mocker.call_count == 0
        messages.append(str(caught.value))

    assert messages[0] == messages[1]
