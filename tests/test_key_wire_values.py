"""Tests of the wire values that a primary key may have."""

from __future__ import annotations

import enum
import uuid
from typing import Annotated, Any, Literal, NewType, Optional

import pytest

from eazyrest import (
    API,
    FieldConversionError,
    JSONObject,
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


@pytest.mark.parametrize(
    ("model", "given", "url"),
    [
        pytest.param(Label, LoudString("abc"), "/labels/abc/", id="str"),
        pytest.param(Item, LoudInt(7), "/items/7/", id="int"),
    ],
)
def test_a_key_given_as_a_subclass_gives_the_plain_key(
    model: Any, given: Any, url: str
) -> None:
    """The URL does not depend on how a ``str`` or ``int`` subclass prints."""
    assert model(id=given).url == url


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
def test_top_level_key_of_another_json_type_is_unknown(key: Any) -> None:
    """Such a key names no resource, so the object equals only itself."""
    first = Item(json={"id": key})
    second = Item(json={"id": key})

    assert first != second
    assert first.__eq__(first) is True
    with pytest.raises(TypeError):
        hash(first)
    with pytest.raises(FieldConversionError, match=r"Item\.id"):
        _ = first.url


def test_collection_item_with_a_key_of_another_json_type_is_unknown(
    requests_mock: Any,
) -> None:
    """A collection item whose key has another JSON type is not keyed."""
    requests_mock.get("https://wire.example/items/", json=[{"id": "7"}])

    (item,) = Item.filter()

    assert item != Item(id=7)
    assert item != Item(json={"id": "7"})


@json_object
class Doc(WireBase):
    """Model keyed by a ``UUID``."""

    class_url = "/docs/"

    id: uuid.UUID


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
