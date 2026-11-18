"""Tests of how a class is recognized as configured for Pydantic.

The refusal goes by a class's attribute and by the name and module of its
bases, so it never imports Pydantic and gives the same result whether or not
Pydantic is installed. These tests build classes that carry the markers, and
run without Pydantic.
"""

import dataclasses
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
