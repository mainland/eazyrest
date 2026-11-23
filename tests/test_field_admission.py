"""Tests of which field types a model admits.

A field type is admitted if an entry of the model's conversions converts it, or
it is a relation, a supported structure, a ``Literal``, ``Any`` or ``object``,
a JSON primitive, or a bare ``dict`` or ``list``. Any other class has no JSON
form that eazyrest knows, so it is refused wherever it appears.
"""

import collections.abc
import dataclasses
import enum
import ipaddress
import pathlib
import typing
from collections.abc import Callable
from typing import Any, NamedTuple, TypedDict

import pytest

from eazyrest import (
    API,
    Conversion,
    FieldConversionError,
    JSONObject,
    encode_json,
    json_object,
)

BASE = "https://admission.example.com/"


class Plain:
    """Class of an application that nothing converts."""


_ADDRESS = Conversion(
    decode=lambda value, cls: cls(value),
    encode=lambda value, cls: str(value),
)

_REFUSED: dict[str, Any] = {
    "typing_hashable": typing.Hashable,
    "abc_hashable": collections.abc.Hashable,
    "builtin_type": type,
    "typing_type": typing.Type,  # noqa: UP006
    "typing_callable": typing.Callable,
    "abc_callable": collections.abc.Callable,
    "bytes": bytes,
    "bare_set": set,
    "typing_set": typing.Set,  # noqa: UP006
    "bare_sequence": typing.Sequence,
    "path": pathlib.Path,
    "address": ipaddress.IPv4Address,
    "class_of_the_application": Plain,
    "annotated_bytes": typing.Annotated[bytes, "metadata"],
    "new_type_of_bytes": typing.NewType("Blob", bytes),
}


class Holding(TypedDict):
    """``TypedDict`` that is the vehicle for a nested member."""

    inner: Any


def _in_typed_dict(ty: Any) -> Any:
    """Return a ``TypedDict`` that has a key of a type.

    Args:
        ty: Type of the key.

    Returns:
        The ``TypedDict``.
    """
    return typing.cast(Any, TypedDict)("Holding", {"inner": ty})


def _in_dataclass(ty: Any) -> Any:
    """Return a dataclass that has a field of a type.

    Args:
        ty: Type of the field.

    Returns:
        The dataclass.
    """
    return dataclasses.make_dataclass("Holding", [("inner", ty)])


def _in_named_tuple(ty: Any) -> Any:
    """Return a ``NamedTuple`` that has a field of a type.

    Args:
        ty: Type of the field.

    Returns:
        The ``NamedTuple``.
    """
    return NamedTuple("Holding", [("inner", ty)])


_PLACES: dict[str, Callable[[Any], Any]] = {
    "root": lambda ty: ty,
    "optional": lambda ty: ty | None,
    "list": lambda ty: list[ty],  # type: ignore[valid-type]
    "mapping": lambda ty: dict[str, ty],  # type: ignore[valid-type]
    "fixed_tuple": lambda ty: tuple[int, ty],  # type: ignore[valid-type]
    "union": lambda ty: int | ty,
    "new_type": lambda ty: typing.NewType("Wrapped", ty),
    "typed_dict": _in_typed_dict,
    "dataclass": _in_dataclass,
    "named_tuple": _in_named_tuple,
}


def _model(
    hint: Any,
    conversions: dict[Any, Conversion] | None = None,
    base: type[JSONObject] = JSONObject,
) -> Any:
    """Decorate a model with one field of a type.

    Args:
        hint: Annotation of the field ``value``.
        conversions: The model's conversions, or ``None`` for none.
        base: Base class.

    Returns:
        The decorated class.

    Raises:
        TypeError: If decorating the class refuses the field.
    """
    body: dict[str, Any] = {
        "__module__": __name__,
        "__annotations__": {"id": int, "value": hint},
    }
    if conversions is not None:
        body["conversions"] = conversions
    model: Any = json_object(type("Holder", (base,), body))
    model.register_api(API(BASE))
    return model


@pytest.mark.parametrize("place", sorted(_PLACES))
@pytest.mark.parametrize("name", sorted(_REFUSED))
def test_a_class_with_no_conversion_is_refused_at_decoration(
    name: str, place: str
) -> None:
    """A field of such a class, at the root or inside, is refused.

    Args:
        name: Which class.
        place: Where it appears.
    """
    hint = _PLACES[place](_REFUSED[name])

    with pytest.raises(TypeError, match=r"Holder\.value: "):
        _model(hint)


@pytest.mark.parametrize("place", sorted(set(_PLACES) - {"union"}))
def test_an_entry_of_the_conversions_admits_a_class(place: str) -> None:
    """A class that an entry converts is admitted at the root and inside.

    A union cannot hold a class that converts, whatever admits it.

    Args:
        place: Where the class appears.
    """
    hint = _PLACES[place](ipaddress.IPv4Address)

    assert _model(hint, {ipaddress.IPv4Address: _ADDRESS}) is not None


def test_the_message_names_the_class_and_the_two_remedies() -> None:
    """The refusal says to annotate Any or to register a conversion."""
    with pytest.raises(TypeError) as refused:
        _model(Plain)

    message = str(refused.value)
    assert "Plain has no conversion" in message
    assert "Annotate Any" in message
    assert "register a Conversion" in message


_ADMITTED: dict[str, Any] = {
    "any": Any,
    "object": object,
    "str": str,
    "int": int,
    "float": float,
    "bool": bool,
    "dict": dict,
    "list": list,
    "typing_list": typing.List,  # noqa: UP006
    "typing_dict": typing.Dict,  # noqa: UP006
    "literal": typing.Literal["a", "b"],
    "list_of_str": list[str],
    "mapping_of_any": dict[str, Any],
    "bare_tuple": tuple,
    "optional_int": int | None,
    "union_of_primitives": int | str,
    "new_type_of_str": typing.NewType("Name", str),
    "annotated_str": typing.Annotated[str, "metadata"],
}


@pytest.mark.parametrize("name", sorted(_ADMITTED))
def test_a_field_type_with_a_defined_reading_is_admitted(name: str) -> None:
    """A JSON type, ``Any``, and a supported structure decorate.

    The model also gives each a JSON schema.

    Args:
        name: Which type.
    """
    model = _model(_ADMITTED[name])

    assert "value" in model.json_schema("read")["properties"]
    assert "value" in model.json_schema("write")["properties"]


class Pair(enum.Enum):
    """Enum whose member values are tuples, which JSON does not have."""

    ORIGIN = (0, 0)


class Mixed(enum.Enum):
    """Enum whose member values are all JSON scalars."""

    TEXT = "a"
    NUMBER = 1
    REAL = 1.5
    FLAG = True


_NOT_SCALARS: dict[str, Any] = {
    "literal_bytes": typing.Literal[b"x"],
    "literal_with_bytes": typing.Literal["a", b"x"],
    "literal_of_member_with_tuple": typing.Literal[Pair.ORIGIN],
    "enum_with_tuples": Pair,
}


@pytest.mark.parametrize("place", sorted(_PLACES))
@pytest.mark.parametrize("name", sorted(_NOT_SCALARS))
def test_a_literal_or_enum_with_a_value_that_is_no_json_scalar_is_refused(
    name: str, place: str
) -> None:
    """Such a value has no JSON reading, so the field is refused.

    Args:
        name: Which type.
        place: Where it appears.
    """
    hint = _PLACES[place](_NOT_SCALARS[name])

    # A union that holds a type that converts has its own refusal first.
    message = r"not a JSON scalar" if place != "union" else r"Holder\.value: "
    with pytest.raises(TypeError, match=message):
        _model(hint)


def test_an_enum_with_an_entry_of_its_own_may_have_any_values() -> None:
    """A conversion of the enum says how its members read."""
    own = Conversion(
        decode=lambda value, cls: cls(tuple(value)),
        encode=lambda value, cls: list(value.value),
    )

    model = _model(Pair, {Pair: own})

    assert model(json={"id": 1, "value": [0, 0]}).value is Pair.ORIGIN


@pytest.mark.parametrize(
    "hint",
    [
        Mixed,
        typing.Literal["a", 1, 1.5, True] | None,
        typing.Literal[Mixed.TEXT, Mixed.NUMBER],
    ],
    ids=["enum", "literal", "literal_of_members"],
)
def test_json_scalar_values_are_admitted_in_a_literal_or_an_enum(
    hint: Any,
) -> None:
    """A string, an integer, a float, and a ``bool`` decorate.

    Args:
        hint: Annotation of the field.
    """
    assert "value" in _model(hint).json_schema()["properties"]


def test_the_bare_typing_alias_is_refused_as_the_class_is() -> None:
    """``typing.Hashable`` and ``collections.abc.Hashable`` are one class."""
    for spelling in (typing.Hashable, collections.abc.Hashable):
        with pytest.raises(TypeError, match="Hashable has no conversion"):
            _model(spelling)


def test_raw_json_of_any_is_kept_as_it_is() -> None:
    """A field annotated ``Any`` reads what the server sent."""
    model = _model(Any)

    assert model(json={"id": 1, "value": [1, {"a": None}]}).value == [
        1,
        {"a": None},
    ]


def test_a_late_member_with_no_conversion_is_refused_when_first_used() -> None:
    """A member that names a class defined later is checked then."""

    @dataclasses.dataclass
    class Waiting:
        inner: "LaterPlain"  # type: ignore[name-defined]  # noqa: F821

    model = _model(Waiting)
    obj = model(json={"id": 1, "value": {"inner": 1}})
    globals()["LaterPlain"] = Plain
    try:
        with pytest.raises(
            FieldConversionError, match="Plain has no conversion"
        ):
            _ = obj.value
    finally:
        del globals()["LaterPlain"]


def test_a_subclass_with_an_entry_reads_a_late_member_its_base_refuses() -> (
    None
):
    """The base's decision about an inherited field is not the subclass's."""

    @dataclasses.dataclass
    class Waiting:
        inner: "LaterAddress"  # type: ignore[name-defined]  # noqa: F821

    base = _model(Waiting)
    child: Any = json_object(
        type(
            "Child",
            (base,),
            {
                "__module__": __name__,
                "conversions": {ipaddress.IPv4Address: _ADDRESS},
            },
        )
    )
    globals()["LaterAddress"] = ipaddress.IPv4Address
    try:
        value = child(json={"id": 1, "value": {"inner": "10.0.0.1"}}).value
        with pytest.raises(
            FieldConversionError, match="IPv4Address has no conversion"
        ):
            _ = base(json={"id": 1, "value": {"inner": "10.0.0.1"}}).value
    finally:
        del globals()["LaterAddress"]

    assert value.inner == ipaddress.IPv4Address("10.0.0.1")


def test_an_inherited_field_follows_the_conversions_of_its_subclass() -> None:
    """A subclass keeps a field that its own entry admits, not a sibling."""

    class Shared(JSONObject):
        """Base whose subclasses differ in their conversions."""

    @json_object
    class Owner(Shared):
        class_url = "/owners/"

        id: int
        value: "Later"  # type: ignore[name-defined]  # noqa: F821

    admitting: Any = json_object(
        type(
            "Admitting",
            (Owner,),
            {
                "__module__": __name__,
                "conversions": {ipaddress.IPv4Address: _ADDRESS},
            },
        )
    )
    other: Any = json_object(
        type("Other", (Owner,), {"__module__": __name__, "conversions": {}})
    )
    for model in (Owner, admitting, other):
        model.register_api(API(BASE))
    globals()["Later"] = ipaddress.IPv4Address
    try:
        address = admitting(json={"id": 1, "value": "10.0.0.1"}).value
        with pytest.raises(FieldConversionError, match="no conversion"):
            _ = other(json={"id": 1, "value": "10.0.0.1"}).value
    finally:
        del globals()["Later"]

    assert address == ipaddress.IPv4Address("10.0.0.1")


def test_encode_json_refuses_a_class_with_no_conversion() -> None:
    """``encode_json()`` applies the rule that a field does."""
    with pytest.raises(TypeError, match="IPv4Address has no conversion"):
        encode_json(ipaddress.IPv4Address("10.0.0.1"), ipaddress.IPv4Address)


def test_a_key_of_a_class_with_no_conversion_is_still_refused() -> None:
    """A primary key of such a class is refused, as it was."""
    body = {"__module__": __name__, "__annotations__": {"id": Plain}}

    with pytest.raises(TypeError):
        json_object(type("Keyed", (JSONObject,), body))


def test_a_subclass_with_other_conversions_keeps_its_inherited_key() -> None:
    """The copied field is still the key: equality, hash, and url work."""

    @json_object
    class Parent(JSONObject):
        class_url = "/items/"

        id: int

    child: Any = json_object(
        type(
            "Child",
            (Parent,),
            {
                "__module__": __name__,
                "class_url": "/children/",
                "conversions": {ipaddress.IPv4Address: _ADDRESS},
            },
        )
    )
    api = API(BASE)
    Parent.register_api(api)
    child.register_api(api)

    first = child(json={"id": 1})
    same = child(json={"id": 1})
    other = child(json={"id": 2})

    assert first.pk == 1
    assert first == same
    assert hash(first) == hash(same)
    assert first != other
    assert first.url == "/children/1/"


def test_a_grandchild_reads_a_late_member_as_its_parent_does() -> None:
    """A grandchild with its parent's conversions shares its decision."""

    @dataclasses.dataclass
    class Waiting:
        inner: "LaterGrandchild"  # type: ignore[name-defined]  # noqa: F821

    base = _model(Waiting)
    child: Any = json_object(
        type(
            "Child",
            (base,),
            {
                "__module__": __name__,
                "conversions": {ipaddress.IPv4Address: _ADDRESS},
            },
        )
    )
    grandchild: Any = json_object(
        type("Grandchild", (child,), {"__module__": __name__})
    )
    globals()["LaterGrandchild"] = ipaddress.IPv4Address
    try:
        json = {"id": 1, "value": {"inner": "10.0.0.1"}}
        read = grandchild(json=json).value
        with pytest.raises(
            FieldConversionError, match="IPv4Address has no conversion"
        ):
            _ = base(json=json).value
    finally:
        del globals()["LaterGrandchild"]

    assert read.inner == ipaddress.IPv4Address("10.0.0.1")
