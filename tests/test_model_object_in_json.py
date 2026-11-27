"""A model object in the JSON that an object is given is no JSON value.

A model object cannot be copied, but that is not why ``json=`` and
``update_from_json()`` refuse one inside a mapping. The message says that the
object is no JSON value and tells what to give instead.
"""

from __future__ import annotations

import collections
import copy
import datetime
import functools
import pickle
import weakref
from collections.abc import Callable, Iterator
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


class ShallowDict(JSONDict):
    """A copied mapping whose hook does not visit its values."""

    def __deepcopy__(self, memo: dict[int, Any]) -> ShallowDict:
        """Copy the mapping without asking for a copy of its members."""
        return type(self)(self)


class ShallowList(JSONList):
    """A copied list whose hook does not visit its values."""

    def __deepcopy__(self, memo: dict[int, Any]) -> ShallowList:
        """Copy the list without asking for a copy of its members."""
        return type(self)(self)


class SlottedShallowHolder:
    """An object whose shallow copy does not visit a slotted value."""

    __slots__ = ("value",)

    def __init__(self, value: Any) -> None:
        """Hold the value that a deep copy would otherwise visit."""
        self.value = value

    def __deepcopy__(self, memo: dict[int, Any]) -> SlottedShallowHolder:
        """Return another holder without copying its value."""
        return type(self)(self.value)


EXTERNAL_OWNER = Owner(pk=7)


class CopiesExternalObject:
    """A copy hook that tries to copy a model outside its own value."""

    def __deepcopy__(self, memo: dict[int, Any]) -> CopiesExternalObject:
        """Try an unrelated copy without returning a model in this value."""
        copy.deepcopy(EXTERNAL_OWNER)
        return self


class ReturnsExternalObject:
    """A copy hook that returns a model absent from the original value."""

    def __deepcopy__(self, memo: dict[int, Any]) -> Owner:
        """Return the external model without copying it."""
        return EXTERNAL_OWNER


SHALLOW_HOLDERS: dict[str, Callable[[Any], Any]] = {
    "dict": lambda obj: ShallowDict(k=obj),
    "list": lambda obj: ShallowList([obj]),
    "slotted object": SlottedShallowHolder,
}


METHOD_HOLDERS: dict[str, Callable[[Owner], Any]] = {
    "bound method": lambda obj: obj.save,
    "partial of a bound method": lambda obj: functools.partial(obj.save),
}


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


class ContainerContents:
    """A copy hook that reads a shared container through the copy memo."""

    def __init__(self, values: list[Any]) -> None:
        """Keep the source list, which can also be a sibling or ancestor."""
        self.values = values

    def __deepcopy__(self, memo: dict[int, Any]) -> tuple[Any, ...]:
        """Record the contents visible during the copy."""
        return tuple(copy.deepcopy(self.values, memo))


class ContainerLengthKey:
    """A dictionary key whose hash depends on its copied list."""

    def __init__(self, values: list[Any]) -> None:
        """Hold the list that defines the key's hash."""
        self.values = values

    def __hash__(self) -> int:
        """Return the length of the list."""
        return len(self.values)


class RecordsCopy:
    """A copy hook that records when the copier reaches it."""

    def __init__(self, name: str, events: list[str]) -> None:
        """Keep the label and the shared event list."""
        self.name = name
        self.events = events

    def __deepcopy__(self, memo: dict[int, Any]) -> str:
        """Record the event and return the label."""
        self.events.append(self.name)
        return self.name


@pytest.mark.parametrize("entry", ENTRY_POINTS)
def test_a_dictionary_value_is_copied_before_its_key(entry: str) -> None:
    """The two hooks run in the same order as Python's dictionary copy."""
    events: list[str] = []
    source = {RecordsCopy("key", events): RecordsCopy("value", events)}

    result = _given(entry, source).json["tags"]

    assert events == ["value", "key"]
    assert result == {"key": "value"}


@pytest.mark.parametrize("entry", ENTRY_POINTS)
@pytest.mark.parametrize("source_first", [True, False], ids=["source", "hook"])
def test_a_copy_hook_reads_a_complete_sibling(
    entry: str, source_first: bool
) -> None:
    """A copy hook reads the complete shared list in either sibling order."""
    shared = [1, 2]
    fields: list[tuple[str, Any]] = [
        ("source", shared),
        ("snapshot", ContainerContents(shared)),
    ]
    payload = dict(fields if source_first else reversed(fields))
    payload["lookup"] = {ContainerLengthKey(shared): "stored"}

    expected = copy.deepcopy(payload)
    result = _given(entry, payload).json["tags"]

    assert result["snapshot"] == expected["snapshot"] == (1, 2)
    key = next(iter(result["lookup"]))
    assert key.values is result["source"]
    assert result["lookup"][key] == "stored"


@pytest.mark.parametrize("entry", ENTRY_POINTS)
def test_a_copy_hook_can_read_the_active_prefix_of_an_ancestor(
    entry: str,
) -> None:
    """A true cycle sees the same copied prefix as Python's copy."""
    source: list[Any] = ["first"]
    source.extend([ContainerContents(source), "last", source])

    result = _given(entry, source).json["tags"]

    assert result[:3] == copy.deepcopy(source)[:3]
    assert result[3] is result


@pytest.mark.parametrize("entry", ENTRY_POINTS)
def test_a_deep_plain_branch_can_be_copied_alongside_a_hook(
    entry: str,
) -> None:
    """A deep plain branch remains iterative when a sibling has a hook."""
    source: Any = "bottom"
    for _ in range(1500):
        source = [{"child": source}]

    result = _given(
        entry, {"deep": source, "hook": ContainerContents([1, 2])}
    ).json["tags"]

    assert result["hook"] == (1, 2)
    child = result["deep"]
    for _ in range(1500):
        assert child is not source
        child = child[0]["child"]
        source = source[0]["child"]
    assert child == "bottom"


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


@pytest.mark.parametrize("entry", ENTRY_POINTS)
@pytest.mark.parametrize("hold", SHALLOW_HOLDERS.values(), ids=SHALLOW_HOLDERS)
def test_a_shallow_container_copy_cannot_keep_a_model_in_caller_json(
    entry: str, hold: Callable[[Any], Any]
) -> None:
    """A custom copy hook cannot hide a model that the input holds."""
    with pytest.raises(TypeError, match="Owner object is no JSON value"):
        _given(entry, hold(Owner(pk=3)))


@pytest.mark.parametrize("entry", ENTRY_POINTS)
@pytest.mark.parametrize("hold", METHOD_HOLDERS.values(), ids=METHOD_HOLDERS)
def test_a_callable_that_copies_a_model_is_no_json_value(
    entry: str, hold: Callable[[Owner], Any]
) -> None:
    """A callable that holds the model must give the caller JSON error."""
    with pytest.raises(TypeError, match="Owner object is no JSON value"):
        _given(entry, hold(Owner(pk=3)))


@pytest.mark.parametrize("hold", METHOD_HOLDERS.values(), ids=METHOD_HOLDERS)
def test_copying_a_callable_with_a_model_keeps_the_copy_error(
    hold: Callable[[Owner], Any],
) -> None:
    """Outside caller JSON, a model still refuses the callable's copy."""
    with pytest.raises(TypeError, match="cannot be copied or pickled"):
        copy.deepcopy(hold(Owner(pk=3)))


@pytest.mark.parametrize("entry", ENTRY_POINTS)
def test_a_function_closure_is_shared_by_deepcopy(entry: str) -> None:
    """A function's captured model is not copied as part of caller JSON."""
    owner = Owner(pk=3)

    def saved_owner() -> Owner:
        return owner

    assert copy.deepcopy(saved_owner) is saved_owner
    assert _given(entry, saved_owner).json["tags"] is saved_owner


class LocalZone(datetime.tzinfo):
    """A timezone that can hold a model in an ordinary attribute."""

    def __init__(self, owner: Owner | None = None) -> None:
        """Store the owner, if given."""
        self.owner = owner

    def utcoffset(self, dt: datetime.datetime | None) -> datetime.timedelta:
        """Return a fixed offset."""
        return datetime.timedelta(hours=2)

    def dst(self, dt: datetime.datetime | None) -> datetime.timedelta:
        """Return zero daylight saving time."""
        return datetime.timedelta()

    def tzname(self, dt: datetime.datetime | None) -> str:
        """Return the timezone's name."""
        return "UTC+02:00"


class ShallowZone(LocalZone):
    """A timezone whose copy shares the state in its attributes."""

    copies = 0

    def __deepcopy__(self, memo: dict[int, Any]) -> ShallowZone:
        """Return a new timezone that holds the original owner."""
        self.copies += 1
        return copy.copy(self)


class ShadowedTime(datetime.time):
    """A time with a property that shadows its stored timezone."""

    @property
    def tzinfo(self) -> datetime.tzinfo | None:
        """Raise if accessed through an instance attribute lookup."""
        raise AssertionError("timezone looked up on the instance")


TimeValue = datetime.datetime | datetime.time
TimeMaker = Callable[[datetime.tzinfo | None], TimeValue]
TIME_VALUES: dict[str, TimeMaker] = {
    "datetime": lambda zone: datetime.datetime(2026, 1, 1, tzinfo=zone),
    "time": lambda zone: datetime.time(10, tzinfo=zone),
}


@pytest.mark.parametrize("entry", ENTRY_POINTS)
@pytest.mark.parametrize(
    "make",
    [*TIME_VALUES.values(), lambda zone: ShadowedTime(10, tzinfo=zone)],
    ids=[*TIME_VALUES, "shadowed timezone"],
)
@pytest.mark.parametrize("zone_type", [LocalZone, ShallowZone])
def test_a_model_in_a_timezone_is_refused_before_copying(
    entry: str, make: TimeMaker, zone_type: type[LocalZone]
) -> None:
    """Both timezone descriptors belong to the input's stored state."""
    zone = zone_type(Owner(pk=3))

    with pytest.raises(TypeError, match="Owner object is no JSON value"):
        _given(entry, make(zone))

    if isinstance(zone, ShallowZone):
        assert zone.copies == 0


class ReturnsTimeWithModel:
    """A hook whose result adds a timezone holding a model."""

    def __init__(self, make: TimeMaker) -> None:
        """Store the function that builds the result."""
        self.make = make

    def __deepcopy__(self, memo: dict[int, Any]) -> TimeValue:
        """Return a new value with a timezone that holds the external owner."""
        return self.make(ShallowZone(EXTERNAL_OWNER))


@pytest.mark.parametrize("entry", ENTRY_POINTS)
@pytest.mark.parametrize("make", TIME_VALUES.values(), ids=TIME_VALUES)
def test_a_copy_hook_cannot_add_a_timezone_that_holds_a_model(
    entry: str, make: TimeMaker
) -> None:
    """The completed copy is subject to the same check as the input."""
    with pytest.raises(TypeError, match="Owner object is no JSON value"):
        _given(entry, ReturnsTimeWithModel(make))


@pytest.mark.parametrize("entry", ENTRY_POINTS)
@pytest.mark.parametrize("make", TIME_VALUES.values(), ids=TIME_VALUES)
@pytest.mark.parametrize("zone", [None, datetime.UTC, LocalZone()])
def test_a_time_with_no_model_is_copied(
    entry: str, make: TimeMaker, zone: datetime.tzinfo | None
) -> None:
    """Naive values and timezones with model-free state can be copied."""
    value = make(zone)
    stored = _given(entry, value).json["tags"]

    assert stored == value
    assert stored is not value
    if isinstance(zone, LocalZone):
        assert stored.tzinfo is not zone


def test_refusing_a_copied_timezone_preserves_pending_changes() -> None:
    """The update leaves pending and stored values intact after refusal."""
    thing = Thing(
        json={"id": 2, "name": "a", "tags": ["x"]}, write_mode="lazy"
    )
    thing.name = "pending"
    before = thing.json

    with pytest.raises(TypeError, match="Owner object is no JSON value"):
        thing.update_from_json(
            {
                "name": "incoming",
                "tags": ReturnsTimeWithModel(TIME_VALUES["time"]),
            }
        )

    assert thing.json == before
    assert thing.name == "pending"


CALLBACK_VALUES: dict[str, Callable[[Owner], Any]] = {
    "property getter": lambda owner: property(owner.__eq__),
    "property setter": lambda owner: property(fset=owner.__setattr__),
    "property deleter": lambda owner: property(fdel=owner.__delattr__),
    "weak-reference callback": lambda owner: weakref.ref(
        EXTERNAL_OWNER, functools.partial(owner.__eq__)
    ),
    "method wrapper": lambda owner: owner.__str__,
}


@pytest.mark.parametrize("entry", ENTRY_POINTS)
@pytest.mark.parametrize("hold", CALLBACK_VALUES.values(), ids=CALLBACK_VALUES)
def test_a_model_in_a_strongly_held_callback_is_refused(
    entry: str, hold: Callable[[Owner], Any]
) -> None:
    """The receiver of a bound callback belongs to the callback's state."""
    with pytest.raises(TypeError, match="Owner object is no JSON value"):
        _given(entry, hold(Owner(pk=3)))


@pytest.mark.parametrize("entry", ENTRY_POINTS)
def test_a_weak_target_and_model_free_callbacks_can_be_shared(
    entry: str,
) -> None:
    """A weak target is outside the payload and ordinary callbacks can pass."""
    owner = Owner(pk=3)
    prop = property(str.upper)
    reference = weakref.ref(owner, lambda ref: None)

    stored = _given(entry, [prop, reference]).json["tags"]

    assert stored[0] is prop
    assert stored[1] is reference
    assert stored[1]() is owner


class CopyingString(str):
    """A string subclass whose copy is a plain string."""

    def __deepcopy__(self, memo: dict[int, Any]) -> str:
        """Return the plain string."""
        return str(self)


class CheckedString(CopyingString):
    """A string that detects instance class lookup."""

    def __getattribute__(self, name: str) -> Any:
        """Raise if inspected through the instance attribute protocol."""
        if name == "__class__":
            raise AssertionError("class looked up on the instance")
        return super().__getattribute__(name)


class CheckedMeta(type):
    """A metaclass that detects attribute lookup and equality."""

    __hash__ = type.__hash__

    def __getattribute__(self, name: str) -> Any:
        """Raise if the MRO is looked up through the metaclass."""
        if name == "__mro__":
            raise AssertionError("MRO looked up on the metaclass")
        return super().__getattribute__(name)

    def __eq__(self, other: object) -> bool:
        """Raise if classification compares classes by equality."""
        raise AssertionError("metaclass equality")


class ComparedString(CopyingString, metaclass=CheckedMeta):
    """A string whose class checks for metaclass dispatch."""


@pytest.mark.parametrize("entry", ENTRY_POINTS)
@pytest.mark.parametrize("kind", [CheckedString, ComparedString])
def test_copying_uses_the_actual_type_without_calling_class_hooks(
    entry: str, kind: type[CopyingString]
) -> None:
    """Validation can classify the string without interceptable lookups."""
    value = kind("label")
    assert type(copy.deepcopy(value)) is str

    stored = _given(entry, value).json["tags"]

    assert type(stored) is str
    assert stored == "label"


def test_a_generator_is_not_traversed_before_its_copy_fails() -> None:
    """Checking caller JSON neither consumes nor enters a generator."""
    owner = Owner(pk=3)

    def values() -> Iterator[Owner]:
        yield owner

    generator = values()
    with pytest.raises(TypeError) as error:
        Thing(json={"id": 2, "tags": [generator]})
    assert "no JSON value" not in str(error.value)
    assert next(generator) is owner


def test_a_refused_shallow_copy_leaves_the_object_unchanged() -> None:
    """The update does not apply a field before refusing the model value."""
    thing = Thing(json={"id": 2, "name": "a", "tags": ["x"]})
    before = copy.deepcopy(thing.json)

    with pytest.raises(TypeError, match="Owner object is no JSON value"):
        thing.update_from_json(
            {"name": "b", "tags": ShallowDict(k=Owner(pk=3))}
        )

    assert thing.json == before


@pytest.mark.parametrize("entry", ENTRY_POINTS)
def test_copying_a_model_external_to_caller_json_keeps_its_copy_error(
    entry: str,
) -> None:
    """A copy hook's unrelated model is outside the caller's value."""
    with pytest.raises(TypeError, match="cannot be copied or pickled"):
        _given(entry, CopiesExternalObject())


@pytest.mark.parametrize("entry", ENTRY_POINTS)
def test_a_copy_hook_cannot_insert_a_model_into_caller_json(
    entry: str,
) -> None:
    """The copied result must not contain a newly returned model."""
    with pytest.raises(TypeError, match="Owner object is no JSON value"):
        _given(entry, ReturnsExternalObject())


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
