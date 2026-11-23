"""The errors of a ``merge_response()`` override reach the caller."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
import requests_mock

from eazyrest import (
    API,
    APIError,
    DoesNotExist,
    JSONObject,
    NotFound,
    ResponseShapeError,
    json_object,
)

api = API("https://merge.example/")
ACTION: list[Callable[[], Any]] = [lambda: None]


@json_object
class Owner(JSONObject):
    """A related model."""

    class_url = "/owners/"
    id: int
    name: str


Owner.register_api(api)


@json_object
class Probing(JSONObject):
    """A model whose override sends a request of its own."""

    class_url = "/probing/"
    id: int
    name: str
    owner: Owner | None

    def merge_response(  # type: ignore[no-untyped-def]
        self, resp, current, sent, **kwargs
    ):
        """Run the action, then merge as the default does."""
        ACTION[0]()
        return super().merge_response(resp, current, sent, **kwargs)


Probing.register_api(api)

ACTIONS: dict[str, Callable[[], Any]] = {
    "a_request_that_answers_404": lambda: api.get("/missing/"),
    "a_query_that_matches_nothing": lambda: Owner.get(id=99),
    "a_lazy_load_of_a_deleted_object": lambda: Owner(id=98).name,
}


def mock(mocker: Any) -> None:
    """Serve the model's writes and the requests that actions send."""
    mocker.get("https://merge.example/missing/", status_code=404, json={})
    mocker.get("https://merge.example/owners/", json=[])
    mocker.get("https://merge.example/owners/98/", status_code=404, json={})
    mocker.patch("https://merge.example/probing/1/", json={"name": "b"})
    mocker.put("https://merge.example/probing/1/", json={"name": "b"})
    mocker.post("https://merge.example/probing/", json={"id": 1})


def lazy_save() -> None:
    """Save a lazy change."""
    probing = Probing(json={"id": 1, "name": "a"})
    probing.name = "b"
    probing.save()


def full_save() -> None:
    """Replace the whole object."""
    Probing(json={"id": 1, "name": "a"}).save(full=True)


def eager_write() -> None:
    """Assign a field of an eager object."""
    probing = Probing(json={"id": 1, "name": "a"})
    probing.set_write_mode("eager")
    probing.name = "b"


def creation() -> None:
    """Create an object."""
    Probing.create(name="b")


WRITES = {
    "save": lazy_save,
    "full_save": full_save,
    "eager_write": eager_write,
    "create": creation,
}


@pytest.mark.parametrize("write", WRITES.values(), ids=WRITES.keys())
@pytest.mark.parametrize("action", ACTIONS.values(), ids=ACTIONS.keys())
def test_a_missing_object_in_an_override_reaches_the_caller(
    write: Callable[[], Any], action: Callable[[], Any]
) -> None:
    """The error is a ``DoesNotExist``, not an error about the response."""
    ACTION[0] = action
    try:
        with requests_mock.Mocker() as mocker:
            mock(mocker)
            with pytest.raises(DoesNotExist) as caught:
                write()
    finally:
        ACTION[0] = lambda: None
    assert not isinstance(caught.value, ResponseShapeError)


@pytest.mark.parametrize("write", WRITES.values(), ids=WRITES.keys())
def test_a_404_in_an_override_is_not_found(
    write: Callable[[], Any],
) -> None:
    """A caller that catches ``NotFound`` or ``APIError`` sees the 404."""
    ACTION[0] = ACTIONS["a_request_that_answers_404"]
    try:
        with requests_mock.Mocker() as mocker:
            mock(mocker)
            with pytest.raises(NotFound) as caught:
                write()
    finally:
        ACTION[0] = lambda: None
    assert isinstance(caught.value, APIError)


@pytest.mark.parametrize("write", WRITES.values(), ids=WRITES.keys())
def test_a_list_response_still_raises_response_shape_error(
    write: Callable[[], Any],
) -> None:
    """The default hook turns a list of two into an error about the write."""
    with requests_mock.Mocker() as mocker:
        mock(mocker)
        body = [{"id": 1}, {"id": 1}]
        mocker.patch("https://merge.example/probing/1/", json=body)
        mocker.put("https://merge.example/probing/1/", json=body)
        mocker.post("https://merge.example/probing/", json=body)
        with pytest.raises(ResponseShapeError) as caught:
            write()
    assert caught.value.response is not None
    assert caught.value.response.json() == body
    assert "not one object" in str(caught.value)
