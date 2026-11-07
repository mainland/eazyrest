"""Tests of the conversion table: entries, lookup, and inheritance."""

from __future__ import annotations

import collections
import dataclasses
import datetime
import decimal
import enum
import ipaddress
import uuid
from collections.abc import (
    Collection,
    Iterable,
    Mapping,
    MutableMapping,
    MutableSequence,
    MutableSet,
    Sequence,
)
from collections.abc import Set as AbstractSet
from typing import Any, ClassVar, Literal, NamedTuple, NewType, Protocol

import jsonschema
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
            for key in (
                object,
                bool,
                int,
                float,
                str,
                type(None),
                list,
                tuple,
                dict,
                set,
                frozenset,
                collections.deque,
                Iterable,
                Collection,
                Sequence,
                MutableSequence,
                AbstractSet,
                MutableSet,
                Mapping,
                MutableMapping,
            )
        ),
        ({Host: ADDRESS}, "the key Host, a model"),
        ({NewType("Owner", Host): ADDRESS}, "the key Owner, a model or a"),
        ({NewType("Ints", list[int]): ADDRESS}, "which is not a class"),
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
    assert read["when"] == {
        "anyOf": [
            {"type": "string", "format": "date-time"},
            {"type": "number"},
        ]
    }
    assert read["user"] == {"type": "integer"}
    assert read["start"] == {
        "anyOf": [
            {"type": "string", "format": "date-time"},
            {"type": "number"},
        ]
    }
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

    good = {"id": 1, "token": {"part": 1}, "tokens": [], "chain": [[], [[]]]}
    jsonschema.validate(good, schema)
    for bad in ({"token": {"part": "x"}}, {"chain": [1]}):
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate({**good, **bad}, schema)


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
    assert jsonschema.Draft202012Validator(first).is_valid(1)
    assert jsonschema.Draft202012Validator(second).is_valid(value)
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
    validator = jsonschema.Draft202012Validator(schema)
    assert validator.is_valid({"a": 1, "b": value})
    assert not validator.is_valid({"a": "x", "b": value})


@dataclasses.dataclass
class Money:
    """Dataclass whose JSON form is a string, such as ``"12 USD"``."""

    amount: int
    currency: str


@dataclasses.dataclass
class Tagged(Money):
    """Subclass of ``Money``, which converts by ``Money``'s entry."""

    tag: str = ""


class Point(NamedTuple):
    """``NamedTuple`` whose JSON form is a string, such as ``"1,2"``."""

    x: int
    y: int


class Pair(TypedDict):
    """``TypedDict`` whose JSON form is a string, such as ``"a=b"``."""

    key: str
    value: str


class LongPair(Pair):
    """Subclass of ``Pair``, which an exact ``TypedDict`` entry skips."""


@dataclasses.dataclass
class Owned:
    """Dataclass that holds a model, which eazyrest refuses to structure."""

    host: Host


Price = NewType("Price", Money)
Fee = NewType("Fee", Money)
Stamps = NewType("Stamps", list[datetime.datetime])


def _money(value: Any, cls: type[Any]) -> Money:
    """Decode ``"12 USD"``.

    Args:
        value: The JSON string.
        cls: The declared type.

    Returns:
        The money.
    """
    del cls
    amount, currency = value.split()
    return Money(int(amount), currency)


class ClassBase(JSONObject):
    """Base whose table converts classes that eazyrest would structure."""

    conversions: ClassVar[Mapping[Any, Conversion]] = {
        Money: Conversion(
            decode=_money,
            encode=lambda value, cls: f"{value.amount} {value.currency}",
            schema={"type": "string", "pattern": "^[0-9]+ [A-Z]{3}$"},
        ),
        Point: Conversion(
            decode=lambda value, cls: cls(*map(int, value.split(","))),
            encode=lambda value, cls: f"{value.x},{value.y}",
        ),
        Pair: Conversion(
            decode=lambda value, cls: dict(
                zip(("key", "value"), value.split("="), strict=True)
            ),
            encode=lambda value, cls: f"{value['key']}={value['value']}",
        ),
        Fee: Conversion(
            decode=lambda value, cls: Money(value, "USD"),
            encode=lambda value, cls: value.amount,
        ),
        Owned: Conversion(
            decode=lambda value, cls: cls(Host(id=value)),
            encode=lambda value, cls: value.host.id,
        ),
    }


ClassBase.register_api(API("https://classes.example.com/"))


@json_object
class Item(ClassBase):
    """Model with fields of classes that its table converts."""

    class_url = "/items/"

    id: int
    price: Money
    prices: list[Money]
    tagged: Tagged
    point: Point
    pair: Pair
    long_pair: LongPair
    listed: Price
    fee: Fee
    owned: Owned
    stamps: Stamps


ITEM_JSON: dict[str, Any] = {
    "id": 1,
    "price": "12 USD",
    "prices": ["1 EUR"],
    "tagged": "3 GBP",
    "point": "1,2",
    "pair": "a=b",
    "long_pair": {"key": "c", "value": "d"},
    "listed": "4 USD",
    "fee": 5,
    "owned": 7,
    "stamps": ["2026-01-02T03:04:05Z"],
}


def test_entries_replace_the_structure_of_classes() -> None:
    """A dataclass's, ``NamedTuple``'s, or ``TypedDict``'s entry converts it.

    A subclass converts by its base's entry, except that a ``TypedDict``'s
    entry converts exactly that ``TypedDict``, whose subclasses do not have
    it in their MRO.
    """
    item = Item(json=dict(ITEM_JSON))

    assert item.price == Money(12, "USD")
    assert item.prices == [Money(1, "EUR")]
    assert item.tagged == Money(3, "GBP")
    assert item.point == Point(1, 2)
    assert item.pair == {"key": "a", "value": "b"}
    assert item.long_pair == {"key": "c", "value": "d"}

    item.price = Money(13, "USD")
    item.tagged = Tagged(14, "EUR", "sale")
    item.point = Point(3, 4)
    item.pair = {"key": "e", "value": "f"}
    assert item.json["price"] == "13 USD"
    assert item.json["tagged"] == "14 EUR"
    assert item.json["point"] == "3,4"
    assert item.json["pair"] == "e=f"


def test_an_entry_writes_other_values_as_they_are() -> None:
    """A class entry writes a value that is not an instance as it is.

    It must be JSON, as for an entry of a leaf type.
    """
    item = Item(json=dict(ITEM_JSON))

    item.price = "15 USD"  # type: ignore[assignment]
    item.point = "5,6"  # type: ignore[assignment]
    assert item.json["price"] == "15 USD"
    assert item.json["point"] == "5,6"
    with pytest.raises(FieldConversionError, match="does not encode to JSON"):
        item.price = object()  # type: ignore[assignment]
    assert item.json["price"] == "15 USD"


def test_new_types_of_classes_convert_by_their_own_entry_or_the_class() -> (
    None
):
    """A ``NewType`` of a class uses its own entry, else the class's.

    A ``NewType`` of a collection converts as the collection.
    """
    item = Item(json=dict(ITEM_JSON))

    assert item.listed == Money(4, "USD")
    assert item.fee == Money(5, "USD")
    assert item.stamps == [WHEN]
    item.fee = Fee(Money(6, "USD"))
    assert item.json["fee"] == 6


def test_an_entry_converts_a_class_that_eazyrest_would_refuse() -> None:
    """An entry for a dataclass that holds a model replaces its structure."""
    item = Item(json=dict(ITEM_JSON))

    assert item.owned.host.id == 7
    item.owned = Owned(Host(id=8))
    assert item.json["owned"] == 8


def test_class_entries_give_schemas_queries_and_encode_json() -> None:
    """``json_schema()``, query values, and ``encode_json()`` use the entry."""
    schema = Item.json_schema()["properties"]

    assert schema["price"] == {
        "type": "string",
        "pattern": "^[0-9]+ [A-Z]{3}$",
    }
    assert schema["point"] == {}
    assert schema["long_pair"]["type"] == "object"
    assert encode_json(Money(1, "USD"), Money) == {
        "amount": 1,
        "currency": "USD",
    }
    with requests_mock.Mocker() as mock:
        mock.get("https://classes.example.com/items/", json=[])
        assert list(Item.filter(price=Money(2, "USD"))) == []
        assert mock.last_request.qs == {"price": ["2 usd"]}


def test_writes_that_are_not_json_raise_when_assigned() -> None:
    """A value that is not JSON raises, naming the field, and is not kept.

    So does one that an entry's ``encode`` returns.
    """

    class BadEncodeBase(JSONObject):
        conversions: ClassVar[Mapping[Any, Conversion]] = {
            ipaddress.IPv6Address: Conversion(
                decode=lambda value, cls: cls(value),
                encode=lambda value, cls: value,
            )
        }

    BadEncodeBase.register_api(API("https://bad-encode.example.com/"))

    @json_object
    class Plain(BadEncodeBase):
        class_url = "/plain/"

        id: int
        when: datetime.datetime
        name: str
        address: ipaddress.IPv4Address
        v6: ipaddress.IPv6Address

    plain = Plain(
        json={"id": 1, "when": None, "name": "a", "address": "", "v6": "::"}
    )
    assignments: list[tuple[str, Any]] = [
        ("when", datetime.date(2026, 1, 2)),
        ("name", WHEN),
        ("address", ipaddress.IPv4Address("10.0.0.1")),
        ("v6", ipaddress.IPv6Address("::1")),
    ]
    for field, value in assignments:
        with pytest.raises(
            FieldConversionError, match=rf"^Plain\.{field}: .*does not encode"
        ):
            setattr(plain, field, value)
    assert plain.json == {
        "id": 1,
        "when": None,
        "name": "a",
        "address": "",
        "v6": "::",
    }


class Shade(enum.Enum):
    """Enum whose members a ``Literal`` names."""

    DARK = "dark"
    LIGHT = "light"


class NamedBase(JSONObject):
    """Base whose table writes ``Shade`` members by name."""

    conversions: ClassVar[Mapping[Any, Conversion]] = {
        Shade: Conversion(
            decode=lambda value, cls: cls[value],
            encode=lambda value, cls: value.name,
        )
    }


NamedBase.register_api(API("https://named.example.com/"))


@json_object
class Shaded(TableBase):
    """Model with ``Literal`` fields of enum members."""

    class_url = "/shaded/"

    id: int
    shade: Literal[Shade.DARK, "none"]
    level: Literal[Level.LOW]


@json_object
class Named(NamedBase):
    """Model whose ``Literal`` members encode by name."""

    class_url = "/named/"

    id: int
    shade: Literal[Shade.DARK]


def test_literals_read_and_write_their_enum_members() -> None:
    """A ``Literal``'s enum member reads from, and writes, its JSON value.

    The member's JSON value is its conversion's, from the model's table.
    Another value reads as it is, and ``true`` is not the member whose
    value is 1.
    """
    shaded = Shaded(json={"id": 1, "shade": "dark", "level": 1})
    named = Named(json={"id": 1, "shade": "DARK"})

    assert shaded.shade is Shade.DARK
    assert shaded.level is Level.LOW
    assert named.shade is Shade.DARK
    unchecked: Any = Shaded(json={"id": 1, "shade": "none", "level": True})
    assert unchecked.level is True
    shaded.shade = Shade.DARK
    named.shade = Shade.DARK
    assert shaded.json["shade"] == "dark"
    assert named.json["shade"] == "DARK"
    assert Shaded.json_schema()["properties"]["shade"] == {
        "enum": ["dark", "none"]
    }
    assert Named.json_schema()["properties"]["shade"] == {"enum": ["DARK"]}


def test_collection_errors_name_the_item() -> None:
    """A failure in a collection's item names its position."""
    host = Host(json={**HOST_JSON, "allowed": ["10.0.0.1", "bad"]})

    with pytest.raises(FieldConversionError, match=r"^Host\.allowed: \[1\]: "):
        _ = host.allowed
    with pytest.raises(FieldConversionError, match=r"^Host\.allowed: \[0\]: "):
        host.allowed = [WHEN]  # type: ignore[list-item]


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf")])
def test_floats_that_are_not_finite_raise_when_assigned(value: float) -> None:
    """JSON has no NaN or infinity, so assigning one raises and keeps nothing.

    Args:
        value: Float that is not finite.
    """

    @json_object
    class Reading(TableBase):
        class_url = "/readings/"

        id: int
        level: float

    reading = Reading(json={"id": 1, "level": 0.5})

    with pytest.raises(FieldConversionError, match="not finite"):
        reading.level = value
    assert reading.json["level"] == 0.5


Ends = NewType("Ends", tuple)  # type: ignore[type-arg]
Ids = NewType("Ids", list[int])

ENDS = Conversion(
    decode=lambda value, cls: tuple(int(part) for part in value.split(",")),
    encode=lambda value, cls: ",".join(str(part) for part in value),
    schema={"type": "string"},
)


class EndsBase(JSONObject):
    """Base whose table converts a ``NewType`` of ``tuple``."""

    conversions: ClassVar[Mapping[Any, Conversion]] = {Ends: ENDS}


EndsBase.register_api(API("https://ends.example.com/"))


@json_object
class Segment(EndsBase):
    """Model with ``NewType`` fields over collections."""

    class_url = "/segments/"

    id: int
    ends: Ends
    more: list[Ends | None]
    ids: Ids


def test_a_new_type_of_a_collection_converts_by_its_own_entry() -> None:
    """A ``NewType`` of a tuple converts by its entry, wherever it appears."""
    segment = Segment(
        json={"id": 1, "ends": "1,2", "more": ["3,4", None], "ids": [5, 6]}
    )

    assert segment.ends == (1, 2)
    assert segment.more == [(3, 4), None]
    segment.ends = Ends((7, 8))
    segment.more = [Ends((7, 8)), None]
    assert segment.json["ends"] == "7,8"
    assert segment.json["more"] == ["7,8", None]
    for mode in ("read", "write"):
        properties = Segment.json_schema(mode)["properties"]
        assert properties["ends"] == {"type": "string"}
        assert properties["more"]["items"] == {
            "anyOf": [{"type": "string"}, {"type": "null"}]
        }


def test_a_new_type_of_a_collection_without_an_entry_is_its_supertype() -> (
    None
):
    """Without an entry, a ``NewType`` of a list converts and queries as one.

    A single query value is one item of the list, as for a list field.
    """
    segment = Segment(json={"id": 1, "ends": "1,2", "more": [], "ids": [5]})

    assert segment.ids == [5]
    assert Segment.json_schema()["properties"]["ids"] == {
        "type": "array",
        "items": {"type": "integer"},
    }
    with requests_mock.Mocker() as mock:
        mock.get("https://ends.example.com/segments/", json=[])
        list(Segment.filter(ids=5))
        assert mock.last_request.qs == {"ids": ["5"]}
        list(Segment.filter(ids=Ids([5, 6])))
        assert mock.last_request.qs == {"ids": ["5", "6"]}


class _Count(TypedDict):
    """``TypedDict`` keyed through a chain of ``NewType``s."""

    n: int


# mypy refuses a NewType of a TypedDict, which Python allows.
_CountId = NewType("_CountId", _Count)  # type: ignore[valid-newtype]
_CountRef = NewType("_CountRef", _CountId)


def test_a_new_type_of_a_typed_dict_converts_by_its_entry() -> None:
    """An entry keyed by a ``NewType`` of a ``TypedDict`` encodes mappings.

    A ``TypedDict`` has no instances, so its entry encodes any mapping,
    through a chain of ``NewType``s too.
    """

    class CountBase(JSONObject):
        conversions: ClassVar[Mapping[Any, Conversion]] = {
            _CountId: Conversion(
                decode=lambda value, cls: {"n": int(value)},
                encode=lambda value, cls: str(value["n"]),
                schema={"type": "string"},
            )
        }

    CountBase.register_api(API("https://counts.example.com/"))

    @json_object
    class Tally(CountBase):
        class_url = "/tallies/"

        id: int
        count: _CountRef

    tally: Any = Tally(json={"id": 1, "count": "3"})

    assert tally.count == {"n": 3}
    tally.count["n"] = 5
    with requests_mock.Mocker() as mock:
        patch = mock.patch(
            "https://counts.example.com/tallies/1/", status_code=204
        )
        tally.save()
    assert patch.last_request.json() == {"count": "5"}


def test_enum_members_are_not_read_across_booleans_and_numbers() -> None:
    """JSON true is not the member whose value is 1, nor 1 the member true.

    Python finds them equal, but the schema, which lists the members'
    values, does not.
    """

    class Level(enum.IntEnum):
        LOW = 1

    class Flag(enum.Enum):
        ON = True

    for cls, value in ((Level, True), (Flag, 1)):
        model: Any = json_object(
            type(
                "Holder",
                (JSONObject,),
                {"__annotations__": {"id": int, "v": cls}},
            )
        )
        with pytest.raises(FieldConversionError, match="not a valid"):
            _ = model(json={"id": 1, "v": value}).v

    level: Any = json_object(
        type(
            "Holder",
            (JSONObject,),
            {"__annotations__": {"id": int, "v": Level}},
        )
    )
    assert level(json={"id": 1, "v": 1}).v is Level.LOW


@pytest.mark.parametrize(
    "schema",
    [
        {"type": "array", "items": {"$ref": "#"}},
        {"$id": "https://example.com/tree", "type": "object"},
        {"$anchor": "node", "type": "object"},
        {"$defs": {"A": {"$defs": {"B": {}}}}, "$ref": "#/$defs/A"},
        {"$ref": "other.json"},
        {"items": {"$dynamicRef": "#node"}},
    ],
    ids=["root", "id", "anchor", "nested-defs", "external", "dynamic"],
)
def test_schemas_that_cannot_be_embedded_are_refused(
    schema: dict[str, Any],
) -> None:
    """A conversion's schema may refer only to its own ``$defs``.

    ``json_schema()`` moves its definitions into the model's, so any other
    reference would name the model's root or nothing.

    Args:
        schema: A schema that could not be embedded.
    """
    with pytest.raises(TypeError, match="conversion's schema"):
        Conversion(decode=lambda v, c: v, encode=lambda v, c: v, schema=schema)

    @json_object
    class Late(JSONObject):
        conversions: ClassVar[Mapping[Any, Conversion]] = {
            Token: Conversion(
                decode=lambda v, c: v,
                encode=lambda v, c: v,
                schema=lambda cls, mode: schema,
            )
        }
        id: int
        token: Token

    with pytest.raises(TypeError, match="conversion's schema"):
        Late.json_schema()


def test_data_that_looks_like_a_reference_is_allowed() -> None:
    """A ``const`` or a property named ``$id`` is not a reference."""
    Conversion(
        decode=lambda v, c: v,
        encode=lambda v, c: v,
        schema={
            "type": "object",
            "properties": {"$id": {"type": "string"}},
            "const": {"$ref": "#"},
        },
    )
