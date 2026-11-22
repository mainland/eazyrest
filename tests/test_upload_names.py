"""An upload cannot write the primary key, or a field twice."""

from __future__ import annotations

from typing import Any

import pytest
import requests_mock

from eazyrest import API, JSONObject, json_object

api = API("https://upload.example/")


@json_object(read_only={"id"})
class Fixed(JSONObject):
    """A model whose key the server assigns."""

    class_url = "/fixed/"
    id: int
    name: str


@json_object(field_map={"id": "docId"}, write_map={"name": "title"})
class Renamed(JSONObject):
    """A model whose key and a field have other JSON and write names."""

    class_url = "/renamed/"
    id: int
    name: str


Fixed.register_api(api)
Renamed.register_api(api)

FILE = (None, "999")


def creations() -> list[Any]:
    """Return calls that create with an upload."""
    return [
        lambda: Fixed.create(name="a", files={"id": FILE}),
        lambda: Fixed.create(name="a", files={"pk": FILE}),
        lambda: Renamed.create(id=1, files={"docId": FILE}),
        lambda: Renamed.create(id=1, files={"id": FILE}),
        lambda: Renamed.create(id=1, files={"pk": FILE}),
        lambda: Renamed.create(name="a", files={"id": FILE}),
    ]


@pytest.mark.parametrize("call", creations())
def test_create_refuses_an_upload_that_names_the_key(call: Any) -> None:
    """The refusal comes before the request."""
    with requests_mock.Mocker() as mocker:
        with pytest.raises(TypeError, match="primary key"):
            call()
        assert mocker.call_count == 0


@pytest.mark.parametrize("name", ["id", "pk", "docId"])
def test_save_refuses_an_upload_that_names_the_key(name: str) -> None:
    """A save sends nothing when an upload names the key."""
    obj = Renamed(id=1)
    with requests_mock.Mocker() as mocker:
        with pytest.raises(TypeError, match="primary key"):
            obj.save(files={name: FILE})
        assert mocker.call_count == 0


def test_an_upload_under_a_field_that_the_form_has_is_refused() -> None:
    """Two values for one form field are not sent."""
    with requests_mock.Mocker() as mocker:
        with pytest.raises(TypeError, match="already writes"):
            Renamed.create(id=1, name="a", files={"name": FILE})
        assert mocker.call_count == 0
        obj = Renamed(json={"docId": 1, "name": "a"})
        obj.name = "b"
        with pytest.raises(TypeError, match="already writes"):
            obj.save(files={"title": FILE})
        assert mocker.call_count == 0


def test_an_upload_under_another_name_is_sent() -> None:
    """An upload that names no key and no form field is sent as before."""
    with requests_mock.Mocker() as mocker:
        mocker.post(
            "https://upload.example/renamed/", json={"docId": 1, "title": "a"}
        )
        mocker.patch("https://upload.example/renamed/1/", json={})
        Renamed.create(id=1, files={"name": FILE})
        Renamed(id=1).save(files={"attachment": (None, "x")})
    assert b'name="title"' in mocker.request_history[0].body
    assert b'name="attachment"' in mocker.request_history[1].body
