"""Tests of how a class is recognized as configured for Pydantic.

The refusal goes by a class's attribute and by the name and module of its
bases, so it never imports Pydantic and gives the same result whether or not
Pydantic is installed. These tests build classes that carry the markers, and
run without Pydantic.
"""

import dataclasses
import typing
from collections.abc import Callable
from typing import Any, TypedDict

import pytest

from eazyrest import Conversion, JSONObject, json_object


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
    return model


@pytest.mark.parametrize("module", ["pydantic", "pydantic.main"])
@pytest.mark.parametrize("name", ["BaseModel", "RootModel"])
def test_a_class_derived_from_a_pydantic_model_is_refused(
    name: str, module: str
) -> None:
    """A base named ``BaseModel`` or ``RootModel`` of ``pydantic`` counts.

    Args:
        name: Name of the base class.
        module: Module that the base class claims.
    """
    base = type(name, (), {"__module__": module})
    child = type("Child", (base,), {})

    with pytest.raises(TypeError, match="Child is configured for Pydantic"):
        _model(child)


@pytest.mark.parametrize(
    ("name", "module"),
    [
        ("BaseModel", "other"),
        ("BaseModel", "pydantic_like"),
        ("Model", "pydantic"),
    ],
)
def test_a_class_with_another_name_or_module_is_not_refused(
    name: str, module: str
) -> None:
    """Only the name and module that Pydantic's classes have count.

    Args:
        name: Name of the base class.
        module: Module that the base class claims.
    """
    base: Any = dataclasses.dataclass(
        type(name, (), {"__module__": module, "__annotations__": {"x": int}})
    )
    child: Any = dataclasses.dataclass(type("Child", (base,), {}))
    model = _model(child)

    assert model(json={"id": 1, "value": {"x": 1}}).value == child(1)


def test_a_typed_dict_with_a_pydantic_config_is_refused() -> None:
    """The attribute that ``with_config()`` sets marks a ``TypedDict``."""

    class Configured(TypedDict):
        """``TypedDict`` with the attribute that ``with_config()`` sets."""

        theme: str

    Configured.__pydantic_config__ = {}  # type: ignore[attr-defined]

    with pytest.raises(TypeError, match="Configured is configured for"):
        _model(Configured)


def test_a_conversion_owns_a_class_with_a_pydantic_config() -> None:
    """An entry for the class makes the field convert by the entry."""

    @dataclasses.dataclass
    class Configured:
        """Dataclass with the attribute that ``with_config()`` sets."""

        x: int

    Configured.__pydantic_config__ = {}  # type: ignore[attr-defined]
    model = _model(
        Configured,
        {
            Configured: Conversion(
                decode=lambda value, cls: cls(value),
                encode=lambda value, cls: value.x,
            )
        },
    )

    assert model(json={"id": 1, "value": 3}).value == Configured(3)


def _metadata(module: str, hook: bool = False) -> Any:
    """Return an object that looks like metadata from a module.

    Args:
        module: The module that the object's class claims.
        hook: Whether the class has Pydantic's schema hook.

    Returns:
        The metadata object.
    """
    body: dict[str, Any] = {"__module__": module}
    if hook:
        body["__get_pydantic_core_schema__"] = classmethod(
            lambda cls, source, handler: None
        )
    return type("Marker", (), body)()


_SHAPES: dict[str, Callable[[Any], Any]] = {
    "root": lambda ty: ty,
    "list": lambda ty: list[ty],  # type: ignore[valid-type]
    "typed_dict": lambda ty: typing.cast(Any, TypedDict)(
        "Holding", {"inner": ty}
    ),
    "dataclass": lambda ty: dataclasses.make_dataclass(
        "Holding", [("inner", ty)]
    ),
}


@pytest.mark.parametrize("shape", sorted(_SHAPES))
@pytest.mark.parametrize(
    "marker",
    [
        _metadata("pydantic"),
        _metadata("pydantic.fields"),
        _metadata("other", hook=True),
    ],
    ids=["pydantic", "submodule", "hook"],
)
def test_metadata_from_pydantic_is_refused(marker: Any, shape: str) -> None:
    """Metadata by module, or with the schema hook, is refused anywhere.

    Args:
        marker: The metadata.
        shape: Where the annotated type appears.
    """
    Cents = typing.NewType("Cents", int)
    conversion = Conversion(decode=lambda v, c: v, encode=lambda v, c: v)
    hint = _SHAPES[shape](typing.Annotated[Cents, marker])

    with pytest.raises(TypeError, match="The Annotated metadata Marker"):
        _model(hint, {Cents: conversion})


@pytest.mark.parametrize("module", ["annotated_types", "pydantic_like"])
def test_metadata_from_another_library_is_accepted(module: str) -> None:
    """Only the ``pydantic`` package and the schema hook count.

    Args:
        module: The module that the metadata's class claims.
    """
    model = _model(typing.Annotated[int, _metadata(module)])

    assert model(json={"id": 1, "value": 5}).value == 5


class _Hooked:
    """User type written for Pydantic, with its schema hook."""

    @classmethod
    def __get_pydantic_core_schema__(cls, source: Any, handler: Any) -> None:
        """Return no schema, as the tests never build one."""


@pytest.mark.parametrize("shape", sorted(_SHAPES))
def test_a_class_with_the_schema_hook_is_refused(shape: str) -> None:
    """A user type written for Pydantic is refused, at the root and inside.

    Args:
        shape: Where the class appears.
    """
    with pytest.raises(TypeError, match="_Hooked is configured for Pydantic"):
        _model(_SHAPES[shape](_Hooked))


def test_a_conversion_owns_a_class_with_the_schema_hook() -> None:
    """An entry for the class makes the field convert by the entry."""
    model = _model(
        list[_Hooked],
        {
            _Hooked: Conversion(
                decode=lambda value, cls: cls(), encode=lambda value, cls: 1
            )
        },
    )

    assert len(model(json={"id": 1, "value": [1, 2]}).value) == 2
