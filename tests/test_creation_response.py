"""A creation refuses a response that it cannot trust, and holds it."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
import requests_mock

from eazyrest import (
    API,
    DoesNotExist,
    JSONObject,
    MultipleObjectsReturned,
    ResponseShapeError,
    json_object,
)

api = API("https://response.example/")


@json_object
class Item(JSONObject):
    """A model with an integer key."""

    class_url = "/items/"
    id: int
    name: str


Item.register_api(api)

CALLS: dict[str, tuple[Callable[[], Any], int]] = {
    "create": (lambda: Item.create(name="a"), 1),
    "get_or_create": (lambda: Item.get_or_create(name="a"), 2),
}
KEYED: dict[str, tuple[Callable[[], Any], int]] = {
    "create": (lambda: Item.create(id=7, name="a"), 1),
    "get_or_create": (lambda: Item.get_or_create(id=7, name="a"), 2),
}

SHAPES = {
    "a_string": '"created"',
    "a_number": "5",
    "null": "null",
    "an_empty_list": "[]",
    "a_list_of_two": '[{"id": 1}, {"id": 2}]',
    "a_list_of_one_string": '["x"]',
}


def refused(call: Callable[[], Any], body: str, requests: int) -> Any:
    """Return the error that a creation raises for a response body."""
    with requests_mock.Mocker() as mocker:
        mocker.get("https://response.example/items/", json=[])
        mocker.post("https://response.example/items/", text=body)
        with pytest.raises(ResponseShapeError) as caught:
            call()
        assert mocker.call_count == requests
    return caught.value


@pytest.mark.parametrize("which", CALLS)
@pytest.mark.parametrize("body", SHAPES.values(), ids=SHAPES.keys())
def test_a_body_that_is_not_one_object_holds_the_response(
    which: str, body: str
) -> None:
    """A body that is no JSON object raises ResponseShapeError."""
    call, requests = CALLS[which]
    error = refused(call, body, requests)
    assert error.response is not None
    assert error.response.text == body
    assert not isinstance(error, (DoesNotExist, MultipleObjectsReturned))
    assert "Item" in str(error)


@pytest.mark.parametrize("which", CALLS)
def test_a_list_body_says_that_the_resource_may_exist(which: str) -> None:
    """A list is refused as the creation's response, not as a query."""
    call, requests = CALLS[which]
    error = refused(call, "[]", requests)
    assert "may have been created" in str(error)
    assert isinstance(error.__cause__, DoesNotExist)
    error = refused(call, '[{"id": 1}, {"id": 2}]', requests)
    assert isinstance(error.__cause__, MultipleObjectsReturned)


@pytest.mark.parametrize("which", CALLS)
def test_a_body_that_is_not_json_holds_the_response(which: str) -> None:
    """A body that does not decode raises with the response."""
    call, requests = CALLS[which]
    error = refused(call, "<html>", requests)
    assert error.response is not None
    assert error.response.text == "<html>"


@pytest.mark.parametrize("which", CALLS)
def test_a_body_without_a_key_holds_the_response(which: str) -> None:
    """A body that names no key raises, and the resource may exist."""
    call, requests = CALLS[which]
    error = refused(call, '{"name": "a"}', requests)
    assert error.response is not None
    assert "may have been created" in str(error)
    assert "Item.id" in str(error)


@pytest.mark.parametrize("which", KEYED)
@pytest.mark.parametrize("body", ['{"id": 8}', '[{"id": 8, "name": "a"}]'])
def test_a_response_that_names_another_key_than_was_sent_is_refused(
    which: str, body: str
) -> None:
    """The error names both keys and holds the response."""
    call, requests = KEYED[which]
    error = refused(call, body, requests)
    assert error.response is not None
    assert error.response.text == body
    message = str(error)
    assert "8" in message
    assert "7" in message
    assert "contradicts" in message


@pytest.mark.parametrize("which", KEYED)
def test_a_response_without_the_key_takes_the_key_that_was_sent(
    which: str,
) -> None:
    """The key that was sent names the object when the body omits it."""
    call, _ = KEYED[which]
    with requests_mock.Mocker() as mocker:
        mocker.get("https://response.example/items/", json=[])
        mocker.post("https://response.example/items/", json={"name": "b"})
        result = call()
    obj = result[0] if which == "get_or_create" else result
    assert obj.pk == 7
    assert obj.name == "b"


@pytest.mark.parametrize("which", KEYED)
def test_a_response_with_the_key_that_was_sent_is_accepted(which: str) -> None:
    """The same key under a body that holds it is not a refusal."""
    call, _ = KEYED[which]
    with requests_mock.Mocker() as mocker:
        mocker.get("https://response.example/items/", json=[])
        mocker.post(
            "https://response.example/items/", json={"id": 7, "name": "a"}
        )
        result = call()
    assert (result[0] if which == "get_or_create" else result).pk == 7
