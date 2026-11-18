"""Tests of the refusals that wait for a field's first use.

A member of a structure that names a class defined later, or a late class that
has no conversion, is refused when the field is first used. Each operation that
uses the field raises ``FieldConversionError`` naming it, as a read does.
"""

import dataclasses
from collections.abc import Callable, Iterator
from typing import Any

import pytest

from eazyrest import (
    API,
    FieldConversionError,
    JSONObject,
    json_object,
)

BASE = "https://first-use.example.com/"


class Plain:
    """Class of an application that nothing converts."""


@dataclasses.dataclass
class Waiting:
    """Dataclass whose member names a class that is defined later."""

    inner: "LaterPlain"  # type: ignore[name-defined]  # noqa: F821


@dataclasses.dataclass
class Missing:
    """Dataclass whose member names a class that is never defined."""

    inner: "NeverDefined"  # type: ignore[name-defined]  # noqa: F821


def _model(hint: Any) -> Any:
    """Decorate a model with one field ``value`` of a type.

    Args:
        hint: Annotation of the field.

    Returns:
        The decorated class, with an API registered.
    """
    model: Any = json_object(
        type(
            "Holder",
            (JSONObject,),
            {
                "__module__": __name__,
                "class_url": "/holders/",
                "__annotations__": {"id": int, "value": hint},
            },
        )
    )
    model.register_api(API(BASE))
    return model


def _filter(model: Any) -> Any:
    return list(model.filter(value=1))


def _get(model: Any) -> Any:
    return model.get(value=1)


def _get_page(model: Any) -> Any:
    return model.get_page(value=1)


def _get_or_create(model: Any) -> Any:
    return model.get_or_create(value=1)


def _get_or_create_of_a_generator(model: Any) -> Any:
    return model.get_or_create(value=(item for item in [1, 2]))


def _prefetch(model: Any) -> Any:
    return list(model.filter(prefetch="value"))


def _save_in_full(model: Any) -> Any:
    model(json={"id": 1, "value": {"inner": 1}}).save(full=True)


_ENTRY_POINTS: dict[str, Callable[[Any], Any]] = {
    "filter": _filter,
    "get": _get,
    "get_page": _get_page,
    "get_or_create": _get_or_create,
    "get_or_create_of_a_generator": _get_or_create_of_a_generator,
    "prefetch": _prefetch,
    "save_in_full": _save_in_full,
}


@pytest.fixture
def define_late_plain() -> Iterator[Callable[[], None]]:
    """Yield a function that defines the class that ``Waiting`` names.

    The model decorates before the class exists, so the test calls the function
    after it decorates one.

    Yields:
        The function that defines the class.
    """

    def define() -> None:
        globals()["LaterPlain"] = Plain

    yield define
    globals().pop("LaterPlain", None)


@pytest.mark.parametrize("entry_point", _ENTRY_POINTS)
@pytest.mark.parametrize(
    ("member", "reason"),
    [
        (Missing, "name 'NeverDefined' is not defined"),
        (Waiting, "Plain has no conversion"),
    ],
    ids=["undefined_member", "late_class_without_a_conversion"],
)
def test_an_operation_refuses_a_member_that_is_refused_at_first_use(
    entry_point: str,
    member: type[Any],
    reason: str,
    define_late_plain: Callable[[], None],
    requests_mock: Any,
) -> None:
    """Each operation raises ``FieldConversionError`` naming the field.

    Nothing is sent.

    Args:
        entry_point: Name of the operation.
        member: Structure whose member is refused when its field is first used.
        reason: Text of the reason that the error gives.
        define_late_plain: Fixture that defines the late class.
        requests_mock: Mock of the transport.
    """
    model = _model(member)
    define_late_plain()

    with pytest.raises(
        FieldConversionError, match=rf"Holder\.value.*{reason}"
    ):
        _ENTRY_POINTS[entry_point](model)

    assert not requests_mock.called
