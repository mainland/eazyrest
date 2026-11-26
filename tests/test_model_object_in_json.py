"""A model object in the JSON that an object is given is no JSON value.

A model object cannot be copied, but that is not why ``json=`` and
``update_from_json()`` refuse one inside a mapping. The message says that the
object is no JSON value and tells what to give instead.
"""

from __future__ import annotations

import collections
import copy
import pickle
from collections.abc import Callable
from typing import Any

import pytest

from eazyrest import API, JSONObject, json_object

api = API("https://model-in-json.example/")


@json_object
class Owner(JSONObject):
    """A model whose object is given where JSON belongs."""

    class_url = "/owners/"
    id: int


@json_object
class Thing(JSONObject):
    """A model that is given JSON."""

    class_url = "/things/"
    id: int
    name: str
    tags: list[str]


Owner.register_api(api)
Thing.register_api(api)

BUILDS: dict[str, Callable[[Any], Any]] = {
    "json list": lambda obj: Thing(json={"id": 2, "tags": [obj]}),
    "json nested": lambda obj: Thing(json={"id": 2, "tags": [{"k": obj}]}),
    "json value": lambda obj: Thing(json={"id": 2, "name": obj}),
    "json tuple": lambda obj: Thing(json={"id": 2, "tags": (obj,)}),
    "json nested tuple": lambda obj: Thing(
        json={"id": 2, "tags": [{"k": (1, (obj,))}]}
    ),
    "json set": lambda obj: Thing(json={"id": 2, "tags": {obj}}),
    "json frozenset": lambda obj: Thing(
        json={"id": 2, "tags": frozenset([obj])}
    ),
    "json dict in tuple": lambda obj: Thing(
        json={"id": 2, "tags": ({"k": [obj]},)}
    ),
    "update_from_json tuple": lambda obj: Thing(
        json={"id": 2, "name": "a"}
    ).update_from_json({"tags": (obj,)}),
    "update_from_json list": lambda obj: Thing(
        json={"id": 2, "name": "a"}
    ).update_from_json({"tags": [obj]}),
    "update_from_json value": lambda obj: Thing(
        json={"id": 2, "name": "a"}
    ).update_from_json({"name": obj}),
}


@pytest.mark.parametrize("build", BUILDS.values(), ids=BUILDS.keys())
def test_a_model_object_in_json_is_no_json_value(
    build: Callable[[Any], Any],
) -> None:
    """The error says that the object is no JSON value, not that no copy."""
    with pytest.raises(TypeError, match="Owner object is no JSON value") as e:
        build(Owner(pk=3))
    assert "copied" not in str(e.value)


class JSONDict(dict[Any, Any]):
    """A ``dict`` subclass, which ``copy.deepcopy()`` copies."""


class JSONList(list[Any]):
    """A ``list`` subclass, which ``copy.deepcopy()`` copies."""


class Holder:
    """An object whose attribute holds a value."""

    def __init__(self, value: Any) -> None:
        """Hold ``value``."""
        self.value = value


def _defaultdict(obj: Any) -> Any:
    found: collections.defaultdict[str, Any] = collections.defaultdict(list)
    found["k"] = obj
    return found


# Each makes a container or object that holds the model object, such that
# copy.deepcopy() enters it.
HOLDERS: dict[str, Callable[[Any], Any]] = {
    "a list": lambda obj: [obj],
    "a dict": lambda obj: {"k": obj},
    "a tuple": lambda obj: (obj,),
    "a set": lambda obj: {obj},
    "a frozenset": lambda obj: frozenset([obj]),
    "a dict subclass": lambda obj: JSONDict(k=obj),
    "a list subclass": lambda obj: JSONList([obj]),
    "a defaultdict": _defaultdict,
    "an OrderedDict": lambda obj: collections.OrderedDict(k=obj),
    "a deque": lambda obj: collections.deque([obj]),
    "a UserDict": lambda obj: collections.UserDict(k=obj),
    "a UserList": lambda obj: collections.UserList([obj]),
    "an object with an attribute": Holder,
    "a dict subclass with the object as a key": lambda obj: JSONDict({obj: 1}),
    "a dict with the object as a key": lambda obj: {obj: 1},
    "a tuple in a deque": lambda obj: collections.deque([(1, obj)]),
}

# Where the container goes in the JSON, so that the object is at depth 1 and 3.
DEPTHS: dict[str, Callable[[Any], Any]] = {
    "at depth 1": lambda held: held,
    "at depth 3": lambda held: [{"k": [held]}],
}


def _given(entry: str, held: Any) -> Thing:
    """Give ``held`` as a field's JSON to the entry point named ``entry``."""
    if entry == "json=":
        return Thing(json={"id": 2, "name": "a", "tags": held})
    thing = Thing(json={"id": 2, "name": "a"})
    thing.update_from_json({"tags": held})
    return thing


ENTRY_POINTS = ["json=", "update_from_json()"]


@pytest.mark.parametrize("entry", ENTRY_POINTS)
@pytest.mark.parametrize("depth", DEPTHS.values(), ids=DEPTHS.keys())
@pytest.mark.parametrize("hold", HOLDERS.values(), ids=HOLDERS.keys())
def test_a_model_object_anywhere_in_caller_json_is_no_json_value(
    entry: str, depth: Callable[[Any], Any], hold: Callable[[Any], Any]
) -> None:
    """Any container or object that a deep copy enters gives the same error."""
    with pytest.raises(TypeError, match="Owner object is no JSON value") as e:
        _given(entry, depth(hold(Owner(pk=3))))
    assert "copied" not in str(e.value)


@pytest.mark.parametrize(
    "hold", [HOLDERS["a list"], HOLDERS["a defaultdict"]], ids=["list", "dd"]
)
def test_update_from_json_that_refuses_a_model_object_changes_nothing(
    hold: Callable[[Any], Any],
) -> None:
    """The object is as it was before the call."""
    thing = Thing(json={"id": 2, "name": "a", "tags": ["x"]})
    before = copy.deepcopy(thing.json)

    with pytest.raises(TypeError, match="no JSON value"):
        thing.update_from_json({"name": "b", "tags": hold(Owner(pk=3))})

    assert thing.json == before
    assert thing.name == "a"


@pytest.mark.parametrize("hold", HOLDERS.values(), ids=HOLDERS.keys())
def test_a_copy_outside_caller_json_still_refuses_with_the_refused_copy(
    hold: Callable[[Any], Any],
) -> None:
    """Only the copy of caller JSON says that the object is no JSON value."""
    held = hold(Owner(pk=3))

    with pytest.raises(TypeError, match="cannot be copied or pickled"):
        copy.deepcopy(held)
    with pytest.raises(TypeError, match="cannot be copied or pickled"):
        pickle.dumps(held)


def test_copy_of_a_model_object_still_refuses_after_a_refused_json() -> None:
    """The refusal of caller JSON does not leave the next copy changed."""
    owner = Owner(pk=3)
    with pytest.raises(TypeError, match="no JSON value"):
        Thing(json={"id": 2, "tags": [owner]})

    with pytest.raises(TypeError, match="cannot be copied or pickled"):
        copy.deepcopy(owner)
    with pytest.raises(TypeError, match="cannot be copied or pickled"):
        copy.copy(owner)
    with pytest.raises(TypeError, match="cannot be copied or pickled"):
        pickle.dumps(owner)


class Uncopyable:
    """An object that raises its own error when it is deep-copied."""

    def __deepcopy__(self, memo: dict[int, Any]) -> Uncopyable:
        """Raise an error that is not a ``TypeError``."""
        raise RuntimeError("uncopyable")


@pytest.mark.parametrize("entry", ENTRY_POINTS)
@pytest.mark.parametrize("hold", HOLDERS.values(), ids=HOLDERS.keys())
def test_an_error_of_the_copy_of_a_container_without_a_model_propagates(
    entry: str, hold: Callable[[Any], Any]
) -> None:
    """The copy's own exception is not replaced by another message."""
    with pytest.raises(RuntimeError, match="uncopyable"):
        _given(entry, [hold(Uncopyable())])


class BuildsAnObjectWhenCopied:
    """An object whose copy gives an object JSON of its own."""

    def __deepcopy__(self, memo: dict[int, Any]) -> BuildsAnObjectWhenCopied:
        """Build an object, which copies the JSON that it is given."""
        Thing(json={"id": 1, "tags": ["x"]})
        return self


def test_a_copy_inside_the_copy_of_caller_json_leaves_the_rule_in_force() -> (
    None
):
    """A model object after the inner copy is still no JSON value."""
    owner = Owner(pk=3)

    with pytest.raises(TypeError, match="Owner object is no JSON value"):
        Thing(json={"id": 2, "tags": [BuildsAnObjectWhenCopied(), owner]})
