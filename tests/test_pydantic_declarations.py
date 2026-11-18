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
def test_a_class_configured_for_pydantic_is_refused_in_a_primary_key(
    name: str,
) -> None:
    """A primary key of such a class is refused, as another field is.

    Args:
        name: Which class.
    """
    cls, _ = _CLASSES[name]
    body = {"__module__": __name__, "__annotations__": {"id": cls}}

    with pytest.raises(TypeError, match="is configured for Pydantic"):
        json_object(type("Keyed", (JSONObject,), body))


def test_a_subclass_of_a_pydantic_model_is_refused() -> None:
    """A class that derives from a model is refused, as the model is."""

    class Child(Point):
        """Subclass of a Pydantic model."""

    with pytest.raises(TypeError, match="Child is configured for Pydantic"):
        _model(Child)


def test_annotated_pydantic_metadata_is_not_detected() -> None:
    """Metadata inside ``Annotated`` does not change how a field converts."""
    model = _model(typing.Annotated[int, Field(gt=0)])

    assert model(json={"id": 1, "value": -1}).value == -1
