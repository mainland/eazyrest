"""When and with what error each refusal that the guide states is raised."""

from __future__ import annotations

import pickle
from typing import TYPE_CHECKING, Any

import pytest
import requests_mock
from typing_extensions import TypedDict

from eazyrest import (
    API,
    FieldConversionError,
    JSONObject,
    ResponseShapeError,
    json_object,
)

if TYPE_CHECKING:
    from nowhere import NotDefined  # type: ignore[import-not-found]


api = API("https://timing.example/")


@json_object
class Machine(JSONObject):
    """A model whose summaries lack a field."""

    class_url = "/machines/"
    id: int
    name: str
    note: str


Machine.register_api(api)


def test_assigning_a_field_that_the_json_lacks_raises_attribute_error() -> (
    None
):
    """A lazy object loads before it raises, one built from JSON does not."""
    summary = Machine(json={"id": 1, "name": "a"})
    with pytest.raises(AttributeError, match="note"):
        summary.note = "x"
    lazy = Machine(id=2)
    with requests_mock.Mocker() as mocker:
        mocker.get(
            "https://timing.example/machines/2/", json={"id": 2, "name": "b"}
        )
        with pytest.raises(AttributeError, match="note"):
            lazy.note = "x"
        assert mocker.call_count == 1


def test_a_pickle_of_an_object_given_an_api_holds_its_credentials() -> None:
    """The session's headers go into the pickle, so it is a secret."""
    private = API("https://private.example/")
    private.session.headers["Authorization"] = "Bearer secret-token"
    machine = Machine(json={"id": 1, "name": "a", "note": "n"}, api=private)
    assert b"secret-token" in pickle.dumps(machine)
    assert b"secret-token" not in pickle.dumps(
        Machine(json={"id": 1, "name": "a", "note": "n"})
    )


def test_the_api_sends_no_head_request() -> None:
    """There is no method for one, so the redirect policy names only a GET."""
    assert not hasattr(API, "head")
    assert not hasattr(API, "request")


@json_object
class Late(JSONObject):
    """A model whose key type is defined after the class."""

    class_url = "/late/"
    id: LateKey
    name: str


Late.register_api(api)
LateKey = float


def test_a_refused_key_type_raises_as_the_first_item_is_built() -> None:
    """An empty collection raises nothing, and a lookup of the key raises."""
    with requests_mock.Mocker() as mocker:
        mocker.get("https://timing.example/late/", json=[])
        assert list(Late.filter(name="a")) == []
        assert Late.get_page().items == []
        with pytest.raises(TypeError, match="primary key"):
            list(Late.filter(id=1))
        assert mocker.call_count == 2
        mocker.get(
            "https://timing.example/late/", json=[{"id": 1.5, "name": "a"}]
        )
        for call in (
            lambda: list(Late.filter()),
            lambda: list(Late.all()),
            lambda: Late.get(name="a"),
            lambda: Late.get_page(),
        ):
            with pytest.raises(TypeError, match="primary key"):
                call()


class Plain:
    """A class that nothing converts."""


class Holds(TypedDict):
    """A structure whose member is a class that nothing converts."""

    member: Plain


class Keys(TypedDict):
    """A structure whose member is a mapping with keys that are no text."""

    member: dict[int, str]


class Names(TypedDict):
    """A structure whose member names what is not defined yet."""

    member: NotDefined


def declare(annotation: Any) -> Any:
    """Decorate a model with a field of the given annotation."""
    model: Any = type(
        "Holder",
        (JSONObject,),
        {
            "class_url": "/holders/",
            "__annotations__": {"id": int, "value": annotation},
        },
    )
    return json_object(model)


def test_a_member_that_nothing_converts_is_refused_at_decoration() -> None:
    """A member that is a class with no conversion stops the class."""
    with pytest.raises(TypeError, match=r"Holder\.value"):
        declare(Holds)


@pytest.mark.parametrize("annotation", [Keys, Names], ids=["keys", "name"])
def test_another_member_is_refused_at_first_use(annotation: Any) -> None:
    """The class decorates, and reading the field raises."""
    holder = declare(annotation)(json={"id": 1, "value": {"member": {}}})
    with pytest.raises(FieldConversionError, match=r"Holder\.value"):
        _ = holder.value


def test_a_response_shape_error_may_have_no_response() -> None:
    """An error from decoded JSON in a hook holds none."""
    assert ResponseShapeError("not one object").response is None
