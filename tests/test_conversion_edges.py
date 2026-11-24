"""Edges of conversion that the other tests leave out."""

from __future__ import annotations

import datetime
import enum
from typing import Any, NamedTuple, NewType

import pytest
import requests_mock

from eazyrest import API, FieldConversionError, JSONObject, json_object

BASE = "https://conversion-edges.example/"
api = API(BASE)


@json_object(pk="slug")
class Tag(JSONObject):
    """A related model with a bulk lookup."""

    class_url = "/tags/"
    slug: str

    @classmethod
    def bulk_get_by_pks(  # type: ignore[no-untyped-def]
        cls, pks, *, api, **kwargs
    ):
        """Give each key an object without a request."""
        return {pk: cls(json={"slug": pk}) for pk in pks}


Tags = NewType("Tags", list[Tag])
OneTag = NewType("OneTag", Tag)
MaybeTags = NewType(  # type: ignore[valid-newtype]
    "MaybeTags", set[Tag] | None
)


@json_object
class Post(JSONObject):
    """A model whose relations are declared through ``NewType``."""

    class_url = "/posts/"
    id: int
    tags: Tags
    first: OneTag
    tagset: MaybeTags


Tag.register_api(api)
Post.register_api(api)


def post() -> Post:
    """Return a post that names its tags by key."""
    return Post(json={"id": 1, "tags": ["a"], "first": "a", "tagset": ["a"]})


def test_a_newtype_of_a_model_reads_and_keeps_an_assigned_object() -> None:
    """A ``NewType`` of a model, or of a collection of models, is its type."""
    obj = post()
    assert obj.first == Tag(slug="a")
    assert obj.tags == [Tag(slug="a")]
    assert obj.tagset == {Tag(slug="a")}
    tag = Tag(slug="b")
    obj.tags = Tags([tag])
    obj.first = OneTag(tag)
    assert obj.tags[0] is tag
    assert obj.first is tag


@pytest.mark.parametrize("field", ["tags", "tagset"])
def test_a_bare_key_added_in_place_to_a_newtype_collection_is_refused(
    field: str,
) -> None:
    """The collection's items are objects, as they are without the alias."""
    obj = post()
    if field == "tags":
        obj.tags.append("b")  # type: ignore[arg-type]
    else:
        assert obj.tagset is not None
        obj.tagset.add("b")
    with pytest.raises(FieldConversionError, match="added in place"):
        obj.save()


def test_a_newtype_of_a_model_is_prefetched_in_one_request() -> None:
    """Each alias is a relation that a prefetch can load."""
    with requests_mock.Mocker() as mocker:
        mocker.get(
            BASE + "posts/",
            json=[{"id": 1, "tags": ["a"], "first": "a", "tagset": ["a"]}],
        )
        (obj,) = Post.filter(prefetch=["tags", "first", "tagset"])
    assert mocker.call_count == 1
    assert obj.first.json == {"slug": "a"}


def test_a_merge_keeps_the_objects_of_a_newtype_collection() -> None:
    """The alias does not hide the relation from a merge."""
    obj = post()
    tag = obj.tags[0]
    obj.update_from_json({"tags": ["a"]})
    assert obj.tags[0] is tag


class Point(NamedTuple):
    """A tuple class with fields."""

    x: int
    when: datetime.date


class Moved(Point):
    """A subclass that inherits the fields and declares none."""


def test_a_named_tuple_that_inherits_its_fields_is_admitted() -> None:
    """The subclass reads as its own class, with the inherited fields."""

    @json_object
    class Holder(JSONObject):
        """A model with a field of the subclass."""

        class_url = "/holders/"
        id: int
        value: Moved

    value = Holder(json={"id": 1, "value": [1, "2024-01-02"]}).value
    assert value == Moved(1, datetime.date(2024, 1, 2))
    assert type(value) is Moved


class Strict(enum.Enum):
    """An enum whose ``_missing_()`` has a message of its own."""

    A = "a"

    @classmethod
    def _missing_(cls, value: object) -> Any:
        raise ValueError("no such status in the catalog")


def test_an_enums_own_value_error_keeps_its_message() -> None:
    """Only the message that Python writes, which quotes the value, goes."""

    @json_object
    class Holder(JSONObject):
        """A model with a field of the enum."""

        class_url = "/holders/"
        id: int
        value: Strict

    with pytest.raises(FieldConversionError) as caught:
        _ = Holder(json={"id": 1, "value": "zzz"}).value
    assert str(caught.value).endswith("no such status in the catalog")
    assert "zzz" not in str(caught.value)


@pytest.mark.parametrize("text", ["09:30+24:00", "09:30+23:60", "09:30-24:00"])
def test_a_time_with_an_offset_out_of_range_has_eazyrests_message(
    text: str,
) -> None:
    """Python's own message for the offset is not the one that is raised."""

    @json_object
    class Holder(JSONObject):
        """A model with a time."""

        class_url = "/holders/"
        id: int
        value: datetime.time

    with pytest.raises(FieldConversionError) as caught:
        _ = Holder(json={"id": 1, "value": text}).value
    assert str(caught.value).endswith("Time has a UTC offset out of range")
