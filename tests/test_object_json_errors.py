"""An error of an ``object_json()`` override reaches the caller as it is.

The default hook raises ``DoesNotExist`` and ``MultipleObjectsReturned`` for a
list, which is no object, and a write or a load reads that as a response of the
wrong shape. An override that sends a request or looks an object up raises its
own errors for a payload that is no list, and those are not the response's
shape.
"""

from __future__ import annotations

from typing import Any

import pytest
import requests_mock

from eazyrest import (
    API,
    DoesNotExist,
    JSONObject,
    NotFound,
    ResponseShapeError,
    json_object,
)

BASE = "https://object-json-errors.example/"
api = API(BASE)


@json_object
class Linked(JSONObject):
    """A model whose envelope names a resource that the hook fetches."""

    class_url = "/linked/"
    id: int
    name: str

    @classmethod
    def object_json(cls, payload: Any, **kwargs: Any) -> Any:
        """Follow the envelope's reference, which may fail."""
        if isinstance(payload, dict) and payload.get("missing"):
            raise DoesNotExist("the referenced object does not exist")
        if isinstance(payload, dict) and payload.get("follow"):
            return api.json(api.get(payload["follow"]))
        return payload


Linked.register_api(api)

MISSING = {"id": 1, "name": "a", "missing": True}
FOLLOWED = {"id": 1, "name": "a", "follow": "/referenced/"}


def _load() -> None:
    _ = Linked(id=1).name


def _save() -> None:
    obj = Linked(json={"id": 1, "name": "a"})
    obj.name = "b"
    obj.save()


def _create() -> None:
    Linked.create(name="a")


ENTRY_POINTS = {"load": _load, "save": _save, "create": _create}


@pytest.mark.parametrize("entry", sorted(ENTRY_POINTS))
@pytest.mark.parametrize(
    ("body", "error"), [(MISSING, DoesNotExist), (FOLLOWED, NotFound)]
)
def test_an_error_of_the_hook_for_an_object_payload_is_not_a_shape_error(
    entry: str, body: dict[str, Any], error: type[Exception]
) -> None:
    """An error that the hook raises for a mapping reaches the caller."""
    with requests_mock.Mocker() as mocker:
        mocker.get(BASE + "linked/1/", json=body)
        mocker.patch(BASE + "linked/1/", json=body)
        mocker.post(BASE + "linked/", json=body)
        mocker.get(BASE + "referenced/", status_code=404)
        with pytest.raises(error) as info:
            ENTRY_POINTS[entry]()
    assert not isinstance(info.value, ResponseShapeError)


@pytest.mark.parametrize("entry", sorted(ENTRY_POINTS))
@pytest.mark.parametrize("body", [[], [{"id": 1}, {"id": 2}]])
def test_a_list_that_the_default_hook_refuses_is_a_shape_error(
    entry: str, body: list[Any]
) -> None:
    """An empty list or a list of two objects is still the response's shape."""
    with requests_mock.Mocker() as mocker:
        mocker.get(BASE + "linked/1/", json=body)
        mocker.patch(BASE + "linked/1/", json=body)
        mocker.post(BASE + "linked/", json=body)
        with pytest.raises(ResponseShapeError) as info:
            ENTRY_POINTS[entry]()
    assert info.value.response is not None
