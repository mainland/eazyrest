"""A creation refuses None for the key before any request."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
import requests_mock

from eazyrest import API, FieldConversionError, JSONObject, json_object

api = API("https://none-key.example/")


@json_object
class Plain(JSONObject):
    """A model with an int key."""

    class_url = "/plain/"
    id: int
    name: str


@json_object
class Maybe(JSONObject):
    """A model whose key is optional."""

    class_url = "/maybe/"
    id: int | None
    name: str


@json_object(field_map={"id": "itemId"})
class Named(JSONObject):
    """A model whose key has another JSON name."""

    class_url = "/named/"
    id: int
    name: str


for model in (Plain, Maybe, Named):
    model.register_api(api)

CASES: dict[str, tuple[type[JSONObject], Callable[[], Any]]] = {
    "id": (Plain, lambda: Plain.create(id=None, name="a")),
    "pk": (Plain, lambda: Plain.create(pk=None)),
    "fields": (Plain, lambda: Plain.create(fields={"id": None})),
    "optional id": (Maybe, lambda: Maybe.create(id=None, name="a")),
    "optional pk": (Maybe, lambda: Maybe.create(pk=None)),
    "json name": (Named, lambda: Named.create(itemId=None)),
    "python name": (Named, lambda: Named.create(id=None)),
    "defaults": (
        Plain,
        lambda: Plain.get_or_create(name="a", defaults={"id": None}),
    ),
    "optional defaults": (
        Maybe,
        lambda: Maybe.get_or_create(name="a", defaults={"pk": None}),
    ),
    "json name defaults": (
        Named,
        lambda: Named.get_or_create(name="a", defaults={"itemId": None}),
    ),
}


@pytest.mark.parametrize("case", CASES)
def test_a_creation_refuses_none_for_the_key(case: str) -> None:
    """The error names the key field and says to leave the key out."""
    model, call = CASES[case]
    with requests_mock.Mocker() as mocker:
        mocker.get(requests_mock.ANY, json=[])
        mocker.post(requests_mock.ANY, json={"id": 1, "itemId": 1})
        with pytest.raises(
            FieldConversionError, match=rf"{model.__name__}\.id"
        ) as caught:
            call()
        assert "leave the key out" in str(caught.value).lower()
        assert mocker.call_count == 0


def test_a_creation_without_the_key_lets_the_server_assign_it() -> None:
    """Leaving the key out posts the other fields."""
    with requests_mock.Mocker() as mocker:
        mocker.post(
            "https://none-key.example/maybe/", json={"id": 4, "name": "a"}
        )
        created = Maybe.create(name="a")
    assert created.pk == 4
    assert mocker.request_history[0].json() == {"name": "a"}
