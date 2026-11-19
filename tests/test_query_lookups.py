"""Tests of how a query encodes a lookup on a field and its values."""

from __future__ import annotations

import dataclasses
import datetime
import enum
from typing import Annotated, Any, NamedTuple, Optional, TypedDict
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


class RepeatAPI(API):
    """API whose server reads a repeated lookup parameter."""

    list_lookup_style = "repeat"


def _filter(requests_mock: Any, *, api: Any = None, **params: Any) -> Any:
    """Send ``Task.filter()`` and return the query string it sent.

    Args:
        requests_mock: The mocked transport.
        api: API to send the query with, or ``None``.
        **params: Query parameters.

    Returns:
        The parsed query string.
    """
    requests_mock.get("https://lookups.example/tasks/", json=[])
    list(Task.filter(api=api, params=params))
    return parse_qs(urlparse(requests_mock.last_request.url).query)


@pytest.mark.parametrize(
    ("params", "sent"),
    [
        ({"id__in": [1, 2]}, {"id__in": ["1,2"]}),
        (
            {"status__in": [Status.OPEN, Status.DONE]},
            {"status__in": ["open,done"]},
        ),
        ({"owner__in": [Owner(id=3), 4]}, {"owner__in": ["3,4"]}),
        ({"linked__in": [Owner(id=3), 4]}, {"linked__in": ["3,4"]}),
        ({"other__in": ("a", "b")}, {"other__in": ["a,b"]}),
        ({"owner__id__in": [3, 4]}, {"owner__id__in": ["3,4"]}),
        ({"id__range": (1, 9)}, {"id__range": ["1,9"]}),
        ({"flag__in": [True, False]}, {"flag__in": ["true,false"]}),
    ],
)
def test_a_lookup_of_several_values_joins_them_with_commas(
    params: dict[str, Any], sent: dict[str, list[str]], requests_mock: Any
) -> None:
    """The default style sends one comma-separated value, as DRF reads."""
    assert _filter(requests_mock, **params) == sent


def test_a_range_lookup_encodes_its_bounds_as_its_field_does(
    requests_mock: Any,
) -> None:
    """The bounds of a range on a ``datetime`` field are ISO 8601 text."""
    start = datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)
    end = datetime.datetime(2026, 2, 1, tzinfo=datetime.UTC)

    sent = _filter(requests_mock, due__range=(start, end))

    assert sent == {
        "due__range": ["2026-01-01T00:00:00+00:00,2026-02-01T00:00:00+00:00"]
    }


@pytest.mark.parametrize(
    "params",
    [
        {"id__in": [1, 2]},
        {"owner__in": [Owner(id=3), 4]},
        {"other__in": ["a", "b"]},
    ],
)
def test_the_repeat_style_sends_the_parameter_repeated(
    params: dict[str, Any], requests_mock: Any
) -> None:
    """With ``"repeat"``, each value is a parameter of its own."""
    name = next(iter(params))
    values = ["3" if isinstance(v, Owner) else str(v) for v in params[name]]

    sent = _filter(
        requests_mock, api=RepeatAPI("https://lookups.example/"), **params
    )

    assert sent == {name: values}
    assert _filter(requests_mock, **params) != sent


def test_the_style_is_read_from_the_scoped_view_of_an_api(
    requests_mock: Any,
) -> None:
    """A query with request options reads the API's setting."""
    requests_mock.get("https://lookups.example/tasks/", json=[])
    api = RepeatAPI("https://lookups.example/")

    list(Task.filter(api=api, request_options={"timeout": 5}, id__in=[1, 2]))

    query = urlparse(requests_mock.last_request.url).query
    assert parse_qs(query) == {"id__in": ["1", "2"]}
    scoped = api._scoped({"timeout": 5})
    assert scoped.list_lookup_style == "repeat"
    list(Task.filter(request_options={"timeout": 5}, id__in=[1, 2]))
    query = urlparse(requests_mock.last_request.url).query
    assert parse_qs(query) == {"id__in": ["1,2"]}


def test_a_model_api_with_the_repeat_style_applies_to_its_queries(
    requests_mock: Any,
) -> None:
    """Without ``api=``, the model's API decides."""
    requests_mock.get("https://repeat.example/things/", json=[])

    class Other(JSONObject):
        """Model of the repeating server."""

        class_url = "/things/"

    Other.register_api(RepeatAPI("https://repeat.example/"))

    @json_object
    class Thing(Other):
        """Model with a key."""

        id: int

    list(Thing.filter(id__in=[1, 2]))

    query = urlparse(requests_mock.last_request.url).query
    assert parse_qs(query) == {"id__in": ["1", "2"]}
    assert _filter(requests_mock, id__in=[1, 2]) == {"id__in": ["1,2"]}


@pytest.mark.parametrize("style", ["Comma", "", None, 1])
def test_an_invalid_style_is_refused_when_a_query_is_encoded(
    style: Any, requests_mock: Any
) -> None:
    """The error names the setting, and nothing is sent."""
    api = API("https://lookups.example/")
    api.list_lookup_style = style
    requests_mock.get("https://lookups.example/tasks/", json=[])

    with pytest.raises(ValueError, match="list_lookup_style"):
        list(Task.filter(api=api, id=1))

    assert requests_mock.call_count == 0


@pytest.mark.parametrize("name", ["id__in", "owner__in", "other__in"])
@pytest.mark.parametrize("value", [7, "1,2", b"1", Owner(id=1), {"a": 1}])
def test_a_lookup_of_several_values_refuses_a_value_that_is_no_collection(
    name: str, value: Any, requests_mock: Any
) -> None:
    """A string, a number, a mapping, and a model are not a list of values."""
    requests_mock.get("https://lookups.example/tasks/", json=[])

    with pytest.raises(ValueError, match=rf"{name}.*Pass a list"):
        list(Task.filter(params={name: value}))

    assert requests_mock.call_count == 0


@pytest.mark.parametrize("items", [[1], [1, 2, 3], iter([1, 2, 3])])
def test_a_range_lookup_takes_exactly_two_values(
    items: Any, requests_mock: Any
) -> None:
    """A range needs a start and an end."""
    requests_mock.get("https://lookups.example/tasks/", json=[])

    with pytest.raises(ValueError, match=r"id__range.*takes two"):
        list(Task.filter(id__range=items))

    assert requests_mock.call_count == 0


def test_a_comma_in_a_value_is_refused_under_the_comma_style(
    requests_mock: Any,
) -> None:
    """The server would split the value in two."""
    requests_mock.get("https://lookups.example/tasks/", json=[])

    with pytest.raises(ValueError, match=r"other__in.*comma"):
        list(Task.filter(other__in=["a,b", "c"]))
    sent = _filter(
        requests_mock,
        api=RepeatAPI("https://lookups.example/"),
        other__in=["a,b", "c"],
    )

    assert sent == {"other__in": ["a,b", "c"]}


def test_a_field_parameter_repeats_whatever_the_style(
    requests_mock: Any,
) -> None:
    """Only a lookup of several values is joined."""
    sent = _filter(requests_mock, labels=[Status.OPEN, Status.DONE])
    other = _filter(requests_mock, labels__in=[Status.OPEN, Status.DONE])

    assert sent == {"labels": ["open", "done"]}
    assert other == {"labels__in": ["open,done"]}


@pytest.mark.parametrize("make", [set, frozenset])
def test_a_set_given_to_a_lookup_is_sent_in_sorted_order(
    make: Any, requests_mock: Any
) -> None:
    """The same query gives the same URL in every process."""
    names = make(["b", "c", "a"])
    numbers = make([10, 9, 8])

    sent = _filter(requests_mock, other__in=names, id__in=numbers)

    assert sent == {"other__in": ["a,b,c"], "id__in": ["8,9,10"]}


def test_a_lookup_reads_a_generator_once(requests_mock: Any) -> None:
    """Any iterable is a collection of values."""
    sent = _filter(requests_mock, id__in=(n for n in (1, 2)))

    assert sent == {"id__in": ["1,2"]}


def test_get_sends_a_lookup_of_several_values_joined(
    requests_mock: Any,
) -> None:
    """``get()`` encodes as ``filter()`` does."""
    requests_mock.get(
        "https://lookups.example/tasks/",
        json=[{"id": 1, "status": "open"}],
    )

    Task.get(id__in=[1, 2])

    query = urlparse(requests_mock.last_request.url).query
    assert parse_qs(query) == {"id__in": ["1,2"]}


def test_get_or_create_joins_a_lookup_and_does_not_create_with_it(
    requests_mock: Any,
) -> None:
    """The lookup is sent joined, and the creation payload omits it."""
    requests_mock.get("https://lookups.example/tasks/", json=[])
    created = requests_mock.post(
        "https://lookups.example/tasks/", json={"id": 1, "type": "x"}
    )

    Task.get_or_create(type_="x", id__in=iter([1, 2]))

    query = urlparse(requests_mock.request_history[0].url).query
    assert parse_qs(query) == {"type": ["x"], "id__in": ["1,2"]}
    assert created.last_request.json() == {"type": "x"}


class Point(NamedTuple):
    """A named tuple, which a field writes as a list."""

    x: int
    y: int


class Corner(TypedDict):
    """A typed dictionary, which a field writes as an object."""

    x: int


@dataclasses.dataclass
class Size:
    """A dataclass, which a field writes as an object."""

    width: int


@json_object
class Placed(LookupBase):
    """Model whose fields write every value as a list or an object."""

    class_url = "/placed/"

    id: int
    point: Point
    maybe: Optional[Point]  # noqa: UP045
    fixed: tuple[int, int]
    corner: Corner
    size: Size
    extra: dict[str, int]
    name: str


@pytest.mark.parametrize(
    "field", ["point", "maybe", "fixed", "corner", "size", "extra"]
)
@pytest.mark.parametrize("suffix", ["exact", "gt", "lte"])
def test_a_value_lookup_on_a_structured_field_is_sent_as_given(
    field: str, suffix: str, requests_mock: Any
) -> None:
    """Such a field's own parameter takes no value, so the caller encodes."""
    requests_mock.get("https://lookups.example/placed/", json=[])

    list(Placed.filter(params={f"{field}__{suffix}": "1,2"}))

    assert parse_qs(urlparse(requests_mock.last_request.url).query) == {
        f"{field}__{suffix}": ["1,2"]
    }


def test_an_in_lookup_on_a_structured_field_is_sent_as_given(
    requests_mock: Any,
) -> None:
    """The items of an ``in`` lookup are the caller's encodings."""
    requests_mock.get("https://lookups.example/placed/", json=[])

    list(Placed.filter(point__in=["1-2", "3-4"]))

    assert parse_qs(urlparse(requests_mock.last_request.url).query) == {
        "point__in": ["1-2,3-4"]
    }


def test_a_value_lookup_on_a_scalar_field_still_refuses_what_it_refuses(
    requests_mock: Any,
) -> None:
    """Only a field that writes a list or an object takes the lookup as is."""
    requests_mock.get("https://lookups.example/placed/", json=[])

    with pytest.raises(FieldConversionError, match=r"Placed\.name"):
        list(Placed.filter(name__exact=object()))

    assert requests_mock.call_count == 0
