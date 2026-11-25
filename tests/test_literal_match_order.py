"""A ``Literal`` matches a member's own JSON before any enum's rule.

A value that a ``Literal`` of enum members and plain values reads is matched in
three passes. The first looks for the JSON of any enum member. The second looks
for any plain member. The third asks each enum, in the order that the
``Literal`` lists the enums, whether its conversion reads the value as a member
that the ``Literal`` holds, and the first that does decides.
"""

from __future__ import annotations

import enum
from typing import Any, Literal

import pytest
import requests_mock

from eazyrest import API, JSONObject, json_object

BASE = "https://literal-order.example/"
api = API(BASE)


class TicketStatus(str, enum.Enum):  # noqa: UP042
    """An enum that maps every value it lacks to ``UNKNOWN``."""

    OPEN = "open"
    CLOSED = "closed"
    UNKNOWN = "unknown"

    @classmethod
    def _missing_(cls, value: object) -> Any:
        """Read any other value as ``UNKNOWN``."""
        return cls.UNKNOWN


class First(enum.Enum):
    """An enum that maps every value it lacks to its only member."""

    A = "a"

    @classmethod
    def _missing_(cls, value: object) -> Any:
        """Read any other value as ``A``."""
        return cls.A


class Second(str, enum.Enum):  # noqa: UP042
    """An enum that maps every value it lacks to its only member."""

    B = "b"

    @classmethod
    def _missing_(cls, value: object) -> Any:
        """Read any other value as ``B``."""
        return cls.B


class Broken(enum.Enum):
    """An enum whose ``_missing_()`` returns what Python refuses."""

    X = "x"

    @classmethod
    def _missing_(cls, value: object) -> Any:
        """Return an object that is no member, which raises ``TypeError``."""
        return "not a member"


@json_object
class Ticket(JSONObject):
    """A model with a plain member beside enum members."""

    class_url = "/tickets/"
    id: int
    title: str
    status: Literal[TicketStatus.OPEN, TicketStatus.UNKNOWN, "archived"]


@json_object
class Doc(JSONObject):
    """A model with a ``Literal`` of two enums, in either order."""

    class_url = "/docs/"
    id: int
    first_then_second: Literal[First.A, Second.B]
    second_then_first: Literal[Second.B, First.A]
    first_then_plain: Literal[First.A, "p", Second.B]
    broken: Literal[Broken.X, Second.B]
    only_broken: Literal[Broken.X]


Ticket.register_api(api)
Doc.register_api(api)


def _read(field: str, raw: Any) -> Any:
    return getattr(Doc(json={"id": 1, field: raw}), field)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("open", TicketStatus.OPEN),
        ("unknown", TicketStatus.UNKNOWN),
        ("archived", "archived"),
        ("other", TicketStatus.UNKNOWN),
        # CLOSED is a member of the enum, not of the Literal.
        ("closed", "closed"),
    ],
)
def test_a_plain_member_is_read_before_an_enums_rule(
    raw: str, expected: Any
) -> None:
    """``"archived"`` is a member of the ``Literal``, not ``UNKNOWN``."""
    read = Ticket(json={"id": 1, "status": raw}).status
    assert read == expected
    assert type(read) is type(expected)


def test_a_plain_member_that_is_assigned_is_read_and_saved() -> None:
    """The member survives an assignment, a read, and a full save."""
    with requests_mock.Mocker() as mocker:
        mocker.get(
            BASE + "tickets/1/",
            json={"id": 1, "title": "t", "status": "archived"},
        )
        mocker.get(
            BASE + "tickets/2/", json={"id": 2, "title": "t", "status": "open"}
        )
        mocker.put(BASE + "tickets/1/", json={})
        mocker.patch(BASE + "tickets/2/", json={})
        ticket = Ticket(pk=1, write_mode="lazy")
        assert ticket.status == "archived"
        ticket.title = "new"
        ticket.save(full=True)
        assert mocker.last_request.json() == {
            "id": 1,
            "title": "new",
            "status": "archived",
        }
        other = Ticket(pk=2, write_mode="lazy")
        other.status = "archived"
        assert other.status == "archived"
        other.save()
        assert mocker.last_request.json() == {"status": "archived"}


@pytest.mark.parametrize(
    ("field", "raw", "expected"),
    [
        # A member's own JSON wins over the rule of an enum that lists first.
        ("first_then_second", "b", Second.B),
        ("first_then_second", "a", First.A),
        ("second_then_first", "a", First.A),
        # Each enum's rule applies in the order of the Literal.
        ("first_then_second", "zzz", First.A),
        ("second_then_first", "zzz", Second.B),
        # A plain member is read before any rule.
        ("first_then_plain", "p", "p"),
        ("first_then_plain", "zzz", First.A),
        # A rule that raises TypeError is skipped.
        ("broken", "zzz", Second.B),
        ("broken", "x", Broken.X),
    ],
)
def test_the_rules_of_enums_apply_in_the_order_that_the_literal_lists_them(
    field: str, raw: str, expected: Any
) -> None:
    """The first rule that finds a member that the ``Literal`` holds wins."""
    read = _read(field, raw)
    assert read == expected
    assert type(read) is type(expected)


def test_a_value_that_no_rule_maps_to_a_member_is_kept() -> None:
    """A ``Literal`` does not check, so the value is read as it is."""
    assert _read("only_broken", 5) == 5
