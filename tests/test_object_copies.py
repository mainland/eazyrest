"""A model object refuses to be copied or pickled."""

from __future__ import annotations

import copy
import dataclasses
import pickle
from collections.abc import Callable
from typing import Any, ClassVar

import pytest
import requests_mock

from eazyrest import API, Conversion, JSONObject, json_object

BASE = "https://copies.example/"
api = API(BASE)


@json_object
class Keyed(JSONObject):
    """A model with a key."""

    class_url = "/keyed/"
    id: int
    name: str


@json_object(write_only={"secret"})
class Account(JSONObject):
    """A model with a write-only field and a list that changes in place."""

    class_url = "/accounts/"
    id: int
    name: str
    tags: list[str]
    secret: str


@json_object
class Keyless(JSONObject):
    """A model without a key."""

    name: str


@json_object
class Member(JSONObject):
    """A model with a set relation to its own model."""

    class_url = "/members/"
    id: int
    friends: set[Member]


@dataclasses.dataclass(frozen=True)
class Code:
    """A frozen dataclass that a conversion decodes to."""

    text: str


@dataclasses.dataclass
class Row(dict[str, Any]):
    """A dataclass that subclasses ``dict``."""

    label: str = ""


class Index(dict[Code, int]):
    """A dict keyed by a frozen dataclass, which a conversion builds."""


@json_object
class Decoded(JSONObject):
    """A model with fields that read as three shapes of decoded value."""

    class_url = "/decoded/"
    conversions: ClassVar[dict[type, Conversion]] = {
        Index: Conversion(
            decode=lambda value, cls: Index(
                {Code(key): n for key, n in value.items()}
            ),
            encode=lambda value, cls: {
                key.text: n for key, n in value.items()
            },
        )
    }

    id: int
    primary: Code
    codes: frozenset[Code]
    by_code: Index
    row: Row


Keyed.register_api(api)
Member.register_api(api)
Account.register_api(api)
Decoded.register_api(api)


def keyed() -> Keyed:
    """Return a keyed object that has read its field."""
    obj = Keyed(json={"id": 3, "name": "a"})
    assert obj.name == "a"
    return obj


def keyless() -> Keyless:
    """Return an object without a key."""
    return Keyless(json={"name": "a"})


def cycle() -> Member:
    """Return an object that holds another that holds it."""
    first = Member(json={"id": 1, "friends": [2]})
    second = Member(json={"id": 2, "friends": [1]})
    first.friends = {second}
    second.friends = {first}
    return first


def given_an_api() -> Keyed:
    """Return an object that was built with ``api=``."""
    return Keyed(json={"id": 3, "name": "a"}, api=API("https://given.test/"))


def with_a_pending_update() -> Keyed:
    """Return an object whose field was assigned and not yet saved."""
    obj = Keyed(json={"id": 3, "name": "a"}, write_mode="lazy")
    obj.name = "b"
    return obj


def with_decoded_values() -> Decoded:
    """Return an object that has read three shapes of value.

    They are a dict keyed by a dataclass, a set that shares a dataclass with a
    field, and a dataclass that is a ``dict``.
    """
    obj = Decoded(
        json={
            "id": 1,
            "primary": {"text": "x"},
            "codes": [{"text": "x"}, {"text": "y"}],
            "by_code": {"x": 1},
            "row": {"label": "a"},
        }
    )
    assert obj.primary == Code("x")
    assert obj.codes == {Code("x"), Code("y")}
    assert obj.by_code == {Code("x"): 1}
    assert obj.row.label == "a"
    return obj


OBJECTS: dict[str, Callable[[], Any]] = {
    "keyed": keyed,
    "keyless": keyless,
    "cycle": cycle,
    "api given": given_an_api,
    "pending update": with_a_pending_update,
    "decoded values": with_decoded_values,
}

ATTEMPTS: dict[str, Callable[[Any], Any]] = {
    "copy.copy()": copy.copy,
    "copy.deepcopy()": copy.deepcopy,
    "list in copy.deepcopy()": lambda obj: copy.deepcopy([obj]),
    "dict in copy.deepcopy()": lambda obj: copy.deepcopy({"a": obj}),
    "set in copy.deepcopy()": lambda obj: copy.deepcopy({obj}),
    "pickle.dumps(), protocol 0": lambda obj: pickle.dumps(obj, 0),
    "pickle.dumps(), protocol 1": lambda obj: pickle.dumps(obj, 1),
    "pickle.dumps(), protocol 2": lambda obj: pickle.dumps(obj, 2),
    "pickle.dumps(), protocol 3": lambda obj: pickle.dumps(obj, 3),
    "pickle.dumps(), protocol 4": lambda obj: pickle.dumps(obj, 4),
    "pickle.dumps(), protocol 5": lambda obj: pickle.dumps(obj, 5),
    "pickle.dumps(), default": pickle.dumps,
    "list in pickle.dumps()": lambda obj: pickle.dumps([obj]),
}


def test_the_table_has_a_row_for_every_pickle_protocol() -> None:
    """A protocol that Python adds later is also tried."""
    protocols = {
        int(name[-1])
        for name in ATTEMPTS
        if name.startswith("pickle.dumps(), protocol")
    }
    assert protocols == set(range(pickle.HIGHEST_PROTOCOL + 1))


@pytest.mark.parametrize("attempt", ATTEMPTS.values(), ids=ATTEMPTS.keys())
@pytest.mark.parametrize("make", OBJECTS.values(), ids=OBJECTS.keys())
def test_an_object_refuses_a_copy_and_a_pickle(
    make: Callable[[], Any], attempt: Callable[[Any], Any]
) -> None:
    """The error names the model and the way to build another object."""
    obj = make()
    name = type(obj).__name__
    with pytest.raises(TypeError) as caught:
        attempt(obj)
    message = str(caught.value)
    assert f"A {name} object cannot be copied or pickled" in message
    assert f"{name}(json=obj.json)" in message
    assert "api=" in message


@pytest.mark.parametrize("make", OBJECTS.values(), ids=OBJECTS.keys())
def test_a_refused_copy_leaves_the_object_as_it_was(
    make: Callable[[], Any],
) -> None:
    """The refusal reads and changes nothing."""
    obj = make()
    before = dict(vars(obj))
    json_before = obj.json
    for attempt in (copy.copy, copy.deepcopy, pickle.dumps):
        with pytest.raises(TypeError):
            attempt(obj)
    assert vars(obj).keys() == before.keys()
    assert obj.json == json_before


def test_an_object_built_from_the_json_is_separate_from_its_original() -> None:
    """``Model(json=obj.json)`` gives the object that a copy used to."""
    original = with_a_pending_update()
    rebuilt = Keyed(json=original.json)
    assert rebuilt == original
    assert rebuilt is not original
    assert rebuilt.name == "b"
    rebuilt.name = "c"
    assert original.name == "b"


def test_an_object_made_without_its_constructor_has_no_key() -> None:
    """No default key stands in for the one that the constructor builds."""
    bare = Keyed.__new__(Keyed)
    with pytest.raises(AttributeError):
        _ = bare.pk
    with pytest.raises(AttributeError):
        _ = hash(bare)


HOLDERS: dict[str, Callable[[Any], Any]] = {
    "list": lambda obj: [obj],
    "dict": lambda obj: {"a": obj},
    "tuple": lambda obj: (obj,),
    "dataclass": lambda obj: Holder(obj),
}


@dataclasses.dataclass
class Holder:
    """A dataclass that holds an object."""

    obj: Any


@pytest.mark.parametrize("hold", HOLDERS.values(), ids=HOLDERS.keys())
def test_a_shallow_copy_of_a_container_does_not_reach_the_object_it_holds(
    hold: Callable[[Any], Any],
) -> None:
    """The container is copied, or shared, and the object is not copied."""
    held = hold(keyed())
    assert type(copy.copy(held)) is type(held)


def test_json_of_an_object_drops_what_it_does_not_hold() -> None:
    """A write-only value and a change in place are not in ``obj.json``."""
    original = Account(json={"id": 1, "name": "a", "tags": ["x"]})
    original.secret = "s3"
    original.tags.append("y")
    twin = Account(json=original.json)
    assert twin.json == {"id": 1, "name": "a", "tags": ["x"]}
    assert twin.tags == ["x"]
    assert "secret" not in twin.json


def test_json_of_a_partial_object_builds_an_object_that_is_not_partial() -> (
    None
):
    """The new object does not load the fields that the first lacks."""
    with requests_mock.Mocker() as mocker:
        mocker.post(BASE + "accounts/", json={"id": 7})
        mocker.get(
            BASE + "accounts/7/",
            json={"id": 7, "name": "srv", "tags": ["t"]},
        )
        made = Account.create(name="n")
        twin = Account(json=made.json)
        with pytest.raises(AttributeError, match="missing"):
            _ = twin.tags
        assert made.tags == ["t"]

        # The replacement that keeps the loading of what is missing.
        loading = Account(pk=made.pk)
        loading.update_from_json(made.json)
        assert loading.tags == ["t"]
