"""An enum member whose value is ``None`` is refused at decoration."""

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


@pytest.mark.parametrize(
    "annotation",
    [
        Literal[Status.UNKNOWN],
        Literal[Status.OPEN, Status.UNKNOWN],
        Literal["closed", Status.UNKNOWN],
        Literal[Status.UNKNOWN] | None,
        list[Literal[Status.UNKNOWN]],
        dict[str, Literal[Status.UNKNOWN]],
    ],
    ids=[
        "plain",
        "with-another-member",
        "with-a-string",
        "optional",
        "list",
        "mapping",
    ],
)
def test_a_literal_holding_the_member_is_refused_naming_it(
    annotation: Any,
) -> None:
    """The error names the field and the member, as for the enum."""
    with pytest.raises(TypeError, match=r"Ticket\.status") as caught:
        declare(annotation)
    assert "Status.UNKNOWN" in str(caught.value)


def test_a_literal_holding_other_members_is_admitted() -> None:
    """The refusal is for the member, not for a Literal of an enum."""
    declare(Literal[Status.OPEN])
    declare(Literal[Plain.OPEN] | None)


def test_an_enum_without_the_member_is_admitted() -> None:
    """The refusal is for the member, not for enums."""
    declare(Plain)
    declare(Plain | None)


def test_a_literal_may_hold_none() -> None:
    """``Literal[None]`` is not an enum member and reads ``None``."""
    ticket = declare(Literal["open"] | None)(json={"id": 1, "status": None})
    assert ticket.status is None


@pytest.mark.parametrize(
    "annotation", [Status, Literal[Status.UNKNOWN]], ids=["enum", "literal"]
)
def test_an_enum_with_a_conversion_of_its_own_is_not_checked(
    annotation: Any,
) -> None:
    """A conversion of the enum decides how it reads, so it is not refused."""
    declare(
        annotation,
        conversions={
            Status: Conversion(
                decode=lambda value, cls: cls.OPEN,
                encode=lambda value, cls: "open",
            )
        },
    )


@pytest.mark.parametrize(
    "annotation",
    [
        Status,
        Literal[Status.UNKNOWN],
        Literal[Status.UNKNOWN, Status.OPEN] | None,
        list[Literal[Status.UNKNOWN]],
    ],
    ids=["enum", "literal", "optional-literal", "list-of-literals"],
)
def test_an_entry_of_its_own_that_writes_none_is_not_checked(
    annotation: Any,
) -> None:
    """The entry decides, as for the enum, so a ``Literal`` is admitted too."""
    declare(
        annotation,
        conversions={
            Status: Conversion(
                decode=lambda value, cls: cls.OPEN,
                encode=lambda value, cls: None,
            )
        },
    )
