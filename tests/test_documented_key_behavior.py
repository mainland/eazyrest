"""Behavior of keyed objects that the guide and the docstrings state."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
import requests_mock

from eazyrest import API, FieldConversionError, JSONObject, json_object

api = API("https://documented.example/")
TARGET = uuid.UUID("abcdef12-1234-5678-1234-567812345abc")


class Base(JSONObject):
    """Models served by the mocked API."""


Base.register_api(api)


@json_object(pk="slug")
class Slug(Base):
    """A model keyed by text, which can name no URL."""

    class_url = "/slugs/"
    slug: str
    name: str

    @classmethod
    def bulk_get_by_pks(  # type: ignore[no-untyped-def]
        cls, pks, *, api, **kwargs
    ):
        """Return an object under a key that differs in case."""
        return {"any": Slug(json={"slug": "A", "name": "hooked"})}


@json_object
class Doc(Base):
    """A model keyed by a UUID."""

    class_url = "/docs/"
    id: uuid.UUID
    name: str

    @classmethod
    def bulk_get_by_pks(  # type: ignore[no-untyped-def]
        cls, pks, *, api, **kwargs
    ):
        """Return an object whose key is spelled in uppercase."""
        return {"any": Doc(json={"id": str(TARGET).upper(), "name": "hooked"})}


@json_object
class Page(Base):
    """A model with a relation to each."""

    class_url = "/pages/"
    id: int
    doc: Doc
    slug: Slug


@pytest.mark.parametrize("key", ["", ".", ".."])
def test_a_key_that_names_no_url_is_refused_when_a_write_has_something_to_send(
    key: str,
) -> None:
    """Delete, refresh, and a save that sends something raise ValueError."""
    obj = Slug(json={"slug": key, "name": "a"})
    with requests_mock.Mocker() as mocker:
        mocker.register_uri(requests_mock.ANY, requests_mock.ANY, json={})
        obj.save()
        assert mocker.call_count == 0
        obj.name = "b"
        for call in (
            lambda: obj.url,
            obj.save,
            lambda: obj.save(full=True),
            lambda: obj.save(files={"f": (None, "x")}),
            obj.delete,
            obj.refresh,
        ):
            with pytest.raises(ValueError, match="no URL"):
                call()
        assert mocker.call_count == 0


def test_prefetch_matches_a_uuid_in_any_spelling_and_a_str_by_case() -> None:
    """The uppercase UUID is found, and the other-case text is loaded."""
    with requests_mock.Mocker() as mocker:
        mocker.get(
            "https://documented.example/pages/",
            json=[{"id": 1, "doc": str(TARGET), "slug": "a"}],
        )
        mocker.get(
            "https://documented.example/slugs/a/",
            json={"slug": "a", "name": "loaded"},
        )
        (page,) = Page.filter(prefetch=["doc", "slug"])
        assert page.doc.name == "hooked"
        assert page.slug.name == "loaded"


def test_the_pk_setter_accepts_the_same_key_and_refuses_another_type() -> None:
    """Assigning the key that an object has, in any spelling, does nothing."""
    doc = Doc(id=TARGET)
    doc.pk = str(TARGET).upper()
    doc.pk = TARGET.hex
    assert doc.pk == TARGET
    with pytest.raises(AttributeError, match="cannot change"):
        doc.pk = uuid.UUID(int=1)
    page = Page(json={"id": 1, "doc": str(TARGET), "slug": "a"})
    for value in ("7", 7.0, {"id": 1}, page):
        with pytest.raises(FieldConversionError):
            page.pk = value


def test_repr_shows_the_key_as_it_is_sent() -> None:
    """A UUID key shows its lowercase hyphenated text, loaded or not."""
    doc: Any = Doc(id=str(TARGET).upper())
    assert repr(doc) == f"<Doc id='{TARGET}'>"
    assert repr(Page(id=3)) == "<Page id=3>"
