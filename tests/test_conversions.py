"""Tests of the conversion table: entries, lookup, and inheritance."""

from __future__ import annotations

import datetime
import decimal
import enum
import ipaddress
import uuid
from collections.abc import Mapping
from typing import Any, ClassVar, Literal, NewType, Protocol

import pytest
import requests_mock
from typing_extensions import TypedDict

from eazyrest import (
    API,
    DEFAULT_CONVERSIONS,
    Conversion,
    FieldConversionError,
    JSONObject,
    encode_json,
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


class Hosts(TypedDict):
    """Mapping that holds an address."""

    primary: ipaddress.IPv4Address


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
    hosts: Hosts
    by_name: dict[str, ipaddress.IPv4Address]
    alias: Address
    when: datetime.datetime
    stamp: Stamp
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
    "hosts": {"primary": "10.0.0.3", "server": 1},
    "by_name": {"a": "10.0.0.4"},
    "alias": "10.0.0.5",
    "when": "2026-01-02T03:04:05Z",
    "stamp": "2026-01-02T03:04:05Z",
    "level": 1,
    "user": 7,
    "start": "2026-01-02T03:04:05Z",
}


def test_table_entries_convert_wherever_their_type_appears() -> None:
    """A class's entry converts its type in fields, lists, and mappings."""
    host = Host(json=dict(HOST_JSON))

    assert host.address == ipaddress.IPv4Address("10.0.0.1")
    assert host.allowed == [ipaddress.IPv4Address("10.0.0.2")]
    assert host.hosts == {
        "primary": ipaddress.IPv4Address("10.0.0.3"),
        "server": 1,
    }
    assert host.by_name == {"a": ipaddress.IPv4Address("10.0.0.4")}

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
    assert host.json["stamp"] == "2026-01-02T03:04:05+00:00"

    @json_object
    class Stamps(TableBase):
        class_url = "/stamps/"

        id: int
        stamps: list[Stamp]

    stamps = Stamps(json={"id": 1, "stamps": ["2026-01-02T03:04:05Z"]})
    stamps.stamps.append(stamps.stamps[0])
    with requests_mock.Mocker() as mock:
        mock.patch("https://table.example.com/stamps/1/", json={"id": 1})
        stamps.save()
        assert mock.last_request.json() == {
            "stamps": ["2026-01-02T03:04:05+00:00"] * 2
        }
    assert encode_json(host.stamp, Stamp) == "2026-01-02T03:04:05+00:00"


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
        datetime.time,
        datetime.timedelta,
        uuid.UUID,
        decimal.Decimal,
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


class SchemaBase(JSONObject):
    """Base whose conversions give schemas, as a mapping and a function."""

    conversions: ClassVar[Mapping[Any, Conversion]] = {
        ipaddress.IPv4Address: Conversion(
            decode=lambda value, cls: cls(value),
            encode=lambda value, cls: str(value),
            schema={"type": "string", "format": "ipv4"},
        ),
        Stamp: Conversion(
            decode=lambda value, cls: value,
            encode=lambda value, cls: value,
            schema=lambda cls, mode: {"title": f"{cls.__name__} {mode}"},
        ),
    }


@json_object
class Schemas(SchemaBase):
    """Model whose fields' schemas come from conversions."""

    class_url = "/schemas/"

    id: int
    address: ipaddress.IPv4Address
    addresses: list[ipaddress.IPv4Address]
    stamp: Stamp
    when: datetime.datetime
    user: UserId
    start: Start
    plain: uuid.UUID


def test_schemas_come_from_conversions() -> None:
    """A conversion's schema, a mapping or a function of type and mode.

    A ``NewType`` without an entry has its supertype's schema, and a type
    with no schema in its conversion allows any value.
    """
    read = Schemas.json_schema()["properties"]
    write = Schemas.json_schema("write")["properties"]

    assert read["address"] == {"type": "string", "format": "ipv4"}
    assert read["addresses"] == {
        "type": "array",
        "items": {"type": "string", "format": "ipv4"},
    }
    assert read["stamp"] == {"title": "Stamp read"}
    assert write["stamp"] == {"title": "Stamp write"}
    assert read["when"] == {"type": "string", "format": "date-time"}
    assert read["user"] == {"type": "integer"}
    assert read["start"] == {"type": "string", "format": "date-time"}
    assert read["plain"] == {"type": "string", "format": "uuid"}
    # The base's address conversion gives no schema.
    assert Host.json_schema()["properties"]["address"] == {}


class Token:
    """A value whose conversion's schema has a definition of its own."""

    def __init__(self, part: int) -> None:
        """Make a token.

        Args:
            part: Its part.
        """
        self.part = part


class Chain:
    """A value whose conversion's schema defines a recursive ``Part``."""

    def __init__(self, links: list[Any]) -> None:
        """Make a chain.

        Args:
            links: Its links, as JSON.
        """
        self.links = links


@json_object
class Defined(JSONObject):
    """Model whose conversions' schemas define the same name differently."""

    conversions: ClassVar[Mapping[Any, Conversion]] = {
        Token: Conversion(
            decode=lambda value, cls: cls(value["part"]),
            encode=lambda value, cls: {"part": value.part},
            schema={
                "type": "object",
                "properties": {"part": {"$ref": "#/$defs/Part"}},
                "$defs": {"Part": {"type": "integer"}},
            },
        ),
        Chain: Conversion(
            decode=lambda value, cls: cls(value),
            encode=lambda value, cls: value.links,
            schema={
                "$ref": "#/$defs/Part",
                "$defs": {
                    "Part": {
                        "type": "array",
                        "items": {"$ref": "#/$defs/Part"},
                    }
                },
            },
        ),
    }

    id: int
    token: Token
    tokens: list[Token]
    chain: Chain | None


@pytest.mark.parametrize("mode", ["read", "write"])
def test_conversion_schemas_keep_their_definitions(
    mode: Literal["read", "write"],
) -> None:
    """A conversion's own ``$defs`` move into the model's, renamed apart.

    Its references keep naming its own definitions, also when another
    conversion defines the same name, or a definition refers to itself.

    Args:
        mode: Schema mode.
    """
    schema = Defined.json_schema(mode)
    properties = schema["properties"]

    assert sorted(schema["$defs"]) == ["Part", "Part_2"]
    assert properties["token"]["properties"]["part"] == {
        "$ref": "#/$defs/Part"
    }
    assert properties["tokens"]["items"] == properties["token"]
    assert properties["chain"]["anyOf"][0] == {"$ref": "#/$defs/Part_2"}
    assert schema["$defs"]["Part_2"]["items"] == {"$ref": "#/$defs/Part_2"}


class FirstCode(str):
    """String whose conversion's schema defines a name."""


class SecondCode(str):
    """String whose conversion's schema defines the same name differently."""


@pytest.mark.parametrize("mode", ["read", "write"])
@pytest.mark.parametrize(
    ("first", "second", "value", "renamed"),
    [
        (
            {"$defs": {"Part": {"type": "integer"}}, "$ref": "#/$defs/Part"},
            {
                "$defs": {
                    "Part": {"properties": {"value": {"type": "string"}}}
                },
                "$ref": "#/$defs/Part/properties/value",
            },
            "ok",
            {"$ref": "#/$defs/Part_2/properties/value"},
        ),
        (
            {"$defs": {"a/b": {"type": "integer"}}, "$ref": "#/$defs/a~1b"},
            {"$defs": {"a/b": {"type": "string"}}, "$ref": "#/$defs/a~1b"},
            "ok",
            {"$ref": "#/$defs/a~1b_2"},
        ),
        (
            {"$defs": {"Part": {"type": "integer"}}, "$ref": "#/$defs/Part"},
            {
                "$defs": {"Part": {"type": "string"}},
                "const": {"$ref": "#/$defs/Part"},
            },
            {"$ref": "#/$defs/Part"},
            {"const": {"$ref": "#/$defs/Part"}},
        ),
        (
            {"$defs": {"Part": {"type": "integer"}}, "$ref": "#/$defs/Part"},
            {
                "$defs": {"Part": {"type": "string"}},
                "type": "object",
                "properties": {"$ref": {"$ref": "#/$defs/Part"}},
            },
            {"$ref": "x"},
            {
                "type": "object",
                "properties": {"$ref": {"$ref": "#/$defs/Part_2"}},
            },
        ),
    ],
    ids=["pointer", "escaped", "const", "property"],
)
def test_renamed_definitions_keep_what_their_schemas_mean(
    first: dict[str, Any],
    second: dict[str, Any],
    value: Any,
    renamed: dict[str, Any],
    mode: Literal["read", "write"],
) -> None:
    """A model's schema accepts what each conversion's own schema accepts.

    Renaming a definition renames it in a reference into the definition and
    in one escaped as a JSON Pointer, and leaves alone a ``const``, which is
    data, and a property named ``$ref``.

    Args:
        first: Schema of ``FirstCode``, which accepts 1.
        second: Schema of ``SecondCode``, which defines a name that
            ``first`` does, and accepts ``value``.
        value: JSON of a ``SecondCode``.
        renamed: Schema of ``SecondCode`` in the model's schema, where its
            definition is renamed.
        mode: Schema mode.
    """
    model: Any = json_object(
        type(
            "Codes",
            (JSONObject,),
            {
                "__annotations__": {"a": FirstCode, "b": SecondCode},
                "conversions": {
                    FirstCode: Conversion(
                        decode=lambda value, cls: value,
                        encode=lambda value, cls: value,
                        schema=first,
                    ),
                    SecondCode: Conversion(
                        decode=lambda value, cls: value,
                        encode=lambda value, cls: value,
                        schema=second,
                    ),
                },
            },
        )
    )

    schema = model.json_schema(mode)

    assert schema["properties"]["b"] == renamed
