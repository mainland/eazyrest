"""Tests of the wire values that a primary key may have."""

from __future__ import annotations

import enum
import re
import uuid
from typing import Annotated, Any, Literal, NewType, Optional

import jsonschema
import pytest

from eazyrest import (
    API,
    FieldConversionError,
    JSONObject,
    ResponseShapeError,
    json_object,
)


class WireBase(JSONObject):
    """Models served by the mocked API."""


WireBase.register_api(API("https://wire.example/"))


@json_object
class Item(WireBase):
    """Model keyed by an ``int``."""

    class_url = "/items/"

    id: int


@json_object
class Label(WireBase):
    """Model keyed by a ``str``."""

    class_url = "/labels/"

    id: str


@json_object
class Holder(WireBase):
    """Model that relates to an item."""

    class_url = "/holders/"

    id: int
    item: Item


def _declare_key(key_type: Any, **extra: Any) -> Any:
    """Decorate a model whose key has a type, and return the model."""
    namespace = {
        "__annotations__": {"id": key_type},
        "class_url": "/things/",
        **extra,
    }
    return json_object(type("Thing", (WireBase,), namespace))


class Tag(enum.IntEnum):
    """Enum whose members are integers."""

    ONE = 1


Serial = NewType("Serial", int)
Ratio = NewType("Ratio", float)


@pytest.mark.parametrize(
    "key_type",
    [
        pytest.param(float, id="float"),
        pytest.param(bool, id="bool"),
        pytest.param(float | None, id="optional-float"),
        pytest.param(Optional[bool], id="optional-bool"),  # noqa: UP045
        pytest.param(Ratio, id="new-type-of-float"),
        pytest.param(NewType("Flag", bool), id="new-type-of-bool"),
        pytest.param(Annotated[float, "note"], id="annotated-float"),
        pytest.param(Literal[True], id="literal-bool"),
        pytest.param(Literal[1.5], id="literal-float"),
        pytest.param(Literal["a", 1.5], id="literal-mixed"),
    ],
)
def test_a_float_or_bool_key_is_refused_at_decoration(key_type: Any) -> None:
    """A key that is a number with many spellings, or a bool, is no key."""
    with pytest.raises(
        TypeError, match=r"Thing\.id is the primary key.*a str, an int, or a"
    ):
        _declare_key(key_type)


class FloatEnum(enum.Enum):
    """Enum whose members are numbers with a fraction."""

    A = 1.5


class BoolEnum(enum.Enum):
    """Enum whose members are booleans."""

    A = True


class MixedFloatEnum(enum.Enum):
    """Enum with a string, an integer, and a float member."""

    A = "a"
    B = 1
    C = 1.5


class StrEnum(enum.Enum):
    """Enum whose members are strings."""

    A = "a"


class MixedEnum(enum.Enum):
    """Enum whose members are strings and integers."""

    A = "a"
    B = 1


@pytest.mark.parametrize(
    "key_type",
    [
        pytest.param(FloatEnum, id="float-valued"),
        pytest.param(BoolEnum, id="bool-valued"),
        pytest.param(MixedFloatEnum, id="mixed-with-a-float"),
        pytest.param(FloatEnum | None, id="optional"),
        pytest.param(NewType("FloatNew", FloatEnum), id="new-type"),
        pytest.param(Annotated[FloatEnum, "note"], id="annotated"),
        pytest.param(Literal[FloatEnum.A], id="literal"),
        pytest.param(Literal[BoolEnum.A], id="literal-bool-valued"),
        pytest.param(Tag, id="int-enum"),
        pytest.param(StrEnum, id="str-enum"),
        pytest.param(MixedEnum, id="mixed"),
        pytest.param(Literal[Tag.ONE], id="literal-of-an-int-enum"),
    ],
)
def test_an_enum_key_is_refused(key_type: Any) -> None:
    """An enum's conversion may map two of the service's keys to one member.

    That holds whatever its members' values are, so every enum is refused when
    the class is decorated.
    """
    with pytest.raises(
        TypeError, match=r"Thing\.id is the primary key.*a str, an int, or a"
    ):
        _declare_key(key_type)


@pytest.mark.parametrize(
    "key_type",
    [int, str, uuid.UUID, Serial, Optional[int]],  # noqa: UP045
)
def test_a_string_or_integer_key_decorates(key_type: Any) -> None:
    """The types whose wire value is a string or an integer are keys."""
    _declare_key(key_type)


class LoudString(str):
    """String whose ``str()`` is another text."""

    def __str__(self) -> str:
        """Return another text."""
        return "other"


class LoudInt(int):
    """Integer whose ``str()`` is another text."""

    def __str__(self) -> str:
        """Return another text."""
        return "other"


class LoudUUID(uuid.UUID):
    """UUID whose ``str()`` is another text."""

    def __str__(self) -> str:
        """Return another text."""
        return "other"


U = uuid.UUID("6f1c0f5e-0d3c-4bd9-9c3e-2f6a1d1a7b40")


@json_object
class Doc(WireBase):
    """Model keyed by a ``UUID``."""

    class_url = "/docs/"

    id: uuid.UUID


@pytest.mark.parametrize(
    ("model", "given", "wire", "plain"),
    [
        pytest.param(Label, LoudString("abc"), "abc", str, id="str"),
        pytest.param(Item, LoudInt(7), 7, int, id="int"),
        pytest.param(Item, Tag.ONE, 1, int, id="int-enum"),
        pytest.param(Doc, LoudUUID(int=U.int), str(U), uuid.UUID, id="uuid"),
    ],
)
def test_a_key_given_as_a_subclass_gives_the_plain_key(
    model: Any, given: Any, wire: Any, plain: type
) -> None:
    """The key is the plain value, however a subclass of its type prints.

    The URL, the key that ``pk`` reads, and equality all use it.
    """
    obj = model(id=given)

    assert obj.url == f"{model.class_url}{wire}/"
    assert type(obj.pk) is plain
    assert obj == model(json={"id": wire})


def test_a_query_sends_the_plain_key_of_a_subclass(requests_mock: Any) -> None:
    """A query on the key, or on a relation to it, sends the built key."""
    requests_mock.get(re.compile(r"https://wire\.example/"), json=[])

    list(Doc.filter(id=LoudUUID(int=U.int)))
    list(Cover.filter(doc=LoudUUID(int=U.int)))
    list(Holder.filter(item=Tag.ONE))

    assert [r.qs for r in requests_mock.request_history] == [
        {"id": [str(U)]},
        {"doc": [str(U)]},
        {"item": ["1"]},
    ]


@pytest.mark.parametrize(
    ("model", "key", "url"),
    [
        (Item, 7, "/items/7/"),
        (Item, 10**20, f"/items/{10**20}/"),
        (Item, -3, "/items/-3/"),
        (Label, "abc", "/labels/abc/"),
        (Label, "a/b", "/labels/a%2Fb/"),
        (Label, "100%", "/labels/100%25/"),
        (Label, "7", "/labels/7/"),
    ],
)
def test_url_formats_a_string_as_its_text_and_an_integer_in_decimal(
    model: Any, key: Any, url: str
) -> None:
    """The key is percent-encoded once."""
    assert model(id=key).url == url
    assert model(json={"id": key}).url == url


@pytest.mark.parametrize("key", ["", ".", ".."])
def test_url_refuses_an_empty_or_dot_segment(key: str) -> None:
    """Such a key would name the collection or its parent."""
    with pytest.raises(ValueError, match="no URL"):
        _ = Label(id=key).url


@pytest.mark.parametrize("key", ["7", 7.0, True, {"a": 1}, [1]])
def test_embedded_related_object_with_a_key_of_another_json_type_raises(
    key: Any,
) -> None:
    """An embedded object's key is checked as a bare key is."""
    holder = Holder(json={"id": 1, "item": {"id": key, "name": "x"}})

    with pytest.raises(FieldConversionError, match=r"Holder\.item"):
        _ = holder.item


@pytest.mark.parametrize("key", ["7", 7.0, True, {"a": 1}, [1]])
def test_top_level_key_of_another_json_type_is_refused(key: Any) -> None:
    """Such a key names no resource, so no object is built."""
    with pytest.raises(FieldConversionError, match=r"Item\.id"):
        Item(json={"id": key})


def test_collection_item_with_a_key_of_another_json_type_is_refused(
    requests_mock: Any,
) -> None:
    """A collection item whose key has another JSON type names no object."""
    requests_mock.get("https://wire.example/items/", json=[{"id": "7"}])

    with pytest.raises(ResponseShapeError, match=r"Item \[0\].*Item\.id"):
        list(Item.filter())


@json_object
class Cover(WireBase):
    """Model that relates to a document."""

    class_url = "/covers/"

    id: int
    doc: Doc


def test_embedded_key_that_does_not_convert_raises_for_the_relation() -> None:
    """An embedded key goes through the same constructor as a given one."""
    cover = Cover(json={"id": 1, "doc": {"id": "not-a-uuid"}})

    with pytest.raises(FieldConversionError, match=r"Cover\.doc"):
        _ = cover.doc


def test_create_refuses_a_key_of_another_json_type_before_the_request(
    requests_mock: Any,
) -> None:
    """A key that the constructor refuses is not sent."""
    posted = requests_mock.post("https://wire.example/items/", json={})

    with pytest.raises(FieldConversionError, match=r"Item\.id"):
        Item.create(id="1")

    assert not posted.called


def test_create_refuses_a_key_that_does_not_convert_before_the_request(
    requests_mock: Any,
) -> None:
    """A key that cannot decode is not sent."""
    posted = requests_mock.post("https://wire.example/docs/", json={})

    with pytest.raises(FieldConversionError, match=r"Doc\.id"):
        Doc.create(id="not-a-uuid")

    assert not posted.called


def test_create_sends_a_key_of_the_right_type(requests_mock: Any) -> None:
    """A key of the field's type is sent, and the object holds it."""
    posted = requests_mock.post("https://wire.example/items/", json={})

    item = Item.create(id=1)

    assert posted.last_request.json() == {"id": 1}
    assert item == Item(id=1)


@json_object(field_map={"id": "recordId"}, read_only={"id"})
class FixedRecord(WireBase):
    """Model whose key is read-only and read under another JSON key."""

    class_url = "/fixed-records/"

    id: int
    name: str


@json_object(field_map={"id": "recordId"}, write_map={"id": "inputId"})
class Record(WireBase):
    """Model whose key is read and written under other JSON keys."""

    class_url = "/records/"

    id: int
    name: str


@pytest.mark.parametrize(
    "given",
    [
        pytest.param({"id": 7}, id="field"),
        pytest.param({"pk": 7}, id="pk"),
        pytest.param({"fields": {"recordId": 7}}, id="json-key"),
    ],
)
def test_create_refuses_a_read_only_key_under_any_name(
    requests_mock: Any, given: dict[str, Any]
) -> None:
    """Each name of a read-only key is refused before the request.

    Args:
        given: The keyword arguments of ``create()``.
    """
    posted = requests_mock.post("https://wire.example/fixed-records/", json={})

    with pytest.raises(AttributeError, match=r"FixedRecord\.id is read-only"):
        FixedRecord.create(**given)

    assert not posted.called


@pytest.mark.parametrize(
    "given",
    [
        pytest.param({"id": "7"}, id="field"),
        pytest.param({"pk": "7"}, id="pk"),
        pytest.param({"fields": {"recordId": "7"}}, id="json-key"),
        pytest.param({"fields": {"inputId": "7"}}, id="write-key"),
    ],
)
def test_create_builds_the_key_under_any_name_before_the_request(
    requests_mock: Any, given: dict[str, Any]
) -> None:
    """A key of another type is refused, whichever name gives it.

    Args:
        given: The keyword arguments of ``create()``.
    """
    posted = requests_mock.post("https://wire.example/records/", json={})

    with pytest.raises(FieldConversionError, match=r"Record\.id"):
        Record.create(**given)

    assert not posted.called


@pytest.mark.parametrize(
    "given",
    [
        pytest.param({"id": 7}, id="field"),
        pytest.param({"pk": 7}, id="pk"),
        pytest.param({"fields": {"recordId": 7}}, id="json-key"),
        pytest.param({"fields": {"inputId": 7}}, id="write-key"),
    ],
)
def test_create_sends_the_key_under_its_write_key_and_keeps_it(
    requests_mock: Any, given: dict[str, Any]
) -> None:
    """The key is sent under its write key, and kept under its JSON key.

    A response that holds no object leaves the object the key that was sent, so
    it has its URL.

    Args:
        given: The keyword arguments of ``create()``.
    """
    posted = requests_mock.post("https://wire.example/records/", json={})

    record = Record.create(name="a", **given)

    assert posted.last_request.json() == {"inputId": 7, "name": "a"}
    assert record.pk == 7
    assert record.url == "/records/7/"
    assert record.json == {"recordId": 7, "name": "a"}


@json_object(
    field_map={"id": "recordId"},
    write_map={"id": "inputId", "legacy": "recordId"},
)
class Legacy(WireBase):
    """Model whose key's JSON key is the write key of another field."""

    class_url = "/legacy/"

    id: int
    legacy: int


def test_create_refuses_a_name_of_the_key_that_another_field_writes(
    requests_mock: Any,
) -> None:
    """A raw name that two fields answer to names neither."""
    posted = requests_mock.post("https://wire.example/legacy/", json={})

    with pytest.raises(TypeError, match=r"recordId.*key id.*legacy"):
        Legacy.create(fields={"recordId": 7})

    assert not posted.called


def test_create_and_a_full_save_send_one_spelling_of_a_uuid_key(
    requests_mock: Any,
) -> None:
    """A key given in compact uppercase is posted and put in one spelling."""
    canonical = str(U)
    posted = requests_mock.post("https://wire.example/docs/", json={})
    put = requests_mock.put(f"https://wire.example/docs/{canonical}/", json={})

    doc = Doc.create(id=U.hex.upper())
    doc.save(full=True)

    assert posted.last_request.json() == {"id": canonical}
    assert put.last_request.json() == {"id": canonical}
    assert doc.url == f"/docs/{canonical}/"


@json_object
class MaybeKeyed(WireBase):
    """Model whose key is annotated as optional."""

    class_url = "/maybe/"

    id: int | None


@pytest.mark.parametrize(
    ("model", "key", "schema"),
    [
        pytest.param(Item, 7, {"type": "integer"}, id="int"),
        pytest.param(Label, "a", {"type": "string"}, id="str"),
        pytest.param(
            Doc, U.hex.upper(), {"type": "string", "format": "uuid"}, id="uuid"
        ),
        pytest.param(MaybeKeyed, 7, {"type": "integer"}, id="optional"),
    ],
)
def test_the_write_schema_takes_what_create_and_a_full_save_send(
    requests_mock: Any, model: Any, key: Any, schema: dict[str, Any]
) -> None:
    """The key's schema is the type that its builder sends, never null.

    Args:
        model: Model whose key is created and saved.
        key: The key, as the caller gives it.
        schema: The key's schema, in reads and writes alike.
    """
    posted = requests_mock.post(re.compile(r"https://wire\.example/"), json={})
    put = requests_mock.put(re.compile(r"https://wire\.example/"), json={})
    validator = jsonschema.Draft202012Validator(
        model.json_schema("write"),
        format_checker=jsonschema.Draft202012Validator.FORMAT_CHECKER,
    )

    obj = model.create(id=key)
    obj.save(full=True)

    for mode in ("read", "write"):
        assert model.json_schema(mode)["properties"]["id"] == schema
    for sent in (posted.last_request.json(), put.last_request.json()):
        validator.validate(sent)
    assert not validator.is_valid({"id": None})


def test_the_write_schema_of_a_relation_takes_the_key_that_it_sends(
    requests_mock: Any,
) -> None:
    """A relation is written as the related model's built key."""
    posted = requests_mock.post("https://wire.example/covers/", json={})
    validator = jsonschema.Draft202012Validator(
        Cover.json_schema("write"),
        format_checker=jsonschema.Draft202012Validator.FORMAT_CHECKER,
    )

    Cover.create(id=1, doc=U.hex.upper())

    validator.validate(posted.last_request.json())
    assert posted.last_request.json() == {"id": 1, "doc": str(U)}
    assert Cover.json_schema("write")["properties"]["doc"] == {
        "type": "string",
        "format": "uuid",
    }
