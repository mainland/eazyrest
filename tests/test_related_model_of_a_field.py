"""Every read of a field's type finds the same related model.

A field names a related model if its type, inside ``Optional`` and ``NewType``,
is the model or a collection of it. Prefetching, querying by a model object,
assigning a related object, and the use of the type as a key decide from the
same annotation whether it does.
"""

from __future__ import annotations

from typing import Any, NewType, Optional

import pytest
import requests_mock

from eazyrest import (
    API,
    FieldConversionError,
    JSONObject,
    PrefetchNotSupported,
    json_object,
)
from eazyrest._models import (
    CollectionType,
    NewTypeType,
    OptionalType,
    RelatedType,
    _holds_model,
    _relation_of,
)

BASE = "https://related-model-of-a-field.example/"


class Base(JSONObject):
    """Models served by the mocked API."""


Base.register_api(API(BASE))


@json_object
class Owner(Base):
    """A related model that does not implement a bulk lookup."""

    class_url = "/owners/"
    id: int


Ref = NewType("Ref", Owner)
Refs = NewType("Refs", list[Owner])
MaybeRef = NewType("MaybeRef", Optional[Owner])  # type: ignore[valid-newtype]  # noqa: UP045
Ints = NewType("Ints", list[int])


@json_object
class Thing(Base):
    """A model with a field of each shape that a type may take."""

    class_url = "/things/"
    id: int
    plain: int
    numbers: list[int]
    new_numbers: Ints
    one: Owner
    maybe: Owner | None
    many: list[Owner]
    maybe_many: list[Owner] | None
    as_set: set[Owner]
    new_one: Ref
    new_many: Refs
    new_maybe: Optional[MaybeRef]  # noqa: UP045


FULL: dict[str, Any] = {
    "id": 1,
    "plain": 1,
    "numbers": [],
    "new_numbers": [],
    "one": 2,
    "maybe": None,
    "many": [],
    "maybe_many": None,
    "as_set": [],
    "new_one": 2,
    "new_many": [],
    "new_maybe": None,
}

# Whether each field names a related model, and whether it holds many.
RELATIONS: dict[str, tuple[bool, bool]] = {
    "plain": (False, False),
    "numbers": (False, False),
    "new_numbers": (False, False),
    "one": (True, False),
    "maybe": (True, False),
    "many": (True, True),
    "maybe_many": (True, True),
    "as_set": (True, True),
    "new_one": (True, False),
    "new_many": (True, True),
    "new_maybe": (True, False),
}


@pytest.mark.parametrize("field", sorted(RELATIONS))
def test_a_type_that_holds_a_model_is_found_as_one(field: str) -> None:
    """The check at declaration agrees with the table."""
    analyzed = getattr(Thing, field).analyzed_type
    assert _holds_model(analyzed) is RELATIONS[field][0]


@pytest.mark.parametrize("field", sorted(RELATIONS))
def test_prefetch_reads_a_relation_where_the_type_names_one(
    field: str,
) -> None:
    """A relation is refused for its missing bulk lookup, and no other."""
    expected = PrefetchNotSupported if RELATIONS[field][0] else ValueError
    with requests_mock.Mocker() as mock:
        with pytest.raises(expected) as caught:
            list(Thing.filter(prefetch=field))
        assert mock.call_count == 0
    assert (type(caught.value) is PrefetchNotSupported) is RELATIONS[field][0]


@pytest.mark.parametrize("field", sorted(RELATIONS))
def test_a_query_takes_a_model_object_for_a_relation_only(field: str) -> None:
    """The object is its key for a relation, and refused for any other."""
    params: dict[str, Any] = {field: Owner(id=1)}
    with requests_mock.Mocker() as mock:
        mock.get(BASE + "things/", json=[])
        if RELATIONS[field][0]:
            list(Thing.filter(**params))
            assert mock.last_request is not None
            assert mock.last_request.qs == {field: ["1"]}
        else:
            with pytest.raises((FieldConversionError, ValueError)):
                list(Thing.filter(**params))
            assert mock.call_count == 0


@pytest.mark.parametrize(
    "field", [name for name, (named, _) in RELATIONS.items() if named]
)
def test_an_assigned_related_object_is_kept_where_the_type_names_one(
    field: str,
) -> None:
    """Reading the field returns the objects that were assigned."""
    thing = Thing(json=FULL)
    owner = Owner(id=2)
    many = RELATIONS[field][1]
    with requests_mock.Mocker() as mock:
        mock.get(BASE + "things/1/", json={"id": 1})
        mock.patch(BASE + "things/1/", json={"id": 1})
        setattr(
            thing,
            field,
            {owner} if field == "as_set" else ([owner] if many else owner),
        )
        value = getattr(thing, field)
    kept = list(value) if many else [value]
    assert kept[0] is owner


@pytest.mark.parametrize("field", sorted(RELATIONS))
def test_a_relation_is_no_key_type(field: str) -> None:
    """A key of a relation's type is refused for being a relation."""
    body: dict[str, Any] = {
        "__module__": __name__,
        "__annotations__": {"id": Thing.__annotations__[field]},
    }
    named, many = RELATIONS[field]
    keyed = type("Keyed", (Base,), body)
    if named and not many:
        with pytest.raises(TypeError, match="relation"):
            json_object(keyed)
    else:
        try:
            json_object(keyed)
        except TypeError as err:
            assert "relation" not in str(err).lower()  # noqa: PT017


_RELATED = RelatedType(Owner)
_MANY = CollectionType(list, _RELATED)


@pytest.mark.parametrize(
    ("analyzed", "many"),
    [
        (_RELATED, False),
        (OptionalType(_RELATED), False),
        (NewTypeType(Ref, _RELATED), False),
        (OptionalType(NewTypeType(Ref, OptionalType(_RELATED))), False),
        (_MANY, True),
        (OptionalType(_MANY), True),
        (NewTypeType(Refs, _MANY), True),
        (CollectionType(list, NewTypeType(Ref, _RELATED)), True),
    ],
    ids=[
        "one",
        "optional",
        "new type",
        "wrappers",
        "many",
        "optional many",
        "new type of many",
        "new type item",
    ],
)
def test_the_relation_is_found_inside_every_wrapper(
    analyzed: Any, many: bool
) -> None:
    """The relation and the collection that holds it are both returned."""
    relation = _relation_of(analyzed)
    assert relation is not None
    assert relation.related == _RELATED
    assert (relation.collection is not None) is many


def test_a_collection_of_collections_names_no_relation() -> None:
    """Such a type is refused at declaration, so it names none."""
    assert _relation_of(CollectionType(list, _MANY)) is None


@json_object(field_map={name: f"{name}_json" for name in RELATIONS})
class Mapped(Base):
    """A model whose fields are all queried by a JSON key of their own."""

    class_url = "/mapped/"
    id: int
    plain: int
    numbers: list[int]
    new_numbers: Ints
    one: Owner
    maybe: Owner | None
    many: list[Owner]
    maybe_many: list[Owner] | None
    as_set: set[Owner]
    new_one: Ref
    new_many: Refs
    new_maybe: Optional[MaybeRef]  # noqa: UP045


@pytest.mark.parametrize("field", sorted(RELATIONS))
def test_a_json_key_takes_a_model_object_for_a_relation_only(
    field: str,
) -> None:
    """A key that sends no conversion takes a model as its key."""
    params: dict[str, Any] = {f"{field}_json": Owner(id=1)}
    with requests_mock.Mocker() as mock:
        mock.get(BASE + "mapped/", json=[])
        if RELATIONS[field][0]:
            list(Mapped.filter(**params))
            assert mock.last_request is not None
            assert mock.last_request.qs == {f"{field}_json": ["1"]}
        else:
            with pytest.raises(ValueError, match="sends no conversion"):
                list(Mapped.filter(**params))
            assert mock.call_count == 0


@pytest.mark.parametrize("field", sorted(RELATIONS))
def test_a_path_through_a_relation_compares_one_value(field: str) -> None:
    """A collection for the related key is refused for a relation only."""
    params: dict[str, Any] = {f"{field}__pk": [1, 2]}
    with requests_mock.Mocker() as mock:
        mock.get(BASE + "things/", json=[])
        if RELATIONS[field][0]:
            with pytest.raises(ValueError, match="compares one value"):
                list(Thing.filter(**params))
            assert mock.call_count == 0
        else:
            list(Thing.filter(**params))
            assert mock.call_count == 1


@pytest.mark.parametrize(
    "field", [name for name, (_, many) in RELATIONS.items() if many]
)
def test_a_bare_key_added_in_place_to_a_collection_is_refused(
    field: str,
) -> None:
    """Only a collection of relations holds relations, so only it refuses."""
    thing = Thing(json={**FULL, field: [2]})
    items = getattr(thing, field)
    if isinstance(items, set):
        items.add(5)
    else:
        items.append(5)
    with requests_mock.Mocker() as mock:
        with pytest.raises(FieldConversionError, match=field):
            thing.save()
        assert mock.call_count == 0


@pytest.mark.parametrize(
    "field", [name for name, (named, _) in RELATIONS.items() if named]
)
def test_assigning_the_objects_that_a_field_holds_changes_nothing(
    field: str,
) -> None:
    """The stored keys compare equal to the objects that name them."""
    many = RELATIONS[field][1]
    thing = Thing(json={**FULL, field: [2] if many else 2}, write_mode="eager")
    owner = Owner(id=2)
    with requests_mock.Mocker() as mock:
        setattr(
            thing,
            field,
            ({owner} if field == "as_set" else [owner]) if many else owner,
        )
        assert mock.call_count == 0


_WRAPPED_ITEMS = CollectionType(list, NewTypeType(Ref, _RELATED))


def test_a_wrapped_relation_is_an_item_that_names_a_key_when_read() -> None:
    """A null item and a scalar value are refused as for a relation."""
    thing = Thing(json=FULL)
    with pytest.raises(ValueError, match="no item is null"):
        thing._from_json([None], _WRAPPED_ITEMS, field="many")
    with pytest.raises(TypeError, match="Related collection values"):
        thing._from_json(5, _WRAPPED_ITEMS, field="many")


def test_a_wrapped_relation_is_an_item_that_is_never_null_when_written() -> (
    None
):
    """A null item is refused, as for a relation."""
    with pytest.raises(ValueError, match="no item is null"):
        Thing._encode_value([None], _WRAPPED_ITEMS)


@pytest.mark.parametrize("stored", [{"x": 1}, {"id": None}])
def test_an_object_without_a_key_is_not_replaced_by_the_key_zero(
    stored: dict[str, Any],
) -> None:
    """Embedded JSON that names no key differs from every key, zero too."""
    thing = Thing(json={**FULL, "one": stored}, write_mode="eager")
    with requests_mock.Mocker() as mock:
        mock.patch(BASE + "things/1/", json={**FULL, "one": 0})
        thing.one = Owner(id=0)
        assert mock.call_count == 1
