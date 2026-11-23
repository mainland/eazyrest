"""A key whose JSON name holds two underscores is no get_or_create lookup."""

from __future__ import annotations

from urllib.parse import unquote, urlsplit

import pytest
import requests_mock

from eazyrest import API, JSONObject, json_object

api = API("https://underscores.example/")


@json_object(field_map={"id": "doc__id"})
class Keyed(JSONObject):
    """A model whose key is sent as doc__id."""

    class_url = "/keyed/"
    id: int


Keyed.register_api(api)


@pytest.mark.parametrize("value", [7, "7"])
def test_get_or_create_refuses_the_json_name_before_any_request(
    value: object,
) -> None:
    """The name would be read as a field and a suffix, and posts no key."""
    with requests_mock.Mocker() as mocker:
        mocker.get(requests_mock.ANY, json=[])
        with pytest.raises(ValueError, match="doc__id") as caught:
            Keyed.get_or_create(doc__id=value)
        assert type(caught.value) is ValueError
        assert mocker.call_count == 0
        with pytest.raises(ValueError, match="doc__id"):
            Keyed.get_or_create(params={"doc__id": value})
        assert mocker.call_count == 0


def test_filter_sends_the_json_name_as_given() -> None:
    """The name is an ordinary query parameter for filter()."""
    with requests_mock.Mocker() as mocker:
        mocker.get(requests_mock.ANY, json=[])
        list(Keyed.filter(doc__id=7))
    assert unquote(urlsplit(mocker.request_history[0].url).query) == (
        "doc__id=7"
    )


def test_get_or_create_creates_by_the_python_name_of_the_key() -> None:
    """The key goes in the lookup and in the POST under its JSON name."""
    with requests_mock.Mocker() as mocker:
        mocker.get(requests_mock.ANY, json=[])
        mocker.post(requests_mock.ANY, json={"doc__id": 7})
        obj, created = Keyed.get_or_create(id=7)
    assert created
    assert obj.pk == 7
    assert mocker.request_history[1].json() == {"doc__id": 7}
