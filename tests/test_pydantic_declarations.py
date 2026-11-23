"""Tests of the refusal of declarations that are written for Pydantic.

eazyrest does not apply Pydantic's validation, so a field type that Pydantic
configures would read its JSON as it is. The model refuses such a type, at the
root of a field and inside the types that eazyrest walks, unless an entry of
the model's conversions owns the class. These tests build real Pydantic
classes. The tests that need no Pydantic are in ``test_pydantic_markers.py``.
"""

import dataclasses
import typing
from collections.abc import Callable
from typing import Any, NamedTuple, TypedDict

import pytest

from eazyrest import (
    API,
    Conversion,
    FieldConversionError,
    JSONObject,
    json_object,
)

pytest.importorskip("pydantic")

from pydantic import (
    AfterValidator,
    Base64Bytes,
    BaseModel,
    ConfigDict,
    Field,
    RootModel,
    with_config,
)

BASE = "https://declarations.example.com/"


class Point(BaseModel):
    """Pydantic model."""

    x: int


class Tags(RootModel[list[str]]):
    """Pydantic root model."""


@with_config(ConfigDict(extra="allow"))
class Settings(TypedDict):
    """``TypedDict`` with a Pydantic config."""

    theme: str


@with_config(ConfigDict(extra="forbid"))
@dataclasses.dataclass
class Spot:
    """Dataclass with a Pydantic config."""

    x: int


@with_config(ConfigDict(extra="forbid"))
class Pair(NamedTuple):
    """``NamedTuple`` with a Pydantic config."""

    x: int


class Detail(TypedDict):
    """Plain ``TypedDict`` that is the vehicle for a nested member."""

    inner: Any


# Each class, with a conversion that reads and writes it as JSON.
_CLASSES: dict[str, tuple[Any, Conversion]] = {
    "model": (
        Point,
        Conversion(
            decode=lambda value, cls: cls.model_validate(value),
            encode=lambda value, cls: value.model_dump(),
        ),
    ),
    "root_model": (
        Tags,
        Conversion(
            decode=lambda value, cls: cls(value),
            encode=lambda value, cls: value.root,
        ),
    ),
    "typed_dict": (
        Settings,
        Conversion(
            decode=lambda value, cls: {**value, "read": True},
            encode=lambda value, cls: {
                key: item for key, item in value.items() if key != "read"
            },
        ),
    ),
    "dataclass": (
        Spot,
        Conversion(
            decode=lambda value, cls: cls(**value),
            encode=lambda value, cls: dataclasses.asdict(value),
        ),
    ),
}

_JSON = {
    "model": {"x": 1},
    "root_model": ["a"],
    "typed_dict": {"theme": "dark"},
    "dataclass": {"x": 1},
}


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


# Where a class appears in a field's annotation, as a function of the class.
_PLACES: dict[str, Callable[[Any], Any]] = {
    "root": lambda ty: ty,
    "optional": lambda ty: ty | None,
    "list": lambda ty: list[ty],  # type: ignore[valid-type]
    "mapping": lambda ty: dict[str, ty],  # type: ignore[valid-type]
    "fixed_tuple": lambda ty: tuple[int, ty],  # type: ignore[valid-type]
    "annotated": lambda ty: typing.Annotated[ty, "metadata"],
    "new_type": lambda ty: typing.NewType("Wrapped", ty),
    "typed_dict": _in_typed_dict,
    "dataclass": _in_dataclass,
    "named_tuple": _in_named_tuple,
}


def _model(hint: Any, conversions: dict[Any, Conversion] | None = None) -> Any:
    """Decorate a model with one field of a type.

    Args:
        hint: Annotation of the field ``value``.
        conversions: The model's conversions, or ``None`` for none.

    Returns:
        The decorated class.

    Raises:
        TypeError: If decorating the class refuses the field.
    """
    body: dict[str, Any] = {
        "__module__": __name__,
        "class_url": "/holders/",
        "__annotations__": {"id": int, "value": hint},
    }
    if conversions is not None:
        body["conversions"] = conversions
    model: Any = json_object(type("Holder", (JSONObject,), body))
    model.register_api(API(BASE))
    return model


@pytest.mark.parametrize("place", sorted(_PLACES))
@pytest.mark.parametrize("name", sorted(_CLASSES))
def test_a_class_configured_for_pydantic_is_refused_at_decoration(
    name: str, place: str
) -> None:
    """A field of such a class, at the root or inside, is refused.

    Args:
        name: Which class.
        place: Where the class appears in the field's annotation.
    """
    cls, _ = _CLASSES[name]
    hint = _PLACES[place](cls)

    with pytest.raises(
        TypeError,
        match=(
            rf"^Holder\.value: {cls.__name__} is configured for Pydantic, "
            r"which eazyrest does not apply.* Register a Conversion for it "
            r"in the model's conversions, or remove the Pydantic "
            r"configuration\.$"
        ),
    ):
        _model(hint)


@pytest.mark.parametrize("place", ["root", "optional", "list", "dataclass"])
@pytest.mark.parametrize("name", sorted(_CLASSES))
def test_a_class_configured_for_pydantic_is_accepted_with_a_conversion(
    name: str, place: str
) -> None:
    """An entry that owns the class makes the field convert by the entry.

    Args:
        name: Which class.
        place: Where the class appears in the field's annotation.
    """
    cls, conversion = _CLASSES[name]
    model = _model(_PLACES[place](cls), {cls: conversion})
    raw = _JSON[name]
    json = {
        "root": raw,
        "optional": raw,
        "list": [raw],
        "dataclass": {"inner": raw},
    }[place]

    obj = model(json={"id": 1, "value": json})
    value = obj.value

    assert value is not None
    assert obj.json == {"id": 1, "value": json}


def test_a_nested_class_is_refused_when_its_annotation_resolves_late() -> None:
    """A member whose annotation names a class defined later is checked then.

    Decoration cannot analyze the member yet, so the first read refuses it.
    """

    @dataclasses.dataclass
    class Waiting:
        inner: "LaterPoint"  # type: ignore[name-defined]  # noqa: F821

    model = _model(Waiting)
    obj = model(json={"id": 1, "value": {"inner": {"x": 1}}})
    globals()["LaterPoint"] = Point
    try:
        with pytest.raises(
            FieldConversionError, match="Point is configured for Pydantic"
        ):
            _ = obj.value
    finally:
        del globals()["LaterPoint"]


@pytest.mark.parametrize("name", ["model", "root_model"])
def test_a_class_configured_for_pydantic_is_refused_as_a_primary_key(
    name: str,
) -> None:
    """A primary key of such a class gets the refusal of any key type.

    A key cannot be converted, so advice to register a conversion would be
    wrong. The refusal names the types that a key may be.

    Args:
        name: Which class.
    """
    cls, _ = _CLASSES[name]
    body = {"__module__": __name__, "__annotations__": {"id": cls}}

    with pytest.raises(TypeError, match=r"Keyed\.id") as caught:
        json_object(type("Keyed", (JSONObject,), body))
    for text in ("str", "int", "UUID"):
        assert text in str(caught.value)
    assert "Conversion" not in str(caught.value)


def test_a_subclass_of_a_pydantic_model_is_refused() -> None:
    """A class that derives from a model is refused, as the model is."""

    class Child(Point):
        """Subclass of a Pydantic model."""

    with pytest.raises(TypeError, match="Child is configured for Pydantic"):
        _model(Child)


_METADATA_TYPES = {
    "field": (typing.Annotated[int, Field(gt=0)], "FieldInfo"),
    "alias": (typing.Annotated[int, Field(alias="other")], "FieldInfo"),
    "validator": (
        typing.Annotated[int, AfterValidator(lambda value: value)],
        "AfterValidator",
    ),
    "base64": (Base64Bytes, "EncodedBytes"),
}
_BYTES_CONVERSION = {
    bytes: Conversion(
        decode=lambda value, cls: value.encode(),
        encode=lambda value, cls: value.decode(),
    )
}


@pytest.mark.parametrize("place", sorted(_PLACES))
@pytest.mark.parametrize("name", sorted(_METADATA_TYPES))
def test_pydantic_metadata_is_refused_at_decoration(
    name: str, place: str
) -> None:
    """Metadata for Pydantic is refused at the root and inside, with a path.

    Args:
        name: Which annotation.
        place: Where it appears in the field's annotation.
    """
    ty, metadata = _METADATA_TYPES[name]

    with pytest.raises(
        TypeError,
        match=(
            rf"^Holder\.value: .*The Annotated metadata {metadata} is for "
            r"Pydantic.* Remove the Pydantic metadata\.$"
        ),
    ):
        _model(_PLACES[place](ty))


def test_a_conversion_for_bytes_does_not_allow_base64_bytes() -> None:
    """An entry for ``bytes`` does not make Pydantic's encoder apply."""
    with pytest.raises(
        TypeError, match="metadata EncodedBytes is for Pydantic"
    ):
        _model(Base64Bytes, _BYTES_CONVERSION)


def test_a_conversion_for_a_new_type_does_not_allow_pydantic_metadata() -> (
    None
):
    """An entry for the underlying type does not make the metadata apply."""
    Cents = typing.NewType("Cents", int)
    conversion = Conversion(decode=lambda v, c: v, encode=lambda v, c: v)

    with pytest.raises(TypeError, match="FieldInfo is for Pydantic"):
        _model(typing.Annotated[Cents, Field(gt=0)], {Cents: conversion})


def test_the_member_of_a_typed_dict_with_pydantic_metadata_is_named() -> None:
    """A ``TypedDict`` key with a Pydantic alias is named."""

    class Keyed(TypedDict):
        """``TypedDict`` whose key has a Pydantic alias."""

        name: typing.Annotated[str, Field(alias="label")]

    with pytest.raises(TypeError, match=r"Keyed\.name: The Annotated"):
        _model(Keyed)


def test_a_late_member_is_refused_though_no_value_uses_it() -> None:
    """A member that resolves late is refused when its field is first used.

    The refusal belongs to the declaration, so a value that holds no instance
    of the member, such as an empty list, does not escape it.
    """

    @dataclasses.dataclass
    class Waiting:
        inner: "LaterPoint"  # type: ignore[name-defined]  # noqa: F821

    model = _model(list[Waiting])
    obj = model(json={"id": 1, "value": []})
    globals()["LaterPoint"] = Point
    try:
        with pytest.raises(
            FieldConversionError, match="Point is configured for Pydantic"
        ):
            _ = obj.value
    finally:
        del globals()["LaterPoint"]


def test_a_query_refuses_a_late_pydantic_member_as_a_read_does(
    requests_mock: Any,
) -> None:
    """A query by a field with a member that resolves to Pydantic raises.

    The error is a ``FieldConversionError`` naming the field, and nothing is
    sent.
    """

    @dataclasses.dataclass
    class Waiting:
        inner: "LaterPoint"  # type: ignore[name-defined]  # noqa: F821

    model = _model(Waiting)
    globals()["LaterPoint"] = Point
    try:
        with pytest.raises(
            FieldConversionError, match=r"Holder\.value.*Point is configured"
        ):
            list(model.filter(value=1))
    finally:
        del globals()["LaterPoint"]

    assert not requests_mock.called


class _CountingMeta(type):
    """Metaclass that counts the lookups of Pydantic's config attribute."""

    lookups = 0

    def __getattribute__(cls, name: str) -> Any:
        """Count a lookup of ``__pydantic_config__``, then look it up.

        Args:
            name: Attribute name.

        Returns:
            The attribute.
        """
        if name == "__pydantic_config__":
            count = _CountingMeta.lookups + 1
            type.__setattr__(_CountingMeta, "lookups", count)
        return super().__getattribute__(name)


@dataclasses.dataclass
class _Counted(metaclass=_CountingMeta):
    """Dataclass that counts how often a refusal looks at it."""

    x: int


def test_a_late_member_is_checked_once_and_not_for_each_value() -> None:
    """The check of a member that resolves late does not repeat for a value."""

    @dataclasses.dataclass
    class Waiting:
        inner: "LaterCounted"  # type: ignore[name-defined]  # noqa: F821

    model = _model(list[Waiting])
    items = [{"inner": {"x": index}} for index in range(5)]
    first = model(json={"id": 1, "value": items})
    globals()["LaterCounted"] = _Counted
    try:
        assert len(first.value) == 5
        after_first_use = _CountingMeta.lookups
        assert len(model(json={"id": 2, "value": items}).value) == 5
    finally:
        del globals()["LaterCounted"]

    assert _CountingMeta.lookups == after_first_use


def test_a_subclass_that_converts_a_late_member_reads_it() -> None:
    """A conversion in a subclass admits a late member that its base refuses.

    The base's decision about the inherited field is not the subclass's.
    """

    @dataclasses.dataclass
    class Waiting:
        inner: "LaterPoint"  # type: ignore[name-defined]  # noqa: F821

    base = _model(Waiting)
    conversion = _CLASSES["model"][1]
    child: Any = json_object(
        type(
            "Child",
            (base,),
            {"__module__": __name__, "conversions": {Point: conversion}},
        )
    )
    globals()["LaterPoint"] = Point
    try:
        value = child(json={"id": 1, "value": {"inner": {"x": 1}}}).value
        with pytest.raises(
            FieldConversionError, match="Point is configured for Pydantic"
        ):
            _ = base(json={"id": 1, "value": {"inner": {"x": 1}}}).value
    finally:
        del globals()["LaterPoint"]

    assert value.inner == Point(x=1)


def test_a_member_with_pydantic_metadata_that_resolves_late_is_refused() -> (
    None
):
    """Metadata in a member that cannot be analyzed yet is refused then."""

    @dataclasses.dataclass
    class Waiting:
        inner: "LaterField"  # type: ignore[name-defined]  # noqa: F821

    model = _model(Waiting)
    obj = model(json={"id": 1, "value": {"inner": 1}})
    globals()["LaterField"] = typing.Annotated[int, Field(gt=0)]
    try:
        with pytest.raises(
            FieldConversionError, match=r"Waiting\.inner: The Annotated"
        ):
            _ = obj.value
    finally:
        del globals()["LaterField"]


def test_annotated_types_constraints_are_accepted() -> None:
    """Constraints that other libraries use too do not opt in to Pydantic."""
    annotated_types = pytest.importorskip("annotated_types")
    model = _model(typing.Annotated[int, annotated_types.Gt(0)])

    assert model(json={"id": 1, "value": -1}).value == -1
