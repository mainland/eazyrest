"""A value of a subclass of ``str``, ``int``, or ``float`` is written plain.

A member of an enum that mixes in ``str``, ``int``, or ``float``, or any other
subclass of those types, is written as the plain value that it stands for,
wherever a value is encoded: a JSON body, a form field, a query, and
``encode_json()``. Python's ``str()`` of such a value is not the value, so one
that reached a form field or a query by ``str()`` would send other text than a
body sends. A name that is no field is outside the rule for a query, which
sends it as given.
"""

from __future__ import annotations

import dataclasses
import enum
import json
from collections.abc import Callable, Mapping
from typing import Any, ClassVar, Literal
from urllib.parse import parse_qsl, urlsplit

import pytest
import requests_mock
from typing_extensions import TypedDict

from eazyrest import (
    API,
    Conversion,
    FieldConversionError,
    JSONObject,
    encode_json,
    json_object,
)

BASE = "https://plain-scalars.example/"


class Status(str, enum.Enum):  # noqa: UP042
    """An enum that mixes in ``str``, whose ``str()`` is not its value."""

    OPEN = "open"


class Level(int, enum.Enum):
    """An enum that mixes in ``int``."""

    HIGH = 3


class Weight(float, enum.Enum):
    """An enum that mixes in ``float``."""

    HEAVY = 2.5


class Loud(str):
    """A subclass of ``str`` whose ``str()`` writes other text."""

    __slots__ = ()

    def __str__(self) -> str:
        """Return text that is not the value."""
        return "LOUD:" + str.__str__(self)


class Recording(API):
    """An API that keeps the JSON body of each write that it sends."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        """Make an API that has sent no body."""
        super().__init__(*args, **kwargs)
        self.bodies: list[Any] = []

    def post(self, *args: Any, **kwargs: Any) -> Any:
        """Keep the body of the request, and send it."""
        self.bodies.append(kwargs.get("json"))
        return super().post(*args, **kwargs)

    def patch(self, *args: Any, **kwargs: Any) -> Any:
        """Keep the body of the request, and send it."""
        self.bodies.append(kwargs.get("json"))
        return super().patch(*args, **kwargs)

    def put(self, *args: Any, **kwargs: Any) -> Any:
        """Keep the body of the request, and send it."""
        self.bodies.append(kwargs.get("json"))
        return super().put(*args, **kwargs)


class Payload(TypedDict):
    """A ``TypedDict`` with a member that keeps its JSON as it is."""

    inner: Any


@dataclasses.dataclass
class Box:
    """An embedded dataclass with a field that keeps its JSON as it is."""

    inner: Any = None


class Base(JSONObject):
    """Models served by the mocked API."""


comma = Recording(BASE)
repeat = Recording(BASE)
repeat.list_lookup_style = "repeat"


class Marker:
    """A class of the program whose conversion writes a member of an enum."""


@json_object
class Ticket(Base):
    """A model with a field of each plain type, and of structures."""

    class_url = "/tickets/"
    id: int
    text: str
    count: int
    weight: float
    anything: Any
    texts: list[str]
    table: dict[str, Any]
    typed: Payload
    box: Box
    either: str | int
    marker: Marker
    state: Literal[Status.OPEN, "closed"]
    pair: Box
    conversions: ClassVar[Mapping[Any, Conversion]] = {
        Marker: Conversion(
            decode=lambda value, cls: cls(),
            encode=lambda value, cls: Status.OPEN,
        ),
        Box: Conversion(
            decode=lambda value, cls: cls(value),
            encode=lambda value, cls: Level.HIGH,
        ),
    }


Base.register_api(comma)

# A member, the field of the plain type that it is given to, and the plain
# value that it stands for.
MEMBERS = [
    (Status.OPEN, "text", "open"),
    (Loud("x"), "text", "x"),
    (Level.HIGH, "count", 3),
    (Weight.HEAVY, "weight", 2.5),
]
IDS = ["str enum", "str subclass", "int enum", "float enum"]


def is_plain(value: Any) -> bool:
    """Return whether every scalar in a JSON value has its base type."""
    if isinstance(value, dict):
        return all(
            type(key) is str and is_plain(item) for key, item in value.items()
        )
    if isinstance(value, list):
        return all(is_plain(item) for item in value)
    return type(value) in (str, int, float, bool, type(None))


def answering(mocker: Any) -> None:
    """Make every request of the mocked API answer with an object."""
    reply = {"id": 1, "text": "", "count": 0, "weight": 0.0}
    mocker.post(BASE + "tickets/", json=reply)
    mocker.patch(BASE + "tickets/1/", json=reply)
    mocker.put(BASE + "tickets/1/", json=reply)
    mocker.get(BASE + "tickets/", json=[])


def stored(field: str) -> dict[str, Any]:
    """Return the JSON of a ticket that has read every field."""
    return {
        "id": 1,
        "text": "a",
        "count": 1,
        "weight": 1.0,
        "anything": None,
        "texts": [],
        "table": {},
        "typed": {"inner": 0},
        "box": {"inner": 0},
        "either": 1,
        "marker": "x",
        "state": "closed",
        "pair": 1,
    }


# Each sends a member in the field that its type fits, and returns what the
# bodies and the mocked server saw.
def assigned(member: Any, field: str) -> Any:
    """Assign a member to a field, save, and return the body sent."""
    obj = Ticket(json=stored(field))
    setattr(obj, field, member)
    assert is_plain(obj.json[field])
    obj.save()
    return comma.bodies[-1]


def created(member: Any, field: str) -> Any:
    """Create an object with a member, and return the body sent."""
    Ticket.create(**{field: member})
    return comma.bodies[-1]


def created_by_default(member: Any, field: str) -> Any:
    """Create by ``defaults=``, and return the body sent."""
    Ticket.get_or_create(id=1, defaults={field: member})
    return comma.bodies[-1]


def created_by_lookup(member: Any, field: str) -> Any:
    """Create by a lookup, and return the body sent."""
    Ticket.get_or_create(**{field: member})
    return comma.bodies[-1]


def saved_in_full(member: Any, field: str) -> Any:
    """Assign a member, ``save(full=True)``, and return the body."""
    obj = Ticket(json=stored(field))
    setattr(obj, field, member)
    obj.save(full=True)
    return comma.bodies[-1]


def created_as_an_extra(member: Any, field: str) -> Any:
    """Create with a member as an extra, and return the body."""
    Ticket.create(extra=member)
    return comma.bodies[-1]


def created_by_default_as_an_extra(member: Any, field: str) -> Any:
    """Create by ``defaults=`` with an extra, and return the body sent."""
    Ticket.get_or_create(id=1, defaults={"extra": member})
    return comma.bodies[-1]


def encoded(member: Any, field: str) -> Any:
    """Return what ``encode_json()`` gives for a member."""
    annotation = {"text": str, "count": int, "weight": float}[field]
    return encode_json(member, annotation)


BODIES: dict[str, Callable[[Any, str], Any]] = {
    "assignment": assigned,
    "create": created,
    "get_or_create defaults": created_by_default,
    "get_or_create lookup": created_by_lookup,
    "save(full=True)": saved_in_full,
    "create extra": created_as_an_extra,
    "get_or_create extra": created_by_default_as_an_extra,
}


@pytest.mark.parametrize("write", BODIES.values(), ids=BODIES.keys())
@pytest.mark.parametrize(("member", "field", "plain"), MEMBERS, ids=IDS)
def test_a_body_holds_the_plain_value(
    write: Callable[[Any, str], Any], member: Any, field: str, plain: Any
) -> None:
    """A body is written with the plain value of the member's base type."""
    with requests_mock.Mocker() as mocker:
        answering(mocker)
        body = write(member, field)
    assert is_plain(body)
    if write not in (created_as_an_extra, created_by_default_as_an_extra):
        assert body[field] == plain
        assert type(body[field]) is type(plain)


def test_encode_json_returns_the_plain_value() -> None:
    """``encode_json()`` returns the plain value of a subclass."""
    for member, field, plain in MEMBERS:
        result = encoded(member, field)
        assert result == plain
        assert type(result) is type(plain)


@pytest.mark.parametrize(("member", "field", "plain"), MEMBERS, ids=IDS)
def test_values_inside_structures_are_plain(
    member: Any, field: str, plain: Any
) -> None:
    """A value in ``Any``, ``list``, ``dict``, and structures is plain."""
    cases = [
        (Any, [member, {"k": member}], [plain, {"k": plain}]),
        (list[Any], [member], [plain]),
        (dict[str, Any], {"k": member}, {"k": plain}),
        (Payload, {"inner": member}, {"inner": plain}),
        (
            Payload,
            {"inner": 0, "more": [member]},
            {"inner": 0, "more": [plain]},
        ),
        (Box, Box(member), {"inner": plain}),
    ]
    for annotation, value, expected in cases:
        result = encode_json(value, annotation)
        assert result == expected
        assert is_plain(result)
    keyed = encode_json({Status.OPEN: 1}, dict[str, Any])
    assert is_plain(keyed)
    assert list(keyed) == ["open"]


def test_an_undeclared_key_of_a_typed_dict_is_plain() -> None:
    """A member of a ``TypedDict`` that it does not declare is plain."""
    obj = Ticket(json=stored("typed"))
    obj.typed = {"inner": 0, "more": [Status.OPEN, {"k": Level.HIGH}]}  # type: ignore[typeddict-unknown-key]
    assert is_plain(obj.json["typed"])
    assert obj.json["typed"]["more"] == ["open", {"k": 3}]


def test_a_union_conversion_and_literal_member_write_plain_values() -> None:
    """A union, a conversion's result, and a ``Literal`` member are plain."""
    obj = Ticket(json=stored("either"))
    obj.either = Level.HIGH
    obj.marker = Marker()
    obj.state = Status.OPEN
    obj.pair = Box(0)
    obj.box = Weight.HEAVY  # type: ignore[assignment]
    for field, plain in (
        ("either", 3),
        ("marker", "open"),
        ("state", "open"),
        ("pair", 3),
        ("box", 2.5),
    ):
        assert is_plain(obj.json[field])
        assert obj.json[field] == plain


def test_a_value_that_holds_no_subclass_is_not_copied() -> None:
    """A value that is plain is returned as it is."""
    value: Any = [1, {"a": [2]}]
    assert encode_json(value, Any) is value


def test_a_value_that_holds_a_subclass_is_not_changed() -> None:
    """The caller's value keeps its members, and the result is a copy."""
    value: Any = [Status.OPEN, {"a": Level.HIGH}]
    result = encode_json(value, Any)
    assert value == [Status.OPEN, {"a": Level.HIGH}]
    assert type(value[0]) is Status
    assert result is not value


def test_a_deep_value_of_subclasses_reduces_without_recursion() -> None:
    """The reduction of a value takes the depth that a write takes."""
    value: Any = [Status.OPEN]
    for _ in range(126):
        value = [value]
    assert is_plain(encode_json(value, Any))


def query(call: Callable[[], Any]) -> list[tuple[str, str]]:
    """Return the query that a call sends."""
    with requests_mock.Mocker() as mocker:
        answering(mocker)
        call()
        return parse_qsl(urlsplit(mocker.last_request.url).query)


@pytest.mark.parametrize("lookups", ["comma", "repeat"])
@pytest.mark.parametrize(("member", "field", "plain"), MEMBERS, ids=IDS)
def test_a_query_sends_the_plain_value(
    lookups: str, member: Any, field: str, plain: Any
) -> None:
    """A field's Python name sends the plain value alone and in a lookup."""
    client = comma if lookups == "comma" else repeat
    text = json.dumps(plain).strip('"')
    assert query(lambda: Ticket.filter(api=client, **{field: member})) == [
        (field, text)
    ]
    assert query(
        lambda: Ticket.filter(api=client, **{f"{field}__gt": member})
    ) == [(f"{field}__gt", text)]
    other = {"text": "z", "count": 9, "weight": 9.5}[field]
    sent = query(
        lambda: Ticket.filter(
            api=client, params={f"{field}__in": [member, other]}
        )
    )
    if lookups == "comma":
        assert sent == [
            (f"{field}__in", f"{text},{json.dumps(other)}".replace('"', ""))
        ]
    else:
        assert sent == [
            (f"{field}__in", text),
            (f"{field}__in", json.dumps(other).strip('"')),
        ]
    assert (
        query(
            lambda: Ticket.filter(
                api=client, params={f"{field}__range": [member, other]}
            )
        )[0][0]
        == f"{field}__range"
    )


@pytest.mark.parametrize("lookups", ["comma", "repeat"])
def test_a_collection_field_sends_plain_items(lookups: str) -> None:
    """The items of a collection field's value are plain in a query."""
    client = comma if lookups == "comma" else repeat
    sent = query(lambda: Ticket.filter(api=client, texts=[Status.OPEN, "b"]))
    assert sent == [("texts", "open"), ("texts", "b")]
    sent = query(lambda: Ticket.filter(api=client, texts__in=[Status.OPEN]))
    assert sent == [("texts__in", "open")]


@pytest.mark.parametrize(("member", "field", "plain"), MEMBERS, ids=IDS)
def test_a_lookup_and_the_creation_send_one_value(
    member: Any, field: str, plain: Any
) -> None:
    """``get_or_create()`` looks up the value that it then creates."""
    with requests_mock.Mocker() as mocker:
        answering(mocker)
        Ticket.get_or_create(**{field: member})
        looked_up = parse_qsl(urlsplit(mocker.request_history[0].url).query)
        created = mocker.request_history[1].json()
    assert looked_up == [(field, json.dumps(plain).strip('"'))]
    assert created[field] == plain


@pytest.mark.parametrize(("member", "field", "plain"), MEMBERS, ids=IDS)
def test_a_form_field_holds_the_plain_value(
    member: Any, field: str, plain: Any
) -> None:
    """A write with files sends the plain value in each form field."""
    text = json.dumps(plain).strip('"')
    with requests_mock.Mocker() as mocker:
        answering(mocker)
        Ticket.create(**{field: member}, files={"doc": b"x"})
        body = mocker.last_request.body
    assert f'name="{field}"\r\n\r\n{text}\r\n'.encode() in body
    obj = Ticket(json=stored(field))
    setattr(obj, field, member)
    with requests_mock.Mocker() as mocker:
        answering(mocker)
        obj.save(files={"doc": b"x"})
        body = mocker.last_request.body
    assert f'name="{field}"\r\n\r\n{text}\r\n'.encode() in body
    with requests_mock.Mocker() as mocker:
        answering(mocker)
        Ticket.create(texts=[Status.OPEN, "b"], files={"doc": b"x"})
        body = mocker.last_request.body
    assert b'name="texts"\r\n\r\nopen\r\n' in body
    with requests_mock.Mocker() as mocker:
        answering(mocker)
        Ticket.create(extra=member, files={"doc": b"x"})
        body = mocker.last_request.body
    assert f'name="extra"\r\n\r\n{text}\r\n'.encode() in body


HUGE = 10**4300


def decorated(annotation: Any) -> type[Any]:
    """Decorate a model whose one field has a given annotation."""
    return json_object(
        type(
            "Holder",
            (JSONObject,),
            {
                "__annotations__": {"id": int, "size": annotation},
                "class_url": "/holders/",
            },
        )
    )


def test_an_enum_with_a_huge_integer_is_refused_when_decorated() -> None:
    """An enum whose value Python cannot write as text cannot be a field."""
    huge_enum = enum.Enum("huge_enum", {"HUGE": HUGE})
    with pytest.raises(TypeError, match="digits"):
        decorated(huge_enum).json_schema()


def test_a_literal_with_a_huge_integer_is_refused_when_decorated() -> None:
    """A ``Literal`` of such a value cannot be a field either."""
    with pytest.raises(TypeError, match="digits"):
        decorated(Literal.__getitem__(HUGE)).json_schema()


def test_the_longest_integer_that_python_writes_is_taken() -> None:
    """An integer of 4300 digits is written, and one of 4301 is refused."""
    longest = 10**4300 - 1
    assert encode_json(longest, int) == longest
    assert encode_json(-longest, int) == -longest
    with pytest.raises(ValueError, match="digits"):
        encode_json(longest + 1, int)
    with pytest.raises(ValueError, match="digits"):
        encode_json(-longest - 1, int)


def test_a_huge_integer_is_refused_where_it_is_written() -> None:
    """An integer that Python cannot write as text is no JSON value.

    An assignment queues nothing, so no later ``save()`` fails for it.
    """
    obj = Ticket(json=stored("count"))
    with pytest.raises(FieldConversionError, match=r"Ticket.count"):
        obj.count = HUGE
    assert obj._pending_updates == {}
    with requests_mock.Mocker() as mocker:
        answering(mocker)
        obj.save()
        assert mocker.call_count == 0
        with pytest.raises(FieldConversionError, match=r"Ticket.count"):
            Ticket.create(count=HUGE)
        with pytest.raises(ValueError, match="digits"):
            Ticket.filter(count=HUGE)
        with pytest.raises(ValueError, match="digits"):
            Ticket.create(extra=[HUGE])
        assert mocker.call_count == 0
    with pytest.raises(ValueError, match="digits"):
        encode_json(HUGE, int)
    with pytest.raises(FieldConversionError, match=r"Ticket.anything"):
        obj.anything = [HUGE]
