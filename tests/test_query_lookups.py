"""Tests of how a query encodes a lookup on a field and its values."""

from __future__ import annotations

import dataclasses
import datetime
import enum
from collections.abc import Iterator, Mapping
from typing import (
    Annotated,
    Any,
    ClassVar,
    NamedTuple,
    NewType,
    Optional,
    TypedDict,
)
from urllib.parse import parse_qs, urlparse

import pytest

from eazyrest import (
    API,
    Conversion,
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


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("point__exact", Point(1, 2)),
        ("point__in", Point(1, 2)),
        ("point__range", Point(1, 2)),
        ("maybe__exact", Point(1, 2)),
        ("size__exact", Size(3)),
        ("size__in", Size(3)),
        ("point__in", [Point(1, 2), Point(3, 4)]),
        ("size__in", [Size(3), Size(4)]),
        ("point__exact", [1, 2]),
        ("fixed__exact", (1, 2)),
        ("fixed__gt", iter([1, 2])),
        ("size__exact", [3]),
    ],
)
def test_a_lookup_on_a_structured_field_refuses_a_value_to_encode(
    name: str, value: Any, requests_mock: Any
) -> None:
    """The caller encodes such a value, so it is not sent as its items."""
    requests_mock.get("https://lookups.example/placed/", json=[])

    with pytest.raises(ValueError, match=rf"{name}.*Encode the value"):
        list(Placed.filter(params={name: value}))

    assert requests_mock.call_count == 0


@pytest.mark.parametrize(
    ("name", "value", "sent"),
    [
        ("point__exact", "1,2", "1,2"),
        ("point__in", ("1-2", "3-4"), "1-2,3-4"),
        ("point__range", ["1-2", "3-4"], "1-2,3-4"),
        ("size__in", ["a", "b"], "a,b"),
    ],
)
def test_a_lookup_on_a_structured_field_sends_text_as_given(
    name: str, value: Any, sent: str, requests_mock: Any
) -> None:
    """Text that the caller encoded, alone or in a collection, is sent."""
    requests_mock.get("https://lookups.example/placed/", json=[])

    list(Placed.filter(params={name: value}))

    assert parse_qs(urlparse(requests_mock.last_request.url).query) == {
        name: [sent]
    }


def test_a_value_lookup_on_a_scalar_field_still_refuses_what_it_refuses(
    requests_mock: Any,
) -> None:
    """Only a field that writes a list or an object takes the lookup as is."""
    requests_mock.get("https://lookups.example/placed/", json=[])

    with pytest.raises(FieldConversionError, match=r"Placed\.name"):
        list(Placed.filter(name__exact=object()))

    assert requests_mock.call_count == 0


Pair = NewType("Pair", tuple[int, int])


@json_object
class Paired(LookupBase):
    """Model with a field that is a ``NewType`` of a structure."""

    class_url = "/paired/"

    id: int
    pair: Pair
    maybe: Optional[Pair]  # noqa: UP045


@pytest.mark.parametrize("field", ["pair", "maybe"])
def test_a_value_lookup_on_a_new_type_of_a_structure_is_sent_as_given(
    field: str, requests_mock: Any
) -> None:
    """A ``NewType`` writes the structure that it names."""
    requests_mock.get("https://lookups.example/paired/", json=[])

    list(Paired.filter(params={f"{field}__exact": "1,2"}))

    assert parse_qs(urlparse(requests_mock.last_request.url).query) == {
        f"{field}__exact": ["1,2"]
    }


@json_object
class Boxed(LookupBase):
    """Model whose conversions write a dataclass and a named tuple as text."""

    class_url = "/boxed/"
    conversions: ClassVar[Mapping[Any, Conversion]] = {
        Size: Conversion(
            decode=lambda value, _: Size(int(value)),
            encode=lambda value, _: str(value.width),
        ),
        Point: Conversion(
            decode=lambda value, _: Point(*map(int, value.split("-"))),
            encode=lambda value, _: f"{value.x}-{value.y}",
        ),
    }

    id: int
    size: Size
    point: Point


@pytest.mark.parametrize("suffix", ["exact", "gte"])
def test_a_value_lookup_on_a_converted_dataclass_is_encoded_by_it(
    suffix: str, requests_mock: Any
) -> None:
    """A conversion decides what the field writes, as for its own parameter."""
    requests_mock.get("https://lookups.example/boxed/", json=[])

    list(Boxed.filter(params={f"size__{suffix}": Size(3)}))

    assert parse_qs(urlparse(requests_mock.last_request.url).query) == {
        f"size__{suffix}": ["3"]
    }


def test_a_value_lookup_on_a_converted_named_tuple_is_encoded_by_it(
    requests_mock: Any,
) -> None:
    """The named tuple is one value, not the parameter repeated."""
    requests_mock.get("https://lookups.example/boxed/", json=[])

    list(Boxed.filter(params={"point__exact": Point(1, 2)}))

    assert parse_qs(urlparse(requests_mock.last_request.url).query) == {
        "point__exact": ["1-2"]
    }


def test_an_in_lookup_on_a_converted_named_tuple_encodes_each_item(
    requests_mock: Any,
) -> None:
    """Each item is one value of the field."""
    requests_mock.get("https://lookups.example/boxed/", json=[])

    list(Boxed.filter(params={"point__in": [Point(1, 2), Point(3, 4)]}))

    assert parse_qs(urlparse(requests_mock.last_request.url).query) == {
        "point__in": ["1-2,3-4"]
    }


def test_a_value_lookup_on_a_converted_field_refuses_another_class(
    requests_mock: Any,
) -> None:
    """The field's conversion refuses a value that it does not encode."""
    requests_mock.get("https://lookups.example/boxed/", json=[])

    with pytest.raises(FieldConversionError, match=r"Boxed\.size"):
        list(Boxed.filter(params={"size__exact": object()}))

    assert requests_mock.call_count == 0


class Span:
    """An iterable value that a conversion takes whole."""

    def __init__(self, start: int, end: int) -> None:
        """Hold the bounds."""
        self.start = start
        self.end = end

    def __iter__(self) -> Iterator[int]:
        """Iterate the bounds, which are not values of a field."""
        return iter((self.start, self.end))


@json_object
class Window(LookupBase):
    """Model with a field whose conversion takes an iterable whole."""

    class_url = "/windows/"
    conversions: ClassVar[Mapping[Any, Conversion]] = {
        Span: Conversion(
            decode=lambda value, _: Span(*map(int, value.split("-"))),
            encode=lambda value, _: f"{value.start}-{value.end}",
        )
    }

    id: int
    span: Span
    name: str


@pytest.mark.parametrize("suffix", ["in", "range"])
def test_a_list_lookup_refuses_a_value_that_its_field_takes_whole(
    suffix: str, requests_mock: Any
) -> None:
    """One value of the field is no collection of values, though iterable."""
    requests_mock.get("https://lookups.example/windows/", json=[])

    with pytest.raises(ValueError, match=rf"span__{suffix}.*Pass a list"):
        list(Window.filter(params={f"span__{suffix}": Span(1, 2)}))

    assert requests_mock.call_count == 0


def test_a_lookup_that_compares_no_value_of_its_field_takes_an_iterable(
    requests_mock: Any,
) -> None:
    """``span__year__in`` compares years, so its operands are the caller's."""
    requests_mock.get("https://lookups.example/windows/", json=[])

    list(Window.filter(span__year__in=Span(1, 2)))

    assert parse_qs(urlparse(requests_mock.last_request.url).query) == {
        "span__year__in": ["1,2"]
    }


@pytest.mark.parametrize("suffix", ["in", "range"])
def test_get_or_create_refuses_a_value_that_a_list_lookup_cannot_take(
    suffix: str, requests_mock: Any
) -> None:
    """The value is not read into a list, so the lookup refuses it as such."""
    requests_mock.get("https://lookups.example/windows/", json=[])
    created = requests_mock.post("https://lookups.example/windows/", json={})

    with pytest.raises(ValueError, match=rf"span__{suffix}.*Pass a list"):
        Window.get_or_create(params={"id": 1, f"span__{suffix}": Span(1, 2)})

    assert requests_mock.call_count == 0
    assert created.call_count == 0


def test_get_or_create_sends_one_value_that_its_field_takes_whole(
    requests_mock: Any,
) -> None:
    """An iterable value of a field's own conversion is one operand."""
    requests_mock.get(
        "https://lookups.example/windows/", json=[{"id": 1, "name": "x"}]
    )

    Window.get_or_create(id=1, span__exact=Span(1, 2))

    assert parse_qs(urlparse(requests_mock.last_request.url).query) == {
        "id": ["1"],
        "span__exact": ["1-2"],
    }


def test_a_list_lookup_takes_a_list_of_values_that_its_field_takes_whole(
    requests_mock: Any,
) -> None:
    """Each item of the list is one such value."""
    requests_mock.get("https://lookups.example/windows/", json=[])

    list(Window.filter(span__in=[Span(1, 2), Span(3, 4)]))

    assert parse_qs(urlparse(requests_mock.last_request.url).query) == {
        "span__in": ["1-2,3-4"]
    }


@pytest.mark.parametrize("style", ["comma", "repeat"])
@pytest.mark.parametrize(
    ("name", "items"),
    [
        ("name__in", [""]),
        ("name__in", ["", ""]),
        ("other__in", ["1", ""]),
        ("id__range", ["", "5"]),
    ],
)
def test_a_list_lookup_refuses_an_item_whose_text_is_empty(
    name: str, items: list[str], style: str, requests_mock: Any
) -> None:
    """The server would read the empty text as no filter, or as nothing."""
    requests_mock.get("https://lookups.example/windows/", json=[])
    api = RepeatAPI("https://lookups.example/") if style == "repeat" else None

    with pytest.raises(ValueError, match=rf"{name}.*text is empty"):
        list(Window.filter(api=api, params={name: items}))

    assert requests_mock.call_count == 0
