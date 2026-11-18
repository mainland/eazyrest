"""Tests of the wire values that a primary key may have."""

from __future__ import annotations

import dataclasses
import enum
import uuid
from typing import Annotated, Any, Literal, NewType, Optional

import pytest

from eazyrest import (
    API,
    Conversion,
    FieldConversionError,
    JSONObject,
    UsageError,
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
        TypeError, match=r"Thing\.id is the primary key.*must be one value"
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
    ],
)
def test_an_enum_key_with_a_member_that_is_not_a_key_is_refused(
    key_type: Any,
) -> None:
    """The members' values are known when the class is decorated."""
    with pytest.raises(
        TypeError, match=r"Thing\.id is the primary key.*must be one value"
    ):
        _declare_key(key_type)


@pytest.mark.parametrize(
    ("key_type", "key", "url"),
    [
        pytest.param(Tag, Tag.ONE, "/x/1/", id="int-enum"),
        pytest.param(StrEnum, StrEnum.A, "/x/a/", id="str-enum"),
        pytest.param(MixedEnum, MixedEnum.A, "/x/a/", id="mixed-str"),
        pytest.param(MixedEnum, MixedEnum.B, "/x/1/", id="mixed-int"),
        pytest.param(Tag | None, Tag.ONE, "/x/1/", id="optional"),
        pytest.param(Literal[Tag.ONE], Tag.ONE, "/x/1/", id="literal"),
    ],
)
def test_an_enum_key_of_strings_and_integers_decorates(
    key_type: Any, key: Any, url: str
) -> None:
    """Members that are strings or integers give keys with URLs."""
    model = _declare_key(key_type, class_url="/x/")

    assert model(id=key).url == url


def test_an_enum_that_a_conversion_owns_is_decided_by_it() -> None:
    """A registered conversion of the enum decides the wire type."""
    model = _declare_key(
        FloatEnum,
        class_url="/x/",
        conversions={
            FloatEnum: Conversion(
                decode=lambda value, _: FloatEnum.A,
                encode=lambda value, _: "a",
            )
        },
    )

    assert model(id=FloatEnum.A).url == "/x/a/"


@pytest.mark.parametrize(
    "key_type",
    [int, str, Tag, Serial, Literal["a", 1], Optional[int]],  # noqa: UP045
)
def test_a_string_or_integer_key_decorates(key_type: Any) -> None:
    """The types whose wire value is a string or an integer are keys."""
    _declare_key(key_type)


def test_a_conversion_of_a_float_that_writes_a_string_decorates() -> None:
    """A registered codec decides the wire type, so a float may be keyed."""
    model = _declare_key(
        Ratio,
        conversions={
            Ratio: Conversion(
                decode=lambda value, _: float(value),
                encode=lambda value, _: repr(value),
            )
        },
    )

    assert model(id=Ratio(1.5)).url == "/things/1.5/"
    assert model(json={"id": "1.5"}) == model(id=Ratio(1.5))


@dataclasses.dataclass
class Key:
    """Mutable key class that wraps a value."""

    value: Any


def _key_model(**conversions: Any) -> Any:
    """Return a model keyed by a ``Key`` whose codec is given."""
    return _declare_key(
        Key,
        conversions={Key: Conversion(**conversions)},
    )


@pytest.mark.parametrize(
    "written",
    [
        pytest.param({"x": 1}, id="mapping"),
        pytest.param([1], id="list"),
        pytest.param(1.5, id="float"),
        pytest.param(True, id="bool"),
    ],
)
def test_a_codec_that_writes_another_json_type_gives_no_key(
    written: Any,
) -> None:
    """A key is a string or an integer, whatever codec writes it."""
    model = _key_model(
        decode=lambda value, _: Key(value),
        encode=lambda key, _: key.value,
    )

    with pytest.raises(FieldConversionError, match=r"Thing\.id"):
        model(id=Key(written))
    unread = model(json={"id": written})
    assert unread != model(json={"id": written})
    with pytest.raises(TypeError, match="cannot be hashed"):
        hash(unread)
    with pytest.raises(FieldConversionError, match=r"Thing\.id"):
        _ = unread.url


def test_a_mapping_key_from_a_codec_has_no_url_that_depends_on_order() -> None:
    """Equal mappings in another order are not keys, so no URL differs."""
    model = _key_model(
        decode=lambda value, _: Key(value),
        encode=lambda key, _: key.value,
    )
    first = model(json={"id": {"x": 1, "y": 2}})
    second = model(json={"id": {"y": 2, "x": 1}})

    for obj in (first, second):
        with pytest.raises(FieldConversionError, match=r"Thing\.id"):
            _ = obj.url


def test_a_new_type_key_that_a_codec_writes_as_a_string_is_a_string_key() -> (
    None
):
    """The registered codec decides the wire type, not the supertype."""
    entry = _declare_key(
        Serial,
        conversions={
            Serial: Conversion(
                decode=lambda value, _: int(value),
                encode=lambda value, _: str(value),
            )
        },
    )

    given = entry(id=Serial(3))
    read = entry(json={"id": "3"})

    assert given == read
    assert hash(given) == hash(read)
    assert given.url == read.url == "/things/3/"


def test_a_key_changed_in_place_from_1_to_1_point_0_is_refused(
    requests_mock: Any,
) -> None:
    """The key is compared by its canonical text, which 1.0 does not have."""
    model = _declare_key(
        Key,
        conversions={
            Key: Conversion(
                decode=lambda value, _: Key(value),
                encode=lambda key, _: key.value,
            )
        },
    )
    patch = requests_mock.patch("https://wire.example/things/1/", json={})
    obj = model(id=Key(1), write_mode="lazy")

    obj.id.value = 1.0

    with pytest.raises(UsageError, match=r"key .* cannot change"):
        obj.save()
    assert not patch.called


def test_a_key_changed_in_place_to_the_same_text_is_no_change(
    requests_mock: Any,
) -> None:
    """Python equality is not the test, the canonical text is."""
    model = _declare_key(
        Key,
        conversions={
            Key: Conversion(
                decode=lambda value, _: Key(value),
                encode=lambda key, _: key.value,
            )
        },
    )
    obj = model(id=Key(1))

    obj.id.value = 1

    obj.save()
    assert requests_mock.call_count == 0


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
