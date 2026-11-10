"""Edge cases that the other tests leave unchecked.

Each test checks a detail that a change to one line of eazyrest could break
without another test failing: a copy of a value that passes through
unconverted, a pagination default or bound, a boundary of a limit, or a refusal
that no other test reaches.
"""

from __future__ import annotations

import datetime
from collections.abc import Callable, Mapping
from typing import Any, ClassVar, Union

import pytest
import requests
from typing_extensions import TypedDict

from eazyrest import (
    API,
    Conversion,
    FieldConversionError,
    JSONObject,
    LimitExceeded,
    Page,
    PaginationError,
    follow_next_links,
    json_object,
    link_headers_page,
    next_links_page,
)
from eazyrest.api import _Budget

BASE = "https://edges.example.com/v1/"
OTHER = "https://other.example.net/items/?page=2"


class EdgeBase(JSONObject):
    """Base of the models in this module."""


EdgeBase.register_api(API(BASE))


class Window(TypedDict):
    """``TypedDict`` whose undeclared keys pass through."""

    size: int


@json_object
class Thing(EdgeBase):
    """Model with fields whose values eazyrest passes through."""

    class_url = "/things/"

    id: int
    data: Any
    bare: dict  # type: ignore[type-arg]
    either: Union[int, list]  # type: ignore[type-arg]  # noqa: UP007
    window: Window


THING_URL = BASE + "things/1/"


def _thing() -> Any:
    """Return a ``Thing`` whose values hold nested containers.

    Returns:
        The object.
    """
    return Thing(
        json={
            "id": 1,
            "data": {"a": {"b": [1]}},
            "bare": {"a": {"b": [1]}},
            "either": [[1]],
            "window": {"size": 1, "other": {"k": [1]}},
        }
    )


def _append_to_b(value: Any) -> None:
    """Change a nested list in place.

    Args:
        value: Value whose ``["a"]["b"]`` is a list.
    """
    value["a"]["b"].append(2)


CHANGES: list[tuple[str, Callable[[Any], None], Any]] = [
    ("data", _append_to_b, {"a": {"b": [1, 2]}}),
    ("bare", _append_to_b, {"a": {"b": [1, 2]}}),
    ("either", lambda value: value[0].append(2), [[1, 2]]),
    (
        "window",
        lambda value: value["other"]["k"].append(2),
        {"size": 1, "other": {"k": [1, 2]}},
    ),
]


@pytest.mark.parametrize(("field", "change", "sent"), CHANGES)
def test_a_nested_change_in_place_is_saved(
    requests_mock: Any, field: str, change: Callable[[Any], None], sent: Any
) -> None:
    """A change deep inside a value that passes through is saved.

    Args:
        field: Field whose value changes.
        change: Change made in place.
        sent: The field's JSON that ``save()`` sends.
    """
    patch = requests_mock.patch(THING_URL, status_code=204)
    thing = _thing()

    change(getattr(thing, field))
    thing.save()

    assert patch.last_request.json() == {field: sent}


@pytest.mark.parametrize(("field", "change", "sent"), CHANGES)
def test_json_does_not_show_an_unsaved_change(
    field: str, change: Callable[[Any], None], sent: Any
) -> None:
    """A change made in place is not in ``json`` before ``save()``.

    Args:
        field: Field whose value changes.
        change: Change made in place.
        sent: Ignored.
    """
    thing = _thing()
    before = _thing().json[field]

    change(getattr(thing, field))

    assert thing.json[field] == before


@pytest.mark.parametrize("field", ["data", "bare"])
def test_a_value_changed_twice_is_saved_twice(
    requests_mock: Any, field: str
) -> None:
    """A second change after a save is saved too.

    Args:
        field: Field whose value changes.
    """
    patch = requests_mock.patch(THING_URL, status_code=204)
    thing = _thing()

    value = getattr(thing, field)
    value["x"] = 1
    thing.save()
    value["y"] = 2
    thing.save()

    assert [request.json() for request in patch.request_history] == [
        {field: {"a": {"b": [1]}, "x": 1}},
        {field: {"a": {"b": [1]}, "x": 1, "y": 2}},
    ]


@pytest.mark.parametrize("field", ["data", "bare"])
def test_an_assigned_value_changed_later_is_sent_again(
    requests_mock: Any, field: str
) -> None:
    """A value assigned, changed, and assigned again is sent again.

    Args:
        field: Field that is assigned.
    """
    patch = requests_mock.patch(THING_URL, status_code=204)
    thing = _thing()

    mine: dict[str, list[int]] = {"n": [1]}
    setattr(thing, field, mine)
    thing.save()
    mine["n"].append(2)
    setattr(thing, field, mine)
    thing.save()

    assert [request.json() for request in patch.request_history] == [
        {field: {"n": [1]}},
        {field: {"n": [1, 2]}},
    ]


def test_a_body_of_exactly_the_limit_is_read(requests_mock: Any) -> None:
    """A body of exactly ``max_response_bytes`` bytes is not too large."""
    body = b'{"id": 1}'
    requests_mock.get(BASE + "x", content=body)
    api = API(BASE)
    api.max_response_bytes = len(body)

    assert api.get("x").json() == {"id": 1}


def test_a_link_to_where_a_redirect_ended_is_a_cycle(
    requests_mock: Any,
) -> None:
    """A next link to the final URL of a redirected page was already read."""
    api = API(BASE)
    requests_mock.get(
        BASE + "items/", json={"results": [1], "next": BASE + "items?page=2"}
    )
    requests_mock.get(
        BASE + "items?page=2",
        status_code=307,
        headers={"Location": BASE + "items/?page=2"},
    )
    second = requests_mock.get(
        BASE + "items/?page=2",
        json={"results": [2], "next": BASE + "items/?page=2"},
    )
    first = api.get("items/")

    with pytest.raises(PaginationError, match="already read"):
        list(follow_next_links(api, first.json(), url="items/"))

    assert second.call_count == 1


@json_object
class Shop(EdgeBase):
    """Model with a time field."""

    class_url = "/shops/"

    id: int
    opens: datetime.time


@pytest.mark.parametrize(
    "text", ["09:30:00+00:99", "09:30:00-00:60", "09:30+23:75"]
)
def test_a_time_offset_has_fewer_than_sixty_minutes(text: str) -> None:
    """A UTC offset's minutes must be under 60.

    Args:
        text: Time with an offset out of range.
    """
    shop = Shop(json={"id": 1, "opens": text})

    with pytest.raises(FieldConversionError, match="offset out of range"):
        _ = shop.opens


def test_each_part_of_a_timeout_pair_is_capped() -> None:
    """A ``(connect, read)`` pair is reduced to the remaining budget."""
    assert _Budget(None, 5.0).timeout((9.0, 9.0)) == pytest.approx(
        (5.0, 5.0), abs=0.5
    )


SESSION_SETTINGS: dict[str, Any] = {
    "headers": {"X-Key": "k"},
    "auth": ("user", "password"),
    "proxies": {"https": "http://proxy.example.com:3128"},
    "hooks": {"response": [print]},
    "params": {"format": "json"},
    "verify": "/path/ca.pem",
    "cert": "/path/client.pem",
    "stream": True,
    "trust_env": False,
    "max_redirects": 3,
}


def test_the_session_settings_are_requests_own() -> None:
    """The settings below are every setting that requests' session has."""
    assert set(SESSION_SETTINGS) | {"cookies"} == set(
        requests.Session.__attrs__
    ) - {"adapters"}


@pytest.mark.parametrize("name", sorted(SESSION_SETTINGS))
def test_reset_session_keeps_every_setting(name: str) -> None:
    """A new session keeps each setting of the one it replaces.

    Args:
        name: Setting of ``requests.Session``.
    """
    api = API("https://example.com/")
    setattr(api.session, name, SESSION_SETTINGS[name])

    api.reset_session()

    assert getattr(api.session, name) == SESSION_SETTINGS[name]


def test_a_scheme_relative_uri_is_not_a_path(requests_mock: Any) -> None:
    """``//host/path`` is not sent to the base URL's host."""
    api = API(BASE)

    with pytest.raises(requests.exceptions.MissingSchema):
        api.get("//other.example.net/x")

    assert requests_mock.call_count == 0


class Part:
    """Value whose schema has a definition of its own."""


class Whole:
    """Value whose schema has two definitions, one named as a rename."""


class DefsBase(EdgeBase):
    """Base whose conversions' schemas define the same names."""

    conversions: ClassVar[Mapping[Any, Conversion]] = {
        Part: Conversion(
            decode=lambda value, cls: value,
            encode=lambda value, cls: value,
            schema={
                "$ref": "#/$defs/Item",
                "$defs": {"Item": {"type": "integer"}},
            },
        ),
        Whole: Conversion(
            decode=lambda value, cls: value,
            encode=lambda value, cls: value,
            schema={
                "type": "array",
                "prefixItems": [
                    {"$ref": "#/$defs/Item"},
                    {"$ref": "#/$defs/Item_2"},
                ],
                "$defs": {
                    "Item": {"type": "string"},
                    "Item_2": {"type": "boolean"},
                },
            },
        ),
    }


@json_object
class Assembly(DefsBase):
    """Model with both values."""

    class_url = "/assemblies/"

    id: int
    part: Part
    whole: Whole


def _resolve(schema: Any, root: Any) -> Any:
    """Follow references to a schema's definitions.

    Args:
        schema: Schema, which may be a reference.
        root: Schema whose ``$defs`` the references name.

    Returns:
        The schema that the references lead to.
    """
    while isinstance(schema, dict) and "$ref" in schema:
        schema = root["$defs"][schema["$ref"].removeprefix("#/$defs/")]
    return schema


@pytest.mark.parametrize("mode", ["read", "write"])
def test_a_renamed_definition_does_not_take_a_siblings_name(mode: str) -> None:
    """Two of a schema's own definitions never take one new name.

    Args:
        mode: Schema mode.
    """
    schema = Assembly.json_schema(mode)  # type: ignore[arg-type]
    properties = schema["properties"]

    assert _resolve(properties["part"], schema) == {"type": "integer"}
    first, second = properties["whole"]["prefixItems"]
    assert _resolve(first, schema) == {"type": "string"}
    assert _resolve(second, schema) == {"type": "boolean"}


def test_link_headers_page_refuses_another_origin_by_default(
    requests_mock: Any,
) -> None:
    """A Link header to another origin is not followed unless allowed."""
    api = API(BASE)
    requests_mock.get(
        BASE + "items/", json=[1], headers={"Link": f'<{OTHER}>; rel="next"'}
    )
    other = requests_mock.get(OTHER, json=[2])
    page = link_headers_page(api, api.get("items/"))

    with pytest.raises(PaginationError, match="different origin"):
        page.next_page()

    assert not other.called


@pytest.mark.parametrize("build", ["next_links", "link_headers"])
def test_pages_follow_another_origin_when_allowed(
    requests_mock: Any, build: str
) -> None:
    """``allow_other_origins=True`` holds for each page navigated to.

    Args:
        build: Which page helper reads the first page.
    """
    api = API(BASE)
    api.redirect_origins = ["https://other.example.net"]
    if build == "next_links":
        requests_mock.get(
            BASE + "items/", json={"results": [1], "next": OTHER}
        )
        requests_mock.get(OTHER, json={"results": [2], "next": None})
        page = next_links_page(
            api, api.get("items/"), allow_other_origins=True
        )
    else:
        requests_mock.get(
            BASE + "items/",
            json=[1],
            headers={"Link": f'<{OTHER}>; rel="next"'},
        )
        requests_mock.get(OTHER, json=[2])
        page = link_headers_page(
            api, api.get("items/"), allow_other_origins=True
        )

    following = page.next_page()

    assert following is not None
    assert list(following) == [2]


def test_page_navigation_counts_every_page(requests_mock: Any) -> None:
    """Each page that navigation reads counts toward ``max_pages``."""
    api = API(BASE)
    api.max_pages = 3
    for number in range(1, 6):
        requests_mock.get(
            f"{BASE}items/?page={number}",
            json={
                "results": [number],
                "next": f"{BASE}items/?page={number + 1}",
            },
        )
    page: Page[Any] | None = next_links_page(api, api.get("items/?page=1"))
    for _ in range(2):
        assert page is not None
        page = page.next_page()
    assert page is not None

    with pytest.raises(LimitExceeded, match="more than 3 pages"):
        page.next_page()


def test_a_page_of_exactly_max_items_is_read(requests_mock: Any) -> None:
    """A page of exactly ``max_items`` items is within the limit."""
    api = API(BASE)
    api.max_items = 2
    requests_mock.get(BASE + "items/", json={"results": [1, 2], "next": None})

    assert list(next_links_page(api, api.get("items/"))) == [1, 2]
