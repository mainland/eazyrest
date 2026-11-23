"""``None`` as a type argument is admitted whatever its spelling."""

from __future__ import annotations

import types
import typing
from typing import Any

import pytest

from eazyrest import JSONObject, encode_json, json_object

ANNOTATIONS: dict[str, tuple[Any, Any]] = {
    "list[None]": (list[None], [None]),
    "List[None]": (typing.List[None], [None]),  # noqa: UP006
    "list[NoneType]": (list[types.NoneType], [None]),  # type: ignore[nonetype-type]
    "dict[str, None]": (dict[str, None], {"a": None}),
    "Dict[str, None]": (typing.Dict[str, None], {"a": None}),  # noqa: UP006
    "tuple[int, None]": (tuple[int, None], (1, None)),
    "Tuple[int, None]": (typing.Tuple[int, None], (1, None)),  # noqa: UP006
    "tuple[None, ...]": (tuple[None, ...], (None, None)),
}


def declare(annotation: Any) -> Any:
    """Return a model with a key and a field of the given annotation."""
    model: Any = type(
        "Holder",
        (JSONObject,),
        {
            "class_url": "/holders/",
            "__annotations__": {"id": int, "value": annotation},
        },
    )
    return json_object(model)


@pytest.mark.parametrize("name", ANNOTATIONS)
def test_none_in_a_type_argument_is_admitted_as_a_field(name: str) -> None:
    """The class decorates, and the field reads and writes its value."""
    annotation, value = ANNOTATIONS[name]
    model = declare(annotation)
    json_value = list(value) if isinstance(value, tuple) else value
    obj = model(json={"id": 1, "value": json_value})
    assert obj.value == value
    obj.value = value
    assert obj.json["value"] == json_value


@pytest.mark.parametrize("name", ANNOTATIONS)
def test_none_in_a_type_argument_is_encoded(name: str) -> None:
    """``encode_json()`` takes the annotation that a field takes."""
    annotation, value = ANNOTATIONS[name]
    expected = list(value) if isinstance(value, tuple) else value
    assert encode_json(value, annotation) == expected
