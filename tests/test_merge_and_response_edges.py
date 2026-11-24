"""Edges of a merge, a load, and a response that the other tests leave out."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
import requests_mock

from eazyrest import API, JSONObject, ResponseShapeError, json_object

BASE = "https://merge-edges.example/"
api = API(BASE)
KEY = uuid.UUID("abcdef12-1234-5678-1234-567812345abc")


@json_object
class Doc(JSONObject):
    """A related model with a UUID key."""

    class_url = "/docs/"
    id: uuid.UUID


@json_object
class Shelf(JSONObject):
    """A model that holds documents."""

    class_url = "/shelves/"
    id: int
    title: str
    cover: Doc
    papers: list[Doc]


@json_object
class Wrapped(JSONObject):
    """A model whose responses come in an envelope."""

    class_url = "/wrapped/"
    id: int
    name: str

    @classmethod
    def object_json(cls, payload: Any, **kwargs: Any) -> Any:
        """Read the envelope's result, or refuse a response without one."""
        if "result" not in payload:
            raise ResponseShapeError("The envelope holds no result")
        return payload["result"]


for model in (Doc, Shelf, Wrapped):
    model.register_api(api)


def shelf() -> Shelf:
    """Return a shelf that holds the document by its key."""
    return Shelf(
        json={
            "id": 1,
            "title": "t",
            "cover": str(KEY),
            "papers": [str(KEY)],
        }
    )


@pytest.mark.parametrize(
    "spelling",
    [str(KEY).upper(), KEY.hex, KEY.hex.upper(), f"{{{KEY}}}"],
    ids=["upper", "hex", "upper_hex", "braces"],
)
def test_a_merge_that_spells_a_uuid_key_otherwise_keeps_the_objects(
    spelling: str,
) -> None:
    """The key is the same key, so the related objects stay."""
    obj = shelf()
    cover, paper = obj.cover, obj.papers[0]
    obj.update_from_json({"cover": spelling, "papers": [spelling]})
    assert obj.cover is cover
    assert obj.papers[0] is paper


def test_a_merge_over_a_bare_key_added_in_place_reads_the_payloads_items() -> (
    None
):
    """The change made in place goes, as any other does."""
    obj = shelf()
    obj.papers[0] = str(KEY)  # type: ignore[call-overload]
    obj.update_from_json({"papers": [str(KEY)]})
    assert [type(paper) for paper in obj.papers] == [Doc]


def test_a_merge_over_a_none_added_in_place_reads_the_payloads_items() -> None:
    """A list that cannot be encoded is dropped, not kept or refused."""
    obj = shelf()
    obj.papers.append(None)  # type: ignore[arg-type]
    obj.update_from_json({"papers": [str(KEY)]})
    assert obj.papers == [Doc(id=KEY)]


def test_a_write_answered_with_a_scalar_holds_the_response() -> None:
    """The error is a shape error that holds the answer."""
    obj = shelf()
    obj.title = "x"
    with requests_mock.Mocker() as mocker:
        mocker.patch(BASE + "shelves/1/", json=7, status_code=200)
        with pytest.raises(ResponseShapeError) as caught:
            obj.save()
    assert caught.value.response is not None
    assert caught.value.response.status_code == 200


def test_a_load_answered_with_a_scalar_holds_the_response() -> None:
    """The load's error holds the answer too."""
    with requests_mock.Mocker() as mocker:
        mocker.get(BASE + "shelves/2/", json="text")
        with pytest.raises(ResponseShapeError) as caught:
            _ = Shelf(id=2).title
    assert caught.value.response is not None


def test_a_load_makes_an_object_that_a_merge_made_partial_whole() -> None:
    """A field that the server never sends does not load again and again."""
    obj = Shelf(id=3)
    obj.update_from_json({"title": "t"})
    with requests_mock.Mocker() as mocker:
        mocker.get(BASE + "shelves/3/", json={"id": 3, "title": "t"})
        for _ in range(3):
            with pytest.raises(AttributeError):
                _ = obj.cover
        assert mocker.call_count == 1


def test_a_hooks_own_shape_error_during_create_holds_the_response() -> None:
    """The creation's response is attached, since the resource may exist."""
    with requests_mock.Mocker() as mocker:
        mocker.post(BASE + "wrapped/", json={"status": "ok"})
        with pytest.raises(ResponseShapeError) as caught:
            Wrapped.create(name="a")
    assert caught.value.response is not None
    assert caught.value.response.status_code == 200
