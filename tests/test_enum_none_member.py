"""An enum with a member whose value is ``None`` is refused at decoration."""

from __future__ import annotations

import enum
from typing import Any, Literal

import pytest

from eazyrest import Conversion, JSONObject, json_object


class Status(enum.Enum):
    """An enum with a member that stands for no value."""

    UNKNOWN = None
    OPEN = "open"


class Plain(enum.Enum):
    """An enum without such a member."""

    OPEN = "open"


def declare(annotation: Any, **namespace: Any) -> Any:
    """Decorate a model with a field of the given annotation."""
    model: Any = type(
        "Ticket",
        (JSONObject,),
        {
            "__annotations__": {"id": int, "status": annotation},
            "class_url": "/tickets/",
            **namespace,
        },
    )
    return json_object(model)


@pytest.mark.parametrize(
    "annotation",
    [
        Status,
        Status | None,
        list[Status],
        dict[str, Status],
    ],
    ids=["plain", "optional", "list", "mapping"],
)
def test_the_member_is_refused_naming_the_field_and_the_member(
    annotation: Any,
) -> None:
    """The error says which member and that no field would read it."""
    with pytest.raises(TypeError, match=r"Ticket\.status") as caught:
        declare(annotation)
    message = str(caught.value)
    assert "UNKNOWN" in message
    assert "Status" in message


def test_an_enum_without_the_member_is_admitted() -> None:
    """The refusal is for the member, not for enums."""
    declare(Plain)
    declare(Plain | None)


def test_a_literal_may_hold_none() -> None:
    """``Literal[None]`` is not an enum member and reads ``None``."""
    ticket = declare(Literal["open"] | None)(json={"id": 1, "status": None})
    assert ticket.status is None


def test_an_enum_with_a_conversion_of_its_own_is_not_checked() -> None:
    """A conversion of the enum decides how it reads, so it is not refused."""
    declare(
        Status,
        conversions={
            Status: Conversion(
                decode=lambda value, cls: cls.OPEN,
                encode=lambda value, cls: "open",
            )
        },
    )
