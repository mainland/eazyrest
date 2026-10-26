"""Tests of the conversion table: entries, lookup, and inheritance."""

from __future__ import annotations

import datetime
import enum
import ipaddress
import uuid
from collections.abc import Mapping
from typing import Any, ClassVar, NewType, Protocol

import pytest

from eazyrest import (
    API,
    DEFAULT_CONVERSIONS,
    Conversion,
    FieldConversionError,
    JSONObject,
    json_object,
)

ADDRESS = Conversion(
    decode=lambda value, cls: cls(value),
    encode=lambda value, cls: str(value),
)


def _timestamp(value: Any, cls: type[Any]) -> float:
    """Encode a datetime as a Unix timestamp.

    Args:
        value: The datetime.
        cls: The declared type.

    Returns:
        Seconds since the epoch.
    """
    del cls
    return float(value.timestamp())


TIMESTAMP = Conversion(
    decode=lambda value, cls: datetime.datetime.fromtimestamp(
        value, datetime.timezone.utc
    ),
    encode=_timestamp,
)


class Stamp(datetime.datetime):
    """Subclass of ``datetime``, with no conversion of its own."""


class Level(enum.IntEnum):
    """Enum whose members are also ints."""

    LOW = 1


UserId = NewType("UserId", int)
Start = NewType("Start", datetime.datetime)
Address = NewType("Address", ipaddress.IPv4Address)
Cents = NewType("Cents", int)


class TableBase(JSONObject):
    """Base model whose table converts IPv4 addresses."""

    conversions: ClassVar[Mapping[Any, Conversion]] = {
        ipaddress.IPv4Address: ADDRESS
    }


TableBase.register_api(API("https://table.example.com/"))


@json_object
class Host(TableBase):
    """Model with fields of types that the table converts."""

    class_url = "/hosts/"

    id: int
    address: ipaddress.IPv4Address
    allowed: list[ipaddress.IPv4Address]
    alias: Address
    when: datetime.datetime
    stamp: Stamp
    stamps: list[Stamp]
    level: Level
    user: UserId
    start: Start


class TimestampBase(TableBase):
    """Base that replaces the default ``datetime`` conversion."""

    conversions: ClassVar[Mapping[Any, Conversion]] = {
        datetime.datetime: TIMESTAMP
    }


@json_object
class Event(TimestampBase):
    """Model whose datetimes are timestamps, and addresses still convert."""

    class_url = "/events/"

    id: int
    when: datetime.datetime
    address: ipaddress.IPv4Address


WHEN = datetime.datetime(2026, 1, 2, 3, 4, 5, tzinfo=datetime.timezone.utc)

HOST_JSON: dict[str, Any] = {
    "id": 1,
    "address": "10.0.0.1",
    "allowed": ["10.0.0.2"],
    "alias": "10.0.0.5",
    "when": "2026-01-02T03:04:05Z",
    "stamp": "2026-01-02T03:04:05Z",
    "stamps": ["2026-01-02T03:04:05Z"],
    "level": 1,
    "user": 7,
    "start": "2026-01-02T03:04:05Z",
}


def test_table_entries_convert_wherever_their_type_appears() -> None:
    """A class's entry converts its type in fields and lists."""
    host = Host(json=dict(HOST_JSON))

    assert host.address == ipaddress.IPv4Address("10.0.0.1")
    assert host.allowed == [ipaddress.IPv4Address("10.0.0.2")]

    host.allowed = [ipaddress.IPv4Address("10.0.0.6")]
    assert host.json["allowed"] == ["10.0.0.6"]


def test_lookup_follows_the_mro_and_new_types() -> None:
    """A subclass, an enum, and a ``NewType`` convert by their nearest entry.

    A subclass of ``datetime`` converts as a ``datetime`` and reads a plain
    one. A ``NewType`` of a type with an entry converts by that entry, and
    one of a JSON scalar reads as the scalar.
    """
    host = Host(json=dict(HOST_JSON))

    assert host.stamp == WHEN
    assert type(host.stamp) is datetime.datetime
    assert host.level is Level.LOW
    assert host.user == 7
    assert host.start == WHEN
    assert host.alias == ipaddress.IPv4Address("10.0.0.5")


def test_a_subclass_reads_values_that_write_back() -> None:
    """A field of a subclass of a key's type writes back what it reads."""
    host = Host(json=dict(HOST_JSON))

    host.stamp = host.stamp
    host.stamps = [*host.stamps, host.stamps[0]]

    assert host.json["stamp"] == "2026-01-02T03:04:05+00:00"
    assert host.json["stamps"] == ["2026-01-02T03:04:05+00:00"] * 2


def test_subclass_entries_replace_their_bases() -> None:
    """A subclass's entry replaces the default, and keeps its bases' others."""
    event = Event(json={"id": 1, "when": WHEN.timestamp(), "address": "::"})

    assert event.when == WHEN
    event.when = WHEN
    assert event.json["when"] == WHEN.timestamp()
    event.address = ipaddress.IPv4Address("10.0.0.7")
    assert event.json["address"] == "10.0.0.7"
    # The base class keeps the default datetime conversion.
    assert Host(json=dict(HOST_JSON)).when == WHEN


def test_encode_is_called_only_for_instances_of_the_type() -> None:
    """A value of another type is written as it is."""
    host = Host(json=dict(HOST_JSON))

    host.address = "10.0.0.8"  # type: ignore[assignment]
    host.when = "2026-01-02"  # type: ignore[assignment]

    assert host.json["address"] == "10.0.0.8"
    assert host.json["when"] == "2026-01-02"


def test_default_table_is_public_and_read_only() -> None:
    """The default table lists its types, and cannot be changed."""
    assert set(DEFAULT_CONVERSIONS) == {
        datetime.datetime,
        datetime.date,
        datetime.timedelta,
        uuid.UUID,
        enum.Enum,
    }
    with pytest.raises(TypeError):
        DEFAULT_CONVERSIONS[uuid.UUID] = ADDRESS  # type: ignore[index]


def test_conversion_fields_are_keywords() -> None:
    """``Conversion`` takes no positional arguments, so it can gain fields."""
    with pytest.raises(TypeError, match="positional"):
        Conversion(ADDRESS.decode, ADDRESS.encode)  # type: ignore[call-arg]


def test_entry_errors_name_the_field() -> None:
    """An entry that cannot decode a value raises ``FieldConversionError``."""
    host = Host(json={**HOST_JSON, "address": "not an address"})

    with pytest.raises(FieldConversionError, match=r"^Host\.address: "):
        _ = host.address


def test_other_errors_of_an_entry_propagate() -> None:
    """An exception other than ``TypeError`` or ``ValueError`` is a bug.

    It reaches the caller unchanged, rather than as a conversion error.
    """

    class BuggyBase(JSONObject):
        conversions: ClassVar[Mapping[Any, Conversion]] = {
            ipaddress.IPv4Address: Conversion(
                decode=lambda value, cls: value["missing"],
                encode=lambda value, cls: str(value),
            )
        }

    @json_object
    class Buggy(BuggyBase):
        class_url = "/buggy/"

        id: int
        address: ipaddress.IPv4Address

    with pytest.raises(KeyError):
        _ = Buggy(json={"id": 1, "address": {}}).address


class _Sized(Protocol):
    """Protocol that is not runtime-checkable, so no value is an instance."""

    def size(self) -> int:
        """Return the size."""
        ...


class _Mixin:
    """Class that is not a model, whose ``conversions`` a model inherits."""

    conversions: ClassVar[Any] = {Level: ADDRESS, "text": ADDRESS}


@pytest.mark.parametrize(
    ("conversions", "message"),
    [
        ({"text": ADDRESS}, "neither a type nor a NewType"),
        ({uuid.UUID: str}, r"conversions\[<class 'uuid.UUID'>\] is not a"),
        ([(uuid.UUID, ADDRESS)], "must be a mapping, got list"),
        *(
            ({key: ADDRESS}, f"the key {key.__name__}, a type that eazyrest")
            for key in (object, bool, int, float, str, type(None), list, dict)
        ),
        ({Host: ADDRESS}, "the key Host, a model"),
        # Python 3.10 refuses Any as not a type, and later Pythons, where it
        # is a class, as one that isinstance() cannot check.
        ({Any: ADDRESS}, r"the key typing\.Any, "),
        ({_Sized: ADDRESS}, "cannot be recognized with isinstance"),
        ({NewType("Sizes", _Sized): ADDRESS}, "cannot be recognized with"),
    ],
)
def test_invalid_tables_raise_when_the_class_is_defined(
    conversions: Any, message: str
) -> None:
    """A table whose key or entry is wrong raises ``TypeError`` at once.

    Args:
        conversions: The class's table.
        message: Pattern that the error should match.
    """
    with pytest.raises(TypeError, match=message):
        type("Bad", (JSONObject,), {"conversions": conversions})


def test_a_mixins_table_is_checked_by_the_model() -> None:
    """A model checks the ``conversions`` it inherits from a mixin."""
    with pytest.raises(TypeError, match="'text', which is neither"):

        class Bad(_Mixin, JSONObject):
            pass


def test_subclasses_and_new_types_of_own_types_can_be_keys() -> None:
    """An ``IntEnum`` and a ``NewType`` of ``int`` key their own entries.

    Neither entry changes how a plain ``int`` or ``bool`` field converts.
    """

    class Base(JSONObject):
        conversions: ClassVar[Mapping[Any, Conversion]] = {
            Level: Conversion(
                decode=lambda value, cls: cls[value],
                encode=lambda value, cls: value.name,
            ),
            Cents: Conversion(
                decode=lambda value, cls: round(value * 100),
                encode=lambda value, cls: value / 100,
            ),
        }

    Base.register_api(API("https://keys.example.com/"))

    @json_object
    class Price(Base):
        class_url = "/prices/"

        id: int
        level: Level
        amount: Cents
        flag: bool

    price = Price(json={"id": 1, "level": "LOW", "amount": 1.25, "flag": True})

    assert price.level is Level.LOW
    assert price.amount == 125
    assert price.flag is True
    assert price.id == 1
