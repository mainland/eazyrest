"""Every read of whether a collection's items are relations agrees.

A collection of relations reads, writes, and is declared the same way whether
its item type is the model or the model inside a ``NewType``. One function says
which related model a collection's items are, so no read of it can miss a
wrapper.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable
from typing import Any, NewType

import pytest

from eazyrest import JSONObject, json_object
from eazyrest._models import (
    AnalyzedType,
    CollectionType,
    NewTypeType,
    OptionalType,
    RelatedType,
    _check_relation_collection,
)


@json_object
class Member(JSONObject):
    """A related model that is itself iterable, as a crew of members."""

    class_url = "/members/"
    id: int

    def __iter__(self) -> Any:
        """Iterate over other members."""
        return iter([Member(id=2), Member(id=3)])


Ref = NewType("Ref", Member)


def _outcome(run: Any) -> tuple[str, str]:
    """Return the type and message of what ``run`` raises, or its value."""
    try:
        return "returned", repr(run())
    except Exception as err:  # noqa: BLE001
        return type(err).__name__, str(err)


def _collection(kind: type, wrapped: bool) -> AnalyzedType:
    item: AnalyzedType = RelatedType(Member)
    if wrapped:
        item = NewTypeType(Ref, item)
    return CollectionType(kind, item)


WRAPPED = pytest.mark.parametrize(
    "wrapped", [False, True], ids=["plain", "wrapped"]
)

READS: dict[str, Any] = {
    "an iterable model for a collection": lambda a: Member._encode_value(
        Member(id=1), a
    ),
    "a null item": lambda a: Member._encode_value([None], a),
    "a list of models": lambda a: Member._encode_value([Member(id=5)], a),
    "a number read as a collection": lambda a: Member(id=1)._from_json(5, a),
    "an item without a key": lambda a: Member(id=1)._from_json([{"x": 1}], a),
    "keys": lambda a: Member(id=1)._from_json([{"id": 7}], a),
}


@WRAPPED
@pytest.mark.parametrize("read", READS.values(), ids=READS)
def test_a_wrapped_item_type_is_read_as_the_model_is(
    read: Any, wrapped: bool
) -> None:
    """The outcome does not depend on a wrapper around the item type."""
    plain = _outcome(lambda: read(_collection(list, False)))
    assert _outcome(lambda: read(_collection(list, wrapped))) == plain


@WRAPPED
def test_an_iterable_model_is_no_collection_of_relations(
    wrapped: bool,
) -> None:
    """The members of an iterable model are not the items of a collection."""
    analyzed = _collection(list, wrapped)
    with pytest.raises(TypeError, match="must be collections"):
        Member._encode_value(Member(id=1), analyzed)


@WRAPPED
@pytest.mark.parametrize("kind", [Collection, Iterable])
def test_a_collection_of_relations_that_has_no_order_is_refused(
    kind: type, wrapped: bool
) -> None:
    """The declaration check sees the relation inside a wrapper."""
    analyzed = _collection(kind, wrapped)
    with pytest.raises(TypeError, match="does not say whether"):
        _check_relation_collection("The field", analyzed)


@WRAPPED
def test_an_optional_item_of_a_collection_of_relations_is_refused(
    wrapped: bool,
) -> None:
    """No item has no key, however the item type is wrapped."""
    item: AnalyzedType = RelatedType(Member)
    if wrapped:
        item = NewTypeType(Ref, item)
    analyzed = CollectionType(list, OptionalType(item))
    with pytest.raises(TypeError, match="collection of optional Member"):
        _check_relation_collection("The field", analyzed)
