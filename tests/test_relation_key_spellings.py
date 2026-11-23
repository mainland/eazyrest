"""A relation keeps its objects when the server spells a UUID key otherwise."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
import requests_mock

from eazyrest import API, JSONObject, json_object

api = API("https://spellings.example/")
TARGET = uuid.UUID("abcdef12-1234-5678-1234-567812345abc")
OTHER = uuid.UUID("abcdef12-1234-5678-1234-567812345abd")
LOWER = str(TARGET)
SPELLINGS = {"upper": LOWER.upper(), "compact": TARGET.hex}


@json_object
class Doc(JSONObject):
    """A related model with a UUID key and a field of its own."""

    class_url = "/docs/"
    id: uuid.UUID
    title: str


@json_object
class Shelf(JSONObject):
    """A model with one relation, a list, and a set of documents."""

    class_url = "/shelves/"
    id: int
    cover: Doc | None
    papers: list[Doc]
    docset: set[Doc]
    note: str


Doc.register_api(api)
Shelf.register_api(api)

FIELDS = ("cover", "papers", "docset")


def held(shelf: Shelf, field: str) -> Any:
    """Return the object that a field holds, or its only item."""
    value = getattr(shelf, field)
    return value if field == "cover" else next(iter(value))


def shelf_of(text: str, mode: Any = "lazy") -> Shelf:
    """Return a shelf whose JSON spells each key as given."""
    return Shelf(
        json={"id": 1, "cover": text, "papers": [text], "docset": [text]},
        write_mode=mode,
    )


def edit(doc: Doc) -> None:
    """Change a document that has not loaded, leaving the change unsaved."""
    with requests_mock.Mocker() as mocker:
        mocker.get(
            f"https://spellings.example/docs/{doc.pk}/",
            json={"id": str(doc.pk), "title": "saved"},
        )
        doc.title = "unsaved"


def one(field: str, item: Doc) -> Any:
    """Return a value of a field that holds only the given object."""
    return {"cover": item, "papers": [item], "docset": {item}}[field]


def spelled(field: str, text: str) -> Any:
    """Return the JSON that a field holds when it names one key."""
    return text if field == "cover" else [text]


@pytest.mark.parametrize("spelling", SPELLINGS.values(), ids=SPELLINGS.keys())
@pytest.mark.parametrize("field", FIELDS)
def test_assigning_a_field_its_own_value_keeps_its_objects(
    field: str, spelling: str
) -> None:
    """The objects stay, with their unsaved changes, and nothing is sent."""
    shelf = shelf_of(spelling)
    before = held(shelf, field)
    edit(before)
    setattr(shelf, field, getattr(shelf, field))
    assert held(shelf, field) is before
    with requests_mock.Mocker() as mocker:
        shelf.save()
        assert [r.url for r in mocker.request_history] == []
    assert before.title == "unsaved"


@pytest.mark.parametrize("spelling", SPELLINGS.values(), ids=SPELLINGS.keys())
@pytest.mark.parametrize("field", FIELDS)
def test_a_lazy_save_that_echoes_another_spelling_keeps_the_objects(
    field: str, spelling: str
) -> None:
    """The response repeats the key in another spelling."""
    shelf = shelf_of(str(OTHER))
    new = Doc(id=TARGET)
    setattr(shelf, field, one(field, new))
    with requests_mock.Mocker() as mocker:
        mocker.patch(
            "https://spellings.example/shelves/1/",
            json={field: spelled(field, spelling)},
        )
        shelf.save()
    assert held(shelf, field) is new


@pytest.mark.parametrize("spelling", SPELLINGS.values(), ids=SPELLINGS.keys())
@pytest.mark.parametrize("field", FIELDS)
def test_an_eager_write_that_echoes_another_spelling_keeps_the_objects(
    field: str, spelling: str
) -> None:
    """The write's response repeats the key in another spelling."""
    shelf = shelf_of(str(OTHER), "eager")
    new = Doc(id=TARGET)
    with requests_mock.Mocker() as mocker:
        mocker.patch(
            "https://spellings.example/shelves/1/",
            json={field: spelled(field, spelling)},
        )
        setattr(shelf, field, one(field, new))
    assert held(shelf, field) is new


@pytest.mark.parametrize("field", FIELDS)
def test_a_response_that_names_another_key_replaces_the_objects(
    field: str,
) -> None:
    """A different key in the response is a different object."""
    shelf = shelf_of(str(OTHER))
    new = Doc(id=TARGET)
    setattr(shelf, field, one(field, new))
    with requests_mock.Mocker() as mocker:
        mocker.patch(
            "https://spellings.example/shelves/1/",
            json={field: spelled(field, str(uuid.uuid4()))},
        )
        shelf.save()
    assert held(shelf, field) is not new
    assert held(shelf, field).pk != TARGET


@pytest.mark.parametrize("spelling", SPELLINGS.values(), ids=SPELLINGS.keys())
@pytest.mark.parametrize("field", FIELDS)
def test_a_load_that_spells_the_key_otherwise_keeps_the_objects(
    field: str, spelling: str
) -> None:
    """Loading the missing fields does not replace an unchanged relation."""
    shelf = Shelf(id=1)
    shelf.update_from_json({field: spelled(field, LOWER)})
    before = held(shelf, field)
    edit(before)
    with requests_mock.Mocker() as mocker:
        mocker.get(
            "https://spellings.example/shelves/1/",
            json={
                "id": 1,
                "cover": spelled("cover", spelling),
                "papers": spelled("papers", spelling),
                "docset": spelled("docset", spelling),
                "note": "n",
            },
        )
        assert shelf.note == "n"
    assert held(shelf, field) is before
    assert before.title == "unsaved"


def test_a_field_of_a_type_not_defined_yet_does_not_stop_a_write() -> None:
    """A write and a refresh finish, and the relation reads once defined."""

    @json_object
    class Owner(JSONObject):
        """A model with a relation to a class that is defined later."""

        class_url = "/owners/"
        id: int
        title: str
        later: LateDoc  # type: ignore[name-defined]  # noqa: F821

    Owner.register_api(api)
    owner = Owner(json={"id": 1, "title": "t", "later": LOWER})
    owner.title = "x"
    with requests_mock.Mocker() as mocker:
        mocker.patch(
            "https://spellings.example/owners/1/",
            json={"id": 1, "title": "x", "later": LOWER},
        )
        owner.save()
        assert mocker.call_count == 1
        mocker.get(
            "https://spellings.example/owners/1/",
            json={"id": 1, "title": "y", "later": LOWER.upper()},
        )
        owner.refresh()
        assert owner.title == "y"
    globals()["LateDoc"] = Doc
    try:
        assert owner.later == Doc(id=TARGET)
    finally:
        del globals()["LateDoc"]
