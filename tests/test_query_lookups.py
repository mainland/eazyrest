"""Tests of how a query encodes a lookup on a field and its values."""

from __future__ import annotations

import datetime
import enum
from typing import Annotated, Any
from urllib.parse import parse_qs, urlparse

import pytest

from eazyrest import (
    API,
    FieldConversionError,
    Hyperlinked,
    JSONObject,
    json_object,
)

_API = API("https://lookups.example/")


class LookupBase(JSONObject):
    """Models served by the mocked API."""


LookupBase.register_api(_API)


class Status(enum.Enum):
    """An enum whose values differ from its names."""

    OPEN = "open"
    DONE = "done"


@json_object
class Owner(LookupBase):
    """Model keyed by an ``int``."""

    class_url = "/owners/"

    id: int


@json_object(field_map={"type_": "type", "type": "kind"})
class Task(LookupBase):
    """Model with fields of several types."""

    class_url = "/tasks/"

    id: int
    status: Status
    due: datetime.datetime
    flag: bool
    labels: list[Status]
    owner: Owner
    linked: Annotated[Owner, Hyperlinked()]
    type_: str
    type: str


def _sent(requests_mock: Any, **params: Any) -> dict[str, list[str]]:
    """Return the query string that ``Task.filter()`` sends.

    Args:
        requests_mock: The mocked transport.
        **params: Query parameters.

    Returns:
        The parsed query string.
    """
    requests_mock.get("https://lookups.example/tasks/", json=[])
    list(Task.filter(params=params))
    return parse_qs(urlparse(requests_mock.last_request.url).query)


@pytest.mark.parametrize("suffix", ["exact", "gt", "gte", "lt", "lte"])
def test_a_value_lookup_sends_an_enum_as_its_field_does(
    suffix: str, requests_mock: Any
) -> None:
    """A lookup on an enum field sends the member's value, not its name."""
    sent = _sent(requests_mock, **{f"status__{suffix}": Status.DONE})
    given = _sent(requests_mock, **{f"status__{suffix}": "done"})

    assert sent == {f"status__{suffix}": ["done"]}
    # What the field's own parameter accepts as it is, the lookup accepts.
    assert given == sent


def test_an_in_lookup_sends_each_member_as_its_value(
    requests_mock: Any,
) -> None:
    """A single member of an ``in`` lookup is sent as the field sends it."""
    sent = _sent(requests_mock, status__in=[Status.DONE])

    assert sent == {"status__in": ["done"]}


def test_a_value_lookup_sends_a_datetime_as_iso_text(
    requests_mock: Any,
) -> None:
    """A lookup on a ``datetime`` field sends ISO 8601 text."""
    when = datetime.datetime(2026, 1, 2, 3, 4, 5, tzinfo=datetime.UTC)

    sent = _sent(requests_mock, due__gte=when)

    assert sent == {"due__gte": ["2026-01-02T03:04:05+00:00"]}


def test_a_value_lookup_sends_a_bool_as_json_does(requests_mock: Any) -> None:
    """A lookup on a ``bool`` field sends ``true``, not ``True``."""
    sent = _sent(requests_mock, flag__exact=True)

    assert sent == {"flag__exact": ["true"]}


def test_a_lookup_on_a_collection_field_encodes_the_items(
    requests_mock: Any,
) -> None:
    """A lookup on a collection field takes what one of its items takes."""
    sent = _sent(requests_mock, labels__exact=Status.OPEN)

    assert sent == {"labels__exact": ["open"]}


@pytest.mark.parametrize("name", ["owner__gt", "linked__lte"])
def test_a_value_lookup_on_a_relation_builds_the_key(
    name: str, requests_mock: Any
) -> None:
    """A related object, or a key, is sent as the relation's key."""
    first = _sent(requests_mock, **{name: Owner(id=4)})
    second = _sent(requests_mock, **{name: 5})

    assert first == {name: ["4"]}
    assert second == {name: ["5"]}
    with pytest.raises(FieldConversionError, match=r"Task\.(owner|linked)"):
        _sent(requests_mock, **{name: "5"})


@pytest.mark.parametrize("name", ["status__exact", "due__lt", "flag__in"])
def test_a_value_lookup_refuses_what_its_field_refuses(
    name: str, requests_mock: Any
) -> None:
    """A value that the field would refuse is refused, naming the field."""
    requests_mock.get("https://lookups.example/tasks/", json=[])
    field = name.partition("__")[0]

    with pytest.raises(FieldConversionError, match=rf"Task\.{field}"):
        list(
            Task.filter(
                params={name: [object()] if "in" in name else object()}
            )
        )

    assert requests_mock.call_count == 0


@pytest.mark.parametrize("suffix", ["isnull", "len__gte", "iexact"])
def test_another_lookup_is_not_converted(
    suffix: str, requests_mock: Any
) -> None:
    """A lookup that does not compare values of the field is sent as given."""
    sent = _sent(requests_mock, **{f"type__{suffix}": "2024-01-01"})

    assert sent == {f"kind__{suffix}": ["2024-01-01"]}


def test_a_lookup_is_sent_under_its_fields_json_key(
    requests_mock: Any,
) -> None:
    """The field's name in a lookup is replaced by the field's JSON key."""
    sent = _sent(
        requests_mock, type___in=["a"], type__exact="b", other__in=["x"]
    )

    # A name that starts with no field is sent as given.
    assert sent == {
        "type__in": ["a"],
        "kind__exact": ["b"],
        "other__in": ["x"],
    }


def test_a_lookup_takes_the_longest_field_name(requests_mock: Any) -> None:
    """``type___isnull`` is on ``type_``, as ``type___in`` is."""
    sent = _sent(requests_mock, type___isnull="true")

    assert sent == {"type__isnull": ["true"]}
